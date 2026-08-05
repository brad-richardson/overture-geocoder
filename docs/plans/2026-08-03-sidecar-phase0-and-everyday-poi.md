# Sidecar Phase 0 and the everyday-POI tripwire

Date: 2026-08-03

Status: the cross-release identity measurement, benchmark contracts, all 200
everyday-POI cases, and the first deterministic GERS-to-QID candidate generation
are complete. The cases are frozen from eight government sources before any
provider request, and the readiness report is green. The first three-provider
ranked baseline and companion current-OSM presence control are also complete.
The 200-decision match audit and accepted-mapping broadcast measurement remain
open. Nothing in this work changes a construction request, projection, serving
artifact, catalog, or Worker.

## Decision

Proceed with sidecar Phase 0, but treat accepted GERS-to-QID matches as durable
records with release-attested membership and exceptions. Do not regenerate the
entire match table each month, and do not trust an accepted GERS mapping forever
without checking the release changelog/delta.

GERS is the right durable default key. Overture explicitly intends GERS IDs to
remain stable across releases and maintains a registry, changelog, and bridge
files for that purpose. QID is the right external entity key: the independently
refreshed Wikidata fame table is keyed by QID, while an accepted association
connects that entity to a GERS ID.

The measured exception path is too large to omit. The full public bridge scan
in `benchmarks/2026-08-03-gers-place-stability-v1.json` joined the exact same
upstream `(dataset, record_id)` across Overture `2026-06-17.0` and
`2026-07-22.0` for all nine Places bridge datasets present in both releases.
Of 81,224,863 comparable persistent source records, 76,440,029 retained the
same GERS ID and 4,784,834 were assigned a different GERS ID: **94.109151%
stable, 5.890849% reassigned**. There were zero ambiguous source keys.

This is not an entity-survival or “GERS is only 94% stable” claim. One GERS
entity can contain multiple source records, and a source reassignment can
represent a legitimate merge, split, or conflation correction. The probe is a
strong operational lower-level signal: blindly carrying every GERS-to-QID row
without a release delta can attach external fame to the wrong current entity.

Other measured continuity facts:

- 81,224,863 of 87,845,947 old source keys survived into the new release
  (92.462846%); 6,621,084 were old-only and 7,934,058 were new-only.
- Comparable-record stability ranged from 89.154028% for AllThePlaces to
  95.655417% for PinMeTo. The largest source, `meta`, was 94.742701% stable.
- Krick regenerated every observed source record ID between these releases, so
  it had zero comparable keys. It must use another deterministic continuity
  route or enter the new-candidate path; this probe cannot measure its GERS
  continuity.

## Durable identity model

The sidecar has three logically separate artifacts.

1. A durable accepted-match ledger keyed by `gers_id`, containing `wikidata_qid`,
   decision, method, evidence, matcher version, first release, last validated
   release, and review status. Rejected and needs-review rows are retained.
2. A durable QID-keyed fame table, refreshed independently from Wikidata. Its
   refresh cadence does not force rematching unchanged GERS associations.
3. A release attestation keyed by `(overture_release, gers_id)` with present,
   added, removed, data-changed, or reassigned status and the exact registry,
   changelog, bridge, matcher, and source hashes used to derive it.

Monthly maintenance is differential:

- reuse accepted mappings for present, unchanged GERS IDs;
- revalidate data-changed IDs against the accepted evidence and send failures
  to review;
- candidate-match newly added IDs;
- retain but deactivate mappings for removed IDs; and
- process bridge reassignments as explicit merge/split candidates. A direct
  external identifier may be accepted automatically, but a fuzzy old-to-new
  transfer is never automatic.

This corrects the audit report's wording. The durable ledger is not recreated
per release. Only membership, deltas, and attestations are release-scoped.

## Phase 0 stop line

`benchmarks/gers-qid-sidecar-phase0-spec-v1.json` freezes the decision contract:

