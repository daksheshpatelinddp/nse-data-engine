"""
Joins the histories of renamed stocks in the yearly databases (INFOSYSTCH -> INFY, ZOMATO -> ETERNAL...).

For every rename in data/symbol_changes.csv the price rows dated BEFORE the effective date are moved to
the new name, and the same is done for the corporate-action records. The corporate-action records must
move too: they are what stops an old split being applied a second time.

PREVIEW by default (changes nothing). APPLY=true really changes the databases.
  SYMBOL_APPLY_SINCE_DAYS  only consider renames effective within the last N days or later (0 = all).
                           The daily job uses this, so a new rename is joined automatically while the
                           old, already-checked ones are never touched again.
Safe to run again and again: after a rename nothing is left under the old name.
"""
import glob
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbol_map as sm

DB_DIR = os.getenv("DB_DIR", "data/databases")
APPLY = os.getenv("APPLY", "").strip().lower() in ("1", "true", "yes")
SINCE_DAYS = int(os.getenv("SYMBOL_APPLY_SINCE_DAYS", "0") or 0)
JUMP_LOW, JUMP_HIGH = 0.6, 1.0 / 0.6      # a join where the price jumps more than this gets flagged


def find_databases():
    groups = defaultdict(list)
    for path in glob.glob(os.path.join(DB_DIR, "*_[0-9][0-9][0-9][0-9].db")):
        m = re.match(r"^([A-Za-z0-9]+)_(\d{4})\.db$", os.path.basename(path))
        if m:
            groups[m.group(1).lower()].append((int(m.group(2)), path))
    return {k: sorted(v) for k, v in groups.items()}


def last_close_before(conns, symbol, date):
    for _, c in reversed(conns):
        r = c.execute("SELECT date, close FROM ohlcv WHERE symbol=? AND date<? AND close>0 ORDER BY date DESC LIMIT 1",
                      (symbol, date)).fetchone()
        if r:
            return r
    return None


def first_close_from(conns, symbol, date):
    for _, c in conns:
        r = c.execute("SELECT date, close FROM ohlcv WHERE symbol=? AND date>=? AND close>0 ORDER BY date ASC LIMIT 1",
                      (symbol, date)).fetchone()
        if r:
            return r
    return None


def main():
    rows = sm.read_rows(sm.DEFAULT_PATH)
    if not rows:
        print(f"[SYMBOLS] No usable renames in {sm.DEFAULT_PATH}. Nothing to do.")
        return 0
    cutoff = (datetime.now() - timedelta(days=SINCE_DAYS)).strftime("%Y-%m-%d") if SINCE_DAYS else None
    changes = sorted({(date, old, new) for old, new, date, _ in rows})     # ascending: chains join in the right order
    if cutoff:
        changes = [c for c in changes if c[0] >= cutoff]
    mapper = sm.SymbolMapper()
    mode = "APPLY (changing the databases)" if APPLY else "PREVIEW (nothing is changed)"
    print(f"[SYMBOLS] Mode: {mode} | {len(changes)} renames considered"
          + (f" (effective since {cutoff})" if cutoff else ""))

    dbs = find_databases()
    if not dbs:
        print(f"[SYMBOLS] No database files in {DB_DIR}.")
        return 0

    for db, files in dbs.items():
        conns = [(y, sqlite3.connect(p)) for y, p in files]
        applied, flagged, collisions = [], [], []
        moved_total = 0
        try:
            for date, old, new in changes:
                present = sum(c.execute("SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND date<?", (old, date)).fetchone()[0]
                              for _, c in conns)
                if not present:
                    continue
                lo, fi = last_close_before(conns, old, date), first_close_from(conns, new, date)
                ratio = (fi[1] / lo[1]) if (lo and fi and lo[1] > 0) else None
                moved = left = 0
                for _, c in conns:
                    if APPLY:
                        moved += c.execute("UPDATE OR IGNORE ohlcv SET symbol=? WHERE symbol=? AND date<?",
                                           (new, old, date)).rowcount
                        left += c.execute("SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND date<?", (old, date)).fetchone()[0]
                    else:
                        n_all = c.execute("SELECT COUNT(*) FROM ohlcv WHERE symbol=? AND date<?", (old, date)).fetchone()[0]
                        # rows whose date already exists under the new name cannot move
                        n_clash = c.execute(
                            "SELECT COUNT(*) FROM ohlcv o WHERE o.symbol=? AND o.date<? AND EXISTS "
                            "(SELECT 1 FROM ohlcv n WHERE n.symbol=? AND n.date=o.date)", (old, date, new)).fetchone()[0]
                        moved += n_all - n_clash
                        left += n_clash
                moved_total += moved
                note = ""
                if ratio is not None:
                    note = f" | join: {lo[1]:g} on {lo[0]} -> {fi[1]:g} on {fi[0]} (x{ratio:.2f})"
                    if not (JUMP_LOW <= ratio <= JUMP_HIGH):
                        flagged.append((date, old, new, lo, fi, ratio))
                        note += "  <-- CHECK"
                elif lo and not fi:
                    note = f" | no data yet under {new}"
                applied.append((date, old, new, moved))
                print(f"   RENAME {date}  {old} -> {new}: {moved} rows {'moved' if APPLY else 'would move'}{note}")
                if left:
                    collisions.append((old, new, left))
                    print(f"      {left} rows {'could NOT move' if APPLY else 'will NOT move'} "
                          f"(the new name already has a row on the same date)")

            # corporate-action records follow their stock, otherwise old splits would be applied twice
            ca_moved = sum(sm.migrate_ca_records(c, mapper, apply=APPLY) for _, c in conns)
            if APPLY:
                for _, c in conns:
                    c.commit()
        finally:
            for _, c in conns:
                c.close()

        print(f"\n[SYMBOLS] {db}: {len(applied)} renames {'applied' if APPLY else 'found'}, "
              f"{moved_total} price rows {'moved' if APPLY else 'would move'}, "
              f"{ca_moved} corporate-action records {'renamed' if APPLY else 'would be renamed'}")
        if collisions:
            print(f"[SYMBOLS] {len(collisions)} renames left some rows behind (same-date clash): "
                  + ", ".join(f"{o}->{n}" for o, n, _ in collisions[:10]))
        if flagged:
            print(f"[SYMBOLS] {len(flagged)} joins show a price jump of more than 40% - look at these charts "
                  f"(a corporate action may be missing, or the two names are different companies):")
            for date, old, new, lo, fi, ratio in flagged[:40]:
                print(f"      {old} -> {new} on {date}: {lo[1]:g} -> {fi[1]:g} (x{ratio:.2f})")
    if not APPLY:
        print("\n[SYMBOLS] Preview only. Run again with 'apply' ticked to join the histories, "
              "then run 'Convert DB to Parquet & Upload R2'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
