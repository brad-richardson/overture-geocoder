"""The finalize publication budget: projected at PLAN time, not discovered at publish time.

`max_remote_operations` is enforced by a running counter inside finalize
(`construction_v1_remote.Budget.charge`). At planet scale the count is ~133,000
against an old cap of 100,000, so the cap tripped part-way through publishing tens
of thousands of objects at the very end of a multi-hour run -- and because
publication is create-only, the retry is byte-safe and completely pointless: it
trips at the same object.

These tests pin the three things that fix has to keep true:

* the per-object operation multiplier is what the real primitives charge, not a
  guess that can rot when the publication path changes;
* both pre-publication phases (`predict-reduce` in the dry run, `plan-reduce`
  before the reduce matrix is provisioned) project the publication and fail closed;
* the admitted cap covers the projected planet run, with the arithmetic executable
  rather than only written in a comment.

Plus the routed lane's token cap against the Rust encoder's hard limit, which is
the same class of defect one file over: a limit enforced only by a `bail!` after
the work is paid for.

No network, no cargo build.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


HOSTED = _load("publication_budget_hosted", "scripts/construction_v1_hosted.py")
REMOTE = _load("publication_budget_remote", "scripts/construction_v1_remote.py")
CONTROL = _load("publication_budget_control", "scripts/construction_v1_control.py")

ADDRESS_INVENTORY = ROOT / "benchmarks/address-construction-v1-data/inventory/addresses.json"
PLACES_INVENTORY = ROOT / "benchmarks/places-construction-v1-data/inventory/places.json"
ENCODER_SOURCE = (
    ROOT / "crates/geocoder-construction/src/bin/places_serving_encode_v1.rs"
)


# --------------------------------------------------------------------------- #
# 1. The multiplier is what the primitives charge
# --------------------------------------------------------------------------- #
def test_the_per_object_multiplier_is_what_the_publication_primitives_charge(tmp_path):
    """3 operations per object + 3 fixed, proven against the real primitives.

    The projection is only as good as this constant, and the constant is only
    honest if it tracks `publish_exact_set` + `verify_whole_slice_once`. Run them
    for real against a tmpdir remote and compare the charged total to the
    projection, so a change to the publication shape breaks HERE instead of at
    object 33,000 of a planet run.
    """
    objects = 5
    artifacts = []
    for index in range(objects):
        path = tmp_path / f"object-{index}.bin"
        path.write_bytes(f"payload-{index}".encode())
        artifacts.append((f"slice/objects/{index}.bin", path))

    budget = REMOTE.Budget(
        max_operations=10_000, max_write_bytes=10**9, max_read_bytes=10**9
    )
    remote = REMOTE.FilesystemRemote(tmp_path / "remote", budget)
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=artifacts,
        marker_key="markers/finalize/x.json",
        request_sha256="0" * 64,
    )
    # Publication alone: put + HEAD per object, plus the marker's put + HEAD.
    assert budget.operations == objects * 2 + 2
    expected = [
        {"key": item["key"], "sha256": item["sha256"], "bytes": item["bytes"]}
        for item in marker["artifacts"]
    ]
    REMOTE.verify_whole_slice_once(remote, prefix="slice/objects/", expected=expected)
    # And verification adds one listing plus one streaming read per object.
    assert budget.operations == objects * 3 + 3
    assert budget.operations == HOSTED.finalize_remote_operations(objects)
    assert HOSTED.FINALIZE_OPERATIONS_PER_OBJECT == 3
    assert HOSTED.FINALIZE_FIXED_OPERATIONS == 3


def test_the_operation_count_fails_closed_on_an_unusable_object_count():
    for value in (None, True, "5", 1.5, -1):
        with pytest.raises(SystemExit, match="published object count"):
            HOSTED.finalize_remote_operations(value)


# --------------------------------------------------------------------------- #
# 2. The projection and the gate
# --------------------------------------------------------------------------- #
def _projection(family: str, *, partitions: int, per_record_objects: int):
    return HOSTED._finalize_publication_projection(
        family,
        partitions=partitions,
        per_record_objects=per_record_objects,
        basis="unit test",
    )


def test_the_projection_counts_every_term_finalize_publishes():
    # Places: one serving object per partition, 4096 head shards, two objects per
    # per-record pack, two manifests.
    projection = _projection("places", partitions=10, per_record_objects=6)
    assert projection["serving_objects"] == 10
    assert projection["head_shard_objects"] == 1 << HOSTED.PLACES.DEFAULT_HEAD_SHARD_BITS
    assert projection["per_record_objects"] == 6
    assert projection["manifest_objects"] == 2
    assert projection["published_objects"] == 10 + 4096 + 6 + 2
    assert projection["projected_remote_operations"] == (10 + 4096 + 6 + 2) * 3 + 3
    # Addresses have no head phase at all, so they must not be charged head shards.
    assert HOSTED.HEAD_FAMILIES == ("places",)
    assert _projection("addresses", partitions=10, per_record_objects=6)[
        "head_shard_objects"
    ] == 0


@pytest.mark.parametrize("bad", [None, True, "10", 1.5, -1])
def test_the_projection_fails_closed_on_unusable_counts(bad):
    with pytest.raises(SystemExit):
        _projection("places", partitions=bad, per_record_objects=6)
    with pytest.raises(SystemExit):
        _projection("places", partitions=10, per_record_objects=bad)


def test_the_gate_fires_over_cap_and_passes_under():
    projection = _projection("places", partitions=10, per_record_objects=6)
    operations = projection["projected_remote_operations"]
    passing = HOSTED._gate_finalize_publication(
        {"caps": {"max_remote_operations": operations}},
        "places",
        projection=projection,
    )
    assert passing["within_cap"] is True
    assert passing["projected_remote_operations"] == operations
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._gate_finalize_publication(
            {"caps": {"max_remote_operations": operations - 1}},
            "places",
            projection=projection,
        )
    message = str(excinfo.value)
    # The message must name the projected count, the cap, and what to change.
    assert str(operations) in message
    assert f"max_remote_operations={operations - 1}" in message
    assert "construction_v1_control.py" in message


@pytest.mark.parametrize(
    "caps", [None, {}, {"max_remote_operations": None}, {"max_remote_operations": "300000"},
             {"max_remote_operations": 0}, {"max_remote_operations": True}]
)
def test_the_gate_fails_closed_on_a_missing_or_non_numeric_cap(caps):
    contract = {} if caps is None else {"caps": caps}
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._gate_finalize_publication(
            contract, "places", projection=_projection("places", partitions=1, per_record_objects=0)
        )
    assert "max_remote_operations" in str(excinfo.value) or "no caps" in str(excinfo.value)


def test_a_marker_without_its_per_record_artifact_fails_the_projection():
    # Same GAP finalize aborts on, moved to where it costs a plan phase. Counting it
    # as zero would make the projection quietly optimistic.
    good = {"task_id": "places-map-000", "positions": {"records": 1, "packs": [
        {"object": {"key": "a", "sha256": "0" * 64, "bytes": 1},
         "directory_object": {"key": "b", "sha256": "1" * 64, "bytes": 1}}]}}
    assert HOSTED._per_record_object_count([good], "places") == 2
    with pytest.raises(SystemExit, match="positions"):
        HOSTED._per_record_object_count([good, {"task_id": "places-map-001"}], "places")
    # Deduplicated by content-addressed key exactly as finalize does, so two tasks
    # that produced byte-identical packs are one object in both places.
    assert HOSTED._per_record_object_count([good, dict(good, task_id="x")], "places") == 2


def test_both_pre_publication_phases_carry_the_gate():
    # A contract test, not a behaviour test: the gate's whole value is that it runs
    # BEFORE the phases that cost money, so it must stay wired into both of them.
    import inspect

    for command in (HOSTED.cmd_plan_reduce, HOSTED.cmd_predict_reduce):
        source = inspect.getsource(command)
        assert "_gate_finalize_publication(" in source, command.__name__
        assert "_finalize_publication_projection(" in source, command.__name__


# --------------------------------------------------------------------------- #
# 3. The planet projection, against the admitted cap
# --------------------------------------------------------------------------- #
def _contract(tmp_path: Path, *, max_remote_operations: int) -> Path:
    request = tmp_path / "request.json"
    request.write_text(json.dumps({
        "schema": "overture-construction-v1-request-v1", "release": "2026-06-17.0",
        "families": {"addresses": {}, "places": {}},
        "versions": {"duckdb": "1.5.1", "pyarrow": "25.0.0", "numpy": "2.3.5",
                     "python": "3.12.12", "rustc": "test"},
        "caps": {"max_reducers_per_family": 128,
                 "max_remote_operations": max_remote_operations,
                 "max_remote_write_bytes": 1_000_000_000_000},
        "namespaces": {"immutable_root": "construction-v1/deadbeef",
                       "slice": "construction-v1/deadbeef/slice/slice-x/",
                       "markers": "construction-v1/deadbeef/markers/"},
    }) + "\n")
    contract = tmp_path / "contract.json"
    runtime = tmp_path / "runtime.json"
    assert HOSTED.main(["derive-contract", "--request", str(request),
                        "--output", str(contract), "--runtime", str(runtime),
                        "--allow-unpinned-duckdb"]) == 0
    return contract


def _predict(contract: Path, family: str, inventory: Path, capsys):
    assert HOSTED.main([
        "predict-reduce", "--contract", str(contract), "--family", family,
        "--inventory", str(inventory),
    ]) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


# The projection each family's planet inventory produces, recorded so the numbers
# in the CAPS comment are executable rather than merely asserted in prose. The
# per-record term is the STRUCTURAL bound -- every map task occupying all 256
# shuffle buckets -- because the dry run has no markers to measure. (Measured on
# release 2026-06-17.0, the four planet Places tasks inside source object 0 occupy
# 107/149/160/109 buckets, so the real Places figure is ~132,900 operations.)
PLANET_PROJECTED_OPERATIONS = {"places": 199_665, "addresses": 197_256}
OLD_REMOTE_OPERATION_CAP = 100_000


@pytest.mark.parametrize("family", ["places", "addresses"])
def test_the_planet_publication_projection_exceeds_the_old_cap(tmp_path, capsys, family):
    inventory = PLACES_INVENTORY if family == "places" else ADDRESS_INVENTORY
    contract = _contract(tmp_path, max_remote_operations=OLD_REMOTE_OPERATION_CAP)
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main([
            "predict-reduce", "--contract", str(contract), "--family", family,
            "--inventory", str(inventory),
        ])
    message = str(excinfo.value)
    assert f"exceed the admitted cap max_remote_operations={OLD_REMOTE_OPERATION_CAP}" in message
    assert str(PLANET_PROJECTED_OPERATIONS[family]) in message


@pytest.mark.parametrize("family", ["places", "addresses"])
def test_the_planet_publication_projection_fits_the_admitted_cap(tmp_path, capsys, family):
    inventory = PLACES_INVENTORY if family == "places" else ADDRESS_INVENTORY
    cap = CONTROL.CAPS["max_remote_operations"]
    out = _predict(_contract(tmp_path, max_remote_operations=cap), family, inventory, capsys)
    budget = out["publication_budget"]
    assert budget["projected_remote_operations"] == PLANET_PROJECTED_OPERATIONS[family]
    assert budget["within_cap"] is True
    assert budget["max_remote_operations"] == cap
    # The projection is arithmetic on committed counts, so state the counts too.
    inventory_json = json.loads(inventory.read_text())
    tasks = HOSTED._inventory_task_count(inventory_json)
    assert tasks == (89 if family == "places" else 127)
    assert budget["per_record_objects"] == 2 * tasks * 256
    assert budget["serving_objects"] == out["predicted_partitions"]
    assert budget["head_shard_objects"] == (4096 if family == "places" else 0)


def test_the_admitted_cap_clears_the_structural_ceiling_of_both_families():
    # The cap is sized off the ceiling, not the measurement: the inventory plan gate
    # admits at most 128 map tasks per family, so the per-record term can never
    # exceed 128 x 256 x 2 whatever a re-inventoried release looks like. This is the
    # arithmetic in the CAPS comment, executable.
    ceiling = max(
        HOSTED._finalize_publication_projection(
            family,
            partitions=partitions,
            per_record_objects=HOSTED.PER_RECORD_OBJECTS_PER_PACK * 128 * 256,
            basis="structural ceiling",
        )["projected_remote_operations"]
        # 16,888 is the committed Places partition plan; 725 the modelled address
        # partition count.
        for family, partitions in (("places", 16_888), ("addresses", 725))
    )
    assert ceiling == 259_569
    assert CONTROL.CAPS["max_remote_operations"] >= ceiling
    # And the margin is deliberately modest, because the gate above -- not the size
    # of this number -- is what makes an outgrown cap cheap to discover.
    assert CONTROL.CAPS["max_remote_operations"] < 2 * ceiling


def test_the_inventory_task_count_fails_closed():
    for inventory in ({}, {"map_plan": {}}, {"map_plan": {"task_count": 0}},
                      {"map_plan": {"task_count": "89"}}, {"plan": {"tasks": {}}}):
        with pytest.raises(SystemExit, match="map task count"):
            HOSTED._inventory_task_count(inventory)
    assert HOSTED._inventory_task_count({"plan": {"tasks": [1, 2, 3]}}) == 3
    assert HOSTED._inventory_task_count({"map_plan": {"task_count": 89}}) == 89


# --------------------------------------------------------------------------- #
# 4. The routed lane's token cap against the encoder's hard limit
# --------------------------------------------------------------------------- #
def _encoder_max_index_entries() -> int:
    for line in ENCODER_SOURCE.read_text().splitlines():
        if "const MAX_INDEX_ENTRIES" in line:
            return int(line.split("=")[1].strip().rstrip(";").replace("_", ""))
    raise AssertionError("MAX_INDEX_ENTRIES not found in the Places serving encoder")


def test_the_partition_token_cap_never_exceeds_the_serving_encoder_limit():
    """A routed index entry is a distinct token, so the planner's cap IS the encoder's.

    Routed index keys are `cell\\0token` with the cell constant per partition, so a
    routed artifact's index-entry count is exactly `count(DISTINCT token)` for that
    partition. A partition admitted at 400,000 tokens -- which the hosted caps did
    admit -- cannot be encoded: the Rust encoder `bail!`s, and unlike the head lane
    the routed lane has no fail-fast guard, so it fails after map, plan and part of
    reduce are paid for. Lowering the cap converts that into a plan-time
    subdivision, which `adaptive_genesis_plan` already does for free.
    """
    encoder = _encoder_max_index_entries()
    assert HOSTED.PLACES.SERVING_MAX_INDEX_ENTRIES == encoder == 250_000
    for value in (
        HOSTED.HOSTED_LIMITS["places"]["partition_distinct_tokens"],
        HOSTED.PLACES.Limits().partition_distinct_tokens,
    ):
        assert value <= encoder
        assert value == encoder
    # The offline plan generator must plan under the same cap the build enforces, and
    # the committed plan must record it.
    generator = _load("publication_budget_generator", "scripts/generate_places_partition_plan.py")
    assert generator.DEFAULT_CAPS["distinct_tokens"] == encoder
    committed = json.loads((ROOT / "scripts/places_partition_plan_v1.json").read_text())
    assert committed["partition_contract"]["caps"]["distinct_tokens"] == encoder
    # The worst real cell measured to date still fits, so this changes no plan today.
    assert 201_568 < encoder


def test_the_routed_encoder_still_enforces_the_limit_it_is_mirrored_from():
    # The mirror is a fail-fast convenience; the Rust side is the enforcing one, and
    # this is what says so. If the routed encode ever stops checking, lowering the
    # Python cap would be protecting nothing.
    source = ENCODER_SOURCE.read_text()
    assert "MAX_INDEX_ENTRIES" in source
    assert "bail!" in source
