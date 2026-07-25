"""Tests for `overture-address-map-address-records-v1`.

The artifact's whole value is that an address cannot go missing from it and cannot
be filed under the wrong cell, so the tests are organised around exactly those two
claims plus the additive guarantee:

* the cell/bucket math is bit-identical to the Places implementations (the Rust
  `route()` binary for the cell, `places_construction_v1` for the shuffle);
* every admitted row appears exactly once, bucketed, ordered, deterministic;
* the forward address packs are byte-identical to before the artifact existed;
* a resumed marker without an intact artifact fails closed.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


pa = pytest.importorskip("pyarrow")
pq = pytest.importorskip("pyarrow.parquet")
duckdb = pytest.importorskip("duckdb")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


SPIKE_TEST = _load(
    "address_records_spike_helpers", "tests/test_address_construction_spike.py"
)
PLACES_TEST = _load(
    "address_records_places_helpers", "tests/test_places_construction_v1.py"
)
ADDRESS = _load("address_records_construction", "scripts/address_construction_v1.py")
PLACES = _load("address_records_places", "scripts/places_construction_v1.py")


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bins"],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {
        "transform": target / "address-transform-v1",
        "directory": target / "address-proof-directory",
        "encoder": target / "address-serving-encode-v1",
        "verifier": target / "address-serving-verify-v1",
        "places-transform": target / "places-transform-v1",
    }


def limits(**overrides) -> ADDRESS.Limits:
    values = dict(
        max_input_rows=4_000,
        max_pack_rows=4,
        parquet_row_group_rows=2_048,
        max_rss_bytes=2 * 1024**3,
        max_scratch_bytes=1024**3,
        max_output_bytes=128 * 1024**2,
        max_serving_bytes=16 * 1024**2,
        wall_seconds=300,
        duckdb_memory_limit="512MB",
        duckdb_threads=2,
        allow_unpinned_duckdb=True,
    )
    values.update(overrides)
    return ADDRESS.Limits(**values)


# Points chosen so the fixture spans several cells AND several shuffle buckets:
# Seattle straddles the level-8 x boundary at lon -122.34375, Monaco straddles the
# y boundary at lat 43.9453125, and (0, 0) is null island.
POINTS = [
    (-122.35, 47.60),   # c328
    (-122.33, 47.60),   # c329
    (7.42, 43.74),      # be85
    (7.42, 44.30),      # bf85, across the y boundary at lat 44.296875
    (0.0, 0.0),         # 8080, null island
]


def rows(count: int = 10, *, points=POINTS, duplicate_ids: bool = False) -> list[dict]:
    built = []
    for index in range(count):
        longitude, latitude = points[index % len(points)]
        feature = 1 if duplicate_ids else index + 1
        built.append(
            {
                "id": str(uuid.UUID(int=feature)),
                "street": "Main Street",
                "number": str(index),
                "unit": "",
                "postcode": "02180",
                "postal_city": "Stoneham",
                "address_levels": ["MA", "Stoneham"],
                "country": "US",
                "point": [longitude, latitude],
                "source_object_index": 0,
                "source_row_group": 0,
                "source_row_index": index,
            }
        )
    return built


def run_map(tmp_path: Path, binaries, *, table=None, **kwargs):
    fixture_rows = kwargs.pop("fixture_rows", None) or rows()
    tmp_path.mkdir(parents=True, exist_ok=True)
    projected = tmp_path / "projected.parquet"
    SPIKE_TEST.write_fixture(projected, fixture_rows)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(
        json.dumps({"objects": [{"records": len(fixture_rows), "row_groups": 1}]}) + "\n"
    )
    store = ADDRESS.LocalObjectStore(tmp_path / "objects")
    marker = ADDRESS.map_task(
        input_path=projected,
        source_limits=source_limits,
        store=store,
        scratch_root=tmp_path / "scratch",
        request_sha256="b" * 64,
        task_id=kwargs.pop("task_id", "address-map-000"),
        transform_binary=binaries["transform"],
        directory_binary=binaries["directory"],
        limits=kwargs.pop("limits", limits()),
        **kwargs,
    )
    return marker, store


# --------------------------------------------------------------------------- #
# cell + shuffle parity with the Places implementations
# --------------------------------------------------------------------------- #
def boundary_e7_points() -> list[tuple[int, int]]:
    """Every cell boundary on both axes, plus one E7 unit either side of each.

    `route()`'s only Rust test is a single interior point, so the mirror cannot be
    trusted on the strength of that: the interesting values are exactly the ones a
    floor and a clamp disagree about.
    """
    points: list[tuple[int, int]] = []
    for index in range(ADDRESS.CELL_GRID + 1):
        longitude = index * ADDRESS.LONGITUDE_E7_PER_CELL - ADDRESS.LONGITUDE_E7_ORIGIN
        for delta in (-1, 0, 1):
            value = longitude + delta
            if abs(value) <= ADDRESS.LONGITUDE_E7_ORIGIN:
                points.append((value, 0))
        latitude = index * ADDRESS.LATITUDE_E7_PER_CELL - ADDRESS.LATITUDE_E7_ORIGIN
        for delta in (-1, 0, 1):
            value = latitude + delta
            if abs(value) <= ADDRESS.LATITUDE_E7_ORIGIN:
                points.append((0, value))
    # The four world corners, which are the only inputs that reach the clamp for a
    # coordinate the Places transform accepts.
    points.extend(
        [
            (-ADDRESS.LONGITUDE_E7_ORIGIN, -ADDRESS.LATITUDE_E7_ORIGIN),
            (-ADDRESS.LONGITUDE_E7_ORIGIN, ADDRESS.LATITUDE_E7_ORIGIN),
            (ADDRESS.LONGITUDE_E7_ORIGIN, -ADDRESS.LATITUDE_E7_ORIGIN),
            (ADDRESS.LONGITUDE_E7_ORIGIN, ADDRESS.LATITUDE_E7_ORIGIN),
            (0, 0),
            # One E7 unit south-west of the origin: -1e-7 degrees, which is the
            # cell BELOW (0, 0) on both axes, so it catches an off-by-one that a
            # symmetric formula would hide.
            (-1, -1),
        ]
    )
    # A deterministic interior spread, so the test is not only boundaries.
    for index in range(200):
        longitude = (index * 917_293_331) % (2 * ADDRESS.LONGITUDE_E7_ORIGIN + 1)
        latitude = (index * 461_168_601) % (2 * ADDRESS.LATITUDE_E7_ORIGIN + 1)
        points.append(
            (
                longitude - ADDRESS.LONGITUDE_E7_ORIGIN,
                latitude - ADDRESS.LATITUDE_E7_ORIGIN,
            )
        )
    return points


def test_route_e7_matches_the_authoritative_rust_route_at_every_boundary(
    tmp_path, binaries
):
    """The Python mirror must equal `route()` in places_transform_v1.rs exactly.

    Run through the REAL binary rather than a re-derived formula: a second copy of
    the formula in the test would only prove the test agrees with itself.
    """
    points = boundary_e7_points()
    places_rows = [
        {
            "id": str(uuid.UUID(int=index + 1)),
            "primary_name": f"Feature {index}",
            "country": "US",
            "point": [longitude / 1e7, latitude / 1e7],
            "source_object_index": 0,
            "source_row_group": 0,
            "source_row_index": index,
        }
        for index, (longitude, latitude) in enumerate(points)
    ]
    _, table = PLACES_TEST.run_transform(
        tmp_path, binaries["places-transform"], places_rows, use_limits=False
    )
    observed: dict[str, tuple[str, int]] = {}
    for feature, cell, key in zip(
        table.column("feature_id").to_pylist(),
        table.column("partition_cell").to_pylist(),
        table.column("partition_key").to_pylist(),
        strict=True,
    ):
        observed[bytes(feature).hex()] = (cell, key)
    # Every row must have survived the transform; a silently rejected row would
    # otherwise make the comparison vacuous.
    assert len(observed) == len(points)
    for index, (longitude, latitude) in enumerate(points):
        identity = uuid.UUID(int=index + 1).bytes.hex()
        assert ADDRESS.route_e7(longitude, latitude) == (
            observed[identity][1],
            observed[identity][0],
        ), f"route mismatch at E7 ({longitude}, {latitude})"


def test_route_e7_sql_matches_the_python_mirror_including_the_clamp():
    """The DuckDB expressions and the Python mirror are one contract, two writings.

    Includes out-of-world coordinates, which `Int32` E7 can represent and the
    address transform does not reject -- the SQL must clamp them exactly as the
    Rust does rather than produce a negative index or overflow.
    """
    points = boundary_e7_points() + [
        (2_000_000_000, 0), (-2_000_000_000, 0),
        (0, 1_000_000_000), (0, -1_000_000_000),
        (2_147_483_647, 2_147_483_647), (-2_147_483_648, -2_147_483_648),
    ]
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE points (longitude_e7 INTEGER, latitude_e7 INTEGER)"
    )
    connection.executemany("INSERT INTO points VALUES (?, ?)", points)
    key_sql, cell_sql = ADDRESS.route_e7_sql()
    observed = connection.execute(
        f"SELECT longitude_e7, latitude_e7, {key_sql}, {cell_sql} FROM points"
    ).fetchall()
    assert len(observed) == len(points)
    for longitude, latitude, key, cell in observed:
        assert (int(key), cell) == ADDRESS.route_e7(longitude, latitude)


def test_shuffle_and_cell_key_are_identical_to_the_places_implementations():
    assert ADDRESS.SHUFFLE_BUCKET_BITS == PLACES.SHUFFLE_BUCKET_BITS
    assert ADDRESS.SHUFFLE_MULTIPLIER == PLACES.SHUFFLE_MULTIPLIER
    # Every cell in the world, both directions: the bucket of a key, and the key of
    # a cell string. A low-bits shuffle bug keeps cell COUNTS uniform, so only an
    # exhaustive identity like this catches a divergence.
    for bits in (4, 8, 12):
        for key in range(256 * 256):
            assert ADDRESS.shuffle_bucket(key, bits) == PLACES.shuffle_bucket(key, bits)
    for y in range(256):
        for x in range(256):
            cell = f"{y:02x}{x:02x}"
            assert ADDRESS.cell_partition_key(cell) == PLACES.cell_partition_key(cell)
            assert ADDRESS.route_e7(
                x * ADDRESS.LONGITUDE_E7_PER_CELL - ADDRESS.LONGITUDE_E7_ORIGIN,
                y * ADDRESS.LATITUDE_E7_PER_CELL - ADDRESS.LATITUDE_E7_ORIGIN,
            ) == ((y << 8) | x, cell)
    # The SQL forms must agree too. Compared by EVALUATING both against every cell
    # key rather than by string equality, which would only pin the whitespace.
    connection = duckdb.connect()
    connection.execute(
        "CREATE TABLE keys AS SELECT range::UINTEGER AS partition_key FROM range(65536)"
    )
    mismatches = connection.execute(
        f"SELECT count(*) FROM keys WHERE {ADDRESS.shuffle_bucket_sql('partition_key', 8)} "
        f"IS DISTINCT FROM {PLACES.shuffle_bucket_sql(8)}"
    ).fetchone()[0]
    assert mismatches == 0


def test_shuffle_bits_out_of_range_fails_closed():
    with pytest.raises(ValueError, match="shuffle bucket bits"):
        limits(shuffle_bucket_bits=32).validate()
    with pytest.raises(ValueError, match="must be positive"):
        limits(shuffle_bucket_bits=0).validate()


# --------------------------------------------------------------------------- #
# completeness, bucketing, ordering, determinism
# --------------------------------------------------------------------------- #
def read_pack(store, pack) -> pa.Table:
    return pq.read_table(store.path(pack["object"]["key"]))


def test_records_are_complete_bucketed_ordered_and_self_describing(tmp_path, binaries):
    marker, store = run_map(tmp_path, binaries)
    artifact = marker["address_records"]
    admitted = marker["transform"]["admitted_rows"]

    assert artifact["schema"] == ADDRESS.ADDRESS_RECORDS_SCHEMA
    # EXACTLY the admitted count -- an equality, not a bound.
    assert artifact["records"] == admitted == 10
    assert artifact["admitted_rows"] == admitted
    assert sum(pack["records"] for pack in artifact["packs"]) == admitted
    assert artifact["unroutable_records"] == 0
    # Null island is counted and kept, never dropped: two of the ten fixture rows.
    assert artifact["null_island_records"] == 2

    buckets = [pack["shuffle_bucket"] for pack in artifact["packs"]]
    assert buckets == sorted(buckets) == sorted(set(buckets))
    # The fixture spans four real cells plus null island, in more than one bucket.
    cells = {
        cell["partition_cell"]
        for pack in artifact["packs"]
        for cell in pack["directory"]["cells"]
    }
    assert cells == {"c328", "c329", "be85", "bf85", "8080"}
    assert len(buckets) > 1

    for pack in artifact["packs"]:
        table = read_pack(store, pack)
        assert table.num_rows == pack["records"]
        assert "records_bucket" not in table.schema.names
        # The bucket is implicit in the pack, and `partition_key` is a pure decode
        # of `partition_cell` -- neither is a column. Pinned as an exact set so a
        # future redundant column has to be argued for rather than drifting in at
        # ~4 bytes x 473M rows.
        assert set(table.schema.names) == {
            "feature_id", "partition_cell",
            "longitude_e7", "latitude_e7",
            "source_object_index", "source_row_group", "source_row_index",
            *ADDRESS.ADDRESS_RECORDS_DISPLAY_COLUMNS,
        }
        assert "partition_key" not in table.schema.names
        keys = list(
            zip(
                table.column("partition_cell").to_pylist(),
                [bytes(v) for v in table.column("feature_id").to_pylist()],
                table.column("source_object_index").to_pylist(),
                table.column("source_row_group").to_pylist(),
                table.column("source_row_index").to_pylist(),
                strict=True,
            )
        )
        assert keys == sorted(keys)
        # Every cell in the pack hashes to the pack's own bucket -- the property the
        # shuffle exists for, and the reason one consumer can own a whole cell.
        for cell in table.column("partition_cell").to_pylist():
            key = ADDRESS.cell_partition_key(cell)
            assert ADDRESS.shuffle_bucket(key, 8) == pack["shuffle_bucket"]
        # Coordinates round-trip to the cell they are filed under.
        for longitude, latitude, cell in zip(
            table.column("longitude_e7").to_pylist(),
            table.column("latitude_e7").to_pylist(),
            table.column("partition_cell").to_pylist(),
            strict=True,
        ):
            assert ADDRESS.route_e7(longitude, latitude)[1] == cell

        directory = pack["directory"]
        assert directory["schema"] == ADDRESS.ADDRESS_RECORDS_DIRECTORY_SCHEMA
        assert directory["records"] == table.num_rows
        assert sum(group["records"] for group in directory["row_groups"]) == (
            table.num_rows
        )
        assert sum(cell["records"] for cell in directory["cells"]) == table.num_rows
        # Per-cell counts are what a reverse consumer sizes a shard from without
        # reading data, so they must be per row group as well as per pack.
        per_cell: dict[str, int] = {}
        for group in directory["row_groups"]:
            for cell in group["cells"]:
                per_cell[cell["partition_cell"]] = (
                    per_cell.get(cell["partition_cell"], 0) + cell["records"]
                )
        assert per_cell == {
            cell["partition_cell"]: cell["records"] for cell in directory["cells"]
        }
        # The embedded directory is exactly the published one.
        assert json.loads(
            store.path(pack["directory_object"]["key"]).read_text()
        ) == directory


def test_records_are_byte_deterministic_across_runs(tmp_path, binaries):
    """Two independent runs must produce byte-identical packs.

    Determinism here rests on two mechanisms, both already in the map phase and
    both load-bearing: `map_task` sets `SET threads = 1` before the pack loop, so
    DuckDB cannot interleave rows nondeterministically, and every records COPY uses
    `PRESERVE_ORDER true` over a total ORDER BY. Content-addressed keys mean a
    regression in either shows up as a changed SHA-256 rather than as a subtly
    reordered file.
    """
    first, _ = run_map(tmp_path / "a", binaries)
    second, _ = run_map(tmp_path / "b", binaries)
    def identity(marker):
        return [
            (pack["shuffle_bucket"], pack["records"], pack["object"]["sha256"],
             pack["directory_object"]["sha256"])
            for pack in marker["address_records"]["packs"]
        ]
    assert identity(first) == identity(second)


def test_duplicate_feature_ids_are_two_records_not_one(tmp_path, binaries):
    """Aggregating on the feature ID would silently drop a real address."""
    fixture = rows(6, duplicate_ids=True)
    marker, store = run_map(tmp_path, binaries, fixture_rows=fixture)
    artifact = marker["address_records"]
    assert artifact["records"] == marker["transform"]["admitted_rows"] == 6
    identities = [
        (bytes(feature), locator)
        for pack in artifact["packs"]
        for feature, locator in zip(
            read_pack(store, pack).column("feature_id").to_pylist(),
            read_pack(store, pack).column("source_row_index").to_pylist(),
            strict=True,
        )
    ]
    assert len({feature for feature, _ in identities}) == 1
    assert len(identities) == 6
    assert len(set(identities)) == 6


def test_display_projection_renders_without_a_secondary_lookup(tmp_path, binaries):
    marker, store = run_map(tmp_path, binaries)
    for pack in marker["address_records"]["packs"]:
        table = read_pack(store, pack)
        row = table.slice(0, 1).to_pylist()[0]
        assert row["street"] == "Main Street"
        assert row["postal_city"] == "Stoneham"
        assert row["postcode"] == "02180"
        assert row["display_country"] == "US"
        assert row["country"] == "us"
        assert row["address_levels"] == ["MA", "Stoneham"]
        assert row["number"] != ""


# --------------------------------------------------------------------------- #
# the additive guarantee
# --------------------------------------------------------------------------- #
# Byte identity of the FORWARD address packs, pinned against digests measured on
# the parent commit (before the artifact existed) with the pinned toolchain
# (duckdb 1.5.1 / pyarrow 25.0.0). If a change to the map phase moves these, it is
# not additive and the whole premise of this artifact is broken.
FORWARD_PACK_DIGESTS_BEFORE = [
    "bd6c298440cf2490b06d9b77295e9eee0560fd071c93328ec78a0904f91fc6e1",
    "abcf9ccc749eb57b00315b72648c451ccfb1ac8c2e9b23bdd03f18516af25997",
    "5c7a51ee6c2e43df9ca97a70ebd033411fb6b578aaa2af95a87edff2de231588",
]
FORWARD_BINDING_BEFORE = {
    "records": 10,
    "semantic_sum_a": (
        "ac90fbc705ccf51cc5780f3b5e9f828fc4bb872917b43c7a63f893e40eef1733"
    ),
    "semantic_sum_b": (
        "19413f1b94853af519adfacf6a7f6ab53481058b1a2a770c766e5cc7714fd38b"
    ),
}


def test_forward_packs_are_byte_identical_to_before_the_artifact(tmp_path, binaries):
    marker, _ = run_map(
        tmp_path, binaries, task_id="address-map-golden",
        limits=limits(max_pack_rows=4, wall_seconds=120),
    )
    assert [pack["object"]["sha256"] for pack in marker["packs"]] == (
        FORWARD_PACK_DIGESTS_BEFORE
    )
    assert marker["binding"] == FORWARD_BINDING_BEFORE
    # output_bytes keeps meaning the FORWARD map output; the records artifact
    # reports its own bytes separately so a reverse artifact can never push the
    # forward output over its cap.
    assert marker["output_bytes"] == sum(
        pack[field]["bytes"]
        for pack in marker["packs"]
        for field in ("object", "directory_object")
    )
    assert marker["address_records"]["output_bytes"] > 0


def test_downstream_phases_ignore_the_records_artifact(tmp_path, binaries):
    """genesis_plan and reduce_partition must read only marker["packs"]."""
    marker, store = run_map(tmp_path, binaries)
    plan = ADDRESS.genesis_plan([marker], row_cap=1_000)
    stripped = copy.deepcopy(marker)
    stripped.pop("address_records")
    assert ADDRESS.genesis_plan([stripped], row_cap=1_000) == plan

    reduction = ADDRESS.reduce_partition(
        partition=plan["partitions"][0],
        markers=[marker],
        store=store,
        scratch_root=tmp_path / "reduce-scratch",
        directory_binary=binaries["directory"],
        encoder_binary=binaries["encoder"],
        verifier_binary=binaries["verifier"],
        limits=limits(),
    )
    assert reduction["selected_binding"] == plan["partitions"][0]["binding"]
    stripped_reduction = ADDRESS.reduce_partition(
        partition=plan["partitions"][0],
        markers=[stripped],
        store=store,
        scratch_root=tmp_path / "reduce-scratch-stripped",
        directory_binary=binaries["directory"],
        encoder_binary=binaries["encoder"],
        verifier_binary=binaries["verifier"],
        limits=limits(),
    )
    assert stripped_reduction["artifact"] == reduction["artifact"]


# --------------------------------------------------------------------------- #
# resume, fail closed
# --------------------------------------------------------------------------- #
def test_resume_fails_closed_without_an_intact_records_artifact(tmp_path, binaries):
    marker, store = run_map(tmp_path, binaries)
    identity = {"request_sha256": "b" * 64, "task_id": "address-map-000", "store": store}
    # The happy path first, so the failures below are attributable.
    ADDRESS.validate_marker(copy.deepcopy(marker), **identity)

    missing = copy.deepcopy(marker)
    missing.pop("address_records")
    with pytest.raises(ValueError, match="missing its per-address records artifact"):
        ADDRESS.validate_marker(missing, **identity)

    renamed = copy.deepcopy(marker)
    renamed["address_records"]["schema"] = "overture-address-map-something-else"
    with pytest.raises(ValueError, match="missing its per-address records artifact"):
        ADDRESS.validate_marker(renamed, **identity)

    empty = copy.deepcopy(marker)
    empty["address_records"]["packs"] = []
    with pytest.raises(ValueError, match="records no packs"):
        ADDRESS.validate_marker(empty, **identity)

    short = copy.deepcopy(marker)
    short["address_records"]["records"] += 1
    with pytest.raises(ValueError, match="do not reconstruct the record count"):
        ADDRESS.validate_marker(short, **identity)

    undercount = copy.deepcopy(marker)
    undercount["address_records"]["packs"].pop()
    undercount["address_records"]["records"] = sum(
        pack["records"] for pack in undercount["address_records"]["packs"]
    )
    with pytest.raises(ValueError, match="differ from the admitted row count"):
        ADDRESS.validate_marker(undercount, **identity)

    mislabelled = copy.deepcopy(marker)
    mislabelled["address_records"]["packs"][0]["shuffle_bucket"] += 1
    with pytest.raises(ValueError, match="bucket differs from its directory"):
        ADDRESS.validate_marker(mislabelled, **identity)

    tampered = copy.deepcopy(marker)
    tampered["address_records"]["packs"][0]["directory"]["records"] += 1
    with pytest.raises(ValueError, match="embeds a different records directory"):
        ADDRESS.validate_marker(tampered, **identity)

    # A deleted immutable object is the resume case that matters most: the marker
    # says the artifact exists and the store no longer has it.
    deleted = copy.deepcopy(marker)
    store.path(deleted["address_records"]["packs"][0]["object"]["key"]).unlink()
    with pytest.raises(ValueError, match="records object is missing or changed"):
        ADDRESS.validate_marker(deleted, **identity)


def test_a_resumed_marker_from_before_the_artifact_is_rejected(tmp_path, binaries):
    """An old marker must not admit silently, or a run mixes task generations."""
    marker, store = run_map(tmp_path, binaries)
    legacy = copy.deepcopy(marker)
    legacy.pop("address_records")
    legacy.pop("admitted_existing", None)
    store.path(ADDRESS.marker_key("address-map-legacy")).parent.mkdir(
        parents=True, exist_ok=True
    )
    legacy["task_id"] = "address-map-legacy"
    store.write_marker_last(ADDRESS.marker_key("address-map-legacy"), legacy)
    with pytest.raises(ValueError, match="missing its per-address records artifact"):
        ADDRESS.validate_marker(
            store.read_json(ADDRESS.marker_key("address-map-legacy")),
            request_sha256="b" * 64,
            task_id="address-map-legacy",
            store=store,
        )


# --------------------------------------------------------------------------- #
# unroutable coordinates
# --------------------------------------------------------------------------- #
def _made(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def synthetic_records_table(connection, points: list[tuple[int, int]]) -> None:
    connection.execute(
        "CREATE TABLE packed (feature_id BLOB, longitude_e7 INTEGER, "
        "latitude_e7 INTEGER, source_object_index INTEGER, source_row_group INTEGER, "
        "source_row_index BIGINT, country VARCHAR, display_country VARCHAR, "
        "postal_city VARCHAR, postcode VARCHAR, street VARCHAR, number VARCHAR, "
        "unit VARCHAR, address_levels VARCHAR[])"
    )
    connection.executemany(
        "INSERT INTO packed VALUES (?, ?, ?, 0, 0, ?, 'us', 'US', 'Seattle', "
        "'98104', 'Main Street', '1', '', ['WA'])",
        [
            (uuid.UUID(int=index + 1).bytes, longitude, latitude, index)
            for index, (longitude, latitude) in enumerate(points)
        ],
    )


def test_out_of_world_coordinates_fail_closed_and_are_never_dropped(tmp_path):
    """The address transform validates the WKB shape, not the coordinate range."""
    connection = duckdb.connect()
    synthetic_records_table(
        connection, [(-1_223_400_000, 476_000_000), (2_000_000_000, 0)]
    )
    store = ADDRESS.LocalObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="outside the world bounds"):
        ADDRESS.emit_address_records(
            connection,
            source_table="packed",
            workspace=tmp_path,
            store=store,
            limits=limits(),
            admitted_rows=2,
        )
    # Nothing published: the failure precedes every write.
    assert not list((tmp_path / "objects").rglob("*.parquet"))
    assert ADDRESS.unroutable_e7(2_000_000_000, 0)
    assert not ADDRESS.unroutable_e7(ADDRESS.LONGITUDE_E7_ORIGIN, 0)


def test_a_short_artifact_fails_closed_at_map_time(tmp_path):
    connection = duckdb.connect()
    synthetic_records_table(connection, [(-1_223_400_000, 476_000_000)])
    store = ADDRESS.LocalObjectStore(tmp_path / "objects")
    with pytest.raises(ValueError, match="differ from the admitted row count"):
        ADDRESS.emit_address_records(
            connection,
            source_table="packed",
            workspace=tmp_path,
            store=store,
            limits=limits(),
            admitted_rows=2,
        )


def test_the_output_cap_bounds_the_aggregate_not_only_each_pack(tmp_path):
    """A per-pack-only cap is satisfied by any number of just-under-cap packs.

    With 256 buckets that would admit 256x the bound the cap appears to state, and
    the forward path caps the SUM of its packs -- so this one must too.
    """
    connection = duckdb.connect()
    # Points spread across many cells, so the records land in several buckets and
    # the aggregate genuinely exceeds a cap no single pack does.
    points = [
        (index * 7_000_000 - 1_000_000_000, index * 3_000_000 - 400_000_000)
        for index in range(64)
    ]
    synthetic_records_table(connection, points)
    # Learn the real pack sizes first rather than guessing a cap: parquet has a
    # fixed per-file overhead, so a hardcoded threshold would silently become a
    # per-pack failure and stop testing the aggregate at all.
    generous = ADDRESS.emit_address_records(
        connection,
        source_table="packed",
        workspace=_made(tmp_path / "generous"),
        store=ADDRESS.LocalObjectStore(tmp_path / "objects-generous"),
        limits=limits(),
        admitted_rows=len(points),
    )
    assert len(generous["packs"]) > 1
    largest = max(pack["object"]["bytes"] for pack in generous["packs"])
    total = generous["output_bytes"]
    assert largest < total
    # Strictly between the largest single pack and the aggregate: no pack breaches
    # it, the set does.
    cap = (largest + total) // 2
    with pytest.raises(ValueError, match="exceeded its hard cap in total"):
        ADDRESS.emit_address_records(
            connection,
            source_table="packed",
            workspace=_made(tmp_path / "capped"),
            store=ADDRESS.LocalObjectStore(tmp_path / "objects-capped"),
            limits=limits(max_output_bytes=cap),
            admitted_rows=len(points),
        )


def test_null_island_is_counted_and_kept(tmp_path):
    connection = duckdb.connect()
    synthetic_records_table(connection, [(0, 0), (0, 0), (-1_223_400_000, 476_000_000)])
    store = ADDRESS.LocalObjectStore(tmp_path / "objects")
    artifact = ADDRESS.emit_address_records(
        connection,
        source_table="packed",
        workspace=tmp_path,
        store=store,
        limits=limits(),
        admitted_rows=3,
    )
    assert artifact["records"] == 3
    assert artifact["null_island_records"] == 2
    cells = {
        cell["partition_cell"]
        for pack in artifact["packs"]
        for cell in pack["directory"]["cells"]
    }
    assert "8080" in cells
