#!/usr/bin/env python3
"""Plan and guard paused-v2 cleanup and single-current-v1 retention.

The destructive work remains in GitHub workflows.  This module owns the parts
that are easy to get subtly wrong in shell:

* validate the live v1 catalog and reduce it to exactly its current generation;
* classify only whole, known R2 prefixes (never partial object-key fragments);
* validate every live v2 catalog/release reference before retiring that chain;
* enforce the full rollback hold for every live dispatch;
* distinguish bulky construction/staging data from retained run evidence; and
* compare-and-swap the v1 catalog, with a durable backup and exact readback.

No command in this module deletes an R2 object.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import finalize_rebuild as finalize
import prune_catalog
import v2_retention_guard


ROLLBACK_HOLD_NOT_BEFORE = datetime(2026, 9, 9, 14, 4, 19, tzinfo=timezone.utc)
V1_PREDECESSOR_OVERLAP = timedelta(days=7)
VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")
VERSION_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}\.\d+)/$")
SLICE_PREFIX_RE = re.compile(r"^slice-(\d{4}-\d{2}-\d{2}\.\d+)/$")
CONSTRUCTION_CHILD_RE = re.compile(r"^construction-v1/[0-9a-f]{64}/$")
STAGING_CHILD_RE = re.compile(r"^staging/global-v2/[0-9a-f]{64}/$")
CONSTRUCTION_DATA_PREFIX_RE = re.compile(
    r"^construction-v1/[0-9a-f]{64}/slice/[^/]+/families/"
    r"(?:addresses|places)/(?:objects|positions|records)/$"
)
CONSTRUCTION_DATA_KEY_RE = re.compile(
    r"^(construction-v1/[0-9a-f]{64}/slice/[^/]+/families/"
    r"(?:addresses|places)/(?:objects|positions|records)/).+$"
)
CONSTRUCTION_EVIDENCE_KEY_RE = re.compile(
    r"^construction-v1/[0-9a-f]{64}/(?:"
    r"markers/.+|"
    r"slice/[^/]+/families/(?:addresses|places)/"
    r"(?:family-manifest|slice-manifest)\.json)"
    r"$"
)
STAGING_DATA_PREFIX_RE = re.compile(
    r"^staging/global-v2/[0-9a-f]{64}/"
    r"immutable/map/addresses/objects/$"
)
STAGING_DATA_KEY_RE = re.compile(
    r"^(staging/global-v2/[0-9a-f]{64}/"
    r"immutable/map/addresses/objects/).+$"
)


class DecommissionError(RuntimeError):
    """A fail-closed decommission invariant was violated."""


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DecommissionError(f"invalid timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        raise DecommissionError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def enforce_rollback_hold(*, now: datetime, dry_run: bool) -> None:
    """Refuse every live run until the full rollback hold has elapsed."""
    if now.tzinfo is None:
        raise DecommissionError("current time must be timezone-aware")
    now = now.astimezone(timezone.utc)
    if not dry_run and now < ROLLBACK_HOLD_NOT_BEFORE:
        raise DecommissionError(
            "live deletion is inside the rollback hold; wait until "
            f"{ROLLBACK_HOLD_NOT_BEFORE.isoformat()}"
        )


def _load_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise DecommissionError(f"cannot read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DecommissionError(f"{label} must be a JSON object")
    return value


def _write_object(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _object_fingerprint(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _listing_prefixes(listing: dict[str, Any], label: str) -> set[str]:
    raw = listing.get("CommonPrefixes", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise DecommissionError(f"{label} CommonPrefixes must be an array")
    prefixes: set[str] = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("Prefix"), str):
            raise DecommissionError(f"{label} contains an invalid CommonPrefixes entry")
        prefix = item["Prefix"]
        if not prefix.endswith("/") or prefix in prefixes:
            raise DecommissionError(f"{label} contains an invalid or duplicate prefix")
        prefixes.add(prefix)
    return prefixes


def _refuse_direct_children(listing: dict[str, Any], label: str) -> None:
    contents = listing.get("Contents", [])
    if contents is None:
        contents = []
    if not isinstance(contents, list):
        raise DecommissionError(f"{label} Contents must be an array")
    if contents:
        raise DecommissionError(
            f"{label} contains direct objects outside its per-run prefixes"
        )


def _listing_objects(listing: dict[str, Any], label: str) -> list[dict[str, Any]]:
    """Return a canonical complete ``(key, size, etag)`` recursive inventory."""
    if listing.get("IsTruncated") is True or listing.get("NextContinuationToken"):
        raise DecommissionError(f"{label} is truncated")
    raw = listing.get("Contents", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise DecommissionError(f"{label} Contents must be an array")

    objects: list[dict[str, Any]] = []
    keys: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise DecommissionError(f"{label} contains a non-object entry")
        key = item.get("Key")
        size = item.get("Size")
        etag = item.get("ETag")
        if not isinstance(key, str) or not key or key in keys:
            raise DecommissionError(f"{label} contains an invalid or duplicate key")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DecommissionError(f"{label} contains an invalid object size")
        if not isinstance(etag, str) or not etag.strip('"'):
            raise DecommissionError(f"{label} contains an invalid ETag")
        keys.add(key)
        objects.append({"key": key, "size": size, "etag": etag.strip('"')})
    return sorted(objects, key=lambda item: item["key"])


def _safe_object_key(key: str) -> bool:
    return not key.startswith("/") and all(
        segment not in ("", ".", "..") for segment in key.split("/")
    )


def _classify_run_data_prefixes(
    *,
    objects: list[dict[str, Any]],
    construction_roots: set[str],
    staging_roots: set[str],
) -> tuple[list[str], list[str]]:
    """Return exact bulky subtrees while retaining compact run evidence.

    Construction markers and family/slice manifests stay in their original
    immutable namespaces.  Global-v2 inventories, reports, manifests, and
    completion markers likewise stay in place.  Any unfamiliar construction
    layout fails closed instead of being swept up by a whole-run delete.
    """
    construction_data: set[str] = set()
    staging_data: set[str] = set()
    for item in objects:
        key = item["key"]
        if not _safe_object_key(key):
            raise DecommissionError("full bucket listing contains an unsafe object key")

        construction_root = next(
            (root for root in construction_roots if key.startswith(root)), None
        )
        if construction_root is not None:
            match = CONSTRUCTION_DATA_KEY_RE.fullmatch(key)
            if match is not None:
                prefix = match.group(1)
                if not prefix.startswith(construction_root):
                    raise DecommissionError("construction data escaped its run prefix")
                construction_data.add(prefix)
            elif CONSTRUCTION_EVIDENCE_KEY_RE.fullmatch(key) is None:
                raise DecommissionError(
                    "construction-v1 contains an unfamiliar object outside the "
                    "reviewed data/evidence layout"
                )
            continue

        staging_root = next(
            (root for root in staging_roots if key.startswith(root)), None
        )
        if staging_root is not None:
            match = STAGING_DATA_KEY_RE.fullmatch(key)
            if match is not None:
                prefix = match.group(1)
                if not prefix.startswith(staging_root):
                    raise DecommissionError("staging data escaped its run prefix")
                staging_data.add(prefix)
            # Every other object under this exact digest-qualified run is
            # retained evidence.  The live layout includes inventory, report,
            # manifest, and completion-marker records.

    return sorted(construction_data), sorted(staging_data)


def _plan_targets(plan: dict[str, Any]) -> list[tuple[str, str]]:
    expected_current = plan.get("expected_current")
    if not isinstance(expected_current, str):
        raise DecommissionError("private plan has no expected current generation")

    schema = plan.get("schema")
    if schema == "paused-v2-decommission-plan-v1":
        specifications = (
            ("staging-data", "staging_data_prefixes", STAGING_DATA_PREFIX_RE),
            (
                "construction-data",
                "construction_data_prefixes",
                CONSTRUCTION_DATA_PREFIX_RE,
            ),
            ("slice", "slice_prefixes", SLICE_PREFIX_RE),
            ("old-root", "old_root_prefixes", VERSION_PREFIX_RE),
        )
    elif schema == "single-v1-retention-plan-v1":
        specifications = (("old-root", "old_root_prefixes", VERSION_PREFIX_RE),)
    else:
        raise DecommissionError("private plan has an unexpected schema")
    targets: list[tuple[str, str]] = []
    for category, field, pattern in specifications:
        values = plan.get(field)
        if not isinstance(values, list):
            raise DecommissionError(f"private plan {field} must be an array")
        for prefix in values:
            if not isinstance(prefix, str) or pattern.fullmatch(prefix) is None:
                raise DecommissionError(f"private plan contains an invalid {category} prefix")
            if category == "old-root" and prefix == f"{expected_current}/":
                raise DecommissionError("private plan targets the current v1 generation")
            targets.append((category, prefix))

    if schema == "paused-v2-decommission-plan-v1":
        if plan.get("v2_metadata_prefix") != "v2/":
            raise DecommissionError("private plan has an invalid v2 metadata prefix")
        v1 = plan.get("v1_predecessor_prefixes")
        v2 = plan.get("v2_only_root_prefixes")
        old = plan.get("old_root_prefixes")
        if not all(isinstance(value, list) for value in (v1, v2, old)):
            raise DecommissionError("private plan has invalid root classifications")
        if set(old) != set(v1) | set(v2):
            raise DecommissionError("private plan root classifications disagree")
        targets.append(("v2-metadata", "v2/"))

    prefixes = [prefix for _, prefix in targets]
    if len(prefixes) != len(set(prefixes)):
        raise DecommissionError("private plan contains duplicate deletion prefixes")
    for left in prefixes:
        for right in prefixes:
            if left != right and right.startswith(left):
                raise DecommissionError("private plan contains overlapping deletion prefixes")
    return targets


def build_recursive_inventory(
    *, plan: dict[str, Any], full_listing: dict[str, Any]
) -> dict[str, Any]:
    """Bind every object under every planned deletion prefix."""
    targets = _plan_targets(plan)
    all_objects = _listing_objects(full_listing, "full bucket listing")
    preserved = plan.get("preserved_top_level_prefixes")
    if not isinstance(preserved, list) or any(
        not isinstance(prefix, str) or not prefix.endswith("/")
        for prefix in preserved
    ):
        raise DecommissionError("private plan has invalid preserved prefixes")
    allowed_top = set(preserved)
    allowed_top.update(prefix.split("/", 1)[0] + "/" for _, prefix in targets)
    actual_top = {
        item["key"].split("/", 1)[0] + "/"
        for item in all_objects
        if "/" in item["key"]
    }
    if actual_top != allowed_top:
        raise DecommissionError("full inventory differs from the top-level listing")
    by_prefix: dict[str, list[dict[str, Any]]] = {
        prefix: [] for _, prefix in targets
    }
    for item in all_objects:
        matches = [prefix for _, prefix in targets if item["key"].startswith(prefix)]
        if len(matches) > 1:
            raise DecommissionError("an object matches overlapping deletion prefixes")
        if matches:
            by_prefix[matches[0]].append(item)

    inventory_targets: list[dict[str, Any]] = []
    for category, prefix in targets:
        objects = by_prefix[prefix]
        if not objects:
            raise DecommissionError("a planned deletion prefix has no recursive objects")
        inventory_targets.append(
            {
                "category": category,
                "prefix": prefix,
                "object_count": len(objects),
                "total_bytes": sum(item["size"] for item in objects),
                "objects": objects,
            }
        )

    preserved_evidence: list[dict[str, Any]] = []
    if plan["schema"] == "paused-v2-decommission-plan-v1":
        evidence_specs = (
            ("construction-evidence", "construction_evidence_prefixes"),
            ("staging-evidence", "staging_evidence_prefixes"),
        )
        target_prefixes = [prefix for _, prefix in targets]
        for category, field in evidence_specs:
            roots = plan.get(field)
            if not isinstance(roots, list) or any(
                not isinstance(root, str) for root in roots
            ):
                raise DecommissionError(f"private plan {field} must be an array")
            for root in roots:
                pattern = (
                    CONSTRUCTION_CHILD_RE
                    if category == "construction-evidence"
                    else STAGING_CHILD_RE
                )
                if pattern.fullmatch(root) is None:
                    raise DecommissionError("private plan has an invalid evidence root")
                evidence_objects = [
                    item
                    for item in all_objects
                    if item["key"].startswith(root)
                    and not any(
                        item["key"].startswith(target) for target in target_prefixes
                    )
                ]
                if not evidence_objects:
                    raise DecommissionError("a retained evidence root has no objects")
                preserved_evidence.append(
                    {
                        "category": category,
                        "prefix": root,
                        "object_count": len(evidence_objects),
                        "total_bytes": sum(item["size"] for item in evidence_objects),
                        "objects": evidence_objects,
                    }
                )

    return {
        "schema": (
            "paused-v2-decommission-inventory-v1"
            if plan["schema"] == "paused-v2-decommission-plan-v1"
            else "single-v1-retention-inventory-v1"
        ),
        "expected_current": plan["expected_current"],
        "object_count": sum(item["object_count"] for item in inventory_targets),
        "total_bytes": sum(item["total_bytes"] for item in inventory_targets),
        "targets": inventory_targets,
        "preserved_evidence": preserved_evidence,
    }


def verify_prefix_inventory(
    *, inventory: dict[str, Any], prefix: str, listing: dict[str, Any]
) -> None:
    """Refuse a delete when one target changed after the reviewed dry run."""
    targets = inventory.get("targets")
    if not isinstance(targets, list):
        raise DecommissionError("recursive inventory has no targets array")
    matches = [item for item in targets if isinstance(item, dict) and item.get("prefix") == prefix]
    if len(matches) != 1:
        raise DecommissionError("recursive inventory does not contain the target exactly once")
    expected = matches[0]
    current_objects = _listing_objects(listing, "fresh target listing")
    if any(not item["key"].startswith(prefix) for item in current_objects):
        raise DecommissionError("fresh target listing contains an object outside its prefix")
    current = {
        "category": expected.get("category"),
        "prefix": prefix,
        "object_count": len(current_objects),
        "total_bytes": sum(item["size"] for item in current_objects),
        "objects": current_objects,
    }
    if current != expected:
        raise DecommissionError("target inventory changed after the reviewed dry run")


def verify_prefix_remainder(
    *, inventory: dict[str, Any], prefix: str, listing: dict[str, Any]
) -> None:
    """Verify a retry sees only an exact subset of the reviewed target.

    Recursive object deletion is not atomic.  A cancelled run may therefore
    leave some, but not all, reviewed objects.  Retrying is safe only when every
    remaining key has the exact size and ETag recorded before the first delete
    and no new key appeared.
    """
    targets = inventory.get("targets")
    if not isinstance(targets, list):
        raise DecommissionError("recursive inventory has no targets array")
    matches = [
        item
        for item in targets
        if isinstance(item, dict) and item.get("prefix") == prefix
    ]
    if len(matches) != 1:
        raise DecommissionError(
            "recursive inventory does not contain the target exactly once"
        )
    expected_objects = matches[0].get("objects")
    if not isinstance(expected_objects, list):
        raise DecommissionError("recursive inventory target has no objects array")
    expected = {
        (item.get("key"), item.get("size"), item.get("etag"))
        for item in expected_objects
        if isinstance(item, dict)
    }
    if len(expected) != len(expected_objects):
        raise DecommissionError("recursive inventory target has invalid objects")
    current = _listing_objects(listing, "fresh target remainder listing")
    if any(not item["key"].startswith(prefix) for item in current):
        raise DecommissionError("fresh target listing contains an object outside its prefix")
    actual = {(item["key"], item["size"], item["etag"]) for item in current}
    if not actual.issubset(expected):
        raise DecommissionError("target remainder differs from the reviewed inventory")


def verify_preserved_evidence(
    *, inventory: dict[str, Any], prefix: str, listing: dict[str, Any]
) -> None:
    """Prove a retained run-evidence prefix is byte-identical after cleanup."""
    entries = inventory.get("preserved_evidence")
    if not isinstance(entries, list):
        raise DecommissionError("recursive inventory has no preserved evidence array")
    matches = [
        item
        for item in entries
        if isinstance(item, dict) and item.get("prefix") == prefix
    ]
    if len(matches) != 1:
        raise DecommissionError(
            "recursive inventory does not contain the evidence root exactly once"
        )
    expected = matches[0]
    current_objects = _listing_objects(listing, "fresh evidence listing")
    if any(not item["key"].startswith(prefix) for item in current_objects):
        raise DecommissionError("fresh evidence listing contains an object outside its root")
    current = {
        "category": expected.get("category"),
        "prefix": prefix,
        "object_count": len(current_objects),
        "total_bytes": sum(item["size"] for item in current_objects),
        "objects": current_objects,
    }
    if current != expected:
        raise DecommissionError("retained run evidence changed during cleanup")


def _validate_resume_bundle(
    *,
    plan_path: Path,
    inventory_path: Path,
    catalog_before_path: Path,
    pruned_catalog_path: Path,
    live_catalog_path: Path,
    full_listing_path: Path,
    expected_plan_sha256: str,
    expected_schema: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Validate immutable pending state against the current bucket listing."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise DecommissionError("expected plan SHA-256 is invalid")
    if _file_sha256(plan_path) != expected_plan_sha256:
        raise DecommissionError("pending plan bytes do not match their SHA-256")
    plan = _load_object(plan_path, "pending plan")
    inventory = _load_object(inventory_path, "pending recursive inventory")
    if plan.get("schema") != expected_schema:
        raise DecommissionError("pending plan has an unexpected schema")
    if plan.get("inventory_sha256") != _object_fingerprint(inventory):
        raise DecommissionError("pending inventory does not match the plan")

    before_bytes = catalog_before_path.read_bytes()
    pruned_bytes = pruned_catalog_path.read_bytes()
    live_bytes = live_catalog_path.read_bytes()
    source_sha256 = plan.get("source_sha256")
    if not isinstance(source_sha256, dict) or source_sha256.get(
        "v1_catalog"
    ) != hashlib.sha256(before_bytes).hexdigest():
        raise DecommissionError("pending v1 catalog does not match its source identity")
    if source_sha256.get("v1_catalog_pruned") != hashlib.sha256(
        pruned_bytes
    ).hexdigest():
        raise DecommissionError("pending pruned catalog does not match its identity")
    expected_current = plan.get("expected_current")
    if not isinstance(expected_current, str):
        raise DecommissionError("pending plan has no current generation")
    _catalog_versions(
        json.loads(before_bytes), expected_current=expected_current
    )
    remaining, _ = _catalog_versions(
        json.loads(pruned_bytes), expected_current=expected_current
    )
    if remaining != [expected_current]:
        raise DecommissionError("pending pruned catalog is not single-current")
    if live_bytes not in (before_bytes, pruned_bytes):
        raise DecommissionError("live catalog is neither pending before nor pruned state")

    targets = _plan_targets(plan)
    objects = _listing_objects(
        _load_object(full_listing_path, "fresh full bucket listing"),
        "fresh full bucket listing",
    )
    if not any(item["key"].startswith(f"{expected_current}/") for item in objects):
        raise DecommissionError("current v1 generation is absent during resume")

    allowed_top = set(plan.get("preserved_top_level_prefixes", []))
    if any(not isinstance(prefix, str) for prefix in allowed_top):
        raise DecommissionError("pending plan has invalid preserved prefixes")
    allowed_top.update(prefix.split("/", 1)[0] + "/" for _, prefix in targets)
    actual_top = {
        item["key"].split("/", 1)[0] + "/"
        for item in objects
        if "/" in item["key"]
    }
    if not actual_top.issubset(allowed_top):
        raise DecommissionError("a new top-level prefix appeared after pending plan")

    for _, prefix in targets:
        current = [item for item in objects if item["key"].startswith(prefix)]
        verify_prefix_remainder(
            inventory=inventory,
            prefix=prefix,
            listing={
                "Contents": [
                    {
                        "Key": item["key"],
                        "Size": item["size"],
                        "ETag": item["etag"],
                    }
                    for item in current
                ]
            },
        )
        expected_matches = [
            item
            for item in inventory.get("targets", [])
            if isinstance(item, dict) and item.get("prefix") == prefix
        ]
        if live_bytes == before_bytes and len(current) != expected_matches[0].get(
            "object_count"
        ):
            raise DecommissionError(
                "pending target changed while the pre-prune catalog is live"
            )

    evidence = inventory.get("preserved_evidence", [])
    if not isinstance(evidence, list):
        raise DecommissionError("pending inventory has invalid evidence records")
    target_prefixes = [prefix for _, prefix in targets]
    for entry in evidence:
        if not isinstance(entry, dict) or not isinstance(entry.get("prefix"), str):
            raise DecommissionError("pending inventory has an invalid evidence record")
        root = entry["prefix"]
        current = [
            item
            for item in objects
            if item["key"].startswith(root)
            and not any(item["key"].startswith(target) for target in target_prefixes)
        ]
        verify_preserved_evidence(
            inventory=inventory,
            prefix=root,
            listing={
                "Contents": [
                    {
                        "Key": item["key"],
                        "Size": item["size"],
                        "ETag": item["etag"],
                    }
                    for item in current
                ]
            },
        )
    return plan, inventory


def validate_paused_resume(
    *,
    plan_path: Path,
    inventory_path: Path,
    catalog_before_path: Path,
    pruned_catalog_path: Path,
    live_catalog_path: Path,
    full_listing_path: Path,
    manifest_path: Path,
    v2_catalog_path: Path,
    releases_dir: Path,
    expected_plan_sha256: str,
    now: datetime,
) -> None:
    """Validate a paused-data retry from its immutable evidence bundle."""
    enforce_rollback_hold(now=now, dry_run=False)
    plan, _ = _validate_resume_bundle(
        plan_path=plan_path,
        inventory_path=inventory_path,
        catalog_before_path=catalog_before_path,
        pruned_catalog_path=pruned_catalog_path,
        live_catalog_path=live_catalog_path,
        full_listing_path=full_listing_path,
        expected_plan_sha256=expected_plan_sha256,
        expected_schema="paused-v2-decommission-plan-v1",
    )
    source_sha256 = plan.get("source_sha256")
    builds = plan.get("v2_release_builds")
    if not isinstance(source_sha256, dict) or not isinstance(builds, list):
        raise DecommissionError("pending paused-data plan has no source identities")
    if source_sha256.get("v2_catalog") != _file_sha256(v2_catalog_path):
        raise DecommissionError("pending v2 catalog bytes changed")
    release_sha256 = source_sha256.get("v2_releases")
    if not isinstance(release_sha256, dict):
        raise DecommissionError("pending plan has no v2 release identities")
    if len(builds) != len(set(builds)):
        raise DecommissionError("pending plan contains duplicate v2 builds")
    for build in builds:
        if not isinstance(build, str) or VERSION_RE.fullmatch(build) is None:
            raise DecommissionError("pending plan contains an invalid v2 build")
        if release_sha256.get(build) != _file_sha256(
            _release_path(releases_dir, build)
        ):
            raise DecommissionError("pending v2 release bytes changed")

    artifacts: list[tuple[str, Path]] = [
        ("plan.json", plan_path),
        ("inventory.json", inventory_path),
        ("v1/catalog-before.json", catalog_before_path),
        ("v1/catalog-single-current.json", pruned_catalog_path),
        ("v2/catalog.json", v2_catalog_path),
    ]
    artifacts.extend(
        (
            f"v2/releases/{build}/release.json",
            _release_path(releases_dir, build),
        )
        for build in builds
    )
    manifest = _load_object(manifest_path, "paused-data evidence manifest")
    manifest_objects = manifest.get("objects")
    if (
        manifest.get("schema") != "paused-v2-decommission-evidence-v1"
        or manifest.get("plan_sha256") != expected_plan_sha256
        or not isinstance(manifest_objects, list)
    ):
        raise DecommissionError("paused-data evidence manifest is invalid")
    root = f"backups/paused-data-decommission/{expected_plan_sha256}"
    expected_objects = sorted(
        (
            {
                "key": f"{root}/{relative}",
                "size_bytes": path.stat().st_size,
                "sha256": _file_sha256(path),
            }
            for relative, path in artifacts
        ),
        key=lambda item: item["key"],
    )
    try:
        actual_objects = sorted(manifest_objects, key=lambda item: item["key"])
    except (KeyError, TypeError) as exc:
        raise DecommissionError(
            "paused-data evidence manifest has invalid object records"
        ) from exc
    if actual_objects != expected_objects:
        raise DecommissionError(
            "paused-data evidence manifest does not commit the complete bundle"
        )


def validate_v1_resume(
    *,
    plan_path: Path,
    inventory_path: Path,
    catalog_before_path: Path,
    pruned_catalog_path: Path,
    live_catalog_path: Path,
    full_listing_path: Path,
    expected_plan_sha256: str,
) -> None:
    """Validate a recurring v1 retry from its immutable pending journal."""
    plan, _ = _validate_resume_bundle(
        plan_path=plan_path,
        inventory_path=inventory_path,
        catalog_before_path=catalog_before_path,
        pruned_catalog_path=pruned_catalog_path,
        live_catalog_path=live_catalog_path,
        full_listing_path=full_listing_path,
        expected_plan_sha256=expected_plan_sha256,
        expected_schema="single-v1-retention-plan-v1",
    )
    if plan.get("eligible") is not True:
        raise DecommissionError("pending v1 retention plan was not eligible")


def _catalog_versions(
    catalog: dict[str, Any], *, expected_current: str
) -> tuple[list[str], dict[str, Any]]:
    if not VERSION_RE.fullmatch(expected_current):
        raise DecommissionError("expected current must match YYYY-MM-DD.N")
    links = catalog.get("links")
    if not isinstance(links, list):
        raise DecommissionError("v1 catalog links must be an array")

    versions: list[str] = []
    latest: list[str] = []
    for link in links:
        if not isinstance(link, dict):
            raise DecommissionError("v1 catalog link must be an object")
        if link.get("rel") != "child":
            continue
        href = link.get("href")
        if not isinstance(href, str):
            raise DecommissionError("v1 catalog child href must be a string")
        match = re.fullmatch(r"\./(\d{4}-\d{2}-\d{2}\.\d+)/collection\.json", href)
        if match is None:
            raise DecommissionError(f"unexpected v1 catalog child href {href!r}")
        version = match.group(1)
        # Calendar validation as well as shape validation.
        prune_catalog._version_key(version)
        if version in versions:
            raise DecommissionError(f"duplicate v1 catalog child {version}")
        versions.append(version)
        if link.get("latest") is True:
            latest.append(version)

    if latest != [expected_current]:
        raise DecommissionError(
            f"v1 catalog latest must be exactly {expected_current}; found {latest}"
        )
    if not versions:
        raise DecommissionError("v1 catalog has no child generations")

    try:
        pruned, dropped = prune_catalog.prune_by_retention(
            catalog, keep=1, current=expected_current
        )
    except prune_catalog.PruneError as exc:
        raise DecommissionError(str(exc)) from exc
    if set(dropped) != set(versions) - {expected_current}:
        raise DecommissionError("catalog retention plan did not drop every predecessor")
    return versions, pruned


def _catalog_latest(catalog: dict[str, Any]) -> str:
    links = catalog.get("links")
    if not isinstance(links, list):
        raise DecommissionError("v1 catalog links must be an array")
    latest: list[str] = []
    for link in links:
        if not isinstance(link, dict):
            raise DecommissionError("v1 catalog link must be an object")
        if link.get("rel") != "child" or link.get("latest") is not True:
            continue
        href = link.get("href")
        if not isinstance(href, str):
            raise DecommissionError("v1 catalog latest child href must be a string")
        match = re.fullmatch(r"\./(\d{4}-\d{2}-\d{2}\.\d+)/collection\.json", href)
        if match is None:
            raise DecommissionError("v1 catalog latest child href is invalid")
        latest.append(match.group(1))
    if len(latest) != 1:
        raise DecommissionError("v1 catalog must contain exactly one latest child")
    return latest[0]


def build_v1_retention_plan(
    *,
    catalog: dict[str, Any],
    top_listing: dict[str, Any],
    now: datetime,
    catalog_last_modified: datetime,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Plan the recurring one-current-v1 prune after a full overlap window."""
    if now.tzinfo is None or catalog_last_modified.tzinfo is None:
        raise DecommissionError("retention timestamps must be timezone-aware")
    now = now.astimezone(timezone.utc)
    catalog_last_modified = catalog_last_modified.astimezone(timezone.utc)
    if catalog_last_modified > now:
        raise DecommissionError("v1 catalog last-modified time is in the future")

    current = _catalog_latest(catalog)
    versions, pruned = _catalog_versions(catalog, expected_current=current)
    top = _listing_prefixes(top_listing, "top-level listing")
    root_versions: set[str] = set()
    for prefix in top:
        match = VERSION_PREFIX_RE.fullmatch(prefix)
        if match:
            version = match.group(1)
            prune_catalog._version_key(version)
            root_versions.add(version)
    if set(versions) - root_versions:
        raise DecommissionError("a v1 catalog child has no matching R2 prefix")
    current_key = prune_catalog._version_key(current)
    if any(prune_catalog._version_key(version) > current_key for version in root_versions):
        raise DecommissionError("R2 contains a root newer than the live v1 catalog")

    predecessors = sorted(
        set(versions) - {current}, key=prune_catalog._version_key
    )
    not_before = catalog_last_modified + V1_PREDECESSOR_OVERLAP
    eligible = bool(predecessors) and now >= not_before
    target_prefixes = [f"{version}/" for version in predecessors]
    return (
        {
            "schema": "single-v1-retention-plan-v1",
            "expected_current": current,
            "catalog_last_modified": catalog_last_modified.isoformat(),
            "not_before": not_before.isoformat(),
            "eligible": eligible,
            "catalog_predecessor_prefixes": [
                f"{version}/" for version in predecessors
            ],
            "old_root_prefixes": target_prefixes,
            "preserved_top_level_prefixes": sorted(top - set(target_prefixes)),
        },
        pruned,
    )


