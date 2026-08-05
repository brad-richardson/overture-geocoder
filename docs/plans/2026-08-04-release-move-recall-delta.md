# The Overture release move is not a quality lever

Measured 2026-08-04T23:55Z / 23:57Z against two complete local planet Places
builds, by
`benchmarks/probes/2026-08-04-release-move-recall-delta.py`. Evidence:
`benchmarks/2026-08-04-release-move-recall-delta-{everyday,gold}.json`.

`docs/plans/2026-08-04-measurement-apparatus-findings.md` §4 called the release
move from `2026-06-17.0` (what production serves) to `2026-07-22.0` "the largest
untested quality lever on the board", and framed it as the only change that adds
*data* rather than re-ranking what is already there. **That is now measured and
it is wrong on both counts.** The move is a redistribution, not an addition, and
its net effect on benchmark reachability is +1 case in 206.

## What was compared

Two full planet Places builds, run locally on the same machine through the same
driver, differing only in release:

| | `2026-06-17.0` | `2026-07-22.0` |
|---|---|---|
| places (map phase) | 75,642,289 | 74,223,561 |
| head candidate rows | 65,465,813 | 63,132,306 |
| merged head records | 33,604,005 | 32,871,728 |
| head index entries | 16,353,065 | 16,513,587 |
| populated shards | 4096 / 4096 | 4096 / 4096 |

The newer release carries **1,418,728 fewer places (-1.9%)** while emitting
**160,522 more distinct index entries (+1.0%)**. That combination — fewer
records, more tokens — is the shape of upstream conflation, and it was visible
before a single case was probed. It is also the reason a gains-only framing was
unsafe: a release move can lose what it does not gain.

The two contracts differ in exactly five scalar keys. Four are
`limits.places.duckdb_threads`, `duckdb_memory_limit`, `max_rss_bytes` and the
derived `request_sha256`; the driver documents those as speed-only and they do
not change output. The fifth is `release`. So the comparison is clean.

Build note worth keeping: the `2026-06-17.0` head completed 2026-08-04T23:36Z in
~2h with **zero DuckDB spill** (`peak_duckdb_temp_bytes: 0` over 9,262
observations) at `duckdb_memory_limit=40GB`. Both planet heads now exist on this
machine, which makes any future A/B of this shape a ~90-second question rather
than a build.

## The verdict ladder

Each case gets one verdict per release, independently, from the same function:

| verdict | meaning |
|---|---|
| `ABSENT` | no place near the expected point under a matching name |
| `NOT_ADMITTED` | in the corpus, but no head candidate emitted |
| `QUERY_REFUSED` | admitted, but the query exceeds `HEAD_QUERY_TOKEN_CAP` |
| `EVICTED` | admitted, loses the per-token cap in >= 1 query word |
| `SERVABLE` | admitted, survives the cap in EVERY query word |

The `EVICTED` / `SERVABLE` split is the methodological point.
`docs/plans/2026-08-04-head-miss-interrogation.md` collapsed both into
`IN_HEAD`, published "the largest actionable class needs no rebuild", and had to
retract it. `IN_HEAD` means admitted, not served, and the merge's cap sits
between the two.

## Result: +1 case in 206

Exact-name tier only. Weak (containment / alt-name) tiers are carried in the
evidence but excluded from the headline, because containment accepts an entity
merely *named after* the target.

| set | cases | `SERVABLE` @06-17 | `SERVABLE` @07-22 | gains | losses |
|---|---|---|---|---|---|
| everyday-POI 200 | 174 | 54 | 55 | 3 | 5 |
| forward gold 55 | 32 | 2 | 2 | 0 | 0 |
| **total** | **206** | **56** | **57** | **3** | **5** |

On all tiers including containment the move is clearly negative: 3 gains against
17 losses across both sets.

## Regression risk is real, and it is entirely CJK

All eight reachability flips are CJK, and losses outnumber gains:

```
GAIN  KR 중앙대학교병원              ABSENT   -> SERVABLE
GAIN  KR 하나효요양병원              ABSENT   -> SERVABLE
GAIN  JP メンズライフクリニック 新宿院    EVICTED  -> SERVABLE
LOSS  HK 沙田醫院                   SERVABLE -> ABSENT     <- live production HIT
LOSS  KR 서울대학교치과병원            SERVABLE -> ABSENT     <- live production HIT
LOSS  HK 東華醫院                   SERVABLE -> EVICTED
LOSS  HK 北大嶼山醫院                SERVABLE -> EVICTED
LOSS  HK 威爾斯親王醫院               SERVABLE -> EVICTED
```

Two of the losses are cases production answers correctly today. Five hospital
names present in June and degraded in July is either an upstream conflation
change or a names-field regression; which one is not settled here, and it is
worth knowing before any future release move.

A miss-only probe would have been structurally blind to every one of these,
which is why current hits were probed as a regression control rather than
assumed safe.

## The gold set is cap eviction, and it is release-invariant

31 of 45 gold cases are `EVICTED` in **both** releases, with **zero**
reachability flips and only two weak-tier movements
(`Hotel Sacher Vienna`, `Apple Marunouchi Tokyo`). The gold failures do not
move with corpus vintage at all.

This is independent confirmation, through a different instrument, of the one
conclusion that survived every correction on 2026-08-04: **the lever is
admission**, and it is rebuild-scoped. See
`docs/plans/2026-08-04-head-cap-eviction-ranks.md`.

## Calibration caveat that bounds every number above

Only **2 of 27** exact-tier gold production *hits* register as `SERVABLE`. The
other ~25 are served by the entity-phrase lane (`e2:`/`e3:`) or the prefix-head
fallback, neither of which this probe models.

So `SERVABLE` is a **lower bound on what production serves**, not a measure of
it, and the absolute level must never be quoted as "the system reaches only 2
gold cases". The *delta* is sound — both sides run through the identical
function in the same process, so the unmodelled lanes cancel — but the level is
not.

The instrument does cross-validate. `하나효요양병원` came back
`ABSENT -> SERVABLE`, independently reproducing the single case the head-miss
interrogation had identified as a release-skew artifact by a completely
different route.

## What this decides

1. **Drop the release move as a quality play.** +1 case against five
   regressions, two of them live, is not a lever. If `2026-07-22.0` is wanted
   for freshness or upstream currency that is a separate argument on its own
   merits; it should not be scheduled as a recall improvement and it should not
   gate anything.
2. **The `prominence_rank = 0` phrase-lane admission change is the sole
   remaining candidate**, and it is free to ride on the June corpus. It needs no
   release move ahead of it.
3. **The CJK degradation is worth its own bounded look** — five HK/KR hospital
   names lost between releases, two of them live hits.

## Limits of this measurement

* It models the **head token-intersection lane only**. The entity-phrase lane
  and the prefix-head fallback can serve a record this probe calls `EVICTED`.
* Candidate packs are already capped per map task, so `rank <= HEAD_RESULT_CAP`
  is exact — the decomposability property the cap key is built on — while a rank
  above it is a **lower bound**. The `SERVABLE`/`EVICTED` split only ever tests
  `rank <= cap`, so the split is sound; printed rank magnitudes are not.
* `QUERY_REFUSED` is release-invariant by construction and can never produce a
  delta. It is reported so it cannot masquerade as one.
* `ABSENT` means absent near the expected point under a matching name. A badly
  geocoded gold coordinate looks identical.
* Query tokenization mirrors the producer's word normalization; it is not the
  Worker's tokenizer executed in-process.
