#!/bin/bash
# Download global divisions using the latest Overture release
#
# Usage: ./scripts/download_divisions.sh [RELEASE]
#
# If RELEASE is not provided, fetches the latest from STAC catalog.
# Example: ./scripts/download_divisions.sh 2025-12-17.0

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
    RELEASE=$(curl -sf https://stac.overturemaps.org/catalog.json | python3 -c "import sys, json; print(json.load(sys.stdin)['latest'])" 2>/dev/null) || {
        echo "Warning: Failed to fetch latest release, using fallback: $FALLBACK_RELEASE"
        RELEASE="$FALLBACK_RELEASE"
    }
    echo "Using Overture release: $RELEASE"
fi

# Create exports directory
mkdir -p "$PROJECT_DIR/exports"

# Run SQL with release substituted
cd "$PROJECT_DIR"
sed "s|__OVERTURE_RELEASE__|$RELEASE|g" scripts/download_divisions_global.sql | duckdb

echo "Done! Output: exports/divisions-global.parquet"
