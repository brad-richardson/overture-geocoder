# construction-v1: current state

Last updated 2026-07-26 after PR #178, PR #181, and the live R2 publication
probe.

This is the operational snapshot for construction-v1. It intentionally contains
only the current milestone, measured blockers, next actions, and frozen
decisions. Dated documents in this directory preserve the evidence and history;
they do not override this file unless their findings have been incorporated
here.

## Current milestone

Reach the first non-promoting planet-scale build attempt for both Places and
Addresses by removing only the blockers that prevent the next measured
execution rung.

Do not dispatch a planet build from an agent session. The workflow confirmation
encodes cost and runner limits and must be supplied by the operator.

## Current snapshot

The current construction-v1 code/evidence checkpoint on `main` is `82b4731`:
PR #178's direct bounded R2 publication backend plus PR #181's live probe.
There are no open construction-v1 code PRs.

Neither family is ready for a planet dispatch.

### Places

The Europe run covered 43.9% of the planet. After PR #176 merged, all five
phases completed and head produced all 4,096 populated shards. The full head
measured 2,022.27 seconds wall time, 8,179,167,232 bytes peak RSS and
5,399,313,835 bytes peak sampled runner disk. It hydrated and released all
3,088,544,880 input bytes, with 855,605,976 bytes peak staged-cache residency,
and published 4,098 staged objects / 2,134,262,243 bytes.

The old failure was driven by shard fan-out, not candidate row count. Batching
the write at 256 shard ranges completed at an 8x smaller DuckDB memory limit
and reduced files per partition from 113 to 3, maximum 4. The post-merge Europe
run proves the fix through encode and verify, not only through the former
failing statement.

`DEFAULT_HEAD_SHARD_BITS = 12` remains frozen. The encoder entry cap is a floor
on shard count and serving fetch granularity is the deciding constraint.

The earlier "~40 seconds" rerun estimate covered only the former failing
DuckDB statement. A full 4,096-shard head takes about 34 minutes at Europe
scale. The old projected ~1% planet disk residual remains a planet-preparation
gate, but Europe exercised the complete phase without approaching it.

### Addresses

The Europe address run reached the first hard failure in `run-reduce`.
`_load_markers` consumed 13.45 GB outside the guarded region, and the watchdog
aborted every reduce job on its first observation before useful work began.

The planet marker set projects to about 29 GB against a 16 GB runner. Marker
size is driven mainly by countries and duplicated directory structures, not
row count. About 99.97% of the marker is `packs[*].directory`; reduce needs only
the pack key, group index, country, and min/max `route_hash`.

The current long pole is therefore one design change:

> Project a compact reduce marker or stream the existing marker so address
> reduce does not materialize the full map marker set.

Address finalize's aggregate publication size cannot be measured until this is
fixed. The old ordering "publication first, marker fan-in second" is retired.

### Publication

PR #178 replaced the serial `aws s3api` mirror with direct bounded R2
publication through one persistent botocore client. Publication concurrency is
derived from the contract's enforced per-object cap and the 25.6 GB runner
floor: Places admits 5 workers at its 5 GB single-PUT ceiling; Addresses admits
11 at its 2 GiB serving-object cap. Every member, including finalizer-created
manifests, is checked against the effective cap before any upload.

The live R2 half of the contract is now execution-proven. PR #181 added a
manual, main-only, one-object probe, and Actions run `30203859256` passed against
`geocoder-shards` at main SHA `82b4731`:

- the non-empty object was created through the production persistent-client
  selector with `IfNoneMatch: "*"`;
- R2's single-part ETag equalled the content MD5 and the recorded SHA-256
  metadata equalled the admitted identity;
- a fresh identical retry received the create-only conflict and was accepted
  only after byte-exact read-back;
- same-length different bytes under the same key were rejected; and
- the unconditional cleanup deleted the exact key and proved it absent.

Remote create-only/checksum semantics are therefore closed. Fleet throughput
remains a scale measurement, not a reason for another unit/review loop; the
resumed Europe Addresses finalize will exercise it after marker fan-in is fixed.

## Fastest path

Follow this sequence. Do not interleave hygiene, reverse implementation, or
unrelated hardening. Direct publication and its live remote-semantics probe are
complete.

1. **Implement compact or streaming address markers.** Keep this PR limited to
   making the marker fan-in fit and making its measurement trustworthy.
2. **Resume the preserved Europe address reduce and finalize.** Measure the
   reducer residency and aggregate publication behavior. Fix the next observed
   blocker only.
3. **Prepare the non-promoting planet workflow inputs.** Hand the exact
   confirmation string, projected cost, runner ceilings, and known residuals to
   the operator. The operator decides whether to dispatch.

## Open blockers and gates

| Item | Family | Evidence | Closure gate |
|---|---|---|---|
| Possible ~1% planet head disk residual | Places | Europe full head passed at 5.40 GB peak sampled disk; planet remains a projection | Planet preparation gate, then the authorized non-promoting run |
| Marker fan-in | Addresses | 13.45 GB Europe load; ~29 GB planet projection vs 16 GB runner | Compact/streaming representation and completed Europe reduce |
| Address publication aggregate | Addresses | Old 100–145 GB projection; currently hidden behind marker failure | Completed Europe finalize with direct R2 backend |
| Watchdog loses the useful diagnosis | Addresses | Observed on Europe abort | Fix alongside marker work if needed to trust the rerun |
| Reducer cap fails open when absent | Addresses | `predict-reduce` accepted 242 jobs against its own default | Fail closed before planet request preparation |

