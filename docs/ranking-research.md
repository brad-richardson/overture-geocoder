# Ranking in mature open-source geocoders — and what overture-geocoder should steal

Sources: fetched June 2026 from osm-search/Nominatim master + nominatim.org docs + osm-search/wikipedia-wikidata; komoot/photon master (OpenSearch) and tag 0.6.2 (legacy ES); pelias/api, pelias/query, pelias/placeholder, pelias/documentation; OvertureMaps/schema; plus our own code (`crates/geocoder-core/src/query/*.rs`, `database.rs`, `scripts/build_shards.py`, `scripts/download_divisions_global.sql`).

---

## Part 1 — How the mature systems rank

### Nominatim

1. **Wikipedia/Wikidata importance (primary signal).** Built offline by `osm-search/wikipedia-wikidata` from **inbound link counts**, not pageviews: `totalcount = intra-wiki pagelinks + langlink-weighted cross-language links`, then `importance = log(totalcount) / log(max_totalcount)` normalized so the most-linked article (≈"United States") = 1.0. Published as `wikimedia_importance.tsv.gz` with a **`wikidata_id` column** — directly joinable to QIDs without touching any dumps.
2. **Fallback importance from place rank** when no wiki match: `importance = 0.40001 − rank_search/75` (`lib-sql/functions/importance.sql`, `src/nominatim_api/results.py`). Country=rank 4 → 0.347; state=8; county=12; city=16; town=18; village=19; suburb=19; neighbourhood=24 (`settings/address-levels.json`; `boundary=administrative` → rank = 2×admin_level).
3. **Final order = Σ penalties − importance, ascending** (`db_searches/place_search.py`, `results.py` `ranking` property). Penalties on the same 0–1-ish scale as importance: partial-word base 0.3, edit-distance `dist/len`, looser search strategies +0.2/+0.4/+0.5, viewbox miss 0.5 (extended) / 1.0 (outside), postcode mismatch up to 2.0.
4. **Frequency-aware term strategy, not scoring**: full-name tokens only used when corpus count < 50,000; frequent partials switch to more expensive lookup plans with flat penalties (`db_search_builder.py`).
5. **Tie-breaks**: cutoff at `min_ranking + 0.5` with `+0.03 × (rank_search − min_rank)` size bias; equal-score sort prefers **larger bbox area**; results whose display name misses query words get `+0.3 × distance/query_len` penalty.

### Photon (komoot)

1. **Additive blend, retrieval-time**: OpenSearch `function_score` with `boost_mode=Sum`: `final = text_score + 30·w·importance + 30·(1−w)·exp_distance_decay`, where `w` = `location_bias_scale`, **default 0.4** when a bias point exists, 1.0 otherwise (`opensearch/SearchQueryBuilder.java`, `OpenSearchSearchHandler.java`: `IMPORTANCE_FACTOR = 30f`).
2. **Importance is Nominatim's, with the same fallback**: `importance = NULL ? 0.40001 − rank_search/75 : nominatim_importance` (`nominatim/model/PlaceRowMapper.java`). This fallback *is* their city > town > village > hamlet prior.
3. **Per-term priorities as term frequencies**: terms indexed as `"term|prio"` through a `delimited_term_freq` filter with `norms(false)` — primary names prio 5, other-language names 2, city-level address terms 3, county/state/locality 1, context 1 (`opensearch/NameCollector.java`, `PhotonDocSerializer.java`, `AddressType.java`). BM25 then natively rewards matching a primary name over an alias or context term.
4. **Location bias**: `exp` decay, `decay=0.5`, `offset = radius = 0.1 · 2.2^(18−zoom)` km (zoom 12 default ≈ 11 km), `scale = max(8, radius·(zoom−3))` km; only applied when zoom > 4.
5. **Java-side rerank of top ~1.5×limit** (`QueryReranker.java`): exact name match **+1.0**, starts-with at word boundary **+0.9**, bare prefix **+0.8**, else `0.8 × matched_chars/query_len` coverage; BM25 normalized by the top hit (`osScore/maxScore`) before blending. Note the scale: match quality (0–1.0) deliberately dominates importance (~0–0.4).
6. **Autocomplete**: edge n-grams at index time (whole-name prefix field, minGram 1; per-word minGram 5 with `preserveOriginal`), fuzziness AUTO with prefixLength 1–2 for short queries.

### Pelias

