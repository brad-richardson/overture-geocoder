#!/usr/bin/env python3
"""Single catalog-prune implementation shared by both maintenance workflows.

Two callers with different invariants used to carry their own inline-Python
catalog mutation. This module is the one tested implementation for both:

* ``retain`` — the rebuild finalizer's retention prune: keep the newest
  ``--keep`` versions, require the just-promoted ``--current`` build to be the
  catalog's latest, and refuse to drop below the retention floor. Produces the
  pruned catalog plus the list of versions to delete (the caller never prunes
  ``--current`` because it is always kept as newest).
* ``allowlist`` — the standalone R2 cleanup: remove an explicit set of versions,
  never the ``latest`` link, and keep at least ``--floor`` children.
* ``assert-unreferenced`` — the cleanup delete guard: exit non-zero if a version
  is still referenced by an *exact* child link-target (replacing a substring
  ``grep`` that could both false-match a version embedded in another link and
  miss the real reference).

The retention floor / worker fallback depth (MAX_VERSION_ATTEMPTS) is supplied
by each caller so it stays pinned to the worker constant in the workflow.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path


VERSION_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.\d+$")


class PruneError(RuntimeError):
    """A prune invariant was violated; the caller must abort."""


def _version_key(value: str) -> tuple[int, int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise PruneError(f"invalid catalog version {value!r}")
    date_part, suffix = value.rsplit(".", 1)
    year, month, day = (int(part) for part in date_part.split("-"))
    parsed = date(year, month, day)  # reject impossible calendar dates
    return parsed.year, parsed.month, parsed.day, int(suffix)


def _child_version(link: dict) -> str:
    href = link.get("href", "")
    return href.strip("./").split("/")[0]


def prune_by_retention(catalog: dict, *, keep: int, current: str):
    """Keep the ``keep`` newest children; return ``(pruned_catalog, dropped)``.

    Mirrors the rebuild finalizer's retention guards: the current build must be
    the catalog's newest child and the catalog must not fall below ``keep``.
    """
    if keep < 1:
        raise PruneError(f"retention keep must be >= 1, got {keep}")
    static: list[dict] = []
    children: list[tuple[tuple, str, dict]] = []
    for link in catalog.get("links", []):
        if link.get("rel") != "child":
            static.append(link)
            continue
        version = _child_version(link)
        children.append((_version_key(version), version, link))

    children.sort(key=lambda item: item[0], reverse=True)
    if not children or children[0][1] != current:
        raise PruneError("current build is not catalog latest")
    if len(children) < keep:
        raise PruneError("refusing to prune below retention floor")

    kept = children[:keep]
    dropped = children[keep:]
    pruned = dict(catalog)
    pruned["links"] = static + [link for _, _, link in kept]
    return pruned, [version for _, version, _ in dropped]


def prune_by_allowlist(catalog: dict, *, prune: set[str], floor: int):
    """Remove children whose version is in ``prune``; return
    ``(pruned_catalog, removed)``.

    Mirrors the cleanup guards: never remove the ``latest`` link, and keep at
    least ``floor`` children with a ``latest`` still present.
    """
    if floor < 1:
        raise PruneError(f"retention floor must be >= 1, got {floor}")
    prune = set(prune)
    kept_links: list[dict] = []
    removed: list[str] = []
    for link in catalog.get("links", []):
        if link.get("rel") == "child":
            version = _child_version(link)
            if version in prune:
                if link.get("latest"):
                    raise PruneError(f"refusing to prune latest: {version}")
                removed.append(version)
                continue
        kept_links.append(link)

    children = [link for link in kept_links if link.get("rel") == "child"]
    if len(children) < floor:
        raise PruneError(f"would leave only {len(children)} versions")
    if not any(link.get("latest") for link in children):
        raise PruneError("no latest flag would remain")

    pruned = dict(catalog)
    pruned["links"] = kept_links
    return pruned, removed


def is_referenced(catalog: dict, version: str) -> bool:
    """True iff a child link's exact version-target equals ``version``."""
    for link in catalog.get("links", []):
        if link.get("rel") == "child" and _child_version(link) == version:
            return True
    return False


def _load(path: Path) -> dict:
    with path.open() as src:
        value = json.load(src)
    if not isinstance(value, dict):
        raise PruneError(f"{path} must contain a JSON object")
    return value


def _write(path: Path, catalog: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    retain = subparsers.add_parser("retain")
    retain.add_argument("--catalog", type=Path, required=True)
    retain.add_argument("--keep", type=int, required=True)
    retain.add_argument("--current", required=True)
    retain.add_argument("--output", type=Path, required=True)
    retain.add_argument("--dropped", type=Path, required=True)

    allow = subparsers.add_parser("allowlist")
    allow.add_argument("--catalog", type=Path, required=True)
    allow.add_argument("--prune", required=True, help="space-separated versions")
    allow.add_argument("--floor", type=int, required=True)
    allow.add_argument("--output", type=Path, required=True)

    check = subparsers.add_parser("assert-unreferenced")
    check.add_argument("--catalog", type=Path, required=True)
    check.add_argument("--version", required=True)

    args = parser.parse_args()
    try:
        if args.command == "retain":
            pruned, dropped = prune_by_retention(
                _load(args.catalog), keep=args.keep, current=args.current
            )
            _write(args.output, pruned)
            args.dropped.write_text("".join(f"{version}\n" for version in dropped))
            print(f"Retained {args.keep} newest; dropping {len(dropped)}: {dropped}")
        elif args.command == "allowlist":
            pruned, removed = prune_by_allowlist(
                _load(args.catalog), prune=set(args.prune.split()), floor=args.floor
            )
            _write(args.output, pruned)
            children = [link for link in pruned["links"] if link.get("rel") == "child"]
            print(f"removed {removed}, {len(children)} children remain")
        else:  # assert-unreferenced
            if is_referenced(_load(args.catalog), args.version):
                print(
                    f"::error::{args.version} still referenced by catalog.json - aborting",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"{args.version} is unreferenced; safe to delete.")
    except PruneError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
