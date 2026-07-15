# Places KV/R2 paged-index spike

Measured fixture facts, synthetic stress results, linear extrapolations, and documented prices are kept separate below.

- Input corpus: 1,768 Places
- Selected diagnostic layout: `uniform_256k` (256 KiB lexical, 256 KiB postings, 256 KiB results)
- At least one layout passed the fixture/co-located gates: True
- Fixture published bytes/place: 318.2
- Linear global release / two-release storage: 23.87 / 47.73 GB
- Extrapolation warning: not a forecast; token, language, field, fanout, and compression distributions can differ globally

KV stores only the active/rollback release pointer and format/page targets. Token hashes directly select one of 4,096 packed exact/prefix posting roots; heavy terms use deterministic overflow pages. Longer prefixes use a four-character lexical bucket and then complete exact-token chains. All overflow pages are traversed before ranking; there is no silent candidate cap.

## Page configurations

| layout | fixture bytes | objects | warm typical gate | warm worst gate | 500k co-located | 500k scattered |
|---|---:|---:|---:|---:|---:|---:|
| uniform_16k | 562,822 | 6,189 | False (7 ops/72,340 B) | True (7 ops/76,271 B) | False (94 ops/1,518,827 B) | False (103 ops/1,666,283 B) |
| uniform_64k | 562,654 | 6,182 | False (5 ops/153,867 B) | True (5 ops/141,836 B) | False (25 ops/1,566,188 B) | False (34 ops/2,156,012 B) |
| uniform_256k | 562,606 | 6,180 | True (3 ops/154,173 B) | True (4 ops/164,529 B) | True (8 ops/1,762,354 B) | False (17 ops/4,121,650 B) |
| hybrid_16k_256k_64k | 562,654 | 6,182 | False (5 ops/153,867 B) | True (5 ops/141,836 B) | True (8 ops/1,565,746 B) | False (17 ops/2,155,570 B) |

## Selected-layout fixture queries

| query | tier | ops warm/cold | bytes | amplification | candidates | recall |
|---|---|---:|---:|---:|---:|---:|
| starbucks_exact | typical | 2/3 | 153,690 | 1017.81x | 2 | True |
| warfield_hotel_tokens | typical | 3/4 | 154,173 | 260.87x | 2 | True |
| golden_gate_prefix | typical | 3/4 | 153,819 | 156.96x | 9 | True |
| hotel_category | typical | 2/3 | 154,122 | 123.69x | 125 | True |
| sf_cafe_context | worst_supported | 4/5 | 164,529 | 14.30x | 53 | True |
| starbucks_long_prefix | worst_supported | 3/4 | 153,745 | 844.75x | 2 | True |

## Synthetic overflow

The fixture is too small to stress overflow. A deterministic synthetic token shared by 500,000 documents produced one root plus 6 overflow pages. The reader accounted for 500,000 entries, so full traversal is `True`. With co-located top-10 results, warm operations/bytes were 8 / 1,762,354; gate pass: `True`. With top-10 scattered across ten full result pages, they were 17 / 4,121,650; gate pass: `False`.

The public API defaults to 10 results and permits up to 40. The scattered top-10 stress already fails the operation gate; result locality remains unproven and larger limits can only increase pressure.

## Publication-object sensitivity

A rejected one-object-per-term linear shape would produce about 262,160,633 objects per release. Packed roots avoid that direct scaling, but global payload/skew remain modeled:

| exact/prefix buckets each | modeled objects/release | monthly Class A for one release |
|---:|---:|---:|
| 4,096 | 99,236 | $0.00 |
| 65,536 | 222,116 | $0.00 |
| 1,000,000 | 2,091,044 | $9.00 |

The 4,096-bucket row is used in the cost table. It is a sensitivity model, not a demonstrated global build; a producer must fail if any rare-term bucket root exceeds its page cap.

## Monthly lower-bound cost model

Prices are Cloudflare documentation constants as of 2026-07-14. Worker CPU is unmeasured and excluded, so passing totals are not yet proof of the $30 ceiling; failing totals are decisive.

| queries/month | retained storage | Workers | R2 reads | R2 publish | R2 storage | KV | total lower bound | <=$30 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1,000,000 | 147.7 GB | $5.00 | $0.00 | $0.00 | $2.07 | $0.00 | $7.07 | True |
| 1,000,000 | 247.7 GB | $5.00 | $0.00 | $0.00 | $3.57 | $0.00 | $8.57 | True |
| 1,000,000 | 347.7 GB | $5.00 | $0.00 | $0.00 | $5.07 | $0.00 | $10.07 | True |
| 10,000,000 | 147.7 GB | $5.00 | $5.40 | $0.00 | $2.07 | $0.00 | $12.47 | True |
| 10,000,000 | 247.7 GB | $5.00 | $5.40 | $0.00 | $3.57 | $0.00 | $13.97 | True |
| 10,000,000 | 347.7 GB | $5.00 | $5.40 | $0.00 | $5.07 | $0.00 | $15.47 | True |
| 50,000,000 | 147.7 GB | $17.00 | $41.40 | $0.00 | $2.07 | $0.00 | $60.47 | False |
| 50,000,000 | 247.7 GB | $17.00 | $41.40 | $0.00 | $3.57 | $0.00 | $61.97 | False |
| 50,000,000 | 347.7 GB | $17.00 | $41.40 | $0.00 | $5.07 | $0.00 | $63.47 | False |

KV cache sensitivity at 50M queries and the 200 GB core scenario (0%, 1%, and 100% query reads):

| KV reads/query | KV cost | total lower bound | <=$30 |
|---:|---:|---:|---:|
| 0.00 | $0.00 | $61.97 | False |
| 0.01 | $0.00 | $61.97 | False |
| 1.00 | $20.00 | $81.97 | False |

## Verdict

At least one layout passes the fixture/co-located selection gates: `True`. The selected diagnostic layout passes its warm fixture gates: `True`. It passes the scattered-result stress: `False`. Therefore this spike does not establish production readiness even though fixture recall is complete and posting overflow is never truncated.

A cold catalog miss adds one KV operation and can break the three-operation typical gate. KV must remain tiny and cached; storing per-token postings or results there would invalidate the cost model.

At 1M and 10M monthly queries, the modeled 100–300 GB core plus two linearized Places releases remains below $30 before Worker CPU. At 50M queries, read operations and Worker requests exceed the ceiling. This design therefore needs an explicit traffic/CPU gate and aggressive cache evidence; it is not automatically affordable at arbitrary volume.

Official pricing: [R2](https://developers.cloudflare.com/r2/pricing/), [Workers KV](https://developers.cloudflare.com/kv/platform/pricing/), [Workers](https://developers.cloudflare.com/workers/platform/pricing/).

