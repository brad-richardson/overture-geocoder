# Overture Place hydration timing spike

Date: 2026-07-14

## Verdict

Optional Place-detail hydration is plausible and can be sub-second when warm. The existing format-v3 ID shards remove the public GERS-registry lookup from the hot path by returning a release-pinned source filepath and exact bbox.

Do not hydrate the basic search response: name, category, context, and coordinates are inexpensive and useful in the compact shard. Use this path for optional properties such as websites, phones, socials, full names/taxonomy, sources, and addresses after a client selects a result.

The remaining implementation work is real but bounded:

1. Enable Parquet ZSTD in the Worker.
2. Avoid downloading the source file's multi-megabyte footer on a cold request by publishing source row-group metadata or storing a compact row-group/range locator alongside the ID index.
3. Fetch only the matching row group or requested column chunks.
4. Measure actual Worker CPU, bundle size, and memory after ZSTD decoding is wired in.

## Tested feature

- GERS ID: `035d1d12-8d11-4651-839e-f622ac859fdb`
- Place: Starbucks, San Diego
- ID-index data version: `2026-07-13.0`
- Overture release: `2026-06-17.0`
- Source object: `theme=places/type=place/part-00001-ddbf8ca5-c261-5b5f-b256-7c40face457a-c000.zstd.parquet`
- Source object size: 797,757,478 bytes

## Timing results

### Deployed ID locator

One initial request took 0.74 seconds. Ten subsequent independent requests ranged from 0.11 to 0.43 seconds and were mostly 0.16–0.18 seconds.

The response included the exact source filename, theme/type, release, bbox, and complete `overture_path`.

### Exact-file native hydration

DuckDB on the factory machine queried only the returned source file and filtered by GERS ID plus exact bbox.

| projection | cold | warm range |
|---|---:|---:|
| ID, primary name, category, confidence, bbox | 2.29 s | 0.28–0.31 s |
| Full 19-column feature | 2.48 s | 0.32–0.37 s |

### Combined lookup + full hydration

Five sequential measurements in one native process:

| run | ID lookup | hydration | total |
|---:|---:|---:|---:|
| 1 | 0.157 s | 2.340 s | 2.497 s |
| 2 | 0.107 s | 0.321 s | 0.428 s |
| 3 | 0.106 s | 0.339 s | 0.445 s |
| 4 | 0.189 s | 0.316 s | 0.505 s |
| 5 | 0.095 s | 0.333 s | 0.428 s |

This is a native DuckDB baseline, not a Worker benchmark. It demonstrates that the locator and exact-file strategy can be sub-second after metadata/connections are warm.

## Source Parquet anatomy

The source object contains 512 row groups and is entirely ZSTD-compressed.

The exact bbox statistics identify row group 32:

- Rows: 6,358
- All-column compressed bytes: 1,061,122
- All-column uncompressed bytes: 2,302,776
- Contiguous byte span including small gaps: 1,067,064
- Direct S3 range timing for that span: 0.27 s TTFB / 0.66 s total

A minimal ID/name/category/confidence/bbox projection accounts for roughly 313 KiB of compressed column chunks. Full optional hydration requires about 1.06 MB compressed for this row group.

The file footer is unusually important:

- Footer metadata: 3,093,986 bytes plus the 8-byte trailer
- Direct footer range timing: 0.27 s TTFB / 0.89 s total

Fetching an 8-byte trailer, then a 3.09 MB footer, then a 1.07 MB row group explains much of the cold 2.5-second result. The footer should be cached or removed from the request path with a generated sidecar.

## Worker feasibility

The current Worker dependency is:

```toml
parquet = { version = "54", default-features = false, features = ["snap"] }
```

Therefore it cannot currently decode Overture's ZSTD source columns. A compile-only probe temporarily enabled `features = ["snap", "zstd"]`; `cargo check -p geocoder-worker --target wasm32-unknown-unknown` completed successfully. The temporary dependency and lockfile changes were reverted afterward.

This means ZSTD is not a compile-time WASM blocker. Still unmeasured:

- Release WASM bundle growth from `zstd-sys`.
- ZSTD decode CPU in the Cloudflare runtime.
- Peak allocations in the Parquet record reader.
- Whether nested full-feature decoding stays within Worker CPU limits.

The measured 2.30 MB uncompressed row group is comfortable relative to Worker memory, but the reader may allocate additional buffers and nested values. Projection pushdown would reduce both memory and CPU.

## Batch behavior

All ten measured `starbucks` results and all ten geographically scattered `golden gat*` results resolved to the same source Parquet object.

- Ten parallel deployed ID lookups: 0.77–0.96 seconds wall time.
- One batched full-feature DuckDB query for the Starbucks IDs: 2.52 seconds cold and 0.35–0.38 seconds warm.

One row was excluded from the quick batch only because the test's manually rounded bbox envelope was too tight. Individual exact ID/bbox retrieval succeeded.

Sharing a filepath does not guarantee sharing one row group. Localized results are likely to group well; geographically scattered results may require multiple ~1 MB row-group reads. Since compact search results already contain the useful basic projection, per-selected-result hydration remains the safer API shape.

## Recommended source locator extension

The current locator provides filepath and bbox. Add one of:

- `source_row_group_id` plus a per-source-file sidecar containing row-group column ranges and schema/footer metadata; or
- a direct source-row-group byte range and enough compact Parquet metadata to construct the reader.

The sidecar option is more flexible. For this 512-row-group source file, a compact table of spatial bounds and byte ranges should be tens of KiB rather than the 3.09 MB native Parquet footer. It can be cached with the release manifest and reused across many IDs.

An offline release builder already scans Overture source metadata. Generating these sidecars on the factory machine is inexpensive compared with the full ingestion pipeline.

## Expected path

```text
compact search result
  → optional detail request for selected GERS ID
  → ID shard lookup: filepath + bbox (+ future row-group locator)
  → cached source sidecar / direct row-group range
  → fetch selected ZSTD Parquet column chunks from public Overture S3
  → decode and select exact GERS ID in Worker
  → return hydrated feature
```

With a direct row-group locator, the measured network lower bound is approximately 0.1–0.2 seconds for a warm ID lookup plus 0.66 seconds for an uncached 1.07 MB S3 range, before Worker decode. Sub-second is plausible but not yet proven in the Worker runtime. Edge caching of source row groups or selected column chunks would improve repeat requests.

