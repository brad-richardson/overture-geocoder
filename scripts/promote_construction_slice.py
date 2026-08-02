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

Live probe required before planet promotion. The R2 CopyObject leg rests on
three behaviours of Cloudflare R2 that this repo has never exercised live, and
each must be proved with a one-object probe (the PR #181 pattern: probe the
exact call against the real bucket before trusting it at fleet scale) before
any planet-scale execute:

* that `MetadataDirective: COPY` actually carries the `x-amz-meta-sha256`
  metadata onto the copy (the post-copy sha256-metadata comparison is an echo
  of whatever landed there);
* that a server-side copy of a single-PUT object yields a SINGLE-PART ETag
  equal to the content MD5 (the `content_md5` fidelity proof below fails
  closed on a dashed or non-MD5 ETag rather than passing vacuously);
* that CopyObject accepts the largest serving objects this tool moves (~209 MB
  planet Places, ~2 GiB-capped Addresses) in one call.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO

sys.path.insert(0, str(Path(__file__).resolve().parent))

import global_build_manifest as GBM  # noqa: E402  (#107 family manifest)

PLAN_SCHEMA = "promote-construction-slice-plan-v1"
# Run 30388252232: a 2 GiB single PUT completed, but R2's synchronous
# CopyObject did not acknowledge within botocore's 60-second default and the
# standard client replayed it six times before failing. One 15-minute response
# window stays inside the workflow's 45-minute bound and is scoped only to
# CopyObject; all ordinary R2 operations keep their existing timeout.
COPY_READ_TIMEOUT_SECONDS = 15 * 60
# R2 CopyObject is synchronous and the planet Address objects take tens of
# seconds each even though the bytes stay inside the bucket.  Distinct
# content-addressed destination keys are independent, so a small fixed pool
# keeps the promotion inside the workflow window without weakening any
# per-object source/destination/post-copy identity proof.  Keep this bounded:
# the dedicated copy client deliberately does not replay an ambiguous request.
COPY_WORKERS = 4
# Item 2. Three loops on the promotion critical path issue roughly 84,000
# SEQUENTIAL HEAD requests -- plan's source identity (21,279), execute's
# prepositioned verification (20,777), and verify's per-key destination identity
# (42,058). The plan loop alone was measured twice on identical input at
# 17m10s and 36m21s (48.4 and 102.5 ms/HEAD), so one of the three costs 17-36
# minutes and varies ~2x with nothing but network conditions.
#
# Every one of those keys is immutable and independent, so this is embarrassingly
# parallel. 16 matches what the finalizer already proved on this bucket for its
# publication and whole-slice verification passes; it is deliberately larger than
# COPY_WORKERS because a HEAD is not a CopyObject -- it moves no bytes and the
# ambiguous-replay hazard that keeps the copy pool small does not apply.
IDENTITY_WORKERS = 16
PLACES_ROUTING_SCHEMA = "overture-promoted-places-routing-v1"
ADDRESS_ROUTING_SCHEMA = "overture-promoted-addresses-routing-v1"
REVERSE_CATALOG_SCHEMA = "overture-reverse-catalog-publication-v1"
SLICE_CLAIM_SCHEMA = "overture-construction-slice-claim-v1"
CONSTRUCTION_FAMILY_MANIFEST_SCHEMA = "construction-v1-family-manifest-v1"
CONSTRUCTION_SLICE_MANIFEST_SCHEMA = "construction-v1-slice-manifest-v1"
FINALIZE_MARKER_SCHEMA = "overture-construction-v1-create-only-marker-v1"
HEAD_MANIFEST_SCHEMA = "overture-places-global-head-sharded-v2"
NIBBLE_OWNERSHIP_KIND = "token-sha256-nibble-prefix-v1"
FAMILIES = ("addresses", "places")
REVERSE_ROOT_BYTES = 688
REVERSE_ROOT_HEADER = struct.Struct("<8sBBBBIiiiiQII")
REVERSE_ROOT_SHARD = struct.Struct("<Q32s")
REVERSE_CATALOG_HEADER = struct.Struct("<8sBBBBI")
REVERSE_CATALOG_ENTRY = struct.Struct("<HBBIQII32s")
REVERSE_SHARD_HEADER_BYTES = 32
REVERSE_MAX_ADDRESS_DICTIONARY_BYTES = 8 * 1024 * 1024
REVERSE_FAMILY_CODE = {"places": 1, "addresses": 2}
REVERSE_MAX_RADIUS = {"places": 2_000, "addresses": 500}
REVERSE_MAX_SUB_CELL_LEVEL = {"places": 5, "addresses": 7}
REVERSE_LON_E7_PER_CELL = 14_062_500
REVERSE_LAT_E7_PER_CELL = 7_031_250
REVERSE_LON_E7_ORIGIN = 1_800_000_000
REVERSE_LAT_E7_ORIGIN = 900_000_000

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
        # What the CURRENT encoder produces. A slice built before the
        # prominence byte must be promoted with --places-format
        # PLRV0002+PLHD0002; the worker serves both.
        "format": "PLRV0003+PLHD0003",
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


def check_identity(actual: dict[str, Any] | None, expected: dict[str, Any]) -> bool:
    """True iff ``actual`` proves the ``expected`` identity.

    `bytes` and `sha256` must always match. `content_md5` is compared exactly
    when the expectation records one (the plan records it for every R2 source
    object): then the actual proof MUST also carry one and agree. On R2 the
    sha256 metadata is a producer claim that CopyObject echoes verbatim, so the
    store-computed content MD5 is the only field here derived from the stored
    destination bytes -- dropping it would turn the post-copy proof back into
    an echo comparison.
    """
    if (
        actual is None
        or actual["bytes"] != expected["bytes"]
        or actual["sha256"] != expected["sha256"]
    ):
        return False
    recorded = expected.get("content_md5")
    if recorded is not None:
        return actual.get("content_md5") == recorded
    return True


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

    `identity` is a stored-byte metadata proof, never a re-download. It carries
    THREE fields, and the third is the load-bearing one on the copy path:
    `sha256` is recorded object metadata, which CopyObject with
    MetadataDirective COPY merely ECHOES from the source, so after a copy it
    proves provenance and not bytes; `content_md5` is the store-computed
    single-part ETag over the DESTINATION's stored bytes, which is what makes
    the post-copy proof a byte-fidelity check.
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
        return {
            "bytes": proof["bytes"],
            "sha256": proof["sha256_metadata"],
            "content_md5": proof["content_md5"],
        }

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
        self.store.copy_within_bucket(source_key, destination_key)


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

        return R2Tree(
            r2_verified_store.s3_object_store(
                rest,
                endpoint,
                copy_read_timeout_seconds=COPY_READ_TIMEOUT_SECONDS,
            )
        )
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


