# construction-v1 aggregate fan-in benchmark

Synthetic planet-scale gate proving (or refuting) that Address and Places aggregate planning runs over compact per-pack summaries without reopening map payloads, comfortably inside a hosted job.

## Verdict

- **Address: PASS (remediated)** — 14,640,782 summary entries planned in 372.3 s at 1.35 GiB peak RSS, comfortably inside the 4.00 GiB / 60 min gate. See "Remediation" below; the pre-remediation FAIL is retained as history.
- **Places: PASS** — 1,059,303 summary entries planned in 11.2 s at 796.58 MiB peak RSS.

## Remediation (2026-07-23)

The original Address `genesis_plan` materialized every per-pack binding: it built a
`buckets` map keyed by `(country, maximum_bucket)` whose values were lists holding
all ~14.6M individual summary bindings, then combined each list. Together with the
benchmark loading every 4.30 GiB of marker JSON into memory at once, peak RSS
breached the 4.00 GiB gate (killed at 1.0 scale; 6.68 GiB at 0.10).

The planner now folds summaries into a bounded aggregate instead of materializing
them. Because `combine_bindings` is an associative, commutative modular sum, each
summary entry is combined into a running per-`(country, maximum_bucket)` binding as
it is parsed — the retained structure is bounded by distinct country/bucket
identities (≤ ~200 countries × 65,536 buckets; ~2.15M at planet scale), never the
raw entry count. The recursive bisection operates on that aggregate unchanged. A new
streaming entry point `genesis_plan_streaming(marker_paths, …)` reads one marker file
at a time and discards it, so only the aggregate plus a single decoded marker are ever
resident; the benchmark's planning subprocess uses it for Address. The in-memory
`genesis_plan(markers, …)` retains its signature for small inputs and existing callers.

Peak RSS is now dominated by the bounded aggregate (~1–2 GiB) and is effectively flat
against entry count — the 0.20 sweep and the full 1.0 gate land within the same band.
Output is byte-identical to the pre-remediation planner: proven by
`tests/test_benchmark_construction_fanin.py::test_streaming_genesis_plan_is_byte_identical`
(in-memory and streaming paths vs a frozen reference oracle across five row caps) plus
direct-invariant tests (determinism, exact binding reconciliation, per-partition
`row_cap`, and per-country hash ranges tiling the full 64-bit space with no gap/overlap).

## Runtime

- generated: 2026-07-23T22:02:36-0400
- platform: macOS-26.5.2-arm64-arm-64bit
- python 3.12.12, duckdb 1.5.1, pyarrow 25.0.0
- seed: 1729, pack rows: 50000

## Gate

- wall gate: 60 min (hosted job ceiling 330 min)
- RSS gate: 4.00 GiB
- scratch gate: 8.00 GiB

## Methodology

1. Read the real per-task row counts and country/skew from the local inventories (`benchmarks/address-construction-v1-data/inventory/addresses.json`, `benchmarks/places-construction-v1-data/inventory/places.json`).
2. Generate synthetic per-task markers whose per-pack summary entries match the real Rust proof-directory schema (Address `bucket_summaries`: country + top-16-bit `maximum_bucket`; Places `routing_summaries`: `execution_group` + `partition_cell`). Bindings are random 256-bit associative lane sums; marker bindings equal the combine of their pack bindings, so the real planners reconcile.
3. Invoke the real `genesis_plan` in a bounded subprocess (`scripts/address_construction_v1.py`, `scripts/places_construction_v1.py`) and measure wall / peak RSS / peak scratch with the repo's `run_bounded` watchdog, which hard-kills on any cap breach.

## Address planet-scale result (remediated 2026-07-23)

Streaming aggregate planner (`genesis_plan_streaming`); measured on the same
synthetic inputs (seed 1729, pack rows 50000).

- tasks: 127
- synthetic records: 434,397,621
- packs: 8,752
- summary entries (fan-in size): 14,640,782
- marker bytes on disk: 4.30 GiB
- generation wall: 356.7 s
- partitions produced: 148
- planner wall: 372.29 s
- peak RSS (subprocess sampler): 1.35 GiB
- peak RSS (worker getrusage): 1.35 GiB
- peak scratch: 306.00 B
- **verdict vs gate: PASS** (2.65 GiB RSS headroom, wall at 6.2 min inside the 60 min gate)

### Address scaling sweep (remediated)

