# 2026-08-04 — The instrument is the blocker, and the head reserve is not real

Two independent re-analyses on 2026-08-04 invalidated premises that the
2026-08-04 next-wave and v5-readiness docs were built on. Where those docs
disagree with this one, this one is correct; both have been superseded on the
specific points below and the state doc should carry these forward.

## 1. Production v2 Places is built from Overture `2026-06-17.0`

`/health` reports `2026-07-28.0`, but that is the **v1 legacy shard family**
version, read through `ShardLoader::check_health` (`handlers.rs:290`). It is a
different namespace from the v2 catalog and must not be read as the v2 vintage.

The v2 vintage is on every `/v2/forward` response:

```
"data_version": {"geocoder_build": "2026-08-03.0", "overture_release": "2026-06-17.0"}
```

Corroborated by `benchmarks/places-construction-v1-evidence-spec-v4.json`
(`release = 2026-06-17.0`, `inventory.required_release = 2026-06-17.0`) and
`docs/plans/construction-v1-state.md:671`.

**So the v4 rebuild was a re-derivation, not an upstream refresh.** Every
quality wave since 2026-06-17 has been re-cutting the same seven-week-old
Overture snapshot. Upstream has published `2026-07-22.0` since. Adopting a newer
release is an untested, unquantified quality lever that no benchmark round has
ever exercised, and it is plausibly larger than any ranking change measured so
far.

## 2. The everyday-POI tripwire cannot credit the work being aimed at it

Verified directly against
`benchmarks/2026-08-04-everyday-poi-miss-classification-v1.json`.

**The `empty_token_cap` bucket has zero competitor-solvable cases.**
Cross-tabbing `mechanism` against `competitor_hit`:

| mechanism | competitor solved | not solved |
|---|---|---|
| `empty_index_starved` | 6 | 64 |
| `returned_wrong_entity` | 6 | 18 |
| `hit` | 12 | 54 |
| **`empty_token_cap`** | **0** | **40** |

Every case any competitor solves is 1–3 tokens. The prefix-head fallback targets
the 4–6 token bucket, so **its measurable ceiling on this set is 0 cases**, not
the 30–60 the next-wave doc projected. The `empty_token_cap` label is also
assigned from token count alone (`classify_everyday_poi_misses.py:84`), making it
an upper bound on the cap's blame presented as an attribution.

**The open-data evidence proxy has a ~50% false-negative rate.** Calibrated on
the 66 cases Overture provably solves: **33 of 66 hits carry no open-data name
evidence at all.** So "25 of 134 misses are addressable" is a floor produced by a
blind proxy, not a coverage measurement — and the correction I issued earlier
this session ("upside caps at ~0.455") was itself built on that proxy and is
therefore also unsound. Both the optimistic and the pessimistic number were
artifacts of the same broken test. MX (35 cases) and CO (20 cases) contain zero
hits, so the proxy is entirely uncalibrated on 27.5% of the set.

**Other defects in the instrument, in rough order of cost to fix:**

- **The scorer requires exact name equality.** `_name_matches`
  (`benchmark_v2_forward.py:800`) accepts only a casefold/NFKD string equality
  against `expected_name` or an `alt_names` entry — no punctuation or token
  normalization. Queries are verbatim government-registry strings. `BEDOK
  RESERVOIR MRT STATION` cannot match Overture's `Bedok Reservoir Station` even
  at 10 m. Only 20 of 200 cases carry `alt_names`, and all 20 are Hong Kong —
  the best-performing cell.
- **`recall@10` is arithmetically `recall@1`.** Overture 65→66 across ranks 1→10;
  Nominatim 21→22; Photon 20→23. There is no ranking headroom on this set, so
  the three ranking changes in the wave cannot move it by construction.
- **Strata are perfectly confounded.** country ≡ poi_family ≡ script ≡ source
  dataset, 1:1 across all nine cells. The "Latin 0.07 vs non-Latin 0.58" split is
  not a script finding; the Latin cells are DENUE, Bogotá health posts, an AU
  business register and LTA.
