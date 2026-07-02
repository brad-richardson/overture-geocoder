# Future work — parked with context (2026-07-02)

Two items deliberately not built during the 2026-07-02 perf/quality push,
recorded here so the reasoning survives. Companion docs:
`2026-07-02-id-pipeline-perf-improvements.md` (what WAS built and why),
`../ranking-research.md` (forward-search ranking design),
`../overture-data-feedback-2026-07-02.md` (upstream data issues).

## 1. Incremental ID-index rebuilds

**What:** GERS is append-mostly; the monthly run rebuilds ~120 GB of
parquet shards when only a small fraction of IDs change between releases.
Diff the new release against the prior one (both are id-sorted registry
parquets — a merge-diff is cheap), rebuild only the affected prefixes via
the existing `--prefixes` patch path, and copy the rest forward.

**Why it would pay:** pipeline wall-clock drops from ~2.5 h to minutes;
R2 write ops drop ~99% per month. With the 2026-07-02 staging overhaul
(16-bucket staging, merged reads, bounded downloads) the full rebuild is
already disk-safe and free on public runners, so neither cost bites today.

**Why deferred:** it buys a real correctness surface. Deletes, moved IDs,
and bbox-only updates must ALL be caught by the diff; a missed class of
change silently serves stale bboxes forever (no full rebuild would ever
correct it unless scheduled periodically). The existing pipeline's
correctness story is "rebuild everything from source" — simple to trust.

**If picked up:**
- Diff at the prefix level, not the ID level: any prefix containing any
  changed/added/deleted ID gets a full rebuild through the existing patch
  path (`--prefixes` / range jobs). Never surgically edit a shard.
- Copy-forward for untouched prefixes can be R2 server-side copy (no
  download). The `_SUCCESS` marker system already supports partial-run
  bookkeeping.
- Keep a periodic full rebuild (e.g. quarterly) as a safety net against
  diff-logic bugs.
- Revisit trigger: rebuild wall-clock starts to matter (e.g. release
  cadence increases), or R2 write costs become visible on the bill.

## 2. Language-specific forward shards (multilingual search)

**What:** today `search_name` keeps only primary + short + English
common/official/alternate names (a deliberate TODO in
`download_divisions_global.sql`). Non-Latin-script places are findable by
their local name and English exonym, but not in third languages
("Moscou", "Moskau", "モスクワ" don't match).

**Why deferred:** stuffing all ~40 language variants into one FTS column
unbalances BM25 (long token soup per row deflates name-hit scores and
bloats shards ~5-10x). Doing it right means a schema decision, not a
patch.

**Design sketch (from ranking-research.md):**
- Per-language shard families (`{country}-{lang}.db`) or a separate
  `names_i18n` FTS table joined by gers_id, selected by an
  `Accept-Language`-derived hint (the worker already reads CF request
  metadata for country/region routing — language is available the same
  way).
- Keep the current English+local column as the universal fallback; the
  language shard only ADDS candidates, ranked through the same composed
  score (match quality on the localized name, same importance term).
- The `;`-separated search_name format (landed 2026-07-02) transfers
  directly: localized alt names stay phrase-matchable.
- Watch shard size: languages with few localized names for a country can
  share a "misc" shard; don't mint 40 near-empty files per country.

**Prerequisite before building:** decide whether demand is real — the
typeahead benchmark (`scripts/benchmark_typeahead.py`) can grow non-English
query cases (e.g. "moscou", "pekin", "kolonia") to quantify what's missed
today vs Photon's `lang=` support.
