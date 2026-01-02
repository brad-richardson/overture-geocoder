# D1 Rebuild Optimization Plan

## Problem Summary

Current rebuilds use `INSERT OR REPLACE`, which in SQLite:
1. Deletes the existing row when a conflict occurs
2. Reinserts the new row
3. Triggers FTS delete + reinsert operations per row

This causes **write amplification** that exhausts D1's 50M rows/month included limit during development iterations.

## Solution: DB Swap + Phased Loading

### Strategy Overview

Instead of modifying the active database, we:
1. **Create a fresh D1 database** (e.g., `geocoder-dev-v42`)
2. **Load data optimally** (INSERT-only, deferred indexes)
3. **Swap the binding** in wrangler.toml and redeploy
4. **Delete old database** after validation

### Benefits
- Zero writes to active production DB during rebuild
- Eliminates index/FTS maintenance overhead during bulk load
- Trivial rollback (just keep the old DB)
- Clean separation between rebuild (expensive) and incremental (cheap)

---

## Implementation Details

### 1. Split Schema into Phases

**Current:** Single `schema.sql` with everything

**New:** Three-phase schema files

```
schema-base.sql     # Tables only (no indexes, no constraints except PK)
schema-indexes.sql  # UNIQUE constraints and B-tree indexes
schema-fts.sql      # FTS5 virtual tables and triggers
```

#### schema-base.sql (Forward)
```sql
CREATE TABLE metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE divisions (
    rowid INTEGER PRIMARY KEY,
    gers_id TEXT NOT NULL,           -- No UNIQUE yet
    version INTEGER NOT NULL DEFAULT 0,
    type TEXT NOT NULL,
    primary_name TEXT NOT NULL,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    bbox_xmin REAL NOT NULL,
    bbox_ymin REAL NOT NULL,
    bbox_xmax REAL NOT NULL,
    bbox_ymax REAL NOT NULL,
    population INTEGER,
    country TEXT,
    region TEXT,
    search_text TEXT NOT NULL
);
```

#### schema-indexes.sql (Forward)
```sql
-- Add UNIQUE constraint after data is loaded
CREATE UNIQUE INDEX idx_divisions_gers_id ON divisions(gers_id);
```

#### schema-fts.sql (Forward)
```sql
CREATE VIRTUAL TABLE divisions_fts USING fts5(
    search_text,
    content=divisions,
    content_rowid=rowid,
    tokenize='porter unicode61 remove_diacritics 1',
    prefix='2 3'
);

-- Rebuild FTS from existing data (one-time bulk operation)
INSERT INTO divisions_fts(divisions_fts) VALUES('rebuild');

-- Add triggers for incremental updates
CREATE TRIGGER divisions_ai AFTER INSERT ON divisions BEGIN
    INSERT INTO divisions_fts(rowid, search_text)
    VALUES (new.rowid, new.search_text);
END;

CREATE TRIGGER divisions_ad AFTER DELETE ON divisions BEGIN
    INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
END;

CREATE TRIGGER divisions_au AFTER UPDATE ON divisions BEGIN
    INSERT INTO divisions_fts(divisions_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO divisions_fts(rowid, search_text)
    VALUES (new.rowid, new.search_text);
END;
```

### 2. Export Modes

Modify `export_to_sql.py` to support two modes:

```bash
# Rebuild mode: plain INSERT for fresh DB
python scripts/export_to_sql.py --mode rebuild ...

# Incremental mode: proper UPSERT for live updates
python scripts/export_to_sql.py --mode incremental ...
```

**Rebuild mode output:**
```sql
INSERT INTO divisions (...) VALUES (...);
```

**Incremental mode output:**
```sql
INSERT INTO divisions (...) VALUES (...)
ON CONFLICT(gers_id) DO UPDATE SET
    version = excluded.version,
    type = excluded.type,
    -- ... only update if actually changed
    WHERE divisions.version < excluded.version;
```

### 3. GitHub Actions: DB Swap Workflow

New workflow: `.github/workflows/rebuild-with-swap.yml`

