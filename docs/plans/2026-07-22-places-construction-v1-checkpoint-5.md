# Places construction-v1 checkpoint 5

Status: `ready:false`; blocked before the frozen census by the session's
network-approval usage limit.

No public source columns, candidate construction timings, or role-selection
metrics were observed. No gate was relaxed, and no alternate download path was
attempted.

## Frozen contract and canonical inventory

The evidence spec was created before inventory/candidate scale observation and
is frozen at SHA-256
`0baee5f19bc3419995f504aec9bc6baf31d85b31d130a25fbf8114bbcd429dab`.
It pins Python 3.12.12, DuckDB 1.5.1, NumPy 2.3.5, PyArrow 25.0.0,
Rust/Cargo 1.97.1, the seven distinct role-selection rules, 12-task census
universe, all numeric resource/read/output/wall/head gates, deterministic
double runs, speedup, selective amplification, interruption phases, and 25%
headroom. Its relaxation policy requires a new spec version.

The existing strict footer-only inventory path regenerated and self-validated
the public Overture `2026-06-17.0` Places inventory:

- inventory SHA-256:
  `b1830aee50ea61395cda14f6b04888d846dcba12f24967c7ab52c64fe5944eff`;
- inventory file SHA-256:
  `0a5eaa1ce23a7c71ec4d6303059c0e5e829ba7402d3bac33802bbac16150c2eb`;
- required-schema fingerprint:
  `49453ed2b28a7940fe6664b13ec89631fbee2d98efdad0ff8ab1a26972212a5a`;
- 16 immutable source objects, 75,642,289 source rows, 5,120 row groups,
  and 89 deterministic map tasks;
- maximum task: 999,805 rows, 162,730,587 selected uncompressed bytes,
  78,605,320 selected compressed bytes, and 64 row groups.

The frozen census universe resolves to task indexes
`[15,5,13,1,76,55,85,73,71,86,74,87]`.

## Scale-critical architecture completed before observation

- A focused public-source projector flattens names, common-name maps, brands,
  categories, addresses, confidence, status, geometry, and signed provenance
  with Arrow/NumPy column operations. It does not create Python feature rows.
- Projected Parquet metadata binds the evidence spec, inventory file/content,
  schema fingerprint, exact task/ranges, and source URI/ETag/size/row-group
  identities.
- Rust remains authoritative for admission, tokenizer/ranker, route, stable
  token-partition hash, proof payload, and exact two-lane semantic binding.
- Adaptive predecessor-free genesis recursively subdivides oversized cells by
  SHA-256 token nibbles under frozen row/byte/distinct-token caps. Pack fan-in
  is chunked into an on-disk narrow DuckDB planning table.
- Selective reduce streams bounded Parquet batches into on-disk DuckDB, never
  concatenates the selected relation in memory, and reconciles selected plus
  discarded exact bindings for every overlapping row group.
- Routed candidates are deterministically capped at 256 per cell/token.
- `PLRV0002` and `PLHD0002` include a bounded SHA-256 index. The independent
  verifier reconstructs and reconciles every index entry and payload extent.
  The dormant Worker binary-searches the index with a 32-probe collision cap
  and decodes only the selected candidate extent.
- Each map task emits top-ten/token head candidates from the same Rust terms.
  Global head merges only bounded per-task candidates with deterministic ties,
  then independently verifies the indexed result.
- Local interruption coverage includes local-write, immutable-publish, and
  before-marker failures, marker-last retry/admission, adaptive subdivision,
  multi-task fan-in, mixed-row-group reconciliation, indexed routed/head
  artifacts, and corruption rejection.
- The frozen streaming Python baseline matches Rust exact term count and both
  digest lanes on the hand-authored multilingual/duplicate/rejection fixture.
- The readiness validator is fail-closed over runtime, hashes, the complete
  census/role/run sets, determinism, bindings, speed, resources/headroom,
  amplification, artifacts, interruption/resume, and Worker queries.

## Validation completed

- Focused Python architecture/readiness tests: 7 passed.
- Construction and Worker indexed-query tests passed.
- `cargo clippy -p geocoder-construction -p geocoder-worker --all-targets -- -D warnings`
  passed before the public-read attempt.

## Stop and exact resume point

The first command would have projected frozen task 15 through the focused
read-only projector. Its required network escalation was rejected before
process creation because the session had exhausted its usage/approval limit.

Resume without changing the evidence spec:

1. project and census exactly the 12 tasks above, sequentially;
2. stop at the first frozen source/resource/remote-read gate;
3. select all seven roles with the frozen exclusion/tie rules;
4. run one baseline and two isolated candidate constructions per role;
5. rehearse seven-task adaptive plan/reduce/head plus verifier/Worker queries;
6. replace the intentionally incomplete scale evidence and rerun the
   fail-closed validator.

Hosted orchestration remains out of scope until that local result is
`ready:true` and reviewed.
