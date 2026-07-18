#!/usr/bin/env python3
"""Assemble the family-release-slice scale report (retained evidence).

This is offline, pure control-plane code: it consumes the per-(family, region)
measurements the slice workflow already produced (artifacts, bytes, rows, and
build/publish/verify wall times) and emits one deterministic evidence record
with the projected planet-extrapolation lines the deploy scope doc's exit
artifact asks for. It never touches Cloudflare, R2, Overture S3, or a catalog,
and it is explicitly non-promoting: a green report is measurement, not launch
or publication approval (PENDING_WORK.md decisions 6 and 8).

Extrapolation is honest and conservative: for each family it takes the measured
bytes-per-retained-row coefficient of the *largest* measured region (the most
representative, least fixed-overhead-dominated sample) and multiplies it by the
reference planet-scale row counts measured for that family against a real
Overture release (``docs/plans/2026-07-18-us-ne-regional-deploy-scope.md`` §1,
release 2026-06-17.0). A projection is emitted only for targets whose reference
row count is known; nothing is invented.

Input schema (``overture-slice-scale-input-v1``)::

    {
      "schema": "overture-slice-scale-input-v1",
      "slice_version": "slice-2026-07-18.0",
      "release": "2026-06-17.0",
      "verify_seconds": 78,
      "families": [
        {
          "family": "places",
          "manifest_digest": "<64 hex>",
          "build_seconds": 1234,
          "publish_seconds": 56,
          "regions": [
            {"name": "us-northeast", "bbox": [xmin,ymin,xmax,ymax],
             "rows": 4133950, "artifacts": 3, "bytes": 481000000}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


INPUT_SCHEMA = "overture-slice-scale-input-v1"
REPORT_SCHEMA = "overture-slice-scale-report-v1"

# Reference planet-scale row counts per family, measured with the region
# extractor's --count-only mode against release 2026-06-17.0 and recorded in the
# deploy scope doc (§1). These are the honest denominators the projection scales
# the measured bytes-per-row coefficient onto; a missing entry means no
# projection is emitted for that target rather than a guessed one.
PLANET_REFERENCE: dict[str, dict[str, int]] = {
    "places": {
        "conus": 18_014_140,
        "planet": 75_631_061,
    },
    # The scope doc gives an all-US address upper bound (~131M rows) but no
    # measured planet address row count, so addresses projects only to all-US.
    "addresses": {
        "all_us": 131_000_000,
    },
}


def _require(value: Any, field: str, kind: type | tuple[type, ...]) -> Any:
    if not isinstance(value, kind) or isinstance(value, bool):
        raise ValueError(f"{field} must be {getattr(kind, '__name__', kind)}")
    return value


def _require_positive_int(value: Any, field: str) -> int:
    _require(value, field, int)
    if value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _normalize_region(region: Any) -> dict[str, Any]:
    _require(region, "region", dict)
    name = _require(region.get("name"), "region name", str)
    if not name:
        raise ValueError("region name is required")
    bbox = region.get("bbox")
    if not isinstance(bbox, list) or len(bbox) != 4 or any(
        not isinstance(coordinate, (int, float)) or isinstance(coordinate, bool)
        for coordinate in bbox
    ):
        raise ValueError(f"region {name} bbox must be [xmin, ymin, xmax, ymax]")
    return {
        "name": name,
        "bbox": [float(coordinate) for coordinate in bbox],
        "rows": _require_positive_int(region.get("rows"), f"region {name} rows"),
        "artifacts": _require_positive_int(
            region.get("artifacts"), f"region {name} artifacts"
        ),
        "bytes": _require_positive_int(region.get("bytes"), f"region {name} bytes"),
    }


def _family_projection(
    family: str, regions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Project planet-scale bytes from the largest measured region's coefficient."""
    largest = max(regions, key=lambda region: region["rows"])
    bytes_per_row = largest["bytes"] / largest["rows"]
    targets = PLANET_REFERENCE.get(family, {})
    projections = {
        target: {
            "reference_rows": rows,
            "projected_bytes": round(bytes_per_row * rows),
            "projected_gib": round(bytes_per_row * rows / 1024**3, 3),
        }
        for target, rows in sorted(targets.items())
    }
    return {
        "basis_region": largest["name"],
        "bytes_per_row": round(bytes_per_row, 4),
        "projections": projections,
    }


