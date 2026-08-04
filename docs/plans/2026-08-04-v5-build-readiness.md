# v5 build readiness

Date: 2026-08-04

Status: **DRAFT — not ready to build.** This document is the gate sheet for the
next Places construction generation (v5). It exists so that when the open items
below close, the build is a decision that has already been made rather than one
made under time pressure. Sections marked PENDING have a named owner artifact
and must be filled with measured values, not estimates.

Companion documents: `2026-08-04-benchmark-failure-modes-and-next-wave.md` (why
these changes), `construction-v1-state.md` (authoritative operational state).

## 1. Why v5 exists at all

v4 (`2026-08-03.0`, live since 2026-08-04T03:31Z) closed the bounded phrase
admission milestone. Measured post-promotion against the live build,
Overture-only:

| set | metric | pre-v4 (`2026-08-02.0`) | live v4 (`2026-08-03.0`) |
|---|---|---|---|
| 55-case gold | recall@1 | 0.473 | **0.527** |
| 55-case gold | recall@10 | 0.618 | **0.691** |
| 200-case everyday-POI | recall@1 | 0.310 | **0.325** |
| 200-case everyday-POI | recall@10 | 0.325 | **0.330** |

(The gold re-measure flagged a `name_locality` recall drop; it is entirely one
`Buckingham Palace London` read timeout, not a quality loss. Re-run before
citing that stratum.)

What v4 **cannot** fix, by construction, and therefore what v5 is for:

1. **≥4-token queries** never reach the index. `HEAD_QUERY_TOKEN_CAP = 3`
   returns empty before any read. `entity_phrase_key` also refuses >3 words, so
   no phrase key exists for four-word primary names.
2. **`prominence_rank == 0` entities** get no phrase key at all — ordinary POIs
   ("Dover MRT Station") stay starved even at 3 tokens.
3. **Instance-level fame does not exist in the shards.** `prominence_rank` is a
   category-class prior, so a replica in a "louder" category legitimately
   outranks the real landmark. No Worker change fixes this honestly.

Items 1 and 2 are *partly* addressable in the Worker (see §2); the residue is
what v5 must carry. Item 3 is v5-only and gated on the sidecar.

### 1.1 How much upside actually exists — measured, and smaller than assumed

`benchmarks/2026-08-04-everyday-poi-miss-classification-v1.json`
(reproduce with `scripts/classify_everyday_poi_misses.py`) attributes all 134
post-v4 misses:

| mechanism | misses | with open-data name evidence |
|---|---:|---:|
| empty, ≥4 tokens (blocked by the token cap before any read) | 40 | **3** |
| empty, ≤3 tokens (reached the index and still starved) | 70 | **15** |
| non-empty, wrong entity returned | 24 | **7** |
| **total** | **134** | **25** |

"Open-data name evidence" = an exact or fuzzy OSM name within 500 m of the
authority coordinate, or a Nominatim/Photon hit. **Only 25 of 134 misses have
any independent evidence that the entity exists in open data under the name we
queried.** The evidenced ceiling for this set is therefore ~0.455 recall@10,
not the ~0.6+ suggested by reading the token-cap class as pure upside. The
≥4-token class in particular — 40 cases, and the single loudest signal in the
raw data — carries evidence for only 3.

This changes the v5 argument. Sizing `e4:` keys or admission softening off the
raw miss count would buy far less than the count implies.

**Necessary honesty about that number**: absence of OSM/competitor evidence is
*weak* evidence of absence for Overture specifically — the same run resolves 33
cases OSM has no name for at all, which is the whole premise of a
commercial-feed geocoder. So "109 unevidenced" is not "109 absent"; it is "109
we cannot yet attribute." Settling it requires asking our own index whether it
holds a POI at the authority coordinate under a different name — a
proximity-biased probe.

**That probe is currently blocked**: `/v2/forward` with a `proximity` bias
returns Cloudflare 1101 (uncaught Worker exception) deterministically across
much of Asia and the equatorial band, including every Singapore, Mexico, and
Colombia coordinate in this case set. Fixing that defect is a prerequisite for
the measurement that sizes v5, not merely a serving bug. See §2 item 6.

