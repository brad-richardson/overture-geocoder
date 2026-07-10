# Review follow-ups

Last updated: 2026-07-09

This is the remaining work from the July architecture, performance, and data
review. The implementation PRs for coordinate-based reverse routing, populated
localities, build metrics, quality benchmarks, forward administrative areas,
and worker timing have landed. Items below are intentionally still open.

## P0 — Rebuild and validate production shards

- [ ] Rebuild forward and reverse shards from a current Overture release and
  promote the new catalog. The live `2026-07-02.3` catalog predates the locality
  and forward county/local-admin changes, so the merged code alone does not put
  cities into live reverse results. Note: the same rebuild is the first to pick
  up `ROW_GROUP_SIZE = 50_000` for id-index shards (see
  `docs/plans/2026-07-02-id-pipeline-perf-improvements.md`, proposal 3), so
  expect the /id cold-read drop to show up in the same benchmark pass.
- [ ] Record the new build metrics and compare them with the previous release:
  total records, locality counts, multipart area components, largest country
  shards, and every shard over the 50 MB warning threshold.
- [ ] Specifically measure the size impact of including all counties and
  `localadmin` records in country shards. Confirm that none leak into `HEAD` and
  add a filter or tiering rule if country shards grow unacceptably.
- [ ] Validate the initial reverse-locality population threshold of 50,000
  against the full release. Check coverage and shard growth before treating the
  threshold as permanent.
- [ ] Run the reverse quality/latency benchmark against the promoted catalog,
  including city centers, city/county name collisions, borders, islands, and
  rural points. Save the resulting benchmark artifact.
- [ ] Confirm representative live results after promotion: Boston should gain a
  locality-level result while preserving its county/region/country hierarchy.

## P1 — Reverse-geocoding correctness

- [ ] Replace bbox-only containment with exact polygon containment or a
  precomputed spatial covering such as H3. Bboxes are useful candidate filters,
  but currently produce only approximate confidence and can select the wrong
  container near irregular boundaries.
- [ ] Replace country routing based on a single unambiguous collection bbox with
  a dateline-aware country index. The current safe fallback to caller IP/`HEAD`
  avoids arbitrary routing, but overlapping bboxes and antimeridian-spanning
  metadata can make coordinate routing ineffective (Tokyo is a useful
  regression case).
- [ ] Once exact containment exists, define deterministic behavior for disputed
  or overlapping boundaries and test border coordinates explicitly.

## P1 — Deployment and CI hygiene

- [ ] Add post-deploy checks directly to `deploy-rust-worker.yml`: `/health`, a
  forward query, a reverse query, and an ID lookup. Fail the deployment workflow
  when the deployed worker is not healthy instead of relying on a manual check.
- [ ] Update GitHub Actions and the workflow Node runtime away from deprecated
  Node 20 (`actions/checkout@v4`, `actions/setup-node@v4`,
  `actions/setup-python@v5`, and `node-version: 20`). Current runs pass but emit
  deprecation annotations.
- [x] Update `crates/geocoder-worker/README.md`; its TODO/API sections still say
  reverse geocoding and edge caching are unimplemented even though both exist.
  (Done 2026-07-09, review-followups-fixes.)

## P2 — Pipeline and product improvements

- [ ] Separate raw Overture download from filtering/transformation so ranking or
  schema experiments do not require downloading the source again. This TODO is
  also documented in `scripts/download_divisions_global.sql`.
- [ ] Consider incremental ID-index rebuilds only when rebuild time or R2 writes
  justify the extra correctness surface. The design and safety requirements are
  in `docs/plans/2026-07-02-future-work.md`.
- [ ] Measure demand for multilingual forward search, then choose localized
  shard families or a separate names table rather than putting every language
  into one FTS field. See `docs/plans/2026-07-02-future-work.md`.
- [ ] Improve benchmark scoring/display-name handling for local-script primary
  names. Coordinate-correct results such as Tokyo, Beijing, and Germany should
  not be counted as name failures solely because the benchmark expects an
  English exonym; see `benchmarks/2026-07-02-report.md`.
- [ ] Decide whether to ingest the Overture `places` theme (POIs/landmarks:
  "Golden Gate Bridge", "Times Square") into forward search. Nothing in the
  repo has scoped this yet — today's search is divisions-only, and landmark
  queries return zero results. It is a product-scope decision, not a patch:
  places is orders of magnitude larger than divisions (hundreds of millions of
  rows, mixed confidence), so it needs its own shard family and tiering (e.g. a
  wiki-importance-style prominence bar for a "famous landmarks" rung in HEAD)
  rather than inclusion in the existing country shards. Closest prior art in
  repo: the "Deferred / when addresses land" section of
  `docs/ranking-research.md` (structured street/address search).

## Useful existing context

- `docs/plans/2026-07-02-future-work.md` — deliberately parked architecture work
- `docs/plans/2026-07-02-id-pipeline-perf-improvements.md` — implemented,
  measured, rejected, and deferred ID-pipeline options
- `docs/ranking-research.md` — ranking design and cross-shard BM25 constraints
- `docs/overture-data-feedback-2026-07-02.md` — upstream data-quality findings
- `docs/superpowers/` — historical implementation plans/specs (2026-03), kept
  as companion history for the pipeline docs above
- `benchmarks/2026-07-02-report.md` — baseline quality and latency results
