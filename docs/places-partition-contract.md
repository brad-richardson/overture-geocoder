# Places spatial partition contract

Status: build-ready contract. This change does not run or publish a global
Places build.

## Decision

New Places shards use `world-quadkey-v1` ownership. Historical ordinal shard
objects remain immutable and readable, but new v2 releases do not create more
region-local `{region}-{ordinal}` identities.

The grid is plate-carree over `[-180, 180] x [-90, 90]`. At each level, a cell
digit is `(y_bit << 1) | x_bit`, with `y` increasing northward. The default
minimum level is 6 and the planning level is 12.

Each occupied minimum-level cell produces one shard unless its row count is
above `split_row_cap`. An over-cap cell recursively splits into its occupied
children. A build may supply the preceding `catalog.pcat`; every cell listed in
that catalog's `split_cells` remains split even when its new row count falls
below the cap. This gives the contract two useful properties:

- Adding or removing rows in one cell does not renumber unrelated shards.
- A shard can split when it becomes too large, but a split never silently
  merges in a later release.

The builder fails if a maximum-level cell still exceeds the cap. It does not
create unstable ordinal suffixes as an escape hatch.

## Catalog v2

The `PCAT0001` envelope is retained so existing bounded range readers can
identify the object. Its JSON payload has `schema_version: 2`:

```json
{
  "schema_version": 2,
  "tokenizer_version": "nfkd-latin-fold-cjk-bigram-v2",
  "coverage": [-180.0, -90.0, 180.0, 90.0],
  "partition": {
    "scheme": "world-quadkey-v1",
    "minimum_level": 6,
    "maximum_level": 12,
    "split_row_cap": 1500000,
    "split_cells": ["..."]
  },
  "shards": [
    {
      "id": "q-...",
      "object": "q-....pcsh",
      "cell": "...",
      "bbox": [-73.125, 39.375, -67.5, 42.1875],
      "center": [-70.3125, 40.78125]
    }
  ]
}
```

The Worker derives the query's maximum-level quadkey and selects its one leaf
prefix. It validates shard IDs, object names, cell geometry, split ancestry,
and the absence of overlapping leaf ownership before serving the catalog.
Schema v1 bbox routing remains readable for the isolated historical smoke
fixtures only.

## Coverage and release eligibility

Every build declares the exact extraction coverage. Regional rehearsals route
only inside that bbox and remain non-promoting. A global family release must
use exactly `[-180, -90, 180, 90]`; the publication workflow will enforce that
before constructing the v2 release candidate.

The global extraction writes a level-12 numeric Morton key and sorts by:

1. Morton key;
2. descending quantized confidence; and
3. GERS ID.

The builder first scans only key/longitude/latitude to plan cells, then scans
the full records to build one planned shard at a time. With the default levels,
planning retains at most 4,096 maximum-level count records for one base cell,
not the complete planet dataset.

## Deferred global-search tier

Spatial ownership fixes deterministic routing for coordinate-biased Places
queries. It does not by itself solve context-free general search. The former
single `head.phrp` has measured hard caps and is intentionally skipped by the
bounded-memory global shard build. A separately bounded global/top-results tier
must be built and verified before v2 advertises unrestricted context-free
Places search.