The last two are supporting correctness fixes for the next address execution,
not separate review programs.

## What is already established

- All five phases run on real Monaco Places and Seattle Addresses slices in CI.
- Map output moves through run-scoped, content-addressed R2 staging rather than
  GitHub artifact fan-out.
- Places reduce owns bucket ranges; address reduce releases hydrated packs at
  last use.
- Places term rows are combined before shuffle, removing about 46% at planet
  scale.
- Places head is routed through 4,096 shards with a published routing manifest.
- The complete 43.9%-of-planet Europe Places head passes under merged #176:
  4,096 populated shards, 8.18 GB peak RSS, 5.40 GB peak sampled disk.
- Both families emit and durably publish per-record artifacts needed by a later
  spatial reverse index.
- Finalize verifies an exact publication set and has a projected remote
  operation budget.
- Finalize publishes that exact set directly through a bounded persistent R2
  client. Live R2 create-only, ETag/content, identical-resume, conflicting-byte,
  and cleanup semantics pass.
- The address forward partition key and serving layout have not changed.

## Frozen decisions

Do not relitigate these while closing the current blockers:

- The address map shuffle will not be ported. Address output is already
  hash-clustered; the transport and marker fan-in are reduce-side concerns.
- `address_key_hash`, `route_hash`, `hash_bucket`, `MAXIMUM_HASH_BITS`, and the
  address forward partition key remain frozen.
- Places uses the 256x256 equirectangular cell scheme and the high bits of the
  multiplicative shuffle hash.
- Places head remains at `DEFAULT_HEAD_SHARD_BITS = 12`.
- The per-place positions and per-address records artifacts are required,
  durable outputs. A reverse index must consume them rather than forcing a new
  planet map.
- Reverse geocoding is a separate spatial serving index. It must not distort the
  forward partition keys.
- `/v2/features/:gers_id` is slated for removal and is not a dependency for
  reverse rendering.

## Reverse geocoding fast follow

The map-time prerequisites for reverse are already present:

- Places emits one rich, spatially keyed record per admitted source row.
- Addresses emits an analogous spatially keyed records artifact without
  changing its forward packs.
- Both are content-addressed, published by finalize, and retain source-locator
  identity.

This means forward planet readiness remains the long pole. Reverse can be built
as a second reduce over the published per-record artifacts without rereading
Overture or rerunning map.

The accepted requirements are:

1. One spatial reverse design serves both POI and addresses.
2. Route by the existing level-8 Places cell scheme. Address E7-to-cell
   derivation must pass cross-language parity against the authoritative Places
   route.
3. Emit one reverse shard per populated cell. Dense cells use fine-quadkey
   leaves, with row-major payload order and latitude-aware depth bounds.
4. Queries are bounded-radius k-nearest, with explicit read/byte budgets and
   honest `budget_exhausted` / effective-radius reporting.
5. Use a binary, sharded reverse catalog rather than a large JSON fan-in.
6. Records are self-sufficient for rendering; do not depend on
   `/v2/features/:gers_id`.
7. The reverse finalizer proves total emitted records equal admitted per-record
   inputs before advertising the operation.
8. The v2 catalog advertises reverse per family only when its verified hashed
   artifacts exist. Forward-only publication remains valid.

Keep reverse work off the critical path by sequencing it as:

- **R0, requirements only:** keep the contract and byte-format decisions
  current while forward blockers are being removed.
- **R1:** shared encoder, verifier, cell parity gate, and small real-data harness.
- **R2:** bucket-range reverse reducer and binary catalog.
- **R3:** Worker range reader, bounded query planner, and API capability wiring.
- **R4:** exact-set publication integration and end-to-end release rehearsal.

R1 can begin after the forward blocker PR count falls below the two-PR WIP
limit. R2–R4 should follow the first non-promoting forward planet run unless a
measured dependency requires otherwise.

The full reviewed design, including geometry and API details, is
`docs/plans/2026-07-25-reverse-v2-design.md`.

## Verification ladder for this project

1. Focused unit/contract tests for the changed invariant.
2. Monaco Places or Seattle Addresses slice for end-to-end correctness.
3. Preserved Europe run for RAM, disk, fan-out, object-count, and wall-time
   behavior.
4. Live one-object R2 probe for remote SDK semantics.
5. Non-promoting planet run with operator authorization.

Do not substitute more rung-1 or rung-2 checks for a rung-3 or rung-4 risk.

## Deferred, not active

These remain useful but do not block the next measured milestone:

- range-owning address reducer and row-group range reads, unless the resumed
  Europe run proves them necessary;
- a narrower staging-only R2 credential;
- cleanup of dead evidence-spec hard-cap declarations;
- request-count and storage-cost optimizations;
- general review findings that do not corrupt output or prevent the next probe;
- reverse implementation beyond R0 while the forward long pole is open.

## Evidence and history

- `docs/plans/2026-07-26-planet-probe-findings.md` — Europe runs, corrected
  projections, preserved work trees, and resume commands.
- `docs/plans/2026-07-25-reverse-v2-design.md` — reviewed POI/address reverse
  design.
- `docs/plans/2026-07-24-construction-v1-follow-ups.md` — append-only historical
  findings; not the active queue.
- `docs/plans/2026-07-25-pending-work-archive.md` — historical project handoff
  and former backlog.
- `docs/plans/2026-07-23-construction-v1-one-way-doors.md` — irreversible
  storage and contract choices.
