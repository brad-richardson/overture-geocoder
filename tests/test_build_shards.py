"""Tests for build_shards.py functions."""

import gzip
import sqlite3
import sys
from pathlib import Path

import pytest

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from build_shards import (
    CAPITAL_COUNTRY_BONUS,
    CAPITAL_REGION_BONUS,
    DIVISIONS_INSERT_SQL,
    FALLBACK_REGION_SUFFIX,
    SHARD_SIZE_THRESHOLD_BYTES,
    WIKI_IMPORTANCE_WEIGHT,
    build_country_shard,
    build_head_shard,
    build_search_alias,
    build_shard_schema,
    compute_importance,
    dedup_localities,
    enrich_parquet_with_wiki_importance,
    validate_country_code,
    validate_population_threshold,
    validate_region_code,
)

import duckdb


def fts_query(query: str, autocomplete: bool = True) -> str:
    """
    Replicate geocoder-core's prepare_fts_query output format: every token
    double-quoted, the last token suffixed with * when autocomplete.
    e.g. ("boston ma", True) -> '"boston" "ma"*'
    """
    tokens = [t for t in query.lower().replace(",", " ").split() if t]
    parts = [f'"{t}"' for t in tokens]
    if autocomplete and parts:
        parts[-1] += "*"
    return " ".join(parts)


def make_shard_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    build_shard_schema(db)
    return db


def insert_division(
    db: sqlite3.Connection,
    gers_id: str,
    search_name: str,
    search_alias: str | None = None,
    search_context: str | None = None,
    importance: float = 0.5,
    type_: str = "locality",
):
    db.execute(DIVISIONS_INSERT_SQL, (
        gers_id, 0, type_, gers_id, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
        None, "US", None, search_name, search_alias, search_context, importance,
    ))


WEIGHTED_BM25_QUERY = """
    SELECT d.gers_id, bm25(divisions_fts, 4.0, 2.0, 1.0) AS score
    FROM divisions_fts
    JOIN divisions d ON d.rowid = divisions_fts.rowid
    WHERE divisions_fts MATCH ?
    ORDER BY score
"""


class TestForwardExtraction:
    def test_includes_county_and_localadmin(self):
        sql = (Path(__file__).parent.parent / "scripts" / "download_divisions_global.sql").read_text()
        assert "d.subtype IN ('country', 'region', 'county', 'localadmin')" in sql


class TestValidateCountryCode:
    def test_valid_codes(self):
        assert validate_country_code("US") == "US"
        assert validate_country_code("GB") == "GB"
        assert validate_country_code("CN") == "CN"

    def test_invalid_lowercase(self):
        with pytest.raises(ValueError, match="must be 2 uppercase letters"):
            validate_country_code("us")

    def test_invalid_length(self):
        with pytest.raises(ValueError, match="must be 2 uppercase letters"):
            validate_country_code("USA")

    def test_invalid_characters(self):
        with pytest.raises(ValueError, match="must be 2 uppercase letters"):
            validate_country_code("U1")


class TestValidateRegionCode:
    def test_valid_codes(self):
        assert validate_region_code("US-MA") == "US-MA"
        assert validate_region_code("CN-GD") == "CN-GD"
        assert validate_region_code("GB-ENG") == "GB-ENG"

    def test_fallback_region(self):
        # Fallback regions like CN-XX should be valid
        assert validate_region_code(f"CN-{FALLBACK_REGION_SUFFIX}") == "CN-XX"
        assert validate_region_code(f"IN-{FALLBACK_REGION_SUFFIX}") == "IN-XX"

    def test_invalid_format(self):
        with pytest.raises(ValueError, match="Invalid region code"):
            validate_region_code("US")  # No hyphen

    def test_invalid_country_part(self):
        with pytest.raises(ValueError, match="Invalid region code"):
            validate_region_code("usa-MA")  # Lowercase


