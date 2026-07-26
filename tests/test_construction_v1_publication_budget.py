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
def _publish_and_verify(tmp_path: Path, remote_root: Path, objects: int):
    """Run the real primitives over ``objects`` files and return the charged budget."""
    artifacts = []
    for index in range(objects):
        path = tmp_path / f"object-{index}.bin"
        path.write_bytes(f"payload-{index}".encode())
        artifacts.append((f"slice/objects/{index}.bin", path))
    budget = REMOTE.Budget(
        max_operations=10_000, max_write_bytes=10**9, max_read_bytes=10**9
    )
    remote = REMOTE.FilesystemRemote(remote_root, budget)
    marker = REMOTE.publish_exact_set(
        remote,
        artifacts=artifacts,
        marker_key="markers/finalize/x.json",
        request_sha256="0" * 64,
    )
    published = budget.operations
    expected = [
        {"key": item["key"], "sha256": item["sha256"], "bytes": item["bytes"]}
        for item in marker["artifacts"]
    ]
    REMOTE.verify_whole_slice_once(remote, prefix="slice/objects/", expected=expected)
    return budget, published


def test_the_per_object_multiplier_is_what_the_publication_primitives_charge(tmp_path):
    """3 per object + 2 fixed + the listing, proven against the real primitives.

    The projection is only as good as these constants, and they are only honest if
    they track `publish_exact_set` + `verify_whole_slice_once`. Run them for real
    against a tmpdir remote and compare the charged total, so a change to the
    publication shape breaks HERE instead of at object 33,000 of a planet run.
    """
    objects = 5
    budget, published = _publish_and_verify(tmp_path, tmp_path / "remote", objects)
    # Publication alone: put + HEAD per object, plus the marker's put + HEAD.
    assert published == objects * 2 + 2
    # And verification adds the listing plus one streaming read per object. Five
    # objects is one listing page, so the total is unchanged from when a listing was
    # priced as a single operation -- the difference only appears at planet scale.
    assert budget.operations == objects * 3 + 2 + 1
    assert budget.operations == HOSTED.finalize_remote_operations(objects, retried=False)
    assert HOSTED.FINALIZE_OPERATIONS_PER_OBJECT == 3
    assert HOSTED.FINALIZE_FIXED_OPERATIONS == 2


def test_the_exact_prefix_listing_is_priced_by_page_not_as_one_request(tmp_path):
    """A listing of N objects is ceil(N/1000) billed requests, and both sides agree.

    `verify_whole_slice_once` makes ONE call, and the projection used to charge one
    operation for it. A planet address slice is 65,751 objects = 66 requests. It is
    ~0.02% of the budget, so this is not about dollars: the gate's entire claim is
    that the projection EQUALS what the phase charges, and a real R2 backend is where
    "one listing" stops being a defensible fiction. The reference backend therefore
    prices the same pages, which is what lets the assertion above be a measurement.
    """
    page = HOSTED.FINALIZE_LISTING_PAGE_KEYS
    assert page == 1000
    assert HOSTED.REMOTE.listing_operations(0) == 1
    assert HOSTED.REMOTE.listing_operations(page) == 1
    assert HOSTED.REMOTE.listing_operations(page + 1) == 2
    assert HOSTED.REMOTE.listing_operations(65_751) == 66
    # And the reference backend really charges it, which is the only reason the
    # primitive-versus-projection comparisons in this file mean anything.
    budget = REMOTE.Budget(max_operations=10, max_write_bytes=10**6, max_read_bytes=10**6)
    remote = REMOTE.FilesystemRemote(tmp_path / "listing", budget)
    remote.put_create_only("slice/objects/a", b"a")
    before = budget.operations
    remote.list("slice/objects/")
    assert budget.operations - before == 1
    # The projection's per-object multiplier is untouched by this; only the fixed
    # terms moved, by exactly the one operation the listing used to be.
    for objects in (1, 999, 1000, 1001, 65_751):
        assert HOSTED.finalize_remote_operations(objects, retried=False) == (
            objects * 3 + 2 + HOSTED.REMOTE.listing_operations(objects)
        )