1. **Population/popularity as function scores** (`pelias/api/query/search_defaults.js`, `pelias/query/view/population.js`): `weight × log1p(field)` (log base 10), population weight **2** (autocomplete: 3), popularity weight **1**, `max_boost 20`, `boost_mode: replace`, blended with text via `score_mode: 'avg'`.
2. **Focus point**: `exp` decay, `offset 0km`, **`scale 50km`, `decay 0.5`**, weight **3** for search / **15** for autocomplete.
3. **Structural layer preference, not just score**: `FallbackQuery` builds per-granularity branches; address branch boost 10, street 5; autocomplete postcode boost 2000. Deployment-tunable `customBoosts` per source/layer (×5, capped 50).
4. **Confidence scores** (`middleware/confidenceScoreFallback.js`): start at 1.0; if the result layer is a coarser fallback than what the parse asked for, multiply by granularity: locality-level 0.6, county 0.4, region 0.3, country 0.1. Reverse confidence is pure distance buckets (1.0 at <1 m … 0.5 beyond 1 km).
5. **libpostal is parsing only** — splits free text into housenumber/street/city/state/country to pick the query branch; ~2–4 GB RAM service. Not a ranking component.

### Placeholder (Pelias's coarse, divisions-only geocoder — our closest cousin)

1. **No text-relevance scoring at all.** Final sort, verbatim (`server/routes/search.js`): **`population DESC, then geom.area DESC`**. That's the whole ranking. FTS (SQLite FTS5, `unicode61`, `prefix='1..12'`) is used solely for *candidate generation*; matching is exact-or-prefix, so "matched / didn't match" is binary and prominence does all disambiguation.
2. **Right-to-left subject→object hierarchy walk** (`prototype/query.js`): for "neutral bay north sydney australia", the rightmost token is the *object*, the next-left the *subject*; `match_subject_object.sql` requires the subject doc to have the object in its `lineage` (parent) table; failures skip tokens leftward. So "cambridge uk" is *structured containment*, not bag-of-words.
3. **R-tree geometric fallback** when lineage is missing: subject bbox (expanded 0.2°) must intersect object bbox AND object must be a coarser placetype rank.
4. **Longest-phrase-wins tokenization**: multiword names indexed with `_` joins (`new_york`); all 1–6-token windows checked against the index, longest match consumed first; subsumed groups dropped.
5. **Cheap synonym generation at build time** (`lib/analysis.js`): `saint→st, sainte→ste, mount→mt, fort→ft`; hyphen/apostrophe variants; `&→and`; `ß→ss`, `ü→ue`; "County of X"/"X County"→X, "City of X"→X. Placetype rank is stored but unused in sorting.

---

## Part 2 — Our system today (for reference)

- SQL: `bm25(divisions_fts) − (pop>0 ? 2.0·ln(pop+1) : 2.0)`, `ORDER BY` ascending (`query/mod.rs:32-57`); importance = `−score/50` clamped ≥0 (`types.rs:133`).
- Rust: exact-match +0.1 (`bias.rs:38`), country +0.1 / +0.08, proximity `0.2/(1+d/100km)` (`bias.rs:21-34`).
- FTS5: **`tokenize='porter unicode61 remove_diacritics 1'`** (`build_shards.py:431`); search_text = name + short/common/alternate names + region/country codes + parent names + concatenation/abbreviation aliases (`download_divisions_global.sql:113-163`, `build_shards.py:81-136`).
- Shards carry `type` (subtype) and `population`; Overture's `class` and `wikidata` are **not** carried through (`class` is even selected in the download SQL, line 86, then dropped).

**Found root cause of "france" → San Francisco:** the Porter stemmer. FTS5 stems query tokens too: "france" → stem **"franc"**, and the autocomplete `*` turns it into the prefix query `franc*`, which matches the token "francisco". Porter is an English suffix-stripper and is simply wrong for proper nouns (it also makes "parish" and "paris" collide-adjacent: "parish"→"parish", but e.g. "naples"→"napl").

---

## Part 3 — Prioritized adoption plan

### P0 — Drop the Porter stemmer (bug fix)
- **Build**: change tokenizer to `unicode61 remove_diacritics 2` (Placeholder uses plain unicode61; nobody stems place names — Photon/Pelias use n-grams, Nominatim uses ICU transliteration). Keep the `prefix=` indexes.
- **Query**: none.
- **Effect**: kills "france"→San Francisco and a class of silent stem collisions; slightly stricter matching (plurals no longer conflate — acceptable for toponyms).
- **Effort**: one line + reshard + rerun benchmarks. **Do this first; it changes BM25 distributions that everything below is tuned against.**

