# Reverse Address representative probe

`Probe v2 reverse Address density` is a manual, ad hoc, read-only gate before a
planet Address reverse execute. It is not scheduled and it does not publish a
shard, marker, claim, manifest, release, or catalog.

## Inputs and trust boundary

The operator supplies:

- the run ID of a successful `Build v2 reverse indexes` Address dry run; and
- the exact SHA-256 of that run's `out/reverse-plan.json`.

The workflow authenticates the source as a completed successful main-branch
`reverse-v2.yml` run, downloads the artifact for its exact run attempt, checks
the supplied digest, and requires its publication admission evidence to say
`family=addresses`, `mode=dry-run`, `state=fresh`, and `slice_claim=null`.

For a successful dry-run ID and its reported run attempt:

```bash
gh run download <dry-run-id> \
  --repo brad-richardson/overture-geocoder \
  --name reverse-v2-plan-<dry-run-id>-<attempt> \
  --dir /tmp/reverse-address-plan
sha256sum /tmp/reverse-address-plan/out/reverse-plan.json

gh workflow run reverse-address-probe.yml \
  --repo brad-richardson/overture-geocoder \
  --ref main \
  -f plan_run_id=<dry-run-id> \
  -f plan_sha256=<sha256-from-the-previous-command>
```

This dispatch launches the probe only. It is not an Address reverse execute
confirmation and cannot publish the reverse index.

The probe then revalidates the compact plan. Every directory and selected
Parquet pack is fetched through the construction staging content-addressed
reader and checked against the plan's byte length and SHA-256. The probe store
exposes reads and local cache eviction only; it deliberately has no publication
or marker-writing method.

## Representative selection and evidence

All directory objects are small metadata. The probe streams the complete
authenticated set, reconciles their cell counts to the plan's expected record
total, and selects the densest level-8 cell by:

1. descending record count; then
2. ascending cell identifier as the deterministic tie break.

Only source packs whose directory names that cell are hydrated. DuckDB filters
them to that one cell, and the loaded count must equal the aggregated directory
count. The production `reverse-encode-v1` and `reverse-verify-v1` binaries then
build and verify one ephemeral local shard.

The JSON evidence records:

- request and plan digests;
- all 16 range record/source-byte counts;
- densest cell, bucket, records, source identities, and sub-cell level;
- loaded and encoded record reconciliation;
- per-field dictionary cardinalities;
- dictionary, payload, index, header, and total bytes;
- leaves and bytes per record; and
- whole-probe, watchdog, encoder, verifier, and staging resource evidence.

Every dictionary field remains bound by the production `u16` cardinality and
the complete dictionary remains bound by the production 8 MiB serving-read
limit. The production verifier must pass.

## Conservative projection and 3 GiB gate

The projection uses the worse whole-shard byte rate of:

- the preserved real Seattle measurement: 6,251,653 bytes / 104,928 records;
  and
- the authenticated global densest-cell probe.

For each exact 16-bucket range:

```text
projected_bytes =
  ceil(range_records * worse_basis_bytes / worse_basis_records * 3 / 2)
```

The fixed `3/2` reserves 50% empirical uncertainty. This deliberately consumes
essentially all of the workflow's documented approximately 53% margin between
the Seattle point projection and the confirmation-bound 3 GiB range ceiling,
leaving only the remaining few percentage points plus integer rounding. It is
deterministic and reported as an exact rational, not a rounded decimal.

This is a representative empirical projection, not a mathematical upper bound
over every unencoded cell. The execute workflow independently fails any range
whose actual output exceeds 3 GiB before catalog publication. The probe reports
`execute_gate=blocked` and exits nonzero if its projected maximum range exceeds
that same cap. A green probe is necessary evidence for an Address execute; it
does not itself authorize or dispatch one. The operator must still supply the
execute workflow's exact typed cost confirmation.
