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

### 5. Dry-run does not exercise the execute data plane

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

- `PENDING_WORK.md` is dated 2026-07-19 and its "Next" section still describes
  the abandoned `agent/global-v2-executor` path. It is the largest doc in the
  repo and does not mention construction v1 at all.
- `docs/plans/2026-07-24-places-global-scale-plan.md` and
  `docs/plans/2026-07-24-places-digest-divergence-root-cause.md` both still
  carry `Status: LOCAL SPIKE — uncommitted, no PR, no workflow`, but they are
  committed and their recommendations shipped the same day in #141/#142/#143.

## Already addressed

- **StageWatchdog fail-open.** The watchdog thread is the only enforcement of
  the RSS/scratch/wall caps, and a fault inside it died silently in the daemon
  thread — `__exit__` then reported success and `evidence()` reported zero
  peaks. Fixed to fail closed, with the `psutil` attach moved to the caller's
  thread so a missing dependency fails at the call site.

## Added 2026-07-25, from the adversarial review of PR #155

1. **`reduce_partition` has no `StageWatchdog`.** `StageWatchdog` wraps only the
   two `map_task` stages. In the reducer the RSS/scratch/wall caps reach the
   encoder/verifier *subprocesses* via `A.run_bounded`, but the Python +
   pyarrow + DuckDB ingest loop is unbounded. Pre-existing, but raising
   `partition_term_rows` to 2,000,000 doubles the peak of exactly the phase
   nothing is watching. **Fix before the first planet reduce.**
2. **Three evidence-spec hard caps are dead declarations that now disagree with
   the build.** `partition_term_rows_hard_cap` (1,000,000),
   `partition_distinct_tokens_hard_cap` (250,000) and
   `partition_estimated_uncompressed_bytes_hard_cap` (268,435,456) are read by
   no code; the byte one was already violated before this change. The spec ships
   a `.sha256` companion and is meant to be frozen. Either enforce them or
   delete them — a frozen spec stating caps the build ignores is a trap.
3. **`Limits` dataclass defaults were not raised with the hosted limits.**
   `places_construction_v1.Limits` still defaults to 1,000,000 / 250,000 /
   256 MiB, and `rehearse_places_construction_v1.py` explicitly pins
   1,000,000 / 250,000. Hosted overrides win so nothing breaks, but rehearsals
   now plan at caps the hosted build no longer uses and are not representative.
4. **`predict-reduce` was 14x optimistic and this change made it worse.** Fixed
   in PR #155 by flooring the prediction with the committed plan's partition
   count. Worth a follow-up: the addresses branch of the same function divides
   records by a row cap with no structural floor, and may have the same defect.
5. **The committed plan is only read by `predict-reduce` and the generator.**
   Map-side partition assignment (the fail-closed gate) is still unbuilt, so the
   tree does not yet control anything.
