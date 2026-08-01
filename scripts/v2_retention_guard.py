#!/usr/bin/env python3
"""Retention guard for the v2 catalog root.

`docs/v2-release-catalog-contract.md` states the requirement outright:
"Retention must consider both catalog roots before deleting an object." Only the
v1 root was ever checked. `prune_catalog.py assert-unreferenced` walks child
links of `catalog.json` and contains no reference to `v2` or `slice` anywhere,
so a bucket-root prefix reachable ONLY through the v2 chain reads as
unreferenced -- including a live, serving one.

The chain it cannot see:

    v2/catalog.json
      -> releases[].manifest_key = v2/releases/{build}/release.json
        -> legacy_core.version, legacy_core.entrypoints.*
        -> families[*].version, families[*].manifest_key
        -> families[*] entrypoints/artifacts, which the contract says carry
           "bucket-root keys prefixed by the immutable family source version"

This guard answers "is <target> referenced anywhere in that chain?" for any
bucket-root prefix -- a `slice-YYYY-MM-DD.N` or a plain `YYYY-MM-DD.N` version.

TWO DESIGN RULES, both because the bug being fixed is "absence read as safety":

1. It SCANS, it does not introspect. Every string anywhere in the catalog and
   every release document is examined, and its first path component is treated
   as a referenced prefix. Enumerating the fields that "should" hold a version
   is how the v1 guard came to miss the v2 chain -- and the release document
   gained an external reverse publication path (PR #205) after it was written.
   A recursive scan cannot miss a field nobody thought of.

2. It FAILS CLOSED on anything it cannot read. A release document named by the
   catalog but absent from --releases-dir is an error, never an implicit
   "unreferenced". A release document whose bytes do not match the catalog's
   recorded `manifest_sha256` is an error, because a stale copy would under-report
   references. Deleting 45 GiB on the strength of a file we failed to open is
   exactly the failure mode this exists to prevent.

Usage, from r2-cleanup.yml:

    aws s3 cp s3://$BUCKET/v2/catalog.json /tmp/v2-catalog.json
    # ...download each referenced release.json into /tmp/v2-releases/...
    python3 scripts/v2_retention_guard.py assert-unreferenced \\
        --v2-catalog /tmp/v2-catalog.json \\
        --releases-dir /tmp/v2-releases \\
        --target slice-2026-08-04.0

`list-referenced` prints every prefix the v2 chain depends on, which is also the
download list for the step above.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterator


CATALOG_SCHEMA = "overture-geocoder-v2-catalog-v1"
UNAVAILABLE_CATALOG_SCHEMA = "overture-geocoder-v2-unavailable-v1"
RELEASE_SCHEMA = "overture-geocoder-v2-release-v1"

# A bucket-root prefix is either a slice namespace or a plain version.
SLICE_RE = re.compile(r"^slice-\d{4}-\d{2}-\d{2}\.\d+$")
VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


class GuardError(RuntimeError):
    """Any condition under which a delete must not proceed."""


def is_bucket_root_prefix(value: str) -> bool:
    return bool(SLICE_RE.fullmatch(value) or VERSION_RE.fullmatch(value))


def _strings(node: Any) -> Iterator[str]:
    """Every string anywhere in the document, keys included.

    Keys are included deliberately: a manifest that ever maps
    ``{"slice-2026-08-04.0": {...}}`` would otherwise be invisible.
    """
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)


def referenced_prefixes(document: Any) -> set[str]:
    """Bucket-root prefixes this document depends on.

    A string is treated as a reference if it IS a prefix, or if its first path
    component is one -- which is how entrypoints and artifact keys carry it
    (``slice-2026-07-30.0/families/places/head.phrp``).
    """
    found: set[str] = set()
    for value in _strings(document):
        candidate = value.strip("./").split("/", 1)[0]
        if is_bucket_root_prefix(candidate):
            found.add(candidate)
    return found


def _load(path: Path, label: str) -> Any:
    if not path.is_file():
        raise GuardError(f"{label} is missing at {path} -- refusing to treat an "
                         "unreadable document as proof of non-reference")
    try:
        return json.loads(path.read_bytes())
    except json.JSONDecodeError as error:
        raise GuardError(f"{label} at {path} is not valid JSON: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def catalog_release_entries(catalog: Any) -> list[dict[str, Any]]:
    """Validate the catalog shape and return its release entries.

    An `unavailable` catalog is a legitimate state with no releases; anything
    else unrecognised is an error rather than an empty result.
    """
    if not isinstance(catalog, dict):
        raise GuardError("v2 catalog must be a JSON object")
    schema = catalog.get("schema")
    if schema == UNAVAILABLE_CATALOG_SCHEMA:
        return []
    if schema != CATALOG_SCHEMA:
        raise GuardError(
            f"v2 catalog schema must be {CATALOG_SCHEMA} or "
            f"{UNAVAILABLE_CATALOG_SCHEMA}, found {schema!r}"
        )
    entries = catalog.get("releases")
    if not isinstance(entries, list) or not entries:
        raise GuardError("v2 catalog has no releases array")
    for entry in entries:
        if not isinstance(entry, dict):
            raise GuardError("v2 catalog release entry must be an object")
        for field in ("geocoder_build", "manifest_key", "manifest_sha256"):
            if not isinstance(entry.get(field), str) or not entry[field]:
                raise GuardError(f"v2 catalog release entry has no {field}")
    return entries


def collect_v2_references(
    catalog_path: Path, releases_dir: Path
) -> tuple[set[str], list[str]]:
    """Return (referenced prefixes, release documents actually read).

    Every release named by the catalog must be present and byte-matched, or this
    raises. That is the whole point of the guard.
    """
    catalog = _load(catalog_path, "v2 catalog")
    entries = catalog_release_entries(catalog)

    found = referenced_prefixes(catalog)
    read: list[str] = []
    for entry in entries:
        build = entry["geocoder_build"]
        path = releases_dir / build / "release.json"
        if not path.is_file():
            # Also accept a flat layout, which is easier to produce with `aws s3 cp`.
            path = releases_dir / f"{build}.json"
        release = _load(path, f"v2 release document for {build}")

        actual = _sha256(path)
        if actual != entry["manifest_sha256"]:
            raise GuardError(
                f"v2 release document for {build} has sha256 {actual}, but the "
                f"catalog records {entry['manifest_sha256']}. A stale copy would "
                "under-report references; refusing to proceed."
            )
        if not isinstance(release, dict) or release.get("schema") != RELEASE_SCHEMA:
            raise GuardError(
                f"v2 release document for {build} must use schema {RELEASE_SCHEMA}"
            )
        found |= referenced_prefixes(release)
        read.append(build)
    return found, read


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("assert-unreferenced", "list-referenced"):
        command = sub.add_parser(name)
        command.add_argument("--v2-catalog", type=Path, required=True)
        command.add_argument(
            "--releases-dir", type=Path, required=True,
            help="directory holding each referenced v2 release document, either "
                 "<build>/release.json or <build>.json",
        )
        if name == "assert-unreferenced":
            command.add_argument(
                "--target", required=True,
                help="bucket-root prefix proposed for deletion, e.g. "
                     "slice-2026-08-04.0 or 2026-07-17.0",
            )

    args = parser.parse_args(argv)

    try:
        if args.command == "assert-unreferenced":
            target = args.target.strip("/")
            if not is_bucket_root_prefix(target):
                raise GuardError(
                    f"--target {target!r} is not a bucket-root prefix "
                    "(slice-YYYY-MM-DD.N or YYYY-MM-DD.N). Refusing to reason "
                    "about a partial key."
                )
            found, read = collect_v2_references(args.v2_catalog, args.releases_dir)
            print(f"checked v2 catalog and {len(read)} release document(s): "
                  f"{', '.join(read) if read else 'none'}")
            if target in found:
                print(
                    f"::error::{target} is still referenced by the v2 catalog "
                    "chain - aborting",
                    file=sys.stderr,
                )
                return 1
            print(f"{target} is not referenced by the v2 catalog chain "
                  f"({len(found)} prefixes are)")
        else:
            found, read = collect_v2_references(args.v2_catalog, args.releases_dir)
            for prefix in sorted(found):
                print(prefix)
    except GuardError as error:
        print(f"::error::{error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
