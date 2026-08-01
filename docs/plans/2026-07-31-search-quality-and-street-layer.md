# Search quality and a street layer: findings, cost, and sequencing

Date: 2026-07-31. Status: **review draft, no implementation.**

Scope: four parallel reviews commissioned after the `2026-07-31.0` promotion —
(1) cost of a transportation-derived street layer, (2) root cause of free-text
global search failures, (3) cross-type result ranking, (4) platform limits that
bound both. Plus two operator questions answered inline: an offline one-off
build keyed by stable GERS IDs, and whether other Cloudflare services help.

Guiding constraints, from the operator: **slow builds are acceptable**; it must
run on **free GitHub Actions runners**; it must work with the **R2 + Cloudflare
Worker** setup; and it should **scale down to near-zero cost when unused**.

Every claim below is either MEASURED (cited to `file:line` or a live probe run
on 2026-07-31 against `https://geocoder.bradr.dev`) or explicitly ESTIMATED.

---

## Part 1 — The headline: search is badly broken, and it is not a capacity problem

Three live probes, all against the build promoted today:

```
q=Seattle&limit=10   -> 10 POIs, ALL relevance 1.0:
                        The UPS Store, Verizon, KeyBank, Comfort Inn,
                        Rooster Apartments, ... Bank Of America
                        The CITY OF SEATTLE DOES NOT APPEAR.
q=Seattle&types=locality -> Seattle, WA   relevance 0.8408

q=paris&limit=5      -> Dessirier, Galeries du Diamant, Rexel, New Jawad, Midas
                        PARIS THE CITY IS ABSENT FROM ITS OWN QUERY.

q=eiffel&types=poi   -> Hotel Eiffel Blomet, Hôtel Eiffel Trocadéro,
                        Pasticceria La Tour Eiffel, Sofitel Paris Baltimore
                        Tour Eiffel, Eiffel Tower Diner
                        The Eiffel Tower is not among them.

q=Eiffel Tower       -> 0 features
q=Space Needle       -> 0 features
q=Statue of Liberty  -> 0 features
q=IKEA               -> 3 features (worldwide brands DO work)
q=IKEA Berlin        -> 0 features, and does not even route
```

### The mechanism, confirmed in source

**RC1 — the global head keeps the wrong ten.** Every distinct token gets a head
entry capped at the top **10** by `(confidence_rank DESC, feature_id ASC)`
(`scripts/places_construction_v1.py:368` `head_result_cap = 10`, `HEAD_ORDER`
at `:92-94`; worker `HEAD_RESULT_CAP` at
`crates/geocoder-worker/src/places_construction_v1.rs:69`). `confidence_rank`
is `round(overture_confidence * 255)` — a **u8 that saturates**
(`crates/geocoder-construction/src/bin/places_transform_v1.rs:481`). Enormous
numbers of Overture places tie at 255, so the tie-break `feature_id ASC` on a
random UUID means a head entry is, in practice, *the ten
UUID-lexicographically-smallest confidence-1.0 records for that token*. There is
**no admission gate at all**.

**RC2 — two-token queries intersect two ten-deep lists.** Each token fetches its
head entry, then `intersect_ranked` ANDs by feature id (`v2.rs:1953-1971`,
`places_construction_v1.rs:1093-1103`). `top10("eiffel") ∩ top10("tower") = ∅`.
This is exactly failure mode 2 of
`docs/plans/2026-07-17-famous-unique-head-admission.md:22-30`. Its fix — `e2:`
pair keys plus famous-set admission — was implemented **only in the legacy spike
reader** (`places_pages.rs:909-921`) and **never ported** to the live
`PLHD0002` format. Zero occurrences of `e2` in the construction pipeline.

**RC3 — three-token queries never touch the head.** The no-proximity lane
returns empty for `tokens.len() > 2` (`v2.rs:1946-1948`). `Statue of Liberty` is
structurally unanswerable context-free.

**RC4 — postings are polluted and the field is ignored.** Tokens are emitted
from primary/common names (mask 1), brand (2), category (4), **and
locality/region/country context (8)** into one list
(`places_transform_v1.rs:449-475`). The `field_mask` is stored in every record
and decoded by the worker (`places_construction_v1.rs:144, 600, 624`) but
**never consulted when ranking**. A POI whose only relation to "paris" is being
*located* there ranks identically to one *named* Paris.

**RC5 — the division/POI seam.** Division score is `importance / 2` clamped to
[0,1], capping near 0.9 (`v2.rs:1646-1647`); POI score is raw `confidence`,
which is 1.0 for the saturated class (`v2.rs:1667`). The merge is a raw score
sort (`v2.rs:2320`) and each side pre-truncates to `limit` before a second
truncation after merge (`v2.rs:2327`). **Any confidence-1.0 POI buries every
division**, and ten of them starve it out entirely.