class TestValidatePopulationThreshold:
    def test_valid_thresholds(self):
        assert validate_population_threshold(0) == 0
        assert validate_population_threshold(100000) == 100000
        assert validate_population_threshold(1000000) == 1000000

    def test_invalid_negative(self):
        with pytest.raises(ValueError, match="Invalid population threshold"):
            validate_population_threshold(-1)

    def test_invalid_too_large(self):
        with pytest.raises(ValueError, match="Invalid population threshold"):
            validate_population_threshold(100_000_000_000)


class TestBuildSearchAlias:
    def test_concatenated_pairwise(self):
        alias = build_search_alias("New York City", "new york city nyc")
        tokens = alias.split()
        assert "newyork" in tokens
        assert "yorkcity" in tokens

    def test_concatenated_full(self):
        alias = build_search_alias("New York City", "new york city nyc")
        assert "newyorkcity" in alias.split()

    def test_abbreviation_saint_to_st(self):
        alias = build_search_alias("Saint Louis", "saint louis")
        assert "st" in alias.split()

    def test_abbreviation_st_to_saint(self):
        alias = build_search_alias("St Louis", "st louis")
        assert "saint" in alias.split()

    def test_abbreviation_fort_to_ft(self):
        alias = build_search_alias("Fort Worth", "fort worth")
        assert "ft" in alias.split()

    def test_abbreviation_ft_to_fort(self):
        alias = build_search_alias("Ft Worth", "ft worth")
        assert "fort" in alias.split()

    def test_abbreviation_mount_to_mt(self):
        alias = build_search_alias("Mount Vernon", "mount vernon")
        assert "mt" in alias.split()

    def test_abbreviation_directional(self):
        alias = build_search_alias("North Charleston", "north charleston")
        assert "n" in alias.split()

    def test_ampersand_to_and(self):
        alias = build_search_alias("Town & Country", "town & country")
        assert "and" in alias.split()

    def test_and_does_not_emit_ampersand(self):
        # "&" never survives unicode61 tokenization, so the reverse mapping
        # would index nothing
        alias = build_search_alias("Up and Over", "up and over") or ""
        assert "&" not in alias.split()

    def test_umlaut_folding(self):
        alias = build_search_alias("München", "münchen")
        assert "muenchen" in alias.split()

    def test_eszett_folding(self):
        alias = build_search_alias("Gießen", "gießen")
        assert "giessen" in alias.split()

    def test_apostrophe_stripped(self):
        alias = build_search_alias("Coeur d'Alene", "coeur d'alene")
        assert "dalene" in alias.split()

    def test_designation_strip_x_county(self):
        alias = build_search_alias("Cook County", "cook county")
        assert "cook" in alias.split()

    def test_designation_strip_city_of(self):
        alias = build_search_alias("City of London", "city of london")
        assert "london" in alias.split()

    def test_designation_strip_county_of(self):
        alias = build_search_alias("County of Cork", "county of cork")
        assert "cork" in alias.split()

    def test_single_word_no_alias(self):
        # Single plain word should produce nothing
        assert build_search_alias("Boston", "boston") is None

    def test_empty_input(self):
        assert build_search_alias("", "") is None

    def test_no_duplicates(self):
        alias = build_search_alias("Saint Louis", "saint louis st louis")
        # "st" already present in search_name, must not be re-emitted
        assert alias is None or alias.split().count("st") == 0

    def test_short_concat_skipped(self):
        # Pairwise concatenation of very short words (< 4 chars) should be skipped
        alias = build_search_alias("A Bc", "a bc") or ""
        assert "abc" not in alias.split()


