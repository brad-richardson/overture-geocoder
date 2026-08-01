# One resumable planet job: scope

Date: 2026-08-01. Status: **scope only, nothing implemented.**

Operator ask, verbatim in intent: *build the whole planet as a single job, not
four different workflows; ensure it is easily resumable; minimise the arbitrary
copies.*

This document scopes that against the pipeline as it exists today. It is
deliberately a scope and not a design document — the design half already exists
as **Track C** of `2026-07-28-planet-build-wall-clock-review.md:411`, which has
never been executed. What was missing was a statement of what "one job" collides
with in the current code, and that is what follows.

---

## 1. What the planet path actually is today

Measured by reading the workflows on 2026-08-01, not recalled.

| workflow | jobs | boundary it introduces |
|---|---|---|
| `construction-v1.yml` | 9 (admit, resume_inputs, finalize_resume_inputs, binaries, map, plan, reduce, head, finalize) | GitHub-artifact hand-off between every phase |
| `reverse-v2.yml` | 3 (plan, reduce, catalog) | separate dispatch, separate identity |
| `release-slice-families.yml` | 6 | separate dispatch |
| `promote-v2-release.yml` | 5 (probe, promote-slice, publish-release, promote-catalog) | separate dispatch, concurrency group `r2-v2-publication` |

**23 jobs across 4 dispatches.** Every one of the three inter-workflow
boundaries requires an operator dispatch, re-derives identity from a contract,
and re-provisions an environment.

## 2. The three asks are one architecture

Track C's core is: run the 89 Places / 127 Address map tasks through a
**work-conserving queue on a long-lived worker pool**, rather than provisioning a
fresh runner per matrix entry, with immutable content-addressed fragments
remaining the recovery boundary.

That single change delivers all three asks:

- **one job** — the pool spans phases instead of the matrix spanning runners;
- **resumable** — receipts keyed by `(request, owner, map_task_id, object_sha256)`
  replace per-workflow recovery paths;
- **fewer copies** — long-lived owners consume fragments in place instead of
  re-materialising them per phase.

It also subsumes **item 12** outright: binaries stay warm on the pool, so the
per-task `cargo build` disappears without needing the artifact indirection
landed on 2026-08-01.

## 3. What "one job" collides with, concretely

This is the part that was not written down. Each item is a real property of the
current code that a single-job rewrite has to preserve or consciously replace.

### 3.1 The 330-minute job ceiling is load-bearing

`test_every_phase_carries_the_330_minute_job_timeout` pins map/plan/reduce/head
at 330 minutes and finalize at 360. Measured phase wall clock: Places head
**12,381 s (207 runner minutes)**, Places reverse **2h15m**, Address reverse
**2h33m**, planet finalize **56m50s**.

A single job containing all phases would exceed the 6-hour GitHub job limit
outright. **"One job" therefore cannot mean one GitHub job.** It has to mean one
*dispatch* and one *resume boundary* over a pool — which is what Track C
actually says, and the distinction should be stated before anyone implements
against the phrase.

### 3.2 Identity is re-derived at every boundary, on purpose

`construction_v1_control.py` pins inventory/spec/readiness by sha256, and phases
check out `ref: producer_commit`. The staging prefix derives from
`request_sha256`. Collapsing dispatches must not collapse this: the request
identity is what makes a resume *safe* rather than merely possible.

Constraint from `2026-07-31-promotion-copy-and-efficiency.md`: changes to
`places_construction_v1.py`, `address_construction_v1.py`, `Cargo.lock`,
canonical caps, or the producer commit **mint a new request identity and staging
namespace**. So the rewrite must batch into one request, not arrive
incrementally.

### 3.3 Resume today is per-workflow, and two special cases prove it

- `construction-v1.yml` carries `resume_inputs` **and**
  `finalize_resume_inputs` as separate jobs, plus a `head_only_resume` and a
  `finalize_only_resume` mode.
- Promotion had to special-case a finalize-only run that carries no
  `cv1-reduce-*` at all.

A single resume entry point is the actual deliverable of the "easily resumable"
ask. The mechanism already exists — markers written last, content-addressed
staging, `read_json` falling back to the staging prefix — it is simply not
composed across the four workflows.

### 3.4 The copies, itemised and measured

| item | copy | measured cost | status |
|---|---|---|---|
| 1 | promotion `CopyObject`s every forward serving object between two content-addressed prefixes differing only by prefix | 158 GiB, 42,058 objects | **open** — reverse already avoids it by prepositioning |
| 6 | reduction records only in GitHub artifacts | 7-day promotion deadline | **data half landed 2026-08-01**; consumer still reads artifacts |
| 9 | copy client could not retry a definite 5xx | one 4h17m run discarded | **landed 2026-08-01** |

Item 1 is the largest single arbitrary copy in the system and the clearest
"minimise the copies" win. Reverse already demonstrates the fix works.

## 4. Recommended sequencing

1. **Finish item 6's consumer** — switch `promote-v2-release.yml` to
   `export-reductions` with an artifact fallback. Small, removes the deadline,
   and removes one inter-workflow coupling before anything larger.
2. **Item 1, zero-copy promotion** — preposition forward objects the way reverse
   already does. Biggest measured copy win, and independent of Track C.
3. **Write the Track C owner contract** — Wave 5 item 1: owner identity, exact
   expected task set, receipt, conflict, seal, lease, restart, cleanup. This is
   where the "one job" ambiguity in §3.1 gets resolved explicitly.
4. **Prototype one owner** against the preserved planet marker/object set
   (Wave 5 item 3) before touching the workflows.
5. **Then** collapse dispatches, batched into a single new request identity.

## 5. What this scope does NOT claim

- **No wall-clock estimate.** Track C is described in the wall-clock review as
  "the credible approximately two-hour architecture", but no owner prototype has
  ever run. Treat two hours as a design target, not a measurement.
- **No claim that ARM changes the arithmetic.** Construction moved to
  `ubuntu-24.04-arm` on 2026-08-01 on the operator's observation that the runners
  are faster; that has not been measured on this pipeline, and no phase timing in
  this document reflects it.
- **This does not unblock the rebuild.** The rebuild queue in
  `construction-v1-state.md` is separate, and the quality blockers there
  (RC2/RC3, duplicate collapse, the 98 tied `(token, prominence)` groups that
  exceed the cap) are untouched by anything here.
