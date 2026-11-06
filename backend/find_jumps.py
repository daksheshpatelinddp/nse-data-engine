"""Finds splits / bonuses that are missing from the prices: one-day moves that NSE's 20 % circuit limits do not allow.

    python backend/find_jumps.py [db_dir] [out_dir]        (defaults: data/databases  data/review)

Reads every <database>_<year>.db, compares each close with the previous close of the same stock and writes
    split_candidates.csv        every candidate with the move, the closest usual split ratio, evidence columns, confidence and the reason it is not 'high'
    split_high_confidence_paste.csv    the likely real splits (clean ratio, level moved for good, volume follows the ratio), in the format of manual_splits.csv
    split_medium_confidence_paste.csv  same without the volume proof - look at the chart first.   (NOTHING is applied automatically)
Check each line on a chart (TradingView or the exchange) before copying it into manual_splits.csv. Events already in manual_splits.csv are marked.
Indices (names starting with ^) are ignored.  Thresholds: JUMP_LOW (default 0.78), JUMP_HIGH (default 1.25): a move beyond the 20 % price band.
"""
import csv
import glob
import math
import os
import re
import sqlite3
import sys
from datetime import date

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import splits

# NSE price bands are 2 / 5 / 10 / 20 %, so a one-day close-to-close move beyond 20 % (plus a little room: 0.78 / 1.25) is not a normal trading day.
# Stocks without a band (F&O names), special sessions and IPO days can still move more - the evidence columns sort those out.
LOW = float(os.getenv("JUMP_LOW", "0.78"))
HIGH = float(os.getenv("JUMP_HIGH", "1.25"))
USUAL = [1.25, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10, 20, 25, 50, 100]          # new shares per old share
USUAL = sorted(set(USUAL + [1 / x for x in USUAL if x >= 2]))                 # + reverse splits (0.5, 0.1 ...)
DENY = ("NSE", "BSE", "NIFTY", "NIFTY 50", "SENSEX")


def load(db_dir):
    frames = []
    for p in sorted(glob.glob(os.path.join(db_dir, "*_[0-9][0-9][0-9][0-9].db"))):
        try:
            conn = sqlite3.connect(p)
            frames.append(pd.read_sql_query("SELECT symbol, date, open, close, volume FROM ohlcv WHERE close > 0 AND open > 0 AND symbol IS NOT NULL", conn))
            conn.close()
        except Exception as e:
            print(f"skip {p}: {e}")
    if not frames:
        return pd.DataFrame(columns=["symbol", "date", "open", "close", "volume"])
    df = pd.concat(frames, ignore_index=True)
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df = df[~df["symbol"].str.startswith("^") & ~df["symbol"].str.upper().isin(DENY)]
    return df.drop_duplicates(["symbol", "date"], keep="last").sort_values(["symbol", "date"]).reset_index(drop=True)


def closest(f):
    best = min(USUAL, key=lambda u: abs(math.log(f / u)))
    return best, abs(math.log(f / best))


CLEAN = {2, 3, 4, 5, 10, 20, 25, 50, 100, 0.5, 0.2, 0.1, 0.05, 0.04, 0.02, 0.01, 0.25, 1 / 3, 1.5, 2.5}
WINDOW = 8           # bars looked at on each side of the jump


def _med(a):
    a = a[~pd.isna(a)]
    return float(pd.Series(a).median()) if len(a) else float("nan")