class TestComputeImportance:
    def test_in_unit_range(self):
        cases = [
            ("country", None, 1_400_000_000, 1.0, True, False),
            ("locality", "megacity", 30_000_000, 1.0, True, False),
            ("locality", "hamlet", None, None, False, False),
            ("neighborhood", None, None, None, False, False),
            ("region", None, 50_000_000, 0.5, False, True),
        ]
        for subtype, cls, pop, wiki, cap_c, cap_r in cases:
            imp = compute_importance(subtype, cls, pop, wiki, cap_c, cap_r)
            assert 0.0 <= imp <= 1.0, (subtype, imp)

    def test_megacity_above_village(self):
        megacity = compute_importance("locality", class_="megacity")
        village = compute_importance("locality", class_="village")
        assert megacity > village

    def test_type_prior_ordering(self):
        # Photon searchPrio shape: city above county/state-like types
        country = compute_importance("country")
        region = compute_importance("region")
        city = compute_importance("locality", class_="city")
        county = compute_importance("county")
        neighborhood = compute_importance("neighborhood")
        assert country > city > region > county > neighborhood

    def test_locality_null_class_default(self):
        plain = compute_importance("locality")
        town = compute_importance("locality", class_="town")
        village = compute_importance("locality", class_="village")
        assert village < plain < town

    def test_wiki_component(self):
        without = compute_importance("locality", class_="city")
        with_wiki = compute_importance("locality", class_="city", wiki_importance=0.8)
        assert with_wiki == pytest.approx(without + WIKI_IMPORTANCE_WEIGHT * 0.8)

    def test_null_population_no_component(self):
        assert compute_importance("locality", class_="town") == \
            compute_importance("locality", class_="town", population=None)

    def test_population_component_capped(self):
        small = compute_importance("locality", class_="city", population=10_000)
        huge = compute_importance("locality", class_="city", population=50_000_000)
        assert huge > small
        # Cap: 0.22 prior + 0.2 max pop component
        assert huge == pytest.approx(0.22 + 0.2)

    def test_population_half_weight_for_coarse_types(self):
        # Same population contributes half for a county vs a locality
        loc = compute_importance("locality", class_=None, population=1_000_000)
        county = compute_importance("county", population=1_000_000)
        loc_pop = loc - compute_importance("locality", class_=None)
        county_pop = county - compute_importance("county")
        assert county_pop == pytest.approx(loc_pop / 2)

    def test_capital_bonuses(self):
        base = compute_importance("locality", class_="city")
        country_cap = compute_importance(
            "locality", class_="city", is_country_capital=True)
        region_cap = compute_importance(
            "locality", class_="city", is_region_capital=True)
        assert country_cap == pytest.approx(base + CAPITAL_COUNTRY_BONUS)
        assert region_cap == pytest.approx(base + CAPITAL_REGION_BONUS)

    def test_clamped_to_one(self):
        # 0.30 megacity + 0.5 wiki + 0.2 capped pop + 0.08 capital = 1.08 -> 1.0
        imp = compute_importance(
            "locality", class_="megacity", population=37_000_000,
            wiki_importance=1.0, is_country_capital=True)
        assert imp == 1.0


class TestShardSchema:
    def test_fts_has_three_columns_no_porter(self):
        db = make_shard_db()
        (sql,) = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'divisions_fts'"
        ).fetchone()
        assert "porter" not in sql
        assert "remove_diacritics 2" in sql
        assert "search_name" in sql
        assert "search_alias" in sql
        assert "search_context" in sql

    def test_divisions_has_new_columns_not_search_text(self):
        db = make_shard_db()
        cols = {r[1] for r in db.execute("PRAGMA table_info(divisions)")}
        assert {"search_name", "search_alias", "search_context", "importance"} <= cols
        assert "search_text" not in cols

    def test_importance_not_null(self):
        db = make_shard_db()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(DIVISIONS_INSERT_SQL, (
                "x", 0, "locality", "X", 0.0, 0.0, 0.0, 0.0, 0.0, 0.0,
                None, "US", None, "x", None, None, None,
            ))

    def test_triggers_sync_fts(self):
        db = make_shard_db()
        insert_division(db, "a", "boston", None, "us massachusetts")
        count = db.execute("SELECT COUNT(*) FROM divisions_fts").fetchone()[0]
        assert count == 1

    def test_france_does_not_match_san_francisco(self):
        # P0 regression: porter stemmed "france" -> "franc" which
        # prefix-matched "francisco". Without the stemmer the autocomplete
        # query '"france"*' must not match a doc named "San Francisco".
        db = make_shard_db()
        insert_division(db, "sf", "san francisco", None, "us-ca us california")
        assert fts_query("france") == '"france"*'
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("france"),)).fetchall()
        assert rows == []
        # Genuine prefixes still match
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("san franc"),)).fetchall()
        assert [r[0] for r in rows] == ["sf"]
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("san francisco"),)).fetchall()
        assert [r[0] for r in rows] == ["sf"]

    def test_diacritics_folded(self):
        db = make_shard_db()
        insert_division(db, "muc", "münchen")
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("munchen"),)).fetchall()
        assert [r[0] for r in rows] == ["muc"]

    def test_weighted_bm25_name_beats_context(self):
        # bm25(divisions_fts, 4.0, 2.0, 1.0): a search_name hit must outrank
        # a search_context hit for the same token.
        db = make_shard_db()
        insert_division(db, "name-hit", "york", None, "us pennsylvania")
        insert_division(db, "context-hit", "springfield", None, "york county us")
        rows = db.execute(
            WEIGHTED_BM25_QUERY, (fts_query("york", autocomplete=False),)
        ).fetchall()
        assert [r[0] for r in rows] == ["name-hit", "context-hit"]
        # bm25 is lower-is-better (negative)
        assert rows[0][1] < rows[1][1]

    def test_alias_hit_between_name_and_context(self):
        db = make_shard_db()
        insert_division(db, "alias-hit", "new york city", "newyork", "us")
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("newyork"),)).fetchall()
        assert [r[0] for r in rows] == ["alias-hit"]