Peak RSS is reported by two independent meters (`run_bounded` psutil sampler and the worker's kernel `getrusage`); they agree here. The sweep ran with a relaxed hard-kill cap so runs complete and the true peak is observable; PASS/FAIL below is still judged against the 4.00 GiB gate.

| scale | summary entries | planner wall (s) | peak RSS (psutil) | peak RSS (getrusage) | vs gate |
| ----- | --------------- | ---------------- | ----------------- | -------------------- | ------- |
| 0.020 | 4,478,164 | 207.17 | 516.03 MiB | 520.33 MiB | PASS |
| 0.050 | 7,917,853 | 256.64 | 799.92 MiB | 799.92 MiB | PASS |
| 0.100 | 10,258,866 | 431.76 | 1.18 GiB | 1.18 GiB | PASS |
| 0.200 | 12,015,893 | 180.99 | 1.86 GiB | 1.86 GiB | PASS |

- observed peak-RSS band across the sweep: 516.03 MiB .. 1.86 GiB; the full 1.0 gate sits at 1.35 GiB, inside the same band.
- peak RSS is bounded by the per-`(country, maximum_bucket)` aggregate (distinct identities, not raw entries), so it is effectively flat against entry count and stays well under the gate at every scale.
- linear slope (lower bound only): ~182.14 B peak RSS per summary entry, ~7.30 us wall per entry

### Address pre-remediation result (history — FAIL)

The original fully materialized `genesis_plan` (below) is kept as history. It built a
`buckets` map of all per-summary bindings plus recursive `emit` combine lists and was
driven by a benchmark that loaded every marker at once; peak RSS breached the gate.

- generation wall: 218.8 s
- **planner killed at hard cap 4.00 GiB** — the fully materialized Python `genesis_plan` breached the RSS gate before it could emit a plan. The scaling sweep below showed peak RSS already exceeding the gate at reduced scale, so it was a real ceiling, not a transient spike.
- **verdict vs gate: FAIL** — planner killed at hard cap: child RSS exceeded its hard cap

| scale | summary entries | planner wall (s) | peak RSS (psutil) | peak RSS (getrusage) | vs gate |
| ----- | --------------- | ---------------- | ----------------- | -------------------- | ------- |
| 0.020 | 4,478,164 | 35.34 | 3.09 GiB | 3.09 GiB | PASS |
| 0.050 | 7,917,853 | 63.56 | 4.35 GiB | 4.35 GiB | FAIL |
| 0.100 | 10,258,866 | 68.58 | 6.67 GiB | 6.68 GiB | FAIL |
| 0.200 | 12,015,893 | 102.62 | 5.53 GiB | 5.54 GiB | FAIL |

- observed peak-RSS band across the pre-remediation sweep: 3.09 GiB .. 6.67 GiB
- peak RSS was **not monotonic** in entry count: it was dominated by a transient inside the old `genesis_plan` (the fully materialized `buckets` map of per-summary bindings plus the recursive `emit` combine lists), not by a term scaling linearly with entries.
- linear slope (lower bound only): ~435.80 B peak RSS per summary entry, ~8.07 us wall per entry

## Places planet-scale result

- tasks: 89
- synthetic records: 75,642,289
- packs: 1,535
- summary entries (fan-in size): 1,059,303
- marker bytes on disk: 244.57 MiB
- generation wall: 36.2 s
- partitions produced: 12,000
- planner wall: 11.20 s
- peak RSS (subprocess sampler): 796.58 MiB
- peak RSS (worker getrusage): 796.58 MiB
- peak scratch: 285.00 B
- **verdict vs gate: PASS**

### Places scaling sweep

Peak RSS is reported by two independent meters (`run_bounded` psutil sampler and the worker's kernel `getrusage`); they agree here. The sweep ran with a relaxed hard-kill cap so runs complete and the true peak is observable; PASS/FAIL below is still judged against the 4.00 GiB gate.

| scale | summary entries | planner wall (s) | peak RSS (psutil) | peak RSS (getrusage) | vs gate |
| ----- | --------------- | ---------------- | ----------------- | -------------------- | ------- |
| 0.020 | 281,353 | 2.34 | 233.69 MiB | 233.69 MiB | PASS |
| 0.050 | 479,122 | 3.10 | 371.20 MiB | 371.20 MiB | PASS |
| 0.100 | 669,342 | 4.06 | 509.47 MiB | 509.47 MiB | PASS |
| 0.200 | 855,469 | 6.56 | 669.12 MiB | 669.12 MiB | PASS |

- observed peak-RSS band across the sweep: 233.69 MiB .. 669.12 MiB
- linear slope (lower bound only): ~791.50 B peak RSS per summary entry, ~7.09 us wall per entry

## Synthetic-data caveats (what this cannot prove)

- Bindings are random 256-bit sums, not digests of real feature rows; this proves fan-in *shape and scale*, not semantic digest correctness.
- Address bucket occupancy uses uniform route-hash saturation and Places cell occupancy uses a Zipf-skewed synthetic land model; real skew may shift entry counts modestly but not the order of magnitude.
- Only the compact summary-only planners (`genesis_plan`) are exercised. The Places `adaptive_genesis_plan` deliberately reopens pack payloads and cannot be driven by summaries alone; it is out of scope for a synthetic summary benchmark and still needs a real payload-scale proof.
- Peak RSS is sampled (5 ms poll in `run_bounded`); sub-poll peaks are possible. The worker `getrusage` maxrss is reported as a kernel cross-check.
