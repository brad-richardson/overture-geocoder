import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import finalize_rebuild as fr
import prune_catalog as pc


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
                    "record_count": 1,
                    "size_bytes": len(forward_bytes),
                    "sha256": forward_sha,
                }
            },
            "summaries": {
                "shard_count": 1,
                "total_records": 1,
                "total_size_bytes": len(forward_bytes),
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
                    "record_count": 1,
                    "size_bytes": len(reverse_bytes),
                    "sha256": reverse_sha,
                }
            },
            "summaries": {
                "shard_count": 1,
                "total_records": 1,
                "total_size_bytes": len(reverse_bytes),
            },
        },
    )

    # Build the v3 id-inventories chain bottom-up: stage inventory -> inventory
    # set -> locator dictionary. The finalizer binds each object by byte-SHA, so
    # the aggregate/scope shas need only be internally consistent (the finalizer
    # never recomputes them), and each referenced object's href embeds its own
    # content sha.
    inv_dir = metadata / "id-inventories"
    inv_dir.mkdir()
    agg_sha = hashlib.sha256(b"inventory-references").hexdigest()
    scope_sha = "0" * 16
    stage_bytes = json.dumps(
        {"kind": "registry_range", "scope": {"kind": "registry_range"}}
    ).encode()
    stage_sha = hashlib.sha256(stage_bytes).hexdigest()
    stage_name = f"registry_range-{scope_sha}-{stage_sha}.json"
    (inv_dir / stage_name).write_bytes(stage_bytes)
    stage_ref = {
        "href": f"./id-inventories/{stage_name}",
        "sha256": stage_sha,
        "size_bytes": len(stage_bytes),
        "kind": "registry_range",
        "scope": {"kind": "registry_range"},
    }
    inv_set_bytes = json.dumps(
        {"inventories": [stage_ref], "inventory_references_sha256": agg_sha}
    ).encode()
    set_sha = hashlib.sha256(inv_set_bytes).hexdigest()
    set_name = f"inventory-set-{set_sha}.json"
    (inv_dir / set_name).write_bytes(inv_set_bytes)
    inv_set_ref = {
        "href": f"./id-inventories/{set_name}",
        "sha256": set_sha,
        "size_bytes": len(inv_set_bytes),
        "inventory_references_sha256": agg_sha,
        "inventories_count": 1,
    }
    dictionary_bytes = json.dumps(
        {
            "dictionary": "v3",
            "input_inventory_set_sha256": agg_sha,
            "input_inventory_set": inv_set_ref,
        }
    ).encode()
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
            "sha256": hashlib.sha256(f"id-{value:03x}".encode()).hexdigest(),
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
        _entry(f"id-inventories/{set_name}", len(inv_set_bytes)),
        _entry(f"id-inventories/{stage_name}", len(stage_bytes)),
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
    assert len(manifest["verified_version_objects"]) == 4109
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


def test_verify_release_rejects_unexpected_version_object(tmp_path):
    # An object under the version prefix that no family or root file accounts
    # for (e.g. half-cleaned staging residue) got no content verification, so
    # the exact-inventory gate must fail closed and name it rather than bless
    # it in the manifest.
    metadata, readback, inventory = _release_fixture(tmp_path)
    value = json.loads(inventory.read_text())
    value["Contents"].append(_entry("staging/build-000-3ff/_SUCCESS", 12))
    _write_json(inventory, value)

    with pytest.raises(ValueError, match="not an exact match") as excinfo:
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )
    assert "staging/build-000-3ff/_SUCCESS" in str(excinfo.value)


def test_verify_release_rejects_stray_id_inventory_object(tmp_path):
    # id-inventories objects are accepted only when the trusted locator chain
    # references them; an unreferenced object under the prefix must still fail
    # the exact-set gate (the prefix is not blessed wholesale).
    metadata, readback, inventory = _release_fixture(tmp_path)
    stray = f"id-inventories/registry_range-ffffffffffffffff-{'a' * 64}.json"
    value = json.loads(inventory.read_text())
    value["Contents"].append(_entry(stray, 5))
    _write_json(inventory, value)

    with pytest.raises(ValueError, match="not an exact match") as excinfo:
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )
    assert stray in str(excinfo.value)