## 2. What must land BEFORE v5 is specified

v5 admission must be sized by what the Worker-only fixes fail to reach — not by
the pre-fix miss list. Each item below changes the residue that v5 has to cover.

| # | Change | Scope | Status |
|---|---|---|---|
| 1 | Prefix-head fallback for 4–6-token empty queries | Worker | PENDING |
| 2 | Locality inference: alt-name divisions + bounded homonym retry | Worker | PENDING |
| 3 | Locality blend + centroid-distance near-tie demotion | Worker | PENDING |
| 4 | Exact-name small-radius assembly clustering | Worker | PENDING |
| 5 | Division population tie-break | Worker | Implemented, unmeasured |
| 6 | **Biased-search crash fix** | Worker | **SHIPPED 2026-08-04, verified live** |

Item 6 is done and was an active production outage, not merely a blocked
probe. `f64::clamp` panics when `min > max`, which is exactly what an
antimeridian-crossing shard bbox yields, so `distance_to_bbox`
(`crates/geocoder-core/src/routing.rs:190`) aborted any biased forward query
whose longitude fell inside such a shard's wrapped span while its latitude fell
outside its band — surfacing as Cloudflare 1101, or 1102 when the hung request
also burned the CPU budget. Confirmed in production via `wrangler tail`:
`min > max ... min = 19.879823684692383, max = -171.8721160888672`.

Crucially the `proximity` parameter was never required to trigger it: the text
lane builds the same bias from `CF-IPLatitude`/`CF-IPLongitude`, so ordinary
queries were failing for users across Japan, Australia, India, Southeast Asia
and East Africa. `/search` (v1) with `lat`/`lon` was equally affected; reverse
geocoding was not. Latent since 2026-07-16. Fixed in `cb4deaf`, deployed, and
verified against six previously-crashing coordinates.

Two blind spots let it live that long, and both are worth closing: every
benchmark case in both frozen sets carries no `proximity`, so the bias path had
zero coverage; and all local verification runs from a US IP, where the
predicate does not fire.

Follow-up queued (unreachable from current data, one bad bbox away from the
same outage): `routing.rs:174` `lat.clamp(min_lat, max_lat)` and
`reverse_construction_v1.rs:614` share the panic shape.

Items 1, 2, 4 and 5 are implemented and integrated on
`integration/wave-worker-fixes` (225 worker tests green, wasm32 clean) but
**none is measured**. Note the risk asymmetry: items 1 and 2 are additive and
fire only on otherwise-empty responses, so they cannot regress a non-empty
answer; items 4 and 5 *modify* non-empty responses and can. There is no Worker
preview environment — `preview-v2-candidate.yml` previews data slices, not code
— so measurement requires a production deploy with an immediate paired compare
and rollback readiness.

Gate: all five measured against both frozen sets with
`benchmark_v2_forward.py --compare`, paired no-loss, before the v5 residue is
computed. This is the "require the paired gate for Worker ranking/routing
changes" rule — RC3 regressed production precisely because a ranking change
shipped ahead of its measurement.

## 3. Candidate v5 contents (each independently justified, none yet approved)

### 3.1 `e4:` phrase keys for four-word primary names
Lets a 4-token query run **only** the phrase probe past the token cap; ordinary
head reads stay at ≤3 tokens. Directly addresses the "X Y MRT Station" class.

- Cost: head bytes. **Hard constraint: 31.7 MB formal reserve above the 25%
  headroom floor** (773,590,640 of 805,306,368 B). Must be measured on the
  scale probe before acceptance, not estimated.
- Blocked by: §2 item 1 — if the prefix-head fallback recovers this class at
  query time, `e4:` may be unnecessary and the bytes are better spent on 3.2.
- Decision input PENDING: post-fix residue count for ≥4-token cases.

### 3.2 Admission-gate softening (`prominence_rank > 0`)
Admits phrase keys for ordinary POIs. Largest byte cost of any candidate; also
the largest everyday-POI upside if the residue is dominated by
`prominence_rank == 0` entities.

