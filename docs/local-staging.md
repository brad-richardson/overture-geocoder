# Local planet staging

A machine with real RAM and cores is a better staging rung than a CI runner for
everything except producing promotion evidence. This is how to stand it up.

## Why

The measured blocker it removes is not wall time, it is memory. v4's head merge
died at 79% on 8.5 GiB of DuckDB spill and needed a scoped resume at 13.69 GB.
A workstation with 62 GB does not have that problem, so a head build that
cannot complete in CI can complete here.

The rung ladder in `CLAUDE.md` §3 goes 13-second slice -> planet run, with
nothing in between. This is the missing middle.

## Cost

The whole planet is small, because Overture ships zstd parquet:

| theme | compressed |
|---|---|
| places | 10.5 GiB |
| divisions | 5.2 GiB (division + division_area) |
| addresses | 20.4 GiB |

Working space is the real cost, and it scales off records, not source bytes.
The Europe run in `/home/brad/dev/wt-176-europe-work` covers 32.56 M of the
planet's 74.22 M places -- 43.9% -- in 66 GB, so planet Places staging projects
to roughly **150 GB**.

## Setup

Python must be 3.11 to match CI. The hash-pinned requirements are cp311 wheels
and `--require-hashes` rejects anything else, so 3.12 fails to install rather
than failing at runtime.

```bash
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python --require-hashes \
    -r .github/requirements-hosted-rowgroup.txt
uv pip install --python .venv/bin/python \
    pytest==9.1.1 pytest-xdist==3.8.0 pyyaml==6.0.3 h3==4.3.1 \
    shapely==2.1.2 psutil==7.2.2 requests
```

That pins `duckdb==1.5.1`, which is required: 1.5.5 moves address pack bytes, so
the version is rebuild-scoped rather than cosmetic. Verify with
`.venv/bin/python -m pytest tests/ -q` -- 1798 passed, ~25 s.

Note `unicodedata2==17.0.0` in the pinned set. The tokenizer contract needs a
Unicode version newer than the stdlib's, and tokenizer tests fail against
stdlib `unicodedata` alone.

## Mirroring the source

Download into a plain tree, then present it bucket-shaped via symlinks:

```bash
D=/home/brad/dev/overture-local/2026-07-22.0
for t in "theme=places/type=place" "theme=divisions/type=division" \
         "theme=divisions/type=division_area"; do
  aws s3 sync --no-sign-request \
    "s3://overturemaps-us-west-2/release/2026-07-22.0/$t/" "$D/$t/"
done

M=/home/brad/dev/overture-local/mirror/overturemaps-us-west-2/release/2026-07-22.0
mkdir -p "$M/theme=places" "$M/theme=divisions"
ln -sfn "$D/theme=places/type=place"          "$M/theme=places/type=place"
ln -sfn "$D/theme=divisions/type=division"      "$M/theme=divisions/type=division"
ln -sfn "$D/theme=divisions/type=division_area" "$M/theme=divisions/type=division_area"
```

Then set `OVERTURE_SOURCE_MIRROR` to the mirror root:

```bash
OVERTURE_SOURCE_MIRROR=/home/brad/dev/overture-local/mirror \
  .venv/bin/python scripts/build_slice_inventory_v1.py \
    --release 2026-07-22.0 --bbox 7.36 43.71 7.47 43.78 --output slice/inventory.json
```

## What the mirror does and does not change

It swaps the byte transport and nothing else.

- **Listing still goes to S3.** Which objects exist, their sizes and their
  etags stay authoritative, so a stale or partial mirror fails loudly on read
  instead of silently planning over a different world.
- **URIs stay canonical `s3://`.** `approved_prefix` / `is_approved_source_uri`
  keep working unweakened, and the recorded `inventory_sha256` is unchanged.

That last point is the integrity check, and it is worth running whenever the
mirror is rebuilt. Measured on Monaco Places (task 33) against 2026-07-22.0:

