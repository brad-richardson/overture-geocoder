# Exact-country decision artifact experiment

Date: 2026-07-12
Status: research-only local artifact. No Worker, shard build, R2 object, catalog,
deployment, or production routing behavior was changed.

## Recommendation

Do not integrate any current candidate. The conservative exact oracle retains
all land and territorial claims and is 183,095,296 bytes. The
`territorial-primary` exact artifact is 90,083,328 bytes and matches that oracle
on global points, territorial boundaries, and nearby offsets, but it routes 195
of 200 sampled all-claims boundary vertices that the oracle sends to `HEAD`.
Dropping land claims is therefore not equivalent under the stated rule that any
claim boundary is a blocker.

Ordinary simplification also fails. The 0.0025-degree artifact is 5,263,360
bytes—20,480 bytes over 5 MiB—and produced 64 false-unique, 198 false-negative,
and 145 wrong-country decisions on the primary corpus. The 0.005-degree variant
is smaller but worse. If the 5 MiB gate stands, the next research comparator
should be a conservative H3 or equivalent interior cover followed by exact full
geometry only for boundary cells. Its boundary layer must preserve both land
and territorial blockers. Keep `HEAD` fallback and add no production dependency
or Worker integration in this phase.

## Release facts and policy

The pinned `2026-06-17.0` extraction contained 378 country claim rows and 219
two-letter country codes:

- 219 land-flag rows and 219 territorial-flag rows;
- 60 dual-true rows rather than an exclusive flag pair, including 42 standard
  codes and 18 synthetic-code rows;
- exactly one territorial-flag row for every one of the 219 codes;
- 24 rows across 21 `X*` codes;
- no populated joined parent-division perspectives.

The builder therefore rejects null or false/false flags, preserves and audits
true/true as a `dual` claim, and permits a unique standard dual claim. Any
matched `X*` claim remains `HEAD/synthetic_country`. The optional
`territorial-primary` policy is allowed only if the one-territorial-row-per-code
invariant holds; otherwise the whole build fails. Every source row is decoded
and audited even when that policy excludes it from hot storage.

No geometry is silently repaired or dropped. The exact baseline requires valid
polygonal WKB, forces 2D, decomposes multipart polygons, unwraps and splits
antimeridian components, and writes canonical little-endian WKB. Simplified
variants use topology-preserving Shapely simplification and label themselves
`topology-preserving-simplified-not-an-exact-oracle` in their manifest.

## Artifact contract

The hot SQLite file contains:

- `claims`: one row per retained Overture area, with area/division IDs and
  versions, country, flags, and perspective policy fields;
- `components`: claim and geometry references plus source/piece ordinals;
- `geometries`: canonical WKB deduplicated by SHA-256;
- `component_rtree`: one bbox row per normalized component.

Source JSON is deliberately excluded from hot routing data. A separate
SHA-bound audit JSON records every input area by `area_id`, whether it was
retained, the flags and IDs/versions, perspectives, and area/geometry/division
source provenance. The manifest binds the release, local parquet SHA, extraction
SQL SHA, builder/schema SHA, hot artifact SHA/bytes, and cold sidecar SHA/bytes.
The extraction retains the complete area `sources` structure, including any
property-specific `/geometry` entries; a separate geometry-source column is not
required for this release input.

The committed extraction uses a left parent join so a missing or wrong-subtype
parent remains visible, carries the parent country, and lets the builder fail on
a null or mismatched parent country. Loader-assigned claim/component identity is
ordered by immutable `area_id`, independent of parquet row order.

Lookup exact-tests every R-tree candidate. It routes only when all interior hits
deduplicate to one non-`X*` country and there is no boundary, perspective, or
decode/database blocker. The outcomes are `route`, `no_match`,
`multiple_countries`, `synthetic_country`, `perspective_claim`, `boundary`,
`input_error`, or `artifact_error`; every non-route outcome means `HEAD`.
The query, benchmark, and comparison paths require the sibling manifest by
default and verify artifact size and SHA-256 before opening SQLite. Valid SQLite
tampering, missing manifests, malformed WKB, connection/path mismatches, and
database errors fail to `HEAD/artifact_error`. Multi-file overwrite publication
stages the artifact, audit, and manifest together and restores the prior set if
any replacement fails. Comparisons require identical Overture release and input
parquet SHA unless the caller explicitly requests a cross-source diagnostic.

## Reproduction

The measured environment was DuckDB CLI 1.5.4 for extraction and Python
3.12.12 with DuckDB 1.4.4, Shapely 2.1.2, and SQLite 3.51.0 on macOS arm64 for
builds and diagnostics.
Materialize and verify the pinned input:

