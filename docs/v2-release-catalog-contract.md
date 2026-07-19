# v2 release and catalog contract

Status: control-plane foundation; non-promoting.

The v1 production discovery root remains `catalog.json`. V2 uses a separate
`v2/catalog.json` candidate so its release composition and retention can evolve
without changing historical division clients or moving immutable v1 objects.

One v2 release binds these identities atomically:

- `data_version.overture_release`: the common Overture source release;
- `data_version.geocoder_build`: the geocoder build and rollback identity;
- one verified legacy core release containing division forward/reverse shards
  and the UUID-prefix ID index; and
- zero or more verified Places/address family manifests from the same Overture
  release.

The builder rejects cross-release composition. It records each optional
family's manifest key, file SHA-256, self-digest, format versions, coverage,
enabled operations, and the verified artifact entrypoint for each operation.
Presence does not imply every operation: the first
address family advertises only `structured_forward`, while Places advertises
`forward`. General address forward/reverse can be enabled only by a later build
whose artifacts and Worker support those operations.

An operation cannot be advertised from shard presence alone. Its entrypoint
must be one of the family manifest's hashed artifacts, such as Places
`forward -> catalog.pcat` or addresses
`structured_forward -> address-collection.json`.

## Namespace

```text
catalog.json                                      # unchanged v1 root
{legacy_version}/release-manifest.json            # existing verified core
{legacy_version}/families/{family}/...            # existing family objects
v2/catalog.json                                   # future v2 discovery root
v2/releases/{geocoder_build}/release.json          # v2 composition manifest
```

The v2 release references existing immutable keys; it does not copy division or
ID objects. Retention must consider both catalog roots before deleting an
object. Promotion will be a separate compare-and-swap implementation; the
current CLI only writes local candidates.

## Commands

Build and validate a release candidate:

```bash
python scripts/v2_release_manifest.py release \
  --geocoder-build 2026-07-19.1 \
  --overture-release 2026-06-17.0 \
  --legacy-release-manifest release-manifest.json \
  --family-manifest places=places-family-manifest.json \
  --family-manifest addresses=addresses-family-manifest.json \
  --entrypoint places.forward=families/places/catalog.pcat \
  --entrypoint addresses.structured_forward=families/addresses/address-collection.json \
  --output v2-release.json

python scripts/v2_release_manifest.py validate-release \
  --manifest v2-release.json
```

Build a new catalog or extend an existing one:

```bash
python scripts/v2_release_manifest.py catalog \
  --release-manifest v2-release.json \
  --before v2-catalog-before.json \
  --output v2-catalog-next.json
```

Build IDs use `YYYY-MM-DD.N` and must increase monotonically. Every generated
object is deterministic when `--generated-at` is supplied.
