"""
NSE index data (Nifty 50, Nifty 500, every sector / thematic / strategy index NSE publishes) -> the same .db files.

Index rows are stored in the same yearly databases as the stocks (table ohlcv, is_index = 1) under a name that starts
with "^" and has no spaces, so that
  * the app can tell them from stocks (a scan of "all symbols" never includes an index),
  * a formula can use them:  SetForeign("^NIFTY500");   and the chart can show them like any stock,
  * the name works in every list the app splits at spaces.
      "Nifty 50" -> ^NIFTY50     "Nifty Bank" -> ^NIFTYBANK     "Nifty 500" -> ^NIFTY500     "India VIX" -> ^INDIAVIX

Sources, tried in this order for every trading day that has stock data
  1. NSE's daily file of ALL indices:  ind_close_all_<ddmmyyyy>.csv   (two NSE hosts)
  2. files you put into  data/index_files/  (for years NSE's daily files do not reach: download them from niftyindices.com,
     "Historical Data" of each index, and drop the CSV there; read once, then moved to data/index_files/done/)
  3. optional, experimental:  INDEX_API_BACKFILL=true  asks niftyindices.com for the history of the indices listed in
     data/index_names.txt (cloud machines are often refused there; nothing is stored unless the rows pass every check)
"""
import csv
import glob
import io
import json
import os
import re
import shutil
import time
from datetime import datetime, timedelta

import pandas as pd

INDEX_FILES_DIR = os.getenv("INDEX_FILES_DIR", "data/index_files")
INDEX_NAMES_JSON = os.getenv("INDEX_NAMES_JSON", "data/index_names.json")       # {"^NIFTY50": "Nifty 50", ...}  (official names, for the app)
INDEX_ALIASES_JSON = os.getenv("INDEX_ALIASES_JSON", "data/index_aliases.json")  # optional {"OLD NAME": "NEW NAME"}, extends the built-in renames

URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv",
    "https://archives.nseindia.com/content/indices/ind_close_all_{ddmmyyyy}.csv",
]

# NSE renamed its indices over the years (CNX -> Nifty, S&P CNX ...). Old and new name must give ONE continuous series.
_BUILTIN_RENAMES = {
    "NIFTY": "NIFTY 50", "S&P CNX NIFTY": "NIFTY 50", "CNX NIFTY": "NIFTY 50", "NIFTY 50 INDEX": "NIFTY 50",
    "CNX NIFTY JUNIOR": "NIFTY NEXT 50", "NIFTY JUNIOR": "NIFTY NEXT 50", "JUNIOR NIFTY": "NIFTY NEXT 50",
    "S&P CNX 500": "NIFTY 500", "CNX 500": "NIFTY 500", "CNX 100": "NIFTY 100", "CNX 200": "NIFTY 200",
    "CNX MIDCAP": "NIFTY MIDCAP 100", "NIFTY MIDCAP": "NIFTY MIDCAP 100", "CNX NIFTY MIDCAP": "NIFTY MIDCAP 100",
    "CNX SMALLCAP": "NIFTY SMALLCAP 100", "NIFTY SMALLCAP": "NIFTY SMALLCAP 100",
    "CNX BANK": "NIFTY BANK", "BANK NIFTY": "NIFTY BANK", "CNX IT": "NIFTY IT", "CNX INFRA": "NIFTY INFRASTRUCTURE",
    "NIFTY INFRA": "NIFTY INFRASTRUCTURE", "CNX FMCG": "NIFTY FMCG", "CNX PHARMA": "NIFTY PHARMA", "CNX AUTO": "NIFTY AUTO",
    "CNX FINANCE": "NIFTY FINANCIAL SERVICES", "NIFTY FINANCE": "NIFTY FINANCIAL SERVICES", "CNX ENERGY": "NIFTY ENERGY",
    "CNX METAL": "NIFTY METAL", "CNX REALTY": "NIFTY REALTY", "CNX MEDIA": "NIFTY MEDIA", "CNX PSE": "NIFTY PSE",
    "CNX PSU BANK": "NIFTY PSU BANK", "CNX COMMODITIES": "NIFTY COMMODITIES", "CNX CONSUMPTION": "NIFTY CONSUMPTION",
    "CNX MNC": "NIFTY MNC", "CNX SERVICE": "NIFTY SERVICES SECTOR", "NIFTY SERVICES": "NIFTY SERVICES SECTOR",
    "CNX DIVIDEND OPPORTUNITIES": "NIFTY DIVIDEND OPPORTUNITIES 50", "CNX NIFTY SHARIAH": "NIFTY50 SHARIAH",
    "INDIA VIX": "INDIA VIX", "INDIAVIX": "INDIA VIX",
}


