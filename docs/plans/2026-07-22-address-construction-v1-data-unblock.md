# Address construction-v1 data-unblock result

Status: **blocked on a measured producer/consumer schema gate**.

The current inventory script regenerated and validated the exact public
2026-06-17.0 inventory using the same arguments as
`global-v2-family-build.yml`. Its identities are:

- inventory file SHA-256: `7b13abc149fee69d4931d04dd4f98ed65336e9685d8f3422c6598aa729f1db19`
- canonical inventory: `6a306fc9937dac82602dbc5233952c1f74fdb0f7467ad4cc38dcc559dfc9d34e`
- source inventory: `aa196a40730676efef70413d45bdcadaada3df07c94599b954efd38cd096ec37`
- schema fingerprint: `05260dc6878478fe750a82ad3fb9ddd2fdffcda3f25c00f950acfccca132d7e0`

Evidence spec v2 was frozen before candidate observation at SHA-256
`47bdfc92727725628d4082b7e9a1f8a46959e0f417a16048322909081aba96ec`.
It preserves every v1 numeric gate and fixed-role rationale.

The existing bounded projector successfully materialized the exact twelve-task
census universe: tasks 3, 8, 10, 16, 48, 75, 84, 85, 105, 117, 121, and 126.
Together they contain 40,058,245 rows in 1,482,525,562 stored bytes. The pinned
selected compressed source columns total 1,406,162,784 bytes. Because macOS has
no `/proc/net/dev`, the projector correctly reports network-byte measurement as
unavailable; no transfer number is fabricated. Projection took 463.61 seconds
in aggregate and peaked at 1,429,176,320 bytes RSS. Individual outputs ranged
from 4,967,458 to 160,973,155 bytes and 3.73 to 73.64 seconds.

Candidate admission then stopped on the smallest task, 126, before any census
or benchmark result was observed. `experiment_hosted_rowgroups.py` emits
`source_object_index`, `source_row_group`, and `source_row_index` as signed
`int32`. `address-transform-v1` requires `uint32`, `uint32`, and `uint64`.
The exact error was `input column source_object_index has the wrong Arrow type`.

This is a construction interface decision, not a tunable benchmark limit. Per
the frozen-gate rule, no adapter or post-observation contract change was added.
The fail-closed readiness validator reports `ready: false`; the canonical
inventory/runtime checks pass and scale evidence is absent because admission
failed.

No remote state was written and no workflow was dispatched. No deployment,
publication, commit, push, or Places work was performed.

## Corrected-boundary continuation

The internal physical mismatch was subsequently corrected under explicit review:
the Rust transform now accepts only the projector's signed `int32` locator
columns, rejects negative rows as `invalid_source_locator`, and widens admitted
values to the existing `uint32`/`uint32`/`uint64` logical output. Independent
tests cover zero, `INT32_MAX`, negative rejection, and task-fatal typed
corruption. Evidence spec v3 binds the corrected release binary and is frozen at
`130207f3debde346cc9c1178e5038e2257e883ccd46c5826d1a5ae22c2583af9`;
no numeric gate or selection rule changed.

The transformed census completed for all twelve already-downloaded inputs.
Task 16 deterministically won both selected roles: its largest
`(country, maximum_bucket)` group has 838 rows and its largest exact normalized
key has multiplicity 800. The resulting unique required tasks were 3, 10, 16,
and 48.

The first full bounded run, task 3 run A, then failed the frozen 25% resource
headroom gate. Its absolute 4 GiB cap was not crossed, but whole-process peak RSS
was 4,171,661,312 bytes. The 25%-headroom acceptance ceiling is 3,221,225,472
bytes, so measured headroom was only 2.8709%. Peak scratch was 2,476,101,080
bytes and wall time was 127.15 seconds. The same run was deterministic across
two candidate transforms/packs; its disposable Python baseline took 102.50
seconds while the reported candidate transform, materialization, and export
took 10.91 seconds combined. The resource gate nevertheless fails independently
of speed.

