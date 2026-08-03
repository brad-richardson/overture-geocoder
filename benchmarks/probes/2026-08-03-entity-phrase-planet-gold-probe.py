#!/usr/bin/env python3
"""Measure whether gold entity phrases survive the planet-wide head cap.

This is the cheap falsifier for the exact-primary-name phrase lane.  It scans
only the source columns needed by the producer, applies the transform's NFKD
word normalization and admission rules, then orders every matching prominent
record by the construction-v1 head cap key.  No index build or remote write is
performed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from pathlib import Path
import struct
import time
import uuid

import duckdb
import pyarrow as pa
import pyarrow.compute as pc


ROOT = Path(__file__).resolve().parents[2]
RELEASE = "2026-06-17.0"
SOURCE = (
    "s3://overturemaps-us-west-2/release/"
    f"{RELEASE}/theme=places/type=place/*"
)
HEAD_RESULT_CAP = 10
TARGETS = {
    "big_ben": {
        "phrase": "big ben",
        "canonical_id": "99f74940-898a-49d5-9eca-18a8a2c47918",
    },
    "empire_state_building": {
        "phrase": "empire state building",
        "canonical_id": "91b41dc1-59cb-4822-af8b-bb45d3b96f42",
    },
}


def load_type_prior():
    path = ROOT / "scripts/places_type_prior_v1.py"
    spec = importlib.util.spec_from_file_location("planet_phrase_type_prior", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


TYPE_PRIOR = load_type_prior()


def normalize_phrase_arrow(values: pa.Array) -> pa.Array:
    """Vectorized equivalent of the producer's normalized_words join."""

    result = pc.utf8_normalize(values, form="NFKD")
    result = pc.utf8_lower(result)
    result = pc.replace_substring_regex(result, r"\p{M}+", "")
    result = pc.replace_substring_regex(result, r"[^\p{L}\p{N}_]+", " ")
    return pc.utf8_trim_whitespace(result)


def valid_point(value: bytes | None) -> bool:
    if value is None or len(value) != 21 or value[0] not in (0, 1):
        return False
    order = "<" if value[0] == 1 else ">"
    geometry_type = struct.unpack_from(f"{order}I", value, 1)[0]
    longitude, latitude = struct.unpack_from(f"{order}dd", value, 5)
    return (
        geometry_type == 1
        and math.isfinite(longitude)
        and math.isfinite(latitude)
        and -180 <= longitude <= 180
        and -90 <= latitude <= 90
    )


def confidence_rank(value: object) -> int | None:
    if not isinstance(value, float) or not math.isfinite(value) or not 0 <= value <= 1:
        return None
    return min(255, max(0, math.floor(value * 255 + 0.5)))


def producer_record(row: dict) -> dict | None:
    try:
        identifier = uuid.UUID(row["id"])
    except (AttributeError, TypeError, ValueError):
        return None
    if str(identifier) != row["id"].lower():
        return None
    rank = confidence_rank(row["confidence"])
    if rank is None or not valid_point(row["geometry"]):
        return None
    if str(row.get("operating_status") or "").strip().lower() == "permanently_closed":
        return None
    prominence = TYPE_PRIOR.prominence_rank(
        row["category"],
        row["basic_category"],
        None,
        row["alternate_categories"],
    )
    return {
        "id": str(identifier),
        "primary_name": row["primary_name"],
        "category": row["category"],
        "basic_category": row["basic_category"],
        "confidence_rank": rank,
        "prominence_rank": prominence,
        "source_object": Path(row["filename"]).name,
        "source_file_row_number": int(row["file_row_number"]),
        "_uuid_bytes": identifier.bytes,
    }


def cap_order(row: dict) -> tuple:
    # Every phrase row has FIELD_NAME, so the identifying-first discriminator
    # is equal.  filename order is the frozen inventory object order and an
    # absolute file row preserves row-group/row order for the final tie-break.
    return (
        -row["prominence_rank"],
        -row["confidence_rank"],
        row["_uuid_bytes"],
        row["source_object"],
        row["source_file_row_number"],
    )