_ALIASES = None


def _load_aliases():
    global _ALIASES
    if _ALIASES is not None:
        return _ALIASES
    out = dict(_BUILTIN_RENAMES)
    try:
        with open(INDEX_ALIASES_JSON, encoding="utf-8") as f:
            for k, v in json.load(f).items():
                out[re.sub(r"\s+", " ", str(k).upper().strip())] = re.sub(r"\s+", " ", str(v).upper().strip())
    except (OSError, ValueError):
        pass
    _ALIASES = out
    return out


def official_name(raw):
    """NSE's name of the index, upper case, one space between words, renamed to the current name."""
    name = re.sub(r"\s+", " ", str(raw or "").replace("﻿", "")).strip().upper()
    return _load_aliases().get(name, name)


def index_symbol(raw):
    """'Nifty 50' -> '^NIFTY50'. Keeps letters and digits only."""
    core = re.sub(r"[^A-Z0-9]", "", official_name(raw))
    return f"^{core}" if core else ""


def is_index_symbol(sym):
    return str(sym).startswith("^")


# ---------------------------------------------------------------------------------------------------------------------
# reading files
# ---------------------------------------------------------------------------------------------------------------------
def _norm(c):
    return re.sub(r"[^A-Z0-9]", "", str(c).upper())


def _col(df, *names):
    lookup = {}
    for c in df.columns:
        lookup.setdefault(_norm(c), c)
    for n in names:
        if _norm(n) in lookup:
            return df[lookup[_norm(n)]]
    return None


def _num(series):
    s = series.astype(str).str.replace(",", "", regex=False).str.strip()
    s = s.mask(s.isin(["-", "", "nan", "None", "NaN", "--"]))
    return pd.to_numeric(s, errors="coerce")


def _date(series):
    t = series.astype(str).str.strip()
    out = pd.to_datetime(t, format="%d-%m-%Y", errors="coerce")
    miss = out.isna()
    if miss.any():
        out[miss] = pd.to_datetime(t[miss], dayfirst=True, errors="coerce", format="mixed")
    return out


def parse_index_frame(df, date_str=None, default_name=None):
    """A table of index values -> tidy rows (symbol, name, date, open, high, low, close, volume).

    Understands NSE's all-indices file (Index Name, Index Date, Open Index Value, High Index Value, Low Index Value,
    Closing Index Value, Volume ...) and the CSV of niftyindices.com 'Historical Data' (Index Name, Date, Open, High,
    Low, Close, ...). Where NSE gives no open/high/low (some indices) they are set to the close. If date_str is given the
    file must be for that day, otherwise nothing is returned (never store a file under the wrong day).
    Returns (rows, error)."""
    name_col = _col(df, "Index Name", "INDEX_NAME", "Name", "Index")
    date_col = _col(df, "Index Date", "Date", "HistoricalDate", "Timestamp", "INDEX_DATE")
    close = _col(df, "Closing Index Value", "Close", "CLOSE", "Closing Value", "Close Index Value")
    if close is None:
        return None, f"no close column, columns were {list(df.columns)[:12]}"
    if name_col is None and not default_name:
        return None, "no index name column (and no name could be taken from the file name)"
    if date_col is None and not date_str:
        return None, "no date column"
    out = pd.DataFrame({
        "name": name_col.astype(str) if name_col is not None else default_name,
        "date": _date(date_col) if date_col is not None else pd.Timestamp(date_str),
        "close": _num(close),
        "open": _num(_col(df, "Open Index Value", "Open", "OPEN")) if _col(df, "Open Index Value", "Open", "OPEN") is not None else float("nan"),
        "high": _num(_col(df, "High Index Value", "High", "HIGH")) if _col(df, "High Index Value", "High", "HIGH") is not None else float("nan"),
        "low": _num(_col(df, "Low Index Value", "Low", "LOW")) if _col(df, "Low Index Value", "Low", "LOW") is not None else float("nan"),
    })
    vol = _col(df, "Volume", "Shares Traded", "Volume (Shares)")
    out["volume"] = _num(vol) if vol is not None else float("nan")
    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0]
    if date_str:
        want = pd.Timestamp(date_str)
        if len(out) and (out["date"] != want).mean() > 0.5:
            return None, f"file is for {out['date'].mode().iloc[0]:%Y-%m-%d}, not {date_str}"
        out = out[out["date"] == want]
    for c in ("open", "high", "low"):
        out[c] = out[c].where(out[c] > 0, out["close"])
    out["high"] = out[["high", "open", "close"]].max(axis=1)           # never an impossible bar
    out["low"] = out[["low", "open", "close"]].min(axis=1)
    out["volume"] = out["volume"].fillna(0).clip(lower=0)
    out["symbol"] = [index_symbol(n) for n in out["name"]]
    out["name"] = [official_name(n) for n in out["name"]]
    out = out[out["symbol"] != ""]
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out = out.drop_duplicates(["symbol", "date"], keep="last")           # an old and a new name of one index on the same day
    return out[["symbol", "name", "date", "open", "high", "low", "close", "volume"]], None