def test_a_resumed_finalize_costs_four_per_object_and_that_is_what_is_budgeted(tmp_path):
    """The retry is DEARER, and the budget must price the retry, not the first pass.

    Create-only publication exists so an interrupted finalize can be re-run, so a
    resume is a first-class path -- and on it the create-only put still charges its
    attempt, raises `ConflictError`, and the byte-exactness check on that path
    streams the already-published object a second time. Pricing the first attempt
    would let the gate pass a run whose RESUME aborts inside `Budget.charge`, which
    is exactly the failure this whole change removes.
    """
    objects = 5
    remote_root = tmp_path / "remote"
    first, _ = _publish_and_verify(tmp_path, remote_root, objects)
    assert first.operations == objects * 3 + 2 + 1
    # Same remote, same bytes: every put now conflicts and re-reads.
    retry, _ = _publish_and_verify(tmp_path, remote_root, objects)
    assert retry.operations == objects * 4 + 3 + 1 == 24
    assert retry.operations == HOSTED.finalize_remote_operations(objects)
    assert HOSTED.FINALIZE_RETRY_OPERATIONS_PER_OBJECT == 4
    assert HOSTED.FINALIZE_RETRY_FIXED_OPERATIONS == 3
    # The default is the retry, because that is the case the cap has to cover.
    assert HOSTED.finalize_remote_operations(objects) > HOSTED.finalize_remote_operations(
        objects, retried=False
    )


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
    # Places: one serving object per partition, 4096 head shards, the head routing
    # manifest, two objects per per-record pack, two manifests.
    projection = _projection("places", partitions=10, per_record_objects=6)
    assert projection["serving_objects"] == 10
    assert projection["head_shard_objects"] == 1 << HOSTED.PLACES.DEFAULT_HEAD_SHARD_BITS
    assert projection["head_manifest_objects"] == 1
    assert projection["per_record_objects"] == 6
    assert projection["manifest_objects"] == 2
    assert projection["published_objects"] == 10 + 4096 + 1 + 6 + 2
    total = 10 + 4096 + 1 + 6 + 2
    pages = REMOTE.listing_operations(total)
    assert (projection["listing_operations"], pages) == (5, 5)
    assert projection["projected_remote_operations"] == total * 4 + 3 + pages
    assert projection["first_attempt_remote_operations"] == total * 3 + 2 + pages
    # Addresses have no head phase at all, so no head shards and no head manifest.
    assert HOSTED.HEAD_FAMILIES == ("places",)
    addresses = _projection("addresses", partitions=10, per_record_objects=6)
    assert addresses["head_shard_objects"] == 0
    assert addresses["head_manifest_objects"] == 0


def test_the_projected_term_set_is_the_set_the_publisher_actually_emits():
    """The TERM SET, pinned against `_artifact_keys` + `_positions_objects`.

    The multiplier was pinned against the primitives from the start; the term set was
    not, and #169 then added a published head routing manifest that the projection
    did not count -- so "this is an equality and not an estimate" was false. This
    drives the real `_artifact_keys` with a small head result and asserts the
    projection's own terms reproduce the object count it returns, so the next
    publication-shape change cannot slip through the same way.
    """
    reductions = [
        {"partition": {"id": f"p-{index}"},
         "routed_object": {"key": f"r{index}", "sha256": f"{index}" * 64, "bytes": 1}}
        for index in range(4)
    ]
    shards = [
        {"key": f"s{index}", "sha256": f"{index:064d}", "bytes": 1} for index in range(16)
    ]
    head = {
        "head": {"records": 1},
        "shard_objects": shards,
        "populated_shards": len(shards),
        "shard_count": len(shards),
        "manifest_object": {"key": "m", "sha256": "f" * 64, "bytes": 1},
    }
    published = HOSTED._artifact_keys("places", reductions, head)
    # Every serving member the publisher emits, enumerated by the projection's terms.
    projection = _projection(
        "places", partitions=len(reductions), per_record_objects=0
    )
    serving_terms = (
        projection["serving_objects"] + projection["head_manifest_objects"]
    )
    # The head term is the PRODUCTION shard count, so substitute this head's actual
    # shard count to compare like with like.
    assert len(published) == serving_terms + len(shards)
    assert projection["head_shard_objects"] == 1 << HOSTED.PLACES.DEFAULT_HEAD_SHARD_BITS
    # And the two finalize-authored manifests are the only members the projection
    # counts that `_artifact_keys` does not produce.
    assert projection["published_objects"] == (
        serving_terms + projection["head_shard_objects"] + projection["manifest_objects"]
    )
    assert HOSTED.FINALIZE_MANIFEST_OBJECTS == 2
    assert HOSTED.HEAD_MANIFEST_OBJECTS == 1
    # The address family's serving set is one object per reduction and nothing else.
    address_reductions = [
        {"partition": {"id": "a-0"},
         "artifact": {"key": "a0", "sha256": "0" * 64, "bytes": 1}}
    ]
    assert len(HOSTED._artifact_keys("addresses", address_reductions, None)) == 1
    assert _projection("addresses", partitions=1, per_record_objects=0)[
        "published_objects"
    ] == 1 + 2


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
    # The BEHAVIOURAL coverage is below, one test per call site -- this only catches
    # outright deletion of a call, which is why it is not the whole story.
    import inspect

    for command in (HOSTED.cmd_plan_reduce, HOSTED.cmd_predict_reduce):
        source = inspect.getsource(command)
        assert "_gate_finalize_publication(" in source, command.__name__
        assert "_finalize_publication_projection(" in source, command.__name__


