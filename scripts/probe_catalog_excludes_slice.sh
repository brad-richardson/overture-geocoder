#!/usr/bin/env bash
# Negative probe: assert the PRODUCTION catalog.json does NOT reference a slice
# version. A family-release slice is non-promoting by construction, so the
# production catalog must never link it — before publish, after publish, or after
# cleanup. This reads the public catalog over HTTPS (no credentials, no writes)
# and fails closed if the slice ever appears as a catalog child.
#
# Usage: probe_catalog_excludes_slice.sh <base_url> <slice_version>
set -euo pipefail

BASE_URL="${1:?base url required}"
SLICE_VERSION="${2:?slice version required}"

CATALOG="$(mktemp)"
trap 'rm -f "$CATALOG"' EXIT

# A missing catalog (first-ever deploy) trivially excludes the slice; only a
# fetched catalog that references the slice is a failure.
if ! curl -fsSL "${BASE_URL%/}/catalog.json" -o "$CATALOG"; then
  echo "catalog.json not fetchable at ${BASE_URL} (treated as no reference)"
  exit 0
fi

# Fail closed if ANY child link resolves to the slice version's prefix.
if jq -e --arg v "$SLICE_VERSION" '
  any(.links[]?;
    .rel == "child"
    and ((.href | ltrimstr("./") | split("/")[0]) == $v))
' "$CATALOG" >/dev/null; then
  echo "::error::production catalog.json references slice ${SLICE_VERSION}; a slice must never be promoted"
  exit 1
fi

echo "production catalog.json does not reference ${SLICE_VERSION} (as required)"
