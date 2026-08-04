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
