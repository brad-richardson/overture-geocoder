# Address construction v1 checkpoint 2

Date: 2026-07-22

Status: complete local vertical slice; not approved for planet mapping

## Outcome

The checkpoint-1 spike is now reusable local Address construction machinery:

```text
bounded PyArrow subprocess
  -> Arrow IPC
  -> address-transform-v1
  -> DuckDB fixed-row pack plan + ordered single-threaded Parquet COPY
  -> address-proof-directory
  -> immutable local objects
  -> completion marker last
  -> genesis prefix plan
  -> selective overlapping-row-group reads
  -> selected/discarded proof reconciliation
  -> address-serving-encode-v1
  -> address-serving-verify-v1 + dormant Worker decoder
```

The Rust construction package is `geocoder-construction`. Python owns bounded
columnar hydration and compact control/proof metadata only. The candidate path
does not turn feature rows into Python dictionaries or tuples and does not use
Python sorting or Parquet writing. The legacy mapper and unshipped serving
formats are unchanged and have no compatibility role.

No workflow, R2, credentials, deployment, publication, retention, cleanup,
commit, or push occurred.

## Proven locally

- UUID input is restricted to trimmed canonical hyphenated or 32-hex text.
- Source object, row-group, and row-index bounds are checked against the pinned
  per-object limits before admission.
- Oversize records have a nonzero rejection fixture; a wrong Arrow field type
  is task-fatal rather than a row rejection.
- Fixed-count pack IDs derive only from the unique total order and configured
  row target. Each explicit pack COPY is one-threaded and order-preserving.
- Rust recomputes pack, row-group, and maximum-bucket count plus both additive
  SHA-256 lanes from the sorted rows actually written. Footer statistics are
  not accepted as proof.
- Content-addressed local objects are create-only. A temporary file is fsynced
  and atomically linked as the completion marker only after every immutable
  object verifies.
- An injected interruption after object construction leaves no marker. Retry
  reuses identical immutable objects and publishes the marker. A subsequent
  call admits and validates the marker before attempting to open deliberately
  nonexistent source paths.
- The genesis planner uses no predecessor and splits only by the approved high
  16 route bits. The ceiling remains disposable construction-v1 state.
- Both partitions in the fixture fetch the same physical Parquet row group.
  They independently recompute its proof, select their ownership, bind the
  discarded complement, and prove selected + discarded = fetched.
- Final reduction rejects missing, extra, duplicate, or mismatched inputs and
  proves that every map row is emitted exactly once.
- The new `.av1` artifact is indexed by full route hash and preserves exact-key
  multiplicity. Its independent Rust verifier reparses every record and
  recomputes routing, total order, and semantic bindings.
- A fixed artifact emitted by the construction encoder is accepted by the
  dormant Worker decoder. The structured query returns both UUIDs sharing the
  exact normalized key.

## Final bounded rehearsal

The final rehearsal used three admitted rows: two duplicate-key rows with
different UUID/provenance and one row with a different number. It used test-only
caps of 100 input rows, 10 rows per pack, 1 GiB RSS, 512 MiB scratch, 128 MiB map
output, 16 MiB serving output, 60 seconds per stage, 256 MiB DuckDB memory, and
two construction threads. DuckDB export was forced to one thread.

Observed values from the preserved `/tmp` evidence:

| item | observed |
|---|---:|
| hydration wall / sampled RSS | 0.184 s / 59,949,056 B |
| transform wall | 0.011 s |
| DuckDB construction wall / process RSS | 0.017 s / 162,004,992 B |
| map immutable output | 6,655 B |
| packs / row groups | 1 / 1 |
| genesis partitions | 2 |
| partition selected/discarded rows | 1/2 and 2/1 |
| serving artifacts | 320 B and 596 B |
| query matches | 2 |

The very short Rust/proof stages can finish between 5 ms RSS polls, so their
sampled child RSS is not acceptance-quality evidence. The whole DuckDB process
and isolated hydration measurements are the meaningful local peaks. All values
are far below the deliberately generous fixture caps; none sets a planet cap.

The local environment has DuckDB 1.4.4. Production defaults fail closed unless
the runtime is exactly 1.5.1; tests set an explicit local-only override. Thus
this rehearsal proves the gate, not the pinned runtime.

## Resource and interruption boundary

Rust, proof, encoder, verifier, and hydration run as child processes with
external RSS, scratch, and wall-time termination. DuckDB runs under a watchdog
that samples whole-process RSS/scratch/deadline and calls `interrupt()` on a
violation. Closed pack, aggregate output, and serving bytes are checked again
against hard caps.

The later hosted runner still needs its own process-group/cgroup-style kill
boundary. A cooperative in-process DuckDB interruption and polling cannot prove
an absolute kernel-enforced RSS ceiling, and sub-poll peaks are possible.

Planet numeric values deliberately remain unset: task row/decoded-byte caps,
pack rows, Parquet row-group rows, RSS, scratch, remaining disk, output, wall
time, DuckDB memory/threads, range grouping, and concurrency must come from the
frozen representative/near-cap/skew suite.

## New serving artifact

`.av1` is a single immutable partition artifact:

```text
44-byte header
24-byte fixed index entry per row:
  route_hash u64be / absolute payload offset u64be /
  payload length u32be / reserved zero u32be
canonical variable-length logical payloads
```

Index entries are ordered by route hash, normalized eight-field key, UUID, and
source locator. The Worker binary-searches the route-hash run, caps every hash
candidate examined, decodes bounded records, and compares the complete key.
The format is intentionally lean and dormant: there is no endpoint, catalog
dependency, R2 loader, cache policy, or dual-format decoder in this checkpoint.

## Remaining gates before Address planet mapping

1. Install and run the exact DuckDB 1.5.1 runtime; the default gate already
   rejects any other version.
2. Freeze exact representative, near-cap, and worst-skew task identities and
   limits before observing candidate results.
3. Demonstrate at least 2x construction speed against the same frozen Python
   baseline and at least 25% RSS/scratch/output/disk/time headroom.
4. Exercise multiple tasks, multiple packs, multiple Parquet row groups, a
   maximum-bucket-heavy skew case, and the largest acceptable duplicate-key run.
5. Measure Arrow IPC-only cost, DuckDB external spill behavior, full
   process-group RSS, and remaining disk under hosted-equivalent concurrency.
6. Add hosted process-group termination and test interruption during local
   pack write and every future remote-write phase. This checkpoint covers local
   immutable-object completion and marker-last interruption only.
7. Bind exact pinned inventory identities and source row-group assignments into
   the fresh request/marker, then rehearse the complete non-promoting remote set
   in isolation before any planet dispatch.
8. Feed representative encoder artifacts through the actual preview Worker
   loader once that separate loader/catalog wiring exists. The byte decoder and
   construction-produced golden artifact already agree locally.

Places, publication, retention, API behavior, and production catalog work
remain outside this checkpoint.
