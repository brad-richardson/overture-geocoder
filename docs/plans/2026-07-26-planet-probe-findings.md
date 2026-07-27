# 2026-07-26: what a partial planet-scale probe measured, and what it corrects

Point-in-time analysis. `construction-v1-state.md` is the living document; this
file records findings and corrections produced after PR #177 was merged, so that
none of it depends on a session's context surviving.

Everything here postdates the "planet dispatch readiness" section of
`2026-07-24-construction-v1-follow-ups.md`. Where a number here disagrees with a
number there, **this file is right and that one is stale** — the corrections are
listed explicitly in the last section rather than left for a reader to notice.

## 1. DuckDB temp spill is uncapped in production, and it is a parity gap

`SET temp_directory` appears **seven** times across the two production family
scripts. **None** of the seven sets `max_temp_directory_size`:

```
scripts/address_construction_v1.py:1019
scripts/address_construction_v1.py:1484
scripts/places_construction_v1.py:900
scripts/places_construction_v1.py:1255
scripts/places_construction_v1.py:1473
scripts/places_construction_v1.py:2209
scripts/places_construction_v1.py:2454
```

DuckDB's default for `max_temp_directory_size` is literally `'90% of available
disk space'` (confirmed against duckdb 1.5.1). So every one of those seven sites
is licensed to fill the runner's disk to within 10%, inside a workspace no
watchdog covers, and the spill was **not a term in any prior projection**.

Measured on a real 65.8M-row, 4096-shard `COPY ... PARTITION_BY` killed at ~26%
completion: **3.5 GB of spill** across 10 `duckdb_temp_storage_*.tmp` files
(`DEFAULT-0` 1.09 GB, `DEFAULT-1` 1.85 GB, plus S32K–S224K block files).

The repository already knows the fix and applies it in **four** places — every
one of them a benchmark or experiment, none of them production:

```
scripts/benchmark_id_locator_scale.py:113
scripts/benchmark_transport_components.py:1320
scripts/experiment_address_format_convergence.py:478
scripts/experiment_current_release_addresses.py:1162
```

That is the shape of the problem worth remembering: every exploratory script that
ever met real data learned to cap DuckDB spill, and none of that knowledge
reached the production path.

**TODO:** set `max_temp_directory_size` at all seven production sites, derived
from the scratch budget rather than as a fresh magic number. Fix both families —
this is the per-family-parity defect class that has recurred repeatedly, where a
fix lands on places and addresses silently keeps the bug.

## 2. 113 parquet files per partition, and a guard that cannot see

DuckDB's partitioned write does **not** emit one file per partition. Measured:

- **113 files per partition** (p50 = max = 113)
- **451,687 files at 26% completion** → ~1.7M files at completion
- across 4,096 partition directories

This breaks the only guard covering the otherwise-uncovered head region.
`StageWatchdog.disk_bytes([workspace])` (`address_construction_v1.py:405-445`,
called from `run_bounded`) does an `rglob("*")` + `stat()` over the whole tree
inside a `time.sleep(0.005)` loop. Measured on the real tree:

```
StageWatchdog.disk_bytes() sweep: 1.51 s / 1.49 s warm   # vs a 5 ms intended poll
```

**298x over the intended poll interval**, single-threaded in the parent. Two
consequences, the second worse than the first:

- **>=1.7 h of pure `stat()`** for one sweep per encoder x 4,096 encoders — a
  floor, since the loop samples repeatedly. Against
  `HEAD_PHASE_ESTIMATE_MINUTES: "90"` for the entire phase.
- **The guard goes blind exactly when the tree is largest.** A short encoder
  subprocess can start and exit inside a single 1.5 s sweep, so the scratch check
  *and* the adjacent `elapsed > wall_seconds` check may be evaluated **zero**
  times per encode. This is not a slow guard; it is coverage that reads as real
  and is not.

**TODOs:**

- Give `disk_bytes()` a bounded cost — cache per-directory sizes, or sample
  rather than full-sweep. Fix once, verify **both** family call sites.
- Attack the 113-files-per-partition figure itself, since it is the root of both
  the inode blowup and the blind guard. In order: (a) measure whether shard-range
  batching of the COPY collapses files-per-partition; (b) if not, try DuckDB's
  `FILE_SIZE_BYTES` / single-file-per-partition options; (c) only if both fail
  does `shard_bits = 8` (256 shards) come into play. **(c) is an owner decision**
  — it reverses a design merged in #169, though 256 shards is viable at 97,656
  entries/shard against `MAX_INDEX_ENTRIES` 250,000.

## 3. The head workspace is 1.73x the disk floor, not 1.48-1.64x

The earlier projection assumed an optimistic `shard_dir` = 1.0x `merged`. The
probe measured **1.41x and still climbing** toward the 1.61x that completed
8M/16M-row runs showed. With real 106.4 B/row widths and the spill term from §1
added:

| term | GB |
|---|---|
| `merged` (65.8M x 106.4 B, never unlinked) | 7.0 |
| `shard_dir` @ 1.41x measured | 9.9 |
| DuckDB temp spill (uncapped; 3.5 GB @ 26%) | >=3.5 |
| `verify_dir/*.plhd` @ 1.70x measured | 11.9 |
| **workspace** vs `max_scratch_bytes` 25.77 GB | **32.3** |
| + store `.plhd` copy | 11.9 |
| **filesystem `/`** vs the 25.6 GB job floor | **44.2 — 1.73x** |

Corroborating datapoint, and the one to quote: the probe measured **15.11 GB in
the workspace at 26% completion with zero encodes run** — already 59% of the
scratch cap before any `.plhd` file exists.

Note the spill term is a floor, not a bound: uncapped, it can grow to 90% of free
disk on its own.

~~**This is why the `max_head_candidate_rows` -> 200,000,000 raise must stay
dropped.** The merge-bounding work is sound and byte-identical; the cap raise is
precisely what converts a cheap admission-time abort into a late ENOSPC.~~

**SUPERSEDED. Every term in that table has since moved, and the conclusion reversed —
the raise has landed.** Three things changed the argument, in order of importance:

1. **This region is not where the phase dies, so its cost was never binding.** A
   Europe-scale run (43.9% of the planet, 36 map tasks) reached the sharding region and
   refused on its FIRST statement, the `COPY ... PARTITION_BY`, 3/3 deterministically at
   14,026,510 rows — having written **0 bytes, 0 shard directories, 0 files** and spilled
   **0**. Memory was the binding constraint, not disk. The 44.2 GB above prices a region
   the phase never enters, which is also why it stayed unmeasured.
