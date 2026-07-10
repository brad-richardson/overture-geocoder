# Fix ID Index Build & Enable Version Fallback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the DuckDB httpfs bug that broke ID index builds, make catalog.json preserve previous versions so the worker's fallback mechanism works, and fix old version cleanup to re-upload a filtered catalog after deleting stale data.

**Architecture:** Three independent fixes across two files: a one-line Python fix in `build_id_index.py`, a new workflow step in `rebuild-r2-shards.yml` to seed the local catalog from R2 before building, and an update to the cleanup step to re-upload catalog.json after pruning old versions.

**Tech Stack:** Python 3.11, DuckDB, GitHub Actions, Cloudflare Wrangler CLI

---

### Task 1: Fix missing `_ensure_httpfs_installed()` in `phase_partition_release_r2()`

**Files:**
- Modify: `scripts/build_id_index.py:429-436`

- [ ] **Step 1: Add `_ensure_httpfs_installed()` call**

In `scripts/build_id_index.py`, add the call at the start of `phase_partition_release_r2()`, before the ThreadPoolExecutor block. Insert after the docstring, before the print statement:

```python
def phase_partition_release_r2(prefix_len, release_version, r2_config, version, limit=None):
    """Stage release themes to R2, one parquet file per type.

    Discovers type= sub-directories under each theme and runs them
    in parallel. Each type writes a single file to its own staging
    path so there are no conflicts.
    """
    _ensure_httpfs_installed()
    print(f"  [release] Discovering release types...")
```

This matches the pattern used by `phase_stage_r2()` (line 308), `_run_pool()` (line 619), and `phase_build_r2()` (line 771).

- [ ] **Step 2: Verify no other phases are missing the call**

Run this grep to confirm every phase that uses `_r2_con()` or `LOAD httpfs` has a corresponding `_ensure_httpfs_installed()` call in its parent:

```bash
grep -n "LOAD httpfs\|_ensure_httpfs_installed\|_r2_con" scripts/build_id_index.py
```

Verify that every function calling `_r2_con()` is either `_ensure_httpfs_installed()` itself, or is called from a context where `_ensure_httpfs_installed()` was already called.

- [ ] **Step 3: Commit**

```bash
git add scripts/build_id_index.py
git commit -m "Fix missing httpfs install in release partitioning phase"
```

---

### Task 2: Pin DuckDB version in workflow

**Files:**
- Modify: `.github/workflows/rebuild-r2-shards.yml:76,410,464,506`

- [ ] **Step 1: Pin duckdb version in all pip install steps**

There are four `pip install duckdb` lines in the workflow (one in `rebuild-shards`, three in `id-stage`/`id-build`/`id-post`). Change all of them from:

```yaml
run: pip install duckdb
```

to:

```yaml
run: pip install duckdb==1.5.1
```

The four locations are:
- Line 76 (`rebuild-shards` job, "Install Python dependencies" step)
- Line 410 (`id-stage` job, "Install Python dependencies" step)
- Line 464 (`id-build` job, "Install Python dependencies" step)
- Line 506 (`id-post` job, "Install Python dependencies" step)

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/rebuild-r2-shards.yml
git commit -m "Pin duckdb version to 1.5.1 in workflow"
```

---

### Task 3: Seed local catalog.json from R2 before building shards

**Files:**
- Modify: `.github/workflows/rebuild-r2-shards.yml` (add step between "Install wrangler" and "Download divisions data")

- [ ] **Step 1: Add workflow step to fetch existing catalog from R2**

Add a new step after "Install wrangler" (line 86) and before "Download divisions data" (line 88). This seeds the local `shards/catalog.json` so `build_shards.py`'s existing merge logic preserves previous version links:

```yaml
      - name: Seed catalog from R2
        run: |
          mkdir -p shards
          if wrangler r2 object get geocoder-shards/catalog.json --remote --pipe > shards/catalog.json 2>/dev/null; then
            echo "Seeded shards/catalog.json from R2"
            cat shards/catalog.json | python3 -c "
          import json, sys
          catalog = json.load(sys.stdin)
          versions = [l['href'].split('/')[1] for l in catalog.get('links', []) if l.get('rel') == 'child']
          print(f'  Existing versions: {versions}')
          "
          else
            echo "No existing catalog in R2 (first deploy)"
          fi
```

This works because `build_shards.py` (line 1160-1168) checks if `shards/catalog.json` exists and merges its child version links into `existing_versions`.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/rebuild-r2-shards.yml
git commit -m "Seed catalog.json from R2 to preserve previous version links"
```

---

