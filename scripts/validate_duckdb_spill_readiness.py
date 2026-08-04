#!/usr/bin/env python3
"""Fail-closed readiness check for every DuckDB stage's spill allowance.

DuckDB spill caps are this pipeline's recurring planet-scale killer. The v4 head
tree-merge died at 79% with the 8.5 GiB half-share cap full and was recovered
only by a scoped `head_only_resume` raising that head to 13,690,208,256 of
18,253,611,008 B; a rehearsal task died on a 2 GiB temp allowance; a category
probe died twice. Every one of those was a run that had already spent hours.

What was missing was never the cap -- the cap was emitted. What was missing was
the PEAK. Nothing sampled the temp directory, so no stage could say how much of
its allowance it actually used, and every allowance was therefore a guess that
happened to hold until it didn't.

This check closes that loop:

* `benchmarks/duckdb-spill-allowances-v1.json` states each production stage's
  allowance, how it is derived, and its measured peak.
* `StageWatchdog` emits a `duckdb_spill_observation` line per instrumented
  stage, carrying the measured peak against the cap that stage configured.
* This script asserts `allowance >= measured_peak * (1 + headroom)` for every
  declared stage, and **fails closed on a stage with no measurement** rather
  than assuming an unmeasured stage fits.

Failing closed on absence is the point, not an accident. Today every stage is
unmeasured, so this check does not pass, and the message on each stage names the
run that would resolve it. That is the honest state; a check that passed today
would be asserting exactly the thing that has never been true.

Scope. This validates STATED allowances against OBSERVED peaks. It does not
compute allowances (the two helpers in the construction modules do), does not
run DuckDB, and cannot tell whether a peak was observed under a representative
input volume -- `observations` and `peak_sweep_seconds` are carried through so a
blind sampler is visible rather than silently read as "did not spill".
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWANCES = ROOT / "benchmarks/duckdb-spill-allowances-v1.json"

SCHEMA = "overture-duckdb-spill-readiness-v1"
ALLOWANCES_SCHEMA = "overture-duckdb-spill-allowances-v1"
OBSERVATION_SCHEMA = "overture-duckdb-spill-observation-v1"
OBSERVATION_KEY = "duckdb_spill_observation"

# A sampler that took one sweep saw one instant. A peak of zero over fewer than
# this many observations is "not observed", not "did not spill", and must not be
# accepted as a measurement -- that would let a stage pass by being watched
# badly, which is strictly worse than not being watched.
MINIMUM_OBSERVATIONS = 2


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError as error:
        raise SystemExit(f"{path} does not exist") from error
    except ValueError as error:
        raise SystemExit(f"{path} is not valid JSON: {error}") from error


def load_observations(path: Path) -> list[dict[str, Any]]:
    """Observations from a JSON document or from a mixed workflow log.

    Both forms matter. A JSON array is what a test or a harness produces; a
    line-oriented scan is what makes a raw Actions job log usable directly,
    which is where these lines are actually going to be found first.
    """
    if not path.exists():
        raise SystemExit(f"{path} does not exist")
    text = path.read_text()
    found: list[dict[str, Any]] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("schema") == OBSERVATION_SCHEMA:
                found.append(value)
            elif isinstance(value.get(OBSERVATION_KEY), dict):
                found.append(value[OBSERVATION_KEY])
        elif isinstance(value, list):
            for item in value:
                collect(item)

    try:
        collect(json.loads(text))
    except ValueError:
        pass
    if found:
        return found

    for line in text.splitlines():
        stripped = line.strip()
        start = stripped.find("{")
        if start < 0:
            continue
        try:
            collect(json.loads(stripped[start:]))
        except ValueError:
            continue
    return found


def fold_observations(
    observations: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Worst case per stage: the maximum peak, and the weakest sampling."""
    folded: dict[str, dict[str, Any]] = {}
    for record in observations:
        stage = record.get("stage")
        if not isinstance(stage, str) or not stage:
            continue
        peak = record.get("peak_duckdb_temp_bytes")
        if not isinstance(peak, int) or isinstance(peak, bool) or peak < 0:
            continue
        current = folded.get(stage)
        if current is None:
            folded[stage] = {
                "peak_duckdb_temp_bytes": peak,
                "observations": int(record.get("observations") or 0),
                "runs": 1,
                "any_run_failed": bool(record.get("stage_failed")),
                "duckdb_temp_cap_bytes": record.get("duckdb_temp_cap_bytes"),
            }
            continue
        current["peak_duckdb_temp_bytes"] = max(
            current["peak_duckdb_temp_bytes"], peak
        )
        # The weakest sampling across runs, not the best: a single blind run is
        # enough to make the folded peak untrustworthy.
        current["observations"] = min(
            current["observations"], int(record.get("observations") or 0)
        )
        current["runs"] += 1
        current["any_run_failed"] = current["any_run_failed"] or bool(
            record.get("stage_failed")
        )
    return folded


