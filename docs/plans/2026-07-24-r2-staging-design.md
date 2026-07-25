# R2 staging for the construction-v1 store: design

Date: 2026-07-24. Status: DRAFT for owner review. No code written yet.

This is §6 step 2 of `2026-07-24-growth-test-and-path-to-planet.md`, and it is
the only remaining hard blocker on a planet build.

## 1. The problem, measured

`mapdl/store` is the content-addressed pack store — every term row the map phase
produces. Measured from run `30113308268`: **63.5 GB** for Places.

It moves between phases as a GitHub Actions artifact, whole, every time:

| phase | transfer |
|---|---|
| map -> plan | 89 artifacts, 63.5 GB, merged into `cv1-plan` |
| plan -> reduce | each of **128** batches downloads all 63.5 GB |
| reduce -> reduce | each batch **re-uploads** `cv1/mapdl/store` in its own artifact |
| reduce -> head | head downloads `cv1-reduce-*` with `merge-multiple` — all 128 copies |
| head -> finalize | `cv1-head` + `cv1-plan`, 63.5 GB each |

Roughly **24 TB** for one family.

**It is not a performance problem.** A runner has ~70 GB of disk and ~25-30 GB
usable after the toolchain, so 63.5 GB does not fit. `places reduce batch 2`
spent 12 minutes downloading and then failed on the first line of its run step,
`test "$(df -Pk / | awk 'NR==2 {print $4}')" -ge 30000000`. The guard worked.
The job cannot start.

The map-side combiner (landed 2026-07-24) takes the store to roughly 34 GB.
That still does not clear a 30 GB guard, and 128 x 34 GB is still ~4.4 TB. It
softens the wall; it does not remove it.

## 2. Why this is transport-only

Three properties mean nothing about the semantics has to change.

**The reducer's compute is already selective.** `reduce_partition` reads
`row_groups=[index]` for only the row groups whose `routing_summaries` name its
`partition_cell`, and validates a per-row-group binding proof
(`selected + discarded == row_group binding`). It has never wanted the whole
store — only the delivery mechanism forces it to hold one.

**The store is content-addressed.** Keys are `{prefix}/sha256/{digest}{suffix}`,
so every object is immutable and its name proves its contents. That maps onto R2
create-only writes exactly as `finalize` already does them
(`put-object --if-none-match '*'`, then `head-object` to confirm).

**Selective reads are already the declared contract.** The frozen evidence spec
carries `"selective_read_amplification_max": 4.0`. A bound on read amplification
only makes sense for a selective reader. This design *restores* the intended
contract rather than inventing one.

## 3. What already exists

This is the part that makes the work small. None of the following is new:

| asset | what it gives us |
|---|---|
| `scripts/r2_verified_store.py` | An `ObjectStore` Protocol with `FilesystemStore` **and** `S3Store` (R2 via `aws s3api`). Content-addressed `immutable_key()`, create-only `ensure_uploaded()`, `verified_download()` with size+SHA checks, and manifest upload/restore. Covered by `tests/test_r2_verified_store.py`. |
| `.github/workflows/rehearse-address-r2-map-reduce.yml` | A **credentialed, matrix-parallel, real-data R2 map-reduce rehearsal** that already builds map fragments, uploads them under a per-task prefix with `r2_verified_store.py`, and verifies resume. The pattern is proven in this repo, not theoretical. |
| `.github/workflows/rebuild-r2-shards.yml` | The `id-stage-release` -> `id-stage-release-finalize` job shape: matrix jobs writing **disjoint** R2 staging prefixes, then a barrier job that verifies the exact per-type marker set before writing an aggregate marker. This is the pattern the owner asked to keep. |
| `scripts/build_id_index.py`, the worker | Range-reading parquet from R2 by row group, already in production for the ID index. |

## 4. Design

### 4.1 The store seam

`LocalObjectStore` (in `address_construction_v1.py`) exposes exactly four things
the construction code uses: `path(key)`, `put_content(source, prefix, suffix)`,
`read_json(key)`, `write_marker_last(key, value)`.

Add an `R2StagingStore` with the same surface, backed by `r2_verified_store`:

- `put_content` -> `ensure_uploaded()` create-only, returning the same
  `{key, bytes, sha256}` dict. Identical semantics: a re-run that produces
  byte-identical content is a no-op, and differing content for the same digest
  key is impossible by construction.
- `read_json` / `write_marker_last` -> small objects, plain get/put. Marker
  written last is preserved.
- `path(key)` is the hard one — see below.

### 4.2 `path()` must not become "download the pack"

