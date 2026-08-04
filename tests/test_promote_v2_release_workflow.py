"""Structural contract tests for .github/workflows/promote-v2-release.yml.

Like test_rebuild_workflow_contract.py these parse the workflow YAML and pin
*structural* invariants -- stage gating, the
exactly-one-of catalog CAS expectation, create-only discipline, probe cleanup,
and the no-auto-recover smoke -- rather than raw text, so they do not go
vacuous when step names or wording drift.

Note: PyYAML parses the ``on:`` trigger key as the boolean ``True`` (YAML 1.1),
so the workflow's triggers live under ``wf[True]``.
"""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "promote-v2-release.yml"

STAGES = [
    "probe",
    "promote-slice",
    "publish-release",
    "publish-overlay-release",
    "promote-catalog",
]
INPUTS = {
    "stage",
    "mode",
    "slice_version",
    "geocoder_build",
    "base_geocoder_build",
    "overture_release",
    "legacy_core",
    "places_source",
    "addresses_source",
    "catalog_expectation",
}


def load():
    return yaml.safe_load(WORKFLOW.read_text())


def jobs():
    return load()["jobs"]


def scripts(job):
    return [step.get("run") or "" for step in job.get("steps", [])]


def all_scripts():
    return [script for job in jobs().values() for script in scripts(job)]


# --- dispatch surface ---------------------------------------------------------


def test_workflow_is_dispatch_only_with_the_exact_staged_inputs():
    wf = load()
    triggers = wf[True]
    assert set(triggers) == {"workflow_dispatch"}
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert set(inputs) == INPUTS
    assert len(inputs) <= 10
    assert inputs["stage"]["options"] == STAGES
    assert inputs["stage"]["required"] is True
    assert inputs["mode"]["options"] == ["dry-run", "execute"]
    assert inputs["mode"]["default"] == "dry-run"


def test_permissions_are_read_only_with_actions_read_only_for_artifact_fetch():
    wf = load()
    assert wf["permissions"] == {"contents": "read"}
    for name, job in jobs().items():
        if name in {"promote-slice", "publish-release", "publish-overlay-release"}:
            assert job["permissions"] == {"contents": "read", "actions": "read"}
        else:
            assert "permissions" not in job, name


def test_concurrency_is_the_shared_v2_group_and_never_cancels():
    concurrency = load()["concurrency"]
    assert concurrency["group"] == "r2-v2-publication"
    assert concurrency["cancel-in-progress"] is False


# --- stage gating ------------------------------------------------------------


def test_every_stage_job_needs_the_gate_and_is_main_only():
    named = jobs()
    assert set(named) == {"gate", *STAGES}
    assert "github.ref == 'refs/heads/main'" in named["gate"]["if"]
    for stage in STAGES:
        job = named[stage]
        assert job["needs"] == "gate" or job["needs"] == ["gate"], stage
        assert f"inputs.stage == '{stage}'" in job["if"], stage
        assert "github.ref == 'refs/heads/main'" in job["if"], stage


def test_gate_records_the_stage_target_and_validates_the_cas_expectation():
    gate = "\n".join(scripts(jobs()["gate"]))
    for token in (
        "PROMOTE-SLICE",
        "PUBLISH-RELEASE",
        "PUBLISH-OVERLAY-RELEASE",
        "PROMOTE-CATALOG",
    ):
        assert token in gate, token
    # The typed PROBES-GREEN attestation was removed deliberately.
    assert "PROBES-GREEN:" not in gate
    # The stage target is still recorded in the run log.
    assert "GITHUB_STEP_SUMMARY" in gate
    # Exactly-one-of CAS expectation is validated before any credential.
    assert "catalog_expectation must be exactly 'absent'" in gate


def test_execute_only_steps_are_gated_on_mode():
    named = jobs()
    execute_gated = {
        "promote-slice": 2,  # execute+verify, slice manifest (plan is ungated)
        "publish-release": 1,  # the create-only publish
        "publish-overlay-release": 1,  # the create-only overlay publish
        "promote-catalog": 2,  # the CAS execute and the smoke
    }
    for name, minimum in execute_gated.items():
        gated = [
            step
            for step in named[name]["steps"]
            if "inputs.mode == 'execute'" in str(step.get("if", ""))
        ]
        assert len(gated) >= minimum, (name, [s.get("name") for s in gated])


