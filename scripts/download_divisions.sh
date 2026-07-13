#!/bin/bash
# Download global divisions using the latest Overture release
#
# Usage: ./scripts/download_divisions.sh [RELEASE]
#        ./scripts/download_divisions.sh --smoke-monaco [RELEASE]
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

MODE="global"
RELEASE=""
for arg in "$@"; do
    case "$arg" in
        --smoke-monaco)
            MODE="smoke-monaco"
            ;;
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].[0-9]*)
            if [ -n "$RELEASE" ]; then
                echo "ERROR: Multiple Overture releases supplied." >&2
                exit 2
            fi
            RELEASE="$arg"
            ;;
        *)
            echo "ERROR: Unknown argument: $arg" >&2
            exit 2
            ;;
    esac
done

# Use provided release or fetch latest from STAC.
# No hardcoded fallback: Overture purges releases after 90 days, so any
# pinned fallback is guaranteed to break eventually. Fail fast instead.
if [ -n "$RELEASE" ]; then
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

if [ "$MODE" = "smoke-monaco" ]; then
    exec python3 "$SCRIPT_DIR/download_divisions_smoke.py" \
        --release "$RELEASE" \
        --forward-output "$PROJECT_DIR/exports/divisions-global.parquet" \
        --reverse-output "$PROJECT_DIR/exports/divisions-reverse.parquet" \
        --profile-output "$PROJECT_DIR/exports/monaco-export-profile.json"
fi

# Download forward geocoding data
echo "Downloading forward geocoding data..."
sed \
    -e "s|__OVERTURE_RELEASE__|$RELEASE|g" \
    -e "s|__DIVISION_FILTER__|TRUE|g" \
    -e "s|__OUTPUT_PATH__|exports/divisions-global.parquet|g" \
    scripts/download_divisions_global.sql | duckdb

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
sed \
    -e "s|__OVERTURE_RELEASE__|$RELEASE|g" \
    -e "s|__DIVISION_FILTER__|TRUE|g" \
    -e "s|__AREA_FILTER__|TRUE|g" \
    -e "s|__OUTPUT_PATH__|exports/divisions-reverse.parquet|g" \
    scripts/download_divisions_area.sql | duckdb

# Verify reverse data
if [ ! -f "$PROJECT_DIR/exports/divisions-reverse.parquet" ]; then
    echo "ERROR: Reverse output file not created"
    exit 1
fi

REVERSE_COUNT=$(duckdb -csv -noheader -c "SELECT COUNT(*) FROM read_parquet('$PROJECT_DIR/exports/divisions-reverse.parquet')" 2>/dev/null | tr -d '[:space:]')
echo "Reverse geocoding: exports/divisions-reverse.parquet ($REVERSE_COUNT rows)"

echo "Done!"
