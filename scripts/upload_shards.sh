#!/bin/bash
# Upload shards and STAC catalog to R2
#
# Usage:
#   ./scripts/upload_shards.sh [VERSION]
#   ./scripts/upload_shards.sh 2026-01-02.0
#
# Prerequisites:
#   - wrangler CLI installed and authenticated
#   - R2 bucket 'geocoder-shards' created

set -euo pipefail

BUCKET="geocoder-shards"
VERSION="${1:-}"

# Validate version format to prevent shell injection
validate_version() {
    if [[ ! "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$ ]]; then
        echo "Error: Invalid version format: $1"
        echo "Expected format: YYYY-MM-DD.N (e.g., 2026-01-02.0)"
        exit 1
    fi
}

# If no version specified, find the latest
if [ -z "$VERSION" ]; then
    VERSION=$(ls -1 shards/ 2>/dev/null | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+$' | sort -r | head -1)
    if [ -z "$VERSION" ]; then
        echo "Error: No version found in shards/"
        echo "Run: python scripts/build_shards.py"
        exit 1
    fi
fi

validate_version "$VERSION"

echo "Uploading shards for version $VERSION to R2 bucket '$BUCKET'"
echo

# Check if version directory exists
if [ ! -d "shards/${VERSION}" ]; then
    echo "Error: shards/${VERSION} not found"
    exit 1
fi

# Count files to upload
DB_COUNT=$(find "shards/${VERSION}/shards" -name "*.db" 2>/dev/null | wc -l | tr -d ' ')
STAC_ITEM_COUNT=$(find "shards/${VERSION}/items" -name "*.json" 2>/dev/null | wc -l | tr -d ' ')

echo "Files to upload:"
echo "  - ${DB_COUNT} SQLite database shards (.db)"
echo "  - ${STAC_ITEM_COUNT} STAC item metadata files (.json)"
echo "  - 1 collection.json (STAC collection)"
echo "  - 1 catalog.json (STAC root catalog)"
echo

# Upload shard databases
echo "Uploading SQLite shards..."
for shard in "shards/${VERSION}/shards"/*.db; do
    name=$(basename "$shard")
    echo "  $name"
    wrangler r2 object put "${BUCKET}/${VERSION}/shards/${name}" \
        --file "$shard" \
        --content-type "application/x-sqlite3"
done

# Upload STAC items
echo
echo "Uploading STAC items..."
for item in "shards/${VERSION}/items"/*.json; do
    name=$(basename "$item")
    echo "  $name"
    wrangler r2 object put "${BUCKET}/${VERSION}/items/${name}" \
        --file "$item" \
        --content-type "application/geo+json"
done

# Upload collection
echo
echo "Uploading STAC collection..."
wrangler r2 object put "${BUCKET}/${VERSION}/collection.json" \
    --file "shards/${VERSION}/collection.json" \
    --content-type "application/json"

# Upload root catalog (updates "latest" pointer)
echo
echo "Uploading STAC root catalog..."
wrangler r2 object put "${BUCKET}/catalog.json" \
    --file "shards/catalog.json" \
    --content-type "application/json"

echo
echo "Done! Uploaded to R2 bucket '${BUCKET}'"
echo
echo "STAC catalog URL:"
echo "  https://<your-r2-domain>/catalog.json"
echo
echo "To test locally:"
echo "  curl https://<your-r2-domain>/catalog.json"