def find(df, listed=None):
    """Candidate rows: close (or open) of a day versus the previous close of the same stock outside [LOW, HIGH], with evidence columns:
    level_before / level_after  median close of the WINDOW bars before / after, relative to prev_close / close  (a split moves the level for good: both ~1)
    vol_ratio                   median volume after / before  (a split of factor f multiplies the share volume by about f; a crash does not)
    confidence                  high: clean ratio, no gap, level moved for good AND volume follows the factor;  medium: same without the volume proof;  low: the rest."""
    g = df.groupby("symbol", sort=False)
    prev = g["close"].shift(1); pdate = g["date"].shift(1)
    r_close = df["close"] / prev; r_open = df["open"] / prev
    bad = ((r_close < LOW) | (r_close > HIGH) | (r_open < LOW) | (r_open > HIGH)) & prev.notna()
    pos = df.index[bad].to_numpy()
    first = g["close"].transform(lambda x: x.index.min()).to_numpy(); last = g["close"].transform(lambda x: x.index.max()).to_numpy()
    close = df["close"].to_numpy(float); vol = pd.to_numeric(df["volume"], errors="coerce").to_numpy(float)
    lb, la, vr = [], [], []
    for i in pos:
        b0, a1 = max(first[i], i - WINDOW), min(last[i], i + WINDOW - 1)
        pb, pa = close[b0:i], close[i:a1 + 1]
        lb.append(_med(pb) / close[i - 1] if len(pb) >= 3 else float("nan"))
        la.append(_med(pa) / close[i] if len(pa) >= 3 else float("nan"))
        vb, va = vol[b0:i], vol[i:a1 + 1]
        mb, ma = _med(vb), _med(va)
        vr.append(ma / mb if mb and mb > 0 and ma == ma else float("nan"))
    c = df.loc[pos, ["symbol", "date", "close"]].copy()
    c["prev_date"] = pdate[bad].to_numpy(); c["prev_close"] = prev[bad].to_numpy(); c["move"] = (c["close"] / c["prev_close"]).round(4)
    c["gap_days"] = [(date.fromisoformat(a) - date.fromisoformat(b)).days for a, b in zip(c["date"], c["prev_date"])]
    fit = [closest(pc / cl) for pc, cl in zip(c["prev_close"], c["close"])]
    c["likely_factor"] = [a for a, b in fit]; c["fit_error"] = [round(b, 3) for a, b in fit]
    c["level_before"] = np.round(lb, 3); c["level_after"] = np.round(la, 3); c["vol_ratio"] = np.round(vr, 2)
    listed = listed or []
    def is_listed(s, d):
        for ls, ld, lf, _ in listed:
            if ls.upper() == s.upper() and abs((date.fromisoformat(ld) - date.fromisoformat(d)).days) <= 5:
                return True
        return False
    c["already_listed"] = [("yes" if is_listed(s, d) else "") for s, d in zip(c["symbol"], c["date"])]
    conf, why = [], []
    for r in c.itertuples():
        clean = any(abs(r.likely_factor - k) < 1e-6 for k in CLEAN)
        stable = (0.75 <= r.level_before <= 1.33) and (0.75 <= r.level_after <= 1.33)
        volok = r.vol_ratio == r.vol_ratio and 0.6 <= r.vol_ratio / r.likely_factor <= 1.65
        notes = []
        if r.gap_days > 10: notes.append(f"gap {r.gap_days} days (suspension / relisting?)")
        if not clean: notes.append("ratio not a usual split ratio")
        if r.fit_error > 0.08: notes.append("move far from the ratio")
        if not stable: notes.append("level did not move for good (spike / bad tick / crash and recovery)")
        if not volok: notes.append("volume did not follow the ratio")
        small = 0.4 < r.likely_factor < 2.5          # 1:4 / 1:2 bonus sized moves overlap with ordinary big moves: never 'high' unless the fit is exact
        if small and r.fit_error > 0.03: notes.append("small ratio: could be an ordinary big move - check the chart")
        if r.gap_days <= 10 and clean and r.fit_error <= 0.08 and stable and volok and not (small and r.fit_error > 0.03): conf.append("high")
        elif r.gap_days <= 10 and clean and r.fit_error <= 0.08 and stable: conf.append("medium")
        else: conf.append("low")
        why.append("; ".join(notes))
    c["confidence"] = conf; c["note"] = why
    return c[["symbol", "date", "prev_date", "gap_days", "prev_close", "close", "move", "likely_factor", "fit_error", "level_before", "level_after", "vol_ratio",
              "confidence", "already_listed", "note"]].reset_index(drop=True)


def main():
    db_dir = sys.argv[1] if len(sys.argv) > 1 else "data/databases"
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.join("data", "review")
    os.makedirs(out, exist_ok=True)
    df = load(db_dir)
    print(f"{len(df):,} rows, {df['symbol'].nunique():,} stocks")
    c = find(df, splits.read_events())
    c.to_csv(os.path.join(out, "split_candidates.csv"), index=False)
    todo = c[c["already_listed"] == ""]
    for level in ("high", "medium"):
        part = todo[todo["confidence"] == level].sort_values(["symbol", "date"])
        with open(os.path.join(out, f"split_{level}_confidence_paste.csv"), "w", newline="") as f:
            w = csv.writer(f); w.writerow(["symbol", "ex_date", "factor", "note"])
            for _, r in part.iterrows():
                w.writerow([r["symbol"], r["date"], f"{r['likely_factor']:g}", f"{level}: close {r['prev_close']:.2f} -> {r['close']:.2f}, volume x{r['vol_ratio']}"])
    print(f"{len(c)} jumps: {(c['already_listed'] == 'yes').sum()} already in manual_splits.csv; to check: "
          f"{(todo['confidence'] == 'high').sum()} high, {(todo['confidence'] == 'medium').sum()} medium, {(todo['confidence'] == 'low').sum()} low -> {out}")
    print(todo[todo["confidence"] == "high"].sort_values("date").head(40).to_string(index=False))


if __name__ == "__main__":
    main()
