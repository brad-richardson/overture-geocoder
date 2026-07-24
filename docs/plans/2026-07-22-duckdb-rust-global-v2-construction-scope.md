# DuckDB + Rust global v2 construction scope

Date: 2026-07-22

## Status and decision

### Planet-first reset (checkpoint 1)

The unshipped Address and Places family outputs are disposable. Request
`59f326dc2fd0866f54ead2ce0a1b19b5b9955c565cd8ef662d6bf22fc1047a63`, its
outputs, completion markers, intermediate formats, serving bytes, and partition
history are abandoned and cannot constrain the replacement. The first accepted
replacement run will be lineage genesis in a fresh namespace.

The immediate target is one verified, non-promoting planet Address + Places
slice. Compatibility work, dual-path activation, native range-reader work, and
permanent lineage machinery are deferred unless measured evidence makes them
necessary. The initial implementation may retain the bounded PyArrow
row-group reader as a columnar-only hydration boundary. Python may write its
Arrow IPC handoff, but it may not construct feature objects or own semantics,
sorting, grouping, or Parquet output.

The focused checkpoint decision and implementation state are recorded in
`2026-07-22-address-construction-v1-checkpoint.md`. Its planet-first sequence
supersedes the older bounded implementation sequence below where they conflict;
the rest of this document remains design inventory and safety guidance.

This is a local implementation scope. It is not approval to resume the failed
planet request, publish a family slice, promote a catalog, or change production
Workers.

Replace the current Python-row data plane with a DuckDB + vectorized Rust data
plane:

- DuckDB owns relational projection, joins, external sorting, grouping, bounded
  materialization, and Parquet export.
- Rust owns exact row semantics that are unsafe or awkward to reproduce in SQL,
  plus the custom serving-format encoders.
- Python remains control-plane only: invoke bounded stages, validate small
  reports, hash completed files, upload immutable objects, and publish markers.

Do not resume request
`59f326dc2fd0866f54ead2ce0a1b19b5b9955c565cd8ef662d6bf22fc1047a63`.
Keep its retained Address outputs only as mapper benchmark and differential
evidence. It contains no Places map output and no serving artifacts, so it is
not an equivalence oracle. A changed producer or shuffle contract requires a
fresh request and staging namespace.

## Why this change is necessary

Planet run `29881689313` did not time out. Address task 46 finished source
projection and mapping, then its GitHub runner received `SIGTERM` during upload.
The job had run about 24 minutes against a 330-minute limit.

The failure was incidental infrastructure, but the run exposed the architectural
problem:

- 126 of 127 Address tasks completed and were retained.
- A representative 3.98-million-row task spent about 10 seconds hydrating and
  projecting source columns.
- The custom Python Address mapper then used about 23 minutes at 100% CPU.
- Across identical task indexes, the new projection/map step was about 2.2 times
  slower than the earlier fresh planet attempt.
- R2 publication was faster than before, so neither R2 nor queued GitHub matrix
  entries explains the mapper slowdown.

The current mapper repeatedly converts Arrow batches to Python objects, encodes
and decodes records, hashes records, writes custom spills, merges them, converts
rows back to Arrow, and writes Parquet. That work should be vectorized or removed.

## Goals

1. Eliminate Python row iteration, row sorting, semantic hashing, spill merging,
   and Parquet construction from Address and Places construction.
2. Preserve exact admission/rejection semantics, provenance, deterministic
   ownership, duplicate policy, and reducer reconciliation.
3. Use fewer long-lived runner jobs without making a runner interruption force
   large-scale reprocessing.
4. Produce reducer-selective typed Parquet packs with independently verifiable
   logical bindings.
5. Keep all data-plane work bounded by explicit input, RSS, scratch, output,
   runtime, and remote-operation limits.
6. Keep promotion separate. A successful construction run still produces only
   an immutable, non-promoting family slice.

## Non-goals

- Do not optimize or resume the old shuffle format.
- Do not preserve byte compatibility with unshipped intermediate packs.
- Do not change serving API behavior or ranking policy in the same change.
- Do not replace the existing legacy core, divisions, or ID shards.
- Do not write directly from DuckDB to R2.
- Do not add a generalized workflow framework, scheduler service, queue, or
  database-backed coordinator.
