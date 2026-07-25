# Reverse geocoding for POI and addresses in v2

Date: 2026-07-25. Status: DESIGN. No code. Nothing here is built.

Purpose: make it possible for v2 to launch with a complete forward **and**
reverse surface on the day construction-v1 first publishes, instead of shipping
forward-only and retrofitting reverse against an already-published planet build.

This builds on the "Query surface, and what construction-v1 does NOT build"
section of `construction-v1-state.md` and does not re-derive it. In particular
these are taken as settled and are not revisited:

- construction-v1 builds **forward indexes only**; the Places serving key is
  `cell\0token`, the address serving key is `route_hash`, and neither can answer
  a reverse query at any key.
- **divisions reverse is containment**, POI/address reverse is
  **nearest-neighbour over points**. `build_shards.py --reverse` is not a
  template for the latter.
- Places already has **coarse spatial routing** (level-8 quadkey in plate
  carrée, `route()`/`route_point`); addresses' `(country, route_hash)` is not
  spatial and must reuse the Places cell scheme.
- **Token-hash subdivision is useless for spatial queries.** It scatters
  neighbours on purpose.
- Both families hit the same **dense-cell wall** (Tokyo `b2e3` ~1,384,000
  places, São Paulo `5e5e` ~816,000, Osaka `b1e0` ~738,000, mean 4,462).
- Therefore **one spatial reverse design serves both families**.
- `/v2/features/:gers_id` is slated for removal (owner decision, 2026-07-25).
  Nothing below depends on it.

One thing the state doc records as an open decision is now closed by code that
has landed: **the per-place, pre-combiner, cell-keyed positions artifact
exists.** `scripts/places_construction_v1.py` emits
`overture-places-map-place-records-v1` from `map_task`, before the combiner, as
a `GROUP BY feature_id` over the term table, ordered
`(shuffle_bucket, partition_cell, feature_id)`, carrying `feature_id`,
`shuffle_bucket`, `partition_cell`, `partition_key`, `longitude`, `latitude`,
`primary_name`, `brand_name`, `category`, `locality`, `region`, `country`,
`confidence_rank`. The rest of this document is written against that artifact as
its input, and §3 lists the two things still missing from it.

---

## 1. Query contract

### The endpoint

`GET /v2/reverse` keeps `lat` and `lon` required and gains two parameters:

| parameter | range | default | applies to |
|---|---|---|---|
| `radius` | metres, integer | 250 (`poi`), 100 (`address`) | `poi`, `address` |
| `limit` | 1..10 | 1 | `poi`, `address` |

`types` continues to accept division types, `poi`, `address`, and the existing
`place`/`neighbourhood` aliases. Absent `types` continues to mean *all division
types only* — reverse does not silently start returning POIs for existing
callers when the capability appears. A caller opts in with `types=poi` or
`types=address`.

`radius` is rejected (400) when `types` contains only division types, because
containment has no radius. `radius` above the family maximum is a 400 with the
observed and maximum values, never a silent clamp:

| family | default radius | maximum radius | reason |
|---|---|---|---|
| `poi` | 250 m | 2,000 m | 250 m is "what is at this corner"; beyond 2 km a POI is not *here* |
| `address` | 100 m | 500 m | an address 500 m away is not this address |

### Bounded radius, not unbounded nearest

**Decision: POI and address reverse are k-nearest *within a bounded radius*, and
an empty result is a correct answer.** Reasons, in order of weight:

1. It makes the read set exactly computable before any read. With
   `max_radius <= min leaf dimension` (§2) the query disc touches at most a 3x3
   block of leaves, so the worker can plan every byte range it needs from the
   coordinates alone and issue them in one coalesced batch. Unbounded nearest
   cannot be planned: it requires expanding rings until the best distance beats
   the distance to the unexplored frontier, which is an unbounded number of
   dependent round trips in the Sahara and the antarctic interior.
2. It is what callers want. "Nearest POI" 340 km away is not a useful answer and
   is indistinguishable from a coverage bug.
3. It preserves the repo's honesty posture. Forward already declines to claim
   "exhaustive global nearest-POI ranking" (`docs/api-v2.md`); a bounded radius
   states the same bound in the contract instead of in prose.

Unbounded nearest can be added later without changing the artifacts, by adding a
per-cell "nearest populated neighbour" summary to the reverse catalog. It is not
in scope and should not be advertised.

### Response

A GeoJSON `FeatureCollection`, as today. Features are grouped by family in the
fixed order `divisions, poi, address` and sorted by ascending distance within
each family. **There is no cross-family ranking** — the API does not assert that
a POI 12 m away is a better answer than the address 15 m away, because that
depends entirely on the caller's intent.

