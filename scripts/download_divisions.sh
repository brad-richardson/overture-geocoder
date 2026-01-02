#!/bin/bash
# Download global divisions using the latest Overture release
#
# Usage: ./scripts/download_divisions.sh [RELEASE]
#
# If RELEASE is not provided, fetches the latest from STAC catalog.
# Example: ./scripts/download_divisions.sh 2025-12-17.0
#
# Outputs:
#   exports/divisions-global.parquet  - Forward geocoding data (with FTS search_text)
#   exports/divisions-reverse.parquet - Reverse geocoding data (with bbox/H3 cells)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
FALLBACK_RELEASE="2025-12-17.0"

# Use provided release or fetch latest from STAC
if [ -n "$1" ]; then
    RELEASE="$1"
    echo "Using provided Overture release: $RELEASE"
else
    echo "Fetching latest Overture release from STAC..."
    RELEASE=$(python3 scripts/stac.py 2>/dev/null | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+') || {
        echo "Warning: Failed to fetch latest release, using fallback: $FALLBACK_RELEASE"
        RELEASE="$FALLBACK_RELEASE"
    }
    echo "Using Overture release: $RELEASE"
fi

# Create exports directory
mkdir -p "$PROJECT_DIR/exports"

cd "$PROJECT_DIR"

# Download forward geocoding data
echo "Downloading forward geocoding data..."
sed "s|__OVERTURE_RELEASE__|$RELEASE|g" scripts/download_divisions_global.sql | duckdb

# Verify forward data
if [ ! -f "$PROJECT_DIR/exports/divisions-global.parquet" ]; then
    echo "ERROR: Output file not created - release $RELEASE may be expired (data removed after 90 days)"
    exit 1
fi

ROW_COUNT=$(duckdb -c "SELECT COUNT(*) FROM read_parquet('$PROJECT_DIR/exports/divisions-global.parquet')" 2>/dev/null | grep -oE '[0-9]+' | head -1)
if [ -z "$ROW_COUNT" ] || [ "$ROW_COUNT" -eq 0 ]; then
    echo "ERROR: No data returned - release $RELEASE may be expired (data removed after 90 days)"
    exit 1
fi

echo "Forward geocoding: exports/divisions-global.parquet ($ROW_COUNT rows)"

# Download reverse geocoding data (JOINs division + division_area)
echo "Downloading reverse geocoding data..."
sed "s|__OVERTURE_RELEASE__|$RELEASE|g" scripts/download_divisions_area.sql | duckdb

# Verify reverse data
if [ ! -f "$PROJECT_DIR/exports/divisions-reverse.parquet" ]; then
    echo "ERROR: Reverse output file not created"
    exit 1
fi

REVERSE_COUNT=$(duckdb -c "SELECT COUNT(*) FROM read_parquet('$PROJECT_DIR/exports/divisions-reverse.parquet')" 2>/dev/null | grep -oE '[0-9]+' | head -1)
echo "Reverse geocoding: exports/divisions-reverse.parquet ($REVERSE_COUNT rows)"

echo "Done!"