- at least 200 independently hand-checked candidate matches;
- zero false accepts in that set;
- automatic acceptance only from a direct external identifier;
- every fuzzy candidate reviewed;
- coordinates used only as a match-radius gate; and
- no unreviewed match may change prominence.

Zero false accepts in 200 checks is an operational tripwire, not proof of
99.9%-class precision. The candidate audit must deliberately include every
automatic-acceptance rule and retain false candidates and rejection reasons.
Phase 0 remains incomplete until that audit and the broadcast byte/resident
memory measurement exist.

### First deterministic candidate set, 2026-08-03

The first direct-ID rule is now implemented and measured without construction
movement. A frozen 1,000-binding Wikidata SPARQL snapshot of P1968 (Foursquare
City Guide venue ID) contains 978 QIDs and 985 direct claims. Joining those
claims to Overture release `2026-06-17.0`'s public Foursquare bridge produced
344 bridge rows, 343 GERS IDs, and 344 QIDs. The normalized public Places rows
and Wikidata entity rows are frozen and hash-bound by
`benchmarks/2026-08-03-sidecar-phase0-foursquare-collection-v1.json`.

`scripts/sidecar_phase0.py` produces 344 deterministic candidates. It
provisionally auto-accepts 342 unambiguous direct identifiers; one GERS ID maps
to two QIDs, so both candidates correctly require review. A source identity
assigned to multiple GERS IDs also fails into review by contract, although none
occurred in this snapshot. Multiple Wikidata coordinates are retained as source
evidence and never arbitrarily selected; coordinates do not decide a direct-ID
match.

The risk-first review queue freezes 200 decisions: both direct conflicts, all
21 missing-distance cases, all five over-1-km observations, 134 cases with no
normalized label overlap, and 50 clean direct controls. Risk flags overlap.
This is a review queue, not an audit result: all candidates remain
`eligible_for_prominence=false` until an independent verdict is bound to the
exact candidate-set hash. The audit still needs at least 200 checked decisions
with zero false provisional accepts, followed by the broadcast byte/resident
NumPy measurement.

Evidence:

- `benchmarks/sidecar-phase0-foursquare-places-v1.jsonl`;
- `benchmarks/sidecar-phase0-foursquare-wikidata-entities-v1.jsonl`;
- `benchmarks/2026-08-03-sidecar-phase0-foursquare-collection-v1.json`;
- `benchmarks/2026-08-03-sidecar-phase0-candidates-v1.json`; and
- `benchmarks/2026-08-03-sidecar-phase0-review-queue-v1.json`.

### Golden review instrument, 2026-08-04

The 200 decisions now have a review instrument. It decides nothing: every row is
still provisional, no verdict is recorded, and `eligible_for_prominence` is
false everywhere.

- `benchmarks/2026-08-04-sidecar-phase0-golden-review-set-v1.json` joins each
  queued decision to its evidence — GERS ID, Overture names/coordinate/country/
  source ids, QID, labels, coordinate candidates, the P1968 claim values with
  the snapshot and query hashes, the distance or an explicit null reason,
  normalized-label overlap, every risk flag with a plain-language reason, and
  the provisional decision with the exact rule text that produced it. It is
  bound to the candidate-set, queue, places, entities, collection, and spec
  hashes, carries a stable `decision_id` per row, and keeps the frozen
  risk-first order.
- `benchmarks/2026-08-04-sidecar-phase0-golden-verdicts-v1.json` is the empty,
  append-only verdict file (`accept` / `reject` / `needs_more_evidence`), bound
  to the sha256 of the review set so a verdict can never silently attach to
  changed inputs.
- `benchmarks/2026-08-04-sidecar-phase0-golden-review-sheet-v1.md` renders the
  same 200 decisions for reading, risk-first with the 50 clean controls last.
