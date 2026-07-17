"""Pin the Python-generated PCSH/PHRP bytes consumed by Rust tests."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "places-pages"
SCRIPT = Path(__file__).with_name("generate_places_page_fixtures.py")
SPEC = importlib.util.spec_from_file_location("generate_places_page_fixtures", SCRIPT)
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)


@pytest.mark.parametrize("name", ("shard.pcsh", "head.phrp", "report.json"))
def test_committed_places_fixture_matches_generator(tmp_path, name):
    generator.write(tmp_path)
    assert (FIXTURE_DIR / name).read_bytes() == (tmp_path / name).read_bytes()
