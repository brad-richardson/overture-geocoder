# Sidecar Phase 0 and the everyday-POI tripwire

Date: 2026-08-03

Status: the cross-release identity measurement and the benchmark contracts are
complete. The first 90 of 200 everyday-POI cases are frozen from Singapore,
Taiwan, Hong Kong, and South Korea, before any provider request. GERS-to-QID
candidate generation, the 200-decision match audit, and the remaining 110
benchmark cases are not yet
built. Nothing in this work changes a construction request, projection, serving
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

The frozen set is now 90 cases across four countries and three POI families,
including 70 non-Latin cases. Readiness remains correctly red: 110 cases, two
macroregions, four countries, two POI families, ten non-Latin cases, and 70
government/open-primary cases remain to reach the v1 gates.

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