# --------------------------------------------------------------------------- #
# 2b. `plan-reduce` behaviourally: the CALL SITE, not just the helper
# --------------------------------------------------------------------------- #
# A helper with good unit tests behind an unguarded call site is the hole that has
# already slipped through once on this workflow, and the substring pin above cannot
# see it: neutering the call's arguments (per-record term -> 0, cap -> a fabricated
# constant) leaves every other test in this file passing. These drive the real
# `plan-reduce` command end to end.
#
# The address family is used because its plan is pure marker arithmetic
# (`ADDRESS.genesis_plan` folds `packs[].directory.bucket_summaries`), so a real CLI
# invocation needs no store, no cargo build and no DuckDB.
def _binding(records: int) -> dict:
    return {
        "records": records,
        "semantic_sum_a": f"{records * 7:064x}",
        "semantic_sum_b": f"{records * 11:064x}",
    }


def _address_marker(
    index: int, *, record_packs: int, request_sha256: str, records: int = 4
) -> dict:
    """A minimal address map marker: one forward pack, ``record_packs`` per-record packs."""
    binding = _binding(records)
    return {
        "schema": HOSTED.ADDRESS.MARKER_SCHEMA,
        "request_sha256": request_sha256,
        "task_id": f"addresses-map-{index:03d}",
        "binding": binding,
        "packs": [
            {
                "pack_id": 0,
                "object": {
                    "key": f"forward-{index}",
                    "sha256": f"{index + 100:064x}",
                    "bytes": 1,
                },
                "directory": {
                    "bucket_summaries": [
                        {"country": "US", "maximum_bucket": index, "binding": binding}
                    ],
                    "row_groups": [
                        {
                            "index": 0,
                            "binding": binding,
                            "routing_groups": [
                                {
                                    "country": "US",
                                    "minimum_route_hash": index << 48,
                                    "maximum_route_hash": ((index + 1) << 48) - 1,
                                }
                            ],
                        }
                    ],
                }
            }
        ],
        "address_records": {
            "schema": HOSTED.ADDRESS.ADDRESS_RECORDS_SCHEMA,
            "records": records,
            "packs": [
                {
                    "object": {"key": f"k-{index}-{pack}", "sha256": f"{pack:064d}", "bytes": 1},
                    "directory_object": {
                        "key": f"d-{index}-{pack}", "sha256": f"{pack + 1:064d}", "bytes": 1
                    },
                }
                for pack in range(record_packs)
            ],
        },
    }


def _plan_reduce_addresses(tmp_path: Path, *, cap: int, record_packs: int, tag: str):
    markers_dir = tmp_path / f"markers-{tag}"
    markers_dir.mkdir()
    contract = _contract(tmp_path / tag, max_remote_operations=cap)
    marker = _address_marker(
        0,
        record_packs=record_packs,
        request_sha256=json.loads(contract.read_text())["request_sha256"],
    )
    (markers_dir / "000.json").write_text(json.dumps(marker) + "\n")
    return HOSTED.main([
        "plan-reduce", "--contract", str(contract),
        "--store-root", str(tmp_path / f"store-{tag}"),
        "--family", "addresses", "--markers-dir", str(markers_dir),
        "--row-cap", "1000",
        "--output", str(tmp_path / f"plan-{tag}.json"),
    ])


