from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "factory_extract_places.py"


def test_factory_extractor_discloses_nonrepresentative_source_order_limit():
    source = SCRIPT.read_text()
    assert "source-order LIMIT" in source
    assert "ORDER BY" not in source
    assert "bbox.xmin BETWEEN -124.5 AND -114.0" in source
    assert "LIMIT {args.limit}" in source
