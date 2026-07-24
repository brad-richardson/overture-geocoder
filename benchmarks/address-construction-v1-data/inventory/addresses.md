# Address row-group inventory and task-plan report

Release: `2026-06-17.0`

## Decision

The contiguous row-group plan uses **127 tasks** against the configured maximum of 128: **PASS**.

This is a footer-only planning result. Hosted execution and compact-output skew remain separate gates.

## Complete source inventory

- Objects: 32
- Source bytes: 21.899 GB
- Records: 473,576,753
- Row groups: 8,704
- Selected compressed column bytes: 16.706 GB
- Selected uncompressed column bytes: 33.173 GB
- Exact-country row groups: 8,023
- Mixed/unknown-country row groups: 681

## Planned task tails

| measure | min | p50 | p95 | max |
|---|---:|---:|---:|---:|
| rows | 154,296 | 3,965,016 | 3,998,407 | 3,999,690 |
| selected compressed bytes | 4,635,815 | 141,951,525 | 150,518,066 | 153,316,171 |
| selected uncompressed bytes | 9,087,576 | 284,146,267 | 292,882,928 | 296,773,842 |

## Largest exact-country populations observable from footer statistics

| country | rows in exact-country row groups |
|---|---:|
| US | 122,160,936 |
| BR | 88,876,419 |
| MX | 28,838,831 |
| IT | 23,963,864 |
| FR | 23,129,298 |
| JP | 19,484,686 |
| DE | 16,032,241 |
| AU | 15,562,541 |
| ES | 14,379,603 |
| CA | 14,316,403 |
| TW | 9,399,743 |
| NL | 7,839,931 |

## Remaining gate

Run small, median, large, and non-US planned ranges through projection, compact assembly, strict decode, and Worker reads. Footer statistics cannot establish retained-row coverage, heap, or compact-shard skew.
