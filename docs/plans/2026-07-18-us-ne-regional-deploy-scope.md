# US-Northeast regional address + Places deploy — scope

Date: 2026-07-18. Owner decision inputs: promote address and Places together;
region cut is a rough bounding box with parquet predicate pushdown doing the
scan pruning; both families should be built and rehearsed by the July 25
scheduled rebuild; the primary client (the Overture Maps explore page) must
not change behavior.

## 1. Region definition

One rough US-Northeast box, DC-through-Maine:

```
lon: -80.5 .. -66.9    lat: 38.0 .. 47.5
```

Rough is fine: the box exists to bound the source scan, not to draw a
coverage promise. Both Overture source themes carry a `bbox` struct column
with row-group statistics, so a `WHERE bbox.xmin BETWEEN ... AND bbox.ymin
BETWEEN ...` predicate prunes at the row-group level via DuckDB over S3.
The family manifest records the box (section 4a) so an out-of-box query is
classified out-of-coverage, never not-found.

- **Places**: bbox extraction is already the native mechanism —
  `experiment_places_partition_extract.py` takes `--xmin/--xmax/--ymin/--ymax`,
  applies the pushdown predicate, and has a `--count-only` sizing mode. The
  smoke fixtures (Boston/Tokyo/Mexico City) are exactly this pattern at
  0.4-degree scale. Measured against release 2026-06-17.0 (`--count-only`,
  2026-07-18): full NE box **4,133,950 places**; metro boxes boston 173,095 /
  nyc 539,642 / philadelphia 149,585 / dc 151,187 (~1.01M combined). At the
  Latin-script coefficient ~116.4 B/place (Boston-measured) the full box is
  ~481 MB — 3-4 shards at the 1.5M-row/256 MB cap — and the metros-only cut
  is ~118 MB. Decision: build the **full NE box** (it is the meaningful
  region, the cost is trivial, and rural coverage differentiates the deploy
  from a metros-only demo); the four metro boxes remain natural
  routing-catalog context entries if the shard split follows metro lines.
- **Address**: the pushdown pattern is proven in-repo
  (`download_addresses.sql` filters `bbox.*` and even `address_levels`
  state values) but the family producer that feeds R2
  (`experiment_hosted_rowgroups.py`) reads whole row groups by contiguous
  index with no spatial predicate, and `inventory_address_rowgroups.py`
  records only `country` row-group stats. A bbox-scoped address build needs
  one of:
  1. a DuckDB bbox extract producer (cheap, real S3 pruning, but bypasses
     the map/reduce resume machinery the finalizer work hardens), or
  2. a bbox-aware inventory (record per-row-group bbox min/max, prune the
     task plan to NE-intersecting groups — keeps map/reduce, net-new
     inventory code).
  Decision: start with (2) — the point of the exercise is to rehearse the
  pipeline that will run at planet scale, and the inventory change is
  reusable. (1) remains the fallback if (2) overruns.
  All-US upper bound if pruning slips: 33 tasks, ~131M rows, ~2.5-3 h wall,
  ~17 GB fragments — feasible but wasteful; the NE box should cut this
  substantially (sizing requires a fresh bbox scan; the current inventory
  cannot answer it).

## 2. Compatibility contract (what keeps the explorer safe)

Verified against the deployed surface:

- `/search` already has an undocumented `types=` param; default is the seven
  division types and `place` is deliberately excluded (pinned by test).
  The Places forward path (`types=place` + `-places` shards present in the
  collection) is already plumbed. **Disabled-by-default = param opt-in AND
  catalog presence.**
- The explore page sends only `q`/`limit`, `lat`/`lon`, and `/id/{id}` via
  the published JS client. It never sends `types` and never enumerates
  catalog collections. Unknown query params are ignored server-side; the
  catalog parser ignores unknown entries; the Worker fetches only fixed
  collection keys per version. **Family publication is additive by
  construction; the explorer cannot observe it.**
- `/id` already spans all 15 feature types including address and place.
- Structured address gets a NEW `/address` route (8-field exact lookup with
  its own response schema, modeled on the existing spike route shape); it
  cannot share `/search`'s single-`q` contract. Free-form located Places
  serving, when promoted, mirrors the `/__places-page-spike` param shapes on
  a `/places` route; `types=place` on `/search` stays the shared-surface
  opt-in.