def test_dry_run_announcement_precedes_every_execute():
    named = jobs()
    # publish-release: an ungated publish-release invocation WITHOUT --execute
    # must come before the gated one WITH --execute.
    for job_name, needle in (
        ("publish-release", "v2_release_manifest.py publish-release"),
        ("publish-overlay-release", "v2_release_manifest.py publish-release"),
        ("promote-catalog", "v2_release_manifest.py promote"),
    ):
        steps = named[job_name]["steps"]
        invocations = [
            (str(step.get("if", "")), step.get("run") or "")
            for step in steps
            if needle in (step.get("run") or "")
        ]
        assert len(invocations) == 2, job_name
        (dry_if, dry_run), (execute_if, execute_run) = invocations
        assert "--execute" not in dry_run and "inputs.mode" not in dry_if
        assert "--execute" in execute_run
        assert "inputs.mode == 'execute'" in execute_if


# --- promote-slice discipline --------------------------------------------------


def test_promote_slice_runs_plan_execute_verify_then_slice_manifest():
    body = "\n".join(scripts(jobs()["promote-slice"]))
    for order, needle in enumerate(
        [
            "promote_construction_slice.py plan",
            "promote_construction_slice.py execute",
            "promote_construction_slice.py verify",
            "promote_construction_slice.py slice-manifest",
        ]
    ):
        assert needle in body, needle
        if order:
            assert body.index(previous) < body.index(needle)  # noqa: F821
        previous = needle  # noqa: F841


def test_promote_slice_publishes_the_slice_manifest_create_only():
    body = "\n".join(scripts(jobs()["promote-slice"]))
    manifest = body[body.index("promote_construction_slice.py slice-manifest") :]
    assert "--execute" in manifest
    assert "slice-manifest.json" in manifest


def test_promote_slice_authenticates_the_construction_request_sha():
    body = "\n".join(scripts(jobs()["promote-slice"]))
    assert "request_sha256" in body
    assert "cv1-control" in body and "cv1-reduce-" in body


def test_promote_slice_rebuilds_reductions_from_r2_before_reaching_for_artifacts():
    # Item 6 consumer. The reduction set is the routing authority, and it used
    # to reach promotion only through GitHub artifacts that retain 7 days. R2
    # must now be the source of record, with the artifacts reachable only after
    # the R2 export has been attempted and failed.
    body = "\n".join(scripts(jobs()["promote-slice"]))
    export_at = body.index("construction_v1_hosted.py export-reductions")
    assert export_at < body.index("--pattern 'cv1-reduce-*'")
    assert "--staging-bucket" in body and "--staging-endpoint-url" in body
    # A fallback that is silent is a deadline nobody knows they are under.
    warning_at = body.index("falling back to the run's GitHub artifacts")
    assert export_at < warning_at < body.index("--pattern 'cv1-reduce-*'")


def test_promote_slice_binds_the_reduction_count_to_the_published_manifest():
    # Whichever source supplied the records, the set must be exactly the
    # partition count the published family manifest attests. Counting only the
    # records that turned up would make a partial set self-consistent -- and
    # the artifact fallback has no per-partition existence check of its own.
    body = "\n".join(scripts(jobs()["promote-slice"]))
    assert "families/${family}/family-manifest.json" in body
    assert '.schema == "construction-v1-family-manifest-v1"' in body
    assert ".request_sha256 == $request" in body
    assert '"$count" -ne "$partitions"' in body
    count_at = body.index('"$count" -ne "$partitions"')
    assert body.index("--pattern 'cv1-reduce-*'") < count_at


def test_promote_slice_accepts_a_finalize_only_runs_resume_reductions():
    # A finalize-only recovery run skips every paid upstream phase, so it
    # publishes no cv1-reduce-* batches; the reduction set it authenticated and
    # reused rides as cv1-resume-reductions. promote-slice must accept that
    # run, and must still fail closed when a run carries neither.
    body = "\n".join(scripts(jobs()["promote-slice"]))
    assert "--name cv1-resume-reductions" in body
    assert "carries neither cv1-reduce-* batches nor cv1-resume-reductions" in body
    # The per-batch artifacts stay preferred, so a run carrying both cannot
    # trip the duplicate-partition check below.
    reduce_at = body.index("--pattern 'cv1-reduce-*'")
    assert reduce_at < body.index("--name cv1-resume-reductions")