def test_verify_release_rejects_tampered_stage_inventory(tmp_path):
    # Each referenced id-inventories object is byte-SHA-bound to its reference,
    # so replacing its content (keeping the key) fails closed.
    metadata, readback, inventory = _release_fixture(tmp_path)
    stage_file = next((metadata / "id-inventories").glob("registry_range-*.json"))
    stage_file.write_bytes(b'{"tampered": true}')

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )


@pytest.mark.parametrize(
    ("file_name", "mutation", "message"),
    [
        (
            "collection.json",
            lambda value: value["items"]["AA"].update(href="./wrong.db"),
            "invalid href",
        ),
        (
            "reverse-collection.json",
            lambda value: value["summaries"].update(total_records=2),
            "total_records mismatch",
        ),
        (
            "id-collection.json",
            lambda value: value["items"]["000"].update(sha256="bad"),
            "producer SHA-256",
        ),
    ],
)
def test_verify_release_rejects_metadata_integrity_mismatch(
    tmp_path, file_name, mutation, message
):
    metadata, readback, inventory = _release_fixture(tmp_path)
    path = metadata / file_name
    value = json.loads(path.read_text())
    mutation(value)
    _write_json(path, value)

    with pytest.raises(ValueError, match=message):
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )


def _bind_id_shard(metadata, inventory, prefix, *, content_md5, etag):
    """Attach content_md5 to an id-collection item and set its R2 ETag."""
    coll_path = metadata / "id-collection.json"
    coll = json.loads(coll_path.read_text())
    if content_md5 is not None:
        coll["items"][prefix]["content_md5"] = content_md5
    _write_json(coll_path, coll)

    inv = json.loads(inventory.read_text())
    key = f"{VERSION}/id-index/{prefix}.parquet"
    for item in inv["Contents"]:
        if item["Key"] == key:
            item["ETag"] = etag
    _write_json(inventory, inv)