- Hygiene before promotion: document `types=` in the README; keep new-family
  collections optional in `check_health` until builds reliably produce them
  (a mandatory check could 503 and mask versions).

## 3. What the July 25 date does and does not mean

The July 25 scheduled rebuild remains division-only and is not gated on any
of this work. "Ready by the 25th" = both families built from the NE box,
verified, and rehearsed non-promoting, with the scale report — evidence,
not user-visible serving. Promotion is a separate, opt-in-gated switch that
can flip whenever its gates pass, without a rebuild, precisely because of
the section 2 contract.

## 4. Work breakdown (tracks parallel; sizes S/M/L; ~6 working days)

**Track A — dry-run + Places build (low risk)**
1. (S, in flight) Green `promote=false` rebuild dry-run — validates the
   #96/#97 finalize path post-#104. Run 29624600543.
2. (S-M) US-NE Places region set: `--count-only` the candidate boxes, build
   `.pcsh` shards + `catalog.pcat` + packed head, add NE contexts to the
   Places smoke matrix. Proven pattern.

**Track B — address bbox producer (medium risk)**
3. (M) bbox-aware row-group inventory (per-group bbox stats + NE-pruned task
   plan); fallback: DuckDB bbox extract producer.
4. (M, dep 3) US-NE address build through the rehearse-address map/reduce
   machinery (upload / verified resume / restore / stale-repair / cleanup)
   with the scale report.

**Track C — shared finalizer integration (long pole, at-risk)**
5. (M) Family manifest schema in `global_build_manifest.py`: immutable object
   identities, lineage, format/tokenizer/normalization versions, **bbox
   scope**. `promotion_eligible: false` is already the fan-in default.
6. (M-L, at-risk) Object-level publication guard: create-only
   (`If-None-Match`) and expected-current (`If-Match`) conditional writes in
   the finalize R2 path; today the only serialization is the workflow
   concurrency group and a read-back byte compare.
7. (L, dep 5, at-risk) Optional non-promoting families in the shared
   finalizer: either teach `verify_release`'s exact-set gate about optional
   families or stand up a parallel non-promoting finalize path driven by
   `global_build_manifest fan-in` (never invoked in CI today).
8. (L, dep 2+4+5+7, at-risk) The two-region-per-family slice through the real
   `releases/{version}/` layout with downloaded-hash remote verification.

**Serving gaps — none block the rehearsal; all gate promotion**
- 941 KB address cold side-index: promotion cost/latency decision.
- Places bounded located ranking: promotion blocker for any near-me claim.
- chain_name read-chain: structural (2 uncounted catalog reads; 8 sequential
  stage reads precede records; global-rank layout cannot cluster a chain).
  Hardest open serving item; needs a read-chain design iteration plus a
  re-derived gate budget.
- Places comparator relevance panel (vs Nominatim et al.): not started;
  it is the launch/stop arbiter for Places serving.

## 5. Fallback ladder (decide at ~July 22)

1. **Full**: Tracks A+B+C land; both families flow through the real
   `releases/{version}/` layout, non-promoting, with remote verification.
2. **Baseline (planned floor)**: A+B land; families rehearse through the
   existing isolated-prefix machinery with NE inputs and the scale report;
   C items 6-8 carry into the next cycle with documented limitations.
3. **Address-first promotion**: if promotion is pulled forward, address goes
   first (no relevance/ranking/latency debt); Places data publishes unrouted
   behind its two gates until its promotion gates pass.

## 6. Promotion decision (deferred, gates explicit)

Promoting both families together is compatible with the section 2 contract
at any time after:
- address: side-index decision made; `/address` route reviewed; family
  manifest + publication guard in place.
- Places: chain_name read-chain iteration lands and the routed cold gates
  pass; bounded located ranking complete or near-me explicitly not claimed;
  comparator relevance panel adjudicated `proceed`.
- both: green non-promoting slice through `releases/{version}/`, family-aware
  production smoke, rollback behavior defined.

Realistic promotion window: after the July 25 rebuild, targeting the August
cycle, as a catalog+deploy switch rather than a rebuild event.
