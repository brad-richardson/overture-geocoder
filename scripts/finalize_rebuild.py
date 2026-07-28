#!/usr/bin/env python3
"""Validate a complete immutable rebuild and prepare its atomic publication.

The long-running build jobs only upload objects under a new version prefix.
This script is used by the finalizer job after every family succeeds to:

* compare the exact R2 object inventory with forward, reverse, and ID metadata;
* verify every small SQLite shard by SHA-256 after readback;
* require a complete uniform v3 ID fleet and its locator dictionary; and
* write one release manifest and one next-catalog candidate.

It does not upload anything itself. The workflow retains the previous catalog,
uploads the generated manifest, publishes the generated catalog once, and can
restore the retained catalog if production smoke checks fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
# The finalizer's SHA-256-of-file is the shared pipeline helper; the local
# alias keeps this script's call sites and monkeypatch surface stable.
from common import sha256_file as _sha256  # noqa: E402,F401

# Optional experimental-family support (#107 manifests). global_build_manifest is
# stdlib-only, so importing it keeps the finalize job dependency-thin (no duckdb;
# see PR #104).
from global_build_manifest import (  # noqa: E402
    FAMILIES,
    canonical_json as _family_canonical_json,
    validate_family_manifest,
    verify_family_manifest_against_listing,
)


VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
# Non-production slice versions for the family-release rehearsal. A slice version
# MUST NOT match VERSION_RE, so a slice prefix can never collide with, or
# masquerade as, a real release. The families-only slice path accepts exactly
# this pattern and fails closed on a production-pattern version.
SLICE_VERSION_RE = re.compile(r"^slice-\d{4}-\d{2}-\d{2}\.\d+$")
ID_PREFIX_RE = re.compile(r"^[0-9a-f]{3}$")

# v3 fleets publish permanent id-inventories objects alongside the ID shards.
ID_INVENTORY_DIR = "id-inventories"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Root discovery catalog + durable operator-recovery backups.
BUCKET = "geocoder-shards"
CATALOG_KEY = "catalog.json"
BACKUP_PREFIX = "backups"
BASE_URL_DEFAULT = "https://geocoder.bradr.dev"

# The immutable per-release manifest published under the version prefix by the
# finalize job AFTER verify succeeds. A rerun of a finalize that failed later
# (e.g. in the promote smoke) therefore finds it already present.
RELEASE_MANIFEST_KEY = "release-manifest.json"


def _load_json(path: Path) -> dict:
    with path.open() as src:
        value = json.load(src)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _verify_referenced_json_object(
    key: str, reference: dict, inventory: dict, local_dir: Path
) -> None:
    """Byte-verify one id-inventories object against its trusted reference.

    The object must be present in the R2 inventory listing, present in the
    local readback (synced into ``local_dir`` by the finalize job), match the
    reference's ``size_bytes``, and hash to the reference's ``sha256``. This
    binds each referenced key to content, so a stray object squatting a
    referenced key fails closed on the SHA and any unreferenced key fails at
    the exact-set gate.
    """
    entry = inventory.get(key)
    local = local_dir / key
    if entry is None or not local.is_file():
        raise ValueError(f"{key} is missing from inventory or readback")
    if entry["size_bytes"] != reference.get("size_bytes"):
        raise ValueError(f"{key} size mismatch")
    if _sha256(local) != reference.get("sha256"):
        raise ValueError(f"{key} SHA-256 mismatch")


def _version_key(value: str) -> tuple[int, int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise ValueError(f"invalid rebuild version {value!r}")
    date_part, suffix = value.rsplit(".", 1)
    year, month, day = (int(part) for part in date_part.split("-"))
    # Reject syntactically plausible but impossible calendar dates.
    date_value = date(year, month, day)
    return date_value.year, date_value.month, date_value.day, int(suffix)


def _slice_version_key(value: str) -> tuple[int, int, int, int]:
    """Validate a NON-production slice version and return its sort key.

    Fail closed on anything but the ``slice-YYYY-MM-DD.N`` pattern, and REJECT a
    production-pattern version (VERSION_RE) even though it also carries a date: a
    slice must never be able to name, or be named by, a real release prefix.
    """
    if VERSION_RE.fullmatch(value):
        raise ValueError(
            f"{value!r} matches the production release pattern; slice versions "
            "must not collide with a real release"
        )
    if not SLICE_VERSION_RE.fullmatch(value):
        raise ValueError(
            f"invalid slice version {value!r} (expected slice-YYYY-MM-DD.N)"
        )
    date_part, suffix = value[len("slice-"):].rsplit(".", 1)
    year, month, day = (int(part) for part in date_part.split("-"))
    # Reject syntactically plausible but impossible calendar dates.
    date_value = date(year, month, day)
    return date_value.year, date_value.month, date_value.day, int(suffix)


def _publishable_version_key(value: str) -> tuple[int, int, int, int]:
    """Version validation for family publication: a real release OR a slice.

    ``publish-family`` is the one shared publication path for optional families
    in both a real release (under the production version prefix) and a
    non-promoting slice (under a ``slice-*`` prefix). Accept either validated
    pattern and reject anything else; both are calendar-checked so an impossible
    date still fails closed.
    """
    try:
        return _version_key(value)
    except ValueError:
        return _slice_version_key(value)


def _inventory_by_relative_key(inventory: dict, version: str) -> dict[str, dict]:
    prefix = f"{version}/"
    contents = inventory.get("Contents")
    if not isinstance(contents, list) or not contents:
        raise ValueError("R2 inventory is empty")
    result = {}
    for entry in contents:
        if not isinstance(entry, dict):
            raise ValueError("R2 inventory contains a non-object entry")
        key = entry.get("Key")
        size = entry.get("Size")
        etag = entry.get("ETag")
        if not isinstance(key, str) or not key.startswith(prefix):
            raise ValueError(f"inventory key is outside {prefix}: {key!r}")
        relative = key[len(prefix):]
        if not relative or relative in result:
            raise ValueError(f"duplicate or empty inventory key: {relative!r}")
        if not isinstance(size, int) or size < 0:
            raise ValueError(f"invalid size for {relative}")
        if not isinstance(etag, str) or not etag.strip('"'):
            raise ValueError(f"missing ETag for {relative}")
        result[relative] = {
            "href": f"./{relative}",
            "size_bytes": size,
            "etag": etag.strip('"'),
        }
    return result


def _collection_items(collection: dict, label: str) -> dict:
    items = collection.get("items")
    if not isinstance(items, dict) or not items:
        raise ValueError(f"{label} collection has no items")
    return items


def _verify_sqlite_family(
    *,
    label: str,
    collection: dict,
    subdir: str,
    inventory: dict[str, dict],
    readback_dir: Path,
) -> list[dict]:
    items = _collection_items(collection, label)
    expected_keys = {f"{subdir}/{shard_id}.db" for shard_id in items}
    actual_keys = {key for key in inventory if key.startswith(f"{subdir}/")}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)[:10]
        extra = sorted(actual_keys - expected_keys)[:10]
        raise ValueError(f"{label} inventory mismatch: missing={missing}, extra={extra}")

    result = []
    total_size = 0
    total_records = 0
    for shard_id, metadata in sorted(items.items()):
        if not isinstance(metadata, dict):
            raise ValueError(f"invalid {label} metadata for {shard_id}")
        key = f"{subdir}/{shard_id}.db"
        entry = inventory[key]
        if metadata.get("href") != f"./{key}":
            raise ValueError(f"{key} has an invalid href")
        expected_size = metadata.get("size_bytes")
        expected_sha = metadata.get("sha256")
        record_count = metadata.get("record_count")
        if entry["size_bytes"] != expected_size:
            raise ValueError(f"{key} size mismatch")
        if not isinstance(record_count, int) or record_count < 0:
            raise ValueError(f"{key} has an invalid record count")
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ValueError(f"{key} has no valid SHA-256")
        local = readback_dir / key
        if not local.is_file():
            raise ValueError(f"missing readback file {local}")
        actual_sha = _sha256(local)
        if actual_sha != expected_sha:
            raise ValueError(f"{key} SHA-256 mismatch")
        result.append({**entry, "sha256": actual_sha})
        total_size += expected_size
        total_records += record_count

    summaries = collection.get("summaries")
    expected_summary = {
        "shard_count": len(items),
        "total_size_bytes": total_size,
        "total_records": total_records,
    }
    if not isinstance(summaries, dict):
        raise ValueError(f"{label} collection has no summaries")
    for field, expected in expected_summary.items():
        if summaries.get(field) != expected:
            raise ValueError(f"{label} collection {field} mismatch")
    return result


def _require_metadata_file(metadata_dir: Path, name: str) -> dict:
    path = metadata_dir / name
    if not path.is_file():
        raise ValueError(f"missing required metadata readback {name}")
    return _load_json(path)


# ---------------------------------------------------------------------------
# Optional experimental families (addresses, places).
#
# Core forward/reverse/id verification stays exact-set and fail-closed. An
# optional family is *added* to the expected object set only when the operator
# allowlists it (finalize `verify --families ...`) AND its #107 family manifest
# is present under the release and valid. The manifest is the create-only,
# digest-addressed marker published after (never before) its artifacts, so a
# present-and-valid manifest attests that every artifact it lists exists; verify
# re-proves that by byte-hashing each artifact from the readback. Family objects
# live under `{version}/families/{family}/`; the manifest is
# `family-manifest.json` under that prefix.
#
# Deviation from the design doc: the doc sketches `releases/{version}/places/...`
# and a `releases/` root, but the shipped finalizer uses the flat `{version}/`
# layout (forward `shards/`, `reverse/`, `id-index/`, `router.db`). Rather than
# fork the release root, families nest under `{version}/families/{family}/`.
# ---------------------------------------------------------------------------

FAMILY_PREFIX = "families"


def _family_manifest_key(family: str) -> str:
    return f"{FAMILY_PREFIX}/{family}/family-manifest.json"


def _is_safe_family_artifact_key(key: str, *, prefix: str, manifest_key: str) -> bool:
    """True iff ``key`` is a clean object key strictly under ``prefix``.

    Rejects the manifest key itself, any key outside the family prefix, and any
    key carrying a traversal or non-canonical path segment (``..``, ``.``, an
    empty segment, or a leading slash). Without the segment check a key like
    ``families/{family}/../../catalog.json`` passes a bare ``startswith(prefix)``
    test yet escapes the family prefix -- both admitting it into the expected set
    and, worse, making ``readback_dir / key`` resolve OUTSIDE the readback dir so
    the artifact hash proves nothing about the object stored under that key. This
    keeps every admitted artifact literally inside ``{version}/families/{family}/``.
    """
    if key == manifest_key or not key.startswith(prefix):
        return False
    return not any(segment in ("", ".", "..") for segment in key.split("/"))


def _verify_optional_family(
    *,
    family: str,
    release: str,
    inventory: dict[str, dict],
    metadata_dir: Path,
    readback_dir: Path,
) -> tuple[set[str], dict]:
    """Verify one allowlisted optional family, returning (expected_keys, summary).

    Loads and validates the family manifest (self-digest via #107), then
    byte-verifies every artifact it lists (present in the R2 inventory, present
    in the readback, size + SHA-256 match). Enforces a per-family exact-set gate:
    every non-manifest object under `families/{family}/` must be a manifest
    artifact, so an extra object squatting the family prefix fails closed here
    before the release-wide exact-set gate ever sees it. The returned key set is
    exactly the manifest object plus its artifacts, which the caller unions into
    the release expected set.
    """
    if family not in FAMILIES:
        raise ValueError(f"unknown optional family {family!r}")
    prefix = f"{FAMILY_PREFIX}/{family}/"
    manifest_key = _family_manifest_key(family)

    manifest_entry = inventory.get(manifest_key)
    manifest_local = metadata_dir / manifest_key
    if manifest_entry is None or not manifest_local.is_file():
        raise ValueError(
            f"optional family {family} manifest is missing from inventory or readback"
        )
    if manifest_entry["size_bytes"] != manifest_local.stat().st_size:
        raise ValueError(f"optional family {family} manifest size mismatch")
    manifest = validate_family_manifest(_load_json(manifest_local))
    if manifest["family"] != family:
        raise ValueError(
            f"optional family {family} manifest declares family {manifest['family']!r}"
        )
    if manifest["lineage"]["overture_release"] != release:
        raise ValueError(f"optional family {family} manifest release mismatch")

    artifact_keys: set[str] = set()
    objects: list[dict] = []
    for artifact in manifest["artifacts"]:
        key = artifact["object_key"]
        if not _is_safe_family_artifact_key(key, prefix=prefix, manifest_key=manifest_key):
            raise ValueError(
                f"optional family {family} artifact key is outside its prefix: {key}"
            )
        entry = inventory.get(key)
        local = readback_dir / key
        if entry is None or not local.is_file():
            raise ValueError(f"optional family {family} artifact missing: {key}")
        if entry["size_bytes"] != artifact["bytes"]:
            raise ValueError(f"optional family {family} artifact size mismatch: {key}")
        if _sha256(local) != artifact["sha256"]:
            raise ValueError(f"optional family {family} artifact SHA-256 mismatch: {key}")
        artifact_keys.add(key)
        objects.append({**entry, "sha256": artifact["sha256"]})

    # Per-family exact-set: nothing may live under the family prefix that the
    # manifest does not list (the manifest object itself excepted).
    actual_family_keys = {
        key for key in inventory if key.startswith(prefix) and key != manifest_key
    }
    if actual_family_keys != artifact_keys:
        extra = sorted(actual_family_keys - artifact_keys)[:20]
        missing = sorted(artifact_keys - actual_family_keys)[:20]
        raise ValueError(
            f"optional family {family} inventory mismatch: missing={missing}, extra={extra}"
        )

    expected_keys = {manifest_key} | artifact_keys
    summary = {
        "manifest": f"./{manifest_key}",
        "manifest_digest": manifest["manifest_digest"],
        "region": manifest["region"],
        "artifact_count": len(objects),
        "total_bytes": sum(obj["size_bytes"] for obj in objects),
        "objects": objects,
        # Optional families are never promotion targets: they add no catalog
        # link and this record exists only to document the verified fleet.
        "promotion_eligible": False,
    }
    return expected_keys, summary


# The release manifest is NOT byte-deterministic across verify runs: it embeds
# exactly one run-varying field, ``generated_at`` (the verify wall-clock,
# stamped in verify_release below). Every other field is a pure function of the
# immutable release fleet (relative keys, sizes, R2 ETags, producer SHA-256s,
# all iterated in sorted order), so two verify runs over the same objects must
# agree on this canonical form or one of them verified a different fleet.
_RUN_VARYING_MANIFEST_FIELDS = frozenset({"generated_at"})


def _canonical_manifest_form(manifest: dict) -> str:
    return json.dumps(
        {
            key: value
            for key, value in manifest.items()
            if key not in _RUN_VARYING_MANIFEST_FIELDS
        },
        sort_keys=True,
    )


def _matching_preexisting_manifest(
    *, entry: dict, metadata_dir: Path, version: str, manifest: dict
) -> bytes:
    """Return the pre-existing release-manifest.json bytes iff they match.

    Rerun tolerance for run 30360636219 (2026-07-28): attempt 1 published
    release-manifest.json and then failed in the promote smoke; attempt 2 then
    failed verify with ``unexpected=['release-manifest.json']``, bricking the
    rerun. A pre-existing manifest is acceptable only when its content
    canonically equals the manifest THIS verification just recomputed from the
    fleet (see ``_canonical_manifest_form``: byte comparison minus only the
    provably run-varying ``generated_at``). Anything else — absent from the
    metadata readback, size drift from the inventory entry, or any content
    difference — stays a hard failure, because it means the published manifest
    attests a fleet this verify did not prove.
    """
    local = metadata_dir / RELEASE_MANIFEST_KEY
    if not local.is_file():
        raise ValueError(
            f"{RELEASE_MANIFEST_KEY} is in the R2 inventory but missing from the "
            "metadata readback"
        )
    existing_bytes = local.read_bytes()
    if len(existing_bytes) != entry["size_bytes"]:
        raise ValueError(
            f"pre-existing {RELEASE_MANIFEST_KEY} size does not match its "
            "inventory entry"
        )
    existing = json.loads(existing_bytes)
    if not isinstance(existing, dict):
        raise ValueError(f"pre-existing {RELEASE_MANIFEST_KEY} is not a JSON object")
    if _canonical_manifest_form(existing) != _canonical_manifest_form(manifest):
        raise ValueError(
            f"version {version} already has a {RELEASE_MANIFEST_KEY} that does "
            "not match this verification (differences beyond the run-varying "
            "generated_at); refusing to overwrite or bless it"
        )
    return existing_bytes


def verify_release(
    *,
    version: str,
    release: str,
    inventory_path: Path,
    metadata_dir: Path,
    readback_dir: Path,
    output_path: Path,
    optional_families: list[str] | None = None,
) -> dict:
    _version_key(version)
    inventory = _inventory_by_relative_key(_load_json(inventory_path), version)

    # A finalize rerun finds the release manifest already under the version
    # prefix when the previous attempt failed AFTER "Publish immutable release
    # manifest" (run 30360636219, 2026-07-28: attempt 1 died in the promote
    # smoke, attempt 2 died here with unexpected=['release-manifest.json']).
    # Set that one key aside now — so the exact-set gate and the verified
    # object set match a first-run manifest exactly — and require at the end
    # that its content canonically equals the manifest this run recomputes;
    # any other content remains a hard failure.
    preexisting_manifest_entry = inventory.pop(RELEASE_MANIFEST_KEY, None)

    forward = _require_metadata_file(metadata_dir, "collection.json")
    reverse = _require_metadata_file(metadata_dir, "reverse-collection.json")
    id_collection = _require_metadata_file(metadata_dir, "id-collection.json")
    id_meta = _require_metadata_file(metadata_dir, "id-meta.json")
    id_locator_manifest = _require_metadata_file(metadata_dir, "id-locator-manifest.json")
    forward_build = _require_metadata_file(metadata_dir, "forward-build-meta.json")
    reverse_build = _require_metadata_file(metadata_dir, "reverse-build-meta.json")

    for name, build_meta, reverse_expected in (
        ("forward", forward_build, False),
        ("reverse", reverse_build, True),
    ):
        if build_meta.get("version") != version:
            raise ValueError(f"{name} build metadata version mismatch")
        if build_meta.get("overture_release") != release:
            raise ValueError(f"{name} build metadata release mismatch")
        if bool(build_meta.get("args", {}).get("reverse")) != reverse_expected:
            raise ValueError(f"{name} build metadata family mismatch")

    forward_objects = _verify_sqlite_family(
        label="forward",
        collection=forward,
        subdir="shards",
        inventory=inventory,
        readback_dir=readback_dir,
    )
    reverse_objects = _verify_sqlite_family(
        label="reverse",
        collection=reverse,
        subdir="reverse",
        inventory=inventory,
        readback_dir=readback_dir,
    )

    router = forward.get("router")
    if not isinstance(router, dict):
        raise ValueError("forward collection has no router metadata")
    if router.get("href") != "./router.db":
        raise ValueError("router.db has an invalid href")
    router_entry = inventory.get("router.db")
    router_file = readback_dir / "router.db"
    if router_entry is None or not router_file.is_file():
        raise ValueError("router.db is missing from inventory or readback")
    router_sha = _sha256(router_file)
    if router_entry["size_bytes"] != router.get("size_bytes"):
        raise ValueError("router.db size mismatch")
    if router_sha != router.get("sha256"):
        raise ValueError("router.db SHA-256 mismatch")

    expected_prefixes = {format(value, "03x") for value in range(16**3)}
    id_items = _collection_items(id_collection, "ID")
    if set(id_items) != expected_prefixes:
        raise ValueError("ID collection must contain exactly all 4096 three-hex prefixes")
    id_keys = {key for key in inventory if key.startswith("id-index/")}
    expected_id_keys = {f"id-index/{prefix}.parquet" for prefix in expected_prefixes}
    if id_keys != expected_id_keys:
        raise ValueError("R2 ID inventory must contain exactly 4096 expected shards")

    for source, label in ((id_meta, "id-meta"), (id_collection.get("summaries", {}), "ID collection")):
        if source.get("format_version") != 3:
            raise ValueError(f"{label} is not format v3")
        if source.get("overture_release") != release:
            raise ValueError(f"{label} release mismatch")
        if source.get("prefix_len") != 3 or source.get("shard_count") != 4096:
            raise ValueError(f"{label} shard contract mismatch")

    id_objects = []
    total_id_size = 0
    for prefix in sorted(expected_prefixes):
        key = f"id-index/{prefix}.parquet"
        entry = inventory[key]
        metadata = id_items[prefix]
        if not isinstance(metadata, dict) or metadata.get("href") != f"./{key}":
            raise ValueError(f"invalid ID metadata href for {prefix}")
        if metadata.get("size_bytes") != entry["size_bytes"] or entry["size_bytes"] <= 0:
            raise ValueError(f"ID shard size mismatch for {prefix}")
        expected_sha = metadata.get("sha256")
        if not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_sha):
            raise ValueError(f"ID shard {prefix} has no valid producer SHA-256")
        shard_object = {**entry, "sha256": expected_sha}
        # Older markers predate the content MD5. When the producer recorded
        # one, the shard was uploaded single-part with a Content-MD5, so R2
        # stored it whole and its ETag is that MD5 (no "-<parts>" suffix).
        # Binding the two rejects a byte-level replacement that keeps the size.
        content_md5 = metadata.get("content_md5")
        if content_md5 is not None:
            if not isinstance(content_md5, str) or not re.fullmatch(
                r"[0-9a-f]{32}", content_md5
            ):
                raise ValueError(f"ID shard {prefix} has an invalid content MD5")
            etag = entry["etag"]
            # The producer never multipart-uploads ID shards, so a "-<parts>"
            # ETag on a shard whose marker recorded a content MD5 is itself
            # evidence of an out-of-band replacement: fail closed rather than
            # skip the integrity binding. Legacy md5-less entries above keep
            # the compatibility skip.
            if "-" in etag:
                raise ValueError(
                    f"ID shard {prefix} has a multipart ETag ({etag}) but its "
                    f"marker records a content MD5: the shard was not produced "
                    f"by the single-part pipeline"
                )
            if etag != content_md5:
                raise ValueError(
                    f"ID shard {prefix} ETag does not match producer content MD5"
                )
            shard_object["content_md5"] = content_md5
        total_id_size += entry["size_bytes"]
        id_objects.append(shard_object)
    if id_collection["summaries"].get("total_size_bytes") != total_id_size:
        raise ValueError("ID collection total_size_bytes mismatch")

    dictionary = id_meta.get("locator_dictionary")
    if not isinstance(dictionary, dict):
        raise ValueError("id-meta has no locator dictionary reference")
    dictionary_href = dictionary.get("href")
    if not isinstance(dictionary_href, str) or not dictionary_href.startswith("./"):
        raise ValueError("invalid locator dictionary href")
    if id_collection["summaries"].get("locator_dictionary") != dictionary:
        raise ValueError("ID collection and id-meta locator dictionaries differ")
    if id_locator_manifest.get("format_version") != 3:
        raise ValueError("ID locator manifest is not format v3")
    if id_locator_manifest.get("overture_release") != release:
        raise ValueError("ID locator manifest release mismatch")
    if id_locator_manifest.get("locator_dictionary") != dictionary:
        raise ValueError("ID locator manifest dictionary mismatch")
    dictionary_key = dictionary_href[2:]
    dictionary_entry = inventory.get(dictionary_key)
    dictionary_path = metadata_dir / dictionary_key
    if dictionary_entry is None or not dictionary_path.is_file():
        raise ValueError("locator dictionary is missing from inventory or readback")
    if dictionary_entry["size_bytes"] != dictionary.get("size_bytes"):
        raise ValueError("locator dictionary size mismatch")
    if _sha256(dictionary_path) != dictionary.get("sha256"):
        raise ValueError("locator dictionary SHA-256 mismatch")

    # v3 fleets bind the locator dictionary to a permanent id-inventories set.
    # Parse the already-SHA-verified dictionary file and walk the committed
    # chain (dictionary -> inventory-set -> stage inventories) to derive the
    # EXACT id-inventories key set, byte-verifying each referenced object. The
    # exact-set gate then accepts exactly these keys and rejects any stray or
    # tampered id-inventories object. The referenced JSON is synced into
    # metadata_dir by the finalize job, alongside the dictionary file.
    dictionary_payload = _load_json(dictionary_path)
    inv_set_ref = dictionary_payload.get("input_inventory_set")
    inv_set_sha = dictionary_payload.get("input_inventory_set_sha256")
    if not isinstance(inv_set_ref, dict) or not isinstance(inv_set_sha, str):
        raise ValueError(
            "locator dictionary has no bound id-inventories set (not a v3 fleet)"
        )
    set_href = inv_set_ref.get("href")
    set_sha = inv_set_ref.get("sha256")
    if (
        not isinstance(set_sha, str)
        or not SHA256_RE.fullmatch(set_sha)
        or set_href != f"./{ID_INVENTORY_DIR}/inventory-set-{set_sha}.json"
    ):
        raise ValueError("invalid id-inventories set reference")
    if inv_set_ref.get("inventory_references_sha256") != inv_set_sha:
        raise ValueError("locator dictionary inventory-set binding mismatch")

    inv_set_key = set_href[2:]
    _verify_referenced_json_object(inv_set_key, inv_set_ref, inventory, metadata_dir)

    inv_set_payload = _load_json(metadata_dir / inv_set_key)
    stage_refs = inv_set_payload.get("inventories")
    if not isinstance(stage_refs, list) or not stage_refs:
        raise ValueError("id-inventories set has no stage inventories")

    expected_inventory_keys = {inv_set_key}
    for ref in stage_refs:
        if not isinstance(ref, dict):
            raise ValueError("invalid stage inventory reference")
        href = ref.get("href")
        sha = ref.get("sha256")
        if (
            not isinstance(sha, str)
            or not SHA256_RE.fullmatch(sha)
            or not isinstance(href, str)
            or not href.startswith(f"./{ID_INVENTORY_DIR}/")
            or not href.endswith(f"-{sha}.json")
        ):
            raise ValueError("invalid stage inventory reference")
        key = href[2:]
        if key in expected_inventory_keys:
            raise ValueError(f"duplicate id-inventories reference {key}")
        _verify_referenced_json_object(key, ref, inventory, metadata_dir)
        expected_inventory_keys.add(key)

    required_root = {
        "collection.json",
        "reverse-collection.json",
        "forward-build-meta.json",
        "reverse-build-meta.json",
        "router.db",
        "id-collection.json",
        "id-meta.json",
        "id-locator-manifest.json",
        dictionary_key,
    }
    # The version prefix must contain exactly the objects this release
    # verified: the required root files above, every forward and reverse SQLite
    # shard, all 4096 ID shards, and the referenced id-inventories objects.
    # `staging/` is transient build scaffolding (multi-GB, never a released
    # artifact) that now survives until post-finalize cleanup so a failed
    # finalize can be recovered; it is excluded here rather than blessed. Any
    # OTHER extra key (stray uploads) or a missing expected key fails closed.
    # release-manifest.json is uploaded only afterward, so it is legitimately
    # absent from a first-run listing; a rerun's pre-existing copy was set
    # aside above and is content-checked after the manifest is recomputed.
    expected_keys = set(required_root)
    expected_keys |= {f"shards/{shard_id}.db" for shard_id in _collection_items(forward, "forward")}
    expected_keys |= {f"reverse/{shard_id}.db" for shard_id in _collection_items(reverse, "reverse")}
    expected_keys |= expected_id_keys
    expected_keys |= expected_inventory_keys

    # Optional experimental families become expected exactly when allowlisted and
    # their manifest is present + valid; each verified family contributes exactly
    # its manifest object and artifacts. A family object present but NOT allowlisted
    # stays unexpected and fails the release-wide exact-set gate below. With no
    # allowlist this loop is skipped and the expected set is byte-identical to a
    # core-only release.
    optional_summaries: dict[str, dict] = {}
    for family in optional_families or []:
        if family in optional_summaries:
            raise ValueError(f"duplicate optional family {family!r}")
        family_expected, summary = _verify_optional_family(
            family=family,
            release=release,
            inventory=inventory,
            metadata_dir=metadata_dir,
            readback_dir=readback_dir,
        )
        expected_keys |= family_expected
        optional_summaries[family] = summary

    actual_keys = {key for key in inventory if not key.startswith("staging/")}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)[:20]
        unexpected = sorted(actual_keys - expected_keys)[:20]
        raise ValueError(
            f"version {version} object inventory is not an exact match: "
            f"missing={missing}, unexpected={unexpected}"
        )

    manifest = {
        "schema_version": 1,
        "version": version,
        "overture_release": release,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "families": {
            "forward": {
                "collection": "./collection.json",
                "shard_count": len(forward_objects),
                "objects": forward_objects,
                "router": {**router_entry, "sha256": router_sha},
            },
            "reverse": {
                "collection": "./reverse-collection.json",
                "shard_count": len(reverse_objects),
                "objects": reverse_objects,
            },
            "id": {
                "collection": "./id-collection.json",
                "format_version": 3,
                "shard_count": len(id_objects),
                "total_size_bytes": total_id_size,
                "objects": id_objects,
                "integrity": (
                    "producer SHA-256 and R2 size; single-part ETag verified "
                    "against the producer content MD5 when the inventory "
                    "records one"
                ),
                "locator_dictionary": dictionary,
            },
        },
        # Exact-set equality above proved every non-staging key under the
        # version prefix is one this release content-verified, so the verified
        # object set is the inventory minus the transient staging/ scaffolding.
        # release-manifest.json is uploaded from this payload afterward, so it
        # is intentionally absent rather than being a self-referential inventory.
        "verified_version_objects": [
            inventory[key]
            for key in sorted(inventory)
            if not key.startswith("staging/")
        ],
    }
    # Record the optional families only when at least one was verified, so a
    # core-only release (no allowlist) produces a byte-identical manifest.
    if optional_summaries:
        manifest["optional_families"] = optional_summaries
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if preexisting_manifest_entry is not None:
        existing_bytes = _matching_preexisting_manifest(
            entry=preexisting_manifest_entry,
            metadata_dir=metadata_dir,
            version=version,
            manifest=manifest,
        )
        # Adopt the already-published bytes (proven above to differ from the
        # recomputed manifest only in the run-varying generated_at) so the
        # subsequent "Publish immutable release manifest" step re-writes the
        # immutable object with identical bytes instead of a fresh timestamp.
        output_path.write_bytes(existing_bytes)
        return json.loads(existing_bytes.decode())
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def verify_families_only(
    *,
    version: str,
    release: str,
    inventory_path: Path,
    metadata_dir: Path,
    readback_dir: Path,
    output_path: Path,
    families: list[str],
) -> dict:
    """Verify a NON-PROMOTING, families-only slice under a ``slice-*`` prefix.

    ``verify_release`` proves a complete core forward/reverse/id release and can
    *add* optional families; a family-release slice has NO core fleet — its
    prefix holds exactly the allowlisted families' manifests and artifacts. This
    mode reuses the identical per-family verification (``_verify_optional_family``:
    manifest self-digest, artifact byte-hashing from the readback, and a
    per-family exact-set gate) and layers a slice-wide exact-set gate whose
    expected set is EXACTLY the union of the families' keys. So any core object,
    stray upload, or unlisted family object under the slice prefix fails closed,
    just as the release-wide gate does for a real release.

    It is structurally non-promoting: it never reads or writes a catalog, and the
    record is stamped ``is_slice``/``promotion_eligible: False``. The slice
    version is validated with ``_slice_version_key`` so a production-pattern
    version is rejected before any object is inspected.
    """
    _slice_version_key(version)
    if not families:
        raise ValueError("verify-families-only requires at least one family")
    inventory = _inventory_by_relative_key(_load_json(inventory_path), version)

    expected_keys: set[str] = set()
    summaries: dict[str, dict] = {}
    for family in families:
        if family in summaries:
            raise ValueError(f"duplicate family {family!r}")
        family_expected, summary = _verify_optional_family(
            family=family,
            release=release,
            inventory=inventory,
            metadata_dir=metadata_dir,
            readback_dir=readback_dir,
        )
        expected_keys |= family_expected
        summaries[family] = summary

    # A slice prefix must contain EXACTLY the verified family objects: no core
    # release files, no stray uploads. `staging/` scaffolding is excluded like
    # the release path (a slice build should not produce it, but excluding it
    # keeps the gate identical rather than special-casing).
    actual_keys = {key for key in inventory if not key.startswith("staging/")}
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)[:20]
        unexpected = sorted(actual_keys - expected_keys)[:20]
        raise ValueError(
            f"slice {version} object inventory is not an exact match: "
            f"missing={missing}, unexpected={unexpected}"
        )

    manifest = {
        "schema_version": 1,
        "slice_version": version,
        "overture_release": release,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        # This record documents a verified, NON-promoting family fleet. There is
        # no core release and no catalog link: a slice can never be promoted.
        "is_slice": True,
        "promotion_eligible": False,
        "families": summaries,
        "verified_version_objects": [
            inventory[key] for key in sorted(inventory) if not key.startswith("staging/")
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def build_catalog(*, before_path: Path, version: str, output_path: Path) -> dict:
    _version_key(version)
    catalog = _load_json(before_path)
    links = catalog.get("links")
    if not isinstance(links, list):
        raise ValueError("catalog has no links")

    static_links = []
    versions = {}
    for link in links:
        if not isinstance(link, dict):
            raise ValueError("catalog contains an invalid link")
        if link.get("rel") != "child":
            static_links.append(link)
            continue
        href = link.get("href")
        if not isinstance(href, str):
            raise ValueError("catalog child has no href")
        parts = href.strip("./").split("/")
        child_version = parts[0]
        _version_key(child_version)
        if child_version == version:
            raise ValueError(f"catalog already contains version {version}")
        if child_version in versions:
            raise ValueError(f"catalog contains duplicate version {child_version}")
        normalized = dict(link)
        normalized.pop("latest", None)
        versions[child_version] = normalized

    if versions and _version_key(version) <= max(map(_version_key, versions)):
        raise ValueError(f"candidate version {version} is not newer than the catalog")

    versions[version] = {
        "rel": "child",
        "href": f"./{version}/collection.json",
        "type": "application/json",
        "title": f"Geocoder shards {version}",
        "latest": True,
        "release_manifest": f"./{version}/release-manifest.json",
    }
    ordered = [versions[value] for value in sorted(versions, key=_version_key, reverse=True)]
    catalog["links"] = static_links + ordered
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(catalog, indent=2) + "\n")
    return catalog


# ---------------------------------------------------------------------------
# Atomic promotion, rollback, and crash-window recovery.
#
# These reimplement the finalize-release "promote and smoke" bash step and the
# post-finalize "recover" bash step so their guards live in a unit-tested layer
# rather than only in YAML + shell comments. All R2 and production-HTTP I/O is
# behind a small client so the orchestration can be exercised with a fake.
# ---------------------------------------------------------------------------


class PromotionError(RuntimeError):
    """A promotion guard failed; the workflow step must exit non-zero."""


class PreconditionFailed(PromotionError):
    """A conditional PUT was rejected by R2 with HTTP 412.

    Raised for both a create-only conflict (the immutable key already exists)
    and a compare-and-swap miss (the object changed under us). It is a hard
    error that is *never* retried: a lost create-only race or a lost CAS means
    another publisher won, and overwriting its object would be last-write-wins.
    """


class RecoveryError(RuntimeError):
    """Crash-window recovery could not safely reconcile the catalog."""


class PromotionInterrupted(Exception):
    """Raised from a SIGTERM handler so the finally/rollback path still runs.

    Mirrors the bash ``trap 'exit 143' TERM`` -> EXIT-trap rollback: a TERM
    while a publish is outstanding must trigger the same rollback that a normal
    failure does, not a bare process kill.
    """


def _load_json_bytes(data: bytes) -> dict:
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError("catalog must contain a JSON object")
    return value


def _latest_version(catalog: dict) -> str:
    """Return the version of the ``latest`` child link (mirrors the jq guard)."""
    for link in catalog.get("links", []):
        if (
            isinstance(link, dict)
            and link.get("rel") == "child"
            and link.get("latest") is True
        ):
            href = link.get("href")
            if not isinstance(href, str) or not href:
                raise ValueError("latest child link has no href")
            return href.strip("./").split("/")[0]
    raise ValueError("catalog has no latest child")


def _flush_log(message: str) -> None:
    """``print`` that flushes immediately: the promote/rollback default logger.

    Run 30360636219 (2026-07-28) emitted ~6.5 minutes of promote and rollback
    smoke output in ONE buffered flush at the end of the step, so the Actions
    per-line timestamps said nothing about when each attempt actually ran.
    Promotion diagnostics must land in the live log as they happen, even with
    stdout block-buffered (non-tty runner); the workflow additionally invokes
    this script with ``python -u``.
    """
    print(message, flush=True)


def _get_json(base_url: str, path: str, timeout: float) -> dict:
    """GET a JSON body, raising (like ``curl -f``) on any non-2xx status."""
    request = urllib.request.Request(base_url + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed host)
        body = response.read()
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object response")
    return value


def _smoke_production(base_url: str, version: str, log=_flush_log) -> bool:
    """health + a forward search + a reverse + an ID lookup, all pinned to
    ``version`` via the ``rebuild`` cache-buster. Ports the bash ``smoke_once``.

    Pass/fail semantics are unchanged; every check now logs the endpoint it
    hit and what it observed. Run 30360636219 (2026-07-28): all 14 promote
    attempts AND all 14 rollback attempts returned a bare False, leaving no
    way to tell "health still serves the prior version (stale catalog)" from
    "/search returned 500" from "search matched nothing". The per-check lines
    below make each failure mode name itself in the Actions log.
    """
    path = f"/health?rebuild={version}"
    try:
        health = _get_json(base_url, path, 20)
        if health.get("status") != "ok" or health.get("version") != version:
            log(
                f"smoke {path}: status={health.get('status')!r} "
                f"version={health.get('version')!r} "
                f"(want status='ok' version={version!r})"
            )
            return False
        log(f"smoke {path}: ok, version={health.get('version')!r}")

        path = f"/search?q=boston&limit=1&rebuild={version}"
        search = _get_json(base_url, path, 20)
        results = search.get("results")
        result_count = len(results) if isinstance(results, list) else None
        if search.get("data_version") != version or not isinstance(results, list) or not results:
            log(
                f"smoke {path}: data_version={search.get('data_version')!r} "
                f"result_count={result_count} "
                f"(want data_version={version!r} and at least one result)"
            )
            return False
        first = results[0]
        gers_id = first.get("gers_id") if isinstance(first, dict) else None
        if not isinstance(gers_id, str) or not gers_id:
            log(f"smoke {path}: first result has no usable gers_id ({gers_id!r})")
            return False
        log(
            f"smoke {path}: ok, data_version={search.get('data_version')!r} "
            f"result_count={result_count}"
        )

        path = f"/reverse?lat=42.36&lon=-71.06&rebuild={version}"
        reverse = _get_json(base_url, path, 20)
        if reverse.get("data_version") != version or not isinstance(reverse.get("gers_id"), str):
            log(
                f"smoke {path}: data_version={reverse.get('data_version')!r} "
                f"gers_id={reverse.get('gers_id')!r} "
                f"(want data_version={version!r} and a gers_id)"
            )
            return False
        log(f"smoke {path}: ok, data_version={reverse.get('data_version')!r}")

        path = f"/id/{gers_id}?rebuild={version}"
        id_json = _get_json(base_url, path, 30)
        if id_json.get("data_version") != version or id_json.get("id") != gers_id:
            log(
                f"smoke {path}: data_version={id_json.get('data_version')!r} "
                f"id={id_json.get('id')!r} "
                f"(want data_version={version!r} id={gers_id!r})"
            )
            return False
        log(f"smoke {path}: ok, data_version={id_json.get('data_version')!r}")
        return True
    except urllib.error.HTTPError as exc:
        # Before URLError: HTTPError subclasses it, and the status line is the
        # diagnostic that separates a 500 from a stale-but-healthy endpoint.
        log(f"smoke {path}: HTTP {exc.code} {exc.reason}")
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # URLError covers connection/timeout failures; OSError covers socket
        # timeouts; ValueError covers JSON decode errors.
        log(f"smoke {path}: {type(exc).__name__}: {exc}")
        return False


def _health_production(base_url: str, version: str, cache_buster: str, log=_flush_log) -> bool:
    """health-only check (ports the bash recovery-loop curl); logs like
    ``_smoke_production`` so a failed recovery names what production returned.
    """
    path = f"/health?{cache_buster}"
    try:
        health = _get_json(base_url, path, 20)
        if health.get("status") == "ok" and health.get("version") == version:
            log(f"health {path}: ok, version={health.get('version')!r}")
            return True
        log(
            f"health {path}: status={health.get('status')!r} "
            f"version={health.get('version')!r} "
            f"(want status='ok' version={version!r})"
        )
        return False
    except urllib.error.HTTPError as exc:
        log(f"health {path}: HTTP {exc.code} {exc.reason}")
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        log(f"health {path}: {type(exc).__name__}: {exc}")
        return False


def _content_etag(data: bytes) -> str:
    """The ETag R2 assigns a single-part PutObject: the quoted hex MD5.

    The root catalog and the operator-recovery backups are tiny JSON objects
    always written whole (single-part), so their stored ETag equals this digest.
    That lets a caller derive the expected-current precondition for a
    compare-and-swap from the *exact bytes it already validated*, instead of
    issuing a second, racy read to learn the live ETag.
    """
    return '"' + hashlib.md5(data, usedforsecurity=False).hexdigest() + '"'


def _is_precondition_failed(exc: subprocess.CalledProcessError) -> bool:
    """True when an aws failure is R2's 412 precondition rejection.

    The aws CLI surfaces R2's ``412 PreconditionFailed`` as a client error whose
    message names ``PreconditionFailed``; match that (or a bare 412) so a lost
    conditional write is distinguished from a transient/network failure.
    """
    blob = f"{exc.stdout or ''}{exc.stderr or ''}"
    return "PreconditionFailed" in blob or "412" in blob


def _is_unsupported_conditional_option(exc: subprocess.CalledProcessError) -> bool:
    """True when aws rejected ``--if-match``/``--if-none-match`` as unknown.

    An aws CLI too old to expose the PutObject conditional-write options rejects
    them at argument-parse time (``Unknown options: --if-match, ...``) rather
    than reaching R2. Detecting that lets the guard fail fast with an actionable
    "upgrade the CLI" message instead of retrying a request that can never
    succeed and reporting a misleading transient-publish failure.
    """
    blob = f"{exc.stdout or ''}{exc.stderr or ''}"
    lowered = blob.lower()
    names = ("--if-match" in blob) or ("--if-none-match" in blob)
    return names and ("unknown option" in lowered or "unrecognized argument" in lowered)


class R2Client:
    """Production client: shells out to the aws CLI and to
    ``scripts/r2_catalog_fetch.sh`` (whose two-consecutive-404 rule keeps an
    outage from being read as an empty catalog), and hits the production HTTPS
    endpoint with urllib for smoke checks.
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint: str,
        base_url: str,
        repo_root: Path,
        publish_attempts: int = 3,
        sleep=time.sleep,
    ) -> None:
        self.bucket = bucket
        self.endpoint = endpoint
        self.base_url = base_url
        self.repo_root = Path(repo_root)
        self.publish_attempts = publish_attempts
        self._sleep = sleep

    def _aws(self, *args: str, capture: bool = False):
        cmd = ["aws", *args, "--endpoint-url", self.endpoint, "--region", "auto"]
        return subprocess.run(cmd, check=True, text=True, capture_output=capture)

    def fetch_catalog(self) -> bytes | None:
        script = self.repo_root / "scripts" / "r2_catalog_fetch.sh"
        with tempfile.TemporaryDirectory() as work:
            dest = Path(work) / "catalog.json"
            result = subprocess.run(["bash", str(script), str(dest)], text=True)
            if result.returncode != 0:
                # Transient failure after retries; never treat as empty.
                raise PromotionError(
                    "catalog fetch failed (transient); refusing to proceed"
                )
            if not dest.exists():
                return None  # genuinely absent (first deploy)
            return dest.read_bytes()

    def _put_object(
        self,
        key: str,
        data: bytes,
        *,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        """PutObject via ``aws s3api`` so a conditional header can be attached.

        ``aws s3 cp`` cannot express preconditions, so the guarded writes use
        ``put-object`` directly. R2 evaluates ``If-None-Match``/``If-Match``
        server-side and rejects a miss with 412, which surfaces here as
        ``PreconditionFailed``; any other failure propagates as the raw
        ``CalledProcessError`` so callers can tell a lost race from a transient
        fault.
        """
        with tempfile.TemporaryDirectory() as work:
            body = Path(work) / "body"
            body.write_bytes(data)
            self._put_object_path(
                key,
                body,
                content_type="application/json",
                if_match=if_match,
                if_none_match=if_none_match,
            )

    def _put_object_path(
        self,
        key: str,
        body: Path,
        *,
        content_type: str,
        if_match: str | None = None,
        if_none_match: str | None = None,
    ) -> None:
        """Conditionally upload ``body`` without materializing it in memory."""
        args = [
            "s3api", "put-object",
            "--bucket", self.bucket, "--key", key,
            "--body", str(body),
            "--content-type", content_type,
            "--no-cli-pager",
        ]
        if if_none_match is not None:
            args += ["--if-none-match", if_none_match]
        if if_match is not None:
            args += ["--if-match", if_match]
        try:
            self._aws(*args, capture=True)
        except subprocess.CalledProcessError as exc:
            if _is_precondition_failed(exc):
                raise PreconditionFailed(
                    f"conditional PUT of {key} rejected by R2 (412 precondition failed)"
                ) from exc
            if _is_unsupported_conditional_option(exc):
                # A pre-2.15-ish aws CLI has no --if-match/--if-none-match, so
                # the guarded write silently degrades to unguarded. Refuse
                # rather than retry a request that can never parse.
                raise PromotionError(
                    "aws CLI does not support PutObject conditional writes "
                    "(--if-match/--if-none-match); upgrade the CLI on the runner "
                    "before publishing the production catalog"
                ) from exc
            raise

    def _download_object(self, key: str, dest: Path) -> None:
        self._aws(
            "s3", "cp", f"s3://{self.bucket}/{key}", str(dest), "--only-show-errors"
        )

    def _get_object(self, key: str) -> bytes:
        with tempfile.TemporaryDirectory() as work:
            dest = Path(work) / "obj"
            self._download_object(key, dest)
            return dest.read_bytes()

    def object_identity(self, key: str) -> tuple[int, str]:
        """Download one object to disk and return its size and streaming SHA-256."""
        with tempfile.TemporaryDirectory() as work:
            dest = Path(work) / "obj"
            self._download_object(key, dest)
            return dest.stat().st_size, _sha256(dest)

    def _verify_readback(self, key: str, data: bytes) -> None:
        """Read the object back and hard-fail if its digest is not ``data``.

        R2 enforces the precondition, but only an end-to-end readback proves the
        bytes it committed are the bytes we intended; a mismatch is never
        retried, it is a fatal integrity failure.
        """
        stored = self._get_object(key)
        if hashlib.sha256(stored).digest() != hashlib.sha256(data).digest():
            raise PromotionError(
                f"read-back digest mismatch for {key}: R2 stored unexpected bytes"
            )

    def publish_create_only(self, key: str, data: bytes) -> None:
        """Server-side create-only PUT (``If-None-Match: "*"``).

        R2 writes ``key`` only if it does not already exist. A concurrent
        duplicate publisher that already wrote it is rejected with 412, surfaced
        as a hard :class:`PreconditionFailed`; the existing immutable object is
        never overwritten. The write is then read back and SHA-256-verified.
        """
        self._put_object(key, data, if_none_match="*")
        self._verify_readback(key, data)

    def swap_expected_current(self, key: str, data: bytes, expected_etag: str) -> None:
        """Compare-and-swap PUT (``If-Match: expected_etag``).

        R2 replaces ``key`` only if its current ETag still equals
        ``expected_etag`` (derived by the caller from the exact bytes it
        validated). A 412 means another publisher changed the object first; it
        aborts immediately with :class:`PreconditionFailed` and never retries --
        a lost CAS must never degrade to last-write-wins. The write is then read
        back and SHA-256-verified.
        """
        self._put_object(key, data, if_match=expected_etag)
        self._verify_readback(key, data)

    def publish_catalog(self, data: bytes, *, expected_etag: str | None = None) -> None:
        """Publish the root catalog under an object-level precondition.

        With ``expected_etag`` this is a compare-and-swap (``If-Match``) so a
        catalog another publisher swapped in between our read and write is not
        clobbered; without it -- only a hypothetical first-ever publish, no live
        catalog exists -- it is create-only. A 412 (:class:`PreconditionFailed`)
        and a readback digest mismatch are both fatal and never retried; only a
        transient aws/network failure is retried.
        """
        for attempt in range(1, self.publish_attempts + 1):
            try:
                if expected_etag is None:
                    self.publish_create_only(CATALOG_KEY, data)
                else:
                    self.swap_expected_current(CATALOG_KEY, data, expected_etag)
                return
            except subprocess.CalledProcessError:
                # A 412 is raised as PreconditionFailed (not CalledProcessError)
                # and propagates without retry; this branch is only transient.
                if attempt >= self.publish_attempts:
                    raise PromotionError(
                        f"Failed to publish catalog after {self.publish_attempts} attempts"
                    )
                self._sleep(attempt * 10)

    def put_immutable(self, key: str, data: bytes) -> None:
        """Create-only publish that tolerates an identical re-publish.

        The single guarded write for immutable objects (backups, family
        artifacts, family manifests). A create-only 412 is fatal only when the
        object that already exists differs -- a genuine conflicting publisher;
        re-writing the identical bytes (an operator re-running a job that got
        partway through) stays idempotent rather than bricking the retry.
        """
        try:
            self.publish_create_only(key, data)
        except PreconditionFailed:
            expected = (len(data), hashlib.sha256(data).hexdigest())
            if self.object_identity(key) != expected:
                raise
            # Same key, same bytes: the immutable object already exists.

    def put_immutable_path(
        self,
        key: str,
        path: Path,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        """Create-only publish a large immutable file without loading it in RAM.

        The caller supplies the already-validated manifest identity. Re-check it
        immediately before upload, then verify R2's readback by streaming one
        downloaded object from disk. An identical pre-existing object makes a
        retry idempotent; any identity mismatch remains a hard conflict.
        """
        local_identity = (path.stat().st_size, _sha256(path))
        expected_identity = (expected_size, expected_sha256)
        if local_identity != expected_identity:
            raise PromotionError(f"local artifact changed before upload: {path}")
        try:
            self._put_object_path(
                key,
                path,
                content_type="application/octet-stream",
                if_none_match="*",
            )
        except PreconditionFailed:
            if self.object_identity(key) != expected_identity:
                raise
            return
        if self.object_identity(key) != expected_identity:
            raise PromotionError(
                f"read-back digest mismatch for {key}: R2 stored unexpected bytes"
            )

    def put_backup(self, name: str, data: bytes) -> None:
        """Write a durable operator-recovery copy, create-only (idempotent)."""
        self.put_immutable(f"{BACKUP_PREFIX}/{name}", data)

    def get_object(self, key: str) -> bytes:
        """Download an object's bytes (public wrapper for family re-verification)."""
        return self._get_object(key)

    def list_prefix(self, prefix: str) -> list[str]:
        """Return every object key under ``prefix`` (empty list when none)."""
        result = self._aws(
            "s3api", "list-objects-v2", "--bucket", self.bucket,
            "--prefix", prefix, "--query", "Contents[].Key", "--output", "json",
            capture=True,
        )
        keys = json.loads(result.stdout or "null")
        if keys is None:
            return []
        if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
            raise PromotionError(f"unexpected list-objects-v2 response for {prefix}")
        return keys

    def get_backup(self, name: str) -> bytes:
        key = f"s3://{self.bucket}/{BACKUP_PREFIX}/{name}"
        with tempfile.TemporaryDirectory() as work:
            dest = Path(work) / "obj.json"
            self._aws("s3", "cp", key, str(dest), "--only-show-errors")
            return dest.read_bytes()

    def backup_exists(self, name: str) -> bool:
        result = self._aws(
            "s3api", "list-objects-v2", "--bucket", self.bucket,
            "--prefix", f"{BACKUP_PREFIX}/{name}", "--max-keys", "1",
            "--query", "KeyCount", "--output", "text",
            capture=True,
        )
        return result.stdout.strip() != "0"

    def delete_version(self, version: str) -> None:
        self._aws(
            "s3", "rm", f"s3://{self.bucket}/{version}/",
            "--recursive", "--only-show-errors",
        )

    def smoke(self, version: str) -> bool:
        return _smoke_production(self.base_url, version)

    def health(self, version: str, *, cache_buster: str) -> bool:
        return _health_production(self.base_url, version, cache_buster)


def _confirm_serving(check, version, *, attempts, interval, sleep, log, label) -> bool:
    """Poll ``check(version)`` up to ``attempts`` times, sleeping between tries.

    Note: unlike the bash loop, this does not sleep after the final failed
    attempt (a trailing sleep only delays reporting the failure).
    """
    for attempt in range(1, attempts + 1):
        log(f"{label} attempt {attempt}/{attempts}...")
        if check(version):
            return True
        if attempt < attempts:
            sleep(interval)
    return False


def _install_promotion_signal_handlers():
    def _raise(signum, _frame):
        raise PromotionInterrupted(signum)

    previous = {}
    for sig in (signal.SIGTERM,):
        try:
            previous[sig] = signal.signal(sig, _raise)
        except (ValueError, OSError):
            pass  # not in the main thread / unsupported platform

    def _restore():
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    return _restore


def promote(
    client,
    *,
    version: str,
    before_bytes: bytes,
    candidate_bytes: bytes,
    smoke_attempts: int = 14,
    smoke_interval: int = 30,
    sleep=time.sleep,
    log=_flush_log,
    manage_signals: bool = True,
) -> None:
    """Atomically publish ``candidate`` as the production catalog and smoke it,
    rolling back to ``before`` on failure. Faithful port of the bash step; each
    guard below maps 1:1 to a bash guard.
    """
    _version_key(version)  # reject an impossible version before touching R2
    before = _load_json_bytes(before_bytes)
    previous_version = _latest_version(before)

    # Compare-before-swap: refuse to clobber a catalog another workflow changed
    # while this finalizer was reading and hashing the candidate fleet.
    prepublish = client.fetch_catalog()
    if prepublish is None:
        raise PromotionError("catalog.json absent at publish time; refusing to promote")
    if prepublish != before_bytes:
        raise PromotionError("catalog.json changed during finalization; refusing to clobber it")

    # Durable operator-recovery copies outside the immutable release prefix. The
    # in-process rollback handles normal failures; these survive runner loss and
    # are what post-finalize `recover` keys off of.
    client.put_backup(f"catalog-before-{version}.json", before_bytes)
    client.put_backup(f"catalog-candidate-{version}.json", candidate_bytes)

    def _rollback() -> None:
        log("::error::Restoring the exact previous catalog after failed promotion")
        live = client.fetch_catalog()
        if live != candidate_bytes:
            raise PromotionError(
                "Live catalog no longer equals this candidate; refusing rollback clobber"
            )
        # Compare-and-swap the previous catalog back only if the candidate we
        # just confirmed live is still current (If-Match on its content ETag).
        client.publish_catalog(
            before_bytes, expected_etag=_content_etag(candidate_bytes)
        )
        readback = client.fetch_catalog()
        if readback != before_bytes:
            raise PromotionError("rollback readback did not equal the previous catalog")
        # The worker isolate-caches catalog.json for up to CATALOG_CACHE_TTL;
        # confirm the prior version is served again before claiming rollback.
        if not _confirm_serving(
            client.smoke, previous_version,
            attempts=smoke_attempts, interval=smoke_interval,
            sleep=sleep, log=log, label="Rollback smoke",
        ):
            raise PromotionError(
                f"R2 rolled back but production did not confirm {previous_version}"
            )
        log(f"Rollback confirmed: production serves {previous_version}.")

    published = False
    restore_signals = _install_promotion_signal_handlers() if manage_signals else None
    try:
        log("Publishing catalog.json once after all family verification...")
        # Treat publication as possibly successful before invoking the client:
        # a connection can fail after R2 accepted the write.
        published = True
        # Expected-current swap: R2 accepts the candidate only if the live
        # catalog still equals `before` (If-Match on its content ETag), so a
        # catalog another publisher swapped in during finalization is rejected
        # server-side (412) rather than clobbered. The prepublish byte compare
        # above and this precondition together fail closed on a lost race.
        client.publish_catalog(
            candidate_bytes, expected_etag=_content_etag(before_bytes)
        )
        if not _confirm_serving(
            client.smoke, version,
            attempts=smoke_attempts, interval=smoke_interval,
            sleep=sleep, log=log, label="Production smoke",
        ):
            raise PromotionError("Production smoke failed")
        published = False
        log(f"Production health, search, reverse, and ID all serve {version}.")
    except BaseException:
        if published:
            try:
                _rollback()
            except BaseException as rollback_error:  # noqa: BLE001
                log(
                    "::error::Automatic catalog rollback needs operator attention: "
                    f"{rollback_error}"
                )
        raise
    finally:
        if restore_signals is not None:
            restore_signals()


def recover(
    client,
    *,
    version: str,
    health_attempts: int = 14,
    health_interval: int = 30,
    sleep=time.sleep,
    log=_flush_log,
) -> None:
    """Reconcile the catalog after a promotion that a hard runner loss cut off
    between publish and smoke. Uses the durable before/candidate pair and the
    same compare-before-swap refusal guard. Faithful port of the bash step.
    """
    _version_key(version)
    before_name = f"catalog-before-{version}.json"
    candidate_name = f"catalog-candidate-{version}.json"

    if not client.backup_exists(candidate_name):
        log("Promotion never reached durable candidate publication; nothing to recover.")
        return

    before_bytes = client.get_backup(before_name)
    candidate_bytes = client.get_backup(candidate_name)
    live = client.fetch_catalog()
    if live is None:
        raise RecoveryError("live catalog absent during recovery; refusing to proceed")

    if live == before_bytes:
        log("Previous catalog is already restored.")
    elif live == candidate_bytes:
        # CAS the previous catalog back, guarding on the interrupted candidate
        # still being live (If-Match on its content ETag).
        try:
            client.publish_catalog(
                before_bytes, expected_etag=_content_etag(candidate_bytes)
            )
        except PreconditionFailed as exc:
            # Another recoverer/publisher swapped the catalog between our read and
            # this CAS. The compare-and-swap aborted without clobbering anything;
            # the readback below decides whether the prior catalog nonetheless
            # ended up live (someone else restored it -> success) or a foreign
            # catalog is now live (-> refuse, as RecoveryError not a raw crash).
            log(f"Recovery compare-and-swap lost a race: {exc}")
        restored = client.fetch_catalog()
        if restored != before_bytes:
            raise RecoveryError("restore readback did not equal the previous catalog")
    else:
        raise RecoveryError(
            "Live catalog is neither the interrupted candidate nor its predecessor; "
            "refusing to clobber it"
        )

    previous_version = _latest_version(_load_json_bytes(before_bytes))
    for attempt in range(1, health_attempts + 1):
        if client.health(previous_version, cache_buster=f"recovery={version}-{attempt}"):
            log(f"Recovery confirmed: production serves {previous_version}.")
            return
        if attempt < health_attempts:
            sleep(health_interval)
    raise RecoveryError("Catalog restored in R2 but cached production did not recover")


def publish_family(
    client,
    *,
    version: str,
    family: str,
    manifest_path: Path,
    artifacts_root: Path,
    log=_flush_log,
) -> dict:
    """Publish one non-promoting optional family under ``{version}/families/{family}/``.

    Ordering mirrors id_index_protocol's "data before marker": every artifact is
    published create-only first, then the family manifest is published LAST so a
    present manifest always attests already-present artifacts (a crash mid-publish
    never leaves a manifest without its data). Publication is non-promoting: no
    catalog object is read or written. Steps:

    1. Validate the #107 manifest and verify it locally against ``artifacts_root``
       (every artifact present, size + SHA-256 match, keys inside the family
       prefix, no extra local file under the prefix).
    2. Stream each artifact from disk to a create-only upload (idempotent on an
       identical re-run).
    3. Publish the manifest last, create-only.
    4. Re-verify remotely: list the family prefix, stream-hash every non-manifest
       object one at a time, and check the fleet against the manifest via #107's
       ``verify_family_manifest_against_listing`` (catches a missing, extra,
       resized, or tampered remote object).
    """
    if family not in FAMILIES:
        raise ValueError(f"unknown optional family {family!r}")
    # A real release version OR a non-promoting slice version; both are
    # validated and calendar-checked, and the two patterns never collide.
    _publishable_version_key(version)
    manifest = validate_family_manifest(_load_json(manifest_path))
    if manifest["family"] != family:
        raise ValueError(
            f"manifest declares family {manifest['family']!r}, not {family!r}"
        )

    prefix = f"{FAMILY_PREFIX}/{family}/"
    manifest_key = _family_manifest_key(family)
    artifacts = manifest["artifacts"]

    # 1. Local verification (before any publish).
    local_by_key: dict[str, tuple[Path, int, str]] = {}
    for artifact in artifacts:
        key = artifact["object_key"]
        if not _is_safe_family_artifact_key(key, prefix=prefix, manifest_key=manifest_key):
            raise ValueError(f"artifact key is outside the family prefix: {key}")
        local = artifacts_root / key
        if not local.is_file():
            raise ValueError(f"artifact missing from local build: {key}")
        if local.stat().st_size != artifact["bytes"]:
            raise ValueError(f"artifact size mismatch for {key}")
        if _sha256(local) != artifact["sha256"]:
            raise ValueError(f"artifact SHA-256 mismatch for {key}")
        local_by_key[key] = (local, artifact["bytes"], artifact["sha256"])

    # No stray local artifact under the family prefix (the manifest excepted).
    prefix_root = artifacts_root / prefix
    if prefix_root.is_dir():
        for path in prefix_root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(artifacts_root).as_posix()
            if key != manifest_key and key not in local_by_key:
                raise ValueError(f"unexpected local artifact under family prefix: {key}")

    manifest_bytes = _family_canonical_json(manifest)

    # 2. Publish artifacts create-only (data before marker), deterministic order.
    for key in sorted(local_by_key):
        log(f"Publishing family artifact {version}/{key}")
        local, expected_size, expected_sha256 = local_by_key[key]
        client.put_immutable_path(
            f"{version}/{key}",
            local,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    # 3. Publish the manifest LAST.
    log(f"Publishing family manifest {version}/{manifest_key}")
    client.put_immutable(f"{version}/{manifest_key}", manifest_bytes)

    # 4. Exact remote key-set verification. Each artifact was already
    # independently stream-read back by put_immutable_path immediately after its
    # create-only upload (including identical resume objects). Avoid downloading
    # that planet-scale fleet a second time here: list exact keys, verify the
    # small completion manifest body, and re-run the manifest identity check from
    # those already-proven artifact identities. The slice finalizer performs one
    # fresh full-fleet streaming hash immediately before publishing its marker.
    expected_full_keys = {
        f"{version}/{artifact['object_key']}" for artifact in artifacts
    } | {f"{version}/{manifest_key}"}
    actual_full_keys = set(client.list_prefix(f"{version}/{prefix}"))
    if actual_full_keys != expected_full_keys:
        missing = sorted(expected_full_keys - actual_full_keys)[:20]
        extra = sorted(actual_full_keys - expected_full_keys)[:20]
        raise ValueError(
            "family manifest listing has missing or unexpected objects: "
            f"missing={missing}, unexpected={extra}"
        )
    expected_manifest_identity = (
        len(manifest_bytes),
        hashlib.sha256(manifest_bytes).hexdigest(),
    )
    if client.object_identity(f"{version}/{manifest_key}") != expected_manifest_identity:
        raise ValueError("remote family manifest identity differs after publication")
    listing = {
        artifact["object_key"]: (artifact["bytes"], artifact["sha256"])
        for artifact in artifacts
    }
    verify_family_manifest_against_listing(manifest, listing)
    log(
        f"Published and remotely verified family {family}: "
        f"{len(artifacts)} artifacts under {version}/{prefix}"
    )
    return manifest


def _build_client_from_env(args) -> R2Client:
    account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    endpoint = (
        getattr(args, "endpoint", None)
        or os.environ.get("R2_ENDPOINT")
        or (f"https://{account}.r2.cloudflarestorage.com" if account else None)
    )
    if not endpoint:
        raise SystemExit(
            "::error::R2 endpoint unknown: set CLOUDFLARE_ACCOUNT_ID or pass --endpoint"
        )
    base_url = getattr(args, "base_url", None) or os.environ.get(
        "GEOCODER_BASE_URL", BASE_URL_DEFAULT
    )
    bucket = getattr(args, "bucket", None) or os.environ.get("R2_BUCKET", BUCKET)
    # Let r2_catalog_fetch.sh + aws use the R2 S3 credentials when the caller
    # only exported the R2_* names.
    for aws_name, r2_name in (
        ("AWS_ACCESS_KEY_ID", "R2_ACCESS_KEY_ID"),
        ("AWS_SECRET_ACCESS_KEY", "R2_SECRET_ACCESS_KEY"),
    ):
        if not os.environ.get(aws_name) and os.environ.get(r2_name):
            os.environ[aws_name] = os.environ[r2_name]
    return R2Client(
        bucket=bucket,
        endpoint=endpoint,
        base_url=base_url,
        repo_root=Path(__file__).resolve().parent.parent,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--version", required=True)
    verify.add_argument("--release", required=True)
    verify.add_argument("--inventory", type=Path, required=True)
    verify.add_argument("--metadata-dir", type=Path, required=True)
    verify.add_argument("--readback-dir", type=Path, required=True)
    verify.add_argument("--output", type=Path, required=True)
    # Allowlist of optional experimental families to admit into the exact-set
    # gate (e.g. --families addresses,places). Omitted -> core-only, byte-
    # identical to today. A listed family whose manifest is absent fails closed;
    # a family object present but NOT listed stays unexpected and fails.
    verify.add_argument(
        "--families",
        default="",
        help="comma-separated optional families to verify (addresses,places)",
    )

    verify_families = subparsers.add_parser("verify-families-only")
    verify_families.add_argument("--version", required=True)
    verify_families.add_argument("--release", required=True)
    verify_families.add_argument("--inventory", type=Path, required=True)
    verify_families.add_argument("--metadata-dir", type=Path, required=True)
    verify_families.add_argument("--readback-dir", type=Path, required=True)
    verify_families.add_argument("--output", type=Path, required=True)
    # Required: a families-only slice has nothing else to verify. An empty or
    # omitted allowlist fails closed rather than verifying an empty prefix.
    verify_families.add_argument(
        "--families",
        required=True,
        help="comma-separated families in the slice (addresses,places)",
    )

    publish_family_p = subparsers.add_parser("publish-family")
    publish_family_p.add_argument("--version", required=True)
    publish_family_p.add_argument("--family", choices=sorted(FAMILIES), required=True)
    publish_family_p.add_argument("--manifest", type=Path, required=True)
    publish_family_p.add_argument("--artifacts-root", type=Path, required=True)
    publish_family_p.add_argument("--base-url", default=None)
    publish_family_p.add_argument("--endpoint", default=None)
    publish_family_p.add_argument("--bucket", default=None)

    catalog = subparsers.add_parser("catalog")
    catalog.add_argument("--before", type=Path, required=True)
    catalog.add_argument("--version", required=True)
    catalog.add_argument("--output", type=Path, required=True)

    promote_p = subparsers.add_parser("promote")
    promote_p.add_argument("--version", required=True)
    promote_p.add_argument("--before", type=Path, required=True)
    promote_p.add_argument("--candidate", type=Path, required=True)
    promote_p.add_argument("--base-url", default=None)
    promote_p.add_argument("--endpoint", default=None)
    promote_p.add_argument("--bucket", default=None)

    recover_p = subparsers.add_parser("recover")
    recover_p.add_argument("--version", required=True)
    recover_p.add_argument("--base-url", default=None)
    recover_p.add_argument("--endpoint", default=None)
    recover_p.add_argument("--bucket", default=None)

    args = parser.parse_args()
    if args.command == "promote":
        client = _build_client_from_env(args)
        try:
            promote(
                client,
                version=args.version,
                before_bytes=args.before.read_bytes(),
                candidate_bytes=args.candidate.read_bytes(),
            )
        except (PromotionError, RecoveryError) as exc:
            print(f"::error::{exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        return
    if args.command == "recover":
        client = _build_client_from_env(args)
        try:
            recover(client, version=args.version)
        except RecoveryError as exc:
            print(f"::error::{exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        return
    if args.command == "publish-family":
        client = _build_client_from_env(args)
        try:
            publish_family(
                client,
                version=args.version,
                family=args.family,
                manifest_path=args.manifest,
                artifacts_root=args.artifacts_root,
            )
        except (PromotionError, ValueError) as exc:
            print(f"::error::{exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        return
    if args.command == "verify-families-only":
        families = [name.strip() for name in args.families.split(",") if name.strip()]
        try:
            manifest = verify_families_only(
                version=args.version,
                release=args.release,
                inventory_path=args.inventory,
                metadata_dir=args.metadata_dir,
                readback_dir=args.readback_dir,
                output_path=args.output,
                families=families,
            )
        except ValueError as exc:
            print(f"::error::{exc}", file=sys.stderr, flush=True)
            sys.exit(1)
        summary = ", ".join(
            f"{name}={info['artifact_count']}"
            for name, info in sorted(manifest["families"].items())
        )
        print(f"Verified non-promoting slice {args.version}: {summary}")
        return
    if args.command == "verify":
        optional_families = [
            name.strip() for name in args.families.split(",") if name.strip()
        ]
        manifest = verify_release(
            version=args.version,
            release=args.release,
            inventory_path=args.inventory,
            metadata_dir=args.metadata_dir,
            readback_dir=args.readback_dir,
            output_path=args.output,
            optional_families=optional_families,
        )
        optional = manifest.get("optional_families", {})
        summary = ", ".join(
            f"{name}={info['artifact_count']}" for name, info in sorted(optional.items())
        )
        print(
            "Verified complete release: "
            f"forward={manifest['families']['forward']['shard_count']}, "
            f"reverse={manifest['families']['reverse']['shard_count']}, "
            f"id={manifest['families']['id']['shard_count']}"
            + (f", optional families: {summary}" if summary else "")
        )
    else:
        build_catalog(before_path=args.before, version=args.version, output_path=args.output)
        print(f"Prepared catalog with latest={args.version}")


if __name__ == "__main__":
    main()
