# Places recall: independent audit feedback, 2026-08-03

Point-in-time analysis produced by a parallel Claude session at the operator's
request, for the Codex session that owns the entity-phrase evidence work. Per
CLAUDE.md rule 2 this document does **not** override `construction-v1-state.md`;
it is evidence for that file's next revision.

Nine independent read-only auditors plus direct probing ran against branch
`codex/entity-phrase-formal-evidence` at `05ac13b`, live `geocoder.bradr.dev`
(`data_version 2026-08-02.0`), and Overture releases `2026-06-17.0` and
`2026-07-22.0` on S3. Nothing was edited, committed, pushed, or dispatched.

Every claim is marked **MEASURED** (read from code, an evidence file, or a probe
reproduced here) or **INFERRED**.

All sections are complete; nothing is pending.

---

## 0. Hypotheses this audit refuted, including its own

The previous session's self-review identified "promote a plausible explanation
to root cause before running the cheapest discriminating check" as the recurring
failure mode. These were our starting hypotheses; the measurements killed them.

| Hypothesis | Verdict |
|---|---|
| The gold contract inflates the miss count materially | **REFUTED.** 22 of 26 misses are real; corrected ceiling 0.564–0.600 |
| The prominence category table is too narrow; widening unlocks the misses | **PARTLY REFUTED.** 11 misses die on an absent category, but token-count binds harder — only 2 additions unlock anything |
| The alias gap is the biggest remaining bucket | **REFUTED.** 2 of 26 are alias-only |
| Overture has no alternate-name data at all | **REFUTED** (correcting our own §7) — `names.rules` carries 358,195 language-tagged values differing from primary |
| A multi-metro servable build is a cheap falsifying rung | **REFUTED.** ~40–45% of a planet run, and structurally biased toward false positives |
| The entity-phrase lane early-return could suppress a working fallback | **REFUTED by code.** It returns only `if !records.is_empty()` |
| Locality-token queries "annihilate" result sets | **PARTLY REFUTED.** Only 6 genuinely return zero; `Colosseum Rome` and `Machu Picchu Cusco` return 2 |
| Times Square and Golden Gate Bridge are absent from Overture | **SPLIT.** Times Square exists and is retrievable; Golden Gate Bridge is genuinely absent at its true location |
| `websites`/`socials` can cluster fragmented entities and recover aliases | **REFUTED.** 16–30% coverage on landmark classes, ~50% precision at the only safe radius, 1 of 7 gold cases recovered |
| `names.rules` can supply landmark aliases | **REFUTED.** Exactly **0** rows on every landmark category planet-wide |

The mass is in **retrieval and ranking**, not admission width, aliases, or the
benchmark.

---

## 1. P0 — production throws 1101 on ~30–40% of all requests

**MEASURED, reproduced repeatedly over several hours.**

| probe | result |
|---|---|
| error code | **1101** (Worker threw an exception → wasm panic). Not 1102 |
| failure latency | **33–44 ms**, against `cpu_ms = 500` (`wrangler.toml:13`) |
| `q=Paris&limit=1` | 4/10 fail |
| `/` root — static string, no routing, no R2 | 6/12, then 6/15, then 6/20 fail |
| `/v2/health` — 404, matches no route | 500s 3/10 |
| burst of 20, no delay (warm isolate) | 6/20 fail |
| spaced 3 s apart (cold isolate) | 3/10 fail |
| failing body | 17 bytes, Cloudflare edge error page |

A request matching **no route** still panics, and warm and cold isolates fail at
the same rate. That eliminates every query-path explanation — dense cells,
expansion, scoring, R2 fan-out — and also eliminates cold-start.

**INFERRED, not proven from outside:** the shape that fits a route-independent,
warmth-independent, fast-failing, stable ~30–40% rate is **deployment
inconsistency** — a fraction of edge instances running a build that throws at
startup. Two auditors independently misread this as CPU exhaustion; that reading
is wrong (1102 is the resource-limit code, and a CPU kill would burn the full
500 ms first). Do not carry it forward.

**Why this is P0** (a defect that invalidates the next measurement):

- `scripts/benchmark_v2_forward.py:989-1008` retries **only on 429**.
  `ok = status is not None and 200 <= status < 400`; a 500 becomes
  `error = f"http {status}"` and scores as a hard miss. **Any gold measurement
  taken now is silently deflated by ~30%.**
- It is already corrupting audits. One auditor's first pass recorded empty
  bodies as "0 results" and manufactured false absences. **Every probe against
  production must retry**, including yours.