### P1 — Wikidata-derived importance column (the prominence fix)
- **Build**: carry Overture's top-level **`wikidata`** property (confirmed in `OvertureMaps/schema/schema/divisions/division.yaml`) into the parquet; download Nominatim's published `wikimedia_importance.tsv.gz` (it has a `wikidata_id` column — join in DuckDB, no dump processing needed) and store `wiki_importance REAL` (0–1, log-of-inlinks normalized). Fallback for unmatched rows: Nominatim's shape, `0.40001 − rank/75`, using the type rank from P2. Also store a `capital` flag from Overture's `capital_of_divisions` (country capital +0.05–0.1, region capital less — Nominatim gets this implicitly via Wikipedia links; we can make it explicit).
- **Query**: replace `2.0·ln(population)` with `w·wiki_importance` in the SQL ORDER BY (population stays only as fallback input, see P2).
- **Effect**: directly fixes paris/london/berlin — Paris-FR's inbound-link mass dwarfs Paris-TX regardless of how population columns are filled; fixes "famous city loses to populous county" because importance is no longer raw population.
- **Effort**: medium — ~1 day pipeline work (TSV is a few hundred MB; DuckDB join on QID), small worker change. Highest impact-per-effort of anything here.

### P2 — Type/level prior + population dampening by type
- **Build**: precompute `type_rank INTEGER` from `subtype` × Overture's **`class`** enum (`megacity, city, town, village, hamlet` — already downloaded, currently discarded). Two cautions from the research: (a) Nominatim's hierarchy ranks county (12) as *more* important than city (16) — copying it raw would worsen our county-beats-city problem; (b) Photon's `AddressType.searchPrio` is the better template for *search*: **city 3 > street/country 2 > county/state/locality-district 1**. Suggested prior (added to importance): country 0.30, region 0.18, megacity 0.30, city 0.22, county 0.10, localadmin 0.08, town 0.14, village 0.06, neighborhood 0.04. Then **only let population contribute within-type**, capped: e.g. `pop_component = min(0.2, 0.03·log10(1+pop))` for localities, half-weight for counties/regions, zero beyond the cap (Pelias caps its population function score at `max_boost 20` for exactly this reason).
- **Query**: SQL ORDER BY becomes `bm25 − k·(type_prior + wiki_importance + pop_component)`; better, precompute the whole static sum into **one `importance REAL` column** at build time so SQL is `bm25 − k·importance` and the no-math-functions fallback path disappears.
- **Effect**: regions vs localities now typed in scoring; big-population counties stop outranking cities; small towns get a sane floor when population is null.
- **Effort**: small-medium; mostly choosing constants and re-running the benchmark suite (`scripts/benchmark_geocoders.py`).