def validate(
    allowances_path: Path, observation_paths: list[Path]
) -> dict[str, Any]:
    document = load_json(allowances_path)
    blockers: list[str] = []

    if not isinstance(document, dict) or document.get("schema") != ALLOWANCES_SCHEMA:
        raise SystemExit(
            f"{allowances_path} is not an {ALLOWANCES_SCHEMA} document"
        )
    headroom = document.get("headroom_fraction")
    if not isinstance(headroom, (int, float)) or not 0 < float(headroom) < 1:
        raise SystemExit(
            "headroom_fraction must be a fraction strictly between 0 and 1; "
            f"got {headroom!r}"
        )
    declared = document.get("stages")
    if not isinstance(declared, list) or not declared:
        raise SystemExit(f"{allowances_path} declares no stages")

    observations: list[dict[str, Any]] = []
    for path in observation_paths:
        observations.extend(load_observations(path))
    observed = fold_observations(observations)

    stages: dict[str, Any] = {}
    for entry in declared:
        stage = entry.get("stage")
        if not isinstance(stage, str) or not stage:
            raise SystemExit(f"{allowances_path} has a stage entry with no name")
        allowance = entry.get("allowance_bytes")
        if not isinstance(allowance, int) or isinstance(allowance, bool) or allowance <= 0:
            blockers.append(
                f"{stage}: no positive allowance_bytes is stated, so there is "
                "nothing to check a peak against"
            )
            stages[stage] = {"allowance_bytes": allowance, "measured_peak_bytes": None}
            continue

        # Precedence is deliberate: a fresh observation from this run's logs
        # beats the checked-in figure, because the checked-in figure is only
        # ever a fold of an earlier run's observation.
        measurement = observed.get(stage)
        peak = entry.get("measured_peak_bytes")
        source = "allowances file"
        sampling_ok = True
        if measurement is not None:
            peak = measurement["peak_duckdb_temp_bytes"]
            source = "observations"
            sampling_ok = (
                measurement["observations"] >= MINIMUM_OBSERVATIONS or peak > 0
            )

        record: dict[str, Any] = {
            "allowance_bytes": allowance,
            "measured_peak_bytes": peak if isinstance(peak, int) else None,
            "measurement_source": source if isinstance(peak, int) else None,
            "required_bytes": None,
        }
        if measurement is not None:
            record["observations"] = measurement["observations"]
            record["runs"] = measurement["runs"]
            record["any_run_failed"] = measurement["any_run_failed"]
        stages[stage] = record

        if not isinstance(peak, int) or isinstance(peak, bool) or peak < 0:
            lower_bound = entry.get("measured_peak_lower_bound_bytes")
            detail = entry.get("measurement_note") or ""
            bound = (
                f" The only figure available is a LOWER BOUND of {lower_bound} B "
                "from a run that died on its cap, which is not a peak."
                if isinstance(lower_bound, int)
                else ""
            )
            blockers.append(
                f"{stage}: allowance {allowance} B is stated but the stage has "
                "NO measured spill peak, so the allowance is unverified."
                f"{bound} Run the stage with the instrumented StageWatchdog and "
                f"fold its `{OBSERVATION_KEY}` line into "
                f"{allowances_path.name}, or pass the run log with "
                f"--observations. {detail}".rstrip()
            )
            continue

        if not sampling_ok:
            blockers.append(
                f"{stage}: the observed peak is 0 B over "
                f"{measurement['observations']} watchdog observation(s). That is "
                "an unobserved stage, not a stage that did not spill; it must "
                "not be accepted as a measurement."
            )
            continue

        required = int(peak * (1 + float(headroom)))
        record["required_bytes"] = required
        record["used_fraction_of_allowance"] = peak / allowance
        if allowance < required:
            blockers.append(
                f"{stage}: allowance {allowance} B is below the measured peak "
                f"{peak} B plus {float(headroom) * 100:.0f}% headroom "
                f"({required} B). The stage is one input-volume increase from "
                "dying on its spill cap mid-run."
            )

    # A stage that spills but is not declared has no stated allowance at all,
    # which is the same failure as an unmeasured one seen from the other side.
    for stage in sorted(set(observed) - set(stages)):
        blockers.append(
            f"{stage}: observed spilling but is not declared in "
            f"{allowances_path.name}. Every production DuckDB stage must state "
            "an allowance."
        )
        stages[stage] = {
            "allowance_bytes": None,
            "measured_peak_bytes": observed[stage]["peak_duckdb_temp_bytes"],
            "measurement_source": "observations",
            "required_bytes": None,
        }

    return {
        "schema": SCHEMA,
        "ready": not blockers,
        "headroom_fraction": float(headroom),
        "allowances": str(allowances_path),
        "observation_records": len(observations),
        "blockers": blockers,
        "stages": stages,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allowances", type=Path, default=DEFAULT_ALLOWANCES)
    parser.add_argument(
        "--observations",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help=(
            "A JSON document of duckdb_spill_observation records, or a raw run "
            "log to scan for them. Repeatable."
        ),
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    report = validate(args.allowances, list(args.observations))
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
    print(text, end="")
    if not report["ready"]:
        for blocker in report["blockers"]:
            print(f"BLOCKER {blocker}")
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
