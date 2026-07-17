#!/usr/bin/env python3
"""Pin the records/record_index coalesce constants across Python and Rust.

The reader model (`experiment_places_compact_shard`) and the Worker
(`crates/geocoder-worker/src/places_pages.rs`) each define the record_index and
records coalesce-gap thresholds and the per-physical-read size caps. All four
must stay identical so the producer oracle and the Worker plan the same
physical reads; this test fails if either side changes without the other.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUST_SOURCE = ROOT / "crates" / "geocoder-worker" / "src" / "places_pages.rs"


def _load_python_module():
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
    return module


def _rust_constant(text: str, name: str) -> int:
    match = re.search(
        rf"const\s+{name}\s*:\s*(?:u64|usize)\s*=\s*([0-9_*+\s]+);", text
    )
    assert match, f"Rust constant {name} not found in {RUST_SOURCE}"
    expression = match.group(1).strip().replace("_", "")
    assert re.fullmatch(r"[0-9*+\s]+", expression), expression
    return int(eval(expression, {"__builtins__": {}}))  # noqa: S307 - digits/*/+ only


def _call_site_arguments(text: str, wants: str) -> tuple[str, str]:
    """The (gap, max_range) argument names passed to coalesced() for `wants`.

    Whitespace-insensitive so rustfmt line wrapping cannot hide a drift.
    """
    condensed = re.sub(r"\s+", "", text)
    match = re.search(rf"\.coalesced\(&{wants},(\w+),(\w+),?\)", condensed)
    assert match, f"coalesced call for {wants} not found in {RUST_SOURCE}"
    return match.group(1), match.group(2)


def test_record_index_and_records_plans_match_across_languages():
    module = _load_python_module()
    text = RUST_SOURCE.read_text()
    assert module.RECORD_INDEX_COALESCE_GAP == _rust_constant(
        text, "RECORD_INDEX_COALESCE_GAP"
    )
    assert module.RECORDS_COALESCE_GAP == _rust_constant(text, "RECORDS_COALESCE_GAP")
    assert module.RECORD_INDEX_MAX_RANGE_BYTES == _rust_constant(
        text, "RECORD_INDEX_MAX_RANGE_BYTES"
    )
    assert module.RECORDS_MAX_RANGE_BYTES == _rust_constant(
        text, "MAX_RESULT_RANGE_BYTES"
    )
    # The two coalesce call sites in places_pages.rs must pass exactly the named
    # constants above, not re-inlined literals or other constants, so the parity
    # check cannot be bypassed.
    assert _call_site_arguments(text, "index_wants") == (
        "RECORD_INDEX_COALESCE_GAP",
        "RECORD_INDEX_MAX_RANGE_BYTES",
    )
    assert _call_site_arguments(text, "record_wants") == (
        "RECORDS_COALESCE_GAP",
        "MAX_RESULT_RANGE_BYTES",
    )
    # The postings stage plans per-matched-entry wants at gap 0 with the same
    # cap on both sides (Worker: coalesced(&posting_wants, 0, MAX_POSTING_BYTES)).
    assert module.POSTINGS_COALESCE_GAP == 0
    assert module.POSTINGS_MAX_RANGE_BYTES == _rust_constant(
        text, "MAX_POSTING_BYTES"
    )
    assert module.QUERY_POSTINGS_MAX_BYTES == _rust_constant(
        text, "MAX_QUERY_POSTING_BYTES"
    )
    assert _call_site_arguments(text, "posting_wants") == (
        "0",
        "MAX_POSTING_BYTES",
    )
    # The lexicon stage and the per-clause union candidate cap are mirrored
    # too, so every stage of the modeled read plan (and every hard-cap failure)
    # transfers to the Worker.
    assert module.LEXICON_MAX_RANGE_BYTES == _rust_constant(
        text, "MAX_LEXICON_BLOCK_BYTES"
    )
    assert module.POSTING_CANDIDATES_CAP == _rust_constant(
        text, "MAX_POSTING_CANDIDATES"
    )
    assert _call_site_arguments(text, "wants") == (
        "0",
        "MAX_LEXICON_BLOCK_BYTES",
    )


def test_python_model_enforces_the_max_range_cap(tmp_path):
    """The model's planner splits at the cap exactly like the Rust planner, so
    modeled read counts transfer to the Worker even for wide served windows."""
    module = _load_python_module()
    path = tmp_path / "blob.bin"
    path.write_bytes(bytes(4096))
    reader = module.RangeReader(path)
    # Two wants whose merged span exceeds the cap must split even at a huge gap.
    chunks = reader.read_ranges(
        [(0, 100), (2000, 100)], "records", max_gap=1 << 30, max_range=1024
    )
    assert [(chunk.offset, len(chunk.data)) for chunk in chunks] == [
        (0, 100),
        (2000, 100),
    ]
    # Within the cap the same wants merge into one physical read.
    merged = reader.read_ranges(
        [(0, 100), (2000, 100)], "records", max_gap=1 << 30, max_range=4096
    )
    assert [(chunk.offset, len(chunk.data)) for chunk in merged] == [(0, 2100)]
    # A single want larger than the cap fails closed, mirroring coalesce_ranges.
    try:
        reader.read_ranges([(0, 2048)], "records", max_gap=0, max_range=1024)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized want must fail closed")
