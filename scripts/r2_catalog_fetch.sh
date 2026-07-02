#!/usr/bin/env bash
# Fetch catalog.json from R2, distinguishing "object does not exist" from
# transient failure. Retention and version-numbering steps must never mistake
# an outage for an empty catalog: an empty catalog means "nothing is
# referenced", which fails open into deleting or overwriting live versions.
#
# Usage: r2_catalog_fetch.sh <dest-path>
# Requires: AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, CLOUDFLARE_ACCOUNT_ID
#
# Exit 0 with <dest-path> written: catalog fetched.
# Exit 0 with NO <dest-path>: catalog genuinely absent (first deploy).
# Exit 1: transient failure after retries — caller must abort, not assume {}.
set -u

DEST="$1"
ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"

for attempt in 1 2 3; do
  if ERR=$(aws s3 cp "s3://geocoder-shards/catalog.json" "$DEST" \
      --endpoint-url "$ENDPOINT" --region auto 2>&1); then
    exit 0
  fi
  if echo "$ERR" | grep -qiE 'Not Found|NoSuchKey|404'; then
    echo "catalog.json not present in R2 (first deploy)" >&2
    rm -f "$DEST"
    exit 0
  fi
  echo "catalog fetch attempt ${attempt}/3 failed: $ERR" >&2
  [ "$attempt" -lt 3 ] && sleep $((attempt * 5))
done

echo "::error::Unable to fetch catalog.json after 3 attempts (transient failure, not a 404); refusing to treat the catalog as empty" >&2
exit 1