- `scripts/build_sidecar_phase0_golden_review.py` regenerates all three;
  `scripts/validate_sidecar_phase0_golden_review.py` checks binding integrity,
  reports coverage, and computes false accepts (provisionally accepted
  decisions the reviewer rejected). It fails closed: incomplete review is
  reported as gate-not-met, never as passed.

Four evidence fields are absent from the frozen inputs and cannot be added
without breaking the frozen hashes: Overture categories, Wikidata descriptions,
Wikidata aliases, and P1968 statement GUIDs. Each row states this explicitly and
carries the wikidata.org and Foursquare URLs to check by hand.

Any accepted sidecar that changes `prominence_rank` moves projection identity
and construction evidence. v4 is already consumed by the in-flight phrase
admission build, so the first possible construction generation is **v5**.

## Everyday-POI tripwire

The fast-loop contract is
`benchmarks/everyday-poi-tripwire-spec-v1.json`; the source acquisition plan is
`benchmarks/everyday-poi-source-plan-v1.json`. The plan fills exactly 200 cases
from eight government sources, all outside Europe and North America:

| region | countries | cases |
|---|---|---:|
| East Asia | Taiwan, Japan, South Korea, Hong Kong | 100 |
| Southeast Asia | Singapore | 20 |
| Latin America | Mexico, Colombia | 55 |
| Oceania | Australia | 25 |

The planned family totals are civic/transit 20, food/drink 30, healthcare 90,
lodging 20, and retail 40. One hundred cases use non-Latin source names. No
country contributes more than 35 cases.

Collection batch 1 freezes 20 distinct Singapore rail stations and 30 active
named restaurants in Tainan's Zhongxi, East, North, South, and Anping districts.
The Taiwan pivot is evidence-driven: the downloaded 2026-08-03 national
snapshot contains no Taipei records, while those five source-coded Tainan core
districts contain 237 active named points (235 after excluding both rows in one
duplicate-name group). The Singapore feed contains 613 exits collapsing to 190
stations. Selection made zero compared-provider requests and retained the exact
dataset and licence bytes under `benchmarks/everyday-poi-source-data-v1/`, with
their hashes in `benchmarks/everyday-poi-source-snapshots-v1.json`.

Collection batch 2 adds 20 Hong Kong Hospital Authority facilities and 20
active Seoul hospitals. The Hong Kong CSDI GeoJSON contains 44 bilingual WGS84
points with stable `OBJECTID` values; selection uses the Traditional Chinese
name and retains the official English name as an accepted alias. Seoul's
official 1,000-row preview contains the complete 929-row licensing dataset:
555 rows report both active trade status and `영업중`, eight of those lack
coordinates, and excluding both sides of ten duplicate-name pairs leaves 527
eligible records. Its data-only JavaScript response is parsed without `eval`.
The official EPSG:5174 coordinates are converted with pinned pyproj 3.7.2 /
PROJ 9.5.1 using the named EPSG operation at 1 m stated accuracy; original
coordinates and the complete transformation contract are retained in every
Seoul case. Batch 2 also made zero compared-provider requests.

Collection batch 3 adds 20 public healthcare facilities from Bogotá and 25
physical-retail establishments from Melbourne. The Bogotá district layer has
117 unique stable `ID` and `OBJECTID` values, 116 valid points, and 114 points
inside the frozen dense-city bounds; hash selection admits 20 distinct official
names. Melbourne's 2024 snapshot has 19,672 stable Opendatasoft `recordid`
values. ANZSIC divisions 39-42 yield 1,852 named physical-retail candidates and
1,286 after excluding duplicate official names. Review rejected one top-ranked
storage-only record before filling the 25-case quota. Exact dataset, catalog,
ArcGIS-layer, and CC BY 4.0 evidence bytes are retained. Batch 3 also made zero
compared-provider requests.

