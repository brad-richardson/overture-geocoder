# Construction-v1 hosted control checkpoint 6

## Outcome

The new Address + Places control contract is isolated from the unshipped
global-v2 workflows. It has no predecessor and no compatibility path. Nothing
in this checkpoint deploys, dispatches, authenticates, contacts cloud storage,
or mutates a public catalog.

The local admission result is intentionally false. Address readiness passes.
Places readiness is false with the exact reasons recorded in
`benchmarks/places-construction-v1-data/evidence/readiness-v1.json`. The exact
legacy core version and release-manifest SHA-256 are also operator inputs and
are currently unavailable locally; neither is guessed.

## Canonical preparation command

```sh
python3 scripts/construction_v1_control.py prepare \
  --request-id request-YYYYMMDD-random \
  --build-id build-YYYYMMDD-random \
  --slice-id slice-YYYYMMDD-random \
  --staging-id staging-YYYYMMDD-random \
  --producer-commit 40_LOWERCASE_HEX \
  --legacy-core-version EXACT_VERSION \
  --legacy-core-manifest-sha256 64_LOWERCASE_HEX \
  --output /tmp/construction-v1-review.json
```

The package binds fresh IDs, producer commit, release `2026-06-17.0`, legacy
core identity, both family inventories/schemas/specs/readiness files, toolchain
and format versions, genesis lineage, immutable request-derived namespaces,
hard concurrency/minute/cost/remote-operation/byte caps, map matrices,
replaceable genesis-derived reducer matrix contracts, and the exact typed
confirmation. Missing identity or failed readiness emits an honest review
package and exits nonzero.

`admit-dispatch` independently regenerates the package from a submitted request
and rejects any changed byte, hash, confirmation, readiness result, or run
attempt other than one. This is ready to be the secret-free first job of the
hosted workflow.

## Remote safety contract

`scripts/construction_v1_remote.py` supplies the backend-neutral mutation
semantics required by the eventual hosted adapter:

- hard operation/read/write counters;
- create-only objects;
- retry conflicts accepted only for the exact pre-admitted key, byte length,
  and SHA-256;
- per-upload HEAD verification;
- completion marker written last;
- one exact-prefix listing and one streaming read per final slice object;
- missing/extra/different final objects are fatal;
- cleanup accepts an explicit exact set under the preview prefix and enforces
  object and byte caps.

Production catalog and release prefixes are explicitly forbidden by the
request. Only a non-promoting slice and temporary preview namespace exist.

## Adversarial validation

`python3 -m pytest -q tests/test_construction_v1_control.py tests/test_construction_v1_remote.py`
passes 8 tests. They cover deterministic regeneration, ID mutation, typed
confirmation binding, genesis/no predecessor, namespace isolation, current
readiness failure, absent core identity, interruption before marker, exact
resume, corrupt conflict, operation/byte caps, exact final verification, and
preview-only cleanup.

## Deliberate stop line and handoff

No dormant GitHub execution workflow was added in this checkpoint. Adding a
workflow that merely names map/reduce/head steps without a complete hosted
Address + Places adapter would create a false dispatch surface. The existing
construction modules are real local data planes, but Places' checked-in
readiness specifically says planet fan-in, adaptive subdivision, routed/head
verification, Worker queries, and interruption coverage are not yet proven.

The exact next action is to close the Places readiness reasons and commit its
new passing readiness evidence. Then obtain the exact legacy core version and
manifest SHA-256, run the preparation command with fresh IDs, and review the
result. The next checkpoint may wire the admitted package to a serialized
hosted workflow only when every invoked map/genesis/reduce/head/verify/slice/
preview-cleanup command is real. There must be no production write or public
projection in that workflow.