def test_promote_slice_authenticates_optional_reverse_runs_without_copying_them():
    gate = "\n".join(scripts(jobs()["gate"]))
    assert "<request-sha256>:<construction-run-id>[:<reverse-run-id>]" in gate
    body = "\n".join(scripts(jobs()["promote-slice"]))
    assert 'local run_and_reverse="${source#*:}"' in body
    assert 'reverse_run="${run_and_reverse#*:}"' in body
    assert '.name == "Build v2 reverse indexes"' in body
    assert 'reverse-v2-${family}-catalog' in body
    assert "--reverse-catalog" in body


def test_publish_release_attaches_authenticated_reverse_without_copying_data():
    gate = "\n".join(scripts(jobs()["gate"]))
    assert "publish-release family sources must include :<reverse-run-id>" in gate
    body = "\n".join(scripts(jobs()["publish-release"]))
    assert '.name == "Build v2 reverse indexes"' in body
    assert 'reverse-v2-${family}-catalog' in body
    assert "--reverse-publication" in body
    assert "copy_within_bucket" not in body
    assert "promote_construction_slice.py" not in body


def test_publish_release_attaches_external_reverse_only_when_not_in_source():
    # promote-slice binds a prepositioned reverse exact set INTO the family
    # manifest, and assemble refuses a family that declares reverse both
    # in-source and externally. So the external record may be attached only for
    # a family whose PROMOTED manifest does not already record its reverse root
    # -- decided from the manifest, never from the dispatch inputs.
    body = "\n".join(scripts(jobs()["publish-release"]))
    probe = 'any(.artifacts[]?; .object_key == $key)'
    assert probe in body
    assert 'families/${FAMILY}/reverse-catalog.rcat' in body
    assert body.index(probe) < body.index('--reverse-publication "${FAMILY}=')
    # The old unconditional per-family attachment is gone.
    assert '--reverse-publication "places=' not in body
    assert '--reverse-publication "addresses=' not in body


def test_overlay_release_preserves_operations_and_requires_matching_reverse():
    gate = "\n".join(scripts(jobs()["gate"]))
    assert "publish-overlay-release needs places_source with :<reverse-run-id>" in gate
    assert "publish-overlay-release needs the current catalog sha256" in gate
    body = "\n".join(scripts(jobs()["publish-overlay-release"]))
    assert 'reverse-v2-places-catalog' in body
    assert '.name == "Build v2 reverse indexes"' in body
    assert "v2_release_manifest.py assemble-overlay" in body
    assert '--catalog-expect-sha256 "$CATALOG_EXPECTATION"' in body
    assert '--base-build "$BASE_GEOCODER_BUILD"' in body
    assert "overlay changed the live operation set" in body


# --- create-only / no-delete discipline ----------------------------------------


def test_no_step_swallows_failures():
    for script in all_scripts():
        assert "|| true" not in script


def test_every_repo_python_invocation_is_unbuffered():
    for script in all_scripts():
        for line in script.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("echo "):
                continue  # printed operator guidance, not an invocation
            for match in re.finditer(r"python\S*\s+(?:-\S+\s+)*scripts/", line):
                assert "python -u " in match.group(0), line


def test_no_raw_object_writes_and_deletes_only_in_probe_cleanup():
    named = jobs()
    for name, job in named.items():
        body = "\n".join(scripts(job))
        assert "aws s3 cp" not in body, name
        assert "aws s3 sync" not in body, name
        assert "put-object" not in body, name
        assert "aws s3 rm" not in body, name
        if name != "probe":
            assert "delete-object" not in body, name


def test_probe_cleanup_is_unconditional_and_verifies_absence():
    probe = jobs()["probe"]
    cleanup = [
        step for step in probe["steps"] if "delete-object" in (step.get("run") or "")
    ]
    assert len(cleanup) == 1
    step = cleanup[0]
    assert str(step.get("if")) == "always()"
    assert "KeyCount" in step["run"]
    # Run-unique keys under the construction probes prefix.
    assert "construction-v1/probes/promote-v2" in str(probe.get("env", {}).get(
        "PROBE_PREFIX", ""
    ))


