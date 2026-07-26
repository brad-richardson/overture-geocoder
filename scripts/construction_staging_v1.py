#!/usr/bin/env python3
"""R2 staging transport for the construction-v1 map-phase intermediate store.

The planet blocker is transport, not compute: the map phase's content-addressed
store (63.5 GB pre-combiner, ~34 GB after) moves between phases as a GitHub
Actions artifact, whole, every time. It does not fit on a runner, so reduce has
never started. See ``docs/plans/2026-07-24-r2-staging-design.md`` §1-§2 and the
"next increment" section of ``docs/plans/construction-v1-state.md``.

This module is the seam that fixes it. ``StagedObjectStore`` presents exactly the
four-method surface the construction code already uses
(``path`` / ``put_content`` / ``read_json`` / ``write_marker_last``, defined by
``address_construction_v1.LocalObjectStore``) and mirrors every object into an R2
staging prefix through the existing, tested ``scripts/r2_verified_store.py``
``ObjectStore`` seam -- ``FilesystemStore`` for credential-free rehearsals and
the slice harness, ``S3Store`` for real R2.

Three properties make this transport-only, and all three are already true of the
store; nothing about the pipeline's semantics changes:

* **Content-addressed.** Construction keys are ``{class}/sha256/{digest}{suffix}``,
  so an object's name proves its bytes. That is what lets ``path()`` hydrate a
  missing object and verify it with no side table: the expected digest is IN the
  key it was asked for.
* **Create-only.** ``ensure_uploaded`` writes with ``If-None-Match: '*'``, reads
  the object back, verifies size and SHA-256, and re-HEADs. A byte-identical
  re-run is a no-op; differing content under the same key raises.
* **Deterministic keys.** The staging prefix is a pure function of
  ``request_sha256`` and the family, so a consumer DISCOVERS its objects by
  deriving keys from the markers it already carries. No LIST, no manifest, no
  directory object -- and therefore no listing cost and no listing race.

Fail-closed rules, all of them deliberate:

* a staged object that is absent, short, or whose bytes do not hash to the digest
  in its key ABORTS. There is no fallback to the artifact path -- a silent
  fallback is how a partial store becomes a wrong slice;
* a key that is not content-addressed cannot be hydrated as an object at all
  (``path()`` refuses it), because there would be nothing to verify it against.
  Markers, which are not content-addressed, travel through ``read_json`` and are
  verified against the store's own recorded ``sha256`` metadata; a staged marker
  with no such metadata aborts rather than being trusted;
* the staging prefix must be ``staging/global-v2/<64-lowercase-hex>/...``. That is
  the existing bucket convention AND what keeps ``.github/workflows/r2-cleanup.yml``
  able to guard and expire abandoned run prefixes (its phase-2 guard is literally
  ``^staging/global-v2/[0-9a-f]{64}/$``). A prefix outside that shape is refused
  here rather than becoming un-cleanable debris.

What this does NOT do, deliberately: row-group range reads. ``path()`` hydrates a
WHOLE object. That is sufficient for the increment because the map-side shuffle
made a fragment hold a complete set of cells and nothing else, so a bucket-range
reduce job fetches only the fragments in its own range instead of the whole
store. Range reads are step 2 of the design doc's sequencing and are tracked as a
follow-up.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import threading
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relative: str):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R2 = _load("construction_staging_r2_store", "scripts/r2_verified_store.py")

HEX = "0123456789abcdef"
# The bucket-root convention every construction-v1 staging tree lives under, and
# the one r2-cleanup.yml knows how to guard and expire.
STAGING_ROOT = "staging/global-v2"
# One extra segment below the run digest so a construction-v1 run's intermediate
# store is distinguishable from the older global-v2 pipelines' `immutable/...`
# subtrees under the same convention (r2-cleanup.yml phase 4 targets those by
# name).
STAGING_SEGMENT = "construction-v1"
FAMILIES = ("addresses", "places")


def is_canonical_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in HEX for character in value)
    )


def staging_prefix(request_sha256: str, family: str) -> str:
    """The staging root for one run and one family.

    Run-scoped, so concurrent or retried dispatches never collide and a resumed
    run finds its own objects already present -- which is how create-only plus
    content-addressing gives resume for free. Family-scoped below that so the two
    families never write into one another's create-only key space.
    """
    if not is_canonical_digest(request_sha256):
        raise ValueError(
            "construction-v1 staging requires a canonical lowercase-hex "
            "request_sha256; r2-cleanup.yml guards this prefix shape"
        )
    if family not in FAMILIES:
        raise ValueError(f"unknown construction-v1 family: {family!r}")
    return f"{STAGING_ROOT}/{request_sha256}/{STAGING_SEGMENT}/{family}"


def validate_staging_prefix(prefix: str) -> str:
    """Return ``prefix`` normalized, or raise if it is not a legal staging root.

    Legal means exactly what `staging_prefix` produces:
    ``staging/global-v2/<64-lowercase-hex>/construction-v1/<family>``. This exists
    because the docstring above is a claim and `StagedObjectStore` needs an
    enforcement — a prefix outside this shape writes objects that `r2-cleanup.yml`
    cannot target and therefore can never expire.
    """
    if not isinstance(prefix, str):
        raise ValueError("staging prefix must be a string")
    parts = PurePosixPath(prefix.strip("/")).parts
    if len(parts) != 5:
        raise ValueError(f"staging prefix is not a construction-v1 root: {prefix!r}")
    root, version, digest, segment, family = parts
    if f"{root}/{version}" != STAGING_ROOT or segment != STAGING_SEGMENT:
        raise ValueError(f"staging prefix is not a construction-v1 root: {prefix!r}")
    # Re-derive through the single legal producer, so the digest and family checks
    # can never drift from the ones `staging_prefix` applies.
    return staging_prefix(digest, family)


def content_addressed_digest(key: str) -> str | None:
    """The digest a construction store key asserts about its own bytes.

    Keys are ``{class}/sha256/{digest}{suffix}`` (``LocalObjectStore.put_content``).
    Returns None for anything else -- markers, most importantly -- so callers can
    refuse to hydrate what they could not verify.
    """
    parts = PurePosixPath(key).parts
    if len(parts) < 3 or parts[-2] != "sha256":
        return None
    name = parts[-1]
    digest = name.split(".", 1)[0]
    return digest if is_canonical_digest(digest) else None


class StagedObjectStore:
    """``LocalObjectStore`` surface, mirrored create-only into an R2 staging prefix.

    The local store is a CACHE, not the record: every object is published to
    staging as it is written, and any object a later phase asks for that is not
    in the local cache is hydrated from staging and verified before use. That is
    what lets each phase run with an EMPTY local store instead of downloading the
    whole map output.
    """

    def __init__(self, local: Any, store: Any, prefix: str):
        self.local = local
        self.store = store
        # Enforce the shape rather than merely document it. An object written under
        # any other prefix is one `r2-cleanup.yml`'s phase-2 guard
        # (`^staging/global-v2/[0-9a-f]{64}/$`) can never target, so it becomes
        # permanent debris. `staging_prefix` is the only legal producer, and this
        # re-derives from its own parse so a hand-built prefix cannot slip past.
        self.prefix = validate_staging_prefix(prefix)
        # These counters are read by a fail-closed gate, not just printed: both
        # slice-smoke jobs and the hosted finalize job assert
        # `staged_peak_resident_bytes < staged_bytes_hydrated` and
        # `staged_objects_released > 0` on them. Finalize's upload pass now runs
        # PUBLISH_CONCURRENCY threads through `path()` and `release()`, and
        # `x += 1` is not atomic under the GIL for a read-modify-write pair, so a
        # lost update would silently soften the very bound the gate checks.
        self._lock = threading.Lock()
        # Counters, not evidence of correctness -- but a run that hydrates nothing
        # when it should have is visible here, and the slice summary reports them.
        self.published = 0
        self.published_bytes = 0
        self.hydrated = 0
        self.hydrated_bytes = 0
        self.released = 0
        self.released_bytes = 0
        # Bytes of HYDRATED input resident in the local cache, and the high-water
        # mark. This is the number the transport exists to bound, so it is measured
        # rather than argued: a consumer whose peak equals its total hydrated bytes
        # is holding its whole fan-in and is not bounded by anything.
        self.resident_bytes = 0
        self.peak_resident_bytes = 0

    def _account_hydrated(self, size: int) -> None:
        with self._lock:
            self.hydrated += 1
            self.hydrated_bytes += size
            self.resident_bytes += size
            self.peak_resident_bytes = max(
                self.peak_resident_bytes, self.resident_bytes
            )

    def _account_released(self, size: int) -> None:
        with self._lock:
            self.released += 1
            self.released_bytes += size
            self.resident_bytes = max(0, self.resident_bytes - size)

    def _account_published(self, size: int) -> None:
        with self._lock:
            self.published += 1
            self.published_bytes += size

    @property
    def root(self) -> Path:
        return self.local.root

    def staging_key(self, key: str) -> str:
        relative = PurePosixPath(key)
        if not key or relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"object key escapes the staging prefix: {key!r}")
        return f"{self.prefix}/{key}"

    # -- writes ------------------------------------------------------------- #
    def _publish(self, key: str) -> dict[str, Any]:
        """Create-only publication of one already-written local object."""
        source = self.local.path(key)
        report = R2.ensure_uploaded(self.store, source, self.staging_key(key))
        expected = content_addressed_digest(key)
        if expected is not None and report["sha256"] != expected:
            # Unreachable via put_content (the key is built FROM the digest), so
            # this catches a caller that hand-built a key, not a transport fault.
            raise ValueError(
                f"staged object bytes do not hash to the digest in its key: {key}"
            )
        self._account_published(int(report["bytes"]))
        return report

    def put_content(self, source: Path, prefix: str, suffix: str) -> dict[str, Any]:
        identity = self.local.put_content(source, prefix, suffix)
        self._publish(identity["key"])
        return identity

    def write_marker_last(self, key: str, value: dict[str, Any]) -> None:
        # Local first, then staging, so the durable record is never ahead of the
        # local one. Both are create-only: a second write of DIFFERENT bytes under
        # the same marker key raises on either side.
        self.local.write_marker_last(key, value)
        self._publish(key)

    # -- reads -------------------------------------------------------------- #
    def path(self, key: str) -> Path:
        path = self.local.path(key)
        if path.is_file():
            return path
        digest = content_addressed_digest(key)
        if digest is None:
            raise ValueError(
                f"refusing to hydrate a key that is not content-addressed: {key}. "
                "There would be no digest to verify the fetched bytes against."
            )
        staging = self.staging_key(key)
        info = self.store.head(staging)
        if info is None:
            raise FileNotFoundError(
                f"staged object is absent: {staging}. The map phase's store now "
                "travels through R2 staging, so a missing object is a missing "
                "input -- aborting rather than continuing with partial data."
            )
        if info.sha256 is not None and info.sha256 != digest:
            raise ValueError(
                f"staged object metadata digest differs from its key: {staging}"
            )
        R2.verified_download(
            self.store,
            staging,
            path,
            expected_bytes=info.bytes,
            expected_sha256=digest,
        )
        self._account_hydrated(int(info.bytes))
        return path

    def release(self, key: str) -> None:
        """Drop a hydrated object from the LOCAL cache; staging keeps the record.

        This is what makes a batched consumer actually bounded. Without it, a phase
        that fetches N batches of packs ends holding all N -- which is how the plan
        phase silently hydrated the entire term store despite batching its reads.
        Safe by construction: the object is content-addressed and still in staging,
        so a later `path()` re-fetches and re-verifies it.

        Deliberately NOT present on `LocalObjectStore`: there the local directory IS
        the store, and callers reach it through `getattr(store, "release", None)` so
        a local-only run never deletes anything.
        """
        path = self.local.path(key)
        if content_addressed_digest(key) is None:
            # Markers are the durable record of completion within a phase; nothing
            # asks to evict one, and doing so would make a later read look like a
            # fresh task.
            raise ValueError(f"refusing to evict a non-content-addressed key: {key}")
        if path.is_file():
            size = path.stat().st_size
            self._account_released(size)
            path.unlink()

    def read_json(self, key: str) -> dict[str, Any] | None:
        local = self.local.read_json(key)
        if local is not None:
            return local
        staging = self.staging_key(key)
        info = self.store.head(staging)
        if info is None:
            # Definitively absent means "not written". Under create-only writes a
            # re-run is safe; a TRANSPORT error propagates instead of reading as
            # absence, which is the same fail-closed direction as
            # construction_v1_hosted._remote_marker_completed.
            return None
        if info.sha256 is None:
            raise ValueError(
                f"staged marker carries no sha256 metadata and cannot be verified: "
                f"{staging}"
            )
        destination = self.local.path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        R2.verified_download(
            self.store,
            staging,
            destination,
            expected_bytes=info.bytes,
            expected_sha256=info.sha256,
        )
        self._account_hydrated(int(info.bytes))
        return json.loads(destination.read_text())

    # -- reporting ---------------------------------------------------------- #
    def evidence(self) -> dict[str, Any]:
        return {
            "staging_prefix": self.prefix,
            "staged_objects_published": self.published,
            "staged_bytes_published": self.published_bytes,
            "staged_objects_hydrated": self.hydrated,
            "staged_bytes_hydrated": self.hydrated_bytes,
            "staged_objects_released": self.released,
            "staged_bytes_released": self.released_bytes,
            # The bound that matters: high-water mark of hydrated input resident on
            # this runner at once. A batched consumer keeps this far below
            # `staged_bytes_hydrated`; an eagerly-hydrating one makes them equal.
            "staged_peak_resident_bytes": self.peak_resident_bytes,
        }


def staging_backend(
    *,
    store_root: str | Path | None = None,
    bucket: str | None = None,
    endpoint_url: str | None = None,
) -> Any:
    """A ``FilesystemStore`` or ``S3Store`` for the staging tree.

    The filesystem backend is what keeps this whole path exercisable with no
    credentials -- the slice harness and the pytest suites use it, so the risky
    logic is covered offline rather than only on a credentialed dispatch.
    """
    if store_root is not None:
        if bucket or endpoint_url:
            raise ValueError(
                "staging takes either a filesystem root or a bucket+endpoint, "
                "not both"
            )
        return R2.FilesystemStore(Path(store_root))
    if not bucket or not endpoint_url:
        raise ValueError(
            "R2 staging requires a bucket and an endpoint URL (or a filesystem "
            "staging root for credential-free runs)"
        )
    # The persistent-client backend, not `S3Store`. Finalize hydrates its whole
    # published set through this store -- 65,751 objects for a planet address slice
    # -- and one `aws` process per object is 0.339 s of CPU each, i.e. 6.2 hours of
    # startup before a byte moves. See `r2_verified_store.s3_object_store`.
    return R2.s3_object_store(bucket, endpoint_url)
