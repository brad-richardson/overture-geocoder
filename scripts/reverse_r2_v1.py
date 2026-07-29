#!/usr/bin/env python3
"""Bucket-range reverse reducer and binary catalog for construction-v1.

Reverse R2 consumes the per-record artifacts already emitted by the forward map:
Places ``positions`` packs and Address ``address_records`` packs. A reducer owns
an inclusive range of the shared 8-bit shuffle space, opens every selected pack
once, proves the loaded rows equal the embedded and stored directory counts, and
emits one verified ``.plrx`` shard per populated level-8 cell.

Catalog assembly requires an exact, gap-free cover of all 256 buckets. It proves
the reductions reconstruct the expected family record count, then publishes 16
fixed-width binary catalog shards plus one 688-byte root. All objects are
content-addressed; range and catalog completion markers are create-only and
written last.

The map marker is the input manifest for R2 because it preserves the association
between a parquet pack, its directory object, and its shuffle bucket. The current
construction family manifest durably publishes all those objects but flattens
that relationship, so guessing pairs from the flat object set is deliberately
forbidden.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADDRESS = _load("reverse_r2_address", "scripts/address_construction_v1.py")
PLACES = _load("reverse_r2_places", "scripts/places_construction_v1.py")
R1 = _load("reverse_r2_r1", "scripts/reverse_r1_slice_v1.py")
REVERSE = R1.REVERSE
STAGING = _load("reverse_r2_staging", "scripts/construction_staging_v1.py")
PROMOTION = _load(
    "reverse_r2_promotion", "scripts/promote_construction_slice.py"
)

RANGE_SCHEMA = "overture-reverse-bucket-range-reduction-v1"
CATALOG_SCHEMA = "overture-reverse-catalog-publication-v1"
PLAN_SCHEMA = "overture-reverse-r2-plan-v1"
SLICE_CLAIM_SCHEMA = "overture-construction-slice-claim-v1"
CATALOG_ROOT_MAGIC = b"RCAT0001"
CATALOG_SHARD_MAGIC = b"RCAS0001"
CATALOG_ROOT_HEADER = struct.Struct("<8sBBBBIiiiiQII")
CATALOG_ROOT_SHARD = struct.Struct("<Q32s")
CATALOG_SHARD_HEADER = struct.Struct("<8sBBBBI")
CATALOG_CELL_ENTRY = struct.Struct("<HBBIQI32s")
CATALOG_SHARDS = 16
CELL_LEVEL = 8
SHUFFLE_BUCKET_BITS = 8
SHUFFLE_BUCKETS = 1 << SHUFFLE_BUCKET_BITS
FAMILY_CODE = {"places": 1, "addresses": 2}
CODE_FAMILY = {value: key for key, value in FAMILY_CODE.items()}
MAX_RADIUS_M = {"places": 2_000, "addresses": 500}
MAX_U32 = (1 << 32) - 1
MAX_U64 = (1 << 64) - 1


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def validate_identity(value: Any, *, what: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or set(value) < {"key", "bytes", "sha256"}
        or not isinstance(value["key"], str)
        or not value["key"]
        or isinstance(value["bytes"], bool)
        or not isinstance(value["bytes"], int)
        or value["bytes"] < 1
        or not is_sha256(value["sha256"])
    ):
        raise ValueError(f"{what} has an invalid immutable identity")
    identity = {
        "key": value["key"],
        "bytes": value["bytes"],
        "sha256": value["sha256"],
    }
    content_md5 = value.get("content_md5")
    if content_md5 is not None:
        if (
            not isinstance(content_md5, str)
            or len(content_md5) != 32
            or any(character not in "0123456789abcdef" for character in content_md5)
        ):
            raise ValueError(f"{what} has an invalid stored-byte MD5")
        identity["content_md5"] = content_md5
    return identity


class DirectPublishedArtifactStore:
    """Create-only reverse artifacts written once into their serving namespace.

    Range and catalog completion markers deliberately stay in the request-scoped
    construction store. This store handles only immutable serving bytes, after a
    create-only slice claim has bound the destination version to the producing
    request and Overture release.
    """

    def __init__(
        self,
        *,
        destination: Any,
        version: str,
        family: str,
        request_sha256: str,
        overture_release: str,
    ):
        self.destination = destination
        self.version = PROMOTION.validate_slice_version(version)
        if family not in FAMILY_CODE or not is_sha256(request_sha256):
            raise ValueError("reverse direct publication identity is invalid")
        if not isinstance(overture_release, str) or not overture_release:
            raise ValueError("reverse direct publication requires an Overture release")
        self.family = family
        self.request_sha256 = request_sha256
        self.overture_release = overture_release
        self.family_prefix = f"{self.version}/families/{self.family}"
        self.slice_claim = self._claim_slice()

    @staticmethod
    def _content_md5(path: Path) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _expected(self, path: Path) -> dict[str, Any]:
        expected = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        if self.destination.scheme == "r2":
            expected["content_md5"] = self._content_md5(path)
        return expected

    @staticmethod
    def _matches(actual: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
        return (
            actual is not None
            and actual.get("bytes") == expected["bytes"]
            and actual.get("sha256") == expected["sha256"]
            and (
                "content_md5" not in expected
                or actual.get("content_md5") == expected["content_md5"]
            )
        )

    def _put_exact(self, source: Path, key: str) -> dict[str, Any]:
        if not source.is_file():
            raise ValueError(f"reverse artifact is not a regular file: {source}")
        expected = self._expected(source)
        existing = self.destination.identity(key)
        if existing is None:
            try:
                if self.destination.scheme == "local":
                    self.destination.copy_from_local(source, key)
                else:
                    with source.open("rb") as handle:
                        self.destination.store.upload_fileobj(
                            handle,
                            key,
                            expected["sha256"],
                            size=expected["bytes"],
                        )
            except FileExistsError:
                pass
            existing = self.destination.identity(key)
        if not self._matches(existing, expected):
            raise ValueError(
                f"existing immutable reverse artifact differs: {key}"
            )
        identity = {
            "key": key,
            "bytes": existing["bytes"],
            "sha256": existing["sha256"],
        }
        if self.destination.scheme == "r2":
            # Record the store-computed single-part ETag/MD5 that passed the
            # comparison above, not merely the locally predicted value.
            identity["content_md5"] = existing["content_md5"]
        return identity

    def verify_identity(self, value: dict[str, Any]) -> None:
        identity = validate_identity(value, what="published reverse artifact")
        expected = {
            "bytes": identity["bytes"],
            "sha256": identity["sha256"],
        }
        content_md5 = identity.get("content_md5")
        if self.destination.scheme == "r2" and content_md5 is None:
            raise ValueError(
                "published R2 reverse artifact has no store-computed content MD5"
            )
        if content_md5 is not None:
            expected["content_md5"] = content_md5
        if not self._matches(
            self.destination.identity(identity["key"]), expected
        ):
            raise ValueError(
                f"published reverse artifact differs: {identity['key']}"
            )

    def _claim_slice(self) -> dict[str, Any]:
        claim = {
            "schema": SLICE_CLAIM_SCHEMA,
            "version": self.version,
            "family": self.family,
            "request_sha256": self.request_sha256,
            "overture_release": self.overture_release,
        }
        payload = canonical_json(claim) + b"\n"
        with tempfile.NamedTemporaryFile(
            prefix=".reverse-slice-claim.", suffix=".json", delete=False
        ) as output:
            path = Path(output.name)
            output.write(payload)
        try:
            return self._put_exact(
                path, f"{self.version}/claims/{self.family}.json"
            )
        finally:
            path.unlink(missing_ok=True)

    def put_content(
        self, source: Path, prefix: str, suffix: str
    ) -> dict[str, Any]:
        logical = f"reverse/{self.family}/"
        if not prefix.startswith(logical):
            raise ValueError(
                f"reverse artifact prefix escapes its family: {prefix!r}"
            )
        relative = prefix[len(logical) :].strip("/")
        if not relative or ".." in Path(relative).parts:
            raise ValueError("reverse artifact has an unsafe serving prefix")
        digest = sha256_file(source)
        key = (
            f"{self.family_prefix}/reverse/{relative}/sha256/"
            f"{digest}{suffix}"
        )
        return self._put_exact(source, key)

    def publish_entrypoint(self, source: Path) -> dict[str, Any]:
        return self._put_exact(
            source, f"{self.family_prefix}/reverse-catalog.rcat"
        )


def verified_path(store: Any, identity: dict[str, Any], *, what: str) -> Path:
    identity = validate_identity(identity, what=what)
    path = store.path(identity["key"])
    if (
        not path.is_file()
        or path.stat().st_size != identity["bytes"]
        or sha256_file(path) != identity["sha256"]
    ):
        raise ValueError(f"{what} differs from its immutable identity")
    return path


def verify_output_identity(
    artifact_store: Any, identity: dict[str, Any], *, what: str
) -> None:
    verify = getattr(artifact_store, "verify_identity", None)
    if callable(verify):
        verify(identity)
    else:
        verified_path(artifact_store, identity, what=what)


def marker_paths(markers_dir: Path) -> list[Path]:
    paths = sorted(markers_dir.glob("*.json")) if markers_dir.is_dir() else []
    if not paths:
        raise ValueError(f"no map markers under {markers_dir}")
    return paths


def load_markers(markers_dir: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text()) for path in marker_paths(markers_dir)]


def marker_key(family: str, task_id: str) -> str:
    if family == "places":
        return PLACES.marker_key(task_id)
    if family == "addresses":
        return ADDRESS.marker_key(task_id)
    raise ValueError(f"unknown reverse family: {family!r}")


def staged_markers(
    store: Any, *, family: str, task_ids: list[str]
):
    """Yield durable map markers one at a time and evict each hydrated copy."""
    seen: set[str] = set()
    for task_id in task_ids:
        if not isinstance(task_id, str) or not task_id or task_id in seen:
            raise ValueError("reverse plan task ids are invalid or duplicated")
        seen.add(task_id)
        key = marker_key(family, task_id)
        marker = store.read_json(key)
        if marker is None:
            raise ValueError(f"durable reverse input marker is absent: {key}")
        try:
            yield marker
        finally:
            release = getattr(store, "release_marker", None)
            if callable(release):
                release(key)


def artifact_spec(family: str) -> tuple[str, str, str]:
    if family == "places":
        return (
            "positions",
            PLACES.POSITIONS_SCHEMA,
            PLACES.POSITIONS_DIRECTORY_SCHEMA,
        )
    if family == "addresses":
        return (
            "address_records",
            ADDRESS.ADDRESS_RECORDS_SCHEMA,
            ADDRESS.ADDRESS_RECORDS_DIRECTORY_SCHEMA,
        )
    raise ValueError(f"unknown reverse family: {family!r}")


def admitted_rows(marker: dict[str, Any], family: str) -> int:
    key = "admitted_features" if family == "places" else "admitted_rows"
    value = (marker.get("transform") or {}).get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"map marker carries no usable {key}")
    return value


def cell_bucket(cell: str) -> int:
    return PLACES.shuffle_bucket(
        PLACES.cell_partition_key(cell), SHUFFLE_BUCKET_BITS
    )


def validate_pack_metadata(
    pack: dict[str, Any],
    *,
    family: str,
    task_id: str,
) -> dict[str, Any]:
    _artifact_key, _schema, directory_schema = artifact_spec(family)
    bucket = pack.get("shuffle_bucket")
    if (
        isinstance(bucket, bool)
        or not isinstance(bucket, int)
        or not 0 <= bucket < SHUFFLE_BUCKETS
        or pack.get("pack_id") != bucket
    ):
        raise ValueError(f"{task_id} reverse pack has an invalid shuffle bucket")
    directory = pack.get("directory")
    if (
        not isinstance(directory, dict)
        or directory.get("schema") != directory_schema
        or directory.get("shuffle_bucket") != bucket
    ):
        raise ValueError(f"{task_id} reverse pack has no matching directory")
    cells = directory.get("cells")
    row_groups = directory.get("row_groups")
    if not isinstance(cells, list) or not cells or not isinstance(row_groups, list):
        raise ValueError(f"{task_id} reverse pack directory is structurally empty")
    cell_records = 0
    seen_cells: set[str] = set()
    for cell in cells:
        name = cell.get("partition_cell")
        records = cell.get("records")
        REVERSE.cell_yx(name)
        if (
            name in seen_cells
            or isinstance(records, bool)
            or not isinstance(records, int)
            or records < 1
            or cell_bucket(name) != bucket
        ):
            raise ValueError(f"{task_id} reverse pack directory has an invalid cell")
        seen_cells.add(name)
        cell_records += records
    directory_records = directory.get("records")
    pack_records = pack.get("records")
    if (
        isinstance(directory_records, bool)
        or not isinstance(directory_records, int)
        or directory_records < 1
        or directory_records != cell_records
        or pack_records != directory_records
        or sum(int(group.get("records", -1)) for group in row_groups)
        != directory_records
    ):
        raise ValueError(f"{task_id} reverse pack counts do not reconcile")
    return {
        "task_id": task_id,
        "pack_id": bucket,
        "shuffle_bucket": bucket,
        "records": directory_records,
        "object": validate_identity(
            pack.get("object"), what=f"{task_id} reverse pack"
        ),
        "directory_object": validate_identity(
            pack.get("directory_object"), what=f"{task_id} reverse directory"
        ),
        "directory": directory,
    }


def per_record_packs(
    markers: Any,
    *,
    family: str,
    request_sha256: str | None = None,
    bucket_start: int | None = None,
    bucket_end: int | None = None,
    retain_directory: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    artifact_key, schema, _directory_schema = artifact_spec(family)
    packs: list[dict[str, Any]] = []
    total_admitted = 0
    task_ids: set[str] = set()
    object_keys: set[str] = set()
    directory_keys: set[str] = set()
    marker_schema = (
        PLACES.MARKER_SCHEMA if family == "places" else ADDRESS.MARKER_SCHEMA
    )
    if (bucket_start is None) != (bucket_end is None):
        raise ValueError("reverse pack filtering requires both bucket bounds")
    if bucket_start is not None:
        bucket_start, bucket_end = PLACES.validate_bucket_range(
            bucket_start, bucket_end, SHUFFLE_BUCKET_BITS
        )
    for marker in markers:
        task_id = marker.get("task_id")
        if not isinstance(task_id, str) or not task_id or task_id in task_ids:
            raise ValueError("reverse input has an invalid or duplicate task id")
        if (
            marker.get("schema") != marker_schema
            or (
                request_sha256 is not None
                and marker.get("request_sha256") != request_sha256
            )
        ):
            raise ValueError(
                f"{task_id} is not a marker for this construction request"
            )
        task_ids.add(task_id)
        artifact = marker.get(artifact_key)
        if (
            not isinstance(artifact, dict)
            or artifact.get("schema") != schema
            or artifact.get("shuffle_bucket_bits") != SHUFFLE_BUCKET_BITS
            or not isinstance(artifact.get("packs"), list)
            or not artifact["packs"]
        ):
            raise ValueError(f"{task_id} carries no {schema} artifact")
        declared = artifact.get("records")
        admitted = admitted_rows(marker, family)
        validated = [
            validate_pack_metadata(pack, family=family, task_id=task_id)
            for pack in artifact["packs"]
        ]
        if (
            isinstance(declared, bool)
            or not isinstance(declared, int)
            or declared != admitted
            or sum(pack["records"] for pack in validated) != declared
        ):
            raise ValueError(f"{task_id} per-record artifact differs from admitted rows")
        for pack in validated:
            object_key = pack["object"]["key"]
            directory_key = pack["directory_object"]["key"]
            if object_key in object_keys or directory_key in directory_keys:
                raise ValueError("reverse input repeats a logical pack object")
            object_keys.add(object_key)
            directory_keys.add(directory_key)
        for pack in validated:
            if (
                bucket_start is not None
                and not bucket_start <= pack["shuffle_bucket"] <= bucket_end
            ):
                continue
            if retain_directory:
                packs.append(pack)
            else:
                packs.append(
                    {
                        key: value
                        for key, value in pack.items()
                        if key != "directory"
                    }
                )
        total_admitted += admitted
    packs.sort(key=lambda pack: (pack["shuffle_bucket"], pack["task_id"]))
    return packs, total_admitted


def build_plan(
    *,
    family: str,
    request_sha256: str,
    markers: Any,
) -> dict[str, Any]:
    packs, expected_records = per_record_packs(
        markers,
        family=family,
        request_sha256=request_sha256,
        retain_directory=False,
    )
    task_ids = sorted({pack["task_id"] for pack in packs})
    if not packs or not task_ids or expected_records < 1:
        raise ValueError("reverse plan cannot be empty")
    plan = {
        "schema": PLAN_SCHEMA,
        "family": family,
        "request_sha256": request_sha256,
        "shuffle_bucket_bits": SHUFFLE_BUCKET_BITS,
        "task_ids": task_ids,
        "expected_records": expected_records,
        "packs": packs,
    }
    return validate_plan(plan, family=family, request_sha256=request_sha256)


def validate_plan(
    value: Any,
    *,
    family: str | None = None,
    request_sha256: str | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or value.get("schema") != PLAN_SCHEMA
        or value.get("family") not in FAMILY_CODE
        or not is_sha256(value.get("request_sha256"))
        or value.get("shuffle_bucket_bits") != SHUFFLE_BUCKET_BITS
    ):
        raise ValueError("reverse plan identity is invalid")
    if family is not None and value["family"] != family:
        raise ValueError("reverse plan family differs")
    if request_sha256 is not None and value["request_sha256"] != request_sha256:
        raise ValueError("reverse plan request differs")
    task_ids = value.get("task_ids")
    expected_records = value.get("expected_records")
    packs = value.get("packs")
    if (
        not isinstance(task_ids, list)
        or not task_ids
        or task_ids != sorted(set(task_ids))
        or any(not isinstance(task_id, str) or not task_id for task_id in task_ids)
        or isinstance(expected_records, bool)
        or not isinstance(expected_records, int)
        or expected_records < 1
        or not isinstance(packs, list)
        or not packs
    ):
        raise ValueError("reverse plan contents are invalid")
    normalized = []
    object_keys: set[str] = set()
    directory_keys: set[str] = set()
    records = 0
    previous: tuple[int, str] | None = None
    for pack in packs:
        task_id = pack.get("task_id") if isinstance(pack, dict) else None
        bucket = pack.get("shuffle_bucket") if isinstance(pack, dict) else None
        count = pack.get("records") if isinstance(pack, dict) else None
        if (
            task_id not in task_ids
            or isinstance(bucket, bool)
            or not isinstance(bucket, int)
            or not 0 <= bucket < SHUFFLE_BUCKETS
            or pack.get("pack_id") != bucket
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
        ):
            raise ValueError("reverse plan carries an invalid pack")
        order = (bucket, task_id)
        if previous is not None and order < previous:
            raise ValueError("reverse plan packs are not sorted")
        previous = order
        object_identity = validate_identity(
            pack.get("object"), what="reverse plan pack"
        )
        directory_identity = validate_identity(
            pack.get("directory_object"), what="reverse plan directory"
        )
        if (
            object_identity["key"] in object_keys
            or directory_identity["key"] in directory_keys
        ):
            raise ValueError("reverse plan repeats an immutable input")
        object_keys.add(object_identity["key"])
        directory_keys.add(directory_identity["key"])
        records += count
        normalized.append(
            {
                "task_id": task_id,
                "pack_id": bucket,
                "shuffle_bucket": bucket,
                "records": count,
                "object": object_identity,
                "directory_object": directory_identity,
            }
        )
    if records != expected_records:
        raise ValueError("reverse plan pack records do not reconcile")
    return {
        "schema": PLAN_SCHEMA,
        "family": value["family"],
        "request_sha256": value["request_sha256"],
        "shuffle_bucket_bits": SHUFFLE_BUCKET_BITS,
        "task_ids": task_ids,
        "expected_records": expected_records,
        "packs": normalized,
    }


def _sql_paths(paths: list[Path]) -> str:
    return ", ".join(
        "'" + str(path).replace("'", "''") + "'" for path in paths
    )


def _cell_query(family: str, cell: str, level: int) -> str:
    if family == "places":
        longitude = "CAST(longitude * 10000000 AS BIGINT)"
        latitude = "CAST(latitude * 10000000 AS BIGINT)"
        columns = R1.PLACES_COLUMNS
    else:
        longitude, latitude = "longitude_e7", "latitude_e7"
        columns = R1.ADDRESS_COLUMNS
    leaf = REVERSE.leaf_sql(
        level, longitude_e7=longitude, latitude_e7=latitude
    )
    return (
        f"SELECT {', '.join(columns)} FROM reverse_rows "
        f"WHERE partition_cell = '{cell}' ORDER BY "
        f"translate({leaf}, '0123', '0011'), "
        f"translate({leaf}, '0123', '0101'), "
        "feature_id, source_object_index, source_row_group, source_row_index"
    )


def remaining_wall(started: float, limits: ADDRESS.Limits) -> float:
    remaining = limits.wall_seconds - (time.monotonic() - started)
    if remaining <= 0:
        raise ValueError(
            "reverse bucket-range job exhausted its whole-job wall budget"
        )
    return remaining


def bounded_limits(
    limits: ADDRESS.Limits, *, wall_seconds: float
) -> ADDRESS.Limits:
    return ADDRESS.Limits(
        max_rss_bytes=limits.max_rss_bytes,
        max_scratch_bytes=limits.max_scratch_bytes,
        max_output_bytes=limits.max_output_bytes,
        wall_seconds=wall_seconds,
        duckdb_memory_limit=limits.duckdb_memory_limit,
        duckdb_threads=limits.duckdb_threads,
        required_duckdb_version=limits.required_duckdb_version,
        allow_unpinned_duckdb=limits.allow_unpinned_duckdb,
    )


def staged_resident_guard(store: Any, limits: ADDRESS.Limits) -> None:
    resident = getattr(store, "resident_bytes", None)
    if isinstance(resident, int) and resident > limits.max_scratch_bytes:
        raise RuntimeError("reverse staged input exceeds its hard scratch cap")


def _release(store: Any, key: str) -> None:
    release = getattr(store, "release", None)
    if callable(release):
        release(key)


def range_marker_key(family: str, bucket_start: int, bucket_end: int) -> str:
    return (
        f"reverse/{family}/ranges/"
        f"{bucket_start:03d}-{bucket_end:03d}/complete.json"
    )


def reduce_bucket_range(
    *,
    family: str,
    request_sha256: str,
    markers: Any | None = None,
    plan: dict[str, Any] | None = None,
    store: Any,
    artifact_store: Any | None = None,
    bucket_start: int,
    bucket_end: int,
    scratch_root: Path,
    encoder_binary: Path,
    verifier_binary: Path,
    limits: ADDRESS.Limits | None = None,
) -> dict[str, Any]:
    import duckdb
    import pyarrow.ipc as ipc

    limits = limits or ADDRESS.Limits()
    artifact_store = artifact_store or store
    limits.validate()
    ADDRESS.require_duckdb_runtime(duckdb, limits)
    artifact_spec(family)
    if not is_sha256(request_sha256):
        raise ValueError("reverse request id is not a canonical sha256")
    job_started = time.monotonic()
    start, end = PLACES.validate_bucket_range(
        bucket_start, bucket_end, SHUFFLE_BUCKET_BITS
    )
    if (markers is None) == (plan is None):
        raise ValueError("reverse reduce requires exactly one marker set or plan")
    if plan is not None:
        admitted_plan = validate_plan(
            plan, family=family, request_sha256=request_sha256
        )
        selected = [
            pack
            for pack in admitted_plan["packs"]
            if start <= pack["shuffle_bucket"] <= end
        ]
    else:
        selected, _admitted = per_record_packs(
            markers,
            family=family,
            request_sha256=request_sha256,
            bucket_start=start,
            bucket_end=end,
        )
    base = {
        "schema": RANGE_SCHEMA,
        "family": family,
        "request_sha256": request_sha256,
        "shuffle_bucket_bits": SHUFFLE_BUCKET_BITS,
        "bucket_start": start,
        "bucket_end": end,
        "source_packs": [pack["object"] for pack in selected],
        "source_directories": [
            pack["directory_object"] for pack in selected
        ],
        "directory_records": sum(pack["records"] for pack in selected),
    }
    durable = store.read_json(range_marker_key(family, start, end))
    if durable is not None:
        if any(durable.get(key) != value for key, value in base.items()):
            raise ValueError(
                "durable reverse range marker differs from its admitted inputs"
            )
        cells = durable.get("cells")
        shards = durable.get("shards")
        if (
            not isinstance(cells, list)
            or not isinstance(shards, list)
            or durable.get("records") != durable.get("loaded_records")
            or durable.get("records") != durable.get("directory_records")
            or sum(cell.get("records", -1) for cell in cells)
            != durable.get("records")
            or [cell.get("partition_cell") for cell in cells]
            != [shard.get("partition_cell") for shard in shards]
        ):
            raise ValueError("durable reverse range marker does not reconcile")
        for shard in shards:
            verify_output_identity(
                artifact_store,
                shard.get("object"),
                what="durable reverse data shard",
            )
        return durable

    directory_counts: dict[str, int] = {}
    for pack in selected:
        remaining_wall(job_started, limits)
        directory_path = verified_path(
            store, pack["directory_object"], what="reverse input directory"
        )
        try:
            staged_resident_guard(store, limits)
            stored_directory = json.loads(directory_path.read_text())
            embedded_directory = pack.get("directory")
            if (
                embedded_directory is not None
                and stored_directory != embedded_directory
            ):
                raise ValueError("map marker embeds a different reverse directory")
            validated = validate_pack_metadata(
                {
                    **pack,
                    "directory": stored_directory,
                },
                family=family,
                task_id=pack["task_id"],
            )
        finally:
            _release(store, pack["directory_object"]["key"])
        for cell in validated["directory"]["cells"]:
            name = cell["partition_cell"]
            directory_counts[name] = (
                directory_counts.get(name, 0) + cell["records"]
            )

    if not selected:
        result = {
            **base,
            "loaded_records": 0,
            "records": 0,
            "cells": [],
            "shards": [],
            "evidence": {
                "resources": {
                    "peak_rss_bytes": 0,
                    "peak_scratch_and_output_bytes": 0,
                    "wall_seconds": time.monotonic() - job_started,
                    "observations": 0,
                    "peak_sweep_seconds": 0.0,
                },
                "encode": [],
                "verify": [],
                "output_bytes": 0,
                "duckdb": {
                    "version": duckdb.__version__,
                    "memory_limit": limits.duckdb_memory_limit,
                    "threads": limits.duckdb_threads,
                },
            },
        }
        staging_evidence = getattr(store, "evidence", None)
        if callable(staging_evidence):
            result["evidence"]["staging"] = staging_evidence()
        store.write_marker_last(range_marker_key(family, start, end), result)
        return result

    scratch_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"reverse-{family}-b{start:03d}-{end:03d}-",
        dir=scratch_root,
    ) as temporary:
        workspace = Path(temporary)
        duckdb_scratch = workspace / "duckdb-spill"
        duckdb_scratch.mkdir()
        connection = duckdb.connect(str(workspace / "reverse.duckdb"))
        connection.execute(f"SET memory_limit = '{limits.duckdb_memory_limit}'")
        connection.execute(f"SET threads = {limits.duckdb_threads}")
        connection.execute(f"SET temp_directory = '{duckdb_scratch}'")
        connection.execute(
            "SET max_temp_directory_size = "
            f"'{ADDRESS.duckdb_temp_limit(limits.max_scratch_bytes)}'"
        )
        watchdog_roots = [workspace]
        if callable(getattr(store, "release", None)):
            watchdog_roots.append(Path(store.root))
        watchdog = ADDRESS.StageWatchdog(
            watchdog_roots,
            bounded_limits(
                limits, wall_seconds=remaining_wall(job_started, limits)
            ),
            connection,
        )
        encode_evidence = []
        verify_evidence = []
        output_bytes = 0
        try:
            with watchdog:
                columns = ", ".join(
                    R1.PLACES_COLUMNS
                    if family == "places"
                    else R1.ADDRESS_COLUMNS
                )
                # Hydrate, ingest, and evict one immutable pack at a time. Keeping
                # every selected pack resident until one read_parquet call would
                # make cache use proportional to the whole range.
                for position, pack in enumerate(selected):
                    pack_path = verified_path(
                        store, pack["object"], what="reverse input pack"
                    )
                    try:
                        staged_resident_guard(store, limits)
                        action = "CREATE TABLE reverse_rows AS" if position == 0 else (
                            "INSERT INTO reverse_rows"
                        )
                        connection.execute(
                            f"{action} SELECT {columns} "
                            f"FROM read_parquet([{_sql_paths([pack_path])}])"
                        )
                    finally:
                        _release(store, pack["object"]["key"])

                loaded = int(
                    connection.execute(
                        "SELECT count(*) FROM reverse_rows"
                    ).fetchone()[0]
                )
                if loaded != base["directory_records"]:
                    raise ValueError(
                        "reverse pack COUNT(*) differs from directory records"
                    )
                observed_counts = dict(
                    connection.execute(
                        "SELECT partition_cell, count(*) FROM reverse_rows "
                        "GROUP BY partition_cell ORDER BY partition_cell"
                    ).fetchall()
                )
                if observed_counts != directory_counts:
                    raise ValueError(
                        "reverse pack per-cell counts differ from the directories"
                    )

                cells = []
                shards = []
                for cell in sorted(directory_counts):
                    count = directory_counts[cell]
                    level = REVERSE.sub_cell_level(
                        count, cell, REVERSE.DEPTH_FAMILY_BY_SERVING[family]
                    )
                    arrow = workspace / f"{family}-{cell}.arrow"
                    cell_rows = ADDRESS.write_arrow_query(
                        connection,
                        _cell_query(family, cell, level),
                        arrow,
                        65_536,
                    )
                    if cell_rows != count:
                        raise ValueError(
                            f"reverse cell {cell} rows differ from its directory"
                        )
                    with arrow.open("rb") as source:
                        actual_schema = ipc.open_stream(source).schema
                    if not actual_schema.equals(
                        R1.input_schema(family), check_metadata=False
                    ):
                        raise ValueError(
                            f"reverse cell {cell} Arrow schema differs"
                        )
                    shard = workspace / f"{family}-{cell}.plrx"
                    sidecar = workspace / f"{family}-{cell}.digest.json"
                    encode_evidence.append(
                        ADDRESS.run_bounded(
                            [
                                str(encoder_binary),
                                "--input",
                                str(arrow),
                                "--output",
                                str(shard),
                                "--family",
                                family,
                                "--cell",
                                cell,
                                "--records",
                                str(count),
                                "--digest-out",
                                str(sidecar),
                            ],
                            scratch_roots=watchdog_roots,
                            limits=bounded_limits(
                                limits,
                                wall_seconds=remaining_wall(
                                    job_started, limits
                                ),
                            ),
                        )
                    )
                    verify_evidence.append(
                        ADDRESS.run_bounded(
                            [
                                str(verifier_binary),
                                "--input",
                                str(shard),
                                "--family",
                                family,
                                "--cell",
                                cell,
                                "--records",
                                str(count),
                                "--digest",
                                str(sidecar),
                            ],
                            scratch_roots=watchdog_roots,
                            limits=bounded_limits(
                                limits,
                                wall_seconds=remaining_wall(
                                    job_started, limits
                                ),
                            ),
                        )
                    )
                    decoded = REVERSE.ReverseShard(shard.read_bytes())
                    if (
                        decoded.family != family
                        or decoded.cell != cell
                        or decoded.records != count
                        or decoded.sub_cell_level != level
                    ):
                        raise ValueError(f"reverse shard {cell} header differs")
                    size = shard.stat().st_size
                    output_bytes += size
                    if output_bytes > limits.max_output_bytes:
                        raise ValueError(
                            "reverse bucket-range output exceeds its hard cap"
                        )
                    identity = artifact_store.put_content(
                        shard, f"reverse/{family}/shards", ".plrx"
                    )
                    cells.append(
                        {
                            "partition_cell": cell,
                            "records": count,
                            "sub_cell_level": level,
                        }
                    )
                    shards.append(
                        {
                            "partition_cell": cell,
                            "records": count,
                            "sub_cell_level": level,
                            "leaves": len(decoded.leaf_ranges()),
                            "index_bytes": size - decoded.index_offset,
                            "object": identity,
                        }
                    )
                    arrow.unlink()
                    shard.unlink()
                    sidecar.unlink()
        finally:
            connection.close()

    result = {
        **base,
        "loaded_records": loaded,
        "records": sum(cell["records"] for cell in cells),
        "cells": cells,
        "shards": shards,
        "evidence": {
            "resources": watchdog.evidence(),
            "encode": encode_evidence,
            "verify": verify_evidence,
            "output_bytes": output_bytes,
            "duckdb": {
                "version": duckdb.__version__,
                "memory_limit": limits.duckdb_memory_limit,
                "threads": limits.duckdb_threads,
            },
        },
    }
    staging_evidence = getattr(store, "evidence", None)
    if callable(staging_evidence):
        result["evidence"]["staging"] = staging_evidence()
    if not (
        result["records"]
        == result["loaded_records"]
        == result["directory_records"]
    ):
        raise ValueError("reverse bucket-range output does not reconcile")
    store.write_marker_last(range_marker_key(family, start, end), result)
    return result


def catalog_shard_id(cell: str) -> int:
    REVERSE.cell_yx(cell)
    return int(cell[0], 16)


def encode_catalog_shard(
    *, family: str, shard_id: int, cells: list[dict[str, Any]]
) -> bytes:
    if family not in FAMILY_CODE or not 0 <= shard_id < CATALOG_SHARDS:
        raise ValueError("reverse catalog shard id is out of range")
    ordered = sorted(cells, key=lambda cell: int(cell["partition_cell"], 16))
    if len(ordered) > MAX_U32:
        raise ValueError("reverse catalog shard carries too many cells")
    if any(catalog_shard_id(cell["partition_cell"]) != shard_id for cell in ordered):
        raise ValueError("reverse catalog cell is assigned to the wrong shard")
    output = bytearray(
        CATALOG_SHARD_HEADER.pack(
            CATALOG_SHARD_MAGIC,
            FAMILY_CODE[family],
            CELL_LEVEL,
            shard_id,
            0,
            len(ordered),
        )
    )
    previous = None
    for cell in ordered:
        cell_id = int(cell["partition_cell"], 16)
        if previous is not None and cell_id <= previous:
            raise ValueError("reverse catalog cells are not unique and sorted")
        previous = cell_id
        identity = validate_identity(
            cell["object"], what=f"reverse shard {cell['partition_cell']}"
        )
        level = cell.get("sub_cell_level")
        records = cell.get("records")
        index_bytes = cell.get("index_bytes")
        maximum_level = REVERSE.FAMILIES[
            REVERSE.DEPTH_FAMILY_BY_SERVING[family]
        ].l_lat
        if (
            isinstance(level, bool)
            or not isinstance(level, int)
            or not 0 <= level <= maximum_level
            or isinstance(records, bool)
            or not isinstance(records, int)
            or not 0 < records <= MAX_U32
            or isinstance(index_bytes, bool)
            or not isinstance(index_bytes, int)
            or not 0 < index_bytes <= min(identity["bytes"], MAX_U32)
            or identity["bytes"] > MAX_U64
        ):
            raise ValueError("reverse catalog cell fields are out of range")
        output.extend(
            CATALOG_CELL_ENTRY.pack(
                cell_id,
                level,
                0,
                records,
                identity["bytes"],
                index_bytes,
                bytes.fromhex(identity["sha256"]),
            )
        )
    return bytes(output)


def parse_catalog_shard(data: bytes) -> dict[str, Any]:
    if len(data) < CATALOG_SHARD_HEADER.size:
        raise ValueError("reverse catalog shard is truncated")
    magic, family_code, cell_level, shard_id, flags, count = (
        CATALOG_SHARD_HEADER.unpack_from(data)
    )
    if (
        magic != CATALOG_SHARD_MAGIC
        or family_code not in CODE_FAMILY
        or cell_level != CELL_LEVEL
        or not 0 <= shard_id < CATALOG_SHARDS
        or flags != 0
        or len(data)
        != CATALOG_SHARD_HEADER.size + count * CATALOG_CELL_ENTRY.size
    ):
        raise ValueError("reverse catalog shard header is invalid")
    cells = []
    previous = None
    offset = CATALOG_SHARD_HEADER.size
    family = CODE_FAMILY[family_code]
    maximum_level = REVERSE.FAMILIES[
        REVERSE.DEPTH_FAMILY_BY_SERVING[family]
    ].l_lat
    for _ in range(count):
        cell, level, entry_flags, records, size, index_bytes, digest = (
            CATALOG_CELL_ENTRY.unpack_from(data, offset)
        )
        offset += CATALOG_CELL_ENTRY.size
        name = f"{cell:04x}"
        if (
            entry_flags != 0
            or level > maximum_level
            or records < 1
            or size < 1
            or index_bytes < 1
            or index_bytes > size
            or catalog_shard_id(name) != shard_id
            or (previous is not None and cell <= previous)
        ):
            raise ValueError("reverse catalog shard entry is invalid")
        previous = cell
        cells.append(
            {
                "partition_cell": name,
                "sub_cell_level": level,
                "records": records,
                "bytes": size,
                "index_bytes": index_bytes,
                "sha256": digest.hex(),
            }
        )
    return {
        "family": family,
        "shard_id": shard_id,
        "cells": cells,
    }


def coverage_bbox_e7(cells: list[str]) -> tuple[int, int, int, int]:
    coordinates = [REVERSE.cell_yx(cell) for cell in cells]
    ys = [value[0] for value in coordinates]
    xs = [value[1] for value in coordinates]
    return (
        min(xs) * REVERSE.LONGITUDE_E7_PER_CELL - REVERSE.LONGITUDE_E7_ORIGIN,
        min(ys) * REVERSE.LATITUDE_E7_PER_CELL - REVERSE.LATITUDE_E7_ORIGIN,
        (max(xs) + 1) * REVERSE.LONGITUDE_E7_PER_CELL
        - REVERSE.LONGITUDE_E7_ORIGIN,
        (max(ys) + 1) * REVERSE.LATITUDE_E7_PER_CELL
        - REVERSE.LATITUDE_E7_ORIGIN,
    )


def encode_catalog_root(
    *,
    family: str,
    records: int,
    cells: list[str],
    shards: list[dict[str, Any]],
) -> bytes:
    if (
        family not in FAMILY_CODE
        or len(shards) != CATALOG_SHARDS
        or not cells
        or isinstance(records, bool)
        or not isinstance(records, int)
        or not 0 < records <= MAX_U64
        or len(cells) > MAX_U32
    ):
        raise ValueError("reverse catalog root inputs are incomplete")
    bbox = coverage_bbox_e7(cells)
    output = bytearray(
        CATALOG_ROOT_HEADER.pack(
            CATALOG_ROOT_MAGIC,
            FAMILY_CODE[family],
            CELL_LEVEL,
            CATALOG_SHARDS,
            0,
            MAX_RADIUS_M[family],
            *bbox,
            records,
            len(cells),
            0,
        )
    )
    for shard_id, identity in enumerate(shards):
        identity = validate_identity(
            identity, what=f"reverse catalog shard {shard_id}"
        )
        if identity["bytes"] > MAX_U64:
            raise ValueError("reverse catalog shard identity is too large")
        output.extend(
            CATALOG_ROOT_SHARD.pack(
                identity["bytes"], bytes.fromhex(identity["sha256"])
            )
        )
    return bytes(output)


def parse_catalog_root(data: bytes) -> dict[str, Any]:
    expected = CATALOG_ROOT_HEADER.size + CATALOG_SHARDS * CATALOG_ROOT_SHARD.size
    if len(data) != expected:
        raise ValueError("reverse catalog root has the wrong size")
    (
        magic,
        family_code,
        cell_level,
        shard_count,
        flags,
        max_radius_m,
        min_lon,
        min_lat,
        max_lon,
        max_lat,
        records,
        cells,
        reserved,
    ) = CATALOG_ROOT_HEADER.unpack_from(data)
    if (
        magic != CATALOG_ROOT_MAGIC
        or family_code not in CODE_FAMILY
        or cell_level != CELL_LEVEL
        or shard_count != CATALOG_SHARDS
        or flags != 0
        or reserved != 0
        or max_radius_m != MAX_RADIUS_M[CODE_FAMILY[family_code]]
        or records < 1
        or cells < 1
        or min_lon >= max_lon
        or min_lat >= max_lat
    ):
        raise ValueError("reverse catalog root header is invalid")
    shards = []
    offset = CATALOG_ROOT_HEADER.size
    for shard_id in range(CATALOG_SHARDS):
        size, digest = CATALOG_ROOT_SHARD.unpack_from(data, offset)
        offset += CATALOG_ROOT_SHARD.size
        if size < CATALOG_SHARD_HEADER.size:
            raise ValueError("reverse catalog root carries an invalid shard")
        shards.append(
            {"shard_id": shard_id, "bytes": size, "sha256": digest.hex()}
        )
    return {
        "family": CODE_FAMILY[family_code],
        "cell_level": cell_level,
        "max_radius_m": max_radius_m,
        "bbox_e7": [min_lon, min_lat, max_lon, max_lat],
        "records": records,
        "cells": cells,
        "shards": shards,
    }


def validate_reduction_cover(
    reductions: list[dict[str, Any]],
    *,
    family: str,
    request_sha256: str,
    expected_records: int,
) -> list[dict[str, Any]]:
    if (
        family not in FAMILY_CODE
        or not is_sha256(request_sha256)
        or isinstance(expected_records, bool)
        or not isinstance(expected_records, int)
        or expected_records < 0
    ):
        raise ValueError("reverse catalog reconciliation inputs are invalid")
    ordered = sorted(reductions, key=lambda item: item.get("bucket_start", -1))
    cursor = 0
    source_pack_keys: set[str] = set()
    source_directory_keys: set[str] = set()
    cells: set[str] = set()
    total = 0
    for reduction in ordered:
        start = reduction.get("bucket_start")
        end = reduction.get("bucket_end")
        if (
            reduction.get("schema") != RANGE_SCHEMA
            or reduction.get("family") != family
            or reduction.get("request_sha256") != request_sha256
            or reduction.get("shuffle_bucket_bits") != SHUFFLE_BUCKET_BITS
            or isinstance(start, bool)
            or not isinstance(start, int)
            or start != cursor
            or isinstance(end, bool)
            or not isinstance(end, int)
            or not start <= end < SHUFFLE_BUCKETS
        ):
            raise ValueError("reverse reductions do not exactly cover bucket space")
        cursor = end + 1
        records = reduction.get("records")
        if not (
            isinstance(records, int)
            and not isinstance(records, bool)
            and records >= 0
            and records
            == reduction.get("loaded_records")
            == reduction.get("directory_records")
        ):
            raise ValueError("reverse reduction record counts do not reconcile")
        source_packs = reduction.get("source_packs")
        source_directories = reduction.get("source_directories")
        if (
            not isinstance(source_packs, list)
            or not isinstance(source_directories, list)
            or len(source_packs) != len(source_directories)
            or (not source_packs and records)
        ):
            raise ValueError("reverse reduction source identities do not reconcile")
        for source in source_packs:
            key = validate_identity(source, what="reverse source pack")["key"]
            if key in source_pack_keys:
                raise ValueError("reverse reductions consume one source pack twice")
            source_pack_keys.add(key)
        for source in source_directories:
            key = validate_identity(source, what="reverse source directory")["key"]
            if key in source_directory_keys:
                raise ValueError(
                    "reverse reductions consume one source directory twice"
                )
            source_directory_keys.add(key)
        declared = reduction.get("cells")
        shards = reduction.get("shards")
        if not isinstance(declared, list) or not isinstance(shards, list):
            raise ValueError("reverse reduction carries invalid cell lists")
        declared_by_cell: dict[str, dict[str, Any]] = {}
        for cell in declared:
            name = cell.get("partition_cell")
            if name in declared_by_cell:
                raise ValueError("reverse reduction repeats a declared cell")
            REVERSE.cell_yx(name)
            declared_by_cell[name] = cell
        shard_by_cell: dict[str, dict[str, Any]] = {}
        for shard in shards:
            cell = shard.get("partition_cell")
            if cell in shard_by_cell:
                raise ValueError("reverse reduction repeats a shard cell")
            REVERSE.cell_yx(cell)
            shard_by_cell[cell] = shard
        if set(shard_by_cell) != set(declared_by_cell):
            raise ValueError("reverse reduction shards differ from declared cells")
        cell_records = 0
        for cell, declared_cell in declared_by_cell.items():
            shard = shard_by_cell[cell]
            if (
                declared_cell.get("records") != shard.get("records")
                or declared_cell.get("sub_cell_level")
                != shard.get("sub_cell_level")
            ):
                raise ValueError(
                    "reverse reduction shard metadata differs from its cell"
                )
            shard_records = shard.get("records")
            if (
                isinstance(shard_records, bool)
                or not isinstance(shard_records, int)
                or shard_records < 1
            ):
                raise ValueError("reverse reduction cell records are invalid")
            cell_records += shard_records
        if cell_records != records:
            raise ValueError("reverse reduction cell records do not reconcile")
        for shard in shards:
            cell = shard["partition_cell"]
            if cell in cells or not start <= cell_bucket(cell) <= end:
                raise ValueError("reverse reduction repeats or misowns a cell")
            encode_catalog_shard(
                family=family,
                shard_id=catalog_shard_id(cell),
                cells=[shard],
            )
            cells.add(cell)
        total += records
    if cursor != SHUFFLE_BUCKETS or total != expected_records:
        raise ValueError(
            "reverse reductions do not reconstruct the expected family records"
        )
    return ordered


def catalog_marker_key(family: str) -> str:
    return f"reverse/{family}/catalog/complete.json"


def assemble_catalog(
    *,
    family: str,
    request_sha256: str,
    reductions: list[dict[str, Any]],
    expected_records: int,
    store: Any,
    artifact_store: Any | None = None,
    scratch_root: Path,
) -> dict[str, Any]:
    artifact_store = artifact_store or store
    reductions = validate_reduction_cover(
        reductions,
        family=family,
        request_sha256=request_sha256,
        expected_records=expected_records,
    )
    cells = [
        shard
        for reduction in reductions
        for shard in reduction.get("shards") or ()
    ]
    if not cells:
        raise ValueError("reverse catalog cannot advertise an empty family")
    durable = store.read_json(catalog_marker_key(family))
    if durable is not None:
        data_shards = [
            validate_identity(
                shard["object"], what="durable reverse data shard"
            )
            for reduction in reductions
            for shard in reduction["shards"]
        ]
        root = validate_identity(
            durable.get("root"), what="durable reverse root"
        )
        catalog_shards = [
            validate_identity(value, what="durable reverse catalog shard")
            for value in durable.get("catalog_shards") or []
        ]
        artifacts = [
            validate_identity(value, what="durable reverse artifact")
            for value in durable.get("artifacts") or []
        ]
        expected_artifacts = sorted(
            [*data_shards, *catalog_shards, root],
            key=lambda identity: identity["key"],
        )
        if (
            durable.get("schema") != CATALOG_SCHEMA
            or durable.get("family") != family
            or durable.get("request_sha256") != request_sha256
            or durable.get("records") != expected_records
            or durable.get("cells") != len(cells)
            or durable.get("bucket_ranges") != len(reductions)
            or len(catalog_shards) != CATALOG_SHARDS
            or artifacts != expected_artifacts
        ):
            raise ValueError(
                "durable reverse catalog marker does not reconcile"
            )
        slice_claim = getattr(artifact_store, "slice_claim", None)
        if slice_claim is not None and validate_identity(
            durable.get("slice_claim"), what="durable reverse slice claim"
        ) != validate_identity(slice_claim, what="reverse slice claim"):
            raise ValueError("durable reverse catalog slice claim differs")
        for identity in artifacts:
            verify_output_identity(
                artifact_store,
                identity,
                what="durable reverse catalog artifact",
            )
        return durable

    scratch_root.mkdir(parents=True, exist_ok=True)
    catalog_shards = []
    for shard_id in range(CATALOG_SHARDS):
        data = encode_catalog_shard(
            family=family,
            shard_id=shard_id,
            cells=[
                cell
                for cell in cells
                if catalog_shard_id(cell["partition_cell"]) == shard_id
            ],
        )
        parsed = parse_catalog_shard(data)
        if parsed["family"] != family or parsed["shard_id"] != shard_id:
            raise ValueError("reverse catalog shard self-check differs")
        path = scratch_root / f"{family}-{shard_id:x}.rcas"
        path.write_bytes(data)
        catalog_shards.append(
            artifact_store.put_content(
                path, f"reverse/{family}/catalog-shards", ".rcas"
            )
        )
        path.unlink(missing_ok=True)
    root_data = encode_catalog_root(
        family=family,
        records=expected_records,
        cells=[cell["partition_cell"] for cell in cells],
        shards=catalog_shards,
    )
    root = parse_catalog_root(root_data)
    if root["records"] != expected_records or root["cells"] != len(cells):
        raise ValueError("reverse catalog root self-check differs")
    root_path = scratch_root / f"{family}.rcat"
    root_path.write_bytes(root_data)
    publish_entrypoint = getattr(artifact_store, "publish_entrypoint", None)
    root_identity = (
        publish_entrypoint(root_path)
        if callable(publish_entrypoint)
        else artifact_store.put_content(
            root_path, f"reverse/{family}/catalog-root", ".rcat"
        )
    )
    root_path.unlink(missing_ok=True)
    data_shards = [
        shard["object"]
        for reduction in reductions
        for shard in reduction["shards"]
    ]
    artifacts = sorted(
        [*data_shards, *catalog_shards, root_identity],
        key=lambda identity: identity["key"],
    )
    if len({identity["key"] for identity in artifacts}) != len(artifacts):
        raise ValueError("reverse catalog publication repeats an artifact key")
    result = {
        "schema": CATALOG_SCHEMA,
        "family": family,
        "request_sha256": request_sha256,
        "records": expected_records,
        "cells": len(cells),
        "bucket_ranges": len(reductions),
        "root": root_identity,
        "catalog_shards": catalog_shards,
        "artifacts": artifacts,
    }
    slice_claim = getattr(artifact_store, "slice_claim", None)
    if slice_claim is not None:
        result["slice_claim"] = slice_claim
    store.write_marker_last(catalog_marker_key(family), result)
    return result


def _store(args: argparse.Namespace):
    local = ADDRESS.LocalObjectStore(Path(args.store_root))
    if not args.staging_root and not args.staging_bucket and not args.staging_endpoint_url:
        return local
    backend = STAGING.staging_backend(
        store_root=args.staging_root,
        bucket=args.staging_bucket,
        endpoint_url=args.staging_endpoint_url,
    )
    return STAGING.StagedObjectStore(
        local,
        backend,
        STAGING.staging_prefix(args.request_sha256, args.family),
    )


def _artifact_store(args: argparse.Namespace, store: Any) -> Any:
    destination = getattr(args, "publish_destination", None)
    version = getattr(args, "version", None)
    overture_release = getattr(args, "overture_release", None)
    supplied = [destination is not None, version is not None, overture_release is not None]
    if any(supplied) and not all(supplied):
        raise SystemExit(
            "reverse direct publication requires --publish-destination, "
            "--version, and --overture-release together"
        )
    if destination is None:
        return store
    return DirectPublishedArtifactStore(
        destination=PROMOTION.open_tree(
            destination, "--publish-destination"
        ),
        version=version,
        family=args.family,
        request_sha256=args.request_sha256,
        overture_release=overture_release,
    )


def _add_store_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--family", choices=("places", "addresses"), required=True)
    parser.add_argument("--request-sha256", required=True)
    parser.add_argument("--store-root", type=Path, required=True)
    parser.add_argument("--staging-root", type=Path, default=None)
    parser.add_argument("--staging-bucket", default=None)
    parser.add_argument("--staging-endpoint-url", default=None)
    parser.add_argument(
        "--publish-destination",
        default=None,
        help="local:<absolute-root> or r2:<bucket>; writes reverse serving "
        "artifacts directly to the claimed final slice namespace",
    )
    parser.add_argument("--version", default=None)
    parser.add_argument("--overture-release", default=None)


def _limits(args: argparse.Namespace) -> ADDRESS.Limits:
    return ADDRESS.Limits(
        max_rss_bytes=args.max_rss_bytes,
        max_scratch_bytes=args.max_scratch_bytes,
        max_output_bytes=args.max_output_bytes,
        wall_seconds=args.wall_seconds,
        duckdb_memory_limit=args.duckdb_memory_limit,
        duckdb_threads=args.duckdb_threads,
        required_duckdb_version=args.required_duckdb_version,
        allow_unpinned_duckdb=args.allow_unpinned_duckdb,
    )


def _task_ids(path: Path) -> list[str]:
    values = [line.strip() for line in path.read_text().splitlines() if line.strip()]
    if not values:
        raise SystemExit("reverse plan requires at least one task id")
    return values


def cmd_plan(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.markers_dir is not None:
        markers = load_markers(args.markers_dir)
    else:
        markers = staged_markers(
            store,
            family=args.family,
            task_ids=_task_ids(args.task_ids_file),
        )
    result = build_plan(
        family=args.family,
        request_sha256=args.request_sha256,
        markers=markers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(result) + b"\n")
    print(
        json.dumps(
            {
                "plan": str(args.output),
                "family": result["family"],
                "request_sha256": result["request_sha256"],
                "tasks": len(result["task_ids"]),
                "packs": len(result["packs"]),
                "expected_records": result["expected_records"],
            },
            sort_keys=True,
        )
    )
    return 0


def cmd_reduce(args: argparse.Namespace) -> int:
    store = _store(args)
    plan = (
        validate_plan(
            json.loads(args.plan.read_text()),
            family=args.family,
            request_sha256=args.request_sha256,
        )
        if args.plan is not None
        else None
    )
    result = reduce_bucket_range(
        family=args.family,
        request_sha256=args.request_sha256,
        markers=(
            load_markers(args.markers_dir)
            if args.markers_dir is not None
            else None
        ),
        plan=plan,
        store=store,
        artifact_store=_artifact_store(args, store),
        bucket_start=args.bucket_start,
        bucket_end=args.bucket_end,
        scratch_root=args.scratch_dir,
        encoder_binary=args.encoder_binary,
        verifier_binary=args.verifier_binary,
        limits=_limits(args),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(result) + b"\n")
    print(json.dumps(result, sort_keys=True))
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    reductions = [
        json.loads(path.read_text())
        for path in sorted(args.reductions_dir.glob("*.json"))
    ]
    if not reductions:
        raise SystemExit("reverse catalog requires reduction records")
    store = _store(args)
    artifact_store = _artifact_store(args, store)
    for reduction in reductions:
        durable = store.read_json(
            range_marker_key(
                args.family,
                reduction.get("bucket_start"),
                reduction.get("bucket_end"),
            )
        )
        if durable != reduction:
            raise SystemExit(
                "reverse reduction record differs from its durable completion marker"
            )
    result = assemble_catalog(
        family=args.family,
        request_sha256=args.request_sha256,
        reductions=reductions,
        expected_records=args.expected_records,
        store=store,
        artifact_store=artifact_store,
        scratch_root=args.scratch_dir,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical_json(result) + b"\n")
    print(json.dumps(result, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan = commands.add_parser("plan")
    _add_store_arguments(plan)
    source = plan.add_mutually_exclusive_group(required=True)
    source.add_argument("--markers-dir", type=Path)
    source.add_argument("--task-ids-file", type=Path)
    plan.add_argument("--output", type=Path, required=True)
    plan.set_defaults(func=cmd_plan)

    reduce = commands.add_parser("reduce")
    _add_store_arguments(reduce)
    source = reduce.add_mutually_exclusive_group(required=True)
    source.add_argument("--markers-dir", type=Path)
    source.add_argument("--plan", type=Path)
    reduce.add_argument("--bucket-start", type=int, required=True)
    reduce.add_argument("--bucket-end", type=int, required=True)
    reduce.add_argument("--scratch-dir", type=Path, required=True)
    reduce.add_argument("--encoder-binary", type=Path, required=True)
    reduce.add_argument("--verifier-binary", type=Path, required=True)
    reduce.add_argument("--output", type=Path, required=True)
    defaults = ADDRESS.Limits()
    reduce.add_argument(
        "--max-rss-bytes", type=int, default=defaults.max_rss_bytes
    )
    reduce.add_argument(
        "--max-scratch-bytes", type=int, default=defaults.max_scratch_bytes
    )
    reduce.add_argument(
        "--max-output-bytes", type=int, default=defaults.max_output_bytes
    )
    reduce.add_argument(
        "--wall-seconds", type=float, default=defaults.wall_seconds
    )
    reduce.add_argument(
        "--duckdb-memory-limit", default=defaults.duckdb_memory_limit
    )
    reduce.add_argument(
        "--duckdb-threads", type=int, default=defaults.duckdb_threads
    )
    reduce.add_argument(
        "--required-duckdb-version", default=defaults.required_duckdb_version
    )
    reduce.add_argument("--allow-unpinned-duckdb", action="store_true")
    reduce.set_defaults(func=cmd_reduce)

    catalog = commands.add_parser("catalog")
    _add_store_arguments(catalog)
    catalog.add_argument("--reductions-dir", type=Path, required=True)
    catalog.add_argument("--expected-records", type=int, required=True)
    catalog.add_argument("--scratch-dir", type=Path, required=True)
    catalog.add_argument("--output", type=Path, required=True)
    catalog.set_defaults(func=cmd_catalog)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