Execution stopped immediately without running the second baseline or remaining
role inputs. `readiness-v3.json` is fail-closed with the single blocker `one or
more frozen scale gates did not pass`.

## Memory-remediation continuation

Phase diagnostics attributed task 3's original peak to the disposable baseline,
not candidate construction. RSS was 472,629,248 bytes at baseline entry,
1,981,153,280 after payload loading, and 1,999,028,224 after sorting, then crossed
4 GiB while expanding a second full list of row dictionaries. Bounded 65,536-row
Parquet conversion removed that duplicate representation. Task 3 then peaked at
2,277,720,064 bytes with byte-identical baseline Parquet, candidate pack,
summary, proof lanes, counts, and deterministic second candidate output.

Task 10 exposed the remaining full payload list, reaching 3,768,582,144 bytes
after its in-memory sort. The baseline was replaced with the same Python decode,
row-object, semantic, total-sort, and Parquet work using 250,000-record closed
sorted spills, a capped 16-way deterministic merge, and 65,536-row output
batches. A forced multi-chunk fixture is byte-identical to the prior in-memory
baseline. Task 10's baseline then peaked at 826,753,024 bytes, spilled
942,739,465 bytes, and cleaned its scratch directory.

Task 10 nevertheless narrowly failed the unchanged whole-run headroom gate due
to retained DuckDB allocator memory between deterministic candidate runs. The
first construction peaked at 2,974,662,656 bytes and ended at 2,869,723,136;
the second began at 2,869,870,592 and peaked at 3,258,712,064. This is
37,486,592 bytes above the 3,221,225,472-byte acceptance ceiling, leaving
24.1272% rather than 25% headroom. Determinism passed. Execution stopped before
tasks 16 and 48.

Each deterministic construction was then moved into a fresh bounded subprocess
and process group, matching the hosted execution unit. The parent receives only
compact JSON; it verifies zero scratch after child close and complete child exit
before starting the next run. Focused coverage proves distinct PID/process-group
identities and byte-identical packs/summaries.

Isolation removed inherited allocator state but did not clear the frozen gate.
Task 10 child one started at 25,231,360 bytes and peaked at 3,053,748,224.
Child two used a different PID/group, started at 25,182,208, and peaked at
3,261,284,352. The second fresh run is 40,058,880 bytes above the acceptance
ceiling, leaving 24.0673% headroom. The outer orchestrator peaked at only
811,433,984 bytes. This attributes the remaining failure to the isolated
DuckDB construction's full transformed Arrow `read_all` materialization and
run-to-run peak variation, not prior-process residency. Execution again stopped
before tasks 16 and 48.

The final candidate correction removed `read_all` from construction. Each fresh
child now reads the transformed IPC as record batches capped at 65,536 rows and
serially registers, inserts into the on-disk DuckDB table, and unregisters each
batch. Python never receives feature dictionaries or tables. A forced
multi-batch regression fails if full-table ingestion returns and also verifies
distinct child process groups, zero retained scratch, and byte-identical
pack/summary output.

Task 10 then passed with 61 ingestion batches; its construction peaks fell to
2,281,619,456 and 2,310,766,592 bytes. Tasks 16 and 48 also passed. Across the
four required roles, the minimum baseline/candidate speedup was 4.02x, maximum
accepted phase RSS was 2,310,766,592 bytes, maximum bounded scratch/output was
5,078,793,470 bytes, maximum wall time was 300.91 seconds, and maximum pack size
was 469,678,813 bytes. All deterministic pack, summary, transformed IPC, logical
lane, and count comparisons passed.

The final scale evidence is `scale-evidence-v3.json`. The fail-closed validator
reports `ready: true` with no blockers under Python 3.12.12, DuckDB 1.5.1,
NumPy 2.3.5, and PyArrow 25.0.0. Focused validation includes multi-task,
multi-pack, multi-row-group, overlap/proof reconciliation, resume-before-
hydration, process-group isolation, and interrupted local-write/immutable/
marker publication behavior.