def _reverse_publication(
    *,
    path: Path,
    family: str,
    request_sha256: str,
    version: str,
    release: str,
) -> dict[str, Any]:
    """Validate one directly published reverse exact-set completion record."""
    publication = _load_json_object(
        path.read_bytes(), f"{family} reverse publication"
    )
    if (
        publication.get("schema") != REVERSE_CATALOG_SCHEMA
        or publication.get("family") != family
        or publication.get("request_sha256") != request_sha256
    ):
        raise fail(f"{family} reverse publication identity differs")
    if (
        not isinstance(publication.get("records"), int)
        or isinstance(publication.get("records"), bool)
        or publication["records"] < 1
        or not isinstance(publication.get("cells"), int)
        or isinstance(publication.get("cells"), bool)
        or publication["cells"] < 1
    ):
        raise fail(f"{family} reverse publication has invalid record/cell totals")

    prefix = f"{version}/families/{family}/"

    def normalized(value: Any, what: str) -> dict[str, Any]:
        identity = require_identity(value, what)
        if (
            identity["bytes"] < 1
            or len(identity["sha256"]) != 64
            or set(identity["sha256"]) - _HEX
            or not identity["key"].startswith(prefix)
        ):
            raise fail(f"{what} has an invalid final-slice identity")
        result = {
            "destination_key": identity["key"],
            "bytes": identity["bytes"],
            "sha256": identity["sha256"],
        }
        content_md5 = identity.get("content_md5")
        if content_md5 is not None:
            if (
                not isinstance(content_md5, str)
                or len(content_md5) != 32
                or set(content_md5) - _HEX
            ):
                raise fail(f"{what} has an invalid stored-byte MD5")
            result["content_md5"] = content_md5
        return result

    root = normalized(publication.get("root"), f"{family} reverse root")
    expected_root = f"{prefix}reverse-catalog.rcat"
    if (
        root["destination_key"] != expected_root
        or root["bytes"] != REVERSE_ROOT_BYTES
    ):
        raise fail(
            f"{family} reverse root must be the fixed {REVERSE_ROOT_BYTES}-byte "
            f"entrypoint at {expected_root}"
        )

    catalog_shards = [
        normalized(value, f"{family} reverse catalog shard")
        for value in publication.get("catalog_shards") or []
    ]
    if len(catalog_shards) != 16:
        raise fail(f"{family} reverse publication must carry 16 catalog shards")
    if len({item["destination_key"] for item in catalog_shards}) != 16:
        raise fail(f"{family} reverse publication repeats a catalog shard")
    catalog_prefix = f"{prefix}reverse/catalog-shards/sha256/"
    data_prefix = f"{prefix}reverse/shards/sha256/"

    artifacts = [
        normalized(value, f"{family} reverse artifact")
        for value in publication.get("artifacts") or []
    ]
    if not artifacts:
        raise fail(f"{family} reverse publication carries no exact artifact set")
    by_key = {item["destination_key"]: item for item in artifacts}
    if len(by_key) != len(artifacts):
        raise fail(f"{family} reverse publication repeats an artifact key")
    required = {root["destination_key"]: root}
    required.update(
        (item["destination_key"], item) for item in catalog_shards
    )
    for key, identity in required.items():
        if by_key.get(key) != identity:
            raise fail(
                f"{family} reverse exact set differs from its root/catalog records"
            )
    data = [
        identity
        for key, identity in by_key.items()
        if key.startswith(data_prefix)
    ]
    if not data:
        raise fail(f"{family} reverse publication carries no data shards")
    allowed = {expected_root} | {
        key
        for key in by_key
        if key.startswith(catalog_prefix) or key.startswith(data_prefix)
    }
    if set(by_key) != allowed:
        raise fail(
            f"{family} reverse publication has an object outside its serving layout"
        )
    if {item["destination_key"] for item in catalog_shards} != {
        key for key in by_key if key.startswith(catalog_prefix)
    }:
        raise fail(f"{family} reverse catalog shard exact set differs")
    for key, identity in by_key.items():
        if key == expected_root:
            continue
        name = PurePosixPath(key).name
        digest = name.split(".", 1)[0]
        if digest != identity["sha256"]:
            raise fail(
                f"{family} reverse content-addressed key differs from its sha256"
            )
    claim_value = require_identity(
        publication.get("slice_claim"), f"{family} reverse slice claim"
    )
    claim_payload = canonical(
        {
            "schema": SLICE_CLAIM_SCHEMA,
            "version": version,
            "family": family,
            "request_sha256": request_sha256,
            "overture_release": release,
        }
    )
    claim = {
        "destination_key": claim_value["key"],
        "bytes": claim_value["bytes"],
        "sha256": claim_value["sha256"],
    }
    if (
        claim["destination_key"] != f"{version}/claims/{family}.json"
        or claim["bytes"] != len(claim_payload)
        or claim["sha256"] != sha256_bytes(claim_payload)
    ):
        raise fail(
            f"{family} reverse slice claim does not bind version/request/release"
        )
    content_md5 = claim_value.get("content_md5")
    if content_md5 is not None:
        if (
            not isinstance(content_md5, str)
            or len(content_md5) != 32
            or set(content_md5) - _HEX
        ):
            raise fail(f"{family} reverse slice claim has an invalid MD5")
        claim["content_md5"] = content_md5
    return {
        "artifacts": [by_key[key] for key in sorted(by_key)],
        "slice_claim": claim,
        "records": publication["records"],
        "cells": publication["cells"],
    }


