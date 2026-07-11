#!/usr/bin/env python3
"""Offline quality benchmark for nested Overture Places samples.

The benchmark sorts a local flattened Places export once per strategy, then
evaluates prefix samples (for example 10k, 25k, and 50k).  It never downloads
data or builds shards.  JSON output is intended for comparing experiment runs;
Markdown output is a compact review artifact.

Example:
    python scripts/benchmark_places_sampling.py exports/places-CA.parquet \
      --cases benchmarks/places-sampling-cases.example.json \
      --sizes 10000,25000,50000 \
      --json-out /tmp/places-sampling.json \
      --markdown-out /tmp/places-sampling.md

CSV, JSONL, JSON arrays, and parquet are accepted.  Parquet input uses the
project's existing optional ``duckdb`` dependency; the other formats use only
the Python standard library.
"""

from __future__ import annotations

import argparse
import csv
import heapq
import json
import math
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence


# Kept in sync with the places prototype in build_shards.py.  Defining these
# here keeps this read-only experiment importable without DuckDB installed.
CATEGORY_PRIORS = {
    "airport": 0.25,
    "national_park": 0.20,
    "university": 0.15,
    "hospital": 0.12,
    "stadium": 0.12,
    "museum": 0.10,
    "hotel": 0.05,
    "restaurant": 0.02,
}
ROUTING_CLASSES = {"famous_unique", "local_unique", "ubiquitous_brand"}
STRATEGIES = ("confidence", "prominence")
WORD_RE = re.compile(r"[\w]+", re.UNICODE)


def normalize(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip().lower())
    return " ".join(WORD_RE.findall("".join(c for c in text if not unicodedata.combining(c))))


def optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def truthy_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def prominence_score(row: dict[str, Any]) -> float:
    """Mirror the prototype's places importance formula."""
    confidence = optional_float(row.get("confidence"))
    confidence = 0.5 if confidence is None else min(1.0, max(0.0, confidence))
    score = confidence * 0.5
    if truthy_text(row.get("brand_name")):
        score += 0.20
        if truthy_text(row.get("brand_wikidata")):
            score += 0.10
    if confidence >= 0.90:
        score += 0.10
    category = normalize(row.get("category_primary") or row.get("basic_category"))
    score += CATEGORY_PRIORS.get(category, 0.0)
    return min(1.0, score)


@dataclass(frozen=True, slots=True)
class Place:
    place_id: str
    primary_name: str
    brand_name: str
    category_primary: str
    basic_category: str
    locality: str
    region: str
    lat: float | None
    lon: float | None
    confidence: float
    prominence: float
    search_text: str


@dataclass(frozen=True, slots=True)
class Case:
    case_id: str
    query: str
    routing_class: str
    expected_ids: frozenset[str]
    target_name: str
    target_brand: str
    target_lat: float | None
    target_lon: float | None
    tolerance_km: float
    min_retained: int
    note: str


def place_from_row(row: dict[str, Any], row_number: int) -> Place:
    place_id = truthy_text(row.get("gers_id") or row.get("id"))
    if not place_id:
        place_id = f"__row_{row_number}"
    primary_name = truthy_text(row.get("primary_name") or row.get("name"))
    brand_name = truthy_text(row.get("brand_name"))
    category = truthy_text(row.get("category_primary"))
    basic_category = truthy_text(row.get("basic_category"))
    confidence = optional_float(row.get("confidence"))
    confidence = 0.5 if confidence is None else min(1.0, max(0.0, confidence))
    parts = (
        row.get("search_name_base"), primary_name, brand_name, category,
        basic_category, row.get("locality"), row.get("region"),
    )
    return Place(
        place_id=place_id,
        primary_name=primary_name,
        brand_name=brand_name,
        category_primary=category,
        basic_category=basic_category,
        locality=truthy_text(row.get("locality")),
        region=truthy_text(row.get("region")),
        lat=optional_float(row.get("lat")),
        lon=optional_float(row.get("lon")),
        confidence=confidence,
        prominence=prominence_score(row),
        search_text=normalize(" ".join(truthy_text(part) for part in parts)),
    )


