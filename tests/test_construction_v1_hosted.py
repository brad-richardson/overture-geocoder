"""End-to-end, no-network proof of the construction-v1 execute command sequence.

This composes the EXACT construction_v1_hosted.py subcommands the hosted
workflow runs in execute mode -- derive-contract -> admit-task -> run-map ->
plan-reduce -> run-reduce -> run-head (places) -> finalize -- against a tmpdir
store and a tmpdir "remote" instead of R2. If any phase produced or consumed a
file the previous phase never wrote, this fails locally, which is what prevents
a planet attempt from burning ~500 runner-minutes on a file-not-found.

It also proves the runner-minute ledger fails closed before the next phase when
prior + projected minutes exceed the confirmation cap.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


pytest.importorskip("pyarrow")

ROOT = Path(__file__).parents[1]


def _load(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


ADDR_SPIKE = _load("hosted_test_address_spike", "tests/test_address_construction_spike.py")
PLACES_TEST = _load("hosted_test_places", "tests/test_places_construction_v1.py")
HOSTED = _load("hosted_test_adapter", "scripts/construction_v1_hosted.py")


@pytest.fixture(scope="module")
def binaries() -> dict[str, Path]:
    subprocess.run(
        ["cargo", "build", "-p", "geocoder-construction", "--bins"],
        cwd=ROOT / "crates",
        check=True,
    )
    target = ROOT / "crates/target/debug"
    return {name: target / name for name in (
        "address-transform-v1", "address-proof-directory",
        "address-serving-encode-v1", "address-serving-verify-v1",
        "places-transform-v1", "places-proof-directory",
        "places-serving-encode-v1", "places-serving-verify-v1",
    )}


def _request(tmp_path: Path) -> Path:
    request = {
        "schema": "overture-construction-v1-request-v1",
        "release": "2026-06-17.0",
        "families": {"addresses": {}, "places": {}},
        "versions": {"duckdb": "1.5.1", "pyarrow": "25.0.0", "numpy": "2.3.5",
                     "python": "3.12.12", "rustc": "test"},
        "caps": {"max_remote_operations": 100000, "max_remote_write_bytes": 1_000_000_000_000},
        "namespaces": {
            "immutable_root": "construction-v1/deadbeef",
            "slice": "construction-v1/deadbeef/slice/slice-x/",
            "markers": "construction-v1/deadbeef/markers/",
        },
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(request) + "\n")
    return path


def _run(*argv: str) -> None:
    assert HOSTED.main([str(a) for a in argv]) == 0


def _derive(tmp_path: Path) -> tuple[Path, Path]:
    contract = tmp_path / "contract.json"
    runtime = tmp_path / "runtime.json"
    _run("derive-contract", "--request", _request(tmp_path),
         "--output", contract, "--runtime", runtime,
         "--allow-unpinned-duckdb", "--map-input-rows-cap", "100")
    assert json.loads(runtime.read_text())["strict_versions"] is False
    return contract, runtime


def _admit_completed(store: Path, family: str, phase: str, **kw) -> bool:
    out = store.parent / f"admit-{family}-{phase}-{kw.get('task_id', kw.get('index', ''))}.json"
    argv = ["admit-task", "--store-root", store, "--family", family, "--phase", phase, "--output", out]
    for key, value in kw.items():
        argv += [f"--{key.replace('_', '-')}", value]
    _run(*argv)
    return json.loads(out.read_text())["completed"]


def test_execute_sequence_addresses_end_to_end_no_network(tmp_path, binaries):
    contract, _ = _derive(tmp_path)
    store = tmp_path / "store"
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir()

    # One map task from a tiny projected fixture.
    rows = [
        {"id": str(uuid.UUID(int=2)), "street": "Main Street", "number": "10", "unit": "",
         "postcode": "02180", "postal_city": "Stoneham", "address_levels": ["MA", "Stoneham"],
         "country": "US", "point": [-71.0, 42.0], "source_object_index": 0,
         "source_row_group": 0, "source_row_index": 1},
        {"id": str(uuid.UUID(int=1)), "street": "Main Street", "number": "10", "unit": "",
         "postcode": "02180", "postal_city": "Stoneham", "address_levels": ["MA", "Stoneham"],
         "country": "US", "point": [-71.0, 42.0], "source_object_index": 0,
         "source_row_group": 0, "source_row_index": 0},
        {"id": str(uuid.UUID(int=3)), "street": "Main Street", "number": "99", "unit": "",
         "postcode": "02180", "postal_city": "Stoneham", "address_levels": ["MA", "Stoneham"],
         "country": "US", "point": [-71.0, 42.0], "source_object_index": 0,
         "source_row_group": 0, "source_row_index": 2},
    ]
    projected = tmp_path / "projected.parquet"
    ADDR_SPIKE.write_fixture(projected, rows)
    source_limits = tmp_path / "source-limits.json"
    source_limits.write_text(json.dumps({"objects": [{"records": 3, "row_groups": 1}]}) + "\n")

    assert _admit_completed(store, "addresses", "map", task_id="addresses-map-000") is False
    _run("run-map", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--task-id", "addresses-map-000", "--input", projected, "--source-limits", source_limits,
         "--transform-binary", binaries["address-transform-v1"],
         "--proof-binary", binaries["address-proof-directory"],
         "--scratch-dir", tmp_path / "map-scratch",
         "--marker-out", markers_dir / "000.json")
    # Idempotent: a re-admit now reports completed and re-running the map admits
    # the existing marker.
    assert _admit_completed(store, "addresses", "map", task_id="addresses-map-000") is True

    # P1-4: a fresh RESUME dispatch has no local store, but the durable create-only
    # marker in the remote store lets admit-task skip a genuinely completed task.
    remote_markers = tmp_path / "remote-markers"
    marker_key = HOSTED.ADDRESS.marker_key("addresses-map-000")
    (remote_markers / marker_key).parent.mkdir(parents=True, exist_ok=True)
    (remote_markers / marker_key).write_bytes((store / marker_key).read_bytes())
    fresh_store = tmp_path / "fresh-store"  # empty: models a resume with no artifacts
    contract_path = contract
    out = tmp_path / "resume-admit-000.json"
    assert HOSTED.main(["admit-task", "--store-root", str(fresh_store),
                        "--family", "addresses", "--phase", "map",
                        "--task-id", "addresses-map-000", "--contract", str(contract_path),
                        "--remote-root", str(remote_markers), "--output", str(out)]) == 0
    assert json.loads(out.read_text())["completed"] is True  # completed subset skipped
    # An unpublished sibling task on the same resume is NOT skipped: it must run.
    out2 = tmp_path / "resume-admit-001.json"
    assert HOSTED.main(["admit-task", "--store-root", str(fresh_store),
                        "--family", "addresses", "--phase", "map",
                        "--task-id", "addresses-map-001", "--contract", str(contract_path),
                        "--remote-root", str(remote_markers), "--output", str(out2)]) == 0
    assert json.loads(out2.read_text())["completed"] is False

    plan = tmp_path / "plan.json"
    _run("plan-reduce", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--markers-dir", markers_dir, "--row-cap", "2", "--output", plan,
         "--matrix-out", tmp_path / "reduce-matrix.json")
    partitions = json.loads(plan.read_text())["partitions"]
    assert len(partitions) == 2

    reductions_dir = tmp_path / "reductions"
    reductions_dir.mkdir()
    for index in range(len(partitions)):
        _run("run-reduce", "--contract", contract, "--store-root", store, "--family", "addresses",
             "--plan", plan, "--markers-dir", markers_dir, "--partition-index", index,
             "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-scratch-{index}",
             "--output", reductions_dir / f"{index:04d}.json")
        assert _admit_completed(store, "addresses", "reduce", index=index) is True

    head = tmp_path / "head.json"
    _run("run-head", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--markers-dir", markers_dir, "--output", head)
    assert json.loads(head.read_text())["head"] is None

    remote = tmp_path / "remote"
    final = tmp_path / "final.json"
    _run("finalize", "--contract", contract, "--store-root", store, "--family", "addresses",
         "--plan", plan, "--reductions-dir", reductions_dir, "--markers-dir", markers_dir,
         "--remote-root", remote,
         "--work-root", tmp_path / "final-work", "--output", final)
    result = json.loads(final.read_text())
    assert result["reconciles"] is True
    assert result["marker_written_last"] is True

    # The per-address records packs must be PUBLISHED, not merely produced -- the
    # same reasoning as the Places positions packs, through the SAME seam
    # (PER_RECORD_ARTIFACTS): the store travels as a GitHub artifact with a 7-day
    # retention, so anything outside the durable slice is gone a week after a
    # planet run, and then an address reverse index costs the full address map
    # re-run this artifact exists to avoid.
    manifest = json.loads((tmp_path / "final-work/family-manifest.json").read_text())
    published_records = manifest["positions"]
    assert published_records["schema"] == HOSTED.ADDRESS.ADDRESS_RECORDS_SCHEMA
    expected_records = sum(
        json.loads(path.read_text())["address_records"]["records"]
        for path in sorted(markers_dir.glob("*.json"))
    )
    assert published_records["records"] == expected_records == len(rows)
    assert result["positions_objects"] == len(published_records["objects"]) > 0
    assert result["positions_records"] == expected_records
    # Addresses publish under records/, not positions/: the tree names the
    # artifact it holds instead of calling address records "positions".
    slice_root = json.loads(contract.read_text())["namespaces"]["slice"].rstrip("/")
    for item in published_records["objects"]:
        path = remote / f"{slice_root}/families/addresses/records/{Path(item['key']).name}"
        assert path.is_file()
        assert path.stat().st_size == item["bytes"]
    # The family manifest + slice manifest + one serving object per partition +
    # the records set, all present in the "remote" and byte-verified exactly once.
    assert result["objects"] == 2 + len(partitions) + len(published_records["objects"])
    assert json.loads((tmp_path / "final-work/slice-manifest.json").read_text())[
        "positions_object_count"
    ] == len(published_records["objects"])
    # The completion marker is outside the verified family prefix and written
    # last: publish_exact_set HEAD-verified it.
    marker_key = result["marker_key"]
    assert (remote / marker_key).is_file()

    # Publishing must not be skippable by omission. Before the records artifact
    # existed, addresses were EXEMPT from this gate; they no longer are, so a
    # workflow that forgets --markers-dir fails instead of shipping an address
    # slice with no per-address records.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "addresses", "--plan", str(plan),
                     "--reductions-dir", str(reductions_dir),
                     "--remote-root", str(tmp_path / "remote-b"),
                     "--work-root", str(tmp_path / "final-work-b"),
                     "--output", str(tmp_path / "final-b.json")])
    assert "--markers-dir is required" in str(excinfo.value)
    assert HOSTED.ADDRESS.ADDRESS_RECORDS_SCHEMA in str(excinfo.value)

    # A marker that predates the artifact is the same gap one level in, and the
    # error must say how to remediate it: markers are write-once.
    stale_markers = tmp_path / "stale-markers"
    stale_markers.mkdir()
    for path in sorted(markers_dir.glob("*.json")):
        stale = json.loads(path.read_text())
        stale.pop("address_records")
        (stale_markers / path.name).write_text(json.dumps(stale))
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "addresses", "--plan", str(plan),
                     "--reductions-dir", str(reductions_dir),
                     "--markers-dir", str(stale_markers),
                     "--remote-root", str(tmp_path / "remote-c"),
                     "--work-root", str(tmp_path / "final-work-c"),
                     "--output", str(tmp_path / "final-c.json")])
    assert "carries no address_records artifact" in str(excinfo.value)
    assert "re-run its map task" in str(excinfo.value)


def test_the_per_record_publication_seam_covers_both_families():
    """One seam, not two parallel mechanisms.

    The publication path, the fail-closed gate, the manifest listing and the
    result keys are all family-generic; only the marker key, the published
    sub-prefix and the schema string come from the table. A family added to
    FAMILIES without an entry here would silently publish nothing.
    """
    assert set(HOSTED.PER_RECORD_ARTIFACTS) == set(HOSTED.FAMILIES)
    assert HOSTED.PER_RECORD_ARTIFACTS["places"]["marker_key"] == "positions"
    assert HOSTED.PER_RECORD_ARTIFACTS["addresses"]["marker_key"] == "address_records"
    # Distinct sub-prefixes, so the published tree never calls address records
    # "positions".
    prefixes = {spec["prefix"] for spec in HOSTED.PER_RECORD_ARTIFACTS.values()}
    assert prefixes == {"positions", "records"}
    assert HOSTED.PER_RECORD_ARTIFACTS["places"]["schema"] == HOSTED.PLACES.POSITIONS_SCHEMA
    assert HOSTED.PER_RECORD_ARTIFACTS["addresses"]["schema"] == (
        HOSTED.ADDRESS.ADDRESS_RECORDS_SCHEMA
    )
    # The accessor reads the family's own key and nothing else, so a places marker
    # cannot satisfy the address gate or vice versa.
    places_marker = {"positions": {"records": 1, "packs": []}}
    address_marker = {"address_records": {"records": 2, "packs": []}}
    assert HOSTED._per_record_artifact(places_marker, "places")["records"] == 1
    assert HOSTED._per_record_artifact(places_marker, "addresses") is None
    assert HOSTED._per_record_artifact(address_marker, "addresses")["records"] == 2
    assert HOSTED._per_record_artifact(address_marker, "places") is None
    # Objects are collected per family from the family's own key.
    assert HOSTED._positions_objects([address_marker], "places") == []


def test_high_noncontiguous_source_row_groups_survive_correct_limits_and_fail_closed_on_wrong(
    tmp_path, binaries
):
    """Close the silent wrong-bytes class through the REAL transform: projected
    rows carrying a high, non-contiguous source_row_group (255) survive a
    correctly report-derived source-limits bound, and a wrong row_groups:1 bound
    is caught (fail-closed) instead of silently dropping every row at exit 0."""
    contract, _ = _derive(tmp_path)
    # Planet-shaped locators: original row group 255, non-contiguous, three rows.
    rows = [
        {"id": str(uuid.UUID(int=i)), "street": "Main Street", "number": str(10 + i),
         "unit": "", "postcode": "02180", "postal_city": "Stoneham",
         "address_levels": ["MA", "Stoneham"], "country": "US", "point": [-71.0, 42.0],
         "source_object_index": 0, "source_row_group": 255, "source_row_index": i}
        for i in range(3)
    ]
    projected = tmp_path / "projected-255.parquet"
    ADDR_SPIKE.write_fixture(projected, rows)

    def run_map(source_limits: Path, store: Path, markers: Path):
        markers.mkdir()
        return HOSTED.main([
            "run-map", "--contract", str(contract), "--store-root", str(store),
            "--family", "addresses", "--task-id", "addresses-map-000",
            "--input", str(projected), "--source-limits", str(source_limits),
            "--transform-binary", str(binaries["address-transform-v1"]),
            "--proof-binary", str(binaries["address-proof-directory"]),
            "--scratch-dir", str(tmp_path / f"scratch-{store.name}"),
            "--marker-out", str(markers / "000.json"),
        ])

    # A row_groups:1 bound rejects every locator (255 >= 1) -> fail closed.
    wrong = tmp_path / "wrong-limits.json"
    wrong.write_text(json.dumps({"objects": [{"records": 3, "row_groups": 1}]}))
    with pytest.raises(Exception) as excinfo:
        run_map(wrong, tmp_path / "store-wrong", tmp_path / "markers-wrong")
    assert "invalid_source_locator" in str(excinfo.value)

    # A correct per-object bound (row_groups > 255) admits every row.
    correct = tmp_path / "correct-limits.json"
    correct.write_text(json.dumps({"objects": [{"records": 3, "row_groups": 256}]}))
    marker_out = tmp_path / "markers-ok" / "000.json"
    assert run_map(correct, tmp_path / "store-ok", tmp_path / "markers-ok") == 0
    marker = json.loads(marker_out.read_text())
    assert marker["binding"]["records"] == 3  # every row survived, none dropped


def _address_map_store(
    tmp_path: Path, contract: Path, binaries, tag: str, staging: Path | None = None
) -> tuple[Path, Path]:
    """Build a store + markers dir with one address map task from a tiny fixture.

    With ``staging`` the map output is mirrored into a filesystem R2 staging root,
    which is the hosted shape: the artifacts between jobs carry markers only.
    """
    store = tmp_path / f"store-{tag}"
    markers_dir = tmp_path / f"markers-{tag}"
    markers_dir.mkdir()
    rows = [
        {"id": str(uuid.UUID(int=i)), "street": "Main Street", "number": str(10 + i),
         "unit": "", "postcode": "02180", "postal_city": "Stoneham",
         "address_levels": ["MA", "Stoneham"], "country": "US", "point": [-71.0, 42.0],
         "source_object_index": 0, "source_row_group": 0, "source_row_index": i}
        for i in range(6)
    ]
    projected = tmp_path / f"projected-{tag}.parquet"
    ADDR_SPIKE.write_fixture(projected, rows)
    source_limits = tmp_path / f"source-limits-{tag}.json"
    source_limits.write_text(json.dumps({"objects": [{"records": len(rows), "row_groups": 1}]}) + "\n")
    _run("run-map", "--contract", contract, "--store-root", store, "--family", "addresses",
         *(("--staging-root", staging) if staging else ()),
         "--task-id", "addresses-map-000", "--input", projected, "--source-limits", source_limits,
         "--transform-binary", binaries["address-transform-v1"],
         "--proof-binary", binaries["address-proof-directory"],
         "--scratch-dir", tmp_path / f"map-scratch-{tag}",
         "--marker-out", markers_dir / "000.json",
         "--output", tmp_path / f"map-{tag}.json")
    return store, markers_dir


def test_the_staging_transport_carries_the_store_and_outputs_are_unchanged(
    tmp_path, binaries
):
    """The planet blocker, closed: no phase needs the previous phase's store dir.

    Every phase after map runs with its OWN EMPTY local store and fetches by key
    from the run-scoped staging prefix, exactly as the hosted jobs now do (the
    inter-job artifacts carry markers and JSON only). The serving artifacts are
    then compared to a legacy shared-store run of the same fixture: this is a
    TRANSPORT change, so a single byte of difference in the published objects would
    mean it is not.
    """
    contract, _ = _derive(tmp_path)
    request_sha256 = json.loads(contract.read_text())["request_sha256"]
    staging = tmp_path / "staging"

    def plan_reduce_finalize(store_of, markers_dir, tag, staged):
        plan = tmp_path / f"plan-{tag}.json"
        argv = ["plan-reduce", "--contract", contract, "--store-root", store_of("plan"),
                "--family", "addresses", "--markers-dir", markers_dir, "--row-cap", "2",
                "--output", plan]
        _run(*argv, *staged)
        reductions = tmp_path / f"reductions-{tag}"
        reductions.mkdir()
        partitions = json.loads(plan.read_text())["partitions"]
        for index in range(len(partitions)):
            _run("run-reduce", "--contract", contract, "--store-root", store_of("reduce"),
                 *staged, "--family", "addresses", "--plan", plan,
                 "--markers-dir", markers_dir, "--partition-index", index,
                 "--proof-binary", binaries["address-proof-directory"],
                 "--encoder-binary", binaries["address-serving-encode-v1"],
                 "--verifier-binary", binaries["address-serving-verify-v1"],
                 "--scratch-dir", tmp_path / f"reduce-{tag}-{index}",
                 "--output", reductions / f"{index:04d}.json")
        final = tmp_path / f"final-{tag}.json"
        _run("finalize", "--contract", contract, "--store-root", store_of("finalize"),
             *staged, "--family", "addresses", "--plan", plan,
             "--reductions-dir", reductions, "--markers-dir", markers_dir,
             "--remote-root", tmp_path / f"remote-{tag}",
             "--work-root", tmp_path / f"final-work-{tag}", "--output", final)
        return plan, reductions, json.loads(final.read_text())

    # Legacy: one store shared by every phase, as a merged artifact provided.
    shared, shared_markers = _address_map_store(tmp_path, contract, binaries, "legacy")
    _, legacy_reductions, legacy_final = plan_reduce_finalize(
        lambda _phase: shared, shared_markers, "legacy", ()
    )

    # Staged: map publishes to staging; every later phase starts from nothing.
    staged_store, staged_markers = _address_map_store(
        tmp_path, contract, binaries, "staged", staging=staging
    )
    map_summary = json.loads((tmp_path / "map-staged.json").read_text())
    assert map_summary["staged_objects_published"] > 0
    assert map_summary["staging_prefix"] == HOSTED.STAGING.staging_prefix(
        request_sha256, "addresses"
    )
    # The prefix the objects actually landed under is the one r2-cleanup.yml guards.
    assert (staging / "staging" / "global-v2" / request_sha256).is_dir()

    _, staged_reductions, staged_final = plan_reduce_finalize(
        lambda phase: tmp_path / f"store-staged-{phase}",
        staged_markers,
        "staged",
        ("--staging-root", str(staging)),
    )
    # Proof the consumers really read from staging rather than a store they
    # inherited: finalize's local cache started EMPTY, so every published object
    # had to be hydrated and digest-verified.
    assert staged_final["staged_objects_hydrated"] > 0
    assert staged_final["staged_bytes_hydrated"] > 0
    # ...and BOUNDED while doing it. Finalize used to build its exact set out of
    # `store.path(...)` calls, so every published object was hydrated onto this
    # runner before the first upload and NOTHING was released anywhere in the
    # phase -- 13-18 GB at planet scale, on the last job of a multi-hour run. It
    # now hydrates, verifies, uploads and evicts one object at a time, so peak
    # below total is how you can tell (eager hydration makes them equal).
    assert staged_final["staged_objects_released"] > 0
    assert (
        staged_final["staged_peak_resident_bytes"]
        < staged_final["staged_bytes_hydrated"]
    )

    # Transport only. Identical content-addressed keys means identical bytes.
    names = sorted(path.name for path in legacy_reductions.glob("*.json"))
    assert names == sorted(path.name for path in staged_reductions.glob("*.json"))
    for name in names:
        legacy = json.loads((legacy_reductions / name).read_text())
        staged = json.loads((staged_reductions / name).read_text())
        assert legacy["artifact"] == staged["artifact"], name
        assert legacy["partition"] == staged["partition"], name
        assert legacy["selected_binding"] == staged["selected_binding"], name
    assert staged_final["reconciles"] is True
    assert staged_final["objects"] == legacy_final["objects"]
    assert staged_final["positions_records"] == legacy_final["positions_records"]
    legacy_tree = sorted(
        (path.relative_to(tmp_path / "remote-legacy").as_posix(), path.stat().st_size)
        for path in (tmp_path / "remote-legacy").rglob("*") if path.is_file()
    )
    staged_tree = sorted(
        (path.relative_to(tmp_path / "remote-staged").as_posix(), path.stat().st_size)
        for path in (tmp_path / "remote-staged").rglob("*") if path.is_file()
    )
    assert legacy_tree == staged_tree

    # And the durable marker makes resume work with NO local store at all, which
    # is what the artifact-carried store used to provide.
    fresh = tmp_path / "store-resume"
    resumed_marker = tmp_path / "resumed-marker.json"
    assert _admit_completed(
        fresh, "addresses", "map", task_id="addresses-map-000",
        contract=str(contract), staging_root=str(staging),
        marker_out=str(resumed_marker),
    ) is True
    # A SKIPPED task must still contribute its marker to the fan-in: the artifacts
    # now carry markers only, so a skip that emitted nothing would silently drop
    # this task from plan-reduce.
    original_marker = json.loads((staged_markers / "000.json").read_text())
    # `admitted_existing` is run-map's report about ITS invocation, not part of the
    # stored marker; everything the plan phase reads is identical.
    original_marker.pop("admitted_existing")
    assert json.loads(resumed_marker.read_text()) == original_marker
    # A task that is NOT complete writes no marker and does not fail.
    absent = tmp_path / "absent-marker.json"
    assert _admit_completed(
        tmp_path / "store-resume-b", "addresses", "map", task_id="addresses-map-999",
        contract=str(contract), staging_root=str(staging), marker_out=str(absent),
    ) is False
    assert not absent.exists()


def test_a_missing_or_tampered_staged_object_aborts_the_consumer(tmp_path, binaries):
    """No fallback path exists any more, so a gap must abort rather than degrade.

    Before this transport a missing store object meant a missing artifact and the
    job simply failed. Now the failure is per-OBJECT, and a silent skip would
    publish a slice that is short by whole partitions while every binding check
    still passed on what it did read.
    """
    contract, _ = _derive(tmp_path)
    staging = tmp_path / "staging"
    _store_dir, markers_dir = _address_map_store(
        tmp_path, contract, binaries, "gap", staging=staging
    )
    marker = json.loads((markers_dir / "000.json").read_text())
    prefix = HOSTED.STAGING.staging_prefix(
        json.loads(contract.read_text())["request_sha256"], "addresses"
    )
    pack_key = marker["packs"][0]["object"]["key"]
    staged_pack = staging / prefix / pack_key

    # The address plan phase reads marker JSON only; the REDUCER is what reads the
    # map packs by key, so it is the consumer under test here.
    plan_path = tmp_path / "plan-gap.json"
    _run("plan-reduce", "--contract", contract, "--store-root", tmp_path / "store-plan-gap",
         "--staging-root", staging, "--family", "addresses",
         "--markers-dir", markers_dir, "--row-cap", "2", "--output", plan_path)

    def reduce(tag):
        return HOSTED.main([
            "run-reduce", "--contract", str(contract),
            "--store-root", str(tmp_path / f"store-{tag}"),
            "--staging-root", str(staging), "--family", "addresses",
            "--plan", str(plan_path), "--markers-dir", str(markers_dir),
            "--partition-index", "0",
            "--proof-binary", str(binaries["address-proof-directory"]),
            "--encoder-binary", str(binaries["address-serving-encode-v1"]),
            "--verifier-binary", str(binaries["address-serving-verify-v1"]),
            "--scratch-dir", str(tmp_path / f"reduce-scratch-{tag}"),
            "--output", str(tmp_path / f"reduction-{tag}.json"),
        ])

    # Absent: the consumer aborts naming the key it could not fetch.
    original = staged_pack.read_bytes()
    staged_pack.unlink()
    with pytest.raises(FileNotFoundError) as excinfo:
        reduce("absent")
    assert "staged object is absent" in str(excinfo.value)

    # Short: the digest in the key is the only thing that catches a truncation.
    staged_pack.write_bytes(original[:-16])
    with pytest.raises(ValueError):
        reduce("short")

    # Same length, different bytes: size checks cannot see this at all.
    staged_pack.write_bytes(b"\x00" * len(original))
    with pytest.raises(ValueError):
        reduce("swapped")

    # Restored: the same phase now succeeds, so the aborts above were about the
    # object and not about the wiring.
    staged_pack.write_bytes(original)
    assert reduce("restored") == 0


def test_finalize_refuses_to_publish_bytes_that_are_not_the_bytes(tmp_path, binaries):
    """A right key over wrong bytes was publishable, and reported reconciles: true.

    `verify_whole_slice_once` derives what it expects from the very files it
    publishes, and the reconciliation compares BINDINGS out of the reduction JSON,
    which substituted file content does not touch. So every check passed on bytes
    nothing had ever produced. Finalize now compares each file to the digest in its
    content-addressed key AND to the identity its producing phase recorded.
    """
    contract, _ = _derive(tmp_path)
    staging = tmp_path / "staging"
    _store_dir, markers_dir = _address_map_store(
        tmp_path, contract, binaries, "tamper", staging=staging
    )
    plan_path = tmp_path / "plan-tamper.json"
    _run("plan-reduce", "--contract", contract, "--store-root", tmp_path / "store-plan-t",
         "--staging-root", staging, "--family", "addresses",
         "--markers-dir", markers_dir, "--row-cap", "2", "--output", plan_path)
    reductions = tmp_path / "reductions-tamper"
    reductions.mkdir()
    partitions = json.loads(plan_path.read_text())["partitions"]
    reduce_store = tmp_path / "store-reduce-t"
    for index in range(len(partitions)):
        _run("run-reduce", "--contract", contract, "--store-root", reduce_store,
             "--staging-root", staging, "--family", "addresses", "--plan", plan_path,
             "--markers-dir", markers_dir, "--partition-index", index,
             "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-t-{index}",
             "--output", reductions / f"{index:04d}.json")

    # Pre-plant wrong bytes at a right key in the finalize runner's local cache, so
    # the store already "has" the object and nothing hydrates it from staging.
    finalize_store = tmp_path / "store-finalize-t"
    victim = json.loads((reductions / "0000.json").read_text())["artifact"]
    planted = finalize_store / victim["key"]
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"\x00" * victim["bytes"])  # same length, wrong bytes

    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract),
                     "--store-root", str(finalize_store), "--staging-root", str(staging),
                     "--family", "addresses", "--plan", str(plan_path),
                     "--reductions-dir", str(reductions), "--markers-dir", str(markers_dir),
                     "--remote-root", str(tmp_path / "remote-tamper"),
                     "--work-root", str(tmp_path / "final-work-tamper"),
                     "--output", str(tmp_path / "final-tamper.json")])
    assert "content-addressed key declares" in str(excinfo.value)
    # Nothing was published: the check runs BEFORE publish_exact_set.
    assert not (tmp_path / "remote-tamper").exists()
    # And the OFFENDING BYTES SURVIVE. Finalize now evicts each object once it has
    # been read, but only on the success path: an early draft released in a
    # `finally`, which deleted the planted file as the run aborted and destroyed the
    # only evidence of what was published-and-refused. A failing identity gate must
    # leave its input on disk for a human -- the run is aborting anyway, so there is
    # nothing to reclaim.
    assert planted.is_file(), "the object that failed its identity gate was evicted"
    assert planted.read_bytes() == b"\x00" * victim["bytes"]

    # Remove the planted file and the same finalize succeeds from staging.
    planted.unlink()
    _run("finalize", "--contract", contract, "--store-root", finalize_store,
         "--staging-root", staging, "--family", "addresses", "--plan", plan_path,
         "--reductions-dir", reductions, "--markers-dir", markers_dir,
         "--remote-root", tmp_path / "remote-clean",
         "--work-root", tmp_path / "final-work-clean",
         "--output", tmp_path / "final-clean.json")
    assert json.loads((tmp_path / "final-clean.json").read_text())["reconciles"] is True


def test_the_serving_object_set_is_family_correct_and_fails_closed():
    """A Places reduction records `routed_object`, not `artifact`.

    `_artifact_keys` collected `reduction.get("artifact")` and skipped a falsy one,
    so a Places finalize published head shards, positions packs and two manifests
    and DROPPED every routed `.plrv` serving payload. Nothing caught it: the
    reconciliation compares bindings out of the reduction JSON and never looks at
    the published object set.
    """
    assert set(HOSTED.REDUCTION_SERVING_OBJECTS) == set(HOSTED.FAMILIES)
    assert HOSTED.REDUCTION_SERVING_OBJECTS["places"] == "routed_object"
    assert HOSTED.REDUCTION_SERVING_OBJECTS["addresses"] == "artifact"

    def identity(key: str) -> dict:
        return {"key": key, "sha256": "a" * 64, "bytes": 10}

    places = [{"partition": {"id": "p-0"},
               "routed_object": identity("serve/places-v1/routed/sha256/a.plrv"),
               # The leaf is a build intermediate the head phase reads; it holds
               # TERM rows and must NOT be published.
               "leaf_object": identity("reduce/places-v1/leaves/sha256/b.parquet")}]
    head = {"populated_shards": 1, "shard_count": 16,
            "shard_objects": [identity("serve/places-v1/head/sha256/c.plhd")]}
    keys = [item["key"] for item in HOSTED._artifact_keys("places", places, head)]
    assert keys == [
        "serve/places-v1/routed/sha256/a.plrv",
        "serve/places-v1/head/sha256/c.plhd",
    ]

    # A reduction naming no serving object ABORTS instead of shortening the set.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("places", [{"partition": {"id": "p-9"},
                                          "leaf_object": identity("x")}], head)
    assert "records no 'routed_object'" in str(excinfo.value)
    assert "p-9" in str(excinfo.value)
    # Including the exact shape the defect produced: `artifact` present but None.
    with pytest.raises(SystemExit):
        HOSTED._artifact_keys("places", [{"partition": {"id": "p-8"},
                                          "artifact": None,
                                          "routed_object": None}], head)
    # TRUTHY but malformed must give the intended message, not a raw
    # KeyError/TypeError traceback out of the publication verification.
    for broken in ("a-string", 7, {"key": "k"}, {"key": "k", "sha256": "a" * 64},
                   {"key": 1, "sha256": "a" * 64, "bytes": 2},
                   {"key": "k", "sha256": "a" * 64, "bytes": "2"},
                   {"key": "k", "sha256": 64, "bytes": 2}):
        with pytest.raises(SystemExit) as excinfo:
            HOSTED._artifact_keys(
                "places", [{"partition": {"id": "p-7"}, "routed_object": broken}], head
            )
        assert "not a publishable object identity" in str(excinfo.value), broken
    # Falsy shapes are caught one branch earlier, by the missing-object gate.
    for empty in ({}, "", 0, []):
        with pytest.raises(SystemExit) as excinfo:
            HOSTED._artifact_keys(
                "places", [{"partition": {"id": "p-6"}, "routed_object": empty}], head
            )
        assert "records no 'routed_object'" in str(excinfo.value), empty
    # The address path is unchanged and equally fail-closed.
    assert [item["key"] for item in HOSTED._artifact_keys(
        "addresses", [{"artifact": identity("reduce/address/artifacts/sha256/a.av1")}], None
    )] == ["reduce/address/artifacts/sha256/a.av1"]
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("addresses", [{"routed_object": identity("x")}], None)
    assert "records no 'artifact'" in str(excinfo.value)


def test_the_head_half_of_the_serving_set_fails_closed_too(tmp_path):
    """`head.get("shard_objects", [])` was the same permissive get, one line down.

    It published a places slice with ZERO `.plhd` shards -- exit 0,
    `reconciles: true`, and both new workflow gates satisfied -- including the shape
    where head.json claims `shard_count: 16` while the tree holds none.
    """
    def identity(key: str) -> dict:
        return {"key": key, "sha256": "b" * 64, "bytes": 20}

    reductions = [{"partition": {"id": "p-0"},
                   "routed_object": identity("serve/places-v1/routed/sha256/a.plrv")}]

    # 1. --head omitted entirely.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("places", reductions, None)
    assert "requires a --head result" in str(excinfo.value)
    # 2. An address-shaped head result threaded to places.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("places", reductions, {"family": "places", "head": None})
    assert "requires a --head result" in str(excinfo.value)
    # 3. The nasty one: head.json REPORTS shards and hands over none.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("places", reductions, {
            "shard_count": 16, "populated_shards": 16, "shard_objects": []})
    assert "records no shard_objects" in str(excinfo.value)
    assert "shard_count=16" in str(excinfo.value)
    # 4. A count that disagrees with the objects handed over.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("places", reductions, {
            "shard_count": 16, "populated_shards": 16,
            "shard_objects": [identity("serve/places-v1/head/sha256/c.plhd")]})
    assert "reports populated_shards=16" in str(excinfo.value)
    # 5. A malformed shard entry names itself.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys("places", reductions, {
            "shard_count": 1, "populated_shards": 1, "shard_objects": [{"key": "k"}]})
    assert "head shard 0" in str(excinfo.value)
    # 6. Missing populated_shards at all.
    with pytest.raises(SystemExit):
        HOSTED._artifact_keys("places", reductions, {
            "shard_objects": [identity("serve/places-v1/head/sha256/c.plhd")]})
    # And a family with no head phase must not smuggle shards into the slice.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED._artifact_keys(
            "addresses", [{"artifact": identity("reduce/address/artifacts/sha256/a.av1")}],
            {"shard_objects": [identity("serve/places-v1/head/sha256/c.plhd")]},
        )
    assert "no global head phase" in str(excinfo.value)


def test_places_finalize_publishes_every_routed_object(tmp_path, binaries):
    contract, _ = _derive(tmp_path)
    store = tmp_path / "store"
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir()
    import pyarrow.parquet as pq

    rows = [
        {"id": str(uuid.UUID(int=7000 + i)),
         "primary_name": f"Common Place {i}", "category": "library",
         "locality": "Town", "country": "XX", "confidence": 1.0 - (i % 8) / 20,
         "point": [0.0, 0.0], "source_row_index": i}
        for i in range(60)
    ]
    source = tmp_path / "projected.parquet"
    PLACES_TEST.write_fixture(source, rows, row_group_size=32)
    (tmp_path / "source-limits.json").write_text(json.dumps({"objects": [
        {"records": len(rows),
         "row_groups": pq.ParquetFile(source).metadata.num_row_groups}]}) + "\n")
    _run("run-map", "--contract", contract, "--store-root", store, "--family", "places",
         "--task-id", "places-map-000", "--input", source,
         "--source-limits", tmp_path / "source-limits.json",
         "--transform-binary", binaries["places-transform-v1"],
         "--proof-binary", binaries["places-proof-directory"],
         "--scratch-dir", tmp_path / "map-scratch",
         "--marker-out", markers_dir / "000.json")
    plan = tmp_path / "plan.json"
    _run("plan-reduce", "--contract", contract, "--store-root", store,
         "--family", "places", "--markers-dir", markers_dir,
         "--scratch-dir", tmp_path / "plan-scratch", "--output", plan)
    reductions = tmp_path / "reductions"
    reductions.mkdir()
    partitions = json.loads(plan.read_text())["partitions"]
    for batch in json.loads(plan.read_text())["reduce_execution"]["batches"]:
        _run("run-reduce", "--contract", contract, "--store-root", store,
             "--family", "places", "--plan", plan, "--markers-dir", markers_dir,
             "--batch-index", batch["batch_index"],
             "--encoder-binary", binaries["places-serving-encode-v1"],
             "--verifier-binary", binaries["places-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-{batch['batch_index']}",
             "--output-dir", reductions)
    head = tmp_path / "head.json"
    _run("run-head", "--contract", contract, "--store-root", store, "--family", "places",
         "--markers-dir", markers_dir,
         "--encoder-binary", binaries["places-serving-encode-v1"],
         "--verifier-binary", binaries["places-serving-verify-v1"],
         "--scratch-dir", tmp_path / "head-scratch", "--shard-bits", "4",
         "--output", head)
    remote = tmp_path / "remote"
    final = tmp_path / "final.json"
    _run("finalize", "--contract", contract, "--store-root", store, "--family", "places",
         "--plan", plan, "--reductions-dir", reductions, "--markers-dir", markers_dir,
         "--head", head, "--remote-root", remote,
         "--work-root", tmp_path / "final-work", "--output", final)
    result = json.loads(final.read_text())
    head_result = json.loads(head.read_text())

    # One routed `.plrv` per partition, plus one `.plhd` per populated head shard.
    assert result["serving_object_key"] == "routed_object"
    assert result["reduction_serving_objects"] == len(partitions)
    assert result["serving_objects"] == len(partitions) + head_result["populated_shards"]
    routed = sorted(path.name for path in remote.rglob("*.plrv"))
    assert len(routed) == len(partitions) > 0
    # Every published name is the digest the reducer recorded: registration, not
    # just a count.
    expected = sorted(
        f"{json.loads(path.read_text())['routed_object']['sha256']}.plrv"
        for path in sorted(reductions.glob("*.json"))
    )
    assert routed == expected
    assert len(list(remote.rglob("*.plhd"))) == head_result["populated_shards"]
    # The leaf is an intermediate and must NOT be in the slice.
    assert not list(remote.rglob("*.parquet")) or all(
        "/positions/" in str(path) for path in remote.rglob("*.parquet")
    )
    manifest = json.loads((tmp_path / "final-work/family-manifest.json").read_text())
    assert len(manifest["artifacts"]) == result["serving_objects"]

    # And wrong bytes under a right routed key abort rather than publish.
    victim = json.loads((reductions / sorted(p.name for p in reductions.glob("*.json"))[0]).read_text())
    planted = tmp_path / "store-planted"
    (planted / victim["routed_object"]["key"]).parent.mkdir(parents=True, exist_ok=True)
    for item in store.rglob("*"):
        if item.is_file():
            target = planted / item.relative_to(store)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    (planted / victim["routed_object"]["key"]).write_bytes(
        b"\x00" * victim["routed_object"]["bytes"]
    )
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(planted),
                     "--family", "places", "--plan", str(plan),
                     "--reductions-dir", str(reductions), "--markers-dir", str(markers_dir),
                     "--head", str(head), "--remote-root", str(tmp_path / "remote-bad"),
                     "--work-root", str(tmp_path / "final-work-bad"),
                     "--output", str(tmp_path / "final-bad.json")])
    assert "content-addressed key declares" in str(excinfo.value)
    assert not (tmp_path / "remote-bad").exists()

    # THE HEAD HALF, end to end and through the real CLI. Both shapes the review
    # published a shard-free places slice with:
    #   (a) --head omitted entirely
    #   (b) a head.json reporting shard_count/populated_shards while handing over
    #       no shard objects
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "places", "--plan", str(plan),
                     "--reductions-dir", str(reductions), "--markers-dir", str(markers_dir),
                     "--remote-root", str(tmp_path / "remote-nohead"),
                     "--work-root", str(tmp_path / "final-work-nohead"),
                     "--output", str(tmp_path / "final-nohead.json")])
    assert "requires a --head result" in str(excinfo.value)
    assert not (tmp_path / "remote-nohead").exists()

    hollow = tmp_path / "head-hollow.json"
    hollow.write_text(json.dumps({**head_result, "shard_objects": []}) + "\n")
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "places", "--plan", str(plan),
                     "--reductions-dir", str(reductions), "--markers-dir", str(markers_dir),
                     "--head", str(hollow),
                     "--remote-root", str(tmp_path / "remote-hollow"),
                     "--work-root", str(tmp_path / "final-work-hollow"),
                     "--output", str(tmp_path / "final-hollow.json")])
    assert "records no shard_objects" in str(excinfo.value)
    assert f"shard_count={head_result['shard_count']}" in str(excinfo.value)
    assert not (tmp_path / "remote-hollow").exists()


def test_the_places_plan_phase_is_bounded_not_eagerly_hydrated(tmp_path):
    """The plan phase must never hold its whole pack fan-in.

    `adaptive_genesis_plan` batches its DuckDB reads by `max_fan_in_packs`, but the
    paths were resolved EAGERLY in one list comprehension, so with the store in
    staging every pack was hydrated before the first read -- the entire planet term
    store on the one job run 30113308268 died on. Eviction between batches is what
    makes the batching real, and peak-below-total is how you can tell.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hosted_plan_bound_places", ROOT / "scripts/places_construction_v1.py"
    )
    source = spec.loader.get_source("hosted_plan_bound_places")
    # The eager form must not come back: it is a one-line regression that no
    # functional test on a small slice would notice.
    assert 'paths = [store.path(pack["object"]["key"]) for pack in packs]' not in source
    assert 'release = getattr(store, "release", None)' in source
    # Both passes over the packs release what they fetched.
    assert source.count('release(pack["object"]["key"])') == 2


