# Live-service baseline for address and Places prototypes

Read-only observations from `https://geocoder.bradr.dev` on 2026-07-16. The
service reported data version `2026-07-13.0`.

| query | HTTP | Worker timing | observed result |
|---|---:|---:|---|
| health | 200 | 18 ms | healthy, version `2026-07-13.0` |
| `Boston` | 200 | 318 ms | Boston, MA locality first |
| `Tokyo` | 200 | 66 ms | 東京都 (`JP-13`) locality first |
| `Mexico City` | 200 | 0 ms | Ciudad de México (`MX-CMX`) locality first |
| `Starbucks Boston` | 200 | 0 ms | no results |
| `Tokyo Tower` | 200 | 86 ms | no results |

The first three queries pin the regional context used by the three-shard Places
smoke. The last two confirm the current production contract is still
division-only; they are a baseline, not evidence about prototype relevance.
The address and Places workflows use isolated Worker names and run-specific R2
prefixes and never alter this service or its catalog.
