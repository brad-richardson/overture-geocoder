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
