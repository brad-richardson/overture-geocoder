"""Unit tests for the offline scoring pieces of the type-ahead benchmark."""

import importlib.util
import sys
import types
from pathlib import Path


# The type-ahead script only imports requests to run a live benchmark. Stub it
# so these scoring tests stay hermetic in a minimal environment.
requests_stub = types.ModuleType("requests")
requests_stub.get = lambda *args, **kwargs: None
sys.modules.setdefault("requests", requests_stub)


SCRIPT = Path(__file__).parent.parent / "scripts" / "benchmark_typeahead.py"
spec = importlib.util.spec_from_file_location("benchmark_typeahead", SCRIPT)
benchmark_typeahead = importlib.util.module_from_spec(spec)
spec.loader.exec_module(benchmark_typeahead)

Case = benchmark_typeahead.Case
is_target = benchmark_typeahead.is_target


def test_local_script_primary_name_counts_as_target():
    # A coordinate-correct result whose primary name is the native form must
    # still count for an English-exonym query.
    case = Case("tokyo", "Tokyo", 35.6762, 139.6503, alt_targets=("東京都", "東京"))
    assert is_target(case, "東京都", 35.6895, 139.6917) is True


def test_english_target_without_variants_still_matches():
    case = Case("berlin", "Berlin", 52.5200, 13.4050)
    assert is_target(case, "Berlin", 52.52, 13.405) is True


def test_variant_accepted_but_unrelated_name_rejected():
    case = Case("germany", "Germany", 51.1, 10.4, alt_targets=("Deutschland",))
    # The native variant at the right place counts...
    assert is_target(case, "Deutschland", 51.0, 10.5) is True
    # ...but an unrelated name at the same place does not.
    assert is_target(case, "Gelsenkirchen", 51.0, 10.5) is False


def test_variant_name_still_subject_to_distance_tolerance():
    case = Case("tokyo", "Tokyo", 35.6762, 139.6503, alt_targets=("東京都", "東京"))
    # Right name, far from the target coordinate -> still a miss.
    assert is_target(case, "東京都", 0.0, 0.0) is False


def test_case_sets_registry():
    # The multilingual set is additive: it must not disturb the standard set.
    sets = benchmark_typeahead.CASE_SETS
    assert set(sets) == {"standard", "multilingual", "all"}
    assert sets["standard"] is benchmark_typeahead.CASES
    assert sets["multilingual"] is benchmark_typeahead.MULTILINGUAL_CASES
    assert len(sets["all"]) == len(sets["standard"]) + len(sets["multilingual"])
    # Every multilingual case is tagged and carries real coordinates.
    for c in benchmark_typeahead.MULTILINGUAL_CASES:
        assert c.note == "multilingual"
        assert -90 <= c.lat <= 90 and -180 <= c.lon <= 180


def test_multilingual_exonym_scores_via_alt_targets():
    # "moscou" (fr) must count when an engine returns the native primary name
    # at Moscow's coordinates, and a same-named decoy far away must not.
    case = Case("moscou", "Москва", 55.7558, 37.6173, note="multilingual",
                alt_targets=("Moscow", "Moskau", "Moscou", "Moskva"))
    assert is_target(case, "Москва", 55.7505, 37.6175) is True
    assert is_target(case, "Moscow", 55.75, 37.62) is True
    # Moscou, Belgium (a Ghent neighborhood) shares the name but is ~1900 km off.
    assert is_target(case, "Moscou", 51.0286, 3.7582) is False
