# Overture Geocoder - Project Guide

Five things that are not derivable from the source. Everything else — layout,
build commands, CI gates, commit style — read it from the repo.

## 1. Optimize for the next measured milestone

The objective is the shortest safe path to the next non-promoting planet-scale
execution milestone for Places and Addresses. Work in this order:

1. A measured blocker that prevents the next execution rung.
2. A defect that would corrupt output, make resume unsafe, invalidate the next
   measurement, or waste a planet run.
3. Everything else is recorded and deferred.

Hygiene, speculative hardening, design completeness, and last-mile optimization
are not active work while a measured planet blocker is open. "P1" is
milestone-relative: a finding blocks only if it prevents or invalidates the next
agreed milestone.

## 2. Read the state doc first

`docs/plans/construction-v1-state.md` is the living operational snapshot. The
dated docs in `docs/plans/` are point-in-time analyses; the state doc supersedes
them where they disagree. Check it before touching construction-v1, and fold
durable results back into it.

## 3. Use the fast loop, then jump straight to scale

`scripts/build_slice_inventory_v1.py` + `scripts/run_slice_construction_v1.py`
run all five construction phases on real Overture data in ~13 seconds with no
credentials. Nothing else in `scripts/` is that path.

Use the cheapest rung that can falsify the current hypothesis, then advance:
unit/contract tests → Monaco Places / Seattle Addresses slice → representative
scale probe → non-promoting planet run. The small slice is a correctness gate,
not planet-scale evidence — once it passes, stop expanding tests there when the
open risk is RAM, disk, shard fan-out, object count, remote semantics, or wall
time.

## 4. The address map shuffle will NOT be ported

Decided 2026-07-25. Address map output is already hash-clustered (`pack_id` is a
row counter over `TOTAL_ORDER`, which begins `country, maximum_bucket` = the top
16 bits of `route_hash`), so the shuffle buys little; the transport cost is
whole-object hydration at reduce, not layout. The fix is a range-owning reducer.

The address partition key (`address_key_hash` / `route_hash` / `hash_bucket`)
and `MAXIMUM_HASH_BITS` are **FROZEN**. See the "DEFERRED, do not lose" section
of `docs/plans/2026-07-24-construction-v1-follow-ups.md`.

## 5. The Worker is wasm32 — some crates silently aren't

`crates/geocoder-worker` compiles to `wasm32-unknown-unknown`. The Cargo.toml
shows the resulting feature flags but not why:

- `parquet`: `snap` feature only. **zstd does not compile to wasm32**, and snap
  is also required for backward compatibility with older Snappy shards.
- `rusqlite`: uses the Workers runtime's built-in SQLite via
  `from_bytes`/`deserialize`, not a bundled engine.

Any dependency added here must compile to wasm32.
</content>
</invoke>
