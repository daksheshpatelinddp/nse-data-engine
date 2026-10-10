"""
Finds (and optionally removes) "copied days" in the yearly databases.

What is a copied day?  A date whose rows are a copy of the previous trading day:
for almost every share that traded on both days, the close price AND the traded volume are
identical. That never happens on a real trading day, but it does happen when a file for a
holiday/weekend was stored under the wrong date. Real special sessions (Muhurat, Budget day)
have their own prices and are kept.

Safe by default: without APPLY=true nothing is changed, it only prints a report.
Works on every <database>_<year>.db file (nse_2026.db, bse_2026.db, ...). Years are checked in
order, so a copy of 31 December stored on 1 January is found too.

Settings (environment):
  APPLY            true = really delete the copied days (default: report only)
  DUP_THRESHOLD    share of shares that must be identical to call a day a copy (default 0.90)
  DUP_MIN_SYMBOLS  minimum shares traded on both days to judge a day (default 50)
"""
import glob
import os
import re
import sqlite3
import sys
from collections import defaultdict

import numpy as np
import pandas as pd

DB_DIR = os.getenv("DB_DIR", "data/databases")
APPLY = os.getenv("APPLY", "").strip().lower() in ("1", "true", "yes")
THRESHOLD = float(os.getenv("DUP_THRESHOLD", "0.90") or 0.90)
MIN_COMMON = int(os.getenv("DUP_MIN_SYMBOLS", "50") or 50)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def find_databases():
    """{database name: [(year, path), ...] sorted by year}"""
    groups = defaultdict(list)
    for path in glob.glob(os.path.join(DB_DIR, "*_[0-9][0-9][0-9][0-9].db")):
        m = re.match(r"^([A-Za-z0-9]+)_(\d{4})\.db$", os.path.basename(path))
        if m:
            groups[m.group(1).lower()].append((int(m.group(2)), path))
    return {k: sorted(v) for k, v in groups.items()}


def weekday_name(date_str):
    return WEEKDAYS[pd.Timestamp(date_str).weekday()]


def check_year(conn, prev):
    """Returns (flagged, kept_weekend, last_kept, n_dates).

    flagged: [(date, share_identical, n_common, n_rows)]
    prev: DataFrame (index symbol; columns close, volume) of the last kept date before this file.
    """
    df = pd.read_sql_query(
        "SELECT date, symbol, close, volume FROM ohlcv "
        "WHERE is_index = 0 AND close > 0 AND volume > 0 ORDER BY date",
        conn,
    )
    flagged, kept_weekend = [], []
    prev_date = prev[0] if prev else None
    prev_df = prev[1] if prev else None
    n_dates = 0
    for date, g in df.groupby("date", sort=True):
        n_dates += 1
        g = g.drop_duplicates("symbol").set_index("symbol")[["close", "volume"]]
        share, n_common = 0.0, 0
        if prev_df is not None:
            common = g.index.intersection(prev_df.index)
            n_common = len(common)
            if n_common >= MIN_COMMON:
                a, b = g.loc[common], prev_df.loc[common]
                same = np.isclose(a["close"].values, b["close"].values, rtol=2e-3, atol=0.03) & \
                       (a["volume"].values == b["volume"].values)
                share = float(same.mean())
        is_copy = prev_df is not None and n_common >= MIN_COMMON and share >= THRESHOLD
        if is_copy:
            flagged.append((date, share, n_common, len(g)))
        else:
            if weekday_name(date) in ("Sat", "Sun"):
                kept_weekend.append((date, len(g), share))
            prev_date, prev_df = date, g          # later days are compared with the last KEPT day
    return flagged, kept_weekend, (prev_date, prev_df), n_dates


def main():
    dbs = find_databases()
    if not dbs:
        print(f"No database files found in {DB_DIR}.")
        return 0
    mode = "APPLY (deleting)" if APPLY else "PREVIEW (nothing is changed)"
    print(f"[CLEAN] Mode: {mode} | copy threshold {THRESHOLD:.0%} of shares, at least {MIN_COMMON} shares compared")

    total_days, total_rows = 0, 0
    for name, files in dbs.items():
        prev = None
        for year, path in files:
            conn = sqlite3.connect(path)
            try:
                flagged, kept_weekend, prev, n_dates = check_year(conn, prev)
                print(f"\n== {os.path.basename(path)}: {n_dates} trading dates checked ==")
                if kept_weekend:
                    print("   Weekend dates kept (own prices, real special sessions): " +
                          ", ".join(f"{d} ({n} shares)" for d, n, _ in kept_weekend))
                if not flagged:
                    print("   No copied days found.")
                for date, share, n_common, n_rows in flagged:
                    print(f"   COPY  {date} ({weekday_name(date)}): {share:.1%} of {n_common} shares identical "
                          f"to the previous trading day, {n_rows} rows")
                if flagged and APPLY:
                    dates = [f[0] for f in flagged]
                    removed = 0
                    for chunk_start in range(0, len(dates), 500):
                        chunk = dates[chunk_start:chunk_start + 500]
                        marks = ",".join("?" * len(chunk))
                        removed += conn.execute(f"DELETE FROM ohlcv WHERE date IN ({marks})", chunk).rowcount
                    conn.commit()
                    conn.execute("VACUUM")
                    print(f"   DELETED {removed} rows on {len(dates)} copied days from {os.path.basename(path)}")
                    total_rows += removed
                total_days += len(flagged)
            finally:
                conn.close()

    print("\n[CLEAN] ============ SUMMARY ============")
    if total_days == 0:
        print("[CLEAN] No copied days found. Nothing to clean.")
    elif APPLY:
        print(f"[CLEAN] Removed {total_days} copied days ({total_rows} rows). "
              f"Now run 'Convert DB to Parquet & Upload R2' (manual run) to refresh the files on R2.")
    else:
        print(f"[CLEAN] Found {total_days} copied days. Nothing was deleted. "
              f"Run again with 'apply' ticked to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
