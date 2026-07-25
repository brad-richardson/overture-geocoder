# Reverse geocoding for POI and addresses in v2

Date: 2026-07-25. Status: DESIGN, revised after adversarial review. No code.

Purpose: make it possible for v2 to launch with a complete forward **and**
reverse surface on the day construction-v1 first publishes, instead of shipping
forward-only and retrofitting reverse against an already-published planet build.

This builds on the "Query surface, and what construction-v1 does NOT build"
section of `construction-v1-state.md` and does not re-derive it. Taken as
settled:

- construction-v1 builds **forward indexes only**; the Places serving key is
  `cell\0token`, the address serving key is `route_hash`, and neither can answer
  a reverse query at any key.
- **divisions reverse is containment**, POI/address reverse is
  **nearest-neighbour over points**. `build_shards.py --reverse` is not a
  template for the latter.
- Places already has **coarse spatial routing** (256x256 equirectangular cell =
  level-8 quadkey in plate carrée); addresses' `(country, route_hash)` is not
  spatial and must reuse that cell scheme.
- **Token-hash subdivision is useless for spatial queries.** It scatters
  neighbours on purpose.
- Both families hit the same **dense-cell wall** (Tokyo `b2e3` ~1,384,000
  places, São Paulo `5e5e` ~816,000, Osaka `b1e0` ~738,000, mean 4,462).
- Therefore **one spatial reverse design serves both families**.

### Input: the positions artifact (PR #157, in flight)

`scripts/places_construction_v1.py` `map_task` emits
**`overture-places-map-positions-v1`** with a companion
`overture-places-map-positions-directory-v1`, before the combiner. The marker
carries it under the key `positions`, as
`packs: [{pack_id, shuffle_bucket, records, object, directory_object, directory}]`
— **one pack per present shuffle bucket**, not a single object, mirroring the
term packs. `shuffle_bucket` and `partition_key` are not columns: the bucket is
implicit in the pack (`EXCLUDE(pack_id)`).

Three properties of #157 this design leans on directly:

- The positions directory carries **per-row-group bindings and per-cell record
  counts**, and `validate_positions` re-verifies them. So "one consumer holds a
  cell's complete data" is no longer an argument from the shuffle's construction
  — it is **proven by the directory's wrong-bucket check**.
- Emission is **one row per admitted ROW with source-locator identity**
  (`source_object_index`, `source_row_group`, `source_row_index`), not one row per
  distinct `feature_id`. That is the duplicate-UUID gate's requirement, and it is
  what reverse must key on too: a duplicate GERS ID is two real records at two
  positions and reverse must be able to return both.
- Positions objects are in the **finalize publication set** and appear in the
  family/slice manifests, so they are durable published artifacts, not build
  scratch. There is no durability gap for reverse to solve.

What is **not** yet in the positions artifact is the **rich display projection**
(`primary_name`, `brand_name`, `category`, `locality`, `region`, `country`,
`confidence_rank`). Restoring it is in flight on #157 and is the one genuinely
map-blocking Places item (§3, §6 increment 0).

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
types only* — reverse does not silently start returning POIs for existing callers
when the capability appears. A caller opts in with `types=poi` / `types=address`.

`radius` is rejected (400) when `types` contains only division types, because
containment has no radius. `radius` above the family maximum is a 400 with the
observed and maximum values, never a silent clamp:

| family | default radius | maximum radius | reason |
|---|---|---|---|
| `poi` | 250 m | 2,000 m | 250 m is "what is at this corner"; beyond 2 km a POI is not *here* |
| `address` | 100 m | 500 m | an address 500 m away is not this address |

**`limit` is per family, not per response.** `types=locality,poi,address&limit=10`
can therefore return up to 21 features (10 POI + 10 address + 1 division).
Deliberate: a shared budget would let one family's density silently starve
another. Also deliberate is that the default is **1** while `/v2/forward` defaults
to 10 — reverse today returns zero-or-one feature and that stays the default
shape.

### Bounded radius, not unbounded nearest

**Decision: POI and address reverse are k-nearest *within a bounded radius*, and
an empty result is a correct answer.** Reasons, in order of weight:

1. It makes the read set exactly computable before any read. Every leaf the query
   can touch is enumerated arithmetically from `(lat, lon, radius)` and the
   shard's recorded depth, so all byte ranges are known before the first payload
   byte arrives. Unbounded nearest cannot be planned: it requires expanding rings
   until the best distance beats the distance to the unexplored frontier, which is
   an unbounded number of *dependent* round trips in the Sahara and the antarctic
   interior.
2. It is what callers want. "Nearest POI" 340 km away is not a useful answer and
   is indistinguishable from a coverage bug.
3. It preserves the repo's honesty posture. Forward already declines to claim
   "exhaustive global nearest-POI ranking" (`docs/api-v2.md`); a bounded radius
   states the same bound in the contract instead of in prose.

Unbounded nearest can be added later without changing the artifacts, via a
per-cell "nearest populated neighbour" summary in the reverse catalog. Out of
scope; must not be advertised.

### Response

A GeoJSON `FeatureCollection`, as today. GeoJSON has no grouping construct, so
"grouped by family" means precisely: **`features` array order is
`divisions, poi, address`, ascending `distance_m` within each family, and every
feature carries `properties.feature_type`** so a client can partition it without
relying on order. **There is no cross-family ranking** — the API does not assert
that a POI 12 m away beats the address 15 m away, because that depends on caller
intent it does not know.

POI feature properties, which are exactly the columns the rich positions
projection carries:

```json
{
  "type": "Feature",
  "id": "08f2a1…",
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

Address feature properties carry the display projection `address-transform-v1`
already emits (`street`, `number`, `unit`, `postcode`, `postal_city`,
`address_levels`, `display_country`) plus `distance_m`, so a reverse hit and a
structured forward hit for the same record render identically. Divisions features
are unchanged from today's `handle_reverse`.

`metadata.reverse` records, per requested family: `radius_m` applied,
`effective_radius_m` (§2, when a budget bound), `limit`, `sub_cell_level`, cells
read, leaves read, `budget_exhausted`, and the `RangeReadMetrics` totals. Same
diagnostic discipline as `PlacesShardLookup.stages`.

Distance is **haversine on a WGS84 sphere, in metres**. Not squared
equirectangular distance: at 60° latitude an unprojected degree-space comparison
mis-orders candidates by up to 2x, and the candidate set is at most a few thousand
rows, so exact haversine costs nothing.

### Catalog advertising

The machinery already exists (`docs/v2-release-catalog-contract.md`): an operation
is advertised only when its entrypoint is one of the family manifest's verified
hashed artifacts.

- Places family `operations` becomes `["forward", "reverse"]` with
  `reverse -> families/places/reverse-catalog.rcat`.
- Addresses family `operations` becomes `["structured_forward", "reverse"]` with
  `reverse -> families/addresses/reverse-catalog.rcat`.
- `release.operations["reverse"]` gains `places` / `addresses`. The worker builds
  `expected_operations` from `family.operations` already, so this is automatic.

Three worker changes in `v2.rs`:

1. `validate_family` hardcodes exact operation lists
   (`family.operations != ["forward"]`, `!= ["structured_forward"]`). These become
   sorted-set allowlists accepting `["forward"]`, `["forward","reverse"]`,
   `["structured_forward"]`, `["structured_forward","reverse"]` and nothing else.
   **Forward-only must stay valid** so a release can publish forward without
   reverse — the coupling this document wants is a sequencing goal, not a contract
   constraint.
2. The entrypoint loop (`v2.rs` ~L398-418) pins **one** `object_key` per family
   regardless of which operation it is iterating, so as written it rejects *any*
   reverse entrypoint. It must become per-operation:
   `(family, operation) -> expected key + byte cap`. This is the change most
   likely to be missed.
3. A `MAX_REVERSE_CATALOG_OBJECT_BYTES` cap, by analogy with `catalog.pcat` /
   `address-collection.json`.

`v2_release_manifest.py` needs `--entrypoint places.reverse=…` /
`--entrypoint addresses.reverse=…`; it already takes arbitrary `family.operation`
pairs.

The reverse catalog carries the geometry the worker must not hardcode:
`cell_level` (8), per-shard `sub_cell_level`, `max_radius_m`, and the family
coverage bbox. A worker enumerating leaves at a depth the build did not use would
return silently empty results.

### Status codes for a partial rollout

Today `handle_reverse` returns **400 `capability_unavailable`** for `poi`/`address`
*before* `load_available_release`. That is the wrong code for a
published-release-missing-operation, and it disagrees with every analogue:
structured address, POI forward and the ID index all return **503**
(`v2.rs` ~L1138, ~L1280, ~L1423), and `docs/api-v2.md` says a missing explicitly
requested family returns 503.

Required semantics:

| condition | status |
|---|---|
| `types` contains a type this API version does not know | 400 `invalid_request` |
| `radius` with divisions-only `types`, or out of range | 400 `invalid_request` |
| no v2 release published | 503 `release_unavailable` |
| release published, family present, `reverse` not among its operations | **503 `capability_unavailable`** |

One related defect to fix in the same change: a malformed reverse entrypoint
surfaces as **500**, because only `NOT_FOUND_SENTINEL` string-matches into a 503
and `validate_family` errors do not.

---

## 2. One spatial index, both families

### Cell-level routing

Route with the existing 256x256 cell. Three things the first draft conflated:

- `route()` (`places_transform_v1.rs` ~L304) maps `(lon, lat)` to
  `partition_key = (y<<8)|x` and `partition_cell = "{y:02x}{x:02x}"` — a **4-char
  hex** identifier.
- `point_quadkey()` (`places_pages.rs` ~L561) maps `(lon, lat)` to an **8-char
  base-4** string on the same grid.
- `cell_partition_key()` maps a hex cell string back to `u16`. It does not take
  coordinates.

Same grid, three identifiers. `world_quadkey_matches_the_python_partition_contract`
is a Rust-only test against hardcoded base-4 strings; **no cross-implementation
test of the cell identifier exists**, and the first draft cited it as if one did.
Adding one is a named gate (§6, increment 1b).

**Leaf key = `partition_cell` (4 hex chars) + L base-4 sub-digits** = `4 + L`
bytes, 9 at L=5. This keeps "the leaf key extends its cell key" true in the hex
form actually used, keeps object names unambiguous (`r-{cell}.plrx`, cell in hex),
and pins the encoding increment 3's byte-identical gate needs.

**Sub-digits are computed relative to the shard's own cell**, by clamping the
record's E7 position into that cell's bbox and subdividing it. Not cosmetic:
`partition_cell` comes from `route()` on `f64` coordinates while leaf keying uses
E7, and the two can disagree for a point within ~5 mm of a cell boundary. Deriving
sub-digits *within* the authoritative cell makes that disagreement
unrepresentable rather than a rare mis-keyed record.

**One reverse serving shard per populated level-8 cell** — 16,633 objects for
Places. Reverse shards deliberately do **not** reuse the forward partition tree:
that tree subdivides by token hash, the one dimension that destroys spatial
locality. A reverse shard is a whole cell, and the positions directory's per-cell
counts and wrong-bucket check prove a single reducer holds that cell completely,
so its depth can be decided locally with no global planning barrier.

### Within-cell structure: three candidates

**(a) SQLite R-tree, as divisions reverse uses.** Rejected. The divisions path
(`stac/reverse.rs::query_reverse_shard` -> `load_shard_db`) downloads the
**entire** `.db` and hands it to the Workers SQLite via `deserialize`. Fine for a
country's admin polygons; not for a Tokyo POI cell (§5: 133 MB), and there is no
range-read path into a SQLite file. It would also add a second query engine for
artifacts whose whole family is already byte-format-plus-`RangeReader`.

**(b) In-shard R-tree or kd-tree node pages, descended by range read.**
Rejected — but not for the reason the first draft gave. That draft claimed the
incumbent reader is a single planned batch against an 8-read budget. Both halves
were wrong: `MAX_INFLIGHT_READS = 4` is a *concurrency* limit, not a plan cap;
`MAX_COLD_READS = 8` / `MAX_COLD_BYTES = 512 KiB` live in
`tests/test_places_records_gap_sweep.py`, not in the runtime; and the incumbent
routed reader is itself **6 chained waves** (`2 + 2C + 2` dependent round trips),
which `PENDING_WORK.md` (~L538-557) measures at **19 cold reads / 1.42 s against a
1.0 s gate** on the live path, with the model omitting 2 catalog reads.

The honest rejection: a tree descent adds `L` dependent waves **on top of** a
reader that is already chained and already over its read gate, and it buys density
adaptation the enumerated-leaf design gets for free at build time — the positions
directory's per-cell counts already say how deep each cell should be, with no data
read at all.

**(c) Recommended: fine-quadkey leaf key, hash-index dictionary, row-major
payload.** The existing `.plrv` index shape with the serving key changed from
`cell\0token` to a leaf key.

```
header      magic PLRX0001 | records u64 | index_offset u64 | index_count u32 |
            family u8 | cell_level u8 | sub_cell_level u8 | flags u8
            (32 bytes; the family/level/flags quartet occupies the reserved u32
             the .plrv header already writes as zero)
