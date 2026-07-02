# ID index pipeline — proposed performance improvements

Date: 2026-07-02
Status: proposals 1 and 2 IMPLEMENTED later the same day (see below);
proposal 3 has experiment tooling in place; proposal 4 stays deferred.
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

**Status: IMPLEMENTED 2026-07-02** — `_partition_release_type` stages
`PARTITION_BY (bucket)` (first hex char, 16 buffers), build jobs filter to
their buckets via `_release_files_for_prefixes`, and the per-theme local
downloads merged into ONE sorted local file (proposal 2's read-side half:
one probe per prefix instead of one per theme/type). Legacy single-file
staging remains readable for patch runs against older versions. Validated
by the smoketest-r2-pipeline run on the landing commit.

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

Prior-trial context: the build's GROUP BY dedup was deliberately dropped
(`1336d0a`) on the assumption that registry and release IDs are disjoint
(`RELEASE_THEMES` comment: "Release themes with IDs not in the
registry"). A merged dataset must either preserve that invariant or
reintroduce dedup at merge time — silently duplicated IDs inflate shards
and record counts. Also note `6ef9e07` (download-locally batching)
exists to avoid 16 redundant R2 reads per prefix; bucketed staging keeps
that read locality, so it supersedes rather than regresses it.

## Proposal 3 — Tune ROW_GROUP_SIZE for /id cold latency

**Status: MEASURED AND ADOPTED 2026-07-02** — `--row-group-size` is
threaded through the build, and `scripts/rowgroup_experiment.py` (dispatch
via the Row-Group Size Experiment workflow) rebuilds live shards at
candidate sizes and measures the worker's exact cold path (32 KB suffix
read + one row-group range read) against R2. Results on 2026-07-02.1
shards (3 prefixes, 8 lookups each):

| rg size | groups/shard | footer | cold read | cold p50 |
|---------|--------------|--------|-----------|----------|
| 25k     | 40           | 16.4 KB | 0.88 MB  | ~197 ms  |
| 50k     | 21           | 8.7 KB  | 1.67 MB  | ~198 ms  |
| 100k    | 11           | 4.7 KB  | 3.24 MB  | ~245 ms  |

Default changed to 25,000: 3.7x smaller cold reads, better tail, footer
still single-suffix-read. Takes effect on the next id-index build.

100k rows ≈ 2.4-3 MB per row group; every cold lookup range-reads a full
row group. Halving row-group size halves the cold read, at the cost of a
larger footer. Cheap experiment: build one prefix at 25k/50k/100k and
compare `scripts/latency_test.py` cold `/id` numbers.

Prior-trial context:
- 100k was chosen deliberately (`f78a6a0`): ~10 groups/shard, sorted
  UUIDs + row-group stats already skip ~90% of data per lookup.
- The "smaller cold-read units" goal was previously attacked at shard
  granularity — prefix-len 4 / 65,536 × 1.8 MB shards (`4cd3048`) — and
  REVERTED in favor of range reads at prefix-len 3 (`b500cd5`). Don't
  re-litigate shard count; row-group size is the surviving knob.
- Hard constraint: the worker's footer suffix read is 32 KB
  (`FOOTER_SUFFIX_SIZE`, stac.rs). Smaller row groups → more column
  chunks → bigger footer; past ~32 KB every cold lookup pays a second
  suffix read. If row groups shrink 4x, bump the constant in the same
  change and re-measure.

## Proposal 4 — Incremental rebuilds (deferred)

GERS is append-mostly; the monthly run rebuilds ~120 GB when a small
fraction of IDs change. Diffing against the prior release and rebuilding
only affected prefixes would cut the pipeline to minutes. Costs a real
correctness surface (deletes, moves, bbox updates must not be missed) for
a job that is free on public runners and already parallel. Revisit only
if wall-clock or R2 write ops start to matter.

## Tried and rejected — do not retry without new evidence

- **Compressing the output shards.** Snappy was the ORIGINAL format and
  was deliberately removed (`b500cd5`): random 16-byte UUIDs and floats
  are incompressible, Snappy framing *expanded* the files, and dropping
  it also eliminated per-lookup decode CPU. ZSTD compresses floats a bit
  better but does not compile to wasm32 (CLAUDE.md). The ~120 GB/version
  is inherent to the data; the cost lever is retention (keep-newest-2),
  not codecs. The worker keeps the `snap` read feature only for
  backward compat with pre-`b500cd5` shards.
- **Per-prefix PARTITION_BY release staging** (`2bd3f45` → retired in
  `3e220a6`): see "the constraint" above; proposal 1's 16-bucket variant
  is the safe replacement.
- **Prefix-len 4 (65,536 small shards)** (`4cd3048` → reverted in
  `b500cd5`): cold-latency goal met by range reads instead; 16x the PUT
  count and footer overhead for nothing.

## Non-goals

- **One giant sorted parquet instead of 4,096 shards**: the footer for a
  ~120 GB file (~50k row groups) is too large for the worker's suffix-read
  + edge-cache model. The prefix-shard design is sound for edge range
  reads.
- **Raising `--workers` / matrix width**: the build phase is R2
  round-trip bound per prefix; more workers mostly adds memory pressure
  (see `17530fa`, which reduced them for stability).