def test_plan_reduce_refuses_a_plan_whose_publication_exceeds_the_cap(tmp_path, capsys):
    # 20 per-record packs -> 40 published per-record objects. With 1 partition and 2
    # manifests that is 43 objects = 176 operations on a resumed finalize, over a cap
    # of 100. Sized so the per-record TERM is what breaches it: without that term the
    # projection is 3 objects = 16 operations and passes, so dropping the term to 0
    # flips this test.
    with pytest.raises(SystemExit) as excinfo:
        _plan_reduce_addresses(tmp_path, cap=100, record_packs=20, tag="over")
    message = str(excinfo.value)
    assert "exceed the admitted cap max_remote_operations=100" in message
    assert "176 remote operations" in message
    assert "40 per-record" in message
    # And it refuses for the addresses family, which has no head term to hide behind.
    assert "0 head shards" in message


def test_plan_reduce_passes_and_reports_the_budget_when_the_publication_fits(tmp_path, capsys):
    # Same shape, a cap that admits it: the command succeeds and REPORTS the
    # projection, so the number is visible in the phase's own output.
    assert _plan_reduce_addresses(tmp_path, cap=1000, record_packs=20, tag="under") == 0
    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    budget = summary["publication_budget"]
    assert budget["within_cap"] is True
    assert budget["max_remote_operations"] == 1000
    assert budget["per_record_objects"] == 40
    assert budget["serving_objects"] == summary["partitions"]
    assert budget["projected_remote_operations"] == 176
    assert budget["first_attempt_remote_operations"] == 132
    assert "map markers" in budget["basis"]


def test_plan_reduce_fails_closed_on_a_marker_with_no_per_record_artifact(tmp_path):
    # The projection cannot be built from a marker set it would undercount, and this
    # is the same gap finalize aborts on -- moved to where it costs one plan phase.
    markers_dir = tmp_path / "markers-gap"
    markers_dir.mkdir()
    contract = _contract(tmp_path / "gap", max_remote_operations=100_000)
    request_sha256 = json.loads(contract.read_text())["request_sha256"]
    good = _address_marker(0, record_packs=2, request_sha256=request_sha256)
    gap = _address_marker(1, record_packs=2, request_sha256=request_sha256)
    del gap["address_records"]
    (markers_dir / "000.json").write_text(json.dumps(good) + "\n")
    (markers_dir / "001.json").write_text(json.dumps(gap) + "\n")
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main([
            "plan-reduce", "--contract", str(contract),
            "--store-root", str(tmp_path / "store-gap"),
            "--family", "addresses", "--markers-dir", str(markers_dir),
            "--row-cap", "1000", "--output", str(tmp_path / "plan-gap.json"),
        ])
    assert "address_records" in str(excinfo.value)
    assert "addresses-map-001" in str(excinfo.value)


def test_plan_reduce_fails_closed_when_the_contract_carries_no_cap(tmp_path):
    # The gate reads the cap from the CONTRACT. A contract with no usable cap must
    # abort rather than let the phase invent one.
    markers_dir = tmp_path / "markers-nocap"
    markers_dir.mkdir()
    contract = _contract(tmp_path / "nocap", max_remote_operations=1000)
    request_sha256 = json.loads(contract.read_text())["request_sha256"]
    (markers_dir / "000.json").write_text(
        json.dumps(
            _address_marker(
                0, record_packs=2, request_sha256=request_sha256
            )
        )
        + "\n"
    )
    payload = json.loads(contract.read_text())
    del payload["caps"]["max_remote_operations"]
    contract.write_text(json.dumps(payload) + "\n")
    with pytest.raises(SystemExit, match="max_remote_operations"):
        HOSTED.main([
            "plan-reduce", "--contract", str(contract),
            "--store-root", str(tmp_path / "store-nocap"),
            "--family", "addresses", "--markers-dir", str(markers_dir),
            "--row-cap", "1000", "--output", str(tmp_path / "plan-nocap.json"),
        ])


