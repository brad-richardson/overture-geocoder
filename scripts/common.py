#!/usr/bin/env python3
"""Shared helpers for the Overture geocoder data-pipeline scripts.

One tested home for the small utilities that were copy-pasted across the
pipeline scripts: SHA-256 of a file, atomic pretty-JSON writes, version
ordering, and a spill-hardened in-memory DuckDB connection. A single copy
means a fix or hardening (e.g. making the JSON write atomic) lands everywhere
at once instead of drifting between nine near-identical inline definitions.

``duckdb`` is imported lazily inside ``spill_safe_connect`` so the
dependency-thin jobs (the finalizer, which only needs ``sha256_file``) can
import this module without a DuckDB install on the runner.

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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
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

    ``duckdb`` is imported here rather than at module load so callers that only
    need the file/JSON/version helpers (e.g. the finalizer job, which does not
    install DuckDB) can import this module without the dependency present.
    """
    import duckdb

    con = duckdb.connect()
    con.execute(f"SET memory_limit = '{memory_limit}';")
    spill_dir = Path(tempfile.gettempdir()) / "duckdb_spill.tmp"
    con.execute(f"SET temp_directory = '{spill_dir}';")
    con.execute("SET threads = 2;")
    return con


SOURCE_MIRROR_ENV = "OVERTURE_SOURCE_MIRROR"


def source_filesystem(pafs, *, region: str, quiet: bool = False):
    """The filesystem Overture source bytes are read through.

    Defaults to anonymous S3, which is what CI and every promotion path use.
    When ``OVERTURE_SOURCE_MIRROR`` names a directory, reads resolve there
    instead. The directory must be BUCKET-SHAPED -- containing
    ``overturemaps-us-west-2/release/<release>/theme=.../type=.../`` -- because
    callers strip only the ``s3://`` scheme and pass the remaining key.

    This swaps the byte transport and NOTHING else:

    * Object LISTING and the ``head_identity`` etag/size check still go to S3,
      so which objects exist and what they contain stay authoritative. A stale
      or partial mirror fails loudly rather than silently planning over a
      different world -- and the projector's post-inventory size check compares
      the mirror's bytes against the S3 listing, so a truncated mirror is
      caught before it can produce output.
    * URIs stay canonical ``s3://`` strings, so ``approved_prefix`` and
      ``is_approved_source_uri`` keep working unweakened, and the inventory and
      every PUBLISHED serving artifact are byte-identical to a pure-S3 run.
      That equality is the integrity check: if they ever disagree, the mirror
      is wrong.

      Scope that claim carefully. The intermediate ``map/places-v1`` class is
      NOT byte-stable -- two consecutive pure-S3 runs of the same Monaco task
      published 25,876,445 and 25,876,446 bytes, and the mirror 25,876,443, so
      the variance is inherent to that artifact and independent of transport.
      Compare inventory digests and the serving objects, never map bytes.

    Fails closed on a configured-but-missing directory rather than falling back
    to S3, which would silently cost a slow run the operator believed was local.

    Local mirrors are a staging convenience for experimentation. Evidence
    intended for promotion should still come from the sanctioned path.
    """
    mirror = os.environ.get(SOURCE_MIRROR_ENV)
    if not mirror:
        return pafs.S3FileSystem(anonymous=True, region=region)
    root = Path(mirror).expanduser()
    if not root.is_dir():
        raise SystemExit(f"{SOURCE_MIRROR_ENV} is not a directory: {root}")
    if not quiet:
        import sys

        print(f"[source] reading bytes from local mirror {root}", file=sys.stderr)
    return pafs.SubTreeFileSystem(str(root), pafs.LocalFileSystem())
