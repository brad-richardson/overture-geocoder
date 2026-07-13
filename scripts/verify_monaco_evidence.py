#!/usr/bin/env python3
"""Generate or verify the pinned Monaco subset-equivalence evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
FORWARD_SQL = ROOT / "scripts/download_divisions_global.sql"
REVERSE_SQL = ROOT / "scripts/download_divisions_area.sql"
CONTRACT = ROOT / "scripts/monaco_smoke_contract.json"
BUILD_SHARDS = ROOT / "scripts/build_shards.py"
EXPORT_COMPARATOR = ROOT / "scripts/verify_monaco_export.py"
SMOKE_EXPORTER = ROOT / "scripts/download_divisions_smoke.py"
EVIDENCE_GENERATOR = ROOT / "scripts/verify_monaco_evidence.py"
PINNED_DUCKDB_VERSION = "1.5.1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return payload


def _input_hashes() -> dict[str, str]:
    return {
        "forward": _sha256(FORWARD_SQL),
        "reverse": _sha256(REVERSE_SQL),
        "contract": _sha256(CONTRACT),
        "build_shards": _sha256(BUILD_SHARDS),
        "export_comparator": _sha256(EXPORT_COMPARATOR),
        "smoke_exporter": _sha256(SMOKE_EXPORTER),
        "evidence_generator": _sha256(EVIDENCE_GENERATOR),
    }


def _require_equivalent(report: dict[str, Any], *, built: bool) -> None:
    for family in ("forward", "reverse"):
        result = report.get(family, {})
        if result.get("legacy_only_rows") != 0 or result.get("subset_only_rows") != 0:
            raise RuntimeError(f"{family} equivalence report contains drift")
        if not isinstance(result.get("rows"), int) or result["rows"] <= 0:
            raise RuntimeError(f"{family} equivalence report contains no rows")
    if built:
        comparisons = report.get("built_shards", {})
        for family in ("forward", "reverse", "router"):
            comparison = comparisons.get(family, {})
            if comparison.get("logical_contents_equal") is not True:
                raise RuntimeError(f"built {family} equivalence is not proven")
            if comparison.get("legacy_snapshot_sha256") != comparison.get(
                "subset_snapshot_sha256"
            ):
                raise RuntimeError(f"built {family} snapshot hashes differ")
            if comparison.get("legacy_table_row_counts") != comparison.get(
                "subset_table_row_counts"
            ):
                raise RuntimeError(f"built {family} table row counts differ")


def _portable_file_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "path"}


def build_evidence(
    profile_path: Path,
    global_equivalence_path: Path,
    baseline_equivalence_path: Path,
) -> dict[str, Any]:
    profile = _load(profile_path)
    global_equivalence = _load(global_equivalence_path)
    baseline_equivalence = _load(baseline_equivalence_path)
    _require_equivalent(global_equivalence, built=True)
    _require_equivalent(baseline_equivalence, built=True)
    input_hashes = _input_hashes()
    if profile.get("sql_sha256") != {
        key: input_hashes[key] for key in ("forward", "reverse", "contract")
    }:
        raise RuntimeError("profile is stale for the current SQL or contract")
    release = profile["overture_release"]
    if global_equivalence.get("overture_release") != release:
        raise RuntimeError("full-global equivalence release differs from the profile")
    if baseline_equivalence.get("overture_release") != release:
        raise RuntimeError("no-bbox equivalence release differs from the profile")
    baseline = profile.get("authoritative_baseline")
    if not baseline:
        raise RuntimeError("profile has no no-bbox authoritative baseline")
    for family in ("forward", "reverse"):
        output_sha = profile["outputs"][family]["sha256"]
        if global_equivalence[family].get(
            "logical_rows_sha256"
        ) != baseline_equivalence[family].get("logical_rows_sha256"):
            raise RuntimeError(
                f"full-global {family} report is not logically bound to the "
                "current equivalence report"
            )
        if baseline_equivalence[family].get("subset_sha256") != output_sha:
            raise RuntimeError(
                f"no-bbox {family} report is not bound to the profile output"
            )
        if baseline_equivalence[family].get("legacy_sha256") != baseline[family][
            "sha256"
        ]:
            raise RuntimeError(
                f"no-bbox {family} report is not bound to its baseline output"
            )
    scans = profile["scan_profiles"]
    compact_scans = {}
    for family in ("division", "division_area"):
        scan = scans[family]
        compact_scans[family] = {
            key: scan[key]
            for key in (
                "result_rows",
                "total_row_groups",
                "metadata_eligible_row_groups",
                "total_row_group_bytes",
                "metadata_eligible_row_group_bytes",
                "total_rows",
                "metadata_eligible_row_group_rows",
                "duckdb_rows_scanned_counter",
                "plan_has_country_filter",
                "plan_has_bbox_filter",
                "plan_sha256",
                "profile_sha256",
                "profiled_predicate",
            )
        }
    raw_baseline = baseline
    baseline = None
    if raw_baseline:
        baseline = {
            **raw_baseline,
            "forward": _portable_file_record(raw_baseline["forward"]),
            "reverse": _portable_file_record(raw_baseline["reverse"]),
        }
    if not baseline:
        raise RuntimeError("profile has no no-bbox authoritative baseline")
    return {
        "evidence_version": 2,
        "overture_release": profile["overture_release"],
        "comparison": (
            "shared-production-sql; full-global MC filter and no-bbox baseline "
            "use schema plus EXCEPT ALL across every output column"
        ),
        "derived_bbox": profile["derived_bbox"],
        "validated_source": {
            "division_rows": len(profile["selected_division_ids"]),
            "division_area_rows": profile["selected_area_count"],
            "division_ids": profile["selected_division_ids"],
            "division_area_ids": profile["selected_area_ids"],
            "geometry_sha256": profile["source_geometry_sha256"],
        },
        "pushdown": compact_scans,
        "logical_equivalence_to_full_global": global_equivalence,
        "logical_equivalence_to_no_bbox_baseline": baseline_equivalence,
        "outputs": {
            family: _portable_file_record(profile["outputs"][family])
            for family in ("forward", "reverse")
        },
        "authoritative_baseline": baseline,
        "timings_seconds": profile["timings_seconds"],
        "input_sha256": input_hashes,
        "rendered_sql_sha256": profile["rendered_sql_sha256"],
        "provenance": {
            key: profile["provenance"][key]
            for key in (
                "duckdb_python_version",
                "duckdb_cli_version",
                "command",
            )
        }
        | {"base_git_sha": profile["provenance"]["git_sha"]},
        "notes": [
            "The recurring smoke runtime excludes the one-time no-bbox baseline.",
            "Metadata-eligible row groups describe the country+bbox fast branch. "
            "The saved executed area profile uses the actual rendered ownership OR.",
            "DuckDB 1.5.1 reports rows_scanned as twice total source rows for these "
            "remote nested-column scans, so the raw counter is retained but is not a gate.",
        ],
    }


def verify_evidence(evidence_path: Path) -> dict[str, Any]:
    evidence = _load(evidence_path)
    if evidence.get("evidence_version") != 2:
        raise RuntimeError("unsupported Monaco evidence version")
    expected_hashes = _input_hashes()
    if evidence.get("input_sha256") != expected_hashes:
        raise RuntimeError("Monaco evidence is stale for the current SQL or contract")
    _require_equivalent(evidence["logical_equivalence_to_full_global"], built=True)
    _require_equivalent(
        evidence["logical_equivalence_to_no_bbox_baseline"], built=True
    )
    validated = evidence["validated_source"]
    if validated.get("division_rows") != len(validated.get("division_ids", [])):
        raise RuntimeError("validated division count does not match its ID inventory")
    if validated.get("division_area_rows") != len(
        validated.get("division_area_ids", [])
    ):
        raise RuntimeError("validated division-area count does not match its ID inventory")
    contract = _load(CONTRACT)
    required_divisions = {
        item["id"] for item in contract.get("required_divisions", [])
    }
    required_areas = {item["id"] for item in contract.get("required_areas", [])}
    division_ids = set(validated.get("division_ids", []))
    area_ids = set(validated.get("division_area_ids", []))
    if not required_divisions.issubset(division_ids):
        raise RuntimeError("evidence omits a contract-required division")
    if required_areas != area_ids:
        raise RuntimeError("evidence division-area inventory differs from the contract")
    geometry_hashes = validated.get("geometry_sha256", {})
    if set(geometry_hashes.get("divisions", {})) != division_ids:
        raise RuntimeError("division geometry hashes do not cover the evidence inventory")
    if set(geometry_hashes.get("division_areas", {})) != area_ids:
        raise RuntimeError("area geometry hashes do not cover the evidence inventory")
    if evidence.get("timings_seconds", {}).get("total", 60) >= 60:
        raise RuntimeError("pinned Monaco smoke evidence misses the 60-second gate")
    provenance = evidence.get("provenance", {})
    if provenance.get("duckdb_python_version") != PINNED_DUCKDB_VERSION:
        raise RuntimeError("evidence Python DuckDB version differs from production")
    if not str(provenance.get("duckdb_cli_version", "")).startswith(
        f"v{PINNED_DUCKDB_VERSION} "
    ):
        raise RuntimeError("evidence CLI DuckDB version differs from production")
    command = provenance.get("command")
    if not isinstance(command, list) or command[:2] != [
        "python",
        "scripts/download_divisions_smoke.py",
    ]:
        raise RuntimeError("evidence has no executable exporter command")
    for flag in (
        "--release",
        "--forward-output",
        "--reverse-output",
        "--profile-output",
        "--authoritative-forward-output",
        "--authoritative-reverse-output",
    ):
        if flag not in command:
            raise RuntimeError(f"evidence exporter command omits {flag}")
    for report_name in (
        "logical_equivalence_to_full_global",
        "logical_equivalence_to_no_bbox_baseline",
    ):
        if evidence[report_name].get("duckdb_python_version") != PINNED_DUCKDB_VERSION:
            raise RuntimeError(f"{report_name} used a different DuckDB version")
        for family in ("forward", "reverse"):
            for side in ("legacy", "subset"):
                created_by = evidence[report_name][family].get(
                    f"{side}_created_by", ""
                )
                if f"v{PINNED_DUCKDB_VERSION}" not in created_by:
                    raise RuntimeError(
                        f"{report_name} {family} {side} writer was not pinned"
                    )
    for family in ("division", "division_area"):
        scan = evidence["pushdown"][family]
        eligible = scan.get("metadata_eligible_row_groups", 0)
        total = scan.get("total_row_groups", 0)
        if not (0 < eligible < total):
            raise RuntimeError(f"{family} footer statistics prove no row-group pruning")
        if family == "division" and scan.get("plan_has_country_filter") is not True:
            raise RuntimeError(f"{family} scan has no country filter")
        if scan.get("plan_has_bbox_filter") is not True:
            raise RuntimeError(f"{family} scan has no bbox filter")
    if evidence["pushdown"]["division_area"].get("profiled_predicate") != (
        "actual rendered country-or-division_id ownership predicate plus bbox"
    ):
        raise RuntimeError("division-area profile did not execute the rendered predicate")
    return evidence


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--global-equivalence", type=Path)
    parser.add_argument("--baseline-equivalence", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    if args.check:
        if any(
            value is not None
            for value in (
                args.profile,
                args.global_equivalence,
                args.baseline_equivalence,
                args.output,
            )
        ):
            parser.error("--check cannot be combined with generation arguments")
        verify_evidence(args.check)
        print(f"Monaco evidence is current and complete: {args.check}")
        return
    required = (
        args.profile,
        args.global_equivalence,
        args.baseline_equivalence,
        args.output,
    )
    if any(value is None for value in required):
        parser.error(
            "generation requires --profile, --global-equivalence, "
            "--baseline-equivalence, and --output"
        )
    evidence = build_evidence(
        args.profile, args.global_equivalence, args.baseline_equivalence
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    verify_evidence(args.output)
    print(f"Wrote current Monaco evidence: {args.output}")


if __name__ == "__main__":
    main()