def validate_reverse_graph(
    *,
    family: str,
    version: str,
    artifacts: list[dict[str, Any]],
    destination: LocalTree | R2Tree,
    expected_records: int | None = None,
    expected_cells: int | None = None,
) -> dict[str, int]:
    """Prove root -> 16 catalog shards -> every data identity and total."""
    prefix = f"families/{family}/"
    root_relative = f"{prefix}reverse-catalog.rcat"
    catalog_prefix = f"{prefix}reverse/catalog-shards/sha256/"
    data_prefix = f"{prefix}reverse/shards/sha256/"
    by_key = {item["object_key"]: item for item in artifacts}
    root_identity = by_key.get(root_relative)
    catalog_identities = {
        key: value for key, value in by_key.items() if key.startswith(catalog_prefix)
    }
    data_identities = {
        key: value for key, value in by_key.items() if key.startswith(data_prefix)
    }
    if (
        root_identity is None
        or len(catalog_identities) != 16
        or not data_identities
    ):
        raise fail(f"{family} reverse manifest graph is incomplete")

    def fetched(relative: str, identity: dict[str, Any], what: str) -> bytes:
        payload = destination.read_bytes(f"{version}/{relative}")
        if (
            len(payload) != identity["bytes"]
            or sha256_bytes(payload) != identity["sha256"]
        ):
            raise fail(f"{family} {what} bytes differ from its manifest identity")
        return payload

    root = fetched(root_relative, root_identity, "reverse root")
    if len(root) != REVERSE_ROOT_BYTES:
        raise fail(f"{family} reverse root has the wrong size")
    (
        magic,
        family_code,
        cell_level,
        shard_count,
        flags,
        max_radius,
        min_lon,
        min_lat,
        max_lon,
        max_lat,
        records,
        cells,
        reserved,
    ) = REVERSE_ROOT_HEADER.unpack_from(root)
    if (
        magic != b"RCAT0001"
        or family_code != REVERSE_FAMILY_CODE[family]
        or cell_level != 8
        or shard_count != 16
        or flags != 0
        or reserved != 0
        or max_radius != REVERSE_MAX_RADIUS[family]
        or records < 1
        or cells < 1
        or min_lon >= max_lon
        or min_lat >= max_lat
        or (expected_records is not None and records != expected_records)
        or (expected_cells is not None and cells != expected_cells)
    ):
        raise fail(f"{family} reverse root contract differs")

    root_catalogs = []
    offset = REVERSE_ROOT_HEADER.size
    for _ in range(16):
        size, digest = REVERSE_ROOT_SHARD.unpack_from(root, offset)
        offset += REVERSE_ROOT_SHARD.size
        root_catalogs.append((size, digest.hex()))

    seen_cells: set[int] = set()
    reached_data: set[str] = set()
    record_total = 0
    min_x = min_y = 256
    max_x = max_y = -1
    for shard_id, (catalog_bytes, catalog_sha) in enumerate(root_catalogs):
        relative = f"{catalog_prefix}{catalog_sha}.rcas"
        identity = catalog_identities.get(relative)
        if (
            identity is None
            or identity["bytes"] != catalog_bytes
            or identity["sha256"] != catalog_sha
        ):
            raise fail(
                f"{family} reverse root names an unattested catalog shard"
            )
        payload = fetched(relative, identity, "catalog shard")
        if len(payload) < REVERSE_CATALOG_HEADER.size:
            raise fail(f"{family} reverse catalog shard is truncated")
        (
            catalog_magic,
            catalog_family,
            catalog_level,
            catalog_id,
            catalog_flags,
            count,
        ) = REVERSE_CATALOG_HEADER.unpack_from(payload)
        if (
            catalog_magic != b"RCAS0002"
            or catalog_family != family_code
            or catalog_level != 8
            or catalog_id != shard_id
            or catalog_flags != 0
            or len(payload)
            != REVERSE_CATALOG_HEADER.size + count * REVERSE_CATALOG_ENTRY.size
        ):
            raise fail(f"{family} reverse catalog shard header differs")
        previous = -1
        entry_offset = REVERSE_CATALOG_HEADER.size
        for _ in range(count):
            (
                cell,
                sub_level,
                entry_flags,
                cell_records,
                data_bytes,
                index_bytes,
                dictionary_bytes,
                data_digest,
            ) = REVERSE_CATALOG_ENTRY.unpack_from(payload, entry_offset)
            entry_offset += REVERSE_CATALOG_ENTRY.size
            if (
                entry_flags != (1 if family == "addresses" else 0)
                or sub_level > REVERSE_MAX_SUB_CELL_LEVEL[family]
                or cell_records < 1
                or data_bytes < 1
                or index_bytes < 1
                or index_bytes > data_bytes
                or (
                    family == "places"
                    and dictionary_bytes != 0
                )
                or (
                    family == "addresses"
                    and not 0
                    < dictionary_bytes
                    <= REVERSE_MAX_ADDRESS_DICTIONARY_BYTES
                )
                or REVERSE_SHARD_HEADER_BYTES + dictionary_bytes + index_bytes
                >= data_bytes
                or cell >> 12 != shard_id
                or cell <= previous
                or cell in seen_cells
            ):
                raise fail(f"{family} reverse catalog cell entry differs")
            previous = cell
            seen_cells.add(cell)
            digest = data_digest.hex()
            data_relative = f"{data_prefix}{digest}.plrx"
            data_identity = data_identities.get(data_relative)
            if (
                data_identity is None
                or data_identity["bytes"] != data_bytes
                or data_identity["sha256"] != digest
                or data_relative in reached_data
            ):
                raise fail(
                    f"{family} reverse catalog names an unattested data shard"
                )
            reached_data.add(data_relative)
            record_total += cell_records
            y, x = cell >> 8, cell & 0xFF
            min_x, max_x = min(min_x, x), max(max_x, x)
            min_y, max_y = min(min_y, y), max(max_y, y)

    expected_bbox = (
        min_x * REVERSE_LON_E7_PER_CELL - REVERSE_LON_E7_ORIGIN,
        min_y * REVERSE_LAT_E7_PER_CELL - REVERSE_LAT_E7_ORIGIN,
        (max_x + 1) * REVERSE_LON_E7_PER_CELL - REVERSE_LON_E7_ORIGIN,
        (max_y + 1) * REVERSE_LAT_E7_PER_CELL - REVERSE_LAT_E7_ORIGIN,
    )
    if (
        len(seen_cells) != cells
        or record_total != records
        or reached_data != set(data_identities)
        or expected_bbox != (min_lon, min_lat, max_lon, max_lat)
    ):
        raise fail(f"{family} reverse catalog graph totals differ")
    return {"records": records, "cells": cells}