def write_test_parquet(path: Path):
    """Synthetic divisions parquet matching download_divisions_global.sql output."""
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT
                gers_id, version, name, subtype, class, country, region,
                population, wikidata, is_country_capital, is_region_capital,
                CAST(lon AS DOUBLE) AS lon, CAST(lat AS DOUBLE) AS lat,
                CAST(bbox_xmin AS DOUBLE) AS bbox_xmin,
                CAST(bbox_ymin AS DOUBLE) AS bbox_ymin,
                CAST(bbox_xmax AS DOUBLE) AS bbox_xmax,
                CAST(bbox_ymax AS DOUBLE) AS bbox_ymax,
                primary_name, search_name, search_context
            FROM (VALUES
                ('nyc-001', 1, 'New York City', 'locality', 'megacity', 'US', 'US-NY',
                 8336817, 'Q60', false, false,
                 -74.0060, 40.7128, -74.2591, 40.4774, -73.7004, 40.9176,
                 'New York City, NY', 'new york city;nyc;new york',
                 'us-ny us new york united states'),
                ('sf-001', 1, 'San Francisco', 'locality', 'city', 'US', 'US-CA',
                 873965, 'Q62', false, false,
                 -122.4194, 37.7749, -122.5151, 37.7034, -122.3568, 37.8324,
                 'San Francisco, CA', 'san francisco;sf',
                 'us-ca us california united states'),
                ('tiny-001', 1, 'Tinyville', 'locality', 'town', 'US', 'US-NY',
                 15000, NULL, false, false,
                 -75.0, 43.0, -75.1, 42.9, -74.9, 43.1,
                 'Tinyville, NY', 'tinyville',
                 'us-ny us new york united states'),
                ('obscure-001', 1, 'Obscureville', 'locality', 'village', 'US', 'US-NY',
                 500, 'Q777', false, false,
                 -75.5, 43.5, -75.6, 43.4, -75.4, 43.6,
                 'Obscureville, NY', 'obscureville',
                 'us-ny us new york united states'),
                ('known-001', 1, 'Knownburg', 'locality', 'village', 'US', 'US-NY',
                 3000, 'Q888', false, false,
                 -76.0, 44.0, -76.1, 43.9, -75.9, 44.1,
                 'Knownburg, NY', 'knownburg',
                 'us-ny us new york united states'),
                ('famous-001', 1, 'Gettysburg', 'locality', 'village', 'US', 'US-PA',
                 2600, 'Q999', false, false,
                 -77.2311, 39.8309, -77.3, 39.8, -77.2, 39.9,
                 'Gettysburg, PA', 'gettysburg',
                 'us-pa us pennsylvania united states'),
                ('region-ny', 1, 'New York', 'region', NULL, 'US', 'US-NY',
                 NULL, 'Q1384', false, false,
                 -75.0, 43.0, -79.8, 40.5, -71.9, 45.0,
                 'New York', 'new york',
                 'us-ny us united states'),
                -- Administrative records are intentionally unfiltered by
                -- population and belong only to their country shard.
                ('county-cook', 1, 'Cook County', 'county', NULL, 'US', 'US-IL',
                 NULL, NULL, false, false,
                 -87.75, 41.85, -88.30, 41.45, -87.20, 42.20,
                 'Cook County, IL', 'cook county',
                 'us-il us illinois united states'),
                ('localadmin-manhattan', 1, 'Manhattan', 'localadmin', NULL, 'US', 'US-NY',
                 NULL, NULL, false, false,
                 -73.97, 40.78, -74.05, 40.68, -73.90, 40.88,
                 'Manhattan, NY', 'manhattan',
                 'us-ny us new york united states')
            ) AS t(gers_id, version, name, subtype, class, country, region,
                   population, wikidata, is_country_capital, is_region_capital,
                   lon, lat, bbox_xmin, bbox_ymin, bbox_xmax, bbox_ymax,
                   primary_name, search_name, search_context)
        ) TO '{path}' (FORMAT PARQUET);
    """)
    con.close()


def write_test_importance_file(path: Path):
    """Tiny QID-keyed importance file in Nominatim's published format."""
    with gzip.open(path, "wt") as f:
        f.write("language\ttype\ttitle\timportance\twikidata_id\n")
        f.write("en\ta\tNew_York_City\t0.91\tQ60\n")
        f.write("fr\ta\tNew_York\t0.88\tQ60\n")
        f.write("en\ta\tSan_Francisco\t0.84\tQ62\n")
        f.write("en\ta\tGettysburg\t0.80\tQ999\n")
        f.write("en\ta\tObscureville\t0.20\tQ777\n")
        f.write("en\ta\tKnownburg\t0.55\tQ888\n")


