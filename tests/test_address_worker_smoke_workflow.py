import subprocess
from pathlib import Path


ROOT = Path(__file__).parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "smoketest-address-worker.yml"
CONFIG = ROOT / "crates" / "geocoder-worker" / "wrangler.address-smoke.toml"


def test_address_worker_smoke_is_manual_isolated_and_cleans_up():
    workflow = WORKFLOW.read_text()
    config = CONFIG.read_text()

    assert "workflow_dispatch:" in workflow
    assert "push:" not in workflow
    assert "permissions:\n  contents: read" in workflow
    assert "persist-credentials: false" in workflow
    assert "SMOKE_VERSION: smoketest-address-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert "npm install -g wrangler@4.111.0" in workflow
    assert "cargo install worker-build --version 0.7.5 --locked" in workflow
    assert "ADDRESS_SPIKE_PREFIX:${SMOKE_VERSION}" in workflow
    assert "candidate_count == 137" in workflow
    assert "workers/scripts/geocoder-address-smoke" in workflow
    assert "--request DELETE" in workflow
    assert 'if [ "$HTTP_STATUS" != "404" ]' in workflow
    assert 'test "$HTTP_STATUS" = "200"' in workflow
    assert "--write-out '%{http_code} %{time_total}\\n'" in workflow
    assert 's3://geocoder-shards/${SMOKE_VERSION}/' in workflow
    assert 'name = "geocoder-address-smoke"' in config
    assert 'ENVIRONMENT = "address-smoke"' in config
    assert 'command = "worker-build --release --features address-spike"' in config
    assert "routes" not in config
    global_env = workflow.split("jobs:", 1)[0]
    assert "secrets." not in global_env


def test_measurement_status_and_time_read_survives_strict_bash_eof_handling():
    completed = subprocess.run(
        [
            "bash",
            "-euo",
            "pipefail",
            "-c",
            'read -r status elapsed < <(printf "200 0.125\\n"); '
            'test "$status" = 200; test "$elapsed" = 0.125',
        ],
        check=False,
    )

    assert completed.returncode == 0


def test_spike_endpoint_is_not_advertised_as_a_public_api():
    worker = (ROOT / "crates" / "geocoder-worker" / "src" / "lib.rs").read_text()

    assert '.get_async("/__address-page-spike"' in worker
    assert '"endpoints":["/search","/reverse","/id/:id"]' in worker
