"""Offline tests for the construction-v1 slice inventory builder.

The builder's whole job is to turn one real Overture object into a valid, finest
possible canonical inventory plus the index of the task that covers a bbox. These
tests cover both halves offline: the footer bbox search against a locally written
parquet, and the address branch against synthetic footers, asserting the report it
produces is one the address projector's own canonical validator accepts.

The network path (real S3 listing + footers) is exercised by
``scripts/run_slice_construction_v1.py``, not here.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


builder = _load("build_slice_inventory_v1", "scripts/build_slice_inventory_v1.py")
inventory = _load("inventory_address_rowgroups", "scripts/inventory_address_rowgroups.py")

RELEASE = "2026-07-22.0"
ADDRESS_PREFIX = (
    f"s3://{inventory.BUCKET}/release/{RELEASE}/theme=addresses/type=address/"
)


def address_table(pa, row_count: int, *, bbox=None):
    address_level_type = pa.list_(pa.struct([pa.field("value", pa.string())]))
    columns = {
        "id": pa.array([f"id-{index}" for index in range(row_count)]),
        "street": pa.array(["Main Street"] * row_count),
        "number": pa.array([str(index) for index in range(row_count)]),
        "unit": pa.array([""] * row_count),
        "postcode": pa.array(["98104"] * row_count),
        "postal_city": pa.array(["Seattle"] * row_count),
        "address_levels": pa.array(
            [[{"value": "WA"}]] * row_count, type=address_level_type
        ),
        "country": pa.array(["US"] * row_count),
        "geometry": pa.array([b"point"] * row_count, type=pa.binary()),
    }
    if bbox is not None:
        columns["bbox"] = bbox
    return pa.table(columns)


def synthetic_footers(uri: str, extents: list[tuple[float, float, float, float]]):
    """One synthetic address footer object, two rows per row group."""
    groups = [
        {
            "index": index,
            "rows": 2,
            "all_compressed_bytes": 400,
            "all_uncompressed_bytes": 800,
            "selected_compressed_bytes": 200,
            "selected_uncompressed_bytes": 400,
            "country_min": "US",
            "country_max": "US",
            "exact_country": "US",
            "bbox_xmin_min": extent[0],
            "bbox_xmax_max": extent[1],
            "bbox_ymin_min": extent[2],
            "bbox_ymax_max": extent[3],
            "bbox_stats_complete": True,
        }
        for index, extent in enumerate(extents)
    ]
    return {
        "uri": uri,
        "etag": "etag",
        "bytes": 4096,
        "records": sum(group["rows"] for group in groups),
        "row_groups": len(groups),
        "selected_compressed_bytes": sum(
            group["selected_compressed_bytes"] for group in groups
        ),
        "selected_uncompressed_bytes": sum(
            group["selected_uncompressed_bytes"] for group in groups
        ),
        "schema_contract": inventory.canonical_schema_contract(
            [
                {"path": path, "type": kind, "nullable": True}
                for path, kind in sorted(inventory.REQUIRED_FIELD_TYPES.items())
            ]
        ),
        "groups": groups,
    }


def test_covering_row_group_finds_the_overlapping_group_from_footers_alone(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pafs = pytest.importorskip("pyarrow.fs")
    import pyarrow.parquet as pq

    # Three row groups marching east; only the middle one overlaps the query box.
    bbox = pa.StructArray.from_arrays(
        [
            pa.array([0.0, 0.1, 10.0, 10.1, 20.0, 20.1], type=pa.float64()),
            pa.array([0.05, 0.15, 10.05, 10.15, 20.05, 20.15], type=pa.float64()),
            pa.array([0.0, 0.1, 10.0, 10.1, 20.0, 20.1], type=pa.float64()),
            pa.array([0.05, 0.15, 10.05, 10.15, 20.05, 20.15], type=pa.float64()),
        ],
        names=["xmin", "xmax", "ymin", "ymax"],
    )
    path = tmp_path / "spatial.parquet"
    pq.write_table(address_table(pa, 6, bbox=bbox), path, row_group_size=2)
    filesystem = pafs.LocalFileSystem()
    assert builder.covering_row_group(filesystem, str(path), (10.0, 10.0, 10.2, 10.2)) == (1, 3)
    assert builder.covering_row_group(filesystem, str(path), (0.0, 0.0, 0.2, 0.2)) == (0, 3)
    assert builder.covering_row_group(filesystem, str(path), (50.0, 50.0, 51.0, 51.0)) == (
        None, 3,
    )

    # A source with no bbox column cannot be sliced by footer statistics at all,
    # and must say so rather than guessing a row group. The row-group count is
    # still reported, because the caller sizes the plan from it.
    plain = tmp_path / "plain.parquet"
    pq.write_table(address_table(pa, 2), plain)
    assert builder.covering_row_group(filesystem, str(plain), (0.0, 0.0, 1.0, 1.0)) == (
        None, 1,
    )


def test_finest_groups_per_task_keeps_the_plan_inside_the_task_cap():
    # 256-row-group objects (the common case) and the 512-row-group address
    # objects in 2026-07-22.0, which a hardcoded 2 would plan into 256 tasks --
    # double the cap, a hard failure.
    assert builder.finest_groups_per_task(256) == 2
    assert builder.finest_groups_per_task(512) == 4
    assert builder.finest_groups_per_task(128) == 1
    assert builder.finest_groups_per_task(129) == 2
    for row_groups in (1, 2, 127, 128, 129, 255, 256, 511, 512, 1024):
        per_task = builder.finest_groups_per_task(row_groups)
        assert -(-row_groups // per_task) <= builder.MAX_TASKS
    with pytest.raises(SystemExit, match="no row groups"):
        builder.finest_groups_per_task(0)


def test_task_covering_maps_a_row_group_to_its_task():
    tasks = [
        {"ranges": [{"first_row_group": 0, "last_row_group": 1}]},
        {"ranges": [{"first_row_group": 2, "last_row_group": 3}]},
        {"ranges": [{"first_row_group": 4, "last_row_group": 5}]},
    ]
    assert [builder.task_covering(tasks, group) for group in range(6)] == [
        0, 0, 1, 1, 2, 2,
    ]
    # A row group outside every task means the plan does not cover its own object;
    # the error has to name the row group, which is the entire diagnosis.
    with pytest.raises(SystemExit, match="row group 6 is in no task of a 3-task plan"):
        builder.task_covering(tasks, 6)


def test_addresses_slice_builds_a_canonical_inventory_the_projector_accepts(monkeypatch):
    footers = synthetic_footers(
        f"{ADDRESS_PREFIX}part-00000-slice.zstd.parquet",
        [(float(index), float(index) + 0.5, 40.0, 40.5) for index in range(6)],
    )
    monkeypatch.setattr(inventory, "inventory_object", lambda source, fs: footers)
    monkeypatch.setattr(builder, "AINV", inventory)

    report, tasks, digest = builder.addresses_slice(
        {"uri": footers["uri"], "etag": "etag", "bytes": 4096},
        None,
        release=RELEASE,
        groups_per_task=2,
    )
    # Finest plan the cap admits: two row groups per task, nothing dropped.
    assert len(tasks) == 3
    assert [task["ranges"][0]["row_groups"] for task in tasks] == [2, 2, 2]
    assert sum(task["rows"] for task in tasks) == footers["records"]
    # The address projector re-validates the inventory it is handed, including a
    # full deterministic rebuild; a slice inventory must pass that unchanged.
    identity = inventory.validate_canonical_inventory(report)
    assert identity["inventory_sha256"] == digest
    assert identity["tasks"] == tasks
    # A slice plan must never be the bbox-scoped variant: that plan shape is one
    # the canonical validator cannot reproduce.
    assert "bbox" not in report["plan"]
    assert report["plan"]["safe_at_configured_task_count"] is True


def test_addresses_slice_task_index_matches_the_covering_row_group(monkeypatch):
    footers = synthetic_footers(
        f"{ADDRESS_PREFIX}part-00001-slice.zstd.parquet",
        [(float(index), float(index) + 0.5, 40.0, 40.5) for index in range(8)],
    )
    monkeypatch.setattr(inventory, "inventory_object", lambda source, fs: footers)
    monkeypatch.setattr(builder, "AINV", inventory)
    _, tasks, _ = builder.addresses_slice(
        {"uri": footers["uri"], "etag": "etag", "bytes": 4096},
        None,
        release=RELEASE,
        groups_per_task=2,
    )
    # Row group 5 lands in task 2, and that task's range really contains it.
    task_index = builder.task_covering(tasks, 5)
    assert task_index == 2
    selected = tasks[task_index]["ranges"][0]
    assert selected["first_row_group"] <= 5 <= selected["last_row_group"]


def test_places_slice_builds_a_canonical_inventory_the_projector_accepts(monkeypatch):
    """The places branch is an extraction, so it needs its own regression.

    Same claim as the address branch: the report must pass the family's own
    canonical validator, and the plan must be the finest the task cap admits.
    """
    places = _load("places_inventory_v1_slice", "scripts/places_inventory_v1.py")
    uri = f"{places.approved_prefix(RELEASE)}part-00000-slice.zstd.parquet"
    source = {"uri": uri, "etag": "etag", "bytes": 4096}
    contract = places.canonical_schema_contract(
        [
            {"path": path, "type": kind, "nullable": True}
            for path, kind in sorted(places.REQUIRED_FIELD_TYPES.items())
        ]
    )
    details = {
        "records": 12,
        "row_group_count": 6,
        "row_groups": [
            {
                "index": index,
                "rows": 2,
                "selected_compressed_bytes": 200,
                "selected_uncompressed_bytes": 400,
            }
            for index in range(6)
        ],
        "schema_contract": contract,
    }
    profiles = []

    def inspect(_source, _filesystem, *, profile="auto"):
        profiles.append(profile)
        return details

    monkeypatch.setattr(places, "inspect_parquet_object", inspect)
    monkeypatch.setattr(builder, "INV", places)

    inventory, tasks, digest = builder.places_slice(
        source, None, release=RELEASE, groups_per_task=2
    )
    assert len(tasks) == 3
    assert [task["ranges"][0]["row_groups"] for task in tasks] == [2, 2, 2]
    assert sum(task["expected_input_records"] for task in tasks) == details["records"]
    identity = places.validate_inventory(inventory)
    assert identity["inventory_sha256"] == digest
    assert builder.task_covering(tasks, 5) == 2
    assert profiles == ["auto"]


def test_places_slice_can_force_the_taxonomy_contract(monkeypatch):
    places = _load("places_inventory_v1_taxonomy_slice", "scripts/places_inventory_v1.py")
    uri = f"{places.approved_prefix(RELEASE)}part-00000-slice.zstd.parquet"
    source = {"uri": uri, "etag": "etag", "bytes": 4096}
    contract = places.canonical_schema_contract(
        [
            {"path": path, "type": kind, "nullable": True}
            for path, kind in sorted(places.TAXONOMY_REQUIRED_FIELD_TYPES.items())
        ]
    )
    details = {
        "records": 4,
        "row_group_count": 2,
        "row_groups": [
            {
                "index": index,
                "rows": 2,
                "selected_compressed_bytes": 200,
                "selected_uncompressed_bytes": 400,
            }
            for index in range(2)
        ],
        "schema_contract": contract,
    }
    profiles = []

    def inspect(_source, _filesystem, *, profile="auto"):
        profiles.append(profile)
        return details

    monkeypatch.setattr(places, "inspect_parquet_object", inspect)
    monkeypatch.setattr(builder, "INV", places)

    inventory, tasks, digest = builder.places_slice(
        source,
        None,
        release=RELEASE,
        groups_per_task=1,
        schema_profile="taxonomy",
    )

    assert profiles == ["taxonomy"]
    assert places.schema_profile_name(inventory["schema_contract"]) == "taxonomy"
    assert len(tasks) == 2
    assert places.validate_inventory(inventory)["inventory_sha256"] == digest


def test_main_writes_the_inventory_and_reports_one_records_key(monkeypatch, tmp_path):
    """--family addresses must print the same keys the places branch prints."""
    footers = synthetic_footers(
        f"{ADDRESS_PREFIX}part-00002-slice.zstd.parquet",
        [(float(index), float(index) + 0.5, 40.0, 40.5) for index in range(4)],
    )
    monkeypatch.setattr(inventory, "inventory_object", lambda source, fs: footers)
    monkeypatch.setattr(builder, "AINV", inventory)
    monkeypatch.setattr(
        inventory, "list_objects",
        lambda release: [{"uri": footers["uri"], "etag": "etag", "bytes": 4096}],
    )
    monkeypatch.setattr(builder, "covering_row_group", lambda fs, uri, bbox: (2, 4))

    class _Filesystem:
        def __init__(self, *_, **__):
            pass

    monkeypatch.setattr(
        "pyarrow.fs.S3FileSystem", _Filesystem, raising=False
    )
    output = tmp_path / "inventory.json"
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *args, **kw: printed.append(args[0]))
    assert builder.main([
        "--family", "addresses", "--release", RELEASE,
        "--bbox", "1.0", "40.0", "1.1", "40.1", "--output", str(output),
    ]) == 0
    summary = json.loads(printed[-1])
    # Four row groups is well inside the 128-task cap, so the finest plan is one
    # group per task and the covering row group 2 is task 2.
    assert summary == {
        "family": "addresses",
        "object_index": 0,
        "row_group": 2,
        "source_row_groups": 4,
        "groups_per_task": 1,
        "task_index": 2,
        "task_records": 2,
        "tasks": 4,
        "inventory_sha256": summary["inventory_sha256"],
    }
    written = json.loads(output.read_text())
    assert inventory.validate_canonical_inventory(written)["inventory_sha256"] == (
        summary["inventory_sha256"]
    )


# ---------------------------------------------------------------------------
# Local source mirror (OVERTURE_SOURCE_MIRROR)
# ---------------------------------------------------------------------------


def test_source_filesystem_defaults_to_anonymous_s3(monkeypatch):
    # The default must stay S3: CI and every promotion path depend on it, and
    # an accidentally-set mirror must never be the silent default.
    monkeypatch.delenv(builder.SOURCE_MIRROR_ENV, raising=False)
    import pyarrow.fs as pafs

    assert isinstance(builder.source_filesystem(pafs, region="us-west-2"), pafs.S3FileSystem)


def test_source_filesystem_reads_through_a_configured_mirror(monkeypatch, tmp_path):
    import pyarrow.fs as pafs

    key = "overturemaps-us-west-2/release/2026-07-22.0/theme=places/x.parquet"
    target = tmp_path / key
    target.parent.mkdir(parents=True)
    target.write_bytes(b"mirror-bytes")

    monkeypatch.setenv(builder.SOURCE_MIRROR_ENV, str(tmp_path))
    filesystem = builder.source_filesystem(pafs, region="us-west-2")
    # Callers strip only the `s3://` scheme, so the mirror must be
    # bucket-shaped and resolve the remaining key verbatim.
    with filesystem.open_input_file(key) as handle:
        assert handle.readall() == b"mirror-bytes"


def test_missing_mirror_directory_fails_closed(monkeypatch, tmp_path):
    # Rather than silently falling back to S3 and quietly costing a slow run
    # that the operator believed was local.
    import pyarrow.fs as pafs

    monkeypatch.setenv(builder.SOURCE_MIRROR_ENV, str(tmp_path / "absent"))
    with pytest.raises(SystemExit, match="not a directory"):
        builder.source_filesystem(pafs, region="us-west-2")


def test_empty_mirror_value_is_treated_as_unset(monkeypatch):
    import pyarrow.fs as pafs

    monkeypatch.setenv(builder.SOURCE_MIRROR_ENV, "")
    assert isinstance(builder.source_filesystem(pafs, region="us-west-2"), pafs.S3FileSystem)


def test_projector_default_filesystem_honours_the_mirror(monkeypatch, tmp_path):
    # The projector is the path a head build reads through, so its DEFAULT has
    # to honour the mirror -- not just the inventory builder's.
    import pyarrow.fs as pafs

    projector = _load(
        "project_places_construction_v1_mirror",
        "scripts/project_places_construction_v1.py",
    )
    monkeypatch.setenv(builder.SOURCE_MIRROR_ENV, str(tmp_path))
    resolved = projector.source_filesystem(pafs, region="us-west-2")
    assert isinstance(resolved, pafs.SubTreeFileSystem)

    monkeypatch.delenv(builder.SOURCE_MIRROR_ENV, raising=False)
    assert isinstance(
        projector.source_filesystem(pafs, region="us-west-2"), pafs.S3FileSystem
    )


def test_slice_builder_and_projector_share_one_mirror_implementation():
    # Two copies would drift, and the one that drifted would be the one that
    # quietly stopped honouring the mirror.
    projector = _load(
        "project_places_construction_v1_shared",
        "scripts/project_places_construction_v1.py",
    )
    assert builder.source_filesystem is projector.source_filesystem
