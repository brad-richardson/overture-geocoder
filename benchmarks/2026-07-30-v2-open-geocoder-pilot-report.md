# V2 open-geocoder pilot — 2026-07-30

This is a plumbing and case-review pilot, not a quality baseline. It compares
Overture with the free public Nominatim and Photon services using the semantic
scoring added in PR #211.

Nominatim and Photon results are derived from
[OpenStreetMap](https://www.openstreetmap.org/copyright) data:
© OpenStreetMap contributors, [ODbL 1.0](https://opendatacommons.org/licenses/odbl/1-0/).
The public services are used only for this small, sequential ad hoc comparison.
Because both external services derive from OpenStreetMap, they are separate
implementations but not independent source datasets.

The cases are deliberately small:

- forward: six Monaco/Nice Places and four public Seattle Addresses;
- reverse: five Monaco Places and five Seattle Addresses.

They were selected from preserved Overture construction slices. Provider IDs
are not used for comparison scoring, but the selection and coordinates are
still Overture-derived. The sample is therefore too narrow and too correlated
with one provider to support a general quality claim.

## Execution

The run was sequential and ad hoc. Forward used its 1.2-second default
spacing. Reverse used its built-in 1.1-second Nominatim and 1.0-second Photon
floors.

```bash
python scripts/benchmark_v2_forward.py run \
  --cases benchmarks/v2-open-geocoder-forward-pilot-cases-v1.json \
  --skip-builtin \
  --provider overture --provider nominatim --provider photon \
  --output /tmp/v2-open-forward-pilot-results.json

python scripts/benchmark_v2_reverse.py \
  --base-url https://geocoder.bradr.dev \
  --cases benchmarks/v2-open-geocoder-reverse-pilot-cases-v1.json \
  --provider overture --provider nominatim --provider photon \
  --output /tmp/v2-open-reverse-pilot-results.json
```

Overture reported geocoder build `2026-07-28.0`. All 30 forward requests
returned HTTP 200. The final follow-up snapshot ran at
`2026-07-30T22:27:39Z`, after Worker deploy `30586786906` passed for merged
Address and Places fixes. Its result SHA-256 is
`011c2aede4168b9af8ecd0dba0722fe24a538e3808e96b8b9a4dbc2ec1e33796`;
the case-file SHA-256 is
`cf5e0c9996bb5580bf1c0bceb318bfe377522ffeb454c97c44a98ae304db010f`.

## Forward result

| provider | Places @1 | Places @10 | Places p50 | Addresses @1 | Addresses @10 | Addresses p50 |
|---|---:|---:|---:|---:|---:|---:|
| Overture | 1/6 | 3/6 | 295.9 ms | 4/4 | 4/4 | 1,303.8 ms |
| Nominatim | 3/6 | 3/6 | 8.0 ms | 4/4 | 4/4 | 24.5 ms |
| Photon | 3/6 | 4/6 | 141.6 ms | 4/4 | 4/4 | 140.4 ms |

This small sample shows:

- the bounded `state=WA&city=Seattle` compatibility bridge restored all four
  source-correlated Seattle Address cases at rank 1. This validates ordinary
  input handling, not global Address quality. Overture remains materially
  slower than the two public services on this one cold/warm mixed pass;
- the bounded locality-suffix planner raised Overture Places from 1/6 to 3/6 at
  rank 10. It returned Matisse Museum at rank 1, Stade Louis II at rank 3, and
  the explicitly biased ALDI case at rank 2. The three `name_locality` cases
  were therefore 3/3 at rank 10, while the three name-only cases remained 0/3.
  Name-only global retrieval and the division/POI seam are now the next
  measured forward quality priorities.

Forward Address semantic success here checks house number, street, and
distance; it does not require every returned postcode/locality/country field.
These counts should be read as diagnostic examples, not provider rankings.
The single-pass public latency values are especially unstable: upstream and
edge caches can dominate them, and the benchmark does not attempt cold-cache
control.

## Reverse result

No Overture point-family quality score is available yet. All five `poi`
requests returned:

```json
{"error":"capability_unavailable","message":"poi reverse data is unavailable"}
```

All five `address` requests returned the analogous
`address reverse data is unavailable` response. This matched the deployed
construction state at benchmark time. R2–R4 implementation is now merged and
fresh authenticated dry-runs have planned both families, but no reverse
catalog has been executed or published yet.

For harness validation only, the external providers produced:

| provider | Places @1 | Places @5 | Addresses @1 | Addresses @5 | HTTP errors |
|---|---:|---:|---:|---:|---:|
| Nominatim | 1/5 | 1/5 | 4/5 | 4/5 | 0 |
| Photon | 1/5 | 1/5 | 5/5 | 5/5 | 0 |

Nominatim reverse returns one result, so its @5 is necessarily identical to
@1. Photon has no portable generic POI layer equivalent; its Places row uses
the unfiltered reverse response. Those capability differences belong beside
any future reverse comparison.

## Reverse construction follow-up

After the pilot, fresh read-only reverse dry-runs succeeded on merged `main`
for the same authenticated construction request:

| family | workflow run | records | packs | plan SHA-256 |
|---|---:|---:|---:|---|
| Places | 30585252475 | 75,631,061 | 10,119 | `365c070577642badf03a40ad7def6b2450a65d4e455bef2f11dd44a0199f4729` |
| Addresses | 30585253742 | 431,705,590 | 5,174 | `a25c02d1bee5aec2c9a90395cc308d2702aec231ae374b0057232e0acde3ae67` |

Both plans bind request
`88b7f17149fd5d75bf64720f0640d2cbe8aeb5ead750d279c1881f9bd5332614`
to fresh destination `slice-2026-07-30.0`. They are planning evidence, not
permission to execute. Addresses still requires the reviewed representative
densest-cell dictionary and output-projection gate before operator
confirmation.

## Next benchmark rung

1. Keep these files as the smoke set for case/schema changes.
2. Curate an independent, manually adjudicated global set before making a
   provider-quality claim. A useful first baseline is roughly 20 Places and 20
   Addresses, balanced across regions, scripts, name-only/name-plus-locality
   queries, and address formats. Gold should come from open primary or
   government sources rather than any compared provider.
   Pin the requested language, retain normalized candidates for miss
   adjudication, and report point-biased cases separately: Overture and Photon
   accept point bias while Nominatim's closest portable option is a soft
   viewbox.
3. Fix the measured forward locality planning issue first, then the
   division/POI seam, rerunning this pilot after each scoped change.
4. Satisfy the Address densest-cell projection gate, then execute and deploy
   the reviewed reverse plans before spending requests on an Overture reverse
   quality comparison.
5. After manual miss adjudication, freeze the global semantic case set as v1
   and run it only on demand.