**RC6 — `IKEA Berlin` returns 0** because locality-suffix inference gates on
`(3..=4).contains(&tokens.len())` (`v2.rs:1709`, PR #214). Two tokens get no
routing, then die in RC2.

### Why brands work and landmarks do not

Not a hypothesis — a mechanism. A brand token's posting list is **homogeneous**:
nearly every record is a correct answer, so whichever ten survive the arbitrary
tie-break are all IKEA stores. A landmark token's list is **heterogeneous**, and
the correct answer holds no ranking advantage, because **the Places pipeline has
no fame signal in its inputs at all**. The projection
(`scripts/project_places_construction_v1.py:151-180`) carries
id/names/brand/category/address-context/confidence/geometry — **no wikidata, no
website, no source counts**. Fame ≠ Overture existence-confidence.

`Colosseum` returns a Nordic dental chain because "Colosseum Dental" matches on
the brand mask at confidence 1.0, and the Rome Colosseum (primary name
"Colosseo") loses the tie.

### The P0-P6 audit: divisions got the research, Places got none of it

`docs/ranking-research.md` is the original 2026-06-11 comparison against
Nominatim, Photon, Pelias and Placeholder. Verified against source, **P0-P5 all
landed — for divisions only**: Porter stemmer dropped
(`scripts/build_shards.py:73-75`), Wikidata importance (`:157-172`), type prior
with dampened population, BM25-recall plus deterministic rerank
(`crates/geocoder-core/src/query/mod.rs:15-23`), weighted three-column FTS
`bm25(divisions_fts, 4.0, 2.0, 1.0)` (`:52`), synonyms/designations (`:78-108`).
P6 (relative cutoff, tie-breakers) never landed.

**The Places lane has none of this.** That asymmetry *is* the bug. The division
scoring is already the right shape; the work is extending it, not inventing it.

Correction to a claim in `ranking-research.md`: `class` and `wikidata` are no
longer "downloaded then discarded" for divisions — both now drive `importance`.
For Places they are genuinely absent from the projection.

---

## Part 2 — Cross-type ranking

### What happens today

Divisions and POIs are queried independently, each truncated to `limit`, mapped
to a score on **unrelated scales**, concatenated, sorted, deduped, and truncated
again. There is no shared scale and no cross-type normalization.

| | Divisions | POIs |
|---|---|---|
| Formula | `(match_quality + 0.5·importance + 0.2·bm25 + bias) / 2` | `confidence_rank / 255` |
| Text relevance | dominant term | **absent entirely** |
| Distribution at top | exact match ≈ 0.7-0.95 | mass at exactly 1.0 |

### Proposal (design, not current behavior)

One score for every family: **`S = Q + 0.5·P + B`** — `Q` match quality (query
time, 0-1), `P` precomputed static prior (build time, 0-1), `B` location bias
(≤0.3, existing `bias.rs` shape). Serialize `S/2` clamped, as divisions already
do. Ties break by feature-type rank (coarser wins — Photon `searchPrio` shape:
city > street/country > county/state), then population/bbox, then distance, then
id for determinism.

- **Build time:** replace the raw POI confidence byte with a dampened prior
  capped near 0.08, so it occupies the same "static prior" band divisions use and
  can never outvote a match-quality step. (This originally read "near 0.4", which
  is **incompatible with the sentence that follows it**: the closest rungs of the
  `match_quality` ladder are 0.05 apart, and a 0.4 cap contributes up to 0.2 to a
  halved score — four rungs. `POI_PRIOR_CAP = 0.08` is the largest value that
  cannot outvote one rung; it was derived from a failing test, see Part 6c.)
  Reorder the per-`(cell, token)`
  256-cap eviction to `(name-or-brand bit) DESC, confidence DESC` — otherwise
  query-time reranking cannot recover what the cap already discarded.
- **Query time:** compute `Q` for POIs with the *existing* `geocoder-core`
  match-quality ladder against primary/brand name, and use the already-decoded
  `field_mask` to zero the contribution of context-only matches. Pure string
  work over ≤ `limit` candidates. **No extra R2 reads, no rebuild.**

Do **not** copy Nominatim's rank table: its county-above-city ordering is an
address hierarchy and would recreate the known county-beats-city problem
(`ranking-research.md:107`).

**ID lookup:** detect UUID-shaped `q` and short-circuit to the exact ID path,
saving the text indexes' R2 subrequests. Do not merge it into ranked results —
the ID index deliberately returns no names, so an injected feature would be
nameless and unrankable. Low priority.

---

## Part 3 — The street layer

### Two sources, and the honest gap between them

**Transportation-derived** (`docs/plans/2026-07-11-address-street-experiments.md`
§3): connect named road segments through connector IDs into connected
components, splitting a repeated name into components rather than one global
centroid. Correct, and gated behind 7 acceptance experiments, **none of which
have been run** — local transportation artifacts are currently empty.

**Address-derived** (`scripts/experiment_address_division_index.py:336-357`
already builds a `street_dim`): cheap, reuses published data. Measured cost on
the Boston locality — **75.29% cluster coverage** (4,391 of 5,832), and **1,215
of the 4,391 covered clusters are ambiguous repeated-name matches**. Loses
highways, ramps, unaddressed and rural roads entirely; gives an address centroid
rather than road extent. The design doc says outright that address-derived
counts "must not be presented as a replacement."

**Verdict:** transportation is the authoritative layer; address-derived is at
best an interim recall aid, and shipping it as the answer would bake in a 25%
hole.

### The finding that makes this feasible

Under the documented **primary-name** topology rule, two segments can only join
a component if they share a normalized primary name **and** a connector. So
`hash(normalized_primary_name)` is an **exact reduce partition key with zero
boundary problem** — every component lives entirely inside one bucket. Each
reducer runs a trivial per-name union-find. Disconnected same-name roads fall
out naturally, which is the whole point.

This is the same shape as the already-proven Address hash-bucket build. Spatial
partitioning would *not* have this property (measured: 0.48% of core named
segments change under a 0.005° halo), so **name-hash is the choice**.

**The one change that breaks it:** merging components across aliases or name
changes ("Broadway" continuing as "Broad St"). That converts an embarrassingly
parallel per-name union-find into multi-round distributed connected components —
the only version with a real >16 GB failure mode. Do not promise it in v1.

### Sizing

MEASURED Boston: 65,176 road segments, 22,385 named (34.3%); 4,864 primary names
→ 5,832 clusters (≈3.8 named segments/cluster); cluster size p50/p90/p99/max =
1/7/30/161.

ESTIMATED global: ~60-110M named segments → **~16-30M street components**,
publishing **~2-10 GiB** — smaller than either existing family (Places reverse
7.82 GiB, Addresses reverse 21.85 GiB). Per-object sizes land 1-3 MB against a
2 GiB cap. Object count in the low thousands against 42,058 already promoted.
**Fits comfortably.** Carries Boston urban bias in both directions.

### A new family costs real surface, but is contractually supported

Families are hardcoded in at least seven scripts (`global_build_manifest.py:23`,
`construction_v1_hosted.py:202`, `construction_v1_control.py:25`,
`construction_staging_v1.py:93`, `build_slice_inventory_v1.py:62`,
`promote_construction_slice.py:107`, `reverse_shard_v1.py:58`), and the Worker
rejects any other name (`v2.rs:435`). First publication creates a **permanent
decode obligation across 64 retained releases**.

Mitigation: the catalog contract explicitly supports a new family arriving via a
**non-promoting families-only slice**
(`docs/v2-release-catalog-contract.md:42-51`), so streets can be built and
promoted **without touching the live Places/Addresses artifacts**.

**Do not** instead inject streets into the Places family: the worker hardcodes
`"feature_type": "poi"` (`v2.rs:1676, 1822`), it would break `types=` filtering,
street tokens would compete with POIs in the same head entry, and it re-opens
frozen Places head sizing evidence.

---

## Part 4 — Operator question: an offline one-off build keyed by stable GERS IDs

**Recommendation: reject as proposed; adopt its useful half.**

**The precedent is real but is not analogous in scale.**
`scripts/places_partition_plan_v1.json` is **2,422 bytes** — a routing tree
derived offline from a 286M-term-row local run. Two properties make it
admissible: the hosted build **re-verifies it on every use** ("assigns
partitions locally from the committed result and only verifies, per partition,
that no cap was breached"), and it ships a **byte-for-byte reproduction proof**
(`--check`). It is a small frozen *contract*, not serving data. Nothing in this
repo has ever committed or hand-published serving bytes.

**The stability premise is weaker than it appears.** The repo's own street
experiment labels its output "release-scoped snapshot clusters, **not** stable
real-world street IDs"
(`2026-07-11-transportation-components-experiment.md:28`). Component membership
is a function of the whole graph: a segment can keep its GERS ID and still
change components because a neighbour was re-split or renamed. The diff unit is
the component closure, not the row. No numeric churn rate for transportation
exists in the repo — "small fraction" is qualitative and was measured over the
whole GERS registry.

**Precedent for not doing it:** incremental ID-index rebuilds were designed and
**deliberately deferred** because the diff "buys a real correctness surface …
a missed class of change silently serves stale bboxes forever", versus "rebuild
everything from source — simple to trust"
(`docs/plans/2026-07-02-future-work.md:22-27`). A component map has a *worse*
diff surface than the ID index.

**Size:** ESTIMATED 3-12 GB for a segment→component map plus metadata. Not
committable (GitHub's 100 MB file cap, by 1.5-2 orders of magnitude); it would
have to be R2 regardless.

**And the compute problem it solves may not exist** — name-hash partitioning
already makes the hosted build embarrassingly parallel, and free runners already
stream a same-order theme (Places, 63.5 GB) green.

**The useful half:** use the local high-core machine where the repo already does
— MEASURED ~50 min local planet Places map vs 248 min hosted — for the bounded
metro acceptance experiments, and to derive a small committed **partition plan**
in the exact `places_partition_plan_v1.json` mould, re-verified per use.

---

## Part 5 — Platform limits that bound every option

| Limit | Value | Class |
|---|---|---|
| Worker CPU / request | 500 ms | `wrangler.toml:14` |
| Isolate memory | 128 MiB, ~72 MiB pre-budgeted → **~56 MiB working room** | `stac/cache.rs:60-75` |
| Concurrent in-flight R2 reads | **4** | `range_reader.rs:25` |
| Posting bytes per query | 16 MiB; 4 clauses; 10 tokens; 200 chars | `places_pages.rs:20-63`, `handlers.rs:13-15` |
| Address serving object | 2 GiB | `address_construction_v1.rs:63` |
| Single PUT | 5 GB; **multipart fails closed** (ETag ≠ MD5) | `construction_v1_remote.py:70-74`, `r2_verified_store.py:191-207` |
| Read-path codecs | **Snappy + gzip only.** No zstd, no brotli | `geocoder-worker/Cargo.toml:28-31` |
| Retained releases | 64 → permanent decode obligation | `v2.rs:39` |
| Runner | 4 vCPU, 16 GB RAM, 14 GB SSD; 256-job matrix; 6 h ceiling | platform, enforced in code |
| Frozen | address partition key, `MAXIMUM_HASH_BITS=16`, format magics, quadkey scheme | CLAUDE.md §4, one-way-doors |
| New request identity | touching producers, `Cargo.lock`, or canonical caps re-buys the planet build | wall-clock review Wave 0 |

**What these rule out:** one big street index object; whole-shard hydration or
in-memory search structures (SQLite-FTS-style shards were already measured dead
at ~1.1 GB); zstd payloads; high-fan-out query-time retrieval or fuzzy expansion;
millions of small objects (the accepted target is "hundreds — not millions");
and any build needing sustained >16 GB RAM.

---

## Part 6 — Operator question: other Cloudflare services

**D1: already rejected** by prior prototyping — updates too slow and too
expensive for this use case. Confirmed by pricing: $1.00/million rows written,
10 GB per database.

**Durable Objects as a data store or result cache: rejected on the same
economics.** SQLite-backed DOs are 10 GB/object (Paid) with row writes at
**$1.00/million** — the identical shape that killed D1. Bulk-loading 507M
published records ≈ **$500 per release** in write charges alone. Each DO is also
a single instance in one location, so a global service would need regional
sharding, and R2 bills per *object* not per row (all 42,058 objects of
`slice-2026-07-30.0` cost roughly $0.19 in class-A operations).

**Durable Object holding the partition/routing map: VIABLE, costed, not yet
needed.** This is the operator's refinement and it inverts the arithmetic. The
routing structures are ~10⁵ rows, not 10⁸: Places `routing.json` is 1.43 MB,
Addresses 93 KB, against enforced caps of 8 MiB / ≤65,536 partitions (address
entrypoint) and 2 MiB / ≤32,768 shards (Places catalog). Twenty rebuilds a month
sits inside the 50M included row writes — **$0**. With hibernation, idle storage
of a map that size is inside the 5 GB included allowance, so it **scales to
near-zero when unused**, which is a stated requirement.

*But the benefit today is modest*: at 1.43 MB the map already fits the 8 MiB
entrypoint cap and the 48 MiB in-isolate cache, and is edge-cached for 7 days.
**Trigger for adopting it: when a routing structure outgrows the 8 MiB entrypoint
cap** — most plausibly the street layer's name→bucket routing — not before.

Note the Worker **already runs SQLite** against the runtime's built-in engine via
`from_bytes`/`deserialize` (CLAUDE.md §5); division FTS shards *are* SQLite
databases served from R2. The binding constraint was never "cannot run SQLite",
it is the 48 MiB DB cache.

### Can anything serve the larger FTS SQLite?

The 2026-07-10 spike measured ~1.1 GB for a full-CA Places SQLite FTS and
recorded it as "too large for CF Worker"
(`docs/plans/2026-07-10-places-prototype.md:118, 213`). Asked whether any
Cloudflare product lifts that:

**Workers: no, permanently.** 128 MB per isolate on **both** Free and Paid. It
cannot be raised by plan upgrade and has no announced increase — a foundational
platform constraint. The spike's conclusion stands for the Worker path forever.

**Durable Objects: partially, but for the wrong reason.** A 10 GB SQLite-backed
DO is *disk-backed and queried by SQL*, not deserialized into RAM, so "bigger
than memory" is genuinely solved. But loading it is billed per row written
($1.00/million), which is the wall that killed D1 — see above.

**Cloudflare Containers: YES — this is the real answer.** Instance types reach
**standard-4: 4 vCPU, 12 GiB memory, 20 GB disk**, and they **scale to zero**
(billed per 10 ms while running; charges stop when the instance sleeps), which
satisfies the near-zero-when-idle requirement natively. Included in the $5/month
Workers Paid plan: 25 GiB-hours memory, 375 vCPU-minutes, 200 GB-hours disk;
overage $0.0000025/GiB-s memory, $0.000020/vCPU-s, $0.00000007/GB-s disk. A
1.1 GB FTS — or an order of magnitude more — fits comfortably.

Three caveats decide usability, and none of them is the bill:

1. **Hydration/cold start is the real engineering problem.** Container disk is
   ephemeral per instance, so each wake must either pull the FTS from R2 (slow
   at GB scale) or bake it into the image — which means building and pushing a
   container image per release, a pipeline that does not exist today.
2. **Regional, not edge.** Containers run in specific locations rather than
   every PoP, so the edge-latency property of the current design is lost.
   Acceptable only while latency is explicitly not the priority.
3. **It departs from the immutable-object model.** Create-only R2 objects,
   exact-set verification, CAS catalog flip and 64-release instant rollback all
   need a container-image equivalent. Design it deliberately, not by accident.

**Crucially, this fixes none of Part 1.** `q=Seattle` returning ten bank
branches is a build-time ranking and admission bug; more memory does not touch
it. Containers are an **enabler for a future richer index**, not a remedy for
the current one — so they belong after Stages 1-3, if at all.

---

## Part 6b — MEASURED: Stage 1 fix 1 landed and moved nothing

Deployed 2026-07-31 (`291d243`, deploy run `30666821930`) and re-measured
against the committed baseline. Evidence:
`benchmarks/2026-07-31-forward-gold-after-field-mask.json`.

**Every metric is byte-identical to baseline.** seam t@1 0.000 (10/10 starved),
name 0.400 (6 starved), name_locality 0.700 (3 starved), inverse_seam 0.200
(4 starved), multilingual 0.000 (5 starved). The prediction that `name` and
`inverse_seam` would improve was WRONG.

The fix is nonetheless live and demonstrably correct. `q=paris` changed by
exactly one position:

```
before: Dessirier, Galeries du Diamant, Rexel, New Jawad, Midas
after:  Mercure Paris Opéra Garnier, Dessirier, Galeries du Diamant, Rexel, New Jawad
```

Exactly one of the ten head entries for "paris" was name-identified; it moved to
rank 1 and the nine context-only records shifted down intact. That is precisely
the designed behaviour, at precisely the available scale.

**Why it cannot do more, and the general lesson.** Query-time reranking is
bounded by what the build-time cap admitted. The head stores **ten** entries per
token, chosen during construction by `(confidence_rank DESC, feature_id ASC)`.
For `eiffel`, all ten are *already* name matches — "Hotel Eiffel Blomet",
"Hôtel Eiffel Trocadéro", "Pasticceria La Tour Eiffel" — so there is nothing to
demote and the ordering is unchanged. The Eiffel Tower is not among the ten and
no query-time change can retrieve it.

This confirms the review's warning verbatim: *"otherwise query-time reranking
cannot recover what the cap already discarded."*

Consequences for the plan, all of them sharpening rather than contradicting it:

- **Fix 1 is necessary but not sufficient.** Keep it — it is correct, free, and
  it is a precondition for the cap reorder paying off — but do not expect it to
  move a metric on its own.
- **The gold set could not have seen this even if it were larger.** Seam cases
  expect a *locality*, and reordering within Places cannot produce one. The
  measurement was right; the prediction was wrong.
- **Stage 2 item 5 (reorder the 256-cap eviction to prefer the name bit) is
  promoted**: it is what makes fix 1 pay, and it is build-side.
- **Fix 2 (seam calibration) is the only Stage 1 item that can move `seam`**,
  because only it changes how a Places score compares with a division score.

## Part 6c — MEASURED: Stage 1 fix 2 fixed the seam, and cost one POI case

Deployed 2026-07-31 (`2389871`, deploy run `30668828219`) and re-measured.
Evidence: `benchmarks/2026-07-31-forward-gold-after-seam-calibration.json`.

| group | t@1 before | t@1 after | starved before | starved after |
|---|---|---|---|---|
| `place:seam` | 0.000 | **0.900** | 10 | **0** |
| `kind:multilingual` | 0.000 | **0.600** | 5 | **2** |
| `place:name_locality` | 0.700 | 0.700 | 3 | 3 |
| `place:inverse_seam` | 0.200 | 0.200 | 4 | 4 |
| `place:name` | 0.400 | **0.300** | 6 | 6 |

Overall t@1 0.261 → 0.587; type starvation 34 → 15 of 46. Overall exact-id
self-recall r@1 0.000 → 0.370, r@10 0.065 → 0.413 — the first non-zero recall
this gold set has ever produced.

The prediction that `seam` would move substantially **held**. The prediction
that `name` would be unaffected was **wrong** — it regressed, and the multilingual
gain was not predicted at all.

**The regression is one case and it is diagnostic, not noise.**
`q=Sagrada Familia` returns:

```
locality  Sagrada Família, BR-RS
locality  Sagrada Família, BR-ES
poi       La Cachapera Sagrada Familia
```

Two obscure Brazilian localities now outrank every POI, and the Barcelona
basilica is absent entirely (that part is the build-time head cap, Part 6b, not
this fix). The mechanism: locality and POI both score `match_quality = 1.0` on an
exact name hit, so the *only* remaining discriminator is the static prior — and
`POI_PRIOR_CAP = 0.08` now guarantees the division wins every such tie.

This is the same defect as before with the sign flipped. Previously any
confidence-1.0 POI beat any locality; now any locality beats any POI. Both are
wrong for the same reason: **neither side carries a prominence signal**, so an
exact-name tie is decided by feature class instead of by which feature anyone
was actually looking for.

The fix is not to retune the cap — no scalar makes "Sagrada Família, BR-RS" lose
to a basilica while "Paris, TX" still beats a Paris hotel. It needs the
prominence term Part 6 already specifies (`P`: population/bbox for divisions,
Wikidata importance or equivalent for POIs). Until that exists, the current
setting is the correct trade: it converts 18 broken cases into working ones and
breaks 1.

Net for Stage 1: fix 2 is the first change in this workstream to move a metric,
and it moved several. Stage 2 item 5 (cap eviction) and the `P` prominence term
are now both load-bearing rather than nice-to-have.

## Part 6d — MEASURED: the prominence hypothesis was wrong; categories are the signal

Run 2026-07-31 against Overture places `2026-06-17.0` directly (DuckDB over the
S3 parquet, three token/bbox slices: `sagrada`/Barcelona n=214,
`eiffel`/Paris n=299, `seattle`/Seattle n=4058).

**The hypothesis in Part 6 — that count columns already extracted by
`download_places.sql` carry prominence — is FALSIFIED.**

| column | hypothesis | measured |
|---|---|---|
| `LEN(names.common)` | multilingual notability, the Wikidata-importance proxy | **0 for every record.** Re-measured over 772,341 places across Barcelona / Paris / Seattle / Tokyo: **0.0% non-empty in all four.** The field is entirely unpopulated in the places theme. |
| `root_source_count` | independent corroboration | **1 for every record.** No variance. |
| `websites` / `socials` / `phones` | web presence | ~1/1/1 almost everywhere; where it varies it is *anti*-correlated — "Bruce Jones SEO Services Seattle" ranked 2nd. |

**Worse, `confidence` is anti-correlated with prominence.** Measured:

```
Basílica de la Sagrada Família   confidence 0.9897
Starbucks Barcelona Sagrada familia         0.9998
Clínica Veterinària Sagrada Família         1.0000
Tour Eiffel                                 0.7700   (rank 202 of 299)
```

Every count column on the basilica is byte-identical to the Starbucks next
door: `websites 1, socials 1, phones 1, sources 2, common_names 0`. This is why
`confidence DESC` eviction discards landmarks — it is not a weak prominence
signal, it is an *existence* signal that anti-correlates.

**The one field that separates them is `categories`** — and specifically
`categories.alternate`, which `download_places.sql` does **not** currently
extract:

```
basilica  primary=catholic_church  alternate=[landmark_and_historical_building, monument]
starbucks primary=coffee_shop      alternate=[cafe, restaurant]
vet       primary=veterinarian     alternate=null
```

### Measured effect of a category type prior as the eviction key

Tuned table: primary category dominates, alternates at half weight, a commodity
primary is dispositive (a holiday rental listing `monument` as an alternate is
not a monument), and `landmark_and_historical_building` demoted to 0.35 —
Overture tags US apartment blocks with it ("Marq West Seattle", "Neptune SLU
Apartments"), so it is noisy where `monument` is clean.

| target | rank by `confidence` (today) | rank by type prior | in 10-entry cap? |
|---|---|---|---|
| Basílica de la Sagrada Família | 22 / 214 | **5 / 214** | **yes** (was no) |
| Tour Eiffel | 202 / 299 | 16 / 299 | no (still) |
| `seattle` top-10 | car dealers, florists, gyms | Space Needle, Chief Seattle Statue, Fishermen's Memorial | — |

So the type prior is **necessary and largely sufficient, but not complete.**

**Eiffel's residual failure is a different defect: duplicates.** The 15 records
above `Tour Eiffel` are near-duplicates of the same monument, all tagged
`monument`, all with *higher* confidence than the canonical record:

```
Eiffel Tower,Paris · La Tour Eiffel · Eiffel tower · Eiffel Tower, Paris France
Eiffel Tower,,Paris,Fra · Toul Eiffel · 2ème étage de la tour Eiffel
```

No prior fixes that; it needs duplicate collapse at build time. Note the
canonical record has the *lowest* confidence of the set — the same
anti-correlation, one level down.

### Consequences

- **Part 6's `P` term must be sourced from `categories`, not from counts.** The
  count-based design in Part 6 should not be implemented as written.
- **`categories.alternate` is now projected, as an OPTIONAL field.** It carries
  the landmark signal and is non-empty on **61-73%** of places in all four
  cities measured. It costs **zero extra parquet I/O**: `categories` is already
  in `places_inventory_v1.PROJECTED_COLUMN_ROOTS`, so the column group is read
  either way.

  It is deliberately **not** added to `REQUIRED_FIELD_TYPES`. That contract is
  matched by exact set equality and its fingerprint is bound into frozen
  evidence from real planet runs — every
  `benchmarks/places-construction-v1-data/evidence/*.json` and the pinned
  `readiness-v2.json` record `inventory_sha256 b1830aee...` /
  `schema_fingerprint 49453ed2...`. Adding a required path invalidates that
  attestation chain, and the only honest repair is to **re-run** readiness and
  evidence production, not to rewrite the files. Verified by regenerating the
  inventory: the diff was 19 leaves, all contract-or-hash, map plan and totals
  byte-identical — but it broke `test_control_pins_match_the_real_committed_evidence_files`,
  which is the gate working as designed. Promote the field to required in the
  same change that regenerates evidence for the next planet build.
- **The prominence byte is settled: add a dedicated u8**, +0.60% of the head.
  See item 10 of `2026-07-31-promotion-copy-and-efficiency.md`.
- **The type prior and the Stage 2 cap-eviction reorder are one work item.** The
  prior is the eviction key; it does nothing at query time until eviction uses
  it. Both are build-side and want to land before the next planet rebuild.
- **Duplicate collapse is a newly-identified, separately-tracked defect.** It
  bounds Eiffel-class queries independently of ranking.
- **`common_names` is dead weight in the projection** — always empty, consumed
  only by `baseline_places_construction_v1.py`. Free to drop.

## Part 6e — WIRED: prominence now drives both caps and query-time scoring

The signal from Part 6d is now consumed end to end. Head/serving entries carry a
`prominence_rank` u8 and both build-side caps rank by it before confidence.

**The head entry layout changed, so the format is versioned.** `PLHD0002` ->
`PLHD0003` (and `PLRV0002` -> `PLRV0003`), and the worker decodes **both**:

- `SUPPORTED_PLACES_CONSTRUCTION_FORMATS` lists 0003 and 0002; the three
  `v2.rs` gates call `supports_places_construction_format` instead of comparing
  to one string.
- `decode_entry` takes the version detected from the magic and reads the
  prominence byte only for 0003. A 0002 entry decodes with prominence 0, which
  is exactly the ranking it was built with.

This is not defensive politeness. The deploy carrying this change reaches
production long before any 0003 build is promoted — a worker that accepted only
0003 would take Places search down the moment it shipped. Pinned by
`still_decodes_the_live_0002_head_layout`.

**The head-cap ranking was four inline literals.** The per-task cap, the fan-in
merge, the sharded head and the merged head each spelled out
`confidence_rank DESC, feature_id, ...`. The merge argument
`top_n(A u B) = top_n(top_n(A) u top_n(B))` holds only while all four agree, so
changing three would have produced a different head with no row lost, no binding
violated and no test failing. They are now one `HEAD_CAP_ORDER` constant, the
same treatment `SERVING_ORDER` already had.

`SERVING_ORDER` (the 256 cap) also leads with prominence, and that matters more
than the head cap: it runs first, so a landmark evicted there can never be
recovered downstream however the head is ranked.

**Query time:** `place_score` gives a real prominence the same 0.5 static band
divisions get, because it is now the same kind of signal. `POI_PRIOR_CAP = 0.08`
stays in force wherever no prominence exists (PCSH pages, 0002 shards), so the
measured Part 6c behaviour is reproduced exactly on already-published data
rather than silently re-ranked.

**Status: unmeasurable until a rebuild.** Every published shard is 0002, so
prominence is 0 everywhere in production today and the gold set cannot move.
The tests pin the mechanism (`prominence_outranks_confidence_within_a_token`
puts a prominence-153/confidence-252 landmark above a prominence-0/confidence-255
commodity record); the metric can only follow a 0003 build.

## Part 6f — VALIDATED end to end on the Monaco slice, and it caught two real bugs

Ran the full five-phase fast loop on real Overture places `2026-07-22.0`
(task 33, 38,182 Monaco places) against the `0003` format. **The unit tests all
passed while the pipeline was broken in two places.** Both were found in the
13-second loop, neither by `cargo test`:

1. **`TOTAL_ORDER` was left on `confidence_rank DESC`.** I had reasoned it was
   "layout, not selection" and deliberately skipped it. Wrong:
   `places-serving-encode-v1` ASSERTS its routed input arrives in exactly that
   order and failed closed with *"serving input is not in unique total order"*.
   The producer order and the encoder's `OrderKey` must move together.
2. **`places-serving-verify-v1` still parsed the `0002` entry layout.** I bumped
   its magic without widening its entry walk or its own `OrderKey`, so it read
   past the end: *"truncated entry"*.

Both now fixed and pinned by the slice run: `reconciles=True`,
`marker_written_last=True`, 38,182 records, 4 partitions, 16 head shards,
69,069 head records, 36.23 MB published.

### The measured effect, on real data through the real pipeline

Prominence is populated with a genuine distribution (46 records at 255, then
230/217/153/140/128/115/…). What the two cap orderings actually retain:

| cap ordering | what it keeps |
|---|---|
| `confidence DESC` (before) | Aubade (lingerie store), Autogrill, Burger King x3, Carglass x3 — every one at confidence **1.0**, prominence 0 |
| `prominence DESC` (after) | Trophy of Augustus, Memoriale della deportazione, Collégiale Notre-Dame-de-l'Assomption, Porte d'Italie, Statua della Regina Margherita — every one a `monument`, and **every one at lower confidence than Burger King** |

That is Part 6d's anti-correlation reproduced end to end rather than argued: the
records the old cap kept are chains, and the landmarks it discarded are exactly
the ones a user searching Monaco would want.

## Part 6g — CAUGHT BY REVIEW: prominence alone made name retrieval WORSE

An external review flagged that the build caps rank only by `prominence_rank`,
`confidence_rank` and identity-of-row, and still do not prefer name/brand field
matches — even though Stage 2 item 5 says they must, and Part 6b concluded item
5 "is what makes fix 1 pay". **The claim is correct, and the consequence is
worse than incomplete: it was a regression I introduced.**

`prominence_rank` is a per-RECORD category prior, so it attaches to *every*
token a record emits — including the locality/region/country CONTEXT tokens
(field_mask 8). Ranking a cap by prominence therefore promotes a famous
building's context match above a record actually named for the token. Measured
on the Monaco slice, token `monte`:

| order | top 6 |
|---|---|
| `confidence DESC` (original) | 5/6 context-only; `Novotel Monte Carlo` (a name match) survived at 6 |
| `prominence DESC` (my change) | **6/6 context-only** — Princesse Grace Monument, Villa Sauber, three churches. Every name match displaced. |
| `identity, prominence, confidence` (fixed) | **6/6 name matches** — Gare SNCF Monaco Monte Carlo, Monte Carlo Monaco Train Station, Santuario Monte Bruno … |

`nice` behaved the same way: two context-only churches outranked
`Aéroport de Nice` and `Base aérienne 943 Nice` until identity led.

The fix is Stage 2 item 5, now implemented as `IDENTIFYING_FIRST`
(`((field_mask & 3) != 0) DESC`) leading `TOTAL_ORDER`, `HEAD_CAP_ORDER` and
`SERVING_ORDER`, mirrored in the encoder and verifier `OrderKey`s, and matching
`identifying()` in the worker so build order and query order finally agree.

**Lesson, and it is the same one twice.** Part 6f recorded that the Monaco slice
caught what the unit tests could not. This time the slice ran green — the
pipeline was *correct*, it just ranked the wrong things — and only inspecting
what the cap actually retained exposed it. A passing pipeline is not evidence of
a good ranking. The cheap check that found it (dump the top-N per token by each
candidate order, on real data) should run for every future cap change, and
should be widened to many tokens across regions before a planet run is paid for.

## Part 7 — Recommended sequencing

Ordered by (impact ÷ effort), with correctness before capability and
measurement before both. Nothing here is started.

**Stage 0 — make improvement measurable (prerequisite).**
Build the curated global gold set (~20+20, still unbuilt) and add a **seam
stratum**: ~10 queries that are simultaneously a division name and a POI context
token (Seattle, Paris, Berlin, Monaco), plus ~5 inverse cases where a POI must
beat a same-named division. Add a **type-composition metric** — rank@k alone
would score today's `q=Seattle` as a simple miss without revealing that the city
was displaced by ten 1.0-ties. Without this, none of the below can be claimed.

**Stage 1 — Worker-only fixes. No rebuild, no format change, no new R2 reads.**
1. Field-mask-aware rerank: demote context-only matches. Fixes `q=paris` and
   `q=Seattle` pollution.
2. Division/POI seam calibration: put POI confidence in the same static-prior
   band so an exact division match cannot be buried.
3. Extend locality-suffix inference from `3..=4` to `2..=4` tokens. Fixes
   `IKEA Berlin`. Guarded by exact-locality match, so `Eiffel Tower` cannot
   misroute on "tower".
4. P6 for divisions: relative cutoff and deterministic tie-breaks.

These four plausibly recover most of the visible damage for days of work and
near-zero risk. **Do these first.**

**Stage 2 — build-time Places fixes (planet head rebuild, free runners).**
5. Reorder the 256-cap eviction to prefer name/brand matches over raw
   confidence — recovers retrieval that Stage 1 cannot reach.
6. Dampened POI prior byte replacing saturated confidence.
7. Raise `head_result_cap` (10 → 64) and break confidence ties by a build-time
   prominence instead of UUID order.
8. **Acquire a fame signal.** The projection currently carries none. Cheapest
   real option is a build-time join against the Wikimedia importance data
   *already downloaded for divisions* (`build_shards.py:161`). Do **not** reuse
   quantized confidence — it is the saturated signal that caused this.

**Stage 3 — port famous-unique admission (format bump `PLHD0002` → `PLHD0003`).**
9. Reserve head slots for a famous set and emit `e2:` pair-intersection keys per
   the 2026-07-17 design. This is the structural fix for `Eiffel Tower` /
   `Space Needle`. Requires encoder/verifier/oracle lockstep and a planet head
   rebuild; head shards have 16 MiB headroom.

**Stage 4 — street layer, transportation-derived, as a new family.**
10. Run the 7 acceptance experiments on a bounded metro (use the local machine).
11. Derive a committed name-hash **partition plan** in the
    `places_partition_plan_v1.json` mould, re-verified per use.
12. Build hosted, per release, name-hash partitioned, published into a
    non-promoting families-only slice. Give streets a `field_mask` and a type
    prior **from day one** so the seam is not recreated.
13. v1 explicitly excludes alias/name-change component merging.

**Deferred, with triggers rather than dates:**
- Durable Object partition map — trigger: routing outgrows the 8 MiB cap.
- Cloudflare Containers for a heavier FTS index — trigger: Stages 1-3 land and
  measured recall is *still* the binding limit, i.e. the index shape itself is
  the constraint rather than its ranking. Requires solving image/hydration and a
  container release-and-rollback story first. Do not start here: it fixes none
  of Part 1.
- Free-text address search with house numbers — needs a parser; street-only
  free text sidesteps it and should ship first.
- Address-derived `street_dim` as an interim recall aid — only if Stage 4 slips
  and the 25% coverage hole is explicitly accepted.

### Why this order

Stage 1 is nearly free and fixes user-visible wrongness. Stage 4 is the largest
build and the only one with a genuine one-way door (a new family's format is
permanent across 64 retained releases), so it goes last and only after its
acceptance experiments. Stage 0 goes first because every claim after it is
otherwise unfalsifiable — and because today's promotion smoke was red for months
on an assertion nobody could evaluate.
