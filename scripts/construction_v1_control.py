#!/usr/bin/env python3
"""Fail-closed admission contract for the Address + Places construction-v1 run.

This module deliberately contains no cloud client and cannot start a build.  It
creates the immutable review package consumed by the dormant hosted workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HEX64 = re.compile(r"^[0-9a-f]{64}$")
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")
RELEASE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")

# The Overture release the frozen readiness and scale evidence was GENERATED
# against. It is provenance, not a target: the evidence documents name this
# release's inventory, and they keep naming it after a build moves on.
ATTESTED_RELEASE = "2026-06-17.0"

# The release a build targets when the operator names none.
DEFAULT_RELEASE = "2026-07-22.0"

# WHY THE ATTESTED RELEASE AND THE BUILD RELEASE ARE ALLOWED TO DIVERGE
#
# Every one of the twelve pins below used to be an exact-equality check against a
# single release, which made a release move a full re-attestation: regenerate
# twelve projections and censuses, seven task runs, and the functional rehearsal,
# on free public runners, to re-derive evidence about a producer that did not
# change. That is ceremony priced far above what it protects.
#
# What the readiness and scale evidence actually characterize is a PRODUCER
# running against a SCHEMA at a SCALE. So those three are what still bind:
#
#   * `spec_sha256`, `readiness_file_sha256`, `scale_evidence_sha256` and
#     `readiness.ready` are unchanged exact checks. The evidence has to be the
#     reviewed evidence, and it has to say ready.
#   * `schema_fingerprint_sha256` is an unchanged EXACT check against the LIVE
#     inventory. A schema change means the evidence describes a different
#     producer contract, and no envelope rescues that -- re-attest.
#   * `attested_scale` is the new part, and it is one-directional: the live
#     inventory may be SMALLER than the release the evidence was measured on,
#     never larger. Evidence that a producer survived N records and T tasks is
#     evidence about anything under N and T; it says nothing about more.
#
# `inventory_sha256` therefore splits in two. `attested_inventory_sha256` is the
# inventory the readiness document names, and stays pinned to ATTESTED_RELEASE.
# `inventory_sha256` is the live inventory this build will actually read, and
# moves with the release. When they are equal the gate is exactly what it was;
# when they differ the envelope above is what admits the difference.
#
# The two inventories are separate FILES, and that is load-bearing rather than
# tidy. Each frozen evidence spec names its inventory by path and carries its own
# `release`, and the spec is sha256-pinned -- so overwriting the attested path
# with a newer release would leave a frozen artifact pointing at a file that no
# longer holds what it attests, and would strand the readiness validators, which
# require `inventory.release == spec.release` to regenerate anything at all.
# `test_the_frozen_specs_still_point_at_the_inventory_they_attest` holds that
# line.
#
# This is a NARROWING of the release axis, not a removal of the gate. A bigger
# release, a reshaped schema, a non-ready readiness, or an inventory whose bytes
# differ from the committed file all still fail closed.

FAMILIES = {
    "addresses": {
        "inventory": "benchmarks/address-construction-v1-data/inventory/addresses-2026-07-22.0.json",
        "attested_inventory": "benchmarks/address-construction-v1-data/inventory/addresses.json",
        "inventory_file_sha256": "8ab088b39d198097be5dc301d91845360642efe63708d5f6bbed060a3b456d6a",
        "inventory_sha256": "f153d7d13e54554032185d807c7b57ce617a715e3894c7f9f269b073339f4e92",
        "attested_inventory_sha256": "6a306fc9937dac82602dbc5233952c1f74fdb0f7467ad4cc38dcc559dfc9d34e",
        "attested_scale": {"records": 473_576_753, "selected_uncompressed_bytes": 33_172_987_981, "map_tasks": 127},
        # Per-task bounds come from the spec's OWN declared caps, by path, not
        # from constants retyped here -- the spec is already verified byte for
        # byte above, so it is the strongest reference available.
        "task_caps": {
            "max_task_rows": ("acceptance_gates", "input_rows_hard_cap"),
            "max_task_row_groups": ("acceptance_gates", "row_groups_hard_cap"),
            "max_task_selected_compressed_bytes": ("acceptance_gates", "selected_compressed_bytes_hard_cap"),
            "max_task_selected_uncompressed_bytes": ("acceptance_gates", "selected_uncompressed_bytes_hard_cap"),
        },
        "schema_fingerprint_sha256": "05260dc6878478fe750a82ad3fb9ddd2fdffcda3f25c00f950acfccca132d7e0",
        "spec": "benchmarks/address-construction-v1-evidence-spec-v3.json",
        "spec_sha256": "130207f3debde346cc9c1178e5038e2257e883ccd46c5826d1a5ae22c2583af9",
        "readiness": "benchmarks/address-construction-v1-data/evidence/readiness-final-v3.json",
        "readiness_file_sha256": "f3a11863637151eaf255b79993737e3b595a3674f742315bd852691c360e118e",
        "scale_evidence_sha256": "dce535350bcb97b1871fa81e5a3e9c863b9b0ce8969175f743705237a9d980ea",
        # The LIVE inventory, not the readiness document. Both carried a
        # byte-identical task list while the build sat on ATTESTED_RELEASE, so
        # this is a no-op there -- and it is the difference between correct and
        # corrupt once the release moves, because readiness names the attested
        # release's row-group ranges and etags forever.
        "task_path": ("plan", "tasks"),
        "construction": "address-construction-v1",
    },
    # V4 EVIDENCE GENERATION, 2026-08-03. Prior generations remain on disk as
    # true attestations of their own runs. V4 is fully fresh after the
    # planet-category audit added bounded hospital/opera prominence and made
    # observed ATM/bank/laundry spellings dispositive commodity classes. All
    # twelve projections/censuses, seven task runs, and the functional rehearsal
    # were regenerated; readiness is fail-closed and green.
    "places": {
        "inventory": "benchmarks/places-construction-v1-data/inventory/places-2026-07-22.0.json",
        "attested_inventory": "benchmarks/places-construction-v1-data/inventory/places.json",
        "inventory_file_sha256": "f40964c7bb43c234372ff8d2cfe14d5b4f09bfbf7297b03e28e6d9e395a39298",
        "inventory_sha256": "0a89c944623e0f524a3616fd1f4597a6619e1ec94d70bbd00854069c86ee072d",
        "attested_inventory_sha256": "9ea4eff665766c3c1146ee7baed413fcf76f097e1724d795c77baabe7dff1795",
        "attested_scale": {"records": 75_642_289, "selected_uncompressed_bytes": 10_604_105_681, "map_tasks": 89},
        # The places inventory records no per-task compressed size, so the spec's
        # compressed cap has nothing to check and is deliberately absent rather
        # than checked against a None.
        "task_caps": {
            "max_task_rows": ("acceptance_gates", "input", "rows_hard_cap"),
            "max_task_row_groups": ("acceptance_gates", "input", "row_groups_hard_cap"),
            "max_task_selected_uncompressed_bytes": ("acceptance_gates", "input", "selected_uncompressed_bytes_hard_cap"),
        },
        "schema_fingerprint_sha256": "31809dbadf976783e7863d2694d2cfe870f53665ef81d007076144c55bf64e67",
        "spec": "benchmarks/places-construction-v1-evidence-spec-v4.json",
        "spec_sha256": "77bce6209c9c98ee4243167982fe11b13f7702c042e48bfad90daa6b3b26bfed",
        "readiness": "benchmarks/places-construction-v1-data/evidence/readiness-v4.json",
        "readiness_file_sha256": "312020845eb32dffdc49f6db269e4a45d887c20442734bdf763bfb9453bbeaec",
        "scale_evidence_sha256": "a6d3de90cda567d405c56231070324babc4b9e53715e14cdc136d99d215f2527",
        "task_path": ("map_plan", "tasks"),
        "construction": "places-construction-v1",
    },
}

# The scale dimensions the envelope compares, and where each family's live
# inventory records them. `map_tasks` is the one that actually moves the work
# shape -- it is the matrix width every later phase provisions against.
SCALE_PATHS = {
    "records": ("totals", "records"),
    "selected_uncompressed_bytes": ("totals", "selected_uncompressed_bytes"),
}

# Per-task dimensions, and the task field each reads. `rows` for addresses and
# `expected_input_records` for places are the same quantity under two names,
# because the two inventories were written by different generators.
TASK_SCALE_FIELDS = {
    "max_task_rows": ("rows", "expected_input_records"),
    "max_task_selected_compressed_bytes": ("selected_compressed_bytes",),
    "max_task_selected_uncompressed_bytes": ("selected_uncompressed_bytes",),
    "max_task_row_groups": ("row_groups",),
}

VERSIONS = {
    "python": "3.12.12",
    "duckdb": "1.5.1",
    "numpy": "2.3.5",
    "pyarrow": "25.0.0",
    "unicodedata2": "17.0.0",
    "rustc": "1.97.1 (8bab26f4f 2026-07-14)",
    "cargo": "1.97.1 (c980f4866 2026-06-30)",
    "arrow_ipc": "construction-v1-arrow-ipc-v1",
    "shuffle_parquet": "construction-v1-shuffle-parquet-v1",
    "directory": "construction-v1-directory-v1",
    "proof": "construction-v1-proof-v1",
    "serving": "construction-v1-serving-v1",
}

CAPS = {
    "max_parallel": 4,
    "max_total_runner_minutes": 40_000,
    "prior_runner_minutes": 0,
    "max_cost_usd": "1200.00",
    "max_reducers_per_family": 128,
    # Raised from 100_000 after projecting what a planet finalize actually charges.
    #
    # The unit cost is fixed by the publication primitives, not chosen. A FIRST
    # attempt costs 3 operations per published object (create-only put + per-upload
    # HEAD in construction_v1_remote.publish_exact_set, plus one streaming read in
    # verify_whole_slice_once), 2 for the slice (marker put, marker HEAD), and the
    # exact-prefix listing. A RESUMED finalize costs 4 and 3: the create-only put
    # still charges its attempt, raises ConflictError, and the byte-exactness check
    # on that path streams the already-published object a second time. Resuming is a
    # first-class path -- create-only publication exists so an interrupted finalize
    # can be re-run -- so the BUDGET is the retry. Pricing the first attempt only
    # would let the gate pass a run whose retry aborts inside Budget.charge, which is
    # the failure being removed.
    #
    # The LISTING is paginated and is priced by page: ceil(N / 1000) requests, from
    # r2_verified_store.LIST_PAGE_KEYS. It used to be counted as one operation, which
    # understated every planet slice by 60-90 requests -- immaterial in dollars, but
    # the whole point of this gate is that the projection equals what the phase
    # charges. So the retry cost is
    #
    #     ops = 4N + 3 + ceil(N / 1000)
    #
    # and the first attempt 3N + 2 + ceil(N / 1000).
    #
    # Planet N, per family, from committed artifacts:
    #
    #   places       16,888 routed serving objects (scripts/places_partition_plan
    #                _v1.json generated_from.partitions, one per partition)
    #              +  4,096 head shards (1 << DEFAULT_HEAD_SHARD_BITS)
    #              +      1 head routing manifest (published since PR #169)
    #              +      2 slice/family manifests
    #              +  2 objects (pack + row-group directory) per per-record pack,
    #                 and map emits one pack per PRESENT shuffle bucket per task:
    #                 89 map tasks (places inventory map_plan.task_count) x up to
    #                 256 buckets. MEASURED on release 2026-06-17.0, the four
    #                 planet tasks inside source object 0 occupy 107/149/160/109
    #                 buckets, so ~131/task => ~23,300 objects; the structural
    #                 bound is 89 x 256 x 2 = 45,568.
    #                 => N 44,305 measured (132,962 first / 177,268 retry)
    #                    N 66,555 bound    (199,734 first / 266,290 retry)
    #
    #   addresses      725 serving objects (per-country bisection estimate)
    #              +      2 manifests, no head phase
    #              +  127 tasks x <=256 buckets x 2 = <=65,024 record objects
    #                 => N <= 65,751 (197,321 first / 263,073 retry)
    #
    # Both families are therefore OVER 100_000 at planet scale on a FIRST attempt,
    # and the old cap tripped inside finalize's running counter -- specifically
    # inside verify_whole_slice_once, after every object was already published, so
    # the run produced no verification evidence and no result file.
    #
    # WHAT THIS CAP DOES **NOT** COVER, because the projection matching the charge is
    # the whole point of the gate and the claim has to be exact. It counts operations
    # against the PUBLISHED SLICE PREFIX only. Two real classes of request are charged
    # nothing at all:
    #
    #   * STAGING HYDRATION. Every `StagedObjectStore.path()` is one GET whose
    #     response supplies the length and SHA metadata while its body is hashed
    #     against the content-addressed key. Finalize hydrates each published object
    #     TWICE (admission, then the upload pass) -- so ~2 uncharged operations per
    #     published object, ~132,000 for a planet address slice against a budgeted
    #     263,073. `Budget`
    #     wraps only the publication remote; `StagedObjectStore` and
    #     `r2_verified_store` charge nothing.
    #   * SDK RETRIES. `Boto3Store` retries a transient 5xx up to MAX_ATTEMPTS, and the
    #     budget charges the logical operation once. MEASURED by injecting two 500s: 6
    #     real HTTP requests against 4 charged.
    #
    # So the REAL request count for a planet finalize is roughly 1.5x what this cap
    # bounds, and 346,182/400,000 is a margin computed against the publication part of
    # the traffic. That is a fail-OPEN inaccuracy, not a correctness hole: R2 class-A
    # operations are ~$4.50/million, so even 2x the ceiling is under $4, and the byte
    # caps are what bound a runaway's cost. Fixing it means giving staging its own
    # counter -- tracked in docs/plans/2026-07-24-construction-v1-follow-ups.md -- not
    # inflating this number, which is calibrated against what `Budget.charge` actually
    # sees.
    #
    # 400_000 is sized off the RETRY-INCLUSIVE STRUCTURAL CEILING, not the
    # measurement: at the inventory gate's max_tasks 128 (both families' plan
    # limits), all 256 buckets occupied in every task, 4,096 head shards + manifest
    # and the committed 16,888 partitions, places is 86,523 objects = 346,182
    # operations on a resumed finalize. 400_000 clears that by 1.16x, which is
    # 53,818 spare operations = room for 13,454 more partitions than the committed
    # plan; it is ~2.3x the measured planet places retry. The margin is deliberately
    # modest because the cap no longer stands alone: predict-reduce and plan-reduce
    # both refuse a run whose projection exceeds it, so an inventory that outgrows
    # this cap fails in the dry run instead of at publication. R2 charges ~$4.50 per
    # million class-A operations, so the whole budget is under $2 either way; the
    # byte caps below, not this one, are what bound a runaway's cost.
    "max_remote_operations": 400_000,
    "max_remote_write_bytes": 1_000_000_000_000,
    "max_cleanup_objects": 20_000,
    "max_cleanup_bytes": 250_000_000_000,
    "map_wall_minutes": {"addresses": 60, "places": 45},
    "reduce_wall_minutes": 90,
}


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode() + b"\n"


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_json(relative: str) -> dict[str, Any]:
    return json.loads((ROOT / relative).read_text())


def live_scale(contract: dict[str, Any], inventory: dict[str, Any]) -> dict[str, int]:
    """The scale dimensions the envelope compares, read off the live inventory.

    TOTALS ARE NOT ENOUGH, and this is the subtle half. Every hard cap in the
    evidence specs is PER TASK -- rows, selected bytes, row groups -- and a
    release can shrink in total while pushing one task past a cap the evidence
    never covered. The address planner's own gate is wider than the attested
    per-task byte cap, so that is a reachable state, not a hypothetical. The
    per-task maxima below are what make the envelope bind where the caps bind.
    """
    scale = {
        field: inventory.get(section, {}).get(key)
        for field, (section, key) in SCALE_PATHS.items()
    }
    section, key = contract["task_path"]
    scale["map_tasks"] = inventory.get(section, {}).get("task_count")
    tasks = inventory.get(section, {}).get(key) or []
    for field, names in TASK_SCALE_FIELDS.items():
        values = [
            task[name] for task in tasks for name in names
            if isinstance(task.get(name), int)
        ]
        scale[field] = max(values) if values else None
    return scale


def scale_envelope_errors(name: str, contract: dict[str, Any], inventory: dict[str, Any]) -> list[str]:
    """Admit a live inventory no larger than the one the evidence was measured on.

    One-directional by construction: `<=` in every dimension. Evidence that a
    producer survived N records is evidence about anything under N and nothing
    about more, so a release that GROWS past the attested scale fails closed and
    demands fresh evidence -- which is the case the ceremony was actually for.
    """
    errors: list[str] = []
    actual = live_scale(contract, inventory)
    for field, attested in sorted(contract["attested_scale"].items()):
        measured = actual.get(field)
        if not isinstance(measured, int):
            errors.append(f"{name} live inventory does not report {field}")
        elif measured > attested:
            errors.append(
                f"{name} {field} {measured:,} exceeds the attested {attested:,}; "
                f"regenerate the evidence against this release"
            )
    # Per-task dimensions are bound by the SPEC'S DECLARED CAP, not by the
    # attested release's incidental maximum. The evidence asserts "the producer
    # stays inside these caps"; the largest task that happened to occur is an
    # observation, not a bound, and holding a release to it would refuse a build
    # over a nineteen-row difference -- a false refusal, and false refusals are
    # how gates get switched off. The cap is also TIGHTER than the address
    # planner's own gate (350 MB against 400 MB), so this is what stops a
    # within-totals release from producing a task the evidence never covered.
    spec = read_json(contract["spec"])
    for field, path in sorted(contract["task_caps"].items()):
        cap: Any = spec
        for key in path:
            cap = cap.get(key, {}) if isinstance(cap, dict) else {}
        measured = actual.get(field)
        if not isinstance(cap, int):
            errors.append(f"{name} spec declares no cap at {'.'.join(path)}")
        elif not isinstance(measured, int):
            errors.append(f"{name} live inventory does not report {field}")
        elif measured > cap:
            errors.append(
                f"{name} {field} {measured:,} exceeds the spec's declared "
                f"{'.'.join(path)} {cap:,}"
            )
    return errors


def family_status(name: str, contract: dict[str, Any], release: str) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    for field in ("inventory", "spec", "readiness"):
        actual = sha256_file(ROOT / contract[field])
        expected = contract[f"{field}_file_sha256"] if field in ("inventory", "readiness") else contract[f"{field}_sha256"]
        if actual != expected:
            errors.append(f"{name} {field} file SHA-256 differs: {actual}")
    inventory = read_json(contract["inventory"])
    readiness = read_json(contract["readiness"])
    readiness_identity = {**readiness.get("checks", {}), **readiness}
    readiness_identity = {
        **readiness.get("checks", {}).get("canonical_inventory_identity", {}),
        **readiness_identity,
    }
    if inventory.get("release") != release:
        errors.append(f"{name} inventory is release {inventory.get('release')}, not {release}")
    if inventory.get("inventory_sha256") != contract["inventory_sha256"]:
        errors.append(f"{name} inventory content identity differs")
    schema = inventory.get("schema_contract", {}).get("fingerprint_sha256")
    if schema != contract["schema_fingerprint_sha256"]:
        errors.append(f"{name} schema fingerprint differs")
    # The readiness document names the ATTESTED inventory and always will; the
    # envelope is what licenses a live inventory that differs from it.
    for field, expected in (
        ("inventory_sha256", contract["attested_inventory_sha256"]),
        ("evidence_spec_sha256", contract["spec_sha256"]),
        ("scale_evidence_sha256", contract["scale_evidence_sha256"]),
    ):
        if readiness_identity.get(field) != expected:
            errors.append(f"{name} readiness {field} differs")
    if contract["inventory_sha256"] != contract["attested_inventory_sha256"]:
        errors.extend(scale_envelope_errors(name, contract, inventory))
    if readiness.get("ready") is not True:
        reasons = readiness.get("reasons") or readiness.get("blockers") or ["readiness is false"]
        errors.extend(f"{name} readiness: {reason}" for reason in reasons)
    return readiness, errors


def map_tasks(name: str, contract: dict[str, Any], readiness: dict[str, Any]) -> list[dict[str, Any]]:
    inventory = read_json(contract["inventory"])
    section, key = contract["task_path"]
    tasks = inventory[section][key]
    matrix = []
    for task in tasks:
        digest = task.get("task_digest_sha256", task.get("task_digest"))
        source_digest = task.get("source_digest_sha256", task.get("source_digest"))
        matrix.append({
            "family": name,
            "task_id": f"{name}-map-{task['index']:03d}",
            "task_index": task["index"],
            "task_digest": digest,
            "source_digest": source_digest,
            "expected_input_records": task.get("rows", task.get("expected_input_records")),
            "selected_uncompressed_bytes": task["selected_uncompressed_bytes"],
            "ranges": task["ranges"],
        })
    return matrix


def confirmation(request_sha256: str, caps: dict[str, Any]) -> str:
    return (
        f"EXECUTE_CONSTRUCTION_V1::{request_sha256}"
        f"::MODE=execute::MAX_PARALLEL={caps['max_parallel']}"
        f"::MAX_TOTAL_RUNNER_MINUTES={caps['max_total_runner_minutes']}"
        f"::PRIOR_RUNNER_MINUTES={caps['prior_runner_minutes']}"
        f"::MAX_COST_USD={caps['max_cost_usd']}"
    )


def prepare(values: argparse.Namespace) -> tuple[dict[str, Any], bool]:
    blockers: list[str] = []
    ids = [values.request_id, values.build_id, values.slice_id, values.staging_id]
    if len(set(ids)) != 4 or any(not SAFE_ID.fullmatch(value) for value in ids):
        blockers.append("request/build/slice/staging IDs must be four distinct canonical fresh IDs")
    if not HEX40.fullmatch(values.producer_commit):
        blockers.append("producer commit must be an exact lowercase 40-hex commit")
    if not values.legacy_core_version or not values.legacy_core_manifest_sha256:
        blockers.append("exact legacy core version and release-manifest SHA-256 are required")
    elif not HEX64.fullmatch(values.legacy_core_manifest_sha256):
        blockers.append("legacy core release-manifest SHA-256 is not canonical")

    # Retained runner minutes from earlier attempts. A fresh resume dispatch
    # binds them into both the request and the typed confirmation so the honest
    # prior total cannot be silently reset to zero.
    prior_runner_minutes = int(getattr(values, "prior_runner_minutes", 0) or 0)
    if prior_runner_minutes < 0:
        blockers.append("prior runner minutes must be a non-negative integer")
    caps = {**CAPS, "prior_runner_minutes": prior_runner_minutes}

    release = getattr(values, "release", None) or DEFAULT_RELEASE
    if not RELEASE_RE.fullmatch(release):
        blockers.append("release must use YYYY-MM-DD.N")

    readiness: dict[str, Any] = {}
    matrices: dict[str, list[dict[str, Any]]] = {}
    family_contracts: dict[str, Any] = {}
    for name, base in FAMILIES.items():
        status, errors = family_status(name, base, release)
        readiness[name] = {"ready": status.get("ready") is True and not errors, "file": base["readiness"], "file_sha256": base["readiness_file_sha256"]}
        blockers.extend(errors)
        matrices[name] = map_tasks(name, base, status)
        family_contracts[name] = {key: value for key, value in base.items() if key != "task_path"}

    request: dict[str, Any] | None = None
    request_sha: str | None = None
    typed: str | None = None
    if values.legacy_core_version and values.legacy_core_manifest_sha256 and HEX40.fullmatch(values.producer_commit):
        request = {
            "schema": "overture-construction-v1-request-v1",
            "mode": "execute",
            "identity": dict(zip(("request_id", "build_id", "slice_id", "staging_id"), ids)),
            "producer_commit": values.producer_commit,
            "release": release,
            # What the frozen evidence was measured on, and the rule that lets
            # this build run on a different release. Recorded in the request so
            # the divergence is visible to a reviewer rather than implicit in a
            # constant, and hashed into the request identity so moving the
            # release mints a fresh staging namespace.
            "attestation": {
                "attested_release": ATTESTED_RELEASE,
                "envelope": "exact schema fingerprint; live inventory scale at or below the attested scale",
            },
            "lineage": {"genesis": True, "generation": 1, "predecessor": None},
            "legacy_core": {
                "version": values.legacy_core_version,
                "release": release,
                "manifest_key": f"{values.legacy_core_version}/release-manifest.json",
                "manifest_sha256": values.legacy_core_manifest_sha256,
                "access": "read-only-head-and-range-get",
            },
            "families": family_contracts,
            "versions": {**VERSIONS, "cargo_lock_sha256": sha256_file(ROOT / "crates/Cargo.lock"), "address_source_sha256": sha256_file(ROOT / "scripts/address_construction_v1.py"), "places_source_sha256": sha256_file(ROOT / "scripts/places_construction_v1.py")},
            "caps": caps,
            "publication": {"production_writes": False, "non_promoting_slice": True, "preview_only": True},
        }
        # Avoid a self-referential fixed point: namespace binds the independently hashed request core.
        namespace_binding = sha256_bytes(canonical(request))
        root = f"construction-v1/{namespace_binding}"
        request["namespaces"] = {
            "binding_sha256": namespace_binding,
            "immutable_root": root,
            "staging": f"{root}/staging/{values.staging_id}/",
            "content": f"{root}/content/sha256/",
            "markers": f"{root}/markers/",
            "slice": f"{root}/slice/{values.slice_id}/",
            "preview": f"{root}/preview/{values.slice_id}/",
            "forbidden": ["catalog.json", "v2/catalog.json", "v2/releases/"],
        }
        request_sha = sha256_bytes(canonical(request))
        typed = confirmation(request_sha, caps)

    projected_minutes = 30 + len(matrices["addresses"]) * 60 + len(matrices["places"]) * 45 + 2 * CAPS["max_reducers_per_family"] * CAPS["reduce_wall_minutes"] + 300
    if prior_runner_minutes + projected_minutes > CAPS["max_total_runner_minutes"]:
        blockers.append("prior plus projected runner minutes exceed the admitted cap")
    report = {
        "schema": "overture-construction-v1-review-package-v1",
        "admitted": not blockers and request is not None and all(item["ready"] for item in readiness.values()),
        "blockers": blockers,
        "request_sha256": request_sha,
        "typed_confirmation": typed,
        "request": request,
        "readiness": readiness,
        "map_matrices": matrices,
        "reducer_matrices": {
            name: {"derivation": "adaptive-genesis-plan-v1", "replaceable": True, "maximum_entries": CAPS["max_reducers_per_family"], "admitted_marker_set": [task["task_id"] for task in matrix]}
            for name, matrix in matrices.items()
        },
        "cost": {"projected_runner_minutes_upper_bound": projected_minutes, "prior_runner_minutes": prior_runner_minutes, "max_total_runner_minutes": CAPS["max_total_runner_minutes"], "max_cost_usd": CAPS["max_cost_usd"]},
        "next_action": "Satisfy every blocker, rerun prepare, review the canonical request, then dispatch once with its exact hash and typed confirmation.",
    }
    return report, report["admitted"]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for name in ("request-id", "build-id", "slice-id", "staging-id", "producer-commit"):
        prep.add_argument(f"--{name}", required=True)
    prep.add_argument("--legacy-core-version")
    prep.add_argument("--legacy-core-manifest-sha256")
    prep.add_argument(
        "--release",
        default=DEFAULT_RELEASE,
        help=(
            "Overture release to build. Admitted while its inventory keeps the "
            f"attested schema and stays within the scale measured on {ATTESTED_RELEASE}."
        ),
    )
    prep.add_argument("--prior-runner-minutes", type=int, default=0)
    prep.add_argument("--output", type=Path, required=True)
    admit = sub.add_parser("admit-dispatch")
    admit.add_argument("--request", type=Path, required=True)
    admit.add_argument("--request-sha256", required=True)
    admit.add_argument("--confirmation", required=True)
    admit.add_argument("--run-attempt", type=int, required=True)
    admit.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    if args.command == "admit-dispatch":
        request = json.loads(args.request.read_text())
        identity = request.get("identity", {})
        core = request.get("legacy_core", {})
        regenerated, admitted = prepare(argparse.Namespace(
            request_id=identity.get("request_id", ""), build_id=identity.get("build_id", ""),
            slice_id=identity.get("slice_id", ""), staging_id=identity.get("staging_id", ""),
            producer_commit=request.get("producer_commit", ""), legacy_core_version=core.get("version"),
            legacy_core_manifest_sha256=core.get("manifest_sha256"),
            prior_runner_minutes=request.get("caps", {}).get("prior_runner_minutes", 0),
            # Re-derive against the release the dispatched request NAMES. A
            # request claiming a release whose committed inventory does not match
            # it fails the byte-for-byte comparison below, so this cannot be used
            # to smuggle in an unattested release -- it only lets the gate check
            # the request the operator actually submitted.
            release=request.get("release"),
        ))
        actual_sha = sha256_bytes(canonical(request))
        if args.run_attempt != 1:
            raise SystemExit("run_attempt must be exactly 1; create a fresh request for every retry")
        if request != regenerated["request"] or actual_sha != args.request_sha256:
            raise SystemExit("dispatch request differs from the canonical reviewed request")
        if args.confirmation != regenerated["typed_confirmation"]:
            raise SystemExit("typed confirmation differs")
        if not admitted:
            raise SystemExit("readiness or admission failed: " + "; ".join(regenerated["blockers"]))
        if args.github_output:
            def compact(family):
                return {"include": [{"task_id": item["task_id"], "task_index": item["task_index"]} for item in regenerated["map_matrices"][family]]}
            with args.github_output.open("a") as output:
                output.write(f"address_matrix={json.dumps(compact('addresses'), separators=(',', ':'))}\n")
                output.write(f"places_matrix={json.dumps(compact('places'), separators=(',', ':'))}\n")
        print(json.dumps({"admitted": True, "request_sha256": actual_sha}, sort_keys=True))
        return 0
    report, admitted = prepare(args)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(canonical(report))
    print(json.dumps({"admitted": admitted, "request_sha256": report["request_sha256"], "typed_confirmation": report["typed_confirmation"], "output": str(args.output)}, sort_keys=True))
    return 0 if admitted else 1


if __name__ == "__main__":
    sys.exit(main())
