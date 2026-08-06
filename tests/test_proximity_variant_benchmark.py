import importlib.util
import json
from pathlib import Path


SCRIPT = (
    Path(__file__).parent.parent
    / "benchmarks/probes/2026-08-06-proximity-variant-baseline.py"
)
spec = importlib.util.spec_from_file_location("proximity_variant_benchmark", SCRIPT)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def test_fresh_run_has_no_extension_provenance():
    provenance = probe.request_count_provenance(
        False, {}, 84, 84, "2026-08-06T20:00:00+00:00"
    )

    assert provenance == {
        "initial_requests": 84,
        "extension_requests": 0,
        "requests_this_run": 84,
        "initial_timestamp": "2026-08-06T20:00:00+00:00",
    }


def test_append_preserves_initial_run_and_accumulates_extensions():
    provenance = probe.request_count_provenance(
        True,
        {
            "timestamp": "2026-08-06T18:00:00+00:00",
            "requests": 84,
            "initial_requests": 80,
            "extension_requests": 4,
        },
        2,
        86,
        "2026-08-06T20:00:00+00:00",
    )

    assert provenance == {
        "initial_requests": 80,
        "extension_requests": 6,
        "requests_this_run": 2,
        "initial_timestamp": "2026-08-06T18:00:00+00:00",
    }


def test_committed_fresh_evidence_has_fresh_run_provenance():
    evidence = json.loads(
        (
            Path(__file__).parent.parent
            / "benchmarks/2026-08-06-proximity-variant-post-worker-00bc46c.json"
        ).read_text()
    )
    meta = evidence["meta"]

    assert meta["requests"] == 84
    assert meta["initial_requests"] == 84
    assert meta["extension_requests"] == 0
    assert meta["requests_this_run"] == 84
    assert meta["initial_timestamp"] == meta["timestamp"]
    assert meta["extension_run_base_git_sha"] is None
    assert meta["content_sha256"]["probe"] == probe.file_sha256(SCRIPT)