payload     length-prefixed records, grouped by leaf, LEAVES IN ROW-MAJOR ORDER
            (ascending leaf y, then ascending leaf x); records within a leaf
            ordered by (feature_id, source_object_index, source_row_group,
            source_row_index)
index       count u32, then index_count x 40 bytes:
            hash u64 | key_offset u64 | key_len u32 | records u32 |
            payload_offset u64 | payload_bytes u64   (sorted by (hash, key))
keys        concatenated leaf keys (4 hex + L base-4 chars)
```

Why this wins:

- The leaf set for a bounded disc is **enumerated arithmetically with zero
  reads**. After one index read every payload range is known.
- It reuses the encoder, verifier, dual-lane digest, `IndexEntry` layout, index
  hashing and `put_text` framing of `places_serving_encode_v1.rs` essentially
  unchanged, including the independent Rust verifier.
- Empty leaves cost nothing: the index only contains keys that occur.
- Index cost is small: Tokyo at L=5 is ≤ 1,024 entries x (40 + 9) = 49 KB.
  `MAX_INDEX_ENTRIES` (250,000) — a constant in the **construction encoder**, not
  the worker — has ~2.4 orders of magnitude of headroom here.

### Payload order: row-major, not the curve

**The key is a quadkey; the payload order is row-major.** A correction, not a
refinement. Quadkey (Morton) order does not make a 3x3 leaf block contiguous:
exhaustively over all 900 possible 3x3 blocks at L=5, the block occupies **4 runs
in 480 cases and 5 runs in 420 cases — never fewer than 4**, and a
centre-straddling block spans 517 of 1,024 leaves, i.e. ~62 MB of Tokyo's 133 MB
payload. `RECORDS_COALESCE_GAP` (64 KiB) cannot merge megabyte gaps, so the runs
stay separate physical reads. This is the same failure `PENDING_WORK.md` already
diagnosed for the forward records stage.

Writing leaves in row-major order (ascending leaf `y`, then ascending leaf `x`)
makes a 3x3 block **exactly 3 runs, always** — one per leaf row — and an `n x m`
block exactly `n` runs. The key stays a quadkey (prefix-decomposable, already
implemented, human-checkable); only the physical order changes, so this is
format-local and costs nothing.

This settles the old "quadkey or Hilbert?" question differently than the first
draft did: **neither**, as a payload order. Hilbert would be 2-3 runs for a 3x3
block, still worse than row-major's guaranteed 3, and would add a second spatial
encoding.

### Sub-cell depth: the cos φ term

A level-8 cell is 1.40625° x 0.703125°. Adding `L` levels gives 4^L leaves of
`0.703125/2^L` (lat) x `1.40625/2^L` (lon) degrees. In plate carrée the **latitude
dimension is latitude-independent; the longitude dimension shrinks as cos φ**, so
the two cross at φ = 60° and **longitude is the binding constraint poleward of
that**. The first draft's table covered only the equatorial case, so its
"|lat| > 85" degradation threshold was ~19° too late:

```
h(L)     =  78.184 / 2^L            km    (0.703125 · 111.195)
w(L, φ)  = 156.368 · cos φ / 2^L     km    (1.40625  · 111.195 · cos φ)
```

The sizing rule is `max_radius <= min(h, w)`, which is what guarantees a query
disc touches at most a 3x3 leaf block. With a latitude-blind depth clamp the rule
fails from **|lat| 65.84°** for both families — the ratio `r/h` is 0.4093 in both
cases, coincidentally identical — and Tromsø, Murmansk, Norilsk, Utqiaġvik and
Fairbanks all sit in or near the broken band. Leaves spanned at L=5 with r = 2 km:
9 at 60°, 12 at 66°, 18 at 80°, **33 at 85°** (already past a 32-leaf budget *at*
the old threshold), 285 at 89.5°.

**Corrected depth rule.** Three ceilings; take the minimum:

```
L_records = ceil(log4(records / 2048))                        # density
L_lat     = family ceiling: 5 for poi     (h = 2.44 km >= 2 km)
                            7 for address (h = 0.61 km >= 0.5 km; also
                              MAX_QUADKEY_LEVEL = 15)
