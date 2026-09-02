import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import decommission_paused_data as decommission
import finalize_rebuild


CURRENT = "2026-08-25.0"


def _catalog(*versions, latest=CURRENT):
    links = [
        {"rel": "self", "href": "./catalog.json"},
        {"rel": "root", "href": "./catalog.json"},
    ]
    links.extend(
        {
            "rel": "child",
            "href": f"./{version}/collection.json",
            **({"latest": True} if version == latest else {}),
        }
        for version in versions
    )
    return {"id": "geocoder-shards", "links": links}


def _listing(*prefixes, contents=None):
    return {
        "CommonPrefixes": [{"Prefix": prefix} for prefix in prefixes],
        "Contents": [] if contents is None else contents,
    }


def _object_listing(*objects):
    return {
        "IsTruncated": False,
        "Contents": [
            {"Key": key, "Size": size, "ETag": f'"{etag}"'}
            for key, size, etag in objects
        ],
    }


def _valid_plan_inputs():
    catalog = _catalog(
        CURRENT,
        "2026-07-28.0",
        "2026-07-13.0",
    )
    top = _listing(
        f"{CURRENT}/",
        "2026-07-28.0/",
        "2026-07-18.0/",
        "2026-07-13.0/",
        "backups/",
        "construction-v1/",
        "slice-2026-08-07.0/",
        "slice-2026-08-04.0/",
        "staging/",
        "v2/",
    )
    construction_root = f"construction-v1/{'a' * 64}/"
    staging_root = f"staging/global-v2/{'b' * 64}/"
    construction = _listing(construction_root)
    staging = _listing(staging_root)
    full = _object_listing(
        (f"{construction_root}markers/finalize/places.json", 11, "marker"),
        (
            f"{construction_root}slice/slice-test/families/places/"
            "family-manifest.json",
            12,
            "manifest",
        ),
        (
            f"{construction_root}slice/slice-test/families/places/objects/a.plrv",
            13,
            "forward",
        ),
        (
            f"{construction_root}slice/slice-test/families/places/positions/a.bin",
            14,
            "positions",
        ),
        (f"{staging_root}immutable/inventory/places.json", 15, "inventory"),
        (f"{staging_root}immutable/map/addresses/reports/000.json", 16, "report"),
    )
    references = {
        "2026-07-18.0",
        "2026-07-28.0",
        "slice-2026-08-07.0",
    }
    return catalog, top, construction, staging, full, references


def test_plan_classifies_exact_prefixes_and_preserves_current_and_backups():
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    plan, pruned = decommission.build_plan(
        catalog=catalog,
        top_listing=top,
        construction_listing=construction,
        staging_listing=staging,
        full_listing=full,
        expected_current=CURRENT,
        v2_references=references,
        v2_release_count=5,
    )

    assert plan["old_root_prefixes"] == [
        "2026-07-13.0/",
        "2026-07-18.0/",
        "2026-07-28.0/",
    ]
    assert plan["v1_predecessor_prefixes"] == [
        "2026-07-13.0/",
        "2026-07-28.0/",
    ]
    assert plan["v2_only_root_prefixes"] == ["2026-07-18.0/"]
    assert plan["slice_prefixes"] == [
        "slice-2026-08-04.0/",
        "slice-2026-08-07.0/",
    ]
    construction_root = f"construction-v1/{'a' * 64}/"
    assert plan["construction_data_prefixes"] == [
        f"{construction_root}slice/slice-test/families/places/objects/",
        f"{construction_root}slice/slice-test/families/places/positions/",
    ]
    assert plan["staging_data_prefixes"] == []
    assert plan["construction_evidence_prefixes"] == [construction_root]
    assert plan["staging_evidence_prefixes"] == [
        f"staging/global-v2/{'b' * 64}/"
    ]
    assert plan["v2_metadata_prefix"] == "v2/"
    assert plan["preserved_top_level_prefixes"] == [
        f"{CURRENT}/",
        "backups/",
        "construction-v1/",
        "staging/",
    ]

    children = [link for link in pruned["links"] if link.get("rel") == "child"]
    assert children == [
        {
            "rel": "child",
            "href": f"./{CURRENT}/collection.json",
            "latest": True,
        }
    ]


@pytest.mark.parametrize(
    ("latest", "expected_message"),
    [
        ("2026-07-28.0", "latest must be exactly"),
        (None, "latest must be exactly"),
    ],
)
def test_plan_requires_exactly_one_expected_latest(latest, expected_message):
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    for link in catalog["links"]:
        link.pop("latest", None)
        if link.get("href") == f"./{latest}/collection.json":
            link["latest"] = True
    with pytest.raises(decommission.DecommissionError, match=expected_message):
        decommission.build_plan(
            catalog=catalog,
            top_listing=top,
            construction_listing=construction,
            staging_listing=staging,
            full_listing=full,
            expected_current=CURRENT,
            v2_references=references,
            v2_release_count=5,
        )