def test_the_finalize_phase_is_bounded_not_eagerly_hydrated():
    """The same source-level guard the plan phase has, for the same defect.

    Finalize was the only phase left that hydrated its whole input with no
    eviction: `exact_set.append((source, store.path(artifact["key"])))` resolved
    every published object's path up front, and `release` appeared nowhere in the
    module. The published set is small on a slice and 13-18 GB on the planet, so no
    functional test on Monaco would ever notice the difference -- which is exactly
    why the eager form is asserted absent here as well as measured in the smoke.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "hosted_finalize_bound", ROOT / "scripts/construction_v1_hosted.py"
    )
    source = spec.loader.get_source("hosted_finalize_bound")
    assert 'exact_set.append((source, store.path(artifact["key"])))' not in source
    # `getattr`, not `store.release`: `release` is deliberately absent from
    # `LocalObjectStore` (there the local directory IS the store), so a local or
    # offline finalize must evict nothing rather than delete its own inputs.
    assert 'release = getattr(store, "release", None)' in source
    assert "release(store_key)" in source


def test_staging_without_a_contract_fails_closed(tmp_path):
    """The staging prefix is derived from the contract, never guessed."""
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main([
            "admit-task", "--store-root", str(tmp_path / "store"),
            "--family", "places", "--phase", "map", "--task-id", "places-map-000",
            "--staging-root", str(tmp_path / "staging"),
        ])
    assert "request_sha256" in str(excinfo.value)


def test_reduce_batching_matches_the_unbatched_plan(tmp_path, binaries):
    """P0-3: a fixture that yields MORE partitions than a tiny job cap forces
    multiple partitions per matrix JOB, and the batched serving outputs are
    byte-identical to running one job per partition."""
    contract, _ = _derive(tmp_path)

    def plan_and_partitions(store: Path, markers_dir: Path, tag: str, *, max_reduce_jobs=None):
        plan = tmp_path / f"plan-{tag}.json"
        matrix = tmp_path / f"matrix-{tag}.json"
        argv = ["plan-reduce", "--contract", contract, "--store-root", store,
                "--family", "addresses", "--markers-dir", markers_dir, "--row-cap", "2",
                "--output", plan, "--matrix-out", matrix]
        if max_reduce_jobs is not None:
            argv += ["--max-reduce-jobs", str(max_reduce_jobs)]
        _run(*argv)
        return json.loads(plan.read_text()), json.loads(matrix.read_text()), plan

    # Unbatched: default cap -> one job per partition (batch_size 1).
    store_u, markers_u = _address_map_store(tmp_path, contract, binaries, "unbatched")
    plan_u, _matrix_u, plan_u_path = plan_and_partitions(store_u, markers_u, "unbatched")
    partition_count = len(plan_u["partitions"])
    assert partition_count >= 3, "fixture must exceed the tiny job cap to force batching"
    assert plan_u["reduce_execution"]["batch_size"] == 1
    assert plan_u["reduce_execution"]["job_count"] == partition_count

    unbatched_dir = tmp_path / "reductions-unbatched"
    unbatched_dir.mkdir()
    for index in range(partition_count):
        _run("run-reduce", "--contract", contract, "--store-root", store_u,
             "--family", "addresses", "--plan", plan_u_path, "--markers-dir", markers_u,
             "--partition-index", index, "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-u-{index}",
             "--output", unbatched_dir / f"{index:04d}.json")

    # Batched: a job cap of 2 forces batch_size = ceil(partitions/2) > 1.
    store_b, markers_b = _address_map_store(tmp_path, contract, binaries, "batched")
    plan_b, matrix_b, plan_b_path = plan_and_partitions(store_b, markers_b, "batched", max_reduce_jobs=2)
    # Same partitions; only the execution grouping changes.
    assert plan_b["partitions"] == plan_u["partitions"]
    assert plan_b["reduce_execution"]["batch_size"] > 1
    assert plan_b["reduce_execution"]["job_count"] <= 2
    assert len(matrix_b["include"]) == plan_b["reduce_execution"]["job_count"]
    assert matrix_b["include"][0]["partition_count"] >= 2

    batched_dir = tmp_path / "reductions-batched"
    batched_dir.mkdir()
    for batch in matrix_b["include"]:
        _run("run-reduce", "--contract", contract, "--store-root", store_b,
             "--family", "addresses", "--plan", plan_b_path, "--markers-dir", markers_b,
             "--batch-index", batch["batch_index"], "--proof-binary", binaries["address-proof-directory"],
             "--encoder-binary", binaries["address-serving-encode-v1"],
             "--verifier-binary", binaries["address-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-b-{batch['batch_index']}",
             "--output-dir", batched_dir)

    # Every partition produced exactly one reduction in both modes, and the
    # per-partition serving artifacts are byte-identical.
    unbatched = sorted(p.name for p in unbatched_dir.glob("*.json"))
    batched = sorted(p.name for p in batched_dir.glob("*.json"))
    assert unbatched == batched == [f"{i:04d}.json" for i in range(partition_count)]
    for name in unbatched:
        u = json.loads((unbatched_dir / name).read_text())
        b = json.loads((batched_dir / name).read_text())
        # The content-addressed serving artifact and the partition binding are
        # identical; only per-run timing evidence may differ.
        assert u["artifact"] == b["artifact"], name
        assert u["partition"] == b["partition"], name
        assert u["verification"]["binding"] == b["verification"]["binding"], name


def test_batch_retention_is_the_suffix_union_of_what_a_job_still_needs():
    """`_batch_retention[i]` == the packs partitions i+1.. of the same job will open.

    This is what stops per-partition eviction from re-fetching a pack that straddles a
    partition boundary once per partition. Asserted on a hand-built plan/marker pair so
    the suffix-union arithmetic is pinned independently of any fixture's hash layout:
    the last entry must be EMPTY (the final partition of a job releases everything) and
    each earlier entry must be a superset of the next.
    """

    def group(country, low, high):
        return {
            "index": 0,
            "routing_groups": [
                {
                    "country": country,
                    "minimum_route_hash": low,
                    "maximum_route_hash": high,
                }
            ],
        }

    def pack(name, country, low, high):
        return {
            "object": {"key": f"map/address/packs/sha256/{name}.parquet"},
            "directory": {"row_groups": [group(country, low, high)]},
        }

    # Three packs over ascending hash ranges, one of them (b) overlapping both
    # partitions -- exactly the boundary pack the retention exists for.
    markers = [{
        "packs": [
            pack("a" * 64, "US", 0, 89),
            pack("b" * 64, "US", 90, 210),
            pack("c" * 64, "US", 211, 300),
        ]
    }]
    plan = {"partitions": [
        {"id": "p0", "country": "US", "hash_start": 0, "hash_end": 99},
        {"id": "p1", "country": "US", "hash_start": 100, "hash_end": 199},
        {"id": "p2", "country": "US", "hash_start": 200, "hash_end": 300},
    ]}
    keys = {name: f"map/address/packs/sha256/{name * 64}.parquet" for name in "abc"}

    retention = HOSTED._batch_retention("addresses", plan, markers, 0, 3)
    assert len(retention) == 3
    # p0 opens a and b; only b and c are still wanted afterwards, so a is released
    # at p0 and b is held across the boundary rather than re-fetched at p1.
    assert retention[0] == frozenset({keys["b"], keys["c"]})
    assert retention[1] == frozenset({keys["b"], keys["c"]})
    # The last partition of a job retains nothing, so the job ends holding nothing.
    assert retention[2] == frozenset()
    # A suffix union is monotonically non-increasing; anything else means a key could
    # be released while a later partition still needs it.
    for earlier, later in zip(retention, retention[1:]):
        assert later <= earlier

    # A one-partition batch and the places family both retain nothing: places jobs own
    # a bucket range and open each fragment once for the whole range already.
    assert HOSTED._batch_retention("addresses", plan, markers, 1, 1) == [frozenset()]
    assert HOSTED._batch_retention("places", plan, markers, 0, 3) == [frozenset()] * 3


def _small_pack_contract(tmp_path: Path, pack_rows: int) -> Path:
    """A derived contract with a planet-SHAPED address pack size.

    The production `max_pack_rows` is 1,000,000, so any fixture small enough for a
    unit test emits ONE pack per task -- and a one-pack-per-task fixture cannot tell a
    correctly-retaining reducer from an over-retaining one, because the peak is one
    pack either way. Shrinking the cap in a TEST contract is what makes the retention
    observable; the production contract is untouched.
    """
    contract, _ = _derive(tmp_path)
    document = json.loads(contract.read_text())
    document["limits"]["addresses"]["max_pack_rows"] = pack_rows
    document["limits"]["addresses"]["parquet_row_group_rows"] = 2_048
    small = tmp_path / "contract-small-packs.json"
    small.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    return small


def _address_multi_task_store(
    tmp_path: Path, contract: Path, binaries, staging: Path, *, tasks: int, rows: int
) -> tuple[Path, Path]:
    """Map output from SEVERAL tasks, published to a filesystem staging root.

    Rows are dealt ROUND-ROBIN across tasks so every task holds rows spread over the
    whole `route_hash` space -- which is the planet shape, because the map-side sort is
    INTRA-task. A partition therefore needs roughly one pack per task, and that is the
    quantity the retention set has to hold without holding more.
    """
    store = tmp_path / "store-multi"
    markers_dir = tmp_path / "markers-multi"
    markers_dir.mkdir()
    for task in range(tasks):
        fixture = [
            {"id": str(uuid.UUID(int=index + 1)),
             # Vary the street so route_hash (FNV-1a over the normalized fields)
             # spreads and the genesis plan can cut several partitions.
             "street": f"{index % 7} Divided Street", "number": str(100 + index * 3),
             "unit": "", "postcode": f"0{2000 + index}", "postal_city": "Stoneham",
             "address_levels": ["MA", "Stoneham"], "country": "US",
             "point": [-71.0, 42.0], "source_object_index": 0,
             "source_row_group": 0, "source_row_index": index}
            for index in range(rows) if index % tasks == task
        ]
        projected = tmp_path / f"projected-multi-{task}.parquet"
        ADDR_SPIKE.write_fixture(projected, fixture)
        source_limits = tmp_path / f"source-limits-multi-{task}.json"
        source_limits.write_text(
            json.dumps({"objects": [{"records": rows, "row_groups": 1}]}) + "\n"
        )
        _run("run-map", "--contract", contract, "--store-root", store,
             "--staging-root", staging, "--family", "addresses",
             "--task-id", f"addresses-map-{task:03d}",
             "--input", projected, "--source-limits", source_limits,
             "--transform-binary", binaries["address-transform-v1"],
             "--proof-binary", binaries["address-proof-directory"],
             "--scratch-dir", tmp_path / f"map-scratch-multi-{task}",
             "--marker-out", markers_dir / f"{task:03d}.json",
             "--output", tmp_path / f"map-multi-{task}.json")
    return store, markers_dir


def test_batched_address_reduce_fetches_each_pack_once_and_stays_bounded(
    tmp_path, binaries
):
    """The hosted batch path, end to end: each pack fetched once, peak bounded.

    This covers the WIRING, which no other test did. `test_address_construction_v1`
    proves `reduce_partition` releases and `test_batch_retention_...` proves the
    suffix union, but between them sat `cmd_run_reduce`, and both failure directions
    passed CI: dropping `retain_keys` at the call site (back to per-partition
    eviction, which triples R2 reads) and passing the maximal set (over-retention,
    i.e. the original unbounded defect for every batched job). The address slice is one
    partition in one job, so `_batch_retention` even early-returns on `count <= 1`
    there and the suffix union never ran in ANY integration test.

    `released > 0` cannot catch either mutation: the LAST partition of a job always
    retains nothing, so it releases whatever it holds even under maximal
    over-retention. The two assertions below are what discriminate -- hydration count
    catches under-retention, peak catches over-retention.
    """
    tasks = 3
    contract = _small_pack_contract(tmp_path, pack_rows=4)
    staging = tmp_path / "staging"
    store, markers_dir = _address_multi_task_store(
        tmp_path, contract, binaries, staging, tasks=tasks, rows=36
    )
    markers = [json.loads(p.read_text()) for p in sorted(markers_dir.glob("*.json"))]
    assert len(markers) == tasks
    packs = {
        pack["object"]["key"]: pack["object"]["bytes"]
        for marker in markers for pack in marker["packs"]
    }
    # Several packs PER TASK, so over-retention has room to show up as a peak above
    # the one-pack-per-task bound. Without this the test would pass vacuously.
    assert len(packs) >= 2 * tasks, packs

    plan_path = tmp_path / "plan-multi.json"
    matrix_path = tmp_path / "matrix-multi.json"
    _run("plan-reduce", "--contract", contract, "--store-root", tmp_path / "plan-cache",
         "--staging-root", staging, "--family", "addresses",
         "--markers-dir", markers_dir, "--row-cap", "4",
         "--max-reduce-jobs", "1",
         "--output", plan_path, "--matrix-out", matrix_path)
    plan = json.loads(plan_path.read_text())
    batches = plan["reduce_execution"]["batches"]
    # ONE job over MANY partitions: the shape that exercises the suffix union rather
    # than the `count <= 1` early return.
    assert len(batches) == 1
    assert batches[0]["partition_count"] >= 4, plan["reduce_execution"]

    needed = set()
    for partition in plan["partitions"]:
        needed |= HOSTED.ADDRESS.partition_pack_keys(markers, partition)
    assert needed, "fixture selected no packs"

    # A FRESH empty local cache, as a hosted reduce runner has.
    reduce_cache = tmp_path / "reduce-cache"
    staging_report = tmp_path / "reduce-staging-multi.json"
    _run("run-reduce", "--contract", contract, "--store-root", reduce_cache,
         "--staging-root", staging, "--family", "addresses",
         "--plan", plan_path, "--markers-dir", markers_dir,
         "--batch-index", 0,
         "--proof-binary", binaries["address-proof-directory"],
         "--encoder-binary", binaries["address-serving-encode-v1"],
         "--verifier-binary", binaries["address-serving-verify-v1"],
         "--scratch-dir", tmp_path / "reduce-scratch-multi",
         "--staging-report", staging_report,
         "--output-dir", tmp_path / "reductions-multi")
    evidence = json.loads(staging_report.read_text())

    # Under-retention tripwire: with `retain_keys=None` at the call site, every
    # partition re-fetches its packs and this count rises above the distinct set.
    assert evidence["staged_objects_hydrated"] == len(needed), evidence
    # Over-retention tripwire, and the reason peak is asserted against the DATA rather
    # than against the total: peak resident is one pack per map task holding this
    # partition's country, so it must not scale with the number of packs the job
    # touches. `tasks + 1` leaves one pack of slack for a hash-range boundary.
    assert evidence["staged_peak_resident_bytes"] <= (tasks + 1) * max(packs.values()), (
        evidence, len(packs), max(packs.values()),
    )
    # And the job ends holding nothing.
    assert evidence["staged_bytes_released"] == evidence["staged_bytes_hydrated"]

    written = sorted(p.name for p in (tmp_path / "reductions-multi").glob("*.json"))
    assert len(written) == batches[0]["partition_count"]


def test_execute_sequence_places_end_to_end_with_head_no_network(tmp_path, binaries):
    contract, _ = _derive(tmp_path)
    store = tmp_path / "store"
    markers_dir = tmp_path / "markers"
    markers_dir.mkdir()

    def one_map(task_id: str, seed: int, offset: int) -> None:
        rows = [
            {"id": str(uuid.UUID(int=seed * 1000 + i)),
             "primary_name": f"Common Place {offset + i}", "category": "library",
             "locality": "Town", "country": "XX", "confidence": 1.0 - (i % 8) / 20,
             "point": [0.0, 0.0], "source_row_index": i}
            for i in range(60)
        ]
        source = tmp_path / f"{task_id}.parquet"
        PLACES_TEST.write_fixture(source, rows, row_group_size=32)
        import pyarrow.parquet as pq

        (tmp_path / f"{task_id}-limits.json").write_text(json.dumps(
            {"objects": [{"records": len(rows), "row_groups": pq.ParquetFile(source).metadata.num_row_groups}]}))
        _run("run-map", "--contract", contract, "--store-root", store, "--family", "places",
             "--task-id", task_id, "--input", source,
             "--source-limits", tmp_path / f"{task_id}-limits.json",
             "--transform-binary", binaries["places-transform-v1"],
             "--proof-binary", binaries["places-proof-directory"],
             "--scratch-dir", tmp_path / f"{task_id}-scratch",
             "--marker-out", markers_dir / f"{task_id}.json")

    assert _admit_completed(store, "places", "map", task_id="places-map-000") is False
    one_map("places-map-000", 11, 0)
    one_map("places-map-001", 22, 500)
    assert _admit_completed(store, "places", "map", task_id="places-map-000") is True

    plan = tmp_path / "plan.json"
    _run("plan-reduce", "--contract", contract, "--store-root", store, "--family", "places",
         "--markers-dir", markers_dir, "--scratch-dir", tmp_path / "plan-scratch",
         "--output", plan, "--matrix-out", tmp_path / "reduce-matrix.json")
    plan_document = json.loads(plan.read_text())
    partitions = plan_document["partitions"]
    assert len(partitions) >= 1
    # Places reduce jobs own a SHUFFLE-BUCKET RANGE, not a partition range: each
    # job opens the map fragments in its range once and emits every partition
    # whose cell hashes into it.
    execution = plan_document["reduce_execution"]
    assert execution["ownership"] == "shuffle-bucket-range"
    assert execution["bucket_count"] == 1 << execution["shuffle_bucket_bits"]
    assert all(batch["bucket_start"] <= batch["bucket_end"] for batch in execution["batches"])

    reductions_dir = tmp_path / "reductions"
    reductions_dir.mkdir()
    for batch in execution["batches"]:
        _run("run-reduce", "--contract", contract, "--store-root", store, "--family", "places",
             "--plan", plan, "--markers-dir", markers_dir,
             "--batch-index", batch["batch_index"],
             "--encoder-binary", binaries["places-serving-encode-v1"],
             "--verifier-binary", binaries["places-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-scratch-{batch['batch_index']}",
             "--output-dir", reductions_dir)
    assert sorted(p.name for p in reductions_dir.glob("*.json")) == [
        f"{index:04d}.json" for index in range(len(partitions))
    ]

    # The same partitions reduced one at a time must produce the SAME serving
    # artifacts: how the bucket space is cut is an execution grouping only.
    reference_dir = tmp_path / "reductions-per-partition"
    reference_dir.mkdir()
    for index in range(len(partitions)):
        _run("run-reduce", "--contract", contract, "--store-root", store, "--family", "places",
             "--plan", plan, "--markers-dir", markers_dir, "--partition-index", index,
             "--encoder-binary", binaries["places-serving-encode-v1"],
             "--verifier-binary", binaries["places-serving-verify-v1"],
             "--scratch-dir", tmp_path / f"reduce-reference-{index}",
             "--output", reference_dir / f"{index:04d}.json")
    for index in range(len(partitions)):
        ranged = json.loads((reductions_dir / f"{index:04d}.json").read_text())
        single = json.loads((reference_dir / f"{index:04d}.json").read_text())
        for field in ("partition", "binding", "leaf_object", "routed_object",
                      "reconciled_row_groups", "serving_candidate_rows"):
            assert ranged[field] == single[field], (index, field)
        # Both paths are now watched, and the evidence records real peaks.
        assert ranged["ingest_evidence"]["peak_rss_bytes"] > 0
        assert single["ingest_evidence"]["peak_rss_bytes"] > 0
        # The published bytes are bound to the plan, not merely intended to be.
        assert ranged["emit_verification"]["binds_published_bytes"] is True
        assert ranged["emit_verification"]["leaf_binding"] == ranged["binding"]
        assert ranged["emit_verification"]["foreign_cell_rows"] == 0

    # A plan whose batch claims a partition range the bucket range does not own
    # must abort: the plan and the reducer would otherwise disagree about
    # ownership and the reductions directory would be silently misfiled.
    disagreeing = tmp_path / "plan-disagreeing.json"
    tampered = json.loads(plan.read_text())
    tampered["reduce_execution"]["batches"][0]["partition_start"] += 1
    disagreeing.write_text(json.dumps(tampered))
    with pytest.raises(SystemExit, match="did not assign"):
        HOSTED.main([str(a) for a in (
            "run-reduce", "--contract", contract, "--store-root", store,
            "--family", "places", "--plan", disagreeing, "--markers-dir", markers_dir,
            "--batch-index", 0,
            "--encoder-binary", binaries["places-serving-encode-v1"],
            "--verifier-binary", binaries["places-serving-verify-v1"],
            "--scratch-dir", tmp_path / "reduce-disagree",
            "--output-dir", tmp_path / "reductions-disagree")])

    head = tmp_path / "head.json"
    assert _admit_completed(store, "places", "head", index=0) is False
    _run("run-head", "--contract", contract, "--store-root", store, "--family", "places",
         "--markers-dir", markers_dir, "--encoder-binary", binaries["places-serving-encode-v1"],
         "--verifier-binary", binaries["places-serving-verify-v1"],
         "--scratch-dir", tmp_path / "head-scratch", "--shard-bits", "4", "--output", head)
    head_result = json.loads(head.read_text())
    assert head_result["shard_count"] == 16
    assert _admit_completed(store, "places", "head", index=0) is True

    remote = tmp_path / "remote"
    final = tmp_path / "final.json"
    _run("finalize", "--contract", contract, "--store-root", store, "--family", "places",
         "--plan", plan, "--reductions-dir", reductions_dir, "--head", head,
         "--markers-dir", markers_dir,
         "--remote-root", remote, "--work-root", tmp_path / "final-work", "--output", final)
    result = json.loads(final.read_text())
    assert result["reconciles"] is True
    assert result["marker_written_last"] is True
    assert (remote / result["marker_key"]).is_file()

    # The map-phase per-place positions packs must be PUBLISHED, not merely
    # produced: the store travels as a GitHub artifact with a 7-day retention, so
    # anything not in the durable slice is gone a week after a planet run -- and
    # then a spatial reverse index costs the full map re-run this artifact exists
    # to avoid. Each pack and each directory lands under positions/ and is part
    # of the single whole-slice verification.
    manifest = json.loads((tmp_path / "final-work/family-manifest.json").read_text())
    positions = manifest["positions"]
    expected_records = sum(
        json.loads(path.read_text())["positions"]["records"]
        for path in sorted(markers_dir.glob("*.json"))
    )
    assert positions["records"] == expected_records > 0
    assert result["positions_objects"] == len(positions["objects"]) > 0
    assert result["positions_records"] == expected_records
    slice_root = json.loads(contract.read_text())["namespaces"]["slice"].rstrip("/")
    for item in positions["objects"]:
        published = remote / f"{slice_root}/families/places/positions/{Path(item['key']).name}"
        assert published.is_file()
        assert published.stat().st_size == item["bytes"]
    # The verified object count covers the serving set AND the positions set.
    assert result["objects"] == 2 + len(manifest["artifacts"]) + len(positions["objects"])
    assert json.loads((tmp_path / "final-work/slice-manifest.json").read_text())[
        "positions_object_count"
    ] == len(positions["objects"])

    # Publishing them must not be skippable by omission: a workflow that forgets
    # --markers-dir would otherwise ship a places slice with no per-place records,
    # and the cost of noticing that is a full planet map re-run.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "places", "--plan", str(plan),
                     "--reductions-dir", str(reductions_dir), "--head", str(head),
                     "--remote-root", str(tmp_path / "remote-b"),
                     "--work-root", str(tmp_path / "final-work-b"),
                     "--output", str(tmp_path / "final-b.json")])
    assert "--markers-dir is required" in str(excinfo.value)
    # A marker that predates the artifact is the same gap, one level in.
    stale_markers = tmp_path / "stale-markers"
    stale_markers.mkdir()
    for path in sorted(markers_dir.glob("*.json")):
        stale = json.loads(path.read_text())
        stale.pop("positions")
        (stale_markers / path.name).write_text(json.dumps(stale))
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "places", "--plan", str(plan),
                     "--reductions-dir", str(reductions_dir), "--head", str(head),
                     "--markers-dir", str(stale_markers),
                     "--remote-root", str(tmp_path / "remote-c"),
                     "--work-root", str(tmp_path / "final-work-c"),
                     "--output", str(tmp_path / "final-c.json")])
    assert "carries no positions artifact" in str(excinfo.value)

    # `reconciles: true` for places used to be a LITERAL: finalize compared only
    # the summed binding, so a reduction that did not belong to this plan at all
    # still reconciled. Misfile one -- same bytes, same binding, a partition id
    # the plan never had -- and finalize must refuse to publish. This is the
    # failing case that proves the flag can now be false.
    misfiled = tmp_path / "reductions-misfiled"
    misfiled.mkdir()
    for path in sorted(reductions_dir.glob("*.json")):
        reduction = json.loads(path.read_text())
        if path.name == "0000.json":
            reduction["partition"] = {**reduction["partition"], "id": "p-not-in-plan"}
        (misfiled / path.name).write_text(json.dumps(reduction))
    with pytest.raises(ValueError, match="missing, extra, or duplicate"):
        HOSTED.main(["finalize", "--contract", str(contract), "--store-root", str(store),
                     "--family", "places", "--plan", str(plan),
                     "--reductions-dir", str(misfiled), "--head", str(head),
                     "--markers-dir", str(markers_dir),
                     "--remote-root", str(tmp_path / "remote-d"),
                     "--work-root", str(tmp_path / "final-work-d"),
                     "--output", str(tmp_path / "final-d.json")])


def test_ledger_fails_closed_before_the_next_phase(tmp_path):
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "schema": "construction-v1-run-ledger-v1",
        "max_total_runner_minutes": 100,
        "prior_runner_minutes": 40,
        "phases": [{"phase": "admission", "runner_minutes": 0}],
    }))
    # Append a map phase; consumed = 40 + 0 + 30 = 70, still under 100.
    _run("ledger-append", "--ledger", ledger, "--phase", "map", "--minutes", "30")
    # A next phase estimated at 25 keeps us at 95 <= 100: passes.
    _run("ledger-check", "--ledger", ledger, "--next-phase-minutes", "25")
    # A next phase estimated at 40 projects 110 > 100: must fail closed.
    with pytest.raises(SystemExit) as excinfo:
        HOSTED.main(["ledger-check", "--ledger", str(ledger), "--next-phase-minutes", "40"])
    assert "exceed cap" in str(excinfo.value)


def test_prior_runner_minutes_bind_into_the_typed_confirmation():
    control = _load("hosted_test_control", "scripts/construction_v1_control.py")
    import argparse

    def args(**overrides):
        base = dict(request_id="request-20260722-a1", build_id="build-20260722-a1",
                    slice_id="slice-20260722-a1", staging_id="staging-20260722-a1",
                    producer_commit="1" * 40, legacy_core_version="legacy-core-20260722-a1",
                    legacy_core_manifest_sha256="2" * 64)
        base.update(overrides)
        return argparse.Namespace(**base)

    base, _ = control.prepare(args())
    resumed, _ = control.prepare(args(prior_runner_minutes=5000))
    assert "PRIOR_RUNNER_MINUTES=0" in base["typed_confirmation"]
    assert "PRIOR_RUNNER_MINUTES=5000" in resumed["typed_confirmation"]
    assert base["request_sha256"] != resumed["request_sha256"]
    # admit-dispatch regeneration reproduces the resumed confirmation exactly.
    identity = resumed["request"]["identity"]
    core = resumed["request"]["legacy_core"]
    regen, _ = control.prepare(argparse.Namespace(
        request_id=identity["request_id"], build_id=identity["build_id"],
        slice_id=identity["slice_id"], staging_id=identity["staging_id"],
        producer_commit=resumed["request"]["producer_commit"],
        legacy_core_version=core["version"], legacy_core_manifest_sha256=core["manifest_sha256"],
        prior_runner_minutes=resumed["request"]["caps"]["prior_runner_minutes"]))
    assert regen["request"] == resumed["request"]
    assert regen["typed_confirmation"] == resumed["typed_confirmation"]
