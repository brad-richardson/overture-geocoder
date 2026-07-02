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
#
# A single 404 is NOT proof of absence: R2 has returned spurious 404s under
# transient conditions (2026-07-02: one such misclassification made a rebuild
# compute version .0 and republish it as latest over a live .2). A real
# missing object 404s deterministically, so absence is only declared after
# TWO CONSECUTIVE 404s with a delay between them.
set -u

DEST="$1"
ENDPOINT="https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"

consecutive_404=0
for attempt in 1 2 3 4; do
  if ERR=$(aws s3 cp "s3://geocoder-shards/catalog.json" "$DEST" \
      --endpoint-url "$ENDPOINT" --region auto 2>&1); then
    exit 0
  fi
  if echo "$ERR" | grep -qiE 'Not Found|NoSuchKey|404'; then
    consecutive_404=$((consecutive_404 + 1))
    echo "catalog fetch attempt ${attempt}/4: 404 (${consecutive_404} consecutive): $ERR" >&2
    if [ "$consecutive_404" -ge 2 ]; then
      echo "catalog.json consistently absent in R2 (first deploy)" >&2
      rm -f "$DEST"
      exit 0
    fi
  else
    consecutive_404=0
    echo "catalog fetch attempt ${attempt}/4 failed: $ERR" >&2
  fi
  [ "$attempt" -lt 4 ] && sleep $((attempt * 5))
done

echo "::error::Unable to fetch catalog.json after 4 attempts (transient failure, not a confirmed 404); refusing to treat the catalog as empty" >&2
exit 1
