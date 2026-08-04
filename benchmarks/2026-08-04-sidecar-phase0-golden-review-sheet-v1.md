# Sidecar Phase 0 golden review sheet

Generated at: 2026-08-04T00:00:00Z

This sheet is a reading surface for `benchmarks/2026-08-04-sidecar-phase0-golden-review-set-v1.json`. It records provisional decisions only. Nothing here is a verdict, a gate result, or a prominence change.

- Decisions: 200 (provisionally accepted 198, provisionally needs_review 2)
- Overture release: 2026-06-17.0
- Matcher version: direct-foursquare-p1968-v1
- Candidate set sha256: `498b9f60a0ffafbe8ef6d8a829accbab764e7425444f3281d457fb6bad90a031`
- Review queue sha256: `3d087a4a2573a5d1e1990ee9735b4a6132456828c730cfcc30de734aced17a98`
- Record verdicts in `benchmarks/2026-08-04-sidecar-phase0-golden-verdicts-v1.json`, then run `scripts/validate_sidecar_phase0_golden_review.py`.

## Evidence known to be missing from the frozen inputs

- **overture_categories** (absent_from_frozen_input): scripts/collect_sidecar_phase0_foursquare.py selected only names, bbox coordinates, country, and bridge source ids from the public Places rows, so benchmarks/sidecar-phase0-foursquare-places-v1.jsonl carries no category. Re-collecting would change the frozen input hashes that the candidate set and this review set are bound to. _Workaround:_ Use the Overture Explorer or the release parquet for the GERS ID if a category is decisive; record what you found in the verdict note.
- **wikidata_description** (absent_from_frozen_input): The frozen SPARQL snapshot selected ?item, ?foursquare, ?coord and ?itemLabel only, so no schema:description was retrieved. _Workaround:_ Open the wikidata.org URL in review_urls.
- **wikidata_aliases** (absent_from_frozen_input): The frozen SPARQL snapshot requested no skos:altLabel values. The names list below is the set of distinct itemLabel values observed for the QID in the snapshot, not the Wikidata alias set. _Workaround:_ Open the wikidata.org URL in review_urls.
- **wikidata_statement_id** (absent_from_frozen_input): The snapshot used the truthy wdt:P1968 predicate, which returns claim values without statement GUIDs, ranks, or qualifiers. _Workaround:_ The claim value (the Foursquare venue id) plus the snapshot and query hashes below identify the claim; open the QID to see rank and references.

## Risk class index

| order | risk class | decisions |
| ---: | --- | ---: |
| 1 | Direct identifier conflict | 2 |
| 3 | Observed distance beyond the match-radius gate | 5 |
| 4 | No computable distance | 21 |
| 5 | No normalized label overlap | 122 |
| 6 | Clean direct control | 50 |

## Direct identifier conflict (2)

Two or more Wikidata entities claim the same Overture place through the same direct Foursquare identifier. At most one can be the same entity, and possibly neither is. Decide each candidate row on its own: accept only the row whose Wikidata entity is the venue Overture names, and reject the others.

### 001. `gqd-48f7cbe64dfd5434013470f5`

- Provisional decision: **needs_review** (automatic_acceptance=false, rule `direct_source_wikidata_id.conflict`)
- Rule: This Overture place's Foursquare source identity resolves to more than one Wikidata QID, or the source record is claimed by more than one GERS ID. The direct identifier is therefore ambiguous, automatic acceptance is withheld by contract, and the candidate is provisionally needs_review.
- Overture `ac23fb09-5c14-4c9f-8d78-e3872fab4f6d` — names: Zoo Veldhoven — country: NL — point: 51.426010, 5.354108 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba61403f964a5200f3239e3
- Wikidata `Q2198862` — labels: Parrot Park NOP — point: 51.425560, 5.353330 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba61403f964a5200f3239e3 (matches Overture source)
- Distance: 0.073603 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `direct_identifier_conflict` — This GERS place resolves to more than one Wikidata QID through the same direct Foursquare identifier, so at most one candidate row for this place can be the same entity.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2198862 https://foursquare.com/v/4ba61403f964a5200f3239e3

### 002. `gqd-dbd3c583dff469812e86dab7`

- Provisional decision: **needs_review** (automatic_acceptance=false, rule `direct_source_wikidata_id.conflict`)
- Rule: This Overture place's Foursquare source identity resolves to more than one Wikidata QID, or the source record is claimed by more than one GERS ID. The direct identifier is therefore ambiguous, automatic acceptance is withheld by contract, and the candidate is provisionally needs_review.
- Overture `ac23fb09-5c14-4c9f-8d78-e3872fab4f6d` — names: Zoo Veldhoven — country: NL — point: 51.426010, 5.354108 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba61403f964a5200f3239e3
- Wikidata `Q16740309` — labels: Zoo Veldhoven — point: 51.425556, 5.353333 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba61403f964a5200f3239e3 (matches Overture source)
- Distance: 0.073754 km (gate 1.000 km)
- Shared normalized names: zoo veldhoven
- Risk flags:
  - `direct_identifier_conflict` — This GERS place resolves to more than one Wikidata QID through the same direct Foursquare identifier, so at most one candidate row for this place can be the same entity.
- Links: https://www.wikidata.org/wiki/Q16740309 https://foursquare.com/v/4ba61403f964a5200f3239e3


## Observed distance beyond the match-radius gate (5)

The direct identifier matched but the Overture and Wikidata coordinates disagree by more than the 1 km gate. Distance never accepts a match under the Phase 0 contract; treat the gap as a prompt to confirm the two records describe the same venue rather than a venue and its operator, a chain, or a relocated site.

### 003. `gqd-04000f4576cb2763765e6388`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `7cacfab5-cb45-444c-95a1-e5cb9b1c0379` — names: Lake Elkhorn Park — country: US — point: 39.215820, -76.856651 — categories: null (not in frozen input)
- Overture sources: Foursquare:521cacbe498ef38bfce78eae
- Wikidata `Q116187039` — labels: Lake Elkhorn Park — point: 39.185189, -76.844545 — description/aliases: null (not in frozen input)
- P1968 claims: 521cacbe498ef38bfce78eae (matches Overture source)
- Distance: 3.562188 km (gate 1.000 km)
- Shared normalized names: lake elkhorn park
- Risk flags:
  - `distance_over_gate` — The Overture and Wikidata coordinates are 3.562188 km apart, beyond the 1.000 km match-radius gate. Distance never accepts a match under the Phase 0 contract; it is a prompt to confirm both records describe the same venue.
- Links: https://www.wikidata.org/wiki/Q116187039 https://foursquare.com/v/521cacbe498ef38bfce78eae

### 004. `gqd-346cb75ce4d4ae73aa572a78`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b64dd0c4-b535-47c4-afac-3d0e4524f4dc` — names: Read Between the Signs — country: US — point: 41.617924, -80.226013 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c4af19fc668e21e0e045cf9
- Wikidata `Q105891261` — labels: PennDOT Road Sign Sculpture Garden — point: 41.620833, -80.166667 — description/aliases: null (not in frozen input)
- P1968 claims: 4c4af19fc668e21e0e045cf9 (matches Overture source)
- Distance: 4.943865 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_over_gate` — The Overture and Wikidata coordinates are 4.943865 km apart, beyond the 1.000 km match-radius gate. Distance never accepts a match under the Phase 0 contract; it is a prompt to confirm both records describe the same venue.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105891261 https://foursquare.com/v/4c4af19fc668e21e0e045cf9

### 005. `gqd-6de9f8c2f3dbe63b649784ea`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a9573e3e-8770-485d-bfeb-cde1f1e68f57` — names: Κλειστό Γυμναστήριο Δημήτρης Κραχτίδης — country: GR — point: 41.146011, 24.163933 — categories: null (not in frozen input)
- Overture sources: Foursquare:530898ec498e6152e52bb23b
- Wikidata `Q16327827` — labels: Dimitris Krachtidis Municipal Indoor Hall — point: 41.156774, 24.145532 — description/aliases: null (not in frozen input)
- P1968 claims: 530898ec498e6152e52bb23b (matches Overture source)
- Distance: 1.950846 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_over_gate` — The Overture and Wikidata coordinates are 1.950846 km apart, beyond the 1.000 km match-radius gate. Distance never accepts a match under the Phase 0 contract; it is a prompt to confirm both records describe the same venue.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16327827 https://foursquare.com/v/530898ec498e6152e52bb23b

### 006. `gqd-c614f035067375b926a5330f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `dc0fe5d3-c240-44f7-955c-cd6139e44ff4` — names: State Game Lodge (Custer State Park Resort) — country: US — point: 43.766476, -103.600136 — categories: null (not in frozen input)
- Overture sources: Foursquare:5a394acb35f9830b3b8b7d7b
- Wikidata `Q106714477` — labels: State Game Lodge — point: 43.764273, -103.380938 — description/aliases: null (not in frozen input)
- P1968 claims: 5a394acb35f9830b3b8b7d7b (matches Overture source)
- Distance: 17.603876 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_over_gate` — The Overture and Wikidata coordinates are 17.603876 km apart, beyond the 1.000 km match-radius gate. Distance never accepts a match under the Phase 0 contract; it is a prompt to confirm both records describe the same venue.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106714477 https://foursquare.com/v/5a394acb35f9830b3b8b7d7b

### 007. `gqd-d2094f870004ed79def8eed5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `83c1de72-504a-4482-a827-37e9d42db140` — names: The Main Street Deli and Ice Cream Shoppe at Circus Circus Reno — country: US — point: 39.531261, -119.815292 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c46a23696abd13a09666e01
- Wikidata `Q107622065` — labels: Main Street Deli — point: 36.137228, -115.165269 — description/aliases: null (not in frozen input)
- P1968 claims: 4c46a23696abd13a09666e01 (matches Overture source)
- Distance: 555.898782 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_over_gate` — The Overture and Wikidata coordinates are 555.898782 km apart, beyond the 1.000 km match-radius gate. Distance never accepts a match under the Phase 0 contract; it is a prompt to confirm both records describe the same venue.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107622065 https://foursquare.com/v/4c46a23696abd13a09666e01


## No computable distance (21)

No distance could be computed, so the only evidence is the identifier and the names. Read the null reason: either Wikidata published no P625 coordinate, or it published several and the collector deliberately refused to pick one.

### 008. `gqd-0586bda3c91656bdc0facfd7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `3dc661bd-8302-43e0-8a5d-0950bb9ae367` — names: Villa Victoria Center for the Arts — country: US — point: 42.341000, -71.075890 — categories: null (not in frozen input)
- Overture sources: Foursquare:4adc7853f964a5207c2c21e3
- Wikidata `Q107651039` — labels: Villa Victoria Center for the Arts — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 4adc7853f964a5207c2c21e3 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: villa victoria center for the arts
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q107651039 https://foursquare.com/v/4adc7853f964a5207c2c21e3

### 009. `gqd-0794bede93ba4665c43bf2c3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a7976584-f666-4fd7-9f9e-21d41ee3bd79` — names: Chicago Botanic Garden — country: US — point: 42.149048, -87.789528 — categories: null (not in frozen input)
- Overture sources: Foursquare:4a4fa2f5f964a52083af1fe3
- Wikidata `Q2919730` — labels: Chicago Botanic Garden — point: 42.147500, -87.789722 | 42.148333, -87.790000 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4a4fa2f5f964a52083af1fe3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: chicago botanic garden
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q2919730 https://foursquare.com/v/4a4fa2f5f964a52083af1fe3

### 010. `gqd-28356093926d9a468ba5e7f9`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `7f973e99-cae8-43d5-b8cb-cf98aced0118` — names: Ardex — country: DE — point: 51.448883, 7.384526 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ca9746ff47ea143cb7e7f21
- Wikidata `Q296573` — labels: Ardex — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 4ca9746ff47ea143cb7e7f21 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: ardex
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q296573 https://foursquare.com/v/4ca9746ff47ea143cb7e7f21

### 011. `gqd-2a6778ef5f0c34d1cd0c990c`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f3f18e32-3fdf-4fe8-b21f-787f9a52b242` — names: Hilton Stockholm Slussen — country: SE — point: 59.320454, 18.069277 — categories: null (not in frozen input)
- Overture sources: Foursquare:4adcdaeaf964a520985921e3
- Wikidata `Q10524047` — labels: Hilton Stockholm Slussen — point: 59.320420, 18.069210 | 59.320554, 18.069111 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4adcdaeaf964a520985921e3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: hilton stockholm slussen
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q10524047 https://foursquare.com/v/4adcdaeaf964a520985921e3

### 012. `gqd-30869952692d1d57a2d433d5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `5e7fee20-02b1-44fd-bd63-d9e5b80837a0` — names: Hilton Prague Old Town — country: CZ — point: 50.088558, 14.431809 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b233fbcf964a520a95424e3
- Wikidata `Q30139243` — labels: Hilton Prague Old Town — point: 50.088550, 14.431840 | 50.088691, 14.431864 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4b233fbcf964a520a95424e3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: hilton prague old town
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q30139243 https://foursquare.com/v/4b233fbcf964a520a95424e3

### 013. `gqd-4bcb24e1850398253a4a37f7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `546d7016-db98-4ce5-80a6-86e346c0dec7` — names: Museu de Arte Sacra — country: BR — point: -12.978951, -38.516212 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d695b156297721ecab4c7b5
- Wikidata `Q10333776` — labels: Museum of Sacred Art — point: -12.979017, -38.516075 | -12.979009, -38.515848 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4d695b156297721ecab4c7b5 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10333776 https://foursquare.com/v/4d695b156297721ecab4c7b5