- Do not implement a new address parser, fuzzy search behavior, or Places ranking
  model.

## Target boundary

```text
exact source inventory and row-group assignments
                    |
                    v
       bounded Arrow/Rust range hydration
                    |
                    v
       vectorized Rust semantic transform
                    |
                    v
       bounded DuckDB table and spill space
              /                     \
             v                       v
  grouped summaries and       total-order pack plan
   logical bindings                    |
                                       v
                           explicit ordered Parquet COPY
                                       |
                                       v
                       local proof, SHA, create-only upload
                                       |
                                       v
                              completion marker last
```

Python may coordinate these operations, but it must never receive feature rows
as Python dictionaries, tuples, or objects.

## Source hydration

DuckDB does not expose an interface for selecting the arbitrary source Parquet
row-group IDs in the pinned inventories. Preserve a thin native columnar reader
for that boundary.

The hosted replacement must not retain the current Python-owned concatenation
and projected-Parquet writer. The reader may use Arrow's native Parquet and
range-read implementations, but it must expose bounded Arrow batches to Rust
and DuckDB through Arrow IPC or the Arrow C data/stream interface. Python may
pass configuration and paths; it may not pull batches, concatenate tables,
materialize projected Parquet, or own a feature-row writer. The reader must:

- read exactly the inventory-assigned objects, columns, and row-group ranges;
- attach source object, row-group, and row identity columns;
- enforce compressed-byte, decoded-row, output-byte, RSS, scratch, and runtime
  limits; and
- emit bounded Arrow batches through Arrow IPC or the Arrow C data/stream
  interface.

Direct full-theme or full-object scans are not an acceptable shortcut.
The current PyArrow hydrator remains only a reference benchmark until this
native batch boundary replaces it.

## Exact semantic transformation

Pure SQL is not the contract for row semantics. A pinned, vectorized Rust
transform must implement or share the exact logic for:

- strict WKB Point validation, dimensional/EWKB rejection, finite coordinates,
  and world bounds;
- canonical UUID handling;
- NFC and whitespace normalization and the versioned ASCII-only fold;
- stable Address routing/hash semantics;
- nested address levels and Places names;
- Places cell routing, tokenization, term generation, and rejection precedence;
- duplicate preservation and stable total-order tie breakers; and
- canonical logical digest inputs.

The transform accepts and returns Arrow record batches through a bounded Rust
process using Arrow IPC or the Arrow C data/stream interface. A custom DuckDB
extension is deliberately outside the first implementation because it would add
ABI, packaging, loading, and extension-trust scope. The transform must not be a
per-row Python UDF.

SQL macros remain appropriate for simple expressions already proven equivalent,
such as the existing NFC/ASCII normalization expression, but the Rust contract
is authoritative when SQL and serving code could diverge.

## DuckDB construction stage

Use the exact pinned DuckDB runtime. The first implementation remains on 1.5.1
until a separate runtime upgrade is reviewed.

For each admitted logical subtask:

1. Load transformed Arrow batches into a bounded on-disk DuckDB table.
2. Record exact input, admitted, and exclusive rejection counts.
3. Compute routing columns and a unique total sort key.
4. Compute a deterministic pack plan from counts and conservative encoded-byte
   bounds.
5. Materialize or sort once with explicit memory and temporary-directory limits.
6. Export each planned pack with an explicit ordered `COPY` to one local path.
7. Produce compact summary and proof Parquets with native `GROUP BY` operations.
8. Inspect every file and row group before any upload.

DuckDB supports native Parquet export, `RETURN_STATS`, row-group sizing,
key-value metadata, and Parquet metadata inspection:

- <https://duckdb.org/docs/stable/sql/statements/copy>
- <https://duckdb.org/docs/current/data/parquet/metadata>

### Deterministic pack export

Do not use one `COPY ... PARTITION_BY` as the deterministic pack contract.
Exact testing against DuckDB 1.5.1 confirmed that `PRESERVE_ORDER` is rejected
with the proposed partitioned-write parameters. Partitioned output can also
produce files per thread, and file-size/row-group split options are approximate:

