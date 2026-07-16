from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "factory_extract_places.py"


def test_factory_extractor_is_deterministic_and_discloses_nonrepresentative_bbox():
    source = SCRIPT.read_text()
    # Deterministic extraction: id-ordered before LIMIT, insertion order kept.
    assert "ORDER BY id" in source
    assert "LIMIT {args.limit}" in source
    assert "preserve_insertion_order=true" in source
    assert "preserve_insertion_order=false" not in source
    # ORDER BY id must precede the LIMIT (sort, then slice).
    assert source.index("ORDER BY id") < source.index("LIMIT {args.limit}")
    # Disclosure that this is a bbox slice, not exact CA or a representative sample.
    assert "bbox.xmin BETWEEN -124.5 AND -114.0" in source
    assert "representative sample" in source
    # Disclosure that previously pinned SHAs predate this determinism fix.
    assert "predate this determinism fix" in source
