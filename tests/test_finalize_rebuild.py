import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import finalize_rebuild as fr
import global_build_manifest as gbm
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
    # A non-staging object under the version prefix that no family or root file
    # accounts for got no content verification, so the exact-inventory gate must
    # fail closed and name it rather than bless it in the manifest.
    metadata, readback, inventory = _release_fixture(tmp_path)
    value = json.loads(inventory.read_text())
    value["Contents"].append(_entry("stray-root.json", 12))
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
    assert "stray-root.json" in str(excinfo.value)


def test_verify_release_tolerates_staging_objects_during_finalize(tmp_path):
    # Staging now survives until post-finalize cleanup so a failed finalize can
    # be recovered, so finalize must run with staging/ present. It is excluded
    # from the exact-set gate and never blessed into the verified object set.
    metadata, readback, inventory = _release_fixture(tmp_path)
    value = json.loads(inventory.read_text())
    value["Contents"].append(_entry("staging/build-000-3ff/_SUCCESS", 12))
    value["Contents"].append(_entry("staging/registry/_SUCCESS", 9))
    _write_json(inventory, value)

    manifest = fr.verify_release(
        version=VERSION,
        release=RELEASE,
        inventory_path=inventory,
        metadata_dir=metadata,
        readback_dir=readback,
        output_path=tmp_path / "manifest.json",
    )
    hrefs = {obj["href"] for obj in manifest["verified_version_objects"]}
    assert not any(href.startswith("./staging/") for href in hrefs)


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

    def publish_catalog(self, data, *, expected_etag=None):
        # Model R2's server-side compare-and-swap: when the caller supplies an
        # expected-current ETag, reject the write (as R2 does with 412) unless
        # the live catalog still hashes to it, so a lost CAS never clobbers.
        if expected_etag is not None:
            current = fr._content_etag(self.catalog) if self.catalog is not None else None
            if current != expected_etag:
                raise fr.PreconditionFailed("catalog changed under compare-and-swap")
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


class _RaceThenLiveClient(FakeClient):
    """Models a recover whose CAS loses to a concurrent writer: the first fetch
    still sees the interrupted candidate, ``publish_catalog`` 412s, and the next
    fetch reflects whatever that concurrent writer left live.
    """

    def __init__(self, *, candidate, post_race):
        super().__init__(catalog=candidate)
        self._post_race = post_race
        self._first_fetch = True

    def publish_catalog(self, data, *, expected_etag=None):
        raise fr.PreconditionFailed("lost CAS to concurrent writer")

    def fetch_catalog(self):
        if self._first_fetch:
            self._first_fetch = False
            return self.catalog
        self.catalog = self._post_race
        return self._post_race


def test_recover_lost_cas_but_previous_now_live_succeeds():
    # Another recoverer won the race and restored the previous catalog first: a
    # 412 here is success, not a raw crash.
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    client = _RaceThenLiveClient(candidate=candidate, post_race=before)
    client.backups[f"catalog-before-{NEW}.json"] = before
    client.backups[f"catalog-candidate-{NEW}.json"] = candidate

    _recover(client)  # must not raise


def test_recover_lost_cas_to_foreign_catalog_refuses_cleanly():
    # The race left a catalog we do not recognise live: refuse as a RecoveryError
    # (which main handles) rather than an unhandled PreconditionFailed traceback.
    before = _catalog_bytes(latest=PREVIOUS)
    candidate = _catalog_bytes(latest=NEW, others=[PREVIOUS])
    intruder = _catalog_bytes(latest="2026-07-16.0")
    client = _RaceThenLiveClient(candidate=candidate, post_race=intruder)
    client.backups[f"catalog-before-{NEW}.json"] = before
    client.backups[f"catalog-candidate-{NEW}.json"] = candidate

    with pytest.raises(fr.RecoveryError, match="restore readback did not equal"):
        _recover(client)


# ---------------------------------------------------------------------------
# R2Client conditional-write helpers: exercised through a fake `aws` that models
# R2's server-side If-None-Match / If-Match preconditions (no live R2 calls).
# ---------------------------------------------------------------------------


