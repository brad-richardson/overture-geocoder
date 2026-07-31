# Promotion without a copy, and other efficiencies for the next planet run

Status: **recorded and deferred.** Nothing here blocks the 2026-07-31.0
promotion or the next measured milestone. This is the queue to draw from once a
planet blocker is not open, per `CLAUDE.md` §1.

Every number below is MEASURED from the 2026-07-31 promotion of
`slice-2026-07-30.0` unless marked ESTIMATED. Sources: dry-run
`30605936253`, execute `30629402228`, and the two reverse runs `30595904973`
(Places) / `30599227663` (Addresses).

## 1. Promotion should not copy any bytes

### What it does today

`promote-slice` server-side `CopyObject`s every forward serving object from the
construction namespace into the release slice namespace:

```
construction-v1/8655821.../slice/slice-20260726145910-dryrun/families/{family}/objects/<sha256>.<ext>
  ->  slice-2026-07-30.0/families/{family}/objects/<sha256>.<ext>
```

Both keys are already content-addressed by the object's own sha256 —
`object_name()` in `scripts/promote_construction_slice.py` refuses a store key
whose basename is not `<sha256><ext>`. The copy therefore changes the prefix and
nothing else.

| | objects copied | bytes copied |
|---|---|---|
| Places | 20,698 | 45.03 GiB |
| Addresses | 581 | 113.65 GiB |
| **total** | **21,279** | **158.68 GiB** |

(Family totals from the plan are 52.85 GiB / 135.5 GiB; the reverse halves,
7.82 GiB and 21.85 GiB, are prepositioned and not copied.)

Two costs follow:

- **Wall clock.** The copy is the long pole of the whole promotion.
- **Storage.** Nothing deletes the construction-side original. `r2-cleanup.yml`
  is manual and prefix-gated by design, so both copies persist until an operator
  removes one. 158.68 GiB is a large fraction of the post-cleanup bucket.

The dollar cost of the operations themselves is small — the problem is wall
clock and duplicated bytes, not the class-A charges.

### The model to copy: reverse already does this

`reverse-v2.yml` takes `slice_version` as a dispatch input and writes its
immutable shards **directly** into
`{slice_version}/families/{family}/reverse/shards/sha256/<sha256>.plrx`, marker
last. `promote-slice` then treats them as *prepositioned*: `_reverse_publication`
validates the exact set, `_verify_prepositioned` proves each destination
identity, and the objects are bound into the family manifest without being
touched. 20,775 reverse objects / 29.67 GiB moved zero bytes at promotion time
this run.

This is also what the v2 catalog already does one layer up. From
`docs/v2-release-catalog-contract.md`: *"The v2 release references existing
immutable keys; it does not copy division or ID objects."* Forward Places and
Addresses are the only things in the system that still get copied.

### The change

Have construction's finalize phase publish forward serving objects create-only
into the release slice namespace under their final content-addressed names, and
have `promote-slice` bind them as prepositioned — the code path already exists
and is exercised every run by reverse.

