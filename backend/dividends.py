"""
Dividend adjustment (total-return style prices), done at CONVERSION time.

Why not in the .db files like splits and bonuses?
  The .db files keep prices adjusted for splits / bonus / rights / demergers only. Those adjustments are applied once and
  can never be undone. A dividend adjustment is different: every new dividend changes EVERY older price of that stock,
  and the same event shows up in several source files. Done inside the databases, one repeated run would adjust twice.
  Done here, from a plain list of dividends, the result is always computed fresh from the databases, so it can never be
  applied twice, and it can be switched off (DIVIDEND_ADJUST=false) to get the split-only prices back.

How a factor is computed
  For a dividend D (rupees per share) with ex-date X:
      cum_close_raw = last close before X, taken back to the raw price of that day
                      (the databases hold it divided by every split/bonus factor that came AFTER that day)
      factor        = 1 - D / cum_close_raw
  Every row of that stock dated BEFORE X is multiplied by the factor (open, high, low, close; volume is left alone).
  Several dividends multiply together.  Rows dated on or after X are not touched.

Files
  data/corporate_actions/dividends.csv   symbol, ex_date, amount, purpose      (rebuilt from the NSE corporate-action files
                                          on every run; rows found by the live NSE fetch are added and kept)
  data/corporate_actions/dividend-report.txt   what was adjusted / skipped and why   (written by the convert step)

Run by hand:   python backend/dividends.py --rebuild          (re-read the NSE files, rewrite dividends.csv)
               python backend/dividends.py --report           (also compute the factors and write the report; needs the .db files)
"""
import glob
import hashlib
import os
import re
import sqlite3
import sys
from datetime import datetime

import numpy as np
import pandas as pd

CA_DIR = os.getenv("CA_DIR", "data/corporate_actions")
DB_DIR = os.getenv("DB_DIR", "data/databases")
DIVIDENDS_CSV = os.path.join(CA_DIR, "dividends.csv")
REPORT_FILE = os.path.join(CA_DIR, "dividend-report.txt")

MIN_FACTOR = float(os.getenv("DIVIDEND_MIN_FACTOR", "0.5") or 0.5)     # a dividend of more than half the share price is not adjusted (almost surely a data error)
ROUND_TO = int(os.getenv("DIVIDEND_ROUND", "2") or 2)                 # same 2 decimals as the split adjustments in the databases


def enabled():
    return os.getenv("DIVIDEND_ADJUST", "true").strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------------------------------------------------
# 1. reading the dividend amount out of NSE's PURPOSE text
# ---------------------------------------------------------------------------------------------------------------------
_FACE = re.compile(r"(?:face\s*value|f\.?\s*v\.?)\s*(?:of\s*)?(?:rs|re|inr)?\.?\s*[:\-–]*\s*\d+(?:\.\d+)?", re.I)
_RUPEES = re.compile(r"\b(?:rs|re|inr)(?![a-z])\.?[\s:\-–.]*?(\d+(?:\.\d+)?)", re.I)   # 'Rs 5', 'Re.1/-', 'Rs3.50', 'Rs:- 0.70' but not 'Revised' 
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_NOT_DIVIDEND = re.compile(r"dividend\s+distribution\s+tax|ddt\b", re.I)


def parse_dividend(purpose, face_value=None):
    """Per-share dividend amounts (rupees) found in one PURPOSE text. [] if it is not a cash dividend.

    Examples that work:  'Dividend - Rs 5 Per Share'   'Interim Dividend - Re 0.50 Per Share'
                         'Annual General Meeting/Dividend-Rs.20/- Per Share'   'Agm/Dividend-25%' (25% of face value)
                         'Special Interim Dividend-Rs.63/- Per Share'   'Div-30%/Bonus 1:1'
    A purpose that holds two dividends ('Interim Dividend - Rs 2 / Special Dividend - Rs 3') gives two amounts."""
    text = re.sub(r"\s+", " ", str(purpose or "")).strip()
    low = text.lower()
    if not re.search(r"\bdiv(?:idend)?\b|dividend", low):
        return []
    out = []
    for seg in re.split(r"/(?!\s*-)", text):             # a "/" splits two events, but not the "/-" of "Rs 5/-"
        s = seg.strip()
        if not re.search(r"\bdiv(?:idend)?\b|dividend", s, re.I) or _NOT_DIVIDEND.search(s):
            continue
        s = _FACE.sub(" ", s)
        found = [float(x) for x in _RUPEES.findall(s)]
        words = len(re.findall(r"dividend|\bdiv\b", s, re.I))
        if found:
            # 'Dividend Rs 525 & Special Dividend Rs 131': as many dividends as amounts -> add them; otherwise the first amount
            amt = sum(found) if (words > 1 and len(found) <= words) else found[0]
        else:
            p = _PERCENT.search(s)
            fv = _num(face_value)
            amt = (float(p.group(1)) / 100.0 * fv) if (p and fv and fv > 0) else None
        if amt is not None and amt > 0:
            out.append(round(amt, 4))
    return out


