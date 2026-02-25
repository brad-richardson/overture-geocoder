"""Tests for build_shards.py functions."""

import sys
from pathlib import Path

import pytest

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from build_shards import (
    enrich_search_text,
    validate_country_code,
    validate_region_code,
    validate_population_threshold,
    SHARD_SIZE_THRESHOLD_BYTES,
    FALLBACK_REGION_SUFFIX,
)


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


class TestEnrichSearchText:
    def test_concatenated_pairwise(self):
        result = enrich_search_text("new york city nyc ny")
        tokens = result.split()
        assert "newyork" in tokens
        assert "yorkcity" in tokens

    def test_concatenated_full(self):
        result = enrich_search_text("new york city nyc ny")
        tokens = result.split()
        assert "newyorkcity" in tokens

    def test_abbreviation_saint_to_st(self):
        result = enrich_search_text("saint louis missouri")
        assert " st " in f" {result} "

    def test_abbreviation_st_to_saint(self):
        result = enrich_search_text("st louis missouri")
        assert "saint" in result

    def test_abbreviation_fort_to_ft(self):
        result = enrich_search_text("fort worth texas")
        assert " ft " in f" {result} "

    def test_abbreviation_ft_to_fort(self):
        result = enrich_search_text("ft worth texas")
        assert "fort" in result

    def test_abbreviation_mount_to_mt(self):
        result = enrich_search_text("mount vernon virginia")
        assert " mt " in f" {result} "

    def test_abbreviation_directional(self):
        result = enrich_search_text("north charleston south carolina")
        assert " n " in f" {result} "
        assert " s " in f" {result} "

    def test_single_word_no_concatenation(self):
        result = enrich_search_text("boston")
        # Single word should not produce concatenations
        assert result == "boston"

    def test_empty_input(self):
        assert enrich_search_text("") == ""

    def test_no_duplicates_in_abbreviations(self):
        # If "st" is already in the text, don't add it again
        result = enrich_search_text("saint louis st louis")
        count = result.split().count("st")
        # "st" appears once in original, should not be added again
        assert count == 1

    def test_short_concat_skipped(self):
        # Pairwise concatenation of very short words (< 4 chars) should be skipped
        result = enrich_search_text("a bc rest of text")
        extras = result.split()[5:]  # Skip original words
        # "abc" (3 chars) should not appear as its own token
        assert "abc" not in extras

    def test_preserves_original_text(self):
        original = "boston massachusetts united states"
        result = enrich_search_text(original)
        assert result.startswith(original)


class TestConstants:
    def test_shard_threshold_is_50mb(self):
        assert SHARD_SIZE_THRESHOLD_BYTES == 50 * 1024 * 1024

    def test_fallback_region_suffix(self):
        assert FALLBACK_REGION_SUFFIX == "XX"