POI feature properties, which are exactly the columns the per-place artifact
already carries:

```json
{
  "type": "Feature",
  "id": "08f2a1...",
  "geometry": {"type": "Point", "coordinates": [139.7671, 35.6812]},
  "properties": {
    "feature_type": "poi",
    "name": "…", "brand": "…", "category": "…",
    "locality": "…", "region": "…", "country": "JP",
    "confidence_rank": 7,
    "distance_m": 41.6
  }
}
```

Address feature properties carry the same raw display projection the structured
forward endpoint returns (`street`, `number`, `unit`, `postcode`, `postal_city`,
both admin levels, `country`) plus `distance_m`, so a reverse hit and a
structured forward hit for the same feature render identically. Divisions
features are unchanged from today's `handle_reverse`.

`metadata.reverse` records, per requested family: `radius_m` applied, `limit`,
`sub_cell_level` used, cells read, leaves read, and the `RangeReadMetrics`
totals. Same diagnostic discipline as `PlacesShardLookup.stages`.

Distance is **haversine on a WGS84 sphere, in metres**. Not squared
equirectangular distance: at 60° latitude an unprojected degree-space comparison
mis-orders candidates by up to 2x, and the candidate set is at most a few
thousand rows, so exact haversine costs nothing.

### Catalog advertising

The machinery already exists and needs no new concepts
(`docs/v2-release-catalog-contract.md`). An operation is advertised only when its
entrypoint is one of the family manifest's verified hashed artifacts.

- Places family `operations` becomes `["forward", "reverse"]` with
  `reverse -> families/places/reverse-catalog.rcat`.
- Addresses family `operations` becomes `["structured_forward", "reverse"]` with
  `reverse -> families/addresses/reverse-catalog.rcat`.
- `release.operations["reverse"]` gains `places` / `addresses`. The worker builds
  `expected_operations` from `family.operations` already, so this is automatic.

Two worker checks in `v2.rs::validate_family` are hardcoded exact lists and must
become sorted-set allowlists:

```rust
family.operations != ["forward"]                 // places
family.operations != ["structured_forward"]      // addresses
```

Accept `["forward"]`, `["forward","reverse"]`, `["structured_forward"]`,
`["structured_forward","reverse"]` and nothing else. **Forward-only must remain
valid** so a release can publish forward without reverse — the coupling this
document wants is a sequencing goal, not a contract constraint.

Each `reverse` entrypoint needs its object-key equality check and a byte cap
(`MAX_REVERSE_CATALOG_OBJECT_BYTES`), by exact analogy with `catalog.pcat` and
`address-collection.json`. `v2_release_manifest.py` needs
`--entrypoint places.reverse=…` / `--entrypoint addresses.reverse=…`; it takes
arbitrary `family.operation` pairs already.

The reverse catalog also carries the geometry the worker must not hardcode:
`cell_level` (8), per-shard `sub_cell_level`, `max_radius_m`, and the family
coverage bbox. A worker that enumerated leaves at a depth the build did not use
would return silently empty results.

---

## 2. One spatial index, both families

### Cell-level routing: unchanged

Route with the existing level-8 quadkey. `point_quadkey(lon, lat, 8)` in
`places_pages.rs` and `cell_partition_key` in `places_construction_v1.py` are
already the same function, tested against each other
(`world_quadkey_matches_the_python_partition_contract`). Reverse computes the
level-8 cell from `lat`/`lon` and looks it up in the reverse catalog. No new
partitioning, no new client-visible key.

**One reverse serving shard per populated level-8 cell.** 16,633 objects for
Places. Reverse shards deliberately do **not** reuse the forward partition tree:
that tree subdivides by token hash, which is the one dimension that destroys
spatial locality. A reverse shard is a whole cell, and the cell-keyed shuffle
guarantees a single consumer holds that cell's complete data, so its subdivision
can be decided locally with no global planning barrier.

### Within-cell structure: three candidates

**(a) SQLite R-tree, as divisions reverse uses.** Rejected. The divisions path
(`stac/reverse.rs::query_reverse_shard` -> `load_shard_db`) downloads the
**entire** `.db` and hands it to the Workers SQLite via `deserialize`. That is
fine for a country's admin polygons; it is not fine for a Tokyo POI cell
(§5: 125 MB) and there is no range-read path into a SQLite file. It would also
introduce a second query engine for artifacts whose whole family is already
byte-format-plus-`RangeReader`.