**Dating. MEASURED:** the audited run at `2026-08-03T01:51` (`git_sha ea59ecb`)
reported `errors: 0` across all 55 cases; at ~30% it would have logged ~16. The
panic postdates that run, and the only deploy since is `05ac13b` (#231).
**Suggestive, not proof** — #231's diff is in `search_places_construction`, which
the root path never reaches, so if it is implicated it is via the wasm module or
the deployment, not that logic.

**Next step (yours):** `wrangler tail` for the actual exception, plus deployed
version inspection for a partially-rolled release.

### 1b. A second, separate defect: three routed cells fail 10/10

**MEASURED.** Proximity probes into cells `99eb` (Sydney), Seattle, and
Singapore returned 1101/1102 on **10 of 10 attempts each**, while Paris, NYC,
Rome, Berlin, London, and Cusco succeeded on the first or second try. This is
per-cell and distinct from the site-wide panic.

Both `merge_routed_candidates`' `Err` paths and `routed_fetch_plan`'s
fail-closed tiling check surface as **HTTP 500 rather than an empty result**, so
this is invisible as a retrieval problem. Any routing-based fix is inert in
those cells until it is found. Worth its own ticket.

**Do not measure anything against production until §1 and §1b are closed.**

---

## 2. P0 — the control plane is inconsistent and the test suite cannot see it

**MEASURED.** At HEAD `05ac13b`:

- `benchmarks/places-construction-v1-evidence-spec-v3.json` is **untracked**; the
  v2→v3 bump is the 11-file uncommitted working tree.
- `scripts/construction_v1_control.py:56-57` still pins **v2**
  (`spec_sha256 = 5b779b9f...`).
- `scripts/validate_places_planet_readiness.py:184` already requires the **v3**
  schema string (v3's sha256 is `8aad1990...`).
- `tests/test_construction_v1_control.py:42` still passes because the v2 file is
  unchanged. **The inconsistency is invisible to the suite.**

Close before any v4 generation.

---

## 3. Corrections to `construction-v1-state.md`

**3.1 `prominence_rank` is NOT zero in production. MEASURED.** The state doc says
"every published shard is 0002, so `prominence_rank` is 0 in production and the
gold set physically cannot move," and uses it as the rebuild's core
justification. It is stale — the 2026-08-02 rebuild shipped prominence. Priors
recovered algebraically from live relevance (`prior = 4·rel − 2·quality`), each
landing exactly on its quantized table value:

| category | live prior |
|---|---|
| `monument` | 1.000 |
| `landmark_and_historical_building` | 0.349 |
| `art_gallery` | 0.502 |
| `airport` | 0.902 |
| `museum`, `palace` | 0.851 |
| `catholic_church` | 0.600 |
| `train_station` | 0.549 |
| `park`, `theatre`, `stadium_arena` | 0.451 |
| `library` | 0.400 |
| retail / health / parking | 0.000 (`0.08 · confidence` takes over) |

**3.2 The prior is live and measurably inverted — via alternate-category
inflation. MEASURED, and this is the mechanism, not a bad table.**

`type_prior` (`places_type_prior_v1.py:140-168`) computes from
`primary ∪ basic ∪ hierarchy ∪ 0.5·alternates`. The **display** category carries
only the primary. So records whose primary contributes zero inherit an inflated
prior from an alternate:

| live category | primary-table value | shipped prior | source |
|---|---|---|---|
| `travel_services` | absent | 0.502 | 0.5 × `monument` alternate |
| `fountain` | absent | 0.502 / 0.177 | 0.5 × `monument` / `landmark…` |
| `tours` | absent | 0.502 | 0.5 × `monument` |
| `arts_and_entertainment` | absent | 0.502 | 0.5 × `monument` |
| `attractions_and_activities` | absent | 0.224 | 0.5 × 0.45 |
| `atms` | absent | 0.688 | see 3.3 |

Consequences, both MEASURED live:

- `q=Big Ben London`: ranks 1–2 are a `travel_services` and a **`fountain`**
  named "Big Ben" at prior 0.502; the real landmark is rank 3 at **0.349**.
- `q=Colosseum`: rank 1 is a `monument`-tagged Colosseum **in Serbia** at 1.000;
  Rome's is `landmark_and_historical_building` at 0.502.

The 0.35 weighting of `landmark_and_historical_building` was a documented,
reasonable decision (`places_type_prior_v1.py:44-50`, US apartment blocks). **Its
cost had never been measured.**

**3.3 Build-side defect: `COMMODITY_CATEGORIES` contains `atm`, Overture emits
`atms`. MEASURED.** The commodity override never fires, so ATMs are exempt and
can be inflated by alternates to 0.688. Audit the whole set against actual
Overture strings before the rebuild — `stadium` vs `stadium_arena` is the same
shape (plain `stadium` scores 0 while `stadium_arena` scores 0.451).

**3.4 The state doc does not mention the entity-phrase lane at all.** MEASURED.

**3.5 Stratum figures in circulation are provider-mixed. MEASURED.** The results
file holds 165 rows = **3 providers × 55 cases**, not 55. Overture-only:

| stratum | n | r@1 | r@10 | Nominatim r@10 | Photon r@10 |
|---|---|---|---|---|---|
| seam | 10 | 0.90 | 1.00 | 1.00 | 1.00 |
| name | 10 | 0.20 | 0.70 | 0.90 | 0.80 |
| name_locality | 10 | 0.30 | **0.50** | **1.00** | **1.00** |
| named_poi | 17 | 0.29 | 0.41 | 0.76 | 0.76 |
| inverse_seam | 5 | 0.00 | **0.00** | 1.00 | 1.00 |
| brand_branch | 3 | 0.00 | **0.00** | 0.67 | 1.00 |
| **overall** | **55** | **0.345** | **0.527** | **0.891** | **0.891** |

`inverse_seam` 0/5 and `brand_branch` 0/3 are perfect zeros while Photon scores
5/5 and 3/3 on identical cases, so "the benchmark is unfair" is not available
as an explanation there. See §10.4 for why the comparison is cross-corpus.

---

## 4. Ship first: three Worker-only ranking rules, +6 rank@1, zero regressions

**MEASURED** via an offline simulator that reproduces all 55 live ranks exactly
(19@1/29@10). Artifacts: `scratchpad/{rankprobe.py, sim.py, sim_cat.py,
sim_cat2.py}`.

| variant | @1 | @10 | seam @1 |
|---|---|---|---|
| today | 19/55 | 29/55 | 9/10 |
| B — full-name-exact rung | 22/55 | 29/55 | 9/10 |
| A2 — multi-token coverage | 21/55 | 29/55 | 9/10 |
| A2B | 24/55 | 29/55 | 9/10 |
| **A2B + C** | **25/55** | **29/55** | 9/10 |

`recall@1` 0.345 → **0.455**. ~95 lines in `crates/geocoder-worker/src/v2.rs`.
No rebuild, no data change, no new deps, wasm32-safe.

**Note the `@10` column never moves.** Ranking work cannot close the gap against
0.891 — that gap is retrieval. Only §5 and build-side admission touch `@10`.

**Rule B.** `match_quality` comma-truncates the display name, so
`"Taj Mahal, Agra, India"` and `"Taj Mahal"` both score 1.0 and the tie falls to
UUID order. Add a rung inside `place_score`: an exact match whose *full*
normalized name also equals the query stays at 1.0; one reaching it via comma
truncation drops to 0.97. Fixes `eiffel-tower` 2→1 (both styles), `taj-mahal`
3→1.

**Rule A2.** The char-LCP partial rung (`geocoder-core/src/query/mod.rs:241-250`)
is monotone in name length, so `SickKids Foundation` scores 0.450 against the
gold `SickKids` at 0.400 — the "Statue of Liberty Deli" failure mode, live, in
the ladder. For multi-token queries replace it with
`0.7 · (query tokens present in full name) · (matched chars / total name chars)`.
The 0.7 cap sits below the 0.8 bare-prefix rung so it can never outvote a real
prefix. Fixes `kadewe` 3→1, `sickkids-toronto` 3→1.

**Rule C — query-time prior correction.** A pure function between
`place.prominence` and `place_score`:

```
effective_prior(category, prominence, confidence):
  c = category.trim()
  if c empty                                  -> prominence
  if c in {landmark_and_historical_building,
           historic_site}                     -> max(prominence, 0.85)   # RAISE
  if c in LANDMARK_PRIOR                      -> min(prominence, table[c])  # CLAMP
  otherwise                                   -> min(prominence, 0.08)
```

Both halves independently deliver the full win. Fixes `big-ben` (name_locality)
3→1 and moves `machu-picchu` 10→8 standalone, 7→4 with A2B.

`category` at query time is the raw Overture snake_case primary string
(`project_places_construction_v1.py:218-244`: `display = primary ?: basic`),
enumerable and stable — so the Worker can reproduce the primary-derived
component. It **cannot see alternates**, which is exactly why the clamp works:
alternates are what inflate the wrong records.

**Risk on the clamp:** it materially changes 15/226 sampled live rows.
`airport_terminal` is the real casualty (0.902 → 0.08, a legitimate child
category inheriting from an `airport` alternate). Adding `airport_terminal` and
`tourist_attraction` to the query-time table changes nothing on gold and removes
the loss — ship the clamp *with* those additions, or ship raise-only first for a
minimal diff.

**Seam is provably immune. MEASURED.** POI score is `(quality + 0.5·prior)/2`
with both ≤ 1.0, so the POI ceiling is **0.7500** — already reached today by
`monument` records. The lowest rank-1 division score across all ten seam cases is
**0.78597** (`gold:seam:monaco`). All three rules live in POI-local wrappers in
`v2.rs` and never touch the shared `geocoder_core::query::match_quality`
(`geocoder-core/src/database.rs:308`). Bit-identical seam results across all
eight simulated variants.

**Also: `records.truncate(limit)` runs before scoring** (`v2.rs:2014`, `:2045`,
`:2070`). MEASURED on `q=Taj Mahal`: at `limit=3` the gold record is absent
entirely but present at `limit=9`. A real correctness defect at `limit < 10`
regardless of recall@10 upside. ~10 lines: score the merged pool, sort, then
truncate. The `@10` head-lane upside is **INFERRED, unmeasured** — cheap to check
on the 13-second slice loop.

**Query-time duplicate collapse: recommended against.** For `q=Eiffel Tower`
ranks 1–4 are four copies of one entity at 0.7500; collapsing frees slots only
`The Paris, Texas, Eiffel Tower` fills, and rank@1 still turns on which copy
represents. **It can regress:** rank 1 is 2.57 km from gold (tolerance 2.0, a
miss) and rank 2 is 1.64 km (a hit), so keeping rank 1 converts a hit to a miss.
Revisit after A2B as a diversity feature only.

---

## 5. Locality-token failures: seven mechanisms, and a one-line fix for two cases

**MEASURED**, ≥8 retries per query. This is the only work that moves `@10`.

**Correction to earlier framing:** these do not uniformly "annihilate."
`Colosseum Rome` and `Machu Picchu Cusco` return **2** features reliably. Only 6
of the cited queries genuinely return zero.

| mech | cause | cases | fixable |
|---|---|---|---|
| **A** | 5-token hard gate: `locality_suffix_candidates` requires `(2..=4).contains(&tokens.len())` (`v2.rs:1777`), and `query_terms` keeps `of` | 2 | **yes, trivially** |
| **B** | 2-token blanket suppression: `(!global_head.is_empty() && tokens.len() == 2)` (`v2.rs:1779`) | 4 | yes, with a guard |
| **C** | `e655615`'s exact-prefix `retain` filter (`v2.rs:1795-1802`) | 1 | yes, gated |
| **D** | exonym mismatch in `exact_locality_result` (`v2.rs:1811-1815`) | 2 | yes, one-liner |
| **E** | post-inference ranking, not retrieval | 2 | yes |
| **F** | locality is a **prefix**, not a suffix — inference structurally cannot fire | 3 | **no, build-side** |
| **G** | the division does not exist (`q=Monte Carlo` → 0 features) | 1 | **no, build-side** |

### Fix 1 — widen the token window. 2 cases, ~zero risk. Do this first.

`v2.rs:1777`: `!(2..=4).contains(&tokens.len())` → `!(2..=6)`. Leave the
`tokens.len() > 4` gate in `search_places_construction` alone — the head lane
returning empty for 5-token queries is what *enables* the fallback (empty
`global_head` ⇒ the `retain` filter is skipped).

Decisive A/B, one token removed:
- `Statue of Liberty New York` (5 tok) → 0 features, no inference.
  `Statue Liberty New York` (4 tok) → **10 features**, inference fires, correct
  record at rank 2.
- `Empire State Building New York` (5 tok) → 0. `Empire Building New York`
  (4 tok) → **10 features**, inference fires.

Risk: 5–6 token queries currently return zero POIs unconditionally, so this can
only add. Seam queries are 1–2 tokens and are never reached.

### Fix 2 — carry exonyms into the locality test. 1–2 cases, low risk.

`q=Noma Copenhagen` → 0 features. `q=Noma København` → **3 features, inference
fires, rank 1.** Identical modulo the endonym. Same for `Rome`/`Roma`.

`DivisionRow.search_name` ("primary + alternates/exonyms", `types.rs:157`)
**already carries the exonym and is already in the deployed shards** (proved by
`q=Rome` ranking `Roma` first) — `into_result` simply drops it. Add
`search_name: Option<String>` to `GeocoderResult`, populate at `types.rs:164`,
and accept a match against it in `exact_locality_result`. No rebuild.

Risk: widens what counts as a locality name; mitigated by the existing
longest-suffix-first ordering (`new york` is tried before `york`).

### Fix 3 — relax `retain` from equality to prefix-containment. 1 case, moderate.

`v2.rs:1799-1801`. Isolating evidence: `Taj Mahal Agra` (3 tok) fires inference
because the head contains an exact `Taj Mahal`; `Machu Picchu Cusco` (3 tok) does
not, because its head is `['Machu Picchu Museum', 'Museo Machu Picchu Casa
Concha']`. Same token count, same shape, opposite outcome.

Measured ceiling: `q=Machu Picchu&proximity=Cusco` puts an accepted `alt_names`
entry at rank 5 — satisfies @5 and @10, not @1.

**This partially re-opens what `e655615` closed. Read that commit first** and
gate behind a full gold rerun.

### Fix 4 — replace the 2-token blanket block with the uniform retain rule. Highest risk.

Fixes KaDeWe Berlin and SickKids Toronto (both heads contain the exact
single-token name). Does **not** fix Harrods London or Colosseum Rome.

**Specific risk:** a two-token query like `Berlin Wall` whose head contains a POI
named exactly `Berlin` would reroute to the locality `Wall, SD`. Recommended
guard: skip inference when any head record's full name tokens equal the *whole*
query tokens. **`Berlin Wall` was not probed — do so before shipping.**

### Ruled out explicitly (MEASURED)

- **Strict AND on generic locality postings.** `merge_bounded_candidates`
  (`places_construction_v1.rs:1272-1291`) admits records missing from a
  *saturated* posting when stored display fields prove the token. That is why
  those queries return 2 rather than 0.
- **Entity-phrase early-return suppressing a fallback.** `v2.rs:2033-2050`
  returns only `if !records.is_empty()`; otherwise it falls through. It cannot
  suppress anything. **This removes a pre-rebuild gate we had flagged.**
- **Cell mismatch.** z8 `construction_cell` computed for every landmark/locality
  pair: Machu Picchu/Cusco both `894c`, Statue of Liberty/Empire State/NYC all
  `604b`, Colosseum/Roma both `5f88`, Sydney Opera/Sydney both `99eb`. Routing
  into the locality centroid reaches the landmark in every case.

**Not Worker-fixable (4 cases):** Sydney Opera House, Seattle Central Library,
Singapore General Hospital (prefix-locality — needs build-side entity-phrase
admission; the `e3:` lane exists at `places_construction_v1.rs:1340` but returns
nothing in prod for `e3:sydney opera house`), and Novotel Monte Carlo (no such
division exists).

**The rank simulator cannot validate Fixes 1–4** — all four change which query
and proximity reach the retrieval lane, and the simulator models scoring over an
already-retrieved pool. It is correct only for mechanism E.

---

## 6. The entity-phrase lane: coverage

**6.1 It reaches 3 of 26 current misses. MEASURED**, corroborated by your own
`2026-08-03-poi-admission-audit-v1.json`, whose `entity_phrase_candidate_ids` is
non-empty for exactly `gold:name:big-ben`,
`gold:name:empire-state-building`, `gold:named_poi:seattle-central-library`.

The binding constraint is not the category table. `v2.rs:2033-2050` builds the
probe key from the whole query token vector, and `validate_entity_phrase_records`
(`places_construction_v1.rs:1348-1356`) drops any posting where
`query_terms(primary_name) != tokens`. A hit requires **normalized query ==
normalized record primary_name, both 2–3 tokens** — so every `name_locality` and
`brand_branch` style is structurally excluded at any prominence.

Miss causes: 3 hit · **11 killed by a category absent from `LANDMARK_PRIOR`** ·
7 have prominence > 0 but fail the token/exact-phrase constraint · 3 killed by
`COMMODITY_CATEGORIES` · 2 data absence/quality (§10.5).

**6.2 Which table additions are worth it. MEASURED.** Absent and silently
yielding 0: `hospital`, `clinic`, `bridge`, `casino`, `department_store`,
`post_office`, `stadium`, `government_office`, `opera_and_ballet`,
`childrens_hospital`, `medical_center`, `resort`, `hiking_trail`.

The docstring records exactly three deliberate decisions (weak `historic_site`,
commodity-primary dispositive, alternates at half weight) and **mentions none of
these** — widening reopens nothing. Adding `hotel`/`restaurant` **would** reopen
a measured decision; don't.

Only two additions unlock anything, because token count binds harder:

| addition | unlocks | risk |
|---|---|---|
| `opera_and_ballet` | Sydney Opera House (3 tokens, exact match) | LOW |
| `hospital` | Singapore General Hospital (3 tokens, 0.141 km, tol 1.5) | MODERATE |
| `casino`, `bridge`, `post_office`, `department_store`, … | **nothing** — 4+ tokens or query mismatch | `medical_center`/`clinic` RISKIEST: 13/13 records in the Mayo bbox carry medical-family categories |

Blocked by the 2–3 token limit despite prominence > 0: `statue-of-liberty`
(5 tokens, rank 255), `machu-picchu` (5, 89), `louvre-museum` (5, 217),
`plaza-hotel` (8, 217). `royal-childrens-melbourne` normalizes to 4 because
`Children's` splits into `children` + `s`.

**6.3 The `e2:` spelling divergence is safe. MEASURED, closed.** construction-v1
builds `e{n}:{words in source order}` (`places_transform_v1.rs:300`,
`geocoder-worker/src/places_construction_v1.rs:2897`); the older pages lane
builds `e2:{low} {high}` sorted (`places_pages.rs:927`, `famous_pair_key`).
`entity_phrase_key` is referenced only at `v2.rs:2034` and in the construction
transform (PLHD shards); `famous_pair_key` only at `places_pages.rs:1130`, fed by
`experiment_places_head_repack.py` / `prepare_places_worker_smoke.py` (PCSH
pages). Different producers, readers, and artifact families. A naming wart, not
a risk.

---

## 7. Aliases and multilingual names

**7.1 `names.common` is empty, planet-wide, in both releases. MEASURED.**

| dataset | rows | `names.common` non-empty |
|---|---|---|
| Places `2026-06-17.0` (part-00000) | 4,717,270 | **0** |
| Places `2026-07-22.0` (part-00000) | 4,595,262 | **0** |
| Places `2026-07-22.0` **full planet** | **74,223,561** | **0** |
| Divisions `2026-07-22.0` | 4,655,003 | 1,526,014 (32.8%), up to 301 languages |

Not a June bug, and not a misread — the field works in the same release for
Divisions. The projection already reads it and indexes it under `field_mask`
bit 1 (`project_places_construction_v1.py:261`,
`places_transform_v1.rs:490-499`); it is simply empty.

**7.2 `names.rules` is NOT empty — correcting our own earlier finding. MEASURED,
planet-wide.** An earlier auditor sampled Berlin and Paris only, measured
0.049%/0.22%, and concluded "no alternate-name data, not worth doing." Planet
totals:

| variant | n | language-tagged | differs from primary |
|---|---|---|---|
| **language** | **711,037** | **711,037** | **358,195** |
| official | 36,202 | 11,248 | 35,683 |
| short | 25,729 | 2,854 | 25,724 |
| alternate | 25,507 | 7,009 | 25,502 |
| international | 507 | 0 | 10 |
| historical | 242 | 0 | 242 |

~445,000 alias strings differing from primary, 358,195 explicitly
language-tagged. `names.rules` is **not currently projected**, but it sits inside
the `names` struct root the inventory already reads — so adding it needs no new
column group and no map-plan change, only a projection change plus an
inventory-contract fingerprint change (`places_inventory_v1.py:96`).

**But it is useless for this problem: `names.rules` is exactly 0 on every
landmark category, planet-wide. MEASURED.**

| category | n | rows with `names.rules` |
|---|---|---|
| historic_site | 1,022,932 | **0** |
| park | 503,689 | **0** |
| art_gallery | 154,468 | **0** |
| library | 129,439 | **0** |
| train_station | 100,781 | 43 (0.043%) |
| stadium_arena | 99,815 | **0** |
| museum | 78,094 | **0** |
| public_plaza | 65,833 | **0** |
| airport | 56,484 | **0** |
| monument | 48,937 | **0** |
| castle | 15,760 | **0** |
| art_museum | 13,383 | **0** |
| palace | 5,998 | **0** |

**Not one of the seven gold landmark entities has a `names.rules` entry.** In the
six gold cities it is populated on 0.03–0.13% of records (Berlin 41 of 140,635;
London 134 of 369,178; Paris 332 of 259,833) and on **0.0%** of every landmark
category. The 436,632 planet rows that do carry rules are concentrated somewhere
other than landmark POIs. **Do not project it for this purpose.**

Incidental finding worth folding into the §3.3 category audit: `tourist_attraction`,
`cathedral`, `theatre`, and `university` **do not exist as `taxonomy.primary`
values in this release** (MEASURED), yet all four carry weights in
`LANDMARK_PRIOR`.

**7.3 Cross-record URL clustering — MEASURED, and the verdict is NO.**

The hypothesis was promising: near the Eiffel Tower a record `Eiffelturm`
(German) carries `tour-eiffel.fr` and `برج ایفل` (Persian) carries
`eiffel-tower.com`, suggesting the missing multilingual names exist as separate
records clusterable on an exact URL key — recovering both de-duplication and
aliases without the disproven fuzzy-matching problem.

It does not survive measurement. **Website clustering is exact but not precise,
and where it is precise it recovers almost nothing multilingual.** Those two
properties trade off directly: the clusters carrying cross-script aliases are
exactly the clusters spanning hundreds of kilometres (because the duplicates are
badly geocoded), and the spatial constraint that makes the key safe deletes the
alias yield.

**Sampling correction first.** `part-00000` is **Latin America only** (MX 2.7M,
PE, CO, EC), so the earlier 37.9%/93.7% figures were regional. Planet-wide:
**websites 58.55%, socials 83.57%.**

*Coverage is worst exactly where it is needed* (MEASURED, planet, EU bbox):
`monument` 29.3%, `historic_site` 23.7%, `palace` 23.5%, `park` 23.4%,
`public_plaza` **16.0%** — all far below the 58.55% corpus average. Museums
(72.6%) and galleries (77.2%) are the only well-covered landmark class.

*Precision at the only safe radius is ~50%.* Six-city corpus (Paris, London,
Berlin, Rome, NYC, Sydney; 1,336,138 records). Chain and municipal domains
dominate: `yespark.fr` 4,564 records, `justpark.com` 2,865, `westernunion.com`
1,183, and **`berlin.de` = 647 records / 616 name-forms / 46.1 km span**, merging
Brandenburger Tor with every Bürgeramt and Friedhof in Berlin. A spatial cap
collapses yield — and at ≤200 m all three key normalizations converge (15,486 vs
15,761 clusters), meaning *the spatial constraint, not the key, does all the
work*. A deterministic hand sample of 40 multi-record clusters at ≤200 m judged
**~20 genuine, ~20 false merges**: a mall plus a store inside it, hotels plus
their restaurants, two unrelated braiding salons, a butcher and a graphic
designer.

*Alias yield is negligible.* At ≤200 m the six most alias-rich cities on Earth
produce **121 cross-script clusters out of 1.34M records**, mean 2.05 distinct
name-forms. Planet-wide: **~31,600 safe cross-script clusters covering ~1.3% of
places**, before the ~50% precision haircut — and much of it is Japan/Korea/
Israel/Thailand script-mixing (`スターバックス`/`Starbucks`), not exonym recovery.

*Gold cases: 1 clear yes, 1 partial-but-unsafe, 5 no.* Only **Louvre** works
(`louvre.fr`, 12 records at ≤200 m spanning Han + Kana + Latin — though it still
false-merges `Café Mollien`, `La Joconde`, `Cour Carrée`). **Eiffel Tower**
aliases exist but split across three mutually unlinked hosts and evaporate under
the 200 m cap. **Big Ben** already has `bigben.parliament.uk` as a singleton;
widening to the registrable domain merges it with the House of Lords and
contributes that entity's aliases, not Big Ben's. **Brandenburg Gate,
Colosseum, Sydney Opera House, Times Square: no.**

*Socials are a dead end.* Facebook page IDs are effectively record-unique —
976,093 social values resolve to 935,367 distinct keys, producing **3**
multiscript clusters in 1.34M records. The 96–99% social coverage on monuments
buys nothing, and this supersedes the earlier optimism about `P2002`/`P2003`
handle joins.

**Hard boundary regardless:** URL clustering can only cluster records that exist.
The correctly-located Times Square record has **no website at all**, and Golden
Gate Bridge has no record (§10.5). This was never a coverage fix.

**Recommendation: record and defer.** If ever pursued, the only defensible
configuration is host key + aggregator denylist + all members within 200 m +
≥2 name-forms + ≥2 scripts. Against the cost of building and maintaining a
chain-domain denylist and a containment filter, for 1 of 7 gold cases, it is not
worth it now.

**7.4 The gap is not alias-shaped anyway. MEASURED.** Of 26 misses: 2 alias-only
(`鼎泰豐`, `Apple 丸の内`), 2 same-language surface variants (`Royal Children's
Hospital` apostrophe, `GPO`→`General Post Office`), 2 data absence/quality, 1
outside tolerance, and **15 present-but-misranked**.

---

## 8. The cheap falsifying rung — and the illusory one

**8.1 The multi-metro servable build does not work. MEASURED.**

- The only bbox-native end-to-end path (`build-places-region.yml` →
  `release-slice-families.yml`) builds a **different producer**:
  `scripts/build_places_region_shards.py` has **zero** references to
  `prominence_rank` and no phrase keys.
- construction-v1 subsets by **map task**, not bbox, with no row-level clipping.
  Reading all 5,120 row-group footers: the 18 gold metros touch 263 row groups
  (**5.14%** of rows) sitting inside **22 map tasks holding 24.15% of the
  planet**. Scaled against the measured 2026-08-02 run: **~3.5–4.5 h, ~40–45% of
  a planet construction**, plus a forced Places reverse rebuild
  (`promote-v2-release.yml:677` requires matching `request_sha256`; a new forward
  digest orphans the existing reverse — 225 runner-minutes on 2026-08-02).

It also **cannot falsify in the direction that matters**: a partial build has
fewer competitors per head token, so records clear cap 10 more easily than on the
planet. Wave C measured this (Louvre posting 20 → 455 from 1 region to 15) and
then had its 0/11 → 6/11 prediction refuted by exactly this optimism.

**8.2 The real rung, ~1% of a planet run, RUN LOCALLY. RECOMMENDED.** The lane's
one load-bearing claim is a **posting-size** claim:
`largest_prominent_phrase_posting` of 7–20 across seven metros against
`head_result_cap = 10`. The key is defined purely on source columns, so it needs
no build:

> Take `benchmarks/probes/2026-08-03-entity-phrase-scale-probe.py`, delete the
> `REGIONS` bbox predicate, run planet-wide over the 16 source objects, and add
> one output: per gold POI's normalized primary-name phrase, the planet-wide
> count of prominence-gated records sharing it, plus the canonical record's rank
> under the new order key.

One column-projected DuckDB scan of 75.6M rows. **Local machine, not a runner** —
this session ran comparable full-planet aggregates in minutes with the DuckDB CLI
at `/home/brad/.duckdb/cli/latest/duckdb` and no credentials. If any gold
phrase's planet posting exceeds 10, the lane is falsified before the rebuild is
dispatched.

**8.3 Preview measurement is mostly existing config. MEASURED**, for later.
`wrangler.global-v2-preview.toml` exists (added `47f87d4`, never used) with
`ENVIRONMENT=preview` and **no rate limiter binding**; `stac/catalog.rs:182-213`
accepts `V2_CATALOG_KEY_OVERRIDE` only when `ENVIRONMENT ∈ {smoke, preview}`;
`v2_release_manifest.py` takes `--catalog-key`; `benchmark_v2_forward.py:1507`
takes `--base-url`. Missing: a workflow deploying the preview toml, one passing
`--catalog-key`, and cleanup owning `smoketest-v2/`. `smoketest-r2-id.yml:178-300`
is a working precedent.

---

## 9. Gold contract: real, small, worth fixing before the rebuild

**MEASURED.** 22 of 26 misses are real. Corrected estimate if every defensible
defect is fixed: **0.564–0.600 (31–33 of 55)** — understated by **4–7 points**,
not the ~half the Sacher/Machu Picchu pattern suggested.

- Clean artifacts: `gold:name_locality:colosseum` — rank 1 is `Coliseo Romano` at
  **0.081 km**, the right building under its Spanish exonym;
  `gold:named_poi:plaza-hotel` — rank 8 `The Plaza - A Fairmont Managed Hotel` at
  **0.042 km**.
- Arguable: `raffles-singapore` (`萊佛士酒店` at 0.029 km), `gpo-dublin`
  (`GPO Museum` at 0.021 km — right building, different entity; query genuinely
  underspecified).

**Tolerance is not extent-aware** — hand-authored
(`{2.0: 24, 1.0: 17, 25.0: 10, 0.25: 2, 1.5: 2}`), never derived from geometry —
but **no defensible tolerance change moves recall@10 for any miss.**

**Also MEASURED:** all 55 cases carry `expected_gers_id: null`, so
`exact_id_self_recall` and `provider_neutral_semantic` are **identical for this
file** — the `benchmark_mode` label is a no-op. `expected_feature_type` does not
gate hits, so no miss is a type mismatch.

Minimal fixes, before the rebuild, so the delta is measured against a clean
baseline:

1. `colosseum` — add `Coliseo Romano`, `Anfiteatro Flavio` to `alt_names`
   (precedent: `Москва`, `東京都`, `Apple 丸の内`, `鼎泰豐`). **+1**
2. `plaza-hotel` — add the two observed full names. **+1**
3. `name_locality:statue-of-liberty` — restore
   `Statue of Liberty National Monument`; the sibling `name:` case has it and
   this one doesn't. **+0 now**, but the inconsistency will distort the
   post-rebuild delta.
4. `raffles-singapore` / `gpo-dublin` — decide explicitly, record either way.
5. `machu-picchu` — tolerance to 3.0 with a written extent justification
   (32,592 ha sanctuary). Moves r@1/MRR only.
6. Add a `tolerance_basis` field (`point` vs `extent`) so tolerances are
   auditable rather than folkloric.

**14 of 26 misses return zero features**, and in nearly every one the target
exists at ≤200 m with high `confidence_rank` and returns with proximity — Noma at
9.5 m, Novotel at 61 m, Singapore General Hospital at 139 m, Din Tai Fung at 5 m.

---

## 10. Measurement, scope, and what the comparison actually means

**10.1 The headline is inside noise. MEASURED.** 95% Wilson CI on
`recall@10 = 0.527` at n=55 is **[0.398, 0.653], ±12.8 points**. True independent
entities are **45, not 55** — `name` and `name_locality` are the same 10
landmarks queried twice (agreement 6/10, φ = 0.218). Per-stratum: `name` r@10
±24.8 pp, `named_poi` r@10 ±21.2 pp. `inverse_seam` 0/5 is consistent with a true
rate up to **43%**; `brand_branch` 0/3 up to **56%**.

**10.2 The operative threshold.** McNemar exact requires **≥6 case flips in one
direction with zero regressions** for p < 0.05 (6 → 0.031, 5 → 0.0625). On n=55
that is a +10.9 pp floor.

| claim | flips | p | verdict |
|---|---|---|---|
| Wave C predicted 0/11 → 6/11 | 6 | 0.031 | **properly sized**, correctly refuted at p = 0.0049 |
| RC3 6/10 → 7/10 | 1 | 1.000 | not a result |
| Hotel Sacher contract fix | 1 | 1.000 | already labelled correctly |
| **"recovers Big Ben and Empire State Building"** | **2** | **0.500** | **not a measurable justification** |

**10.3 Re-justify the rebuild, don't stop it.** If the phrase rebuild lands
exactly as predicted the gold set reports 0.527 → 0.564 and the honest statement
is "no detectable change." The defensible argument is already in your state doc
and is *deterministic*: Big Ben and Empire State Building are **provably absent
from the head token postings**, with exact primary-name records confirmed at the
gold points and proximity retrieval at ranks 3 and 1; the phrase key provably
admits records that provably were not admissible; cost bounded at **2.961%** of
source rows. Delete "recovers two gold cases" from the decision-record headline.

**Zero-cost harness change:** emit paired McNemar discordant-pair counts per
stratum alongside recall. On the current set that alone would have labelled RC3
and the Sacher fix as null results.

**10.4 The comparison is cross-corpus. MEASURED planet-wide — Overture Places
contains no OpenStreetMap data.** Full source census over all 74,223,561 rows of
`2026-07-22.0` returns exactly 11 datasets and OSM is not among them:

| dataset | records |
|---|---|
| Overture-signals | 80,216,454 |
| Overture | 74,223,561 |
| meta | 60,591,049 |
| Microsoft | 6,305,853 |
| Foursquare | 4,748,001 |
| AllThePlaces | 1,418,830 |
| BrightQuery | 831,651 |
| PinMeTo | 159,426 |
| DAC | 149,700 |
| Krick | 15,396 |
| RenderSEO | 3,655 |

Nominatim and Photon are OSM-derived. So the 0.527-vs-0.891 headline is not a like-for-like quality gap —
it compares commercial POI feeds against volunteer-mapped geography, on a gold
set that is **45% global-icon landmarks, 80% Europe/North America, 0% non-Latin
script**, with only ~9 of 55 genuinely long-tail POIs (half the 20 "everyday
POIs" added in the 35→55 expansion are internationally famous). Volunteers map
places; commercial feeds map businesses — which is why Times Square arrives as 25
hotels.

**Recommendation:** before committing to fame/alias infrastructure and a sequence
of rebuilds, build ~200 everyday-POI cases in the regime Overture should win
(dense commercial POIs, non-Latin script, outside Europe/NA, from government open
data) and measure all three providers. If Overture is at parity or ahead there,
the headline is a case-selection artifact and the optimization target should
change. Sizing for the decisions actually being made: **1,200 cases, ≥150 per
reportable stratum**, plus a frozen **200-case tripwire** for the fast loop.
Sources must stay open-primary/government and never a compared provider;
Wikidata (CC0) is cheap bulk but partly OSM-imported, so label those cases;
OSM-sourced cases must never enter the Nominatim/Photon comparison strata.

**10.5 Two Overture data-quality defects worth filing upstream. MEASURED.**

- **`Times Square` (NYC) is one record, categorized `hiking_trail`, confidence
  0.204, at 0.984 km from the gold point** — inside the 2 km tolerance, so it
  *would* score if retrieved. It fails only because `hiking_trail` gives
  `prominence_rank = 0`. `Father Duffy Square` at 121 m is correctly tagged
  `public_plaza` at confidence 0.979, so the taxonomy handles the concept. The
  `Times Square Alliance` record at 102 m carries **no website**.
- **`Golden Gate Bridge` has no record at the bridge under any name or URL.**
  Within 2 km there are ~20 satellites (`Tower 2`, `Overlook Of Golden Gate
  Bridge`, `Golden Gate Bridge Toll Plaza`, `Golden Gate Bridge Welcome Center`,
  vista points) but no span record. The only two exact-name records sit **~7 km
  inland**. No record near the bridge carries `goldengate.org`.
  `Puente De San Francisco` at 904 m is a Spanish-language name for the bridge on
  a `landmark_and_historical_building` record.
- Related noise signal: `Starfleet Headquarters` appears as a **`monument` at
  confidence 0.972** 1.4 km from the bridge — directly relevant to a prior table
  that treats `monument` as 1.000.

---

## 11. The durable local sidecar — viable, fame first

Responding to the operator's constraint: planet generation is sharded across
GitHub runners with limited disk/RAM, but the local machine does planet-scale
work offline (~50 min planet map), provided artifacts are **durable** across
releases and **non-blocking**.

**Verdict: viable; runner constraints are not the binding limit — contract
regeneration is.** `prominence_rank` is already an optional `UInt8` the
projection computes, the Rust transform reads via `optional::<UInt8Array>()`, and
the Python baseline reads off the same projected parquet — and it is the first
discriminator in `CAP_ORDER` after the identity bit. A sidecar that changes only
**how the projection fills that u8** needs zero change to the Rust transform,
baseline, reducer, head builder, head wire format, or wasm32 Worker, and baseline
parity is preserved for free.

**This is the established shape. MEASURED:** no change in repo history has ever
added a new pipeline *input*. `category_terms` and `prominence_rank` were both
new optional *columns* from an in-repo Python table. The evidence spec's
`inventory` block declares exactly one source and has no slot for a second.

**Attestation slot:** not the hashed request digest (that only works for a
repo-committed file). `places_construction_v1.py:1326` already writes
`source_limits_sha256` into the task marker; a sidecar should ride the marker the
same way, and its digest should also enter `projection_identity()`
(`project_places_construction_v1.py:312-356`) so it travels in the
`overture.places_projection_identity` parquet metadata and is re-checked by
`require_bound_projection`. Binds **per task**, not per run.

**Join key — the open risk. MEASURED:** GERS stability across releases is
**nowhere measured in this repo**, and the only statement in executable code
assumes the opposite (`deploy-rust-worker.yml:131-133`: "Derive the ID from the
search result so we never hardcode a GERS ID that can churn between releases").
The one attempted cross-release measurement timed out
(`2026-07-12-id-locator-scale-gates.md:42-45`).

Design around it: a **durable** Wikidata-side table keyed by QID (fame, labels,
aliases; regenerated 1–2×/year) plus a **release-scoped** GERS→QID match table
(per release, ~50 min locally). GERS is then only an *intra-release* key, which
is the property the repo does rely on. Fuzzy matching happens offline with
unbounded compute and a reviewable audit list; the runner does an exact 16-byte
join. **§7.3 measured URL/social as a replacement for fuzzy matching and it does
not work** (~50% precision at 200 m, 16–30% coverage on landmark classes), so the
offline fuzzy-match-with-audit-list remains the design, and Phase 0's
match-precision gate is the load-bearing check.

**Broadcast cost is a non-issue:**

| matched POIs | parquet | resident (sorted NumPy) |
|---|---|---|
| 1 M | ~12 MB | ~17 MB |
| 3 M | ~35 MB | ~51 MB |
| 10 M | ~120 MB | ~170 MB |

against a **4 GiB** `process_group_rss_hard_cap_bytes` — ~4% at 10 M. Sharded by
`source_object_index` (each task's `task["ranges"]` names which of the 16 objects
it reads), a task fetches ~2–4 MB. Precedent exists: `inventory/places.json` is
944,768 B, committed, read by every map job. Join via `np.searchsorted`.

**Phasing.**

- **Phase 0 (~1 day, no contract movement):** GERS churn merge-diff over two
  id-sorted registry snapshots (~1 h local); offline match-precision audit on one
  metro (200 hand-checked matches). **Go/no-go is match precision** — a wrong
  high fame value evicts the correct record at the map-side 256-row combiner, the
  earliest and most irreversible cap in the system. Also close §2.
- **Phase 1 — fame only, joined at the projection.** Touches
  `project_places_construction_v1.py` (`_prominence_rank_array`),
  `construction_v1_control.py`, a v4 spec, the readiness validator, and a
  head-manifest `prominence_source` capability. **Cost: v4 generation + 12 census
  reports + 7 role task runs** — the census now binds `transform_binary`, so it
  cannot be carried forward as `categories.alternate` was. Land with the held
  DuckDB 1.5.5 rebuild, where the request digest moves anyway.
- **Phase 2 — aliases. RESOLVED by §7.2 and §7.3: both in-source routes are
  dead.** `names.rules` is exactly 0 on every landmark category, and URL
  clustering recovers 1 of 7 gold cases at ~50% precision. **An external table is
  the only viable alias source**, which raises Phase 2's cost from "project an
  existing column" to "build and maintain a matched alias corpus" — and makes the
  §10.4 recommendation (measure the everyday-POI regime first) more important,
  not less. Injecting into `common_names` remains the right *delivery* mechanism
  whenever an alias source exists: the column is present-but-empty, already
  projected and identity-masked, so injected content is unambiguously
  attributable and needs no Rust/Worker/baseline change.
- **Phase 3 — aliases via the entity-phrase lane: not yet.** Needs a PLHD wire
  field, a wasm32 Worker change, and relaxing `validate_entity_phrase_records`'
  fail-closed check — an invariant v3 was frozen on.

**Fame first, unambiguously.** The inverted prior (§3.2) is an *ordering* failure
at the earliest irreversible cap, and aliases cannot help a record evicted by a
bad `prominence_rank` before it reaches the head. Fame is also the cheaper
artifact (one u8) and needs no new column.

**Not worth doing:** Phase 3 before Phase 2 is measured; runner-side fuzzy
matching of any kind; entity-fragmentation collapse scoped into the sidecar
(changes the term-row unit, violates the frozen duplicate-UUID contract —
separate project); sidecar coordinates for anything but a match-radius gate; and
Nominatim's `wikimedia-importance.csv.gz` for Places (`build_shards.py` uses it
for divisions, but ranking POIs on a signal Nominatim publishes while
benchmarking POI recall against Nominatim is worth avoiding — use raw sitelink
counts and footnote it).

---

## 12. Suggested order

1. **Close the 1101 panic (§1) and the per-cell 500s (§1b).** Nothing can be
   measured until they are.
2. **Close the control-plane v2/v3 inconsistency (§2).**
3. **Ship locality Fix 1 (§5)** — one line, 2 cases, ~zero risk, moves `@10`.
4. **Ship A2B + C (§4)** — ~95 lines, +6 rank@1, zero regressions, seam provably
   untouched. Add score-then-truncate.
5. **Run the planet-wide phrase-posting probe locally (§8.2)** — falsifies or
   confirms the lane before the rebuild is dispatched.
6. **Ship locality Fixes 2–4 (§5)** in risk order, gating Fix 3 and Fix 4 behind
   a full gold rerun and the `Berlin Wall` probe.
7. **Apply the gold-contract fixes (§9) and add McNemar reporting (§10.2)** so
   the rebuild is measured against a clean, correctly-analysed baseline.
8. **Audit `COMMODITY_CATEGORIES` and `LANDMARK_PRIOR` against real Overture
   category strings (§3.3)** — `atm`/`atms`, `stadium`/`stadium_arena` — and add
   `opera_and_ballet` + `hospital` (§6.2). All build-side; they must ride this
   rebuild or wait for the next one.
9. **Re-justify the rebuild on the deterministic admission-gap argument
   (§10.3)**, then proceed with v4.
10. **Sidecar Phase 0 (§11)** in parallel — cheap, local, independent.
11. **Build the everyday-POI benchmark (§10.4)** before committing to the sidecar
    beyond Phase 0.

---

## 13. Open items this audit could not settle

- The actual exception behind 1101 (§1) — needs `wrangler tail` and
  deployed-version inspection.
- Why cells `99eb`/Seattle/Singapore fail 10/10 (§1b).
- `Berlin Wall` behaviour under locality Fix 4 (§5).
- Recall@10 upside of score-then-truncate on the head lane at `limit=10` (§4).
- GERS churn rate across releases (§11 Phase 0).
- Whether `q=Monte Carlo` returning 0 features (§5 mech G) is a divisions-index
  gap or a query-path defect.
