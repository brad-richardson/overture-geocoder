# overture-geocoder

Python client for the [Overture Geocoder](https://github.com/brad-richardson/overture-geocoder) — a forward and reverse geocoder built on [Overture Maps](https://overturemaps.org/) division data, served from Cloudflare Workers.

## Install

```bash
pip install overture-geocoder
```

With optional geometry support (fetches full polygons from Overture S3):

```bash
pip install "overture-geocoder[geometry]"
```

Requires Python 3.10+.

## Quickstart

### Forward geocoding

```python
from overture_geocoder import OvertureGeocoder

with OvertureGeocoder() as client:
    results = client.search("Boston, MA", limit=5)
    for r in results:
        print(f"{r.primary_name} ({r.type}): {r.lat}, {r.lon}")
        # r.boundingbox is [min_lon, min_lat, max_lon, max_lat]
        # r.country (e.g. "US") and r.region (e.g. "US-MA") when available
```

Search supports autocomplete-style prefix matching and location biasing:

```python
results = client.search("Bos", autocomplete=True, lat=42.36, lon=-71.06)
```

GeoJSON output:

```python
fc = client.search_geojson("Boston, MA")  # FeatureCollection with Point features
```

### Reverse geocoding

```python
with OvertureGeocoder() as client:
    results = client.reverse(42.36, -71.06)
    if results:
        r = results[0]
        print(f"{r.primary_name} ({r.subtype}), confidence={r.confidence}")
        for entry in r.hierarchy or []:
            print(f"  {entry.subtype}: {entry.name}")
```

`reverse()` returns a list with at most one result (the best-matching division plus its administrative hierarchy); it returns an empty list when no division contains the point. `confidence` is one of `"high"`, `"medium"`, or `"low"`.

### GERS ID lookup

Look up the bounding box for any GERS ID in the ID index:

```python
with OvertureGeocoder() as client:
    result = client.lookup_id("5df2793f-5a0a-4fcf-bd3c-7edb8cc495d8")
    if result:
        print(result.bbox.xmin, result.bbox.ymin, result.bbox.xmax, result.bbox.ymax)
    # None if the ID is unknown; GeocoderError(status=503) if the index is unavailable
```

### Health check

```python
client.health()  # {"status": "ok", "version": "..."}
```

### Convenience functions

```python
from overture_geocoder import geocode, reverse_geocode

results = geocode("Boston, MA")
divisions = reverse_geocode(42.36, -71.06)
```

## Configuration

```python
client = OvertureGeocoder(
    base_url="https://geocoder.bradr.dev",  # default
    timeout=30.0,        # request timeout in seconds
    retries=2,           # retry attempts (default: 2)
    retry_delay=1.0,     # base backoff delay; grows as retry_delay * 2^attempt with jitter
    headers={"X-Client-ID": "my-app"},
)
```

The client retries 5xx responses, timeouts, and network errors with exponential backoff, and retries HTTP 429 (rate limit) responses honoring the `Retry-After` header (capped at 30 seconds). The public service is rate limited to 60 requests/minute per IP.

## Errors

```python
from overture_geocoder import GeocoderError, GeocoderTimeoutError, GeocoderNetworkError

try:
    results = client.search("Boston")
except GeocoderTimeoutError:
    ...  # request timed out after retries
except GeocoderNetworkError as e:
    ...  # connection failure; e.cause has the underlying exception
except GeocoderError as e:
    ...  # HTTP error; e.status has the status code, e.response the httpx.Response
```

## Optional geometry extras

With `overture-geocoder[geometry]` installed (`overturemaps` + `shapely`), results can fetch their full geometry directly from Overture S3:

```python
results = client.search("Boston, MA")
feature = results[0].get_geometry()  # GeoJSON Feature with full polygon

divisions = client.reverse(42.36, -71.06)
divisions[0].verify_contains_point(42.36, -71.06)  # point-in-polygon check
```

## Development

```bash
pip install -e .[dev]
python -m pytest
```

The test suite is fully offline: unit tests mock HTTP, and contract tests run against saved production responses in `tests/fixtures/live/`.

## License

MIT