def test_plan_and_predict_require_the_admitted_reducer_cap(tmp_path):
    contract = _contract(tmp_path / "reducers", max_remote_operations=400_000)
    request_sha256 = json.loads(contract.read_text())["request_sha256"]
    payload = json.loads(contract.read_text())
    del payload["caps"]["max_reducers_per_family"]
    contract.write_text(json.dumps(payload) + "\n")

    markers_dir = tmp_path / "reducers-markers"
    markers_dir.mkdir()
    (markers_dir / "000.json").write_text(
        json.dumps(
            _address_marker(
                0, record_packs=2, request_sha256=request_sha256
            )
        )
        + "\n"
    )
    with pytest.raises(SystemExit, match="max_reducers_per_family"):
        HOSTED.main(
            [
                "plan-reduce",
                "--contract",
                str(contract),
                "--store-root",
                str(tmp_path / "reducers-store"),
                "--family",
                "addresses",
                "--markers-dir",
                str(markers_dir),
                "--row-cap",
                "1000",
                "--output",
                str(tmp_path / "reducers-plan.json"),
            ]
        )
    with pytest.raises(SystemExit, match="max_reducers_per_family"):
        HOSTED.main(
            [
                "predict-reduce",
                "--contract",
                str(contract),
                "--family",
                "addresses",
                "--inventory",
                str(ADDRESS_INVENTORY),
            ]
        )


# --------------------------------------------------------------------------- #
# 3. The planet projection, against the admitted cap
# --------------------------------------------------------------------------- #
def _contract(tmp_path: Path, *, max_remote_operations: int) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
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
# in the CAPS comment are executable rather than merely asserted in prose. Two
# figures per family because a resumed finalize costs 4N+4 and a first attempt 3N+3;
# the BUDGETED one is the retry.
#
# The per-record term is the STRUCTURAL bound -- every map task occupying all 256
# shuffle buckets -- because the dry run has no markers to measure. (Measured on
# release 2026-06-17.0, the four planet Places tasks inside source object 0 occupy
# 107/149/160/109 buckets, so the real Places figures are ~132,900 first attempt /
# ~177,200 retry.)
PLANET_PROJECTED_OPERATIONS = {"places": 266_290, "addresses": 263_073}
PLANET_FIRST_ATTEMPT_OPERATIONS = {"places": 199_734, "addresses": 197_321}
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
    assert budget["first_attempt_remote_operations"] == PLANET_FIRST_ATTEMPT_OPERATIONS[family]
    # The FIRST attempt already exceeded the old cap; the retry is what the new cap
    # has to cover, and it does.
    assert budget["first_attempt_remote_operations"] > OLD_REMOTE_OPERATION_CAP
    assert budget["within_cap"] is True
    assert budget["max_remote_operations"] == cap
    # The projection is arithmetic on committed counts, so state the counts too.
    inventory_json = json.loads(inventory.read_text())
    tasks = HOSTED._inventory_task_count(inventory_json)
    assert tasks == (89 if family == "places" else 127)
    assert budget["per_record_objects"] == 2 * tasks * 256
    assert budget["serving_objects"] == out["predicted_partitions"]
    assert budget["head_shard_objects"] == (4096 if family == "places" else 0)
    assert budget["head_manifest_objects"] == (1 if family == "places" else 0)


def test_the_admitted_cap_clears_the_retry_inclusive_structural_ceiling():
    # The cap is sized off the ceiling, not the measurement: the inventory plan gate
    # admits at most 128 map tasks per family, so the per-record term can never
    # exceed 128 x 256 x 2 whatever a re-inventoried release looks like. This is the
    # arithmetic in the CAPS comment, executable -- and it is the RETRY ceiling,
    # because a resumed finalize is the case the running counter has to survive.
    projections = [
        HOSTED._finalize_publication_projection(
            family,
            partitions=partitions,
            per_record_objects=HOSTED.PER_RECORD_OBJECTS_PER_PACK * 128 * 256,
            basis="structural ceiling",
        )
        # 16,888 is the committed Places partition plan; 725 the modelled address
        # partition count.
        for family, partitions in (("places", 16_888), ("addresses", 725))
    ]
    ceiling = max(item["projected_remote_operations"] for item in projections)
    first_attempt_ceiling = max(
        item["first_attempt_remote_operations"] for item in projections
    )
    assert (first_attempt_ceiling, ceiling) == (259_658, 346_182)
    assert CONTROL.CAPS["max_remote_operations"] >= ceiling
    # The margin is deliberately modest, because the gate -- not the size of this
    # number -- is what makes an outgrown cap cheap to discover. And it must NOT be
    # sized off the first attempt: 300,000 clears that and a resumed planet finalize
    # at the ceiling would still abort inside Budget.charge.
    assert CONTROL.CAPS["max_remote_operations"] < 2 * ceiling
    assert 300_000 < ceiling


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
    # The offline plan generator must plan under the same cap the build enforces.
    generator = _load("publication_budget_generator", "scripts/generate_places_partition_plan.py")
    assert generator.DEFAULT_CAPS["distinct_tokens"] == encoder
    # The committed plan keeps recording the caps it was GENERATED under (400,000);
    # what has to hold is ADMISSIBILITY, which its headroom threshold delivers --
    # 0.5 x 400,000 = 200,000 tokens per unsplit leaf, under the encoder's 250,000.
    # tests/test_generate_places_partition_plan.py owns that property.
    committed = json.loads((ROOT / "scripts/places_partition_plan_v1.json").read_text())
    caps = committed["partition_contract"]["caps"]
    assert caps["distinct_tokens"] * committed["headroom"]["threshold"] <= encoder
    # The worst real cell measured to date still fits, so this changes no plan today.
    assert 201_568 < encoder


