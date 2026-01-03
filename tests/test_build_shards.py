"""Tests for build_shards.py functions."""

import sys
from pathlib import Path

import pytest

# Add scripts to path
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from build_shards import (
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


class TestConstants:
    def test_shard_threshold_is_50mb(self):
        assert SHARD_SIZE_THRESHOLD_BYTES == 50 * 1024 * 1024

    def test_fallback_region_suffix(self):
        assert FALLBACK_REGION_SUFFIX == "XX"