Collection batch 4 completes the set with 30 Shinjuku healthcare facilities and
35 Ciudad de México establishments: 20 lodging and 15 retail. The planned 2020
MLIT medical source was rejected because its own catalog says it includes
suspended facilities. The replacement Shinjuku municipal-standard dataset was
updated 2025-12-12 and has 695 unique stable `ID` values and valid points. It
provides 673 unique named Japanese-script candidates; one temporary vaccination
site is retained as a reviewed rejection. The corrected DENUE 05/2026 snapshot
has 462,732 unique `id` and `CLEE` values. The collector retains the exact bulk
ZIP, free-use terms, methodology, and May 2026 correction notice; requires
fixed establishments and official SCIAN 721 lodging or 46 retail activities;
and excludes generic, duplicate, or out-of-bounds records. Batch 4 also made
zero compared-provider requests.

The frozen set is now complete: 200 cases, eight countries, four macroregions,
five POI families, 100 non-Latin cases, and 200 government-source cases. Every
frozen minimum passes and `everyday-poi-tripwire-readiness-v1.json` is
`ready=true` with no blockers.

The sources are official spatial restaurant, transit, medical-facility,
business, and health-network datasets. Selection is deterministic from frozen
source bytes and stable source record IDs before any compared-provider request.
The gold carries no expected GERS ID. If later source inspection finds any
OSM-derived rows, the validator forces those cases to Overture-only and excludes
Nominatim and Photon before spending a request.

`scripts/validate_everyday_poi_tripwire.py` enforces the case schema,
provenance, licensing fields, coordinates, provider eligibility, and all frozen
coverage gates. `scripts/benchmark_v2_forward.py` now honors each case's
`comparison_providers`; excluded providers are marked unscorable before a
network request and cannot enter paired comparisons.

The first full provider-neutral run is frozen in
`benchmarks/2026-08-03-everyday-poi-external-baseline-v1.json`. It measured the
live Overture `2026-08-02.0` build and the public Nominatim and Photon endpoints
with all 200 cases and the same semantic scoring contract. All 600 eligible
requests returned HTTP 200 with zero harness errors:

| provider | recall@1 | recall@10 | p50 | p95 |
| --- | ---: | ---: | ---: | ---: |
| Overture | 31.0% | 32.5% | 292.0 ms | 926.5 ms |
| Nominatim | 10.5% | 11.0% | 345.8 ms | 731.2 ms |
| Photon | 10.0% | 11.5% | 161.0 ms | 454.7 ms |

Overture recall@10 was 73.3% for food/drink and 40.0% for healthcare, but 25.0%
for civic/transit, 5.0% for retail, and 0% for lodging. Its macroregion split was
58.0% East Asia, 25.0% Southeast Asia, 8.0% Oceania, and 0% Latin America. The
paired outcomes were 54 Overture-only hits, 11 hits shared by all providers, 13
external-only hits, and 122 shared misses. Overture returned no POI anywhere in
the top ten for 114 cases. That makes admission/routing coverage the leading
diagnostic, while the identical post-v4-preview rerun remains the next measured
gate.

The companion OSM-presence control is frozen in
`benchmarks/2026-08-03-everyday-poi-overpass-presence-v1.json`. Report it beside
the three ranked providers as a fourth **control**, not as a fourth geocoder:
Overpass queries nearby OSM nodes, ways, and relations and does no comparable
free-text ranking. Within 500 m of the independent authority coordinate, 46 of
200 cases have an exact normalized accepted name in current OSM and 12 have a
fuzzy name candidate requiring review. Another 134 have a named same-family
feature nearby without an accepted-name match; those are density evidence, not
proof that the gold entity exists in OSM. Eight have no named same-family
candidate.

The control materially changes failure attribution. All 22 Nominatim hits and
22 of 23 Photon hits coincide with exact current OSM names, but each provider
also misses 24 cases where the exact name is present. Those 24 are direct
search/index/query-surface failures rather than OSM-absence explanations.
Overture hits 37 cases without an exact current OSM name match, evidence that
its useful coverage is not limited to this current OSM representation. The
artifact retains OSM element IDs, relevant tags, distances, OSM base timestamps,
query hashes, authority record IDs, and the cross-tab against the frozen ranked
baseline. Fuzzy candidates remain unaccepted until manual review.

