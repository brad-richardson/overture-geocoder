from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "smoketest-places-worker.yml"
CONFIG = ROOT / "crates" / "geocoder-worker" / "wrangler.places-smoke.toml"


def test_places_smoke_is_manual_real_three_shard_and_isolated():
    workflow = WORKFLOW.read_text()
    config = CONFIG.read_text()
    trigger = workflow[workflow.index("on:") : workflow.index("permissions:")]

    assert "workflow_dispatch:" in trigger
    assert "push:" not in trigger
    assert "contents: read" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "persist-credentials: false" in workflow
    assert workflow.count("experiment_places_partition_extract.py") == 3
    for required_behavior in (
        "prepare_places_worker_smoke.py",
        "benchmarks/places-relevance-seed.json",
        '(.shards | length) == 1',
        "EXPECTED_RESULTS",
        "overture-places-worker-technical-decision-v1",
        "awaiting_relevance_benchmark",
        "s3://geocoder-shards/${SMOKE_VERSION}/",
    ):
        assert required_behavior in workflow
    assert 'name = "geocoder-places-smoke"' in config
    assert 'ENVIRONMENT = "places-smoke"' in config
    assert 'command = "worker-build --release --features places-spike"' in config
    assert "routes" not in config


def test_places_spike_endpoint_is_not_advertised_as_public():
    worker = (ROOT / "crates" / "geocoder-worker" / "src" / "lib.rs").read_text()

    assert '.get_async("/__places-page-spike"' in worker
    assert '"endpoints":["/search","/reverse","/id/:id"]' in worker
