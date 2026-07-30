# Unified v2 API contract

Status: implemented Worker contract; not discoverable until a verified
`v2/catalog.json` is published. Preparing or deploying the Worker does not build
shards or publish a v2 release.

V2 consolidates division and Places text search, structured exact-address
lookup, reverse geocoding, and GERS ID lookup under a small
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
`proximity` first consults only the packed global head, which supports one or
two exact normalized tokens. When a three- or four-token global-head query is
empty solely because of that token limit, the reader may interpret the final
one or two exact tokens as a locality-like division and route the remaining
name once at that division's centroid. This fallback does not run for explicit
proximity or a non-empty global-head result. The inference is recorded in
`metadata.places_locality_inference`; distance from its routing centroid is not
exposed as user-proximity distance.

A located query routes to exactly one stable world-quadkey shard and supports
up to four tokens, with optional last-token prefix matching. Reported distance
is diagnostic within that bounded candidate set; it is not a claim of
exhaustive global nearest-POI ranking.

The index also stores CJK bigrams for later substring work, but v2 query
planning currently uses full normalized word tokens. This keeps ordinary long
CJK names inside the four-clause bound without pretending that a partial-name
substring parser is already supported.

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

Explicit canonical context fields use one literal exact key. When a request
instead supplies `state` (or `region`) plus `city`, with no canonical context
field or `county`, the Worker tries three bounded exact source representations:
city in the last address level, in `postal_city`, then in both. The first
non-empty result wins. It does not infer a missing state or treat omitted
context as a wildcard. Structured response metadata reports
`resolution_variant` and `lookup_attempts`.

If `types` is supplied it must be exactly `address`. `limit`, `autocomplete`,
and `proximity` are rejected in structured mode because an exact lookup returns
every duplicate candidate up to the hard 512-candidate safety cap; it does not
silently truncate ambiguity.

Forward responses are GeoJSON `FeatureCollection` objects. `metadata.mode` is
`text` or `structured_address`. Structured responses also report coverage,
normalization version, candidate count, and ambiguity. Text responses include
`metadata.places_locality_inference` only when the bounded locality-suffix
fallback supplied an internal routing centroid.

## `GET /v2/reverse`

`lat` and `lon` are required. Reverse currently serves divisions only. Missing
`types` means all division types; an explicit division subset is honored by
filtering the returned subtype. `poi` and `address` are rejected until their
spatial reverse indexes exist.

The response is a GeoJSON `FeatureCollection` containing zero or one feature.

## `GET /v2/ids/:id`

Looks up a 32-hex-digit or canonical hyphenated Overture GERS UUID in the exact
core release selected by v2. The successful response matches the existing
`/id/:gers_id` object shape—`id`, `bbox`, and optional locator metadata—and adds
the atomic v2 `data_version` object. A syntactically invalid ID returns 400
without an R2 shard read; a valid absent ID returns 404.

## Legacy and availability behavior

The existing `/search`, `/reverse`, and `/id/:gers_id` endpoints remain in
place for current clients and continue using the v1 catalog/fallback behavior.
The former `/address`, `/__address-page-spike`, and
`/__places-page-spike` endpoints are removed.

If no v2 catalog/release has been published, v2 endpoints return a structured
503 `release_unavailable`. A missing explicitly requested family also returns
503. Invalid parameter combinations return 400.