```sh
sed -e 's|__OVERTURE_RELEASE__|2026-06-17.0|g' \
    -e 's|__OUTPUT_PATH__|/tmp/overture-country-2026-06-17.0.parquet|g' \
    scripts/extract_country_router.sql | duckdb

test "$(shasum -a 256 /tmp/overture-country-2026-06-17.0.parquet | cut -d' ' -f1)" = \
  2921a0d40ea8929e6ce1410aba5cb947319cb7c61246182d6bc4e66cc74f2775
```

Build all four measured artifacts. Each build writes sibling `.audit.json` and
`.manifest.json` files:

```sh
build_variant() {
  python3 scripts/experiment_exact_country_router.py build \
    --division-area-parquet /tmp/overture-country-2026-06-17.0.parquet \
    --overture-release 2026-06-17.0 \
    --expected-country-count 219 \
    --expected-country-codes \
      docs/plans/2026-07-12-exact-country-codes.json \
    --claim-policy "$2" \
    --simplify-tolerance "$3" \
    --output "/tmp/country-router-$1.db"
}

build_variant all-exact all-claims 0
build_variant territorial-exact territorial-primary 0
build_variant territorial-0025 territorial-primary 0.0025
build_variant territorial-005 territorial-primary 0.005
```

Run the curated query benchmark with the measured iteration counts:

```sh
for variant in all-exact territorial-exact territorial-0025 territorial-005; do
  python3 scripts/experiment_exact_country_router.py benchmark \
    --artifact "/tmp/country-router-$variant.db" \
    --queries docs/plans/2026-07-12-exact-country-queries.json \
    --iterations 20 \
    --open-iterations 20 \
    --fail-on-mismatch \
    --report "/tmp/country-router-$variant.benchmark.json"
done
```

Run both predicate-verified boundary diagnostics:

```sh
python3 scripts/experiment_exact_country_router.py compare \
  --oracle /tmp/country-router-all-exact.db \
  --boundary-source /tmp/country-router-territorial-exact.db \
  --candidate territorial-exact=/tmp/country-router-territorial-exact.db \
  --candidate territorial-0025=/tmp/country-router-territorial-0025.db \
  --candidate territorial-005=/tmp/country-router-territorial-005.db \
  --seed 20260712 \
  --global-points 5000 \
  --boundary-points 200 \
  --jitters 0.0001,0.001,0.0025 \
  --report /tmp/country-router-drift.json

python3 scripts/experiment_exact_country_router.py compare \
  --oracle /tmp/country-router-all-exact.db \
  --boundary-source /tmp/country-router-all-exact.db \
  --candidate territorial-exact=/tmp/country-router-territorial-exact.db \
  --candidate territorial-0025=/tmp/country-router-territorial-0025.db \
  --candidate territorial-005=/tmp/country-router-territorial-005.db \
  --seed 20260712 \
  --global-points 0 \
  --boundary-points 200 \
  --jitters 0.0001,0.001,0.0025 \
  --report /tmp/country-router-all-boundary-crosscheck.json
```

For a separately materialized area parquet plus an unjoined division parquet,
pass `--division-parquet`; the loader joins strictly by `division_id`, requires
one matching country parent, verifies the country code agrees, and retains the
parent perspective JSON. If a different extraction query produced the parquet,
pass its path with `--extraction-sql` so the correct SQL bytes are bound.

## Pinned-release measurements

The [committed compact evidence](2026-07-12-exact-country-evidence.json) records
environment, source, builder, schema, extraction, artifact, sidecar, benchmark
measurements, and corpus hashes. Its builder SHA is
`4e6e8916a394f5aceabcf982d36f695856bee34c8ebf603c64cb9d1daec9beb4`;
the extraction SQL SHA is
`bf0f5d830df3d91e60b495d434d336d0d580a61df4d5fe5462aa236b783d3d9d`.

All sizes below are measured SQLite bytes after moving source provenance to the
cold sidecar. The all-claims sidecar was 8,304,921 bytes; territorial variants
shared an 8,305,089-byte sidecar. Sidecars are audit data, not runtime data.

| Policy | Tolerance | Claims | Components | Unique WKB bytes | Hot SQLite bytes |
|---|---:|---:|---:|---:|---:|
| all claims | 0 exact | 378 | 71,866 | 162,449,408 | 183,095,296 |
| territorial primary | 0 exact | 219 | 1,255 | 89,092,710 | 90,083,328 |
| territorial primary | 0.0025° | 219 | 1,255 | 4,628,294 | 5,263,360 |
| territorial primary | 0.005° | 219 | 1,255 | 2,711,398 | 3,305,472 |