def fetch_index_day(session, dt, get):
    """Downloads the all-indices file of one day. get(session, url) -> response. Returns (rows, source, last_error)."""
    date_str = dt.strftime("%Y-%m-%d")
    last = None
    for tpl in URLS:
        url = tpl.format(ddmmyyyy=dt.strftime("%d%m%Y"))
        try:
            res = get(session, url)
            if res.status_code != 200:
                last = f"HTTP {res.status_code} from {url}"
                continue
            df = pd.read_csv(io.StringIO(res.text), skipinitialspace=True)
            rows, err = parse_index_frame(df, date_str)
            if err:
                last = f"{err} ({url})"
                continue
            if rows.empty:
                last = f"no index rows for {date_str} in {url}"
                continue
            return rows, "nse", None
        except Exception as e:                                           # network trouble, an HTML error page ...
            last = f"{type(e).__name__}: {e} ({url})"
    return None, None, last


# ---------------------------------------------------------------------------------------------------------------------
# storing
# ---------------------------------------------------------------------------------------------------------------------
def ensure_tables(conn):
    conn.execute("CREATE TABLE IF NOT EXISTS index_checked (date TEXT PRIMARY KEY, status TEXT, checked TEXT)")


def store_rows(conn, rows):
    """INSERT OR REPLACE the rows as index rows (is_index = 1). Returns how many."""
    recs = [(r.symbol, r.date, float(r.open), float(r.high), float(r.low), float(r.close), int(r.volume), 1, None)
            for r in rows.itertuples(index=False)]
    conn.executemany("INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, is_index, delivery) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", recs)
    conn.commit()
    return len(recs)


def remember_names(rows):
    """Keeps data/index_names.json ({symbol: official name}) up to date; the conversion uploads it for the app."""
    try:
        cur = json.load(open(INDEX_NAMES_JSON, encoding="utf-8"))
    except (OSError, ValueError):
        cur = {}
    before = len(cur)
    for sym, name in zip(rows["symbol"], rows["name"]):
        cur[sym] = name
    if len(cur) != before:
        os.makedirs(os.path.dirname(INDEX_NAMES_JSON) or ".", exist_ok=True)
        with open(INDEX_NAMES_JSON, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(cur.items())), f, indent=1)


def trading_dates(conn, year):
    """Dates of that year that have real stock data (a trading day), newest last."""
    return [r[0] for r in conn.execute(
        "SELECT date FROM ohlcv WHERE is_index = 0 AND close > 0 GROUP BY date HAVING COUNT(*) > 100 ORDER BY date")]


def days_with_indices(conn):
    return {r[0] for r in conn.execute("SELECT DISTINCT date FROM ohlcv WHERE is_index = 1")}


