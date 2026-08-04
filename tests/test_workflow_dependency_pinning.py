"""Every workflow dependency is pinned at authoring time.

The v4 build/promotion session burned two preview attempts on first-execution
failures that a pin would have prevented outright:

* an unpinned ``cargo install worker-build`` resolved to 0.8.x, which refuses
  ``worker = "0.7"`` (hotfixed in #243 by pinning ``^0.7``);
* a hosted runner had no Python ``requests`` for the benchmark step (#245).

Both surfaced inside a measurement window, because the workflows that carry
them run once every few weeks. The cold-start smoke workflow executes the setup
steps so drift is caught by CI; this test is the cheaper, earlier half of the
same gate -- it refuses an unpinned dependency at authoring time, in the same
`pytest tests/` run that already gates every PR.

Scope, stated precisely. This is a source-text check over
``.github/workflows/*.yml``. It knows nothing about whether a pinned version
exists or works; that is the cold-start smoke's job. It checks three things:

1. ``uses:`` refs are a 40-hex commit SHA, an immutable ``vN``-style tag, or a
   local ``./`` path -- never a floating branch like ``@main`` or ``@stable``.
2. ``cargo install`` names a ``--version``.
3. ``npm install`` of a named package carries ``@<version>``.
4. ``pip install`` of a named package carries ``==``, unless the whole install
   is a hash-pinned requirements file.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).parents[1]
WORKFLOW_DIR = ROOT / ".github/workflows"

# A ref that cannot move: a full commit SHA, or a release tag. Tags are
# mutable in principle; `vN` major tags are the convention this repo already
# uses widely and re-pointing one is a visible upstream act, whereas a branch
# ref moves silently on every upstream push. Branch refs are what this test
# exists to stop.
SHA_REF = re.compile(r"^[0-9a-f]{40}$")
TAG_REF = re.compile(r"^v\d+(\.\d+)*$")

USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)")

# `pip install` / `npm install` / `cargo install` anywhere in a run: block,
# including inside a multi-line script.
CARGO_INSTALL = re.compile(r"\bcargo install\b([^\n|&;]*)")
NPM_INSTALL = re.compile(r"\bnpm (?:install|i)\b([^\n|&;]*)")
PIP_INSTALL = re.compile(r"\b(?:pip|python -m pip|python3 -m pip) install\b([^\n|&;]*)")

# Flags that carry no package name.
_FLAG = re.compile(r"^-")


def workflow_files() -> list[Path]:
    files = sorted(WORKFLOW_DIR.glob("*.yml"))
    # A glob that silently collapses would turn this gate into a no-op that
    # still exits 0. The repo carries well over a dozen workflows.
    assert len(files) >= 15, f"only found {len(files)} workflows; the glob collapsed"
    return files


def executable_text(path: Path) -> str:
    """Workflow source with comment tails removed.

    Comments in these workflows quote the very commands this test forbids (the
    #243 rationale block names ``cargo install worker-build``), so scanning raw
    text produces false positives. Dropping from the first ``#`` on each line is
    crude but conservative in the right direction for a `run:` block: prose
    stops matching, real commands keep matching.
    """
    lines = []
    for line in path.read_text().splitlines():
        head = line.split("#", 1)[0]
        lines.append(head)
    return "\n".join(lines)


def _arguments(tail: str) -> list[str]:
    """Bare (non-flag) arguments of an install command, in order."""
    return [token for token in tail.split() if not _FLAG.match(token)]


def _is_local_path(name: str) -> bool:
    """True for an editable/local install target such as ``.`` or ``".[dev]"``."""
    return name.strip("\"'").startswith((".", "/"))


def test_no_workflow_action_is_pinned_to_a_floating_branch():
    offenders: list[str] = []
    for path in workflow_files():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            match = USES.match(line)
            if match is None:
                continue
            reference = match.group(1)
            if reference.startswith("./"):
                # A local reusable workflow moves with this repo's own commit.
                continue
            if "@" not in reference:
                offenders.append(f"{path.name}:{number}: {reference} has no ref")
                continue
            ref = reference.rsplit("@", 1)[1]
            if SHA_REF.match(ref) or TAG_REF.match(ref):
                continue
            offenders.append(f"{path.name}:{number}: {reference} is a floating ref")
    assert not offenders, (
        "workflow actions pinned to a moving ref:\n  " + "\n  ".join(offenders)
    )


def test_every_cargo_install_pins_a_version():
    offenders: list[str] = []
    for path in workflow_files():
        for match in CARGO_INSTALL.finditer(executable_text(path)):
            tail = match.group(1)
            packages = _arguments(tail)
            if not packages:
                # `cargo install --list` names nothing to install.
                continue
            if "--version" in tail:
                continue
            # `cargo install pkg@^0.7` is equally pinned.
            if any("@" in argument for argument in packages):
                continue
            offenders.append(f"{path.name}: cargo install{tail}")
    assert not offenders, (
        "unpinned `cargo install` -- worker-build 0.8 broke the preview build "
        "exactly this way (#243):\n  " + "\n  ".join(offenders)
    )


def test_every_npm_install_pins_a_version():
    offenders: list[str] = []
    for path in workflow_files():
        for match in NPM_INSTALL.finditer(executable_text(path)):
            tail = match.group(1)
            packages = _arguments(tail)
            if not packages:
                # `npm ci` / bare `npm install` installs from a lockfile.
                continue
            unpinned = [name for name in packages if "@" not in name.lstrip("@")]
            if not unpinned:
                continue
            offenders.append(f"{path.name}: npm install {' '.join(unpinned)}")
    assert not offenders, (
        "unpinned `npm install` of a named package:\n  " + "\n  ".join(offenders)
    )


def test_every_pip_install_pins_a_version():
    offenders: list[str] = []
    for path in workflow_files():
        for match in PIP_INSTALL.finditer(executable_text(path)):
            tail = match.group(1)
            if "--require-hashes" in tail or "-r " in tail:
                # A hash-pinned requirements file is the strongest pin there is.
                continue
            packages = _arguments(tail)
            unpinned = [
                name
                for name in packages
                if "==" not in name and not _is_local_path(name)
            ]
            # `pip install --upgrade pip` and `pip install -e .` name no
            # third-party version surface worth pinning.
            unpinned = [name for name in unpinned if name != "pip"]
            if not unpinned:
                continue
            offenders.append(f"{path.name}: pip install {' '.join(unpinned)}")
    assert not offenders, (
        "unpinned `pip install` -- a hosted runner missing `requests` cost a "
        "preview attempt (#245):\n  " + "\n  ".join(offenders)
    )


def test_cold_start_smoke_executes_the_rare_workflow_setup_steps():
    """The smoke must actually run the install lines, not just lint them."""
    smoke = (WORKFLOW_DIR / "cold-start-smoke.yml").read_text()
    preview = (WORKFLOW_DIR / "preview-v2-candidate.yml").read_text()

    # The two install lines that failed, byte-for-byte identical to preview's.
    for line in (
        "cargo install worker-build --version '^0.7' --locked",
        "npm install --global wrangler@4.118.0",
    ):
        assert line in preview, f"preview no longer contains: {line}"
        assert line in smoke, f"cold-start smoke no longer exercises: {line}"

    # The hash-pinned install of BOTH requirement files -- #245 was a package
    # present only in requirements-preview-v2.txt.
    assert "-r .github/requirements-hosted-rowgroup.txt" in smoke
    assert "-r .github/requirements-preview-v2.txt" in smoke
    assert "import requests" in smoke or '"requests",' in smoke

    # Installing the right worker-build is not the same as building with it.
    assert "worker-build --release" in smoke

    # Every rarely-run workflow named in the forensics must be covered by the
    # entrypoint import sweep.
    for workflow in (
        "preview-v2-candidate.yml",
        "promote-v2-release.yml",
        "reverse-v2.yml",
        "release-slice-families.yml",
    ):
        assert f"--workflow .github/workflows/{workflow}" in smoke, (
            f"cold-start smoke does not cover {workflow}"
        )


def test_check_hosted_imports_extra_workflows_can_only_widen_the_set():
    import importlib.util
    import sys

    path = ROOT / "scripts" / "check_hosted_imports.py"
    spec = importlib.util.spec_from_file_location("check_hosted_imports_pin", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_hosted_imports_pin"] = module
    spec.loader.exec_module(module)

    baseline = module.discover()
    widened = module.discover(
        extra_workflows=(".github/workflows/preview-v2-candidate.yml",)
    )
    assert set(baseline) <= set(widened)
    assert "benchmark_v2_forward" in widened, (
        "the preview benchmark entrypoint -- the #245 failure -- is not derived"
    )
