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
