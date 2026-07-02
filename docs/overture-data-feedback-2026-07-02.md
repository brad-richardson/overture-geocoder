# Overture divisions: duplicate locality entities (release 2026-06-17.0)

Found while benchmarking this geocoder; worth reporting upstream
(github.com/OvertureMaps/data), since GERS's core promise is one stable ID
per real-world entity.

## Method

Scanned `theme=divisions/type=division`, `subtype='locality'`,
`population >= 50000`. Paired records sharing (primary name, country,
region), computed centroid distance per pair. Raw pairs:
`overture-duplicate-localities-2026-06-17.csv` (40 pairs).

Pairs hundreds of km apart are mostly *legitimately* distinct same-named
places and should not be reported: 城关镇 ×7 in Gansu (generic county-seat
name), 城区 / 铁西区 district names reused across Chinese cities, Rosario and
Roxas in the Philippines, Палилула in both Belgrade and Niš.

## Likely true duplicates (distinct GERS IDs, same place)

Exact-coordinate twins:

| name | region | populations | note |
|------|--------|-------------|------|
| Villa Mercedes | AR-D | 111,391 / 111,391 | identical |
| San Ignacio Guazú | PY-8 | 50,468 / 50,468 | identical |
| 北區 (Hsinchu) | TW-HSZ | 152,332 / 152,332 | identical |
| Hoover | US-AL | 92,606 / 92,606 | identical |
| Saint Cloud | US-MN | 67,109 / 72,145 | conflicting pop |
| Guéckédou | GN-N | 290,611 / 221,715 | conflicting pop |
| Randburg | ZA-GP | 335,000 / 335,000 | identical |

Near twins (<15 km):

| name | region | populations | km apart | note |
|------|--------|-------------|----------|------|
| Kissimmee | US-FL | 60,894 ×4 | 0.8–7.2 | **four** records |
| ביתר עילית (Beitar Illit) | XW | 72,412 / 58,985 ×2 | 0.4–0.9 | three records |
| الشيخ زايد (Sheikh Zayed) | EG-GZ | 80,000 / 383,000 | 0.5 | conflicting pop |
| بٹ خیلہ (Batkhela) | PK-KP | 68,200 / 50,688 | 0.5 | |
| Bad Kreuznach | DE-RP | 52,385 / 155,300 | 0.7 | likely city vs district as locality |
| חדרה (Hadera) | IL-HA | 102,223 / 102,223 | 1.9 | |
| Tuban | ID-JI | 88,025 / 1,266,396 | 2.3 | likely city vs regency as locality |
| Xai-Xai | MZ-G | 116,343 / 56,489 | 5.3 | |
| Hanumangarh | IN-RJ | 151,100 / 129,700 | 6.1 | may be Town vs Junction (real twin towns) |
| بغلان (Baghlan) | AF-BGL | 119,607 / 119,607 | 8.6 | |
| ပြင်ဦးလွင် (Pyin Oo Lwin) | MM-04 | 255,000 / 255,000 | 69.9 | same pop; one centroid likely misplaced |

Two flavors worth distinguishing upstream: (a) genuine double-ingestion
(identical coordinates and population), (b) different admin levels both
tagged `subtype=locality` (Tuban city vs regency, Bad Kreuznach city vs
Landkreis) — arguably a subtype bug rather than a duplicate.

## Build-time dedup strategy (until fixed upstream)

In `build_shards.py`, after enrichment:

1. Group by (normalized primary name, country, region).
2. Within a group, cluster records whose centroids are within 15 km or
   whose bboxes overlap.
3. Collapse each cluster to one record: keep the highest-importance member,
   take max(population), union the bboxes, merge search aliases.
4. Log dropped GERS IDs so the dedup is auditable and reversible.

The distance guard keeps genuinely distinct same-named places (the 城关镇 /
Rosario class) intact. Cost: one window query over ~100k enriched rows —
negligible.
