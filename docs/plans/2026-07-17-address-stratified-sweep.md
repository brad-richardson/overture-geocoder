# Stratified multi-task address sweep

Date: 2026-07-17
Status: scoped design + implementation spec. This is sequencing step 3
(PENDING_WORK.md): the evidence input for ratifying the address partition
rule, normalization contract, and planet storage estimates. Independent of
the Places read-path work; touches only the address rehearsal workflow and
scripts.

## Goal

Extend the proven two-task rehearsal
(`.github/workflows/rehearse-address-r2-map-reduce.yml`, currently a
hardcoded matrix of US task 48 + Mexico task 3) into a stratified ~12-task
sweep over the fixed 127-task inventory, producing the retention/byte/
wall-time/RSS/retry-amplification distribution that the two-task run cannot:
structured retention varied 40.29%→~100% regionally, and the roadmap forbids
extrapolating one regional ratio.

## Task selection (deterministic, from the checked-in inventory)

Selection must be computed by a small committed script (with a unit test)
reading `benchmarks/address-rowgroup-inventory-report.json` `plan.tasks`
(fields: `index`, `rows`, `selected_compressed_bytes`,
`exact_country_rows`, `mixed_or_unknown_country_rows`) — not a hand-typed
list — and its output committed as the workflow's default matrix. Strata,
with the indices the current inventory yields (sanity reference, computed
2026-07-17):

| stratum | rationale | indicative tasks |
|---|---|---|
| continuity anchors | already measured; regression reference | US 48 (also the max-compressed-bytes task), MX 3 |
| CJK Japan | CJK tokens/normalization, bytes-per-row divergence | JP 117, JP 121 |
| CJK traditional | second CJK script family | TW 105 |
| Latin high-density non-US | BR is the second-largest population | BR 16 |
| Latin Europe | different address-level conventions | IT 84, FR 75, DE 85 |
| sparse tail | min-rows task (154,296 rows) exercises small-task overhead | task 126 |
| mixed/unknown-country | ~50% mixed rows; stresses the country partition rule | task 8 |
| US mid-range | separates US-48-specific effects from US-general | US 10 |

Twelve tasks total. The script must emit the exact matrix JSON (name +
task_index per entry) deterministically ordered, and record per-task expected
rows/bytes from the inventory for later reconciliation.

## Workflow changes

- Replace the hardcoded two-entry matrix with `fromJSON` over the committed
  selection (mirrors the backlog direction already accepted for the ID
  workflows). A `workflow_dispatch` input may override the task list for
  debugging, validated as data, defaulting to the committed selection.
- `strategy.max-parallel: 4` — the global design fixes 4 as the starting
  parallelism to avoid amplifying Overture S3 scans and R2 requests;
  increases require measured evidence, not this PR.
- Keep every existing per-task step unchanged: projection, map, partial/
  repeated verified resume, empty/stale restore, byte-identical local-oracle
  reduce, cleanup, `always()` evidence upload, run-unique non-promoting
  prefixes (decision 8 constraints all stand).
- Add a fan-in aggregation job (`needs` all matrix jobs, `if: always()`)
  that downloads the per-task `resume-measurement-*.json` artifacts and emits
  one summary artifact (JSON + markdown): per-task structured-retention %,
  fragment bytes, map/reduce wall seconds, peak RSS, retry read
  amplification, output B/retained-row, plus min/median/p95/max across
  tasks and a reconciliation of projected rows against the inventory's
  per-task expectations. Partial failure must not lose the completed tasks'
  evidence.

## Envelope check (from measured anchors)

US task 48 measured map 157 s / reduce 148 s / 542 MB fragments / 866 MB map
RSS; Mexico task 3 was smaller on every axis. Twelve tasks at max-parallel 4
is roughly an hour of wall clock and well inside the 6-hour hosted job limit
and 14 GB disk per job. If any task exceeds the measured envelope materially,
that is a finding to report, not a reason to retry-loop.

## Acceptance gates

1. Workflow-contract tests updated (`tests/test_address_real_r2_resume_workflow.py`)
   and green; selection script unit-tested against the checked-in inventory.
2. A dispatched run completes all twelve tasks with the full per-task
   verified-resume evidence and the aggregation artifact, including at least
   one task per stratum.
3. Byte-identical local-oracle reduce (`local_oracle_match: true`) holds for
   every task; any mismatch is a stop-and-report finding.
4. Cleanup verified per task; partial runs stay undiscoverable; nothing
   touches the production catalog.

Exit artifact: the aggregated multi-task evidence report — the input for the
coverage/partition-contract review (open decision 2). Committing the
benchmark report into `benchmarks/` happens as a follow-up PR after the run,
matching existing practice.

## Non-goals

Selecting the final partition rule (that is the review this evidence feeds),
any publication-path change, Unicode normalization changes, raising
max-parallel, and any promotion or catalog mutation.