class FakeAws:
    """Stand-in for ``R2Client._aws`` backing a tiny in-memory object store.

    Models R2 semantics needed for the guards: single-part objects carry a
    quoted-md5 ETag, ``put-object`` honours ``--if-none-match``/``--if-match``
    server-side (a miss raises a ``PreconditionFailed`` CalledProcessError like
    the real CLI), and ``s3 cp`` reads an object back. ``corrupt_readback`` and
    ``transient_puts`` let a test inject a wrong-bytes readback or N transient
    put failures before success.
    """

    def __init__(self, store=None):
        self.store = dict(store or {})
        self.puts = []
        self.corrupt_readback = None
        self.transient_puts = 0
        # Model an aws CLI too old to expose the conditional-write options.
        self.reject_conditional_options = False

    def _fail(self, message):
        return subprocess.CalledProcessError(254, ["aws"], output="", stderr=message)

    def __call__(self, *args, capture=False):
        args = list(args)
        if args[:2] == ["s3api", "put-object"]:
            opts = {args[i]: args[i + 1] for i in range(len(args) - 1)}
            key = opts["--key"]
            data = Path(opts["--body"]).read_bytes()
            if self.reject_conditional_options and (
                "--if-match" in opts or "--if-none-match" in opts
            ):
                bad = "--if-match" if "--if-match" in opts else "--if-none-match"
                raise self._fail(f"aws: [ERROR]: Unknown options: {bad}, <value>")
            if self.transient_puts > 0:
                self.transient_puts -= 1
                raise self._fail("An error occurred (RequestTimeout) ...")
            if opts.get("--if-none-match") == "*" and key in self.store:
                raise self._fail(
                    "An error occurred (PreconditionFailed) when calling the PutObject operation"
                )
            if "--if-match" in opts:
                current = fr._content_etag(self.store[key]) if key in self.store else None
                if current != opts["--if-match"]:
                    raise self._fail(
                        "An error occurred (PreconditionFailed) when calling the PutObject operation"
                    )
            self.store[key] = data
            self.puts.append(key)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["s3", "cp"]:
            src, dest = args[2], args[3]
            key = src.split(f"{fr.BUCKET}/", 1)[1] if src.startswith("s3://") else None
            payload = self.corrupt_readback if self.corrupt_readback is not None else self.store[key]
            Path(dest).write_bytes(payload)
            return subprocess.CompletedProcess(args, 0, "", "")
        if args[:2] == ["s3api", "list-objects-v2"]:
            opts = {args[i]: args[i + 1] for i in range(len(args) - 1)}
            prefix = opts.get("--prefix", "")
            keys = sorted(key for key in self.store if key.startswith(prefix))
            query = opts.get("--query")
            if query == "KeyCount":
                return subprocess.CompletedProcess(args, 0, str(len(keys)), "")
            if query == "Contents[].Key":
                # Real aws prints `null` (not `[]`) when there are no matches.
                return subprocess.CompletedProcess(args, 0, json.dumps(keys or None), "")
            raise AssertionError(f"unexpected list-objects-v2 query: {query}")
        raise AssertionError(f"unexpected aws call: {args}")


def _client_with(store=None):
    client = fr.R2Client(
        bucket=fr.BUCKET,
        endpoint="https://example.invalid",
        base_url="https://example.invalid",
        repo_root=Path("."),
        sleep=lambda _s: None,
    )
    fake = FakeAws(store)
    client._aws = fake
    return client, fake


def test_publish_create_only_writes_and_verifies():
    client, fake = _client_with()
    client.publish_create_only("backups/x.json", b"payload")
    assert fake.store["backups/x.json"] == b"payload"


def test_publish_create_only_conflict_is_hard_error_and_no_overwrite():
    client, fake = _client_with({"backups/x.json": b"original"})
    with pytest.raises(fr.PreconditionFailed):
        client.publish_create_only("backups/x.json", b"replacement")
    assert fake.store["backups/x.json"] == b"original"  # never overwritten
    assert fake.puts == []  # the put was rejected server-side


def test_swap_expected_current_replaces_when_etag_matches():
    old = b"catalog-old"
    client, fake = _client_with({fr.CATALOG_KEY: old})
    client.swap_expected_current(fr.CATALOG_KEY, b"catalog-new", fr._content_etag(old))
    assert fake.store[fr.CATALOG_KEY] == b"catalog-new"