@pytest.fixture
def enriched_parquet(tmp_path):
    base = tmp_path / "divisions.parquet"
    write_test_parquet(base)
    importance = tmp_path / "wikimedia-importance.csv.gz"
    write_test_importance_file(importance)
    out = tmp_path / "divisions-wiki.parquet"
    enrich_parquet_with_wiki_importance(base, out, importance)
    return out


def write_dedup_test_parquet(path: Path):
    """Post-enrichment parquet (has wiki_importance) with duplicate clusters."""
    con = duckdb.connect()
    con.execute(f"""
        COPY (
            SELECT gers_id, name, subtype, country, region, population,
                   CAST(wiki_importance AS DOUBLE) AS wiki_importance,
                   CAST(lon AS DOUBLE) AS lon, CAST(lat AS DOUBLE) AS lat,
                   CAST(bbox_xmin AS DOUBLE) AS bbox_xmin,
                   CAST(bbox_ymin AS DOUBLE) AS bbox_ymin,
                   CAST(bbox_xmax AS DOUBLE) AS bbox_xmax,
                   CAST(bbox_ymax AS DOUBLE) AS bbox_ymax,
                   search_name
            FROM (VALUES
                -- Duplicate pair ~0.1 km apart: twin-1 leads (higher wiki
                -- importance) but twin-2 has the larger population.
                ('twin-1', 'Twinsburg', 'locality', 'US', 'US-OH', 50000, 0.6,
                 -81.0, 41.0, -81.10, 40.90, -80.90, 41.10,
                 'twinsburg;twin city'),
                ('twin-2', 'Twinsburg', 'locality', 'US', 'US-OH', 60000, NULL,
                 -81.001, 41.001, -81.20, 40.95, -80.95, 41.20,
                 'twinsburg;tburg'),
                -- Same name, same region, ~100 km apart: genuinely distinct
                ('far-1', 'Springfield', 'locality', 'US', 'US-MO', 40000, NULL,
                 -93.3, 37.2, -93.4, 37.1, -93.2, 37.3, 'springfield'),
                ('far-2', 'Springfield', 'locality', 'US', 'US-MO', 30000, NULL,
                 -92.2, 37.5, -92.3, 37.4, -92.1, 37.6, 'springfield'),
                -- Same name, different region: kept
                ('other-region', 'Twinsburg', 'locality', 'US', 'US-PA', 20000, NULL,
                 -81.0, 41.0, -81.1, 40.9, -80.9, 41.1, 'twinsburg'),
                -- Non-locality with a colliding name: untouched
                ('region-tw', 'Twinsburg', 'region', 'US', 'US-OH', NULL, NULL,
                 -81.0, 41.0, -82.0, 40.0, -80.0, 42.0, 'twinsburg')
            ) AS t(gers_id, name, subtype, country, region, population,
                   wiki_importance, lon, lat, bbox_xmin, bbox_ymin,
                   bbox_xmax, bbox_ymax, search_name)
        ) TO '{path}' (FORMAT PARQUET);
    """)
    con.close()


