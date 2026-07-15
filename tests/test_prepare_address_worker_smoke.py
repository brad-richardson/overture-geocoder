import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "prepare_address_worker_smoke.py"
SPEC = importlib.util.spec_from_file_location("prepare_address_worker_smoke", SCRIPT)
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


def test_worker_smoke_fixture_is_deterministic_and_bounded(tmp_path):
    first = smoke.build(tmp_path / "first")
    second = smoke.build(tmp_path / "second")

    assert first == second
    assert first["candidate_count"] == 137
    assert first["page_length"] < 16_000
    assert (tmp_path / "first" / "useful_gzip.bin").read_bytes() == (
        tmp_path / "second" / "useful_gzip.bin"
    ).read_bytes()
    assert json.loads((tmp_path / "first" / "report.json").read_text()) == first