- <https://duckdb.org/docs/stable/data/partitioning/partitioned_writes>

Instead:

- enumerate a small deterministic pack plan from the materialized table;
- use one explicit `COPY (SELECT ... WHERE pack_id = ? ORDER BY total_key)` per
  pack;
- export with one thread, `PRESERVE_ORDER true`, a fixed Parquet version,
  compression, compression level, and row-group size;
- use an explicit unique total key so DuckDB sort stability for ties is
  irrelevant; and
- pin and record DuckDB, Parquet, Arrow IPC, and Rust transform versions.

Scanning, transformation, planning, aggregation, and external sorting may use
multiple threads. Only the final deterministic file export is single-threaded.

### Places execution-group boundaries

Before implementation, choose one proof-compatible policy:

1. Partition packs so a file and every row group belong to exactly one Places
   execution group; or
2. permit a row group to span adjacent execution groups, fetch the overlap, and
   require exact post-filter logical reconciliation.

If DuckDB cannot flush row groups at the selected boundary without violating the
file contract, retain a small Rust Parquet writer for this boundary only. It
must consume vectorized sorted batches and must not recreate a general custom
mapper.

## Integrity and semantic bindings

Do not replace the current semantic proof with only whole-file SHA-256, record
counts, and footer min/max statistics.

A selective reducer does not read enough bytes to recompute a pack's whole-file
SHA. Footer statistics are routing optimizations, not an independent logical
proof. The new contract must include associative, vectorized logical bindings
at the planning bucket and selective row-group boundaries.

Required properties:

- the binding includes the canonical logical serving row, including duplicate
  multiplicity and source tie breaker;
- map summaries are provably derived from the same logical rows written to the
  packs;
- reducers recompute and reconcile bindings for every selected row group and
  emitted serving partition;
- counts and at least two independent digest lanes are combined associatively;
- whole-file SHA-256 and byte length still protect immutable pack identity;
- Parquet min/max statistics are accepted only when exactness flags and null
  counts satisfy the contract; and
- no digest design relies on XOR alone, because even duplicate multiplicities
  can cancel.

The digest implementation may use pinned vectorized Rust or a proven DuckDB
aggregate over canonical digest lanes. Its exact algorithm is a versioned
contract and requires independent collision/accounting fixtures.

## Reducers and serving encoders

Reducers use the same Rust semantic transform and DuckDB runtime as mappers:

1. Restore the exact plan and selective row-group proof.
2. Range-fetch only the assigned Parquet footer and row groups.
3. Recompute logical bindings and reject missing, extra, overlapping, or
   mismatched input.
4. Use DuckDB for filtering, grouping, deduplication policy, joins, and total
   ordering.
5. Stream sorted Arrow/Parquet batches into Rust serving encoders.
6. Reconcile exact input, rejection, duplicate, and output totals.

Rust owns `.adat`, `.aidx`, `.pcsh`, and `.phrp` encoding. Python must not encode
or inspect serving rows. Places head admission must use the same tokenizer and
ranking contract as the map/reducer path; it cannot consume a simplified SQL
approximation.

## Aggregate planning, head admission, and final verification

The no-Python-row boundary applies after mapping and reduction as well:

- count and ownership summaries remain typed relational Parquet;
- Places famous-candidate summaries flow through DuckDB and Rust without
  becoming Python dictionaries or feature objects;
- Places head admission and ranking execute in the authoritative Rust contract
  over vectorized inputs;
- Rust supplies independent decoders and structural/semantic verifiers for
  `.adat`, `.aidx`, `.pcsh`, and `.phrp`;
- those verifiers emit only bounded counts, digests, invariants, and sampled
  query results to Python; and
- aggregate planning, head construction, serving-artifact verification, and
  final remote-set reconciliation are all part of the version activation gate.

The new Rust encoder and verifier must not share unchecked internal state.
Fixed binary golden artifacts must be accepted by the existing Worker decoder,
and artifacts produced by the new encoder must be independently decoded and
compared with frozen expected logical rows.

