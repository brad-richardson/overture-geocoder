#!/usr/bin/env python3
"""Import every hosted entrypoint module under only the hash-pinned deps.

Missing-dependency defects are the cheapest possible way to kill a hosted
construction-v1 dispatch, and they are invisible to a dry-run: #150 died minutes
into an execute because `psutil` was imported by a module the dry-run branch
never loaded. This walks the modules the hosted workflows actually invoke and
imports each one, so the same class of defect fails in CI in seconds.

The module set is DERIVED from the workflows rather than hardcoded, so a new
`scripts/x.py` invocation in a hosted workflow is covered without touching this
file. Modules that parse arguments at import time cannot be imported and are
listed in EXECUTED_AT_IMPORT; they are covered by actually running them (the
slice harness) instead.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Workflows whose steps run hosted construction-v1 Python on a runner.
WORKFLOWS = (
    ".github/workflows/construction-v1.yml",
    ".github/workflows/slice-smoke.yml",
)

# Modules reachable only through the slice harness / control surfaces, which the
# workflow greps do not name directly.
EXTRA_MODULES = (
    "address_construction_v1",
    "address_partition",
    "construction_v1_remote",
    "pack_schemas_v1",
    "places_construction_v1",
    "places_inventory_v1",
    "places_partition",
    "r2_verified_store",
)

# Top-level argparse at import time: running these IS the test.
EXECUTED_AT_IMPORT = frozenset({"run_slice_construction_v1"})

REFERENCE = re.compile(r"scripts/([a-z0-9_]+)\.py")


def discover(root: Path = ROOT) -> list[str]:
    names: set[str] = set(EXTRA_MODULES)
    for relative in WORKFLOWS:
        path = root / relative
        if not path.exists():
            continue
        names.update(REFERENCE.findall(path.read_text()))
    return sorted(names - EXECUTED_AT_IMPORT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="Print the derived module set and exit."
    )
    args = parser.parse_args(argv)

    modules = discover()
    if args.list:
        print("\n".join(modules))
        return 0

    sys.path.insert(0, str(ROOT / "scripts"))
    failures: list[str] = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as error:  # noqa: BLE001 - report every failure at once
            failures.append(f"{name}: {type(error).__name__}: {error}")
            print(f"FAIL {name}: {type(error).__name__}: {error}")
        else:
            print(f"ok   {name}")

    if failures:
        print(
            f"\n{len(failures)} hosted entrypoint module(s) failed to import under "
            "the hash-pinned dependency set. A hosted dispatch would die on this.",
            file=sys.stderr,
        )
        return 1
    print(f"\nall {len(modules)} hosted entrypoint modules import cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