def build_scale_report(document: Any) -> dict[str, Any]:
    """Validate a slice-scale input document and return the deterministic report."""
    _require(document, "input document", dict)
    if document.get("schema") != INPUT_SCHEMA:
        raise ValueError(f"input schema must be {INPUT_SCHEMA!r}")
    slice_version = _require(document.get("slice_version"), "slice_version", str)
    release = _require(document.get("release"), "release", str)
    # Verification is a single slice-wide pass (finalize verify-families-only runs
    # once over every family), so its wall time is recorded at the top level
    # rather than split, misleadingly, across families.
    verify_seconds = _require_positive_int(
        document.get("verify_seconds"), "verify_seconds"
    )
    families_in = document.get("families")
    if not isinstance(families_in, list) or not families_in:
        raise ValueError("families must be a non-empty array")

    families: list[dict[str, Any]] = []
    seen: set[str] = set()
    grand_rows = grand_bytes = grand_artifacts = 0
    for entry in families_in:
        _require(entry, "family entry", dict)
        family = _require(entry.get("family"), "family", str)
        if family in seen:
            raise ValueError(f"duplicate family {family!r}")
        seen.add(family)
        regions_in = entry.get("regions")
        if not isinstance(regions_in, list) or not regions_in:
            raise ValueError(f"family {family} must have a non-empty regions array")
        regions = [_normalize_region(region) for region in regions_in]
        names = [region["name"] for region in regions]
        if len(set(names)) != len(names):
            raise ValueError(f"family {family} has duplicate region names")
        total_rows = sum(region["rows"] for region in regions)
        total_bytes = sum(region["bytes"] for region in regions)
        total_artifacts = sum(region["artifacts"] for region in regions)
        grand_rows += total_rows
        grand_bytes += total_bytes
        grand_artifacts += total_artifacts
        families.append(
            {
                "family": family,
                "manifest_digest": _require(
                    entry.get("manifest_digest"), "manifest_digest", str
                ),
                "regions": regions,
                "region_count": len(regions),
                "totals": {
                    "rows": total_rows,
                    "bytes": total_bytes,
                    "artifacts": total_artifacts,
                },
                "wall_seconds": {
                    "build": _require_positive_int(
                        entry.get("build_seconds"), f"family {family} build_seconds"
                    ),
                    "publish": _require_positive_int(
                        entry.get("publish_seconds"), f"family {family} publish_seconds"
                    ),
                },
                "planet_extrapolation": _family_projection(family, regions),
            }
        )

    return {
        "schema": REPORT_SCHEMA,
        "slice_version": slice_version,
        "release": release,
        "promotion_eligible": False,
        "note": (
            "Non-promoting family-release slice. A green report is measurement "
            "and cleanup succeeding; it is not launch or publication approval."
        ),
        "verify_seconds": verify_seconds,
        "families": families,
        "totals": {
            "families": len(families),
            "rows": grand_rows,
            "bytes": grand_bytes,
            "artifacts": grand_artifacts,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    """A compact GitHub-step-summary table for the scale report."""
    lines = [
        f"## Family-release slice scale report — {report['slice_version']}",
        "",
        f"Overture release `{report['release']}` · non-promoting · "
        f"{report['totals']['families']} families · "
        f"{report['totals']['artifacts']} artifacts · "
        f"{report['totals']['bytes']:,} bytes · "
        f"verify {report['verify_seconds']}s (slice-wide)",
        "",
        "| family | region | rows | artifacts | bytes |",
        "| --- | --- | ---: | ---: | ---: |",
    ]
    for family in report["families"]:
        for region in family["regions"]:
            lines.append(
                f"| {family['family']} | {region['name']} | {region['rows']:,} | "
                f"{region['artifacts']} | {region['bytes']:,} |"
            )
    lines += ["", "### Projected planet extrapolation", ""]
    for family in report["families"]:
        extrapolation = family["planet_extrapolation"]
        basis = extrapolation["basis_region"]
        coefficient = extrapolation["bytes_per_row"]
        for target, projection in extrapolation["projections"].items():
            lines.append(
                f"- **{family['family']} → {target}**: "
                f"{projection['reference_rows']:,} rows × {coefficient} B/row ≈ "
                f"{projection['projected_gib']} GiB "
                f"(basis region `{basis}`)"
            )
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    args = parser.parse_args()

    document = json.loads(args.input.read_text())
    report = build_scale_report(document)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    if args.markdown is not None:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(report))


if __name__ == "__main__":
    main()