## Runner topology and resume behavior

Logical task identity remains the current exact inventory subtask. Range jobs
are scheduler assignments only.

The planner groups logical subtasks by selected compressed bytes, estimated
decoded bytes, and worst-case runtime rather than by task count. Range-job count
is exclusively a measured planner output, not a normative architecture value.

Each range job:

- admits each logical subtask marker before reading source data;
- uses an isolated workspace for one subtask at a time;
- invokes the bounded Rust/DuckDB stages for that subtask;
- uploads immutable content-addressed outputs create-only;
- publishes that subtask's completion marker last; and
- clears only that completed subtask's local workspace before proceeding.

A runner interruption therefore loses at most the current logical subtask.
Previously completed subtasks in the same range are admitted on retry.

Benchmark active concurrency at 1, 2, 4, and 8 before selecting the hosted
default. Budget reports must distinguish logical subtasks, runner invocations,
admitted subtasks, newly processed subtasks, and retained runner minutes.

Never use GitHub's "Re-run failed jobs" for execute mode. Resume with a fresh
dispatch, an updated prior-minute total, and a confirmation bound to those
limits.

## Hard resource and remote-write rules

DuckDB settings alone are not hard process bounds. Enforce:

- explicit DuckDB memory, threads, and temporary-directory settings;
- an external RSS and scratch-space watchdog;
- conservative projected encoded-byte bounds before export;
- post-write pack, row-group, file, summary, and total-byte caps;
- a per-subtask wall-clock deadline below the hosted job timeout;
- a range-job deadline with sufficient time to finish or stop before beginning
  another subtask; and
- bounded R2 HEAD, GET-range, PUT, retry, and cleanup operations.

DuckDB always writes locally. The control plane then hashes and validates the
closed files, uploads them under content-addressed immutable keys, verifies
remote bytes and SHA metadata, and publishes the marker last. Direct DuckDB
`COPY` to R2 is forbidden because cancellation can leave an ambiguous partial
partitioned write.

Every benchmark or rehearsal produces a canonical evidence manifest binding:

- exact inventory SHA, logical task indexes, and documented selection rationale;
- command, producer commit, runner image, and pinned DuckDB/Rust/Arrow toolchain;
- a frozen pre-run specification containing numeric RSS, scratch, output,
  remaining-disk, wall-time, and remote-operation caps;
- construction-only baseline and candidate timings measured from the same
  frozen local Arrow IPC input to closed local packs and summaries, excluding
  remote hydration and upload from both sides;
- maximum interruption loss expressed as one named logical subtask and its
  bounded rows, selected bytes, output bytes, and minutes;
- per-phase and total S3/R2 GET-range, transferred-byte, HEAD, PUT, retry, and
  cleanup counts; and
- observed values and an explicit pass/fail result for every frozen gate.

Limits cannot be relaxed after observing a candidate run. A changed limit
requires a new evidence specification and explanation.

## Versioning and migration

Version only artifacts whose schema or semantics change. Expected changes are:

- Address typed shuffle rows and routing hash, if changed;
- Places typed shuffle rows and execution-group boundary policy;
- semantic multiset and row-group bindings;
- DuckDB/Rust construction evidence; and
- the map, plan, reduce, head, or family manifest fields that bind those changed
  contracts.

Unchanged serving formats, request fields, or manifests do not receive a version
bump merely because their producer implementation moved from Python.

Reuse the exact pinned Overture inventories after verifying their current
object identities. Do not reuse old completion markers or typed packs across a
contract change. A new producer commit, canonical request, request SHA, build
identity, slice identity, and staging namespace are required.

## Bounded implementation sequence

Each item is a separate, reviewable change. Do not combine later items merely
because an earlier prototype is promising.

1. **Freeze authoritative semantics and fixtures.** Define Arrow schemas,
   rejection precedence, routing, total keys, semantic bindings, and Places
   boundary policy. Use hand-authored golden edge cases for every rejection and
   normalization rule, fixed binary artifacts decoded by the Worker, and frozen
   expected values that do not call the new Rust transform. Record intentional
   semantic changes explicitly rather than assuming legacy Python is correct.
   No workflow changes.