def test_probe_proves_copy_metadata_etag_size_and_put_if_match():
    body = "\n".join(scripts(jobs()["probe"]))
    assert "copy_within_bucket" in body
    assert "MetadataDirective" in body or "sha256 metadata" in body
    assert "2147483648" in str(jobs()["probe"].get("env", {}).get(
        "PROBE_LARGE_BYTES", ""
    ))
    assert "StateConflict" in body
    assert "stale If-Match PUT did not 412" in body
    assert "stale If-Match DELETE did not 412" not in body
    assert "store.delete(" not in body


# --- promote-catalog discipline --------------------------------------------------


def test_catalog_cas_passes_exactly_one_expectation_flag():
    body = "\n".join(scripts(jobs()["promote-catalog"]))
    assert "--expect-absent" in body
    assert "--expect-sha256" in body
    # The two flags are alternatives of one validated input, never combined.
    for script in scripts(jobs()["promote-catalog"]):
        if "v2_release_manifest.py promote" in script:
            assert 'if [ "$CATALOG_EXPECTATION" != "absent" ]' in script
            assert "EXPECT_ARGS=(--expect-absent)" in script


def test_smoke_never_auto_recovers():
    for script in all_scripts():
        for line in script.splitlines():
            if "v2_release_manifest.py recover" in line:
                assert line.lstrip().startswith("echo "), line


def test_smoke_recovery_uses_cas_unavailable_state_not_conditional_delete():
    body = "\n".join(scripts(jobs()["promote-catalog"]))
    assert "recover --store" in body
    assert "--unavailable" in body
    for line in body.splitlines():
        if "recover --store" in line:
            assert "--delete" not in line


def test_smoke_is_bounded_and_checks_the_promoted_build():
    steps = jobs()["promote-catalog"]["steps"]
    smoke = [
        step for step in steps if "/v2/forward" in (step.get("run") or "")
    ]
    assert len(smoke) == 1
    run = smoke[0]["run"]
    assert "inputs.mode == 'execute'" in str(smoke[0].get("if", ""))
    assert "geocoder_build == $build" in run
    assert "release_unavailable" in run
    # Bounded retries, no unbounded loop.
    assert "for ATTEMPT in 1 2 3 4 5 6 7 8 9 10" in run


def test_smoke_separates_the_places_head_and_routed_paths():
    # The context-free `q=Eiffel Tower` assertion was a false red: head recall
    # for a lone landmark name is an open quality gate, so every correct
    # promotion reported failure. It is replaced by two checks that pin which
    # path served the request -- absent locality inference for the global head,
    # present for the located routed query -- so neither can pass by accident
    # nor silently absorb the other's regression.
    run = next(
        step["run"]
        for step in jobs()["promote-catalog"]["steps"]
        if "/v2/forward" in (step.get("run") or "")
    )
    assert 'q=Eiffel Tower"' not in run  # the bare landmark assertion is gone
    assert 'q=IKEA' in run
    assert 'q=Eiffel Tower Paris' in run
    assert ".metadata.places_locality_inference == null" in run
    assert ".metadata.places_locality_inference.routing != null" in run
    # Both Places checks prove they returned POIs, not divisions.
    assert run.count('.properties.feature_type == "poi"') == 2


def test_smoke_structured_address_sends_country_and_asserts_a_match():
    # The structured request omitted the REQUIRED country field, so it could
    # only ever return invalid_request -- masked because the Places check
    # returned first. It also used an address that does not resolve, behind a
    # warn that could never fail.
    run = next(
        step["run"]
        for step in jobs()["promote-catalog"]["steps"]
        if "/v2/forward" in (step.get("run") or "")
    )
    assert 'country=us' in run
    assert 'postcode=98109' in run  # an exact lookup needs the full context
    assert '.metadata.mode == "structured_address"' in run
    assert '.properties.feature_type == "address"' in run
    # The warn-only escape hatch is gone; an empty result now fails.
    assert "WARN addresses" not in run


# --- action pinning --------------------------------------------------------------


def test_every_action_is_sha_pinned():
    for job in jobs().values():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses:
                assert re.search(r"@[0-9a-f]{40}$", uses), uses