- Decision input PENDING: of the post-fix everyday-POI misses, how many are
  `prominence_rank == 0` with a 2–3-word primary name. Requires joining the
  miss list against the producer's prominence assignment.
- Must be sized against the same 31.7 MB reserve as 3.1. **3.1 and 3.2 compete
  for the same budget** — this is the central v5 product decision.

### 3.3 Instance fame from the GERS↔QID sidecar
The only honest fix for the homonym/fame class (Colosseum replicas, Statue of
Liberty, Harrods vs the Depository, Times Square, Raffles, Manchester's POI
analogue).

- **Hard gate**: sidecar Phase 0 requires ≥200 independently hand-checked
  decisions with **zero false accepts**, then a broadcast byte/resident-memory
  measurement. The review instrument is built (see §5); the review is a human
  judgment that must not be self-certified by an agent.
- Any accepted sidecar that changes `prominence_rank` moves projection identity
  and construction evidence — so it lands as evidence generation **v5**, never
  as a patch to v4.
- Carrying accepted mappings forward across releases is unsafe without a delta:
  measured GERS source-assignment stability is 94.109151% (4,784,834 of
  81,224,863 comparable records reassigned between `2026-06-17.0` and
  `2026-07-22.0`).

### 3.4 Tokenizer / alias work
Apostrophe folding ("Childrens" ↔ "Children's" — the Royal Children's Hospital
is indexed 51 m from gold and hits rank 1 only with the apostrophe) and an
alias route for abbreviations ("GPO" ↔ "General Post Office" — target indexed
7 m away). Cheap relative to phrase keys; verify whether folding is Worker-side
(query normalization) or producer-side (index keys) before scheduling, since
that determines whether it needs v5 at all.

## 4. Standing prohibitions (do not relitigate inside v5)

- `head_result_cap` stays at 10 — contract-bound; any change needs spec-hash
  re-attestation sized against Wave C Q4, and required caps grow ~sqrt(n) at
  planet scale.
- Confidence never ranks (source-relative flat constants).
- The address partition key (`address_key_hash` / `route_hash` / `hash_bucket`)
  and `MAXIMUM_HASH_BITS` are FROZEN; the address map shuffle is not ported.
- DuckDB stays pinned at 1.5.1 for construction (1.5.5 moves address forward
  pack bytes and would drag Addresses into a Places-only generation).
- Anything touching the frozen evidence spec batches into ONE re-attestation
  pass. Never rewrite pins to match output.
