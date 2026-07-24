# Construction-v1 legacy core identity retrieval (2026-07-23)

Goal: obtain the exact legacy core version and the release-manifest SHA-256 that
`scripts/construction_v1_control.py prepare` requires (`--legacy-core-version`,
`--legacy-core-manifest-sha256`), and re-verify the pinned Overture
`2026-06-17.0` address source inventory before reuse. Read-only remote access
only; no writes, dispatches, or commits.

## What the control contract requires (verified from source)

From `scripts/construction_v1_control.py` (lines 174-206) the two operator
inputs feed `request.legacy_core`:

- `--legacy-core-version` -> `legacy_core.version` (EXACT_VERSION).
- `--legacy-core-manifest-sha256` -> `legacy_core.manifest_sha256`; must match
  `^[0-9a-f]{64}$`.
- The script derives `legacy_core.manifest_key = "{version}/release-manifest.json"`.

The SHA is therefore the SHA-256 of the **exact bytes of the R2 object
`{version}/release-manifest.json`** in bucket `geocoder-shards`. This matches
the Worker contract in `crates/geocoder-worker/src/v2.rs` (`validate_release`,
lines 435-438: `legacy_core.manifest_key == "{version}/release-manifest.json"`
with a `valid_sha256(legacy_core.manifest_sha256)`) and the producer side in
`scripts/v2_release_manifest.py` (`_validate_legacy_release`, `build_release_manifest`)
and `scripts/finalize_rebuild.py` (line 803, writes `{version}/release-manifest.json`).

The `version` is a geocoder build id matching `BUILD_RE = \d{4}-\d{2}-\d{2}\.\d+`
and is the latest child version in the production STAC catalog.

## Retrieved: legacy core version

- **Legacy core version = `2026-07-13.0`**
- Source: production Worker health endpoint `GET https://geocoder.bradr.dev/health`
  (custom domain from `crates/geocoder-worker/wrangler.toml`).
- Retrieval (UTC): 2026-07-24T01:50:34Z. Response `date: Fri, 24 Jul 2026 01:50:34 GMT`.
- HTTP 200, `content-type: application/json; charset=utf-8`, `content-length: 40`.
- Body (40 bytes): `{"status":"ok","version":"2026-07-13.0"}`
- Body SHA-256: `f11d6ec2281dd99345b8efe53241e99c69ea30e79811b61bca11739f38687ac6`
  (SHA of the health response, not the release manifest — recorded only for provenance).
- Semantics (confirmed in `crates/geocoder-worker/src/handlers.rs` `handle_health`
  and `stac/catalog.rs` `check_health`): `version` is the latest ordered version
  from the production `catalog.json`, i.e. the legacy core data version. This is
  exactly `legacy_core.version`.

## NOT retrieved: release-manifest SHA-256 (credential blocker)

`legacy_core_manifest_sha256` = SHA-256 of `2026-07-13.0/release-manifest.json`
in R2 bucket `geocoder-shards` **could not be retrieved.** It was not guessed.

Access paths attempted, all read-only, all exhausted:

1. **Worker public endpoints** — the deployed Worker does not serve raw catalog
   or manifest objects. Routes are `/`, `/health`, `/search`, `/reverse`,
   `/id/:id`, `/v2/forward`, `/v2/reverse`, `/v2/features/:gers_id`
   (`crates/geocoder-worker/src/lib.rs`). Probes:
   - `GET /catalog.json` -> HTTP 404
   - `GET /v2/catalog.json` -> HTTP 404
   - `GET /2026-07-18.0/release-manifest.json` -> HTTP 404
   - `GET /v2/forward?q=test` -> HTTP 503
     `{"error":"release_unavailable","message":"no v2 geocoder release is currently available"}`
     (confirms no v2 release manifest is live in production to read the SHA from).
2. **Public R2 / HTTP bucket access** — no `r2.dev` public URL exists for
   `geocoder-shards`. `pub-geocoder-shards.r2.dev` / `geocoder-shards.r2.dev`
   return Cloudflare 500/error pages, not the object. The bucket is private,
   reachable only through the Worker (custom domain `geocoder.bradr.dev`).
3. **`wrangler r2 object get`** — `wrangler` is **not installed** (`wrangler not
   found`; `npx wrangler` blocked on package download). No local Wrangler config
   (`~/.wrangler` absent). `wrangler whoami` unavailable.