def _num(x):
    try:
        v = float(str(x).replace(",", "").strip())
        return v if v == v else None
    except (TypeError, ValueError):
        return None


def _norm_col(c):
    c = str(c).replace("\ufeff", "").strip().upper().replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"_+", "_", re.sub(r"[\s\-]+", "_", c)).strip("_")


def _pick(row, *names):
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip() not in ("", "nan", "None", "-"):
            return str(v).strip()
    return ""


def rows_from_ca_frame(df, mapper=None):
    """NSE corporate-action rows (a frame with the CF-CA columns, or live-API records) -> dividend rows."""
    out = []
    for rec in df.to_dict("records"):
        row = {_norm_col(k): v for k, v in rec.items()}
        series = _pick(row, "SERIES").upper()
        if series and series not in ("EQ", "BE"):
            continue
        symbol = _pick(row, "SYMBOL", "SCRIP", "SECURITY").upper()
        purpose = _pick(row, "PURPOSE", "SUBJECT", "DESCRIPTION", "CORPORATE_ACTION", "REMARKS")
        ex_raw = _pick(row, "EX_DATE", "EXDATE", "EX")
        if not symbol or not purpose or not ex_raw:
            continue
        try:
            ex = pd.to_datetime(ex_raw, dayfirst=True).strftime("%Y-%m-%d")
        except Exception:
            continue
        amounts = parse_dividend(purpose, _pick(row, "FACE_VALUE", "FACEVALUE", "FACEVAL", "FV"))
        if not amounts:
            continue
        if mapper is not None:
            symbol = mapper.canonical(symbol, ex)
        for a in amounts:
            out.append((symbol, ex, a, purpose[:120]))
    return pd.DataFrame(out, columns=["symbol", "ex_date", "amount", "purpose"])