def test_plan_refuses_a_newer_unpublished_root():
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    top["CommonPrefixes"].append({"Prefix": "2026-08-26.0/"})
    with pytest.raises(decommission.DecommissionError, match="newer than"):
        decommission.build_plan(
            catalog=catalog,
            top_listing=top,
            construction_listing=construction,
            staging_listing=staging,
            full_listing=full,
            expected_current=CURRENT,
            v2_references=references,
            v2_release_count=5,
        )


def test_plan_refuses_an_old_root_outside_both_reviewed_reference_graphs():
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    top["CommonPrefixes"].append({"Prefix": "2026-07-17.0/"})
    with pytest.raises(decommission.DecommissionError, match="neither a v1"):
        decommission.build_plan(
            catalog=catalog,
            top_listing=top,
            construction_listing=construction,
            staging_listing=staging,
            full_listing=full,
            expected_current=CURRENT,
            v2_references=references,
            v2_release_count=5,
        )


def test_fresh_plan_refuses_a_missing_catalog_predecessor_without_a_journal():
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    top["CommonPrefixes"] = [
        item
        for item in top["CommonPrefixes"]
        if item["Prefix"] != "2026-07-13.0/"
    ]
    with pytest.raises(decommission.DecommissionError, match="no matching R2 prefix"):
        decommission.build_plan(
            catalog=catalog,
            top_listing=top,
            construction_listing=construction,
            staging_listing=staging,
            full_listing=full,
            expected_current=CURRENT,
            v2_references=references,
            v2_release_count=5,
        )


def test_recursive_inventory_binds_every_target_object_and_detects_drift():
    catalog, top, construction, staging, plan_source, references = _valid_plan_inputs()
    plan, _ = decommission.build_plan(
        catalog=catalog,
        top_listing=top,
        construction_listing=construction,
        staging_listing=staging,
        full_listing=plan_source,
        expected_current=CURRENT,
        v2_references=references,
        v2_release_count=5,
    )
    target_objects = [
        (f"{prefix}object", index + 1, f"etag-{index}")
        for index, (_, prefix) in enumerate(decommission._plan_targets(plan))
    ]
    full = _object_listing(
        *target_objects,
        (
            f"construction-v1/{'a' * 64}/markers/finalize/places.json",
            20,
            "marker",
        ),
        (
            f"construction-v1/{'a' * 64}/slice/slice-test/families/places/"
            "family-manifest.json",
            21,
            "manifest",
        ),
        (
            f"staging/global-v2/{'b' * 64}/immutable/inventory/places.json",
            22,
            "inventory",
        ),
        (f"{CURRENT}/collection.json", 10, "current"),
        ("backups/catalog.json", 11, "backup"),
        ("catalog.json", 12, "root"),
    )
    inventory = decommission.build_recursive_inventory(
        plan=plan, full_listing=full
    )

    assert inventory["object_count"] == len(target_objects)
    assert inventory["total_bytes"] == sum(item[1] for item in target_objects)
    assert len(inventory["preserved_evidence"]) == 2
    prefix = plan["slice_prefixes"][0]
    expected = next(item for item in target_objects if item[0].startswith(prefix))
    decommission.verify_prefix_inventory(
        inventory=inventory,
        prefix=prefix,
        listing=_object_listing(expected),
    )

    changed = (expected[0], expected[1], "different-etag")
    with pytest.raises(decommission.DecommissionError, match="changed after"):
        decommission.verify_prefix_inventory(
            inventory=inventory,
            prefix=prefix,
            listing=_object_listing(changed),
        )

    remainder = _object_listing(expected)
    decommission.verify_prefix_remainder(
        inventory=inventory,
        prefix=prefix,
        listing=remainder,
    )
    decommission.verify_prefix_remainder(
        inventory=inventory,
        prefix=prefix,
        listing=_object_listing(),
    )
    with pytest.raises(decommission.DecommissionError, match="remainder differs"):
        decommission.verify_prefix_remainder(
            inventory=inventory,
            prefix=prefix,
            listing=_object_listing(
                (f"{prefix}new-object", 1, "new")
            ),
        )

    changed_inventory = json.loads(json.dumps(inventory))
    changed_inventory["targets"][0]["objects"][0]["size"] += 1
    original_plan = {**plan, "inventory_sha256": decommission._object_fingerprint(inventory)}
    changed_plan = {
        **plan,
        "inventory_sha256": decommission._object_fingerprint(changed_inventory),
    }
    assert decommission._object_fingerprint(changed_plan) != decommission._object_fingerprint(original_plan)


