"""Tests for the shared pipeline helpers in scripts/common.py."""

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

import common


def test_sha256_file_matches_hashlib(tmp_path):
    payload = b"exact bytes" * 1000
    path = tmp_path / "object.bin"
    path.write_bytes(payload)
    assert common.sha256_file(path) == hashlib.sha256(payload).hexdigest()
    # str paths are accepted alongside Path objects.
    assert common.sha256_file(str(path)) == hashlib.sha256(payload).hexdigest()


def test_write_json_pretty_atomic_and_creates_parents(tmp_path):
    path = tmp_path / "nested" / "out.json"
    common.write_json(path, {"b": 2, "a": 1})
    assert json.loads(path.read_text()) == {"b": 2, "a": 1}
    # Pretty-printed like the inline helpers it replaces.
    assert path.read_text() == json.dumps({"b": 2, "a": 1}, indent=2)
    # The temp file used for the atomic replace never survives.
    assert [p.name for p in path.parent.iterdir()] == ["out.json"]


def test_write_json_replaces_existing_content(tmp_path):
    path = tmp_path / "out.json"
    common.write_json(path, {"old": True})
    common.write_json(path, {"new": True})
    assert json.loads(path.read_text()) == {"new": True}


def test_version_sort_key_orders_numeric_suffixes():
    versions = ["2026-07-02.10", "2026-07-02.9", "2026-06-17.0", "2026-07-02.2"]
    assert sorted(versions, key=common.version_sort_key) == [
        "2026-06-17.0",
        "2026-07-02.2",
        "2026-07-02.9",
        "2026-07-02.10",
    ]
    # Unparseable versions fall back to plain string ordering, suffix 0.
    assert common.version_sort_key("fixture") == ("fixture", 0)