```yaml
name: Rebuild Database (DB Swap)

on:
  workflow_dispatch:
    inputs:
      database:
        type: choice
        options: [forward, reverse]
      suffix:
        description: 'DB name suffix (e.g., v42)'
        required: true

jobs:
  rebuild:
    steps:
      # 1. Create new database
      - name: Create fresh D1 database
        run: |
          NEW_DB="geocoder-${{ inputs.database }}-${{ inputs.suffix }}"
          wrangler d1 create "$NEW_DB" --json > db-info.json

      # 2. Apply base schema (tables only)
      - name: Apply base schema
        run: wrangler d1 execute $NEW_DB --file=schema-base.sql

      # 3. Download and build data
      - name: Build local index
        run: |
          duckdb < scripts/download_divisions_global.sql
          python scripts/build_divisions_index.py

      # 4. Export with INSERT-only
      - name: Export data
        run: python scripts/export_to_sql.py --mode rebuild ...

      # 5. Bulk load data
      - name: Load data chunks
        run: |
          for chunk in exports/data-*.sql; do
            wrangler d1 execute $NEW_DB --file="$chunk"
          done

      # 6. Create indexes (after data load)
      - name: Create indexes
        run: wrangler d1 execute $NEW_DB --file=schema-indexes.sql

      # 7. Create FTS and rebuild
      - name: Build FTS index
        run: wrangler d1 execute $NEW_DB --file=schema-fts.sql

      # 8. Update wrangler.toml
      - name: Update binding
        run: |
          # Update database_id in wrangler.toml
          NEW_ID=$(jq -r '.uuid' db-info.json)
          sed -i "s/database_id = .*/database_id = \"$NEW_ID\"/" wrangler.toml

      # 9. Deploy worker
      - name: Deploy with new binding
        run: wrangler deploy

      # 10. Validate
      - name: Validate new database
        run: |
          curl https://your-worker.workers.dev/search?q=boston

      # 11. Commit and push
      - name: Commit binding update
        run: |
          git add wrangler.toml
          git commit -m "chore: switch to $NEW_DB"
          git push
```

### 4. Update Incremental Path

Modify `diff_and_update.py` to use proper UPSERT:

```sql
-- Current (causes delete+insert):
INSERT OR REPLACE INTO divisions (...) VALUES (...);

-- New (true upsert, no delete):
INSERT INTO divisions (...) VALUES (...)
ON CONFLICT(gers_id) DO UPDATE SET
    version = excluded.version,
    type = excluded.type,
    primary_name = excluded.primary_name,
    lat = excluded.lat,
    lon = excluded.lon,
    bbox_xmin = excluded.bbox_xmin,
    bbox_ymin = excluded.bbox_ymin,
    bbox_xmax = excluded.bbox_xmax,
    bbox_ymax = excluded.bbox_ymax,
    population = excluded.population,
    country = excluded.country,
    region = excluded.region,
    search_text = excluded.search_text;
```

---

## File Changes Summary

| File | Changes |
|------|---------|
| `scripts/export_to_sql.py` | Add `--mode` flag, split schema generation into phases |
| `scripts/diff_and_update.py` | Change to proper `ON CONFLICT` UPSERT |
| `.github/workflows/rebuild-with-swap.yml` | **New** - DB swap workflow |
| `.github/workflows/data-update.yml` | Minor: use incremental mode |
| `.github/workflows/schema-apply-*.yml` | **Remove** or deprecate |

---

## Migration Path

1. **Phase 1**: Update scripts (export modes, UPSERT syntax)
2. **Phase 2**: Create rebuild workflow
3. **Phase 3**: Test with dev database
4. **Phase 4**: Document new workflow, deprecate schema-apply

---

## Expected Outcomes

| Metric | Before | After |
|--------|--------|-------|
| Rebuild write cost | ~15M rows (4.3M × 3-4x amplification) | ~4.5M rows (data + one-time FTS) |
| Incremental writes | DELETE + INSERT per row | Single UPSERT per row |
| Rollback complexity | Manual data restore | Keep old DB |
| Dev iteration cost | Hits D1 limits quickly | Sustainable |
