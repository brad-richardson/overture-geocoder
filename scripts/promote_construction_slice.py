#!/usr/bin/env python3
"""Promote a completed construction-v1 slice into the worker-readable layout.

A construction-v1 finalize publishes, per family, a create-only tree under its
contract's slice namespace:

    <slice_root>families/{family}/family-manifest.json   construction-v1-family-manifest-v1
    <slice_root>families/{family}/slice-manifest.json    construction-v1-slice-manifest-v1
    <slice_root>families/{family}/objects/<sha256><ext>  the serving set
    <slice_root>families/{family}/positions|records/...  per-record packs (NOT serving)
    <markers_root>finalize/{family}.json                 written last, names the exact set

and the #107 release tooling (finalize_rebuild verify-families-only,
v2_release_manifest) consumes a `slice-YYYY-MM-DD.N/families/{family}/` tree
whose family manifest uses the `overture-global-family-manifest-v1` schema.
This tool bridges the two: `plan` derives a deterministic promotion plan from
the construction outputs, `execute` copies the serving set (R2 server-side
CopyObject within one bucket, or plain file copies for the local harness case)
and writes derived routing plus the #107 manifest last, and `verify`
independently re-lists the destination and proves the exact set.

Per-record `positions/` / `records/` packs are build-plane insurance, not
serving objects; they are never promoted.

Routing derivation (the construction family manifest carries no partition ->
object binding, so it is derived from the authoritative reduction records):

* Places (`overture-places-selective-reduce-v1` reduction records): each
  record's `partition.partition_cell` (level-4 quadkey `{y:02x}{x:02x}` cell),
  `partition.ownership.{kind,depth,prefix}` (`token-sha256-nibble-prefix-v1`:
  the first `depth` hex nibbles of the token's SHA-256 must equal `prefix`) and
  `routed_object.{key,sha256,bytes}` bind one `.plrv` object. The `.plhd` head
  shards are routed by the published head routing manifest
  (`overture-places-global-head-sharded-v2`, `shards[].{path,sha256,bytes}` and
  `shard_bits`), named by the construction family manifest's `head.manifest`
  block; routing.json carries a pointer to that copied manifest object.
* Addresses (`overture-address-selective-reduce-v1` reduction records): each
  record's `partition.{country,hash_start,hash_end}` (inclusive `route_hash`
  envelope) and `artifact.{key,sha256,bytes}` bind one OAV1ART object.

Everything fails closed: a missing finalize marker, any recorded/actual SHA-256
disagreement, a pre-existing destination object with different bytes, a routing
entry without its object, or any object-count mismatch aborts with a named
diagnosis.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import global_build_manifest as GBM  # noqa: E402  (#107 family manifest)

PLAN_SCHEMA = "promote-construction-slice-plan-v1"
PLACES_ROUTING_SCHEMA = "overture-promoted-places-routing-v1"
ADDRESS_ROUTING_SCHEMA = "overture-promoted-addresses-routing-v1"
CONSTRUCTION_FAMILY_MANIFEST_SCHEMA = "construction-v1-family-manifest-v1"
CONSTRUCTION_SLICE_MANIFEST_SCHEMA = "construction-v1-slice-manifest-v1"
FINALIZE_MARKER_SCHEMA = "overture-construction-v1-create-only-marker-v1"
HEAD_MANIFEST_SCHEMA = "overture-places-global-head-sharded-v2"
NIBBLE_OWNERSHIP_KIND = "token-sha256-nibble-prefix-v1"
FAMILIES = ("addresses", "places")

# The serving-object key each family's reduction record carries
# (construction_v1_hosted.REDUCTION_SERVING_OBJECTS).
REDUCTION_SERVING_OBJECTS = {"addresses": "artifact", "places": "routed_object"}
REDUCE_SCHEMAS = {
    "addresses": "overture-address-selective-reduce-v1",
    "places": "overture-places-selective-reduce-v1",
}

# Descriptive #107 `versions` identities for the construction-v1 serving
# formats. `format` names the artifact magics
# (crates/geocoder-construction/src/bin/*_serving_encode_v1.rs); the places
# tokenizer is the one frozen in scripts/places_unicode_tables_v1.json and the
# address normalization is the address-transform-v1 Rust transform. All three
# are CLI-overridable.
DEFAULT_VERSIONS = {
    "places": {
        "format": "PLRV0002+PLHD0002",
        "tokenizer": "nfkd-lower-stripmark-cjk-bigram-v4",
        "normalization": None,
    },
    "addresses": {
        "format": "OAV1ART",
        "tokenizer": None,
        "normalization": "address-transform-v1",
    },
}

SLICE_VERSION_RE = re.compile(r"slice-(\d{4})-(\d{2})-(\d{2})\.(\d+)")
_HEX = set("0123456789abcdef")


def canonical(value: Any) -> bytes:
    """One canonical JSON encoding for every derived artifact (matches #107)."""
    return GBM.canonical_json(value)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fail(message: str) -> "SystemExit":
    return SystemExit(f"promote-construction-slice: {message}")


def validate_slice_version(version: str) -> str:
    matched = SLICE_VERSION_RE.fullmatch(version)
    if not matched:
        raise fail(f"version must match slice-YYYY-MM-DD.N, got {version!r}")
    year, month, day, _ = matched.groups()
    try:
        datetime.date(int(year), int(month), int(day))
    except ValueError as error:
        raise fail(f"version {version!r} has an invalid calendar date") from error
    return version


def require_key(prefix: str, what: str) -> str:
    """A clean, relative, '/'-terminated key prefix; rejects traversal."""
    if not prefix or prefix.startswith("/"):
        raise fail(f"{what} must be a non-empty relative key prefix")
    normalized = prefix if prefix.endswith("/") else prefix + "/"
    if any(part in ("", ".", "..") for part in normalized.rstrip("/").split("/")):
        raise fail(f"{what} contains an empty or traversal path segment")
    return normalized


def require_identity(value: Any, what: str) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("key"), str)
        or not isinstance(value.get("sha256"), str)
        or not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
    ):
        raise fail(f"{what} is not an object identity (need key/sha256/bytes)")
    return value


