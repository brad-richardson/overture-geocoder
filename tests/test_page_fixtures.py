"""Pin the cross-language page/extension fixtures consumed by the Rust core.

Asserts the committed ``tests/fixtures/pages/*`` bytes are exactly what the
Python generator produces today, and that the committed extended page still
round-trips through the real convergence codec. The matching Rust decoders live
in ``crates/geocoder-core/src/pages.rs`` and ``crates/geocoder-worker/src/address_pages.rs``.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "pages"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


generator = _load("generate_page_fixtures", Path(__file__).with_name("generate_page_fixtures.py"))
convergence = _load(
    "experiment_address_format_convergence",
    ROOT / "scripts" / "experiment_address_format_convergence.py",
)

FIXTURE_NAMES = (
    "plain_page.bin",
    "extended_page.bin",
    "truncated_extended_page.bin",
    "report.json",
)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_committed_fixture_matches_generator_byte_for_byte(tmp_path, name):
    generator.write(tmp_path)
    committed = (FIXTURE_DIR / name).read_bytes()
    regenerated = (tmp_path / name).read_bytes()
    assert committed == regenerated, f"{name} drifted from generate_page_fixtures.py"


def test_committed_extended_page_round_trips():
    payload = (FIXTURE_DIR / "extended_page.bin").read_bytes()
    records, extension = convergence.decode_extended_page(payload)
    assert len(records) == len(extension) == 3
    assert records[0]["number"] == "10"
    assert records[1]["number"] == "11"
    assert records[0]["address_levels"] == ["MA", "Cambridge"]
    # Record 0 carries two containing-division GERS IDs; record 2 carries none.
    assert set(extension[0]["division_gers_ids"]) == {
        str(uuid.UUID(int=7)),
        str(uuid.UUID(int=8)),
    }
    assert extension[0]["match_method"] == convergence.MATCH_METHOD_INTERIOR
    assert extension[0]["match_confidence"] == 2
    assert extension[2]["division_gers_ids"] == []
    assert extension[2]["match_method"] == convergence.MATCH_METHOD_NONE


def test_committed_plain_page_is_the_extended_core():
    # The extended page's core half must be exactly the standalone plain page,
    # which is the invariant the Rust core test also checks via include_bytes!.
    plain = (FIXTURE_DIR / "plain_page.bin").read_bytes()
    extended = (FIXTURE_DIR / "extended_page.bin").read_bytes()
    core_length, position = convergence.decode_uvarint(extended, 0)
    assert extended[position : position + core_length] == plain


def test_committed_truncated_page_is_rejected():
    payload = (FIXTURE_DIR / "truncated_extended_page.bin").read_bytes()
    with pytest.raises(ValueError, match="truncated extended-page core"):
        convergence.decode_extended_page(payload)