**(b) In-shard R-tree or kd-tree node pages, descended by range read.**
Rejected. Descending a tree is a chain of *dependent* reads: you cannot know the
child's byte range until the parent's bytes arrive. The Places reader budgets 8
physical cold reads for an entire query and plans them in one coalesced batch
(`RangeReader::coalesced`, `MAX_INFLIGHT_READS = 4`). A depth-5 descent is 5
serialised cache/R2 round trips before the first record, which is precisely the
latency shape the pages design was built to avoid. Its advantage — adapting to
density without a declared depth — is worth less than the round trips, because
the density adaptation can be done at build time instead (below).

**(c) Recommended: fine-quadkey leaf key, hash-index dictionary, contiguous
payload per leaf.** This is the existing `.plrv` index shape with the serving
key changed from `cell\0token` to a fine quadkey.

Concretely, the format (`PLRX0001`, one per cell):

```
header      magic PLRX0001 | records u64 | index_offset u64 | index_count u32 |
            family u8 | cell_level u8 | sub_cell_level u8 | flags u8
payload     length-prefixed records, grouped by leaf key, leaves in ascending
            quadkey order, records within a leaf ordered by feature_id
index       count u32, then index_count x 40 bytes:
            hash u64 | key_offset u64 | key_len u32 | records u32 |
            payload_offset u64 | payload_bytes u64      (sorted by (hash, key))
keys        concatenated leaf quadkeys
```

Why this wins:

- The leaf set for a bounded disc is **enumerated arithmetically in the worker
  with zero reads**. After one index read every payload range is known, and they
  coalesce (adjacent leaves are adjacent in the payload because leaves are
  written in quadkey order).
- It reuses the encoder, verifier, digest lanes, `IndexEntry` layout, index
  hashing, and `put_text` framing of `places_serving_encode_v1.rs` essentially
  unchanged. The dual-lane additive digest and the independent Rust verifier come
  for free.
- Empty leaves cost nothing: the index only contains keys that occur.
- Index cost is trivial: ≤ 4^L entries, so Tokyo at L=5 is 1,024 entries x
  (40 + 13) = 54 KB — one 256 KB range read. `MAX_INDEX_ENTRIES` (250,000) has
  four orders of magnitude of headroom here.

**Quadkey, not Hilbert.** Hilbert's only advantage is better contiguity under a
*range scan* along the curve, and this design never range-scans the curve — it
enumerates the exact leaf set. Quadkey is already implemented identically on both
sides, is prefix-decomposable (a leaf key literally extends its cell key), and is
the scheme the partition contract already pins. Adopting Hilbert would add a
second spatial encoding for zero query benefit.

**One format for both families.** `family u8` in the header and a
`--family places|addresses` flag on the encoder. The record projection differs;
the index, leaf keying, digest, verifier, and worker enumeration do not. Building
the dense-cell machinery twice is exactly what the state doc's conclusion warns
against.

### Sub-cell depth, sized against Tokyo and the 250 ms gate

A level-8 cell is 1.40625° x 0.703125°. Adding `L` quadkey levels gives 4^L
leaves of `1.40625/2^L` x `0.703125/2^L` degrees. In plate carrée the **latitude
dimension is latitude-independent**, so it is the binding constraint:

| total level | lat dimension | lon dimension at equator | at 35.7° (Tokyo) |
|---|---|---|---|
| 13 (L=5) | 2.45 km | 4.89 km | 3.97 km |
| 14 (L=6) | 1.22 km | 2.45 km | 1.99 km |
| 15 (L=7) | 0.61 km | 1.22 km | 0.99 km |

**Sizing rule: `max_radius_m <= min leaf dimension`.** Then a query disc
intersects at most a 3x3 block of leaves, so the read plan is 1 index read plus
at most 9 payload ranges that mostly coalesce. This fixes the per-family depth
ceiling:

- `poi`, max radius 2,000 m -> depth ceiling **level 13** (2.45 km).
- `address`, max radius 500 m -> depth ceiling **level 15** (0.61 km). Level 15
  is also `MAX_QUADKEY_LEVEL` in the worker, so it is a hard ceiling anyway.

Depth per shard is then derived deterministically from that cell's record count:

```
L = clamp(ceil(log4(records / TARGET_LEAF_RECORDS)), 0, family_ceiling - 8)
TARGET_LEAF_RECORDS = 2048
```

Derived from a count, so it is reproducible, and recorded in both the shard
header and the catalog entry so the worker cannot enumerate at a depth the build
did not use. Against the tail:

| cell | records | L | leaves | mean records/leaf | mean leaf bytes (§5) |
|---|---|---|---|---|---|
| `b2e3` Tokyo | 1,384,000 | 5 (ceiling) | 1,024 | 1,352 | 122 KB |
| `5e5e` São Paulo | 816,000 | 5 (ceiling) | 1,024 | 797 | 72 KB |
| `b1e0` Osaka | 738,000 | 5 (ceiling) | 1,024 | 721 | 65 KB |
| mean cell | 4,462 | 1 | 4 | 1,116 | 100 KB |

Tokyo is the worst case and it is a 122 KB mean leaf — one range read, well
inside `MAX_RESULT_RANGE_BYTES` (2 MiB) and `RECORD_INDEX_MAX_RANGE_BYTES`
(256 KiB). Intra-cell density skew makes some leaves several times the mean;
a 4x-worst leaf is still ~500 KB, one read. The 250 ms warm-median gate is not
the constraint here; the constraint is that Tokyo must never require reading the
whole 125 MB cell, and at L=5 it reads ~0.1% of it.

The depth ceiling means the *target* is advisory in the very densest cells. That
is deliberate: exceeding the ceiling would break the `max_radius <= leaf`
invariant, which is worth more than a uniform leaf size.

### Edge cases, stated so they are not discovered in production

**Cell-boundary queries.** The query disc can cross a level-8 cell boundary, and
the neighbour leaves then live in a *different shard*. The worker computes the
disc bbox, enumerates the level-8 cells it intersects (1, 2, or 4), and reads
each. With a 2 km maximum radius against a cell that is 78 km tall, the disc
crosses a boundary only within 2 km of it, so this is 1 shard almost always and
never more than 4. Reading a neighbour cell that is absent from the catalog is a
proven-empty contribution, not an error.

**Antimeridian.** The disc bbox wraps at lon ±180, so the x cell index wraps
modulo 256 and the enumerator must produce `[255, 0]`, not an empty range. The
divisions reverse path already carries the `min_lon > max_lon` crossing
convention (`build_shards.py` ~L1154, matching `stac/reverse.rs`); use the same
convention and give it its own test.

**Poles.** Longitude cell width collapses with `cos(lat)`. At 89.5° a level-8
cell is 1.36 km wide, so a 2 km disc spans ~3 cells and ~93 leaves in x at
level 13. Rule: for `|lat| > 85`, enumerate leaves in ascending distance order
and stop at a hard budget of 32 leaves, reporting
`metadata.reverse.leaf_budget_exhausted: true` and the effective radius actually
covered. Do not silently return fewer results with a full-radius claim. This is
acceptable because POI and address density above 85° is negligible; the count
should be measured once from the planet map output and recorded, and if it is
zero the branch can be a rejection instead.

**Coordinate precision.** The map artifact keeps `f64` longitude/latitude
(lossless, matches the transform). The serving encoder truncates to E7 fixed
point (1.1 cm), which is what `address-transform-v1` already emits
(`longitude_e7`/`latitude_e7`, `Int32`) and halves the coordinate bytes. A place
whose E7-rounded position lands in a different leaf than its `f64` position must
be keyed by the **E7** value, so the verifier's re-derivation and the worker's
enumeration agree. Round once, in the encoder, before keying.

---

## 3. Build integration: a second serving index off the same map output

Reverse is a second reduce over the same map fragments. It adds no phase to map,
no new source read, and no new inventory.

### Places: the input already exists

`map_task` already emits `overture-places-map-place-records-v1` before the
combiner, keyed and ordered by `(shuffle_bucket, partition_cell, feature_id)`.
The comment in the source states the two reasons correctly: it must precede the
combiner (a place whose tokens all sit in saturated `(cell, token)` groups can
vanish from the term set entirely — harmless for forward, fatal for reverse), and
a `GROUP BY feature_id` is exact because every retained column is per-feature.

Two gaps remain in it, and both are cheap now and expensive later because they
change the marker schema:

1. **No proof directory or binding.** Packs get `directory()` + `combine_bindings`
   and reconcile against the transform. `place_records` records only
   `{schema, records, admitted_features, object}`. Reverse's whole value
   proposition is that it *cannot* lose a record, and today nothing proves the
   artifact's bytes. Emit a directory proof for it and carry its binding in the
   marker.
2. **The row count is only bounded above.** The code raises if
   `place_rows > parquet.metadata.num_rows` or `> max_input_rows`. It should
   assert `place_rows == transform["admitted_features"]` — an equality, not a
   bound. A silently short artifact is exactly the failure mode reverse must not
   have.

### Places: the new phases, by analogy with the forward path

