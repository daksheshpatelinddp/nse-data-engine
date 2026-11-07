"""Split / bonus adjustment that is applied while the files for R2 are built (like dividends.py): the .db files are never changed.

Why: some splits and bonuses are missing from the prices (they were not in the NSE corporate-action files that were used to adjust the .db files).
A stock that was split 5-for-1 then shows a fake -80% fall on the ex-date, which breaks every indicator and backtest that runs over it.

File:  data/corporate_actions/manual_splits.csv      one line per event
    symbol,ex_date,factor,note
    KRBL,2010-02-10,10,face value 10 -> 1
factor = how many new shares one old share became:  5-for-1 split -> 5;   1:1 bonus -> 2;   1:2 bonus (1 new for 2 held) -> 1.5;
         reverse split 1-for-10 -> 0.1.
Prices BEFORE ex_date are divided by factor, volume and delivery are multiplied by factor. Rows on/after ex_date are untouched.

Safety: before an event is used, the prices in the .db files around ex_date are checked. If there is no visible drop (prev close / next close is
close to 1) the event is treated as ALREADY adjusted and skipped with a warning, so a split can never be applied twice. SPLIT_CHECK=false switches
the check off. SPLIT_ADJUST_VOLUME=false leaves volume alone.

Order: dividends first, then splits (a dividend is quoted per old share, so its factor must be computed on the old, unscaled price).
"""
import csv
import datetime
import glob
import hashlib
import math
import os
import re
import sqlite3

import pandas as pd

SPLIT_FILE = os.getenv("SPLIT_FILE") or os.path.join("data", "corporate_actions", "manual_splits.csv")
CHECK = os.getenv("SPLIT_CHECK", "true").lower() != "false"
PRICE_DECIMALS = 4
ADJUST_VOLUME = os.getenv("SPLIT_ADJUST_VOLUME", "true").lower() != "false"
PRICE_COLS = ("open", "high", "low", "close")


def read_events(path=None):
    """[(SYMBOL, 'YYYY-MM-DD', factor, note)] from the csv; bad lines are reported and skipped."""
    path = path or SPLIT_FILE
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.reader(f) if r and not r[0].strip().startswith("#")]
    if not rows:
        return out
    head = [c.strip().lower() for c in rows[0]]
    if "symbol" in head:
        rows = rows[1:]
    else:
        head = ["symbol", "ex_date", "factor", "note"]
    ix = {k: head.index(k) for k in ("symbol", "ex_date", "factor") if k in head}
    for n, r in enumerate(rows, 2):
        try:
            sym = r[ix["symbol"]].strip().upper()
            d = datetime.date.fromisoformat(r[ix["ex_date"]].strip()).isoformat()
            fac = float(r[ix["factor"]])
            if not sym or not fac > 0 or math.isclose(fac, 1.0):
                raise ValueError("empty symbol or factor <= 0 or factor = 1")
            note = r[head.index("note")].strip() if "note" in head and len(r) > head.index("note") else ""
            out.append((sym, d, fac, note))
        except Exception as e:
            print(f"[SPLITS] {path} line {n}: skipped ({e}): {r}")
    return out


def _db_files(db_dir):
    files = []
    for p in glob.glob(os.path.join(db_dir, "*_[0-9][0-9][0-9][0-9].db")):
        m = re.match(r"^[A-Za-z0-9]+_(\d{4})\.db$", os.path.basename(p))
        if m:
            files.append((int(m.group(1)), p))
    return sorted(files)


def observed_ratio(db_dir, symbol, ex_date, files=None):
    """(previous close) / (first close on or after ex_date), read from the .db files; None when either side is not there."""
    files = files if files is not None else _db_files(db_dir)
    y = int(ex_date[:4]); lo = (datetime.date.fromisoformat(ex_date) - datetime.timedelta(days=21)).isoformat()
    hi = (datetime.date.fromisoformat(ex_date) + datetime.timedelta(days=21)).isoformat()
    rows = []
    for year, p in files:
        if year < y - 1 or year > y + 1:
            continue
        try:
            conn = sqlite3.connect(p)
            rows += conn.execute("SELECT date, close FROM ohlcv WHERE UPPER(TRIM(symbol)) = ? AND date >= ? AND date <= ? AND close > 0 ORDER BY date",
                                 (symbol, lo, hi)).fetchall()
            conn.close()
        except Exception:
            continue
    before = [c for d, c in sorted(rows) if d < ex_date]
    after = [c for d, c in sorted(rows) if d >= ex_date]
    if not before or not after:
        return None
    return before[-1] / after[0]