def fetch_missing_days(get_conn, years, session, get, delay=0.2, recheck=False, deadline=None, log=print, recent_days=14, date_min=None, date_max=None, give_up_after=None):
    """For every trading day without index rows: download NSE's all-indices file and store it.
    get_conn(year_str) -> sqlite connection of that year.  deadline = time.time() limit or None."""
    if give_up_after is None:
        give_up_after = int(os.getenv("INDEX_GIVEUP", "60") or 0)     # this many days in a row without a file -> leave the rest of that year for a later run
    stats = {"stored_days": 0, "rows": 0, "missing": 0, "skipped_known_missing": 0, "gave_up_years": []}
    checked = 0
    today = datetime.now().date()
    stopped = None
    for y in years:
        conn = get_conn(str(y))
        ensure_tables(conn)
        have = days_with_indices(conn)
        known_missing = {r[0] for r in conn.execute("SELECT date FROM index_checked WHERE status = 'missing'")}
        in_row = 0
        for d in trading_dates(conn, y):
            if give_up_after and in_row >= give_up_after:
                stats["gave_up_years"].append(y)
                log(f"[INDICES] {y}: {in_row} days in a row without an index file at NSE - rest of {y} skipped for now "
                    f"(each run tries {give_up_after} more days; put CSV files in data/index_files/ for these years)")
                break
            if d in have or (date_min and d < date_min) or (date_max and d > date_max):
                continue
            age = (today - datetime.strptime(d, "%Y-%m-%d").date()).days
            if d in known_missing and not recheck and age > recent_days:
                stats["skipped_known_missing"] += 1
                continue
            if deadline and time.time() > deadline:
                stopped = d
                break
            rows, src, err = fetch_index_day(session, datetime.strptime(d, "%Y-%m-%d"), get)
            checked += 1
            if checked % 50 == 0:
                log(f"[INDICES] {checked} days asked so far (at {d}): stored {stats['stored_days']}, no file {stats['missing']}")
            if delay:
                time.sleep(delay)
            if rows is None:
                stats["missing"] += 1
                in_row += 1
                conn.execute("INSERT OR REPLACE INTO index_checked VALUES (?, 'missing', ?)", (d, datetime.now().strftime("%Y-%m-%d")))
                conn.commit()
                continue
            in_row = 0
            n = store_rows(conn, rows)
            remember_names(rows)
            conn.execute("INSERT OR REPLACE INTO index_checked VALUES (?, 'ok', ?)", (d, datetime.now().strftime("%Y-%m-%d")))
            conn.commit()
            stats["stored_days"] += 1
            stats["rows"] += n
            if stats["stored_days"] % 100 == 0:
                log(f"[INDICES] {stats['stored_days']} days stored so far (at {d})")
        if stopped:
            break
    if stopped:
        log(f"[INDICES] time limit reached before {stopped}; run again to continue (days already stored are skipped)")
    return stats


# ---------------------------------------------------------------------------------------------------------------------
# files you downloaded yourself
# ---------------------------------------------------------------------------------------------------------------------
def _name_from_filename(path):
    base = os.path.splitext(os.path.basename(path))[0]
    base = re.split(r"[_\-]?(?:historical|hist|data|\d{2}[-_ ]?[a-z]{3}[-_ ]?\d{4}|\d{8})", base, maxsplit=1, flags=re.I)[0]
    base = base.replace("_", " ").strip()
    return base or None


