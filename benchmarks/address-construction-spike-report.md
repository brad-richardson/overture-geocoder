# Address Rust + DuckDB construction spike

Date: 2026-07-22

## Result

The architecture is directionally validated on the strongest checked-in real
Address sample, but planet-scale performance is unproven. The sample has 17,014
rows and DuckDB was 1.4.4 rather than the required 1.5.1.

Input:

```text
exports/experiment/addresses-raw.parquet
bytes: 454897
sha256: ec396f79bc571f9a60694aca06703a3ef3377a5e63567722fdc7bcecd1783419
rows: 17014
```

Command:

```text
python3 scripts/spike_address_construction.py \
  --input exports/experiment/addresses-raw.parquet \
  --experiment-input \
  --binary crates/target/release/address-construction-spike \
  --output-dir /tmp/address-construction-spike-20260722-v4 \
  --allow-unpinned-duckdb
```

The experiment sample predates the projected global schema. The harness adapts
it with Arrow/NumPy column buffers only: no feature dictionaries, tuples,
`to_pylist`, or `from_pylist` enter the candidate path.

Recorded runtime: macOS 26.5.2 arm64, Python 3.12.12, PyArrow 23.0.0, DuckDB
1.4.4, and Rust 1.97.1. The release binary SHA-256 was
`9e733338c6ac835c1c75c564d5fe94c66b35dbca6a7c3ba1a859d6a9ae22c237`.

## Measured evidence

| stage | result |
|---|---:|
| columnar hydration/adaptation | 0.874 s |
| hydrated Arrow IPC | 2,831,824 bytes |
| Rust transform internal | 0.338 s |
| Rust process peak RSS | 41,140,224 bytes |
| Rust transform wall (including RSS polling) | 0.390 s |
| transformed Arrow IPC | 4,968,520 bytes |
| DuckDB materialize (two threads) | 0.193 s |
| DuckDB ordered pack + summary export (one thread) | 0.068 s |
| admitted / rejected | 17,013 / 1 |
| pack bytes | 1,569,896 |
| summary bytes | 6,879 |
| DuckDB scratch after close | 0 bytes |
| disposable Python-row reference | 2.964 s |

The Python-row reference used the same frozen Arrow IPC input and produced one
sorted typed Parquet. It omitted the old spill merge, summaries, manifests, and
multi-pack planning, so the comparison favors the old path. It exists only to
show the size of the row-object cost; it is not a compatibility oracle.
Local timings varied materially across repeated runs, so only this final
recorded run is reported and no extrapolated planet throughput is claimed.

Two complete candidate constructions had identical transformed Arrow IPC,
logical two-lane sums, rejection counts, pack SHA-256, and summary SHA-256:

```text
pack sha256:    03d4e286b2cc5bb3f8fb98b15bde954700f4ddf12b8c950aee353ab1f8701b33
summary sha256: e40107627349d1de91a5063d5a2948ce69d5a1312c93fd5a675f40fde8acf3c4
lane A:         fdad80c4c665893a45aad623670f46a0177d4057c5c5ef235f468e5cd566f29c
lane B:         b68d99151e1f9f3c09bdade59b8511d9863170b1d7671e97e6f1337df7f4c8ab
```

## Limits of this result

- DuckDB 1.4.4 makes this non-acceptance evidence; 1.5.1 remains the pinned gate.
- 17,014 rows cannot establish near-cap runtime, RSS, scratch, skew, or IPC cost.
- The checked-in sample has one row group and required a benchmark-only schema
  adapter.
- Hydration/adaptation and Arrow IPC writing were timed together, so IPC-only
  overhead is not isolated.
- Peak RSS covers the Rust child only; whole construction/DuckDB peak RSS still
  needs the frozen near-cap harness.
- The Python comparison is not the complete legacy construction path.
- No source network, R2, workflow, reducer, serving encoder, Worker, or
  publication behavior was exercised.
- The spike summary contains route counts only. Exact row-group count plus two
  digest-lane summaries remain checkpoint 2 work.

The next benchmark must freeze exact representative, near-cap, and worst-skew
task identities before execution and record normalized peak RSS, scratch,
remaining disk, output, and wall time against fixed caps.