class TestDedupLocalities:
    @pytest.fixture
    def deduped(self, tmp_path):
        base = tmp_path / "enriched.parquet"
        write_dedup_test_parquet(base)
        out = tmp_path / "deduped.parquet"
        dedup_localities(base, out)
        con = duckdb.connect()
        cur = con.execute(f"SELECT * FROM read_parquet('{out}')")
        cols = [d[0] for d in cur.description]
        rows = {r[cols.index("gers_id")]: dict(zip(cols, r)) for r in cur.fetchall()}
        con.close()
        return rows

    def test_duplicate_dropped_leader_kept(self, deduped):
        assert "twin-1" in deduped
        assert "twin-2" not in deduped

    def test_distinct_same_name_places_kept(self, deduped):
        assert "far-1" in deduped and "far-2" in deduped
        assert "other-region" in deduped
        assert "region-tw" in deduped
        assert len(deduped) == 5

    def test_leader_absorbs_cluster(self, deduped):
        leader = deduped["twin-1"]
        # Max population across the cluster
        assert leader["population"] == 60000
        # Bbox union
        assert leader["bbox_xmin"] == -81.20
        assert leader["bbox_ymax"] == 41.20
        # search_name segment union (order-independent)
        assert set(leader["search_name"].split(";")) == {
            "twinsburg", "twin city", "tburg"}

    def test_untouched_rows_pass_through(self, deduped):
        far = deduped["far-1"]
        assert far["population"] == 40000
        assert far["search_name"] == "springfield"


class TestWikiImportanceJoin:
    def test_join_by_qid(self, enriched_parquet):
        con = duckdb.connect()
        rows = dict(con.execute(f"""
            SELECT gers_id, wiki_importance
            FROM read_parquet('{enriched_parquet}')
        """).fetchall())
        con.close()
        assert rows["nyc-001"] == pytest.approx(0.91)  # MAX over languages
        assert rows["sf-001"] == pytest.approx(0.84)
        assert rows["tiny-001"] is None  # no wikidata QID (over population bar)
        # Famous small locality survives the prune; obscure one does not.
        assert rows["famous-001"] == pytest.approx(0.80)
        assert "obscure-001" not in rows
        # Mid-tier fame clears the keep threshold (0.5) into country shards
        assert rows["known-001"] == pytest.approx(0.55)

    def test_null_column_without_importance_file(self, tmp_path):
        base = tmp_path / "divisions.parquet"
        write_test_parquet(base)
        out = tmp_path / "divisions-nowiki.parquet"
        enrich_parquet_with_wiki_importance(base, out, None)
        con = duckdb.connect()
        rows = dict(con.execute(
            f"SELECT gers_id, wiki_importance FROM read_parquet('{out}')"
        ).fetchall())
        con.close()
        assert all(v is None for v in rows.values())
        # Without importance data, small QID localities cannot prove fame
        # and are pruned like before the wikidata over-fetch.
        assert "famous-001" not in rows
        assert "obscure-001" not in rows
        assert "known-001" not in rows
        assert {"nyc-001", "sf-001", "tiny-001", "region-ny"} <= set(rows)


