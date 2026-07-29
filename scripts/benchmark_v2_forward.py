#!/usr/bin/env python3
"""
Accuracy benchmark for the /v2/forward endpoint (divisions + construction-v1
Places/Addresses blend).

Two modes:

``sample`` builds a deterministic (seeded) self-recall case set from the
per-record artifacts a construction-v1 slice work tree leaves behind
(``run_slice_construction_v1.py --work <root>``): Places positions packs
(``**/positions/*.parquet``) and Address records packs
(``**/records/*.parquet``). Places cases are stratified by country, category,
confidence band, name script (latin vs non-latin), and token commonality of the
primary name (document frequency within the sampled artifact); Address cases by
country and completeness (number/unit/postcode). Each case records the expected
gers_id, expected coordinates, the query, and a location-bias point jittered
near the record. Planet-scale sampling straight from R2 (``--r2-prefix``) is a
stub until credentials/scope are decided.

``run`` executes a case file against ``/v2/forward``. Per case: found@1 and
found@10 (exact gers_id for self-recall cases, name+distance for the built-in
sets), rank, distance-km to the expected point, latency. Also runs built-in
blend-seam cases (names that are both a division and a POI, with and without
bias), a small multilingual exonym set, and geocoder-tester world cases when
that checkout exists. Requests are paced under the worker's 60 req/min per-IP
limit. A 503 ``release_unavailable`` aborts with a clear message and exit
code 3 — the endpoint answers only after a v2 release is promoted.

Usage:
    # Build slice artifacts first (fast loop, ~13 s, no credentials):
    python scripts/build_slice_inventory_v1.py --release 2026-07-22.0 \\
        --bbox 7.36 43.71 7.47 43.78 --output slice/inventory.json
    python scripts/run_slice_construction_v1.py --inventory slice/inventory.json \\
        --task-index 33 --release 2026-07-22.0 --work slice/work

    python scripts/benchmark_v2_forward.py sample --slice-root slice/work \\
        --output v2-cases.json
    python scripts/benchmark_v2_forward.py run --cases v2-cases.json \\
        --output results.json
    python scripts/benchmark_v2_forward.py run --cases v2-cases.json \\
        --compare baseline.json --assert-recall 0.9

Exit codes: 0 ok; 1 --assert-recall failed; 2 usage/case-loading error;
3 v2 release unavailable.
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
import unicodedata
from datetime import datetime, timezone
from math import atan2, cos, radians, sin, sqrt
from pathlib import Path

import requests

DEFAULT_BASE_URL = "https://geocoder.bradr.dev"
CASES_SCHEMA = "benchmark-v2-forward-cases-v1"
RESULTS_SCHEMA = "benchmark-v2-forward-results-v1"
GEOCODER_TESTER_DIR = Path.home() / "dev" / "geocoder-tester" / "geocoder_tester" / "world"
# search_places drops queries above four tokens, so longer names can never
# self-recall through the POI path; they are excluded at sampling time.
MAX_QUERY_TOKENS = 4
# Self-recall kinds are scored by exact gers_id; the rest by name + distance.
SELF_RECALL_KINDS = ("place", "address")
FOUND_AT = 10

# The eight structured-address key fields, in the worker's FIELD_NAMES order.
# admin_level_general/admin_level_specific are the first/last address_levels
# entry (see geocoder-construction main.rs). Empty values are literal parts of
# the key, so cases send exactly the stored values and omit only empties.
ADDRESS_FIELDS = (
    "country", "admin_level_general", "admin_level_specific", "postal_city",
    "postcode", "street", "number", "unit",
)

# Blend-seam built-ins: names that are simultaneously a division and a common
# POI name. Without bias the division should win; with a nearby bias the blend
# must still surface the right locality within FOUND_AT.
SEAM_CASES = [
    {"id": "seam:paris", "kind": "seam", "query": "Paris",
     "expected_name": "Paris", "alt_names": [],
     "expected_lat": 48.8566, "expected_lon": 2.3522, "tolerance_km": 50.0},
    {"id": "seam:paris:biased", "kind": "seam", "query": "Paris",
     "expected_name": "Paris", "alt_names": [],
     "expected_lat": 48.8566, "expected_lon": 2.3522, "tolerance_km": 50.0,
     "proximity": [2.3522, 48.8566]},
    {"id": "seam:monaco", "kind": "seam", "query": "Monaco",
     "expected_name": "Monaco", "alt_names": [],
     "expected_lat": 43.7384, "expected_lon": 7.4246, "tolerance_km": 50.0},
    {"id": "seam:monaco:biased", "kind": "seam", "query": "Monaco",
     "expected_name": "Monaco", "alt_names": [],
     "expected_lat": 43.7384, "expected_lon": 7.4246, "tolerance_km": 50.0,
     "proximity": [7.4246, 43.7384]},
    {"id": "seam:nice", "kind": "seam", "query": "Nice",
     "expected_name": "Nice", "alt_names": [],
     "expected_lat": 43.7102, "expected_lon": 7.2620, "tolerance_km": 50.0},
    {"id": "seam:nice:biased", "kind": "seam", "query": "Nice",
     "expected_name": "Nice", "alt_names": [],
     "expected_lat": 43.7102, "expected_lon": 7.2620, "tolerance_km": 50.0,
     "proximity": [7.2620, 43.7102]},
]

# Third-language exonyms (benchmark_typeahead.py's multilingual idea): a hit
# counts under any accepted name form as long as it is the right place.
MULTILINGUAL_CASES = [
    {"id": "ml:moscou", "kind": "multilingual", "query": "moscou",
     "expected_name": "Москва", "alt_names": ["Moscow", "Moscou", "Moskva"],
     "expected_lat": 55.7558, "expected_lon": 37.6173, "tolerance_km": 50.0},
    {"id": "ml:londres", "kind": "multilingual", "query": "londres",
     "expected_name": "London", "alt_names": ["Londres"],
     "expected_lat": 51.5074, "expected_lon": -0.1278, "tolerance_km": 50.0},
    {"id": "ml:tokio", "kind": "multilingual", "query": "tokio",
     "expected_name": "東京都", "alt_names": ["Tokyo", "東京", "Tokio"],
     "expected_lat": 35.6762, "expected_lon": 139.6503, "tolerance_km": 50.0},
    {"id": "ml:praga", "kind": "multilingual", "query": "praga",
     "expected_name": "Praha", "alt_names": ["Prague", "Praga"],
     "expected_lat": 50.0875, "expected_lon": 14.4213, "tolerance_km": 50.0},
    {"id": "ml:pekin", "kind": "multilingual", "query": "pekin",
     "expected_name": "北京市", "alt_names": ["Beijing", "北京", "Pekin", "Pékin"],
     "expected_lat": 39.9042, "expected_lon": 116.4074, "tolerance_km": 50.0},
]


class ReleaseUnavailableError(Exception):
    """The endpoint answered 503 release_unavailable: no v2 release yet."""


# ---------------------------------------------------------------------------
# Shared pure helpers


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return r * 2 * atan2(sqrt(a), sqrt(1 - a))


def normalize_name(value):
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in value if not unicodedata.combining(char))


def format_gers_id(feature_id):
    """Hyphenated lowercase UUID from a 16-byte feature_id (str passes through)."""
    if isinstance(feature_id, str):
        return feature_id.lower()
    raw = bytes(feature_id).hex()
    if len(raw) != 32:
        raise ValueError(f"feature_id must be 16 bytes, got {len(raw) // 2}")
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


def tokenize(name):
    return [token for token in re.findall(r"\w+", name.casefold()) if token]


def name_script(name):
    """"latin", "non_latin", or "other" (no letters at all)."""
    latin = other = 0
    for char in name:
        if not char.isalpha():
            continue
        if unicodedata.name(char, "").startswith("LATIN"):
            latin += 1
        else:
            other += 1
    if latin == other == 0:
        return "other"
    return "latin" if latin >= other else "non_latin"


def confidence_band(rank):
    """uint8 confidence_rank -> one of four fixed bands."""
    rank = max(0, min(255, int(rank)))
    low = (rank // 64) * 64
    return f"{low}-{low + 63}"


def token_document_frequencies(names):
    """Token -> number of names containing it (once per name)."""
    frequencies = {}
    for name in names:
        for token in set(tokenize(name)):
            frequencies[token] = frequencies.get(token, 0) + 1
    return frequencies


def commonality_bucket(name, frequencies):
    """Bucket by the most common token: rare (<=2 docs), mid (<=20), common."""
    tokens = tokenize(name)
    if not tokens:
        return "rare"
    peak = max(frequencies.get(token, 0) for token in tokens)
    if peak <= 2:
        return "rare"
    if peak <= 20:
        return "mid"
    return "common"


def stratum_key(strata):
    return "|".join(f"{key}={strata[key]}" for key in sorted(strata))


# ---------------------------------------------------------------------------
# sample: case construction (pure; artifact loading is separate)


def _stable_rng(seed, label):
    import random

    return random.Random(f"{seed}:{label}")


def jitter_point(rng, lat, lon):
    """Bias point near the record: up to ~2 km offset, deterministic per rng."""
    bias_lat = max(-90.0, min(90.0, lat + rng.uniform(-0.02, 0.02)))
    bias_lon = max(-180.0, min(180.0, lon + rng.uniform(-0.02, 0.02)))
    return round(bias_lon, 6), round(bias_lat, 6)


def place_stratum(record, frequencies):
    name = record.get("primary_name") or ""
    return {
        "country": record.get("country") or "unknown",
        "category": record.get("category") or "uncategorized",
        "confidence": confidence_band(record.get("confidence_rank") or 0),
        "script": name_script(name),
        "commonality": commonality_bucket(name, frequencies),
    }


def build_place_cases(records, seed, per_stratum, max_cases):
    """Deterministic stratified self-recall cases from positions records.

    Records need: feature_id, primary_name, locality, category, country,
    confidence_rank, longitude, latitude. Records with empty names, names
    longer than MAX_QUERY_TOKENS tokens, or duplicate gers_ids are skipped.
    """
    usable, seen = [], set()
    for record in records:
        name = (record.get("primary_name") or "").strip()
        if not name or len(tokenize(name)) > MAX_QUERY_TOKENS:
            continue
        gers = format_gers_id(record["feature_id"])
        if gers in seen:
            continue
        seen.add(gers)
        usable.append((gers, name, record))
    frequencies = token_document_frequencies(name for _, name, _ in usable)

    strata = {}
    for gers, name, record in usable:
        key = stratum_key(place_stratum(record, frequencies))
        strata.setdefault(key, []).append((gers, name, record))

    cases = []
    for key in sorted(strata):
        members = sorted(strata[key], key=lambda item: item[0])
        rng = _stable_rng(seed, f"place:{key}")
        rng.shuffle(members)
        for gers, name, record in members[:per_stratum]:
            case_rng = _stable_rng(seed, f"place-case:{gers}")
            locality = (record.get("locality") or "").strip()
            query, style = name, "name"
            with_locality = f"{name} {locality}"
            if (locality and case_rng.random() < 0.5
                    and len(tokenize(with_locality)) <= MAX_QUERY_TOKENS):
                query, style = with_locality, "name_locality"
            lat, lon = float(record["latitude"]), float(record["longitude"])
            bias_lon, bias_lat = jitter_point(case_rng, lat, lon)
            cases.append({
                "id": f"place:{gers}",
                "kind": "place",
                "query": query,
                "query_style": style,
                "expected_gers_id": gers,
                "expected_lat": lat,
                "expected_lon": lon,
                "proximity": [bias_lon, bias_lat],
                "strata": place_stratum(record, frequencies),
            })
    cases.sort(key=lambda case: case["id"])
    return _cap_per_stratum(cases, max_cases, seed, "place-cap")


def address_completeness(record):
    parts = [part for part in ("number", "unit", "postcode")
             if (record.get(part) or "").strip()]
    return "+".join(parts) if parts else "bare"


def address_stratum(record):
    return {
        "country": (record.get("country") or "unknown").lower(),
        "completeness": address_completeness(record),
    }


def address_case_params(record):
    """The exact 8-field structured query for a records-pack row.

    Empty fields are omitted: the worker treats a missing parameter as the
    literal empty string, which is what the stored key holds for them.

    The producer (geocoder-construction `levels()`) drops only NULL entries and
    keys on first/last including empty strings, so only None is filtered here —
    dropping "" would shift first/last and mismatch the stored key.
    """
    levels = [level for level in (record.get("address_levels") or [])
              if level is not None]
    values = {
        "country": record.get("country") or "",
        "admin_level_general": levels[0] if levels else "",
        "admin_level_specific": levels[-1] if levels else "",
        "postal_city": record.get("postal_city") or "",
        "postcode": record.get("postcode") or "",
        "street": record.get("street") or "",
        "number": record.get("number") or "",
        "unit": record.get("unit") or "",
    }
    return {field: values[field] for field in ADDRESS_FIELDS if values[field]}


def build_address_cases(records, seed, per_stratum, max_cases):
    """Deterministic stratified structured-address self-recall cases.

    Records need the records-pack display columns plus feature_id, the E7
    coordinates, and the source locator triple (case identity: two admitted
    rows may share a feature_id). Rows missing country/street/number cannot be
    looked up structurally and are skipped.
    """
    strata = {}
    for record in records:
        if not all((record.get(field) or "").strip()
                   for field in ("country", "street", "number")):
            continue
        locator = (record.get("source_object_index"), record.get("source_row_group"),
                   record.get("source_row_index"))
        gers = format_gers_id(record["feature_id"])
        key = stratum_key(address_stratum(record))
        strata.setdefault(key, []).append((gers, locator, record))

    cases = []
    for key in sorted(strata):
        members = sorted(strata[key], key=lambda item: (item[0], item[1]))
        rng = _stable_rng(seed, f"address:{key}")
        rng.shuffle(members)
        for gers, locator, record in members[:per_stratum]:
            lat = int(record["latitude_e7"]) / 1e7
            lon = int(record["longitude_e7"]) / 1e7
            display = ", ".join(part for part in (
                f"{record.get('number', '')} {record.get('street', '')}".strip(),
                record.get("postal_city") or "", record.get("postcode") or "",
                (record.get("display_country") or record.get("country") or "").upper(),
            ) if part)
            cases.append({
                "id": f"address:{gers}:{locator[0]}-{locator[1]}-{locator[2]}",
                "kind": "address",
                "query": display,
                "params": address_case_params(record),
                "expected_gers_id": gers,
                "expected_lat": lat,
                "expected_lon": lon,
                "strata": address_stratum(record),
            })
    cases.sort(key=lambda case: case["id"])
    return _cap_per_stratum(cases, max_cases, seed, "address-cap")


def _cap_per_stratum(cases, max_cases, seed, label):
    """Round-robin across strata so a cap keeps strata represented.

    The stratum visit order is seeded-shuffled, not alphabetical: with many
    single-member strata (fine categories) an alphabetical order would keep
    only the lexicographically first strata.
    """
    if max_cases is None or len(cases) <= max_cases:
        return cases
    queues = {}
    for case in cases:
        queues.setdefault(stratum_key(case["strata"]), []).append(case)
    order = sorted(queues)
    _stable_rng(seed, label).shuffle(order)
    kept = []
    while len(kept) < max_cases:
        progressed = False
        for key in order:
            if queues[key] and len(kept) < max_cases:
                kept.append(queues[key].pop(0))
                progressed = True
        if not progressed:
            break
    kept.sort(key=lambda case: case["id"])
    return kept


def case_counts(cases):
    counts = {}
    for case in cases:
        counts[case["kind"]] = counts.get(case["kind"], 0) + 1
    return counts


def stratum_counts(cases):
    counts = {}
    for case in cases:
        if "strata" in case:
            key = f"{case['kind']}:{stratum_key(case['strata'])}"
            counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def build_case_file(cases, seed, sources, timestamp=None):
    return {
        "schema": CASES_SCHEMA,
        "meta": {
            "seed": seed,
            "sources": sources,
            "timestamp": timestamp,
            "case_counts": case_counts(cases),
            "stratum_counts": stratum_counts(cases),
        },
        "cases": cases,
    }


# ---------------------------------------------------------------------------
# sample: artifact loading (pyarrow, local slice work tree)

PLACE_COLUMNS = ("feature_id", "longitude", "latitude", "primary_name",
                 "category", "locality", "country", "confidence_rank")
ADDRESS_COLUMNS = ("feature_id", "longitude_e7", "latitude_e7", "country",
                   "display_country", "postal_city", "postcode", "street",
                   "number", "unit", "address_levels", "source_object_index",
                   "source_row_group", "source_row_index")


def _load_packs(root, directory_name, columns):
    import pyarrow.parquet as pq

    rows, loaded = [], 0
    for path in sorted(Path(root).rglob(f"{directory_name}/*.parquet")):
        schema_names = set(pq.ParquetFile(path).schema_arrow.names)
        if not set(columns) <= schema_names:
            continue
        rows.extend(pq.read_table(path, columns=list(columns)).to_pylist())
        loaded += 1
    return rows, loaded


def load_place_records(root):
    return _load_packs(root, "positions", PLACE_COLUMNS)


def load_address_records(root):
    return _load_packs(root, "records", ADDRESS_COLUMNS)


def load_r2_records(prefix):
    # Planet artifacts live under a credentialed R2 prefix; sampling them will
    # reuse the construction remote transport once a scoped read token exists
    # (see R2 credential lockdown TODO). Local slice work trees cover the
    # correctness rung until then.
    raise NotImplementedError(
        "sampling planet artifacts from R2 is not implemented yet; "
        "use --slice-root against a local slice work tree"
    )


# ---------------------------------------------------------------------------
# run: built-in and geocoder-tester case sets


def load_geocoder_tester_cases(directory, limit, seed):
    """World cases from a geocoder-tester checkout; [] when absent."""
    import csv

    directory = Path(directory)
    if not directory.is_dir():
        return []
    rows = []
    for csv_path in sorted(directory.rglob("*.csv")):
        try:
            with open(csv_path, encoding="utf-8") as handle:
                reader = csv.DictReader(handle, delimiter=";")
                for index, row in enumerate(reader):
                    query = (row.get("query") or "").strip()
                    coordinate = (row.get("expected_coordinate") or "").split(",")
                    if not query or len(coordinate) < 2:
                        continue
                    try:
                        lat, lon = float(coordinate[0]), float(coordinate[1])
                    except ValueError:
                        continue
                    rows.append({
                        "id": f"tester:{csv_path.stem}:{index}",
                        "kind": "tester",
                        "query": query,
                        "expected_name": (row.get("expected_name") or "").strip(),
                        "alt_names": [],
                        "expected_lat": lat,
                        "expected_lon": lon,
                        "tolerance_km": 25.0,
                    })
        except OSError:
            continue
    rows.sort(key=lambda case: case["id"])
    if limit is not None and len(rows) > limit:
        rng = _stable_rng(seed, "tester")
        rows = sorted(rng.sample(rows, limit), key=lambda case: case["id"])
    return rows


# ---------------------------------------------------------------------------
# run: request execution and scoring


def case_request(case, base_url, limit):
    """(url, params) for one case."""
    url = f"{base_url.rstrip('/')}/v2/forward"
    if case["kind"] == "address":
        return url, dict(case["params"])
    params = {"q": case["query"], "limit": str(limit), "autocomplete": "false"}
    if case.get("proximity"):
        lon, lat = case["proximity"]
        params["proximity"] = f"{lon},{lat}"
    return url, params


def feature_point(feature):
    coordinates = (feature.get("geometry") or {}).get("coordinates") or []
    if len(coordinates) < 2:
        return None
    return float(coordinates[1]), float(coordinates[0])


def _name_matches(case, feature):
    observed = (feature.get("properties") or {}).get("name")
    if not isinstance(observed, str) or not observed:
        return False
    observed = normalize_name(observed.split(",")[0])
    accepted = [case["expected_name"], *case.get("alt_names", [])]
    return any(observed.startswith(normalize_name(name))
               or normalize_name(name).startswith(observed)
               for name in accepted if name)


def score_case(case, features):
    """rank (1-based or None), distance to matched feature, top-1 distance."""
    expected = (case["expected_lat"], case["expected_lon"])
    rank = matched_distance = None
    for index, feature in enumerate(features):
        point = feature_point(feature)
        if case["kind"] in SELF_RECALL_KINDS:
            hit = (feature.get("id") or "").lower() == case["expected_gers_id"]
        else:
            hit = (_name_matches(case, feature) and point is not None
                   and haversine_km(*expected, *point) <= case.get("tolerance_km", 50.0))
        if hit:
            rank = index + 1
            if point is not None:
                matched_distance = haversine_km(*expected, *point)
            break
    top1_distance = None
    if features and (point := feature_point(features[0])) is not None:
        top1_distance = haversine_km(*expected, *point)
    return rank, matched_distance, top1_distance


def check_release_unavailable(status, body):
    if status == 503 and isinstance(body, dict) and body.get("error") == "release_unavailable":
        raise ReleaseUnavailableError(body.get("message") or "release unavailable")


class Runner:
    """Paced /v2/forward executor (same pacing scheme as benchmark_latency)."""

    def __init__(self, base_url, interval, timeout, limit=FOUND_AT,
                 sleep_fn=time.sleep, monotonic_fn=time.monotonic,
                 rate_limit_retries=1):
        self.base_url = base_url.rstrip("/")
        self.interval = interval
        self.timeout = timeout
        self.limit = limit
        self.session = requests.Session()
        self._sleep = sleep_fn
        self._monotonic = monotonic_fn
        self._last_request = None
        self.rate_limit_retries = max(0, rate_limit_retries)
        self.results = []
        self.data_version = None

    def _pace(self):
        now = self._monotonic()
        if self._last_request is None:
            self._last_request = now
            return
        wait = self._last_request + self.interval - now
        if wait > 0:
            self._sleep(wait)
        self._last_request = self._monotonic()

    def execute(self, case):
        url, params = case_request(case, self.base_url, self.limit)
        status, body, error = None, None, None
        ms = 0.0
        for attempt in range(self.rate_limit_retries + 1):
            # Pacing and Retry-After sleeps happen before the timer starts, so
            # ms measures only the (last) HTTP attempt, not the send schedule.
            self._pace()
            start = time.perf_counter()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                ms = (time.perf_counter() - start) * 1000
                status = resp.status_code
                if status == 429 and attempt < self.rate_limit_retries:
                    try:
                        retry_after = float(resp.headers.get("Retry-After", 30))
                    except (TypeError, ValueError):
                        retry_after = 30.0
                    self._sleep(max(0.0, min(retry_after, 60.0)))
                    continue
                try:
                    body = resp.json()
                except ValueError:
                    body = None
                if self.data_version is None:
                    self.data_version = resp.headers.get("X-Geocoder-Build")
                break
            except requests.RequestException as exception:
                ms = (time.perf_counter() - start) * 1000
                error = str(exception)
                break
        check_release_unavailable(status, body)

        features = body.get("features", []) if isinstance(body, dict) else []
        ok = status is not None and 200 <= status < 400
        rank = matched_distance = top1_distance = None
        if ok:
            rank, matched_distance, top1_distance = score_case(case, features)
        elif error is None:
            error = f"http {status}"
        result = {
            "case_id": case["id"],
            "kind": case["kind"],
            "query": case.get("query"),
            "query_style": case.get("query_style"),
            "status": status,
            "ms": round(ms, 1),
            "error": error,
            "rank": rank,
            "found_at_1": rank == 1 if ok else False,
            "found_at_10": rank is not None and rank <= FOUND_AT if ok else False,
            "matched_distance_km": _round_or_none(matched_distance),
            "top1_distance_km": _round_or_none(top1_distance),
        }
        if "strata" in case:
            result["strata"] = case["strata"]
        self.results.append(result)
        shown = rank if rank is not None else "-"
        print(f"  {case['kind']:<12} {str(case.get('query'))[:40]:<42} "
              f"{ms:6.0f}ms  rank {shown}  {status if error is None else error}")
        return result


def _round_or_none(value):
    return None if value is None else round(value, 3)


# ---------------------------------------------------------------------------
# run: aggregation, comparison, assertions


def _percentile(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(len(ordered) * percentile), len(ordered) - 1)]


def _aggregate(rows):
    successful = [row for row in rows if row["error"] is None]
    found1 = sum(row["found_at_1"] for row in rows)
    found5 = sum(
        row["rank"] is not None and row["rank"] <= 5
        for row in rows
    )
    found10 = sum(row["found_at_10"] for row in rows)
    ranks = [row["rank"] for row in rows if row["rank"] is not None]
    distances = [row["top1_distance_km"] for row in successful
                 if row["top1_distance_km"] is not None]
    latencies = [row["ms"] for row in successful]
    denominator = len(rows)
    return {
        "n": denominator,
        "errors": denominator - len(successful),
        "found_at_1": found1,
        "found_at_5": found5,
        "found_at_10": found10,
        # Transport failures are misses, as well as a separate zero-error gate.
        # Excluding them would make an unavailable endpoint look more accurate.
        "recall_at_1": round(found1 / denominator, 3) if rows else None,
        "recall_at_5": round(found5 / denominator, 3) if rows else None,
        "recall_at_10": round(found10 / denominator, 3) if rows else None,
        "mrr": round(sum(1 / rank for rank in ranks) / denominator, 3) if rows else None,
        "median_top1_distance_km": (round(statistics.median(distances), 3)
                                    if distances else None),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": round(_percentile(latencies, 0.95), 1) if latencies else None,
    }


def summarize_results(rows):
    by_kind, by_query_style, by_stratum = {}, {}, {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)
        if row.get("query_style"):
            key = f"{row['kind']}:{row['query_style']}"
            by_query_style.setdefault(key, []).append(row)
        if row["kind"] in SELF_RECALL_KINDS and "strata" in row:
            key = f"{row['kind']}:{stratum_key(row['strata'])}"
            by_stratum.setdefault(key, []).append(row)
    self_recall_rows = [row for row in rows if row["kind"] in SELF_RECALL_KINDS]
    return {
        "overall": _aggregate(rows),
        "self_recall": _aggregate(self_recall_rows),
        "by_kind": {kind: _aggregate(group) for kind, group in sorted(by_kind.items())},
        "by_query_style": {
            key: _aggregate(group) for key, group in sorted(by_query_style.items())
        },
        "by_stratum": {key: _aggregate(group)
                       for key, group in sorted(by_stratum.items())},
    }


def self_recall_at_10(summary):
    return summary["self_recall"]["recall_at_10"]


def compare_summaries(baseline, current, threshold):
    """Regression strings for recall drops beyond threshold (absolute)."""
    regressions = []
    groups = [("overall", baseline.get("overall"), current.get("overall")),
              ("self_recall", baseline.get("self_recall"), current.get("self_recall"))]
    for section in ("by_kind", "by_query_style", "by_stratum"):
        base_section = baseline.get(section, {})
        current_section = current.get(section, {})
        for key in sorted(set(base_section) & set(current_section)):
            groups.append((f"{section}:{key}", base_section[key], current_section[key]))
    for name, base, now in groups:
        if not base or not now:
            continue
        for metric in ("recall_at_1", "recall_at_10"):
            before, after = base.get(metric), now.get(metric)
            if before is None or after is None:
                continue
            if before - after > threshold:
                regressions.append(
                    f"{name} {metric} regressed {before:.3f} -> {after:.3f}"
                )
    return regressions


def print_comparison(baseline_payload, current_summary, threshold):
    meta = baseline_payload.get("meta", {})
    baseline = baseline_payload["summary"]
    print(f"\nComparison vs baseline ({meta.get('timestamp')}, "
          f"sha {meta.get('git_sha')}):")
    print(f"{'group':<34}{'base r@1':>9}{'now r@1':>9}{'base r@10':>10}{'now r@10':>10}")
    print("-" * 72)
    names = ["overall", "self_recall"]
    names += [f"by_kind:{key}" for key in sorted(
        set(baseline.get("by_kind", {})) | set(current_summary.get("by_kind", {})))]

    def lookup(summary, name):
        if name.startswith("by_kind:"):
            return summary.get("by_kind", {}).get(name.split(":", 1)[1])
        return summary.get(name)

    def cell(stats, metric):
        value = stats.get(metric) if stats else None
        return "-" if value is None else f"{value:.3f}"

    for name in names:
        base, now = lookup(baseline, name), lookup(current_summary, name)
        print(f"{name:<34}{cell(base, 'recall_at_1'):>9}{cell(now, 'recall_at_1'):>9}"
              f"{cell(base, 'recall_at_10'):>10}{cell(now, 'recall_at_10'):>10}")
    regressions = compare_summaries(baseline, current_summary, threshold)
    if regressions:
        print("\nRegressions beyond threshold "
              f"({threshold:.2f} absolute recall):")
        for regression in regressions:
            print(f"  REGRESSION: {regression}")
    else:
        print("\nNo regressions beyond threshold.")
    return regressions


def print_summary(summary):
    print(f"\n{'group':<44}{'n':>4}{'err':>4}{'r@1':>7}{'r@5':>7}{'r@10':>7}"
          f"{'mrr':>7}{'med km':>8}{'p50ms':>7}{'p95ms':>7}")
    print("-" * 92)
    rows = [("overall", summary["overall"]), ("self_recall", summary["self_recall"])]
    rows += [(f"kind:{key}", stats) for key, stats in summary["by_kind"].items()]
    rows += [(f"style:{key}", stats)
             for key, stats in summary["by_query_style"].items()]
    rows += [(key, stats) for key, stats in summary["by_stratum"].items()]
    for name, stats in rows:
        def cell(value, width, digits=3):
            return "-".rjust(width) if value is None else f"{value:>{width}.{digits}f}"
        print(f"{name[:43]:<44}{stats['n']:>4}{stats['errors']:>4}"
              f"{cell(stats['recall_at_1'], 7)}{cell(stats['recall_at_5'], 7)}"
              f"{cell(stats['recall_at_10'], 7)}"
              f"{cell(stats['mrr'], 7)}"
              f"{cell(stats['median_top1_distance_km'], 8)}"
              f"{cell(stats['p50_ms'], 7, 1)}"
              f"{cell(stats['p95_ms'], 7, 1)}")


def git_sha():
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=Path(__file__).parent,
        ).stdout.strip()
    except OSError:
        return "unknown"


# ---------------------------------------------------------------------------
# CLI


def run_sample(args):
    if args.r2_prefix:
        load_r2_records(args.r2_prefix)
    if not args.slice_root:
        print("sample requires --slice-root (or, later, --r2-prefix)", file=sys.stderr)
        return 2
    place_records, address_records = [], []
    place_packs = address_packs = 0
    for root in args.slice_root:
        if not Path(root).is_dir():
            print(f"slice root not found: {root}", file=sys.stderr)
            return 2
        places, packs = load_place_records(root)
        place_records.extend(places)
        place_packs += packs
        addresses, packs = load_address_records(root)
        address_records.extend(addresses)
        address_packs += packs
    print(f"loaded {len(place_records):,} place rows from {place_packs} positions "
          f"packs, {len(address_records):,} address rows from {address_packs} "
          f"records packs")
    if not place_records and not address_records:
        print("no positions/records packs found under the slice roots; "
              "run the fast slice loop first", file=sys.stderr)
        return 2
    cases = build_place_cases(place_records, args.seed, args.per_stratum,
                              args.max_cases)
    cases += build_address_cases(address_records, args.seed,
                                 args.address_per_stratum, args.max_cases)
    payload = build_case_file(
        cases, args.seed, sources=[str(root) for root in args.slice_root],
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    with open(args.output, "w") as handle:
        json.dump(payload, handle, indent=1, ensure_ascii=False)
    print(f"wrote {len(cases)} cases to {args.output}")
    print_dimension_summary(cases)
    return 0


def print_dimension_summary(cases):
    """Compact per-dimension counts (full strata live in the case file)."""
    for kind in sorted({case["kind"] for case in cases}):
        members = [case for case in cases if case["kind"] == kind]
        print(f"  {kind}: {len(members)} cases")
        dimensions = sorted({key for case in members for key in case["strata"]})
        for dimension in dimensions:
            counts = {}
            for case in members:
                value = case["strata"].get(dimension, "-")
                counts[value] = counts.get(value, 0) + 1
            if len(counts) > 8:
                top = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:5]
                shown = ", ".join(f"{value}={count}" for value, count in top)
                print(f"    {dimension}: {len(counts)} values ({shown}, ...)")
            else:
                shown = ", ".join(f"{value}={count}"
                                  for value, count in sorted(counts.items()))
                print(f"    {dimension}: {shown}")


def load_case_files(paths):
    cases = []
    for path in paths:
        with open(path) as handle:
            payload = json.load(handle)
        if payload.get("schema") != CASES_SCHEMA:
            raise ValueError(f"{path} is not a {CASES_SCHEMA} file")
        cases.extend(payload["cases"])
    return cases


def run_run(args):
    cases = []
    if args.cases:
        try:
            cases.extend(load_case_files(args.cases))
        except (OSError, ValueError, KeyError) as exception:
            print(f"failed to load cases: {exception}", file=sys.stderr)
            return 2
    if not args.skip_builtin:
        cases.extend(SEAM_CASES)
        cases.extend(MULTILINGUAL_CASES)
    tester_cases = load_geocoder_tester_cases(
        args.geocoder_tester, args.tester_limit, args.seed)
    if tester_cases:
        cases.extend(tester_cases)
    elif not args.skip_builtin:
        print(f"note: no geocoder-tester cases (checkout absent or empty at "
              f"{args.geocoder_tester})")
    if not cases:
        print("no cases to run", file=sys.stderr)
        return 2

    print(f"{len(cases)} cases at {args.interval}s spacing "
          f"(~{len(cases) * args.interval / 60:.1f} min) against {args.base_url}\n")
    runner = Runner(args.base_url, args.interval, args.timeout)
    try:
        for case in cases:
            runner.execute(case)
    except ReleaseUnavailableError as exception:
        print(f"\nAborting: /v2/forward returned 503 release_unavailable "
              f"({exception}).\nThe v2 endpoint answers only after a v2 release "
              f"is promoted; re-run once promotion lands.", file=sys.stderr)
        return 3

    summary = summarize_results(runner.results)
    print_summary(summary)

    payload = {
        "schema": RESULTS_SCHEMA,
        "meta": {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "base_url": args.base_url,
            "git_sha": git_sha(),
            "data_version": runner.data_version,
            "interval_s": args.interval,
            "case_counts": case_counts(cases),
        },
        "summary": summary,
        "results": runner.results,
    }
    if args.compare:
        with open(args.compare) as handle:
            print_comparison(json.load(handle), summary, args.regression_threshold)
    if args.output:
        with open(args.output, "w") as handle:
            json.dump(payload, handle, indent=1, ensure_ascii=False)
        print(f"\nResults written to {args.output}")

    if args.assert_recall is not None:
        recall = self_recall_at_10(summary)
        if recall is None or recall < args.assert_recall:
            print(f"\nSelf-recall@10 {recall} is below the required "
                  f"{args.assert_recall}", file=sys.stderr)
            return 1
        print(f"\nSelf-recall@10 {recall} meets the required {args.assert_recall}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n")[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    modes = parser.add_subparsers(dest="mode", required=True)

    sample = modes.add_parser(
        "sample", help="build a deterministic self-recall case set from slice artifacts")
    sample.add_argument("--slice-root", action="append", type=Path, default=[],
                        help="construction-v1 slice work tree; repeatable "
                             "(e.g. slice/work and slice/address-work)")
    sample.add_argument("--r2-prefix", type=str,
                        help="planet artifact prefix in R2 (not implemented yet)")
    sample.add_argument("--seed", type=int, default=42)
    sample.add_argument("--per-stratum", type=int, default=2,
                        help="place cases sampled per stratum (default: 2)")
    sample.add_argument("--address-per-stratum", type=int, default=25,
                        help="address cases sampled per stratum; address "
                             "strata are much coarser (default: 25)")
    sample.add_argument("--max-cases", type=int, default=150,
                        help="cap per family, round-robin across strata "
                             "(default: 150)")
    sample.add_argument("--output", required=True, help="cases JSON path")
    sample.set_defaults(func=run_sample)

    run = modes.add_parser("run", help="execute cases against /v2/forward")
    run.add_argument("--cases", action="append", type=Path, default=[],
                     help="cases JSON from sample mode; repeatable")
    run.add_argument("--base-url", default=DEFAULT_BASE_URL)
    run.add_argument("--interval", type=float, default=1.2,
                     help="seconds between requests; keep >1.0 to stay under "
                          "the 60 req/min rate limit (default: 1.2)")
    run.add_argument("--timeout", type=float, default=20.0)
    run.add_argument("--seed", type=int, default=42,
                     help="seed for geocoder-tester case selection")
    run.add_argument("--skip-builtin", action="store_true",
                     help="skip the built-in seam and multilingual sets")
    run.add_argument("--geocoder-tester", type=Path, default=GEOCODER_TESTER_DIR,
                     help="geocoder-tester world directory (skipped if absent)")
    run.add_argument("--tester-limit", type=int, default=25,
                     help="max geocoder-tester cases (default: 25)")
    run.add_argument("--output", type=str, help="write results JSON here")
    run.add_argument("--compare", type=str, help="baseline results JSON to diff against")
    run.add_argument("--regression-threshold", type=float, default=0.05,
                     help="absolute recall drop flagged as regression "
                          "(default: 0.05)")
    run.add_argument("--assert-recall", type=float,
                     help="exit non-zero if self-recall found@10 is below this")
    run.set_defaults(func=run_run)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
