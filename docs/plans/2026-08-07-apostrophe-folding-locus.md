# Apostrophe folding: Worker-side or producer-side? — 2026-08-07

`2026-08-04-v5-build-readiness.md` §3.4 deferred the tokenizer work with an
explicit instruction: *"verify whether folding is Worker-side (query
normalization) or producer-side (index keys) before scheduling, since that
determines whether it needs v5 at all."* That was never done, and the 2026-08-06
variant stratum then measured the class at **0/5 for both spellings** — worse
than either lane predicts alone, which is itself a signal that two things are
wrong rather than one.

**Answer: both, and the halves are separable.** One ships without a rebuild; the
other belongs in v5 and costs less than the `e4:` keys already adopted.

Probe: `benchmarks/probes/2026-08-07-apostrophe-locus-probe.py`.
Result: `benchmarks/2026-08-07-apostrophe-locus-v1.json`.

## 1. The mechanism, and the part that turns out not to be the problem

The apostrophe is a plain separator in `normalized_words`, and the Worker mirrors
that byte-for-byte (`places_transform_v1.rs` ↔ `places_pages.rs`). So
`Len's Mill Store` indexes as `[len, s, mill, store]` and the two spellings of
one name produce **disjoint token sets**.

Two consequences, and only one of them is what actually refuses these queries:

- **The bare `s` posting is already survivable.** The saturated-posting
  relaxation in `places_construction_v1.rs` admits a record that is absent from a
  saturated posting when the token appears in its display tokens. `Domino's
  Pizza` returns a result today, live, and it carries exactly that shape.
- **The token cap is what refuses them.** `HEAD_QUERY_TOKEN_CAP = 3` rejects
  before any read, and the possessive spends a slot. `Len's Mill Store` is four
  tokens; `Queen's Medical Center` is four.

This distinction matters because it is the difference between a scoring fix and
a cap-accounting fix, and only the second one is real.

## 2. What the corpus says

Overture places `2026-06-17.0`, the release production serves:

| | records |
|---|---|
| names containing an apostrophe | 2,553,035 |
| …that produce a ≤1-char token | 2,342,346 |
| …that emit a bare `s` | 1,858,191 |
| …over the 3-token head cap **today** | 1,565,166 |
| …over the cap **if the apostrophe were elided** | 831,465 |
| **…pushed over the cap only by the split** | **733,701** |
| phrase-key eligible today (2–3 words) | 972,366 |
| phrase-key eligible if elided | 1,472,322 |

**733,701 records are unreachable at their own full name purely because the
apostrophe spends a token-cap slot**, and eliding it would move 499,956 more
records into phrase-key range.

## 3. What production says

Paired live requests, same target, only the apostrophe differing:

| apostrophe-typed | result | ASCII-typed | result |
|---|---|---|---|
| `Domino's Pizza` | Domino's Pizza Broken Hill | `Dominos Pizza` | **empty** |
| `Queen's Medical Center` | **empty** | `Queens Medical Center` | Queens Medical Center |
| `Women's Health Center` | NRMC Women's Health Center | `Womens Health Center` | Womens Health Center |
| `Shriners Children's Hawaii` | **empty** | `Shriners Childrens Hawaii` | **empty** |
| `Len's Mill Store` | **empty** | `Lens Mill Store` | **empty** |

Read the first two rows together: they fail in *opposite* directions. Three
tokens with an apostrophe works; four does not. An ASCII-typed query never
reaches an apostrophe-named record, and the ASCII hits above are **different
records that happen to be spelled without one** — the spelling-islands effect
the variant stratum already found.

## 4. The split, and what each half costs

### Worker-side, ships without a rebuild

**An apostrophe-born degenerate token must not consume a token-cap slot.** The
index already holds `[len, s, mill, store]`, so an AND over `[len, mill, store]`
can retrieve the record; the query simply has to get under the cap to ask.

Scope it to apostrophe-born tokens, **not** to short tokens generally.
**5,071,193 records carry a ≤1-char token with no apostrophe anywhere** — `H&M`,
`A&W`, initials, single CJK characters. `H&M` is retrievable today *only*
because both single-letter postings survive, so a blanket "drop 1-char tokens"
rule would break a working class to fix a broken one.

This is a retrieval-path change, so the standing rule applies: it ships behind a
paired measurement on both frozen sets, and now also the proximity stratum.

### Producer-side, belongs in v5

Index the elided form as an additional token, so `dominos` reaches
`Domino's Pizza`. No query-time rule can do this: synthesizing `domino + s` from
`dominos` would fire on every ordinary plural (`gardens` → `garden` + `s`) and
inflate postings for a guess.

| | |
|---|---|
| distinct new index tokens | 296,553 |
| new (record, token) rows | 2,424,496 |
| new head records after the cap of 10 | 666,637 |
| new head bytes | 113,415,307 |
| **head growth** | **+1.98%** |

For comparison, the `e4:` keys adopted on 2026-08-07 cost +3.0%. **Apostrophe
folding is the cheaper of the two and should ride the same generation.**

## 5. Recommendation

1. Ship the Worker half now, behind the paired gate — it needs no rebuild and it
   is the half that unblocks 733,701 records at their own spelling.
2. Add the producer half to the v5 bundle beside `e4:` keys, at +1.98%.
3. Keep the rule apostrophe-scoped in both halves. The 5.07M non-apostrophe
   degenerate tokens are load-bearing.
4. Re-measure the variant stratum's five apostrophe cases after each half. Today
   they are 0/5 both ways; the two halves should move different columns, and if
   they do not, the mechanism above is wrong.

## 6. Limits

- The corpus counts are **denominators, not failure rates**. A record with an
  apostrophe fails only for the spelling a user actually types, and this probe
  does not know that distribution.
- The five live pairs are spot checks against one build on one day. They
  demonstrate the mechanism exists in both directions; they do not size it.
- `733,701 rescued` counts records whose *own full name* becomes askable. It is
  not a promise that each one then ranks: the head cap of 10 still arbitrates,
  and `Len's Mill Store` fails its ASCII spelling for a second reason (both of
  its remaining tokens are commodity words).
- The producer cost prices head records at the same 170.131 B/record as
  `2026-08-07-phrase-admission-sizing.md`, from the same local build, and
  likewise ignores head-build DuckDB spill.
