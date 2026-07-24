# Address construction-v1 checkpoint 3

Status: **blocked before planet evidence; not ready to dispatch**.

## Completed locally

- Froze `benchmarks/address-construction-v1-evidence-spec.json` before running any
  candidate scale benchmark. It pins the runtime, representative/near-cap/known
  skew tasks, the exact twelve-task census universe for maximum-bucket and
  duplicate-heavy selection, and numeric performance/resource/correctness gates.
- Exercised Python 3.12.12, DuckDB 1.5.1, NumPy 2.3.5, and PyArrow 25.0.0 in the
  local `.venv`. Added the exact macOS arm64 wheel hashes to the hosted runtime
  lock without removing its Linux hashes.
- Changed bounded child execution to create and monitor a process group, include
  recursive descendants in its RSS measurement, and terminate the entire group
  on a cap breach.
- Changed immutable object publication to write and fsync a temporary object,
  atomically hard-link it into its content-addressed identity, verify it, and
  leave completion-marker publication last.
- Added `scripts/validate_address_planet_readiness.py`. It exits nonzero unless
  the exact runtime, canonical inventory identity, frozen evidence identity, and
  all scale gates reconcile.

## Exact prerequisite

The pinned file `benchmarks/address-rowgroup-inventory-report.json` has SHA-256
`7c198331b96cc168235dfccac1b0f6919eadaad996db5fc176fac5cca93e7771`, but it is
the older inventory shape. It lacks the current canonical contract's
`schema_contract`, `inventory_sha256`, `source_inventory_sha256`, per-task
execution/source/task digests, and range-level source object sizes/counts.
The current inventory validator therefore fails closed with `address source
inventory digest differs`.

There is also no locally available projected planet task corpus. The existing
footer inventory can identify representative, near-cap, and prior retention-skew
tasks, but cannot truthfully identify maximum-bucket-heavy or duplicate-heavy
tasks; those metrics require the frozen twelve-task read-only transformed census.

Required read-only inputs to resume:

1. Regenerate or provide the self-bound 2026-06-17.0 inventory using the current
   `scripts/inventory_address_rowgroups.py`, without changing the frozen gates.
2. Provide projected inputs for the exact frozen candidate universe, or read-only
   access sufficient for the harness to produce them with embedded inventory,
   schema, object, ETag, size, row-group, and task identities.
3. Run the census to resolve the two metric-selected roles, then run the frozen
   Python baseline and candidate twice per required input.

No cloud write, workflow dispatch, publication, commit, or push was performed.
