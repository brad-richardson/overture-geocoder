from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/deploy-rust-worker.yml"
WRANGLER = ROOT / "crates/geocoder-worker/wrangler.toml"


def test_post_deploy_gate_repeats_route_independent_startup_probe():
    value = WORKFLOW.read_text()

    assert "run_startup_checks()" in value
    assert "for probe in $(seq 1 15)" in value
    assert 'curl -fsS --max-time 15 "$BASE_URL/" >/dev/null' in value
    assert "if run_startup_checks && run_checks; then" in value


def test_worker_build_pin_matches_the_worker_0_7_crate():
    workflow = WORKFLOW.read_text()
    wrangler = WRANGLER.read_text()

    assert 'WORKER_BUILD_VERSION: "0.7.5"' in workflow
    assert 'cargo install worker-build --locked --version "${WORKER_BUILD_VERSION}"' in workflow
    assert 'test "$(worker-build --version)" = "${WORKER_BUILD_VERSION}"' in workflow
    assert "cargo install -q --locked worker-build@0.7.5" in wrangler
    assert "worker-build@^0.7" not in wrangler


def test_production_is_fail_closed_for_v2_and_omits_it_from_discovery():
    workflow = WORKFLOW.read_text()
    wrangler = WRANGLER.read_text()

    assert 'ENABLE_V2_SERVING = "false"' in wrangler
    observability = wrangler.split("[observability]", 1)[1].split("[build]", 1)[0]
    assert "enabled = false" in observability
    assert '.endpoints == ["/search", "/reverse", "/id/:id"]' in workflow
    for path in (
        '"/v2"',
        '"/v2/forward?q=berlin"',
        '"/v2/reverse?lat=42.3601&lon=-71.0589"',
        '"/v2/ids/00000000-0000-4000-8000-000000000000"',
    ):
        assert path in workflow
    assert 'if [ "$status" != "404" ]; then' in workflow
    assert 'echo "  OK   HEAD /v2/forward (404)"' in workflow
    assert 'echo "  OK   OPTIONS /v2/forward (404)"' in workflow
    assert 'if [ "$v1_attempts" -eq 3 ]; then' in workflow
    assert "sleep 61" in workflow
