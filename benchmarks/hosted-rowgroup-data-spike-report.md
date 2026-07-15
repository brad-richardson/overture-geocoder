# Hosted address row-group data spike

Date: 2026-07-15  
GitHub Actions run: [29389874223](https://github.com/brad-richardson/overture-geocoder/actions/runs/29389874223)

## Verdict

A standard public GitHub-hosted runner can read and project this current-release
address range within the bounded runner envelope without downloading its
complete source object. The result makes a hosted global producer plausible,
not proven.

The initial projection's non-loopback receive-byte upper bound was 106,639,014
bytes, 16.2% of the 657,344,776-byte object. That is 2.08x the 51,341,886 bytes
reported by Parquet metadata for the projected compressed column chunks, so
range coalescing, read-ahead, protocol overhead, and unrelated runner traffic
need an explicit amplification budget. The checked-in
[`hosted-rowgroup-data-spike-raw.json`](hosted-rowgroup-data-spike-raw.json)
preserves the emitted fields and values; the companion report JSON documents
its presentation normalizations and added provenance.

## Hosted measurement

| metric | result |
|---|---:|
| input rows | 1,415,000 |
| source row groups | 24 / 256 |
| selected columns | 9 |
| selected-column uncompressed metadata bytes | 100,466,544 |
| selected-column compressed metadata bytes | 51,341,886 |
| initial network receive upper bound | 106,639,014 |
| hydration/final-HEAD receive upper bound | 6,389,856 |
| output Parquet | 54,101,306 bytes |
| elapsed | 6.11 seconds |
| read and decode | 3.14 seconds |
| write | 0.73 seconds |
| peak RSS | 764,813,312 bytes (729.4 MiB) |

The output retained all intended current address columns plus exact source row
group and row indexes. Its schema metadata carries the canonical single-object
inventory—release, family, URI, ETag, bytes, and version ID—plus its digest.
Pre/post source identity, output record count, and three source-locator hydration
samples all verified.

## Diagnostic global shape

Linearizing this one range to the 473 million planning count gives 334.276 task
equivalents and requires 335 covering jobs: 35.6 GB of runner receive traffic,
18.1 GB of intermediate projected Parquet, and 89.3 aggregate runner-minutes
using the observed 16-second job duration. Ideal four-way division is a
22.3-minute task-stage lower-bound diagnostic; it assumes immediate scheduling,
identical tasks, no S3 contention, and no orchestration barriers.

These are diagnostics, not forecasts. The run intentionally chose the first
eligible object and its first row groups. It excludes the global distribution,
object and row-group skew, inventory reads, retries, R2 fragment upload,
cross-job shuffle, reduce/sort, compact serving-index assembly, multi-source
locator dictionaries/manifests, address/division spatial joins, Places, and
release promotion. At this task size 335 jobs also
exceed GitHub's 256-job matrix limit, so production planning must either use two
bounded batches or safely increase each task range.

## Next gate

Inventory every current-release address object's row-group rows and byte sizes,
then create byte-balanced ranges across small, median, large, and non-US source
objects. The next planner should test whether a 128-map-job target is safe; it
would require about 3.70M addresses per task, 2.61x this measured range. It must
fail closed on per-task memory, disk, runtime, or network amplification before
any R2 credentials are introduced.