### Task 4: Update cleanup step to re-upload filtered catalog.json

**Files:**
- Modify: `.github/workflows/rebuild-r2-shards.yml:299-325` (the "Cleanup old versions" step)

- [ ] **Step 1: Add catalog re-upload after pruning old versions**

Replace the existing cleanup step (lines 299-325) with a version that also re-uploads a filtered catalog.json after deleting old version data. The key addition is a Python snippet at the end that removes pruned version links from the catalog:

```yaml
      - name: Cleanup old versions (90+ days)
        if: github.event_name == 'schedule' || github.event.inputs.cleanup_old_versions == 'true'
        run: |
          echo "Checking for old versions to clean up..."
          CUTOFF=$(date -d '-90 days' +%Y-%m-%d)
          echo "Cutoff date: $CUTOFF"

          # List all version directories in R2 by parsing catalog.json
          CATALOG=$(wrangler r2 object get geocoder-shards/catalog.json --remote 2>/dev/null || echo '{}')
          VERSIONS=$(echo "$CATALOG" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}\.[0-9]+' | sort -u || true)

          DELETED_VERSIONS=""
          DELETED_COUNT=0
          for VERSION in $VERSIONS; do
            VERSION_DATE="${VERSION%.*}"  # Remove .0 suffix
            if [[ "$VERSION_DATE" < "$CUTOFF" ]]; then
              echo "Deleting old version: $VERSION"
              # List and delete all objects with this version prefix
              # Note: wrangler doesn't have recursive delete, so we list and delete individually
              OBJECTS=$(wrangler r2 object list geocoder-shards --prefix "${VERSION}/" --remote 2>/dev/null | grep -oE '"key": "[^"]+' | sed 's/"key": "//' || true)
              for OBJ in $OBJECTS; do
                wrangler r2 object delete "geocoder-shards/${OBJ}" --remote 2>/dev/null || true
              done
              DELETED_VERSIONS="$DELETED_VERSIONS $VERSION"
              DELETED_COUNT=$((DELETED_COUNT + 1))
            fi
          done

          echo "Deleted $DELETED_COUNT old versions"

          # Re-upload catalog.json with pruned versions removed
          if [ "$DELETED_COUNT" -gt 0 ]; then
            echo "Updating catalog.json to remove pruned versions..."
            echo "$CATALOG" | python3 -c "
          import json, sys
          deleted = set('$DELETED_VERSIONS'.split())
          catalog = json.load(sys.stdin)
          catalog['links'] = [
              l for l in catalog.get('links', [])
              if l.get('rel') != 'child' or l['href'].split('/')[1] not in deleted
          ]
          json.dump(catalog, sys.stdout, indent=2)
          " > /tmp/catalog-pruned.json
            wrangler r2 object put geocoder-shards/catalog.json \
              --file /tmp/catalog-pruned.json --remote
            echo "Updated catalog.json in R2"
          fi
```

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/rebuild-r2-shards.yml
git commit -m "Re-upload filtered catalog.json after pruning old versions"
```

---

### Task 5: Verify all changes together

- [ ] **Step 1: Run grep to confirm httpfs fix**

```bash
grep -n "_ensure_httpfs_installed" scripts/build_id_index.py
```

Expected: should show the new call inside `phase_partition_release_r2` alongside the existing ones in `phase_stage_r2`, `_run_pool`, and `phase_build_r2`.

- [ ] **Step 2: Run grep to confirm duckdb pinning**

```bash
grep -n "pip install duckdb" .github/workflows/rebuild-r2-shards.yml
```

Expected: all four lines should show `duckdb==1.5.1`.

- [ ] **Step 3: Verify the new "Seed catalog" step exists**

```bash
grep -n "Seed catalog" .github/workflows/rebuild-r2-shards.yml
```

Expected: one match for the new step name.

- [ ] **Step 4: Verify the cleanup step has the re-upload logic**

```bash
grep -n "catalog-pruned" .github/workflows/rebuild-r2-shards.yml
```

Expected: matches for the pruned catalog temp file.

- [ ] **Step 5: Validate YAML syntax**

```bash
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/rebuild-r2-shards.yml'))"
```

If `pyyaml` isn't installed locally, use:

```bash
python3 -c "
import json, subprocess
result = subprocess.run(['python3', '-c', 'import yaml'], capture_output=True)
if result.returncode != 0:
    print('pyyaml not installed, skipping YAML validation')
else:
    import yaml
    yaml.safe_load(open('.github/workflows/rebuild-r2-shards.yml'))
    print('YAML is valid')
"
```

Alternatively, just eyeball the indentation in the diff.