2. **The feared failure mode is cheap, which was the whole objection.** "A late ENOSPC"
   was the reason to hold the cap; the actual late failure is 37 s, deterministic, zero
   bytes written, no ENOSPC and no disk pressure at all.
3. **The old cap did not admit a planet head in the first place.** Europe's candidate set
   is 26,168,687 rows — **5.2x the 5,000,000 cap** — for 43.9% of the planet, so
   `max_head_candidate_rows = 5_000_000` would have refused that very run at admission.
   Europe also measured 0.8045 candidate rows/place, refuting the 1.809 Monaco figure and
   the 134.3M planet projection built on it; the measured floor is ~59.7M with ~120M as
   the plausible upper end once the CJK tasks Europe excludes are counted.

The table's terms, revised — every one of them was a copy that is now released at its
last use, plus the spill term this doc's own §1 asked to cap:

| term | this doc | now |
|---|---|---|
| un-partitioned payload | `merged` 7.0, never unlinked | pre-sharded 7.0; `merged` unlinked |
| `shard_dir` @ 1.41x | 9.9 | ~7.2 (≈1.03x — derived, applying the measured 13.9x file-count cut to the overhead half of 1.41x) |
| DuckDB spill | >=3.5, **uncapped** | <=4.25, **declared** (17 GiB / 4) |
| `verify_dir/*.plhd` | 11.9 | 11.9, hardlinked to the store copy |
| store `.plhd` copy | 11.9 | **0 additional** — same inode |
| **filesystem `/`** vs the 25.6 GB floor | **44.2 — 1.73x** | **~18.5 — 0.72x** |

`max_scratch_bytes` is also 17 GiB now, not 25.77 GB: 24 GiB sat 170 MB **above** the
floor this table measures against, so every guard built on it was unreachable.

