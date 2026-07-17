<!-- Provenance: workflow rehearse-address-r2-map-reduce.yml, dispatch run 29585075948 (main @ 474856d, PR #99), release 2026-06-17.0, aggregate artifact address-sweep-aggregate-29585075948-1. Non-promoting, run-unique prefixes, cleanup verified. -->

# Stratified address sweep aggregate

- Release: `2026-06-17.0`
- Tasks completed: **12/12**
- Byte-identical local oracle across all tasks: **yes**
- Rows reconciled vs inventory: **12/12**

## Per-task metrics

| task | idx | stratum | status | retention % | frag bytes | map s | reduce s | peak RSS | retry amp | B/row | oracle |
|---|---|---|---|---|---|---|---|---|---|---|---|
| mexico-full | 3 | continuity-anchor | complete | 40.29 | 260,214,199 | 78.88 | 44.36 | 798,449,664 | 2.02 | 161.49 | yes |
| mixed-country | 8 | mixed-unknown | complete | 77.17 | 400,849,088 | 132.76 | 112.90 | 808,321,024 | 2.03 | 130.75 | yes |
| us-mid | 10 | us-mid-range | complete | 100.00 | 483,379,607 | 149.05 | 145.64 | 852,193,280 | 2.03 | 122.14 | yes |
| brazil-full | 16 | latin-high-density | complete | 80.85 | 455,005,068 | 125.45 | 112.43 | 815,632,384 | 2.04 | 143.06 | yes |
| us-full | 48 | continuity-anchor | complete | 100.00 | 541,958,847 | 147.30 | 141.94 | 869,634,048 | 2.03 | 135.52 | yes |
| france-full | 75 | latin-europe | complete | 99.20 | 530,503,576 | 138.06 | 135.74 | 770,777,088 | 2.03 | 134.21 | yes |
| italy-full | 84 | latin-europe | complete | 99.65 | 512,841,969 | 112.37 | 98.80 | 800,919,552 | 2.03 | 129.53 | yes |
| germany-full | 85 | latin-europe | complete | 99.95 | 465,879,792 | 147.72 | 147.70 | 797,122,560 | 2.03 | 117.14 | yes |
| taiwan-full | 105 | cjk-traditional | complete | 99.83 | 288,061,064 | 76.49 | 73.67 | 804,786,176 | 2.06 | 140.30 | yes |
| japan-full | 117 | cjk-japan | complete | 99.47 | 514,511,246 | 150.13 | 150.51 | 822,337,536 | 2.03 | 130.19 | yes |
| japan-pure | 121 | cjk-japan | complete | 100.00 | 270,538,839 | 49.35 | 43.87 | 713,347,072 | 2.07 | 130.40 | yes |
| sparse-tail | 126 | sparse-tail | complete | 100.00 | 21,624,419 | 4.41 | 4.36 | 461,201,408 | 2.83 | 140.15 | yes |

## Distribution across completed tasks

| metric | min | median | p95 | max | n |
|---|---|---|---|---|---|
| Retention % | 40.29 | 99.74 | 100.00 | 100.00 | 12 |
| Fragment bytes | 21,624,419.00 | 460,442,430.00 | 535,658,447.95 | 541,958,847.00 | 12 |
| Map wall s | 4.41 | 129.11 | 149.54 | 150.13 | 12 |
| Reduce wall s | 4.36 | 112.66 | 148.96 | 150.51 | 12 |
| Peak RSS bytes | 461,201,408.00 | 802,852,864.00 | 860,041,625.60 | 869,634,048.00 | 12 |
| Retry amp | 2.02 | 2.03 | 2.41 | 2.83 | 12 |
| B/retained row | 117.14 | 132.48 | 151.35 | 161.49 | 12 |