4. **R2 S3 credentials** — none present. `CLOUDFLARE_ACCOUNT_ID`,
   `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` (the vars every R2 script here
   requires, e.g. `scripts/patch_failed_shards.py`, `scripts/rowgroup_experiment.py`)
   are all unset in this environment.
5. **Repo / local copies** — no checked-in `2026-07-13.0/release-manifest.json`,
   no recorded SHA for it anywhere in the repo. `shards/catalog.json` is a stale
   local fixture (latest child `2026-02-24.0`), not production.

**Conclusion:** obtaining the release-manifest SHA-256 requires either a
`wrangler` install authenticated to the Cloudflare account or R2 S3 credentials
(`CLOUDFLARE_ACCOUNT_ID` + `R2_ACCESS_KEY_ID` + `R2_SECRET_ACCESS_KEY`) for
bucket `geocoder-shards`. Neither is available in this environment. Per the
no-guessing rule, the SHA is left unresolved.

### Exact command to complete once credentials exist

```sh
# Option A: wrangler (read-only get)
wrangler r2 object get geocoder-shards/2026-07-13.0/release-manifest.json \
  --remote --pipe | shasum -a 256

# Option B: S3 API against R2 (read-only)
aws s3api get-object \
  --endpoint-url "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com" \
  --bucket geocoder-shards \
  --key 2026-07-13.0/release-manifest.json /tmp/release-manifest.json
shasum -a 256 /tmp/release-manifest.json
```

The 64-hex digest is the `--legacy-core-manifest-sha256` value. Also confirm the
object actually exists for `2026-07-13.0` (release-manifest.json is a newer
`finalize_rebuild.py` artifact; if this build predates it, a build id that has
one must be chosen).

## Admission prepare status (local, no remote writes)

Ran `scripts/construction_v1_control.py prepare` with fresh distinct IDs, a
40-hex producer commit, and `--legacy-core-version 2026-07-13.0` (SHA omitted,
not guessed). Result: `admitted=false`, exit 1. Blockers, in two groups:

- `exact legacy core version and release-manifest SHA-256 are required`
  — the credential blocker above (version present, SHA absent).
- Places readiness blockers (owned by another agent; out of this scope). Address
  readiness passes.

`readiness = {addresses: true, places: false}` — Address readiness confirmed
passing. The admission package therefore still cannot be finalized for two
independent reasons: the missing manifest SHA-256 (this task) and Places
readiness being false (separate agent's scope).

`python3 -m pytest -q tests/test_construction_v1_control.py` -> 4 passed.

## Overture 2026-06-17.0 source inventory re-verification (task 5)

Re-verified the pinned source objects referenced by
`benchmarks/address-construction-v1-data/inventory/addresses.json` (read-only;
file not modified). Source prefix (from the inventory's `source_inventory.discovery`):
`s3://overturemaps-us-west-2/release/2026-06-17.0/theme=addresses/type=address/`.

HEAD each of the **32** pinned objects via public S3
(`aws s3api head-object --no-sign-request`) and compared `ETag` + `ContentLength`
against the pinned `etag` / `bytes`:

- **32 objects, 32 matched, 0 mismatched, 0 errors.**
- Retrieval (UTC): 2026-07-24, same session.

The pinned Overture `2026-06-17.0` address source inventory is **still current**
and safe to reuse. (Places source objects were out of scope / not part of this
inventory file.)

## Files created / touched

- Created: this doc, `docs/plans/2026-07-23-construction-v1-legacy-core-identity.md`.
- No repository data files modified. No commits, pushes, or remote writes.
- Scratchpad only (not committed): review package and health capture under the
  session scratchpad dir.

## RESOLVED: release-manifest SHA-256 (2026-07-24)

Retrieved via the read-only `r2-object-sha` utility workflow (added in PR #139,
run 30060326608, conclusion success, 2026-07-24T01:57:10Z):

- Object: `geocoder-shards/2026-07-13.0/release-manifest.json`
- Byte length: `1710409`
- SHA-256: `26d9c9d38dacafa87d9f9693f989f812cd37c08699e3d0515a2de9776258f7f7`

`construction_v1_control.py prepare` accepts the identity (exit 0) and emits
request SHA `a7fa8a31c117af7b829d05150ff0bc99c825bb25d1e25067f0d4f618b46c5537`
for the placeholder IDs used in the local check. Admission remains `false`
solely on Places readiness, which is in progress. The IDs used for a real
dispatch must be regenerated at dispatch time; the SHA/version above are the
durable facts.