RESIDUAL, and it is a residual rather than a blocker: ~18.5 GB still marginally exceeds
the 18.25 GB cap, so a planet head aborts at `check_head_disk` with a diagnosis naming
both terms and the knobs. Closing that last ~1% means reordering the independent binding
ahead of the shard COPY (undoing #169's guard-before-binding ordering), lowering the
spill share, or giving the head job more disk than the floor guarantees.

## 4. PR #178: an R2 publication backend, under review, NOT merged

Branch `finalize-r2-publication-backend`, rebased onto `9593ab3`, CI green 10/10.
It replaces the serial `aws s3api` mirror and **claims to close BLOCKER A and
BLOCKER C**. Treat that claim as unadjudicated until the review lands.

What it does: `r2_verified_store.Boto3Store` (persistent-client S3/R2 adapter,
create-only + sha256-metadata rules, listing validator extracted and shared so a
fail-closed rule cannot hold on one backend only, boto3 + 6 transitive deps
hash-pinned); `construction_v1_remote.VerifiedStoreRemote` wrapping an
`ObjectStore` rather than adding a second S3 client, so the path is exercisable
offline via `FilesystemStore` and `Budget` measures R2 instead of pricing local
file ops as a proxy; `PUBLISH_CONCURRENCY = 16` submitted in admitted order, pool
drained before any failure propagates, marker strictly last; uploads streamed
from one open handle so RAM is independent of object size.

Verification it reported: published byte set identical to `origin/main` by
path+sha256 (32 places files, 8 address files, baselined *before* any change);
1241 tests pass; 21 mutations all caught, of which **3 survived the first pass and
each was a genuine hole in its own tests**; and the process found two of its own
bugs — a resume-path double operation charge (5N vs a budgeted 4N, a 25% cap
overrun) and a non-rewindable upload body that would have made a retryable 5xx
fatal.

### Two open questions raised against it, not yet answered

- **The ETag content proof may be void where it matters.** Whole-slice
  verification is now metadata-based — single-part ETag vs the MD5 of the bytes
  sent — instead of a re-download. That equivalence holds **only** for
  single-part uploads: boto3's default `multipart_threshold` is 8 MB, and a
  multipart ETag is a digest-of-digests with a part suffix, not a content MD5.
  The PR itself states objects run **2-4.5 GB**. So either single-part is
  enforced (and a 4.5 GB object sits just under S3's hard 5 GB single-`PutObject`
  cliff) or the proof silently degrades. Must be settled by execution.
- **"Closes BLOCKER C" may be "reduces".** Reported peak local disk is
  **16 x largest object**. At 2 GB that is 32 GB; at 4.5 GB it is 72 GB —
  against a 25 GB floor. Going from a 100-145 GB `publish/` tree to 32-72 GB is
  real progress but is not under the floor. If that holds,
  `PUBLISH_CONCURRENCY` must be **derived from the disk budget**, not a constant.

### Residuals the implementer flagged honestly

- The content proof is **MD5, not SHA-256**: it did not use
  `x-amz-checksum-sha256` because R2 support for it was unverified without a live
  bucket. `FilesystemRemote` still does a full SHA-256 read-back, and a resume
  with no local proof falls back to it. **TODO:** a one-object R2 probe to
  confirm `x-amz-checksum-sha256`, then use it. Reverting to full read-back is a
  one-method change costing ~17-50 min per address run.
- **Throughput is a projection, labelled as one.** The only measured input is the
  0.339 s aws-cli startup (~100% CPU, which is *why* concurrency over aws-cli
  would not have fixed it). Projected: places ~12-43 min, addresses ~48-208 min.
  The pessimistic address end exceeds `FINALIZE_PHASE_ESTIMATE_MINUTES: 120` (the
  ledger cost projection, not the 360-min timeout). It deliberately did not raise
  that rather than swap one guess for another. **TODO:** set it from the first
  real measurement.
- Operation accounting moved with the backend: a listing is `ceil(N/1000)`
  requests, so fixed terms dropped by one each. Ceiling **346,182** of a 400,000
  cap — **86.5%, with no margin story.** Open question for review: an
  SDK-internal retry costs a real R2 operation, so a `Budget` charging once per
  logical call under-counts exactly when things go wrong.

## 5. Europe-scale stress runs: what exists, and how to resume them

Two local runs, no credentials, no R2. Both preserved deliberately — **do not
delete these trees.**

**Places** (executing at the time of writing):

```
worktree : /home/brad/dev/wt-176-europe
harness  : /home/brad/dev/wt-176-europe-logs/eu_run.py
logs     : /home/brad/dev/wt-176-europe-logs/{map.log,events.jsonl,measurements.jsonl}
work dir : /home/brad/dev/wt-176-europe-work
resume   : <logs>/venv/bin/python <logs>/eu_run.py --phases <subset> [--map-workers N]
```

It drives `scripts/construction_v1_hosted.py` directly — the same commands
`run_slice_construction_v1.py` issues — but multi-task, with production limits,
`--shard-bits 12`, and per-phase peak-RSS / per-directory peak-disk sampling.
`EU_BBOX = (-25.0, 34.0, 45.0, 72.0)`, `RELEASE = "2026-07-22.0"`,
`DISK_FLOOR_BYTES = 200 GB`. Resume works per phase: `--phases` selects, map
skips on marker-exists (line ~297), reduce skips on reduction-exists (line ~409),
inventory/contract are cached.

Measured over the first 32 of 36 map tasks: **max per-task scratch 2.51 GB**, max
per-task store 0.52 GB, **summed store 12.38 GB**, staging tree 16 GB (growing to
44 GB as head ran). Map is comfortably inside its bounds at Europe scale.

**Addresses**: a separate harness in `/home/brad/dev/wt-176-europe-addr*`.
`eu_run.py:32` hardcodes `FAMILY = "places"` and lines ~300-330 hardcode the
places projector, evidence spec, `places-transform-v1` and
`places-proof-directory` — so the address run required its own harness rather
than a flag.

### Three things about these runs that must not be forgotten

- **`immutable_root` is hardcoded**, not derived from `request_sha256`
  (`eu_run.py:278`, `"construction-v1/eu-places"`). That is convenient — it is
  why a head-phase code fix can re-run against existing map output — but it means
  **these runs do not exercise production's resume-after-code-fix behavior**,
  where editing a family script relocates the staging root and orphans all map
  output. Do not read a successful re-entry here as evidence that production
  resume works.
- **The places harness unlinks its projected inputs** (`eu_run.py:342`,
  `projected.unlink(missing_ok=True)`), so retrying a map task re-runs projection
  against Overture S3. That saves ~2.2 GB against 1.3 TB free and was a bad
  trade. Retention is on for the addresses harness. The per-task
  `rmtree(scratch)` / `rmtree(store_map)` reclaims must stay — they exist to
  measure the per-task disk bound.
- **Wall-clock from either run is contended** and is an upper bound only, because
  the two runs overlapped on one machine. RSS and byte measurements remain valid.

## 6. Corrections to figures recorded earlier

Listed explicitly because the stale numbers are already written down elsewhere.

| claim | recorded as | corrected to |
|---|---|---|
| head workspace vs the 25.6 GB floor | 1.48-1.64x | **1.73x** (44.2 GB), spill uncapped on top (§3) |
| `shard_dir` size vs `merged` | 1.0x optimistic floor | **1.41x measured, climbing toward 1.61x** |
| DuckDB temp spill | not a term at all | **>=3.5 GB, uncapped, inside the workspace** (§1) |
| files per head partition | implicitly 1 | **113** (§2) |
| finalize op ceiling | 346,096 | **346,182** of 400,000 (listing = `ceil(N/1000)`) |
| address publish object count | 65,751 / 65,024 as a projection | **65,024 is a structural bound (127 x 256)**; single-country map tasks imply **~41,200**, nearer 33% of cap than 66% |
| Places blocker list | "effectively cleared" | **wrong** — the address readiness pass found three more, one of which stops Places too |
| remaining hard blockers | A, B, C | **B only, IF #178 survives review** (§4) |

Two older corrections worth keeping visible because they were each wrong twice:
the address map shuffle is **not** the address transport blocker (output is
already hash-clustered; the fix is a range-owning reducer), and address reduce
peak resident is **not** flat — the law is
`peak ~= (map tasks holding the country) x pack bytes`, ~127 packs / ~8.1 GB at
planet scale.

**Corrections made to THIS table by the Europe-scale run and the head-bounding change**
(the rows above are what the 65.8M-row probe supported; these supersede them):

| claim | recorded above | corrected to |
|---|---|---|
| head workspace vs the floor | 1.73x (44.2 GB) | **0.72x (~18.5 GB)** once all four payload copies release, the spill is capped and the COPY is batched (§3) |
| `shard_dir` vs `merged` | 1.41x, climbing to 1.61x | **≈1.03x** — the 0.41 was mostly per-file overhead, and batching cuts the file count 13.9x |
| DuckDB temp spill | >=3.5 GB uncapped | **<=4.25 GB declared**; and the Europe run spilled **0**, so 3.5 GB is this probe's figure, not evidence the phase spills |
| files per head partition | 113 | **3 (max 4)**, pinned at the thread count and flat in row volume once batched (§7.3) |
| the COPY OOM's driver | rows (+1.07 GiB per doubling) | **shard count** — ~1.94 MB pinned per open partition; it refuses at 14.0M rows, 4.7x fewer than projected, before its first flush |
| candidate rows per place | 1.809 (Monaco-linear) | **0.8045 measured** over Europe ⇒ planet floor ~59.7M, upper ~120M, not 134.3M |
| `max_head_candidate_rows` | must stay 5,000,000 | **200,000,000** — 5,000,000 is 5.2x below Europe's candidate set alone and would have refused that run at admission (§3) |
| `shard_bits` = 12 | over-provisioned 64-128x | **correct** — the encoder cap is a floor on shard count; serving FETCH SIZE sets it (~976 KB/shard vs ~62 MB at 6 bits) (§7.3) |
| `max_scratch_bytes` | 25.77 GB (24 GiB) | **17 GiB** — 24 GiB sat 170 MB *above* the very floor this table measures against, so no guard built on it could fire |

## 7. Open TODOs created by this round

1. ~~Cap DuckDB temp at all seven production sites, both families, derived from the
   scratch budget. (§1)~~ **DONE.** All seven derive it from `max_scratch_bytes`
   through one shared helper (`DUCKDB_TEMP_SHARE`, a quarter), and a preflight test
   pins the `SET temp_directory` / `SET max_temp_directory_size` pairing **per
   module**, so the parity gap cannot reopen on one family. Mutation-tested by
   dropping the cap from the address reducer alone. Note §1's 3.5 GB is this probe's
   figure; the Europe run spilled **zero**, so the hazard closed is the uncapped
   default rather than an observed overrun.
2. ~~Bound `StageWatchdog.disk_bytes()`; verify both family call sites. (§2)~~
   **DONE**, and the sweep is one of two loops, not one. Both `StageWatchdog._run`
   and `run_bounded` now wait at least as long as their own worst sweep (1 s
   ceiling), capping the watchdog at ~50% duty cycle whatever the tree looks like,
   and both report `peak_sweep_seconds` so the resolution achieved is visible
   instead of assumed. `run_bounded` is the one that matters most: it runs once per
   encoder subprocess, 4,096 times in the head phase, and its fixed
   `time.sleep(0.005)` against a 1.51 s sweep was a 100% duty cycle on `stat()` AND
   a 1.5 s blind window for all three of its checks.
3. ~~Measure files-per-partition after shard-range batching; then `FILE_SIZE_BYTES`;
   `shard_bits = 8` only as an owner decision. (§2)~~ **MEASURED, and the first step
   was sufficient.** Files per partition is driven by flush pressure across OPEN
   partitions, so batching pins it at the thread count and it stops growing with
   volume:

   | rows | one COPY (4,096 open) | batched (256 open) |
   |---|---|---|
   | 4.2M | 10 files/partition, 39,705 files | 3 (max 4), 10,317 |
   | 16.8M | **34** files/partition, 135,736 files | **3** (max 4), **10,156** |

   Planet projection ~12–16k files against the 1.7M §2 measured, a ~100x cut taking
   the sweep from 1.51 s to ~13 ms; it also cuts `shard_dir` BYTES by 3.20x, since
   per-file overhead was most of that term. So **`FILE_SIZE_BYTES` was not needed and
   `shard_bits` stays at 12** — and §2's suggestion that 4,096 over-provisions by
   64–128x is wrong, because it reasons only from `SERVING_MAX_INDEX_ENTRIES`, which
   is a correctness FLOOR on shard count rather than the target. What sets 4,096 is
   serving fetch granularity: `lookup_head_shard` fetches the single shard a token
   names, so shard bytes are the per-request fetch size — ~976 KB/shard at 4,096
   (this probe measured 427 KB), ~15.6 MB at 256, ~62 MB at 64, the last unusable in
   a Worker. #169 stands and this is not an owner call.
4. Derive `PUBLISH_CONCURRENCY` from the disk budget if 16 x object size exceeds
   the finalize floor. (§4)
5. One-object R2 probe for `x-amz-checksum-sha256`, then upgrade the content
   proof from MD5. (§4)
6. Set `FINALIZE_PHASE_ESTIMATE_MINUTES` from the first real measurement. (§4)
7. BLOCKER B remains open and unmeasured: the address marker fan-in, 14.6 GB of
   JSON / 23.94 GB RSS against a 16 GB runner, loaded whole by `plan-reduce`, by
   every reduce job, and again by finalize. The Europe addresses run exists to
   produce measured bytes-per-marker and bytes-per-partition constants.
8. Places `_reduce_ingest` still hydrates without releasing — the #171 defect,
   fixed on addresses, unfixed on places. Same parity class as §1.

---

# Addendum: the two Europe-scale runs both reached a hard failure

Added later on 2026-07-26. Both local Europe runs (§5) have now executed to a
hard failure, so most of what follows is **measured** where it used to be
projected. Several figures in this file and in the follow-ups doc are corrected
again; §D lists them.

## A. Europe PLACES — 4 of 5 phases passed; head dies in the partitioned COPY

43.9% of the planet (7 of 16 source objects, 32,561,035 records, 36 map tasks at
production per-task granularity — a superset of Europe, not a slice).

| phase | result | peak RSS vs 12.885 GB | peak disk vs 25.770 GB |
|---|---|---|---|
| map (36 tasks) | PASS | 4.474 GB — 34.7% | 2.932 GB — 11.4% |
| plan-reduce | PASS, 8,012 partitions | 3.100 GB — 24.1% | 3.042 GB — 11.8% |
| reduce (128 jobs) | PASS, all 8,012 | 2.004 GB — 15.6% | 1.972 GB — 7.7% |
| **head** | **FAIL — DuckDB OOM** | 8.197 GB, pinned at `memory_limit='8GB'` | 3.909 GB |
| finalize | not reached (requires `--head`) | | |

**Nothing breached a RAM limit, the disk floor, or a wall-clock cap anywhere in
the run.** 3-6x headroom on RAM, 8-13x on disk. The only binding limit is the
8 GB DuckDB `memory_limit`, reached in 37 s.

The COPY fails at **14,026,510 rows on a 1.75 GB input** — 4.7x FEWER rows than
the projection's basis — writing **0 bytes, 0 directories, 0 files** and spilling
**0 bytes**. So the cost is shard-count-driven: `PARTITION_BY` pins ~1.94 MB of
un-evictable buffer per open partition writer, and 4,096 x 1.94 MB ~= 7.4 GiB.
Reproduced 3/3.

The fix is batching the COPY over shard ranges, demonstrated decisively by
holding rows and shards fixed and varying only the batch:

| batch | `memory_limit` | outcome | peak RSS | files/partition |
|---|---|---|---|---|
| 4,096 | 1 GB | OOM at 953.6 MiB, 0 bytes written | 393 MB | n/a |
| **256** | **1 GB** | **completed, all 4,096 dirs** | **554 MB** | **3 (max 4)** |
| 256 | 8 GB | completed | 574 MB | 3 (max 4) |

Batching succeeds at an **8x smaller memory limit than the one that fails
unbatched**, and 554 MB brackets the predicted 256 x 1.94 MB = 497 MB. It also
takes files-per-partition from 113 (§2) to 3, a 13.9x reduction, which fixes the
blind-guard problem at its root.

### `DEFAULT_HEAD_SHARD_BITS` stays at 12 — the shard-count question is CLOSED

Both the PR #176 reviewer (`shard_bits = 8`) and the Europe report (6 bits
suffices) recommended coarsening the head, reasoning from
`SERVING_MAX_INDEX_ENTRIES` 250,000 and `minimum_head_shard_bits`. **That
reasoning is wrong: the encoder cap is a correctness FLOOR on shard count, not
the target.** What sets 4,096 is serving fetch granularity —
`lookup_head_shard` (`crates/geocoder-worker/src/places_construction_v1.rs:288`)
resolves a token by fetching the single shard `head_shard_id(token, shard_bits)`
names, so shard bytes are the per-request fetch size:

