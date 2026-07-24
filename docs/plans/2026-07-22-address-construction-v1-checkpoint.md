# Address construction v1 checkpoint

Date: 2026-07-22

Status: checkpoint 1 defaults approved for internal construction-v1 work; not
approved for planet dispatch

The seven choices below were approved as lean provisional defaults. The 16-bit
maximum bucket is a ceiling for this disposable construction contract, not
permanent serving lineage until an operator accepts a planet result. Checkpoint
2 implementation and evidence are recorded in
`2026-07-22-address-construction-v1-checkpoint-2.md`.

## Reset and target

All unshipped Address and Places requests, outputs, intermediate formats,
completion markers, serving bytes, and partition history are abandoned. They
are implementation evidence only and create no compatibility obligation. Do
not resume request
`59f326dc2fd0866f54ead2ce0a1b19b5b9955c565cd8ef662d6bf22fc1047a63`.

The replacement contract is `global-v2-construction-v1`. Its genesis staging
root is:

```text
staging/global-v2-construction-v1/genesis/<request_sha256>/
```

The first target is a complete, independently verified, non-promoting planet
Address + Places slice. It cannot update `v2/catalog.json`. Address proves the
shared engine first; Places follows on the same engine. Publication, public API
decisions, retention, rollback, historical compatibility, and a native range
reader are outside this checkpoint.

## Checkpoint 1 result

The isolated spike is:

```text
bounded PyArrow batches -> Arrow IPC -> Rust transform
    -> Arrow IPC -> on-disk DuckDB -> ordered Parquet pack + summary
```

Python performs only bounded columnar hydration and process coordination in the
candidate path. Rust iterates Arrow arrays and owns semantic admission. DuckDB
owns materialization, grouping, total ordering, and Parquet output. The current
production mapper is unchanged.

The hand-authored fixture covers the complete precedence list, both WKB byte
orders in Rust tests, normalization, UUID bytes, routing, source provenance,
duplicate multiplicity, and total ordering. Literal expected values do not call
the new transform or old mapper. `invalid_record` and `record_too_large` are
named with zero fixture counts; checkpoint 2 needs focused nonzero limit and
typed-corruption tests before planet mapping.

The checked-in real sample contains 17,014 rows, not a representative or
near-cap global task. On the final recorded run the Rust semantic transform took
0.338 s internally and the DuckDB materialize/export stages took 0.261 s. The
disposable Python-row reference took 2.964 s while omitting legacy spills and
summaries. Timings varied materially across local runs, which reinforces that
only the frozen near-cap suite may set a planet gate.
Both candidate runs produced identical Arrow IPC, pack, summary, logical
bindings, accounting, and rejections. This is directionally compelling but is
not planet-scale acceptance evidence. See
`benchmarks/address-construction-spike-report.md`.

## Provisional Address logical schema

Every admitted row has these non-null fields unless noted:

| field | Arrow type | meaning |
|---|---|---|
| `country` | UTF-8 | normalized routing country |
| `maximum_bucket` | uint32 | high 16 bits of `route_hash` |
| `route_hash` | uint64 | full lookup-key FNV-1a hash |
| `normalized_key_0..7` | UTF-8 | country, first/last address level, postal city, postcode, street, number, unit |
| `feature_id` | fixed binary[16] | canonical UUID bytes |
| `longitude_e7`, `latitude_e7` | int32 | finite point coordinates, ties-to-even at 1e-7 degrees |
| `source_object_index` | uint32 | pinned inventory object index |
| `source_row_group` | uint32 | source Parquet row group |
| `source_row_index` | uint64 | stable row identity within the source object |
| `display_country`, `postal_city`, `postcode`, `street`, `number`, `unit` | UTF-8 | original display strings, null mapped to empty |
| `address_levels` | list<UTF-8> | ordered non-null source level values; list items are schema-nullable for Arrow interoperability |
| `semantic_digest_a`, `semantic_digest_b` | fixed binary[32] | per-row digest contributions |

The unique total order is:

```text
country / maximum_bucket / normalized_key_0..7 / feature_id /
source_object_index / source_row_group / source_row_index
```

UUID and provenance are deliberately both present: UUID orders different
features sharing an exact query key, while provenance preserves repeated source
rows even if all serving values and UUID match.

## Admission and normalization

The exclusive first-match rejection precedence is:

1. `missing_street_or_number`
2. `invalid_geometry`
3. `blank_country`
4. `invalid_country`
5. `missing_uuid`
6. `invalid_uuid`
7. `invalid_source_locator`
8. `record_too_large`
9. `invalid_record`