def _forward_slice_claim(
    *, family: str, version: str, request_sha256: str, release: str
) -> dict[str, Any]:
    """The slice claim a zero-copy forward finalize must have written.

    DERIVED, not read: the claim is a pure function of (version, family,
    request, release), so promotion computes the bytes it requires and
    `_verify_prepositioned` proves the destination holds exactly those. Reading
    the claim and believing it would make it a label rather than a binding.

    No `content_md5`: promotion did not publish this object and has no recorded
    store-computed digest for it. `check_identity` then compares bytes and the
    recorded sha256 only -- see the residual noted on the forward prepositioned
    path in `_plan_family`.
    """
    payload = canonical(
        {
            "schema": SLICE_CLAIM_SCHEMA,
            "version": version,
            "family": family,
            "request_sha256": request_sha256,
            "overture_release": release,
        }
    )
    return {
        "destination_key": f"{version}/claims/{family}.json",
        "bytes": len(payload),
        "sha256": sha256_bytes(payload),
    }


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
    reverse_catalog: Path | None,
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

    # Item 1, zero-copy promotion. The finalize marker's OWN KEYS say where the
    # serving objects are, and it is the create-only document written last over
    # the exact published set, so it is the authority -- not the dispatch input,
    # not a flag, and not an inference from what happens to exist.
    #
    #   construction layout  the objects are under the request-scoped
    #                        construction prefix and promotion CopyObjects each
    #                        one into the release namespace. 158.68 GiB / 21,279
    #                        objects on the 2026-07-31 promotion, changing the
    #                        prefix and nothing else.
    #   release layout       finalize published them straight into the release
    #                        namespace under their final content-addressed names
    #                        (`--release-slice-version`) and promotion binds them
    #                        in place, exactly as it already does for reverse.
    #
    # A slice must be entirely one or the other. A mixture means two finalizes
    # with different destinations wrote into one marker set, and promoting it
    # would publish a family manifest naming objects from two different runs.
    construction_objects_prefix = f"{family_prefix}objects/"
    release_objects_prefix = f"{version}/families/{family}/objects/"
    in_construction = any(
        key.startswith(construction_objects_prefix) for key in marker_artifacts
    )
    in_release = any(
        key.startswith(release_objects_prefix) for key in marker_artifacts
    )
    if in_construction and in_release:
        raise fail(
            f"{family} finalize marker lists serving objects under BOTH the "
            "construction and the release namespace; a slice is one layout or "
            "the other"
        )
    if not in_construction and not in_release:
        raise fail(
            f"{family} finalize marker lists no serving object under "
            f"{construction_objects_prefix} or {release_objects_prefix}"
        )
    zero_copy = in_release
    objects_prefix = release_objects_prefix if zero_copy else construction_objects_prefix

    reductions = _load_reductions(reductions_dir, family)
    if family == "places":
        head = construction_manifest.get("head")
        if not isinstance(head, dict):
            raise fail("places family manifest carries no head block")
        head_manifest_name = head["manifest"]["object"]
        # Read from wherever the marker says the objects live. Under the release
        # layout this means `--source` must be able to READ the release
        # namespace; in the promotion workflow source and destination are the
        # same bucket, and the routing manifest's contents are the one thing plan
        # cannot derive from the family manifest alone (the shard_id -> object
        # map exists only inside this object).
        head_manifest_bytes = source.read_bytes(
            f"{objects_prefix}{head_manifest_name}"
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
    forward_prepositioned = []
    for name in sorted(serving):
        source_key = f"{objects_prefix}{name}"
        published = marker_artifacts.get(source_key)
        if published is None:
            raise fail(f"{family} finalize marker does not list {source_key}")
        if (
            published["sha256"] != serving[name]["sha256"]
            or published["bytes"] != serving[name]["bytes"]
        ):
            raise fail(f"{family} finalize marker identity differs for {source_key}")
        destination_key = f"{version}/families/{family}/objects/{name}"
        if zero_copy:
            # Nothing to copy: the marker's key IS the destination key. Proved at
            # execute and verify by `_verify_prepositioned` against the
            # destination, exactly as reverse's objects are.
            #
            # THE RESIDUAL, stated rather than glossed. On the copy path the plan
            # records the SOURCE object's store-computed content MD5 and the
            # post-copy check compares the DESTINATION's own ETag against it --
            # a stored-byte proof that the copy did not corrupt anything. There
            # is no copy here, so there is nothing for that check to compare and
            # `content_md5` is absent; `check_identity` then proves length plus
            # the recorded sha256 metadata, which on R2 is a producer echo.
            #
            # What replaces it is upstream and stronger for the property that
            # actually matters: construction's finalize ran `read_back_identity`
            # on every one of these objects at publication, which compares R2's
            # server-computed MD5 of the stored bytes against the MD5 of the
            # bytes it sent. So the producer->object chain IS a stored-byte
            # proof; what is no longer independently re-proved at promotion time
            # is that the object has not been replaced since, by someone able to
            # write this bucket and forge matching sha256 metadata. The copy path
            # does not prove that about its source either -- it proves the
            # destination equals the source. Recording finalize's content MD5 in
            # the marker would close it; see docs/plans/.
            if source_key != destination_key:
                raise fail(
                    f"{family} prepositioned object {name} is at {source_key}, "
                    f"not at its release key {destination_key}"
                )
            forward_prepositioned.append(
                {
                    "destination_key": destination_key,
                    "sha256": serving[name]["sha256"],
                    "bytes": serving[name]["bytes"],
                }
            )
            continue
        objects.append(
            {
                "source_key": source_key,
                "destination_key": destination_key,
                "sha256": serving[name]["sha256"],
                "bytes": serving[name]["bytes"],
            }
        )

    if isinstance(source, R2Tree):
        # Record the store-computed content MD5 (single-part ETag) of every
        # source object. After the server-side copy the sha256 metadata is an
        # echo of the source's, so this recorded MD5 is what execute and verify
        # compare against the DESTINATION's own ETag to prove byte fidelity.
        # Deterministic: the source objects are immutable, so their ETags are.
        # Item 2: 21,279 HEADs, measured at 17-36 minutes. The plan stays
        # byte-identical under concurrency because each task writes only its own
        # item's `content_md5` and the list order is untouched.
        def record_source_identity(item: dict[str, Any]) -> None:
            proof = source.identity(item["source_key"])
            if proof is None:
                raise fail(f"{family} source object is missing: {item['source_key']}")
            if (
                proof["bytes"] != item["bytes"]
                or proof["sha256"] != item["sha256"]
            ):
                raise fail(
                    f"{family} source object {item['source_key']} does not match "
                    "the identity its producing phase recorded"
                )
            item["content_md5"] = proof["content_md5"]

        _for_each_ordered(objects, record_source_identity, parallel=True)

    reverse_publication = (
        _reverse_publication(
            path=reverse_catalog,
            family=family,
            request_sha256=request_sha256,
            version=version,
            release=release,
        )
        if reverse_catalog is not None
        else None
    )
    reverse_prepositioned = (
        reverse_publication["artifacts"] if reverse_publication else []
    )
    # The slice claim binds this release VERSION to the request and the Overture
    # release that produced it. It is what restores the authenticated late
    # binding that `namespaces.slice` cannot carry: the release version is chosen
    # after the build, so it is a dispatch input, and the claim is the create-only
    # document that proves the producer chose it deliberately.
    #
    # Forward and reverse write BYTE-IDENTICAL claims to the same key, so a
    # family carrying both must present exactly one claim. Disagreeing claims
    # would mean the two halves of a family slice came from different requests.
    slice_claim = reverse_publication["slice_claim"] if reverse_publication else None
    if zero_copy:
        forward_claim = _forward_slice_claim(
            family=family, version=version, request_sha256=request_sha256, release=release
        )
        if slice_claim is not None and (
            slice_claim["destination_key"] != forward_claim["destination_key"]
            or slice_claim["sha256"] != forward_claim["sha256"]
            or slice_claim["bytes"] != forward_claim["bytes"]
        ):
            raise fail(
                f"{family} forward and reverse slice claims differ; the two halves "
                "of this family slice were produced by different requests"
            )
        # Keep reverse's claim when there is one: it carries the store-computed
        # content MD5 its publication recorded, which is a strictly stronger
        # expectation for `_verify_prepositioned` than the derived bytes alone.
        slice_claim = slice_claim or forward_claim
    # Forward and reverse prepositioned objects stay in SEPARATE plan fields, and
    # that is load-bearing rather than tidy. They are verified identically, but
    # every other check treats them differently: the forward set is the routing
    # table's object set (`_derived_members` proves routing and objects are the
    # same set), the reverse set is deliberately outside it and is proved by
    # `validate_reverse_graph` instead. Merging them would make every reverse
    # shard an unrouted forward object.
    prepositioned = reverse_prepositioned
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
    manifest_artifacts.extend(
        {
            "object_key": item["destination_key"][len(version) + 1 :],
            "bytes": item["bytes"],
            "sha256": item["sha256"],
        }
        for item in reverse_prepositioned
    )
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
        "prepositioned": prepositioned,
        "forward_prepositioned": forward_prepositioned,
        "slice_claim": slice_claim,
        # Item 1. Stated in the plan so the operator reads which shape they are
        # about to execute rather than inferring it from a zero copy count.
        "forward_layout": "release-prepositioned" if zero_copy else "construction-copy",
        "reverse_totals": (
            {
                "records": reverse_publication["records"],
                "cells": reverse_publication["cells"],
            }
            if reverse_publication
            else None
        ),
        "routing": routing,
        "routing_key": f"{version}/families/{family}/routing.json",
        "routing_sha256": sha256_bytes(routing_bytes),
        "routing_bytes": len(routing_bytes),
        "family_manifest": family_manifest,
        "family_manifest_key": f"{version}/families/{family}/family-manifest.json",
        "family_manifest_sha256": sha256_bytes(family_manifest_bytes),
        "family_manifest_bytes": len(family_manifest_bytes),
        "totals": {
            "objects": len(objects) + len(prepositioned) + len(forward_prepositioned),
            "copied_objects": len(objects),
            "prepositioned_objects": len(prepositioned) + len(forward_prepositioned),
            # Broken out because it is the number item 1 exists to move: 21,279
            # forward objects and 158.68 GiB copied on the 2026-07-31 promotion,
            # zero under the release layout.
            "forward_prepositioned_objects": len(forward_prepositioned),
            "bytes": sum(
                item["bytes"]
                for item in objects + prepositioned + forward_prepositioned
            ),
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


def _optional_per_family_option(
    values: list[str], families: list[str], what: str
) -> dict[str, str]:
    """Resolve an optional repeated option without requiring every family."""
    default: str | None = None
    resolved: dict[str, str] = {}
    for value in values:
        prefix, separator, rest = value.partition("=")
        if separator and prefix in FAMILIES and rest:
            if prefix not in families:
                raise fail(f"{what} given for unselected family {prefix}")
            resolved[prefix] = rest
        elif default is None:
            default = value
        else:
            raise fail(f"multiple default {what} values given ({default!r}, {value!r})")
    if default is not None:
        return {family: resolved.get(family, default) for family in families}
    return resolved


def cmd_plan(args: argparse.Namespace) -> int:
    version = validate_slice_version(args.version)
    families = sorted(set(args.family))
    source = open_tree(args.source, "--source")
    slice_roots = _per_family_option(args.slice_root, families, "--slice-root")
    markers_roots = _per_family_option(args.markers_root, families, "--markers-root")
    reductions = _per_family_option(args.reductions_dir, families, "--reductions-dir")
    reverse_catalogs = _optional_per_family_option(
        args.reverse_catalog, families, "--reverse-catalog"
    )
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
            reverse_catalog=(
                Path(reverse_catalogs[family])
                if family in reverse_catalogs
                else None
            ),
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
    # The FORWARD serving set, however it got to the destination. Under the
    # release layout (item 1) nothing is copied, so `objects` is empty and every
    # routed object is in `forward_prepositioned` instead -- reading only
    # `objects` would call the entire routing table orphaned. Reverse's
    # prepositioned artifacts are deliberately NOT here: they are outside the
    # routing table by design and `validate_reverse_graph` proves them.
    planned = {
        PurePosixPath(item["destination_key"]).name
        for item in (*value["objects"], *(value.get("forward_prepositioned") or []))
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
    # The payload is in hand, so its content MD5 is computable directly and the
    # post-write proof can demand the store-computed one match it (R2; a local
    # identity carries no md5 and the sha256 there is a fresh byte hash).
    expected = {
        "bytes": len(payload),
        "sha256": sha256,
        "content_md5": (
            hashlib.md5(payload).hexdigest()
            if isinstance(destination, R2Tree)
            else None
        ),
    }
    existing = destination.identity(key)
    if existing is not None:
        if check_identity(existing, expected):
            return "already-present"
        raise fail(f"destination {key} exists with different bytes; refusing")
    try:
        destination.put_bytes_create_only(key, payload)
    except FileExistsError as error:
        raise fail(f"destination {key} appeared during create-only write") from error
    if not check_identity(destination.identity(key), expected):
        raise fail(f"destination {key} failed post-write verification")
    return "written"


def _execute_object(
    item: dict[str, Any],
    source: LocalTree | R2Tree,
    destination: LocalTree | R2Tree,
) -> str:
    expected = {
        "bytes": item["bytes"],
        "sha256": item["sha256"],
        "content_md5": item.get("content_md5"),
    }
    # The source must still be the object the plan admitted, down to its
    # stored-byte MD5 when the plan recorded one (R2 source).
    actual = source.identity(item["source_key"])
    if actual is None:
        raise fail(f"source object vanished: {item['source_key']}")
    if not check_identity(actual, expected):
        raise fail(
            f"source object {item['source_key']} does not match the "
            f"planned identity (sha/bytes/md5 changed)"
        )
    existing = destination.identity(item["destination_key"])
    if existing is not None:
        # Byte-identical is a resume; anything else is a squatter.
        if check_identity(existing, expected):
            return "already-present"
        raise fail(
            f"destination {item['destination_key']} exists with "
            "different bytes; refusing to overwrite"
        )
    try:
        copy_object(source, destination, item["source_key"], item["destination_key"])
    except FileExistsError as error:
        raise fail(
            f"destination {item['destination_key']} appeared during the "
            "create-only copy"
        ) from error
    # On R2 this is the byte-fidelity gate: the destination's OWN single-part
    # ETag (content MD5 of its stored bytes) must equal the source MD5 the plan
    # recorded; the copied sha256 metadata alone is only an echo.
    verified = destination.identity(item["destination_key"])
    if not check_identity(verified, expected):
        raise fail(
            f"destination {item['destination_key']} failed its post-copy "
            "identity proof (stored bytes differ from the planned source identity)"
        )
    return "copied"


def _for_each_ordered(
    items: Iterable[Any],
    work: Callable[[Any], None],
    *,
    parallel: bool,
) -> None:
    """Apply `work` to every item, preserving DETERMINISTIC failure order.

    Two properties matter more than the speedup, and both are why this is a
    helper rather than an inline `ThreadPoolExecutor`:

    1. The exception that propagates belongs to the EARLIEST item in list order,
       never to whichever thread happened to fail first. Without this the same
       broken promotion reports a different object every run and an operator
       ends up debugging thread scheduling instead of the defect.
    2. It still fails fast. Once any item has failed, later items short-circuit
       rather than issuing another 40,000 pointless HEADs. Items submitted
       BEFORE the failure still run to completion, which is what makes property
       1 sound: the pool submits in index order, so anything with a lower index
       than the first failure was already in flight and cannot be skipped.

    Parallelism is the caller's call because it only pays for network round
    trips; against a LocalTree this runs the identical sequential loop it
    replaced.
    """
    items = list(items)
    if not parallel or len(items) < 2:
        for item in items:
            work(item)
        return

    failures: dict[int, BaseException] = {}
    stop = threading.Event()

    def run(index: int, item: Any) -> None:
        if stop.is_set():
            return
        try:
            work(item)
        except BaseException as error:  # re-raised in index order below
            failures[index] = error
            stop.set()

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=IDENTITY_WORKERS
    ) as pool:
        for index, item in enumerate(items):
            pool.submit(run, index, item)
    if failures:
        raise failures[min(failures)]


def _verify_prepositioned(
    item: dict[str, Any], destination: LocalTree | R2Tree
) -> None:
    expected = {
        "bytes": item["bytes"],
        "sha256": item["sha256"],
        "content_md5": item.get("content_md5"),
    }
    if not check_identity(destination.identity(item["destination_key"]), expected):
        raise fail(
            f"prepositioned destination {item['destination_key']} is absent "
            "or differs from its direct-publication record"
        )


def cmd_execute(args: argparse.Namespace) -> int:
    plan = _load_plan(args.plan)
    source = open_tree(args.source, "--source")
    destination = open_tree(args.destination, "--destination")
    report: dict[str, Any] = {}
    for family in sorted(plan["families"]):
        value = plan["families"][family]
        derived = _derived_members(family, value)
        prepositioned = value.get("prepositioned") or []
        forward_prepositioned = value.get("forward_prepositioned") or []
        slice_claim = value.get("slice_claim")
        if slice_claim is not None:
            _verify_prepositioned(slice_claim, destination)
        # Item 2: 20,777 HEADs. Each proves one immutable destination key
        # against its own record, so they are independent.
        _for_each_ordered(
            (*prepositioned, *forward_prepositioned),
            lambda item: _verify_prepositioned(item, destination),
            parallel=isinstance(destination, R2Tree),
        )
        reverse_totals = value.get("reverse_totals")
        if reverse_totals is not None:
            validate_reverse_graph(
                family=family,
                version=plan["version"],
                artifacts=value["family_manifest"]["artifacts"],
                destination=destination,
                expected_records=reverse_totals["records"],
                expected_cells=reverse_totals["cells"],
            )
        if isinstance(source, R2Tree) and isinstance(destination, R2Tree):
            # Each task owns one immutable destination key.  executor.map
            # propagates every worker exception; the manifest is written only
            # after the entire pool has completed successfully.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=COPY_WORKERS
            ) as executor:
                states = list(
                    executor.map(
                        lambda item: _execute_object(item, source, destination),
                        value["objects"],
                    )
                )
        else:
            states = [
                _execute_object(item, source, destination)
                for item in value["objects"]
            ]
        copied = states.count("copied")
        skipped = states.count("already-present")
        # Routing first, manifest STRICTLY last: a present #107 manifest must
        # always attest already-present data.
        derived_states = [
            _put_derived_create_only(destination, key, payload, sha256)
            for key, payload, sha256 in derived
        ]
        report[family] = {
            "copied": copied,
            "already_present": skipped,
            "prepositioned_verified": len(prepositioned) + len(forward_prepositioned),
            "forward_prepositioned_verified": len(forward_prepositioned),
            "slice_claim": (
                "verified" if slice_claim is not None else "not-required"
            ),
            "routing": derived_states[0],
            "family_manifest": derived_states[1],
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
        slice_claim = value.get("slice_claim")
        if slice_claim is not None:
            _verify_prepositioned(slice_claim, destination)
        expected = {
            item["destination_key"]: {
                "bytes": item["bytes"],
                "sha256": item["sha256"],
                "content_md5": item.get("content_md5"),
            }
            for item in value["objects"]
        }
        expected.update(
            {
                item["destination_key"]: {
                    "bytes": item["bytes"],
                    "sha256": item["sha256"],
                    "content_md5": item.get("content_md5"),
                }
                for item in (
                    *(value.get("prepositioned") or []),
                    *(value.get("forward_prepositioned") or []),
                )
            }
        )
        # The derived documents' bytes are reproducible from the plan, so their
        # content MD5 is too -- on R2 that turns the per-key HEAD proof into a
        # stored-byte check for them as well.
        derived_payloads = {
            value["routing_key"]: canonical(value["routing"]),
            value["family_manifest_key"]: canonical(value["family_manifest"]),
        }
        on_r2 = isinstance(destination, R2Tree)
        expected[value["routing_key"]] = {
            "bytes": value["routing_bytes"],
            "sha256": value["routing_sha256"],
            "content_md5": (
                hashlib.md5(derived_payloads[value["routing_key"]]).hexdigest()
                if on_r2
                else None
            ),
        }
        expected[value["family_manifest_key"]] = {
            "bytes": value["family_manifest_bytes"],
            "sha256": value["family_manifest_sha256"],
            "content_md5": (
                hashlib.md5(derived_payloads[value["family_manifest_key"]]).hexdigest()
                if on_r2
                else None
            ),
        }
        listed = destination.list_prefix(prefix)
        if listed != sorted(expected):
            missing = sorted(set(expected) - set(listed))[:5]
            extra = sorted(set(listed) - set(expected))[:5]
            raise fail(
                f"{family} destination is not the exact planned set: "
                f"missing={missing}, unexpected={extra}"
            )
        # Item 2: 42,058 HEADs, the largest of the three loops and the one that
        # survives zero-copy promotion -- verify must still prove the
        # destination. `listed` is sorted, so the reported key is stable.
        def verify_destination_key(key: str) -> None:
            if not check_identity(destination.identity(key), expected[key]):
                raise fail(f"{family} destination object {key} identity differs")

        _for_each_ordered(listed, verify_destination_key, parallel=on_r2)
        reverse_totals = value.get("reverse_totals")
        if reverse_totals is not None:
            validate_reverse_graph(
                family=family,
                version=version,
                artifacts=value["family_manifest"]["artifacts"],
                destination=destination,
                expected_records=reverse_totals["records"],
                expected_cells=reverse_totals["cells"],
            )
        # Routing and the family manifest are re-read FROM THE DESTINATION and
        # their DOWNLOADED bytes are hashed against the plan-recorded digests:
        # this pass proves what a reader will actually fetch, not what a HEAD's
        # metadata claims about it.
        routing_bytes = destination.read_bytes(value["routing_key"])
        if sha256_bytes(routing_bytes) != value["routing_sha256"]:
            raise fail(
                f"{family} destination routing.json bytes do not hash to the "
                "plan-recorded digest"
            )
        routing = _load_json_object(routing_bytes, f"{family} routing.json")
        # The forward serving set again -- copied or prepositioned, both are
        # objects a reader routes to and both are proved against the destination
        # above. Reading only `objects` would make every routing entry of a
        # zero-copy slice look dangling.
        object_keys = {
            PurePosixPath(item["destination_key"]).name
            for item in (*value["objects"], *(value.get("forward_prepositioned") or []))
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
            head_manifest_key = f"{prefix}objects/{routing['head']['manifest_object']}"
            head_manifest_bytes = destination.read_bytes(head_manifest_key)
            if (
                head_manifest_key not in expected
                or sha256_bytes(head_manifest_bytes)
                != expected[head_manifest_key]["sha256"]
            ):
                raise fail(
                    f"{family} destination head routing manifest bytes do not "
                    "hash to the plan-recorded digest"
                )
            head_manifest = _load_json_object(
                head_manifest_bytes, f"{family} head routing manifest"
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
        # The destination #107 manifest must hash to the planned bytes,
        # validate self-consistently, and attest exactly the non-manifest
        # objects under the family prefix.
        manifest_bytes = destination.read_bytes(value["family_manifest_key"])
        if sha256_bytes(manifest_bytes) != value["family_manifest_sha256"]:
            raise fail(
                f"{family} destination family manifest bytes do not hash to "
                "the plan-recorded digest"
            )
        manifest = GBM.validate_family_manifest(
            _load_json_object(manifest_bytes, f"{family} family manifest")
        )
        attested = {
            f"{version}/{artifact['object_key']}": {
                "bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
            for artifact in manifest["artifacts"]
        }
        non_manifest = {
            key: {"bytes": identity["bytes"], "sha256": identity["sha256"]}
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
# Slice manifest: the #107 slice-wide source document the v2 release tooling
# (v2_release_manifest.py assemble / _validate_family_source) requires at
# {version}/slice-manifest.json. Derived from the SAME plan(s) that execute
# wrote and verify proved, and bound to the destination by re-downloading each
# published family manifest before a byte is written.


def cmd_slice_manifest(args: argparse.Namespace) -> int:
    plans = [_load_plan(path) for path in args.plan]
    version = plans[0]["version"]
    release = plans[0]["release"]
    for plan in plans[1:]:
        if plan["version"] != version or plan["release"] != release:
            raise fail("slice-manifest plans disagree on version or release")
    validate_slice_version(version)
    families: dict[str, dict[str, Any]] = {}
    for plan in plans:
        for family, value in plan["families"].items():
            if family in families:
                raise fail(f"family {family} appears in more than one plan")
            families[family] = value
    if not families:
        raise fail("slice-manifest requires at least one planned family")

    destination = open_tree(args.destination, "--destination")
    # Deterministic by default (the slice date at midnight UTC), so two runs
    # over the same plans emit byte-identical documents and the create-only
    # publication is naturally resumable.
    generated_at = args.generated_at or f"{version[6:16]}T00:00:00+00:00"

    summaries: dict[str, dict[str, Any]] = {}
    verified: list[dict[str, Any]] = []
    for family in sorted(families):
        value = families[family]
        manifest = GBM.validate_family_manifest(value["family_manifest"])
        manifest_bytes = canonical(manifest)
        if (
            sha256_bytes(manifest_bytes) != value["family_manifest_sha256"]
            or len(manifest_bytes) != value["family_manifest_bytes"]
        ):
            raise fail(f"{family} plan-recorded family manifest identity disagrees")
        # Bind the document to what is actually published: the DOWNLOADED
        # destination manifest bytes must hash to the plan-recorded digest, so
        # a slice manifest can never attest a tree that execute+verify did not
        # put there.
        published = destination.read_bytes(value["family_manifest_key"])
        if sha256_bytes(published) != value["family_manifest_sha256"]:
            raise fail(
                f"{family} destination family manifest at "
                f"{value['family_manifest_key']} does not hash to the "
                "plan-recorded digest; run execute and verify first"
            )
        artifacts = manifest["artifacts"]
        href = f"./families/{family}/family-manifest.json"
        objects = [
            {
                "href": f"./{artifact['object_key']}",
                "size_bytes": artifact["bytes"],
                "sha256": artifact["sha256"],
            }
            for artifact in artifacts
        ]
        summaries[family] = {
            "manifest": href,
            "manifest_digest": manifest["manifest_digest"],
            "region": manifest["region"],
            "artifact_count": len(artifacts),
            "total_bytes": sum(artifact["bytes"] for artifact in artifacts),
            "objects": objects,
            "promotion_eligible": False,
        }
        verified.append(
            {
                "href": href,
                "size_bytes": value["family_manifest_bytes"],
                "sha256": value["family_manifest_sha256"],
            }
        )
        verified.extend(objects)

    document = {
        "schema_version": 1,
        "slice_version": version,
        "overture_release": release,
        "generated_at": generated_at,
        # A verified, NON-promoting family fleet: no core release, no catalog
        # link. Promotion eligibility is decided by the v2 catalog CAS layer,
        # never by this document.
        "is_slice": True,
        "promotion_eligible": False,
        "families": summaries,
        "verified_version_objects": verified,
    }
    payload = canonical(document)
    sha256 = sha256_bytes(payload)
    key = f"{version}/slice-manifest.json"
    print(
        json.dumps(
            {
                "planned_write": {
                    "key": key,
                    "sha256": sha256,
                    "bytes": len(payload),
                    "mode": "create-only",
                    "families": sorted(summaries),
                }
            },
            sort_keys=True,
        )
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(payload)

    existing = destination.identity(key)
    if existing is not None and existing["sha256"] != sha256:
        # Fail closed in BOTH modes: an existing different document means this
        # slice version already attests another family set, and slice
        # manifests are immutable -- promote under a new slice version.
        raise fail(
            f"destination {key} exists with different bytes (sha256 "
            f"{existing['sha256']}); slice manifests are immutable"
        )
    if not args.execute:
        status = "already-published" if existing is not None else "dry-run"
    else:
        state = _put_derived_create_only(destination, key, payload, sha256)
        status = "already-published" if state == "already-present" else "written"
    print(
        json.dumps(
            {
                "slice_manifest": key,
                "sha256": sha256,
                "bytes": len(payload),
                "families": sorted(summaries),
                "output": str(output),
                "status": status,
            },
            sort_keys=True,
        )
    )
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
    plan_parser.add_argument(
        "--reverse-catalog",
        action="append",
        default=[],
        help="direct reverse publication completion JSON (repeatable; "
        "family=path for per-family). Its artifacts are verified in place and "
        "attested without copying.",
    )
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

    slice_manifest_parser = commands.add_parser(
        "slice-manifest",
        help="emit and create-only publish {version}/slice-manifest.json "
        "from executed+verified promotion plans",
    )
    slice_manifest_parser.add_argument(
        "--plan",
        action="append",
        required=True,
        type=Path,
        help="promotion plan (repeatable; one slice manifest covers every "
        "planned family, and the set is immutable once published)",
    )
    slice_manifest_parser.add_argument("--destination", required=True)
    slice_manifest_parser.add_argument("--output", required=True)
    slice_manifest_parser.add_argument(
        "--generated-at",
        help="defaults to the slice date at midnight UTC so two runs over the "
        "same plans are byte-identical",
    )
    slice_manifest_parser.add_argument(
        "--execute",
        action="store_true",
        help="write to the destination; the default is a dry-run that only "
        "prints the planned create-only write",
    )
    slice_manifest_parser.set_defaults(entry=cmd_slice_manifest)

    args = parser.parse_args(argv)
    return args.entry(args)


if __name__ == "__main__":
    raise SystemExit(main())