| forward | reverse |
|---|---|
| `reduce_partition` over packs, per forward partition | `reduce_reverse_cell` over `place_records`, per level-8 cell, driven by the same **bucket range** |
| serving QUALIFY top-N per `(cell, token)` | no combiner, no top-N — every record is served |
| `places-serving-encode-v1 --mode routed` -> `.plrv` | `places-reverse-encode-v1 --family places` -> `.plrx` |
| `places-serving-verify-v1 --mode routed` | `places-reverse-verify-v1` |
| `directory()` proof + dual-lane binding | identical |
| `catalog.pcat` (cell -> `q-{cell}.pcsh`) | `reverse-catalog.rcat` (cell -> `r-{cell}.plrx`, `records`, `sub_cell_level`, bbox, bytes, sha256) |
| family manifest `forward` entrypoint | family manifest `reverse` entrypoint |

The reverse reducer owns a bucket range exactly as the in-flight bucket-range
reduce does, reads each `place_records` fragment in its range once, and emits one
`.plrx` per cell in that range. It consumes R2 staging fragments when map moves
there; it needs no separate staging design.

The reverse verifier can assert something forward cannot: **`sum(records) over
all reverse shards == planet admitted_features`.** The forward term store cannot
close that loop because the combiner legitimately discards rows. This is the
strongest end-to-end completeness invariant in the pipeline and the finalizer
should enforce it before the reverse entrypoint is publishable.

### Addresses: the analogous artifact does not exist yet

The address map emits packs keyed by a **row counter** (`pack_id` =
`row_number()/max_pack_rows`) and carries no spatial column at all. It does
already carry coordinates: `address-transform-v1` emits `longitude_e7` /
`latitude_e7` as non-null `Int32`.

Specify `overture-address-map-address-records-v1`: one row per address, columns
`feature_id`, `longitude_e7`, `latitude_e7`, `partition_cell`, `partition_key`,
plus the display projection the structured endpoint returns (`street`, `number`,
`unit`, `postcode`, `postal_city`, both admin levels, `country`). Written in one
`COPY` from the already-materialised transform table, ordered
`(shuffle_bucket, partition_cell, feature_id)` using the **Places**
`shuffle_bucket_sql` so the reverse reducer can bucket-range it identically.

**This is additive and touches nothing about forward addresses.** It does not
change `address_key_hash`, `route_hash`, `hash_bucket`, `TOTAL_ORDER`,
`SERVING_ORDER`, the pack layout, the partition plan, or
`country-fnv1a-high-bits-v1`. The `(country, route_hash)` forward partition key
is not modified, not reinterpreted, and not shadowed. Concretely: the address
family now emits a spatial shuffle **for the reverse artifact only**. That is not
the deferred forward-shuffle port in
`2026-07-24-construction-v1-follow-ups.md` ("DEFERRED, do not lose"), it does not
start it, and it must not be used as an excuse to start it. `partition_cell` here
is a column on a new artifact, never a routing key for `.aidx`/`.adat`.

After that, the chain is the same one: reverse reduce -> shared encoder
`--family addresses` -> shared verifier -> `reverse-catalog.rcat` -> family
manifest entrypoint.

### What must exist BEFORE the first planet map run

Only per-record positions artifacts, because they are the only thing whose
absence forces a planet **map re-run**:

- **Places**: the artifact has landed. What must still land before planet is its
  **proof directory** and the `== admitted_features` **equality**, plus the
  decision to keep `f64` in the map artifact. Marker-schema changes after the
  planet map run mean re-running it.
- **Addresses**: `overture-address-map-address-records-v1` does not exist. It
  must land before the planet **address** map run.

Everything else is purely additive after the fact: the encoder, the verifier, the
reverse reducer, the reverse catalog, the manifest entrypoint, the worker reader,
and the endpoint. All of them consume artifacts the map already wrote.

---

## 4. Worker changes

**Routing.** `handle_reverse` gains a per-family dispatch. For `poi`/`address`:
compute the disc bbox from `(lat, lon, radius)`, enumerate the intersecting
level-8 cells (with the antimeridian wrap and the pole budget), look each up in
the reverse catalog, and for each present shard enumerate the intersecting leaf
quadkeys at that shard's recorded `sub_cell_level`. Filter and rank by haversine
distance, truncate to `limit`.

**Shard open and cache.** One `RangeReader` per shard, exactly as
`places_pages.rs` does: `range(0, PREAMBLE/HEADER)` for the header, one range for
the tail index (bounded by `records`-derived caps), then `coalesced(...)` for the
leaf payload ranges. Immutable objects, so `IMMUTABLE_CACHE_TTL` (7 days);
`reverse-catalog.rcat` is immutable too and takes the same TTL, with the mutable
`v2/catalog.json` staying on `CATALOG_CACHE_TTL` (300 s). Nothing new is needed
in `stac/cache.rs`.