def test_swap_expected_current_precondition_failure_aborts_without_overwrite():
    live = b"catalog-live-someone-else"
    client, fake = _client_with({fr.CATALOG_KEY: live})
    stale = fr._content_etag(b"catalog-we-thought-was-current")
    with pytest.raises(fr.PreconditionFailed):
        client.swap_expected_current(fr.CATALOG_KEY, b"catalog-ours", stale)
    assert fake.store[fr.CATALOG_KEY] == live  # untouched: a lost CAS never clobbers
    assert fake.puts == []


def test_swap_expected_current_readback_digest_mismatch_is_hard_error():
    old = b"catalog-old"
    client, fake = _client_with({fr.CATALOG_KEY: old})
    fake.corrupt_readback = b"corrupted-on-store"  # R2 accepted the PUT but readback differs
    with pytest.raises(fr.PromotionError, match="read-back digest mismatch"):
        client.swap_expected_current(fr.CATALOG_KEY, b"catalog-new", fr._content_etag(old))


def test_publish_catalog_cas_precondition_failure_is_not_retried():
    live = b"catalog-live"
    client, fake = _client_with({fr.CATALOG_KEY: live})
    with pytest.raises(fr.PreconditionFailed):
        client.publish_catalog(b"catalog-ours", expected_etag=fr._content_etag(b"catalog-stale"))
    assert fake.store[fr.CATALOG_KEY] == live
    assert fake.puts == []  # aborted on first attempt, no retry loop


def test_publish_catalog_retries_transient_put_failures():
    old = b"catalog-old"
    client, fake = _client_with({fr.CATALOG_KEY: old})
    fake.transient_puts = 2  # two transient failures, then success within 3 attempts
    client.publish_catalog(b"catalog-new", expected_etag=fr._content_etag(old))
    assert fake.store[fr.CATALOG_KEY] == b"catalog-new"


def test_put_backup_is_create_only():
    client, fake = _client_with({f"{fr.BACKUP_PREFIX}/dup.json": b"first"})
    with pytest.raises(fr.PreconditionFailed):
        client.put_backup("dup.json", b"second")
    assert fake.store[f"{fr.BACKUP_PREFIX}/dup.json"] == b"first"


def test_put_backup_identical_content_is_idempotent():
    # An operator re-running the same finalize re-writes the identical backup;
    # the create-only 412 must not brick that legitimate retry.
    key = f"{fr.BACKUP_PREFIX}/dup.json"
    client, fake = _client_with({key: b"same-bytes"})
    client.put_backup("dup.json", b"same-bytes")  # must not raise
    assert fake.store[key] == b"same-bytes"


def test_content_etag_is_quoted_md5_and_forwarded_verbatim():
    # R2's single-part ETag is the *quoted* hex MD5; the CAS must send exactly
    # that quoted form as If-Match. A fake that (like R2) only accepts the quoted
    # value proves the code does not strip or re-quote it.
    old = b"catalog-old"
    etag = fr._content_etag(old)
    assert etag == '"' + hashlib.md5(old).hexdigest() + '"'
    assert etag.startswith('"') and etag.endswith('"')

    client, fake = _client_with({fr.CATALOG_KEY: old})
    client.swap_expected_current(fr.CATALOG_KEY, b"catalog-new", etag)
    assert fake.store[fr.CATALOG_KEY] == b"catalog-new"

    # The unquoted digest is not R2's ETag, so the CAS must be rejected.
    client, fake = _client_with({fr.CATALOG_KEY: old})
    with pytest.raises(fr.PreconditionFailed):
        client.swap_expected_current(
            fr.CATALOG_KEY, b"catalog-new", hashlib.md5(old).hexdigest()
        )
    assert fake.store[fr.CATALOG_KEY] == old


def test_put_object_unsupported_conditional_option_is_clear_error():
    # An aws CLI too old for --if-match/--if-none-match must fail fast with an
    # actionable upgrade message, not silently retry an unparseable request.
    client, fake = _client_with({fr.CATALOG_KEY: b"catalog-old"})
    fake.reject_conditional_options = True
    with pytest.raises(fr.PromotionError, match="does not support PutObject conditional"):
        client.swap_expected_current(
            fr.CATALOG_KEY, b"catalog-new", fr._content_etag(b"catalog-old")
        )
    assert fake.puts == []  # never written


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


# ---------------------------------------------------------------------------
# Optional experimental families in verify_release (addresses / places).
# ---------------------------------------------------------------------------