def import_index_files(get_conn, folder=None, log=print):
    """Reads every CSV in data/index_files/ (not in done/), stores the rows, moves the file to done/.
    Needs an 'Index Name' column or an index name at the start of the file name ('Nifty 50_Historical....csv')."""
    folder = folder or INDEX_FILES_DIR
    files = sorted(glob.glob(os.path.join(folder, "*.csv")))
    total = 0
    for path in files:
        try:
            try:
                df = pd.read_csv(path, skipinitialspace=True, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(path, skipinitialspace=True, encoding="latin-1")
            rows, err = parse_index_frame(df, None, default_name=_name_from_filename(path))
            if err or rows is None or rows.empty:
                log(f"[INDEX FILES] {os.path.basename(path)}: skipped ({err or 'no usable rows'})")
                continue
            n = 0
            for year, g in rows.groupby(rows["date"].str[:4]):
                conn = get_conn(year)
                ensure_tables(conn)
                n += store_rows(conn, g)
            remember_names(rows)
            total += n
            done = os.path.join(folder, "done")
            os.makedirs(done, exist_ok=True)
            shutil.move(path, os.path.join(done, os.path.basename(path)))
            log(f"[INDEX FILES] {os.path.basename(path)}: {n} rows for {rows['symbol'].nunique()} index(es), {rows['date'].min()} .. {rows['date'].max()}")
        except Exception as e:
            log(f"[INDEX FILES] {os.path.basename(path)}: skipped ({type(e).__name__}: {e})")
    return total


# ---------------------------------------------------------------------------------------------------------------------
# optional: niftyindices.com history API (experimental, never stores anything that does not pass the checks)
# ---------------------------------------------------------------------------------------------------------------------
API_URL = "https://www.niftyindices.com/Backpage.aspx/getHistoricaldatatabletoDate"
DEFAULT_API_NAMES = ["NIFTY 50", "NIFTY NEXT 50", "NIFTY 100", "NIFTY 200", "NIFTY 500", "NIFTY MIDCAP 50", "NIFTY MIDCAP 100",
                     "NIFTY MIDCAP 150", "NIFTY SMALLCAP 50", "NIFTY SMALLCAP 100", "NIFTY SMALLCAP 250", "NIFTY BANK", "NIFTY IT",
                     "NIFTY AUTO", "NIFTY FMCG", "NIFTY PHARMA", "NIFTY METAL", "NIFTY ENERGY", "NIFTY REALTY", "NIFTY MEDIA",
                     "NIFTY PSU BANK", "NIFTY FINANCIAL SERVICES", "NIFTY INFRASTRUCTURE", "NIFTY COMMODITIES", "NIFTY CONSUMPTION"]


def fetch_api_history(get_conn, session, names, start_year, end_year, log=print, post=None):
    """names: official index names. One request per index and year. Returns rows stored."""
    post = post or (lambda url, body, headers: session.post(url, data=body, headers=headers, timeout=30))
    headers = {"Content-Type": "application/json; charset=UTF-8", "Accept": "application/json, text/javascript, */*; q=0.01",
               "Origin": "https://www.niftyindices.com", "Referer": "https://www.niftyindices.com/reports/historical-data",
               "X-Requested-With": "XMLHttpRequest"}
    total = 0
    for nm in names:
        for y in range(start_year, end_year + 1):
            body = json.dumps({"cinfo": json.dumps({"name": nm, "startDate": f"01-Jan-{y}", "endDate": f"31-Dec-{y}", "indexName": nm})})
            try:
                res = post(API_URL, body, headers)
                if res.status_code != 200:
                    log(f"[INDEX API] {nm} {y}: HTTP {res.status_code}")
                    if res.status_code in (403, 429, 503):
                        return total                                   # refused: no point asking 600 more times
                    continue
                data = json.loads(json.loads(res.text)["d"])
                df = pd.DataFrame(data)
                if df.empty:
                    continue
                df = df.rename(columns={"HistoricalDate": "Date", "OPEN": "Open", "HIGH": "High", "LOW": "Low", "CLOSE": "Close"})
                df["Index Name"] = nm
                rows, err = parse_index_frame(df, None)
                if err or rows is None or rows.empty:
                    log(f"[INDEX API] {nm} {y}: unusable ({err})")
                    continue
                conn = get_conn(str(y))
                ensure_tables(conn)
                total += store_rows(conn, rows)
                remember_names(rows)
            except Exception as e:
                log(f"[INDEX API] {nm} {y}: {type(e).__name__}: {e}")
            time.sleep(0.5)
    return total


def alias_map(symbols, stocks):
    """Typed-name help for the app: 'NIFTY50', 'NIFTY 50' ... -> '^NIFTY50' (never replaces a real stock name)."""
    out = {}
    known = set(stocks)
    for s in symbols:
        core = s[1:]
        for key in (core,):
            if key not in known:
                out[key] = s
    common = {"NSEI": "^NIFTY50", "NSEBANK": "^NIFTYBANK", "CNXIT": "^NIFTYIT", "CNXAUTO": "^NIFTYAUTO", "CNXPHARMA": "^NIFTYPHARMA",
              "CNXFMCG": "^NIFTYFMCG", "CNXMETAL": "^NIFTYMETAL", "CNXENERGY": "^NIFTYENERGY", "CNXREALTY": "^NIFTYREALTY",
              "CNXMEDIA": "^NIFTYMEDIA", "CNX500": "^NIFTY500", "CNX100": "^NIFTY100", "CNX200": "^NIFTY200", "NIFTYJUNIOR": "^NIFTYNEXT50",
              "BANKNIFTY": "^NIFTYBANK", "VIX": "^INDIAVIX"}
    syms = set(symbols)
    for k, v in common.items():
        if v in syms and k not in known:
            out[k] = v
        if v in syms:
            out["^" + k] = v
    return out