- **122 of 200 cases are unsolved by all three geocoders.** CO healthcare (20)
  and MX retail (15) score zero for every provider in every round. Some queries
  are non-identifying by construction (`HOTEL CENTRAL`, `TIENDA DE ABARROTES LA
  QUERETANA`) and at least one is corrupt (`"St Station"`).
- **Run artifacts discard the candidate list**, keeping only `top1_*`. Retrieval
  versus ranking cannot be answered post hoc from any frozen run.
- **Zero proximity bias across all 255 cases** in both sets, and zero address
  cases despite `address_structured` being advertised.

## 3. The head is not resident, and the 31.7 MB reserve is not a serving limit

The v5-readiness doc framed `e4:` keys and admission softening as competing for a
31.7 MB head-byte reserve. That framing is wrong in three ways.

- **The head is not broadcast or resident.** Since the sharded `PLHD` format it is
  4,096 R2 objects fetched per token per query and edge-cached
  (`v2.rs:2126`+ → `places_construction_object`). Nothing about it is retained in
  the isolate; the isolate byte budget covers SQLite and JSON text, not `.plhd`.
- **The head is already split**, hash-lexically, 4,096 ways.
- **The gate is on a rehearsal fixture, not production.**
  `validate_places_planet_readiness.py:694` compares
  `rehearsal["head_output_bytes"]` (773,590,640 B, 64 shards, 4.42M records)
  against a self-imposed 1 GiB cap × 0.75. The live planet head is
  **5,141,583,720 B** — 6.4× the cap it is nominally 31.7 MB below. The reserve
  is 4.1% headroom on a ~14%-of-planet fixture and is not a capacity statement
  about anything served.

Consequences: the §3.1-vs-§3.2 "one reserve, two claimants" choice is a false
dilemma, and any decision made on it is unfounded. The real measured constraint
in that area is **head-build DuckDB spill** — v4's merge died at 79% on 8.5 GiB
and needed a scoped resume at 13.69 GB.

The genuinely available capacity levers, none of which widen phrase admission:

1. **Range-read the head shard** instead of fetching the whole object. The
   container is byte-identical in shape to the routed `.plrv` artifact whose
   range path is already written and tested (`range_reader.rs`,
   `places_construction_v1.rs:429-610`). Today each head read transfers ~1–2.7 MB
   to use ≤10 records ≈ 1.7 KB. The analogous legacy repack measured
   6,624 B → 791 B per hit. Worker-only, fix-forward, no rebuild. This was the
   original design (`2026-07-24-places-global-scale-plan.md:118-134`) and was
   simply never implemented.
2. **Drop two provably-wasted fields** — lat/lon f64→f32 (the Worker already
   downcasts to f32) and `source_row_index` u64→u32 (the producer already writes
   int32). 12 B of 166.712 B per record with zero serving-semantics change;
   ~53 MB on the rehearsal artifact, i.e. 1.7× the entire supposed reserve.
   Needs a `PLHD0004` bump, precedented by `PLHD0002→0003`.
3. **Measure head hit rate before evicting anything.** No such measurement
   exists, and the token distribution argues against eviction: 95.1% of tokens
   have ≤10 rows total and 69% are singletons, so the head entry *is* the
   complete planet answer for the long tail.

Not blocked but worth stating: `flate2`/miniz_oxide already compiles to wasm32
and already ships in the Worker for address pages, so "the head cannot be
compressed on wasm32" is false — that constraint applies to zstd only. Deflate is
nonetheless strictly worse than lever 1 and should not precede it.

## 4. What this changes

- The prefix-head fallback should not be justified by the 200-set. It remains
  additive and safe, but this benchmark cannot credit it.
- The v5 `e4:`-vs-softening decision is unfounded as posed and should be reopened
  only after the head byte gate is re-derived at planet scale.
- Measurement repair is cheap and mostly requires no rebuild and no requests:
  re-scoring the **frozen** v4 run with `alt_names` sourced from the Overpass
  control already on disk costs zero live traffic.
- The largest untested quality lever on the board is the Overture release move
  from `2026-06-17.0`, which has never been benchmarked.

---

# Measured outcome (same day, after the additive wave deployed)

Both hypotheses above were tested. One held, one was refuted by measurement.

## The additive wave moved both sets

