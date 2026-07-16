#!/usr/bin/env python3
"""Shared helpers for the Overture geocoder data-pipeline scripts.

One tested home for the small utilities that were copy-pasted across the
pipeline scripts: SHA-256 of a file, atomic pretty-JSON writes, version
ordering, and a spill-hardened in-memory DuckDB connection. A single copy
means a fix or hardening (e.g. making the JSON write atomic) lands everywhere
at once instead of drifting between nine near-identical inline definitions.

TODO: scripts/build_shards.py still carries its own hash_file / write_json /
version_sort_key / spill_safe_connect. Its SHA-256 is pinned by the Monaco
subset evidence (docs/plans/2026-07-12-monaco-subset-evidence.json), whose
regeneration needs a long network run, so build_shards.py converts to these
shared helpers the next time that evidence is regenerated.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

import duckdb


def sha256_file(path: Path | str) -> str:
    """Return the SHA-256 hex digest of a file, read in 1 MiB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as src:
        for chunk in iter(lambda: src.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path | str, data) -> None:
    """Atomically write ``data`` as pretty-printed JSON.

    Writes to a sibling temp file and ``os.replace()``s it into place, so a
    crash mid-write can never leave a truncated object behind for a later
    reader (or uploader) to consume.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w") as handle:
            json.dump(data, handle, indent=2)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def version_sort_key(version: str) -> tuple[str, int]:
    """Sort key for ``{YYYY-MM-DD}.{N}`` versions.

    The date part sorts lexicographically and the ``.N`` suffix numerically
    (plain string order would rank ``.9`` above ``.10``).
    """
    date, _, suffix = version.rpartition(".")
    if date and suffix.isdigit():
        return (date, int(suffix))
    return (version, 0)


def spill_safe_connect(memory_limit: str = "10GB") -> "duckdb.DuckDBPyConnection":
    """In-memory DuckDB connection hardened for the CI runner.

    In-memory sessions disable disk spill by default, so a big join or
    aggregation OOMs instead of going out-of-core (the DuckDB 1.5 failure
    mode the download SQL scripts guard against). Cap memory, provide a
    spill directory, and bound parallel pipeline buffers.
    """
    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}';")
    spill_dir = Path(tempfile.gettempdir()) / "duckdb_spill.tmp"
    con.execute(f"SET temp_directory = '{spill_dir}';")
    con.execute("SET threads = 2;")
    return con