def test_a_contract_override_above_the_encoder_limit_fails_closed(tmp_path):
    """The bound is checked where the value TRAVELS, not where the constant is written.

    `HOSTED_LIMITS["places"]["partition_distinct_tokens"]` IS the encoder constant, so
    comparing those two can never fire. The value that actually reaches
    `adaptive_genesis_plan` comes from `contract["limits"]`, which is a documented
    per-run override -- so a rehearsal or hand-built contract carrying 400,000 would
    otherwise still admit a partition the routed encode cannot write.
    """
    contract_path = _contract(tmp_path / "override", max_remote_operations=100_000)
    contract = json.loads(contract_path.read_text())
    assert contract["limits"]["places"]["partition_distinct_tokens"] == 250_000
    # The good contract passes.
    assert HOSTED._limits_for(contract, "places").partition_distinct_tokens == 250_000
    contract["limits"]["places"]["partition_distinct_tokens"] = 400_000
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._limits_for(contract, "places")
    message = str(excinfo.value)
    assert "400000" in message and "MAX_INDEX_ENTRIES" in message
    # One below the cap is admitted, one above is not: the boundary is exact.
    contract["limits"]["places"]["partition_distinct_tokens"] = 250_001
    with pytest.raises(SystemExit):
        HOSTED._limits_for(contract, "places")
    contract["limits"]["places"]["partition_distinct_tokens"] = 250_000
    assert HOSTED._limits_for(contract, "places").partition_distinct_tokens == 250_000


def test_the_routed_encoder_still_enforces_the_limit_it_is_mirrored_from():
    # The mirror is a fail-fast convenience; the Rust side is the enforcing one, and
    # this is what says so. If the routed encode ever stops checking, lowering the
    # Python cap would be protecting nothing.
    source = ENCODER_SOURCE.read_text()
    assert "MAX_INDEX_ENTRIES" in source
    assert "bail!" in source


def test_no_phase_falls_back_to_the_superseded_remote_operation_cap():
    # The gate hard-aborts on a missing cap while two Budget constructions silently
    # defaulted to 100,000 -- the very figure this change declares insufficient for a
    # planet publication. Same literal-drifting-from-its-limit class the token cap
    # had.
    source = (ROOT / "scripts/construction_v1_hosted.py").read_text()
    assert 'max_remote_operations", 100_000' not in source
    assert "100_000" not in source.split("HOSTED_LIMITS")[-1].split("def canonical")[0]
    # admit-task's --contract is optional, so it defaults -- to the ADMITTED cap.
    assert HOSTED._admitted_remote_operation_cap({}) == CONTROL.CAPS["max_remote_operations"]
    assert HOSTED._admitted_remote_operation_cap(
        {"caps": {"max_remote_operations": 5}}
    ) == 5
    # Finalize is always handed a contract, so a missing cap there is a broken
    # contract, not a default to invent.
    for contract in ({}, {"caps": {}}, {"caps": {"max_remote_operations": 0}},
                     {"caps": {"max_remote_operations": "400000"}}):
        with pytest.raises(SystemExit, match="max_remote_operations"):
            HOSTED._required_remote_operation_cap(contract)