### P3 — Restructure score composition: BM25 for recall, deterministic rerank for order
Every mature system converged on the same shape: *text engine generates candidates; a transparent reranker on the top ~1.5–10× decides order* (Photon's `QueryReranker`, Nominatim's penalty−importance, Placeholder's pure sort). We already fetch 10× — lean into it:
- **Query (Rust)**: replace `importance = −bm25/50` with a composed score, lower-is-worse → higher-is-better:
  `score = match_quality + α·importance + bias`, with Photon's battle-tested match-quality ladder: exact name match **+1.0**, word-boundary starts-with **+0.9**, prefix **+0.8**, else `0.8 × matched_chars/len(query)`; normalize BM25 by the top hit (`bm25_i/bm25_max`) and give it a *small* weight (~0.2) as a tiebreaker only. Set α ≈ 0.5 so importance (0–1) can never outvote a full match-quality step — this is the principled version of our current +0.1 exact bonus, which today is smaller than the importance spread and so barely matters.
- **Effect**: "Paris" vs "Jefferson Parish" handled structurally; same-name disambiguation becomes "all exact matches tie on match quality, importance+proximity break the tie" — exactly the regime where P1/P2 shine. Also fixes cross-shard merging (see traps).
- **Effort**: medium; refactor of `bias.rs`/`merge.rs` scoring path + tests. No reshard needed.

### P4 — Weighted FTS columns instead of one search_text soup
- **Build**: split FTS into columns: `name` (primary + genuine short names), `alias` (alternates, concatenations, abbreviation variants), `context` (parent region/country names + codes). FTS5's `bm25(fts, 4.0, 2.0, 1.0)` gives per-column weights — this is the SQLite-native equivalent of Photon's `term|prio` term-frequency trick and Pelias's field boosts.
- **Query**: pass the weights in the bm25() call; in the Rust reranker, match-quality checks run against `primary_name` only (already true).
- **Effect**: matching "york" against New York's *context* tokens no longer scores like matching its *name*; alias hits rank below primary-name hits instead of equally; doc-length inflation from parent names stops distorting BM25.
- **Effort**: medium (schema change + reshard); pairs naturally with P0's reshard.

### P5 — Adopt Placeholder's synonym table; retire parts of the concatenation hack
- **Build**: extend `ABBREVIATION_PAIRS` with Placeholder's `lib/analysis.js` set (st/ste/mt/ft already partially there; add `&↔and`, umlaut foldings `ü→ue`, apostrophe variants, and the designation strips: "City of X"→X, "X County"→X, "County of X"→X). Keep the pairwise concatenations (they serve "newyork") but emit them into the `alias` column from P4 so they can't outrank real names.
- **Effect**: replaces hand-rolled token concatenation as the main alias mechanism with a curated, auditable list; designation-stripping directly helps county/city same-name pairs ("Cook County" vs "Cook").
- **Effort**: small.

### P6 — Cheap tie-breakers and a confidence field
- **Query**: (a) Nominatim's bbox-area tie-break — among near-equal scores prefer larger bbox (we already store bbox; one comparator line). (b) Nominatim's relative cutoff — drop results worse than `best_score − 0.5`-equivalent instead of always returning `limit`. (c) Expose a Pelias-style `confidence`/`match_type`: exact vs prefix vs alias match, multiplied by layer granularity (their constants: locality 0.6, county 0.4, region 0.3, country 0.1) if we ever fall back.
- **Effect**: fewer junk tail results; clients can threshold.
- **Effort**: small.

### Deferred / when addresses land
- Placeholder's **right-to-left subject→object walk**: when we add streets/addresses, structured "X in Y" beats bag-of-words. Even now, a lightweight version — if the trailing tokens match a country/region name, *filter* (not just boost) candidates by that parent — would make "cambridge uk" precise. We already have `country`/`region` columns to filter on; the parent-name→code mapping can be a tiny static table in the worker or the HEAD shard. Effort: medium, high value for multi-token queries.
- Photon's zoom-derived bias radius (`0.1·2.2^(18−zoom)`) if/when we accept a zoom param. Our current `0.2/(1+d/100)` is within the family of Pelias's exp(scale 50 km, decay 0.5); not a weakness.

---

## Part 4 — Traps

1. **Cross-shard BM25 is not comparable.** Photon/Pelias/Placeholder all assume one global index; we merge 1–3 shards. Raw BM25 embeds per-shard IDF and document-length stats — a mediocre match in a tiny country shard can out-bm25 a perfect match in the US shard. P3's normalization (per-shard `bm25/bm25_max`, rank on match-quality + precomputed importance, which are shard-independent) is the fix; do **not** try to ship global term statistics to the worker.
2. **Don't copy Nominatim's rank table verbatim for search.** Its county-above-city ordering is an *address* hierarchy; used as a search prior it re-creates our county problem. Use Photon's searchPrio shape (city above county/state).
3. **Don't build the Wikipedia pipeline yourself.** Pageview/pagerank pipelines are heavy and the osm-search TSV is already published, QID-keyed, and refreshed. Likewise skip Nominatim's secondary-importance raster (PostGIS raster infrastructure for a ≤0.0001 tie-break).
4. **libpostal cannot run in the worker** (2–4 GB models) and isn't a ranker anyway. Its value to us is offline: optionally use its address-expansion data at build time to generate aliases. Don't put it on the query path.
5. **Photon's `delimited_term_freq` trick doesn't port** — FTS5 has no term-frequency injection. The honest equivalent is P4's column weights; the dirty equivalent (repeating tokens N times) corrupts doc-length normalization — avoid.
6. **Re-tune after de-stemming.** P0 changes BM25 magnitudes; any constants calibrated against today's scores (the `/50` normalization, the 2.0 multiplier) are stale the moment the tokenizer changes. Land P0 + P4 in one reshard, then calibrate P1–P3 constants against the benchmark suite.
7. **No online learning.** Pelias's `popularity` is a curated/derived field, not click feedback; nothing in these systems learns at query time, and neither can we — every adaptive-looking behavior above is precomputed columns + fixed arithmetic, which is exactly what our build-time-rich/query-time-poor architecture wants.
