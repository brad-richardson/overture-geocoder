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
import re
from datetime import date, datetime, timezone
from pathlib import Path


VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
ID_PREFIX_RE = re.compile(r"^[0-9a-f]{3}$")


def _load_json(path: Path) -> dict:
    with path.open() as src:
        value = json.load(src)
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
        total_id_size += entry["size_bytes"]
        id_objects.append({**entry, "sha256": expected_sha})
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
    missing_root = sorted(required_root - set(inventory))
    if missing_root:
        raise ValueError(f"required release objects are missing: {missing_root}")

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
                "integrity": "producer SHA-256 plus R2 size/ETag and validated Parquet schema/footer",
                "locator_dictionary": dictionary,
            },
        },
        # release-manifest.json itself is uploaded from this payload afterward,
        # so this is intentionally the complete verified input set rather than
        # a self-referential inventory.
        "verified_version_objects": [inventory[key] for key in sorted(inventory)],
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

    args = parser.parse_args()
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