| shard bits | shards | planet bytes/shard | verdict |
|---|---|---|---|
| 12 (current) | 4,096 | ~976 KB | fine for an edge fetch |
| 8 | 256 | ~15.6 MB | too large |
| 6 | 64 | ~62 MB | unusable in a Worker |

#169 stands. This is no longer an owner decision.

### `max_head_candidate_rows` MUST rise — an earlier instruction here was wrong

This file and two review rounds said to keep the raise to 200,000,000 dropped.
**That was wrong.** main's `max_head_candidate_rows = 5_000_000` would have
aborted the Europe run **at admission**: the candidate set is **26,168,687 rows
for 43.9% of the planet**, 5.2x the cap. The old cap does not admit a planet head
phase at all, so keeping it guarantees failure rather than preventing one.

The reviewer's objection was sequencing — that raising it converts a cheap
admission-time abort into a late ENOSPC. Europe executed that late failure and it
was **cheap**: 37 s, deterministic, 0 bytes written, no ENOSPC, no disk pressure.
And the 44.2 GB-vs-25.6 GB post-merge region priced in §3 is **unreachable and
therefore unmeasured** — the phase dies on that region's first statement, so its
disk cost is not the binding constraint. Memory is.

Correct ordering: land the batching bound first, then the raise. Value justified
from measurement — **0.8045 candidate rows per admitted place** (which refutes the
1.809 Monaco-linear figure and its 134.3M projection), giving a planet floor of
**~59.7M** and ~120M upper end with the CJK caveat, since the Europe object set
**excludes the CJK tasks** that roughly double term fan-out.