def build_plan(
    *,
    catalog: dict[str, Any],
    top_listing: dict[str, Any],
    construction_listing: dict[str, Any],
    staging_listing: dict[str, Any],
    full_listing: dict[str, Any],
    expected_current: str,
    v2_references: set[str],
    v2_release_count: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return ``(private plan, single-current catalog)`` after strict checks."""
    catalog_versions, pruned = _catalog_versions(
        catalog, expected_current=expected_current
    )
    top = _listing_prefixes(top_listing, "top-level listing")
    construction = _listing_prefixes(
        construction_listing, "construction-v1 listing"
    )
    staging = _listing_prefixes(staging_listing, "global-v2 staging listing")
    _refuse_direct_children(construction_listing, "construction-v1 listing")
    _refuse_direct_children(staging_listing, "global-v2 staging listing")

    if any(CONSTRUCTION_CHILD_RE.fullmatch(prefix) is None for prefix in construction):
        raise DecommissionError("construction-v1 contains an unexpected child prefix")
    if any(STAGING_CHILD_RE.fullmatch(prefix) is None for prefix in staging):
        raise DecommissionError("global-v2 staging contains an unexpected child prefix")

    all_objects = _listing_objects(full_listing, "full bucket listing")
    construction_data, staging_data = _classify_run_data_prefixes(
        objects=all_objects,
        construction_roots=construction,
        staging_roots=staging,
    )

    root_versions: set[str] = set()
    slices: set[str] = set()
    for prefix in top:
        version_match = VERSION_PREFIX_RE.fullmatch(prefix)
        slice_match = SLICE_PREFIX_RE.fullmatch(prefix)
        if version_match:
            version = version_match.group(1)
            prune_catalog._version_key(version)
            root_versions.add(version)
        elif slice_match:
            version = slice_match.group(1)
            prune_catalog._version_key(version)
            slices.add(prefix)

    if expected_current not in root_versions:
        raise DecommissionError("current v1 generation is absent from R2")
    missing_catalog_prefixes = set(catalog_versions) - root_versions
    if missing_catalog_prefixes:
        raise DecommissionError("a v1 catalog child has no matching R2 prefix")
    current_key = prune_catalog._version_key(expected_current)
    newer = sorted(
        version
        for version in root_versions
        if prune_catalog._version_key(version) > current_key
    )
    if newer:
        raise DecommissionError(
            "R2 contains a version newer than the asserted live current; "
            "it may be an in-flight build"
        )

    if "v2/" not in top:
        raise DecommissionError("v2 metadata prefix is absent; refusing a fresh plan")
    if v2_release_count < 1:
        raise DecommissionError("v2 catalog chain contains no verified releases")

    v1_predecessors = set(catalog_versions) - {expected_current}
    v2_only_roots = root_versions - set(catalog_versions)
    unclassified_roots = v2_only_roots - v2_references
    if unclassified_roots:
        raise DecommissionError(
            "an old root is neither a v1 catalog predecessor nor referenced by v2"
        )
    old_roots = sorted(
        v1_predecessors | v2_only_roots,
        key=prune_catalog._version_key,
    )
    sorted_slices = sorted(
        slices,
        key=lambda prefix: prune_catalog._version_key(
            SLICE_PREFIX_RE.fullmatch(prefix).group(1)  # type: ignore[union-attr]
        ),
    )

    deleted_top = (
        set(sorted_slices)
        | {f"{version}/" for version in old_roots}
        | {"v2/"}
    )
    plan = {
        "schema": "paused-v2-decommission-plan-v1",
        "expected_current": expected_current,
        "catalog_predecessors": sorted(
            v1_predecessors, key=prune_catalog._version_key
        ),
        "catalog_v1_predecessor_prefixes": [
            f"{version}/"
            for version in sorted(v1_predecessors, key=prune_catalog._version_key)
        ],
        "v1_predecessor_prefixes": [
            f"{version}/"
            for version in sorted(v1_predecessors, key=prune_catalog._version_key)
        ],
        "v2_only_root_prefixes": [
            f"{version}/"
            for version in sorted(v2_only_roots, key=prune_catalog._version_key)
        ],
        "old_root_prefixes": [f"{version}/" for version in old_roots],
        "slice_prefixes": sorted_slices,
        "construction_data_prefixes": construction_data,
        "staging_data_prefixes": staging_data,
        "construction_evidence_prefixes": sorted(construction),
        "staging_evidence_prefixes": sorted(staging),
        "v2_metadata_prefix": "v2/",
        "v2_release_count": v2_release_count,
        "v2_reference_count": len(v2_references),
        "v2_references": sorted(v2_references),
        "preserved_top_level_prefixes": sorted(top - deleted_top),
    }
    return plan, pruned


def publish_pruned_catalog(
    client: Any,
    *,
    before_bytes: bytes,
    pruned_bytes: bytes,
    expected_current: str,
) -> bool:
    """CAS-publish the single-current catalog; return False when already equal."""
    before = json.loads(before_bytes)
    pruned = json.loads(pruned_bytes)
    _catalog_versions(before, expected_current=expected_current)
    remaining, _ = _catalog_versions(pruned, expected_current=expected_current)
    if remaining != [expected_current]:
        raise DecommissionError("pruned catalog does not contain only current v1")

    live = client.fetch_catalog()
    if live == pruned_bytes:
        return False
    if live != before_bytes:
        raise DecommissionError("v1 catalog changed after planning; refusing to publish")
    if before_bytes == pruned_bytes:
        return False

    digest = hashlib.sha256(before_bytes).hexdigest()[:16]
    client.put_backup(
        f"catalog-before-single-current-{expected_current}-{digest}.json",
        before_bytes,
    )
    client.publish_catalog(
        pruned_bytes, expected_etag=finalize._content_etag(before_bytes)
    )
    if client.fetch_catalog() != pruned_bytes:
        raise DecommissionError("single-current catalog readback mismatch")
    return True


def restore_catalog(
    client: Any, *, before_bytes: bytes, pruned_bytes: bytes
) -> None:
    """Restore the pre-prune catalog only while the exact pruned bytes are live."""
    if client.fetch_catalog() != pruned_bytes:
        raise DecommissionError("live catalog is not the planned prune; refusing restore")
    client.publish_catalog(
        before_bytes, expected_etag=finalize._content_etag(pruned_bytes)
    )
    if client.fetch_catalog() != before_bytes:
        raise DecommissionError("catalog restore readback mismatch")


def _release_path(releases_dir: Path, build: str) -> Path:
    nested = releases_dir / build / "release.json"
    return nested if nested.is_file() else releases_dir / f"{build}.json"


def backup_evidence(
    client: Any,
    *,
    plan_path: Path,
    inventory_path: Path,
    catalog_path: Path,
    pruned_catalog_path: Path,
    v2_catalog_path: Path,
    releases_dir: Path,
    expected_plan_sha256: str,
) -> int:
    """Durably retain the exact private plan and compact production metadata."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise DecommissionError("expected plan SHA-256 is invalid")
    if _file_sha256(plan_path) != expected_plan_sha256:
        raise DecommissionError("private plan bytes do not match the reviewed SHA-256")

    plan = _load_object(plan_path, "private plan")
    inventory = _load_object(inventory_path, "recursive deletion inventory")
    if plan.get("inventory_sha256") != _object_fingerprint(inventory):
        raise DecommissionError("recursive inventory does not match the private plan")

    source_sha256 = plan.get("source_sha256")
    builds = plan.get("v2_release_builds")
    if not isinstance(source_sha256, dict) or not isinstance(builds, list):
        raise DecommissionError("private plan has no source identities")
    for field, path in (
        ("v1_catalog", catalog_path),
        ("v1_catalog_pruned", pruned_catalog_path),
        ("v2_catalog", v2_catalog_path),
    ):
        if source_sha256.get(field) != _file_sha256(path):
            raise DecommissionError(f"{field} bytes changed after planning")

    release_sha256 = source_sha256.get("v2_releases")
    if not isinstance(release_sha256, dict):
        raise DecommissionError("private plan has no v2 release identities")

    artifacts: list[tuple[str, Path]] = [
        ("plan.json", plan_path),
        ("inventory.json", inventory_path),
        ("v1/catalog-before.json", catalog_path),
        ("v1/catalog-single-current.json", pruned_catalog_path),
        ("v2/catalog.json", v2_catalog_path),
    ]
    if len(builds) != len(set(builds)):
        raise DecommissionError("private plan contains duplicate v2 release builds")
    for build in builds:
        if not isinstance(build, str) or VERSION_RE.fullmatch(build) is None:
            raise DecommissionError("private plan contains an invalid v2 release build")
        path = _release_path(releases_dir, build)
        if not path.is_file() or release_sha256.get(build) != _file_sha256(path):
            raise DecommissionError("v2 release bytes changed after planning")
        artifacts.append((f"v2/releases/{build}/release.json", path))

    root = f"paused-data-decommission/{expected_plan_sha256}"
    manifest_objects: list[dict[str, Any]] = []
    for relative, path in artifacts:
        data = path.read_bytes()
        client.put_backup(f"{root}/{relative}", data)
        manifest_objects.append(
            {
                "key": f"backups/{root}/{relative}",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )

    manifest = {
        "schema": "paused-v2-decommission-evidence-v1",
        "plan_sha256": expected_plan_sha256,
        "objects": manifest_objects,
    }
    client.put_backup(
        f"{root}/manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    return len(artifacts) + 1


def backup_v1_retention_evidence(
    client: Any,
    *,
    plan_path: Path,
    inventory_path: Path,
    catalog_path: Path,
    pruned_catalog_path: Path,
    expected_plan_sha256: str,
) -> int:
    """Durably retain one recurring v1 prune's exact catalog and inventory."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise DecommissionError("expected plan SHA-256 is invalid")
    if _file_sha256(plan_path) != expected_plan_sha256:
        raise DecommissionError("private plan bytes do not match the reviewed SHA-256")
    plan = _load_object(plan_path, "private plan")
    inventory = _load_object(inventory_path, "recursive deletion inventory")
    if plan.get("schema") != "single-v1-retention-plan-v1":
        raise DecommissionError("private plan is not a v1 retention plan")
    if plan.get("eligible") is not True:
        raise DecommissionError("v1 retention plan is not old enough to execute")
    if plan.get("inventory_sha256") != _object_fingerprint(inventory):
        raise DecommissionError("recursive inventory does not match the private plan")
    source_sha256 = plan.get("source_sha256")
    if not isinstance(source_sha256, dict) or source_sha256.get(
        "v1_catalog"
    ) != _file_sha256(catalog_path):
        raise DecommissionError("v1 catalog bytes changed after planning")
    if source_sha256.get("v1_catalog_pruned") != _file_sha256(
        pruned_catalog_path
    ):
        raise DecommissionError("pruned v1 catalog bytes changed after planning")

    artifacts = [
        ("plan.json", plan_path),
        ("inventory.json", inventory_path),
        ("catalog-before.json", catalog_path),
        ("catalog-single-current.json", pruned_catalog_path),
    ]
    root = f"single-v1-retention/{expected_plan_sha256}"
    manifest_objects: list[dict[str, Any]] = []
    for relative, path in artifacts:
        data = path.read_bytes()
        client.put_backup(f"{root}/{relative}", data)
        manifest_objects.append(
            {
                "key": f"backups/{root}/{relative}",
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    manifest = {
        "schema": "single-v1-retention-evidence-v1",
        "plan_sha256": expected_plan_sha256,
        "objects": manifest_objects,
    }
    client.put_backup(
        f"{root}/manifest.json",
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(),
    )
    current = plan.get("expected_current")
    if not isinstance(current, str) or VERSION_RE.fullmatch(current) is None:
        raise DecommissionError("v1 retention plan has an invalid current generation")
    pointer = {
        "schema": "single-v1-retention-pending-v1",
        "expected_current": current,
        "plan_sha256": expected_plan_sha256,
    }
    client.put_backup(
        f"single-v1-retention/pending/{current}.json",
        (json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode(),
    )
    return len(artifacts) + 2


def mark_v1_retention_complete(
    client: Any, *, plan_path: Path, expected_plan_sha256: str
) -> None:
    """Create the immutable completion record for a pending v1 transaction."""
    if not re.fullmatch(r"[0-9a-f]{64}", expected_plan_sha256):
        raise DecommissionError("expected plan SHA-256 is invalid")
    if _file_sha256(plan_path) != expected_plan_sha256:
        raise DecommissionError("completed plan bytes do not match the expected SHA-256")
    plan = _load_object(plan_path, "completed v1 retention plan")
    current = plan.get("expected_current")
    if (
        plan.get("schema") != "single-v1-retention-plan-v1"
        or not isinstance(current, str)
        or VERSION_RE.fullmatch(current) is None
    ):
        raise DecommissionError("completed v1 retention plan is invalid")
    marker = {
        "schema": "single-v1-retention-complete-v1",
        "expected_current": current,
        "plan_sha256": expected_plan_sha256,
    }
    client.put_backup(
        f"single-v1-retention/completed/{current}/{expected_plan_sha256}.json",
        (json.dumps(marker, indent=2, sort_keys=True) + "\n").encode(),
    )


def _client() -> finalize.R2Client:
    class Args:
        endpoint = None
        base_url = None
        bucket = None

    return finalize._build_client_from_env(Args())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument("--catalog", type=Path, required=True)
    plan_parser.add_argument("--top-listing", type=Path, required=True)
    plan_parser.add_argument("--construction-listing", type=Path, required=True)
    plan_parser.add_argument("--staging-listing", type=Path, required=True)
    plan_parser.add_argument("--full-listing", type=Path, required=True)
    plan_parser.add_argument("--v2-catalog", type=Path, required=True)
    plan_parser.add_argument("--v2-releases-dir", type=Path, required=True)
    plan_parser.add_argument("--expected-current", required=True)
    plan_parser.add_argument("--now", required=True)
    plan_parser.add_argument("--dry-run", action="store_true")
    plan_parser.add_argument("--output", type=Path, required=True)
    plan_parser.add_argument("--inventory-output", type=Path, required=True)
    plan_parser.add_argument("--catalog-output", type=Path, required=True)

    v1_parser = sub.add_parser("plan-v1-retention")
    v1_parser.add_argument("--catalog", type=Path, required=True)
    v1_parser.add_argument("--catalog-last-modified", required=True)
    v1_parser.add_argument("--top-listing", type=Path, required=True)
    v1_parser.add_argument("--full-listing", type=Path, required=True)
    v1_parser.add_argument("--now", required=True)
    v1_parser.add_argument("--output", type=Path, required=True)
    v1_parser.add_argument("--inventory-output", type=Path, required=True)
    v1_parser.add_argument("--catalog-output", type=Path, required=True)

    verify_parser = sub.add_parser("verify-prefix-inventory")
    verify_parser.add_argument("--inventory", type=Path, required=True)
    verify_parser.add_argument("--prefix", required=True)
    verify_parser.add_argument("--listing", type=Path, required=True)

    verify_evidence_parser = sub.add_parser("verify-preserved-evidence")
    verify_evidence_parser.add_argument("--inventory", type=Path, required=True)
    verify_evidence_parser.add_argument("--prefix", required=True)
    verify_evidence_parser.add_argument("--listing", type=Path, required=True)

    verify_remainder_parser = sub.add_parser("verify-prefix-remainder")
    verify_remainder_parser.add_argument("--inventory", type=Path, required=True)
    verify_remainder_parser.add_argument("--prefix", required=True)
    verify_remainder_parser.add_argument("--listing", type=Path, required=True)

    for name in ("validate-paused-resume", "validate-v1-resume"):
        command = sub.add_parser(name)
        command.add_argument("--plan", type=Path, required=True)
        command.add_argument("--inventory", type=Path, required=True)
        command.add_argument("--catalog-before", type=Path, required=True)
        command.add_argument("--pruned-catalog", type=Path, required=True)
        command.add_argument("--live-catalog", type=Path, required=True)
        command.add_argument("--full-listing", type=Path, required=True)
        command.add_argument("--expected-plan-sha256", required=True)
        if name == "validate-paused-resume":
            command.add_argument("--manifest", type=Path, required=True)
            command.add_argument("--v2-catalog", type=Path, required=True)
            command.add_argument("--v2-releases-dir", type=Path, required=True)
            command.add_argument("--now", required=True)

    backup_parser = sub.add_parser("backup-evidence")
    backup_parser.add_argument("--plan", type=Path, required=True)
    backup_parser.add_argument("--inventory", type=Path, required=True)
    backup_parser.add_argument("--catalog", type=Path, required=True)
    backup_parser.add_argument("--pruned-catalog", type=Path, required=True)
    backup_parser.add_argument("--v2-catalog", type=Path, required=True)
    backup_parser.add_argument("--v2-releases-dir", type=Path, required=True)
    backup_parser.add_argument("--expected-plan-sha256", required=True)

    backup_v1_parser = sub.add_parser("backup-v1-evidence")
    backup_v1_parser.add_argument("--plan", type=Path, required=True)
    backup_v1_parser.add_argument("--inventory", type=Path, required=True)
    backup_v1_parser.add_argument("--catalog", type=Path, required=True)
    backup_v1_parser.add_argument("--pruned-catalog", type=Path, required=True)
    backup_v1_parser.add_argument("--expected-plan-sha256", required=True)

    complete_v1_parser = sub.add_parser("mark-v1-complete")
    complete_v1_parser.add_argument("--plan", type=Path, required=True)
    complete_v1_parser.add_argument("--expected-plan-sha256", required=True)

    for name in ("publish-catalog", "restore-catalog"):
        command = sub.add_parser(name)
        command.add_argument("--before", type=Path, required=True)
        command.add_argument("--pruned", type=Path, required=True)
        command.add_argument("--expected-current", required=True)

    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            enforce_rollback_hold(
                now=_parse_utc(args.now),
                dry_run=args.dry_run,
            )
            references, releases = v2_retention_guard.collect_v2_references(
                args.v2_catalog, args.v2_releases_dir
            )
            if len(releases) != len(set(releases)):
                raise DecommissionError("v2 catalog contains duplicate release builds")
            plan, pruned = build_plan(
                catalog=_load_object(args.catalog, "v1 catalog"),
                top_listing=_load_object(args.top_listing, "top-level listing"),
                construction_listing=_load_object(
                    args.construction_listing, "construction-v1 listing"
                ),
                staging_listing=_load_object(
                    args.staging_listing, "global-v2 staging listing"
                ),
                full_listing=_load_object(args.full_listing, "full bucket listing"),
                expected_current=args.expected_current,
                v2_references=references,
                v2_release_count=len(releases),
            )
            inventory = build_recursive_inventory(
                plan=plan,
                full_listing=_load_object(args.full_listing, "full bucket listing"),
            )
            plan["v2_release_builds"] = sorted(releases)
            plan["source_sha256"] = {
                "v1_catalog": _file_sha256(args.catalog),
                "v2_catalog": _file_sha256(args.v2_catalog),
                "v2_releases": {
                    build: _file_sha256(_release_path(args.v2_releases_dir, build))
                    for build in sorted(releases)
                },
            }
            plan["inventory_sha256"] = _object_fingerprint(inventory)
            plan["inventory_object_count"] = inventory["object_count"]
            plan["inventory_total_bytes"] = inventory["total_bytes"]
            _write_object(args.inventory_output, inventory)
            _write_object(args.catalog_output, pruned)
            plan["source_sha256"]["v1_catalog_pruned"] = _file_sha256(
                args.catalog_output
            )
            _write_object(args.output, plan)
            print(
                "Plan validated: "
                f"{len(plan['construction_data_prefixes'])} construction-data, "
                f"{len(plan['staging_data_prefixes'])} staging-data, "
                f"{len(plan['slice_prefixes'])} slice, and "
                f"{len(plan['old_root_prefixes'])} retired root prefixes."
            )
        elif args.command == "plan-v1-retention":
            catalog = _load_object(args.catalog, "v1 catalog")
            plan, pruned = build_v1_retention_plan(
                catalog=catalog,
                top_listing=_load_object(args.top_listing, "top-level listing"),
                now=_parse_utc(args.now),
                catalog_last_modified=_parse_utc(args.catalog_last_modified),
            )
            inventory = build_recursive_inventory(
                plan=plan,
                full_listing=_load_object(args.full_listing, "full bucket listing"),
            )
            plan["source_sha256"] = {"v1_catalog": _file_sha256(args.catalog)}
            plan["inventory_sha256"] = _object_fingerprint(inventory)
            plan["inventory_object_count"] = inventory["object_count"]
            plan["inventory_total_bytes"] = inventory["total_bytes"]
            _write_object(args.inventory_output, inventory)
            _write_object(args.catalog_output, pruned)
            plan["source_sha256"]["v1_catalog_pruned"] = _file_sha256(
                args.catalog_output
            )
            _write_object(args.output, plan)
            print(
                "V1 retention plan validated: "
                f"{len(plan['old_root_prefixes'])} predecessor prefix(es), "
                f"eligible={str(plan['eligible']).lower()}."
            )
        elif args.command == "verify-prefix-inventory":
            verify_prefix_inventory(
                inventory=_load_object(args.inventory, "recursive deletion inventory"),
                prefix=args.prefix,
                listing=_load_object(args.listing, "fresh target listing"),
            )
            print("Target inventory still matches the reviewed plan.")
        elif args.command == "verify-preserved-evidence":
            verify_preserved_evidence(
                inventory=_load_object(args.inventory, "recursive deletion inventory"),
                prefix=args.prefix,
                listing=_load_object(args.listing, "fresh evidence listing"),
            )
            print("Retained run evidence still matches the reviewed plan.")
        elif args.command == "verify-prefix-remainder":
            verify_prefix_remainder(
                inventory=_load_object(args.inventory, "recursive deletion inventory"),
                prefix=args.prefix,
                listing=_load_object(args.listing, "fresh target remainder listing"),
            )
            print("Target remainder is an exact subset of the reviewed plan.")
        elif args.command == "validate-paused-resume":
            validate_paused_resume(
                plan_path=args.plan,
                inventory_path=args.inventory,
                catalog_before_path=args.catalog_before,
                pruned_catalog_path=args.pruned_catalog,
                live_catalog_path=args.live_catalog,
                full_listing_path=args.full_listing,
                manifest_path=args.manifest,
                v2_catalog_path=args.v2_catalog,
                releases_dir=args.v2_releases_dir,
                expected_plan_sha256=args.expected_plan_sha256,
                now=_parse_utc(args.now),
            )
            print("Paused-data pending transaction is safe to resume.")
        elif args.command == "validate-v1-resume":
            validate_v1_resume(
                plan_path=args.plan,
                inventory_path=args.inventory,
                catalog_before_path=args.catalog_before,
                pruned_catalog_path=args.pruned_catalog,
                live_catalog_path=args.live_catalog,
                full_listing_path=args.full_listing,
                expected_plan_sha256=args.expected_plan_sha256,
            )
            print("V1 retention pending transaction is safe to resume.")
        elif args.command == "backup-evidence":
            count = backup_evidence(
                _client(),
                plan_path=args.plan,
                inventory_path=args.inventory,
                catalog_path=args.catalog,
                pruned_catalog_path=args.pruned_catalog,
                v2_catalog_path=args.v2_catalog,
                releases_dir=args.v2_releases_dir,
                expected_plan_sha256=args.expected_plan_sha256,
            )
            print(f"Durably backed up and verified {count} evidence objects.")
        elif args.command == "backup-v1-evidence":
            count = backup_v1_retention_evidence(
                _client(),
                plan_path=args.plan,
                inventory_path=args.inventory,
                catalog_path=args.catalog,
                pruned_catalog_path=args.pruned_catalog,
                expected_plan_sha256=args.expected_plan_sha256,
            )
            print(f"Durably backed up and verified {count} v1 retention objects.")
        elif args.command == "mark-v1-complete":
            mark_v1_retention_complete(
                _client(),
                plan_path=args.plan,
                expected_plan_sha256=args.expected_plan_sha256,
            )
            print("Durably recorded v1 retention completion.")
        elif args.command == "publish-catalog":
            changed = publish_pruned_catalog(
                _client(),
                before_bytes=args.before.read_bytes(),
                pruned_bytes=args.pruned.read_bytes(),
                expected_current=args.expected_current,
            )
            print("Catalog published and verified." if changed else "Catalog already pruned.")
        elif args.command == "restore-catalog":
            restore_catalog(
                _client(),
                before_bytes=args.before.read_bytes(),
                pruned_bytes=args.pruned.read_bytes(),
            )
            print("Catalog restored and verified.")
    except (
        DecommissionError,
        finalize.PromotionError,
        finalize.PreconditionFailed,
        v2_retention_guard.GuardError,
        json.JSONDecodeError,
        OSError,
        ValueError,
    ) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