- Fuzzy same-name merging stays disqualified (no threshold separates "Statue of
  Liberty" from "Statue of Liberty Deli").

## 5. Sidecar review instrument

Built 2026-08-04 so the Phase 0 audit can proceed asynchronously: the 200
frozen decisions joined to complete per-decision evidence, a resumable verdict
file bound to the candidate-set hash, a fail-closed validator that computes the
zero-false-accept gate, and a human-readable review sheet ordered risk-first.
Paths, schema, and exact commands: see §5 of the wave doc and the artifacts
committed alongside it.

The gate is **not met** until the review is done by a human and the validator
reports full coverage with zero false accepts. Partial review is "gate not
met", never "passed".

## 6. Operational readiness for the build itself

From the v4 session forensics — every item cost real wall-clock in the last
planet run and will be paid again:

- DuckDB spill allowances must be stated per stage and ≥ measured peak + 25%.
  The head temp peak is still unmeasured; v4's head merge died at 79% with the
  8.5 GiB half-share cap full and was recovered only by a scoped
  `head_only_resume` at three-quarters (13,690,208,256 B).
- Prefer targeted resume dispatches over GitHub's "rerun failed jobs" — it
  re-ran the entire 128-reducer matrix and broke the resume gate's
  expectations (mitigated by #237, but the semantics remain matrix-wide).
- Pin every workflow dependency at authoring time; two preview attempts were
  lost to an unpinned `worker-build` and a missing Python `requests`.
- promote-slice's ~4,096 serial marker reads take ~2h with no progress signal.
- Preview failures must self-classify as setup / operational-transient /
  quality-regression so retries are mechanical.

Status: **landed 2026-08-04**, with two corrections worth carrying forward.

- Workflow dependency pinning + a cold-start smoke (weekly + on workflow PRs)
  now exist, and a `tests/test_workflow_dependency_pinning.py` gate refuses
  unpinned installs and floating action refs. The #243 bug was found **still
  live on the production deploy path** (not only in preview), and a latent
  cold-start break was fixed in `release-slice-families.yml`, which mixed
  `download-artifact@v5` with `upload-artifact@v7`.
- **Correction to the earlier claim about `worker-build`:** the theory that
  0.8.x refuses `worker = "0.7"` is stale. The 2026-08-04 production deploy
  installed and built **0.8.5** against `worker = "0.7"` successfully — the pin
  is now 0.8.5 and must not be "restored" to `^0.7`, which would move the
  deploy off the only proven combination.
- **Correction to §6's promote-slice claim:** the ~4,096-marker serial loop is
  in `construction_v1_hosted.py::cmd_export_reductions`, not
  `promote_construction_slice.py` (whose HEAD loops were already parallel). It
  now emits progress every 128 markers and fans out bounded; safe because every
  request on that path is a read, so no create-only semantic is weakened.
- Preview failures now self-classify (setup / operational-transient /
  quality-regression) and write an acceptance document on failure instead of a
  bare traceback; the benchmark harness retries only definite transient codes
  (parsed 5xx, GET timeout) and never 4xx, with retry counts reported at row,
  aggregate and summary level so a retry can never launder a quality signal.
- **The DuckDB spill readiness check is red by design and not yet wired into a
  blocking workflow.** Every stage is currently unmeasured, and the v4 head's
  9,126,805,504 B is a lower bound from a run that *died*, not a peak. Wiring
  it before a first instrumented head completes would simply fail every
  construction run. Wire it with that first measurement.

Test/CI cost also came down: the pipeline suite runs **1773 tests in ~26 s**
(from ~175 s serial), with CI/deploy build caching added. The deploy's 15
consecutive startup probes, retries and functional checks are byte-for-byte
unchanged — verified by diff — so none of that speedup came from checking less.

## 6.1 Standing red signal, unrelated to this wave

`Smoke Test R2 Shards` has failed on every run since **2026-07-28** (last
success 2026-07-17), and it is not caused by the 2026-08-04 changes — the run
history predates them. It fails at `scripts/download_divisions_smoke.py:616`:

```
RuntimeError: Monaco hierarchy closure rows are missing:
['87aaf449-d1f6-49c1-ab5a-9fb8e50d85df']
```

The smoke runs `OVERTURE_RELEASE=2026-07-22.0` while production still serves
`2026-06-17.0`, so this is an early warning that the newer release breaks a
division hierarchy closure assumption. Settle it as part of the queued August
release re-audit: establish whether the closure invariant or the upstream data
changed, **before** any release move. Note separately that a red smoke went
unnoticed for a week — scheduled smoke failures appear to have no alerting.

## 7. Readiness checklist

v5 may be specified when every line is YES. It may be **built** when §2 is
measured and the product decision in 3.1-vs-3.2 is made explicitly.

- [ ] §2 items 1–5 landed, paired-gated, no losses on either frozen set
- [ ] Post-fix residue classified: ≥4-token vs `prominence_rank == 0` vs
      absent-from-source, with counts
- [ ] 3.1 and 3.2 byte costs measured on the scale probe against the 31.7 MB
      reserve; the competing-budget decision made and recorded
- [ ] Sidecar Phase 0 gate met by human review (zero false accepts) and
      broadcast memory measured — or 3.3 explicitly deferred out of v5
- [ ] Evidence spec v5 drafted as ONE re-attestation pass
- [ ] §6 operational items landed or explicitly accepted as risk
- [ ] Rebuild scope restated: families, DuckDB pin, reverse rebuild yes/no,
      `head_result_cap`, promotion copy mode