def read_ca_files(files=None):
    files = files if files is not None else (sorted(glob.glob(os.path.join(CA_DIR, "CF-CA-*.csv"))) +
                                              [os.path.join(CA_DIR, n) for n in ("bonus.csv", "split.csv", "demerger.csv", "rights.csv")])
    frames = []
    for f in files:
        if not os.path.exists(f):
            continue
        try:
            try:
                df = pd.read_csv(f, skipinitialspace=True, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(f, skipinitialspace=True, encoding="latin-1")
            frames.append(df)
        except Exception as e:
            print(f"[DIVIDENDS] could not read {f}: {e}")
    return frames


def _dedupe(df):
    if df.empty:
        return df
    df = df.sort_values(["symbol", "ex_date", "amount"]).drop_duplicates(["symbol", "ex_date", "amount"], keep="first")
    return df.reset_index(drop=True)


def rebuild_store(mapper=None, extra=None, path=None, files=None):
    """Re-reads the NSE corporate-action files, adds `extra` (rows from the live fetch) and what the store already holds
    (so an event found live is never lost), writes dividends.csv. Returns the table."""
    path = path or DIVIDENDS_CSV
    parts = [rows_from_ca_frame(df, mapper) for df in read_ca_files(files)]
    if extra is not None and len(extra):
        parts.append(extra)
    old = load_store(path)
    if len(old):
        parts.append(old)
    df = _dedupe(pd.concat([p for p in parts if len(p)] or [pd.DataFrame(columns=["symbol", "ex_date", "amount", "purpose"])], ignore_index=True))
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    df.to_csv(path, index=False)
    return df


def load_store(path=None):
    path = path or DIVIDENDS_CSV
    if not os.path.exists(path):
        return pd.DataFrame(columns=["symbol", "ex_date", "amount", "purpose"])
    df = pd.read_csv(path, encoding="utf-8-sig")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
    return df.dropna(subset=["symbol", "ex_date", "amount"])


# ---------------------------------------------------------------------------------------------------------------------
# 2. factors from the databases
# ---------------------------------------------------------------------------------------------------------------------
def _year_dbs(db_dir):
    out = []
    for f in sorted(glob.glob(os.path.join(db_dir, "*_[0-9][0-9][0-9][0-9].db"))):
        m = re.match(r"^nse_(\d{4})\.db$", os.path.basename(f))     # the dividend step works on the stock database "nse"
        if m:
            out.append((int(m.group(1)), f))
    return out


def load_split_factors(dbs):
    """{symbol: [(ex_date, ratio), ...]} from every database's corporate_actions table (each holds the same list)."""
    seen, out = set(), {}
    for _, path in dbs:
        try:
            c = sqlite3.connect(path)
            rows = c.execute("SELECT symbol, ex_date, ratio, action_type FROM corporate_actions WHERE ratio > 0").fetchall()
            c.close()
        except sqlite3.Error:
            continue
        for sym, ex, ratio, typ in rows:
            key = (sym.upper(), ex, typ)
            if key in seen:
                continue
            seen.add(key)
            out.setdefault(sym.upper(), []).append((ex, float(ratio)))
    return out


def _last_close_before(conns, symbol, ex_date):
    """Last positive close of `symbol` before ex_date (newest database first). None if the stock has no earlier row."""
    year = int(ex_date[:4])
    for y in (year, year - 1, year - 2):
        c = conns.get(y)
        if not c:
            continue
        r = c.execute("SELECT close FROM ohlcv WHERE symbol = ? AND date < ? AND is_index = 0 AND close > 0 "
                      "ORDER BY date DESC LIMIT 1", (symbol, ex_date)).fetchone()
        if r:
            return float(r[0])
    return None


def compute_factors(dividends=None, db_dir=None, asof=None):
    """-> (factors, report_lines). factors = DataFrame(symbol, ex_date, factor, amount, cum_close_raw)."""
    db_dir = db_dir or DB_DIR
    dividends = load_store() if dividends is None else dividends
    dbs = _year_dbs(db_dir)
    report = []
    if not dbs or dividends.empty:
        return pd.DataFrame(columns=["symbol", "ex_date", "factor", "amount", "cum_close_raw"]), ["No databases or no dividends: nothing to adjust."]
    conns = {y: sqlite3.connect(p) for y, p in dbs}
    try:
        if asof is None:
            asof = max(c.execute("SELECT MAX(date) FROM ohlcv WHERE is_index = 0").fetchone()[0] or "0000" for c in conns.values())
        splits = load_split_factors(dbs)
        rows, skipped = [], {"future": 0, "no_price": 0, "too_big": 0, "bad": 0}
        notes = []
        for sym, ex, amt in dividends[["symbol", "ex_date", "amount"]].itertuples(index=False):
            sym = str(sym).upper()
            if ex > asof:
                skipped["future"] += 1
                continue
            cum_db = _last_close_before(conns, sym, ex)
            if not cum_db:
                skipped["no_price"] += 1
                continue
            later = 1.0
            for ex_a, ratio in splits.get(sym, ()):
                if ex_a >= ex:
                    later *= ratio               # the database already divided older prices by these
            raw = cum_db / later if later > 0 else cum_db
            if not (raw > 0) or not (amt > 0):
                skipped["bad"] += 1
                continue
            f = 1.0 - amt / raw
            if f < MIN_FACTOR:
                skipped["too_big"] += 1
                if len(notes) < 60:
                    notes.append(f"  skipped (dividend {amt:g} is {100 * amt / raw:.0f}% of the price {raw:.2f}): {sym} {ex}")
                continue
            rows.append((sym, ex, f, float(amt), raw))
    finally:
        for c in conns.values():
            c.close()
    fac = pd.DataFrame(rows, columns=["symbol", "ex_date", "factor", "amount", "cum_close_raw"])
    # several dividends of one stock on one ex-date multiply into one factor
    if len(fac):
        fac = fac.groupby(["symbol", "ex_date"], as_index=False).agg(factor=("factor", "prod"), amount=("amount", "sum"), cum_close_raw=("cum_close_raw", "first"))
    report.append(f"Dividends in the list: {len(dividends)}; adjusted: {len(rows)}; skipped: future ex-date {skipped['future']}, "
                  f"no price before the ex-date {skipped['no_price']}, bigger than {int((1 - MIN_FACTOR) * 100)}% of the price {skipped['too_big']}, bad data {skipped['bad']}.")
    if len(fac):
        report.append(f"Average adjustment per dividend: {100 * (1 - fac['factor'].mean()):.2f}%   largest: {100 * (1 - fac['factor'].min()):.1f}% ({fac.loc[fac['factor'].idxmin(), 'symbol']} {fac.loc[fac['factor'].idxmin(), 'ex_date']})")
    report += notes
    return fac, report


# ---------------------------------------------------------------------------------------------------------------------
# 3. applying them
# ---------------------------------------------------------------------------------------------------------------------
class DividendAdjuster:
    """adjust(frame) multiplies open/high/low/close of every row by the product of the factors of all dividends of that stock
    whose ex-date is AFTER the row's date."""

    def __init__(self, factors):
        self.by_symbol = {}
        if factors is not None and len(factors):
            for sym, g in factors.sort_values("ex_date").groupby("symbol"):
                ex = np.array(g["ex_date"].values, dtype="datetime64[D]")
                f = g["factor"].values.astype("float64")
                suffix = np.append(np.cumprod(f[::-1])[::-1], 1.0)         # suffix[i] = product of f[i:], suffix[k] = 1
                self.by_symbol[sym] = (ex, suffix)
        h = hashlib.sha1()
        if factors is not None and len(factors):
            for r in factors.sort_values(["symbol", "ex_date"]).itertuples(index=False):
                h.update(f"{r.symbol}|{r.ex_date}|{r.factor:.8f};".encode())
        h.update(f"round{ROUND_TO}".encode())
        self.signature = h.hexdigest()[:16] if self.by_symbol else ""         # "" = nothing to adjust (so old fingerprints stay valid)

    def __bool__(self):
        return bool(self.by_symbol)

    def multiplier(self, symbol, dates):
        """Array of factors for the given dates (strings or datetime64) of one symbol."""
        hit = self.by_symbol.get(symbol)
        d = np.asarray(dates, dtype="datetime64[D]")
        if hit is None:
            return np.ones(len(d))
        ex, suffix = hit
        return suffix[np.searchsorted(ex, d, side="right")]

    def adjust(self, df):
        """Returns df with adjusted prices (a copy). df needs columns symbol, date, open, high, low, close."""
        if not self.by_symbol or df.empty:
            return df
        df = df.copy()
        mult = np.ones(len(df))
        syms = df["symbol"].values
        for sym in set(self.by_symbol) & set(np.unique(syms)):
            idx = np.nonzero(syms == sym)[0]
            mult[idx] = self.multiplier(sym, df["date"].values[idx])
        touched = mult != 1.0
        if touched.any():
            for col in ("open", "high", "low", "close"):
                df.loc[touched, col] = np.round(df.loc[touched, col].values * mult[touched], ROUND_TO)
        return df


def build_adjuster(db_dir=None):
    """The adjuster used by convert.py. Empty (does nothing) when disabled or when there is no dividend list."""
    if not enabled():
        print("[DIVIDENDS] disabled (DIVIDEND_ADJUST=false): prices are adjusted for splits/bonus/rights/demergers only.")
        return DividendAdjuster(None)
    div = load_store()
    if div.empty:
        print("[DIVIDENDS] no dividends.csv: prices are adjusted for splits/bonus/rights/demergers only.")
        return DividendAdjuster(None)
    fac, report = compute_factors(div, db_dir)
    for line in report[:4]:
        print("[DIVIDENDS]", line)
    return DividendAdjuster(fac)


def main(argv):
    if "--rebuild" in argv or "--report" in argv:
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            from symbol_map import SymbolMapper
            mapper = SymbolMapper()
        except Exception:
            mapper = None
        df = rebuild_store(mapper if mapper else None)
        print(f"[DIVIDENDS] {len(df)} dividends from {df['symbol'].nunique() if len(df) else 0} stocks written to {DIVIDENDS_CSV} "
              f"({df['ex_date'].min() if len(df) else '-'} .. {df['ex_date'].max() if len(df) else '-'})")
    if "--report" in argv:
        fac, report = compute_factors(load_store())
        os.makedirs(CA_DIR, exist_ok=True)
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            f.write(f"Dividend adjustment report, {datetime.now():%Y-%m-%d %H:%M}\n" + "\n".join(report) + "\n")
        print("\n".join(report))


if __name__ == "__main__":
    main(sys.argv[1:])
