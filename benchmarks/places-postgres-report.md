# PlanetScale Postgres Places spike

**Status: model-only; PostgreSQL was not executed**

- Fixture: `exports/experiment/places-raw.parquet` (1,768 named Places)
- Release partition: `fixture-2025-12-17.0`
- Logical searchable/result payload: 139,439 bytes (78.9 B/place)
- Token occurrences / unique field-token pairs: 14,288 / 2,781
- Database execution: no --database-url supplied; PATH psql is not PostgreSQL psql

PlanetScale documents standard PostgreSQL full-text search support and currently lists `pg_trgm` as supported. This spike uses only core `tsvector`/GIN plus B-tree prefix/context indexes; typo correction remains out of scope.

## Release strategy

Each immutable data release gets a LIST partition keyed by `release_id`. A release is loaded as `loading`, row-count validated, marked `ready`, and exposed by atomically changing one catalog row. The serving tier resolves that pointer, then binds the concrete release ID in search SQL so PostgreSQL can prune other retained release partitions. Rollback is another catalog-pointer transaction.

PlanetScale Postgres branches are isolated deployments; a newly created development branch does not currently copy schema/data automatically, so this schema must be explicitly applied there. The benchmark never assumes branch merge/deploy-request behavior.

For a hosted run, use a direct connection for schema creation and bulk load, then benchmark the application path through the pooled connection separately; PlanetScale documents those as distinct connection modes.

## Fixture model

| query shape | model candidates |
|---|---:|
| name_prefix_starb | 2 |
| token_exact_warfield_hotel | 2 |
| token_prefix_golden_gate | 9 |
| category_hotel | 125 |
| sf_context_cafe_prefix | 53 |

Linear logical payload only: 78,868,213 bytes at 1M rows and 5,915,115,950 bytes at 75M rows.
Warning: not PostgreSQL storage: excludes tuple/TOAST/page overhead, generated tsvector, GIN/B-tree indexes, WAL, replicas, and release retention

## Database evidence

No PostgreSQL server was available. There are no measured plans, relation/index sizes, network latency, concurrency results, or PlanetScale cost claims in this report.
Run the command below with a disposable database URL to collect them; it drops only the dedicated `places_planetscale_spike` schema.

```sh
python3 scripts/experiment_places_postgres.py exports/experiment/places-raw.parquet \
  --database-url "$DATABASE_URL" --schema-out /tmp/places.sql --queries-out /tmp/queries.sql \
  --json-out /tmp/places-postgres.json --markdown-out /tmp/places-postgres.md
```

Expected plans below are hypotheses, not EXPLAIN output:
- `name_prefix`: B-tree range/index scan on normalized_name text_pattern_ops
- `token_exact`: GIN bitmap index scan on search_document followed by top-k ranking
- `token_prefix`: GIN bitmap index scan on search_document followed by top-k ranking
- `category`: B-tree index scan on category followed by confidence sort
- `context_token`: planner-dependent bitmap combination or one selective index plus filter across context B-tree and FTS GIN

## Interpretation

The plausible role is a remotely queried regional Places service, not a whole-loaded edge shard. PostgreSQL removes custom range-planning and publication-object fanout, but introduces a network/database dependency, retained-release index duplication, connection-pool behavior, and operational cost. A decision requires a real PlanetScale development-branch run with representative regional scale, concurrency, cold/warm latency, query plans, index bytes, and labelled ranking—not this 0.98 km² fixture model.

Official references: [Postgres compatibility](https://planetscale.com/docs/postgres/postgres-compatibility), [extensions](https://planetscale.com/docs/postgres/extensions), [branching](https://planetscale.com/docs/postgres/branching), and [connections](https://planetscale.com/docs/postgres/connecting).