### 014. `gqd-4db25be0d2ac1bf902002fa1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c5048960-bc9c-4dce-9818-8d7fcd68d347` — names: Factorhy Avocats — country: FR — point: 48.871445, 2.318853 — categories: null (not in frozen input)
- Overture sources: Foursquare:60cf7a38dad9d840f77b9d5d
- Wikidata `Q106594103` — labels: Factorhy Avocats — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 60cf7a38dad9d840f77b9d5d (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: factorhy avocats
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q106594103 https://foursquare.com/v/60cf7a38dad9d840f77b9d5d

### 015. `gqd-531411f38185c8d446707d16`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d9fd9c8f-6b28-4441-a284-cca328e6b98c` — names: Hilton Warsaw City — country: PL — point: 52.233509, 20.986036 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b96b46cf964a52029df34e3
- Wikidata `Q1630911` — labels: Hilton Warsaw City — point: 52.233510, 20.985840 | 52.233561, 20.985900 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4b96b46cf964a52029df34e3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: hilton warsaw city
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q1630911 https://foursquare.com/v/4b96b46cf964a52029df34e3

### 016. `gqd-560f1d68828c86758691f98a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b844e5ff-bb52-449d-b552-4c26baa82f76` — names: Mons Venus — country: US — point: 27.960939, -82.506111 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b2e624bf964a52037df24e3
- Wikidata `Q113001860` — labels: Mons Venus — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 4b2e624bf964a52037df24e3 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: mons venus
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q113001860 https://foursquare.com/v/4b2e624bf964a52037df24e3

### 017. `gqd-568d0c5008b3a724858ca858`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `4176f9ef-95b2-4abc-a3c8-f0df13835dc3` — names: Kechara — country: MY — point: 3.119046, 101.599014 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c3c8bc64bc9a5939ac8d271
- Wikidata `Q106570608` — labels: Kechara Buddhist Association of Malaysia — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 4c3c8bc64bc9a5939ac8d271 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106570608 https://foursquare.com/v/4c3c8bc64bc9a5939ac8d271

### 018. `gqd-66590d02c74bd835d9c1571d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c4218e93-95c4-413b-b696-b5df3916e1c4` — names: Bangladesh Institute of Health Sciences (BIHS) — country: BD — point: 23.781849, 90.352386 — categories: null (not in frozen input)
- Overture sources: Foursquare:50f55e3bebca56a9eabca181
- Wikidata `Q103165302` — labels: Bangladesh Institute of Health Sciences — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 50f55e3bebca56a9eabca181 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q103165302 https://foursquare.com/v/50f55e3bebca56a9eabca181

### 019. `gqd-6fe101d369d12554d2689794`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `14347efa-84c6-4ff7-aaf2-aee07a7d86a8` — names: DoubleTree by Hilton Hotel Avanos - Cappadocia — country: TR — point: 38.714661, 34.830135 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d41c644915a37045166237f
- Wikidata `Q30242708` — labels: DoubleTree by Hilton Avanos - Cappadocia — point: 38.714012, 34.830340 | 38.714472, 34.830222 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4d41c644915a37045166237f (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q30242708 https://foursquare.com/v/4d41c644915a37045166237f

### 020. `gqd-752e402eb4571e898877b8b0`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `18ac152b-cfc9-4733-971a-2cbad8972ad2` — names: Cake In A Cup — country: US — point: 41.674400, -83.705635 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b7c534bf964a520788c2fe3
- Wikidata `Q16828739` — labels: Cake in a Cup — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 4b7c534bf964a520788c2fe3 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: cake in a cup
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q16828739 https://foursquare.com/v/4b7c534bf964a520788c2fe3

### 021. `gqd-7d0696a5e3726aa15cb86c6a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `088e0522-f96c-428d-8e6d-3718d5f5e5e9` — names: The Rooftop at Pier 17 — country: US — point: 40.705460, -74.001610 — categories: null (not in frozen input)
- Overture sources: Foursquare:5b4fc2b467af3a002c9ab00f
- Wikidata `Q107281426` — labels: The Rooftop at Pier 17 — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 5b4fc2b467af3a002c9ab00f (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: the rooftop at pier 17
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q107281426 https://foursquare.com/v/5b4fc2b467af3a002c9ab00f

### 022. `gqd-8ca0cfa110d1b5cd7b3860f3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `4037f8c5-720f-489b-b2ea-8382bceda81f` — names: KSL Hotel & Resort — country: MY — point: 1.486075, 103.762901 — categories: null (not in frozen input)
- Overture sources: Foursquare:51de5ce5ccda86a6009d167f
- Wikidata `Q105742660` — labels: KSL Hotel and Resort — point: 1.484450, 103.762344 | 1.485528, 103.762361 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 51de5ce5ccda86a6009d167f (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105742660 https://foursquare.com/v/51de5ce5ccda86a6009d167f

### 023. `gqd-99238f1d233f517af865bd25`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e7488c0d-27aa-4f06-85df-377fc75d95d4` — names: Four Seasons Hotel Philadelphia at Comcast Center — country: US — point: 39.955196, -75.170937 — categories: null (not in frozen input)
- Overture sources: Foursquare:5d3784ea65a8bc000839118b
- Wikidata `Q101337606` — labels: Four Seasons Hotel Philadelphia at Comcast Center — point: 39.955000, -75.170833 | 39.955160, -75.170960 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 5d3784ea65a8bc000839118b (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: four seasons hotel philadelphia at comcast center
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q101337606 https://foursquare.com/v/5d3784ea65a8bc000839118b

### 024. `gqd-a9b3c2a482ff94fd95c5ecbe`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `cd312061-2e72-4ac2-8314-82f60f0b26e6` — names: Spitali Universitar Obstetrik Gjinekologjik Mbretresha Geraldine — country: AL — point: 41.334606, 19.817278 — categories: null (not in frozen input)
- Overture sources: Foursquare:5458cb1e498ebedaabf75273
- Wikidata `Q101424666` — labels: Spitali Universitar Obstetrik Gjinekologjik Mbreteresha Geraldine — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 5458cb1e498ebedaabf75273 (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q101424666 https://foursquare.com/v/5458cb1e498ebedaabf75273

### 025. `gqd-b2febc08d9b3b549a4910653`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a6fd6a5d-8a43-4932-a4e1-9cdab4058cfb` — names: Restaurant Pier — country: CH — point: 47.586021, 9.343198 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c24b097f7ced13a5f2e236d
- Wikidata `Q101537821` — labels: Restaurant Pier — point: absent — description/aliases: null (not in frozen input)
- P1968 claims: 4c24b097f7ced13a5f2e236d (matches Overture source)
- Distance: null — wikidata_coordinate_absent (gate 1.000 km)
- Shared normalized names: restaurant pier
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned no P625 coordinate for this QID, so no distance can be computed.
- Links: https://www.wikidata.org/wiki/Q101537821 https://foursquare.com/v/4c24b097f7ced13a5f2e236d

### 026. `gqd-c2d40893ec650778f2c76535`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c8cac867-e5a1-4d8e-957e-c239104b409b` — names: Conrad Bangkok — country: TH — point: 13.738386, 100.548363 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b0587f6f964a5203aa922e3
- Wikidata `Q11303288` — labels: Conrad Bangkok — point: 13.738417, 100.548139 | 13.738611, 100.548333 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4b0587f6f964a5203aa922e3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: conrad bangkok
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q11303288 https://foursquare.com/v/4b0587f6f964a5203aa922e3

### 027. `gqd-d43fc792b5b03ef226bf5768`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `89879185-20b5-4b1a-8266-96df530ba0f5` — names: ヒルトン大阪 — country: JP — point: 34.699875, 135.495987 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b62de21f964a52062562ae3
- Wikidata `Q11330331` — labels: Hilton Osaka — point: 34.699922, 135.495876 | 34.699972, 135.495944 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4b62de21f964a52062562ae3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q11330331 https://foursquare.com/v/4b62de21f964a52062562ae3

### 028. `gqd-dda7d13184d604792dbb24a1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `0d972151-832f-4adf-8f92-2186dc29ac32` — names: Hilton Cologne — country: DE — point: 50.943115, 6.955848 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b831ec5f964a520e9f830e3
- Wikidata `Q30021076` — labels: Hilton Cologne — point: 50.943028, 6.955778 | 50.943080, 6.955880 (ambiguous, none selected) — description/aliases: null (not in frozen input)
- P1968 claims: 4b831ec5f964a520e9f830e3 (matches Overture source)
- Distance: null — wikidata_coordinate_ambiguous (gate 1.000 km)
- Shared normalized names: hilton cologne
- Risk flags:
  - `distance_missing` — No distance could be computed for this pair. The frozen Wikidata snapshot returned 2 distinct P625 coordinates for this QID. The collector deliberately refuses to select one, so no distance is computed. Every candidate point is listed under wikidata.coordinate_candidates.
- Links: https://www.wikidata.org/wiki/Q30021076 https://foursquare.com/v/4b831ec5f964a520e9f830e3


## No normalized label overlap (122)

The Overture names and the Wikidata label share no normalized token string, so the acceptance rests entirely on the direct identifier. Expect legitimate cases (different language, official versus common name, renamed venue) and illegitimate ones (recycled Foursquare venue id, entity describing the operator rather than the place).

### 029. `gqd-00e8584da25ea6b2a53b6ee6`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `5a9fe517-b78e-41c1-81b7-ee04f99b052f` — names: Καπάνι (Kapani) — country: GR — point: 40.635590, 22.942883 — categories: null (not in frozen input)
- Overture sources: Foursquare:5089077de4b09cdfe678a123
- Wikidata `Q16329958` — labels: Kapani — point: 40.635878, 22.942477 — description/aliases: null (not in frozen input)
- P1968 claims: 5089077de4b09cdfe678a123 (matches Overture source)
- Distance: 0.046898 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16329958 https://foursquare.com/v/5089077de4b09cdfe678a123

### 030. `gqd-01be468a130af600fe9de872`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `85e467c9-0524-4965-ab1d-281701b8d228` — names: St. Nicholas Of Tolentine Parish Cathedral — country: PH — point: 15.489221, 120.964264 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ca14e4d542b224b5db20da0
- Wikidata `Q105154182` — labels: Cabanatuan Cathedral — point: 15.489167, 120.964167 — description/aliases: null (not in frozen input)
- P1968 claims: 4ca14e4d542b224b5db20da0 (matches Overture source)
- Distance: 0.012025 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105154182 https://foursquare.com/v/4ca14e4d542b224b5db20da0

### 031. `gqd-030c2c50e006c4095c74fceb`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `0e05f930-7e0f-470a-bd18-bbca641548a3` — names: Γενική Αστυνομική Διεύθυνση Αττικής — country: GR — point: 37.987610, 23.756083 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d0f066038bb6ea814a0bcaa
- Wikidata `Q16252151` — labels: Attica General Police Directorate — point: 37.987731, 23.756128 — description/aliases: null (not in frozen input)
- P1968 claims: 4d0f066038bb6ea814a0bcaa (matches Overture source)
- Distance: 0.014011 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16252151 https://foursquare.com/v/4d0f066038bb6ea814a0bcaa

### 032. `gqd-045fec9224f7948805007833`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6fb3dc47-7f8b-48b7-a6a6-17f6ee727acf` — names: Université Laval — country: CA — point: 46.782440, -71.276695 — categories: null (not in frozen input)
- Overture sources: Foursquare:50d09adfe4b0e699d70e12dd
- Wikidata `Q1067935` — labels: Laval University — point: 46.780000, -71.274722 — description/aliases: null (not in frozen input)
- P1968 claims: 50d09adfe4b0e699d70e12dd (matches Overture source)
- Distance: 0.310152 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1067935 https://foursquare.com/v/50d09adfe4b0e699d70e12dd

### 033. `gqd-04de6a7c037583a745963f0a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ac2c1200-497d-4353-9c55-91f7cef43c84` — names: Farol da Barra — country: BR — point: -13.010328, -38.532875 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bbb5abc935e95218a102990
- Wikidata `Q10283940` — labels: Forte de Santo Antônio da Barra — point: -13.010278, -38.532778 — description/aliases: null (not in frozen input)
- P1968 claims: 4bbb5abc935e95218a102990 (matches Overture source)
- Distance: 0.01191 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10283940 https://foursquare.com/v/4bbb5abc935e95218a102990

### 034. `gqd-0663c7fa9c46327de23f9a4e`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2e7e68fc-bc09-46f0-9b2f-0a7518b9e1ad` — names: Árkád Pécs Bevásárlóközpont — country: HU — point: 46.071743, 18.231821 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd5c26229eb9c746ada93e1
- Wikidata `Q113025717` — labels: Árkád Pécs — point: 46.072122, 18.232133 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd5c26229eb9c746ada93e1 (matches Overture source)
- Distance: 0.048562 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q113025717 https://foursquare.com/v/4bd5c26229eb9c746ada93e1

### 035. `gqd-0667a541ce3351f391c34c6f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `5ffa6804-7921-41f6-903d-dcda45a16f5e` — names: วัดลาดบัวหลวง — country: TH — point: 14.164798, 100.303299 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cb6c8bb3ac937048245d60a
- Wikidata `Q100449088` — labels: Wat Lat Bua Luang — point: 14.163611, 100.302778 — description/aliases: null (not in frozen input)
- P1968 claims: 4cb6c8bb3ac937048245d60a (matches Overture source)
- Distance: 0.143418 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100449088 https://foursquare.com/v/4cb6c8bb3ac937048245d60a

### 036. `gqd-071033536271dadf1786fe06`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f0c832da-f687-4395-907a-b03de523a481` — names: วัดบางกะจะ — country: TH — point: 14.344787, 100.575020 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c48069b972c0f47f9ab2521
- Wikidata `Q107549092` — labels: Wat Bang Kracha — point: 14.344586, 100.575137 — description/aliases: null (not in frozen input)
- P1968 claims: 4c48069b972c0f47f9ab2521 (matches Overture source)
- Distance: 0.025652 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107549092 https://foursquare.com/v/4c48069b972c0f47f9ab2521

### 037. `gqd-097ac9008add0a39ac27c825`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ded881e9-1727-499c-a572-d727f3aa1ac1` — names: Piano Piano Restaurant — country: CA — point: 43.662956, -79.402908 — categories: null (not in frozen input)
- Overture sources: Foursquare:56eb72e3498eefb98d82274b
- Wikidata `Q102121416` — labels: Piano Piano — point: 43.663029, -79.402887 — description/aliases: null (not in frozen input)
- P1968 claims: 56eb72e3498eefb98d82274b (matches Overture source)
- Distance: 0.008226 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102121416 https://foursquare.com/v/56eb72e3498eefb98d82274b

### 038. `gqd-0aade9e7ff08a94c955d3bb6`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6321f1f9-04d5-4db2-bba3-e3fc703321c1` — names: College Park Airport (CGS) — country: US — point: 38.979939, -76.923080 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b893417f964a520d12232e3
- Wikidata `Q1655471` — labels: College Park Airport — point: 38.980583, -76.922306 — description/aliases: null (not in frozen input)
- P1968 claims: 4b893417f964a520d12232e3 (matches Overture source)
- Distance: 0.098114 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1655471 https://foursquare.com/v/4b893417f964a520d12232e3

### 039. `gqd-0aadea72aa6609122adae977`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2460f1fd-2dee-4d9c-812f-3146f3492b71` — names: The American Civil War Center At Historic Tredegar — country: US — point: 37.535137, -77.445534 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c59c59d67ac0f47f512054c
- Wikidata `Q104845473` — labels: Civil War Visitor Center — point: 37.535377, -77.445822 — description/aliases: null (not in frozen input)
- P1968 claims: 4c59c59d67ac0f47f512054c (matches Overture source)
- Distance: 0.036797 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q104845473 https://foursquare.com/v/4c59c59d67ac0f47f512054c

### 040. `gqd-0b9e5d8e54b294fca574cbda`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e15426d8-c79b-4f1e-a580-c6a1100e8f0c` — names: Burgruine Hardenstein — country: DE — point: 51.421013, 7.302111 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d6132f11939a35de0842aee
- Wikidata `Q1012201` — labels: Hardenstein Castle — point: 51.421000, 7.301000 — description/aliases: null (not in frozen input)
- P1968 claims: 4d6132f11939a35de0842aee (matches Overture source)
- Distance: 0.077028 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1012201 https://foursquare.com/v/4d6132f11939a35de0842aee

### 041. `gqd-0ba185bdb317331a9f31a9cb`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `04f7d6da-c379-4217-b022-7510432b5619` — names: Taco Bell — country: US — point: 37.597153, -122.503693 — categories: null (not in frozen input)
- Overture sources: Foursquare:4aac0edbf964a520f65b20e3
- Wikidata `Q107011918` — labels: Taco Bell Cantina (Pacifica, California) — point: 37.597528, -122.503754 — description/aliases: null (not in frozen input)
- P1968 claims: 4aac0edbf964a520f65b20e3 (matches Overture source)
- Distance: 0.042065 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107011918 https://foursquare.com/v/4aac0edbf964a520f65b20e3

### 042. `gqd-0c298bad74ae36ec2d937756`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `07578e1f-b72b-4882-8ea3-632c9185917b` — names: Les Docks – Cité de la Mode et du Design — country: FR — point: 48.840839, 2.370182 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba12ba3f964a520849e37e3
- Wikidata `Q2974772` — labels: Cité de la mode et du design — point: 48.841111, 2.369722 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba12ba3f964a520849e37e3 (matches Overture source)
- Distance: 0.045238 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2974772 https://foursquare.com/v/4ba12ba3f964a520849e37e3

### 043. `gqd-0dfede4022a3fcd1f1197f15`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `5453bb19-4969-4fe6-9169-00c6747d1131` — names: MTA Subway - 34th St/Herald Sq (B/D/F/M/N/Q/R/W) — country: US — point: 40.749935, -73.987968 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ad7a8d9f964a520640d21e3
- Wikidata `Q2982193` — labels: 34th Street – Herald Square — point: 40.749300, -73.988000 — description/aliases: null (not in frozen input)
- P1968 claims: 4ad7a8d9f964a520640d21e3 (matches Overture source)
- Distance: 0.070676 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2982193 https://foursquare.com/v/4ad7a8d9f964a520640d21e3

### 044. `gqd-0e697b07dfefc565141b4b87`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c204d449-e80b-4f70-bb97-740e6968ef42` — names: วัดพระนอนจักรสีห์วรวิหาร — country: TH — point: 14.851560, 100.388664 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b960732f964a52093ba34e3
- Wikidata `Q106675856` — labels: Wat Phra Non Chaksi Worawihan — point: 14.850833, 100.388611 — description/aliases: null (not in frozen input)
- P1968 claims: 4b960732f964a52093ba34e3 (matches Overture source)
- Distance: 0.080963 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106675856 https://foursquare.com/v/4b960732f964a52093ba34e3

### 045. `gqd-0f10597fab415604104215aa`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `34cf20d6-27fc-4df1-8219-668f3abfdbac` — names: Folkets Bio Malmö/ Biograf Panora — country: SE — point: 55.594269, 13.009488 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e79e07c1f6e07f917e0433b
- Wikidata `Q106592879` — labels: Panora — point: 55.594270, 13.009490 — description/aliases: null (not in frozen input)
- P1968 claims: 4e79e07c1f6e07f917e0433b (matches Overture source)
- Distance: 0.000179 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106592879 https://foursquare.com/v/4e79e07c1f6e07f917e0433b

### 046. `gqd-106352491b512b217046ba9f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `09f16159-5c4b-4492-bc8d-82b5e24208d9` — names: Terminal Rodoviário de Salvador — country: BR — point: -12.977987, -38.465847 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bbde361a8cf76b07748b2fd
- Wikidata `Q10380661` — labels: Estação Rodoviária Armando Viana de Castro — point: -12.977500, -38.465833 — description/aliases: null (not in frozen input)
- P1968 claims: 4bbde361a8cf76b07748b2fd (matches Overture source)
- Distance: 0.054204 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10380661 https://foursquare.com/v/4bbde361a8cf76b07748b2fd

### 047. `gqd-115093da579b05af020c8cf3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `45626db0-e38a-4a96-ac8c-7bfc25824def` — names: Akvariet i Bergen — country: NO — point: 60.399693, 5.303374 — categories: null (not in frozen input)
- Overture sources: Foursquare:4baf4151f964a520def33be3
- Wikidata `Q29329` — labels: Bergen Aquarium — point: 60.399700, 5.303800 — description/aliases: null (not in frozen input)
- P1968 claims: 4baf4151f964a520def33be3 (matches Overture source)
- Distance: 0.023423 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q29329 https://foursquare.com/v/4baf4151f964a520def33be3

### 048. `gqd-11f71b49b54dfed35093a733`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `bf85d1fe-7465-40ba-aff4-9dbe1eeab878` — names: Ebbas — country: SE — point: 57.957359, 19.237547 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c3864a6dfb0e21ef373aea8
- Wikidata `Q106651015` — labels: Ebbas Mat & Kaffe — point: 57.957368, 19.237547 — description/aliases: null (not in frozen input)
- P1968 claims: 4c3864a6dfb0e21ef373aea8 (matches Overture source)
- Distance: 0.000966 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106651015 https://foursquare.com/v/4c3864a6dfb0e21ef373aea8

### 049. `gqd-1432dd792731d2f5712bd062`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ca2936c5-ad53-43ea-a03e-7dc00d8b1fbe` — names: วัดสว่างภพ — country: TH — point: 14.171268, 100.675499 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d43d75214aa8cfaaf995f3d
- Wikidata `Q100670889` — labels: Wat Sawang Phop — point: 14.171111, 100.676111 — description/aliases: null (not in frozen input)
- P1968 claims: 4d43d75214aa8cfaaf995f3d (matches Overture source)
- Distance: 0.068276 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100670889 https://foursquare.com/v/4d43d75214aa8cfaaf995f3d

### 050. `gqd-157ee75782b5dab3c65057a4`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `656d2ce2-764e-407d-be15-af9effa5509c` — names: イコアス千城台 — country: JP — point: 35.622032, 140.189713 — categories: null (not in frozen input)
- Overture sources: Foursquare:5e8d356be177c700082a3f6d
- Wikidata `Q106359407` — labels: ICOAS Chishirodai — point: 35.623194, 140.189056 — description/aliases: null (not in frozen input)
- P1968 claims: 5e8d356be177c700082a3f6d (matches Overture source)
- Distance: 0.142229 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106359407 https://foursquare.com/v/5e8d356be177c700082a3f6d

### 051. `gqd-1665d153b07a447e81240246`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `67bbfec0-a95c-45b6-878e-d7709456116c` — names: วัดโพธิ์ประสิทธิ์ — country: TH — point: 14.153801, 100.394402 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d381375b274b1f7ee1de5a3
- Wikidata `Q100449105` — labels: Wat Pho Prasit — point: 14.153889, 100.394444 — description/aliases: null (not in frozen input)
- P1968 claims: 4d381375b274b1f7ee1de5a3 (matches Overture source)
- Distance: 0.010815 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100449105 https://foursquare.com/v/4d381375b274b1f7ee1de5a3

### 052. `gqd-16d5a1dcce155d6f17bf1c65`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `35cd3f88-d857-4f61-84fd-5f3be4a6f9cb` — names: Подписные издания — country: RU — point: 59.934475, 30.347639 — categories: null (not in frozen input)
- Overture sources: Foursquare:4f3d10d2e4b02e31118f47c5
- Wikidata `Q112960429` — labels: Podpisnie Izdaniya — point: 59.934737, 30.347628 — description/aliases: null (not in frozen input)
- P1968 claims: 4f3d10d2e4b02e31118f47c5 (matches Overture source)
- Distance: 0.029157 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q112960429 https://foursquare.com/v/4f3d10d2e4b02e31118f47c5

### 053. `gqd-18db8e5314c01858c18d7b63`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `70550843-8e47-45d4-ab21-58b6908334f0` — names: Museu da Cidade — country: BR — point: -12.971747, -38.508156 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ece42d26da162f1bc05f8a5
- Wikidata `Q10333666` — labels: City Museum of Salvador — point: -12.971776, -38.510533 — description/aliases: null (not in frozen input)
- P1968 claims: 4ece42d26da162f1bc05f8a5 (matches Overture source)
- Distance: 0.257616 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10333666 https://foursquare.com/v/4ece42d26da162f1bc05f8a5

### 054. `gqd-19d9dc165b27f4048016ce60`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `1f628160-bc51-455a-b262-e8443d12c66e` — names: โรงเรียนเชียงใหม่คริสเตียน (The Chiang Mai Christian School) — country: TH — point: 18.786728, 99.005325 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ce4d6ede571a093a6088787
- Wikidata `Q16333225` — labels: Chiangmai Christian School — point: 18.786809, 99.005490 — description/aliases: null (not in frozen input)
- P1968 claims: 4ce4d6ede571a093a6088787 (matches Overture source)
- Distance: 0.019535 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16333225 https://foursquare.com/v/4ce4d6ede571a093a6088787

### 055. `gqd-1c801ab15da204a0660b77df`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c582da83-be8b-4dfd-bc0a-a03cff45087f` — names: วัดเกวียนหัก — country: TH — point: 12.462008, 102.203552 — categories: null (not in frozen input)
- Overture sources: Foursquare:4da65c504df0c49b0314ed70
- Wikidata `Q106436988` — labels: Wat Kwian Hak — point: 12.462222, 102.204444 — description/aliases: null (not in frozen input)
- P1968 claims: 4da65c504df0c49b0314ed70 (matches Overture source)
- Distance: 0.099769 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106436988 https://foursquare.com/v/4da65c504df0c49b0314ed70

### 056. `gqd-2055be5b11b385fa81e25e10`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e627a9bc-3fa8-460c-b102-79e6cc2122de` — names: C.C Carrefour — country: RE — point: -20.929193, 55.633221 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b9dfe86f964a520b2c536e3
- Wikidata `Q2944944` — labels: Q2944944 — point: -20.929306, 55.633611 — description/aliases: null (not in frozen input)
- P1968 claims: 4b9dfe86f964a520b2c536e3 (matches Overture source)
- Distance: 0.042422 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2944944 https://foursquare.com/v/4b9dfe86f964a520b2c536e3

### 057. `gqd-21f4b8e183121f6798b3e16e`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `283c4776-0db3-43e4-af23-7263fc19e73f` — names: Shopping and Sports — country: BR — point: -25.518589, -49.255505 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d90c32812d52c0fdf6c7834
- Wikidata `Q104836872` — labels: Shopping & Sports — point: -25.518611, -49.257744 — description/aliases: null (not in frozen input)
- P1968 claims: 4d90c32812d52c0fdf6c7834 (matches Overture source)
- Distance: 0.224761 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q104836872 https://foursquare.com/v/4d90c32812d52c0fdf6c7834

### 058. `gqd-245bbac62f375027055522aa`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `26c3166d-1d22-409e-bacb-2133bd310246` — names: ป้ายหยุดรถไฟอุรุพงษ์ (Urupong) SRT3102 — country: TH — point: 13.758678, 100.525604 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ddb98cffa7637ab73dd9fe7
- Wikidata `Q16304392` — labels: Urupong railway halt — point: 13.759000, 100.522000 — description/aliases: null (not in frozen input)
- P1968 claims: 4ddb98cffa7637ab73dd9fe7 (matches Overture source)
- Distance: 0.390913 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16304392 https://foursquare.com/v/4ddb98cffa7637ab73dd9fe7

### 059. `gqd-262f66d6fb3d19efdf74e9f8`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c917dbc6-8914-483c-8c61-606088816113` — names: วัดป่าจิตตภาวนา — country: TH — point: 13.958128, 100.646996 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bff18bfca1920a19e5fec81
- Wikidata `Q100671267` — labels: Wat Pa Chittaphawana — point: 13.958056, 100.646944 — description/aliases: null (not in frozen input)
- P1968 claims: 4bff18bfca1920a19e5fec81 (matches Overture source)
- Distance: 0.00976 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100671267 https://foursquare.com/v/4bff18bfca1920a19e5fec81

### 060. `gqd-2a31702835099f1a7651ab11`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `633b153e-404d-495b-bdd9-7dd22f918d6e` — names: วัดอโยธยา : WatAyothaya — country: TH — point: 14.368190, 100.590149 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e6227a481301b93229a7897
- Wikidata `Q106939574` — labels: Wat Ayothaya — point: 14.368414, 100.589722 — description/aliases: null (not in frozen input)
- P1968 claims: 4e6227a481301b93229a7897 (matches Overture source)
- Distance: 0.052346 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106939574 https://foursquare.com/v/4e6227a481301b93229a7897

### 061. `gqd-2ac0df615da74496540e64f4`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `fedcdd03-79f6-43d6-a753-194a3a5695b0` — names: Abu Nasser | أبو ناصر — country: EG — point: 31.199787, 29.897261 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ffdaa14e4b0184d35e612a0
- Wikidata `Q110488688` — labels: Abo Nasser — point: 31.199700, 29.897210 — description/aliases: null (not in frozen input)
- P1968 claims: 4ffdaa14e4b0184d35e612a0 (matches Overture source)
- Distance: 0.010822 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q110488688 https://foursquare.com/v/4ffdaa14e4b0184d35e612a0

### 062. `gqd-2b105c88a795e8979254198f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b4b728f7-f672-4b6b-9257-7fe68ba4f29b` — names: วัดท้ายหาด — country: TH — point: 13.402438, 99.975792 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cd61d8e94848cfae242ebb1
- Wikidata `Q106737839` — labels: Wat Thai Hat — point: 13.401944, 99.975833 — description/aliases: null (not in frozen input)
- P1968 claims: 4cd61d8e94848cfae242ebb1 (matches Overture source)
- Distance: 0.055082 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106737839 https://foursquare.com/v/4cd61d8e94848cfae242ebb1

### 063. `gqd-2b27673c7ae5adef52255d43`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b39a006c-9e4d-4e1a-837b-90cd1bf8233d` — names: วัดเกาะแรต — country: TH — point: 13.968898, 100.155655 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c397f650a71c9b606ad42c9
- Wikidata `Q100293569` — labels: Wat Ko Raet — point: 13.968889, 100.155278 — description/aliases: null (not in frozen input)
- P1968 claims: 4c397f650a71c9b606ad42c9 (matches Overture source)
- Distance: 0.040707 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100293569 https://foursquare.com/v/4c397f650a71c9b606ad42c9

### 064. `gqd-2ea5e23b8dc2a0a1ca5d6239`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `91cb65e4-8d3d-4731-b4e8-43863fca38e6` — names: Museu de Arte Moderna da Bahia — country: BR — point: -12.983079, -38.518864 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c45fe57f1d80f47ee9ea23c
- Wikidata `Q10333768` — labels: Museum of Modern Art of Bahia — point: -12.982456, -38.520617 — description/aliases: null (not in frozen input)
- P1968 claims: 4c45fe57f1d80f47ee9ea23c (matches Overture source)
- Distance: 0.202195 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10333768 https://foursquare.com/v/4c45fe57f1d80f47ee9ea23c

### 065. `gqd-2f694cf20ff04cc80ca74f95`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d3d5d37b-ec13-41fe-ab8a-62ca8513fd61` — names: HUSL Library — country: US — point: 38.943203, -77.058594 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c6d42e1e13db60c5e2dd8b1
- Wikidata `Q105826454` — labels: Vernon E. Jordan, Jr., Esq. Law Library — point: 38.943157, -77.058552 — description/aliases: null (not in frozen input)
- P1968 claims: 4c6d42e1e13db60c5e2dd8b1 (matches Overture source)
- Distance: 0.006251 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105826454 https://foursquare.com/v/4c6d42e1e13db60c5e2dd8b1

### 066. `gqd-3105576ab5d15003a288a738`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `fa3f44ad-35f7-4482-a115-7ccb6673e379` — names: 鳥羽水族館 — country: JP — point: 34.481434, 136.845779 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b56fc6af964a520d52128e3
- Wikidata `Q1073067` — labels: Toba Aquarium — point: 34.481700, 136.846000 — description/aliases: null (not in frozen input)
- P1968 claims: 4b56fc6af964a520d52128e3 (matches Overture source)
- Distance: 0.03584 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1073067 https://foursquare.com/v/4b56fc6af964a520d52128e3

### 067. `gqd-3413ddb2b87bf4f82f51f0e0`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `53bdcb47-3189-425a-8696-80ce719a9e6b` — names: The Caledonian Edinburgh — country: GB — point: 55.949486, -3.207142 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd16eb89854d13a0588f94d
- Wikidata `Q16256099` — labels: The Caledonian — point: 55.949740, -3.207112 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd16eb89854d13a0588f94d (matches Overture source); waldorfa5289576 (does not match this Overture source)
- Distance: 0.028329 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16256099 https://foursquare.com/v/4bd16eb89854d13a0588f94d

### 068. `gqd-35c41e66ffc960bc0dd6c523`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `5a5c0c6d-41d3-4bf1-b0ed-6094d39d2ce4` — names: วัดวังหิน — country: TH — point: 16.862747, 100.239693 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c787fe5a86837047cb90d4d
- Wikidata `Q102129293` — labels: Wat Wang Hin — point: 16.862778, 100.241111 — description/aliases: null (not in frozen input)
- P1968 claims: 4c787fe5a86837047cb90d4d (matches Overture source)
- Distance: 0.150978 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102129293 https://foursquare.com/v/4c787fe5a86837047cb90d4d

### 069. `gqd-39a445094963fe44f4f80e59`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `243db2db-fbb0-4f0f-a342-d37372ce6248` — names: วัดบึงบอน — country: TH — point: 14.074779, 100.777946 — categories: null (not in frozen input)
- Overture sources: Foursquare:4f24a1b4e4b06176449385ac
- Wikidata `Q100670972` — labels: Wat Bueng Bon — point: 14.074722, 100.777500 — description/aliases: null (not in frozen input)
- P1968 claims: 4f24a1b4e4b06176449385ac (matches Overture source)
- Distance: 0.048561 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100670972 https://foursquare.com/v/4f24a1b4e4b06176449385ac

### 070. `gqd-3a4b617c2a5837f040c5f276`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a2043c03-d414-4544-a687-33cdad8a715a` — names: Manassas National Battlefield Park | Henry Hill Visitors Center — country: US — point: 38.812935, -77.521362 — categories: null (not in frozen input)
- Overture sources: Foursquare:4af6ec9cf964a520280422e3
- Wikidata `Q100148997` — labels: Henry Hill Visitor Center — point: 38.813056, -77.521655 — description/aliases: null (not in frozen input)
- P1968 claims: 4af6ec9cf964a520280422e3 (matches Overture source)
- Distance: 0.028715 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100148997 https://foursquare.com/v/4af6ec9cf964a520280422e3

### 071. `gqd-3c56160ea89cf9fbbd1b8853`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6d2512b1-4b41-40a1-b681-f99271cdac74` — names: วัดตะปอนน้อย — country: TH — point: 12.484447, 102.166756 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cc385c638aaa093c99c0d62
- Wikidata `Q106436951` — labels: Wat Tapon Noi — point: 12.484167, 102.166944 — description/aliases: null (not in frozen input)
- P1968 claims: 4cc385c638aaa093c99c0d62 (matches Overture source)
- Distance: 0.03735 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106436951 https://foursquare.com/v/4cc385c638aaa093c99c0d62

### 072. `gqd-3e5e2acef89ba5f2d5241fb4`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c8b104b9-0581-4672-8634-d1f3f42e869e` — names: MTA Subway - 14th St (F/L/M) — country: US — point: 40.737286, -73.996994 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ae4eb45f964a5206e9f21e3
- Wikidata `Q2983355` — labels: 14th Street — point: 40.737300, -73.996800 — description/aliases: null (not in frozen input)
- P1968 claims: 4ae4eb45f964a5206e9f21e3 (matches Overture source)
- Distance: 0.016425 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2983355 https://foursquare.com/v/4ae4eb45f964a5206e9f21e3

### 073. `gqd-3f67b3de867fed7f83c9a8b6`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `626f5a3d-eabc-4214-8cb6-60b6cf67200f` — names: Oslo Hackney — country: GB — point: 51.547234, -0.055372 — categories: null (not in frozen input)
- Overture sources: Foursquare:5254b94e498eb2b298318f37
- Wikidata `Q16896934` — labels: Oslo — point: 51.547283, -0.055575 — description/aliases: null (not in frozen input)
- P1968 claims: 5254b94e498eb2b298318f37 (matches Overture source)
- Distance: 0.015066 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16896934 https://foursquare.com/v/5254b94e498eb2b298318f37

### 074. `gqd-43ede5a9d964a296d2fc9554`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `49d5f747-88fd-49c8-87a3-4ac4e57ec976` — names: Monde Sauvage — country: BE — point: 50.500656, 5.741757 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd57076637ba593f958f670
- Wikidata `Q2932104` — labels: Le Monde Sauvage — point: 50.500000, 5.741667 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd57076637ba593f958f670 (matches Overture source)
- Distance: 0.073237 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2932104 https://foursquare.com/v/4bd57076637ba593f958f670

### 075. `gqd-4579aa6adac3c853827b79e0`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `26729526-6db4-4d64-9574-ba4c4870b415` — names: วัดบางพูน — country: TH — point: 13.989898, 100.573128 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ccfeed301eaf04de68eb55d
- Wikidata `Q100594880` — labels: Wat Bang Phun — point: 13.990278, 100.572500 — description/aliases: null (not in frozen input)
- P1968 claims: 4ccfeed301eaf04de68eb55d (matches Overture source)
- Distance: 0.079834 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100594880 https://foursquare.com/v/4ccfeed301eaf04de68eb55d

### 076. `gqd-4874dfe5e51cefeb99f263e3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c9bffb33-6e03-4191-bcb3-fb97f249c2f3` — names: Museo Nazionale del Risorgimento Italiano — country: IT — point: 45.068615, 7.686105 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c138642a9c220a1a5d6559d
- Wikidata `Q1062079` — labels: National Museum of the italian Risorgimento — point: 45.069106, 7.685159 — description/aliases: null (not in frozen input)
- P1968 claims: 4c138642a9c220a1a5d6559d (matches Overture source)
- Distance: 0.092252 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1062079 https://foursquare.com/v/4c138642a9c220a1a5d6559d

### 077. `gqd-4c04ef296a07f83c0d43fe7c`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `3614e2e4-ecfe-4682-aefe-e36554622e82` — names: The Garage on Motor Ave — country: US — point: 34.027679, -118.409973 — categories: null (not in frozen input)
- Overture sources: Foursquare:50d52b4de4b047bbfb58fd52
- Wikidata `Q104536858` — labels: The Garage on Motor — point: 34.027674, -118.409942 — description/aliases: null (not in frozen input)
- P1968 claims: 50d52b4de4b047bbfb58fd52 (matches Overture source)
- Distance: 0.002917 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q104536858 https://foursquare.com/v/50d52b4de4b047bbfb58fd52

### 078. `gqd-4d418567919da44c299539f5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ac4969f4-d878-45cf-b312-fc29986e9144` — names: Le Centre Eaton de Montréal — country: CA — point: 45.503242, -73.570282 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ad8ade8f964a520a81321e3
- Wikidata `Q2944810` — labels: Montreal Eaton Centre — point: 45.503000, -73.572000 — description/aliases: null (not in frozen input)
- P1968 claims: 4ad8ade8f964a520a81321e3 (matches Overture source)
- Distance: 0.136579 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2944810 https://foursquare.com/v/4ad8ade8f964a520a81321e3

### 079. `gqd-4fbbe49313e384ba9160d364`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `859e14b0-f1a8-4a83-b21c-cf476cec66a9` — names: Hemköp — country: SE — point: 57.698776, 11.934822 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bb397c9eb3e95213a5ccb0a
- Wikidata `Q113005206` — labels: Hemköp Stigbergstorget — point: 57.698754, 11.934845 — description/aliases: null (not in frozen input)
- P1968 claims: 4bb397c9eb3e95213a5ccb0a (matches Overture source)
- Distance: 0.002824 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q113005206 https://foursquare.com/v/4bb397c9eb3e95213a5ccb0a

### 080. `gqd-53c2239f4b6c3c7131cfd07a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `4639420a-850b-43bb-8c70-e9ec896ed5e7` — names: วัดบางช้างเหนือ (Wat Bang Chang Nuea) — country: TH — point: 13.722145, 100.205925 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c010441cf3aa5933c51ccb0
- Wikidata `Q100410165` — labels: Wat Bang Chang Nuea — point: 13.722222, 100.205556 — description/aliases: null (not in frozen input)
- P1968 claims: 4c010441cf3aa5933c51ccb0 (matches Overture source)
- Distance: 0.040818 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100410165 https://foursquare.com/v/4c010441cf3aa5933c51ccb0

### 081. `gqd-57389f2f882e9f29301a901e`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `684765ce-0a0f-4b4a-8667-9c3790a629af` — names: Rocky Mountain National Park Grand Lake Entrance — country: US — point: 40.266586, -105.832596 — categories: null (not in frozen input)
- Overture sources: Foursquare:4de7c09d887710a065bef824
- Wikidata `Q105061771` — labels: Grand Lake Entrance Station — point: 40.272274, -105.834727 — description/aliases: null (not in frozen input)
- P1968 claims: 4de7c09d887710a065bef824 (matches Overture source)
- Distance: 0.657775 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105061771 https://foursquare.com/v/4de7c09d887710a065bef824

### 082. `gqd-59650e4fb43e4a903371201b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a0c16f31-f552-4f03-879c-b7f65f20a9df` — names: สถานีรถไฟวังก์พง (Wang Phong) SRT4123 — country: TH — point: 12.402354, 99.932915 — categories: null (not in frozen input)
- Overture sources: Foursquare:4f642b78e4b09ff9bc435414
- Wikidata `Q16271192` — labels: Wang Phong Railway Station — point: 12.403600, 99.932900 — description/aliases: null (not in frozen input)
- P1968 claims: 4f642b78e4b09ff9bc435414 (matches Overture source)
- Distance: 0.138532 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16271192 https://foursquare.com/v/4f642b78e4b09ff9bc435414

### 083. `gqd-5a9ceead7590f4a6c0e0b1b0`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `7023628f-bb02-44d9-8913-c1cc3bc6729c` — names: วัดสามง่าม (อรัญญิกกราม) — country: TH — point: 13.964812, 100.083595 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d5387138652224bd50bd1d7
- Wikidata `Q100375996` — labels: Wat Sam Ngam — point: 13.965556, 100.084167 — description/aliases: null (not in frozen input)
- P1968 claims: 4d5387138652224bd50bd1d7 (matches Overture source)
- Distance: 0.103114 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100375996 https://foursquare.com/v/4d5387138652224bd50bd1d7

### 084. `gqd-5d6e53ed9a29ea0f9bf24967`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2c56c9c5-269a-4476-822c-3f64fe92629c` — names: Smithsonian’s National Zoo and Conservation Biology Institute — country: US — point: 38.929634, -77.049759 — categories: null (not in frozen input)
- Overture sources: Foursquare:4a7dcf98f964a520b6ef1fe3
- Wikidata `Q1043283` — labels: National Zoological Park — point: 38.929444, -77.049722 — description/aliases: null (not in frozen input)
- P1968 claims: 4a7dcf98f964a520b6ef1fe3 (matches Overture source)
- Distance: 0.021326 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1043283 https://foursquare.com/v/4a7dcf98f964a520b6ef1fe3

### 085. `gqd-5f4ffa566834d0555c998984`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `9e1660da-fd3a-428f-b087-645a282dda81` — names: STM Station Jean-Drapeau — country: CA — point: 45.512142, -73.533134 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b0f3224f964a520396023e3
- Wikidata `Q1675164` — labels: Jean-Drapeau metro station — point: 45.512500, -73.533056 — description/aliases: null (not in frozen input)
- P1968 claims: 4b0f3224f964a520396023e3 (matches Overture source)
- Distance: 0.04026 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1675164 https://foursquare.com/v/4b0f3224f964a520396023e3

### 086. `gqd-65edae7a94b3f26b21e91fac`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `dd8120ff-d32d-465f-ae03-1c3476a64ff9` — names: 上野動物園 — country: JP — point: 35.716877, 139.770142 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba5b7e0f964a520181e39e3
- Wikidata `Q162722` — labels: Ueno Zoo — point: 35.717500, 139.771389 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba5b7e0f964a520181e39e3 (matches Overture source)
- Distance: 0.132209 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q162722 https://foursquare.com/v/4ba5b7e0f964a520181e39e3

### 087. `gqd-6706e918e357f5615e89e967`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a6f14b25-9aed-4769-8a6a-e34c834e9fd4` — names: วัดธาตุน้อย (พ่อท่านคล้ายวาจาสิทธิ์) — country: TH — point: 8.373505, 99.542999 — categories: null (not in frozen input)
- Overture sources: Foursquare:4dd5e3e752b1a5c64427a87e
- Wikidata `Q106746596` — labels: Wat That Noi — point: 8.373611, 99.542778 — description/aliases: null (not in frozen input)
- P1968 claims: 4dd5e3e752b1a5c64427a87e (matches Overture source)
- Distance: 0.02709 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106746596 https://foursquare.com/v/4dd5e3e752b1a5c64427a87e

### 088. `gqd-67a96e9e23cbadcb93c6a133`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ce32a118-c70c-44fc-ad16-53b12a08f3d0` — names: สถานีรถไฟคีรีรัฐนิคม — country: TH — point: 9.030890, 98.947250 — categories: null (not in frozen input)
- Overture sources: Foursquare:522a773f8bbde5f6e0ac5e45
- Wikidata `Q16271336` — labels: Khirirat Nikhom railway station — point: 9.031380, 98.947210 — description/aliases: null (not in frozen input)
- P1968 claims: 522a773f8bbde5f6e0ac5e45 (matches Overture source)
- Distance: 0.054614 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16271336 https://foursquare.com/v/522a773f8bbde5f6e0ac5e45

### 089. `gqd-67fa60ac9577bfd947dbd8f3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `cba75e60-30cd-410f-afc2-8f2c27dd622d` — names: คริสตจักรสะพานเหลือง (Sapan Luang Church) — country: TH — point: 13.734643, 100.523636 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c42e9bd7beab713a4f59555
- Wikidata `Q10567186` — labels: Sapan Luang Church — point: 13.734272, 100.523284 — description/aliases: null (not in frozen input)
- P1968 claims: 4c42e9bd7beab713a4f59555 (matches Overture source)
- Distance: 0.056091 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10567186 https://foursquare.com/v/4c42e9bd7beab713a4f59555

### 090. `gqd-6a19e16e9e1fa2294fa19f9d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `56539d03-6410-452d-bee3-92331a86ae2d` — names: Pirámide de Kukulcán — country: MX — point: 20.682930, -88.568733 — categories: null (not in frozen input)
- Overture sources: Foursquare:4dbaf1666e815ab0de5e9a9a
- Wikidata `Q1128327` — labels: Temple of Kukulcan — point: 20.682889, -88.568611 — description/aliases: null (not in frozen input)
- P1968 claims: 4dbaf1666e815ab0de5e9a9a (matches Overture source)
- Distance: 0.0135 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1128327 https://foursquare.com/v/4dbaf1666e815ab0de5e9a9a

### 091. `gqd-6a2d5edd1add228302437d19`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a1772c0d-6ea3-4cd5-b086-e8c31564d51c` — names: วัดสะตือ (พระพุทธไสยาสน์) — country: TH — point: 14.557905, 100.757469 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e505e0ab0fb088f3c2a1547
- Wikidata `Q107487620` — labels: Wat Satue — point: 14.560000, 100.760000 — description/aliases: null (not in frozen input)
- P1968 claims: 4e505e0ab0fb088f3c2a1547 (matches Overture source)
- Distance: 0.358396 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107487620 https://foursquare.com/v/4e505e0ab0fb088f3c2a1547

### 092. `gqd-6b22f54077d7191938f0ae26`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `cb020821-4fd5-4ece-b31c-2d4f4392fce7` — names: MAS | Museum aan de Stroom — country: BE — point: 51.228840, 4.404770 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bf5669894af2d7fc6a93b72
- Wikidata `Q1646305` — labels: Museum aan de Stroom — point: 51.228921, 4.404667 — description/aliases: null (not in frozen input)
- P1968 claims: 4bf5669894af2d7fc6a93b72 (matches Overture source)
- Distance: 0.011523 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1646305 https://foursquare.com/v/4bf5669894af2d7fc6a93b72

### 093. `gqd-6b98a8176af69164395dbcbb`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b1e07e8e-534c-4da5-b02d-174bb524aebb` — names: วัดโปรยฝน — country: TH — point: 14.114270, 100.847160 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cedc8d50acea35da82ae6ae
- Wikidata `Q100671125` — labels: Wat Proi Fon — point: 14.114444, 100.847500 — description/aliases: null (not in frozen input)
- P1968 claims: 4cedc8d50acea35da82ae6ae (matches Overture source)
- Distance: 0.041437 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100671125 https://foursquare.com/v/4cedc8d50acea35da82ae6ae

### 094. `gqd-6c7666757f48f371cd6bc786`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e4007d74-4e74-4df7-97c5-d6ffe54636a4` — names: วัดบูรพาราม (Wat Buraparam) — country: TH — point: 14.884387, 103.493340 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e22a56852b1f82ffbab1f23
- Wikidata `Q29509785` — labels: Wat Burapharam — point: 14.884458, 103.492961 — description/aliases: null (not in frozen input)
- P1968 claims: 4e22a56852b1f82ffbab1f23 (matches Overture source)
- Distance: 0.041438 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q29509785 https://foursquare.com/v/4e22a56852b1f82ffbab1f23

### 095. `gqd-6d0b2ab94cd1b89627e17519`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `79a05bad-9117-47be-9396-2d07f847e53e` — names: Hart's Turkey Farm Restaurant — country: US — point: 43.647575, -71.499840 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b9416e5f964a520546734e3
- Wikidata `Q113001832` — labels: Hart's Turkey Farm — point: 43.647707, -71.499647 — description/aliases: null (not in frozen input)
- P1968 claims: 4b9416e5f964a520546734e3 (matches Overture source)
- Distance: 0.021291 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q113001832 https://foursquare.com/v/4b9416e5f964a520546734e3

### 096. `gqd-6dc2e911e63fa9cfdc6a4cbe`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `9c30bb46-6f30-4006-ab67-51c3a25011ab` — names: วัดธาตุประสิทธิ์ — country: TH — point: 17.484079, 104.097389 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d15ae271356a093cfc8d182
- Wikidata `Q107406651` — labels: Wat  Phra That Prasit — point: 17.484334, 104.097321 — description/aliases: null (not in frozen input)
- P1968 claims: 4d15ae271356a093cfc8d182 (matches Overture source)
- Distance: 0.029229 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107406651 https://foursquare.com/v/4d15ae271356a093cfc8d182

### 097. `gqd-6f00c79043eddb6543f067e1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ed3aa947-0da0-4ed5-9528-4cc8b00d04eb` — names: The Heritage Foundation (South) — country: US — point: 38.886997, -77.002464 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c474e67e8f876b02d195f02
- Wikidata `Q105168428` — labels: The Heritage Foundation Pennsylvania Avenue Building — point: 38.886966, -77.002499 — description/aliases: null (not in frozen input)
- P1968 claims: 4c474e67e8f876b02d195f02 (matches Overture source)
- Distance: 0.004563 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105168428 https://foursquare.com/v/4c474e67e8f876b02d195f02

### 098. `gqd-6f294f3bc08664b8ac8838eb`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b939e2b9-dccc-4e11-b2fe-a42cc5a39e06` — names: วัดขนอนเหนือ (Wat Khanon Nuea) — country: TH — point: 14.289577, 100.609055 — categories: null (not in frozen input)
- Overture sources: Foursquare:4dcf3e3945ddbe15f89ec1a0
- Wikidata `Q107208888` — labels: Wat Khanon Nuea — point: 14.289167, 100.609444 — description/aliases: null (not in frozen input)
- P1968 claims: 4dcf3e3945ddbe15f89ec1a0 (matches Overture source)
- Distance: 0.062062 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107208888 https://foursquare.com/v/4dcf3e3945ddbe15f89ec1a0

### 099. `gqd-70237ce4057d89095c70b6c9`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `7ec521f5-9d74-4e04-905e-b50ab94600d0` — names: Bengans — country: SE — point: 57.699192, 11.934084 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d8df5ae1d06b1f7dc833c3b
- Wikidata `Q10428062` — labels: Q10428062 — point: 57.699194, 11.934083 — description/aliases: null (not in frozen input)
- P1968 claims: 4d8df5ae1d06b1f7dc833c3b (matches Overture source)
- Distance: 0.000269 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10428062 https://foursquare.com/v/4d8df5ae1d06b1f7dc833c3b

### 100. `gqd-70741c85961ed0b6817faa5c`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `11b6557d-2137-4738-8bb0-b46216ff08dd` — names: Richmond National Battlefield Park — country: US — point: 37.526840, -77.412231 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ad4f29af964a520daff20e3
- Wikidata `Q100324858` — labels: Chimborazo Medical Museum — point: 37.526838, -77.412200 — description/aliases: null (not in frozen input)
- P1968 claims: 4ad4f29af964a520daff20e3 (matches Overture source)
- Distance: 0.002784 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100324858 https://foursquare.com/v/4ad4f29af964a520daff20e3

### 101. `gqd-70de02813a2d523f087b85b5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b9520299-0c73-4aa7-9ddc-6959952e68ef` — names: Japas Tapas and Oyster Bar — country: CA — point: 43.663887, -79.417389 — categories: null (not in frozen input)
- Overture sources: Foursquare:5093328ee4b02645de37f610
- Wikidata `Q105877626` — labels: Japas Oyster Bar — point: 43.663902, -79.417360 — description/aliases: null (not in frozen input)
- P1968 claims: 5093328ee4b02645de37f610 (matches Overture source)
- Distance: 0.002845 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q105877626 https://foursquare.com/v/5093328ee4b02645de37f610

### 102. `gqd-71deec0f6b6ecce4e07440a5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d92c12bb-b7c7-4ad3-aeda-7198d5de27cd` — names: Kinos Maskinrum — country: SE — point: 55.703518, 13.192187 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ca369c0554b236a22802648
- Wikidata `Q106368526` — labels: Kino — point: 55.703467, 13.192391 — description/aliases: null (not in frozen input)
- P1968 claims: 4ca369c0554b236a22802648 (matches Overture source)
- Distance: 0.013962 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106368526 https://foursquare.com/v/4ca369c0554b236a22802648

### 103. `gqd-71e434417c9268b92d8112a5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `5b2a4032-adcb-450f-a3ec-3b859050d2e6` — names: Bibliothèque Václav Havel — country: FR — point: 48.888977, 2.363055 — categories: null (not in frozen input)
- Overture sources: Foursquare:5287a50311d2dd3b46b4fbdf
- Wikidata `Q104033931` — labels: Vaclav Havel Library — point: 48.888997, 2.363108 — description/aliases: null (not in frozen input)
- P1968 claims: 5287a50311d2dd3b46b4fbdf (matches Overture source)
- Distance: 0.004434 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q104033931 https://foursquare.com/v/5287a50311d2dd3b46b4fbdf

### 104. `gqd-73a6d1cb6ec21037255b1fb7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `fab4c484-ed6a-4e7d-937c-8003f0e07b75` — names: Le Dôme Café — country: FR — point: 48.842007, 2.328945 — categories: null (not in frozen input)
- Overture sources: Foursquare:4adcda04f964a5203f3221e3
- Wikidata `Q1025695` — labels: Le Dôme — point: 48.841900, 2.329100 — description/aliases: null (not in frozen input)
- P1968 claims: 4adcda04f964a5203f3221e3 (matches Overture source)
- Distance: 0.016393 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1025695 https://foursquare.com/v/4adcda04f964a5203f3221e3

### 105. `gqd-7433e608076d32d159647b93`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d1e4fa35-147f-42c8-a241-a9fb726835a4` — names: Centre Canadien d'Architecture — country: CA — point: 45.491070, -73.578568 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ad4c06cf964a520f7f920e3
- Wikidata `Q2944913` — labels: Canadian Centre for Architecture — point: 45.491000, -73.578556 — description/aliases: null (not in frozen input)
- P1968 claims: 4ad4c06cf964a520f7f920e3 (matches Overture source)
- Distance: 0.007816 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2944913 https://foursquare.com/v/4ad4c06cf964a520f7f920e3

### 106. `gqd-752b793194988320c880ca4a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `91939884-23e6-4918-97f6-f3bd848a4a90` — names: วัดคงคาภิมุข — country: TH — point: 8.840748, 98.362305 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cbd95f6d78f4688a54cce73
- Wikidata `Q107487699` — labels: Wat Khongkha Phimuk — point: 8.840556, 98.362778 — description/aliases: null (not in frozen input)
- P1968 claims: 4cbd95f6d78f4688a54cce73 (matches Overture source)
- Distance: 0.056206 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107487699 https://foursquare.com/v/4cbd95f6d78f4688a54cce73

### 107. `gqd-76e756e32f7f17af441a4f99`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8ef9125f-90cc-4081-aacf-9baa9b1876d7` — names: วัดบ้านพร้าวนอก — country: TH — point: 14.052876, 100.559105 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c4cdce6f5afa5934c519dd6
- Wikidata `Q100594953` — labels: Wat Ban Phrao Nok — point: 14.052222, 100.558333 — description/aliases: null (not in frozen input)
- P1968 claims: 4c4cdce6f5afa5934c519dd6 (matches Overture source)
- Distance: 0.110542 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100594953 https://foursquare.com/v/4c4cdce6f5afa5934c519dd6

### 108. `gqd-788c79ca0146433be7be2989`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `fc608e9a-261f-42b6-b66b-c87655933952` — names: วัดเจริญบุญ — country: TH — point: 14.196829, 100.823982 — categories: null (not in frozen input)
- Overture sources: Foursquare:4dfdaa8545ddebdfeaaee291
- Wikidata `Q100671062` — labels: Wat Charoen Bun — point: 14.197222, 100.824722 — description/aliases: null (not in frozen input)
- P1968 claims: 4dfdaa8545ddebdfeaaee291 (matches Overture source)
- Distance: 0.090975 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100671062 https://foursquare.com/v/4dfdaa8545ddebdfeaaee291

### 109. `gqd-7a5b736ac173e03cdd1eaae1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2b7ac08c-cf82-4fe4-817e-35ae10580162` — names: วัดทรงคนอง — country: TH — point: 13.767405, 100.258247 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d84366f81fdb1f7c91af8bf
- Wikidata `Q100410158` — labels: Wat Song Khanong — point: 13.767500, 100.258333 — description/aliases: null (not in frozen input)
- P1968 claims: 4d84366f81fdb1f7c91af8bf (matches Overture source)
- Distance: 0.0141 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100410158 https://foursquare.com/v/4d84366f81fdb1f7c91af8bf

### 110. `gqd-7d5f68e8c47d3c21f6b91b69`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `3629d391-4a2c-4e3d-b43d-6dea282597c2` — names: วัดพุทธธรรมรังษี — country: TH — point: 13.862007, 100.265533 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d044b878620224bd168a840
- Wikidata `Q100410107` — labels: Wat Phutthatham Rangsi — point: 13.861111, 100.265833 — description/aliases: null (not in frozen input)
- P1968 claims: 4d044b878620224bd168a840 (matches Overture source)
- Distance: 0.104762 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100410107 https://foursquare.com/v/4d044b878620224bd168a840

### 111. `gqd-7f8d93ffd35ca4dc04323266`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `49fe5b12-0875-48b1-adef-9845ffd49dbd` — names: United States Mint — country: US — point: 39.739330, -104.991661 — categories: null (not in frozen input)
- Overture sources: Foursquare:4aa5d157f964a5207f4920e3
- Wikidata `Q2956452` — labels: Denver Mint — point: 39.739444, -104.992500 — description/aliases: null (not in frozen input)
- P1968 claims: 4aa5d157f964a5207f4920e3 (matches Overture source)
- Distance: 0.072847 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q2956452 https://foursquare.com/v/4aa5d157f964a5207f4920e3

### 112. `gqd-82f20a85c0b01285082176b2`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e8a77272-85f2-4461-9604-a29c2f4acf2e` — names: Crackhouse Comedy Club — country: MY — point: 3.154763, 101.623169 — categories: null (not in frozen input)
- Overture sources: Foursquare:53624277498eb1dd2192e04d
- Wikidata `Q113011685` — labels: Crackhouse Comedy Club KL — point: 3.154889, 101.622956 — description/aliases: null (not in frozen input)
- P1968 claims: 53624277498eb1dd2192e04d (matches Overture source)
- Distance: 0.027511 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q113011685 https://foursquare.com/v/53624277498eb1dd2192e04d

### 113. `gqd-83ff7da0180edc5e572aa05d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8bb86a31-58e9-419a-b23f-bea6227eeb48` — names: วัดตะวันเรือง — country: TH — point: 14.114779, 100.687149 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d203ac7b69c6dcb1e496e95
- Wikidata `Q100670904` — labels: Wat Tawan Rueang — point: 14.114722, 100.687500 — description/aliases: null (not in frozen input)
- P1968 claims: 4d203ac7b69c6dcb1e496e95 (matches Overture source)
- Distance: 0.03836 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100670904 https://foursquare.com/v/4d203ac7b69c6dcb1e496e95

### 114. `gqd-86d930f01170d16d3c44cc90`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8c4378e1-91a0-4da4-b368-fb8eb0911c24` — names: Le Giubbe Rosse — country: IT — point: 43.771500, 11.254016 — categories: null (not in frozen input)
- Overture sources: Foursquare:4baba192f964a520e9ba3ae3
- Wikidata `Q1025547` — labels: Caffè Giubbe Rosse — point: 43.771150, 11.253975 — description/aliases: null (not in frozen input)
- P1968 claims: 4baba192f964a520e9ba3ae3 (matches Overture source)
- Distance: 0.039016 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1025547 https://foursquare.com/v/4baba192f964a520e9ba3ae3

### 115. `gqd-87a2d2d6dcf1b3151461fd87`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6680eeee-d70f-4ae0-9d8a-1845f128e742` — names: Universität Wien — country: AT — point: 48.213032, 16.360939 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b0e65f9f964a5205e5723e3
- Wikidata `Q165980` — labels: University of Vienna — point: 48.213056, 16.359722 — description/aliases: null (not in frozen input)
- P1968 claims: 4b0e65f9f964a5205e5723e3 (matches Overture source)
- Distance: 0.090199 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q165980 https://foursquare.com/v/4b0e65f9f964a5205e5723e3

### 116. `gqd-8854ca528d8b58e207cffe72`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d6c83724-6e0f-4aac-b084-6c172f252a45` — names: ที่หยุดรถไฟบางบอน (Bang Bon) SRT5009 — country: TH — point: 13.666409, 100.428810 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ec78ca19a52756c55bc9710
- Wikidata `Q16306299` — labels: Bang Bon railway halt — point: 13.666389, 100.428694 — description/aliases: null (not in frozen input)
- P1968 claims: 4ec78ca19a52756c55bc9710 (matches Overture source)
- Distance: 0.012688 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16306299 https://foursquare.com/v/4ec78ca19a52756c55bc9710

### 117. `gqd-88b912f4ab48aec45e2cb0f1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f97d9fcb-fd3c-49ee-9c5c-2e75e50156b5` — names: Cadet Opéra Hôtel — country: FR — point: 48.875439, 2.343567 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd1b0785e0cce72dfd6a184
- Wikidata `Q102249540` — labels: Hôtel Opéra Cadet — point: 48.875417, 2.343582 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd1b0785e0cce72dfd6a184 (matches Overture source)
- Distance: 0.002615 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102249540 https://foursquare.com/v/4bd1b0785e0cce72dfd6a184

### 118. `gqd-9062e97a054d6f5070f40a33`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b4a19889-4469-48bf-a83c-d6ccb21b8659` — names: Biocafé Tellus — country: SE — point: 59.301941, 18.007664 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b5c4f8ef964a520872a29e3
- Wikidata `Q10429754` — labels: Tellus — point: 59.301944, 18.007500 — description/aliases: null (not in frozen input)
- P1968 claims: 4b5c4f8ef964a520872a29e3 (matches Overture source)
- Distance: 0.009302 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10429754 https://foursquare.com/v/4b5c4f8ef964a520872a29e3

### 119. `gqd-9213f130a0714052f784c784`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `aeaf4518-413c-45ac-8a8f-943bc05ed404` — names: วัดสามัคคิยาราม(วัดเมี่ยง) — country: TH — point: 14.079310, 100.521507 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d69a0132acd6ea81ea03cc0
- Wikidata `Q100594924` — labels: Wat Samakkhiyaram — point: 14.079444, 100.521111 — description/aliases: null (not in frozen input)
- P1968 claims: 4d69a0132acd6ea81ea03cc0 (matches Overture source)
- Distance: 0.045251 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100594924 https://foursquare.com/v/4d69a0132acd6ea81ea03cc0

### 120. `gqd-9324d0f2f3b8cb3266156d43`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c84ebdfd-bb0a-4fa6-8d07-6386dbf95652` — names: สถานีรถไฟบุใหญ่ (Bu Yai) SRT3121 — country: TH — point: 14.428299, 101.006409 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e521d38d1643fe91636a3bf
- Wikidata `Q101041303` — labels: Bua Yai — point: 14.428522, 101.005899 — description/aliases: null (not in frozen input)
- P1968 claims: 4e521d38d1643fe91636a3bf (matches Overture source)
- Distance: 0.060231 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q101041303 https://foursquare.com/v/4e521d38d1643fe91636a3bf

### 121. `gqd-98982f54b594afb1caf8939d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `63b0c4f7-7200-4322-9dec-968c0f8a7b2f` — names: Yılmaz Büyükerşen Balmumu Heykeller Müzesi — country: TR — point: 39.765221, 30.521912 — categories: null (not in frozen input)
- Overture sources: Foursquare:5190e249498e01b52ce9d111
- Wikidata `Q29831487` — labels: Yılmaz Büyükerşen Wax Museum — point: 39.765139, 30.522167 — description/aliases: null (not in frozen input)
- P1968 claims: 5190e249498e01b52ce9d111 (matches Overture source)
- Distance: 0.023619 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q29831487 https://foursquare.com/v/5190e249498e01b52ce9d111

### 122. `gqd-994cadcb3bc5f99fcbdbfa68`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `7010b43a-6e9b-463e-8166-813ce5b71b57` — names: วัดบางบัว — country: TH — point: 13.853915, 100.587929 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c4f8d71371520a13d73d4bf
- Wikidata `Q100698792` — labels: Wat Bang Bua — point: 13.854306, 100.588139 — description/aliases: null (not in frozen input)
- P1968 claims: 4c4f8d71371520a13d73d4bf (matches Overture source)
- Distance: 0.048974 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100698792 https://foursquare.com/v/4c4f8d71371520a13d73d4bf

### 123. `gqd-9bbdeed2885d5bb864d34c51`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `1f22b40c-b3a1-4864-9be3-98e30ee0c7e6` — names: วัดบางน้อย — country: TH — point: 13.481102, 99.971100 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba0c736f964a520037d37e3
- Wikidata `Q106737816` — labels: Wat Bang Noi — point: 13.484444, 99.967222 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba0c736f964a520037d37e3 (matches Overture source)
- Distance: 0.560303 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106737816 https://foursquare.com/v/4ba0c736f964a520037d37e3

### 124. `gqd-9c91315f76ba5bae5162ead6`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d46418bd-0e58-499e-83ed-aff8781539f2` — names: Chessington World of Adventures Resort — country: GB — point: 51.347210, -0.319213 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ac518f8f964a52000b020e3
- Wikidata `Q1070642` — labels: Chessington World of Adventures — point: 51.348611, -0.316667 — description/aliases: null (not in frozen input)
- P1968 claims: 4ac518f8f964a52000b020e3 (matches Overture source)
- Distance: 0.235674 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1070642 https://foursquare.com/v/4ac518f8f964a52000b020e3

### 125. `gqd-9cc45e42796d0693b4d2efd1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6aa32d44-af22-4135-a207-96b2c47e0d32` — names: วัดดาวดึงษ์ (บน) — country: TH — point: 13.967012, 100.537666 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d371b8100946ea862d986ec
- Wikidata `Q100594868` — labels: Wat Daowadueng — point: 13.967778, 100.537222 — description/aliases: null (not in frozen input)
- P1968 claims: 4d371b8100946ea862d986ec (matches Overture source)
- Distance: 0.09767 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100594868 https://foursquare.com/v/4d371b8100946ea862d986ec

### 126. `gqd-9ce61e90afbc51ea111b08fd`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c6c2040a-0951-4a08-9d65-754645a19bf0` — names: Bondi Beach Skatepark — country: AU — point: -33.892193, 151.273880 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b222bc7f964a520224424e3
- Wikidata `Q106827759` — labels: Bondi Skatepark — point: -33.892248, 151.272089 — description/aliases: null (not in frozen input)
- P1968 claims: 4b222bc7f964a520224424e3 (matches Overture source)
- Distance: 0.165427 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106827759 https://foursquare.com/v/4b222bc7f964a520224424e3

### 127. `gqd-9d4ff1c03957825addb765a9`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `60bfa826-4d10-4603-8803-e630dd7836ee` — names: Montgomery College Germantown Campus Store — country: US — point: 39.186897, -77.247963 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bc45849b492d13a79daa960
- Wikidata `Q107597736` — labels: Montgomery College Germantown Campus — point: 39.186111, -77.247500 — description/aliases: null (not in frozen input)
- P1968 claims: 4bc45849b492d13a79daa960 (matches Overture source)
- Distance: 0.096093 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107597736 https://foursquare.com/v/4bc45849b492d13a79daa960

### 128. `gqd-9d7d7505ace5bf38d752987b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `86a7349b-1004-4904-b470-c6d5df1289e9` — names: コースカベイサイドストアーズ (Coaska Bayside Stores) — country: JP — point: 35.282948, 139.662399 — categories: null (not in frozen input)
- Overture sources: Foursquare:5e999bd92892610008c5dc19
- Wikidata `Q11308175` — labels: Coaska Bayside Stores — point: 35.283333, 139.662778 — description/aliases: null (not in frozen input)
- P1968 claims: 5e999bd92892610008c5dc19 (matches Overture source)
- Distance: 0.054943 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q11308175 https://foursquare.com/v/5e999bd92892610008c5dc19

### 129. `gqd-9f09f6caae5b0e89dacd736a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f2b7d737-168d-4563-9aa0-eb434ebfeb94` — names: Koninklijke Serres / Serres Royales — country: BE — point: 50.889164, 4.358109 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd6a13b6f649521b32271ec
- Wikidata `Q1644661` — labels: Royal Greenhouses of Laeken — point: 50.888333, 4.360278 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd6a13b6f649521b32271ec (matches Overture source)
- Distance: 0.177976 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1644661 https://foursquare.com/v/4bd6a13b6f649521b32271ec

### 130. `gqd-9f616d3bc3606d7a5a685970`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `03d41212-365c-442c-ab0c-cd533fab4bd9` — names: Hürth Park — country: DE — point: 50.879848, 6.872886 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bbb59b8cf2fc9b61a76a202
- Wikidata `Q1652448` — labels: Q1652448 — point: 50.879100, 6.874480 — description/aliases: null (not in frozen input)
- P1968 claims: 4bbb59b8cf2fc9b61a76a202 (matches Overture source)
- Distance: 0.139394 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1652448 https://foursquare.com/v/4bbb59b8cf2fc9b61a76a202

### 131. `gqd-a0382b22cadcaa8ef6ca8ec5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `0145319d-84f6-430e-9291-f2837094d0fd` — names: La Fortaleza: Palacio de Santa Catalina — country: PR — point: 18.464354, -66.118683 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cded115f8cdb1f7838d8d12
- Wikidata `Q1638733` — labels: La Fortaleza — point: 18.464106, -66.119232 — description/aliases: null (not in frozen input)
- P1968 claims: 4cded115f8cdb1f7838d8d12 (matches Overture source)
- Distance: 0.064144 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1638733 https://foursquare.com/v/4cded115f8cdb1f7838d8d12

### 132. `gqd-a297788771f64bc44461a001`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `366705da-686a-4aab-b4e9-f3cf2169e718` — names: Café Books Ltd — country: CA — point: 51.089325, -115.362450 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b96be48f964a520b4e134e3
- Wikidata `Q104987040` — labels: Café Books — point: 51.089435, -115.362453 — description/aliases: null (not in frozen input)
- P1968 claims: 4b96be48f964a520b4e134e3 (matches Overture source)
- Distance: 0.012218 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q104987040 https://foursquare.com/v/4b96be48f964a520b4e134e3

### 133. `gqd-a326c176aeaebfc2437724fa`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `93e92276-6dca-4f1f-ba9f-4ae198c1f666` — names: Barnes & Noble — country: US — point: 34.018291, -118.499016 — categories: null (not in frozen input)
- Overture sources: Foursquare:4a7f6293f964a520ddf31fe3
- Wikidata `Q104531699` — labels: Barnes & Noble Santa Monica — point: 34.018292, -118.498778 — description/aliases: null (not in frozen input)
- P1968 claims: 4a7f6293f964a520ddf31fe3 (matches Overture source)
- Distance: 0.02192 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q104531699 https://foursquare.com/v/4a7f6293f964a520ddf31fe3

### 134. `gqd-a36042808f36df71a33d67e8`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `70190a9a-5d77-4759-8f89-d4d9f768cddf` — names: Tierpark EuregioZoo — country: DE — point: 50.763870, 6.116232 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd9590ccc5b9521494bf24f
- Wikidata `Q300758` — labels: Aachener Tierpark Euregiozoo — point: 50.763110, 6.115300 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd9590ccc5b9521494bf24f (matches Overture source)
- Distance: 0.106989 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q300758 https://foursquare.com/v/4bd9590ccc5b9521494bf24f

### 135. `gqd-a69c1f3088621cd3a3b4e21a`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ce3a0896-e0d7-4d47-a4b5-539b5c18b355` — names: วัดสีกุก — country: TH — point: 14.327772, 100.450623 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d15c0d5bb488cfa67b2a1d4
- Wikidata `Q106937753` — labels: Wat Sikuk — point: 14.327761, 100.450363 — description/aliases: null (not in frozen input)
- P1968 claims: 4d15c0d5bb488cfa67b2a1d4 (matches Overture source)
- Distance: 0.027979 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106937753 https://foursquare.com/v/4d15c0d5bb488cfa67b2a1d4

### 136. `gqd-a726a0c8a0900a392413d616`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `12b36a8f-840e-43b6-87f5-8620d33e07a3` — names: วัดท่าแขก — country: TH — point: 17.904888, 101.683357 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cd9f752c3f1f04dd1f08c02
- Wikidata `Q102291106` — labels: Wat Tha Khaek — point: 17.905000, 101.683333 — description/aliases: null (not in frozen input)
- P1968 claims: 4cd9f752c3f1f04dd1f08c02 (matches Overture source)
- Distance: 0.012691 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102291106 https://foursquare.com/v/4cd9f752c3f1f04dd1f08c02

### 137. `gqd-a75cb322d5d78137a842a8d8`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8f4a0df4-6651-4ac7-a659-cafe9059072b` — names: Maison des Arts et Métiers — country: FR — point: 48.818100, 2.332870 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b26b21cf964a520a87f24e3
- Wikidata `Q112967095` — labels: Maison des élèves ingénieurs des arts et métiers — point: 48.817745, 2.333171 — description/aliases: null (not in frozen input)
- P1968 claims: 4b26b21cf964a520a87f24e3 (matches Overture source)
- Distance: 0.045234 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q112967095 https://foursquare.com/v/4b26b21cf964a520a87f24e3

### 138. `gqd-a81a74dab48a1a4d7066ed96`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `039f33db-19b0-47a3-99c6-316c54e7da91` — names: Montgomery College — country: US — point: 39.098797, -77.158272 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b642a2df964a52009a22ae3
- Wikidata `Q107603831` — labels: Montgomery College Rockville Campus — point: 39.098810, -77.158265 — description/aliases: null (not in frozen input)
- P1968 claims: 4b642a2df964a52009a22ae3 (matches Overture source)
- Distance: 0.001611 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q107603831 https://foursquare.com/v/4b642a2df964a52009a22ae3

### 139. `gqd-a9601a52222d47d7ff8b540c`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `a3030e1b-f6db-46c7-b231-cf2b1961dac5` — names: Tierpark + Fossilium Bochum — country: DE — point: 51.490028, 7.228048 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c373cbfae2da5931c74fec5
- Wikidata `Q1621348` — labels: Tierpark und Fossilium Bochum — point: 51.490600, 7.227220 — description/aliases: null (not in frozen input)
- P1968 claims: 4c373cbfae2da5931c74fec5 (matches Overture source)
- Distance: 0.085609 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q1621348 https://foursquare.com/v/4c373cbfae2da5931c74fec5

### 140. `gqd-ab172dca538c0df492d73ce4`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `57f72694-def7-487d-b158-03fac341468d` — names: วัดแว่นจันทร์ — country: TH — point: 13.414861, 99.946243 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c81cdbd47cc224b0c4f809f
- Wikidata `Q106737876` — labels: Wat Waen Chan — point: 13.413611, 99.946111 — description/aliases: null (not in frozen input)
- P1968 claims: 4c81cdbd47cc224b0c4f809f (matches Overture source)
- Distance: 0.139684 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106737876 https://foursquare.com/v/4c81cdbd47cc224b0c4f809f

### 141. `gqd-ab2213c710fed0c3203924d3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `796f0292-dd23-4012-89d1-a50f2130e0bb` — names: วัดโพธิ์เฉลิมรักษ์ — country: TH — point: 13.922632, 101.028000 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e6194fdfa76cd64cde034aa
- Wikidata `Q113146386` — labels: Wat Pho Chaloem Rak — point: 13.921111, 101.028333 — description/aliases: null (not in frozen input)
- P1968 claims: 4e6194fdfa76cd64cde034aa (matches Overture source)
- Distance: 0.172926 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q113146386 https://foursquare.com/v/4e6194fdfa76cd64cde034aa

### 142. `gqd-ac1d88551d021f3fc30d47da`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b3198499-7e43-4750-917a-975c8236b8f0` — names: วัดมะขามเตี้ย — country: TH — point: 16.852522, 100.269180 — categories: null (not in frozen input)
- Overture sources: Foursquare:4cc217e7bde8f04dbc2d9c4b
- Wikidata `Q102130323` — labels: Wat Makham Tia — point: 16.852778, 100.269167 — description/aliases: null (not in frozen input)
- P1968 claims: 4cc217e7bde8f04dbc2d9c4b (matches Overture source)
- Distance: 0.02849 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102130323 https://foursquare.com/v/4cc217e7bde8f04dbc2d9c4b

### 143. `gqd-ada0746e487dcc49149fcce1`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8f607817-6b22-448b-b0d3-c7a631e5ea9a` — names: Bio Roy — country: SE — point: 57.697861, 11.978327 — categories: null (not in frozen input)
- Overture sources: Foursquare:4caf450c39458cfa3600f99f
- Wikidata `Q10657470` — labels: Roy — point: 57.697844, 11.978273 — description/aliases: null (not in frozen input)
- P1968 claims: 4caf450c39458cfa3600f99f (matches Overture source)
- Distance: 0.003698 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q10657470 https://foursquare.com/v/4caf450c39458cfa3600f99f

### 144. `gqd-ada0812d6b460918c7925a65`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ef796947-39d3-44e2-a434-94fac20b0a91` — names: วัดเพชรพลี — country: TH — point: 13.100471, 99.954979 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d844a3799b78cfa59afb41f
- Wikidata `Q102183626` — labels: Wat Phet Phli — point: 13.101111, 99.955278 — description/aliases: null (not in frozen input)
- P1968 claims: 4d844a3799b78cfa59afb41f (matches Overture source)
- Distance: 0.078139 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102183626 https://foursquare.com/v/4d844a3799b78cfa59afb41f

### 145. `gqd-af90295719d19d1c9f825902`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c6ba8483-f569-4574-a968-fc2c504a15bd` — names: Hunt Farm Visitor Information Center — country: US — point: 41.200893, -81.571983 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bf94948d4cdb71351ed84fe
- Wikidata `Q100492429` — labels: Hunt House Visitor Information Center — point: 41.200872, -81.571992 — description/aliases: null (not in frozen input)
- P1968 claims: 4bf94948d4cdb71351ed84fe (matches Overture source)
- Distance: 0.002492 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100492429 https://foursquare.com/v/4bf94948d4cdb71351ed84fe

### 146. `gqd-afadbbb680567145fd91eb29`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6ab1df8b-7947-4aa4-95a1-b9cee5ec8929` — names: Göztepe Metro Istasyonu — country: TR — point: 38.395794, 27.094128 — categories: null (not in frozen input)
- Overture sources: Foursquare:53303217498ed3bab8170c4a
- Wikidata `Q16640910` — labels: Göztepe — point: 38.395986, 27.094398 — description/aliases: null (not in frozen input)
- P1968 claims: 53303217498ed3bab8170c4a (matches Overture source)
- Distance: 0.031782 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q16640910 https://foursquare.com/v/53303217498ed3bab8170c4a

### 147. `gqd-b27c671d62c70ac714d128c0`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f7074bd5-f9fc-4f2c-87af-d827dbdd65c0` — names: วัดศรีคุณเมือง (Wat Sri Khun Muang) — country: TH — point: 17.893871, 101.652260 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d8c61c9ac798cfa301b2ae4
- Wikidata `Q102346526` — labels: Wat Si Khun Mueang — point: 17.893333, 101.652778 — description/aliases: null (not in frozen input)
- P1968 claims: 4d8c61c9ac798cfa301b2ae4 (matches Overture source)
- Distance: 0.081132 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q102346526 https://foursquare.com/v/4d8c61c9ac798cfa301b2ae4

### 148. `gqd-b507e2bc89269d7dbab90b0b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e74d7d40-5707-44ed-af9c-199485cf47a9` — names: วัดลานบุญ — country: TH — point: 13.724238, 100.719559 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c40fe69d691c9b6b5208c0a
- Wikidata `Q100698831` — labels: Wat Lan Bun — point: 13.724722, 100.719722 — description/aliases: null (not in frozen input)
- P1968 claims: 4c40fe69d691c9b6b5208c0a (matches Overture source)
- Distance: 0.056624 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100698831 https://foursquare.com/v/4c40fe69d691c9b6b5208c0a

### 149. `gqd-b558006248fefec12e814435`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b1b6248b-f435-4e3a-9149-e8613ec60d60` — names: วัดพุทธาธิวาส (Buddhadhiwat Temple) — country: TH — point: 5.772491, 101.077492 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e6b14a2483bf2d9e683983d
- Wikidata `Q106020386` — labels: Wat Phuthathiwat — point: 5.772778, 101.078333 — description/aliases: null (not in frozen input)
- P1968 claims: 4e6b14a2483bf2d9e683983d (matches Overture source)
- Distance: 0.098432 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q106020386 https://foursquare.com/v/4e6b14a2483bf2d9e683983d

### 150. `gqd-b6bca60e76e1efb474447160`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `b72667ac-1199-43cb-87c7-6a251242e258` — names: วัดป่าภูริทัตตปฏิปทาราม — country: TH — point: 14.090021, 100.507149 — categories: null (not in frozen input)
- Overture sources: Foursquare:4baca8d8f964a52084013be3
- Wikidata `Q100594926` — labels: Wat Pa Phurithattapatipatharam — point: 14.090833, 100.506944 — description/aliases: null (not in frozen input)
- P1968 claims: 4baca8d8f964a52084013be3 (matches Overture source)
- Distance: 0.092962 km (gate 1.000 km)
- Shared normalized names: none
- Risk flags:
  - `no_normalized_name_overlap` — The Overture names and the Wikidata label share no normalized token string, so this acceptance rests entirely on the direct identifier.
- Links: https://www.wikidata.org/wiki/Q100594926 https://foursquare.com/v/4baca8d8f964a52084013be3


## Clean direct control (50)

Unambiguous direct identifier, coordinates inside the gate, and at least one shared normalized name. These are controls: they should be accepts. A reject here is a false accept and fails the Phase 0 gate.

### 151. `gqd-08c3cd1a1bf01eeb4ad35c55`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `98786f5b-bd4b-4499-8041-541e0a5b0861` — names: Hornbake Library — country: US — point: 38.988190, -76.941322 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba42778f964a5201c8738e3
- Wikidata `Q107526636` — labels: Hornbake Library — point: 38.988333, -76.941389 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba42778f964a5201c8738e3 (matches Overture source)
- Distance: 0.016976 km (gate 1.000 km)
- Shared normalized names: hornbake library
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q107526636 https://foursquare.com/v/4ba42778f964a5201c8738e3

### 152. `gqd-09ae1ee38bd772e240892a4f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `793d91e7-e4ba-4875-a12d-969f78602710` — names: Maison des Provinces de France — country: FR — point: 48.821346, 2.331809 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bbccb0d593fef3b8ea10256
- Wikidata `Q112966684` — labels: Maison des Provinces de France — point: 48.821550, 2.331909 — description/aliases: null (not in frozen input)
- P1968 claims: 4bbccb0d593fef3b8ea10256 (matches Overture source)
- Distance: 0.023755 km (gate 1.000 km)
- Shared normalized names: maison des provinces de france
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q112966684 https://foursquare.com/v/4bbccb0d593fef3b8ea10256

### 153. `gqd-0a0957d331e256399a34f9d7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `559bb104-a0dd-4cfd-8bf8-e204f44d6454` — names: XFINITY Center — country: US — point: 38.995708, -76.941780 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b02e9c7f964a520f34a22e3
- Wikidata `Q2985114` — labels: Xfinity Center — point: 38.995417, -76.941556 — description/aliases: null (not in frozen input)
- P1968 claims: 4b02e9c7f964a520f34a22e3 (matches Overture source)
- Distance: 0.037806 km (gate 1.000 km)
- Shared normalized names: xfinity center
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2985114 https://foursquare.com/v/4b02e9c7f964a520f34a22e3

### 154. `gqd-0bd280cd328d9ffcd12b61db`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2eb7ff0a-d594-4738-8e05-31687fa2a9f4` — names: Hofstra University — country: US — point: 40.716724, -73.599411 — categories: null (not in frozen input)
- Overture sources: Foursquare:4a9e6643f964a520273a20e3
- Wikidata `Q1623314` — labels: Hofstra University — point: 40.714606, -73.600458 — description/aliases: null (not in frozen input)
- P1968 claims: 4a9e6643f964a520273a20e3 (matches Overture source)
- Distance: 0.251597 km (gate 1.000 km)
- Shared normalized names: hofstra university
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q1623314 https://foursquare.com/v/4a9e6643f964a520273a20e3

### 155. `gqd-0bd717b8ff68276eae075ec7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e69c33d4-21ff-44a3-afaf-07b8bdf22ed6` — names: Acrisure Stadium — country: US — point: 40.446766, -80.015762 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b155069f964a52086b023e3
- Wikidata `Q1067148` — labels: Acrisure Stadium — point: 40.446667, -80.015833 — description/aliases: null (not in frozen input)
- P1968 claims: 4b155069f964a52086b023e3 (matches Overture source)
- Distance: 0.012564 km (gate 1.000 km)
- Shared normalized names: acrisure stadium
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q1067148 https://foursquare.com/v/4b155069f964a52086b023e3

### 156. `gqd-109f0d0b9eda4a992dd53564`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6638c39c-1efc-41c2-86a9-7d01ad24592b` — names: Disney's Polynesian Village Resort — country: US — point: 28.405273, -81.585220 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b09a33ef964a520c41a23e3
- Wikidata `Q2919757` — labels: Disney's Polynesian Village Resort — point: 28.405370, -81.585400 — description/aliases: null (not in frozen input)
- P1968 claims: 4b09a33ef964a520c41a23e3 (matches Overture source)
- Distance: 0.020593 km (gate 1.000 km)
- Shared normalized names: disney s polynesian village resort
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2919757 https://foursquare.com/v/4b09a33ef964a520c41a23e3

### 157. `gqd-11f887c920a76839f3d3e789`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2655a7d2-f10c-41ab-b0a0-2474b9d92447` — names: Palazzo Cavour — country: IT — point: 45.065266, 7.682751 — categories: null (not in frozen input)
- Overture sources: Foursquare:4df360b98130cf14cc1651fa
- Wikidata `Q1054091` — labels: Palazzo Cavour — point: 45.065330, 7.682580 — description/aliases: null (not in frozen input)
- P1968 claims: 4df360b98130cf14cc1651fa (matches Overture source)
- Distance: 0.015196 km (gate 1.000 km)
- Shared normalized names: palazzo cavour
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q1054091 https://foursquare.com/v/4df360b98130cf14cc1651fa

### 158. `gqd-12bcb68e35e07de6a27e86b4`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `178385ff-fe50-4ce5-928f-dea11ac8f990` — names: Casino Apache — country: US — point: 33.297894, -105.660263 — categories: null (not in frozen input)
- Overture sources: Foursquare:5dd43076d9a1dc000850d5a4
- Wikidata `Q103316510` — labels: Casino Apache — point: 33.298096, -105.660451 — description/aliases: null (not in frozen input)
- P1968 claims: 5dd43076d9a1dc000850d5a4 (matches Overture source)
- Distance: 0.028477 km (gate 1.000 km)
- Shared normalized names: casino apache
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q103316510 https://foursquare.com/v/5dd43076d9a1dc000850d5a4

### 159. `gqd-13ac7a95452a44ea87218b98`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `3c6afe28-61ff-4edb-a921-6f0ec5f6b26c` — names: Prospect Park Zoo — country: US — point: 40.665665, -73.964478 — categories: null (not in frozen input)
- Overture sources: Foursquare:49d8307ef964a520b15d1fe3
- Wikidata `Q1058899` — labels: Prospect Park Zoo — point: 40.665772, -73.964361 — description/aliases: null (not in frozen input)
- P1968 claims: 49d8307ef964a520b15d1fe3 (matches Overture source)
- Distance: 0.015474 km (gate 1.000 km)
- Shared normalized names: prospect park zoo
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q1058899 https://foursquare.com/v/49d8307ef964a520b15d1fe3

### 160. `gqd-14822b3d25d6d5d8bd424b6d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `dbb2e87e-b7c8-4f71-8b53-36ebf2e356dc` — names: BAKA Stenugnsbageri — country: SE — point: 57.699257, 11.909661 — categories: null (not in frozen input)
- Overture sources: Foursquare:50fab6dbe4b020b558f07576
- Wikidata `Q113238541` — labels: Baka Stenugnsbageri — point: 57.699309, 11.909566 — description/aliases: null (not in frozen input)
- P1968 claims: 50fab6dbe4b020b558f07576 (matches Overture source)
- Distance: 0.008101 km (gate 1.000 km)
- Shared normalized names: baka stenugnsbageri
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q113238541 https://foursquare.com/v/50fab6dbe4b020b558f07576

### 161. `gqd-14b2de40cbf63ea80e2e4ed8`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `fd4d2fd1-07a5-4670-afb0-8a6eb270ecb2` — names: Bibliothèque Gabrielle-Roy — country: CA — point: 46.814217, -71.225899 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b3b5929f964a520c27225e3
- Wikidata `Q2901285` — labels: Bibliothèque Gabrielle-Roy — point: 46.814100, -71.225100 — description/aliases: null (not in frozen input)
- P1968 claims: 4b3b5929f964a520c27225e3 (matches Overture source)
- Distance: 0.062151 km (gate 1.000 km)
- Shared normalized names: bibliotheque gabrielle roy
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2901285 https://foursquare.com/v/4b3b5929f964a520c27225e3

### 162. `gqd-16de9072d34db808b784281c`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `da531fe9-e84c-4321-8217-bf7a765a5933` — names: Conrad Bali — country: ID — point: -8.781082, 115.224800 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c6609f219f3c9b6451e9fff
- Wikidata `Q29981099` — labels: Conrad Bali — point: -8.780979, 115.225009 — description/aliases: null (not in frozen input)
- P1968 claims: 4c6609f219f3c9b6451e9fff (matches Overture source)
- Distance: 0.025656 km (gate 1.000 km)
- Shared normalized names: conrad bali
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q29981099 https://foursquare.com/v/4c6609f219f3c9b6451e9fff

### 163. `gqd-199b0a4df85d79f462ee7f8b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `abd991ad-59d3-402e-9c28-5b1d1ad682f8` — names: Museo Egizio — country: IT — point: 45.068443, 7.684414 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b519c6bf964a520be5027e3
- Wikidata `Q19877` — labels: Museo Egizio — point: 45.068333, 7.684444 — description/aliases: null (not in frozen input)
- P1968 claims: 4b519c6bf964a520be5027e3 (matches Overture source)
- Distance: 0.012453 km (gate 1.000 km)
- Shared normalized names: museo egizio
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q19877 https://foursquare.com/v/4b519c6bf964a520be5027e3

### 164. `gqd-1b0451755dbaf3896bc96c9d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `2cb6f872-f1b8-42b6-9cf4-c6c0377db86c` — names: DoubleTree by Hilton London Heathrow Airport — country: GB — point: 51.479980, -0.411196 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ba25fedf964a52099f237e3
- Wikidata `Q30021879` — labels: DoubleTree by Hilton London Heathrow Airport — point: 51.480093, -0.411325 — description/aliases: null (not in frozen input)
- P1968 claims: 4ba25fedf964a52099f237e3 (matches Overture source)
- Distance: 0.015357 km (gate 1.000 km)
- Shared normalized names: doubletree by hilton london heathrow airport
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q30021879 https://foursquare.com/v/4ba25fedf964a52099f237e3

### 165. `gqd-1ce151f1e0d2f7f0029578ce`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ef337001-0a8f-41a0-85ad-cce30b70a47c` — names: La Boule Noire — country: FR — point: 48.882313, 2.340305 — categories: null (not in frozen input)
- Overture sources: Foursquare:4adcda17f964a520413821e3
- Wikidata `Q2921716` — labels: La Boule Noire — point: 48.882349, 2.340321 — description/aliases: null (not in frozen input)
- P1968 claims: 4adcda17f964a520413821e3 (matches Overture source)
- Distance: 0.004203 km (gate 1.000 km)
- Shared normalized names: la boule noire
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2921716 https://foursquare.com/v/4adcda17f964a520413821e3

### 166. `gqd-1cfe2a20bf2a1951ad9cd62b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6f5c408a-37a7-453a-8682-e3f477487fd0` — names: Galerie de Botanique — country: FR — point: 48.842247, 2.359596 — categories: null (not in frozen input)
- Overture sources: Foursquare:525dba2011d2c6cc6c4fda9a
- Wikidata `Q16638401` — labels: Galerie de Botanique — point: 48.842400, 2.360000 — description/aliases: null (not in frozen input)
- P1968 claims: 525dba2011d2c6cc6c4fda9a (matches Overture source)
- Distance: 0.034079 km (gate 1.000 km)
- Shared normalized names: galerie de botanique
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q16638401 https://foursquare.com/v/525dba2011d2c6cc6c4fda9a

### 167. `gqd-1d0d13b49753d3c754b4c28e`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `0c1d4d33-e2bc-4ab1-a8a3-62dc6c313f31` — names: Teatro Castro Alves — country: BR — point: -12.989150, -38.519753 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b9c32e9f964a5200a5436e3
- Wikidata `Q10378817` — labels: Teatro Castro Alves — point: -12.989300, -38.519500 — description/aliases: null (not in frozen input)
- P1968 claims: 4b9c32e9f964a5200a5436e3 (matches Overture source)
- Distance: 0.032039 km (gate 1.000 km)
- Shared normalized names: teatro castro alves
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q10378817 https://foursquare.com/v/4b9c32e9f964a5200a5436e3

### 168. `gqd-1d237c81a60ed39dab87783d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `92f4d6df-95dd-4ce2-9a73-27fe1e6fda35` — names: Twinbrook station — country: US — point: 39.062386, -77.120781 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bd815770b779c74b74f06a0
- Wikidata `Q2917662` — labels: Twinbrook Station — point: 39.062389, -77.120778 — description/aliases: null (not in frozen input)
- P1968 claims: 4bd815770b779c74b74f06a0 (matches Overture source)
- Distance: 0.00046 km (gate 1.000 km)
- Shared normalized names: twinbrook station
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2917662 https://foursquare.com/v/4bd815770b779c74b74f06a0

### 169. `gqd-1d3540a00b493c691ba18254`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `4fcfff6b-1d01-4829-9754-9e56544698a6` — names: Gymnase Marcel Cerdan — country: FR — point: 48.825794, 2.375984 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d96ec82fb0fcbff41a980eb
- Wikidata `Q112756106` — labels: Gymnase Marcel-Cerdan — point: 48.825610, 2.376260 — description/aliases: null (not in frozen input)
- P1968 claims: 4d96ec82fb0fcbff41a980eb (matches Overture source)
- Distance: 0.028787 km (gate 1.000 km)
- Shared normalized names: gymnase marcel cerdan
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q112756106 https://foursquare.com/v/4d96ec82fb0fcbff41a980eb

### 170. `gqd-1e24672274255a65c2f992ec`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ac4a00c5-deb8-4a34-b7ad-22c37db9498d` — names: Old Town Shops — country: US — point: 35.199005, -111.648003 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bb3e58b0cbcef3bdf70582a
- Wikidata `Q104525863` — labels: Old Town Shops — point: 35.199085, -111.648078 — description/aliases: null (not in frozen input)
- P1968 claims: 4bb3e58b0cbcef3bdf70582a (matches Overture source)
- Distance: 0.011224 km (gate 1.000 km)
- Shared normalized names: old town shops
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q104525863 https://foursquare.com/v/4bb3e58b0cbcef3bdf70582a

### 171. `gqd-21ce44531c274a0f1b2fb0f2`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `d87c5223-ad24-432e-92ea-c11b9b044bb1` — names: Wilhelminapark Wormerveer — country: NL — point: 52.488182, 4.800889 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b632fb9f964a5201a692ae3
- Wikidata `Q107113679` — labels: Wilhelminapark, Wormerveer — point: 52.488100, 4.800840 — description/aliases: null (not in frozen input)
- P1968 claims: 4b632fb9f964a5201a692ae3 (matches Overture source)
- Distance: 0.009699 km (gate 1.000 km)
- Shared normalized names: wilhelminapark wormerveer
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q107113679 https://foursquare.com/v/4b632fb9f964a5201a692ae3

### 172. `gqd-24bee68cef38036fe35457b7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `92f3cdcd-d250-4d1b-bfd4-213adabe7674` — names: Fundação Casa de Jorge Amado — country: BR — point: -12.971640, -38.508457 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bba293298c7ef3be9c63202
- Wikidata `Q10286271` — labels: Fundação Casa de Jorge Amado — point: -12.971678, -38.508535 — description/aliases: null (not in frozen input)
- P1968 claims: 4bba293298c7ef3be9c63202 (matches Overture source)
- Distance: 0.009416 km (gate 1.000 km)
- Shared normalized names: fundacao casa de jorge amado
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q10286271 https://foursquare.com/v/4bba293298c7ef3be9c63202

### 173. `gqd-25f7879299cd9bace6a46285`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8f14462b-04d6-417e-bfe6-01cd7dc6459f` — names: Södran — country: SE — point: 55.696316, 13.188700 — categories: null (not in frozen input)
- Overture sources: Foursquare:50796ccee4b01b417a071211
- Wikidata `Q106373266` — labels: Södran — point: 55.696212, 13.188486 — description/aliases: null (not in frozen input)
- P1968 claims: 50796ccee4b01b417a071211 (matches Overture source)
- Distance: 0.017678 km (gate 1.000 km)
- Shared normalized names: sodran
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q106373266 https://foursquare.com/v/50796ccee4b01b417a071211

### 174. `gqd-29f7911137e8e01400400b47`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `cdd9c2ce-dfb6-4be2-a501-a1d0a5e37c51` — names: Museum Speelklok — country: NL — point: 52.090816, 5.119555 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b20bc20f964a520503424e3
- Wikidata `Q1624224` — labels: Museum Speelklok — point: 52.090292, 5.118899 — description/aliases: null (not in frozen input)
- P1968 claims: 4b20bc20f964a520503424e3 (matches Overture source)
- Distance: 0.073553 km (gate 1.000 km)
- Shared normalized names: museum speelklok
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q1624224 https://foursquare.com/v/4b20bc20f964a520503424e3

### 175. `gqd-2ba9bdd3b8fff3daa2166be6`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `dda2cad6-8cbf-4758-88f2-ca5055da2337` — names: Monterey Sports Center — country: US — point: 36.599785, -121.891624 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b101212f964a520746823e3
- Wikidata `Q101557492` — labels: Monterey Sports Center — point: 36.600012, -121.891428 — description/aliases: null (not in frozen input)
- P1968 claims: 4b101212f964a520746823e3 (matches Overture source)
- Distance: 0.030749 km (gate 1.000 km)
- Shared normalized names: monterey sports center
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q101557492 https://foursquare.com/v/4b101212f964a520746823e3

### 176. `gqd-2be08ef99c20804a57967881`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `99868302-37ec-46d5-9de2-6d058b18382d` — names: Foyer Jean Bosco — country: FR — point: 48.841564, 2.256672 — categories: null (not in frozen input)
- Overture sources: Foursquare:577c1e87498e29850f161f4f
- Wikidata `Q107741644` — labels: Foyer Jean-Bosco — point: 48.841599, 2.256851 — description/aliases: null (not in frozen input)
- P1968 claims: 577c1e87498e29850f161f4f (matches Overture source)
- Distance: 0.013719 km (gate 1.000 km)
- Shared normalized names: foyer jean bosco
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q107741644 https://foursquare.com/v/577c1e87498e29850f161f4f

### 177. `gqd-3300268668f8cb6760ca620f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `89268cd3-e9df-424a-8456-0a68db656e2c` — names: Teatro Sesc Casa do Comércio — country: BR — point: -12.980231, -38.456573 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d9255a7b327370432028025
- Wikidata `Q105939350` — labels: Teatro SESC Casa do Comercio — point: -12.980143, -38.456514 — description/aliases: null (not in frozen input)
- P1968 claims: 4d9255a7b327370432028025 (matches Overture source)
- Distance: 0.011745 km (gate 1.000 km)
- Shared normalized names: teatro sesc casa do comercio
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q105939350 https://foursquare.com/v/4d9255a7b327370432028025

### 178. `gqd-33ac83e3c0f3329ccdd1bcf7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `67c96035-3102-47bc-9979-fe0db7f1ffa6` — names: Nimitz Library — country: US — point: 38.985252, -76.486435 — categories: null (not in frozen input)
- Overture sources: Foursquare:4dd0bdc01f6eb12270b27a54
- Wikidata `Q107553518` — labels: Nimitz Library — point: 38.985000, -76.486667 — description/aliases: null (not in frozen input)
- P1968 claims: 4dd0bdc01f6eb12270b27a54 (matches Overture source)
- Distance: 0.034478 km (gate 1.000 km)
- Shared normalized names: nimitz library
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q107553518 https://foursquare.com/v/4dd0bdc01f6eb12270b27a54

### 179. `gqd-34c81759936210816f1070a7`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `4564c9bc-31e2-4b7e-8f38-c9fff8e71e1a` — names: Café des Phares — country: FR — point: 48.853554, 2.368419 — categories: null (not in frozen input)
- Overture sources: Foursquare:4adcda06f964a520d63221e3
- Wikidata `Q2932993` — labels: Café des Phares — point: 48.853600, 2.368260 — description/aliases: null (not in frozen input)
- P1968 claims: 4adcda06f964a520d63221e3 (matches Overture source)
- Distance: 0.012698 km (gate 1.000 km)
- Shared normalized names: cafe des phares
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2932993 https://foursquare.com/v/4adcda06f964a520d63221e3

### 180. `gqd-35278145e495bd9b23f38d2b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `7b477a3c-d48b-4050-8a2a-77de00d823f5` — names: Bird Park — country: US — point: 40.367596, -80.053307 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bfb078465fbc9b69ce2906c
- Wikidata `Q100433526` — labels: Bird Park — point: 40.368611, -80.055000 — description/aliases: null (not in frozen input)
- P1968 claims: 4bfb078465fbc9b69ce2906c (matches Overture source)
- Distance: 0.182569 km (gate 1.000 km)
- Shared normalized names: bird park
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q100433526 https://foursquare.com/v/4bfb078465fbc9b69ce2906c

### 181. `gqd-37bf36a7e58e0f5217a4cc46`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `285bb495-e4ce-40bb-ad8f-fb80dc71390f` — names: Espace Frans Krajcberg — country: FR — point: 48.843510, 2.321280 — categories: null (not in frozen input)
- Overture sources: Foursquare:4be99fcea9900f477a8c1540
- Wikidata `Q105594982` — labels: Espace Frans Krajcberg — point: 48.843488, 2.321487 — description/aliases: null (not in frozen input)
- P1968 claims: 4be99fcea9900f477a8c1540 (matches Overture source)
- Distance: 0.015342 km (gate 1.000 km)
- Shared normalized names: espace frans krajcberg
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q105594982 https://foursquare.com/v/4be99fcea9900f477a8c1540

### 182. `gqd-39fc6130091aef8ea503aa00`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6b7c8999-d46e-4808-ace2-4166777b045b` — names: ICA Nyströms — country: SE — point: 57.961834, 19.238529 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bde94e26198c9b6394214ff
- Wikidata `Q106638337` — labels: ICA Nyströms — point: 57.961776, 19.238563 — description/aliases: null (not in frozen input)
- P1968 claims: 4bde94e26198c9b6394214ff (matches Overture source)
- Distance: 0.006745 km (gate 1.000 km)
- Shared normalized names: ica nystroms
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q106638337 https://foursquare.com/v/4bde94e26198c9b6394214ff

### 183. `gqd-3b741dfeecf6d53ae4babe4f`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `84b592b0-27d5-4363-9c29-43a8af4c6eb9` — names: National Museum of Natural History — country: MT — point: 35.884865, 14.403554 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ebbd4784690a29fd68fd2ca
- Wikidata `Q3014352` — labels: National Museum of Natural History — point: 35.885000, 14.403700 — description/aliases: null (not in frozen input)
- P1968 claims: 4ebbd4784690a29fd68fd2ca (matches Overture source)
- Distance: 0.019977 km (gate 1.000 km)
- Shared normalized names: national museum of natural history
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q3014352 https://foursquare.com/v/4ebbd4784690a29fd68fd2ca

### 184. `gqd-3e26c6de6996aa3b1dcf955e`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `e82da0af-e7d2-4785-b4ef-5fbf654cad9b` — names: Nikola Tesla Statue — country: US — point: 37.426327, -122.141129 — categories: null (not in frozen input)
- Overture sources: Foursquare:52a38a6b11d2c9f5e999c7d2
- Wikidata `Q113031065` — labels: Nikola Tesla statue — point: 37.426111, -122.141111 — description/aliases: null (not in frozen input)
- P1968 claims: 52a38a6b11d2c9f5e999c7d2 (matches Overture source)
- Distance: 0.024028 km (gate 1.000 km)
- Shared normalized names: nikola tesla statue
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q113031065 https://foursquare.com/v/52a38a6b11d2c9f5e999c7d2

### 185. `gqd-3ea681e041f0100920c03702`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `cd5d5fdf-c768-4bc6-8ce2-e9b0f7ecd736` — names: Café de la Paix — country: FR — point: 48.870930, 2.331833 — categories: null (not in frozen input)
- Overture sources: Foursquare:4adcda06f964a520de3221e3
- Wikidata `Q2933008` — labels: Café de la Paix — point: 48.870781, 2.331672 — description/aliases: null (not in frozen input)
- P1968 claims: 4adcda06f964a520de3221e3 (matches Overture source)
- Distance: 0.020327 km (gate 1.000 km)
- Shared normalized names: cafe de la paix
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2933008 https://foursquare.com/v/4adcda06f964a520de3221e3

### 186. `gqd-44d9c78fe25f09561eee2a1d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `22745168-ef8a-48a6-8c33-9c3cba8f123f` — names: The Lodge at Pebble Beach — country: US — point: 36.568542, -121.950371 — categories: null (not in frozen input)
- Overture sources: Foursquare:4b785ad3f964a520e1c72ee3
- Wikidata `Q104806243` — labels: The Lodge at Pebble Beach — point: 36.568463, -121.950239 — description/aliases: null (not in frozen input)
- P1968 claims: 4b785ad3f964a520e1c72ee3 (matches Overture source)
- Distance: 0.014781 km (gate 1.000 km)
- Shared normalized names: the lodge at pebble beach
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q104806243 https://foursquare.com/v/4b785ad3f964a520e1c72ee3

### 187. `gqd-4648518a9aa35488c8d228e5`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `64eb8143-0806-415f-9dc6-1b994a38cf3f` — names: Bistro Napa — country: US — point: 39.488964, -119.794197 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c47bf901ddec9289e8b9e32
- Wikidata `Q107637922` — labels: Bistro Napa — point: 39.489012, -119.794452 — description/aliases: null (not in frozen input)
- P1968 claims: 4c47bf901ddec9289e8b9e32 (matches Overture source)
- Distance: 0.0225 km (gate 1.000 km)
- Shared normalized names: bistro napa
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q107637922 https://foursquare.com/v/4c47bf901ddec9289e8b9e32

### 188. `gqd-4902b168dd39a9efa029db41`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c35bfb99-d4fe-49e0-84b7-b82e5b939d93` — names: Grandemilia — country: IT — point: 44.648174, 10.854365 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bc5f5a1f360ef3b01dfda2d
- Wikidata `Q113361013` — labels: Grandemilia — point: 44.648107, 10.854059 — description/aliases: null (not in frozen input)
- P1968 claims: 4bc5f5a1f360ef3b01dfda2d (matches Overture source)
- Distance: 0.025344 km (gate 1.000 km)
- Shared normalized names: grandemilia
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q113361013 https://foursquare.com/v/4bc5f5a1f360ef3b01dfda2d

### 189. `gqd-4c01693d720f9b0719784739`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `1d0e051e-936b-4551-ac7f-5028b99cad10` — names: Nordens Ark — country: SE — point: 58.442657, 11.434548 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bf65ecb004ed13afc4b42a0
- Wikidata `Q166851` — labels: Nordens Ark — point: 58.441111, 11.435000 — description/aliases: null (not in frozen input)
- P1968 claims: 4bf65ecb004ed13afc4b42a0 (matches Overture source)
- Distance: 0.173945 km (gate 1.000 km)
- Shared normalized names: nordens ark
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q166851 https://foursquare.com/v/4bf65ecb004ed13afc4b42a0

### 190. `gqd-4c13e5e53fcd313cc6eeef6d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8887aa4a-78bf-4cc7-ae64-caf19b615893` — names: Chatham Community Park — country: US — point: 39.665260, -89.697197 — categories: null (not in frozen input)
- Overture sources: Foursquare:4be852ffd837c9b6792ea506
- Wikidata `Q113092029` — labels: Chatham Community Park — point: 39.665975, -89.700015 — description/aliases: null (not in frozen input)
- P1968 claims: 4be852ffd837c9b6792ea506 (matches Overture source)
- Distance: 0.253985 km (gate 1.000 km)
- Shared normalized names: chatham community park
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q113092029 https://foursquare.com/v/4be852ffd837c9b6792ea506

### 191. `gqd-4fb183c509e2885a9bda10a9`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `c7522dcf-4acc-4cb6-8a9d-786459157f52` — names: Forest Cafe — country: US — point: 41.583675, -124.086601 — categories: null (not in frozen input)
- Overture sources: Foursquare:4c48adfffbafc928d72fecd9
- Wikidata `Q103817694` — labels: Forest Cafe — point: 41.583648, -124.086546 — description/aliases: null (not in frozen input)
- P1968 claims: 4c48adfffbafc928d72fecd9 (matches Overture source)
- Distance: 0.005496 km (gate 1.000 km)
- Shared normalized names: forest cafe
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q103817694 https://foursquare.com/v/4c48adfffbafc928d72fecd9

### 192. `gqd-51e58371cd532757725bda1b`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `367174e8-fa52-4f73-b745-a83dd2bec4a8` — names: Château de la Muette — country: FR — point: 48.861496, 2.269327 — categories: null (not in frozen input)
- Overture sources: Foursquare:547d8e5f498e3f57702b46a1
- Wikidata `Q2971342` — labels: Château de la Muette — point: 48.861389, 2.269444 — description/aliases: null (not in frozen input)
- P1968 claims: 547d8e5f498e3f57702b46a1 (matches Overture source)
- Distance: 0.014666 km (gate 1.000 km)
- Shared normalized names: chateau de la muette
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q2971342 https://foursquare.com/v/547d8e5f498e3f57702b46a1

### 193. `gqd-51f8cfe32d1a707c501c4855`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `4e942ee8-9cdc-49e4-9bec-5eab8921da94` — names: Musikens Hus — country: SE — point: 57.697781, 11.930700 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bb606aa46d4a593b492c5c0
- Wikidata `Q105276025` — labels: Musikens Hus — point: 57.697912, 11.931056 — description/aliases: null (not in frozen input)
- P1968 claims: 4bb606aa46d4a593b492c5c0 (matches Overture source)
- Distance: 0.0257 km (gate 1.000 km)
- Shared normalized names: musikens hus
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q105276025 https://foursquare.com/v/4bb606aa46d4a593b492c5c0

### 194. `gqd-5270d87a5c35492010ac3437`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f2282c21-e6d8-4036-b423-b252058ac6c4` — names: Grand Palais Éphémère — country: FR — point: 48.853218, 2.302387 — categories: null (not in frozen input)
- Overture sources: Foursquare:604ca9032e7ea6019c8edd4b
- Wikidata `Q105200341` — labels: Grand Palais Éphémère — point: 48.854176, 2.302183 — description/aliases: null (not in frozen input)
- P1968 claims: 604ca9032e7ea6019c8edd4b (matches Overture source)
- Distance: 0.107597 km (gate 1.000 km)
- Shared normalized names: grand palais ephemere
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q105200341 https://foursquare.com/v/604ca9032e7ea6019c8edd4b

### 195. `gqd-53fa39c8c79faa42ee8dade9`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `07b55390-264f-43be-b349-89c4b99a93ba` — names: Maison du Liban — country: FR — point: 48.817287, 2.338326 — categories: null (not in frozen input)
- Overture sources: Foursquare:5659cdb6498e0edfd7cdccec
- Wikidata `Q112978658` — labels: Maison du Liban — point: 48.817344, 2.338528 — description/aliases: null (not in frozen input)
- P1968 claims: 5659cdb6498e0edfd7cdccec (matches Overture source)
- Distance: 0.016106 km (gate 1.000 km)
- Shared normalized names: maison du liban
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q112978658 https://foursquare.com/v/5659cdb6498e0edfd7cdccec

### 196. `gqd-5798f13f361c6671953a976e`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `f83e2b43-b1c9-42a8-bbf9-0198942461af` — names: Cairo Jazz Club — country: EG — point: 30.062138, 31.211906 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bae84e1f964a52018bc3be3
- Wikidata `Q104786130` — labels: Cairo Jazz Club — point: 30.062193, 31.211933 — description/aliases: null (not in frozen input)
- P1968 claims: 4bae84e1f964a52018bc3be3 (matches Overture source)
- Distance: 0.006669 km (gate 1.000 km)
- Shared normalized names: cairo jazz club
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q104786130 https://foursquare.com/v/4bae84e1f964a52018bc3be3

### 197. `gqd-585a552af52cf57536fcc70c`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `8e839ee5-9041-48c4-8a50-a95206b25e01` — names: Shopping Bela Vista — country: BR — point: -12.969954, -38.474644 — categories: null (not in frozen input)
- Overture sources: Foursquare:4e145fc1fa7622d439e96a8e
- Wikidata `Q10371129` — labels: Shopping Bela Vista — point: -12.970265, -38.476329 — description/aliases: null (not in frozen input)
- P1968 claims: 4e145fc1fa7622d439e96a8e (matches Overture source)
- Distance: 0.185896 km (gate 1.000 km)
- Shared normalized names: shopping bela vista
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q10371129 https://foursquare.com/v/4e145fc1fa7622d439e96a8e

### 198. `gqd-59286933b0dc09d48644c6ed`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `ec015286-d720-4f40-bcec-644c17cabf94` — names: Furnace Creek General Store — country: US — point: 36.456570, -116.865959 — categories: null (not in frozen input)
- Overture sources: Foursquare:4d5f2ea429ef236a94e29559
- Wikidata `Q100258886` — labels: Furnace Creek General Store — point: 36.456396, -116.865851 — description/aliases: null (not in frozen input)
- P1968 claims: 4d5f2ea429ef236a94e29559 (matches Overture source)
- Distance: 0.021599 km (gate 1.000 km)
- Shared normalized names: furnace creek general store
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q100258886 https://foursquare.com/v/4d5f2ea429ef236a94e29559

### 199. `gqd-5dbf3afad8f625633a3641c3`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `6c7a3181-1090-4574-933f-a10f2f8ec4ed` — names: Bibliotheque Mordecai-Richler — country: CA — point: 45.521744, -73.601608 — categories: null (not in frozen input)
- Overture sources: Foursquare:4ae49685f964a520fe9b21e3
- Wikidata `Q16506938` — labels: bibliothèque Mordecai-Richler — point: 45.521700, -73.601800 — description/aliases: null (not in frozen input)
- P1968 claims: 4ae49685f964a520fe9b21e3 (matches Overture source)
- Distance: 0.01571 km (gate 1.000 km)
- Shared normalized names: bibliotheque mordecai richler
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q16506938 https://foursquare.com/v/4ae49685f964a520fe9b21e3

### 200. `gqd-6134931edebbe06c3fa35b1d`

- Provisional decision: **accepted** (automatic_acceptance=true, rule `direct_source_wikidata_id.unambiguous`)
- Rule: Exactly one Wikidata QID claims this Overture place's Foursquare source record through property P1968, and that source record belongs to exactly one GERS ID. A direct external identifier is the only automatic acceptance permitted by benchmarks/gers-qid-sidecar-phase0-spec-v1.json; names and coordinates did not contribute to this provisional decision.
- Overture `12785b3a-b1fa-4380-8fa5-e5ce2567b1a9` — names: Iate Tênis Clube — country: BR — point: -19.854324, -43.975632 — categories: null (not in frozen input)
- Overture sources: Foursquare:4bcda17c937ca59309fdac92
- Wikidata `Q29470059` — labels: Iate Tênis Clube — point: -19.854170, -43.975690 — description/aliases: null (not in frozen input)
- P1968 claims: 4bcda17c937ca59309fdac92 (matches Overture source)
- Distance: 0.018212 km (gate 1.000 km)
- Shared normalized names: iate tenis clube
- Risk flags:
  - `clean_direct_control` — Unambiguous direct identifier, a distance inside the gate, and at least one shared normalized name. Included as a control on the automatic rule; a reject here is a false accept.
- Links: https://www.wikidata.org/wiki/Q29470059 https://foursquare.com/v/4bcda17c937ca59309fdac92
