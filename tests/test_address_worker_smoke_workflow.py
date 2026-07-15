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
    assert "SMOKE_VERSION: smoketest-address" in workflow
    assert "candidate_count == 137" in workflow
    assert "wrangler.address-smoke.toml --force" in workflow
    assert 's3://geocoder-shards/${SMOKE_VERSION}/' in workflow
    assert 'name = "geocoder-address-smoke"' in config
    assert 'ENVIRONMENT = "address-smoke"' in config
    assert "routes" not in config


def test_spike_endpoint_is_not_advertised_as_a_public_api():
    worker = (ROOT / "crates" / "geocoder-worker" / "src" / "lib.rs").read_text()

    assert '.get_async("/__address-page-spike"' in worker
    assert '"endpoints":["/search","/reverse","/id/:id"]' in worker