Normalization is NFC, collapse all Unicode whitespace runs to one ASCII space,
trim leading/trailing whitespace, then lowercase ASCII `A-Z` only. Non-ASCII
case is preserved. Country must normalize to two or three lowercase ASCII
letters/digits.

Geometry accepts exactly a 21-byte, two-dimensional WKB Point in either byte
order with type code 1. EWKB, dimensions, trailing bytes, NaN/infinity, longitude
outside [-180, 180], or latitude outside [-90, 90] are rejected. Coordinates are
stored using IEEE ties-to-even rounding after multiplication by 10,000,000.

The spike trims UUID whitespace and delegates textual parsing to the pinned Rust
UUID crate, then stores only 16 bytes. Before planet mapping, narrow this to the
approved textual forms so a dependency upgrade cannot expand admission.

## Routing and bindings

Routing uses FNV-1a 64 over the eight normalized UTF-8 fields, inserting byte
`0x1f` between fields. `maximum_bucket` is the high 16 bits. A genesis planner
may choose any prefix from 0 to 16 bits; exact-key duplicates therefore cannot
split. There is no predecessor/sticky-split input for the first accepted run.

The binding algorithm is
`sha256-add-mod-2^256-two-domain-v1`. The canonical row frame uses unsigned
64-bit big-endian byte lengths for every UTF-8 string and list count, UUID bytes,
big-endian fixed-width numeric values, and every logical/display/provenance
field. Each domain hashes `domain || payload_length_u64be || payload`; each lane
is the sum of row hashes modulo 2^256. Count is always carried separately. This
preserves duplicate multiplicity and is associative across batches, row groups,
packs, tasks, and reducers.

Checkpoint 2 must add a Rust summary pass over sorted batches. Each pack
manifest and row-group directory will carry count plus both lane sums. DuckDB
footer statistics are pruning hints only.

## Pack and marker proposal

Address row groups may span adjacent maximum buckets. Reducers fetch every row
group whose exact directory range overlaps their route, filter it, and reconcile
count plus both digest lanes for selected and discarded rows. This avoids a
boundary-aware writer in v1 while keeping proof exact.

DuckDB exports one named pack at a time from an explicit `ORDER BY` using one
export thread and pinned Parquet/compression settings. Whole-file SHA-256 and
byte length name immutable packs:

```text
<root>/map/address/tasks/<task_id>/packs/sha256/<sha256>.parquet
<root>/map/address/tasks/<task_id>/summary/sha256/<sha256>.parquet
<root>/map/address/tasks/<task_id>/complete.json
```

The create-only completion marker is written last. It binds request and task
identity, exact source assignments, construction/Arrow/DuckDB/Rust versions,
limits, accounting by rejection precedence, pack and summary identities,
row-group directories, logical bindings, and evidence identity. Resume admits a
valid marker before source hydration. A marker never points at mutable keys.

## Decisions requiring operator approval before checkpoint 2 freezes them

1. Approve the logical schema, display-field preservation, normalization, and
   exclusive rejection precedence.
2. Choose accepted UUID text forms. Recommendation: trimmed canonical
   hyphenated UUID plus the 32-hex form only; output remains 16 bytes.
3. Approve exact 2D WKB Point admission and ties-to-even E7 coordinates.
4. Approve FNV-1a routing and a 16-bit genesis ceiling. Changing either after
   map start invalidates all map packs.
5. Approve the canonical digest frame and two additive SHA-256 lanes. This is a
   proof contract, not a cryptographic collision-proof multiset commitment.
6. Approve overlapping Address row groups with exact post-filter
   reconciliation rather than boundary-aligned writes.
7. Approve `global-v2-construction-v1`, the genesis namespace, and marker-last
   immutable object layout.

Pack row targets, runner grouping, active concurrency, DuckDB memory/threads,
and whether PyArrow hydration remains permanent are measurement-driven and are
not one-way doors yet.

## Gate to checkpoint 2 and planet mapping

Checkpoint 2 may begin after the seven decisions above are approved. Planet
Address mapping must additionally wait for:

- a pinned DuckDB 1.5.1 representative, near-cap, and worst-skew suite;
- nonzero oversize and typed-corruption fixture coverage;
- exact source-locator upper-bound validation from pinned inventory metadata;
- count and two-lane summaries at pack and row-group boundaries;
- hard RSS, scratch, output, and wall-time enforcement with 25% headroom; and
- marker-last resume and interruption tests.

The native range reader is not a gate unless those measurements show hydration
to be material.
