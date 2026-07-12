# Bounded transportation snapshot name-cluster experiment

- Overture release: `2026-06-17.0`
- Boundary: Overture Boston locality division_area (official dataset polygon) with an experimental extraction halo
- Polygon scan bbox: `[-71.1912442, 42.2279149, -70.8044881, 42.3969775]`; topology halo: `0.005°`
- Road / named-road segments: **65,176 / 22,385**
- Materialized input: **14.9 MiB**
- Primary names/core-touching snapshot clusters: **4,864 / 5,832**
- Unique connector IDs: **99,878**
- Names split into snapshot clusters: **559**
- Snapshot cluster segment counts p50/p90/p99/max: **1 / 7 / 30 / 161**
- Core named segments changed by halo: **89 / 18,696**
- Measured hot cluster+alias lookup: **2.01 MiB**
- Measured full deduplicated JSON detail: **16.44 MiB**

## Build and source evidence

- Boundary / road / connector extraction: **56.53s / 85.99s / 21.93s**
- Connector/name cluster build: **6.30s**
- Connector references endpoint/interior/invalid: **130,353 / 82,998 / 0**
- Segment root/property-specific source records: **84,728 / 1,779**
- Segment source records with `between` / confidence: **29,652 / 0**
- Connector root/property-specific source records: **98,521 / 0**
- Source confidence is source-supplied and not calibrated across datasets; missing is not zero.

## Architecture implications

- These are release-scoped snapshot clusters, not stable real-world street IDs.
- Primary names define this conservative topology grouping; common aliases live in a separate lookup.
- Scoped name rules remain assertions until between/side/perspective semantics are evaluated.
- Shared Overture connector IDs are the only graph edges.
- Serialize segment metadata once and let cluster records reference segment IDs.
- Locality/postcode must be spatially enriched. Exact name matching is useful only as a coverage diagnostic.
- Frontier clusters need continuation metadata or overlapping regional partitions.

## Address context diagnostic

- Bbox-observable street-bearing Point-within-polygon population: **397,783**
- Hash sample contributing normalized-name context: **94,771** / cap **100,000**
- The sampler targets 95% of the cap before the hard LIMIT, avoiding cap-edge truncation without a global sort; therefore a result below 100,000 is expected.
- Name-level cluster coverage: **4,391 / 5,832 (75.29%)**
- Extraction: `{"aggregated_street_names": 3712, "bbox_observable_point_within_polygon_population": 397783, "elapsed_seconds": 24.813, "hash_threshold_u32": 1025739896, "row_guard": 100000, "rows": 94771, "sample_fraction": 0.23824798948170234, "sampled": true, "sampling_method": "pinned hash(id) threshold targeting 95% of the row cap, followed by a hard LIMIT; avoids a global deterministic sort", "selection_contract": "bbox-prefiltered for I/O, then exact Point-within unsimplified division_area; missing/discordant bbox rows are unobservable", "skipped": false, "warning": "Name-level proxy only; no contexts are assigned to snapshot clusters."}`
- Diagnostic: `{"address_street_names": 3712, "ambiguous_repeated_name_snapshot_clusters_with_context": 1215, "coverage": 0.752914951989026, "diagnostic_only": true, "snapshot_clusters_with_name_level_address_context": 4391, "uniquely_named_snapshot_clusters_with_context": 3176, "warning": "Exact normalized-name matching is not snapshot-cluster enrichment. Disconnected same-name clusters require a spatial address-to-segment join."}`

## Geometric crossings without shared connector reference

`{"candidate_pair_guard": 1000000, "geometric_crossings_without_shared_connector_reference": 588, "interpretation": "This only shows that two geometries cross without sharing a connector reference; it does not classify grade, bridge, tunnel, or data validity.", "segment_guard": 20000, "segment_rows": 18696, "tile_degrees": 0.01, "tile_membership_guard": 1000000, "tile_memberships": 21716, "tiled_bbox_candidate_pairs": 59974}`

## Connector feature clipping diagnostic

- Referenced connector IDs absent from the polygon+halo connector-feature extract: **2,720 / 99,878 (2.72%)**
- A missing connector feature can be boundary clipping; it is not proof of invalid Overture topology in this bounded snapshot.

## Limitations

The core is Overture's pinned Boston locality dataset polygon, not a claim of legal boundary authority; snapshot clusters remain clipped at its halo, and the address join is name-level only. Conservative exact-name normalization can split abbreviations or spelling variants, and unnamed intermediate segments can fragment a named road. Results are experimental sizing/topology evidence, not a production shard or quality benchmark.
