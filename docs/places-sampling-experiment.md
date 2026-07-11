# Places sampling experiment

This experiment answers a narrower question than a shard build: which places
survive deterministic 10k/25k/50k sampling, and do the retained targets rank
well enough to justify their routing tier? It does not build, upload, or
promote data.

## 2026-07-11 findings

The current confidence/brand/category `prominence` formula is retained only as
a baseline to disprove or replace; it is not a proposed fleet ranking policy.
On the available 1,768-row downtown San Francisco slice, its top 100 contained
only four categories (86 hotels), while confidence-only retained 53 categories.
Asian Art Museum ranked 530/574 by confidence/prominence and The Warfield
961/973, while Tenderloin Museum jumped 414→4 solely because its exact legacy
category received a hard-coded bonus. Confidence is highly quantized and is an
existence signal, not fame.

A bounded audit of 100,000 rows from the official `2026-06-17.0` release also
showed that brand/contact completeness is strongly source-confounded. Every
feature had exactly one external root dataset; additional source rows were
Overture-derived confidence/status signals, so raw source count is not
independent corroboration. Meta rows were all branded/social, Microsoft and
Foursquare rows were unbranded, and AllThePlaces rows were all branded with
frequent brand Wikidata. Brand and completeness bonuses would therefore rank
providers more than venue fame. Overture explicitly defines `confidence` as
confidence that a Place exists, and `brand.wikidata` identifies the brand, not
the individual venue.

Use confidence, operating status, source dataset/update time, and feature
version for eligibility, calibration, freshness analysis, and tie-breaking.
Use `basic_category` and the new taxonomy hierarchy for retrieval and soft
coverage diagnostics, not hard fame priors. A global landmark rung requires a
reviewed allowlist or a venue-level external fame join.

The current exporter produces a rectangular CA-bbox slice, not an exact
California state export. It can include nearby out-of-state or international
features and must not be promoted as `US-CA`. Use it only for bounded ranking
experiments until exact division-area containment is implemented. Do not run
the benchmark against a prior top-50k download:

```bash
python scripts/benchmark_places_sampling.py exports/places-CA-bbox.parquet \
  --cases benchmarks/places-sampling-cases.example.json \
  --sizes 10000,25000,50000 \
  --strategies confidence,experimental-prominence \
  --json-out benchmarks/places-CA-sampling.json \
  --markdown-out benchmarks/places-CA-sampling.md
```

The two default strategies isolate the current sampling ambiguity:

- `confidence` reproduces the existing download's confidence-first prefix.
- `experimental-prominence` uses the rejected prototype score: confidence plus
  brand, Wikidata, high-confidence, and category bonuses. It is never enabled
  by default and must be requested explicitly.

Every size is a prefix of one stable ordering, so changes between sample sizes
are attributable to capacity rather than a re-sample. The JSON report is the
comparison artifact; Markdown is for review.

Reports include bounded root-source cardinality and source-stratified
retention, category, country/region, feature-confidence, root-source-confidence,
update-year, license, and record-ID coverage. Overture feature confidence and
SourceItem confidence are reported separately. Source identity, license,
freshness, record IDs, and root confidence are diagnostics only and never add
a ranking bonus. Confidence samples are ranked by confidence, not silently
reranked by the rejected prominence formula.

The CLI scans the source once to resolve labels, then once per sampling
strategy while retaining only the largest requested top-N. Memory therefore
scales with the 50k sample and label set rather than the full ~1.94M CA rows.

## Label contract

Cases are JSON objects with `id`, `query`, and one routing class:
`famous_unique`, `local_unique`, or `ubiquitous_brand`. A target can be selected
by reviewed `expected_ids`, exact `target_name`, exact `target_brand`, or a
combination. Name rules should include coordinates and `tolerance_km` when a
name is ambiguous. Before using this as a release gate, replace provisional
name-only cases with reviewed GERS IDs.

The benchmark reports target retention and an approximate offline query rank.
That rank uses exact-name/brand, prefix, then token matching followed by the
prototype prominence score. It is useful for comparing samples, but it is not
a substitute for a later SQLite FTS/BM25 benchmark.

## Routing interpretation

- A `famous_unique` case that survives and ranks in the smallest sample is a
  candidate for a small global HEAD rung.
- `local_unique` cases belong behind a geographic/state route even if they are
  prominent within that state.
- `ubiquitous_brand` cases measure local coverage. The brand token may help
  choose relevant regional shards, but copying every chain location into HEAD
  would crowd out unique landmarks and produce ambiguous global fan-out.

## Inputs needed for a decision

1. A full, unsampled current-release exact-CA export. The current bbox slice is
   suitable only for exploratory work. A top-50k source imposes a ceiling
   that makes 10k/25k/50k comparisons unable to observe excluded landmarks.
2. Reviewed GERS IDs for roughly 30-50 famous unique landmarks, 20-30 strong
   local businesses/venues, and 10-15 ubiquitous brands.
3. Source-stratified category and geography coverage across those labels,
   including confidence decile, root dataset/update time, taxonomy branch, and
   places with and without brands/Wikidata, so provider-heavy sampling is not
   self-validating.
4. A target HEAD byte budget. Quality should be considered alongside the
   already measured 10k/50k shard sizes, not retention alone.

The next useful artifact is a compact rank-audit parquet containing the union
of the top candidates under confidence, the rejected prominence baseline, and
soft category/geographic coverage policies. Evaluate reviewed landmark/local/
chain cases by source and taxonomy without building a shard. Add a genuine
place-level fame signal before considering automatic Places in HEAD.
