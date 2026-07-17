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


VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
ID_PREFIX_RE = re.compile(r"^[0-9a-f]{3}$")

# v3 fleets publish permanent id-inventories objects alongside the ID shards.
ID_INVENTORY_DIR = "id-inventories"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

# Root discovery catalog + durable operator-recovery backups.
BUCKET = "geocoder-shards"
CATALOG_KEY = "catalog.json"
BACKUP_PREFIX = "backups"
BASE_URL_DEFAULT = "https://geocoder.bradr.dev"


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


def verify_release(
    *,
    version: str,
    release: str,
    inventory_path: Path,
    metadata_dir: Path,
    readback_dir: Path,
    output_path: Path,
) -> dict:
    _version_key(version)
    inventory = _inventory_by_relative_key(_load_json(inventory_path), version)

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
    # absent from this listing.
    expected_keys = set(required_root)
    expected_keys |= {f"shards/{shard_id}.db" for shard_id in _collection_items(forward, "forward")}
    expected_keys |= {f"reverse/{shard_id}.db" for shard_id in _collection_items(reverse, "reverse")}
    expected_keys |= expected_id_keys
    expected_keys |= expected_inventory_keys
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


def _get_json(base_url: str, path: str, timeout: float) -> dict:
    """GET a JSON body, raising (like ``curl -f``) on any non-2xx status."""
    request = urllib.request.Request(base_url + path, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed host)
        body = response.read()
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object response")
    return value


def _smoke_production(base_url: str, version: str) -> bool:
    """health + a forward search + a reverse + an ID lookup, all pinned to
    ``version`` via the ``rebuild`` cache-buster. Ports the bash ``smoke_once``.
    """
    try:
        health = _get_json(base_url, f"/health?rebuild={version}", 20)
        if health.get("status") != "ok" or health.get("version") != version:
            return False

        search = _get_json(base_url, f"/search?q=boston&limit=1&rebuild={version}", 20)
        results = search.get("results")
        if search.get("data_version") != version or not isinstance(results, list) or not results:
            return False
        first = results[0]
        gers_id = first.get("gers_id") if isinstance(first, dict) else None
        if not isinstance(gers_id, str) or not gers_id:
            return False

        reverse = _get_json(base_url, f"/reverse?lat=42.36&lon=-71.06&rebuild={version}", 20)
        if reverse.get("data_version") != version or not isinstance(reverse.get("gers_id"), str):
            return False

        id_json = _get_json(base_url, f"/id/{gers_id}?rebuild={version}", 30)
        if id_json.get("data_version") != version or id_json.get("id") != gers_id:
            return False
        return True
    except (urllib.error.URLError, OSError, ValueError):
        # URLError covers HTTPError (>=400) and connection/timeout failures;
        # OSError covers socket timeouts; ValueError covers JSON decode errors.
        return False


def _health_production(base_url: str, version: str, cache_buster: str) -> bool:
    """health-only check (ports the bash recovery-loop curl)."""
    try:
        health = _get_json(base_url, f"/health?{cache_buster}", 20)
        return health.get("status") == "ok" and health.get("version") == version
    except (urllib.error.URLError, OSError, ValueError):
        return False


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

    def publish_catalog(self, data: bytes) -> None:
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "catalog.json"
            src.write_bytes(data)
            dest = f"s3://{self.bucket}/{CATALOG_KEY}"
            for attempt in range(1, self.publish_attempts + 1):
                try:
                    self._aws(
                        "s3", "cp", str(src), dest,
                        "--content-type", "application/json", "--only-show-errors",
                    )
                    return
                except subprocess.CalledProcessError:
                    if attempt >= self.publish_attempts:
                        raise PromotionError(
                            f"Failed to publish catalog after {self.publish_attempts} attempts"
                        )
                    self._sleep(attempt * 10)

    def put_backup(self, name: str, data: bytes) -> None:
        key = f"s3://{self.bucket}/{BACKUP_PREFIX}/{name}"
        with tempfile.TemporaryDirectory() as work:
            src = Path(work) / "src.json"
            src.write_bytes(data)
            self._aws(
                "s3", "cp", str(src), key,
                "--content-type", "application/json", "--only-show-errors",
            )
            readback = Path(work) / "readback.json"
            self._aws("s3", "cp", key, str(readback), "--only-show-errors")
            if readback.read_bytes() != data:
                raise PromotionError(f"durable backup readback mismatch for {name}")

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
    log=print,
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
        client.publish_catalog(before_bytes)
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
        client.publish_catalog(candidate_bytes)
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
    log=print,
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
        client.publish_catalog(before_bytes)
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
            print(f"::error::{exc}", file=sys.stderr)
            sys.exit(1)
        return
    if args.command == "recover":
        client = _build_client_from_env(args)
        try:
            recover(client, version=args.version)
        except RecoveryError as exc:
            print(f"::error::{exc}", file=sys.stderr)
            sys.exit(1)
        return
    if args.command == "verify":
        manifest = verify_release(
            version=args.version,
            release=args.release,
            inventory_path=args.inventory,
            metadata_dir=args.metadata_dir,
            readback_dir=args.readback_dir,
            output_path=args.output,
        )
        print(
            "Verified complete release: "
            f"forward={manifest['families']['forward']['shard_count']}, "
            f"reverse={manifest['families']['reverse']['shard_count']}, "
            f"id={manifest['families']['id']['shard_count']}"
        )
    else:
        build_catalog(before_path=args.before, version=args.version, output_path=args.output)
        print(f"Prepared catalog with latest={args.version}")


if __name__ == "__main__":
    main()