def _visible(ratio, factor):
    """True when the drop at the ex-date looks like the split really is still in the data."""
    return abs(math.log(ratio)) >= 0.35 * abs(math.log(factor))


class SplitAdjuster:
    def __init__(self, events):
        self.events = events                               # [(symbol, date, factor, note)] already checked
        self.by_symbol = {}
        for s, d, f, n in events:
            self.by_symbol.setdefault(s, []).append((d, f))
        for v in self.by_symbol.values():
            v.sort()
        self.signature = hashlib.sha1(";".join(f"{s}|{d}|{f:.10g}" for s, d, f, n in sorted(events)).encode()).hexdigest()[:16] if events else ""

    def adjust(self, df):
        """Copy of a frame (symbol, date 'YYYY-MM-DD', open, high, low, close, volume, delivery ...) with the events applied. Indices are not touched."""
        if not self.by_symbol or df is None or df.empty or "symbol" not in df.columns:
            return df
        sym = df["symbol"].astype(str).str.strip().str.upper()
        hit = [s for s in self.by_symbol if (sym == s).any()]
        if not hit:
            return df
        out = df.copy()
        dates = out["date"].astype(str)
        for s in hit:
            m = (sym == s).values
            # one combined divisor per row: product of the factors of every event that is still ahead of the row
            div = pd.Series(1.0, index=out.index)
            for d, f in self.by_symbol[s]:
                div[m & (dates < d).values] *= f
            rows = m & (div != 1.0).values
            if not rows.any():
                continue
            for c in PRICE_COLS:
                if c in out.columns:
                    # 4 decimals, like dividends.py: the R2 pack stores prices as integers (price x 10000); a price such as 5.846667 would
                    # force the slow float64 layout that the app cannot always read ("start offset of Float64Array should be a multiple of 8")
                    out.loc[rows, c] = (out.loc[rows, c] / div[rows]).round(PRICE_DECIMALS)
            if ADJUST_VOLUME:
                for c in ("volume", "delivery"):
                    if c in out.columns:
                        col = out[c]
                        scaled = col[rows] * div[rows]
                        if pd.api.types.is_integer_dtype(col.dtype):
                            out[c] = col.astype("float64")
                            out.loc[rows, c] = scaled.round()
                            out[c] = out[c].round().astype(col.dtype)
                        else:
                            out.loc[rows, c] = scaled.round()      # whole shares, so the pack can keep volume as int32
        return out


class Chain:
    """Applies several adjusters one after the other; the signature changes when any of them changes."""
    def __init__(self, parts):
        self.parts = [p for p in parts if p is not None]
        sigs = [f"{type(p).__name__}:{getattr(p, 'signature', '') or ''}" for p in self.parts]
        self.signature = hashlib.sha1("|".join(sigs).encode()).hexdigest()[:16] if any(getattr(p, 'signature', '') for p in self.parts) else ""

    def adjust(self, df):
        for p in self.parts:
            df = p.adjust(df)
        return df


def chain(*parts):
    return Chain(parts)


def build_adjuster(db_dir, path=None):
    events = read_events(path)
    if not events:
        print("[SPLITS] no manual splits (data/corporate_actions/manual_splits.csv is missing or empty)")
        return SplitAdjuster([])
    files = _db_files(db_dir)
    use, skipped = [], []
    for s, d, f, n in events:
        if CHECK and files:
            r = observed_ratio(db_dir, s, d, files)
            if r is not None and not _visible(r, f):
                skipped.append((s, d, f, r)); continue
        use.append((s, d, f, n))
    for s, d, f, r in skipped:
        print(f"[SPLITS] WARNING {s} {d} x{f:g}: prev close / next close = {r:.2f}, no visible drop - looks ALREADY adjusted, event skipped")
    print(f"[SPLITS] {len(use)} manual split/bonus events applied, {len(skipped)} skipped as already adjusted")
    return SplitAdjuster(use)
