"""Finds splits / bonuses that are missing from the prices: one-day moves that NSE's 20 % circuit limits do not allow.

    python backend/find_jumps.py [db_dir] [out_dir]        (defaults: data/databases  data/review)

Reads every <database>_<year>.db, compares each close with the previous close of the same stock and writes
    split_candidates.csv        every candidate with the move, the gap in days and the closest usual split ratio
    split_candidates_paste.csv  the same, in the format of data/corporate_actions/manual_splits.csv (NOT yet applied)
Check each line on a chart (TradingView or the exchange) before copying it into manual_splits.csv. Events already in manual_splits.csv are marked.
Indices (names starting with ^) are ignored.  Thresholds: JUMP_LOW (default 0.55), JUMP_HIGH (default 1.8).
"""
import csv
import glob
import math
import os
import re
import sqlite3
import sys
from datetime import date

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import splits

LOW = float(os.getenv("JUMP_LOW", "0.55"))
HIGH = float(os.getenv("JUMP_HIGH", "1.8"))
USUAL = [1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 25, 50, 100]          # new shares per old share
USUAL = sorted(set(USUAL + [1 / x for x in USUAL if x >= 2]))                 # + reverse splits (0.5, 0.1 ...)
DENY = ("NSE", "BSE", "NIFTY", "NIFTY 50", "SENSEX")


def load(db_dir):
    frames = []
    for p in sorted(glob.glob(os.path.join(db_dir, "*_[0-9][0-9][0-9][0-9].db"))):
        try:
            conn = sqlite3.connect(p)
            frames.append(pd.read_sql_query("SELECT symbol, date, open, close FROM ohlcv WHERE close > 0 AND open > 0 AND symbol IS NOT NULL", conn))
            conn.close()
        except Exception as e:
            print(f"skip {p}: {e}")
    if not frames:
        return pd.DataFrame(columns=["symbol", "date", "open", "close"])
    df = pd.concat(frames, ignore_index=True)
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df = df[~df["symbol"].str.startswith("^") & ~df["symbol"].str.upper().isin(DENY)]
    return df.drop_duplicates(["symbol", "date"], keep="last").sort_values(["symbol", "date"]).reset_index(drop=True)


def closest(f):
    best = min(USUAL, key=lambda u: abs(math.log(f / u)))
    return best, abs(math.log(f / best))


def find(df, listed=None):
    """Candidate rows: close (or open) of a day versus the previous close of the same stock outside [LOW, HIGH]."""
    g = df.groupby("symbol", sort=False)
    prev = g["close"].shift(1); pdate = g["date"].shift(1)
    r_close = df["close"] / prev; r_open = df["open"] / prev
    bad = ((r_close < LOW) | (r_close > HIGH) | (r_open < LOW) | (r_open > HIGH)) & prev.notna()
    c = df[bad].copy()
    c["prev_date"] = pdate[bad]; c["prev_close"] = prev[bad]; c["move"] = (c["close"] / c["prev_close"]).round(4)
    c["gap_days"] = [(date.fromisoformat(a) - date.fromisoformat(b)).days for a, b in zip(c["date"], c["prev_date"])]
    fit = [closest(pc / cl) for pc, cl in zip(c["prev_close"], c["close"])]
    c["likely_factor"] = [a for a, b in fit]; c["fit_error"] = [round(b, 3) for a, b in fit]
    c["note"] = ["gap of weeks: may be a suspension" if gd > 20 else "" for gd in c["gap_days"]]
    listed = listed or []
    def is_listed(s, d):
        for ls, ld, lf, _ in listed:
            if ls.upper() == s.upper() and abs((date.fromisoformat(ld) - date.fromisoformat(d)).days) <= 5:
                return True
        return False
    c["already_listed"] = [("yes" if is_listed(s, d) else "") for s, d in zip(c["symbol"], c["date"])]
    return c[["symbol", "date", "prev_date", "gap_days", "prev_close", "close", "move", "likely_factor", "fit_error", "already_listed", "note"]].reset_index(drop=True)


def main():
    db_dir = sys.argv[1] if len(sys.argv) > 1 else "data/databases"
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join("data", "review")
    os.makedirs(out, exist_ok=True)
    df = load(db_dir)
    print(f"{len(df):,} rows, {df['symbol'].nunique():,} stocks")
    c = find(df, splits.read_events())
    c.to_csv(os.path.join(out, "split_candidates.csv"), index=False)
    todo = c[c["already_listed"] == ""]
    with open(os.path.join(out, "split_candidates_paste.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "ex_date", "factor", "note"])
        for _, r in todo.iterrows():
            w.writerow([r["symbol"], r["date"], f"{r['likely_factor']:g}", f"CHECK: close {r['prev_close']:.2f} -> {r['close']:.2f} ({r['move']}), fit {r['fit_error']}"])
    print(f"{len(c)} jumps, {len(c) - len(todo)} already in manual_splits.csv, {len(todo)} to check -> {out}")
    if len(todo):
        print(todo.sort_values("date").head(40).to_string(index=False))


if __name__ == "__main__":
    main()
