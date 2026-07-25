"""Contract test pinning the construction-v1 slice smoke workflow.

This job exists because the construction-v1 dry-run mode certifies planning
arithmetic and nothing about the execute data plane (follow-ups item 5). Its
value is entirely in *what it actually runs*, so the properties that make it
real are pinned here: no credentials, the real five phases, the pinned release
and task index, and no write to any production surface.
"""

import importlib.util
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "slice-smoke.yml"


def text() -> str:
    return WORKFLOW.read_text()


def parsed() -> dict:
    doc = yaml.safe_load(text())
    return doc


def triggers() -> dict:
    doc = parsed()
    return doc[True] if True in doc else doc["on"]


def load_checker():
    path = ROOT / "scripts" / "check_hosted_imports.py"
    spec = importlib.util.spec_from_file_location("check_hosted_imports", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_hosted_imports"] = module
    spec.loader.exec_module(module)
    return module


def test_runs_on_pull_request_and_main_push_path_filtered():
    on = triggers()
    assert set(on) == {"push", "pull_request", "workflow_dispatch"}
    assert on["push"]["branches"] == ["main"]
    for event in ("push", "pull_request"):
        paths = on[event]["paths"]
        assert "scripts/**" in paths
        assert "crates/geocoder-construction/**" in paths
        assert ".github/requirements-hosted-rowgroup.txt" in paths
        assert ".github/workflows/slice-smoke.yml" in paths
        # The hosted workflow is the source the import check derives from, so a
        # new script invoked there must retrigger this job.
        assert ".github/workflows/construction-v1.yml" in paths


def test_needs_no_credentials_and_writes_no_production_surface():
    value = text()
    assert "secrets." not in value
    assert "id-token: write" not in value
    assert "contents: read" in value
    assert "aws s3" not in value.lower()
    assert "wrangler" not in value.lower()
    # finalize publishes to a filesystem remote inside the workspace only.
    assert "R2_ACCESS_KEY_ID" not in value


def test_runs_the_real_five_phase_data_plane_not_a_dry_run():
    value = text()
    assert "scripts/build_slice_inventory_v1.py" in value
    assert "scripts/run_slice_construction_v1.py" in value
    # The harness is what runs the phases; the dry-run-only surfaces must not be
    # what this job exercises.
    assert "--validate-only" not in value
    assert "predict-reduce" not in value
    # The transform/encoder binaries are built the same way the hosted jobs do.
    assert (
        "cargo build --manifest-path crates/Cargo.toml -p geocoder-construction "
        "--bins --release" in value
    )


def test_pins_the_release_bbox_and_task_index_with_a_drift_check():
    doc = parsed()
    env = doc["env"]
    assert env["SLICE_RELEASE"] == "2026-07-22.0"
    assert env["SLICE_BBOX"] == "7.36 43.71 7.47 43.78"
    assert env["SLICE_TASK_INDEX"] == "33"
    value = text()
    # A release whose row-group layout moved must fail loudly, not silently run
    # a different task than the pinned one.
    assert 'if [ "$TASK" != "$SLICE_TASK_INDEX" ]' in value
    assert "update SLICE_TASK_INDEX" in value


def test_asserts_real_counts_not_just_the_reconciles_literal():
    value = text()
    # `reconciles` is now reported by `places_construction_v1
    # .validate_complete_reduction` rather than a literal, but it still says
    # nothing about whether the run produced DATA -- an empty head and a zero-row
    # partition set reconcile perfectly -- so the job must keep asserting counts
    # an empty run cannot satisfy.
    for assertion in (
        ".reconciles == true",
        ".records > 0",
        ".partitions > 0",
        ".head_shard_count > 0",
        ".head_populated_shards > 0",
        ".head_total_records > 0",
        '.store_bytes_by_class["map/places-v1"] > 0',
        '.store_bytes_by_class["serve/places-v1"] > 0',
        # The per-place positions artifact is produced by map and PUBLISHED by
        # finalize; a run that emitted it and failed to publish it satisfies every
        # other assertion here.
        ".positions_objects > 0",
        ".positions_records > 0",
    ):
        assert assertion in value, assertion
    # Read from files the harness wrote, never `tail -1` of a merged stream.
    assert "summary.json" in value
    assert "tail -1" not in value
    assert '.populated_shards > 0 and .total_records > 0' in value


def test_both_slices_prove_the_r2_staging_transport_credential_free():
    # The artifact-carried store is the planet blocker, so the transport that
    # replaces it must be exercised on every relevant PR rather than only on a
    # credentialed dispatch. Both families assert the same three things:
    #   - the staging prefix matches the bucket convention r2-cleanup.yml guards;
    #   - map actually PUBLISHED objects to staging; and
    #   - finalize actually HYDRATED objects back from it, which is only possible
    #     because each phase runs with its own empty local store. A change that
    #     reverted to one shared local store would leave this at 0.
    value = text()
    for family in ("places", "addresses"):
        assert (
            f'.staging_prefix | test("^staging/global-v2/[0-9a-f]{{64}}/'
            f'construction-v1/{family}$")' in value
        ), family
    assert value.count(".map_staged_objects_published > 0") == 2
    assert value.count(".finalize_staged_objects_hydrated > 0") == 2
    # The plan phase used to hydrate its whole pack fan-in eagerly. Peak below total
    # is the assertable form of "batched and evicted", so this is the tripwire on a
    # regression that no count-based assertion would notice.
    assert (
        ".plan_staged_peak_resident_bytes < .plan_staged_bytes_hydrated" in value
    )
    assert ".plan_staged_objects_released > 0" in value
    # The identical tripwire on FINALIZE, for the identical defect in the last phase
    # of the run: it built its whole exact set out of `store.path(...)` calls, so
    # every published object was hydrated before the first upload and none was
    # released -- 13-18 GB at planet scale. Asserted for BOTH families, because
    # finalize's fan-in is the published set regardless of family.
    assert value.count(
        ".finalize_staged_peak_resident_bytes < .finalize_staged_bytes_hydrated"
    ) == 2
    assert value.count(".finalize_staged_objects_released > 0") == 2
    # Addresses plan from marker JSON alone, so zero is the CORRECT plan-phase
    # figure there -- asserted as zero rather than skipped, and the address
    # reducer's unbounded fan-in is stated rather than hidden.
    assert ".plan_staged_bytes_hydrated == 0" in value
    assert ".reduce_staged_bytes_hydrated > 0" in value
    # Head hydration is measured, not bounded (one read_parquet over every
    # candidate pack), so the figure must at least be produced.
    assert ".head_staged_bytes_hydrated > 0" in value
    assert ".head_staged_bytes_hydrated == 0" in value  # addresses have no head
    assert "NOT \"the address\n            # planet build is unblocked at reduce\"" in value
    # Still no credentials: the staging backend is a directory, not R2.
    assert "R2_ACCESS_KEY_ID" not in value
    assert "secrets." not in value


def test_both_slices_assert_the_exact_published_serving_set():
    """The count of SERVING objects, per family, as an equality.

    A places finalize published head shards, positions packs and two manifests
    while dropping every routed `.plrv`, because a places reduction records
    `routed_object` and `_artifact_keys` read `artifact`. `objects`, `reconciles`
    and `positions_objects` were all non-zero, so only an assertion on the serving
    set itself can catch it.
    """
    value = text()
    assert '.serving_object_key == "routed_object"' in value
    assert '.serving_object_key == "artifact"' in value
    assert ".reduction_serving_objects == .partitions" in value
    # Places: one routed object per partition PLUS one shard per populated head
    # shard. Addresses: partitions only, since they have no head phase.
    assert ".serving_objects == .partitions + .head_populated_shards" in value
    assert ".serving_objects == .partitions" in value


def test_retry_preserves_every_attempt_and_shouts_when_it_retries():
    value = text()
    # Per-attempt log filenames; a retry must not overwrite attempt 1.
    assert 'LOG="slice/run-attempt-${ATTEMPT}.log"' in value
    assert "slice/run-attempt-*.log" in value
    # An intermittent defect that passes on attempt 2 must be visible.
    assert "::warning title=Slice smoke passed only on retry::" in value
    assert "SLICE_RETRIED=true" in value
    # Phase reports are copied out on the failing path too.
    assert 'cp -f "$FILE" "slice/attempt-${ATTEMPT}-$(basename "$FILE")"' in value


def test_states_per_defect_coverage_honestly():
    value = text()
    header = value[: value.index("on:")]
    # hosted-imports cannot see #150's function-level psutil import; the slice
    # job is what covers it, and #148's surface is covered by neither.
    assert "#150" in header and "SLICE job" in header
    assert "#148" in header and "NEITHER job" in header
    assert "top-level import failures only" in header


def test_installs_only_the_hash_pinned_dependencies_on_the_hosted_python():
    value = text()
    doc = parsed()
    assert (
        "python -m pip install --only-binary=:all: --require-hashes "
        "-r .github/requirements-hosted-rowgroup.txt" in value
    )
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in value
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in value
    assert "persist-credentials: false" in value
    for job in doc["jobs"].values():
        pythons = [
            step["with"]["python-version"]
            for step in job["steps"]
            if "setup-python" in step.get("uses", "")
        ]
        assert pythons == ["3.11.14"], pythons


def test_every_job_is_bounded_and_fast():
    doc = parsed()
    jobs = doc["jobs"]
    assert set(jobs) == {"hosted-imports", "slice", "address-slice"}
    for name, job in jobs.items():
        assert job["timeout-minutes"] <= 20, name


def test_pins_the_address_slice_with_its_own_drift_check():
    """The address family had no data-plane coverage and no drift gate at all."""
    doc = parsed()
    env = doc["env"]
    assert env["SLICE_RELEASE"] == "2026-07-22.0"
    assert env["ADDRESS_SLICE_BBOX"] == "-122.34 47.59 -122.30 47.63"
    assert env["ADDRESS_SLICE_TASK_INDEX"] == "54"
    value = text()
    assert 'if [ "$TASK" != "$ADDRESS_SLICE_TASK_INDEX" ]' in value
    assert "update ADDRESS_SLICE_TASK_INDEX" in value
    # The slice's value as a spatial fixture is that it spans two level-8 cells,
    # so a layout change that kept the task index but moved the cell boundary out
    # of the row group must still fail.
    assert ".object_index == 8 and .row_group == 108 and .task_records == 104928" in value
    # The harness must be driven with the address family, not the places default.
    assert "run_slice_construction_v1.py --family addresses" in value
    assert "build_slice_inventory_v1.py --family addresses" in value


def test_the_address_slice_asserts_real_counts_and_the_documented_no_op_head():
    value = text()
    for assertion in (
        '.family == "addresses"',
        ".admitted_rows > 0",
        ".map_packs > 0",
        # Proves the run used the batch dispatch the hosted matrix uses, not the
        # legacy one-job-per-partition path.
        '.reduce_ownership == "partition-batch"',
        '.store_bytes_by_class["map/address"] > 0',
        '.store_bytes_by_class["reduce/address"] > 0',
        # Map EMITTED the per-address records artifact, complete, and the slice is
        # still the two-cell spatial fixture it is pinned to be.
        ".address_records_rows == .admitted_rows",
        ".address_records_packs > 0",
        ".address_records_null_island == 0",
        "(.address_records_cells | length) == 2",
        # And finalize PUBLISHED it durably. An emitted-but-unpublished artifact
        # expires with the 7-day map artifact retention and satisfies every other
        # assertion in this job.
        ".positions_objects > 0",
        ".positions_records == .admitted_rows",
    ):
        assert assertion in value, assertion
    # Addresses have no global head; run-head must say so rather than produce an
    # empty head that looks like a successful one.
    assert '.family == "addresses" and .head == null' in value
    # Read from files the harness wrote, never `tail -1` of a merged stream.
    assert "address-summary.json" in value
    # Same retry/evidence discipline as the places job, with its own log names so
    # a retry cannot overwrite attempt 1 and the two jobs cannot collide.
    assert 'LOG="slice/address-run-attempt-${ATTEMPT}.log"' in value
    assert "::warning title=Address slice smoke passed only on retry::" in value
    assert "ADDRESS_SLICE_RETRIED=true" in value


def test_the_import_check_covers_every_hosted_workflow_entrypoint():
    module = load_checker()
    modules = module.discover()
    # Derived from the hosted workflows, so a new invocation is covered for free.
    for name in (
        "construction_v1_hosted",
        "construction_v1_control",
        "project_places_construction_v1",
        "experiment_hosted_rowgroups",
        "places_construction_v1",
        "build_slice_inventory_v1",
    ):
        assert name in modules, name
    # Modules that parse arguments at import time cannot be imported; the slice
    # job runs them instead. The checker itself is excluded too.
    assert "run_slice_construction_v1" not in modules
    assert "check_hosted_imports" not in modules
    for name in modules:
        assert (ROOT / "scripts" / f"{name}.py").exists(), name
    assert len(modules) >= module.MINIMUM_MODULES


def test_a_missing_derivation_source_is_a_hard_error_not_a_silent_narrowing():
    module = load_checker()
    empty = Path(__file__).parent / "fixtures"
    try:
        module.discover(empty)
    except SystemExit as error:
        assert "does not exist" in str(error)
    else:  # pragma: no cover - the point of the test
        raise AssertionError("a missing workflow must fail closed")


def test_the_import_check_actually_imports_them():
    module = load_checker()
    assert module.main([]) == 0
