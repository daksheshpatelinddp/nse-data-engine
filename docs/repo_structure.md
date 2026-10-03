================================================================================
  REPO STRUCTURE - MIGRATION GUIDE
================================================================================

## New target layout

    .github/workflows/
        daily_cron.yml          (updated - paths changed, see below)
        convert.yml             (updated - script path changed)
    backend/
        fetch_data.py           (updated - DB_DIR/CA_DIR constants added)
        convert.py              (updated - DB_DIR constant added)
        requirements.txt        (if you keep a backend-specific one; otherwise
                                  leave the existing requirements.txt at root -
                                  Render/Actions will still find it either way,
                                  no change needed unless you want to split it)
    frontend/
        index.html
        manifest.json            (if present)
        sw.js                    (if present)
        icon-192.png              (if present)
        icon-512.png              (if present)
    data/
        databases/
            nse_2023.db, nse_2024.db, nse_2025.db, nse_2026.db  (move these -
                                  do NOT delete and let them re-fetch)
        corporate_actions/
            CF-CA-equities-*.csv
            manual_adjustments.json
            (old bonus.csv/split.csv/demerger.csv/rights.csv if still present)
    docs/
        project_roadmap_2.txt
        roadmap_phase3_onward.md
        phase3_charting_clone_roadmap.md
        repo_structure.md        (this file)
    main.py                      (STAYS at root - Render's start command
                                   references it, not moving it avoids any
                                   risk of breaking the deploy)
    render.yaml                  (stays at root)
    requirements.txt             (stays at root)
    .gitattributes                (stays at root)

## Why main.py was NOT moved

Render's deploy start command (in render.yaml or the Render dashboard) refers
to main.py's current location. Since I don't have render.yaml's contents to
verify safely, main.py stays exactly where it is - only the FILES IT SERVES
(index.html, manifest.json, etc.) moved, and main.py's internal references to
them were updated accordingly. This was a deliberate choice to avoid risking
another deploy break, given how much deploy troubleshooting this project has
already been through.

## Migration steps (mobile-friendly - no computer needed)

GitHub's mobile web editor lets you MOVE a file by editing it and changing its
path in the filename field at the top - this does a move+commit in one step,
no need to download/re-upload.

For EACH file below: open it on GitHub -> tap the pencil (edit) icon -> tap
the filename/path field at the top (not the file content) -> change the path
to the new location -> commit directly to main.

### Step 1 - move the existing database files (IMPORTANT: move, don't delete)
    nse_2023.db  ->  data/databases/nse_2023.db
    nse_2024.db  ->  data/databases/nse_2024.db
    nse_2025.db  ->  data/databases/nse_2025.db
    nse_2026.db  ->  data/databases/nse_2026.db

### Step 2 - move the corporate action files
    CF-CA-equities-....csv       -> data/corporate_actions/CF-CA-equities-....csv
    manual_adjustments.json      -> data/corporate_actions/manual_adjustments.json
    (bonus.csv/split.csv/etc, if any remain) -> data/corporate_actions/

### Step 3 - move the frontend files
    index.html    -> frontend/index.html
    manifest.json -> frontend/manifest.json   (if it exists)
    sw.js         -> frontend/sw.js           (if it exists)
    icon-192.png  -> frontend/icon-192.png    (if it exists)
    icon-512.png  -> frontend/icon-512.png    (if it exists)

### Step 4 - replace the backend scripts (these are NEW CONTENT, not just a
    move - use the updated files provided, don't just rename the old ones)
    Upload/replace as: backend/fetch_data.py
    Upload/replace as: backend/convert.py

### Step 5 - replace the two workflow files
    Upload/replace as: .github/workflows/daily_cron.yml
    Upload/replace as: .github/workflows/convert.yml

### Step 6 - replace main.py (updated content, same location - root)
    Upload/replace as: main.py  (stays at root, content changed)

### Step 7 - move the roadmap docs (optional, pure housekeeping, zero risk)
    project_roadmap_2.txt              -> docs/project_roadmap_2.txt
    roadmap_phase3_onward.md           -> docs/roadmap_phase3_onward.md
    phase3_charting_clone_roadmap.md   -> docs/phase3_charting_clone_roadmap.md
    repo_structure.md                  -> docs/repo_structure.md

## After migration - verify before moving on

1. Manually trigger "Daily NSE Fetcher" workflow. Check the log: it should
   find your 4 existing .db files in data/databases/ (NOT start fetching from
   scratch). If you see it trying to fetch from 2000/2023 onward again, the
   .db files weren't moved correctly - stop and check Step 1.
2. Manually trigger "Convert DB to Parquet & Upload R2". Should complete
   normally, same as before.
3. Confirm Render still deploys and serves the chart correctly (main.py
   didn't move, so this should be unaffected, but worth one check).

================================================================================
