#!/usr/bin/env python3
"""Combine the non-promoting CONUS Places and USA address scale evidence.

The address input is intentionally an upper-bound task fleet: it contains
every complete inventory task whose largest exact-country footer population is
US, without row-level country filtering.  The report preserves that contract
and must not be treated as an exact US export or as publication approval.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


REPORT_SCHEMA = "overture-usa-scale-signal-v1"
PLACES_SCHEMA = "overture-places-region-build-evidence-v1"
ADDRESS_SCHEMA = "overture-address-sweep-aggregate-v1"
SELECTION_SCHEMA = "overture-address-country-dominant-selection-v1"
COMBINED_BYTE_GATE = 40_000_000_000


def _document(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _int(value: Any, field: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}")
    return value


def _number(value: Any, field: str, *, minimum: float = 0.0) -> int | float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < minimum
    ):
        raise ValueError(f"{field} must be a number >= {minimum}")
    return value


def _sum_task_metric(tasks: list[dict[str, Any]], key: str) -> int | float:
    values = [_number(task.get(key), f"task {task.get('name')} {key}") for task in tasks]
    return sum(values)


def build_report(
    places: Any, addresses: Any, selection: Any
) -> dict[str, Any]:
    """Validate both retained evidence records and build the combined gate."""
    places = _document(places, "places evidence")
    addresses = _document(addresses, "address aggregate")
    selection = _document(selection, "address selection")

    if places.get("schema") != PLACES_SCHEMA:
        raise ValueError(f"places schema must be {PLACES_SCHEMA!r}")
    if addresses.get("schema") != ADDRESS_SCHEMA:
        raise ValueError(f"address schema must be {ADDRESS_SCHEMA!r}")
    if selection.get("schema") != SELECTION_SCHEMA:
        raise ValueError(f"selection schema must be {SELECTION_SCHEMA!r}")
    if selection.get("country") != "US" or selection.get("exact_country_export") is not False:
        raise ValueError("selection must be the non-exact US-dominant task fleet")
    if (
        addresses.get("selection_schema") != SELECTION_SCHEMA
        or addresses.get("selection_country") != "US"
        or addresses.get("exact_country_export") is not False
    ):
        raise ValueError("address aggregate must identify the non-exact USA selection")

    releases = {places.get("release"), addresses.get("release"), selection.get("release")}
    if len(releases) != 1 or not all(isinstance(release, str) and release for release in releases):
        raise ValueError("places, addresses, and selection releases must match")
    release = releases.pop()

    selected_tasks = selection.get("tasks")
    measured_tasks = addresses.get("tasks")
    if not isinstance(selected_tasks, list) or not selected_tasks:
        raise ValueError("selection tasks must be a non-empty array")
    if not isinstance(measured_tasks, list):
        raise ValueError("address tasks must be an array")
    identity_fields = (
        "name",
        "task_index",
        "expected_rows",
        "expected_selected_compressed_bytes",
    )
    expected_identity = [
        tuple(task.get(field) for field in identity_fields) for task in selected_tasks
    ]
    measured_identity = [
        tuple(task.get(field) for field in identity_fields) for task in measured_tasks
    ]
    if measured_identity != expected_identity:
        raise ValueError("address aggregate tasks do not exactly match the USA selection")

    task_count = len(selected_tasks)
    if _int(selection.get("task_count"), "selection task_count", minimum=1) != task_count:
        raise ValueError("selection task_count does not match its task array")
    if _int(addresses.get("task_count"), "address task_count", minimum=1) != task_count:
        raise ValueError("address task_count does not match the USA selection")

    selected_projected_rows = int(_sum_task_metric(selected_tasks, "expected_rows"))
    if (
        _int(selection.get("projected_rows"), "selection projected_rows", minimum=1)
        != selected_projected_rows
    ):
        raise ValueError("selection projected_rows does not match its task array")

    completed_count = _int(addresses.get("completed_count"), "completed_count")
    rows_reconciled_count = _int(
        addresses.get("rows_reconciled_count"), "rows_reconciled_count"
    )
    addresses_complete = completed_count == task_count and all(
        task.get("status") == "complete" for task in measured_tasks
    )
    address_oracles_match = addresses.get("all_local_oracle_match") is True and all(
        task.get("local_oracle_match") is True for task in measured_tasks
    )
    address_rows_reconciled = rows_reconciled_count == task_count and all(
        task.get("rows_reconciled") is True for task in measured_tasks
    )

    address_expected_rows = int(_sum_task_metric(measured_tasks, "expected_rows"))
    address_input_rows = int(_sum_task_metric(measured_tasks, "input_rows"))
    address_retained_rows = int(_sum_task_metric(measured_tasks, "selected_rows"))
    address_fragment_bytes = int(_sum_task_metric(measured_tasks, "fragment_bytes"))
    address_map_work_seconds = float(_sum_task_metric(measured_tasks, "map_wall_seconds"))
    address_reduce_work_seconds = float(
        _sum_task_metric(measured_tasks, "reduce_wall_seconds")
    )
    address_rows_reconciled = (
        address_rows_reconciled
        and address_expected_rows == selected_projected_rows
        and address_input_rows == address_expected_rows
        and address_retained_rows <= address_input_rows
    )
    address_peak_rss_bytes = int(
        max(_number(task.get("peak_rss_bytes"), "task peak_rss_bytes") for task in measured_tasks)
    )
    address_max_retry_amplification = float(
        max(
            _number(
                task.get("retry_read_amplification"),
                "task retry_read_amplification",
            )
            for task in measured_tasks
        )
    )

    totals = _document(places.get("totals"), "places totals")
    extract = _document(places.get("extract"), "places extract")
    determinism = _document(places.get("determinism"), "places determinism")
    places_rows = _int(totals.get("shard_rows"), "places shard_rows", minimum=1)
    loaded_places = _int(totals.get("loaded_places"), "places loaded_places", minimum=1)
    places_bytes = _int(totals.get("shard_bytes"), "places shard_bytes", minimum=1)
    places_shards = _int(totals.get("shards"), "places shards", minimum=1)
    extracted_rows = _int(extract.get("extracted_rows"), "places extracted_rows", minimum=1)
    places_complete = (
        extracted_rows == loaded_places == places_rows
        and extract.get("truncated") is False
        and determinism.get("determinism_ok") is True
        and places.get("promotion_eligible") is False
    )
    family_verification = _document(
        places.get("family_verification"), "places family_verification"
    )
    family_manifest_digest = places.get("family_manifest_digest")
    places_region = family_verification.get("region")
    expected_places_region = {
        "name": "conus",
        "bbox": [-125.0, 24.4, -66.9, 49.4],
        "bbox_scope": "exact",
    }
    places_verified_objects = _int(
        family_verification.get("verified_objects"),
        "places verified_objects",
        minimum=1,
    )
    places_family_bytes = _int(
        family_verification.get("verified_bytes"),
        "places verified_bytes",
        minimum=1,
    )
    places_manifest_verified = (
        isinstance(family_manifest_digest, str)
        and len(family_manifest_digest) == 64
        and family_verification.get("manifest_digest") == family_manifest_digest
        and family_verification.get("family") == "places"
        and places_region == expected_places_region
        and places_verified_objects >= places_shards
        and places_family_bytes >= places_bytes
    )

    combined_bytes = places_family_bytes + address_fragment_bytes
    within_byte_gate = combined_bytes <= COMBINED_BYTE_GATE
    passed = all(
        (
            places_complete,
            places_manifest_verified,
            addresses_complete,
            address_oracles_match,
            address_rows_reconciled,
            within_byte_gate,
        )
    )

    return {
        "schema": REPORT_SCHEMA,
        "release": release,
        "promotion_eligible": False,
        "decision": "pass" if passed else "fail",
        "note": (
            "Non-promoting USA scale signal. Address measurements cover complete "
            "US-dominant inventory tasks without a row-level country filter; this "
            "is not an exact US export or publication approval."
        ),
        "places": {
            "scope": "conus-bbox",
            "bbox": expected_places_region["bbox"],
            "rows": places_rows,
            "loaded_rows": loaded_places,
            "shards": places_shards,
            "shard_bytes": places_bytes,
            "family_objects": places_verified_objects,
            "family_bytes": places_family_bytes,
            "extracted_rows": extracted_rows,
            "deterministic": determinism.get("determinism_ok") is True,
            "manifest_verified": places_manifest_verified,
            "complete": places_complete,
        },
        "addresses": {
            "scope": "us-dominant-task-upper-bound",
            "exact_country_export": False,
            "tasks_completed": completed_count,
            "tasks_expected": task_count,
            "expected_rows": address_expected_rows,
            "input_rows": address_input_rows,
            "retained_rows": address_retained_rows,
            "fragment_bytes": address_fragment_bytes,
            "map_work_seconds": round(address_map_work_seconds, 3),
            "reduce_work_seconds": round(address_reduce_work_seconds, 3),
            "peak_rss_bytes": address_peak_rss_bytes,
            "max_retry_read_amplification": round(
                address_max_retry_amplification, 6
            ),
            "complete": addresses_complete,
            "all_local_oracle_match": address_oracles_match,
            "all_rows_reconciled": address_rows_reconciled,
        },
        "gate": {
            "combined_measured_bytes": combined_bytes,
            "combined_byte_limit": COMBINED_BYTE_GATE,
            "within_combined_byte_limit": within_byte_gate,
            "passed": passed,
        },
    }


def render_markdown(report: dict[str, Any]) -> str:
    places = report["places"]
    addresses = report["addresses"]
    gate = report["gate"]
    return "\n".join(
        [
            "## USA scale signal (non-promoting)",
            "",
            f"**Decision: {report['decision'].upper()}** for release `{report['release']}`.",
            "",
            "| family | scope | rows | artifacts/tasks | measured bytes | complete |",
            "| --- | --- | ---: | ---: | ---: | --- |",
            f"| places | CONUS bbox | {places['rows']:,} | {places['shards']} shards | "
            f"{places['family_bytes']:,} | {places['complete']} |",
            f"| addresses | US-dominant task upper bound | {addresses['retained_rows']:,} retained | "
            f"{addresses['tasks_completed']}/{addresses['tasks_expected']} tasks | "
            f"{addresses['fragment_bytes']:,} | {addresses['complete']} |",
            "",
            f"Combined measured bytes: **{gate['combined_measured_bytes']:,} / "
            f"{gate['combined_byte_limit']:,}**; within gate: "
            f"**{gate['within_combined_byte_limit']}**.",
            "",
            "Address scope is an upper-bound US-dominant task fleet, not an exact "
            "country-filtered export. A green result does not approve promotion.",
            "",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--places", type=Path, required=True)
    parser.add_argument("--addresses", type=Path, required=True)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    args = parser.parse_args(argv)

    report = build_report(
        json.loads(args.places.read_text()),
        json.loads(args.addresses.read_text()),
        json.loads(args.selection.read_text()),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(render_markdown(report) + "\n")
    print(
        f"USA scale signal {report['decision']}: "
        f"{report['gate']['combined_measured_bytes']} measured bytes"
    )
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
