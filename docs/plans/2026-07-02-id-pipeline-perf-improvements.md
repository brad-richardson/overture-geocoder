# ID index pipeline — proposed performance improvements

Date: 2026-07-02
Status: proposed (nothing below is implemented except "already landed")
Context: written after the 2026-07-02.0 test rebuild, where `id-build`
range jobs `000-3ff` and `400-7ff` died with disk exhaustion sorting the
addresses release file. Companion history: `docs/superpowers/plans/`,
`docs/ranking-research.md` (forward-search side).

## Already landed today (for reference)

- **Prefix-bounded release download** (`0c17773`): each build range job now
  filters `_download_release` to its own quarter of prefixes before the
  local `ORDER BY prefix, id`. Disk spill is proportional to materialized
  rows, so this cut spill ~4x (~12-15 GB against the runner's ~46 GB free).
  Note the job still *scans* the full staged file to find its quarter —
  the single-file staging layout has no prefix locality (see below).
- **Disk-full is not transient** (`0c17773`): `_is_transient` no longer
  retries "No space left on device" (a failed job previously burned 13
  minutes of retry backoff against a full disk).

Headroom estimate with only these fixes: the addresses theme must roughly
**triple** before a quarter-sort overflows a runner again. Not imminent,
but it is a ceiling, and proposal 1 removes it.

## The constraint that shapes everything (learned the hard way)

A DuckDB partitioned COPY keeps **a write buffer per active partition**.
The original release staging (`2bd3f45`, 2026-02-24) wrote release themes
with `PARTITION_BY (prefix)` — up to 4,096 live buffers — and OOMed the
16 GB runners; it was retired to today's one-file-per-type layout in the
"Fixes" commits (`3e220a6` / `b151cd3`).

The registry staging survives with partitioning only because the registry
parquet is **sorted by id**: filtering to 128-prefix sub-ranges gets
row-group pushdown, so at most 128 buffers are ever active
(`SUB_RANGE_PARTITIONS` in `build_id_index.py`). Release themes are
ordered spatially, not by id, so the sub-range trick does not transfer:
filtered passes rescan the whole theme with no pushdown, and an
unfiltered partitioned COPY holds every buffer at once.

**Rule for all proposals: bound the active-partition count; never scale
it with prefix-len.**

## Proposal 1 — 16-bucket release staging (primary; removes the disk ceiling)

Stage each release theme/type `PARTITION_BY` the **first hex char** of the
prefix: 16 buckets → 16 write buffers, far below the 128 the registry
already sustains, and independent of data growth.

- Build range jobs read only their 4 buckets per theme (`000-3ff` →
  buckets `0..3`): real pushdown instead of scan-everything-keep-a-quarter.
- Per-job disk spill stays proportional to a quarter of one theme
  *regardless of how large the themes grow* — the ceiling disappears.
- Stage-time memory is flat (16 buffers) forever.
- Layout change: `staging/id-release-{theme}-{type}/bucket=X/*.parquet`
  instead of `/data.parquet`. Touches `_partition_release_type`,
  `_discover_release_staging_files`, the build download, and the sweep's
  staging expectations. Deserves its own smoketest-r2-pipeline run.

If per-prefix release files are ever wanted (registry-style layout), do it
two-level: bucket to 16 first, then repartition each bucket to its 256
prefixes in a second bounded pass. Probably unnecessary — bucket-level
locality is enough for the build workers' per-prefix probes.

## Proposal 2 — Merge registry + release staging into one dataset

Today every build worker probes each of ~7 release files per prefix
(`SELECT 1 ... WHERE prefix = X LIMIT 1`): 4,096 × 7 probe queries per
run, plus a UNION ALL across sources per shard. If staging (post
proposal 1) lands in one shared layout, the build inner loop becomes
"read partition, sort, write shard" — no probing, no unions. Fold the
source distinction into a column if it is ever needed for debugging.

## Proposal 3 — Snappy-compress the output shards

Shards are currently UNCOMPRESSED: ~120 GB per version, which is most of
the ~$2/mo per retained version. The worker's parquet reader already
carries the `snap` feature (kept for backward compat with older shards),
and parquet compression is per-page, so range reads are unaffected.
Expected: 30-40% smaller shards (UUID+float columns), smaller range reads
for `/id`, slightly more worker CPU per lookup.

Validate via `smoketest-r2-pipeline.yml` before switching: DuckDB
`COMPRESSION SNAPPY` output must decode in the wasm worker. Change sites:
`_worker_build_r2_batch`'s COPY, `patch_failed_shards.py`, and
`_assert_shard_schema` stays as-is (schema, not codec).

## Proposal 4 — Tune ROW_GROUP_SIZE for /id cold latency

100k rows ≈ 2.4 MB per row group; every cold lookup range-reads a full
row group. Halving row-group size halves the cold read, at the cost of a
slightly larger footer (footers are edge-cached with a 1 h TTL). Cheap
experiment: build one prefix at 25k/50k/100k and compare
`scripts/latency_test.py` cold `/id` numbers.

## Proposal 5 — Incremental rebuilds (deferred)

GERS is append-mostly; the monthly run rebuilds ~120 GB when a small
fraction of IDs change. Diffing against the prior release and rebuilding
only affected prefixes would cut the pipeline to minutes. Costs a real
correctness surface (deletes, moves, bbox updates must not be missed) for
a job that is free on public runners and already parallel. Revisit only
if wall-clock or R2 write ops start to matter.

## Non-goals

- **One giant sorted parquet instead of 4,096 shards**: the footer for a
  ~120 GB file (~50k row groups) is too large for the worker's suffix-read
  + edge-cache model. The prefix-shard design is sound for edge range
  reads.
- **Raising `--workers` / matrix width**: the build phase is R2
  round-trip bound per prefix; more workers mostly adds memory pressure
  (see `17530fa`, which reduced them for stability).