This extends, rather than duplicates,
`2026-07-28-planet-build-wall-clock-review.md` §7 ("Write final immutable
objects once"). That item removes the *finalize* staging round trip and lands
objects in the **construction** slice namespace, which is what the request
digest freezes. The promotion copy survives it. Closing both means the
destination has to be the **release** slice namespace.

Late binding is the crux, and reverse shows how: the release slice version is
chosen after the forward build, so it cannot be frozen into `namespaces.slice`
at request time. Reverse takes it as a dispatch input and restores the
authenticated binding with the per-family slice claim at
`{version}/claims/{family}.json`, whose payload binds `version`, `family`,
`request_sha256`, and `overture_release`. Forward can use the same claim.

**What must be preserved.** Be explicit about what the copy currently buys, so
the rework does not quietly weaken it:

- *Independent byte fidelity.* Today the destination's own single-part ETag is
  compared against the plan-recorded source MD5, which is what makes the
  post-copy check a byte proof rather than a metadata echo (see the `R2Tree`
  docstring). A prepositioned object is proved instead by its producer-recorded
  identity plus a destination HEAD. Forward objects are content-addressed by
  sha256 exactly as reverse's are, so the same key-equals-digest check applies —
  but the chain is genuinely different and should be reviewed, not assumed
  equivalent.
- *Namespace hygiene.* A failed or abandoned construction run would leave debris
  in the release namespace instead of a disposable construction prefix.
  Marker-last plus the per-family claim plus `_admit_slice` keeps it
  non-promoting, but §7 of the wall-clock review is right that this needs
  explicit cleanup for unmarked namespaces before the path is taken.

## 2. Three serial HEAD loops on the promotion critical path

Independent of item 1, and much cheaper to fix. `promote_construction_slice.py`
issues roughly **84,000 sequential HEAD requests** across three loops:

| loop | requests | code |
|---|---|---|
| plan: source identity per object | 21,279 | `_plan_family`, `for item in objects: source.identity(...)` |
| execute: prepositioned verification | 20,777 | `cmd_execute`, `for item in prepositioned: _verify_prepositioned(...)` |
| verify: per-key destination identity | 42,058 | `cmd_verify`, `for key in listed: destination.identity(key)` |

The plan loop is measured twice on identical input, and it is the only
meaningful R2 traffic that step performs:

- dry-run `30605936253`: **17m10s** → 48.4 ms/HEAD
- execute `30629402228`: **36m21s** → 102.5 ms/HEAD

So one of the three loops alone costs 17–36 minutes, varying ~2x with network
conditions, for work that is embarrassingly parallel over immutable keys.
`COPY_WORKERS` already provides the pool; these loops just do not use it.
ESTIMATED: all three at 8–16 way concurrency reclaims something close to an hour
of promotion wall clock.

Item 1 deletes the first two loops outright. The third survives it, because
verify must still prove the destination.

## 3. `COPY_WORKERS = 4` has never been sized

`scripts/promote_construction_slice.py:97`. Addresses copies 581 objects
averaging ~196 MB each; a 113.65 GiB server-side transfer four at a time is not
obviously the right point. Each task owns one immutable destination key, so
concurrency is safe for correctness — the ceiling is R2 throughput, which has
not been probed. Worth a bounded probe if item 1 does not land first, and
worthless afterwards.

## 4. `promote-slice` runs both families serially in one job

The two families are independent up to the slice manifest: separate plans,
separate objects, separate destinations. A 2-way matrix with the manifest write
in a dependent job would halve the promotion critical path while keeping the
manifest single-writer and strictly last. The manifest is already a separate
workflow step, so the seam exists.

## 5. Verify re-HEADs what the list response already partly answers

`cmd_verify` lists the destination prefix and then HEADs every key — 42,058
round trips. `ListObjectsV2` returns `Size` and `ETag` for every key in ~42
pages. It does **not** return the sha256 user metadata, so this cannot be a
drop-in replacement.

Worth noting that the sha256 metadata is the *weaker* of the two signals on the
copy path — after `CopyObject` with `MetadataDirective: COPY` it is an echo of
the source, and the ETag is what proves the destination's stored bytes. A
list-derived verify would keep the load-bearing check for every key and reduce
the metadata check to a sample or to the non-copied members. Record as
*investigate*, not *do*: it trades a per-key proof for a per-page one and needs
a deliberate decision, not an optimization reflex.

## 6. Reduction records should not live only in GitHub artifacts

Not a speed item, but it bit us today and belongs in the queue.

`promote-slice` authenticates reduction records by downloading `cv1-reduce-*`
(or, for a finalize-only recovery run, `cv1-resume-reductions`) from the
construction run's GitHub artifacts. Those retain 7 days. That produced a hard
promotion deadline of 2026-08-02T20:38Z, and commit `8aea031` had to be written
mid-flight because the Places source was a finalize-only run carrying no
`cv1-reduce-*` at all.

Writing the reduction set durably into the construction namespace alongside the
markers — where every other authenticated input already lives — removes the
deadline, the artifact-shape coupling, and the recovery-run special case in one
move. Promotion would then depend only on R2, which is where the data it
promotes lives.

## 7. Reverse ran 16 ranges at `max_parallel=2`; the workflow already allows 4

Both planet reverse runs were dispatched at `max_parallel=2` over a fixed
16-range matrix — eight sequential waves. Places took **2h15m**, Addresses
**2h33m**, about 4h48m combined.

`reverse-v2.yml` already offers `max_parallel` options `["1", "2", "4"]`, so
4-way needs **no code change and no new request identity**. Ranges are disjoint
bucket ranges writing to disjoint content-addressed keys, so the correctness
argument is the same one that already justifies 2-way. ESTIMATED: four waves
instead of eight, roughly halving both runs.

This item did not exist when `2026-07-28-planet-build-wall-clock-review.md` was
written; reverse had not run at planet scale.

## 8. Reverse does not depend on forward reduce, head, or finalize

`scripts/reverse_r2_v1.py` states it: *"Reverse R2 consumes the per-record
artifacts already emitted by the forward map."* Task discovery is from durable
map markers (#206) bound to admitted construction tasks (#207). The one place it
touches a forward family manifest is `_admit_slice`, and that is a **negative**
check — it refuses a destination slice that is already finalized.

So the dependency is `map -> reverse`, not `map -> plan -> reduce -> head ->
finalize -> reverse`. Today reverse is a separate manual dispatch that runs
strictly after the whole forward build.

For Places the overlap window behind map is plan 60.8 + reduce 213.3 + head
206.4 + finalize 57 ≈ **8.9 hours**, against a 2h15m reverse run. Sequencing
reverse to launch on map completion would take it off the end-to-end critical
path entirely.

Two caveats before anyone schedules it:

- Reverse's fan-in needs the **complete** map marker set, so it starts after map
  finishes, not while maps are still running. The window is real but it begins
  at the map barrier.
- Reverse writes into the destination slice under a per-family claim, and
  `promote-slice` writes the slice manifest last. Overlapping does not violate
  marker-last, but the interaction with a *failed* forward build — reverse
  output already in a slice that forward never reaches — needs the same unmarked
  namespace cleanup rule item 1 requires.

## 9. The copy client cannot retry a *definite* 5xx, and that cost 4h17m

MEASURED. Run `30629402228` failed at 16:24:02Z after 4h17m, 42 minutes inside
its 300-minute job timeout:

```
botocore.exceptions.ClientError: An error occurred (InternalError) when calling
the CopyObject operation (reached max retries: 0): We encountered an internal
error. Please try again.
```

Places had already fully succeeded — 20,698 copied, 16,528 prepositioned
verified, routing and family manifest written at 14:45:18Z, and an independent
verify of 37,228 objects / 56,754,975,464 bytes at 15:51:55Z. The whole run was
discarded by one transient error on the Addresses copy.

`reached max retries: 0` is deliberate: `scripts/r2_verified_store.py:643` sets
`retries={"total_max_attempts": 1}` on the dedicated long-timeout copy client,
added by PR #198 after run `30388252232` measured "six ambiguous automatic
replays followed by ReadTimeoutError" on a 2 GiB server-side copy. The comment
states the intended recovery: *"a lost response is reconciled by the promotion's
exact-identity rerun instead of replaying an overwrite inside one process."*

That reasoning is sound for the case it was written for, and too wide for this
one. Two different failures are being treated identically:

- **Ambiguous** — a read timeout after the request was sent. The write may have
  landed; a blind replay is genuinely unsafe. PR #198 is correct here.
- **Definite** — a parsed HTTP 5xx `InternalError` response, which is what
  happened here. The server answered. Nothing is in flight.

A bounded retry of the definite class is safe on this specific path, for a
reason stronger than "5xx is usually retryable": the destination keys are
content-addressed by the source's own sha256 and immutable, `_execute_object`
performs its create-only destination check before copying, and the post-copy
ETag proof runs regardless. Re-copying the same source to the same key writes
identical bytes by construction.

The fix is to distinguish the two rather than to relax the rule — retry on a
parsed `ClientError` carrying a 5xx status with bounded exponential backoff and
a small attempt cap, and keep `total_max_attempts: 1` for read timeouts and any
response that cannot be parsed. Seconds instead of a discarded 4h17m run.

**This is the highest-value item here for the next planet rebuild.** Items 1 and
2 make promotion faster; this one makes it *finish*. A promotion that copies
21,279 objects has 21,279 chances to draw one transient InternalError, and today
any single draw discards every hour spent before it.

Related, and why this compounds: a retry re-pays for work that already passed.
The rerun re-HEADs all 37,228 Places keys in verify — measured at 66 minutes,
107 ms per serial HEAD — to re-prove a family that succeeded an hour earlier.
There is no per-family skip, so item 2 and item 4 both directly reduce the cost
of every future retry.

## Already recorded elsewhere — do not re-derive

These stay owned by their existing docs; listed so this queue does not
duplicate them.

- `2026-07-28-planet-build-wall-clock-review.md` §§1–8 and Waves 0–5: matrix
  concurrency 4→8, build Rust binaries once per architecture, marker discovery
  before expensive resume work, Address reducer fan-out near 60 jobs, remove the
  second full Places planning read, Places head radix + bounded parallel tails,
  single-write finalize publication, runner-image tax.
- `construction-v1-state.md` → "Deferred, not active": range-owning address
  reducer, narrower staging-only R2 credential, runner-minute ledger
  completeness, finalize double-hydration profiling.
- `2026-07-24-construction-v1-follow-ups.md`: the address map shuffle is
  DECIDED not-ported; the address partition key is FROZEN.

## Execution status of the wall-clock plan, as of 2026-07-31

Recorded because it is easy to assume otherwise. **Nothing in Tracks A, B, or C
has been executed beyond Wave 0.**

- Wave 0 items 1-4: **done**, via PR #185, measured in run `30323929757`
  (finalize 56m50s).
- Wave 0 items 5-6 (head substage timing, post-#184 cold control): not started.
- Waves 1-5: not started. Verified against the tree, not inferred —
  `max_parallel` is still `4` (`scripts/construction_v1_control.py:69`); there is
  no shared request-pinned binary artifact; `max_reduce_jobs` still defaults to
  today's behaviour; the only "selective" in `places_construction_v1.py` is the
  reduce schema name, not a selective binding pass; there is no radix stage in
  the head path; and promotion still copies.
- Every PR merged since the review (#186-#219) is reverse-v2, promotion and
  publication, serving, or benchmarks. The single optimization among them is
  #200, which parallelized the promotion **copies** — and left the three HEAD
  loops in item 2 serial.

Track C is not planned; the adopted direction is staged A-to-B. "Do not stream
uncheckpointed records directly between Actions runners" remains in the
review's not-recommended list.

## Free knobs for the next planet rebuild

No code change, therefore no new request identity:

- `max_reduce_jobs` (construction-v1 dispatch). Lowering the cap makes larger
  contiguous partition batches. The workflow's own comment records the measured
  curve: batch 3 (today's default) = 12.7x object amplification / ~0.63 TB of
  Address R2 reads; batch 12 = 4.2x / ~0.21 TB. Median Address reducer job was
  255 s against its timeout, so the headroom is real.
- `max_parallel` on `reverse-v2.yml` — see item 7.

By contrast, `max_parallel` for construction lives in the control caps, so
raising it 4->8 is a code change. Per the review's Wave 0 note, changes to
`places_construction_v1.py`, `address_construction_v1.py`, `Cargo.lock`,
canonical caps, or the producer commit mint a **new request identity and staging
namespace**. Batch every accepted optimization into one reviewed request rather
than paying for a new planet namespace per change.

## Suggested order

1. **Item 9** first. It is the only item here that changes whether a planet
   promotion completes at all, it is small, and it has already cost one
   4h17m run.
2. **Item 6** next — it removes a recurring deadline, and it is a
   prerequisite for treating promotion as an R2-only operation.
3. **Item 2** — pure win, no semantics change, no design decision needed, and it
   cuts the cost of every retry item 9 does not prevent.
4. **Items 7 and 8** before the next rebuild — item 7 is a dispatch flag, and
   item 8 changes only when reverse is launched, not what it computes.
5. **Item 1** as the architecture change, sequenced with wall-clock review §7 so
   finalize and promotion are re-pointed once rather than twice. Items 3 and 4
   become moot if it lands; do them only if it slips.
6. **Item 5** only after a deliberate call on the proof it trades away.

## 10. Shard-format choice for the POI prominence byte — RESOLVED: add a byte

Raised while measuring the POI prominence term (Part 6d of
`2026-07-31-search-quality-and-street-layer.md`). **Resolved 2026-07-31 by
measurement; no longer open.**

The question was whether a dedicated prominence byte on every head entry is
affordable. It is, overwhelmingly. From planet head run `30288619536`
(recorded in `construction-v1-state.md`):

```
head entries         30,841,082
published head bytes  5,141,583,720   (4.79 GiB)
bytes per entry              166.7
one added byte             +29.4 MiB   = +0.60% of the head
```

**Decision: add a dedicated `prominence_rank` u8 to the head entry.** At +0.60%
the cost is not worth trading anything for.

The two alternatives are recorded as rejected, with reasons:

- **Redefine the existing `confidence_rank` byte** (zero format change).
  REJECTED: `confidence_rank` is published. `/v2/reverse` surfaces it, and
  `reverse_shard_v1.py:227` documents it as a carried field. Redefining it
  changes reverse's `confidence` from an *existence* signal to a *prominence*
  one — an API break disguised as a build change — and it saves 29.4 MiB.
- **Pack 4 bits into `field_mask`'s spare high bits** (16..128 are free).
  REJECTED as premature. It is genuinely free and would work, but it costs
  resolution and couples two unrelated fields to save 0.60%. Keep it in reserve
  only if the head budget ever becomes binding.

Related finding, worth its own cleanup: `project_places_construction_v1.py:143`
projects `names.common` as a `common_names` column that is **empty for 100% of
Overture places** — measured over 772,341 places across Barcelona, Paris,
Seattle, and Tokyo, 0.0% non-empty in every one. It is consumed only by
`baseline_places_construction_v1.py`, not by the serving path. Dropping it from
the projection is free and removes a per-row list allocation.
