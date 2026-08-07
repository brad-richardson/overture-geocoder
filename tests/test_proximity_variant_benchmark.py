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


def test_the_gate_scorer_agrees_with_the_frozen_probes_chain_rule():
    """One rule, two callers, and the probe's copy cannot be edited.

    The 2026-08-06 probe froze the stratum and its own file sha256 is pinned
    inside the evidence it produced, so the duplicate cannot be collapsed by
    editing it. The standing preview gate scores with
    `benchmark_v2_forward.chain_name_matches` instead, and this test is what
    keeps the two from drifting: it replays the frozen run through the gate's
    scorer and requires the published headline back, then checks the name rule
    itself on the awkward cases.
    """
    import importlib.util

    root = Path(__file__).parent.parent
    spec = importlib.util.spec_from_file_location(
        "bvf_gate", root / "scripts/benchmark_v2_forward.py"
    )
    bvf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bvf)

    frozen = json.loads(
        (root / "benchmarks/2026-08-06-proximity-variant-post-worker-00bc46c.json").read_text()
    )
    cases = {
        case["id"]: case
        for case in json.loads(
            (root / "benchmarks/proximity-chain-cases-v1.json").read_text()
        )["cases"]
    }

    at_1 = at_10 = 0
    for row in frozen["proximity_results"]:
        case = cases[row["case_id"]]
        features = [
            {
                "properties": {"name": candidate.get("name")},
                "geometry": {
                    "type": "Point",
                    "coordinates": [candidate["lon"], candidate["lat"]],
                },
            }
            for candidate in row.get("candidates", [])
            if candidate.get("lat") is not None and candidate.get("lon") is not None
        ]
        rank, _, _ = bvf.score_case(case, features)
        at_1 += rank == 1
        at_10 += rank is not None and rank <= 10

    published = frozen["proximity_summary"]["overall"]
    assert at_1 == published["chain_within_2km_at_1"] == 28
    assert at_10 == published["chain_within_2km_at_10"] == 40

    # The rule's own edges, stated once so a change to either copy shows up.
    for chain, candidate, expected in (
        ("Starbucks", "Starbucks Reserve Roastery", True),
        ("Woolworths", "Woolworths Riley Street Car Park", True),
        ("Şok", "sok", True),
        ("Starbucks", "Peet's Coffee", False),
        ("Starbucks", "", False),
        ("Starbucks", None, False),
    ):
        assert bvf.chain_name_matches(chain, candidate) is expected, (chain, candidate)