L_lon     = floor(log2(1.40625 · 111195 · cos(phi_edge) / max_radius_m))
L         = clamp(min(L_records, L_lat, L_lon), 0, L_lat)
```

`phi_edge` is the **poleward edge latitude of the cell** (the edge with the
smaller `cos`), not the centre — using the centre leaves the poleward half of the
cell violating the invariant. Maximum admissible `L` by latitude for `poi`/2 km
(identical for `address`/500 m, same ratio):

| up to | max L |
|---|---|
| 65.84° | 5 |
| 78.19° | 4 |
| 84.13° | 3 |
| 87.07° | 2 |
| 88.53° | 1 |
| 89.27° | 0 |

Beyond 89.27° even a whole cell is narrower than the radius, which is why cell
enumeration needs its own cap (below).

`L` is derived from a record count and a cell identifier, so it is reproducible,
and it is recorded in both the shard header and the catalog entry so the worker
cannot enumerate at a depth the build did not use. Against the dense tail (all
equatorward of 65.84°, so `L_lon` does not bind):

| cell | records | L | leaves | mean records/leaf | mean leaf bytes |
|---|---|---|---|---|---|
| `b2e3` Tokyo 35.7°N | 1,384,000 | 5 (`L_lat`) | 1,024 | 1,352 | 130 KB |
| `5e5e` São Paulo | 816,000 | 5 (`L_lat`) | 1,024 | 797 | 76 KB |
| `b1e0` Osaka | 738,000 | 5 (`L_lat`) | 1,024 | 721 | 69 KB |
| mean cell | 4,462 | 1 | 4 | 1,116 | 107 KB |

Tokyo is the worst case and reads ~0.1% of its 133 MB shard. Intra-cell skew makes
some leaves several times the mean; a 4x-worst leaf is ~520 KB, still one read
under `MAX_RESULT_RANGE_BYTES` (2 MiB).

The `L_lat` ceiling means the 2,048-record target is advisory in the densest
cells. Deliberate: exceeding it would break the
`radius <= min leaf dimension` invariant, which is worth more than a uniform leaf
size.

### Degradation: bound by budget, not by latitude

The first draft degraded "above |lat| 85". Wrong axis. Degradation must trigger
**whenever the enumerated read set exceeds the planned budget**, whatever the
cause:

- **Leaf budget: 32 leaves per shard.** After the cos φ clamp this essentially
  never binds — at 85° the clamp gives L=2, a 19.5 x 3.4 km leaf, and a 2 km disc
  spans 3 leaves in 1 row.
- **Cell budget: 4 cells.** This is the binding guard poleward of 89.27°, where
  the cell itself is narrower than the radius: a 2 km disc spans 4 cells in x at
  89.5° and 16 at 89.9°. The first draft's "never more than 4 cells" was false
  above 88.5°; it is now enforced rather than asserted.

Both enumerate **nearest-first** with an explicit tie-break — ascending
`(cell y, cell x)` for cells, ascending `(leaf y, leaf x)` for leaves — because
the Rust/Python byte-identical gate needs a total order, not just a distance
order.

When a budget binds, the response reports `budget_exhausted: true` and
`effective_radius_m` = **the distance to the nearest point of the nearest UNREAD
leaf** (or unread cell). That is the largest radius for which the answer is
provably complete, and it is what a client needs. Never report the requested
radius with a truncated read set.

### Remaining geometry edge cases

**Cell-boundary queries.** The disc can cross a level-8 cell boundary and the
neighbour leaves then live in a different shard. The worker computes the disc
bbox, enumerates the cells it intersects (1, 2 or 4 within the budget), and reads
each. A cell absent from the catalog is a proven-empty contribution, not an error.

**Antimeridian.** The disc bbox wraps at lon ±180, so the x cell index wraps
modulo 256 and the enumerator must produce `[255, 0]`, not an empty range. Use the
`min_lon > max_lon` crossing convention `build_shards.py` (~L1154) and
`stac/reverse.rs` already share, with its own test.

**Poles, bbox arithmetic.** The longitude half-width `r / (111195 · cos φ)`
diverges as `φ -> 90°`; it must **clamp to a full ±180 wrap**, at which point the
x enumeration is the whole row and the cell budget bites. The latitude bbox clamps
at ±90; since `route()` and `point_quadkey` both `clamp(0, 255)`, the polar row is
a single leaf row and must not be enumerated as two.

**Coordinate precision.** The positions artifact keeps `f64` (lossless, matches
the transform). The serving encoder truncates to E7 (1.1 cm), matching
`address-transform-v1`'s `longitude_e7`/`latitude_e7` `Int32`, and halves the
coordinate bytes. Round once, in the encoder, before keying; sub-digits are then
derived from the E7 value clamped into the authoritative cell, so encoder,
verifier and worker cannot disagree.

---

## 3. Build integration: a second serving index off the same map output

Reverse is a second reduce over artifacts map already writes. No new phase in map,
no new source read, no new inventory.

### Places

The reverse reducer owns a **bucket range**, exactly as the in-flight
bucket-range reduce does, and reads **exactly one positions pack per bucket** in
its range (the pack layout guarantees one object per present bucket — not "many
fragments to scan"). It emits one `.plrx` per cell in that range.

| forward | reverse |
|---|---|
| `reduce_partition` over term packs, per forward partition | `reduce_reverse_cell` over positions packs, per level-8 cell, same bucket range |
| serving QUALIFY top-N per `(cell, token)` | no combiner, no top-N — every record is served |
| `places-serving-encode-v1 --mode routed` -> `.plrv` | `places-reverse-encode-v1 --family places` -> `.plrx` |
| `places-serving-verify-v1 --mode routed` | `places-reverse-verify-v1` |
| `directory()` proof + dual-lane binding | identical |
| `catalog.pcat` (cell -> `q-{cell}.pcsh`) | `reverse-catalog.rcat` (§4) |
| family manifest `forward` entrypoint | family manifest `reverse` entrypoint |

The reverse verifier can assert something forward cannot: **`sum(records) over all
reverse shards == planet admitted rows`.** The term store cannot close that loop
because the combiner legitimately discards rows. This is the strongest end-to-end
completeness invariant in the pipeline; the finalizer should enforce it before the
reverse entrypoint is publishable.

**The one map-blocking Places item is the rich display projection.** Both gaps the
first draft listed — no proof directory/binding, and a row count only bounded
above — are **closed by #157**: the positions directory carries per-row-group
bindings and per-cell counts with a content-hashed `directory_object` re-verified
by `validate_positions`, and the count is asserted equal to admitted rows. What
remains is that positions must carry `primary_name`, `brand_name`, `category`,
`locality`, `region`, `country`, `confidence_rank`. That is a **positions schema
change**, so it must land before the planet map run; it must **not** re-add
`shuffle_bucket` or `partition_key` as columns (the bucket is implicit in the
pack).

### Addresses: `partition_cell` does not exist anywhere in the address path

The first draft said the address positions artifact is "one COPY from the
transform table". False as stated: `route()` exists **only** in
`places_transform_v1.rs`, and the frozen address transform output schema
(`crates/geocoder-construction/src/main.rs` ~L114-140) carries
`longitude_e7`/`latitude_e7` (non-null `Int32`) but **no `partition_cell` and no
`partition_key`**. Two ways to get one:

- **(a) add the columns to the address transform's Rust output schema.** That
  schema is consumed by `address_serving_encode_v1.rs` and
  `geocoder-worker/src/address_construction_v1.rs`, so this is a frozen-schema
  change and a *second* map-blocking item.
- **(b) a DuckDB SQL re-implementation of `route()` over the E7 integers.**
  Chosen. It keeps the address transform schema frozen and is exact, because E7
  coordinates are integers and the cell arithmetic is integer division with no
  float rounding:

```sql
-- exact: 3.6e9 * 256 < 2^63, so no overflow and no float
least(greatest((longitude_e7 + 1800000000) * 256 / 3600000000, 0), 255) AS cell_x
least(greatest((latitude_e7  +  900000000) * 256 / 1800000000, 0), 255) AS cell_y
-- partition_key  = (cell_y << 8) | cell_x
-- partition_cell = printf('%02x%02x', cell_y, cell_x)
```

**Option (b) is a second implementation of the partition contract, so it is
admissible only with a parity test, and that test is a gate on the artifact, not a
nice-to-have.** `route()`'s only existing test is a single interior point
(`route(0.0, 0.0)`, `places_transform_v1.rs` ~L722): no edges, no clamp coverage,
no cross-implementation coverage. The gate is:

1. Add `route_e7(lon_e7, lat_e7)` to the Rust side as the **single reference
   implementation** for E7 input.
2. Prove the DuckDB SQL equals `route_e7` exactly over: all 256 x-boundaries and
   256 y-boundaries and the E7 value either side of each; the four corners
   (±180, ±90); out-of-range values exercising both `clamp` arms; and a large
   pseudo-random interior sample.
3. Prove `route_e7` agrees with `route()` on `f64` input except within one E7
   quantum (~5 mm) of a boundary, and record that residual as accepted — §2's
   clamp-into-the-authoritative-cell rule makes it unobservable in the leaf key.

The address implementation agent is already building this; the artifact spec names
the parity test as its acceptance gate so the two land together.

`overture-address-map-address-positions-v1`: one row per admitted address row,
columns `feature_id`, `longitude_e7`, `latitude_e7`, `partition_cell`,
`source_object_index`, `source_row_group`, `source_row_index`, plus the display
projection the transform already emits (`street`, `number`, `unit`, `postcode`,
`postal_city`, `address_levels`, `display_country`). Written by one `COPY` from
the already-materialised transform table, ordered
`(shuffle_bucket, partition_cell, feature_id, source_*)` using the **Places**
`shuffle_bucket_sql`, with a positions directory by exact analogy with #157.

**This is additive to forward addresses in the key, and — with option (b) — in the
schema too.** It does not change `address_key_hash`, `route_hash`, `hash_bucket`,
`TOTAL_ORDER`, `SERVING_ORDER`, the pack layout, the partition plan, or
`country-fnv1a-high-bits-v1`. The `(country, route_hash)` forward partition key is
not modified, reinterpreted, or shadowed. The address family gains a spatial
shuffle **for the reverse artifact only**; that is not the deferred
forward-shuffle port in `2026-07-24-construction-v1-follow-ups.md` ("DEFERRED, do
not lose"), it does not start it, and it must not be used as a reason to start it.
`partition_cell` here is a column on a new artifact, never a routing key for
`.aidx`/`.adat`.

### What must exist BEFORE the first planet map run

Only these, because they are the only things whose absence forces a **map
re-run**:

1. **Places: the rich display projection on positions** (#157). A positions schema
   change.
2. **Addresses: `overture-address-map-address-positions-v1`**, plus its positions
   directory.
3. **The `route_e7` reference + DuckDB parity test**, because item 2's
   `partition_cell` values are otherwise unproven and a mis-keyed planet artifact
   is a re-run.

Everything else is additive after the fact: the encoder, verifier, reverse
reducer, reverse catalog, manifest entrypoints, worker reader, endpoint.

---

## 4. Worker changes

**Routing.** `handle_reverse` gains a per-family dispatch. For `poi`/`address`:
compute the disc bbox, enumerate intersecting cells (budget, nearest-first,
antimeridian, polar clamps), look each up in the reverse catalog, enumerate the
intersecting leaf keys at that shard's recorded `sub_cell_level`, read, filter by
haversine distance, truncate to `limit`.

**Do not extend the existing PLRV reader.** `places_construction_v1.rs` parses a
`.plrv` from a whole `&[u8]` slice and is documented as a dormant bounded
consumer. A 133 MB Tokyo shard cannot be parsed whole. Write the `.plrx` reader
against `RangeReader` and mirror it with a Python oracle, as
`experiment_places_compact_shard.CompactShard.query` mirrors the `.pcsh` reader,
so the two are fuzz-comparable.

**Read waves must be minimised explicitly, because the incumbent's are not.** The
`.plrx` header is at the front and the index at the tail, which is naively 2
chained waves *per shard* on top of the catalog's. Both are avoidable: the catalog
entry carries `records`, `sub_cell_level`, `bytes` and `index_bytes`, so the
header range and the index range are both computable before any read of the shard.
**Mandate one coalesced call of 2 ranges — one wave.** The same rule covers the
address string dictionary: its offset and length go in the **catalog entry**, not
discovered from the shard header, otherwise it adds a fourth wave.

**Cache.** One `RangeReader` per shard. All `.plrx` and `.rcat` objects are
immutable and take `IMMUTABLE_CACHE_TTL` (7 days); the mutable `v2/catalog.json`
stays on `CATALOG_CACHE_TTL` (300 s). Nothing new in `stac/cache.rs`.

### The reverse catalog must be binary and sharded — this is not optional

The first draft estimated 1.33 MB and offered sharding as an improvement. Both
were wrong. Measured against the exact minified-JSON serializer the `.pcat`
control plane uses, a per-entry cost is **183 B** with bbox floats dominating; an
`.rcat` entry carrying cell, object, records, `sub_cell_level`, bbox, bytes and
sha256 is **250-280 B**. At 16,633 Places cells that is **4.2-4.7 MB — more than
2x over the 2 MiB body cap.** Even a lean binary layout at 96 B/entry is 1.60 MB
raw, and 2.13 MB if the digest is hex, i.e. **hex digests alone breach the cap.**

Therefore:

- **`.rcat` is a fully binary format**, not JSON. Raw 32-byte digests are
  impossible inside a JSON payload (base64 costs +55 B/entry), and a binary
  payload is what makes a compact entry real.
- **Entries are 52 fixed bytes**, with no bbox and no object key:
  `cell u16 | sub_cell_level u8 | flags u8 | records u32 | bytes u64 |
  index_bytes u32 | sha256 [u8;32]`. The bbox is *derivable* from the cell and the
  object key is by convention
  (`{version}/families/{family}/reverse/r-{cell}.plrx`), so storing either is pure
  waste. 16,633 x 52 = **865 KB** for Places.
- **Sharded by the first hex digit of the cell**, 16 shards per family, because
  addresses will populate more cells and 865 KB is not a comfortable margin. A
  small **root** object is the manifest entrypoint: magic, family, `cell_level`,
  `max_radius_m`, coverage bbox, and 16 x `{bytes u64, sha256 [u8;32]}` — about
  **700 B**, effectively free and memoizable in-isolate.

Two claims from the first draft are withdrawn. Routing does **not** cost zero
extra reads: the root must be read for the shard digests, so catalog access is
**2 chained waves** (root, then one shard of ~55 KB). And this is **not** "the
same trick sharded `head.phrp` uses" — `head.phrp` is monolithic (`v2.rs` L34);
the real precedent is `.plhd`, sharded by the top 12 bits of the token index hash
(`head_shard_of`).

Neighbouring cells almost always share a catalog shard, because the first hex
digit of `{y:02x}{x:02x}` is the high nibble of `y`: the four corner cells
`c085`/`c086`/`c185`/`c186` all land in shard `c`. So a boundary query is still
2 catalog reads, not 8.

### Self-sufficient records

Reverse records carry the display projection. Two independent reasons, neither of
which depends on the undocumented `/v2/features/:gers_id` removal:

1. **The ID index cannot render a name.** Its lookup returns a bbox plus
   source-locator metadata (theme, feature type, file, row) — `docs/api-v2.md`:
   *"one GeoJSON `Feature` with its bbox and locator metadata"*. There is no name,
   category or street in it.
2. **Resolving that locator means reading Overture source on the request path**,
   which `docs/address-structured-endpoint-contract.md` lists explicitly as
   unsupported.

So a positions-only reverse index has no renderer at all: it would return bare
GERS IDs and distances, which is not a reverse geocoder. The cost is 7.1 GB
instead of 1.8 GB for Places (§5) and it is the reason reverse is a single-shard,
single-round-trip operation.

Caveat, accepted: the reverse index duplicates display data that also lives in the
forward shards. Deduplicating would couple two artifacts with different keys,
different partitions and different completeness guarantees (the combiner drops
rows from one and not the other) — a worse trade than 7.1 GB of R2.

**Benchmarking.** `/v2/reverse?types=poi` and `types=address` are new route classes
and need their own entries in `scripts/benchmark_latency.py`, so the 250 ms
warm-median gate is *measured* per class rather than asserted.

---

## 5. Cost and size

### Record size

From the actual encoder framing (`u16` text length prefixes, 16-byte
`feature_id`, E7 `Int32` coordinates, `u32` record length prefix):

| | Places | Addresses |
|---|---|---|
| record length prefix | 4 | 4 |
| `feature_id` | 16 | 16 |
| lon + lat (E7) | 8 | 8 |
| `confidence_rank` | 1 | — |
| text length prefixes | 6 x u16 = 12 | 8 x u16 = 16 |
| text payload | ~55 | ~60 |
| **total** | **96 B** | **104 B** |

Places text: name ~20, brand ~2, category ~15, locality ~10, region ~6,
country 2. Address text, from the eight contract fields
(`address-structured-endpoint-contract.md` L15-22): street 18, number 4, unit 1,
postcode 7, postal_city 12, admin_general 8, admin_specific 8, country 2.

The first draft asserted 90 B for Places and ~70 B for addresses. Its own Places
table summed to 96, and 70 was not derivable at all — that draft's own dictionary
example implied 40 B of street text per row.

### Artifact sizes

| Places reverse | value |
|---|---|
| records | 74,223,561 |
| bytes, self-sufficient at 96 B | **7.13 GB** |
| bytes, positions only at 24 B (rejected) | 1.78 GB |
| shards | 16,633 |
| mean shard | 418 KiB |
| Tokyo `b2e3` | **133 MB** |
| index overhead, planet | **1.0-3.8 MB** |
| vs the 34 GB post-combiner term store | **21%** |
| vs the 10.6 GB selected Places source | **67%** |

Index overhead: occupied leaves are `records/2048` scaled by intra-cell skew, so
18k-72k leaves x 49 B. The first draft's ~30 MB assumed every planned leaf was
occupied.

The 67% framing is the more useful one for capacity planning: **a self-sufficient
Places reverse index costs two-thirds of the selected source data.** That is the
real price of not needing a round trip to render a result.

| Addresses reverse | value |
|---|---|
| records | 473,576,753 |
| bytes, inline at 104 B | **49.3 GB** |
| vs the 33.2 GB selected address source | **1.48x** |

**Address reverse is the expensive artifact and needs a compression decision.**
The cause is repetition: within a level-14 leaf, `street`, `postal_city`,
`postcode` and both admin levels take a handful of distinct values across
thousands of rows. A per-shard string dictionary, with codes in the record:

| variant | per row | planet |
|---|---|---|
| inline | 104 B | 49.3 GB |
| 5 repeating columns coded (5 x u16 = 10 B of codes replacing 63 B of text+prefix) | 51 B | **24.2 GB** |
| all 8 columns coded, records become fixed-width so the u32 length prefix goes away (16 + 8 + 8 x u16) | 40 B | **18.9 GB** |
| as above with per-column adaptive code width (u8 where a shard has < 256 distinct values) | ~32 B | ~15.2 GB |

The code bytes are not free: 5 x u16 over a 500,000-row shard is ~5 MB of codes,
against ~120 KB of dictionary. Recommendation: target the **fixed-width
all-columns variant, ~19 GB**, and treat adaptive code widths as a later
refinement. Ship the inline version first — it is correct and provable on a slice
— and land the dictionary before the planet address reverse build.

### Build-time cost

Map cost is already paid: positions are one streaming aggregate over a table
DuckDB has already ingested, and the address equivalent is one `COPY` from a table
already materialised. The reverse reducer reads only positions packs (f64+ZSTD
parquet, a few GB planet-wide, against ~34 GB of term fragments), applies no
combiner and no token grouping, and does one sort per cell.

One cost the first draft missed: **the depth `L` must be known before keying**,
which naively means either two passes over the cell or buffering it whole (Tokyo
is ~133 MB, and its bucket carries roughly 64 other cells). #157's
positions-directory **per-cell counts remove the first pass entirely** — the
reducer reads `L` from the directory and keys in one streaming pass. With that,
estimate **≤ 20% of forward reduce wall time** for Places; without it the estimate
does not hold.

### Query read pattern

Waves: (1) catalog root, ~700 B, memoizable in-isolate; (2) catalog shard, ~55 KB;
(3) per shard, header + index in **one** coalesced call, 2 ranges; (4) per shard,
payload — **one run per leaf row spanned**, thanks to row-major order.

| case | physical reads | waves | bytes |
|---|---|---|---|
| warm, any case | same plan, all Cache API hits | 4 | as below, 0 from R2 |
| 1 cell, default radius (disc inside 1 leaf row) | **5** | 4 | ~110 KB |
| 1 cell, max radius (3 leaf rows) | **7** | 4 | ~245 KB |
| 1 cell, max radius, Tokyo | **7** | 4 | ~495 KB |
| 4-cell corner, max radius (2x2, 2x1, 1x2, 1x1 leaves -> 2+1+2+1 = 6 payload runs; the four cells share one catalog shard) | **16** | 4 | ~600 KB |
| budget-capped (4 cells, 32 leaves) | ≤ **42** | 4 | bounded by the budget |

Under quadkey payload order the same rows would be 6-7 reads for a single cell and
**~31 for the corner**; row-major is what brings the corner to 16. The first draft
claimed 4 reads and 13 for the corner, which assumed contiguity Morton order does
not provide.

**Latency framing, corrected.** The first draft compared against v1 `/reverse`
(39 ms cold / 42 ms warm), which is a whole-object SQLite division shard and not
an analogue. The honest analogue is the **routed Places forward path: 19 cold
reads / 1.42 s measured against a 1.0 s gate** (`PENDING_WORK.md` ~L538-557),
whose model also omits 2 catalog reads. Reverse's plan is 5-7 reads in 4 waves
versus that path's 19 reads in 6 chained waves, so it should be materially cheaper
on both axes — but the first draft's "budget 150 ms cold" was invented and is
withdrawn. The 250 ms **warm**-median gate is the one that must be met, and the
warm plan is 4 waves of cache hits, the right shape for it. Both must be measured
through `benchmark_latency.py` before any number is recorded.

Two size notes for the read plan: Tokyo at max radius (~495 KB) grazes the
test-only `MAX_COLD_BYTES` of 512 KiB, and the 4-cell corner exceeds it. Since that
constant is a test gate rather than a runtime cap, the choice is to raise it with
justification for the reverse route class or to lower `poi` max radius; the former
is recommended, because the read *count* — not the byte count — is what the
measured forward path shows to be the problem.

---

## 6. Sequencing

### The harness needs work before increment 2 is demonstrable

Monaco (`--bbox 7.36 43.71 7.47 43.78`, 38,182 places, ~13 s) lies entirely inside
cell `be85`, so it exercises exactly one shard and cannot show the cross-cell path.
The x=133/134 boundary is at lon **8.4375**, and
`--bbox 8.35 45.40 8.55 45.50` does straddle cells **`c085`** (x=133, y=192) and
**`c086`** (x=134, y=192) — verified against real `route()`. Two things block using
it as written:

1. **`build_slice_inventory_v1.py` is not a spatial slice.** It picks the *first*
   overlapping row group of the *first* overlapping object, builds a single task,
   and does not clip rows. Nothing guarantees the selected row group contains rows
   from both cells. **No cross-cell fixture exists anywhere in the repo.** The
   harness needs a mode that selects row groups until both target cells have rows
   and records which cells it captured, so the fixture is a property of the
   inventory rather than luck.
2. **The two cells are in different shuffle buckets.**
   `shuffle_bucket(49285) = 206` and `shuffle_bucket(49286) = 108`. A single
   bucket-range reducer therefore cannot emit both shards. Increment 2 must either
   run **two reducer invocations** (bucket ranges covering 108 and 206) or one
   range spanning `[108, 206]` — which pulls in ~99 unrelated buckets and is not
   representative. **Two invocations** is the specified approach, and the gate is
   that the two `.plrx` shards are produced by *different* reducer runs and the
   worker still answers a boundary query from both.

### Increments

| # | increment | depends on | gate |
|---|---|---|---|
| **0** | **Restore the rich display projection to `overture-places-map-positions-v1`**: `primary_name`, `brand_name`, `category`, `locality`, `region`, `country`, `confidence_rank`, per admitted row with source-locator identity; do not re-add `shuffle_bucket`/`partition_key` columns. | #157 | Monaco positions carry all seven fields; positions directory + `validate_positions` still balance; forward digests unchanged |
| **0b** | **`route_e7` reference + DuckDB SQL parity test** (all 512 boundaries, both clamp arms, four corners, random interior). | — | SQL == `route_e7` exactly; the f64/E7 residual is bounded at one E7 quantum and recorded |
| **0c** | **`overture-address-map-address-positions-v1`** + its positions directory + an address slice harness (`build_slice_inventory_v1.py` / `run_slice_construction_v1.py` are Places-only). | 0b | address slice emits the artifact; forward address digests byte-identical |
| 1 | `.plrx` format + `places-reverse-encode-v1` / `-verify-v1`: leaf key = 4 hex + L base-4, sub-digits clamped into the authoritative cell, **row-major payload**, depth from the three ceilings, dual-lane digest. | 0 | verifier re-derives every leaf key from E7; size within 5% of the **96 B** model; a 3x3 leaf block resolves to exactly 3 payload runs |
| 1b | The missing **cross-implementation cell-identifier test**: Rust `route()` / `point_quadkey()` / `cell_partition_key()` and their Python mirrors agree on a shared vector set including boundaries and clamps. | — | one vector file, three implementations, no skips |
| 2 | `reduce_reverse_cell` in the bucket-range reducer, depth read from the positions directory's per-cell counts (single pass), `r-{cell}.plrx` per cell. | 1, bucket-range reduce (in flight), the harness change above | Monaco emits 1 shard; the boundary fixture emits `c085` and `c086` from **two** reducer invocations; `sum(records) == admitted rows` |
| 3 | Worker `.plrx` range-read reader: header+index in one coalesced call, cell/leaf enumeration with budgets, nearest-first with the stated tie-breaks, antimeridian and polar clamps. Python oracle mirror. | 1, 1b | fuzz set (interior, cell boundary, antimeridian, 66°, 85°, 89.5°, poles) returns byte-identical result sets in Rust and Python |
| 4 | Binary sharded `.rcat` (root + 16 shards, 52-byte entries, raw digests, `index_bytes`), family manifest `reverse` entrypoint, `v2_release_manifest.py` entrypoints, per-operation entrypoint validation in `v2.rs`, allowlist. | 2 | a release with `["forward","reverse"]` validates; a forward-only release still validates; the root fits in one read |
| 5 | `handle_reverse` `poi` branch: `radius`/`limit`, response assembly, `metadata.reverse`, **503 semantics**, `benchmark_latency.py` entries. | 3, 4 | Monaco slice returns POIs with distances; a published release lacking the reverse entrypoint returns **503 `capability_unavailable`**, not 400; unknown `types` still 400 |
| 6 | Address reverse: reverse reduce, shared encoder `--family addresses`, dictionary (offsets in the catalog entry), address `.rcat`, worker address branch. | 0c, 1-5 | address slice end to end; dictionary reaches the fixed-width ~40 B/row target |
| 7 | Planet: reverse reduce over the same map output; publish forward + reverse in one v2 release. | all, R2 staging | one release advertising `forward`+`reverse` for divisions, poi, address |

Increments 1-7 are additive to a published or unpublished build. Only 0, 0b and 0c
are ordering-critical.

**The single most important thing before the planet build:** the positions
artifacts must be complete in **schema**, because a schema change after the planet
map run means re-running it. That is three items, not one — the Places rich
projection (#157), the address positions artifact, and the `route_e7` parity test
that makes the address artifact's `partition_cell` trustworthy. If exactly one
thing gets done from this document before the planet build, it is increments 0, 0b
and 0c.

---

## 7. Open questions, each with a recommendation

1. **Self-sufficient reverse records, or positions plus a round trip?**
   Self-sufficient, and forced rather than chosen: the ID index returns only a
   bbox and a source locator and cannot render a name, and resolving that locator
   would mean request-path access to Overture source data, which is contractually
   unsupported. This does not depend on the `/v2/features/:gers_id` removal.
2. **Bounded radius or unbounded nearest?** Bounded: 250 m / 2,000 m (poi),
   100 m / 500 m (address). It makes the read plan computable with zero reads, and
   an empty answer beats a POI 340 km away. Unbounded can be layered on later via
   a per-cell nearest-neighbour summary.
3. **Within-cell structure?** Fine-quadkey leaf key with a hash-index dictionary
   and a row-major payload. Rejected: SQLite R-tree (whole-object load, 133 MB
   Tokyo), in-shard tree pages (adds L dependent waves on top of a reader already
   measured at 6 waves / 19 cold reads, and buys density adaptation the positions
   directory already provides for free).
4. **Payload order: quadkey, Hilbert, or row-major?** **Row-major** (ascending
   leaf y, then x). A 3x3 block is 4-5 runs under Morton order in every one of the
   900 possible cases and 2-3 under Hilbert, but exactly 3 under row-major, always.
   The quadkey is retained as the *key* only.
5. **Sub-cell depth: fixed globally or per shard?** Per shard,
   `min(L_records, L_lat, L_lon)` where `L_lon` carries the **cos φ term at the
   cell's poleward edge**. Without it the `radius <= min leaf dimension` invariant
   fails from |lat| 65.84°, not 85°. Recorded in the shard header and catalog so
   the worker cannot disagree.
6. **One `.plrx` format for both families, or two?** One, with a `family` byte in
   the header (it fits the reserved `u32` `.plrv` already writes as zero) and a
   `--family` encoder flag.
7. **Do reverse shards reuse the forward partition tree?** No. One shard per
   populated level-8 cell. The forward tree subdivides by token hash, the one
   dimension that destroys spatial locality.
8. **Could reverse be served from the existing `.plrv` forward shards, which
   already carry lon/lat?** No: the combiner can drop a place from the term set
   entirely, and `.plrv` records are token-ordered so a reverse query would scan
   the whole shard. Exactly why the pre-combiner positions artifact exists.
9. **`limit` semantics and default?** Per family, default 1. Per-family means
   `types=all&limit=10` can return up to 21 features; a shared budget would let one
   family starve another. Default 1 diverges from `/v2/forward`'s 10, deliberately,
   matching today's reverse.
10. **Cross-family ranking?** None. Fixed array order `divisions, poi, address`
    plus `properties.feature_type`; distance-sorted within family. GeoJSON has no
    grouping construct, so order plus the property is the whole mechanism.
11. **Reverse catalog format?** Fully **binary**, 52-byte entries with raw
    digests, no bbox (derivable from the cell) and no object key (by convention),
    sharded 16 ways by the cell's first hex digit behind a ~700 B root. Not
    optional: a `.pcat`-style JSON catalog is 4.2-4.7 MB against a 2 MiB cap, and
    even a lean binary layout breaches it with hex digests. Routing costs **2**
    chained waves, not zero.
12. **How does the address dictionary's byte range get discovered?** From the
    **catalog entry** (offset + length), not the shard header. Header-only
    discovery would add a fourth wave; in the catalog it coalesces with the index
    read.
13. **Address reverse at 49.3 GB — compress or accept?** Compress. Target the
    fixed-width all-columns dictionary form, **~19 GB** (5-column coding alone
    only reaches ~24 GB, and ~15 GB needs adaptive code widths). Ship inline first
    for correctness; land the dictionary before the planet address reverse build.
14. **Distance metric?** Haversine in metres over the whole candidate leaf set.
    Degree-space comparison mis-orders candidates by up to 2x at 60° latitude, and
    the candidate set is at most a few thousand rows.
15. **Degradation trigger?** Not latitude. **Budget**: 32 leaves and 4 cells,
    nearest-first with `(y, x)` tie-breaks, reporting `budget_exhausted` and
    `effective_radius_m` = distance to the nearest point of the nearest unread leaf
    or cell. After the cos φ clamp the leaf budget essentially never binds; the
    cell budget is the real guard poleward of 89.27°.
16. **Where does the address `partition_cell` come from?** Option **(b)**, a
    DuckDB SQL mirror of `route()` over the E7 integers (exact integer division, no
    floats), gated on a `route_e7` reference implementation and an exhaustive parity
    test covering all 512 cell boundaries, both `clamp` arms and the four corners.
    Option (a) — adding columns to the frozen address transform schema — is
    rejected because that schema is consumed by `address_serving_encode_v1.rs` and
    the Worker's `address_construction_v1.rs`.
17. **Should divisions reverse move into this structure?** No. Containment is a
    different query and the existing shards are inside the gate. Leave
    `build_shards.py --reverse` alone.
18. **Does the reverse catalog need explicit proven-empty entries, as the address
    collection has?** No. Absence of a cell is the proof of emptiness, provided the
    finalizer asserts `sum(catalog records) == family admitted rows` — which
    reverse can assert and forward cannot. Enforce it before the entrypoint is
    publishable.
19. **Status code for a published release whose family lacks `reverse`?** 503
    `capability_unavailable`, matching structured address, POI forward and the ID
    index. 400 survives only for types this API version does not know. Also fix the
    malformed-entrypoint path, which currently surfaces as 500.
