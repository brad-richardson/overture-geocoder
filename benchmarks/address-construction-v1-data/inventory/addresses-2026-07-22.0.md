# Address row-group inventory and task-plan report

Release: `2026-07-22.0`

## Decision

The contiguous row-group plan uses **126 tasks** against the configured maximum of 128: **PASS**.

This is a footer-only planning result. Hosted execution and compact-output skew remain separate gates.

## Complete source inventory

- Objects: 32
- Source bytes: 21.882 GB
- Records: 472,703,893
- Row groups: 8,704
- Selected compressed column bytes: 16.691 GB
- Selected uncompressed column bytes: 33.117 GB
- Exact-country row groups: 8,019
- Mixed/unknown-country row groups: 685

## Planned task tails

| measure | min | p50 | p95 | max |
|---|---:|---:|---:|---:|
| rows | 1,656,476 | 3,969,609 | 3,995,527 | 3,999,709 |
| selected compressed bytes | 46,101,291 | 142,171,737 | 150,658,881 | 151,544,689 |
| selected uncompressed bytes | 89,311,482 | 284,048,323 | 293,408,714 | 295,715,641 |

## Largest exact-country populations observable from footer statistics

| country | rows in exact-country row groups |
|---|---:|
| US | 120,862,596 |
| BR | 88,755,205 |
| MX | 29,235,017 |
| IT | 24,039,472 |
| FR | 22,730,149 |
| JP | 19,540,996 |
| DE | 16,227,374 |
| AU | 15,564,728 |
| CA | 14,347,695 |
| ES | 14,141,079 |
| TW | 9,415,086 |
| NL | 7,784,754 |

## Remaining gate

Run small, median, large, and non-US planned ranges through projection, compact assembly, strict decode, and Worker reads. Footer statistics cannot establish retained-row coverage, heap, or compact-shard skew.