2. **Build the native range reader and Address prototype.** Produce logical
   Address rows, summaries, explicit ordered packs, and proof reports through
   Rust and DuckDB. Keep the new contract dormant and isolated from hosted
   execution.
3. **Complete the dormant Address chain.** Add the matching planner, selective
   reducer, Rust encoder/verifier, finalization support, and range-job scheduler
   behind the new contract version. Individual PRs remain small; none partially
   activates the contract.
4. **Address benchmark and end-to-end rehearsal.** Run the frozen representative,
   near-cap, and worst-skew evidence specifications; compare concurrency
   1/2/4/8; prove deterministic export, interruption recovery, selective
   reconciliation, serving decode, and isolated R2 behavior. Stop if any gate
   below fails.
5. **Activate Address atomically.** Switch hosted Address map, aggregate plan,
   reduce, verification, and finalization to the rehearsed contract in one small
   wiring PR. There must be no workflow state in which a new mapper feeds an old
   planner or reducer.
6. **Build and rehearse the complete dormant Places chain.** Port source reading,
   strict projection, routing, names, tokenization, summaries, boundary-aligned
   packs, planner, reducer, Rust encoders/verifiers, famous-candidate fan-in, and
   global head. Prove ranking and Worker-decode equivalence without changing the
   hosted contract.
7. **Activate Places atomically.** Switch hosted Places map, aggregate plan,
   reduce, head, verification, and finalization together in one wiring PR.
8. **Non-promoting readiness rehearsal and fresh request.** Run bounded local
   and isolated R2 rehearsals, one adversarial readiness review, then prepare a
   fresh immutable request. Promotion remains a separate decision.

## Acceptance gates

No hosted replacement lands unless all applicable gates pass:

- no feature-row `to_pylist`, `from_pylist`, Python per-row loop, Python UDF,
  Python sort, or Python Parquet writer in the data path;
- logical output and exclusive rejection counts match hand-authored frozen
  values or a reference implementation that does not call the new transform;
- Rust encoder output is accepted by the existing Worker decoder, and fixed
  binary golden artifacts are accepted by the new independent Rust verifier;
- duplicate multiplicity, source provenance, routing, total ordering, and
  semantic bindings reconcile exactly;
- two runs with the same pinned runtime produce identical pack plans, logical
  rows, summaries, proofs, and serving artifacts; byte determinism is required
  for artifacts declared byte-stable by their contract;
- the frozen construction-only Address suite is at least twice as fast as the
  same-task typed Python baseline and every task remains below its absolute
  predeclared wall-time cap, with a stretch goal at or below the earlier fresh
  mapper;
- the exact near-cap and worst-skew task indexes named by the evidence
  specification remain below every numeric RSS, scratch, output, runtime, and
  remaining-disk cap with at least 25 percent headroom;
- a concurrency candidate has zero task errors and unplanned retries, keeps p95
  construction time within 25 percent of the isolated-task p95, keeps observed
  source and R2 bytes within 5 percent of the planned bounds, and passes every
  resource gate; select only among passing candidates;
- selective fetch reads only planned bytes and recomputes exact row-group and
  partition bindings;
- injected termination after local write, during upload, and before marker
  publication resumes without duplicate logical rows and loses no more than the
  one named logical subtask bounded in the evidence specification;
- Places map/reduce/head output and ranking fixtures are equivalent;
- aggregate planning and Places famous-candidate fan-in pass without Python
  feature objects;
- worker-readable Address and Places artifacts pass local, isolated R2, and
  preview Worker query smoke tests; and
- finalization proves the exact remote serving set while remaining incapable of
  production catalog publication.

## Decisions intentionally deferred to measurements

- Exact number and composition of range jobs.
- Default active concurrency.
- DuckDB thread count and memory/scratch limits per runner.
- Address and Places pack row/byte targets.
- Whether Places permits cross-execution-group row groups or uses the narrow
  Rust boundary-aware Parquet writer.
- Exact semantic digest algorithm and lane width.

These decisions must be made from the bounded prototype evidence. They are not
reasons to expand the first contract/fixture change.