The 200-case set is the development tripwire. It does not replace the audit's
recommended 1,200-case decision set with at least 150 cases per reportable
stratum. Build the larger set only after the tripwire exposes which strata are
actually decision-relevant.

## Roads

Do not start the transportation street layer yet. The current measured gate is
the non-promoting v4 Places planet build followed by preview measurement. The
street layer is still Stage 4 and the repository's one-way-door format change;
its seven offline experiments and permanent decoder commitment remain valid,
but neither is the shortest path to the current milestone.

Sidecar Phase 0 and benchmark collection are safe parallel work because they
are local, non-promoting, and do not move construction identity. After v4
preview, use the everyday-POI result and the 200-match sidecar audit to choose
between v5 fame work, more Worker routing/alias work, or the street layer.

## Reproduction

The bridge scan is public-data-only:

```bash
python scripts/benchmark_gers_stability.py \
  --old-release 2026-06-17.0 \
  --new-release 2026-07-22.0 \
  --work-dir /tmp/gers-stability-phase0 \
  --output benchmarks/2026-08-03-gers-place-stability-v1.json \
  --threads 8 \
  --memory-limit 8GB
```

Validate a collected tripwire before measurement:

```bash
python scripts/validate_everyday_poi_tripwire.py \
  --cases benchmarks/everyday-poi-tripwire-cases-v1.json \
  --output benchmarks/everyday-poi-tripwire-readiness-v1.json
```

Reproduce the current-OSM presence control with ten sequential, rate-limited
Overpass queries:

```bash
python scripts/audit_everyday_poi_overpass.py \
  --cases benchmarks/everyday-poi-tripwire-cases-v1.json \
  --baseline benchmarks/2026-08-03-everyday-poi-external-baseline-v1.json \
  --output benchmarks/2026-08-03-everyday-poi-overpass-presence-v1.json
```

During collection, `--allow-incomplete` may write a readiness report, but it
does not weaken or mark any frozen gate as passed.

Reproduce the current collection from the frozen local source bytes. Seoul's
coordinate transformation is intentionally version-pinned:

```bash
python3 -m venv /tmp/everyday-poi-pyproj-venv
/tmp/everyday-poi-pyproj-venv/bin/pip install pyproj==3.7.2
/tmp/everyday-poi-pyproj-venv/bin/python scripts/collect_everyday_poi.py \
  --output benchmarks/everyday-poi-tripwire-cases-v1.json \
  --report benchmarks/everyday-poi-selection-report-v1.json
```

---

# CLOSED 2026-08-05: the sidecar half of this plan is a dead end

The sidecar decision recorded above ("Proceed with sidecar Phase 0") is
**withdrawn for the places theme**. The everyday-POI tripwire half of this
document stands.

Planet-wide there are **7,234** Wikidata items carrying `P1968`, against
75,642,289 Overture places — 0.0096%. The candidate generation here used a
`LIMIT 1000` SPARQL query joined to the Foursquare bridge, so the 200-decision
audit was sizing a path whose entire ceiling is 7,234 entities.

The durable identity model above (GERS ledger, QID fame table, release-attested
membership) is not wrong, and the GERS stability measurement it rests on stays
valid. It is simply aimed at a route that cannot carry enough entities to matter.
`theme=base` carries `wikidata` natively on `infrastructure`, `land_use` and
`land`, which is where the landmark fame this was meant to supply actually lives.

The 200-decision audit is stopped at 57/200 and is not resumed. See
`docs/plans/2026-08-05-sidecar-p1968-dead-end.md`; the state doc carries the
summary and supersedes this file on the point.
