"""Contract test pinning the construction-v1 hosted workflow.

The workflow must never become a false or unsafe dispatch surface: it stays
dispatch-only, gates on the typed EXECUTE_CONSTRUCTION_V1 confirmation in its
very first job, derives the contract/runtime every later job consumes, passes
phase state between jobs as pinned artifacts, keeps every R2 write behind the
execute-mode guard, and ENFORCES the runner-minute ledger (sum + abort) rather
than merely printing it.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import yaml


WORKFLOW = (
    Path(__file__).parent.parent / ".github" / "workflows" / "construction-v1.yml"
)
REQUIREMENTS = (
    Path(__file__).parent.parent / ".github" / "requirements-hosted-rowgroup.txt"
)


def text() -> str:
    return WORKFLOW.read_text()


def parsed() -> dict:
    return yaml.safe_load(text())


def test_workflow_is_dispatch_only_and_non_publishing():
    value = text()
    trigger = value[value.index("on:") : value.index("permissions:")]
    assert "workflow_dispatch:" in trigger
    assert "pull_request:" not in trigger
    assert "push:" not in trigger
    assert "schedule:" not in trigger
    doc = parsed()
    on = doc[True] if True in doc else doc["on"]
    assert list(on.keys()) == ["workflow_dispatch"]
    assert "contents: read" in value


def test_workflow_exposes_the_required_dispatch_inputs():
    doc = parsed()
    on = doc[True] if True in doc else doc["on"]
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["family"]["options"] == ["address", "places"]
    assert inputs["mode"]["options"] == ["dry-run", "execute"]
    assert inputs["confirmation"]["required"] is True
    assert inputs["resume_from"]["required"] is False
    assert "request_json" in inputs
    # The reducer job cap is dispatch-time tunable, and it must stay OPTIONAL with an
    # EMPTY default: a non-empty default would silently change production batching.
    assert inputs["max_reduce_jobs"]["required"] is False
    assert inputs["max_reduce_jobs"]["default"] == ""


def test_the_reduce_job_cap_reaches_both_the_dry_run_and_the_execute_plan():
    """A dry-run must certify the reduce shape the execute would actually dispatch.

    The cap only ever LOWERS the job count, so a dry-run that ignored it would
    certify a matrix and a runner-minute projection for a DIFFERENT batching than the
    execute uses. Both call sites take the same validated flag.
    """
    value = text()
    assert value.count("MAX_REDUCE_JOBS: ${{ inputs.max_reduce_jobs }}") == 2
    # Validated, not interpolated straight into a command line: it is free text.
    assert value.count("max_reduce_jobs must be a positive integer") == 2
    assert value.count('REDUCE_JOB_FLAG=(--max-reduce-jobs "$MAX_REDUCE_JOBS")') == 2
    assert '--staging-report plan/staging.json "${REDUCE_JOB_FLAG[@]}"' in value
    assert '--ledger control/ledger.json "${REDUCE_JOB_FLAG[@]}"' in value


def test_first_job_is_the_fail_closed_gate_that_derives_the_contract():
    value = text()
    doc = parsed()
    jobs = list(doc["jobs"].keys())
    assert jobs[0] == "admit"
    assert "scripts/construction_v1_control.py admit-dispatch" in value
    assert "--run-attempt '${{ github.run_attempt }}'" in value
    assert "EXECUTE_CONSTRUCTION_V1" in value
    # MAJOR-1: contract.json / runtime.json are actually generated (nothing
    # downstream can reference a file that was never produced).
    assert "scripts/construction_v1_hosted.py derive-contract" in value
    assert "--output control/contract.json --runtime control/runtime.json" in value
    admit = doc["jobs"]["admit"]
    assert "secrets." not in yaml.safe_dump(admit)
    assert admit["if"] == "github.ref == 'refs/heads/main'"


def test_execute_data_plane_uses_the_native_adapter_not_a_missing_contract():
    value = text()
    for command in ("run-map", "plan-reduce", "run-reduce", "run-head", "finalize"):
        assert f"scripts/construction_v1_hosted.py {command}" in value
    assert "control/runtime.json" in value


def test_places_map_uses_the_places_projector_not_the_address_experiment():
    # P0-1: Places has its own S3 projector; the address row-group experiment has
    # no --family places code path, so the Places map branch MUST call it.
    value = text()
    assert "scripts/project_places_construction_v1.py" in value
    # The address projector is still wired for the address family.
    assert "scripts/experiment_hosted_rowgroups.py" in value
    # source-limits is derived per-object from the projection report, never a
    # hardcoded row_groups:1 bound that would reject real projected locators.
    assert "construction_v1_hosted.py source-limits" in value
    assert '"row_groups":1' not in value


def _load_module(name: str, relative: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent.parent / relative)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_places_evidence_spec_is_threaded_from_the_contract_not_hardcoded():
    # MAJOR (post-#146): the Places evidence spec is pinned in exactly ONE place
    # (construction_v1_control.py). The workflow must read the spec path from the
    # admission-derived contract, never a hardcoded version constant, so a bump
    # can't leave the projector stamping a stale sha (silent digest divergence).
    value = text()
    assert "jq -er '.families.places.spec' control/contract.json" in value
    # No hardcoded evidence-spec path literal survives in the workflow.
    assert "evidence-spec.json" not in value
    assert "evidence-spec-v2.json" not in value


def test_derived_contract_carries_the_control_pinned_spec_for_each_family(tmp_path):
    # The single-source pin flows control.py -> request -> derived contract, so
    # the projector's spec path always matches the admission-verified sha.
    control = _load_module("contract_test_control", "scripts/construction_v1_control.py")
    hosted = _load_module("contract_test_hosted", "scripts/construction_v1_hosted.py")
    report, _ = control.prepare(argparse.Namespace(
        request_id="request-20260722-a1", build_id="build-20260722-a1",
        slice_id="slice-20260722-a1", staging_id="staging-20260722-a1",
        producer_commit="1" * 40, legacy_core_version="legacy-core-20260722-a1",
        legacy_core_manifest_sha256="2" * 64, prior_runner_minutes=0))
    request_path = tmp_path / "request.json"
    request_path.write_bytes(control.canonical(report["request"]))
    contract = tmp_path / "contract.json"
    runtime = tmp_path / "runtime.json"
    assert hosted.main(["derive-contract", "--request", str(request_path),
                        "--output", str(contract), "--runtime", str(runtime),
                        "--allow-unpinned-duckdb"]) == 0
    import json

    families = json.loads(contract.read_text())["families"]
    for family in ("addresses", "places"):
        assert families[family]["spec"] == control.FAMILIES[family]["spec"]
        assert families[family]["spec_sha256"] == control.FAMILIES[family]["spec_sha256"]


def test_reduce_is_batched_under_the_matrix_and_reducer_cap():
    # P0-3: the reduce matrix has ONE entry per batch JOB (batch_index), each job
    # processes a contiguous partition range via --batch-index/--output-dir, and
    # plan-reduce gates the ledger on the BATCHED job count.
    value = text()
    assert "matrix.batch_index" in value
    assert "matrix.partition_index" not in value
    assert "--batch-index" in value
    assert "--output-dir reductions" in value
    plan_block = value[value.index("Fan in map minutes") : value.index("Publish the plan")]
    assert "--ledger control/ledger.json" in plan_block
    assert "--tail-minutes" in plan_block
    # batch_size and reduce minutes are derived from the MEASURED per-partition
    # reduce time (a per-family constant), not a hand-tuned per-job flag.
    assert "--reduce-minutes-per-job" not in value


def test_dry_run_runs_the_real_projection_validators_and_capacity_prediction():
    # P1-1: a dry-run must invoke the REAL projection argument/inventory-schema
    # validation (no S3) and predict the reduce partition/batch/minute demand,
    # so a dry-run certifies the execute could actually parse and fit.
    value = text()
    dry_block = value[
        value.index("Bounded planning sample") : value.index("Free disk before bounded map work")
    ]
    assert "--validate-only" in dry_block
    assert "scripts/project_places_construction_v1.py --validate-only" in dry_block
    assert "scripts/experiment_hosted_rowgroups.py --validate-only" in dry_block
    assert "construction_v1_hosted.py predict-reduce" in dry_block


def test_phase_outputs_flow_between_jobs_as_pinned_artifacts():
    value = text()
    doc = parsed()
    assert "actions/download-artifact@37930b1c2abaa49bbe596cd826c3c89aef350131" in value
    assert "actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a" in value
    assert "merge-multiple: true" in value
    assert (
        doc["jobs"]["reduce"]["strategy"]["matrix"]
        == "${{ fromJSON(needs.plan.outputs.reduce_matrix) }}"
    )


def test_map_matrix_respects_concurrency_and_the_256_cap():
    value = text()
    doc = parsed()
    strategy = doc["jobs"]["map"]["strategy"]
    assert strategy["max-parallel"] == "${{ fromJSON(needs.admit.outputs.max_parallel) }}"
    assert strategy["fail-fast"] is False
    assert "exceeds the 256 matrix cap" in value
    assert "reduce matrix exceeds 256 cap" in value
    assert (
        "inputs.mode == 'execute' && needs.admit.outputs.map_matrix "
        "|| needs.admit.outputs.sample_matrix" in value
    )


def test_map_matrix_lookup_uses_the_singular_dispatch_family_key():
    # admit-dispatch emits address_matrix=/places_matrix= keyed by the SINGULAR
    # dispatch input family, while the contract families dict is plural
    # (addresses/places). The gate step must read matrices.env with the raw
    # input family, not FAMILY_KEY — the plural form silently matched nothing
    # for address and failed the first real dry-run dispatch.
    value = text()
    control = (WORKFLOW.parent.parent.parent / "scripts" / "construction_v1_control.py").read_text()
    assert 'sed -n "s/^${FAMILY}_matrix=//p" control/matrices.env' in value
    assert '"s/^${FAMILY_KEY}_matrix=//p"' not in value
    assert 'output.write(f"address_matrix=' in control
    assert 'output.write(f"places_matrix=' in control


# --- head shard count -------------------------------------------------------
# The production head shard count is a SIZING decision with a hard downstream
# cliff: a head shard is one PLHD artifact and the encoder fail-closes at
# MAX_INDEX_ENTRIES distinct tokens. The workflow shipped `--shard-bits 4` (16
# shards) against a committed design of 4096, which at the measured planet token
# universe is 6-8x OVER the cap -- and the only thing that reported it was the
# encoder's `bail!`, at encode time. (Ordering, stated precisely: today
# `max_head_candidate_rows = 5_000_000` aborts a ~65M-row planet head phase before
# either check runs, so the sizing has to be right for when that cap is raised.)
# These tests exist so that literal can never silently drift below the design again.

ENCODER_SOURCE = (
    Path(__file__).parent.parent
    / "crates"
    / "geocoder-construction"
    / "src"
    / "bin"
    / "places_serving_encode_v1.rs"
)
# Measured planet token universe, docs/plans/2026-07-24-places-global-scale-plan.md
# (12-task census: 4,749,161 distinct tokens over 14.1% of the source rows, linearly
# extrapolated because distinct-per-feature was still rising in-sample).
PLANET_DISTINCT_TOKENS_LOW = 25_000_000
PLANET_DISTINCT_TOKENS_HIGH = 33_600_000


def _encoder_max_index_entries() -> int:
    # Anchored on the exact declaration, colon included: a substring match would
    # silently bind to a RENAMED superstring constant
    # (`const MAX_INDEX_ENTRIES_PER_SHARD: usize = 900_000;`) and return the wrong
    # number, which is the one way this check could fail quietly.
    for line in ENCODER_SOURCE.read_text().splitlines():
        if line.strip().startswith("const MAX_INDEX_ENTRIES:"):
            return int(line.split("=")[1].strip().rstrip(";").replace("_", ""))
    raise AssertionError(
        "`const MAX_INDEX_ENTRIES:` not found in the Places serving encoder"
    )


def test_head_shard_bits_constant_matches_the_committed_design():
    places = _load_module("contract_test_places", "scripts/places_construction_v1.py")
    # 12 bits => 4096 shards, the count the global-scale plan committed to (and the
    # count the production ID index already uses).
    assert places.DEFAULT_HEAD_SHARD_BITS == 12
    assert (1 << places.DEFAULT_HEAD_SHARD_BITS) == 4096
    # The Python mirror of the encoder cap must track the Rust enforcing side.
    assert places.SERVING_MAX_INDEX_ENTRIES == _encoder_max_index_entries()


def test_planet_head_entries_per_shard_clear_the_encoder_cap_with_margin():
    # The sizing arithmetic itself, encoded as a test: revise the token-universe
    # estimate upward past what 4096 shards can hold and THIS trips, instead of a
    # planet head phase tripping after map+reduce+merge are spent.
    places = _load_module("contract_test_places", "scripts/places_construction_v1.py")
    cap = places.SERVING_MAX_INDEX_ENTRIES
    bits = places.DEFAULT_HEAD_SHARD_BITS
    for tokens in (PLANET_DISTINCT_TOKENS_LOW, PLANET_DISTINCT_TOKENS_HIGH):
        entries = places.head_entries_per_shard(tokens, bits)
        assert entries <= cap
        # Real hashing is not perfectly uniform and the estimate is an
        # extrapolation, so require a wide margin, not a bare pass.
        assert entries * 10 <= cap, f"{entries} entries/shard is under 10x margin"
        assert places.minimum_head_shard_bits(tokens) <= bits
    # And the value the workflow used to pass is genuinely over the cap -- this is
    # the defect statement, kept executable.
    assert places.head_entries_per_shard(PLANET_DISTINCT_TOKENS_LOW, 4) > cap
    assert places.minimum_head_shard_bits(PLANET_DISTINCT_TOKENS_HIGH) > 4


def test_workflow_head_phase_passes_the_designed_shard_bits():
    value = text()
    places = _load_module("contract_test_places", "scripts/places_construction_v1.py")
    expected = f"--shard-bits {places.DEFAULT_HEAD_SHARD_BITS}"
    assert expected in value
    # Exactly one head invocation carries it (addresses have no head phase), and no
    # other shard-bits literal survives anywhere in the workflow.
    assert value.count("--shard-bits") == 1
    assert value.count(expected) == 1


def test_hosted_head_shard_bits_default_is_the_constant_not_a_literal():
    # The whole defect was a literal drifting from the constant, so the CLI default
    # must BE the constant object, verified through the real parser.
    hosted = _load_module("contract_test_hosted_head", "scripts/construction_v1_hosted.py")
    places = _load_module("contract_test_places", "scripts/places_construction_v1.py")
    parser = hosted.build_parser()
    namespace = parser.parse_args([
        "run-head", "--contract", "c.json", "--store-root", "s",
        "--family", "places", "--markers-dir", "m", "--output", "o.json",
    ])
    assert namespace.shard_bits == places.DEFAULT_HEAD_SHARD_BITS == 12


def test_slice_harness_picks_its_small_shard_bits_explicitly():
    # A 4096-shard head over a 38k-place Monaco slice is nonsense, so the harness
    # keeps a small count -- but as a NAMED, deliberate slice choice, never as an
    # inherited default that silently tracks whatever production uses.
    harness = (
        WORKFLOW.parent.parent.parent / "scripts" / "run_slice_construction_v1.py"
    ).read_text()
    assert "SLICE_HEAD_SHARD_BITS = 4" in harness
    assert '"--shard-bits", str(SLICE_HEAD_SHARD_BITS)' in harness
    assert '"--shard-bits", "4"' not in harness


def test_cargo_builds_target_the_crates_workspace_manifest():
    # The workspace Cargo.toml lives in crates/, not the repo root; a bare
    # `cargo build` on the runner dies with "could not find Cargo.toml"
    # (planet attempt 1 lost all its map jobs to this in execute mode only,
    # because the build step was execute-gated and dry-run never compiled).
    value = text()
    assert "cargo build -p geocoder-construction" not in value
    assert value.count(
        "cargo build --manifest-path crates/Cargo.toml -p geocoder-construction --bins --release"
    ) == 3
    # The map-job build is unconditional so dry-run certifies compilation.
    doc = parsed()
    for step in doc["jobs"]["map"]["steps"]:
        if "cargo build" in str(step.get("run", "")):
            assert "if" not in step


def test_every_phase_carries_the_330_minute_job_timeout():
    doc = parsed()
    jobs = doc["jobs"]
    for name in ("map", "plan", "reduce", "head"):
        assert jobs[name]["timeout-minutes"] == 330
    assert jobs["finalize"]["timeout-minutes"] == 360


def test_r2_writes_are_execute_mode_only_and_create_only():
    value = text()
    doc = parsed()
    jobs = doc["jobs"]
    for name in ("plan", "reduce", "head", "finalize"):
        assert jobs[name]["if"] == "inputs.mode == 'execute'"
    for name in ("admit", "map"):
        for step in jobs[name]["steps"]:
            env = step.get("env", {}) or {}
            if any("secrets." in str(v) for v in env.values()):
                assert step.get("if") == "inputs.mode == 'execute'", (name, step.get("name"))
    # Create-only publication is no longer a shell literal in this workflow, and the
    # assertion had to move with it rather than be deleted. It used to be a serial
    # `aws s3api put-object --if-none-match '*'` mirror loop; the exact set is now
    # published by `construction_v1_remote.publish_exact_set` through the R2 backend
    # this workflow selects. So assert the WIRING -- that finalize publishes to the
    # bucket instead of to a local tree the workflow then mirrors -- and let
    # tests/test_construction_v1_remote.py own the create-only behaviour itself.
    assert "--remote-bucket" in value
    assert "--remote-endpoint-url" in value
    # And the mirror really is gone: no `aws s3api` write of any kind survives in
    # this workflow. A resurrected loop is 12.4 hours of process startup for a planet
    # address slice against finalize's 360-minute timeout.
    assert "aws s3api put-object" not in value
    assert "--if-none-match" not in value
    for step in jobs["finalize"]["steps"]:
        assert "aws s3api" not in str(step.get("run", "")), step.get("name")
    assert "marker written last" in value.lower()


def test_runner_minute_ledger_is_enforced_not_decorative():
    value = text()
    doc = parsed()
    # MAJOR-2: the ledger is summed and the run FAILS CLOSED before the next
    # phase (ledger-check exits non-zero when projected > cap, proven in
    # tests/test_construction_v1_hosted.py::test_ledger_fails_closed...).
    assert "scripts/construction_v1_hosted.py ledger-check" in value
    assert "scripts/construction_v1_hosted.py ledger-append" in value
    assert "--next-phase-minutes" in value
    plan_block = value[value.index("Fan in map minutes") : value.index("Publish the plan")]
    assert "ledger-append" in plan_block
    assert "ledger-check" in plan_block
    assert plan_block.index("ledger-check") < plan_block.index("reduce_matrix=")
    assert "reduce_matrix" in doc["jobs"]["plan"]["outputs"]


def test_resume_carries_the_prior_ledger_or_fails_closed():
    value = text()
    doc = parsed()
    # MAJOR-3: resume downloads the prior run's ledger read-only and refuses to
    # start on a mismatch, so a fresh dispatch cannot silently reset prior spend.
    assert doc["permissions"]["actions"] == "read"
    assert "actions/runs/${RESUME_FROM}/artifacts" in value
    assert "resume failed closed" in value
    assert "construction-v1-ledger-" in value
    assert "!= confirmation PRIOR_RUNNER_MINUTES" in value


def test_workflow_pins_actions_and_hash_locked_dependencies():
    value = text()
    requirements = REQUIREMENTS.read_text()
    assert "actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0" in value
    assert "actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1" in value
    assert "--require-hashes" in value
    assert "persist-credentials: false" in value
    assert "@main" not in value
    assert "@v7\n" not in value
    assert "@v5" not in value
    assert "pyarrow==25.0.0" in requirements
    assert "duckdb==1.5.1" in requirements


def test_marker_written_last_and_fresh_dispatch_resume_are_pinned():
    value = text()
    assert "marker_written_last=true" in value
    assert "NON-PROMOTING" in value
    assert "never Re-run failed jobs" in value
    assert "construction-v1-run-ledger-v1" in value


def _upload_paths(job: dict, name_fragment: str) -> str:
    for step in job["steps"]:
        if "upload-artifact" in str(step.get("uses", "")):
            with_block = step.get("with", {}) or {}
            if name_fragment in str(with_block.get("name", "")):
                return str(with_block.get("path", ""))
    raise AssertionError(f"no upload-artifact step named like {name_fragment}")


def test_the_intermediate_store_travels_through_r2_staging_not_artifacts():
    # THE planet blocker. The map store is 63.5 GB for planet Places (34 GB after
    # the combiner) and does not fit on a runner, so run 30113308268 died on a
    # 63 GB plan artifact and reduce has never started. Every inter-job artifact
    # must therefore carry markers and JSON only, and every phase must be pointed
    # at the run-scoped R2 staging prefix instead.
    value = text()
    doc = parsed()
    jobs = doc["jobs"]

    # Not one artifact carries the store directory.
    assert "store" not in _upload_paths(jobs["map"], "cv1-map-").split()
    assert "mapdl/store" not in _upload_paths(jobs["plan"], "cv1-plan")
    assert "cv1/mapdl/store" not in _upload_paths(jobs["reduce"], "cv1-reduce-")
    assert "cv1/cv1/mapdl/store" not in _upload_paths(jobs["head"], "cv1-head")

    # And every phase that touches the store is given the staging prefix.
    assert value.count(
        '--staging-bucket "$R2_BUCKET" --staging-endpoint-url "$R2_ENDPOINT"'
    ) == 7  # admit-task, run-map, plan-reduce, run-reduce, run-head x2, finalize
    for command in ("run-map", "plan-reduce", "run-reduce", "run-head", "finalize"):
        block_start = value.index(f"construction_v1_hosted.py {command}")
        block = value[block_start : block_start + 900]
        assert "--staging-bucket" in block, command

    # A map task that staged nothing wrote its fragments nowhere durable, and the
    # artifact no longer carries them.
    assert 'STAGED="$(jq -r \'.staged_objects_published\' phase/map.json)"' in value
    assert 'test "$STAGED" -gt 0' in value

    # The staged marker makes resume REAL (a fresh runner now sees completed
    # tasks), so the skip path has to republish the marker it is skipping on --
    # otherwise the plan phase silently plans without that task.
    assert '--marker-out "markers/${TASK_INDEX}.json"' in value
    assert 'test -s "markers/${TASK_INDEX}.json"' in value

    # The plan job reads pack BODIES, so it needs the same free-disk floor map and
    # reduce have. It is the job run 30113308268 actually died on.
    # EVERY data-plane job has a free-disk floor. head and finalize were the last
    # two without one, and head cannot batch-and-evict its candidate fan-in, so the
    # floor is its only guard.
    for name in ("map", "plan", "reduce", "head", "finalize"):
        assert any(
            "df -Pk / | awk" in str(step.get("run", "")) for step in jobs[name]["steps"]
        ), f"the {name} job has no free-disk gate"
    assert value.count("df -Pk / | awk 'NR==2 {print $4}'") == 5

    # Peak resident hydrated bytes -- the number that decides whether a phase fits a
    # runner -- is recorded by every phase that reads store objects.
    assert "--staging-report plan/staging.json" in value
    assert '--staging-report "phase/staging-${BATCH_INDEX}.json"' in value
    assert value.count("--staging-report head/staging.json") == 2  # places + addresses
    assert "staged_peak_resident_bytes" in value

    # A published slice with no SERVING payload is the defect this gate exists for:
    # places reductions record `routed_object`, not `artifact`, so finalize silently
    # published no `.plrv` at all while every other number stayed non-zero.
    assert 'SERVING="$(jq -r \'.serving_objects\' final-work/result.json)"' in value
    # The EXACT set, not a lower bound. `-ge REDUCTIONS` would have accepted a slice
    # that published every routed object and NO head shards -- the other half of the
    # same permissive-get defect, which was demonstrated as publishable.
    assert 'test "$SERVING" -ge "$REDUCTIONS"' not in value
    assert "POP=\"$(jq -r '.populated_shards // 0' headdl/head/head.json)\"" in value
    # The head ROUTING MANIFEST is part of the published serving set (shard objects
    # are content-addressed, so it is the only shard_id -> object map), so the
    # equality carries its term. Family-generic: addresses report 0.
    #
    # Extracted fail-closed, the same principle as the staging asserts: bash
    # arithmetic reads an absent key's "null" as 0, so `jq -r` + `// 0` would turn a
    # MISSING manifest into a passing gate. `jq -e` + `| numbers` exits non-zero.
    assert (
        'MANIFESTS="$(jq -er \'.head_manifest_objects | numbers\' '
        'final-work/result.json)"' in value
    )
    assert "jq -r '.head_manifest_objects'" not in value
    assert 'test "$SERVING" -eq "$(( REDUCTIONS + POP + MANIFESTS ))"' in value
    assert 'test "$SERVING" -gt 0' in value

    # And the finalize job asserts its RESIDENCY bound, not just its output set.
    # Finalize used to hydrate the whole published set before its first upload with
    # no eviction -- 13-18 GB at planet scale, on the last job of a multi-hour run.
    # This is the planet workflow, so this is where that bound decides whether the
    # run finishes; the slice-smoke jobs assert the same three facts on the fast
    # loop. Eager hydration makes peak == hydrated with nothing released, so all
    # three are false under a regression.
    assert "(.staged_objects_hydrated | numbers) > 0" in value
    assert "(.staged_objects_released | numbers) > 0" in value
    assert (
        "((.staged_peak_resident_bytes | numbers) < (.staged_bytes_hydrated | numbers))"
        in value
    )
    # `| numbers` rather than a `// 0` default, deliberately: it yields empty for a
    # missing or non-numeric key, so `jq -e` exits non-zero instead of comparing a
    # fabricated value. A permissive default here would turn an ABSENT bound into a
    # passing one, which is the failure mode the gate exists to prevent -- so assert
    # that shape is NOT used for any of the three.
    for key in (
        "staged_objects_hydrated",
        "staged_objects_released",
        "staged_peak_resident_bytes",
        "staged_bytes_hydrated",
    ):
        assert f".{key} // 0" not in value, key


def test_needs_graph_is_connected():
    doc = parsed()
    jobs = doc["jobs"]
    known = set(jobs)
    for name, job in jobs.items():
        needs = job.get("needs", [])
        if isinstance(needs, str):
            needs = [needs]
        for dependency in needs:
            assert dependency in known, f"{name} needs unknown job {dependency}"
    assert jobs["finalize"]["needs"] == ["admit", "plan", "reduce", "head"]
    assert jobs["reduce"]["needs"] == ["admit", "plan"]