The naive implementation hydrates a pack to a local cache and returns its path.
**That is not sufficient, and it is worth being explicit about why.**

Packs are per-map-task, and each task's rows are sorted by `TOTAL_ORDER`, which
begins `execution_group, partition_cell`. So a cell's rows are contiguous
*within* a pack — but a given cell appears in most of the 88 tasks, because map
tasks are row-group ranges of the source, not geographic slices. Hydrating every
pack that contains a cell therefore approaches hydrating the whole store, and we
are back where we started.

So the reducer needs **row-group-level range reads**, not object-level fetches:

1. Range-read the parquet footer (a few KB) to get per-row-group byte offsets
   and sizes.
2. Consult the existing proof directory's `routing_summaries` to choose row
   groups — unchanged logic.
3. Range-read only those row groups and feed them to the existing
   `iter_batches(row_groups=[index])` path.

`places_proof_directory` records row-group **record counts**, not byte offsets,
so no proof format change is needed — the footer supplies offsets, and the
existing per-row-group binding proof still validates what was read. Read
amplification is bounded by row-group granularity, which the spec already caps
at 4.0 and which a gate should assert rather than assume.

### 4.3 Phase-by-phase

| phase | today | after |
|---|---|---|
| map | uploads `cv1-map-N` (63.5 GB total) | writes packs to `staging/{request_sha256}/packs/...` in R2; artifact carries markers + proofs only (single-digit MB) |
| plan | merges 89 artifacts into `cv1-plan` (63.5 GB) | reads markers only; `cv1-plan` becomes `plan.json` + markers + ledger |
| reduce | downloads 63.5 GB per batch | range-reads its own row groups; writes serving objects to R2; artifact carries markers + reductions |
| head | downloads all 128 reduce artifacts | reads per-task `head_candidates` (already bounded) from R2 |
| finalize | downloads `cv1-head` + `cv1-plan` | already R2-native; mirrors from staging to the published prefix |

Every artifact becomes markers and JSON. Peak runner disk goes from "does not
fit" to well under 2 GB.

### 4.4 Staging lifecycle

Staging is keyed by `request_sha256`, so concurrent or retried runs never
collide, and a resumed run finds its own objects already present — which is how
create-only + content-addressing gives resume for free.

Staging is **not** the published prefix. `finalize` still mirrors create-only
into the release location, marker last. A `r2-cleanup.yml` already exists and
should be extended to expire `staging/` prefixes past some age.

## 5. Credentials — the one genuinely new risk

Today only `finalize` has R2 secrets. This needs them in `map` (89 parallel
jobs) and `reduce` (128), on a **public** repository.

- Use a **scoped, staging-only** R2 API token — write access limited to the
  `staging/` prefix, no access to the published release prefixes. Not the
  publish credentials.
- `workflow_dispatch` only, as construction-v1 already is; never on
  `pull_request`, where a fork could reach the secret.
- Keep `persist-credentials: false` on checkout, as the workflow already does.
- Publication stays exactly where it is: only `finalize`, only create-only,
  marker written last.

The blast radius of the staging token is a runner writing junk into a staging
prefix that is expired by lifecycle policy and never served.

## 6. Risks and open questions

- **Request volume.** Row-group range reads turn one 63.5 GB download into many
  small GETs. R2 bills Class B operations; 17k partitions x tens of row groups
  is on the order of 10^5-10^6 requests per family. Cheap, but it should be
  estimated before the first run, not discovered in a bill.
- **Read amplification must be gated, not assumed.** The spec caps it at 4.0.
  The reducer should measure bytes-read / bytes-needed and fail closed above the
  cap, otherwise a bad row-group layout silently reintroduces the wall.
- **`aws s3api` per-object subprocess overhead** is fine for whole objects but
  wrong for many range reads. The reducer path likely wants a real HTTP client
  with connection reuse rather than shelling out per request.
- **Resume semantics** need re-verifying end to end once markers live in R2 —
  the local store is what the current resume tests exercise.
- **Address family** has the same wall and the same fix, but its own reduce
  cost profile. Places first.

## 7. Sequencing

1. `R2StagingStore` behind the existing `ObjectStore` seam, filesystem-backed
   in tests so it stays credential-free to exercise.
2. Row-group range reads in `reduce_partition`, with the amplification gate.
3. Move map output to staging; shrink `cv1-map-N` to markers.
4. Shrink `cv1-plan`, then reduce/head artifacts.
5. Scoped staging token; dry-run dispatch; then a places execute.

Steps 1 and 2 are testable locally with `FilesystemStore` and need no
credentials, which is where the risk actually is.