NE_BBOX = [-80.5, 38.0, -66.9, 47.5]


def _family_versions(family):
    if family == "addresses":
        return {
            "format": gbm.ADDRESS_FORMAT_VERSION,
            "tokenizer": None,
            "normalization": gbm.ADDRESS_NORMALIZATION_VERSION,
        }, "scripts/experiment_address_reduce.py", "row_group_approximate"
    return {
        "format": gbm.PLACES_FORMAT_VERSION,
        "tokenizer": gbm.PLACES_TOKENIZER_VERSION,
        "normalization": None,
    }, "scripts/experiment_places_compact_shard.py", "exact"


def _make_family_manifest(family, artifacts, *, release=RELEASE):
    versions, producer_script, scope = _family_versions(family)
    return gbm.build_family_manifest(
        family,
        lineage={
            "overture_release": release,
            "build_id": "a" * 64,
            "producer_commit": "abc123",
            "producer_script": producer_script,
            "producer_version": "1",
        },
        versions=versions,
        region={"name": "US-Northeast", "bbox": list(NE_BBOX), "bbox_scope": scope},
        artifacts=artifacts,
    )


def _add_family(tmp_path, metadata, readback, inventory, family="addresses",
                artifact_bytes=b"family-artifact-shard-0000"):
    """Attach a valid optional family (one artifact + manifest) to the fixture."""
    art_key = f"families/{family}/shards/0000.pidx"
    art_local = readback / art_key
    art_local.parent.mkdir(parents=True, exist_ok=True)
    art_local.write_bytes(artifact_bytes)
    art_sha = hashlib.sha256(artifact_bytes).hexdigest()

    manifest = _make_family_manifest(
        family,
        [{"object_key": art_key, "bytes": len(artifact_bytes), "sha256": art_sha}],
    )
    manifest_bytes = gbm.canonical_json(manifest)
    manifest_key = f"families/{family}/family-manifest.json"
    manifest_local = metadata / manifest_key
    manifest_local.parent.mkdir(parents=True, exist_ok=True)
    manifest_local.write_bytes(manifest_bytes)

    value = json.loads(inventory.read_text())
    value["Contents"].append(_entry(art_key, len(artifact_bytes)))
    value["Contents"].append(_entry(manifest_key, len(manifest_bytes)))
    _write_json(inventory, value)
    return manifest, art_key, manifest_key


def _verify(metadata, readback, inventory, tmp_path, name="manifest.json", **kwargs):
    return fr.verify_release(
        version=VERSION,
        release=RELEASE,
        inventory_path=inventory,
        metadata_dir=metadata,
        readback_dir=readback,
        output_path=tmp_path / name,
        **kwargs,
    )


def test_verify_release_zero_families_is_byte_identical(tmp_path):
    # A release with no family manifests must behave exactly as today: no
    # `optional_families` key and, modulo the generated_at timestamp, an
    # identical manifest whether the allowlist is omitted or explicitly empty.
    metadata, readback, inventory = _release_fixture(tmp_path)
    baseline = _verify(metadata, readback, inventory, tmp_path, "a.json")
    empty = _verify(
        metadata, readback, inventory, tmp_path, "b.json", optional_families=[]
    )
    for produced in (baseline, empty):
        assert "optional_families" not in produced
    baseline.pop("generated_at")
    empty.pop("generated_at")
    assert baseline == empty
    assert len(empty["verified_version_objects"]) == 4109


