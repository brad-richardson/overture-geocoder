-- Offline PlanetScale/PostgreSQL Places spike. Not production DDL.
BEGIN;
DROP SCHEMA IF EXISTS places_planetscale_spike CASCADE;
CREATE SCHEMA places_planetscale_spike;

CREATE TABLE places_planetscale_spike.releases (
  release_id text PRIMARY KEY,
  overture_release text NOT NULL,
  state text NOT NULL CHECK (state IN ('loading', 'ready')),
  expected_rows bigint NOT NULL CHECK (expected_rows >= 0),
  loaded_rows bigint,
  source_sha256 text NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE places_planetscale_spike.catalog (
  family text PRIMARY KEY CHECK (family = 'places'),
  active_release_id text NOT NULL REFERENCES places_planetscale_spike.releases(release_id)
);

CREATE TABLE places_planetscale_spike.places (
  release_id text NOT NULL,
  gers_id uuid NOT NULL,
  name text NOT NULL,
  brand text NOT NULL DEFAULT '',
  category text NOT NULL DEFAULT '',
  locality text NOT NULL DEFAULT '',
  region text NOT NULL DEFAULT '',
  country text NOT NULL DEFAULT '',
  lat real NOT NULL CHECK (lat BETWEEN -90 AND 90),
  lon real NOT NULL CHECK (lon BETWEEN -180 AND 180),
  confidence real NOT NULL CHECK (confidence BETWEEN 0 AND 1),
  normalized_name text GENERATED ALWAYS AS (lower(name)) STORED,
  search_document tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector('simple'::regconfig, coalesce(name, '')), 'A') ||
    setweight(to_tsvector('simple'::regconfig, coalesce(brand, '')), 'A') ||
    setweight(to_tsvector('simple'::regconfig, coalesce(category, '')), 'B') ||
    setweight(to_tsvector('simple'::regconfig,
      coalesce(locality, '') || ' ' || coalesce(region, '') || ' ' || coalesce(country, '')), 'C')
  ) STORED,
  PRIMARY KEY (release_id, gers_id)
) PARTITION BY LIST (release_id);

CREATE TABLE places_planetscale_spike.places_r_fcfc9c092562
  PARTITION OF places_planetscale_spike.places FOR VALUES IN ('fixture-2025-12-17.0');
CREATE INDEX places_r_fcfc9c092562_fts_gin
  ON places_planetscale_spike.places_r_fcfc9c092562 USING gin (search_document);
CREATE INDEX places_r_fcfc9c092562_name_prefix
  ON places_planetscale_spike.places_r_fcfc9c092562 (normalized_name text_pattern_ops);
CREATE INDEX places_r_fcfc9c092562_category
  ON places_planetscale_spike.places_r_fcfc9c092562 (category);
CREATE INDEX places_r_fcfc9c092562_context
  ON places_planetscale_spike.places_r_fcfc9c092562 (country, region, locality);

INSERT INTO places_planetscale_spike.releases
  (release_id, overture_release, state, expected_rows, source_sha256)
VALUES ('fixture-2025-12-17.0', '2025-12-17.0', 'loading', 1768,
  'c446d81d97c69cf72fff859fa42112027e01c231e08cfb5d65bedf5dcfcaa81d');
COMMIT;