```
S3     inventory_sha256 db2a6430...9d1615   6.58 s
mirror inventory_sha256 db2a6430...9d1615   0.52 s   (artifacts diff clean)

full five-phase run, 63 published serving artifacts:
  S3     sha256 of the digest list  8d421843...02beac
  mirror sha256 of the digest list  8d421843...02beac
```

**Compare the inventory digest and the serving artifacts. Do not compare map
bytes.** The intermediate `map/places-v1` class is not byte-stable: two
consecutive pure-S3 runs of the same task published 25,876,445 and 25,876,446
bytes, and the mirror 25,876,443. That variance is inherent to the artifact and
independent of transport, so it proves nothing either way. Everything that
matters is stable across all three runs -- `serve/places-v1` 34,793,545,
`reduce/places-v1` 16,604,807, the staging-prefix digest, `records` 38,182 and
every term-row count.

If the inventory digest or the serving artifacts ever differ, the mirror is
wrong and the run must not be trusted.

## Discipline

Local runs are for experimentation. Evidence intended for promotion still comes
from the sanctioned path -- the evidence specs carry sha256 pins, and a fast
local result must not become a promotion artifact by accident.

## Scope

`OVERTURE_SOURCE_MIRROR` is wired into `scripts/build_slice_inventory_v1.py`.
Other entry points still construct their own `S3FileSystem` and would each need
the same `source_filesystem()` seam; do that when a specific one is needed
rather than pre-emptively.

## Measured: a complete planet Places run, 2026-07-22.0

First end-to-end local planet run, 2026-08-04, on 20 cores / 62 GB / 1.8 TB.

| phase | wall | note |
|---|---|---|
| map | 34.7 m | 74,223,561 records, 6 workers — reconciles with the inventory total |
| plan-reduce | 7.3 m | 16,723 partitions in 128 jobs, peak resident 0.29 GB |
| reduce | 24.9 m | 4 workers |
| head | 58.4 m | 4,096 of 4,096 shards populated |
| **total** | **~2 h 5 m** | 218 GB of working space at peak |

Head result:

```
populated_shards            4,096 / 4,096
input_candidate_rows        63,132,306
staged_bytes_published      5,538,884,730   (5.54 GB)
staged_objects_published    4,098
staged_peak_resident_bytes  1,465,524,148   (1.47 GB)
staged_objects hydrated/released  88 / 88
```

### The head merge does not fit on a hosted runner

This is the finding the run existed to produce. The tree merge completed with:

```
stage places.head.tree_merge   stage_failed false
peak_duckdb_temp_bytes         9,928,540,160   (9.93 GB)
```

The hosted allowance is `HOSTED_MAX_SCRATCH_BYTES` (17 GiB) divided by four, so
**4.56 GB. The merge needs 2.18x that.** With the hosted cap in place the run
fails as `failed to offload data block ... set by the max_temp_directory_size
setting`, which is v4's failure — v4 died at 79% on 8.5 GiB of spill, and 79%
of 9.93 GB is ~7.8 GB, so the two agree from independent directions.

Raising the cap is not available on a runner. Feeding DuckDB 9.93 GB needs
`max_scratch_bytes` around 37 GiB, and the runner's free-disk floor is ~25.6 GB
— there is no value both large enough to work and small enough to be true. The
17 GiB constant is not a conservative choice that can be relaxed; it is already
near that machine's ceiling.

So the planet head build at `shard_bits=12` is structurally too big for a
standard runner, and the options are a larger runner, an algorithmic change to
the merge, or running it here. That is a different problem from "flaky, needs a
resume", which is how it had been treated.

Two things this run also shows working: the staging transport hydrated all 88
map packs and released all 88, holding a peak of 1.47 GB against 7.44 GB
hydrated; and `--max-scratch-gib` is an explicit local override, so a local run
reproduces the hosted failure by DEFAULT rather than silently diverging.

### An open question for the release move

2026-07-22.0 carries 74,223,561 places against 2026-06-17.0's 75,642,289 --
1.4 M FEWER -- yet its head is 5.54 GB against production's 5.14 GB, about 7.7%
LARGER. Fewer places and a bigger head is not self-explanatory and should be
understood before the release move is committed to, not after.