def test_verify_release_binds_single_part_etag_to_content_md5(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    content_md5 = hashlib.md5(b"000").hexdigest()
    _bind_id_shard(
        metadata, inventory, "000",
        content_md5=content_md5, etag=f'"{content_md5}"',
    )

    manifest = fr.verify_release(
        version=VERSION,
        release=RELEASE,
        inventory_path=inventory,
        metadata_dir=metadata,
        readback_dir=readback,
        output_path=tmp_path / "manifest.json",
    )
    bound = next(
        obj for obj in manifest["families"]["id"]["objects"]
        if obj["etag"] == content_md5
    )
    assert bound["content_md5"] == content_md5


def test_verify_release_rejects_single_part_etag_content_md5_mismatch(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    _bind_id_shard(
        metadata, inventory, "000",
        content_md5=hashlib.md5(b"000").hexdigest(),
        etag=f'"{hashlib.md5(b"tampered").hexdigest()}"',
    )

    with pytest.raises(ValueError, match="ETag does not match producer content MD5"):
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )


def test_verify_release_rejects_multipart_etag_with_content_md5(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    content_md5 = hashlib.md5(b"000").hexdigest()
    # The producer only ever uploads ID shards single-part with a Content-MD5,
    # so a multipart ("-<parts>") ETag on a shard whose marker records a
    # content_md5 is evidence of out-of-band replacement and must fail closed.
    _bind_id_shard(
        metadata, inventory, "000",
        content_md5=content_md5, etag='"deadbeefdeadbeefdeadbeefdeadbeef-4"',
    )

    with pytest.raises(ValueError, match="single-part pipeline"):
        fr.verify_release(
            version=VERSION,
            release=RELEASE,
            inventory_path=inventory,
            metadata_dir=metadata,
            readback_dir=readback,
            output_path=tmp_path / "manifest.json",
        )


def test_verify_release_passes_without_content_md5(tmp_path):
    # Older markers carry no content_md5; verification must still succeed.
    metadata, readback, inventory = _release_fixture(tmp_path)
    manifest = fr.verify_release(
        version=VERSION,
        release=RELEASE,
        inventory_path=inventory,
        metadata_dir=metadata,
        readback_dir=readback,
        output_path=tmp_path / "manifest.json",
    )
    assert all(
        "content_md5" not in obj for obj in manifest["families"]["id"]["objects"]
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


def test_build_catalog_rejects_non_monotonic_or_invalid_version(tmp_path):
    before = tmp_path / "before.json"
    _write_json(
        before,
        {"links": [{"rel": "child", "href": "./2026-07-14.0/collection.json"}]},
    )
    with pytest.raises(ValueError, match="not newer"):
        fr.build_catalog(before_path=before, version=VERSION, output_path=tmp_path / "next.json")
    with pytest.raises(ValueError):
        fr.build_catalog(
            before_path=before,
            version="2026-02-30.0",
            output_path=tmp_path / "next.json",
        )


# ---------------------------------------------------------------------------
# promote / recover: orchestration exercised through a fake R2/HTTP client.
# ---------------------------------------------------------------------------

PREVIOUS = "2026-07-10.0"
NEW = "2026-07-14.0"


def _catalog_bytes(latest, others=()):
    links = [{"rel": "self", "href": "./catalog.json"}]
    links.append({"rel": "child", "href": f"./{latest}/collection.json", "latest": True})
    for version in others:
        links.append({"rel": "child", "href": f"./{version}/collection.json"})
    return (json.dumps({"links": links}, indent=2) + "\n").encode()


def _latest_of(catalog_bytes):
    return fr._latest_version(json.loads(catalog_bytes))


class FakeClient:
    """Records R2 mutations and models production serving the live catalog's
    latest version. ``smoke_fn`` / ``health_fn`` may override the default.
    """

    def __init__(self, *, catalog, smoke_fn=None, health_fn=None):
        self.catalog = catalog
        self.backups = {}
        self.published = []
        self.deleted = []
        self._smoke_fn = smoke_fn
        self._health_fn = health_fn

    def fetch_catalog(self):
        return self.catalog

    def publish_catalog(self, data):
        self.catalog = data
        self.published.append(data)

    def put_backup(self, name, data):
        self.backups[name] = data

    def get_backup(self, name):
        return self.backups[name]

    def backup_exists(self, name):
        return name in self.backups

    def delete_version(self, version):
        self.deleted.append(version)

    def smoke(self, version):
        if self._smoke_fn is not None:
            return self._smoke_fn(version, self)
        return version == _latest_of(self.catalog)

    def health(self, version, *, cache_buster):
        if self._health_fn is not None:
            return self._health_fn(version, self, cache_buster)
        return version == _latest_of(self.catalog)


def _promote(client, **kwargs):
    fr.promote(
        client,
        version=NEW,
        before_bytes=kwargs.pop("before"),
        candidate_bytes=kwargs.pop("candidate"),
        smoke_attempts=kwargs.pop("smoke_attempts", 2),
        smoke_interval=0,
        sleep=lambda _seconds: None,
        log=lambda *_a, **_k: None,
        manage_signals=False,
        **kwargs,
    )


def test_promote_happy_path_publishes_and_smokes():
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    client = FakeClient(catalog=before)

    _promote(client, before=before, candidate=candidate)

    assert client.catalog == candidate  # published, never rolled back
    assert client.published == [candidate]
    assert client.backups[f"catalog-before-{NEW}.json"] == before
    assert client.backups[f"catalog-candidate-{NEW}.json"] == candidate


def test_promote_refuses_when_catalog_changed_during_finalization():
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    intruder = _catalog_bytes(latest="2026-07-11.0")
    client = FakeClient(catalog=intruder)  # live != before at prepublish

    with pytest.raises(fr.PromotionError, match="changed during finalization"):
        _promote(client, before=before, candidate=candidate)

    assert client.published == []  # nothing published
    assert client.backups == {}  # the compare-before-swap guard precedes backups


def test_promote_rolls_back_on_smoke_failure():
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    # New version never smokes clean; the restored previous does.
    client = FakeClient(catalog=before, smoke_fn=lambda version, _c: version == PREVIOUS)

    with pytest.raises(fr.PromotionError, match="Production smoke failed"):
        _promote(client, before=before, candidate=candidate)

    assert client.catalog == before  # rolled back
    assert client.published == [candidate, before]  # publish then rollback-publish


def test_promote_refuses_rollback_clobber():
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    intruder = _catalog_bytes(latest="2026-07-15.0")

    def smoke_fn(_version, client):
        client.catalog = intruder  # a concurrent writer lands during smoke
        return False

    client = FakeClient(catalog=before, smoke_fn=smoke_fn)

    with pytest.raises(fr.PromotionError, match="Production smoke failed"):
        _promote(client, before=before, candidate=candidate, smoke_attempts=1)

    assert client.catalog == intruder  # rollback refused to clobber it
    assert before not in client.published  # the previous catalog was not republished


def _seed_recover(catalog):
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    client = FakeClient(catalog=catalog)
    client.backups[f"catalog-before-{NEW}.json"] = before
    client.backups[f"catalog-candidate-{NEW}.json"] = candidate
    return client, before, candidate


def _recover(client):
    fr.recover(
        client,
        version=NEW,
        health_attempts=2,
        health_interval=0,
        sleep=lambda _seconds: None,
        log=lambda *_a, **_k: None,
    )


def test_recover_restores_after_crash_between_publish_and_smoke():
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    client, before, _candidate = _seed_recover(candidate)  # crash left candidate live

    _recover(client)

    assert client.catalog == before  # restored to the previous catalog
    assert client.published == [before]


def test_recover_noop_when_nothing_was_published():
    before = _catalog_bytes(latest=PREVIOUS)
    client = FakeClient(catalog=before)  # no durable candidate backup exists

    _recover(client)

    assert client.catalog == before
    assert client.published == []


def test_recover_noop_when_already_restored():
    before = _catalog_bytes(latest=PREVIOUS)
    client, _before, _candidate = _seed_recover(before)  # live already == before

    _recover(client)

    assert client.catalog == before
    assert client.published == []  # no republish needed


def test_recover_refuses_unknown_live_catalog():
    intruder = _catalog_bytes(latest="2026-07-16.0")
    client, _before, _candidate = _seed_recover(intruder)

    with pytest.raises(fr.RecoveryError, match="neither the interrupted candidate"):
        _recover(client)

    assert client.published == []


# ---------------------------------------------------------------------------
# prune_catalog: one implementation shared by the rebuild retention prune and
# the standalone R2 cleanup.
# ---------------------------------------------------------------------------


def _catalog(children):
    """children: list of (version, is_latest)."""
    links = [{"rel": "self", "href": "./catalog.json"}]
    for version, latest in children:
        link = {"rel": "child", "href": f"./{version}/collection.json"}
        if latest:
            link["latest"] = True
        links.append(link)
    return {"links": links}


def _child_versions(catalog):
    return [pc._child_version(link) for link in catalog["links"] if link.get("rel") == "child"]


def test_prune_retention_keeps_newest_and_drops_rest():
    catalog = _catalog(
        [
            ("2026-07-14.0", True),
            ("2026-07-13.0", False),
            ("2026-06-25.0", False),
            ("2026-05-25.0", False),
            ("2026-04-25.0", False),
        ]
    )
    pruned, dropped = pc.prune_by_retention(catalog, keep=4, current="2026-07-14.0")
    assert _child_versions(pruned) == [
        "2026-07-14.0",
        "2026-07-13.0",
        "2026-06-25.0",
        "2026-05-25.0",
    ]
    assert dropped == ["2026-04-25.0"]


def test_prune_retention_sorts_numeric_suffixes_before_dropping():
    catalog = _catalog(
        [
            ("2026-07-02.10", True),
            ("2026-07-02.2", False),
            ("2026-06-25.0", False),
            ("2026-05-25.0", False),
            ("2026-04-25.0", False),
        ]
    )
    pruned, dropped = pc.prune_by_retention(catalog, keep=4, current="2026-07-02.10")
    assert "2026-07-02.10" == _child_versions(pruned)[0]  # .10 sorts above .2
    assert dropped == ["2026-04-25.0"]


def test_prune_retention_requires_current_to_be_latest():
    catalog = _catalog(
        [("2026-07-14.0", True), ("2026-07-13.0", False), ("2026-06-25.0", False), ("2026-05-25.0", False)]
    )
    with pytest.raises(pc.PruneError, match="not catalog latest"):
        pc.prune_by_retention(catalog, keep=4, current="2026-07-13.0")


def test_prune_retention_refuses_below_floor():
    catalog = _catalog([("2026-07-14.0", True), ("2026-07-13.0", False)])
    with pytest.raises(pc.PruneError, match="below retention floor"):
        pc.prune_by_retention(catalog, keep=4, current="2026-07-14.0")


def test_prune_retention_rejects_keep_below_one():
    # keep=0 would drop EVERY version; the guard must fire before any
    # catalog mutation is computed.
    catalog = _catalog([("2026-07-14.0", True), ("2026-07-13.0", False)])
    for keep in (0, -1):
        with pytest.raises(pc.PruneError, match="keep must be >= 1"):
            pc.prune_by_retention(catalog, keep=keep, current="2026-07-14.0")


def test_prune_allowlist_rejects_floor_below_one():
    catalog = _catalog([("2026-07-14.0", True), ("2026-07-13.0", False)])
    for floor in (0, -1):
        with pytest.raises(pc.PruneError, match="floor must be >= 1"):
            pc.prune_by_allowlist(catalog, prune={"2026-07-13.0"}, floor=floor)


def test_prune_allowlist_removes_requested_and_keeps_floor():
    catalog = _catalog(
        [
            ("2026-07-14.0", True),
            ("2026-07-13.0", False),
            ("2026-06-25.0", False),
            ("2026-05-25.0", False),
            ("2026-02-26.0", False),
        ]
    )
    pruned, removed = pc.prune_by_allowlist(catalog, prune={"2026-02-26.0"}, floor=4)
    assert removed == ["2026-02-26.0"]
    assert "2026-02-26.0" not in _child_versions(pruned)
    assert len(_child_versions(pruned)) == 4


def test_prune_allowlist_refuses_below_floor():
    catalog = _catalog(
        [("2026-07-14.0", True), ("2026-07-13.0", False), ("2026-06-25.0", False), ("2026-05-25.0", False)]
    )
    with pytest.raises(pc.PruneError, match="would leave only"):
        pc.prune_by_allowlist(catalog, prune={"2026-05-25.0"}, floor=4)


def test_prune_allowlist_refuses_to_remove_latest():
    catalog = _catalog(
        [
            ("2026-07-14.0", True),
            ("2026-07-13.0", False),
            ("2026-06-25.0", False),
            ("2026-05-25.0", False),
            ("2026-02-26.0", False),
        ]
    )
    with pytest.raises(pc.PruneError, match="latest"):
        pc.prune_by_allowlist(catalog, prune={"2026-07-14.0"}, floor=4)


def test_is_referenced_uses_exact_match_and_detects_orphans():
    catalog = _catalog([("2026-07-14.0", True), ("2026-06-25.0", False)])
    assert pc.is_referenced(catalog, "2026-07-14.0") is True
    assert pc.is_referenced(catalog, "2026-06-25.0") is True
    # Orphan prefix never referenced by the catalog -> safe to delete.
    assert pc.is_referenced(catalog, "2026-02-24.0") is False
    # A substring of a real version must NOT count as a reference (the bug the
    # exact link-target comparison fixes versus the old `grep -q`).
    assert pc.is_referenced(catalog, "2026-07-14") is False
    assert pc.is_referenced(catalog, "07-14.0") is False
