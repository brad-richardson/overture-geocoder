#!/usr/bin/env python3
"""Pin the records/record_index coalesce-gap constants across Python and Rust.

The reader model (`experiment_places_compact_shard`) and the Worker
(`crates/geocoder-worker/src/places_pages.rs`) each define the record_index and
records coalesce-gap thresholds. They must stay identical so the producer oracle
and the Worker plan the same physical reads; this test fails if either side
changes without the other.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUST_SOURCE = ROOT / "crates" / "geocoder-worker" / "src" / "places_pages.rs"


def _load_python_constants() -> tuple[int, int]:
    script = SCRIPTS / "experiment_places_compact_shard.py"
    spec = importlib.util.spec_from_file_location(
        "experiment_places_compact_shard", script
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec.loader.exec_module(module)
    return module.RECORD_INDEX_COALESCE_GAP, module.RECORDS_COALESCE_GAP


def _rust_constant(name: str) -> int:
    text = RUST_SOURCE.read_text()
    match = re.search(rf"const\s+{name}\s*:\s*u64\s*=\s*([0-9*+\s]+);", text)
    assert match, f"Rust constant {name} not found in {RUST_SOURCE}"
    expression = match.group(1).strip()
    assert re.fullmatch(r"[0-9*+\s]+", expression), expression
    return int(eval(expression, {"__builtins__": {}}))  # noqa: S307 - digits/*/+ only


def test_record_index_and_records_gaps_match_across_languages():
    python_index_gap, python_record_gap = _load_python_constants()
    assert python_index_gap == _rust_constant("RECORD_INDEX_COALESCE_GAP")
    assert python_record_gap == _rust_constant("RECORDS_COALESCE_GAP")
    # The two coalesce call sites in places_pages.rs must reference the named
    # constants, not re-inlined literals, so the parity check cannot be bypassed.
    text = RUST_SOURCE.read_text()
    assert "coalesced(&index_wants, RECORD_INDEX_COALESCE_GAP" in text
    assert "coalesced(&record_wants, RECORDS_COALESCE_GAP" in text