### Other places findings worth keeping

- **`reduce_staged_objects_released = 0`.** Places reduce never evicts —
  `_batch_retention` is addresses-only — so per-job peak equals per-job total
  hydrated: worst job 300.0 MB at stride 2, but it would be the full 12.28 GB at
  `job_cap` 1. **`max_reducers_per_family = 128` is load-bearing for memory
  safety, not just cost**, and nothing in the reduce path fails closed if the
  stride widens.
- **`MEASURED_REDUCE_MINUTES_PER_PARTITION["places"] = 1.0` is ~190x
  conservative** — measured 0.315 s/partition across 8,012 partitions. Harmless
  today; it inflates the ledger projection and sets `timeout_max_batch`.
- **Marker `construction_evidence.peak_rss_bytes` understates whole-job RSS by
  1.8x** (2.473 GB reported vs 4.474 GB process-tree). Anything sizing a runner
  from marker evidence is reading the transform subprocess, not the job.
- **Zero adaptive subdivision**: 8,012 partitions over 8,012 populated cells,
  `depth > 0` on none. Max partition 71% of the row cap, 59% of the token cap.
- The places marker fan-in is a **non-issue**: 20.78 MB of JSON, 63.4 MB RSS at
  Europe; ~51 MB / ~118 MB projected planet. Places markers carry per-pack
  summaries, not per-row data. **BLOCKER B is structurally addresses-only.**

## B. Europe ADDRESSES — BLOCKER B is what stopped the run

34.2% of the planet (11 objects, 161,954,539 source records, 41 map tasks,
151,371,029 admitted).

| phase | result |
|---|---|
| inventory, contract, project (41x), map (41x) | PASS — 7.80 GB of markers |
| plan-reduce | PASS — 204 partitions, batch 2, 102 jobs. **Peak RSS 14.40 GB = 90% of a 16 GB runner** |
| **run-reduce** | **FAIL — 26 jobs attempted, 0 succeeded, 0 of 204 reductions written** |
| finalize | not reached |

**Every reduce job dies identically, and it dies before doing any work.** The
watchdog aborts on its FIRST observation, **0.95 ms in**:

```json
{"failure": "whole-stage RSS exceeded its hard cap",
 "peak_rss_bytes": 14230532096, "max_rss_bytes": 12884901888,
 "peak_disk_bytes": 786218285, "max_scratch_bytes": 25769803776,
 "wall_seconds": 0.00095, "wall_cap_seconds": 18000}
```

`cmd_run_reduce` calls `_load_markers` (13.45 GB resident) **outside** the guarded
region, then enters a watchdog scoped to the export — which immediately sees
whole-process RSS already 1.10x over the 12 GiB `max_rss_bytes`. Batch size,
partition size and pack hydration are all irrelevant: all 102 jobs are dead
before they start. Not an OOM kill, not ENOSPC, and **no DuckDB spill was
involved** (zero `duckdb_temp_storage*.tmp` anywhere; peak reduce disk 2.36 GB
against a 24 GiB cap), so §1's uncapped spill was not a factor here.

### The structural law: marker size is a function of COUNTRIES, not rows

Measured `bucket_summaries`: 65,538 for a 1-country task, 131,075 for 2, 196,577
for 3. `maximum_bucket` is 16 bits, so a country with N rows in a task occupies
`65536 * (1 - exp(-N/65536))` buckets, saturating at exactly 65,536:

```
entries(task) = 2 * SUM_countries 65536 * (1 - exp(-admitted_rows_c / 65536))
marker_bytes  = entries * 490.41
```

Validated against the 41 measured markers: model/measured = **1.1193** (12%
conservative). This law was not in the docs before.

### Measured constants

| constant | measured |
|---|---|
| marker bytes on disk, total (41 tasks) | 7,799,189,884 (7.80 GB) |
| per marker mean / max / min | 190,224,144 / 444,896,870 / 64,394,017 |
| **per address record** | **51.52 B** |
| **per reduce partition** (204) | **38,231,323 B** |
| entries, both arrays | 15,903,436 (`bucket_summaries` 7,950,647 + `routing_groups` 7,952,789) |
| bytes/entry on disk / compact | 490.41 / 330.33 |
| `indent=2` inflation | **1.4846x** (recorded 1.556 — 4.8% optimistic) |
| `packs[*].directory` share of a marker | **99.97%** |
| **peak RSS, one whole `_load_markers`** | **13,448,597,504 (13.45 GB) = 84.05% of a 16 GB runner** |
| RSS per marker disk byte | **1.7214** (stable at 11 / 29 / 41 markers) |
| load wall time | 39.5 s per consumer, per invocation |

### Planet extrapolation, from the real 2026-07-22.0 planet plan

Built from the same footer survey (126 tasks, 472,703,893 records, US held by 39
tasks — matching the recorded figure) rather than projecting onto the committed
06-17 inventory:

```
modelled entries            38,533,990
x 490.41 B/entry          = 18,897,419,682 B  (18.90 GB)
/ 1.1193 model correction = 16,882,623,168 B  (16.88 GB) on disk
x 1.7214 RSS ratio        = 29,061,747,521 B  (29.06 GB) RSS
```