def test_verify_release_accepts_valid_optional_family(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    manifest, art_key, manifest_key = _add_family(tmp_path, metadata, readback, inventory)

    produced = _verify(
        metadata, readback, inventory, tmp_path, optional_families=["addresses"]
    )

    info = produced["optional_families"]["addresses"]
    assert info["promotion_eligible"] is False
    assert info["artifact_count"] == 1
    assert info["manifest_digest"] == manifest["manifest_digest"]
    assert info["region"]["name"] == "US-Northeast"
    # The verified object set now spans the two family objects too.
    hrefs = {obj["href"] for obj in produced["verified_version_objects"]}
    assert f"./{art_key}" in hrefs
    assert f"./{manifest_key}" in hrefs


def test_verify_release_family_object_without_allowlist_still_fails(tmp_path):
    # An unexpected object outside any allowlisted manifest fails the exact-set
    # gate: family presence is opt-in, never blessed wholesale.
    metadata, readback, inventory = _release_fixture(tmp_path)
    _add_family(tmp_path, metadata, readback, inventory)

    with pytest.raises(ValueError, match="not an exact match") as excinfo:
        _verify(metadata, readback, inventory, tmp_path)
    assert "families/addresses" in str(excinfo.value)


def test_verify_release_allowlisted_family_missing_manifest_fails(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    with pytest.raises(ValueError, match="manifest is missing"):
        _verify(metadata, readback, inventory, tmp_path, optional_families=["addresses"])


def test_verify_release_family_missing_artifact_fails(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    _add_family(tmp_path, metadata, readback, inventory)
    value = json.loads(inventory.read_text())
    value["Contents"] = [
        item for item in value["Contents"]
        if not item["Key"].endswith("/families/addresses/shards/0000.pidx")
    ]
    _write_json(inventory, value)

    with pytest.raises(ValueError, match="artifact missing"):
        _verify(metadata, readback, inventory, tmp_path, optional_families=["addresses"])


def test_verify_release_family_artifact_hash_mismatch_fails(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    _add_family(tmp_path, metadata, readback, inventory)
    # Overwrite the readback artifact with different bytes of the SAME length so
    # the size gate passes and the SHA-256 gate is what fails closed.
    art = readback / "families/addresses/shards/0000.pidx"
    art.write_bytes(b"X" * len(art.read_bytes()))

    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        _verify(metadata, readback, inventory, tmp_path, optional_families=["addresses"])


def test_verify_release_family_extra_object_fails(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    _add_family(tmp_path, metadata, readback, inventory)
    value = json.loads(inventory.read_text())
    value["Contents"].append(_entry("families/addresses/shards/0001.pidx", 5))
    _write_json(inventory, value)

    with pytest.raises(ValueError, match="inventory mismatch") as excinfo:
        _verify(metadata, readback, inventory, tmp_path, optional_families=["addresses"])
    assert "0001.pidx" in str(excinfo.value)


def test_verify_release_rejects_tampered_family_manifest(tmp_path):
    metadata, readback, inventory = _release_fixture(tmp_path)
    manifest, _art_key, manifest_key = _add_family(tmp_path, metadata, readback, inventory)
    # Mutate a manifest field while keeping its recorded manifest_digest: the
    # self-digest recomputation fails closed. Update the inventory size so the
    # size gate passes and validation is what rejects it.
    tampered = {**manifest, "totals": {"artifacts": 1, "bytes": 999}}
    tampered_bytes = gbm.canonical_json(tampered)
    (metadata / manifest_key).write_bytes(tampered_bytes)
    value = json.loads(inventory.read_text())
    for item in value["Contents"]:
        if item["Key"].endswith("/family-manifest.json"):
            item["Size"] = len(tampered_bytes)
    _write_json(inventory, value)

    with pytest.raises(ValueError, match="deterministic contents"):
        _verify(metadata, readback, inventory, tmp_path, optional_families=["addresses"])


def test_verify_release_two_families_do_not_touch_core_promotion_shape(tmp_path):
    # Both experimental families verified: the core forward/reverse/id families
    # and the promotable object shape are unchanged; the optional records are a
    # separate, non-promoting section.
    metadata, readback, inventory = _release_fixture(tmp_path)
    _add_family(tmp_path, metadata, readback, inventory, family="addresses")
    _add_family(tmp_path, metadata, readback, inventory, family="places")

    produced = _verify(
        metadata, readback, inventory, tmp_path,
        optional_families=["addresses", "places"],
    )
    assert set(produced["families"]) == {"forward", "reverse", "id"}
    assert set(produced["optional_families"]) == {"addresses", "places"}
    assert all(
        info["promotion_eligible"] is False
        for info in produced["optional_families"].values()
    )


# ---------------------------------------------------------------------------
# publish-family: publish artifacts create-only, manifest last, remote re-verify.
# ---------------------------------------------------------------------------


def _local_family(tmp_path, family="addresses", count=2):
    root = tmp_path / "build"
    artifacts = []
    for index in range(count):
        data = f"artifact-{family}-{index}".encode()
        key = f"families/{family}/shards/{index:04d}.pidx"
        path = root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        artifacts.append(
            {"object_key": key, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        )
    manifest = _make_family_manifest(family, artifacts)
    manifest_path = root / f"families/{family}/family-manifest.json"
    manifest_path.write_bytes(gbm.canonical_json(manifest))
    return root, manifest_path, manifest, artifacts


def _publish_family(client, tmp_path, family="addresses", count=2, root_override=None,
                    manifest_override=None):
    root, manifest_path, manifest, artifacts = _local_family(tmp_path, family, count)
    fr.publish_family(
        client,
        version=VERSION,
        family=family,
        manifest_path=manifest_override or manifest_path,
        artifacts_root=root_override or root,
        log=lambda *_a, **_k: None,
    )
    return root, manifest, artifacts


def test_publish_family_publishes_artifacts_before_manifest(tmp_path):
    client, fake = _client_with()
    _root, _manifest, artifacts = _publish_family(client, tmp_path)

    manifest_key = f"{VERSION}/families/addresses/family-manifest.json"
    art_keys = sorted(f"{VERSION}/{art['object_key']}" for art in artifacts)
    # Data before marker: every artifact key, then the manifest key last.
    assert fake.puts == art_keys + [manifest_key]
    assert manifest_key in fake.store


def test_publish_family_identical_rerun_is_idempotent(tmp_path):
    root, manifest_path, manifest, artifacts = _local_family(tmp_path)
    store = {
        f"{VERSION}/{art['object_key']}": (root / art["object_key"]).read_bytes()
        for art in artifacts
    }
    store[f"{VERSION}/families/addresses/family-manifest.json"] = gbm.canonical_json(manifest)
    client, fake = _client_with(store)

    fr.publish_family(
        client, version=VERSION, family="addresses",
        manifest_path=manifest_path, artifacts_root=root, log=lambda *_a, **_k: None,
    )
    assert fake.puts == []  # every create-only was a no-op on identical bytes


def test_publish_family_conflicting_artifact_is_hard_error(tmp_path):
    root, manifest_path, _manifest, artifacts = _local_family(tmp_path)
    clashing_key = f"{VERSION}/{artifacts[0]['object_key']}"
    client, fake = _client_with({clashing_key: b"someone-elses-bytes"})

    with pytest.raises(fr.PreconditionFailed):
        fr.publish_family(
            client, version=VERSION, family="addresses",
            manifest_path=manifest_path, artifacts_root=root, log=lambda *_a, **_k: None,
        )
    assert fake.store[clashing_key] == b"someone-elses-bytes"  # never overwritten


def test_publish_family_local_mismatch_aborts_before_any_publish(tmp_path):
    root, manifest_path, _manifest, artifacts = _local_family(tmp_path)
    # Corrupt a local artifact so its hash no longer matches the manifest.
    (root / artifacts[0]["object_key"]).write_bytes(b"tampered-local-bytes")
    client, fake = _client_with()

    with pytest.raises(ValueError, match="mismatch"):
        fr.publish_family(
            client, version=VERSION, family="addresses",
            manifest_path=manifest_path, artifacts_root=root, log=lambda *_a, **_k: None,
        )
    assert fake.puts == []  # local verification precedes every publish


def test_publish_family_remote_reverify_detects_extra_object(tmp_path):
    root, manifest_path, _manifest, _artifacts = _local_family(tmp_path)
    # A stray object already squats the family prefix remotely; it is not local,
    # so local verify passes but the downloaded-hash remote re-verify catches it.
    stray_key = f"{VERSION}/families/addresses/shards/9999.pidx"
    client, fake = _client_with({stray_key: b"stray"})

    with pytest.raises(ValueError, match="unexpected objects"):
        fr.publish_family(
            client, version=VERSION, family="addresses",
            manifest_path=manifest_path, artifacts_root=root, log=lambda *_a, **_k: None,
        )


def test_publish_family_rejects_manifest_family_mismatch(tmp_path):
    root, _manifest_path, _manifest, _artifacts = _local_family(tmp_path, family="addresses")
    manifest_path = root / "families/addresses/family-manifest.json"
    client, fake = _client_with()

    with pytest.raises(ValueError, match="manifest declares family"):
        fr.publish_family(
            client, version=VERSION, family="places",
            manifest_path=manifest_path, artifacts_root=root, log=lambda *_a, **_k: None,
        )
    assert fake.puts == []