def test_recursive_inventory_refuses_a_top_level_prefix_added_between_listings():
    catalog, top, construction, staging, plan_source, references = _valid_plan_inputs()
    plan, _ = decommission.build_plan(
        catalog=catalog,
        top_listing=top,
        construction_listing=construction,
        staging_listing=staging,
        full_listing=plan_source,
        expected_current=CURRENT,
        v2_references=references,
        v2_release_count=5,
    )
    objects = [
        (f"{prefix}object", 1, "etag")
        for _, prefix in decommission._plan_targets(plan)
    ]
    objects.extend(
        [
            (
                f"construction-v1/{'a' * 64}/markers/finalize/places.json",
                1,
                "marker",
            ),
            (
                f"staging/global-v2/{'b' * 64}/immutable/inventory/places.json",
                1,
                "inventory",
            ),
            (f"{CURRENT}/collection.json", 1, "current"),
            ("backups/catalog.json", 1, "backup"),
            ("unexpected/new", 1, "new"),
        ]
    )
    with pytest.raises(decommission.DecommissionError, match="top-level"):
        decommission.build_recursive_inventory(
            plan=plan, full_listing=_object_listing(*objects)
        )


def test_plan_refuses_unknown_construction_layout_instead_of_deleting_it():
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    full["Contents"].append(
        {
            "Key": f"construction-v1/{'a' * 64}/unreviewed/payload.bin",
            "Size": 1,
            "ETag": '"unknown"',
        }
    )
    with pytest.raises(decommission.DecommissionError, match="unfamiliar object"):
        decommission.build_plan(
            catalog=catalog,
            top_listing=top,
            construction_listing=construction,
            staging_listing=staging,
            full_listing=full,
            expected_current=CURRENT,
            v2_references=references,
            v2_release_count=5,
        )


def test_preserved_run_evidence_is_exactly_verified_after_data_removal():
    catalog, top, construction, staging, plan_source, references = _valid_plan_inputs()
    plan, _ = decommission.build_plan(
        catalog=catalog,
        top_listing=top,
        construction_listing=construction,
        staging_listing=staging,
        full_listing=plan_source,
        expected_current=CURRENT,
        v2_references=references,
        v2_release_count=5,
    )
    target_objects = [
        (f"{prefix}object", index + 1, f"target-{index}")
        for index, (_, prefix) in enumerate(decommission._plan_targets(plan))
    ]
    construction_evidence = (
        f"construction-v1/{'a' * 64}/markers/finalize/places.json",
        20,
        "marker",
    )
    staging_evidence = (
        f"staging/global-v2/{'b' * 64}/immutable/inventory/places.json",
        21,
        "inventory",
    )
    inventory = decommission.build_recursive_inventory(
        plan=plan,
        full_listing=_object_listing(
            *target_objects,
            construction_evidence,
            staging_evidence,
            (f"{CURRENT}/collection.json", 22, "current"),
            ("backups/catalog.json", 23, "backup"),
        ),
    )

    construction_root = plan["construction_evidence_prefixes"][0]
    decommission.verify_preserved_evidence(
        inventory=inventory,
        prefix=construction_root,
        listing=_object_listing(construction_evidence),
    )
    with pytest.raises(decommission.DecommissionError, match="evidence changed"):
        decommission.verify_preserved_evidence(
            inventory=inventory,
            prefix=construction_root,
            listing=_object_listing(
                (construction_evidence[0], construction_evidence[1] + 1, "marker")
            ),
        )


def test_recurring_v1_plan_waits_seven_days_and_only_targets_catalog_predecessors():
    catalog = _catalog(CURRENT, "2026-07-28.0")
    top = _listing(
        f"{CURRENT}/",
        "2026-07-28.0/",
        "2026-07-17.0/",
        "backups/",
    )
    published = datetime(2026, 8, 25, 6, tzinfo=timezone.utc)
    early_plan, _ = decommission.build_v1_retention_plan(
        catalog=catalog,
        top_listing=top,
        now=published + timedelta(days=7) - timedelta(seconds=1),
        catalog_last_modified=published,
    )
    assert early_plan["eligible"] is False

    plan, pruned = decommission.build_v1_retention_plan(
        catalog=catalog,
        top_listing=top,
        now=published + timedelta(days=7),
        catalog_last_modified=published,
    )
    assert plan["eligible"] is True
    assert plan["old_root_prefixes"] == ["2026-07-28.0/"]
    assert "2026-07-17.0/" in plan["preserved_top_level_prefixes"]
    children = [link for link in pruned["links"] if link.get("rel") == "child"]
    assert [link["href"] for link in children] == [f"./{CURRENT}/collection.json"]


