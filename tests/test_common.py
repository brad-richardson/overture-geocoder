"""Tests for the shared pipeline helpers in scripts/common.py."""

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import common


def _run_without_duckdb(body: str) -> subprocess.CompletedProcess:
    """Run ``body`` in a child interpreter where ``import duckdb`` fails.

    A meta-path finder installed before any pipeline import makes ``duckdb``
    look uninstalled, reproducing the dependency-thin finalizer runner. The
    child exits 0 only if every assertion in ``body`` holds.
    """
    prelude = textwrap.dedent(
        f"""
        import sys
        import importlib.abc
        import importlib.machinery

        class _BlockDuckDB(importlib.abc.MetaPathFinder):
            def find_spec(self, name, path=None, target=None):
                if name == "duckdb" or name.startswith("duckdb."):
                    raise ModuleNotFoundError("No module named 'duckdb'")
                return None

        sys.meta_path.insert(0, _BlockDuckDB())
        sys.modules.pop("duckdb", None)
        sys.path.insert(0, {str(SCRIPTS_DIR)!r})
        """
    )
    return subprocess.run(
        [sys.executable, "-c", prelude + textwrap.dedent(body)],
        capture_output=True,
        text=True,
    )


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


def test_common_imports_and_hashes_without_duckdb():
    # The finalizer job imports ``common`` for ``sha256_file`` only and does not
    # install duckdb; a module-top-level ``import duckdb`` used to crash it.
    result = _run_without_duckdb(
        """
        import common
        from common import sha256_file, write_json, version_sort_key
        assert callable(write_json)
        assert sha256_file(common.__file__)  # non-empty hex digest
        assert version_sort_key("2026-07-13.0") == ("2026-07-13", 0)
        try:
            import duckdb  # blocked in this child
        except ModuleNotFoundError:
            pass
        else:
            raise AssertionError("duckdb should be blocked in this child")
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_finalize_rebuild_imports_without_duckdb():
    # finalize_rebuild only needs common.sha256_file; importing it must not drag
    # in duckdb transitively (the regression from GH run 29586616677).
    result = _run_without_duckdb(
        """
        import finalize_rebuild  # noqa: F401
        assert hasattr(finalize_rebuild, "verify_release")
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_spill_safe_connect_still_requires_duckdb():
    # The lazy import must surface only when the DuckDB helper is actually used,
    # not merely when the module is imported.
    result = _run_without_duckdb(
        """
        import common
        try:
            common.spill_safe_connect()
        except ModuleNotFoundError as exc:
            assert "duckdb" in str(exc)
            print("ok")
        else:
            raise AssertionError("spill_safe_connect should require duckdb")
        """
    )
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout
