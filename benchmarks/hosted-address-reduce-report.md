# Hosted address reduce-stage spike

Date: 2026-07-15
GitHub Actions run: [29436633251](https://github.com/brad-richardson/overture-geocoder/actions/runs/29436633251)

## Verdict

The reduce stage is computationally viable on a standard public GitHub-hosted
runner. This deliberately simple storage encoding is not viable as the final
planet format without compression or a smaller supported record set.

The complete job passed in 2m38s. Projection peaked at 1,533,964,288 bytes RSS;
the Python map-fragment sort, streaming k-way merge, assembly, and full
verification peaked at 715,354,112 bytes RSS and used a conservative
757,454,450-byte local workspace estimate. Both stages therefore have
substantial headroom against the hosted runner's 16 GB RAM and 14 GB disk.
R2 fragment upload/download remains unmeasured.

## Measured flow

```text
3,743,307 projected source rows (143.9 MB Parquet)
        |
        | require non-empty street + number and valid Point
        v
1,382,264 indexed rows + 2,361,043 explicit rejections
        |
        | 30 independently sorted map fragments (204.3 MB)
        v
streaming k-way merge + sparse directory + full verification
        |
        v
1 immutable range-readable shard (204.6 MB)
```

| metric | result |
|---|---:|
| projected rows | 3,743,307 |
| structured exact-address rows | 1,382,264 (36.93%) |
| missing street or number | 2,361,043 (63.07%) |
| invalid Point geometry | 0 |
| map fragments | 30 |
| fragment build/sort | 80.84 seconds |
| reduce merge/assembly | 31.66 seconds |
| reducer script including verification | 129.48 seconds |
| complete hosted job | 2m38s |
| projection peak RSS | 1,534.0 MB |
| reducer peak RSS | 715.4 MB |
| conservative reducer workspace | 757.5 MB |
| final artifact | 204,646,996 bytes |
| bytes per indexed row | 148.1 |
| distinct exact lookup keys | 1,258,445 |
| maximum candidate fanout | 137 |

Every retained record carries feature ID, quantized coordinates, exact source
row-group and row index, display address fields, and the complete raw
`address_levels` value sequence. The one source filepath and identity are bound
once in the artifact header. A production shard receiving multiple source
objects needs a compact source dictionary plus a per-record source ID.

The reducer streams one record from each sorted fragment through a heap; it does
not load the partition into memory. The final shard has a front sparse directory
every 256 rows, followed by sorted length-delimited records. Verification scanned
the complete artifact for ordering and count agreement and compared three exact
candidate sets and their ID digests with the merge oracle, including the
137-candidate maximum-fanout key. No artifact was uploaded or published.

## Storage/accuracy tradeoff

The current format costs 148.1 B per retained address, 3.38x the earlier 43.8
B/address Massachusetts artifact. That difference is intentional evidence:
the earlier artifact dictionary-encoded repeated structured values but lacked
raw address levels and source locators; this spike preserves those fields but
also naively repeats normalized and display strings.

Linear diagnostics illustrate the decision boundary:

- Keeping this shape for all 473M planning rows would be about 70.0 GB for one
  address release, already over the combined 40 GB Places+address stop gate.
- If the measured 36.93% keyable fraction held globally, the same encoding would
  be about 25.9 GB. This is not a forecast: it comes from one
  purposively selected source-object range and address completeness varies by
  country and provider.
- At 148.1 B/row, a 2-4M-row shard would be roughly 296-592 MB, larger than the
  proposed 96-192 MB target. Smaller shards would increase object count.
- Dropping the 63.07% of rows without both street and number produces a
  defensibly limited structured exact-address service, but is a severe coverage
  choice. Those rows may still be useful for other address-like or reverse
  tasks.

The next storage experiment should keep the same selected rows and exact
candidate oracle while separately measuring:

1. dictionary/prefix compression for repeated normalized and display strings;
2. raw-address-level dictionary IDs and a source-file dictionary;
3. a bare hot record with ID, coordinates, lookup keys, and locator, with full
   display hydration optional rather than required on the normal query path;
4. compressed bytes and Worker range-read fanout, not just local artifact size.

Hydrating directly from Overture S3 remains optional and potentially slow: the
locator makes the row group exact, but the Worker still needs a bounded zstd/
Parquet decoding path. The minimal shard should therefore return a useful exact
address response without mandatory hydration.

## Scope

This is reducer-envelope and storage-shape evidence, not a representative
global completeness measurement, relevance test, one-line parser, division
join, R2 shuffle measurement, or production artifact. The checked-in JSON is a
normalized summary of the ephemeral job report; the Actions log is the raw
execution record.