def test_recurring_v1_inventory_fingerprints_only_the_catalog_predecessor():
    catalog = _catalog(CURRENT, "2026-07-28.0")
    top = _listing(f"{CURRENT}/", "2026-07-28.0/", "backups/")
    published = datetime(2026, 8, 25, 6, tzinfo=timezone.utc)
    plan, _ = decommission.build_v1_retention_plan(
        catalog=catalog,
        top_listing=top,
        now=published + timedelta(days=8),
        catalog_last_modified=published,
    )
    inventory = decommission.build_recursive_inventory(
        plan=plan,
        full_listing=_object_listing(
            (f"{CURRENT}/collection.json", 10, "current"),
            ("2026-07-28.0/collection.json", 20, "old"),
            ("backups/catalog.json", 30, "backup"),
        ),
    )
    assert inventory["object_count"] == 1
    assert inventory["targets"][0]["prefix"] == "2026-07-28.0/"


@pytest.mark.parametrize(
    ("which", "listing"),
    [
        ("construction", _listing("construction-v1/not-a-digest/")),
        ("staging", _listing("staging/global-v2/short/")),
        (
            "construction",
            _listing(
                f"construction-v1/{'a' * 64}/",
                contents=[{"Key": "construction-v1/unscoped.json"}],
            ),
        ),
    ],
)
def test_plan_refuses_unclassified_accumulator_content(which, listing):
    catalog, top, construction, staging, full, references = _valid_plan_inputs()
    if which == "construction":
        construction = listing
    else:
        staging = listing
    with pytest.raises(decommission.DecommissionError, match="unexpected|direct objects"):
        decommission.build_plan(
            catalog=catalog,
            top_listing=top,
            construction_listing=construction,
            staging_listing=staging,
            full_listing=full,
            expected_current=CURRENT,
            v2_references=references,
            v2_release_count=5,
        )


class _FakeCatalogClient:
    def __init__(self, live):
        self.live = live
        self.backups = []
        self.publishes = []

    def fetch_catalog(self):
        return self.live

    def put_backup(self, name, data):
        self.backups.append((name, data))

    def publish_catalog(self, data, *, expected_etag):
        assert expected_etag == finalize_rebuild._content_etag(self.live)
        self.publishes.append((data, expected_etag))
        self.live = data


def _json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def test_catalog_publish_is_backed_up_cas_written_and_read_back():
    catalog, *_ = _valid_plan_inputs()
    _, pruned = decommission._catalog_versions(catalog, expected_current=CURRENT)
    before = _json_bytes(catalog)
    after = _json_bytes(pruned)
    client = _FakeCatalogClient(before)

    assert decommission.publish_pruned_catalog(
        client,
        before_bytes=before,
        pruned_bytes=after,
        expected_current=CURRENT,
    )
    assert client.live == after
    assert client.publishes == [(after, finalize_rebuild._content_etag(before))]
    assert len(client.backups) == 1
    name, backed_up = client.backups[0]
    assert CURRENT in name
    assert hashlib.sha256(before).hexdigest()[:16] in name
    assert backed_up == before


def test_catalog_publish_refuses_a_changed_live_catalog():
    catalog, *_ = _valid_plan_inputs()
    _, pruned = decommission._catalog_versions(catalog, expected_current=CURRENT)
    before = _json_bytes(catalog)
    client = _FakeCatalogClient(before + b"changed")
    with pytest.raises(decommission.DecommissionError, match="changed after planning"):
        decommission.publish_pruned_catalog(
            client,
            before_bytes=before,
            pruned_bytes=_json_bytes(pruned),
            expected_current=CURRENT,
        )
    assert not client.backups
    assert not client.publishes


def test_catalog_publish_treats_the_exact_pruned_state_as_an_idempotent_resume():
    catalog, *_ = _valid_plan_inputs()
    _, pruned = decommission._catalog_versions(catalog, expected_current=CURRENT)
    before = _json_bytes(catalog)
    after = _json_bytes(pruned)
    client = _FakeCatalogClient(after)

    assert not decommission.publish_pruned_catalog(
        client,
        before_bytes=before,
        pruned_bytes=after,
        expected_current=CURRENT,
    )
    assert not client.backups
    assert not client.publishes


