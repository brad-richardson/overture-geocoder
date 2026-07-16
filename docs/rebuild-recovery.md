# Recovering a failed monthly rebuild

The `Rebuild R2 Shards` workflow builds a date-versioned release
(`YYYY-MM-DD.N/`) and, only after every family (forward, reverse, ID) verifies,
has a single `finalize-release` job publish `catalog.json` to promote it. When a
monthly run fails partway, use this ritual — do not dispatch a fresh rebuild.

## Key invariants

- **Versioned prefixes are immutable.** `prep` rejects a version whose prefix
  already exists in R2 (the "Compute version" step lists `${VERSION}/` and aborts
  if any object is present), so a fresh rebuild dispatch cannot re-use the same
  version to fix it.
- **Only the original run can finalize a version.** `patch-id-stage.yml`
  re-stages and rebuilds ID artifact ranges but has no finalize job — it never
  writes `release-manifest.json` or `catalog.json`. The only publication path for
  a patched version is GitHub's **Re-run failed jobs** on the ORIGINAL rebuild
  run, so that `id-post → finalize-release` re-execute with `prep`'s original
  `version`/`release` outputs preserved (successful jobs are not re-run and their
  outputs carry over).
- **Stale re-runs can't clobber newer data.** The finalizer's catalog step
  (`finalize_rebuild.py catalog`) refuses a candidate that is "not newer than the
  catalog", so a late re-run of an old run cannot overwrite a fresher promoted
  version.

## Recovery ritual

1. Identify the failed prefixes/ranges from the failed `id-stage-*` / `id-build`
   jobs of the original run.
2. Dispatch `patch-id-stage.yml` for the SAME `version` and `release`, passing the
   failed `prefixes` AND `run_build=true` in a SINGLE dispatch (see trap below).
   It re-stages the registry, invalidates the affected `build-<range>/_SUCCESS`
   markers, rebuilds the complete affected ranges, and rewrites ID metadata.
3. On the original rebuild run, click **Re-run failed jobs**. `id-post` re-verifies
   the ID artifacts and `finalize-release` publishes the manifest and (when
   promoting) swaps `catalog.json`.

## The two-dispatch stale-marker trap

Do NOT split the patch across two dispatches:

- Dispatch A with `prefixes=...` but `run_build=false` re-stages the registry but
  SKIPS the "Force complete rebuilds of affected ranges" step (it is gated on
  `run_build`), so the affected ranges keep their current `_SUCCESS` markers.
- Dispatch B with empty `prefixes` and `run_build=true` runs `build-ranges`, but
  with no prefixes nothing invalidates those markers, so the build honors them and
  the patched data is never re-emitted — the fix silently never ships.

Always pass `prefixes=<failed>` and `run_build=true` together, so the markers are
invalidated and the ranges actually rebuild, in one dispatch.
