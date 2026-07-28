# Planet Places publication result

GitHub Actions run
[`30323929757`](https://github.com/brad-richardson/overture-geocoder/actions/runs/30323929757)
completed the first non-promoting planet Places publication on 2026-07-28.
Together with the successful planet Addresses run `30215529919`, this closes
the construction-v1 planet execution milestone for both forward families.

This document preserves the run evidence and the immediate operational
learnings. `construction-v1-state.md` remains the current source of truth.

## Provenance and recovery

- Canonical request:
  `88b7f17149fd5d75bf64720f0640d2cbe8aeb5ead750d279c1881f9bd5332614`
- Workflow commit:
  `e981012600a97a3e7ebe540c70ed313f3b54d1d1`
- Request-pinned producer:
  `63b7f71398eb4f50cfbe937314eb2abc6cb342bd`
- Authenticated resume source: run `30288619536`
- Finalizer job: `90165467500`

The workflow deliberately kept the request-pinned producer checkout, then
overlaid and byte-compared the four reviewed finalizer transport files from the
dispatch commit. The job logged the two exact commits before gaining access to
R2. Every paid upstream phase and the global head remained skipped; the run
reused the authenticated plan, reductions, and successful prior head.

The final ledger is artifact `8675902552`,
`construction-v1-ledger-30323929757`, with digest
`sha256:d2b3848f7f4dcef2bb2ee2c447fc4d679697a89e17932c40aa8493cea589de31`.
It carries 1,382 prior runner minutes and the successful 207-minute global head.

## Result

The finalizer reconciled and verified the same exact set admitted by the prior
attempt:

- 40,931 final members and 51,814,660,317 bytes;
- 20,698 serving members;
- 20,231 per-place positions members;
- two finalizer manifests;
- largest recorded object: 209,194,480 bytes;
- `reconciles: true`;
- whole-slice exact-key and stored-byte-metadata verification passed; and
- the completion marker was written last at
  `construction-v1/86558218e2b67db0e0249abbee0c6d17650dea43467ed14c59789bc60c7bacb0/markers/finalize/places.json`.

The prefix is immutable and non-promoting. No production catalog was changed.

The workflow gate also proved that the whole-slice verification contained
40,931 objects, the same non-zero byte total, and a 64-hex binding SHA-256.
The small uploaded ledger does not retain that binding digest or the result
payload; preserving `final-work/result.json` as a future artifact would make
those values independently retrievable after the log and resume artifacts
expire.

## Timing

The main finalization interval was 56 minutes 49.9 seconds:

| Phase | UTC interval | Wall time | Mean object rate |
|---|---|---:|---:|
| Admission | 02:45:43.684–03:10:39.474 | 24m 55.8s | 27.4/s |
| Upload | 03:10:39.494–03:37:41.495 | 27m 02.0s | 25.2/s |
| Marker/list transition | 03:37:41.495–03:38:09.055 | 27.6s | — |
| Verification | 03:38:09.055–03:42:33.328 | 4m 24.3s | 154.9/s |

Admission used five workers, derived conservatively from the untrusted 5 GB
contract object cap and runner disk floor. After it proved the recorded
209.2 MB maximum, upload used all 16 persistent-client workers. Verification
also used 16.

The prior run spent about 4 hours 17 minutes in serial admission and then died
on one streaming-body timeout before any publication. The corrected admission
finished in 24 minutes 56 seconds, about 10.3 times faster, while retaining the
all-members-before-any-PUT barrier. The run completed without a body-read
failure escaping the bounded whole-GET retry loop. Retries are intentionally
silent, so the logs cannot prove that no transient body-read failure occurred.

At the exact-set byte total, admission sustained about 34.6 MB/s of verified
staging bytes. Upload published about 31.9 MB/s of final bytes; because each
staged member is hydrated again immediately before its final PUT, that phase
represents roughly 63.9 MB/s of aggregate object-body read plus write traffic,
excluding overhead.

## R2 operation accounting

The run reported 81,858 successful logical staged-object hydrations: each of
the 40,929 staged members was hydrated once during admission and once during
upload. The one-GET hydration change avoided the separate proof HEAD on both
passes, removing 81,858 Class B operations relative to the old HEAD-plus-GET
implementation.

For the successful first publication, the code-level operation projection is
shown below. The Class A/Class B mapping follows
[Cloudflare's R2 operation classification](https://developers.cloudflare.com/r2/pricing/).

| Operation family | Class A | Class B |
|---|---:|---:|
| Final member PUT + post-PUT HEAD, 40,931 members | 40,931 | 40,931 |
| Exact-prefix LIST, 41 pages | 41 | 0 |
| Whole-slice metadata verification | 0 | 40,931 |
| Completion marker PUT + HEAD | 1 | 1 |
| Two staging hydration passes | 0 | 81,858 |
| **Total attributable to finalize** | **40,973** | **163,721** |

These are the expected successful-path requests, not Cloudflare billing
telemetry; an SDK-level or whole-GET body retry could add a request without
appearing in the progress log. The dominant remaining Class B term is intrinsic
to the current two-pass safety design: one logical staging read for
pre-publication admission and one for upload. Removing that term would require a
design change that preserves the strict admission barrier without keeping the
51.8 GB set resident locally.

## What to optimize later

The measured priorities are now clear:

1. Preserve `result.json` with the final ledger so the verification binding,
   exact byte count, residency, and hydration counters survive artifact expiry.
2. Investigate eliminating or amortizing the second staging hydration only if
   R2 operations remain material. It is a real 40,929-GET opportunity, but it
   must not weaken the pre-publication barrier or exceed runner disk.
3. Use the phase progress events as the baseline for future transport changes.
   Verification is already only 7.7% of finalization wall time; admission and
   upload are the useful targets.
4. Treat runner architecture, build-image caches, and map/reduce query work as
   separate measured optimizations. They cannot improve this finalize-only
   result and should not delay the reverse-geocoding fast follow.

## Next milestone

Construction-v1 forward planet readiness is complete. Begin reverse R1 against
the already published per-record Places and Addresses artifacts:

1. implement the shared encoder and verifier;
2. establish authoritative Places-cell/address-E7 cross-language parity; and
3. extend the small real-data harness before designing the bucket-range reducer.

No forward map needs to be rerun for reverse geocoding.
