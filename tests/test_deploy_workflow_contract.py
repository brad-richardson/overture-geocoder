from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github/workflows/deploy-rust-worker.yml"


def test_post_deploy_gate_repeats_route_independent_startup_probe():
    value = WORKFLOW.read_text()

    assert "run_startup_checks()" in value
    assert "for probe in $(seq 1 15)" in value
    assert 'curl -fsS --max-time 15 "$BASE_URL/" >/dev/null' in value
    assert "if run_startup_checks && run_checks; then" in value
