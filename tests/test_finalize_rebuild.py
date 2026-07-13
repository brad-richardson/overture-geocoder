import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import finalize_rebuild as fr


VERSION = "2026-07-13.0"
RELEASE = "2026-06-17.0"


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def _entry(key, size):
    return {
        "Key": f"{VERSION}/{key}",
        "Size": size,
        "ETag": f'"etag-{key}"',
    }


def _release_fixture(tmp_path):
    metadata = tmp_path / "metadata"
    readback = tmp_path / "readback"
    metadata.mkdir()
    (readback / "shards").mkdir(parents=True)
    (readback / "reverse").mkdir(parents=True)

    forward_bytes = b"forward"
    reverse_bytes = b"reverse"
    router_bytes = b"router"
    (readback / "shards" / "AA.db").write_bytes(forward_bytes)
    (readback / "reverse" / "AA.db").write_bytes(reverse_bytes)
    (readback / "router.db").write_bytes(router_bytes)

    forward_sha = hashlib.sha256(forward_bytes).hexdigest()
    reverse_sha = hashlib.sha256(reverse_bytes).hexdigest()
    router_sha = hashlib.sha256(router_bytes).hexdigest()
    _write_json(
        metadata / "collection.json",
        {
            "items": {
                "AA": {
                    "href": "./shards/AA.db",
                    "size_bytes": len(forward_bytes),
                    "sha256": forward_sha,
                }
            },
            "router": {
                "href": "./router.db",
                "size_bytes": len(router_bytes),
                "sha256": router_sha,
            },
        },
    )
    _write_json(
        metadata / "reverse-collection.json",
        {
            "items": {
                "AA": {
                    "href": "./reverse/AA.db",
                    "size_bytes": len(reverse_bytes),
                    "sha256": reverse_sha,
                }
            }
        },
    )

    dictionary_bytes = b'{"dictionary":"v3"}'
    dictionary_sha = hashlib.sha256(dictionary_bytes).hexdigest()
    dictionary_name = f"id-locator-dictionary-{dictionary_sha}.json"
    (metadata / dictionary_name).write_bytes(dictionary_bytes)
    dictionary = {
        "href": f"./{dictionary_name}",
        "sha256": dictionary_sha,
        "size_bytes": len(dictionary_bytes),
    }
    id_items = {
        format(value, "03x"): {
            "href": f"./id-index/{value:03x}.parquet",
            "size_bytes": 10 + value,
        }
        for value in range(4096)
    }
    total_id_size = sum(value["size_bytes"] for value in id_items.values())
    id_contract = {
        "format_version": 3,
        "overture_release": RELEASE,
        "prefix_len": 3,
        "shard_count": 4096,
        "locator_dictionary": dictionary,
    }
    _write_json(metadata / "id-meta.json", id_contract)
    _write_json(
        metadata / "id-collection.json",
        {
            "summaries": {**id_contract, "total_size_bytes": total_id_size},
            "items": id_items,
        },
    )
    for family, reverse_flag in (("forward", False), ("reverse", True)):
        _write_json(
            metadata / f"{family}-build-meta.json",
            {
                "version": VERSION,
                "overture_release": RELEASE,
                "args": {"reverse": reverse_flag},
            },
        )
    _write_json(
        metadata / "id-locator-manifest.json",
        {
            "format_version": 3,
            "overture_release": RELEASE,
            "locator_dictionary": dictionary,
        },
    )

    entries = [
        _entry("collection.json", (metadata / "collection.json").stat().st_size),
        _entry("reverse-collection.json", (metadata / "reverse-collection.json").stat().st_size),
        _entry("forward-build-meta.json", (metadata / "forward-build-meta.json").stat().st_size),
        _entry("reverse-build-meta.json", (metadata / "reverse-build-meta.json").stat().st_size),
        _entry("id-collection.json", (metadata / "id-collection.json").stat().st_size),
        _entry("id-meta.json", (metadata / "id-meta.json").stat().st_size),
        _entry("id-locator-manifest.json", (metadata / "id-locator-manifest.json").stat().st_size),
        _entry(dictionary_name, len(dictionary_bytes)),
        _entry("shards/AA.db", len(forward_bytes)),
        _entry("reverse/AA.db", len(reverse_bytes)),
        _entry("router.db", len(router_bytes)),
    ]
    entries.extend(
        _entry(f"id-index/{prefix}.parquet", item["size_bytes"])
        for prefix, item in id_items.items()
    )
    inventory = tmp_path / "inventory.json"
    _write_json(inventory, {"Contents": entries})
    return metadata, readback, inventory


def test_verify_release_writes_complete_exact_manifest(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    output = tmp_path / "release-manifest.json"

    manifest = fr.verify_release(
        version=VERSION,
        release=RELEASE,
        inventory_path=inventory,
        metadata_dir=metadata,
        readback_dir=readback,
        output_path=output,
    )

    assert manifest["families"]["forward"]["shard_count"] == 1
    assert manifest["families"]["reverse"]["shard_count"] == 1
    assert manifest["families"]["id"]["shard_count"] == 4096
    assert manifest["families"]["id"]["total_size_bytes"] > 0
    assert len(manifest["verified_version_objects"]) == 4107
    assert json.loads(output.read_text())["version"] == VERSION


def test_verify_release_rejects_missing_id_shard(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    value = json.loads(inventory.read_text())
    value["Contents"] = [
        item for item in value["Contents"]
        if not item["Key"].endswith("/id-index/fff.parquet")
    ]
    _write_json(inventory, value)

    with pytest.raises(ValueError, match="exactly 4096"):
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )


def test_build_catalog_publishes_once_and_sorts_numeric_suffixes(tmp_path):
    before = tmp_path / "before.json"
    output = tmp_path / "next.json"
    _write_json(
        before,
        {
            "links": [
                {"rel": "self", "href": "./catalog.json"},
                {
                    "rel": "child",
                    "href": "./2026-07-02.2/collection.json",
                    "latest": True,
                },
                {"rel": "child", "href": "./2026-07-02.10/collection.json"},
            ]
        },
    )

    catalog = fr.build_catalog(before_path=before, version=VERSION, output_path=output)
    children = [link for link in catalog["links"] if link.get("rel") == "child"]
    assert children[0]["href"] == f"./{VERSION}/collection.json"
    assert children[0]["release_manifest"] == f"./{VERSION}/release-manifest.json"
    assert [link["href"] for link in children[1:]] == [
        "./2026-07-02.10/collection.json",
        "./2026-07-02.2/collection.json",
    ]
    assert [link.get("latest", False) for link in children] == [True, False, False]


def test_build_catalog_rejects_existing_version(tmp_path):
    before = tmp_path / "before.json"
    _write_json(
        before,
        {"links": [{"rel": "child", "href": f"./{VERSION}/collection.json"}]},
    )
    with pytest.raises(ValueError, match="already contains"):
        fr.build_catalog(before_path=before, version=VERSION, output_path=tmp_path / "next.json")