**Do not extend the existing PLRV reader.** `places_construction_v1.rs` parses
a `.plrv` from a whole `&[u8]` slice and is documented as a dormant bounded
consumer. A reverse reader must be range-read from its first line — a Tokyo cell
is 125 MB and a whole-object parse of it is not a thing the worker can do. Write
the `.plrx` reader against `RangeReader` and mirror it with a Python oracle, the
same way `experiment_places_compact_shard.CompactShard.query` mirrors the
`.pcsh` reader, so the two are fuzz-comparable.

**Reverse catalog size.** 16,633 Places entries at ~80 bytes each (4-byte cell +
object key + hex sha256 + counts) is 1.33 MB against `MAX_CATALOG_BYTES` of
2 MiB — only 30% headroom, and addresses will have more populated cells.
Recommendation: **shard the reverse catalog by the first hex digit of the cell**,
16 objects per family, `reverse-catalog-{0..f}.rcat`, with
`reverse-catalog.rcat` as the small root that names them. Routing costs zero
extra reads because the digit is computed from `lat`/`lon`. This is the same
trick sharded `head.phrp` already uses. Also store the digest as raw 32 bytes,
not 64 hex characters.

### Does the record need to be self-sufficient? Yes — it is forced.

This is recorded as an open question in the state doc ("only `feature_id`, or
also name/category/address fields"). It is not actually a tradeoff:

- `/v2/features/:gers_id` is being removed, so there is no public round-trip.
- Even internally, the ID index does not carry a name. Its lookup returns bbox
  plus source-locator metadata (theme, type, file, row) — `docs/api-v2.md`:
  *"one GeoJSON Feature with its bbox and locator metadata"*. It cannot render
  "Café de Paris".
- Resolving the locator would mean reading Overture source data on the request
  path, which `docs/address-structured-endpoint-contract.md` lists explicitly as
  **unsupported**.
- A positions-only reverse index therefore has no renderer at all. It would
  return bare GERS IDs and distances, which is not a reverse geocoder.

So reverse records carry the display projection. The cost is 6.7 GB instead of
1.8 GB for Places (§5) — 14% of the 34 GB post-combiner term store — and the
per-place artifact already carries every one of those columns, so no new
extraction is involved. Secondary benefit: reverse becomes a single-shard,
single-round-trip operation, which is what keeps it inside the gate.

**One caveat worth writing down:** carrying display fields means the reverse
index duplicates data that also lives in the forward shards. That is accepted.
Deduplicating them would couple two artifacts with different keys, different
partitions, and different completeness guarantees (the combiner drops rows from
one and not the other), which is a much worse trade than 6.7 GB of R2.

**Benchmarking.** `/v2/reverse?types=poi` and `types=address` are new route
classes and need their own entries in `scripts/benchmark_latency.py` so the
250 ms warm-median gate is measured per class rather than asserted.

---

## 5. Cost and size

Record size, from the actual encoder framing (`u16` length prefixes, 16-byte
`feature_id`, E7 `Int32` coordinates):

| | bytes |
|---|---|
| `feature_id` | 16 |
| lon + lat (E7) | 8 |
| `confidence_rank` | 1 |
| 6 x `u16` text length prefixes | 12 |
| text (name ~20, brand ~2, category ~15, locality ~10, region ~6, country 2) | ~55 |
| 4-byte record length prefix | 4 |
| **Places reverse record** | **~90** |
| Places minimal (id + coords only) | 24 |

| Places reverse | value |
|---|---|
| records | 74,223,561 |
| artifact bytes, self-sufficient | **6.7 GB** |
| artifact bytes, positions only (rejected) | 1.8 GB |
| shards | 16,633 |
| mean shard | 392 KB |
| Tokyo `b2e3` shard | **125 MB** |
| index overhead, planet | ~30 MB |
| relative to the 34 GB post-combiner term store | **+20%** |

Address records are a similar shape but shorter text (no brand/category, but a
street and number): ~70 bytes.

| Addresses reverse | value |
|---|---|
| records | 473,576,753 |
| artifact bytes, inline text | **~33 GB** |
| relative to the 33.2 GB selected address source | ~1.0x |

**Address reverse is the expensive artifact, and it needs one compression
decision.** 33 GB is the same order as the entire selected address source. The
cause is repetition: within a level-14 leaf, `street`, `postal_city`,
`postcode`, and both admin levels take a handful of distinct values across
thousands of rows. Recommendation: a **per-shard string dictionary component**
for those five columns, with `number` and `unit` inline. A shard with 500,000
addresses and ~5,000 distinct streets replaces ~20 MB of repeated street bytes
with ~120 KB of dictionary plus 1 MB of `u16` codes. Expect 3-4x overall, so
**~10 GB**. The dictionary is a small extra component that coalesces with the
index read, so it costs no extra round trip. Ship the inline version first (it is
correct and provable on a slice) and add the dictionary before the planet address
reverse build.

**Build-time cost, relative to the forward build.** Map cost is already paid —
`place_records` is one streaming hash aggregate over a table DuckDB has already
ingested, and the address equivalent is one `COPY` from a table already
materialised. The reverse reducer reads only the positions artifacts (2.4 GB
Places / ~4 GB addresses as `f64`+ZSTD parquet, against ~34 GB of term
fragments), applies no combiner, no token grouping, and one sort per cell.
Estimate **≤ 20% of forward reduce wall time** for Places. Being per-cell rather
than per-forward-partition, it also parallelises more freely.

**Query read pattern.**

| | reads | bytes |
|---|---|---|
| warm | catalog memoized in-isolate; index + payload from Cache API | ~200 KB, 0 R2 |
| cold, typical | 1 catalog-shard + 1 header + 1 index + 1 payload = 4 | ~200 KB |
| cold, Tokyo | 4 (index 54 KB, payload ~122 KB) | ~180 KB |
| cold, cell-boundary | up to 4x the above = 13 reads, coalesced | ~600 KB |

For scale: today's `/reverse` measures **39 ms cold / 42 ms warm p50** while
downloading and deserialising a whole SQLite division shard. A 4-range read
totalling 200 KB is strictly cheaper than that. The 434 ms cold figure belongs to
`/search`, which loads a large FTS shard whole. Budget 150 ms cold for POI/address
reverse and expect warm well under 100 ms — comfortable margin on the 250 ms
warm-median gate. The residual risk is the cold **catalog** read, which is why it
is hex-sharded to ~90 KB rather than 1.33 MB.

---

## 6. Sequencing

Every increment is demonstrable on the slice harness. Monaco
(`--bbox 7.36 43.71 7.47 43.78`, 38,182 places, ~13 s) lies entirely inside cell
`be85` (x=133, y=190), so it exercises exactly one shard. The cross-shard path
needs a second fixture: the x=133/134 boundary is at lon **8.4375**, so
`--bbox 8.35 45.40 8.55 45.50` straddles cells `c085` and `c086` with real data.
Add both to the harness.

| # | increment | depends on | gate |
|---|---|---|---|
| **0** | **Harden `place_records`**: proof directory + binding in the marker, `place_rows == admitted_features` equality, `f64` retained in the map artifact. | landed positions artifact | Monaco marker carries a reverse binding; a deliberately short artifact fails |
| **0b** | **`overture-address-map-address-records-v1`** in the address map + an address slice harness (`build_slice_inventory_v1.py` / `run_slice_construction_v1.py` are Places-only today). | address transform (`*_e7`) | address slice emits the artifact; forward address digests byte-identical to before |
| 1 | `.plrx` format + `places-reverse-encode-v1` / `-verify-v1`, run offline over a Monaco `place_records` file. Depth rule, E7 keying, dual-lane digest. | 0 | verifier re-derives every leaf key from coordinates; size within 5% of the 90 B/row model |
| 2 | `reduce_reverse_cell` inside the bucket-range reducer; one `r-{cell}.plrx` per cell. | 1, bucket-range reduce (in flight) | Monaco emits 1 shard; boundary slice emits 2; `sum(records) == admitted_features` |
| 3 | Worker `.plrx` range-read reader + leaf/cell enumeration (boundary, antimeridian, pole budget), mirrored by a Python oracle. | 1 | fuzz set of query points returns byte-identical result sets in Rust and Python |
| 4 | `reverse-catalog.rcat` (hex-sharded), family manifest `reverse` entrypoint, `v2_release_manifest.py` entrypoints, `validate_family` allowlist. | 2 | a release with `["forward","reverse"]` validates; a forward-only release still validates |
| 5 | `handle_reverse` `poi` branch: `radius`/`limit`, response assembly, `metadata.reverse`, benchmark entries. | 3, 4 | worker smoke against the Monaco slice returns POIs with distances; `types=address` still 400s |
| 6 | Address reverse: reverse reduce, shared encoder `--family addresses`, per-shard string dictionary, address reverse catalog, worker address branch. | 0b, 1-5 | address slice end to end; dictionary yields ≥ 3x over inline |
| 7 | Planet: reverse reduce over the same map output; publish forward + reverse in one v2 release. | all, R2 staging | one release advertising `forward`+`reverse` for divisions, poi, address |

Increments 1-7 are all additive to a published or unpublished build. Only 0 and
0b are ordering-critical.

**The single most important thing to decide or emit before the planet build:**
the per-record positions artifacts must exist **and be provably complete**.
Places is 90% there — the artifact landed; what is missing is its proof binding
and the `== admitted_features` equality, both of which change the marker schema
and therefore cannot be added without re-running the planet map. Addresses have
no such artifact at all. If exactly one thing gets done from this document before
the planet build, it is increments 0 and 0b.

---

## 7. Open questions, each with a recommendation

1. **Self-sufficient reverse records, or positions plus a round trip?**
   Self-sufficient. It is forced, not chosen: the ID index carries no name, the
   `/v2/features/:gers_id` endpoint is being removed, and request-path access to
   Overture source is contractually unsupported. §4.
2. **Bounded radius or unbounded nearest neighbour?** Bounded radius, defaults
   250 m (poi) / 100 m (address), maxima 2,000 m / 500 m. It makes the read plan
   computable with zero reads, and an empty answer is more useful than a POI
   340 km away. Unbounded can be layered on later via a per-cell nearest-neighbour
   summary.
3. **Within-cell structure?** Fine-quadkey leaf key with a hash-index dictionary
   and contiguous payload — the `.plrv` index shape re-keyed. Rejected: SQLite
   R-tree (whole-object load, 125 MB Tokyo), in-shard tree pages (dependent
   round trips against an 8-read cold budget).
4. **Quadkey or Hilbert?** Quadkey. The leaf set is enumerated, never
   range-scanned, so Hilbert's contiguity buys nothing, and quadkey is already
   implemented and cross-tested on both sides.
5. **Sub-cell depth: fixed globally or per shard?** Per shard, derived
   deterministically from the cell's record count with a per-family ceiling
   (level 13 poi, level 15 address) set by `max_radius <= min leaf dimension`.
   Recorded in the shard header and catalog so the worker cannot disagree.
6. **One `.plrx` format for both families, or two?** One, with a `family` byte
   and a `--family` encoder flag. Building the dense-cell machinery twice is the
   failure mode the state doc's conclusion names.
7. **Do reverse shards reuse the forward partition tree?** No. One shard per
   populated level-8 cell. The forward tree subdivides by token hash, which is
   the one dimension that destroys spatial locality.
8. **Could reverse be served from the existing `.plrv` forward shards, which
   already carry lon/lat?** No, for two independent reasons: the combiner can drop
   a place from the term set entirely, and records inside a `.plrv` are ordered by
   token, so a reverse query would scan the whole shard. This is precisely why the
   pre-combiner positions artifact exists.
9. **`limit` default?** 1, matching today's reverse returning zero-or-one
   feature. Callers that want a list ask for one.
10. **Cross-family ranking when `types` mixes families?** None. Group by family
    in a fixed order, distance-sort within family. A "best" answer across a POI
    and an address depends on caller intent the API does not know.
11. **Reverse catalog size against the 2 MiB body cap (1.33 MB at 16,633
    entries)?** Hex-shard by the cell's first digit into 16 objects, raw 32-byte
    digests. Zero extra reads; the digit comes from `lat`/`lon`.
12. **Pole behaviour?** Hard 32-leaf budget above `|lat| > 85`, nearest-first,
    with `leaf_budget_exhausted` and the effective radius in metadata. Measure the
    planet record count above 85° once; if it is zero, make it a rejection instead.
13. **Address reverse at 33 GB — compress or accept?** Compress, via a per-shard
    string dictionary for `street`, `postal_city`, `postcode`, and both admin
    levels (~10 GB). Ship inline first for correctness, dictionary before the
    planet address reverse build.
14. **Distance metric?** Haversine in metres over the whole candidate leaf set.
    Degree-space comparison mis-orders candidates by up to 2x at 60° latitude and
    the candidate set is at most a few thousand rows.
15. **Should divisions reverse move into this structure?** No. Containment is a
    different query, the existing shards work, and both figures are inside the
    gate (39 ms cold / 42 ms warm). Leave `build_shards.py --reverse` alone.
16. **Does the reverse catalog need explicit proven-empty entries, as the address
    collection has?** No. Absence of a cell from the reverse catalog is the proof
    of emptiness, provided the finalizer asserts
    `sum(catalog records) == family admitted count` — which reverse can assert and
    forward cannot. Enforce that in the finalizer before the entrypoint is
    publishable.
