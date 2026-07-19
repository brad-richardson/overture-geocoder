# Unified v2 API contract

Status: implemented Worker contract; not discoverable until a verified
`v2/catalog.json` is published. Preparing or deploying the Worker does not build
shards or publish a v2 release.

V2 consolidates division and Places text search, structured exact-address
lookup, reverse geocoding, and feature lookup under a small GeoJSON-oriented
surface. It borrows the familiar forward/reverse split and comma-separated
`types` filter used by hosted geocoders, but it does not claim wire compatibility
with Mapbox, TomTom, or Nominatim.

There is deliberately no batch endpoint.

## Version identity

Every successful v2 response is pinned to one atomic release and carries:

```json
{
  "data_version": {
    "overture_release": "2026-06-17.0",
    "geocoder_build": "2026-07-19.1"
  }
}
```

The same values are exposed as `X-Overture-Release` and
`X-Geocoder-Build`; `X-Data-Version` remains an alias for the geocoder build.
The mutable `v2/catalog.json` selects one immutable release manifest. That
manifest binds exact core, Places, and address entrypoints, so one request never
falls back across geocoder builds or mixes Overture releases.

## `GET /v2/forward`

Text mode accepts:

- `q` (required, at most 200 bytes);
- `types`, a comma-separated set of division types, `poi`, or `address`;
- `place` as an input alias for `poi` and `neighbourhood` as an alias for
  `neighborhood`;
- `limit` from 1 through 10 (default 10);
- `autocomplete=true|false` (default true);
- `proximity=longitude,latitude`; and
- `country` as a division-search bias.

Without `types`, text mode searches divisions and POIs. Free-text address
search is intentionally not advertised yet. An explicit `types=address` with
`q` returns an unsupported-capability error rather than silently searching a
different family.

The first Places reader has bounded, explicit recall limits. A query without
`proximity` consults only the packed global head and supports one or two exact
normalized tokens. A located query routes to exactly one stable world-quadkey
shard and supports up to four tokens, with optional last-token prefix matching.
Reported distance is diagnostic within that bounded candidate set; it is not a
claim of exhaustive global nearest-POI ranking.

Structured exact-address mode uses the same endpoint without `q`. Required
fields are `country`, `street`, and `number` (or `address_number`). Optional
fields default to the literal empty string:

| Canonical field | Accepted aliases |
|---|---|
| `admin_level_general` | `state`, `region` |
| `admin_level_specific` | `county` |
| `postal_city` | `city` |
| `postcode` | `postalcode` |
| `number` | `address_number` |

If `types` is supplied it must be exactly `address`. `limit`, `autocomplete`,
and `proximity` are rejected in structured mode because an exact lookup returns
every duplicate candidate up to the hard 512-candidate safety cap; it does not
silently truncate ambiguity.

Forward responses are GeoJSON `FeatureCollection` objects. `metadata.mode` is
`text` or `structured_address`. Structured responses also report coverage,
normalization version, candidate count, and ambiguity.

## `GET /v2/reverse`

`lat` and `lon` are required. Reverse currently serves divisions only. Missing
`types` means all division types; an explicit division subset is honored by
filtering the returned subtype. `poi` and `address` are rejected until their
spatial reverse indexes exist.

The response is a GeoJSON `FeatureCollection` containing zero or one feature.

## `GET /v2/features/:gers_id`

Looks up a 32-hex-digit or canonical hyphenated Overture GERS UUID in the exact
core release selected by v2. The response is one GeoJSON `Feature` with its
bbox and locator metadata. A syntactically invalid ID returns 400 without an R2
shard read; a valid absent ID returns 404.

## Legacy and availability behavior

The existing `/search`, `/reverse`, and `/id/:gers_id` endpoints remain in
place for current clients and continue using the v1 catalog/fallback behavior.
The former `/address`, `/__address-page-spike`, and
`/__places-page-spike` endpoints are removed.

If no v2 catalog/release has been published, v2 endpoints return a structured
503 `release_unavailable`. A missing explicitly requested family also returns
503. Invalid parameter combinations return 400.
