# Construction v1: follow-ups after the first planet execute

Date: 2026-07-24. Status: OPEN. None of these blocked dispatching the first
non-promoting planet slice; all of them were found while building and debugging
it (#140–#150) and lived only in PR bodies until now. Nothing here is a
one-way door — those are tracked separately in
`docs/plans/2026-07-23-construction-v1-one-way-doors.md`.

Ordering is by when the item can hurt, not by size.

## Before the slice is promoted

### 1. R2 mirror hardening

The mirror path used by finalize has three gaps, all of which weaken the
verification the finalize step reports:

- **Single-PUT only.** There is no multipart fallback, so an object above the
  5 GiB single-PUT ceiling fails at upload rather than degrading. Current
  per-object sizes are far below this, but the head/serving artifacts are the
  ones that grow with the planet.
- **Checksums disabled.** `AWS_REQUEST_CHECKSUM_CALCULATION=when_required` and
  `AWS_RESPONSE_CHECKSUM_VALIDATION=when_required` are set workflow-wide
  (`construction-v1.yml`), so transport-level integrity is not being checked on
  these uploads.
- **Post-upload HEAD checks existence, not identity.** The check confirms the
  key is present; it does not compare size or SHA-256. That is weaker than the
  "verified size, metadata, and downloaded SHA-256" rule stated in
  `PENDING_WORK.md`, and weaker than what the finalize summary implies.

The third item is the one worth fixing first: it is cheap, and it is the
difference between "the object exists" and "the object is the object we built."

### 2. Live remote-marker resume is not wired

The store adapter supports skip-via-remote-marker, but the workflow's map jobs
do not pass `--remote-root`, so resume today is honest but full-recompute: a
fresh dispatch re-derives every task rather than skipping completed ones. The
in-job `admit-task` marker check only sees the local store.

Wiring this needs a read-only R2 key decision (the map jobs currently hold no
cloud credentials by design — the admission job is explicitly "no cloud
secrets"). That decision is the actual blocker, not the code.

## Semantics and data

### 3. Finish exporting the Unicode tables

#142 pinned the tokenizer to Rust semantics by exporting Rust's **lowercase**
and **word-character** tables into the Python baseline, which killed the
version-skew class for those two properties. Two properties still ride each
side's own tables:

- NFKD normalization
- combining-mark classification

Both sides therefore still carry an independent Unicode opinion where
independence is spurious, exactly the class-4 argument in
`docs/plans/2026-07-24-places-digest-divergence-root-cause.md`. Exporting these
two the same way closes it permanently.

Note: the interpreter/toolchain Unicode versions should be **re-confirmed from
the running toolchains** before this is written down as a pinned contract
value. The root-cause doc records CPython 15.1 vs Rust 16.0; a later note
recorded 15.0 vs 17.0. One of those is stale and the contract should not pin
either number until it is read out of the actual pinned toolchains.

### 4. Address projection caps have zero headroom

The address map caps are `--max-rows 4000000` and `--max-groups 72`. Measured
task 0 of the live planet run projected **3,980,853 rows across 67 row
groups** — 0.5% and 7% under their hard caps respectively. These are
fail-closed caps, so the failure mode is a clean abort rather than a breach,
but a future Overture release with slightly denser row groups wedges the task
plan with no margin. Either raise the caps with measured justification or make
the planner subdivide when a task lands within some headroom fraction of them.

## Verification and test hygiene

### 5. Dry-run does not exercise the execute data plane — ADDRESSED 2026-07-25

The map job branches on `inputs.mode`: dry-run runs `--validate-only` plus
`predict-reduce` and never touches `run-map`, the transform binary, the S3
projection, or `store.put_content`. A green dry-run therefore certifies the
planning arithmetic and nothing about the data plane.

This cost three consecutive execute dispatches, each dying in its first minutes
on a different one-line defect that a dry-run could not see (#148 matrix key,
#149 cargo manifest path, #150 missing `psutil`). The `cargo build` step was
already made unconditional for exactly this reason; the same reasoning applies
to the rest of the path.

Two fixes, either of which would have caught all three:

- A `smoke` mode (or a tiny execute over one task and a few row groups into a
  throwaway namespace) that runs the real `run-map` end to end.
- A CI job that installs `.github/requirements-hosted-rowgroup.txt` into a
  clean virtualenv and imports every hosted entrypoint module.

**Both shipped** as `.github/workflows/slice-smoke.yml`, on `pull_request` and
push to `main`, path-filtered to `scripts/`, `crates/geocoder-construction/`,
the evidence spec, the pinned requirements file, and `construction-v1.yml`
itself (it is the source the import check derives its module set from):

- `slice` builds the construction binaries, builds the Monaco slice inventory
  from real row-group statistics, and runs the harness's five real phases
  (S3 projection -> `run-map` with the transform binary -> `plan-reduce` ->
  every `run-reduce` partition -> `run-head` -> `finalize`), then asserts that
  records, partitions, head shard count, populated head shards, head records,
  and the map/serve artifact-class byte totals are all non-zero, read from the
  summary file the harness writes. No credentials: the release parquet is read
  anonymously and finalize publishes to a filesystem remote. ~13s of harness
  time (~20s including the inventory); two attempts, each with its own fresh
  work dir and its own log, and a pass-on-retry raises a loud warning
  annotation, so an intermittent defect cannot hide behind a green check.
- `hosted-imports` installs *only* the hash-pinned requirements and imports
  every hosted entrypoint module (`scripts/check_hosted_imports.py`, module set
  derived from the hosted workflows rather than hardcoded, with a hard error on
  a missing workflow and a module-count floor).

**Coverage per defect** — the honest version, because the first draft of this
section overclaimed:

| Defect | Caught by |
| --- | --- |
| #149 cargo manifest path | the `slice` job's build step (byte-identical command) |
| #150 missing `psutil` | the `slice` job. `psutil` is a *function-level* import inside `run_bounded`, so only executing the map phase reaches it |
| #148 admit-job matrix key | **neither job.** The admission gate, request regeneration, and matrix generation are not run here; only `tests/test_construction_v1_workflow_contract.py` covers that surface |

`hosted-imports` catches top-level import failures only: a syntax error, or a
module-level import of a dependency missing from the pinned set. It cannot see
#150's class — with `psutil` uninstalled from the pinned environment every
derived module still imports cleanly.

The release (`2026-07-22.0`), bbox, and map task index are pinned in the
workflow env with the reason: the task index is a property of that release's
row-group layout, and the job fails with an explicit message if the bbox stops
landing on the pinned task.

### 6. Rust verifier test does not isolate the independent-binding check

One verifier test exercises the independent binding check together with
adjacent assertions, so a regression that disabled only the independent check
would not necessarily fail it. Split it so the independent-binding path has its
own failing case.

### 7. PLHD shards are not self-identifying

Sharded head artifacts are disambiguated only by the head manifest; the shard
payload itself does not carry its shard id or shard count. A misfiled or
swapped shard is detectable today only via the manifest. Since the sharded
layout is already a pre-publication format change
(one-way-doors §2), embedding shard identity in the artifact is much cheaper
now than after first publication.

### 8. Runner Python is unpinned relative to the frozen baseline

The hosted runners use Python 3.11.14 (pinned in `construction-v1.yml`) while
the frozen semantic baseline evidence was produced under 3.12. With the
tokenizer tables now exported from Rust the digest risk is largely removed, but
the skew is undeclared. Pin one, or record the skew explicitly in the evidence
spec.

### 9. Worker still accepts the dead v2 tokenizer string

The Worker will accept the superseded tokenizer version string. Nothing
publishes it, so this is not a serving risk today; it is a fail-closed gap that
should be closed before anything serves publicly.

## Documentation drift

Both items below were fixed on 2026-07-25 in the same PR as the slice smoke job.

- ~~`PENDING_WORK.md` is dated 2026-07-19 and its "Next" section still describes
  the abandoned `agent/global-v2-executor` path. It is the largest doc in the
  repo and does not mention construction v1 at all.~~ Its stale planning
  sections were deleted rather than rewritten, and it now points at
  `docs/plans/construction-v1-state.md` as the living state doc.
- ~~`docs/plans/2026-07-24-places-global-scale-plan.md` and
  `docs/plans/2026-07-24-places-digest-divergence-root-cause.md` both still
  carry `Status: LOCAL SPIKE — uncommitted, no PR, no workflow`, but they are
  committed and their recommendations shipped the same day in #141/#142/#143.~~
  Status lines corrected.

## Already addressed

- **StageWatchdog fail-open.** The watchdog thread is the only enforcement of
  the RSS/scratch/wall caps, and a fault inside it died silently in the daemon
  thread — `__exit__` then reported success and `evidence()` reported zero
  peaks. Fixed to fail closed, with the `psutil` attach moved to the caller's
  thread so a missing dependency fails at the call site.
- **Dry-run does not exercise the execute data plane** (item 5 above).
  `.github/workflows/slice-smoke.yml` runs the real five-phase Places data plane
  on the Monaco Overture slice on every relevant PR, and imports every hosted
  entrypoint module under only the hash-pinned dependency set. Both fixes
  proposed in item 5, in one workflow — but see the per-defect coverage table in
  item 5: #148's surface (the admission gate) is still test-only, and the import
  job is a top-level-import tripwire, not data-plane coverage.
- **The reducer had no `StageWatchdog`** (item 1 of "Added 2026-07-25"). The
  RSS/scratch/wall caps reached the encoder and verifier *subprocesses* through
  `A.run_bounded`, but the Python + pyarrow + DuckDB ingest between them was
  bounded by nothing at all — and raising `partition_term_rows` to 2,000,000
  doubled the peak of exactly that phase. The ingest pass and the per-partition
  serving encode are each now wrapped in the same watchdog the two `map_task`
  stages use, with the same caps and the same fail-closed semantics, and every
  reduction records `ingest_evidence` / `serving_evidence`. Landed alongside
  bucket-range reduce, which is the code that made the reducer's ingest a single
  bounded stage worth watching.

## Added 2026-07-25, from the adversarial review of PR #155

1. ~~**`reduce_partition` has no `StageWatchdog`.**~~ ADDRESSED — see "Already
   addressed" above.
2. ~~**Three evidence-spec hard caps are dead declarations that now disagree with
   the build.**~~ **ENFORCED 2026-07-25** — chosen over deletion, and the choice
   needs recording because neither option was free. The three caps
   (`partition_term_rows_hard_cap` 1,000,000,
   `partition_estimated_uncompressed_bytes_hard_cap` 268,435,456,
   `partition_distinct_tokens_hard_cap` 250,000) are **rehearsal** gates, not
   hosted-build limits: the spec is frozen against release `2026-06-17.0` and a
   12-task candidate universe, and `rehearse_places_construction_v1.py` restated
   the same three numbers as literals. So they are now READ from the spec
   (`rehearse_places_construction_v1.spec_partition_caps`) and enforced by
   `adaptive_genesis_plan`, which subdivides or fails at maximum depth against
   exactly those values. Nothing in the repo now states a partition cap that no
   code reads.
   **Not deleted, and not raised to the hosted values,** because either edit
   changes the spec bytes: its `.sha256` companion, the pin in
   `construction_v1_control.py`, and the `evidence_spec_sha256` recorded inside
   the frozen `evidence/readiness-v2.json` and `evidence/scale-evidence-v2.json`
   all bind to the current bytes, and the spec's own relaxation policy is "none;
   any gate change requires a new schema/version". Raising these is a places
   evidence **spec v3** plus a re-run of the real-data rehearsal, which is a
   tracked task and not hygiene.
3. ~~**`Limits` dataclass defaults were not raised with the hosted limits.**~~
   **DONE 2026-07-25 for the dataclass; deliberately NOT done for the
   rehearsal.** `places_construction_v1.Limits` now defaults to 2,000,000 /
   512 MiB / 400,000, equal to `HOSTED_LIMITS["places"]` and asserted equal by
   `tests/test_construction_v1_preflight.py`, so every non-hosted caller plans at
   the caps the planet build actually uses. They remain four separate literal
   sites (dataclass, `HOSTED_LIMITS`, and both pinned dicts in the test); the test
   is the only thing keeping them equal, so this is not a single source of truth.
   The rehearsal pins stay at the spec's declared caps (now read from the spec
   rather than copied) for the reason in item 2: it produces evidence under frozen
   spec v2, whose relaxation policy is "none" and whose coverage gate requires
   genuine adaptive subdivision. "Rehearsal reflects production" is therefore
   still open, and it is a spec-v3 task.
4. ~~**`predict-reduce` was 14x optimistic and this change made it worse.**~~
   Fixed for places in PR #155 by flooring the prediction with the committed
   plan's partition count. **The addresses branch had the same defect class —
   VERIFIED REAL and fixed 2026-07-25, but the magnitude is ~1.5x, not 14x.**
   `_plan_from_summaries` bisects each country independently, so every country
   with rows contributes at least one partition and an over-cap country's leaf
   count is a power of two — both invisible in a total row count. On the planet
   inventory at the 1,000,000 row cap the row division predicts **474** and the
   per-country bisection floor is **725**. It is nowhere near the Places 14x
   because the address structural floor is 34 countries, far below the
   row-derived figure, whereas Places' 16,633 populated cells dwarfed its 1,211.
   Fixed by `construction_v1_hosted._address_structural_partitions`, which floors
   the prediction from the inventory's `exact_country_rows` and fails closed when
   an inventory carries none. Note for the DEFERRED address-shuffle section
   below: its claim that "the address inventory records no per-country row counts"
   is **wrong** — `exact_country_rows` (34 countries, 434,397,621 of the
   473,576,753 records exactly attributed) is already there, and country skew can
   be read off it without a map run.
   **Correction from the review of PR #166:** the per-country figure is a
   uniformity-based ESTIMATE, not a floor, and the code, its docstring and the
   tests now say so. Measured by running `_plan_from_summaries` itself: 4,000,003
   rows over 8 even buckets at a 1,000,000 cap is model 8 vs real **5** (the
   planner stops as soon as a subtree fits), and a heavy adjacent pair plus ten
   scattered light buckets at 2,000,000 rows is model 2 vs real **6** (each split
   isolates an under-cap sibling that becomes its own leaf). It is well conditioned
   on the planet inventory only because `route_hash` is uniform by construction:
   model 725 against a simulated real 725. The genuine per-country lower bound is
   `sum(ceil(country_rows / cap))` = **493** — still above the 474 the total-row
   division gave, recorded in the docstring, and deliberately not used as the
   prediction because it under-provisions relative to the real shape.
5. **The committed plan is only read by `predict-reduce` and the generator.**
   Map-side partition assignment (the fail-closed gate) is still unbuilt, so the
   tree does not yet control anything.

## Added 2026-07-25, from the adversarial review of PR #160

1. ~~**The Places routed serving objects never reach the published slice.**~~
   **FIXED 2026-07-25.** Pre-existing, found while reviewing bucket-range reduce,
   and NOT fixed there. `construction_v1_hosted._artifact_keys` collected
   `reduction["artifact"]`, which the *address* reducer sets and the Places reducer
   does not — Places records `leaf_object` and `routed_object` instead. So a Places
   finalize published the head shards, the positions packs and the two manifests,
   and silently published no `.plrv` at all. Every existing check still passed: the
   reconciliation compares bindings, not the published object set.

   The fix is a per-family table (`REDUCTION_SERVING_OBJECTS`: places →
   `routed_object`, addresses → `artifact`) instead of one hardcoded key, and it is
   **fail-closed** — a reduction naming no serving object aborts and names the key
   it expected, rather than shortening the published set. `leaf_object` is
   deliberately NOT published: the leaf is a build intermediate the head phase
   reads, it holds TERM rows (~7.19 per place), and the per-place positions packs
   are the durable per-record artifact. As the item asked, the published set is now
   asserted to cover every partition: finalize reports `serving_objects` and
   `reduction_serving_objects`, the workflow requires
   `serving_objects >= reductions > 0`, and the slice smoke asserts the exact
   equality (places `partitions + head_populated_shards`, addresses `partitions`).

## Added 2026-07-25: scope down the R2 credentials used by construction-v1

**State on `main` today:** only the finalize job receives
`CLOUDFLARE_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY`. The map
jobs hold no cloud credentials, by design.

**Superseded 2026-07-25: this has now happened.** The staging work landed, and
the `map`, `plan`, `reduce` and `head` jobs all receive those same
workflow-level credentials alongside `finalize`. The description below was
written before the change and is accurate as a description of the state it
created; only the tense is wrong. The follow-up at the end is OPEN and is now
the last credential item before a planet dispatch.

**What changed when the staging work landed.** The map/reduce R2-staging work
needed R2 write access from the *map* jobs. It reuses those same workflow-level
credentials, by owner decision, to avoid blocking the staging work on a token
change. That is a real widening of blast radius, recorded here before it happened
rather than after:

- The reused key is the general-purpose R2 key. It can write anywhere in the
  bucket, including keys the create-only finalize discipline is meant to protect.
- It would reach roughly **89 parallel map jobs** instead of one finalize job,
  in a **public** repository. Exposure surface scales with fan-out.

**The follow-up:** issue a staging-only scoped R2 token — write limited to the
staging prefix, read-only (or no access) everywhere else — and give the map and
reduce jobs that token, leaving the broad key with finalize where the promotion
discipline lives.

Owner: Brad. Deliberate deferral recorded 2026-07-25. Do this before the slice
is promoted, and before any run dispatched from a fork-visible surface.

## Added 2026-07-25, from the adversarial review of the slice smoke job (#159)

Recorded rather than fixed, either because the file is contested by in-flight
branches or because the gap is about scope rather than a defect.

1. ~~**Places reconciliation is a hardcoded literal.**~~ **DONE 2026-07-25.**
   `places_construction_v1.validate_complete_reduction` is the addresses
   equivalent, ported, and `finalize` now calls
   `_family_module(family).validate_complete_reduction` for both families, so the
   literal is gone rather than hidden. It checks three independent things, because
   the summed binding — all the places branch used to check — sees none of them:
   the partition id set matches the plan exactly (no missing, extra or duplicate),
   each reduction's binding equals the binding the PLAN recorded for that
   partition, and the combined binding equals the plan's. Failing cases are
   tested: a duplicate standing in for a missing partition of equal binding and a
   pair of partitions carrying each other's rows both keep the sum correct and are
   both rejected, plus a misfiled reduction fails the real `finalize` in
   `tests/test_construction_v1_hosted.py`. The slice smoke job keeps its count
   assertions: an EMPTY run still reconciles.
   The per-partition plan-binding check was ported to
   `address_construction_v1.validate_complete_reduction` too (review of #166: it
   had landed on the places copy only, leaving the swapped-partitions case
   undetected for addresses), so both families now check the same three things.
2. **The smoke job's assertions on head are counts, not identity.** `run-head`
   computes an `input_binding` (records + semantic sums) that nothing compares
   against the reduce output. Non-zero counts rule out an empty head; they do not
   rule out a *wrong* head. Comparing `input_binding` to the plan's retained
   totals is the cheap next step.
3. **Contract tests pin the smoke job's commands as literals, not as equality
   with `construction-v1.yml`.** A change to a hosted command plus its own
   contract test leaves the smoke job silently behind on the old flags. The
   durable fix is a cross-file equality assertion on the shared command
   fragments (projector limits, cargo build line) rather than two independent
   copies of the same string.
4. **Hosted surfaces the smoke job does NOT cover.** Worth keeping explicit so
   nobody reads a green check as "the hosted path works":
   - the admission job — canonical request regeneration, typed confirmation,
     matrix generation (this is where #148 lived);
   - `ledger-check` / `ledger-append` and the fan-in re-sum gates;
   - `admit-task` marker idempotence, including the `_remote_marker_completed`
     HEAD path;
   - the free-disk-space gate;
   - ~~**inter-job artifact transport of the store and markers** — the actual
     planet blocker (run 30113308268 died on a 63 GB plan artifact);~~
     **partially closed 2026-07-25.** Both slice jobs now run the R2 staging
     transport with a filesystem backend and each phase on its own EMPTY local
     store, and assert `map_staged_objects_published > 0` plus
     `finalize_staged_objects_hydrated > 0`. What is still uncovered is the
     GitHub-artifact plumbing itself (upload/download paths and merge behaviour),
     which only `tests/test_construction_v1_workflow_contract.py` pins;
   - ~~the entire addresses family (no slice harness exists — see the DEFERRED
     section)~~ — **closed 2026-07-25**: `--family addresses` on both slice
     scripts, and a second smoke job runs the address slice in CI;
   - the R2 create-only mirror path.
5. **Two contract divergences inside what the harness does run.** The harness
   hardcodes the evidence-spec path (`run_slice_construction_v1.py`) instead of
   threading it from the contract, which is exactly the property the hosted
   contract test enforces for the workflow; and it passes
   `--allow-unpinned-duckdb` while recording `python: "3.12.3"` in its request
   even when running under 3.11.14. Neither affects what the phases prove today,
   but both are places where the harness is not the hosted path.

## Added 2026-07-25, from the review of the per-place positions artifact (PR #157)

All five were raised in review and deliberately deferred: none of them can lose
data, and each is a hardening or cost item rather than a correctness gap. The
artifact itself is described in `construction-v1-state.md`.

1. **`validate_positions` trusts the inlined directory.** It verifies each pack
   object and directory object against its recorded sha256/bytes, but it does
   not re-read the directory object and compare it to the copy inlined in the
   marker, does not compare `pack["records"]` against the pack's real parquet row
   count, and does not check `positions["shuffle_bucket_bits"]` against the
   marker's own limits. The term packs have the same inline-vs-object pattern, so
   this is a shared hardening item rather than a new one.
2. **Marker JSON growth from inlined per-cell directories.** Each positions pack
   inlines a per-row-group and per-cell record-count directory. At planet fan-in
   (`max_fan_in_tasks` 128, ~89 map tasks) this could add ~1-2 MB per marker,
   which then travels with every phase that loads markers. Re-estimate against
   the per-RECORD emission actually shipped, and if it is material, keep only the
   directory object and drop the inline copy.
3. **`emit_positions` runs outside any `StageWatchdog`.** It sits between ingest
   and the watchdog-wrapped pack export, so its scratch and wall time are absent
   from `construction_evidence`. Small today (one tagged parquet plus one file per
   bucket), but it is the only map stage with no resource evidence at all.
4. **`positions_directory` is an O(rows) Python loop.** Term directories are
   built by the Rust proof binary; this one reads `partition_cell` per row group
   in Python. Fine at slice scale, and it is deliberately not the Rust binary
   (positions rows carry no term digests), but a planet task's cost has not been
   measured.
5. **Row-group skipping is currently vacuous.** Hosted `parquet_row_group_rows`
   is 65,536 and a Monaco positions pack holds ~18k rows, so DuckDB writes ONE
   row group per pack and the per-row-group directory buys nothing yet. It starts
   to matter at planet density; until then the per-cell summary is the useful
   half.

## DEFERRED, do not lose — now DECIDED: the address map shuffle will NOT be ported

Raised and deliberately deferred on 2026-07-25 so Places could be proven first;
**decided against on 2026-07-25** (Brad, explicitly). Nothing about it was ever
started, and now nothing should be. Full reasoning in `construction-v1-state.md`.

The prohibitions in this section stand entirely unchanged and are the reason it is
kept rather than deleted: **do NOT change the address forward partition key**
(`address_key_hash` / `route_hash` / `hash_bucket`), TOTAL_ORDER, SERVING_ORDER, or
the genesis partition plan, and **do NOT widen `MAXIMUM_HASH_BITS = 16`**
(`crates/geocoder-construction/src/main.rs:26`) — that would change
`transform_binary_sha256` and invalidate the frozen address evidence spec v3.

**Addresses have the same BUILD-TIME transport wall and are the bigger half
of it** (this is about moving bytes between phases, NOT query latency --
construction-v1 builds forward indexes only, and address reverse does not
exist; see the query-surface section of `construction-v1-state.md`):
473,576,753 records / 33.2 GB selected uncompressed, against Places'
74,223,561 / 10.6 GB. They run through the identical workflow, so the store
moves as GitHub artifacts exactly the same way, and reduce has never run.

**Do NOT change the address partition key.** It is better than the Places one:

- `address_partition.address_key_hash` is FNV-1a over 8 normalized fields, so
  `route_hash` is effectively unique per address. There is **no indivisible
  group**, unlike Places' `(cell, token)` -- which is the thing that forced the
  combiner, the headroom policy, and the hard floor. Addresses subdivide to any
  depth and each split genuinely halves the load.
- `address_partition.hash_bucket` already takes the HIGH bits
  (`value >> (64 - bits)`), which is exactly the mistake the Places shuffle
  made and had to fix.

**Do NOT port the shuffle. Decided 2026-07-25.** The plan below was
`(country, top-K bits of route_hash)` — the partition key at a fixed granularity —
and it was attractive because `route_hash` is already uniform, partitions map exactly
either way, and the source is largely country-clustered (`exact_country_row_groups`
8,023 of 8,704, only 681 mixed). It is not being done, for two reasons that survive
all of that:

- **The output is ALREADY hash-clustered, so the shuffle buys little.** `pack_id` is
  `row_number() OVER (ORDER BY {TOTAL_ORDER})` (`scripts/address_construction_v1.py:1009-1010`)
  and `TOTAL_ORDER` begins `country, maximum_bucket, ...`
  (`scripts/spike_address_construction.py:77-82`), where `maximum_bucket` is
  `route_hash >> 48` (`crates/geocoder-construction/src/main.rs:26,422`). Packs and
  row groups are therefore contiguous in `(country, route_hash>>48)` already, which
  is why row-group SELECTION is tight (1.55 of 52 groups per partition at 93
  partitions; 2.62 of 53 over 32 partitions on a planet-shaped slice). An explicit
  shuffle would re-derive clustering that the per-task sort already provides.
- **The arithmetic does not close.** Keeping packs at the >=50 MB that makes R2 reads
  efficient forces K ~ 3, and at K = 3 the US (8 buckets per task, 256 partitions)
  still sits at ~32x object amplification. Paying for a whole map-phase change to
  land at 32x is not worth it.

**The load-bearing fix is a range-owning reducer instead** — a port of
`places_construction_v1._reduce_ingest` / `reduce_bucket_range`, in which one reduce
job opens each pack once for a contiguous hash range and emits every partition inside
it. Reduce-side only, no map change, no partition-key change. Queued as separate
work.

**Landed 2026-07-25 in the meantime (reduce-side, no map change):**
`reduce_partition` now calls the staged store's `release()` at each pack's last use,
retaining only what the later partitions of the same job still need
(`construction_v1_hosted._batch_retention`), and enforces the result against
`limits.max_scratch_bytes` after every fetch. **Note the knob's scope changed:** for the
address reduce stage `max_scratch_bytes` governed the temporary workspace alone and now
governs workspace + the hydrated pack cache. No default changed; planet fit is ~9.2 GB
against 24 GiB.

Be precise about what that bounds, because a first draft of this note overstated it.
It does NOT bound resident input to "about one pack": that figure came from a
SINGLE-map-task fixture, whose packs are one global sort. The map-side sort is
INTRA-task, so the law is
**`peak ≈ (map tasks holding this partition's country) × pack bytes`, batch-INDEPENDENT
above batch 1** — verified by splitting the same slice across 1/2/4/8/16 tasks and
measuring peak at exactly 1.00/2.00/4.00/7.99/15.99 packs. Planet: ~39 tasks hold the
US, so ~4.05 GB, or ~8.1 GB if all 77 mixed-country tasks are also selected. Single-
digit GB, ~127 packs worst case. Derivation and the full tables are in
`construction-v1-state.md`.

What the release DOES fix is that peak previously grew with batch size until it reached
the ENTIRE pack set (1.77 / 2.66 / 3.65 / 11.62 MB at batch 1 / 4 / 8 / 32 on the
single-task slice, the last being 100% of it). `--max-reduce-jobs` is now a dispatch
input on `construction-v1.yml` so object amplification is tunable without a code
change — but note it does NOT reduce peak, which is why the scratch-cap check exists.

**Country skew** can still be read off `exact_country_rows` in
`benchmarks/address-construction-v1-data/inventory/addresses.json` (434,397,621 of
473,576,753 records across 34 countries, the remaining 39,179,132 in the 681
mixed-or-unknown row groups); it is what the `predict-reduce` floor uses. It is no
longer a prerequisite for anything, since it existed to set K.

**Related:** `MEASURED_REDUCE_MINUTES_PER_PARTITION["addresses"] = 2.0` is now
CALIBRATED (2026-07-25) and deliberately KEPT at 2.0. Slice measurement: 0.177 s at
3,279 rows, 0.434 s at 14,990, 1.022 s at 52,464, 1.800 s at 104,928 — linear; least
squares gives a 0.168 s per-partition floor plus 1.573e-5 s/row, so a 1M-row planet
partition projects to ~0.265 min of compute plus ~0.12 min of hydration = **~0.39 min**.
2.0 is a ~5x margin, matching the >4x the calibrated Places 1.0 carries; it also feeds
the fail-closed ledger cost projection, where lowering it on slice evidence would be
the wrong direction, and it never refuses a legitimate plan on cost (474 × 2.0 = 948
runner-minutes against a 40,000 cap). What it DOES constrain is batching: it admits 82
partitions per job where ~0.39 would admit 423, which is 6.8x more than the planet plan
needs but means a reduce job cap below ~6 lands within sight of the limit and a cap of 5
exits via `_reduce_batches`. The derivation lives in the comment at
`scripts/construction_v1_hosted.py:MEASURED_REDUCE_MINUTES_PER_PARTITION`. A real
planet reduce measurement should still replace it.

**Also unbuilt for addresses:** there is no slice harness. `build_slice_inventory_v1.py`
and `run_slice_construction_v1.py` are Places-only, so addresses currently have
no fast local loop. Building the address equivalent is probably the actual first
task, since without it any address work repeats the pattern of designing against
phases that have never run.

> **Still deferred after the records artifact (2026-07-25).** The address map now
> emits `overture-address-map-address-records-v1`, which carries a spatial
> `partition_cell` and rides the Places shuffle. That is a COLUMN on a new
> artifact for a future reverse index, not a routing key: `address_key_hash`,
> `route_hash`, `hash_bucket`, TOTAL_ORDER, SERVING_ORDER, the forward pack
> layout and the genesis plan are untouched, and the forward packs are
> byte-identical. It does not start the port above and must not be cited as
> having started it.

> **The R2 staging transport does NOT start it either (2026-07-25).** Both
> families' map output now moves through `staging/global-v2/<request_sha256>/
> construction-v1/<family>/` instead of a GitHub artifact. That is the TRANSPORT
> of the store, not the routing key of its contents: `address_key_hash`,
> `route_hash`, `hash_bucket`, TOTAL_ORDER, SERVING_ORDER, the forward pack layout
> and the genesis partition plan are untouched, the marker records the same keys it
> always did, and the reduce ownership model for addresses is still
> `partition-batch`. The address forward partition key is FROZEN. Do not cite the
> staging work as having begun the port.

> **Status 2026-07-25: DONE.** Both scripts now take `--family addresses`. The
> loop is 104,928 real Overture addresses (Seattle, release `2026-07-22.0`,
> `--bbox -122.34 47.59 -122.30 47.63`) through all five phases in ~9 seconds
> with no credentials. The address shuffle port above is still DEFERRED and
> untouched: the harness runs the existing row-counter pack layout unchanged.

> **Decision 2026-07-25: the port is CANCELLED, not merely deferred.** The two
> status notes above say "still DEFERRED" and were written before the decision; the
> reasoning at the top of this section supersedes them. The map phase is not going to
> be changed. What landed instead is reduce-side only — `release()` at each pack's
> last use plus `retain_keys` for the rest of the job — and what is queued instead is
> a range-owning reducer. The partition-key prohibitions in this section are
> unaffected and remain in force.

## Added 2026-07-25, from the adversarial review of the hygiene bundle (#166)

Recorded rather than fixed. None can lose data; each is a hardening or
consistency item on surfaces the bundle touched.

1. **The rehearsal hardcodes the spec path while `assemble` takes `--spec`.**
   `rehearse … rehearse` reads its partition caps from the module-level
   `EVIDENCE_SPEC` constant, but `rehearse … assemble` takes `--spec` and records
   *that* file's sha256 into the evidence. Point them at different files and the
   caps USED and the spec sha RECORDED diverge silently. The workflow comment at
   `construction-v1.yml:305-309` demands the opposite convention (thread the spec
   path, never hardcode it), and `run_slice_construction_v1.py` has the same
   hardcode already recorded in item 5 of the #159 review section. Fix: thread one
   spec path through both subcommands.
2. **Five more spec gates are still dead declarations restated as rehearsal
   literals.** `maximum_map_pack_rows` (500,000), `parquet_row_group_rows`
   (131,072), `maximum_fan_in_map_tasks` (16), `maximum_fan_in_packs` (64) and
   `adaptive_subdivision_max_depth` (8) are all declared by spec v2 and all
   re-typed as literals or argparse defaults in
   `rehearse_places_construction_v1.py`. The partition caps were fixed by reading
   them from the spec; these five have exactly the same shape and were left out of
   scope. Read them the same way.
3. **Reconciliation binds identity to COUNTS, not bytes.** Both families now
   compare each reduction's binding to the plan's binding for that partition, but
   a binding is records plus two semantic sums — so two partitions whose bindings
   are genuinely equal can still swap their published CONTENT undetected. The
   places reducer's `emit_verification` already binds published bytes to the
   partition's identity per partition; the finalize-side check does not, and this
   is pre-existing for both families.

## Added 2026-07-25, from moving the map store to R2 staging

The transport landed (see the "store transport" section of
`construction-v1-state.md`). These are the items deliberately NOT done in it.
None can lose data: every one of them is a cost or hardening item, and the
fail-closed rules are already in place.

1. **`path()` hydrates a WHOLE object; row-group range reads are still unbuilt.**
   This is step 2 of `2026-07-24-r2-staging-design.md` §7 and is the item that
   still has real headroom in it. It is *sufficient* without them — the map-side
   shuffle makes a fragment hold a complete set of cells and nothing else, so a
   bucket-range reduce job fetches only the fragments in its own range rather than
   the whole store — but the reducer only needs the row groups whose
   `routing_groups` name its cells, and the frozen evidence spec already declares
   `selective_read_amplification_max` 4.0. **That bound is enforced by nothing at
   run time**, only by `validate_places_planet_readiness.py` against the
   rehearsal. Range reads and the run-time gate belong together.
   **Related, and specific to the plan phase:** it now hydrates every pack body
   TWICE (a DuckDB planning pass batched at `max_fan_in_packs` with eviction
   between batches, then a one-at-a-time binding pass). Bounded, but it still
   streams the whole term store through the plan runner. The pack proof
   DIRECTORIES already in the markers carry per-row-group routing and record
   counts, so planning from directories alone — never touching a pack body — is the
   real fix. It is not surgical: `adaptive_genesis_plan` needs per-cell
   `count(DISTINCT token)` and a per-partition two-lane binding, and a directory
   carries neither. Deliberately deferred to keep this change to transport.
2. **`ensure_uploaded` reads every uploaded object back.** That is the create-only
   verification contract and it is why the transport is trustworthy, but it means
   map downloads what it just uploaded: ~34 GB of extra planet GETs, ~380 MB per
   map task. Cheap, measured, and worth revisiting only if it shows up in the
   wall-clock — do NOT drop the readback to save it without replacing the
   verification with something equally strong.
3. **`aws s3api` is one subprocess per object.** Fine for whole objects and it is
   what `S3Store` already does everywhere else, but it is the wrong shape for
   range reads, so item 1 probably wants a real HTTP client with connection reuse
   rather than shelling out per request.
4. **R2 Class B request volume is unestimated.** One 63.5 GB download becomes many
   small GETs. At 256 buckets and ~89 map tasks the object count is ~22.5k per
   family, which is trivially cheap; row-group range reads would take it to
   10^5–10^6. Estimate it before the first planet dispatch, not in a bill.
5. **A locally-cached object is not re-verified on read.** `path()` verifies
   everything it HYDRATES against the digest in the key, but it trusts a local file
   that already exists, exactly as `LocalObjectStore.path` always did. An earlier
   draft of this item justified that with "callers re-digest what they read
   anyway"; **that was wrong at the publication point**, and the review
   demonstrated it — a pre-planted wrong-bytes file at a right key was published
   with `reconciles: true`. Finalize now checks every file against the digest in
   its key and against the identity its producing phase recorded, so the
   publication hole is closed. The remaining gap is narrower and stated
   accurately: a consumer that only READS a cached object (the reducer, head)
   relies on its own per-pack SHA check, and `path()` itself still does not
   re-verify.
6. **Nothing expires an abandoned construction-v1 staging prefix yet, and it is
   now load-bearing.** `r2-cleanup.yml` can delete one by hand — its phase-2 guard
   matches the prefix shape this transport writes, deliberately, and it now REFUSES
   a prefix containing `construction-v1/` unless `allow_construction_staging=true`,
   so a one-input dispatch cannot destroy a live multi-hour run. But there is still
   no lifecycle rule and no failure-path wipe, and a failed planet run leaves up to
   ~34 GB of staging debris per family. Same class as item (a) below for the rebuild
   workflow, and it should probably be fixed the same way.
7. **The head manifest bakes the absolute `--store-root` path into its own
   digest.** `shard_entries` carries `path` (`str(store.path(key))`) and the
   manifest embeds every field except `key`, so the manifest's bytes depend on the
   runner's directory layout. It is why a staged and a `--no-staging` slice run
   differ by 96 bytes in the `serve/` store class. Harmless today only because
   `_artifact_keys` publishes `shard_objects` and not `manifest_object`, so the
   manifest never reaches the slice — which is itself worth a look, since the head
   manifest is the only thing that disambiguates PLHD shards (item 7 of the "test
   hygiene" section). Fix: drop `path` from the manifest projection, or key it
   relatively.
8. **`FilesystemStore.upload` has a crash window between `os.link` and the sidecar
   write.** The object is published create-only and atomically, then its
   `.metadata.json` is written; a crash in between leaves an object whose `head()`
   reports `sha256: None`. Content-addressed keys survive it (`path()` verifies
   against the digest in the key), but a MARKER would then abort forever, since
   `read_json` fails closed on missing digest metadata and the object cannot be
   rewritten. Test-and-rehearsal backend only; R2's `put-object --metadata` is one
   atomic operation. Fix: write the sidecar first, or hold the digest in the object
   name.
9. **A second dispatch under a DIFFERENT confirmation but the same
   `request_sha256` hard-fails rather than degrading.** Two concurrent runs share a
   staging prefix by design (that is what makes resume free), and create-only means
   the second one's `ensure_uploaded` raises on any object whose bytes differ. That
   is the correct direction, but the failure surfaces as a mid-run
   `ValueError: existing object identity differs` on an arbitrary map task rather
   than as an admission-time refusal. Worth an explicit up-front check.
10. **`admit-task --remote-root` plus `--marker-out` is a trap.** Completion
    observed only through the `FilesystemRemote` HEAD path carries no payload, so
    `--marker-out` raises `SystemExit` rather than silently dropping the task from
    the fan-in. Correct, but it means the two flags cannot be combined; the hosted
    workflow uses `--marker-out` and not `--remote-root`, so nothing hits it today.
11. ~~**The plan job does not assert it received every map marker.**~~ FIXED in the
    same change: the plan job now counts the fanned-in markers against
    `admit.outputs.map_task_count` and fails closed on a mismatch. With the store
    out of the artifacts, a missing marker was the only remaining way a map task
    could silently vanish from the plan.
12. **PRE-PLANET CHECK: verify head-candidate volume fits.**
    `Limits.max_head_candidate_rows` is 5,000,000 and the Monaco slice produced
    69,069 head candidates from 38,182 places — about 1.8 candidates per place. A
    straight-line planet projection over 74.2 M places is ~134 M candidates, ~27x
    the cap. The relationship is certainly sublinear (candidates are top-N per
    token and the planet shares tokens far more than a 38k slice does), which is
    exactly why it must be MEASURED from a real planet map run rather than
    projected. Do this before dispatching a planet head phase; it is a fail-closed
    cap, so the failure mode is a clean abort late in an expensive run.
13. **The head phase's candidate hydration is MEASURED, not bounded.**
    `build_sharded_global_head_from_markers` hands every task's head-candidate pack
    to a single `read_parquet([...])`, so unlike the plan phase it cannot
    batch-and-evict without restructuring how DuckDB reads them, and its peak
    resident bytes equal its total by construction. Monaco measures 7.35 MB from one
    map task; at 89 planet tasks the straight-line figure is on the order of 10 GB
    on one runner. `run-head --staging-report` now records it and the job carries the
    same 25 GB free-disk floor as every other phase, so the number comes from a run
    rather than an estimate — but it is a measurement and a floor, not a bound.
    Batching it (per-shard candidate ranges, or an incremental INSERT loop like the
    plan phase's) is the fix, and it is a restructure rather than a flag.
14. **A staging prefix written before the routed-publication fix cannot be resumed
    for reduce, and that is the intended outcome.** The per-partition reduce
    completion marker's payload changed: it recorded
    `{"partition_index": n, "artifact": null}` for places and now records the real
    `routed_object` identity. Markers are create-only and refuse an overwrite with
    different bytes, so a resumed run over a pre-fix prefix hard-fails on every
    reduce batch. **Remediation is to abandon that prefix and dispatch a fresh
    request (a new `request_sha256` gives a new staging root).** Do NOT make the
    marker key-compatible or loosen the create-only rule to paper over it: the old
    payload recorded `artifact: null` precisely because the object was not being
    published, so accepting it would mean resuming a run whose serving set was
    wrong. No planet execute has ever run, so today this affects local work
    directories only.
15. **`leaf_object` is produced, proven, staged with a full readback -- and read by
    nothing on the hosted path.** The reducer digests the leaf against the plan
    binding (that check is load-bearing and stays), then `put_content` uploads it to
    staging and `ensure_uploaded` downloads it again to verify. Monaco: 16.44 MB,
    **46% of the slice's serving volume**, scaling ~linearly to the planet. The only
    readers are `build_global_head` (superseded by
    `build_sharded_global_head_from_markers`, which reads head-candidate packs
    instead) and the rehearsal oracle. So on the hosted path it is pure transport
    cost. Either stop uploading it (keep the local proof, skip the staging write) or
    record why it is kept -- an unpublished intermediate that nothing reads is worth
    a deliberate decision. Deliberately NOT changed alongside the routed-publication
    fix: that PR is about what gets published, and this is about what gets staged.

## Added 2026-07-25: R2 cleanup approved, plus its recurrence fixes

An audit of `geocoder-shards` (981.76 GiB) produced an owner-approved five-phase
cleanup, retargeted into `.github/workflows/r2-cleanup.yml`: abandoned id-index
`staging/` under `2026-07-02.0` and `2026-07-02.2` (112.67 GiB), the orphaned
bucket-root `staging/global-v2/165317ba…/` prefix whose pipeline was deleted in
PR #154 (55.08 GiB), the failed never-catalogued `2026-07-17.0/` (260.77 GiB),
the `immutable/map/addresses/objects/` subtree under `staging/global-v2/59f326dc…/`
with `manifests/`, `reports/` and `inventory/` retained as the 2026-07-22
benchmark evidence (23.62 GiB), and a catalog prune plus delete of `2026-07-02.1`
(129.12 GiB). `2026-07-13.0`, `2026-07-18.0` (the frozen unpromoted core),
`2026-07-02.3` and `backups/` are asserted protected in every phase. Three
tracked items fall out of it:

(a) **The rebuild workflow should wipe its own `{version}/staging/` on failure.**
    Both phase-1 targets are staging trees a cancelled run left behind; nothing
    cleans them today, so this debris recurs every failed rebuild.
(b) **Byte-identical id-index triplication should be deduplicated.** The
    `2026-07-17.0` id-index is byte-identical to `2026-07-13.0`'s, and the same
    bytes exist a third time. Either content-address the id-index shards or have
    a version reference another version's index instead of copying it.
(c) **Decision for the owner, unchanged here: the monthly scheduled rebuild is
    currently a no-op** because `vars.ENABLE_SCHEDULED_REBUILD` is unset. That
    is recorded, not fixed — enabling it is the owner's call, and it changes when
    new versions (and new staging debris) appear.

## Added 2026-07-25, from bounding the finalize/publication phase

The eager-hydration defect #167 fixed in the plan phase was also present in
finalize, in both dimensions, and is now fixed there too (see the hydration table
in `construction-v1-state.md`): `publish_exact_set` held every artifact's full
bytes in one `payloads` dict, and `cmd_finalize` built its exact set out of
`store.path(...)` calls with no `release()` anywhere in the phase. Peak is now one
object in RAM and one object on disk, and both slice-smoke jobs assert
`finalize_staged_peak_resident_bytes < finalize_staged_bytes_hydrated` and
`finalize_staged_objects_released > 0`, **and so does the hosted
`construction-v1.yml` finalize job** — on `staged_objects_hydrated > 0`,
`staged_objects_released > 0` and
`staged_peak_resident_bytes < staged_bytes_hydrated`, fail-closed through
`| numbers` so a missing or non-numeric key exits non-zero instead of defaulting
to a value that passes. Every staging assert in `slice-smoke.yml` was converted to
the same shape: a bare comparison is NOT fail-closed, because jq orders `null`
below numbers (a MISSING peak satisfies `peak < hydrated`) and numbers below
strings (a STRING count satisfies `> 0`). Both holes were reproduced against real
jq before the change and are closed after it; the contract test greps for any
surviving bare `.*staged*` comparison so the old shape cannot be copied back in.

Splitting `publish_exact_set` into an admission pass and an upload pass also lost
an invariant that had held **by construction** — *the admitted identity and the
uploaded payload are the same bytes*, previously guaranteed because one
`read_bytes()` produced both. Nothing else covered the second read: a
`local_member` (the two manifests) is digest-checked on neither read; a staged
member's re-hydration hits `StagedObjectStore.path()`'s `if path.is_file(): return
path` short-circuit, so it is not digest-checked either, and `release()` opens a
window in which that cache slot is unverified-writable which did not exist before;
and the per-upload HEAD compares only `bytes`, so a same-length swap passes it. The
upload loop now re-hashes the payload against the pre-admitted digest — free, since
the payload is already in RAM — and all three attack shapes are pinned by tests
that were each confirmed to fail without it. What was deliberately NOT done:

1. **Finalize reads each published object from staging TWICE.** Admission streams
   every file to compute its identity (and to run the content-addressed and
   provenance gates), then the upload loop re-reads the payload it publishes. That
   is what preserves "the whole admitted set is fixed, gated and sorted before any
   upload" while holding only one object, but it doubles finalize's GET volume:
   ~13–18 GB becomes ~26–36 GB of planet reads. The one-read version is available
   — every object's `sha256` and `bytes` are ALREADY recorded by the producing
   phase (that is what the #168 gate compares against) and its digest is in its
   own content-addressed key, so admission could be built from provenance with no
   hydration at all, leaving a single read that verifies the payload it uploads
   against the pre-admitted identity. Not done here because it changes
   `publish_exact_set` from "hash the file" to "verify against a declared
   identity", which is a contract change in the publication path and deserves its
   own review rather than riding along with a residency fix.
2. **There is no `max_remote_read_bytes` cap, and there never was.** `Budget` takes
   three independent limits, but `construction_v1_hosted.py` populates
   `max_read_bytes` from the contract's **`max_remote_write_bytes`** in both places
   it builds one (`:416` and `:1624`), and no `max_remote_read_bytes` key exists
   anywhere in the repo — not in `construction_v1_control.py`'s caps, not in the
   contract derivation, not in a test fixture. So the remote read budget cannot be
   tightened without also tightening writes, and the 1 TB default is doing double
   duty. Both quantities are ~1.3–1.8% of it at planet scale, so nothing is close
   to tripping; this is a shape problem, not a live one. Fix it by adding the key
   with its own default rather than by widening the write cap.

   Related and worth stating precisely, because it is easy to get backwards: the
   doubled reads in item (1) are **STAGING** GETs, and `Budget` does not govern
   them at all. It wraps only `FilesystemRemote`, i.e. the publication target;
   `StagedObjectStore` and `r2_verified_store` charge nothing. Measured on a
   10-object fixture: `budget.read_bytes` is **0** after `publish_exact_set`
   (20 hydrations for 10 objects) and exactly 1× the set after
   `verify_whole_slice_once`. So the transport that moves tens of GB per phase has
   no byte budget of any kind, while the publication path has one it shares with
   writes. That asymmetry is the thing to fix, and it is bigger than finalize.
3. **The `publish/` mirror tree is still the whole slice on local disk.** Finalize
   writes the published set into `--remote-root publish` and a separate
   `aws s3api` step mirrors it to R2 object by object, so the phase's real disk
   floor is the full slice (13–18 GB at planet scale) plus the one object it holds
   resident — not the "twice the slice" it was, but not one object either.
   Streaming each object straight from staging to R2 in the publisher would remove
   the tree entirely; that means giving `publish_exact_set` a real R2 backend
   instead of `FilesystemRemote` plus a shell mirror, which is the same
   restructure item (1) above touches. The 25 GB free-disk floor on the finalize
   job is what stands in for it today.
4. **`StagedObjectStore.path()` does not re-verify a cached object, and eviction
   now makes that reachable.** This is the pre-existing item (5) of the R2-staging
   list above, but the two-pass publisher changes its exposure rather than merely
   inheriting it: `release()` unlinks a cache slot, and until the next `path()` runs
   there is a path on disk that the store will hand back on `is_file()` alone with
   no digest check. Contained at the publication point — the upload loop re-hashes
   the payload against its pre-admitted digest, which is what closed it — but the
   general fix is for `path()` to verify what it returns from cache, not just what
   it hydrates. Worth doing because the reducer and head phase read cached objects
   through the same short-circuit and rely on their own per-pack checks instead.
5. **The R2 mirror's conflict fallback accepts a pre-existing object on existence
   alone** (`construction-v1.yml`, the `aws s3api put-object … || head-object`
   pairs). For a content-addressed key that is nearly harmless, since the name
   proves the bytes; for the three keys that are NOT content-addressed —
   `family-manifest.json`, `slice-manifest.json` and the completion marker — it
   means a differing pre-existing object is accepted as a successful publish. This
   is outside the present change (the mirror is a separate shell step, and
   `publish_exact_set`'s own `ConflictError` path DOES compare bytes), and it is
   logged rather than fixed here.

## Added 2026-07-25, from the review of the head shard count and its manifest (#169)

All three are fail-closed today and none is a regression; they are recorded because
the review confirmed them by construction, not by guess.

1. **Nothing at RUNTIME checks that the head manifest's `shards[].path` values are
   the objects actually published.** The manifest is now the head's routing table,
   and finalize's exact-set equality
   (`serving == reductions + populated_shards + head_manifest_objects`) counts
   objects without cross-checking the manifest's contents against them. So a head
   phase that handed over a mismatched object in a shard slot would satisfy the gate
   and publish a manifest naming an object that is not in the slice. Only a TEST
   asserts the subset property (`tests/test_construction_v1_hosted.py`,
   `test_places_finalize_publishes_every_routed_object`). This is the most
   interesting of the three: it wants a real assertion in `cmd_finalize` — every
   `shards[].path` must appear in the published set, and every published `.plhd`
   must appear in the manifest — which is a cheap set comparison over data finalize
   already holds. The Rust sharded verifier does check the manifest against the
   shard BYTES, but it runs in the head phase, against the head phase's own view.
2. **A duplicate manifest/shard key aborts with a bare `ValueError: duplicate
   artifact key`** out of `publish_exact_set`, rather than the named `SystemExit`
   every other finalize precondition raises. Correct behaviour, worse diagnosis.
3. **`_artifact_keys` does not validate `shard_count`/`shard_bits`** the way it
   validates `shard_objects`, `populated_shards` and `manifest_object`, so a places
   head result missing either reaches `head_block` construction and dies with a
   `KeyError` instead of a message naming the field. Same class as (2).

## Added 2026-07-25: the finalize publication budget, and what it left open

`max_remote_operations` was 100,000 against a projected planet publication of
~133,000 operations, and it is enforced by a running counter inside finalize, so
it tripped part-way through publishing tens of thousands of objects at the end of
a multi-hour run. Fixed by projecting the publication at PLAN time (and in the dry
run) and raising the cap to 300,000 on the structural ceiling — see
`construction_v1_control.CAPS` for the arithmetic and
`tests/test_construction_v1_publication_budget.py` for the executable version.
Three things fall out of it and are NOT done:

(a) **The routed serving lane still has no fail-fast index-entry guard.** The head
    lane measures its worst shard before any encode; the routed lane discovers an
    over-cap partition only as the Rust encoder's `bail!`. Lowering
    `partition_distinct_tokens` to the encoder's `MAX_INDEX_ENTRIES` (250,000)
    means the planner subdivides instead of ever admitting such a partition, so
    the guard is no longer the only thing standing between a plan and a late
    abort — but it is still the difference between "cannot happen by construction"
    and "cannot happen, and is checked". Port the head lane's pre-encode
    measurement (`build_sharded_global_head_from_markers`) onto `reduce_partition`.

(b) **The committed partition plan is admissible under the tightened token cap but
    less buffered than a fresh generation.** `scripts/places_partition_plan_v1.json`
    was generated with `distinct_tokens` 400,000 and `--headroom-fraction 0.5`, so
    every unsplit leaf holds <=200,000 tokens and therefore satisfies the 250,000
    cap. A regeneration at 0.5 of 250,000 would pre-split every leaf over 125,000
    tokens instead, producing a different (larger) tree. Its recorded
    `partition_contract.caps` is now the cap the build enforces; the `headroom`
    block still records the 0.5-of-400,000 run that produced the tree. Fold this
    into the next plan regeneration rather than doing it on its own — it needs a
    full local offline map to measure from.

(c) **The publication projection's head term assumes the production shard count.**
    The plan phase projects `1 << DEFAULT_HEAD_SHARD_BITS` head shards because it
    does not know what `run-head --shard-bits` the head phase will be handed. That
    is the safe direction (a smaller head publishes fewer objects), but it means
    the slice harness is projected as if it published 4,096 head shards. Threading
    the real value through the plan phase would make the projection exact.