Same model on the committed 06-17.0 / 127-task inventory: **17.12 GB disk /
29.48 GB RSS**. Straight-line from the measured per-task mean gives 23.97 GB /
41.26 GB as an upper bound (EU tasks average 2.96 saturated countries vs the
planet's 2.33).

**So the recorded projection is wrong in the OPTIMISTIC direction:**

| | recorded | measured/modelled | ratio |
|---|---|---|---|
| marker JSON on disk | 14.64 GB | **16.9-17.1 GB** (band to 24.0) | **1.15-1.64x** |
| peak RSS | 23.94 GB | **29.1-29.5 GB** (band to 41.3) | **1.21-1.72x** |

Two error sources: the inflation factor (1.556 recorded vs 1.4846 measured) and,
the larger term, the entry count (14.94M recorded vs 19.27M modelled per array).
The *structure* claim — two parallel arrays at ~314 B/entry compact — is exactly
right; one `routing_groups` entry measured **314 bytes** compact.

At 29 GB the planet marker set is **1.8x a 16 GB runner**, and `plan-reduce`, all
121 reduce jobs and `finalize` each pay it once.

### PR #171's reduce residency bound is correct and is NOT what blocks reduce

Zero jobs completed, so the suffix-union retention set was never exercised. But
its inputs are measured: forward packs 160 total / 16,799,013,377 B / largest
120,400,157 B / **110.98 B per record** (recorded 103.8 — 6.9% optimistic), and
map tasks holding each country `de 15, fr 14, it 13, no 11, es 9, be 8, nl 7,
ch 7, pl 6`. The law `(tasks holding country) x pack bytes` gives a worst EU job
of **15 x 120.4 MB ~= 1.81 GB** against a 24 GiB cap, and a planet worst of US at
39 tasks ~= **4.05 GB**. **#171 bounded the right thing; reduce dies on RAM
0.95 ms before residency matters.**

### BLOCKER C sits BEHIND BLOCKER B

No `.av1` was produced, so the publish tree could not be measured in bytes.
**The dispatch-prerequisite ordering "A+C first, then B" is therefore wrong if the
goal is to measure C** — B has to be fixed first. Projecting C from measured
constants: 412M admitted planet records x 278 B/record (`.av1`, recorded,
unverified) + 31.18 B/record records packs ~= **~128 GB**, inside the recorded
100-145 GB band.

## C. Two production defects the addresses run exposed

### C1. `StageWatchdog.__exit__` destroys the diagnosis it exists to deliver

`__exit__` raises `RuntimeError(self.failure)` only `if exc_type is None`. But
`_abort()` calls `connection.interrupt()`, which makes the guarded DuckDB call
raise **inside** the `with` — so `exc_type` is not None and the message
`"whole-stage RSS exceeded its hard cap"` is discarded. An operator sees only
`_duckdb.InterruptException: INTERRUPT Error: Interrupted!` — no phase, no knob,
no cap value. The comment at `address_construction_v1.py:343` asserts the
opposite ("a fault raised here still surfaces through `__exit__`") and is wrong in
the only case that matters. Recovering that string cost ~2 h of run time.

This is the recorded "the diagnosis that names the knob cannot fire" theme, now
**execution-verified**.

### C2. The reducer-cap gate cannot fire — it is compared against its own default

`construction_v1_hosted.py:1068-1069`:

```python
max_reducers = int(contract.get("caps", {}).get("max_reducers_per_family", REDUCE_MATRIX_CAP))
job_cap = min(REDUCE_MATRIX_CAP, max_reducers)
```

`REDUCE_MATRIX_CAP = 256`, so a contract with no `max_reducers_per_family`
yields `max_reducers = 256` and `job_cap = 256`. Any subsequent
`job_count > max_reducers` check is unfireable, because
`job_count <= job_cap <= max_reducers` holds **by construction**. Measured on the
committed planet inventory:

| contract | `job_cap` | batch | jobs | exit | `fits_reducer_cap` |
|---|---|---|---|---|---|
| cap present (128) | 128 | 6 | **121** | 0 | true |
| **cap absent** | 256 | 3 | **242** | **0** | **true** |

**`predict-reduce` does NOT refuse 242. It accepts it and reports the cap gate as
passing.** `plan-reduce` has the identical shape. Another unguarded call site of
the recorded class — and it means the earlier 3/242 figure was not a harness
artifact that production would have caught; it was a shape **production endorses**
when the cap is absent.

## D. Corrections to figures recorded earlier in THIS file and the follow-ups doc

| claim | recorded | corrected to |
|---|---|---|
| the COPY OOM mechanism | row-count-driven, ~7.8-8.0 GiB at 65.8M rows | **shard-count-driven**: fails at 14.0M rows / 1.75 GB, 0 bytes written, 0 spill; ~1.94 MB pinned per open partition writer |
| head post-merge region 44.2 GB vs 25.6 GB (1.73x) | a blocker | **unreachable and unmeasured** — the phase dies on that region's first statement; memory binds, not disk |
| `max_head_candidate_rows` raise | keep dropped | **must be raised** — 5M aborts a 43.9%-of-planet run at admission (26.2M rows) |
| `shard_bits = 8` / 6 bits as the fix | recommended | **rejected** — encoder cap is a floor; fetch granularity sets 12. Batch the COPY instead |
| DuckDB spill 3.5 GB | attributed loosely | that is the 65.8M-row probe only; **Europe head spilled 0 bytes**, and address reduce spilled 0 |
| files per head partition | 113 | 113 unbatched, **3 (max 4) batched at 256** |
| address marker JSON, planet | 14.64 GB | **16.9-17.1 GB** (band to 24.0) |
| address marker peak RSS, planet | 23.94 GB | **29.1-29.5 GB** (band to 41.3) = **1.8x a 16 GB runner** |
| `predict-reduce` refuses 242 jobs | stated as fact | **FALSE** — it accepts 242 and reports `fits_reducer_cap: true`; the cap must be present in the request for the check to mean anything |
| address publish objects | 65,024 structural / ~41,200 estimated | **7,600-25,000** — saturating 256 buckets needs ~1,400 distinct cells/task; the worst of 41 EU tasks had 104. Addresses is **6-13% of the op cap**, not 66% |
| address forward pack bytes/record | 103.8 B | **110.98 B** (6.9% optimistic) |
| address records-pack bytes/record | 28.3 B | **31.18 B** (10% optimistic) |
| marker `indent=2` inflation | 1.556x | **1.4846x** |
| what blocks address reduce | pack residency | **the marker load, outside the guarded region** — #171 bounded the right thing and it is not the blocker |
| dispatch order A+C then B | recorded ordering | **B must precede C** — C is unmeasurable until B is fixed |

**Object count is not a lever.** With the corrected 7,600-25,000 band, the
packaging work considered and dismissed earlier is dismissed more firmly: it was
weighed against a figure ~5x too high.

## E. Ordered next steps

1. **Shrink the address marker, or stream it.** 99.97% of a marker is
   `packs[*].directory`. Reduce needs only
   `(pack key, group index, country, min/max route_hash)` — a few percent of
   490 B/entry. This is the only thing between here and a *measurable* address
   run, and it unblocks BLOCKER C behind it.
2. **Move `run-reduce`'s marker load inside the guarded region**, or the guard
   keeps reporting a cap breach it cannot attribute.
3. **Fix `StageWatchdog.__exit__`** so an interrupt-induced exception chains
   rather than discards `self.failure`. (C1)
4. **Make the reducer-cap gate fail closed** in both `predict-reduce` and
   `plan-reduce` when `max_reducers_per_family` is absent. (C2)
5. Places: land the COPY batching + the cap raise (PR #176), then re-run the
   Europe head phase — it costs ~40 s against the preserved work dir.
6. Places `_reduce_ingest` release fix, and record that
   `max_reducers_per_family = 128` is a memory-safety bound.

Both work trees, all markers, all staging and both venvs are preserved. Resume
commands are in §5 and in the per-run reports; the address run additionally holds
`load-markers-measurement.json`, `marker-fanin-analysis.json`,
`watchdog-abort.jsonl` (the captured diagnosis), `planet-inventory-2026-07-22.0.json`
(the real 126-task planet plan) and all 26 reduce tracebacks under
`/home/brad/dev/wt-176-europe-addr-logs/`.

## F. Post-merge Europe Places rerun — head passes completely

Added after PR #176 merged as `1500e30`. The preserved worktree was advanced to
that exact commit and the harness ran `--phases contract,head`. Re-deriving the
contract retained request SHA
`d555b55a2cb427c6477453050a734f04b4ce708b3a0b9d305456b54f913f77ed`,
so all existing staging and reductions were reused.

Head completed through the batched write, all 4,096 encodes and all verifications:

| measurement | result |
|---|---:|
| return code | **0** |
| populated / configured shards | **4,096 / 4,096** |
| wall time | **2,022.27 s (33.70 min)** |
| peak RSS | **8,179,167,232 B** |
| peak sampled runner disk | **5,399,313,835 B** |
| peak head scratch | **5,399,313,835 B** |
| peak local staged cache | **855,605,976 B** |
| staged input hydrated / released | **3,088,544,880 / 3,088,544,880 B** |
| staged objects published | **4,098** |
| staged bytes published | **2,134,262,243 B** |

No RAM, disk, timeout or cap guard fired. The original 36-second DuckDB OOM is
closed by execution at Europe scale. The earlier "~40 s" rerun estimate described
only the formerly failing statement; the complete 4,096-shard encode/verify tail
makes the full head a ~34-minute phase at this scale.

The run does not by itself prove the planet's projected ~18.5 GB filesystem peak
fits the 17 GiB contract cap. It does remove the measured Europe blocker and makes
that residual a planet-preparation/authorized-run gate rather than a reason for
another design or review cycle.

## G. Live R2 publication probe — remote semantics pass

Added after PR #178 merged as `0089145` and PR #181 added the manual probe as
`82b4731`. GitHub Actions run
[`30203859256`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30203859256)
completed successfully in 29 seconds on `ubuntu-24.04`.

The workflow selected the same persistent botocore backend that hosted finalize
uses and touched one run-unique key:

```
construction-v1/probes/r2-publication/30203859256-1.bin
```

The payload was non-empty and larger than the publisher's 1 MiB streaming chunk:

| measurement | value |
|---|---:|
| bytes | **1,054,237** |
| SHA-256 | `e0bd1876e92481eb2b5d423733b79f05ab1f2e9bbc71079d63a0c35e249545b4` |
| content MD5 / R2 single-part ETag | `fab1e003082949d5194bb964294041a3` |

The live assertions all passed:

1. `PutObject` with `IfNoneMatch: "*"` created the absent key.
2. `HeadObject` returned the admitted length and SHA-256 metadata, and its
   single-part ETag equalled the MD5 of the bytes sent.
3. A fresh identical retry received the create-only conflict and was accepted
   only after a full byte-exact read-back.
4. A same-length, one-byte-different retry received the conflict and was
   rejected as a conflicting immutable object.
5. The unconditional cleanup deleted that exact key; a subsequent exact-prefix
   listing returned zero objects.

This closes the remote create-only, framing, ETag/content, and resume-conflict
questions that the local real-botocore stand-in could not answer. It does not
measure many-object throughput; that remains part of the resumed Europe
Addresses finalize after marker fan-in is fixed.

## H. PR #182 and both preserved Europe builds complete end to end

Added after PR #182 merged as `94eae08`. No planet workflow was dispatched.
Temporary harness copies pointed at the merged main checkout so the old
instrumented Address worktree and all preserved inputs remained untouched.

### H1. Compact Address projections

The 41 full Address markers totalled 7,799,189,884 bytes and described
151,371,029 records. `plan-reduce` completed in 128.56 seconds at
1,949,052,928 bytes peak RSS and emitted:

| artifact | bytes | SHA-256 |
|---|---:|---|
| plan | 99,749 | `f5d875ddc30bddf4a2ccc027c5e4e3dcdab2e57e8d2a579fa291b26bc265333e` |
| reduce SQLite projection | 667,648 | `db5882164ca33ab388b67cbb35df9c1a76a9ebfed79ea0a178509a0aad898639` |
| finalize identity projection | 586,797 | `e3248437c9a13922f27a2dc47f58e7fb497db0102b67e0d1ad46f3b2df946a8d` |
| core plan payload | 1,363,843 | composed from the three artifacts plus matrix |

The plan was byte-identical to the previously preserved plan. Its compact
representation covered 160 packs, 2,417 row groups, 2,520 exact
row-group/country envelopes, 2,466 per-record object identities, 204
partitions, and 102 two-partition reducer jobs.

All 102 jobs completed with no failure or retry while four local workers ran
concurrently. Each job had its own process-tree and scratch/store sampler, which
matches the hosted one-job-per-runner unit:

| reducer measurement | observed maximum |
|---|---:|
| peak RSS per job | **2,263,089,152 B** |
| peak sampled scratch + store per job | **3,309,679,666 B** |
| wall time per two-partition job | **34.75 s** |

All 204 partition bindings verified, and each job released every hydrated pack.

Address finalize completed in 265.13 seconds at 75,907,072 bytes peak RSS. It
hydrated and released 5,340 staged reads / 94,218,506,354 bytes, reconciled the
exact set, and wrote the marker last:

| Address publication | result |
|---|---:|
| serving objects | **204** |
| per-record objects | **2,466** |
| finalizer manifests | **2** |
| exact-set members | **2,672** |
| files including marker | **2,673** |
| bytes including marker | **47,110,551,015** |
| reconciles / marker last | **true / true** |

This closes the old 13.45 GB Europe marker OOM, the projected 29 GB planet
marker RSS blocker, and the formerly hidden Address publication-size gate.

### H2. Places finalize

The preserved Places map, plan, all 128 reducer jobs, and 4,096-shard head were
reused. On the same merged `94eae08`, finalize completed in 136.37 seconds at
247,562,240 bytes peak RSS. It hydrated and released 41,130 staged reads /
42,060,324,048 bytes and wrote:

| Places publication | result |
|---|---:|
| serving objects | **12,109** |
| positions objects | **8,456** |
| finalizer manifests | **2** |
| exact-set members | **20,567** |
| files including marker | **20,568** |
| bytes including marker | **21,039,995,295** |
| reconciles / marker last | **true / true** |

Together with §F's complete head result, Places has now completed every local
Europe phase through exact publication on merged main.

### H3. Operational conclusion

Both families have passed rung 3 of the verification ladder at Europe scale,
and the live R2 probe in §G passed rung 4 semantics. There is no remaining
measured Europe-scale code blocker. The next forward action is a planet run
sheet—fresh prediction/admission output, exact confirmation, cost and runner
ceilings, family order, and the Places head residual—followed by an explicit
operator decision. Agents must not dispatch it themselves.

## I. First hosted planet Address completion

The operator supplied the exact execute confirmation for request
`88b7f17149fd5d75bf64720f0640d2cbe8aeb5ead750d279c1881f9bd5332614`,
max parallel 4, and a 40,000 runner-minute ceiling. Both runs below used the
request-pinned producer `63b7f71398eb4f50cfbe937314eb2abc6cb342bd` and a
non-promoting, request-derived R2 namespace.

### I1. The first execute measured one workflow transport defect

Run `30207544725` completed all 127 Address map tasks and the stable plan, then
every reducer failed before data work because the downloaded `cv1-plan`
artifact did not contain `control/contract.json`. The plan artifact did contain
the authenticated ledger, compact projections, and plan. It recorded 451
consumed runner-minutes for the same request and family.

PR #183 (`40a7682`) fixed only that measured boundary:

- `cv1-plan` now carries `control/contract.json`;
- a fresh dispatch can resume from either a final ledger or an exact-name
  `cv1-plan` artifact;
- admission verifies the prior ledger's request and family before carrying its
  consumed total forward; and
- the request hash remains unchanged, so the immutable R2 staging prefix and
  completed map outputs remain reusable.

### I2. Resume run `30215529919` completed

The fresh dispatch admitted the same request and family, carried forward exactly
451 runner-minutes, and completed in 17,441 seconds wall time:

| stage | result | measured duration |
|---|---:|---:|
| admission | passed | 26 s |
| map | 127/127 reused, no failure | 83–267 s/job; median 142 s |
| plan | 117 reducer jobs | 537 s |
| reduce | 117/117, no failure | 83–662 s/job; median 255 s; p90 385 s |
| Address head | expected no-op, passed | 30 s |
| finalize | passed | 4,029 s |

Map task 0 proved the resume behavior directly:
`already-completed=true marker_republished=true elapsed=5s`. The job still
performed checkout, dependency setup, disk cleanup, and a release Rust build
before that five-second check.

Reducer transport and residency were bounded:

| reducer measurement | planet result |
|---|---:|
| staging bytes hydrated, all jobs | **314,240,107,255 B (292.66 GiB)** |
| largest job hydration | **6,519,238,407 B** |
| largest staged-cache residency | **4,462,286,106 B** |
| total GitHub job duration | **544 runner-minutes** |

Finalize then hydrated 21,858 staged objects, reconciled the exact set, verified
the published binding, and wrote the marker last:

| Address publication | result |
|---|---:|
| serving objects | **581** |
| per-record position objects | **10,348** |
| finalizer manifests | **2** |
| verified objects | **10,931** |
| reconciles | **true** |
| completion marker | `construction-v1/86558218e2b67db0e0249abbee0c6d17650dea43467ed14c59789bc60c7bacb0/markers/finalize/addresses.json` |

This is the first complete hosted, non-promoting planet Address build. It closes
the planet Address marker fan-in, selective reducer transport, R2 fleet
throughput, bounded finalization, exact-set verification, and marker-last gates
for this release.

### I3. Measured follow-ups, deferred until both planet families complete

These are not blockers for the active Places run:

1. **Check durable map markers before expensive setup/build.** The resume map
   command took five seconds, but 127 complete jobs consumed 296 runner-minutes
   because the check follows setup, disk cleanup, and compilation.
2. **Build the pinned Rust binaries once per architecture.** Maps and reducers
   rebuild the same release binaries on every fresh runner. A verified workflow
   artifact would remove repeated compilation while preserving the
   request-pinned producer.
3. **Use fewer, larger Address reducer jobs.** The existing `max_reduce_jobs`
   knob can lower fan-out without loosening caps. The 117-job run hydrated
   292.66 GiB while the median total job duration was only 255 seconds, leaving
   substantial room under the 90-minute reduce bound. A bounded probe near 60
   jobs should measure the expected reduction in overlapping pack reads before
   changing a default.
4. **Make runner-minute accounting complete.** GitHub job durations for the
   resumed run sum to about 917 runner-minutes, while its ledger appended 613.
   The fragments time map/reduce command bodies, and plan, head, finalize, plus
   setup/build are absent. The final ledger reports 1,064 only after adding the
   prior ledger's 451. This did not approach the operator's 40,000-minute cap,
   but it is not the whole runner cost a future tight authorization would need.
5. **Profile finalization's deliberate second read.** The 10,929 staged
   serving/position inputs were hydrated exactly twice (21,858 reads) before
   10,931 objects including manifests were verified. The phase completed inside
   its 48–208 minute projection. Any optimization must retain independent
   whole-slice identity verification rather than merely removing the second
   pass.

### I4. Places dispatched

After the Address evidence reconciled, the already-authorized Places planet run
`30226086949` was dispatched from main workflow SHA `40a7682`. Fail-closed
admission passed for the same request with prior runner minutes 0 and projected
the next phase at 60/40,000 minutes. The run created 89 map jobs at concurrency
4. Its remaining measured gates are the planet global head disk residual and
Places R2 fleet throughput.

### I5. Places maps and reducers pass; the head measures a spill sub-cap

Run `30226086949` completed all 89 map jobs, planning, and all 128 reducer jobs.
The global head then failed in `_tree_merge_head_candidates` before sharding:
DuckDB could not offload another 128 KiB block because
`max_temp_directory_size` had reached 4.2 GiB. The ledger was at 801 minutes
with a projected 891/40,000 before the head command.

This is narrower than the projected whole-head disk residual. The job did not
hit runner ENOSPC, RSS, R2 transport, or the independent 17 GiB
workspace-plus-cache watchdog; it exhausted the generic DuckDB quarter-share
inside that budget. The scoped correction allocates half of the same 17 GiB to
head spill while retaining the total-stage guard and 25.6 GB free-disk floor.
Because every earlier object is immutable in the same request-derived staging
namespace, the next execution is a fresh-dispatch resume of `30226086949`, not
a rerun and not a new request.