class TestEndToEndShardBuild:
    def test_build_and_query_shard(self, enriched_parquet, tmp_path):
        shard_path = tmp_path / "US.db"
        info = build_country_shard(enriched_parquet, "US", shard_path, "test")
        assert info["record_count"] == 8

        db = sqlite3.connect(shard_path)

        # Alias column contains concatenations
        (alias,) = db.execute(
            "SELECT search_alias FROM divisions WHERE gers_id = 'nyc-001'"
        ).fetchone()
        tokens = alias.split()
        assert "newyork" in tokens
        assert "newyorkcity" in tokens

        # Context column contains parent codes/names
        (context,) = db.execute(
            "SELECT search_context FROM divisions WHERE gers_id = 'nyc-001'"
        ).fetchone()
        assert "us-ny" in context
        assert "united states" in context

        # Importance populated, in [0, 1], megacity > village
        importances = dict(db.execute(
            "SELECT gers_id, importance FROM divisions"
        ).fetchall())
        assert all(0.0 <= v <= 1.0 for v in importances.values())
        assert importances["nyc-001"] > importances["tiny-001"]
        # NYC: 0.30 megacity prior + 0.5*0.91 wiki + 0.2 capped pop
        assert importances["nyc-001"] == pytest.approx(0.955)

        # The exact query shape the Rust side uses: weighted bm25 + MATCH
        rows = db.execute(
            WEIGHTED_BM25_QUERY, (fts_query("new york"),)
        ).fetchall()
        ids = [r[0] for r in rows]
        assert "nyc-001" in ids
        assert all(isinstance(r[1], float) for r in rows)

        # P0: "france" autocomplete must not match San Francisco
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("france"),)).fetchall()
        assert rows == []

        # Concatenation alias is searchable
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("newyork"),)).fetchall()
        assert "nyc-001" in [r[0] for r in rows]

        # Counties and local-admin divisions are searchable from the country
        # shard even without a population or Wikidata record.
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("cook county"),)).fetchall()
        assert [r[0] for r in rows] == ["county-cook"]
        rows = db.execute(WEIGHTED_BM25_QUERY, (fts_query("manhattan"),)).fetchall()
        assert [r[0] for r in rows] == ["localadmin-manhattan"]

        db.close()

    def test_head_shard_includes_wiki_famous(self, enriched_parquet, tmp_path):
        head_path = tmp_path / "HEAD.db"
        info = build_head_shard(
            enriched_parquet, head_path, "test", population_threshold=1_000_000
        )
        db = sqlite3.connect(head_path)
        ids = {r[0] for r in db.execute("SELECT gers_id FROM divisions")}
        db.close()

        assert "nyc-001" in ids        # population >= threshold
        assert "region-ny" in ids      # subtype region
        assert "famous-001" in ids     # wiki_importance 0.80 >= HEAD bar
        assert "tiny-001" not in ids   # small, not famous
        assert "sf-001" in ids         # wiki_importance 0.84 >= HEAD bar
        # Mid-tier fame (0.55): in its country shard, but below the HEAD
        # bar — HEAD is loaded on every search and must stay small.
        assert "known-001" not in ids
        assert "county-cook" not in ids
        assert "localadmin-manhattan" not in ids
        assert info["record_count"] == len(ids)


class TestVersionSortKey:
    def test_numeric_suffix_order(self):
        from build_shards import version_sort_key

        versions = ["2026-02-25.9", "2026-02-25.10", "2026-02-25.2", "2026-03-25.0"]
        ordered = sorted(versions, key=version_sort_key, reverse=True)
        # Lexicographic order would rank .9 above .10
        assert ordered == [
            "2026-03-25.0",
            "2026-02-25.10",
            "2026-02-25.9",
            "2026-02-25.2",
        ]


class TestConstants:
    def test_shard_threshold_is_50mb(self):
        assert SHARD_SIZE_THRESHOLD_BYTES == 50 * 1024 * 1024

    def test_fallback_region_suffix(self):
        assert FALLBACK_REGION_SUFFIX == "XX"
