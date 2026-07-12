# Compact ID locator v3 scale gates

Date: 2026-07-12
Status: local experiment and producer/reader hardening only. No production
shard, R2 object, catalog, Worker deployment, or rebuild was changed.

## Decision

Keep the compact locator design: two nullable one-based integer IDs plus
`registry_member` in each ID shard, backed by one immutable dictionary. Replace
the dictionary builder's global staged-row `DISTINCT` with content-addressed
inventories emitted by the jobs that already enumerate the inputs.

This is a producer architecture change, not a shard-layout change. The Worker
continues accepting legacy shards with the locator columns absent.

## Measured gates

The public registry prefix samples are real data from release `2026-06-17.0`.
Outputs were written only under `/private/tmp`. All three samples happened to
contain current/path-present rows only, so historical release IDs are not
represented in their storage delta.

| Prefix | Rows | v1 bytes | v3 bytes | v3 delta | v3 / v1 | Footer | Cold row-group p50 |
|---|---:|---:|---:|---:|---:|---:|---:|
| `0a1` | 825,692 | 26,432,024 | 27,642,861 | 1.4665 B/row | 104.581% | 12,548 B | 1,744,815 B |
| `7f2` | 825,836 | 26,436,632 | 27,647,507 | 1.4662 B/row | 104.580% | 12,548 B | 1,744,811 B |
| `e3c` | 825,841 | 26,436,792 | 27,647,640 | 1.4662 B/row | 104.580% | 12,548 B | 1,744,811 B |

The three-prefix dictionary had 698 source-file tuples, occupied 90,142 bytes,
and took about 1.19 ms p50 / 1.22 ms p95 for a native-Python local
copy+SHA-256+JSON-parse+validation proxy. A cached lookup was about 0.08
microseconds p50. Those timings exclude network, Rust/wasm allocation, and
Worker isolate startup, so they are shape gates rather than edge-latency claims.

The pinned current-release S3 inventory contained 972 distinct
`(theme, type, basename)` tuples and completed in 0.965 seconds without reading
feature rows. Observed process RSS high-water increased by about 69 MiB during
that discovery. This is an exact object inventory for the pinned release, not a
feature-row sample.

An exact public-registry query for distinct path-null `last_seen` values did not
return within its 600-second gate (4 GiB DuckDB memory, 8 GiB spill, two
threads). STAC exposed only the latest two release catalogs at measurement
time, so STAC release history is not a safe substitute for registry history.

For scale intuition only, applying the measured 4.58% v3/v1 delta to a 120 GiB
v1 fleet would add about 5.5 GiB. Applying 1.466 B/row to 4.3 billion rows gives
about 5.9 GiB. These are not forecasts: deleted rows and direct release rows
have different null/value distributions, and the fleet row count must be
measured from the actual build before budgeting retention.

## Inventory fan-in design

Each registry range job emits the canonical distinct path-null
`last_seen_release` values for its exact staged prefix scope. Each release type
job emits the exact source files used as its input. The mutable `_SUCCESS`
marker binds the immutable inventory href, SHA-256, size, kind, release, and
scope.

Dictionary fan-in then:

1. requires current markers and verifies every referenced inventory's bytes,
   hash, schema, release, kind, and scope;
2. rejects missing, overlapping, duplicate, stale, or partition-count-mismatched
   registry scopes and requires full prefix coverage outside smoke mode;
3. verifies each release-type inventory against the stable pinned-release S3
   file universe and proves the union covers the direct-staged
   addresses/base subset (the all-type current-release inventory separately
   binds the registry-backed tuples);
4. publishes the complete ordered reference list as a permanent,
   content-addressed inventory-set JSON outside staging, then embeds both its
   reference and aggregate reference-list SHA in the immutable dictionary;
5. carries both the dictionary SHA and aggregate inventory-set SHA through range build
   markers and the final metadata marker.

Retries publish an inventory before replacing its marker. Marker-less output
cannot participate in fan-in. Patch builds reuse the immutable manifest;
previously unseen source tuples or releases still fail the row-level mapping
assertion before COPY. A dictionary created before inventory binding fails with
an explicit instruction to use a new version rather than silently upgrading an
existing v3 prefix. The permanent inventory-set artifact makes the dictionary's
provenance chain reloadable after staging markers are swept; orphaned retry
inventories cannot make reconstruction ambiguous.

Parquet footer statistics add cheap post-write bounds and aggregate null-count
checks. They cannot prove the row-level XOR invariant because a both-null row
can cancel a both-present row in aggregate statistics. The pre-COPY
`_assert_compact_locator_mapping` scan remains the primary proof.

## Worker read behavior

The three real v3 samples produced 12.5 KiB footers, within the existing 32 KiB
initial suffix read. The Worker now computes an exact bounded retry when a
future footer is larger than the initial suffix, rather than assuming all
footers fit. Existing 50,000-row groups remain about 1.74 MiB per cold lookup in
these samples, consistent with the adopted row-group tradeoff.

## Recommendation and remaining gates

Proceed with inventory fan-in for the next new-version rebuild. Do not mutate or
backfill the current production shard set. Before scheduling that rebuild:

- run the normal smoke R2 pipeline to exercise real marker publication, retry,
  and fan-in ordering;
- capture the complete dictionary counts and size, including path-null history;
- sample prefixes containing deleted rows and direct release-only rows;
- measure a real Worker cold dictionary fetch and first `/id` request against a
  temporary non-production version;
- compute exact fleet bytes and record counts before choosing retention.

If the smoke fan-in is operationally awkward, the fallback is one dedicated
inventory workflow that scans the staged fleet once and publishes the same
immutable contract. Do not restore the global `DISTINCT` to every dictionary
fan-in: the measured path-null query already exceeds a ten-minute local gate.