def object_name(store_key: str, sha256: str, what: str) -> str:
    """The published object name for a content-addressed store key.

    Construction store keys end `.../sha256/<digest><ext>` and finalize
    publishes the basename, so the name asserts the digest; a name/digest
    disagreement means the record does not describe the published object.
    """
    name = PurePosixPath(store_key).name
    stem, _, extension = name.partition(".")
    if stem != sha256 or len(stem) != 64 or set(stem) - _HEX or not extension:
        raise fail(f"{what} key {store_key!r} is not content-addressed by its sha256")
    return name


# ---------------------------------------------------------------------------
# Stores. Both sides of a promotion use one scheme: local:<root-dir> for the
# harness case, r2:<bucket> for the credential-gated planet case (R2 -> R2
# promotion is a server-side CopyObject inside that one bucket; the serving set
# never transits this machine).


class LocalTree:
    scheme = "local"

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        if key.startswith("/") or any(
            part in ("", ".", "..") for part in key.split("/")
        ):
            raise fail(f"unsafe object key {key!r}")
        return self.root / key

    def identity(self, key: str) -> dict[str, Any] | None:
        path = self._path(key)
        if not path.is_file():
            return None
        return {"bytes": path.stat().st_size, "sha256": sha256_file(path)}

    def read_bytes(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise fail(f"missing object {key} under {self.root}")
        return path.read_bytes()

    def list_prefix(self, prefix: str) -> list[str]:
        root = self._path(prefix.rstrip("/"))
        if not root.is_dir():
            return []
        return sorted(
            path.relative_to(self.root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )

    def put_bytes_create_only(self, key: str, payload: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 'xb' is the filesystem's native create-only PUT.
        with path.open("xb") as handle:
            handle.write(payload)

    def copy_from_local(self, source_path: Path, key: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as staging:
            staging_path = Path(staging.name)
        try:
            shutil.copyfile(source_path, staging_path)
            # link(2) is atomic create-only publication: a concurrent writer
            # loses with FileExistsError instead of silently overwriting.
            os.link(staging_path, path)
        finally:
            staging_path.unlink(missing_ok=True)


class R2Tree:
    """R2 bucket via r2_verified_store's persistent boto3 client.

    `identity` is a stored-byte metadata proof (size + recorded sha256
    metadata), never a re-download; the construction publisher recorded the
    metadata (VerifiedStoreRemote.records_sha256_metadata) and CopyObject with
    MetadataDirective COPY preserves it, so the proof survives promotion.
    """

    scheme = "r2"

    def __init__(self, store: Any):
        self.store = store

    def identity(self, key: str) -> dict[str, Any] | None:
        proof = self.store.head_proof(key)
        if proof is None:
            return None
        if not proof.get("sha256_metadata"):
            raise fail(
                f"object {key} carries no recorded sha256 metadata; refusing to "
                "trust its bytes without a stored-byte proof"
            )
        return {"bytes": proof["bytes"], "sha256": proof["sha256_metadata"]}

    def read_bytes(self, key: str) -> bytes:
        try:
            size, body = self.store.open_stream(key)
        except FileNotFoundError as error:
            raise fail(f"missing object {key} in bucket {self.store.bucket}") from error
        with contextlib.closing(body):
            payload = body.read()
        if len(payload) != size:
            raise fail(f"short read for {key}")
        return payload

    def list_prefix(self, prefix: str) -> list[str]:
        return self.store.list_prefix(prefix)

    def put_bytes_create_only(self, key: str, payload: bytes) -> None:
        self.store.upload_fileobj(
            io.BytesIO(payload), key, sha256_bytes(payload), size=len(payload)
        )

    def copy_within_bucket(self, source_key: str, destination_key: str) -> None:
        # Server-side CopyObject: the object never transits this machine.
        # MetadataDirective COPY carries the producer's sha256 metadata along,
        # which is what lets identity() prove the copy afterwards.
        self.store.client.copy_object(
            Bucket=self.store.bucket,
            Key=destination_key,
            CopySource={"Bucket": self.store.bucket, "Key": source_key},
            MetadataDirective="COPY",
        )


def open_tree(spec: str, what: str) -> LocalTree | R2Tree:
    scheme, _, rest = spec.partition(":")
    if scheme == "local" and rest:
        root = Path(rest)
        if not root.is_absolute():
            raise fail(f"{what} local root must be an absolute path: {rest!r}")
        return LocalTree(root)
    if scheme == "r2" and rest:
        # Credential gate: same env contract as finalize_rebuild / the hosted
        # publication path. No credentials, no client, no writes.
        endpoint = os.environ.get("R2_ENDPOINT") or (
            f"https://{os.environ['CLOUDFLARE_ACCOUNT_ID']}.r2.cloudflarestorage.com"
            if os.environ.get("CLOUDFLARE_ACCOUNT_ID")
            else None
        )
        access = os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get(
            "R2_ACCESS_KEY_ID"
        )
        secret = os.environ.get("AWS_SECRET_ACCESS_KEY") or os.environ.get(
            "R2_SECRET_ACCESS_KEY"
        )
        if not endpoint or not access or not secret:
            raise fail(
                f"{what} {spec!r} needs R2 credentials: set R2_ENDPOINT (or "
                "CLOUDFLARE_ACCOUNT_ID) plus R2_ACCESS_KEY_ID/R2_SECRET_ACCESS_KEY "
                "(or the AWS_* names)"
            )
        os.environ.setdefault("AWS_ACCESS_KEY_ID", access)
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", secret)
        import r2_verified_store

        return R2Tree(r2_verified_store.s3_object_store(rest, endpoint))
    raise fail(f"{what} must be local:<absolute-root> or r2:<bucket>, got {spec!r}")


def copy_object(
    source: LocalTree | R2Tree,
    destination: LocalTree | R2Tree,
    source_key: str,
    destination_key: str,
) -> None:
    if isinstance(source, LocalTree) and isinstance(destination, LocalTree):
        destination.copy_from_local(source._path(source_key), destination_key)
        return
    if isinstance(source, R2Tree) and isinstance(destination, R2Tree):
        if source.store.bucket != destination.store.bucket:
            raise fail(
                "R2 promotion must stay inside one bucket (server-side copy); "
                f"source bucket {source.store.bucket!r} != destination "
                f"{destination.store.bucket!r}"
            )
        source.copy_within_bucket(source_key, destination_key)
        return
    raise fail(
        "source and destination must share a scheme (local->local or r2->r2); "
        "promotion never downloads and re-uploads the serving set"
    )


# ---------------------------------------------------------------------------
# Plan: read the construction outputs, authenticate them, derive routing.


def _load_json_object(payload: bytes, what: str) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except ValueError as error:
        raise fail(f"{what} is not valid JSON") from error
    if not isinstance(value, dict):
        raise fail(f"{what} is not a JSON object")
    return value


def _load_reductions(directory: Path, family: str) -> list[dict[str, Any]]:
    paths = sorted(Path(directory).glob("*.json"))
    if not paths:
        raise fail(f"{family} reductions dir {directory} holds no *.json records")
    reductions = []
    for path in paths:
        record = _load_json_object(path.read_bytes(), f"{family} reduction {path.name}")
        if record.get("schema") != REDUCE_SCHEMAS[family]:
            raise fail(
                f"{family} reduction {path.name} schema is "
                f"{record.get('schema')!r}, expected {REDUCE_SCHEMAS[family]!r}"
            )
        reductions.append(record)
    identifiers = [item["partition"]["id"] for item in reductions]
    if len(identifiers) != len(set(identifiers)):
        raise fail(f"{family} reduction records repeat a partition id")
    return reductions


def _places_routing(
    reductions: list[dict[str, Any]], head_block: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Cell -> routed-object table plus name -> identity for the .plrv set.

    Fails closed unless every cell's nibble-prefix subpartitions tile the
    token-SHA space exactly: a missing subpartition would silently drop every
    token whose hash lands in the gap.
    """
    by_cell: dict[str, list[tuple[int, int, str]]] = {}
    serving: dict[str, dict[str, Any]] = {}
    for record in reductions:
        partition = record["partition"]
        ownership = partition.get("ownership") or {}
        if ownership.get("kind") != NIBBLE_OWNERSHIP_KIND:
            raise fail(
                f"places partition {partition.get('id')!r} ownership kind is "
                f"{ownership.get('kind')!r}, expected {NIBBLE_OWNERSHIP_KIND!r}"
            )
        depth, prefix = ownership.get("depth"), ownership.get("prefix")
        if (
            not isinstance(depth, int)
            or not isinstance(prefix, int)
            or isinstance(depth, bool)
            or isinstance(prefix, bool)
            or depth < 0
            or not 0 <= prefix < 16**max(depth, 1)
        ):
            raise fail(
                f"places partition {partition.get('id')!r} ownership "
                "depth/prefix are not a valid nibble prefix"
            )
        cell = partition["partition_cell"]
        if len(cell) != 4 or set(cell) - _HEX:
            raise fail(f"places partition cell is malformed: {cell!r}")
        routed = require_identity(
            record.get(REDUCTION_SERVING_OBJECTS["places"]),
            f"places routed_object for partition {partition.get('id')!r}",
        )
        name = object_name(routed["key"], routed["sha256"], "places routed_object")
        if name in serving:
            raise fail(f"places routed object name repeats: {name}")
        serving[name] = {"bytes": routed["bytes"], "sha256": routed["sha256"]}
        by_cell.setdefault(cell, []).append((depth, prefix, name))

    cells: dict[str, list[list[str]]] = {}
    for cell in sorted(by_cell):
        entries = sorted(by_cell[cell])
        depth_max = max(depth for depth, _, _ in entries)
        # Exact tiling of the 16**depth_max nibble space, no overlap, no gap.
        covered = 0
        previous_end = 0
        for depth, prefix, _ in sorted(
            entries, key=lambda item: item[1] << (4 * (depth_max - item[0]))
        ):
            width = 16 ** (depth_max - depth)
            start = prefix * width
            if start != previous_end:
                raise fail(
                    f"places cell {cell} token-prefix subpartitions do not tile "
                    "the nibble space (gap or overlap)"
                )
            previous_end = start + width
            covered += width
        if covered != 16**depth_max:
            raise fail(
                f"places cell {cell} token-prefix subpartitions do not cover "
                "the nibble space"
            )
        cells[cell] = [
            ["" if depth == 0 else f"{prefix:0{depth}x}", name]
            for depth, prefix, name in entries
        ]
    routing = {
        "schema": PLACES_ROUTING_SCHEMA,
        "family": "places",
        "cell_scheme": "level-4-quadkey-yx-hex",
        "subpartition_scheme": NIBBLE_OWNERSHIP_KIND,
        "cells": cells,
        "head": head_block,
    }
    return routing, serving


def _address_routing(
    reductions: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """(country, route-hash range) -> object table for the OAV1ART set."""
    serving: dict[str, dict[str, Any]] = {}
    rows = []
    for record in reductions:
        partition = record["partition"]
        country = partition["country"]
        start, end = int(partition["hash_start"]), int(partition["hash_end"])
        if not (0 <= start <= end < 1 << 64):
            raise fail(
                f"address partition {partition.get('id')!r} route-hash range is "
                "not a valid uint64 envelope"
            )
        artifact = require_identity(
            record.get(REDUCTION_SERVING_OBJECTS["addresses"]),
            f"address artifact for partition {partition.get('id')!r}",
        )
        name = object_name(artifact["key"], artifact["sha256"], "address artifact")
        if name in serving:
            raise fail(f"address artifact name repeats: {name}")
        serving[name] = {"bytes": artifact["bytes"], "sha256": artifact["sha256"]}
        rows.append((country, start, end, name))
    rows.sort()
    for (country, start, end, _), (next_country, next_start, _, _) in zip(
        rows, rows[1:]
    ):
        if country == next_country and next_start <= end:
            raise fail(f"address route-hash ranges overlap for country {country!r}")
    routing = {
        "schema": ADDRESS_ROUTING_SCHEMA,
        "family": "addresses",
        "key_scheme": "country-route-hash-range-v1",
        "partitions": [
            {"country": country, "hash_start": start, "hash_end": end, "object": name}
            for country, start, end, name in rows
        ],
    }
    return routing, serving


def _plan_family(
    *,
    family: str,
    source: LocalTree | R2Tree,
    slice_root: str,
    markers_root: str,
    reductions_dir: Path,
    version: str,
    release: str,
    region: dict[str, Any],
    producer_commit: str,
    versions: dict[str, Any],
) -> dict[str, Any]:
    family_prefix = f"{slice_root}families/{family}/"

    manifest_bytes = source.read_bytes(f"{family_prefix}family-manifest.json")
    construction_manifest = _load_json_object(
        manifest_bytes, f"{family} construction family manifest"
    )
    slice_manifest = _load_json_object(
        source.read_bytes(f"{family_prefix}slice-manifest.json"),
        f"{family} construction slice manifest",
    )
    if construction_manifest.get("schema") != CONSTRUCTION_FAMILY_MANIFEST_SCHEMA:
        raise fail(f"{family} family manifest has the wrong schema")
    if slice_manifest.get("schema") != CONSTRUCTION_SLICE_MANIFEST_SCHEMA:
        raise fail(f"{family} slice manifest has the wrong schema")
    for manifest in (construction_manifest, slice_manifest):
        if manifest.get("family") != family:
            raise fail(f"{family} construction manifest names a different family")
    request_sha256 = construction_manifest.get("request_sha256")
    if not isinstance(request_sha256, str) or len(request_sha256) != 64:
        raise fail(f"{family} family manifest carries no request_sha256")
    if slice_manifest.get("request_sha256") != request_sha256:
        raise fail(f"{family} slice/family manifests disagree on request_sha256")
    # The slice manifest binds the family manifest's exact bytes; a substituted
    # family manifest cannot also forge this without rewriting the marker.
    if slice_manifest.get("family_manifest_sha256") != sha256_bytes(manifest_bytes):
        raise fail(f"{family} family manifest bytes do not match the slice manifest")

    # Finalize marker: written last by the construction publisher, so its
    # presence authenticates a COMPLETE slice; absence means never promoted.
    marker_key = f"{markers_root}finalize/{family}.json"
    if source.identity(marker_key) is None:
        raise fail(
            f"{family} finalize marker is missing at {marker_key}; the "
            "construction slice is incomplete or the markers root is wrong"
        )
    marker = _load_json_object(
        source.read_bytes(marker_key), f"{family} finalize marker"
    )
    if marker.get("schema") != FINALIZE_MARKER_SCHEMA:
        raise fail(f"{family} finalize marker has the wrong schema")
    if marker.get("request_sha256") != request_sha256:
        raise fail(f"{family} finalize marker belongs to a different request")
    marker_artifacts = {
        item["key"]: item
        for item in (
            require_identity(value, f"{family} marker artifact")
            for value in marker.get("artifacts") or []
        )
    }
    recorded_manifest = marker_artifacts.get(f"{family_prefix}family-manifest.json")
    if (
        recorded_manifest is None
        or recorded_manifest["sha256"] != sha256_bytes(manifest_bytes)
    ):
        raise fail(f"{family} finalize marker does not attest this family manifest")
    # The marker names the exact published set: the two manifests plus
    # object_count payload objects. Any other total is a count mismatch.
    if slice_manifest.get("object_count") != len(marker_artifacts) - 2:
        raise fail(
            f"{family} slice manifest object_count disagrees with the finalize "
            "marker's exact set"
        )

    artifacts = [
        require_identity(value, f"{family} serving artifact")
        for value in construction_manifest.get("artifacts") or []
    ]
    positions = construction_manifest.get("positions") or {}
    if len(artifacts) != slice_manifest["object_count"] - slice_manifest.get(
        "positions_object_count", 0
    ) or len(positions.get("objects") or []) != slice_manifest.get(
        "positions_object_count", 0
    ):
        raise fail(f"{family} manifest object counts do not reconcile")

    reductions = _load_reductions(reductions_dir, family)
    if family == "places":
        head = construction_manifest.get("head")
        if not isinstance(head, dict):
            raise fail("places family manifest carries no head block")
        head_manifest_name = head["manifest"]["object"]
        head_manifest_bytes = source.read_bytes(
            f"{family_prefix}objects/{head_manifest_name}"
        )
        if sha256_bytes(head_manifest_bytes) != head["manifest"]["sha256"]:
            raise fail("places head routing manifest bytes do not match its record")
        head_manifest = _load_json_object(
            head_manifest_bytes, "places head routing manifest"
        )
        if head_manifest.get("schema") != HEAD_MANIFEST_SCHEMA:
            raise fail("places head routing manifest has the wrong schema")
        shards = head_manifest.get("shards") or []
        if len(shards) != head.get("populated_shards"):
            raise fail(
                "places head routing manifest shard count differs from the "
                "family manifest's populated_shards"
            )
        if head_manifest.get("shard_bits") != head.get("shard_bits") or (
            head_manifest.get("shard_count") != head.get("shard_count")
        ):
            raise fail(
                "places head routing manifest shard geometry differs from the "
                "family manifest's head block"
            )
        head_block = {
            "schema": HEAD_MANIFEST_SCHEMA,
            "shard_bits": head_manifest["shard_bits"],
            "shard_count": head_manifest["shard_count"],
            "populated_shards": len(shards),
            "manifest_object": head_manifest_name,
        }
        routing, serving = _places_routing(reductions, head_block)
        for shard in shards:
            name = shard["path"]
            if name in serving:
                raise fail(f"places head shard name collides: {name}")
            serving[name] = {"bytes": shard["bytes"], "sha256": shard["sha256"]}
        serving[head_manifest_name] = {
            "bytes": head["manifest"]["bytes"],
            "sha256": head["manifest"]["sha256"],
        }
    else:
        routing, serving = _address_routing(reductions)

    # The construction manifest's serving set and the derived set must be the
    # SAME set, object for object: reductions/head are the routing authority,
    # the manifest is the publication authority, and a disagreement means the
    # reduction records do not describe this published slice.
    manifest_by_name: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        name = object_name(artifact["key"], artifact["sha256"], f"{family} artifact")
        if name in manifest_by_name:
            raise fail(f"{family} construction manifest repeats artifact {name}")
        manifest_by_name[name] = artifact
    if set(manifest_by_name) != set(serving):
        missing = sorted(set(serving) - set(manifest_by_name))[:5]
        extra = sorted(set(manifest_by_name) - set(serving))[:5]
        raise fail(
            f"{family} reduction records do not cover the published serving set: "
            f"missing={missing}, extra={extra}"
        )
    for name, identity in serving.items():
        recorded = manifest_by_name[name]
        if (
            recorded["sha256"] != identity["sha256"]
            or recorded["bytes"] != identity["bytes"]
        ):
            raise fail(f"{family} serving object {name} identity disagrees")

    objects = []
    for name in sorted(serving):
        source_key = f"{family_prefix}objects/{name}"
        published = marker_artifacts.get(source_key)
        if published is None:
            raise fail(f"{family} finalize marker does not list {source_key}")
        if (
            published["sha256"] != serving[name]["sha256"]
            or published["bytes"] != serving[name]["bytes"]
        ):
            raise fail(f"{family} finalize marker identity differs for {source_key}")
        objects.append(
            {
                "source_key": source_key,
                "destination_key": f"{version}/families/{family}/objects/{name}",
                "sha256": serving[name]["sha256"],
                "bytes": serving[name]["bytes"],
            }
        )

    routing_bytes = canonical(routing)
    manifest_artifacts = [
        {
            "object_key": f"families/{family}/objects/{name}",
            "bytes": serving[name]["bytes"],
            "sha256": serving[name]["sha256"],
        }
        for name in sorted(serving)
    ] + [
        {
            "object_key": f"families/{family}/routing.json",
            "bytes": len(routing_bytes),
            "sha256": sha256_bytes(routing_bytes),
        }
    ]
    family_manifest = GBM.build_family_manifest(
        family,
        lineage={
            "overture_release": release,
            # The construction request digest IS the build identity: it binds
            # the contract that produced every promoted byte.
            "build_id": request_sha256,
            "producer_commit": producer_commit,
            "producer_script": "scripts/construction_v1_hosted.py",
            "producer_version": "construction-v1",
        },
        versions=versions,
        region=region,
        artifacts=manifest_artifacts,
        generated_at=None,  # deterministic: two plan runs are byte-identical
    )
    family_manifest_bytes = canonical(family_manifest)
    return {
        "request_sha256": request_sha256,
        "slice_root": slice_root,
        "markers_root": markers_root,
        "objects": objects,
        "routing": routing,
        "routing_key": f"{version}/families/{family}/routing.json",
        "routing_sha256": sha256_bytes(routing_bytes),
        "routing_bytes": len(routing_bytes),
        "family_manifest": family_manifest,
        "family_manifest_key": f"{version}/families/{family}/family-manifest.json",
        "family_manifest_sha256": sha256_bytes(family_manifest_bytes),
        "family_manifest_bytes": len(family_manifest_bytes),
        "totals": {
            "objects": len(objects),
            "bytes": sum(item["bytes"] for item in objects),
        },
    }


def _per_family_option(values: list[str], families: list[str], what: str) -> dict[str, str]:
    """Resolve repeated `VALUE` / `family=VALUE` options to one value per family."""
    default: str | None = None
    resolved: dict[str, str] = {}
    for value in values:
        prefix, separator, rest = value.partition("=")
        if separator and prefix in FAMILIES and rest:
            resolved[prefix] = rest
        elif default is None:
            default = value
        else:
            raise fail(f"multiple default {what} values given ({default!r}, {value!r})")
    out = {}
    for family in families:
        chosen = resolved.get(family, default)
        if chosen is None:
            raise fail(f"no {what} given for family {family}")
        out[family] = chosen
    return out


def cmd_plan(args: argparse.Namespace) -> int:
    version = validate_slice_version(args.version)
    families = sorted(set(args.family))
    source = open_tree(args.source, "--source")
    slice_roots = _per_family_option(args.slice_root, families, "--slice-root")
    markers_roots = _per_family_option(args.markers_root, families, "--markers-root")
    reductions = _per_family_option(args.reductions_dir, families, "--reductions-dir")
    region = GBM.normalize_region(
        {"name": args.region_name, "bbox": args.bbox, "bbox_scope": args.bbox_scope}
    )
    plan_families = {}
    for family in families:
        versions = dict(DEFAULT_VERSIONS[family])
        if args.format_version:
            versions["format"] = args.format_version
        plan_families[family] = _plan_family(
            family=family,
            source=source,
            slice_root=require_key(slice_roots[family], f"{family} slice root"),
            markers_root=require_key(markers_roots[family], f"{family} markers root"),
            reductions_dir=Path(reductions[family]),
            version=version,
            release=args.release,
            region=region,
            producer_commit=args.producer_commit,
            versions=versions,
        )
    plan = {
        "schema": PLAN_SCHEMA,
        "version": version,
        "release": args.release,
        "source": args.source,
        "families": plan_families,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(canonical(plan))
    print(
        json.dumps(
            {
                "plan": str(output),
                "version": version,
                "families": {
                    family: value["totals"] for family, value in plan_families.items()
                },
            },
            sort_keys=True,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Execute: copy the serving set, then routing, then the #107 manifest last.


def _load_plan(path: Path) -> dict[str, Any]:
    plan = _load_json_object(Path(path).read_bytes(), "promotion plan")
    if plan.get("schema") != PLAN_SCHEMA:
        raise fail(f"plan schema must be {PLAN_SCHEMA}")
    return plan


def _routing_object_names(routing: dict[str, Any]) -> set[str]:
    if routing.get("schema") == PLACES_ROUTING_SCHEMA:
        names = {name for entries in routing["cells"].values() for _, name in entries}
        names.add(routing["head"]["manifest_object"])
        return names
    if routing.get("schema") == ADDRESS_ROUTING_SCHEMA:
        return {row["object"] for row in routing["partitions"]}
    raise fail(f"unknown routing schema {routing.get('schema')!r}")


def _derived_members(
    family: str, value: dict[str, Any]
) -> list[tuple[str, bytes, str]]:
    """The two generated destination objects, in write order (manifest LAST)."""
    routing_bytes = canonical(value["routing"])
    manifest = GBM.validate_family_manifest(value["family_manifest"])
    manifest_bytes = canonical(manifest)
    # A hand-edited plan must not survive to a write: the recorded identities
    # bind the embedded documents.
    if (
        sha256_bytes(routing_bytes) != value["routing_sha256"]
        or len(routing_bytes) != value["routing_bytes"]
        or sha256_bytes(manifest_bytes) != value["family_manifest_sha256"]
        or len(manifest_bytes) != value["family_manifest_bytes"]
    ):
        raise fail(f"{family} plan-recorded routing/manifest identities disagree")
    # Every routing entry must name a planned object (no orphan entries), and
    # every planned object must be reachable from routing (no unrouted bytes).
    # Places head shards are reachable INDIRECTLY: routing.json points at the
    # head routing manifest, and that manifest's `shards[].path` names them --
    # verify re-proves that hop against the destination's own bytes.
    planned = {
        PurePosixPath(item["destination_key"]).name for item in value["objects"]
    }
    routed = _routing_object_names(value["routing"])
    unrouted = planned - routed
    if value["routing"].get("schema") == PLACES_ROUTING_SCHEMA:
        unrouted = {name for name in unrouted if not name.endswith(".plhd")}
    if unrouted or routed - planned:
        raise fail(
            f"{family} routing table and planned object set differ: "
            f"unrouted={sorted(unrouted)[:5]}, "
            f"orphan={sorted(routed - planned)[:5]}"
        )
    return [
        (value["routing_key"], routing_bytes, value["routing_sha256"]),
        (value["family_manifest_key"], manifest_bytes, value["family_manifest_sha256"]),
    ]


def _put_derived_create_only(
    destination: LocalTree | R2Tree, key: str, payload: bytes, sha256: str
) -> str:
    existing = destination.identity(key)
    if existing is not None:
        if existing["sha256"] == sha256 and existing["bytes"] == len(payload):
            return "already-present"
        raise fail(f"destination {key} exists with different bytes; refusing")
    try:
        destination.put_bytes_create_only(key, payload)
    except FileExistsError as error:
        raise fail(f"destination {key} appeared during create-only write") from error
    verified = destination.identity(key)
    if verified is None or verified["sha256"] != sha256:
        raise fail(f"destination {key} failed post-write verification")
    return "written"


def cmd_execute(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    source = open_tree(args.source, "--source")
    destination = open_tree(args.destination, "--destination")
    report: dict[str, Any] = {}
    for family in sorted(plan["families"]):
        value = plan["families"][family]
        derived = _derived_members(family, value)
        copied = skipped = 0
        for item in value["objects"]:
            expected = {"bytes": item["bytes"], "sha256": item["sha256"]}
            # The source must still be the object the plan admitted.
            actual = source.identity(item["source_key"])
            if actual is None:
                raise fail(f"source object vanished: {item['source_key']}")
            if actual != expected:
                raise fail(
                    f"source object {item['source_key']} does not match the "
                    f"planned identity (sha/bytes changed)"
                )
            existing = destination.identity(item["destination_key"])
            if existing is not None:
                # Byte-identical is a resume; anything else is a squatter.
                if existing == expected:
                    skipped += 1
                    continue
                raise fail(
                    f"destination {item['destination_key']} exists with "
                    "different bytes; refusing to overwrite"
                )
            try:
                copy_object(
                    source, destination, item["source_key"], item["destination_key"]
                )
            except FileExistsError as error:
                raise fail(
                    f"destination {item['destination_key']} appeared during the "
                    "create-only copy"
                ) from error
            verified = destination.identity(item["destination_key"])
            if verified != expected:
                raise fail(
                    f"destination {item['destination_key']} failed its "
                    "post-copy identity proof"
                )
            copied += 1
        # Routing first, manifest STRICTLY last: a present #107 manifest must
        # always attest already-present data.
        states = [
            _put_derived_create_only(destination, key, payload, sha256)
            for key, payload, sha256 in derived
        ]
        report[family] = {
            "copied": copied,
            "already_present": skipped,
            "routing": states[0],
            "family_manifest": states[1],
        }
    print(json.dumps({"executed": report, "version": plan["version"]}, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------
# Verify: independent re-listing and exact-set proof of the destination.


def cmd_verify(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    destination = open_tree(args.destination, "--destination")
    version = plan["version"]
    report: dict[str, Any] = {}
    for family in sorted(plan["families"]):
        value = plan["families"][family]
        prefix = f"{version}/families/{family}/"
        expected = {
            item["destination_key"]: {"bytes": item["bytes"], "sha256": item["sha256"]}
            for item in value["objects"]
        }
        expected[value["routing_key"]] = {
            "bytes": value["routing_bytes"],
            "sha256": value["routing_sha256"],
        }
        expected[value["family_manifest_key"]] = {
            "bytes": value["family_manifest_bytes"],
            "sha256": value["family_manifest_sha256"],
        }
        listed = destination.list_prefix(prefix)
        if listed != sorted(expected):
            missing = sorted(set(expected) - set(listed))[:5]
            extra = sorted(set(listed) - set(expected))[:5]
            raise fail(
                f"{family} destination is not the exact planned set: "
                f"missing={missing}, unexpected={extra}"
            )
        for key in listed:
            actual = destination.identity(key)
            if actual != expected[key]:
                raise fail(f"{family} destination object {key} identity differs")
        # Routing is re-read FROM THE DESTINATION, not from the plan: this pass
        # proves what a reader will actually fetch.
        routing = _load_json_object(
            destination.read_bytes(value["routing_key"]), f"{family} routing.json"
        )
        object_keys = {
            PurePosixPath(key).name
            for key in expected
            if key not in (value["routing_key"], value["family_manifest_key"])
        }
        routed = _routing_object_names(routing)
        if not routed <= object_keys:
            raise fail(
                f"{family} routing.json refers to objects that do not exist: "
                f"{sorted(routed - object_keys)[:5]}"
            )
        if routing.get("schema") == PLACES_ROUTING_SCHEMA:
            # Second routing hop, proved from destination bytes: the head
            # routing manifest routing.json points at must itself name only
            # existing .plhd shards, and together the two hops must reach
            # every promoted object.
            head_manifest = _load_json_object(
                destination.read_bytes(
                    f"{prefix}objects/{routing['head']['manifest_object']}"
                ),
                f"{family} head routing manifest",
            )
            routed |= {shard["path"] for shard in head_manifest.get("shards") or []}
            if not routed <= object_keys:
                raise fail(
                    f"{family} head routing manifest names missing shards: "
                    f"{sorted(routed - object_keys)[:5]}"
                )
        if object_keys - routed:
            raise fail(
                f"{family} destination objects are unreachable from routing: "
                f"{sorted(object_keys - routed)[:5]}"
            )
        # The destination #107 manifest must validate self-consistently and
        # attest exactly the non-manifest objects under the family prefix.
        manifest = GBM.validate_family_manifest(
            _load_json_object(
                destination.read_bytes(value["family_manifest_key"]),
                f"{family} family manifest",
            )
        )
        attested = {
            f"{version}/{artifact['object_key']}": {
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
            for artifact in manifest["artifacts"]
        }
        non_manifest = {
            key: identity
            for key, identity in expected.items()
            if key != value["family_manifest_key"]
        }
        if attested != non_manifest:
            raise fail(f"{family} destination family manifest attests a different set")
        report[family] = {
            "objects": len(expected),
            "bytes": sum(item["bytes"] for item in expected.values()),
        }
    print(json.dumps({"verified": report, "version": version}, sort_keys=True))
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--source", required=True,
                             help="local:<root> or r2:<bucket>")
    plan_parser.add_argument("--slice-root", action="append", required=True,
                             help="construction slice namespace prefix "
                                  "(repeatable; family=prefix for per-family)")
    plan_parser.add_argument("--markers-root", action="append", required=True,
                             help="construction markers namespace prefix")
    plan_parser.add_argument("--family", action="append", required=True,
                             choices=FAMILIES)
    plan_parser.add_argument("--reductions-dir", action="append", required=True,
                             help="directory of reduction *.json records "
                                  "(repeatable; family=path for per-family)")
    plan_parser.add_argument("--version", required=True,
                             help="destination slice-YYYY-MM-DD.N version")
    plan_parser.add_argument("--release", required=True,
                             help="Overture release the slice was built from")
    plan_parser.add_argument("--region-name", default="planet")
    plan_parser.add_argument("--bbox", nargs=4, type=float,
                             default=[-180.0, -90.0, 180.0, 90.0])
    plan_parser.add_argument("--bbox-scope", default="row_group_approximate",
                             choices=sorted(GBM.BBOX_SCOPES))
    plan_parser.add_argument("--producer-commit", required=True,
                             help="commit of the producing construction run")
    plan_parser.add_argument("--format-version", default=None,
                             help="override the family format identity")
    plan_parser.add_argument("--output", required=True)
    plan_parser.set_defaults(entry=cmd_plan)

    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--plan", required=True, type=Path)
    execute_parser.add_argument("--source", required=True)
    execute_parser.add_argument("--destination", required=True)
    execute_parser.set_defaults(entry=cmd_execute)

    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--plan", required=True, type=Path)
    verify_parser.add_argument("--destination", required=True)
    verify_parser.set_defaults(entry=cmd_verify)

    args = parser.parse_args(argv)
    return args.entry(args)


if __name__ == "__main__":
    raise SystemExit(main())
