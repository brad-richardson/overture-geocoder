#!/usr/bin/env python3
"""Import every hosted entrypoint module under only the hash-pinned deps.

**Scope, stated precisely.** This catches TOP-LEVEL import failures only: a
syntax error, or a module-level `import x` where `x` is missing from
`.github/requirements-hosted-rowgroup.txt`. It does NOT catch a
function-level import of a missing dependency -- which is what #150 actually
was (`psutil` is imported inside `run_bounded`). #150's class is caught by the
slice job, which executes the map phase and therefore reaches that call site.
Do not claim more for this check than it does: it is a cheap, fast tripwire on
the import graph, not data-plane coverage.

The module set is DERIVED from the workflows rather than hardcoded, so a new
`scripts/x.py` invocation in a hosted workflow is covered without touching this
file. The derivation is deliberately literal -- it matches
`scripts/<lower_snake_name>.py` as written in the workflow text. It does NOT
see `python -m pkg`, a script path built from a shell variable or matrix value,
a name with hyphens or uppercase, a heredoc-constructed path, or `importlib`
indirection. Add such an entrypoint to EXTRA_MODULES by hand; the module-count
floor below is the backstop that notices if the derivation silently collapses.

Modules that parse arguments at import time cannot be imported and are listed in
EXECUTED_AT_IMPORT; they are covered by actually running them (the slice
harness) instead.
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
    # The R2 staging transport for the intermediate store. Loaded by
    # construction_v1_hosted via importlib, so the workflow grep never names it.
    "construction_staging_v1",
    "construction_v1_remote",
    "pack_schemas_v1",
    "places_construction_v1",
    "places_inventory_v1",
    "places_partition",
    "r2_verified_store",
)

# Top-level argparse at import time: running these IS the test.
# This module itself is excluded: importing the importer proves nothing.
EXECUTED_AT_IMPORT = frozenset({"run_slice_construction_v1", "check_hosted_imports"})

# Floor on the derived set. A workflow rename, a regex that stops matching, or a
# refactor that collapses the derivation would otherwise silently narrow this
# check to almost nothing while still exiting 0. The real set is ~13; this is
# deliberately loose enough not to churn and tight enough to catch a collapse.
MINIMUM_MODULES = 10

REFERENCE = re.compile(r"scripts/([a-z0-9_]+)\.py")


def discover(root: Path = ROOT, extra_workflows: tuple[str, ...] = ()) -> list[str]:
    names: set[str] = set(EXTRA_MODULES)
    for relative in (*WORKFLOWS, *extra_workflows):
        path = root / relative
        if not path.exists():
            # Hard error, not a skip: a renamed or deleted workflow silently
            # shrinks the derived set, which is the exact failure this check is
            # supposed to make impossible.
            raise SystemExit(
                f"{relative} does not exist. check_hosted_imports derives its "
                "module set from it; update WORKFLOWS deliberately rather than "
                "letting the derived set shrink silently."
            )
        names.update(REFERENCE.findall(path.read_text()))
    discovered = sorted(names - EXECUTED_AT_IMPORT)
    if len(discovered) < MINIMUM_MODULES:
        raise SystemExit(
            f"derived only {len(discovered)} hosted entrypoint modules "
            f"({', '.join(discovered)}); the floor is {MINIMUM_MODULES}. The "
            "derivation has collapsed -- fix it rather than lowering the floor."
        )
    return discovered


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--list", action="store_true", help="Print the derived module set and exit."
    )
    parser.add_argument(
        "--workflow",
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "Additional workflow to derive entrypoint modules from, on top of "
            "the fixed hosted set. Used by the cold-start smoke to cover the "
            "rarely-run workflows (preview, promote, reverse, release) whose "
            "first execution is always inside a measurement window. Widens the "
            "checked set only -- it can never shrink it."
        ),
    )
    args = parser.parse_args(argv)

    modules = discover(extra_workflows=tuple(args.workflow))
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
