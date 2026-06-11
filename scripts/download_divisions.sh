#!/bin/bash
# Download global divisions using the latest Overture release
#
# Usage: ./scripts/download_divisions.sh [RELEASE]
#
# If RELEASE is not provided, fetches the latest from STAC catalog.
# Example: ./scripts/download_divisions.sh 2025-12-17.0
#
# Outputs:
#   exports/divisions-global.parquet  - Forward geocoding data (FTS search_name/search_context)
#   exports/divisions-reverse.parquet - Reverse geocoding data (with bbox/H3 cells)

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Use provided release or fetch latest from STAC.
# No hardcoded fallback: Overture purges releases after 90 days, so any
# pinned fallback is guaranteed to break eventually. Fail fast instead.
if [ -n "$1" ]; then
    RELEASE="$1"
    echo "Using provided Overture release: $RELEASE"
else
    echo "Fetching latest Overture release from STAC..."
    RELEASE=$(python3 "$SCRIPT_DIR/stac.py" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+' | head -1 || true)
    if [ -z "$RELEASE" ]; then
        echo "ERROR: Failed to fetch latest Overture release from STAC catalog." >&2
        echo "Pass a release explicitly: ./scripts/download_divisions.sh YYYY-MM-DD.N" >&2
        exit 1
    fi
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

# -csv -noheader: the default box output includes the column type ("int64"),
# which a naive first-number grep matches instead of the count.
ROW_COUNT=$(duckdb -csv -noheader -c "SELECT COUNT(*) FROM read_parquet('$PROJECT_DIR/exports/divisions-global.parquet')" 2>/dev/null | tr -d '[:space:]')
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

REVERSE_COUNT=$(duckdb -csv -noheader -c "SELECT COUNT(*) FROM read_parquet('$PROJECT_DIR/exports/divisions-reverse.parquet')" 2>/dev/null | tr -d '[:space:]')
echo "Reverse geocoding: exports/divisions-reverse.parquet ($REVERSE_COUNT rows)"

echo "Done!"
