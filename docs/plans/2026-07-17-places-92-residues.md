# PR #92 residues: coalescing-branch coverage and clause_candidate_counts contract

Date: 2026-07-17
Status: scoped, test-heavy follow-up to #92 ("Bound Places cold posting reads
and early-exit empty intersections"). Small PR. Implementation is chained
behind the famous-unique head-admission PR (same file territory:
`places_pages.rs`, smoke prep, fixtures).

## Residue 1: untested multi-entry posting coalescing branch

#92 replaced the single `[first,last]` posting-span read with per-matched-entry
ranges through `RangeReader::coalesced(..., gap=0, ...)`
(`places_pages.rs` ~810–931). The branch that matters — a clause matching
several NON-adjacent lexicon entries, where gap-0 coalescing must split into
multiple physical reads and stitch per-entry postings back in order — has no
dedicated test. A regression here (mis-zipped `matches`/`posting_chunks`,
wrong split, dropped entry) would silently change candidate sets.

**Work:** add a fixture case (Python fixture generator + Rust fixture test +
producer-side unit test) with a prefix clause matching ≥ 2 lexicon entries
separated by at least one non-matching entry, asserting: (a) candidate doc-set
equality with the brute-force oracle, (b) the physical read plan actually
splits (read count > 1 for the postings stage), and (c) byte accounting
excludes the dead gap. Also cover the adjacent-entries case (must merge to one
physical read at gap 0).

## Residue 2: clause_candidate_counts drift between Worker and oracle

Verified divergence:

- Python oracle (`experiment_places_compact_shard.py:523–530`): counts every
  clause's candidates unconditionally — no early exit.
- Worker (`places_pages.rs`): #92 skips ALL posting reads when any clause has
  no lexicon match, and breaks the clause loop once the running intersection
  is empty (line ~928). Later clauses' entries in `clause_candidate_counts`
  stay 0 even when their postings are nonempty.

The expected-case JSON carries the oracle's counts
(`prepare_places_worker_smoke.py:136`); the workflow does not currently assert
them, so the drift is latent — but any future assertion, or a human comparing
diagnostics, reads numbers with two different meanings.

**Work:** make the contract explicit and identical on both sides. Recommended
semantics: `clause_candidate_counts[i]` is "candidates actually decoded for
clause i", with a sentinel (`null`) for clauses never read due to early exit —
mirrored exactly by the oracle (model the same skip/break rules). Alternative
(if a sentinel churns too many fixtures): keep numeric zeros but document the
early-exit meaning in both implementations and the smoke prep. Either way:

- implement the chosen rule in BOTH `CompactShard.query` and the Worker;
- add an empty-intersection multi-clause smoke case (clause with matches ∧
  clause with none, and a no-lexicon-match variant) pinning the agreed counts;
- state the semantics in a comment at both sites so the next reader knows the
  field is a diagnostic, not candidate recall.

## Acceptance gates

1. New tests fail against a deliberately reintroduced #92-style bug (verify
   by mutation locally, not in CI).
2. All existing oracle-equality, fixture, and workflow-contract tests pass;
   result IDs/order/projections unchanged for every existing case.
3. Python and Rust produce identical `clause_candidate_counts` for every
   smoke case, including the new early-exit cases.

## Non-goals

No read-path behavior changes beyond the counts contract (the records-stage
work is `2026-07-17-places-chain-name-records-stage.md`), no gate changes, no
new query capabilities.