def test_catalog_restore_requires_the_exact_pruned_catalog():
    catalog, *_ = _valid_plan_inputs()
    _, pruned = decommission._catalog_versions(catalog, expected_current=CURRENT)
    before = _json_bytes(catalog)
    after = _json_bytes(pruned)
    client = _FakeCatalogClient(after)
    decommission.restore_catalog(client, before_bytes=before, pruned_bytes=after)
    assert client.live == before

    client = _FakeCatalogClient(b"foreign")
    with pytest.raises(decommission.DecommissionError, match="refusing restore"):
        decommission.restore_catalog(client, before_bytes=before, pruned_bytes=after)


def test_evidence_backup_preserves_private_inventory_and_verified_v2_documents(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    pruned_path = tmp_path / "catalog-pruned.json"
    inventory_path = tmp_path / "inventory.json"
    v2_catalog_path = tmp_path / "v2-catalog.json"
    releases_dir = tmp_path / "releases"
    release_path = releases_dir / "2026-08-07.0" / "release.json"
    release_path.parent.mkdir(parents=True)

    catalog_path.write_text('{"catalog":"before"}\n')
    pruned_path.write_text('{"catalog":"current"}\n')
    inventory = {"schema": "paused-v2-decommission-inventory-v1", "targets": []}
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    v2_catalog_path.write_text('{"catalog":"v2"}\n')
    release_path.write_text('{"release":"v2"}\n')

    plan = {
        "inventory_sha256": decommission._object_fingerprint(inventory),
        "v2_release_builds": ["2026-08-07.0"],
        "source_sha256": {
            "v1_catalog": decommission._file_sha256(catalog_path),
            "v1_catalog_pruned": decommission._file_sha256(pruned_path),
            "v2_catalog": decommission._file_sha256(v2_catalog_path),
            "v2_releases": {
                "2026-08-07.0": decommission._file_sha256(release_path)
            },
        },
    }
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    plan_sha256 = decommission._file_sha256(plan_path)
    client = _FakeCatalogClient(b"")

    count = decommission.backup_evidence(
        client,
        plan_path=plan_path,
        inventory_path=inventory_path,
        catalog_path=catalog_path,
        pruned_catalog_path=pruned_path,
        v2_catalog_path=v2_catalog_path,
        releases_dir=releases_dir,
        expected_plan_sha256=plan_sha256,
    )

    assert count == 7
    names = [name for name, _ in client.backups]
    root = f"paused-data-decommission/{plan_sha256}"
    assert f"{root}/inventory.json" in names
    assert f"{root}/v2/catalog.json" in names
    assert f"{root}/v2/releases/2026-08-07.0/release.json" in names
    assert names[-1] == f"{root}/manifest.json"


def test_paused_resume_requires_a_complete_manifest_and_exact_remainders(tmp_path):
    catalog, top, construction, staging, plan_source, references = _valid_plan_inputs()
    plan, pruned = decommission.build_plan(
        catalog=catalog,
        top_listing=top,
        construction_listing=construction,
        staging_listing=staging,
        full_listing=plan_source,
        expected_current=CURRENT,
        v2_references=references,
        v2_release_count=1,
    )
    target_objects = [
        (f"{prefix}object", index + 1, f"target-{index}")
        for index, (_, prefix) in enumerate(decommission._plan_targets(plan))
    ]
    evidence_objects = [
        (
            f"construction-v1/{'a' * 64}/markers/finalize/places.json",
            20,
            "marker",
        ),
        (
            f"staging/global-v2/{'b' * 64}/immutable/inventory/places.json",
            21,
            "inventory",
        ),
    ]
    initial_full = _object_listing(
        *target_objects,
        *evidence_objects,
        (f"{CURRENT}/collection.json", 22, "current"),
        ("backups/existing.json", 23, "backup"),
    )
    inventory = decommission.build_recursive_inventory(
        plan=plan, full_listing=initial_full
    )

    catalog_path = tmp_path / "catalog-before.json"
    pruned_path = tmp_path / "catalog-pruned.json"
    inventory_path = tmp_path / "inventory.json"
    plan_path = tmp_path / "plan.json"
    live_path = tmp_path / "catalog-live.json"
    fresh_path = tmp_path / "full-live.json"
    v2_catalog_path = tmp_path / "v2-catalog.json"
    releases_dir = tmp_path / "releases"
    release_path = releases_dir / "2026-08-07.0" / "release.json"
    release_path.parent.mkdir(parents=True)
    catalog_path.write_bytes(_json_bytes(catalog))
    pruned_path.write_bytes(_json_bytes(pruned))
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    live_path.write_bytes(_json_bytes(pruned))
    v2_catalog_path.write_text('{"catalog":"v2"}\n')
    release_path.write_text('{"release":"v2"}\n')
    fresh_path.write_text(
        json.dumps(
            _object_listing(
                *target_objects[1:],
                *evidence_objects,
                (f"{CURRENT}/collection.json", 22, "current"),
                ("backups/existing.json", 23, "backup"),
            )
        )
    )
    plan["v2_release_builds"] = ["2026-08-07.0"]
    plan["inventory_sha256"] = decommission._object_fingerprint(inventory)
    plan["source_sha256"] = {
        "v1_catalog": decommission._file_sha256(catalog_path),
        "v1_catalog_pruned": decommission._file_sha256(pruned_path),
        "v2_catalog": decommission._file_sha256(v2_catalog_path),
        "v2_releases": {
            "2026-08-07.0": decommission._file_sha256(release_path)
        },
    }
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    plan_sha = decommission._file_sha256(plan_path)
    artifacts = [
        ("plan.json", plan_path),
        ("inventory.json", inventory_path),
        ("v1/catalog-before.json", catalog_path),
        ("v1/catalog-single-current.json", pruned_path),
        ("v2/catalog.json", v2_catalog_path),
        ("v2/releases/2026-08-07.0/release.json", release_path),
    ]
    evidence_root = f"backups/paused-data-decommission/{plan_sha}"
    manifest = {
        "schema": "paused-v2-decommission-evidence-v1",
        "plan_sha256": plan_sha,
        "objects": [
            {
                "key": f"{evidence_root}/{relative}",
                "size_bytes": path.stat().st_size,
                "sha256": decommission._file_sha256(path),
            }
            for relative, path in artifacts
        ],
    }
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    decommission.validate_paused_resume(
        plan_path=plan_path,
        inventory_path=inventory_path,
        catalog_before_path=catalog_path,
        pruned_catalog_path=pruned_path,
        live_catalog_path=live_path,
        full_listing_path=fresh_path,
        manifest_path=manifest_path,
        v2_catalog_path=v2_catalog_path,
        releases_dir=releases_dir,
        expected_plan_sha256=plan_sha,
    )

    manifest["objects"].pop()
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    with pytest.raises(decommission.DecommissionError, match="complete bundle"):
        decommission.validate_paused_resume(
            plan_path=plan_path,
            inventory_path=inventory_path,
            catalog_before_path=catalog_path,
            pruned_catalog_path=pruned_path,
            live_catalog_path=live_path,
            full_listing_path=fresh_path,
            manifest_path=manifest_path,
            v2_catalog_path=v2_catalog_path,
            releases_dir=releases_dir,
            expected_plan_sha256=plan_sha,
        )


def test_v1_resume_accepts_only_an_exact_remaining_subset(tmp_path):
    before_catalog = _catalog(CURRENT, "2026-07-28.0")
    published = datetime(2026, 8, 25, 6, tzinfo=timezone.utc)
    plan, pruned_catalog = decommission.build_v1_retention_plan(
        catalog=before_catalog,
        top_listing=_listing(f"{CURRENT}/", "2026-07-28.0/", "backups/"),
        now=published + timedelta(days=8),
        catalog_last_modified=published,
    )
    initial_full = _object_listing(
        (f"{CURRENT}/collection.json", 10, "current"),
        ("2026-07-28.0/a", 11, "old-a"),
        ("2026-07-28.0/b", 12, "old-b"),
        ("backups/existing.json", 13, "backup"),
    )
    inventory = decommission.build_recursive_inventory(
        plan=plan, full_listing=initial_full
    )

    before_path = tmp_path / "before.json"
    pruned_path = tmp_path / "pruned.json"
    live_path = tmp_path / "live.json"
    inventory_path = tmp_path / "inventory.json"
    plan_path = tmp_path / "plan.json"
    fresh_path = tmp_path / "fresh.json"
    before_path.write_bytes(_json_bytes(before_catalog))
    pruned_path.write_bytes(_json_bytes(pruned_catalog))
    live_path.write_bytes(_json_bytes(pruned_catalog))
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    plan["source_sha256"] = {
        "v1_catalog": decommission._file_sha256(before_path),
        "v1_catalog_pruned": decommission._file_sha256(pruned_path),
    }
    plan["inventory_sha256"] = decommission._object_fingerprint(inventory)
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    plan_sha = decommission._file_sha256(plan_path)
    fresh_path.write_text(
        json.dumps(
            _object_listing(
                (f"{CURRENT}/collection.json", 10, "current"),
                ("2026-07-28.0/b", 12, "old-b"),
                ("backups/existing.json", 13, "backup"),
            )
        )
    )

    decommission.validate_v1_resume(
        plan_path=plan_path,
        inventory_path=inventory_path,
        catalog_before_path=before_path,
        pruned_catalog_path=pruned_path,
        live_catalog_path=live_path,
        full_listing_path=fresh_path,
        expected_plan_sha256=plan_sha,
    )

    live_path.write_bytes(_json_bytes(before_catalog))
    with pytest.raises(decommission.DecommissionError, match="pre-prune catalog"):
        decommission.validate_v1_resume(
            plan_path=plan_path,
            inventory_path=inventory_path,
            catalog_before_path=before_path,
            pruned_catalog_path=pruned_path,
            live_catalog_path=live_path,
            full_listing_path=fresh_path,
            expected_plan_sha256=plan_sha,
        )


def test_v1_evidence_writes_pending_pointer_and_completion_marker(tmp_path):
    catalog_path = tmp_path / "catalog.json"
    pruned_path = tmp_path / "catalog-pruned.json"
    inventory_path = tmp_path / "inventory.json"
    plan_path = tmp_path / "plan.json"
    catalog_path.write_text('{"catalog":"before"}\n')
    pruned_path.write_text('{"catalog":"current"}\n')
    inventory = {"schema": "single-v1-retention-inventory-v1", "targets": []}
    inventory_path.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n")
    plan = {
        "schema": "single-v1-retention-plan-v1",
        "expected_current": CURRENT,
        "eligible": True,
        "inventory_sha256": decommission._object_fingerprint(inventory),
        "source_sha256": {
            "v1_catalog": decommission._file_sha256(catalog_path),
            "v1_catalog_pruned": decommission._file_sha256(pruned_path),
        },
    }
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    plan_sha = decommission._file_sha256(plan_path)
    client = _FakeCatalogClient(b"")

    count = decommission.backup_v1_retention_evidence(
        client,
        plan_path=plan_path,
        inventory_path=inventory_path,
        catalog_path=catalog_path,
        pruned_catalog_path=pruned_path,
        expected_plan_sha256=plan_sha,
    )
    assert count == 6
    assert (
        f"single-v1-retention/pending/{CURRENT}.json"
        in [name for name, _ in client.backups]
    )

    decommission.mark_v1_retention_complete(
        client, plan_path=plan_path, expected_plan_sha256=plan_sha
    )
    assert client.backups[-1][0] == (
        f"single-v1-retention/completed/{CURRENT}/{plan_sha}.json"
    )


def test_workflow_is_manual_dry_by_default_and_serializes_catalog_writes():
    path = ROOT / ".github/workflows/decommission-paused-data.yml"
    workflow = yaml.safe_load(path.read_text())
    assert set(workflow[True]) == {"workflow_dispatch"}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["dry_run"]["default"] is True
    assert inputs["confirmation"]["default"] == "PLAN_ONLY"
    assert inputs["expected_plan_sha256"]["default"] == "PLAN_ONLY"
    assert workflow["concurrency"]["group"] == "r2-production-catalog"
    job = workflow["jobs"]["decommission"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    checkout = job["steps"][0]
    assert checkout["uses"] == (
        "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"
    )
    assert checkout["with"]["persist-credentials"] is False


def test_one_time_cleanup_has_no_calendar_hold_but_recurring_retention_does():
    one_time = (ROOT / ".github/workflows/decommission-paused-data.yml").read_text()
    assert "rollback_hold" not in one_time
    assert "--now" not in one_time
    assert decommission.V1_PREDECESSOR_OVERLAP == timedelta(days=7)


def test_workflow_deletes_sequentially_smokes_and_pauses_without_logging_targets():
    text = (ROOT / ".github/workflows/decommission-paused-data.yml").read_text()
    steps = yaml.safe_load(text)["jobs"]["decommission"]["steps"]
    apply = next(
        step
        for step in steps
        if step.get("name")
        == "Delete one logical copy at a time with production smoke"
    )
    assert "!inputs.dry_run" in re.sub(r"\s+", "", apply["if"])
    run = apply["run"]

    helper = run[run.index("delete_and_smoke()") : run.index("mapfile -t staging")]
    assert "verify-prefix-inventory" in helper
    assert "aws s3 rm" in helper
    assert "smoke_production_pause.sh" in helper
    assert "sleep 65" in helper
    assert helper.index("aws s3 rm") < helper.index("smoke_production_pause.sh") < helper.index("sleep 65")
    assert 'echo "$target"' not in run
    assert "s3://$R2_BUCKET/$target" in run  # variable, never expanded in source logs

    order = [
        "publish-catalog",
        "sleep 390",
        "mapfile -t staging",
        "mapfile -t construction",
        "mapfile -t slices",
        "mapfile -t roots",
        'delete_and_smoke "$(jq -r',
    ]
    positions = [run.index(item) for item in order]
    assert positions == sorted(positions)
    assert "restore-catalog" in run
    assert "verify-prefix-remainder" in run
    assert 'if [ "$RESUME" = true ]' in run
    assert ".construction_data_prefixes[]" in run
    assert ".staging_data_prefixes[]" in run
    assert "verify-preserved-evidence" in run
    assert "construction_evidence_prefixes" in run


def test_workflow_preflight_checks_paused_producers_and_private_v2_chain():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/decommission-paused-data.yml").read_text()
    )
    steps = workflow["jobs"]["decommission"]["steps"]
    disabled = next(
        step for step in steps if step.get("name", "").startswith("Assert producers")
    )["run"]
    for filename in (
        "construction-v1.yml",
        "reverse-v2.yml",
        "promote-v2-release.yml",
        "retire-build-scratch.yml",
        "r2-cleanup.yml",
    ):
        assert filename in disabled

    plan = next(
        step for step in steps if step.get("name") == "Build the private fail-closed deletion plan"
    )["run"]
    assert "v2/catalog.json" in plan
    assert "v2-releases-dir" in plan
    assert "--full-listing" in plan
    assert "--inventory-output" in plan
    assert "RUNNER_TEMP" in plan
    assert "sha256sum" in plan
    assert "EXPECTED_PLAN_SHA256" in plan
    assert "differs from the reviewed dry-run plan" in plan
    assert "upload-artifact" not in json.dumps(workflow)

    disabled = next(
        step for step in steps if step.get("name", "").startswith("Assert producers")
    )["run"]
    apply = next(
        step
        for step in steps
        if step.get("name")
        == "Delete one logical copy at a time with production smoke"
    )
    for state in ("queued", "in_progress", "waiting", "requested", "pending"):
        assert state in disabled
        assert state in apply["run"]
    assert "backup-evidence" in apply["run"]
    plan = next(
        step for step in steps if step.get("name") == "Build the private fail-closed deletion plan"
    )["run"]
    assert "validate-paused-resume" in plan
    assert "backups/paused-data-decommission/$EXPECTED_PLAN_SHA256" in plan
    assert "$evidence_root/manifest.json" in plan
    assert '--manifest "$WORK/manifest.json"' in plan


def test_smoke_contract_covers_every_supported_and_paused_route():
    smoke = (ROOT / "scripts/smoke_production_pause.sh").read_text()
    for fragment in (
        '"$BASE_URL/"',
        '"$BASE_URL/health"',
        '"$BASE_URL/search?q=',
        '"$BASE_URL/reverse?lat=',
        '"$BASE_URL/id/$gers_id"',
        "/v2/forward",
        "/v2/reverse",
        "/v2/ids/",
        "-I -o /dev/null",
        "-X OPTIONS",
    ):
        assert fragment in smoke


def test_recurring_v1_workflow_is_age_gated_v2_blocked_and_serial():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/retain-one-v1-copy.yml").read_text()
    )
    assert "schedule" in workflow[True]
    assert "workflow_dispatch" in workflow[True]
    job = workflow["jobs"]["retain"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["concurrency"]["group"] == "r2-production-catalog"
    checkout = job["steps"][0]
    assert checkout["uses"].endswith("9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0")
    assert checkout["with"]["persist-credentials"] is False

    steps = job["steps"]
    v2_guard = next(
        step
        for step in steps
        if step.get("name") == "Refuse retention while the v2 rollback graph exists"
    )["run"]
    assert "--prefix v2/" in v2_guard
    plan = next(
        step for step in steps if step.get("name") == "Build exact private v1 retention plan"
    )["run"]
    assert "plan-v1-retention" in plan
    assert "catalog-head.json" in plan
    assert "--full-listing" in plan

    apply = next(
        step for step in steps if step.get("name") == "Prune aged predecessors serially"
    )["run"]
    for fragment in (
        "backup-v1-evidence",
        "publish-catalog",
        "restore-catalog",
        "verify-prefix-inventory",
        "verify-prefix-remainder",
        "sleep 390",
        "aws s3 rm",
        "smoke_production_pause.sh",
        "sleep 65",
        "mark-v1-complete",
    ):
        assert fragment in apply
    assert apply.index("publish-catalog") < apply.index("sleep 390")
    assert apply.index("sleep 390") < apply.index("aws s3 rm")
    assert "validate-v1-resume" in plan
    assert "single-v1-retention/pending/$current.json" in plan