The 31-query curated seed set includes cities, Andorra's dual claim, Lesotho,
Vatican City, Kosovo and Liancourt synthetic ambiguity, both sides of Fiji near
the dateline, Hawaii, Boston offshore territorial routing, and two ocean misses.
All four artifacts returned the expected 27 routes, two synthetic fallbacks,
and two misses. The three territorial variants had no decision/country
difference from the all-claims exact oracle on this set. These seed cases were
curated for the experiment, not independently reviewed release labels.
Synthetic tests are implementation regression checks, not a correctness proof.

Local Python results from 20 iterations per query were:

| Variant | Post-verification SQLite open+metadata p50/p95 | Lookup p50/p95 | Fanout p50/p95 | RSS high-water delta |
|---|---:|---:|---:|---:|
| all exact | 2.256 / 2.659 ms | 220 / 734 µs | 2 / 6 | 84.19 MiB |
| territorial exact | 0.136 / 0.245 ms | 152 / 427 µs | 1 / 3 | 50.39 MiB |
| territorial 0.0025° | 0.145 / 0.252 ms | 66 / 128 µs | 1 / 3 | 2.89 MiB |
| territorial 0.005° | 0.136 / 0.290 ms | 62 / 104 µs | 1 / 3 | 1.78 MiB |

These are local CPython/SQLite/Shapely shape measurements. SQLite opens a local
file lazily, so the open measurement is not network fetch, Worker artifact
deserialization, isolate startup, or the mandatory manifest/artifact SHA-256
verification, which runs once before timed opens. It cannot be compared directly
to a cold verified-open gate. RSS is the subprocess's native process high-water
delta, including SQLite and Shapely allocations; it is not incremental
Worker/wasm heap. A Rust/wasm reader may have very different behavior.

### Exact-oracle drift diagnostic

The primary deterministic diagnostic used 5,000 global points, 200 stored
territorial polygon vertices that were predicate-verified as `boundary`, and
2,400 cardinal jitters. Its seed was `20260712`; all 200 boundary-source reasons
were `boundary`; and its corpus SHA-256 was
`c3a00bfe289d4c425eddc2c9a98806547032beea4b1dcec6eea05ea4defe1b88`.

| Candidate | Route-target drift | False unique | False negative | Wrong country |
|---|---:|---:|---:|---:|
| territorial exact | 0 | 0 | 0 | 0 |
| territorial 0.0025° | 407 | 64 | 198 | 145 |
| territorial 0.005° | 487 | 59 | 267 | 161 |

The 0.0025-degree candidate had no drift on the 5,000 global points, but changed
50 exact-boundary decisions and 357 jitters. The 0.005-degree candidate had one
global false negative, plus 49 exact-boundary and 437 jitter changes. Uniform
global sampling alone would have hidden nearly all dangerous simplification.

The complementary corpus used 200 predicate-verified all-claims boundary
vertices and 2,400 jitters; its SHA-256 was
`5bcce7ffa10b2ad94fc8ef6ef50127f215552326018ddb2d04e40d58d733128b`.

| Candidate | Route-target drift | False unique | False negative | Wrong country |
|---|---:|---:|---:|---:|
| territorial exact | 195 | 195 | 0 | 0 |
| territorial 0.0025° | 201 | 197 | 0 | 4 |
| territorial 0.005° | 210 | 197 | 6 | 7 |

All 195 territorial-exact changes were all-claims `boundary` to a country
route. The 2,400 nearby offsets had zero drift for territorial exact, isolating
the policy difference to excluded land boundaries. Under conservative boundary
semantics, `territorial-primary` is therefore rejected as an exact replacement,
not merely left unproven. These deterministic corpora still do not replace
independently reviewed real-world labels.

## Remaining gates

Before choosing any production path:

1. Ratify the byte, incremental-heap, cold-open, and warm-p95 gates. The proposed
   5 MiB numerator here means the exact bytes of the uncompressed hot SQLite
   runtime object; it excludes the cold audit and manifest. Any compressed R2 or
   complete published-object budget must be specified separately.
2. Require zero route-target drift—including zero false-unique, false-negative,
   and wrong-country decisions—on independently reviewed correctness labels.
   Diagnostic corpus drift must also be reported even when it is not a release
   gate. Every current candidate fails either bytes or correctness.
3. Build a research-only H3 or equivalent conservative-interior plus exact-full-
   boundary hybrid. Its boundary layer must include land and territorial claims
   and be compared to the all-claims oracle.
4. Only after a candidate passes correctness and byte gates, implement a bounded
   Rust/wasm reader probe for artifact fetch, decode/heap, cold first lookup,
   and warm p50/p95 in the real runtime shape.
5. Preserve `HEAD` fallback for a missing/hash-mismatched/corrupt artifact and
   for every ambiguous or policy-blocked decision.