def iter_rows(path: Path) -> Iterator[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open(newline="", encoding="utf-8-sig") as handle:
            yield from csv.DictReader(handle)
        return
    if suffix in {".jsonl", ".ndjson"}:
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if line.strip():
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise ValueError(f"{path}:{line_number}: expected a JSON object")
                    yield value
        return
    if suffix == ".json":
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
            raise ValueError(f"{path}: expected a JSON array of objects")
        yield from value
        return
    if suffix == ".parquet":
        try:
            import duckdb  # type: ignore
        except ImportError as exc:
            raise RuntimeError("parquet input requires the duckdb Python package") from exc
        connection = duckdb.connect()
        try:
            cursor = connection.execute("SELECT * FROM read_parquet(?)", [str(path)])
            columns = [column[0] for column in cursor.description]
            while rows := cursor.fetchmany(10_000):
                for row in rows:
                    yield dict(zip(columns, row))
        finally:
            connection.close()
        return
    raise ValueError(f"unsupported input format {suffix!r}; use parquet, CSV, JSONL, or JSON")


def load_places(path: Path) -> list[Place]:
    places = list(iter_places(path))
    ids = [place.place_id for place in places]
    if len(set(ids)) != len(ids):
        raise ValueError("input contains duplicate gers_id/id values")
    return places


def iter_places(path: Path) -> Iterator[Place]:
    for number, row in enumerate(iter_rows(path), 1):
        yield place_from_row(row, number)


def load_cases(path: Path) -> list[Case]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict):
        raw = raw.get("cases")
    if not isinstance(raw, list):
        raise ValueError("case file must be a JSON array or an object with a cases array")
    cases: list[Case] = []
    seen: set[str] = set()
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"case {index}: expected an object")
        case_id = truthy_text(item.get("id"))
        query = truthy_text(item.get("query"))
        routing_class = truthy_text(item.get("routing_class"))
        if not case_id or not query:
            raise ValueError(f"case {index}: id and query are required")
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        if routing_class not in ROUTING_CLASSES:
            raise ValueError(f"case {case_id}: routing_class must be one of {sorted(ROUTING_CLASSES)}")
        expected_ids = frozenset(str(value) for value in item.get("expected_ids", []) if value)
        target_name = truthy_text(item.get("target_name"))
        target_brand = truthy_text(item.get("target_brand"))
        if not (expected_ids or target_name or target_brand):
            raise ValueError(f"case {case_id}: provide expected_ids, target_name, or target_brand")
        target_lat = optional_float(item.get("target_lat"))
        target_lon = optional_float(item.get("target_lon"))
        if (target_lat is None) != (target_lon is None):
            raise ValueError(f"case {case_id}: target_lat and target_lon must be provided together")
        tolerance_km = float(item.get("tolerance_km", 5.0))
        if tolerance_km < 0:
            raise ValueError(f"case {case_id}: tolerance_km cannot be negative")
        cases.append(Case(
            case_id=case_id,
            query=query,
            routing_class=routing_class,
            expected_ids=expected_ids,
            target_name=target_name,
            target_brand=target_brand,
            target_lat=target_lat,
            target_lon=target_lon,
            tolerance_km=tolerance_km,
            min_retained=max(1, int(item.get("min_retained", 1))),
            note=truthy_text(item.get("note")),
        ))
        seen.add(case_id)
    return cases


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    radius = 6371.0
    lat_delta = math.radians(lat2 - lat1)
    lon_delta = math.radians(lon2 - lon1)
    a = (
        math.sin(lat_delta / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
        * math.sin(lon_delta / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def matches_case(place: Place, case: Case) -> bool:
    if case.expected_ids and place.place_id not in case.expected_ids:
        return False
    if case.target_name and normalize(place.primary_name) != normalize(case.target_name):
        return False
    if case.target_brand and normalize(place.brand_name) != normalize(case.target_brand):
        return False
    if case.target_lat is not None or case.target_lon is not None:
        if None in (case.target_lat, case.target_lon, place.lat, place.lon):
            return False
        assert case.target_lat is not None and case.target_lon is not None
        assert place.lat is not None and place.lon is not None
        if haversine_km(case.target_lat, case.target_lon, place.lat, place.lon) > case.tolerance_km:
            return False
    return True


def query_relevance(place: Place, query: str) -> int:
    query_norm = normalize(query)
    if not query_norm:
        return 0
    name = normalize(place.primary_name)
    brand = normalize(place.brand_name)
    if query_norm == name or query_norm == brand:
        return 3
    if name.startswith(query_norm) or brand.startswith(query_norm):
        return 2
    tokens = query_norm.split()
    return 1 if all(token in place.search_text.split() for token in tokens) else 0


def strategy_key(place: Place, strategy: str) -> tuple[Any, ...]:
    if strategy == "confidence":
        return (-place.confidence, place.place_id)
    if strategy == "prominence":
        return (-place.prominence, -place.confidence, place.place_id)
    raise ValueError(f"unknown strategy: {strategy}")


def evaluate_case(sample: Sequence[Place], case: Case, eligible_ids: set[str], top_k: int) -> dict[str, Any]:
    retained_ids = {place.place_id for place in sample if place.place_id in eligible_ids}
    candidates = [place for place in sample if query_relevance(place, case.query)]
    candidates.sort(key=lambda place: (-query_relevance(place, case.query), -place.prominence, place.place_id))
    rank = next((index for index, place in enumerate(candidates, 1) if place.place_id in eligible_ids), None)
    return {
        "case_id": case.case_id,
        "query": case.query,
        "routing_class": case.routing_class,
        "eligible_count": len(eligible_ids),
        "retained_count": len(retained_ids),
        "retention_rate": len(retained_ids) / len(eligible_ids) if eligible_ids else None,
        "covered": len(retained_ids) >= case.min_retained,
        "first_target_rank": rank,
        "top_k_hit": rank is not None and rank <= top_k,
        "note": case.note,
    }


def summarize_cases(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    labelled = [row for row in rows if row["eligible_count"] > 0]
    return {
        "case_count": len(rows),
        "labelled_case_count": len(labelled),
        "coverage_rate": (sum(row["covered"] for row in labelled) / len(labelled)) if labelled else None,
        "top_k_rate": (sum(row["top_k_hit"] for row in labelled) / len(labelled)) if labelled else None,
        "unlabelled_cases": [row["case_id"] for row in rows if row["eligible_count"] == 0],
    }


def case_indexes(
    cases: Sequence[Case],
) -> tuple[dict[str, set[int]], dict[str, set[int]], dict[str, set[int]]]:
    by_expected_id: dict[str, set[int]] = defaultdict(set)
    by_name: dict[str, set[int]] = defaultdict(set)
    by_brand: dict[str, set[int]] = defaultdict(set)
    for index, case in enumerate(cases):
        for place_id in case.expected_ids:
            by_expected_id[place_id].add(index)
        if case.target_name:
            by_name[normalize(case.target_name)].add(index)
        if case.target_brand:
            by_brand[normalize(case.target_brand)].add(index)
    return by_expected_id, by_name, by_brand


def add_eligible_place(
    place: Place,
    cases: Sequence[Case],
    indexes: tuple[dict[str, set[int]], dict[str, set[int]], dict[str, set[int]]],
    eligible: dict[str, set[str]],
) -> None:
    by_expected_id, by_name, by_brand = indexes
    possible = set(by_expected_id.get(place.place_id, ()))
    if place.primary_name:
        possible.update(by_name.get(normalize(place.primary_name), ()))
    if place.brand_name:
        possible.update(by_brand.get(normalize(place.brand_name), ()))
    for index in possible:
        case = cases[index]
        if matches_case(place, case):
            eligible[case.case_id].add(place.place_id)


def eligible_ids_by_case(places: Iterable[Place], cases: Sequence[Case]) -> dict[str, set[str]]:
    """Resolve labels in one source pass, even for a large curated case set."""
    indexes = case_indexes(cases)
    eligible = {case.case_id: set() for case in cases}
    for place in places:
        add_eligible_place(place, cases, indexes, eligible)
    return eligible


def routing_recommendation(case: Case, results: Sequence[dict[str, Any]]) -> dict[str, Any]:
    smallest = results[0]
    largest = results[-1]
    if smallest["eligible_count"] == 0:
        route = "label_or_source_gap"
        reason = "No target matched the source; verify the rule/GERS ID and full-export coverage first."
    elif case.routing_class == "ubiquitous_brand":
        route = "regional_places_shards"
        reason = "A global token can choose regions, but individual chain locations belong in local shards."
    elif case.routing_class == "local_unique":
        route = "regional_places_shards"
        reason = "Local intent needs geographic routing and should not consume global HEAD capacity."
    elif smallest["covered"] and smallest["top_k_hit"]:
        route = "global_head_candidate"
        reason = "The famous unique target survives the smallest sample and ranks in its top-k."
    else:
        route = "improve_prominence_signals"
        reason = "The famous unique target is not reliably retained and ranked in the smallest sample."
    return {
        "case_id": case.case_id,
        "routing_class": case.routing_class,
        "recommendation": route,
        "reason": reason,
        "smallest_sample_covered": smallest["covered"],
        "largest_sample_covered": largest["covered"],
    }


def assemble_report(
    source_count: int,
    ordered_by_strategy: dict[str, Sequence[Place]],
    eligible: dict[str, set[str]],
    cases: Sequence[Case],
    sizes: Sequence[int],
    strategies: Sequence[str],
    top_k: int,
) -> dict[str, Any]:
    sizes = sorted(set(size for size in sizes if size > 0))
    if not sizes:
        raise ValueError("at least one positive sample size is required")
    if source_count == 0:
        raise ValueError("source contains no places")
    strategy_reports = []
    for strategy in strategies:
        ordered = ordered_by_strategy[strategy]
        samples = []
        per_case: dict[str, list[dict[str, Any]]] = defaultdict(list)
        previous_ids: set[str] = set()
        for requested_size in sizes:
            sample = ordered[:requested_size]
            ids = {place.place_id for place in sample}
            case_rows = [evaluate_case(sample, case, eligible[case.case_id], top_k) for case in cases]
            for row in case_rows:
                per_case[row["case_id"]].append(row)
            samples.append({
                "requested_size": requested_size,
                "actual_size": len(sample),
                "incremental_rows": len(ids - previous_ids),
                "summary": summarize_cases(case_rows),
                "cases": case_rows,
            })
            previous_ids = ids
        strategy_reports.append({
            "strategy": strategy,
            "samples": samples,
            "routing": [
                routing_recommendation(case, per_case[case.case_id]) for case in cases
            ],
        })
    return {
        "schema_version": 1,
        "source_count": source_count,
        "requested_sizes": list(sizes),
        "top_k": top_k,
        "assumptions": [
            "Samples are deterministic nested prefixes of one sorted source dataset.",
            "Prominence mirrors the current prototype formula; confidence sampling mirrors the download LIMIT ordering.",
            "Offline query rank is an approximation (exact/prefix/token match, then prominence), not SQLite FTS/BM25.",
            "Results measure only the supplied source ceiling and labels; a pre-sampled input cannot measure excluded rows.",
            "The streaming CLI assumes source place IDs are unique, matching the Overture Places contract.",
        ],
        "strategies": strategy_reports,
    }


def run_benchmark(
    places: Sequence[Place], cases: Sequence[Case], sizes: Sequence[int],
    strategies: Sequence[str], top_k: int = 5,
) -> dict[str, Any]:
    ordered = {
        strategy: sorted(places, key=lambda place: strategy_key(place, strategy))
        for strategy in strategies
    }
    return assemble_report(
        len(places), ordered, eligible_ids_by_case(places, cases),
        cases, sizes, strategies, top_k,
    )


def run_benchmark_file(
    path: Path,
    cases: Sequence[Case],
    sizes: Sequence[int],
    strategies: Sequence[str],
    top_k: int = 5,
) -> dict[str, Any]:
    """Benchmark a large source with memory bounded by cases + max sample size."""
    positive_sizes = [size for size in sizes if size > 0]
    if not positive_sizes:
        raise ValueError("at least one positive sample size is required")

    source_count = 0
    indexes = case_indexes(cases)
    eligible = {case.case_id: set() for case in cases}
    for place in iter_places(path):
        source_count += 1
        add_eligible_place(place, cases, indexes, eligible)

    max_size = max(positive_sizes)
    ordered = {
        strategy: heapq.nsmallest(
            max_size,
            iter_places(path),
            key=lambda place, strategy=strategy: strategy_key(place, strategy),
        )
        for strategy in strategies
    }
    return assemble_report(
        source_count, ordered, eligible, cases, sizes, strategies, top_k,
    )


def percent(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_markdown(report: dict[str, Any], source: str) -> str:
    lines = [
        "# Places sampling benchmark", "", f"Source: `{source}` ({report['source_count']:,} rows)", "",
        "## Sample quality", "",
        "| Strategy | Rows | Label coverage | Top-k hit rate | Unlabelled cases |",
        "|---|---:|---:|---:|---:|",
    ]
    for strategy in report["strategies"]:
        for sample in strategy["samples"]:
            summary = sample["summary"]
            lines.append(
                f"| {strategy['strategy']} | {sample['actual_size']:,} | "
                f"{percent(summary['coverage_rate'])} | {percent(summary['top_k_rate'])} | "
                f"{len(summary['unlabelled_cases'])} |"
            )
    lines.extend(["", "## Case detail", "", "| Strategy | Rows | Case | Class | Eligible | Retained | Rank | Covered |", "|---|---:|---|---|---:|---:|---:|:---:|"])
    for strategy in report["strategies"]:
        for sample in strategy["samples"]:
            for case in sample["cases"]:
                rank = case["first_target_rank"] if case["first_target_rank"] is not None else "-"
                lines.append(
                    f"| {strategy['strategy']} | {sample['actual_size']:,} | {case['case_id']} | "
                    f"{case['routing_class']} | {case['eligible_count']} | {case['retained_count']} | "
                    f"{rank} | {'yes' if case['covered'] else 'no'} |"
                )
    lines.extend(["", "## Routing interpretation", ""])
    for strategy in report["strategies"]:
        lines.append(f"### {strategy['strategy']}")
        lines.append("")
        for item in strategy["routing"]:
            lines.append(f"- `{item['case_id']}`: **{item['recommendation']}** — {item['reason']}")
        lines.append("")
    lines.extend(["## Assumptions and limits", ""])
    lines.extend(f"- {item}" for item in report["assumptions"])
    return "\n".join(lines).rstrip() + "\n"


def parse_csv_values(raw: str) -> list[str]:
    return [value.strip() for value in raw.split(",") if value.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Local flattened Places parquet/CSV/JSONL/JSON")
    parser.add_argument("--cases", required=True, type=Path, help="Curated case JSON")
    parser.add_argument("--sizes", default="10000,25000,50000", help="Comma-separated nested sample sizes")
    parser.add_argument("--strategies", default=",".join(STRATEGIES), help="confidence,prominence")
    parser.add_argument("--top-k", type=int, default=5, help="Rank cutoff for hit-rate metrics")
    parser.add_argument("--json-out", type=Path, help="Write machine-readable report")
    parser.add_argument("--markdown-out", type=Path, help="Write Markdown report")
    args = parser.parse_args(argv)
    if not args.input.is_file() or not args.cases.is_file():
        parser.error("input and cases must be existing local files")
    try:
        sizes = [int(value) for value in parse_csv_values(args.sizes)]
        strategies = parse_csv_values(args.strategies)
        unknown = set(strategies) - set(STRATEGIES)
        if unknown:
            raise ValueError(f"unknown strategies: {', '.join(sorted(unknown))}")
        if args.top_k < 1:
            raise ValueError("top-k must be positive")
        report = run_benchmark_file(
            args.input, load_cases(args.cases), sizes, strategies, args.top_k
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    markdown = render_markdown(report, str(args.input))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if not args.markdown_out:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
