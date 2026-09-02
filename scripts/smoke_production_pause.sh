#!/usr/bin/env bash
# Verify the supported v1 API and the complete production v2 pause.
#
# Usage: smoke_production_pause.sh EXPECTED_V1_VERSION [BASE_URL]

set -euo pipefail

EXPECTED_VERSION=${1:?expected v1 version is required}
BASE_URL=${2:-https://geocoder.bradr.dev}

smoke_once() {
  local root health search reverse gers_id id_json path status

  root=$(curl -fsS --max-time 20 "$BASE_URL/")
  jq -e '.endpoints == ["/search", "/reverse", "/id/:id"]' \
    >/dev/null <<<"$root"

  health=$(curl -fsS --max-time 20 "$BASE_URL/health")
  jq -e --arg version "$EXPECTED_VERSION" \
    '.status == "ok" and .version == $version' >/dev/null <<<"$health"

  search=$(curl -fsS --max-time 20 "$BASE_URL/search?q=berlin&limit=1")
  jq -e --arg version "$EXPECTED_VERSION" '
    .data_version == $version
    and (.results | length) >= 1
    and .results[0].country == "DE"
    and (.results[0].lat | type == "number")
    and (.results[0].lon | type == "number")
    and .results[0].lat >= 51 and .results[0].lat <= 54
    and .results[0].lon >= 12 and .results[0].lon <= 15
  ' >/dev/null <<<"$search"

  reverse=$(curl -fsS --max-time 20 \
    "$BASE_URL/reverse?lat=42.3601&lon=-71.0589")
  jq -e --arg version "$EXPECTED_VERSION" '
    .data_version == $version
    and (.primary_name // "" | test("Boston|Suffolk"; "i"))
    and (.hierarchy | length >= 2)
  ' >/dev/null <<<"$reverse"

  gers_id=$(jq -er '.results[0].gers_id | select(type == "string" and length > 0)' \
    <<<"$search")
  id_json=$(curl -fsS --max-time 20 "$BASE_URL/id/$gers_id")
  jq -e --arg version "$EXPECTED_VERSION" --arg id "$gers_id" '
    .data_version == $version
    and .id == $id
    and .bbox
    and (.bbox.xmin | type == "number")
    and (.bbox.xmax | type == "number")
    and (.bbox.ymin | type == "number")
    and (.bbox.ymax | type == "number")
    and .bbox.xmin < .bbox.xmax
    and .bbox.ymin < .bbox.ymax
    and .bbox.xmin >= -180 and .bbox.xmax <= 180
    and .bbox.ymin >= -90 and .bbox.ymax <= 90
  ' >/dev/null <<<"$id_json"

  for path in \
    /v2 \
    '/v2/forward?q=berlin' \
    '/v2/reverse?lat=42.3601&lon=-71.0589' \
    /v2/ids/00000000-0000-4000-8000-000000000000
  do
    status=$(curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
      "$BASE_URL$path")
    [ "$status" = 404 ]
  done

  status=$(curl -sS --max-time 20 -I -o /dev/null -w '%{http_code}' \
    "$BASE_URL/v2/forward")
  [ "$status" = 404 ]
  status=$(curl -sS --max-time 20 -X OPTIONS -o /dev/null -w '%{http_code}' \
    "$BASE_URL/v2/forward")
  [ "$status" = 404 ]
}

for attempt in 1 2 3; do
  if smoke_once; then
    printf 'Production smoke passed for v1 %s at %s.\n' \
      "$EXPECTED_VERSION" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    exit 0
  fi
  if [ "$attempt" -lt 3 ]; then
    echo "Production smoke attempt $attempt failed; waiting for a fresh rate window."
    sleep 65
  fi
done

echo "::error::Production smoke failed after three attempts." >&2
exit 1
