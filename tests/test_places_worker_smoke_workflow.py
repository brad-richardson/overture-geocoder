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
    assert "persist-credentials: false" in workflow
    assert "2026-06-17.0" in workflow
    assert workflow.count("experiment_places_partition_extract.py") == 3
    assert "prepare_places_worker_smoke.py" in workflow
    assert "--context boston --context tokyo --context mexico-city" in workflow
    assert "benchmarks/places-relevance-seed.json" in workflow
    assert "catalog.pcat" in workflow
    assert "required_objects" in workflow
    assert "case-${SCOPE}" in workflow
    assert "cjk_exact" in workflow
    assert 'name == "shard_prefix"' in workflow
    assert "head_hit" in workflow
    assert ".candidate_count == $count" in workflow
    assert '(.shards | length) == 1' in workflow
    assert ".read_metrics.r2_reads > 0" in workflow
    assert 'route == "catalog_miss"' in workflow
    assert "overture-places-worker-failure-spike-v1" in workflow
    assert "overture-places-worker-latency-spike-v2" in workflow
    assert "for propagation_attempt in $(seq 1 12)" in workflow
    assert 'if [ "$STATUS" != "404" ]' in workflow
    assert "Places page spike remained unavailable after propagation retries" in workflow
    assert "| tee /tmp/places-worker-latency.json" in workflow
    assert "workers/scripts/geocoder-places-smoke" in workflow
    assert "s3://geocoder-shards/${SMOKE_VERSION}/" in workflow
    assert 'name = "geocoder-places-smoke"' in config
    assert 'ENVIRONMENT = "places-smoke"' in config
    assert 'command = "worker-build --release --features places-spike"' in config
    assert "routes" not in config


def test_places_spike_endpoint_is_not_advertised_as_public():
    worker = (ROOT / "crates" / "geocoder-worker" / "src" / "lib.rs").read_text()

    assert '.get_async("/__places-page-spike"' in worker
    assert '"endpoints":["/search","/reverse","/id/:id"]' in worker