def public_record(row: dict) -> dict:
    return {key: value for key, value in row.items() if not key.startswith("_")}


def run(output: Path, memory_limit: str, threads: int) -> dict:
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2'")
    con.execute(f"SET memory_limit='{memory_limit}'")
    con.execute(f"SET threads={threads}")
    con.create_function(
        "entity_phrase_normalize",
        normalize_phrase_arrow,
        [duckdb.sqltypes.VARCHAR],
        duckdb.sqltypes.VARCHAR,
        type="arrow",
    )
    phrases = [item["phrase"] for item in TARGETS.values()]
    started = time.monotonic()
    cursor = con.execute(
        f"""
        SELECT
          entity_phrase_normalize(names.primary) AS phrase,
          id::VARCHAR AS id,
          names.primary AS primary_name,
          categories.primary AS category,
          categories.alternate AS alternate_categories,
          basic_category,
          operating_status,
          confidence,
          geometry,
          filename,
          file_row_number
        FROM read_parquet(
          '{SOURCE}',
          hive_partitioning=true,
          filename=true,
          file_row_number=true
        )
        WHERE names.primary IS NOT NULL
          AND entity_phrase_normalize(names.primary) IN (?, ?)
        """,
        phrases,
    )
    columns = [item[0] for item in cursor.description]
    source_rows = [
        dict(zip(columns, values, strict=True)) for values in cursor.fetchall()
    ]
    elapsed = time.monotonic() - started

    targets = {}
    all_survive = True
    for name, target in TARGETS.items():
        matching = [row for row in source_rows if row["phrase"] == target["phrase"]]
        admitted = [record for row in matching if (record := producer_record(row))]
        prominent = sorted(
            (row for row in admitted if row["prominence_rank"] > 0),
            key=cap_order,
        )
        canonical_rank = next(
            (
                index
                for index, row in enumerate(prominent, 1)
                if row["id"] == target["canonical_id"]
            ),
            None,
        )
        survives = canonical_rank is not None and canonical_rank <= HEAD_RESULT_CAP
        all_survive &= survives
        targets[name] = {
            **target,
            "matching_source_rows": len(matching),
            "producer_admitted_rows": len(admitted),
            "prominence_gated_posting_records": len(prominent),
            "canonical_rank_under_head_cap_order": canonical_rank,
            "canonical_survives_head_cap": survives,
            "retained_records": [
                public_record(row) for row in prominent[:HEAD_RESULT_CAP]
            ],
        }

    evidence = {
        "schema": "overture-places-entity-phrase-planet-gold-v1",
        "source_release": RELEASE,
        "head_result_cap": HEAD_RESULT_CAP,
        "producer_contract": {
            "field_mask": "primary_name",
            "normalization": "nfkd-lower-stripmark-words",
            "word_count": [2, 3],
            "admission": "prominence_rank > 0",
            "cap_order": "identifying, prominence DESC, confidence DESC, feature_id, source locator",
        },
        "parameters": {"duckdb_memory_limit": memory_limit, "duckdb_threads": threads},
        "measurement": {
            "wall_seconds": elapsed,
            "matching_source_rows": len(source_rows),
        },
        "targets": targets,
        "all_canonical_targets_survive": all_survive,
        "limitations": [
            "This falsifies target posting survival; it does not measure encoded bytes or a deployed Worker query.",
            "The two canonical IDs are frozen from the independently audited 2026-06-17.0 source matches.",
        ],
    }
    output.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--memory-limit", default="2GB")
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    result = run(args.output, args.memory_limit, args.threads)
    print(
        json.dumps(
            {
                "all_canonical_targets_survive": result[
                    "all_canonical_targets_survive"
                ],
                "targets": {
                    name: {
                        "posting_records": item[
                            "prominence_gated_posting_records"
                        ],
                        "canonical_rank": item[
                            "canonical_rank_under_head_cap_order"
                        ],
                    }
                    for name, item in result["targets"].items()
                },
            },
            sort_keys=True,
        )
    )
    return 0 if result["all_canonical_targets_survive"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
