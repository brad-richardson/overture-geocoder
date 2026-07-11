# Places sampling experiment

This experiment answers a narrower question than a shard build: which places
survive deterministic 10k/25k/50k sampling, and do the retained targets rank
well enough to justify their routing tier? It does not build, upload, or
promote data.

Run it against the full flattened state export, not a prior top-50k download:

```bash
python scripts/benchmark_places_sampling.py exports/places-CA.parquet \
  --cases benchmarks/places-sampling-cases.example.json \
  --sizes 10000,25000,50000 \
  --json-out benchmarks/places-CA-sampling.json \
  --markdown-out benchmarks/places-CA-sampling.md
```

The two default strategies isolate the current sampling ambiguity:

- `confidence` reproduces the existing download's confidence-first prefix.
- `prominence` uses the prototype score: confidence plus brand, Wikidata,
  high-confidence, and category bonuses.

Every size is a prefix of one stable ordering, so changes between sample sizes
are attributable to capacity rather than a re-sample. The JSON report is the
comparison artifact; Markdown is for review.

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

1. A full, unsampled CA flattened export. A top-50k source imposes a ceiling
   that makes 10k/25k/50k comparisons unable to observe excluded landmarks.
2. Reviewed GERS IDs for roughly 30-50 famous unique landmarks, 20-30 strong
   local businesses/venues, and 10-15 ubiquitous brands.
3. Category and geography coverage across those labels, including places with
   and without brands/Wikidata, so bonus-heavy sampling is not self-validating.
4. A target HEAD byte budget. Quality should be considered alongside the
   already measured 10k/50k shard sizes, not retention alone.

The next useful decision is whether prominence materially improves famous
unique retention at 10k/25k without merely filling the sample with chain
locations. If it does not, add a genuine place-level fame signal (for example
a place Wikidata/Wikipedia signal) before increasing sample capacity.