Build `2026-08-03.0` throughout (a Worker change, no rebuild), paired against
baselines captured immediately before the deploy, same scorer both sides:

| set | before | after | delta |
|---|---|---|---|
| gold 55 r@1 | 0.545 | 0.564 | +1 case |
| gold 55 r@10 | 0.709 | **0.727** | +1 case |
| gold 55 starved | 6 | **4** | −2 |
| everyday 200 r@1 | 0.325 | 0.345 | +4 cases |
| everyday 200 r@10 | 0.330 | **0.350** | +4 cases |
| everyday 200 starved | 111 | **107** | −4 |

Notable against the prediction above: the everyday set moved **+4**, where the
entire v4 planet rebuild plus phrase admission had moved it +1. The
"0 of 40 competitor-solvable" figure bounds what *external evidence* can
confirm; it does not bound what is winnable. Both readings were too confident.

## The name-unscorability hypothesis is REFUTED

Predicted: a large share of misses are correct answers rejected on exact-name
equality. Measured, by scoring one run both ways — same build, same responses,
no new requests:

```
baseline  (      exact)  @1  69  @10  70  r@10 0.350
rescored  (containment)  @1  70  @10  71  r@10 0.355
delta @10            +1
```

**One case.** And the ceiling is 5, not 40:

- 130 misses total
- **106 (82%) returned literally nothing.** An empty response has no candidate
  to re-score, so no name rule of any kind can recover it.
- 24 misses were non-empty, and only **5** had any candidate inside the 1 km
  tolerance. Those 5 are the entire addressable surface for name scoring.

The single flip is `許家蝦仁肉圓/芋粿` against `許家蝦仁肉圓 芋粿` at 7 m — the
same restaurant, `/` versus a space. It is recorded `audit: PENDING` in
`benchmarks/2026-08-04-everyday-poi-containment-rescore.json` and needs a human
to confirm before the 0.355 is quoted anywhere.

**So the miss pile is a retrieval problem, not a scoring artifact**, and the
proposed "add alt_names to all 200 cases / relax the scorer" workstream is
worth at most 5 cases. It should not be scheduled. That is the useful result:
a cheap measurement closed an expensive-looking workstream.

Caveat found while measuring: the first re-scoring run reported a confident
"no cases flipped", which was partly a bug — the tokenizer used an ASCII-only
character class, so every CJK, Hangul and Cyrillic name produced an empty token
set and containment was a silent no-op on half the set. Fixed and regression
tested. The corrected number is the +1 above. A known limit remains: the
2-token floor is Latin-shaped and still blocks a dense single CJK compound
(`중앙대학교병원` against `중앙대학교병원 (Chung-Ang Univ. Hospital)`, 7 m). With a
measured ceiling of 5 cases it is not worth guessing at a fix.

## What this leaves

The two live questions are now both about **retrieval**, and the instrument can
speak to neither from where it sits:

1. Why do 106 of 130 misses return nothing at all? That is the whole game.
2. Does the Overture release move from `2026-06-17.0` change it? Never tested,
   and it is the only lever that adds *data* rather than re-ranking what is
   already there.

Candidate retention now ships on every run, so the next round can answer
retrieval-versus-ranking from a frozen artifact instead of re-querying.

---

# CORRECTION: question 2 is answered, and this doc was wrong about it

Both statements above about the release move — "the largest untested quality
lever on the board" (§4) and "the only lever that adds *data* rather than
re-ranking what is already there" (question 2) — are **measured and refuted**.

`2026-07-22.0` does not add data. It carries **1,418,728 FEWER places** than
`2026-06-17.0` (74,223,561 against 75,642,289) while emitting more distinct head
index entries — upstream conflation, not growth. Its net effect on plain-head
reachability is **+1 case across 206 exact-tier benchmark cases**, against five
regressions, two of which production answers correctly today.

The framing error worth keeping: a lever assumed to be additive was never
checked for losses, so the measurement it implied would have probed misses only
and been structurally blind to all five regressions. Probing current HITS as a
control is what caught them.

See `docs/plans/2026-08-04-release-move-recall-delta.md`. The state doc carries
the summary and supersedes this file on the point.
