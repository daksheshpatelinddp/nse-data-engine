import os
import io
import re
import glob
import json
import zipfile
import sqlite3
import time
import pandas as pd
import requests
from datetime import datetime, timedelta

# Config
# Repo layout (see docs/repo_structure.md): data files live under data/, kept
# separate from backend/ (this script) and frontend/ (index.html). These are
# the only two constants that encode that layout - everything else in this
# file builds paths from them, so the folder structure can move again later
# by changing just these two lines.
DB_DIR = "data/databases"
CA_DIR = "data/corporate_actions"
os.makedirs(DB_DIR, exist_ok=True)
os.makedirs(CA_DIR, exist_ok=True)

OVERRIDE_FILE = os.path.join(CA_DIR, "manual_adjustments.json")
# Historical corporate-action file(s): matches any CSV named like NSE's own export
# (e.g. "CF-CA-equities-01-01-2020-to-01-10-2026.csv"), so the 2020-onward backfill
# can be split across multiple files (by year, by chunk, whatever's convenient to
# upload) without needing each filename hardcoded here. Going forward from today,
# fetch_live_corporate_actions() handles new events automatically - these static
# file(s) only need to cover the historical gap once.
CA_FILES = sorted(glob.glob(os.path.join(CA_DIR, "CF-CA-*.csv"))) + \
           sorted(glob.glob(os.path.join(CA_DIR, "bonus.csv"))) + \
           sorted(glob.glob(os.path.join(CA_DIR, "split.csv"))) + \
           sorted(glob.glob(os.path.join(CA_DIR, "demerger.csv"))) + \
           sorted(glob.glob(os.path.join(CA_DIR, "rights.csv")))

raw_start = os.getenv("START_DATE", "2000-01-01")
START_DATE = raw_start.replace("'", "").replace('"', '').strip()

raw_end = os.getenv("END_DATE", "")
END_DATE_ENV = raw_end.replace("'", "").replace('"', '').strip()

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com"
}

db_connections = {}

# ---- New options (all can be set from the GitHub "Run workflow" form) ----
# DELIVERY_BACKFILL: for days already stored WITHOUT delivery data, download NSE's full
#   bhavcopy and fill in only the delivery column (prices are never touched).
# PROBE: do not touch any database; just report which NSE file types exist for sample
#   dates from 2000 to today, so you can see how far back each source goes.
# REQUEST_DELAY: pause (seconds) after every NSE request, to be polite and avoid blocking.
def _flag(name):
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes")

DELIVERY_BACKFILL = _flag("DELIVERY_BACKFILL")
PROBE = _flag("PROBE")
try:
    REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "0.2") or 0.2)
except ValueError:
    REQUEST_DELAY = 0.2

# Three file types, tried in this order for every date. Only the first has delivery data.
URL_FULL  = "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{ddmmyyyy}.csv"
URL_UDIFF = "https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{yyyymmdd}_F_0000.csv.zip"
URL_OLD   = "https://archives.nseindia.com/content/historical/EQUITIES/{year}/{mon}/cm{dd}{mon}{year}bhav.csv.zip"
URL_MTO   = "https://nsearchives.nseindia.com/archives/equities/mto/MTO_{ddmmyyyy}.DAT"  # probe only

def get_db_connection(year_str):
    """Returns or creates a connection for a specific yearly SQLite database file safely."""
    db_name = os.path.join(DB_DIR, f"nse_{year_str}.db")

    if db_name not in db_connections:
        if os.path.exists(db_name):
            try:
                test_conn = sqlite3.connect(db_name)
                test_conn.execute("PRAGMA quick_check;")
                test_conn.close()
            except sqlite3.DatabaseError:
                print(f"[WARNING] Removing corrupted database file: {db_name}")
                os.remove(db_name)

        conn = sqlite3.connect(db_name)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ohlcv (
                symbol TEXT,
                date TEXT,
                open REAL,
                high REAL,
                low REAL,
                close REAL,
                volume INTEGER,
                is_index INTEGER DEFAULT 0,
                delivery INTEGER,
                PRIMARY KEY (symbol, date)
            )
        ''')
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS corporate_actions (
                symbol TEXT,
                ex_date TEXT,
                purpose TEXT,
                ratio REAL,
                action_type TEXT,
                PRIMARY KEY (symbol, ex_date, action_type)
            )
        ''')
        # Databases created before delivery support: add the column once.
        existing_cols = [r[1] for r in cursor.execute("PRAGMA table_info(ohlcv)").fetchall()]
        if "delivery" not in existing_cols:
            cursor.execute("ALTER TABLE ohlcv ADD COLUMN delivery INTEGER")
        conn.commit()
        db_connections[db_name] = conn
    return db_connections[db_name]


def close_all_databases():
    """Flushes and cleanly closes all open database connections."""
    for name, conn in db_connections.items():
        try:
            conn.execute("PRAGMA wal_checkpoint(FULL);")
            conn.close()
        except Exception:
            pass
    db_connections.clear()


def generate_date_range(start_date_str):
    """Generates ALL calendar dates from start_date_str to target end date.

    Weekends are included on purpose: NSE holds special sessions on Saturdays/Sundays
    (Diwali Muhurat trading, Budget day, special live sessions). Days with no file on
    NSE (ordinary weekends and holidays) are simply skipped, quietly for weekends.
    """
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d")
    except ValueError:
        start = datetime.strptime("2000-01-01", "%Y-%m-%d")

    target_end = datetime.now()
    if END_DATE_ENV and len(END_DATE_ENV) == 10:
        try:
            target_end = datetime.strptime(END_DATE_ENV, "%Y-%m-%d")
        except ValueError:
            pass

    date_list = []
    current = start
    while current <= target_end:
        date_list.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return date_list


def collect_required_dates(buffer_days=10):
    """Scans all CA_FILES and returns the minimal sorted list of weekday dates needed to
    resolve every event's cum-close (just before ex-date) and ex-open (on/after ex-date).

    buffer_days is a calendar-day cushion on each side of ex-date, to make sure a nearby
    trading day is available even across weekends/exchange holidays.
    """
    required = set()

    for file_path in CA_FILES:
        if not os.path.exists(file_path):
            continue
        try:
            df = pd.read_csv(file_path, skipinitialspace=True)
            df.columns = [str(c).strip().upper().replace(" ", "_") for c in df.columns]

            for _, row in df.iterrows():
                ex_date_raw = str(row.get("EX-DATE", row.get("EX_DATE", row.get("EXDATE", "")))).strip()
                if not ex_date_raw or ex_date_raw in ["-", "nan", ""]:
                    continue
                try:
                    ex_date = pd.to_datetime(ex_date_raw, dayfirst=True)
                except Exception:
                    continue

                window_start = ex_date - timedelta(days=buffer_days)
                window_end = ex_date + timedelta(days=buffer_days)
                d = window_start
                while d <= window_end:
                    if d.weekday() < 5:
                        required.add(d.strftime("%Y-%m-%d"))
                    d += timedelta(days=1)
        except Exception as e:
            print(f"[WARNING] Could not scan {file_path} for required dates: {e}")

    return sorted(required)


def fetch_targeted_dates_for_corporate_actions():
    """Backfill entry point: downloads only the dates needed to price every corporate
    action correctly, instead of every trading day since 2000. Much faster and avoids
    re-creating a huge multi-decade dataset."""
    date_list = collect_required_dates()
    if not date_list:
        print("[TARGETED FETCH] No corporate action dates found to backfill.")
        return
    fetch_nse_bhavcopy_range(date_list=date_list)


# ==========================================
# PHASE 1: DOWNLOAD AND STORE BASE DATA
# ==========================================

def _norm(c):
    return re.sub(r"[^A-Z0-9]", "", str(c).upper())


def _pick(df, *names):
    """First matching column, ignoring case, spaces and underscores (None if absent)."""
    lookup = {}
    for c in df.columns:
        lookup.setdefault(_norm(c), c)
    for n in names:
        key = _norm(n)
        if key in lookup:
            return df[lookup[key]]
    return None


def _num(series):
    """Text/number column -> numbers; '-', blanks and junk become NaN."""
    s = series.astype(str).str.strip()
    s = s.mask(s.isin(["-", "", "nan", "None", "NaN"]))
    return pd.to_numeric(s, errors="coerce")


def _f(x):
    return None if pd.isna(x) else float(x)


def _i(x):
    return None if pd.isna(x) else int(x)


def parse_bhavcopy(df, date_str, url):
    """Turns any of the three NSE file layouts into one tidy table.

    Returns (table, error). Columns: symbol, date, open, high, low, close, volume,
    is_index, delivery. Only EQ and BE series are kept (as before).
    """
    symbol = _pick(df, "TCKRSYMB", "SYMBOL")
    series = _pick(df, "SCTYSRS", "SERIES")
    if symbol is None:
        return None, f"no symbol column found in {url}"
    # An undetected series column must never mean "accept every row" (index/summary rows
    # would slip in as tradable equities), so treat it as a parse failure for this day.
    if series is None:
        return None, (f"no series column (SCTYSRS/SERIES) found in {url} - refusing to insert "
                      f"unfiltered rows, columns were: {list(df.columns)[:15]}")

    out = pd.DataFrame({
        "symbol": symbol.astype(str).str.strip(),
        "series": series.astype(str).str.strip().str.upper(),
    })
    fields = {
        "open":   ("OPNPRIC", "OPEN_PRICE", "OPEN"),
        "high":   ("HGHPRIC", "HIGH_PRICE", "HIGH"),
        "low":    ("LWPRIC", "LOW_PRICE", "LOW"),
        "close":  ("CLSPRIC", "CLOSE_PRICE", "CLOSE"),
        "volume": ("TTLTRADGVOL", "TTL_TRD_QNTY", "TOTTRDQTY"),
    }
    for name, candidates in fields.items():
        col = _pick(df, *candidates)
        out[name] = _num(col) if col is not None else float("nan")
    deliv = _pick(df, "DELIV_QTY")
    out["delivery"] = _num(deliv) if deliv is not None else float("nan")

    out = out[out["series"].isin(["EQ", "BE"])].copy()
    out["date"] = date_str
    out["is_index"] = 0

    # Sanity check: a day where every close is 0/null means we did not recognise the columns.
    if len(out) > 0 and (out["close"].fillna(0) > 0).sum() == 0:
        return None, (f"parsed {len(out)} rows from {url} but every close price is 0/null - "
                      f"unrecognized column format, columns were: {list(df.columns)[:15]}")
    return out[["symbol", "date", "open", "high", "low", "close", "volume", "is_index", "delivery"]], None


def _http_get(session, url):
    """GET with one retry on network errors (404 and other HTTP answers are not retried)."""
    last = None
    for attempt in range(2):
        try:
            res = session.get(url, timeout=20)
            time.sleep(REQUEST_DELAY)
            return res
        except Exception as e:
            last = e
            time.sleep(1)
    raise last


def fetch_day(session, dt, date_str, only_full=False):
    """Downloads one day. Returns (table, source, last_error).

    source is 'full' (has delivery), 'udiff' or 'old' (no delivery).
    """
    ctx = {
        "ddmmyyyy": dt.strftime("%d%m%Y"), "yyyymmdd": dt.strftime("%Y%m%d"),
        "year": dt.strftime("%Y"), "mon": dt.strftime("%b").upper(), "dd": dt.strftime("%d"),
    }
    sources = [("full", URL_FULL.format(**ctx))]
    if not only_full:
        sources += [("udiff", URL_UDIFF.format(**ctx)), ("old", URL_OLD.format(**ctx))]

    last_error = None
    for label, url in sources:
        try:
            res = _http_get(session, url)
            if res.status_code != 200:
                last_error = f"HTTP {res.status_code} from {url}"
                continue
            if label == "full":
                df = pd.read_csv(io.StringIO(res.text), skipinitialspace=True)
            else:
                with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                    with z.open(z.namelist()[0]) as f:
                        df = pd.read_csv(f, skipinitialspace=True)
            table, err = parse_bhavcopy(df, date_str, url)
            if err:
                last_error = err
                continue
            return table, label, None
        except Exception as e:
            last_error = f"{type(e).__name__}: {e} ({url})"
            continue
    return None, None, last_error


def apply_recorded_adjustments(conn, date_str):
    """Adjusts freshly inserted RAW prices of one date for corporate actions already
    recorded in this database (ex-date after that date), the same way every older row
    was adjusted.

    Why this is needed: a corporate action is applied to a database only once (that is
    the double-adjustment guard in apply_factor_across_all_dbs). Rows added AFTERWARDS
    - an old Muhurat/weekend session, a repaired day - would otherwise stay raw and show
    a price jump next to their adjusted neighbours. Volume and delivery are not adjusted,
    exactly like volume has always been.
    """
    cur = conn.cursor()
    actions = cur.execute(
        "SELECT symbol, ex_date, ratio FROM corporate_actions WHERE ex_date > ? ORDER BY ex_date",
        (date_str,)
    ).fetchall()
    touched = 0
    for symbol, ex_date, factor in actions:
        if factor is None or factor <= 0 or factor == 1:
            continue
        cur.execute(
            "UPDATE ohlcv SET open = ROUND(open * ?, 2), high = ROUND(high * ?, 2), "
            "low = ROUND(low * ?, 2), close = ROUND(close * ?, 2) "
            "WHERE symbol = ? COLLATE NOCASE AND date = ? AND is_index = 0",
            (factor, factor, factor, factor, symbol, date_str)
        )
        touched += cur.rowcount
    conn.commit()
    return touched


def backfill_delivery_for_date(session, dt, date_str, conn):
    """Fills ONLY the delivery column of a day that is already stored. Returns rows updated
    (None if NSE has no full file for that day)."""
    table, source, err = fetch_day(session, dt, date_str, only_full=True)
    if table is None:
        return None
    table = table.dropna(subset=["delivery"]).drop_duplicates("symbol", keep="last")
    rows = [(_i(r.delivery), r.symbol, date_str) for r in table.itertuples(index=False)]
    cur = conn.cursor()
    cur.executemany("UPDATE ohlcv SET delivery = ? WHERE symbol = ? AND date = ?", rows)
    conn.commit()
    return len(rows)


def fetch_nse_bhavcopy_range(start_date=START_DATE, date_list=None):
    """Downloads daily Bhavcopies and stores the raw OHLCV + delivery data into DB files.

    Source order per day: NSE full bhavcopy with delivery (sec_bhavdata_full), then the
    plain UDiFF bhavcopy, then the old-format bhavcopy (those two have no delivery).
    Every calendar day is tried; weekends/holidays without a file are skipped quietly,
    so Muhurat and other special Saturday/Sunday sessions are picked up automatically.

    If date_list is given, only those specific dates are fetched (targeted mode).
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] Session setup issue: {e}")

    if date_list is None:
        date_list = generate_date_range(start_date)
        print(f"[PHASE 1 START] Downloading and storing Bhavcopies from {start_date} to present "
              f"({len(date_list)} calendar days)...")
    else:
        print(f"[PHASE 1 START] Downloading and storing Bhavcopies for {len(date_list)} targeted date(s)...")

    total_added = 0
    weekend_sessions = []
    stats = {"full": 0, "udiff": 0, "old": 0, "failed_weekday": 0,
             "delivery_filled": 0, "delivery_unavailable": 0}

    for date_str in date_list:
        year_str = date_str[:4]
        conn = get_db_connection(year_str)
        cursor = conn.cursor()
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        is_weekend = dt.weekday() >= 5

        cursor.execute(
            "SELECT COUNT(*), SUM(CASE WHEN close > 0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN delivery IS NOT NULL THEN 1 ELSE 0 END) FROM ohlcv WHERE date = ?",
            (date_str,)
        )
        total_rows, nonzero_rows, with_delivery = cursor.fetchone()
        total_rows = total_rows or 0
        nonzero_rows = nonzero_rows or 0
        with_delivery = with_delivery or 0

        # Already stored? A weekday needs a full trading set with real prices (so a re-run can
        # still repair corrupted days). A weekend/special session may legitimately be small, so
        # any real priced rows count as stored - never overwrite them with raw prices again.
        full_day = total_rows > 500 and nonzero_rows > 100
        special_day_stored = is_weekend and nonzero_rows >= 1
        if full_day or special_day_stored:
            if DELIVERY_BACKFILL and with_delivery == 0:
                n = backfill_delivery_for_date(session, dt, date_str, conn)
                if n is None:
                    stats["delivery_unavailable"] += 1
                else:
                    stats["delivery_filled"] += 1
                    print(f"[DELIVERY] {date_str}: delivery filled for {n} rows")
            continue

        table, source, last_error = fetch_day(session, dt, date_str)
        if table is None:
            if not is_weekend:  # ordinary weekends have no file: stay quiet
                stats["failed_weekday"] += 1
                print(f"[FETCH FAILED] {date_str}: all file types failed. Last error: {last_error}")
            continue

        records = [
            (r.symbol, r.date, _f(r.open), _f(r.high), _f(r.low), _f(r.close),
             _i(r.volume), 0, _i(r.delivery))
            for r in table.itertuples(index=False)
        ]
        cursor.executemany(
            "INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, is_index, delivery) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            records
        )
        conn.commit()

        adjusted = apply_recorded_adjustments(conn, date_str)
        total_added += len(records)
        stats[source] += 1
        n_deliv = int(table["delivery"].notna().sum())
        tag = "WEEKEND SESSION" if is_weekend else "STORED"
        if is_weekend:
            weekend_sessions.append(date_str)
        extra = f", {adjusted} price adjustments applied" if adjusted else ""
        print(f"[{tag}] {date_str} -> nse_{year_str}.db ({len(records)} records, source={source}, "
              f"delivery for {n_deliv}{extra})")

    close_all_databases()
    print(f"[PHASE 1 COMPLETE] Added {total_added} new records. "
          f"Files used: full={stats['full']}, udiff={stats['udiff']}, old={stats['old']}; "
          f"weekday failures={stats['failed_weekday']}; "
          f"weekend/special sessions found={len(weekend_sessions)} {weekend_sessions[:30]}")
    if DELIVERY_BACKFILL:
        print(f"[DELIVERY BACKFILL] days filled={stats['delivery_filled']}, "
              f"days with no full file on NSE={stats['delivery_unavailable']}")


def probe_sources():
    """Reports which NSE file types exist for sample dates (no database is touched)."""
    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] Session setup issue: {e}")
    samples = ["2026-09-15", "2025-03-18", "2024-06-18", "2023-03-15", "2022-03-15", "2021-03-16",
               "2020-03-17", "2019-03-19", "2018-03-20", "2016-03-15", "2013-03-19", "2010-03-16",
               "2006-03-14", "2003-03-18", "2000-03-14"]
    print("[PROBE] date | full(delivery) | udiff | old | MTO(delivery, informational)")
    for d in samples:
        dt = datetime.strptime(d, "%Y-%m-%d")
        ctx = {"ddmmyyyy": dt.strftime("%d%m%Y"), "yyyymmdd": dt.strftime("%Y%m%d"),
               "year": dt.strftime("%Y"), "mon": dt.strftime("%b").upper(), "dd": dt.strftime("%d")}
        cells = []
        header = ""
        for label, tpl in (("full", URL_FULL), ("udiff", URL_UDIFF), ("old", URL_OLD), ("mto", URL_MTO)):
            try:
                res = _http_get(session, tpl.format(**ctx))
                cells.append(f"{label}={res.status_code}")
                if label == "full" and res.status_code == 200:
                    header = res.text.splitlines()[0][:200] if res.text else ""
            except Exception as e:
                cells.append(f"{label}=ERR({type(e).__name__})")
        print(f"[PROBE] {d} | " + " | ".join(cells))
        if header:
            print(f"[PROBE]    full-file header: {header}")


# ==========================================
# PHASE 2: READ STORED DATA & APPLY ADJUSTMENTS
# ==========================================

def get_price_on_or_before(symbol, target_date_str, field="close"):
    """Queries all existing database files for the latest available price on or before target_date_str."""
    target_year = target_date_str[:4]

    def sort_key(f):
        y = f.replace("nse_", "").replace(".db", "")
        # Non-numeric filenames (e.g. nse_eod_data.db) get pushed to the end of the
        # "reverse" sort so they're still checked, just after the dated files.
        return y if y.isdigit() else "0000"

    db_files = sorted(
        [f for f in os.listdir(DB_DIR) if f.startswith("nse_") and f.endswith(".db")],
        key=sort_key, reverse=True
    )

    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        # Only skip based on year if it's actually a 4-digit year we can compare.
        # A file like nse_eod_data.db must never be silently skipped.
        if year_str.isdigit() and year_str > target_year:
            continue
            
        try:
            conn = sqlite3.connect(os.path.join(DB_DIR, db_file))
            cursor = conn.cursor()
            # Pull several recent candidates, not just the single nearest date - a
            # stock can be listed with a zero-price/zero-volume row on a given day
            # (illiquid day, or a trading halt right around a corporate action) even
            # while genuinely trading normally a few days earlier.
            cursor.execute(f'''
                SELECT {field} FROM ohlcv 
                WHERE symbol = ? COLLATE NOCASE AND date <= ? AND is_index = 0
                ORDER BY date DESC LIMIT 20
            ''', (symbol, target_date_str))
            rows = cursor.fetchall()
            conn.close()
            for row in rows:
                if row and row[0] is not None and row[0] > 0:
                    return float(row[0])
        except Exception:
            continue
            
    return None


def get_price_on_or_after(symbol, target_date_str, field="open"):
    """Queries all existing database files for the earliest available price on or after target_date_str."""
    target_year = target_date_str[:4]

    def sort_key(f):
        y = f.replace("nse_", "").replace(".db", "")
        # Non-numeric filenames (e.g. nse_eod_data.db) get pushed to the front so
        # they're checked first when the target date predates any yearly db file.
        return y if y.isdigit() else "0000"

    db_files = sorted(
        [f for f in os.listdir(DB_DIR) if f.startswith("nse_") and f.endswith(".db")],
        key=sort_key
    )

    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        # Only skip based on year if it's actually a 4-digit year we can compare.
        # A file like nse_eod_data.db must never be silently skipped.
        if year_str.isdigit() and year_str < target_year:
            continue
            
        try:
            conn = sqlite3.connect(os.path.join(DB_DIR, db_file))
            cursor = conn.cursor()
            # Same fix as get_price_on_or_before: check several upcoming candidates
            # instead of only the single nearest date, since the immediate next day
            # can itself be a zero-price/zero-volume (no-trade) row.
            cursor.execute(f'''
                SELECT {field} FROM ohlcv 
                WHERE symbol = ? COLLATE NOCASE AND date >= ? AND is_index = 0
                ORDER BY date ASC LIMIT 20
            ''', (symbol, target_date_str))
            rows = cursor.fetchall()
            conn.close()
            for row in rows:
                if row and row[0] is not None and row[0] > 0:
                    return float(row[0])
        except Exception:
            continue
            
    return None


def action_already_applied(symbol, ex_date, action_type):
    """Checks if corporate action was already recorded."""
    db_files = [f for f in os.listdir(DB_DIR) if f.startswith("nse_") and f.endswith(".db")]
    for db_file in db_files:
        try:
            conn = sqlite3.connect(os.path.join(DB_DIR, db_file))
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) FROM corporate_actions 
                WHERE symbol = ? COLLATE NOCASE AND ex_date = ? AND action_type = ?
            ''', (symbol, ex_date, action_type))
            count = cursor.fetchone()[0]
            conn.close()
            if count > 0:
                return True
        except Exception:
            continue
    return False


def debug_symbol_presence(symbol, ex_date, days=15):
    """Diagnostic used only when a demerger factor can't be computed. Searches all db
    files for anything resembling `symbol` (case/whitespace-insensitive via LIKE) within
    +/- `days` of ex_date, and prints what it finds. This tells us whether the symbol
    genuinely has no data that day (e.g. trading suspended for the corporate action) or
    whether it exists under a slightly different stored string (case, extra characters)."""
    try:
        dt_ex = datetime.strptime(ex_date, "%Y-%m-%d")
    except ValueError:
        return

    window_start = (dt_ex - timedelta(days=days)).strftime("%Y-%m-%d")
    window_end = (dt_ex + timedelta(days=days)).strftime("%Y-%m-%d")

    db_files = sorted([f for f in os.listdir(DB_DIR) if f.startswith("nse_") and f.endswith(".db")])
    found_rows = []

    for db_file in db_files:
        try:
            conn = sqlite3.connect(os.path.join(DB_DIR, db_file))
            cursor = conn.cursor()
            cursor.execute('''
                SELECT date, symbol, open, close FROM ohlcv
                WHERE symbol LIKE ? AND date BETWEEN ? AND ?
                ORDER BY date ASC LIMIT 5
            ''', (f"%{symbol}%", window_start, window_end))
            rows = cursor.fetchall()
            conn.close()
            found_rows.extend(rows)
        except Exception:
            continue

    if found_rows:
        sample = ", ".join(f"{r[0]} sym='{r[1]}' O={r[2]} C={r[3]}" for r in found_rows[:5])
        all_zero = all((r[2] in (0, 0.0, None)) and (r[3] in (0, 0.0, None)) for r in found_rows)
        note = (" -- ALL sampled rows have 0 price (no real trade that day); "
                "get_price_on_or_before/after now walk further out to find a real traded price."
                if all_zero else "")
        print(f"[DEBUG] Found {len(found_rows)}+ nearby row(s) for '{symbol}' via LIKE match: {sample}{note}")
    else:
        print(f"[DEBUG] No row for '{symbol}' (any casing/spacing) found in {window_start}..{window_end} "
              f"across {len(db_files)} db file(s). Likely trading was suspended that day, "
              f"or the symbol differs entirely from what's in the CA file (renamed/delisted).")


def calculate_demerger_factor(symbol, ex_date, purpose_str=""):
    """Calculates Adjustment Factor: AF = (Ex-Date Open) / (Cum-Date Close)."""
    today_str = datetime.now().strftime("%Y-%m-%d")
    if ex_date > today_str:
        print(f"[DEMERGER SKIPPED] {symbol} @ {ex_date}: Future ex-date.")
        return None

    if action_already_applied(symbol, ex_date, "DEMERGER"):
        return None

    try:
        dt_ex = datetime.strptime(ex_date, "%Y-%m-%d")
    except ValueError:
        return None

    dt_prev = (dt_ex - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # 1. Fetch Cum-Date Close from database
    cum_close = get_price_on_or_before(symbol, dt_prev, field="close")
    
    # 2. Fetch Ex-Date Open from database
    ex_open = get_price_on_or_after(symbol, ex_date, field="open")
    
    if ex_open and cum_close and cum_close > 0:
        factor = ex_open / cum_close
        if 0.0 < factor < 1.0:
            print(f"[DEMERGER FORMULA] {symbol} @ {ex_date}: Ex-Open({ex_open}) / Cum-Close({cum_close}) = {factor:.4f}")
            return factor
        else:
            print(f"[DEMERGER SKIP] {symbol} @ {ex_date}: Factor {factor:.4f} outside range (0, 1)")
    else:
        # Fallback to percentage description parsing if daily prices are missing
        pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", str(purpose_str).lower())
        if pct_match:
            pct = float(pct_match.group(1))
            if 0 < pct < 100:
                fallback_factor = (100.0 - pct) / 100.0
                print(f"[DEMERGER FALLBACK] {symbol} @ {ex_date}: Parsed percentage fallback factor = {fallback_factor:.4f}")
                return fallback_factor
        print(f"[DEMERGER MISSING DATA] {symbol} @ {ex_date}: Cum-Close={cum_close}, Ex-Open={ex_open}")
        debug_symbol_presence(symbol, ex_date)
            
    return None


def extract_split_ratio(purpose_str, row_dict):
    """Extracts stock split factors from split purpose descriptions."""
    p_lower = str(purpose_str).lower().strip()

    # NSE's real phrasing always has "Per Share" (or similar) between the first
    # number and "To" (e.g. "From Rs 10/- Per Share To Rs 5/- Per Share") - the
    # old pattern required "to" immediately after the number with nothing in
    # between, so it silently matched zero real split announcements, ever.
    # ".*?" (non-greedy) bridges that gap regardless of the exact wording used.
    m = re.search(r"from\s*(?:rs|re|\.)*\s*(\d+(?:\.\d+)?)\s*(?:/-)?.*?\bto\b\s*(?:rs|re|\.)*\s*(\d+(?:\.\d+)?)", p_lower)
    if m:
        try:
            v1, v2 = float(m.group(1)), float(m.group(2))
            if v1 > v2 > 0:
                return v2 / v1
        except ValueError:
            pass

    old_fv = _clean_numeric(_get_field(row_dict, ['FACE_VALUE', 'FACEVALUE', 'OLD_FV', 'OLD_FACE_VALUE']))
    new_fv = _clean_numeric(_get_field(row_dict, ['NEW_FACE_VALUE', 'NEW_FV', 'NEW_FACEVALUE']))

    if old_fv is not None and new_fv is not None:
        if old_fv > new_fv > 0:
            return new_fv / old_fv

    return None


def parse_purpose_multipliers(purpose_str, row_dict, symbol, ex_date):
    """Parses corporate action events and returns adjustment factors."""
    factors = []
    p_lower = str(purpose_str).lower()

    # 1. SPLIT
    if any(k in p_lower for k in ["split", "sub-division", "subdivision", "fv", "face value"]):
        factor = extract_split_ratio(purpose_str, row_dict)
        if factor and 0.0 < factor < 1.0:
            factors.append(("SPLIT", factor))

    # 2. DEMERGER
    if "demerger" in p_lower or "de-merger" in p_lower or "demerg" in p_lower:
        factor = calculate_demerger_factor(symbol, ex_date, purpose_str)
        if factor:
            factors.append(("DEMERGER", factor))

    # 3. BONUS
    bonus_match = re.search(r"bonus\s*(?:-\s*)?\b(\d+)\s*:\s*(\d+)", p_lower)
    if bonus_match:
        b_shares = float(bonus_match.group(1))
        h_shares = float(bonus_match.group(2))
        if b_shares + h_shares > 0:
            factors.append(("BONUS", h_shares / (b_shares + h_shares)))

    # 4. RIGHTS
    # Tolerate "Rights Issue N:M" phrasing, not just "Rights N:M" - NSE uses both
    rights_match = re.search(r"rights\s*(?:issue\s*)?(?:-\s*)?\b(\d+)\s*:\s*(\d+)", p_lower)
    if rights_match:
        r_shares = float(rights_match.group(1))
        h_shares = float(rights_match.group(2))
        if r_shares + h_shares > 0:
            factors.append(("RIGHTS", h_shares / (r_shares + h_shares)))

    return factors


def apply_factor_across_all_dbs(symbol, ex_date, factor, purpose, action_type):
    """Applies multiplier adjustments to pre-ex_date historical records in database files.

    IDEMPOTENCY GUARD: this function is called every time fetch_data.py runs (daily
    cron, manual re-runs), and it re-reads the same static CA CSV files each time - so
    without a guard, the SAME historical adjustment would be re-applied on every single
    run, compounding the factor toward zero over time. The INSERT OR IGNORE into
    corporate_actions (PRIMARY KEY symbol+ex_date+action_type) is the de-duplication
    check: if it inserts 0 rows, this exact adjustment was already applied to this db
    file in a previous run, and the price UPDATE below must be skipped entirely.
    """
    db_files = [f for f in os.listdir(DB_DIR) if f.startswith("nse_") and f.endswith(".db")]
    
    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        conn = get_db_connection(year_str)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, purpose, ratio, action_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (symbol, ex_date, purpose, factor, action_type))

        if cursor.rowcount == 0:
            # Already applied to this db file in a prior run - do NOT re-multiply.
            conn.commit()
            continue

        cursor.execute('''
            UPDATE ohlcv 
            SET open = ROUND(open * ?, 2),
                high = ROUND(high * ?, 2),
                low = ROUND(low * ?, 2),
                close = ROUND(close * ?, 2)
            WHERE symbol = ? COLLATE NOCASE AND date < ? AND is_index = 0
        ''', (factor, factor, factor, factor, symbol, ex_date))

        conn.commit()


def _normalize_column_name(c):
    """Normalizes a raw CSV header into a stable form for matching, tolerant of
    the kind of inconsistencies real manually-downloaded NSE files actually have:
    different whitespace, different dash characters (-, --, en-dash, em-dash),
    stray BOM characters, mixed casing, and spaces vs underscores."""
    c = str(c).replace("\ufeff", "")  # strip BOM if pandas didn't already
    c = c.strip().upper()
    c = c.replace("\u2013", "-").replace("\u2014", "-")  # en-dash/em-dash -> hyphen
    c = re.sub(r"[\s\-]+", "_", c)  # collapse any run of spaces/hyphens to one underscore
    c = re.sub(r"_+", "_", c).strip("_")
    return c


def _get_field(row_dict, candidates):
    """Looks up a value from a row by trying several possible normalized column
    names in order, then falls back to a substring match against every column
    name in the row - covers files where the exact header wasn't anticipated
    (e.g. 'SCRIP' or 'SECURITY' instead of 'SYMBOL', 'SUBJECT' or 'DESCRIPTION'
    instead of 'PURPOSE')."""
    for key in candidates:
        if key in row_dict and str(row_dict[key]).strip() not in ("", "nan", "None"):
            return str(row_dict[key]).strip()
    for key, val in row_dict.items():
        if any(cand in key for cand in candidates) and str(val).strip() not in ("", "nan", "None"):
            return str(val).strip()
    return ""


def _clean_numeric(val):
    """Strips currency symbols, commas, and stray text from a value NSE files
    sometimes format inconsistently (e.g. 'Rs. 10/-', '10.00', ' 10 ') before
    float conversion."""
    if val is None:
        return None
    s = str(val).strip()
    s = re.sub(r"[^\d.\-]", "", s)  # keep only digits, decimal point, minus sign
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


# Shared field-name candidates, used by both the static-file path and the live
# NSE API path, since both need the same tolerant matching.
CA_SYMBOL_KEYS = ["SYMBOL", "SCRIP", "SECURITY", "SCRIP_CODE", "SECURITY_NAME"]
CA_DATE_KEYS = ["EX_DATE", "EXDATE", "EX_DATE_", "RECORD_DATE", "EX", "DATE"]
CA_PURPOSE_KEYS = ["PURPOSE", "SUBJECT", "DESCRIPTION", "CORPORATE_ACTION", "REMARKS", "PARTICULARS"]
CA_SERIES_KEYS = ["SERIES"]


def _process_ca_row(row_dict, source_label, row_label):
    """Processes one corporate-action record (from either a static CSV file or
    the live NSE API) through the same symbol/date/purpose extraction, equity-
    series filter, and factor application - so both sources behave identically.
    Returns 'applied', 'skipped', or 'filtered' (non-equity series, not logged
    as a gap since it's expected, e.g. government securities / InvITs)."""
    series = _get_field(row_dict, CA_SERIES_KEYS).upper()
    if series and series not in ("EQ", "BE"):
        return "filtered"

    symbol = _get_field(row_dict, CA_SYMBOL_KEYS).upper()
    ex_date_raw = _get_field(row_dict, CA_DATE_KEYS)
    purpose = _get_field(row_dict, CA_PURPOSE_KEYS)

    if not symbol or not ex_date_raw:
        print(f"[SKIP] {source_label} {row_label}: missing symbol or ex-date "
              f"(symbol={symbol!r}, ex_date={ex_date_raw!r})")
        return "skipped"

    try:
        ex_date = pd.to_datetime(ex_date_raw, dayfirst=True).strftime("%Y-%m-%d")
    except Exception:
        print(f"[SKIP] {source_label} {row_label}: unparseable ex-date {ex_date_raw!r} for {symbol}")
        return "skipped"

    if not purpose:
        print(f"[SKIP] {source_label} {row_label}: no purpose/subject text for {symbol} @ {ex_date}")
        return "skipped"

    events = parse_purpose_multipliers(purpose, row_dict, symbol, ex_date)

    if not events:
        # Purpose text existed but matched none of split/bonus/rights/demerger - this
        # is EXPECTED for the majority of rows (dividends, AGM notices, buybacks don't
        # need a price-factor adjustment), so this isn't logged as a gap unless it's
        # worth a human glancing at - keep it quiet here, counted by the caller instead.
        return "no_action_needed"

    applied_any = False
    for action_type, factor in events:
        if 0.0 < factor < 1.0:
            apply_factor_across_all_dbs(symbol, ex_date, factor, purpose, action_type)
            applied_any = True
            print(f"[{action_type}] Adjusted {symbol} prior to {ex_date} with factor {factor:.4f} (source: {source_label})")

    return "applied" if applied_any else "no_action_needed"


def process_all_corporate_actions():
    """Phase 2a: processes the static, manually-uploaded historical corporate action
    file(s) (e.g. a one-time NSE CF-CA-equities export). Tolerant of inconsistent
    file structure: column names are normalized and matched via multiple candidate
    names, and genuinely unexpected rows are logged so gaps are visible.
    """
    print("[PHASE 2a START] Reading stored historical corporate action file(s)...")
    counts = {"applied": 0, "skipped": 0, "filtered": 0, "no_action_needed": 0}

    for file_path in CA_FILES:
        if not os.path.exists(file_path):
            continue

        print(f"[PROCESSING] Reading '{file_path}'...")
        try:
            try:
                df = pd.read_csv(file_path, skipinitialspace=True, encoding="utf-8-sig")
            except UnicodeDecodeError:
                df = pd.read_csv(file_path, skipinitialspace=True, encoding="latin-1")

            df.columns = [_normalize_column_name(c) for c in df.columns]
            file_counts = {"applied": 0, "skipped": 0, "filtered": 0, "no_action_needed": 0}

            for row_num, row in df.iterrows():
                row_dict = {k: row[k] for k in df.columns}
                result = _process_ca_row(row_dict, file_path, f"row {row_num}")
                counts[result] += 1
                file_counts[result] += 1

            print(f"[{file_path}] {file_counts['applied']} applied, "
                  f"{file_counts['no_action_needed']} no-op (dividend/AGM/etc.), "
                  f"{file_counts['filtered']} non-equity filtered, "
                  f"{file_counts['skipped']} genuinely skipped (check logs above).")

        except Exception as e:
            print(f"[ERROR] Failed to process {file_path}: {e}")

    print(f"[PHASE 2a COMPLETE] Historical file processing finished: "
          f"{counts['applied']} actions applied, {counts['skipped']} rows skipped total.")


def fetch_live_corporate_actions(days_lookback=10):
    """Phase 2b: fetches RECENT corporate actions directly from NSE's live API
    (the same endpoint that powers the downloadable CF-CA-equities CSV export),
    covering the last `days_lookback` days through today. Run on every daily
    cron execution so new announcements are picked up automatically - no more
    manual re-downloading. A rolling lookback window (rather than just
    "yesterday") gives self-healing safety margin if a run is ever missed, and
    the existing idempotency guard in apply_factor_across_all_dbs means
    re-seeing an already-applied action across overlapping runs is harmless.

    NOTE: this endpoint's exact JSON field names are matched defensively via
    the same multi-candidate lookup used elsewhere, since NSE's live API field
    names can differ slightly from the downloadable CSV's column headers (e.g.
    'subject' instead of 'PURPOSE'). If NSE changes their schema, failures will
    show up as [SKIP]/[LIVE-CA ERROR] log lines rather than silently vanishing.
    """
    today = datetime.now()
    from_date = (today - timedelta(days=days_lookback)).strftime("%d-%m-%Y")
    to_date = today.strftime("%d-%m-%Y")

    print(f"[PHASE 2b START] Fetching live corporate actions from NSE: {from_date} to {to_date}...")

    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[LIVE-CA WARNING] Session setup issue: {e}")

    url = (f"https://www.nseindia.com/api/corporates-corporateActions"
           f"?index=equities&from_date={from_date}&to_date={to_date}")

    try:
        res = session.get(url, timeout=15)
        if res.status_code != 200:
            print(f"[LIVE-CA ERROR] NSE API returned HTTP {res.status_code} - skipping this run's live fetch "
                  f"(historical/static files are unaffected). Will retry on the next scheduled run.")
            return

        records = res.json()
        if not isinstance(records, list):
            print(f"[LIVE-CA ERROR] Unexpected response shape (expected a list of records): "
                  f"{type(records)} - NSE may have changed this endpoint's format.")
            return

    except Exception as e:
        print(f"[LIVE-CA ERROR] Fetch/parse failed: {e} - skipping this run's live fetch.")
        return

    counts = {"applied": 0, "skipped": 0, "filtered": 0, "no_action_needed": 0}

    for i, record in enumerate(records):
        # Normalize JSON keys the same way CSV columns are normalized, so the
        # same _get_field candidate lists work for both sources unchanged.
        row_dict = {_normalize_column_name(k): v for k, v in record.items()}
        result = _process_ca_row(row_dict, "live-NSE-API", f"record {i}")
        counts[result] += 1

    print(f"[PHASE 2b COMPLETE] Live fetch processed {len(records)} record(s): "
          f"{counts['applied']} applied, {counts['no_action_needed']} no-op, "
          f"{counts['filtered']} non-equity filtered, {counts['skipped']} skipped.")


def apply_manual_overrides():
    if not os.path.exists(OVERRIDE_FILE):
        return

    try:
        with open(OVERRIDE_FILE, 'r') as f:
            overrides = json.load(f)

        applied_count = 0
        for symbol, events in overrides.items():
            for event in events:
                ex_date = event.get('ex_date')
                factor = float(event.get('factor', 1.0))
                purpose = event.get('description', 'Manual Adjustment')

                if factor < 1.0 and ex_date:
                    apply_factor_across_all_dbs(symbol, ex_date, factor, purpose, "MANUAL_OVERRIDE")
                    applied_count += 1

        print(f"[SUCCESS] Manual overrides applied ({applied_count} events).")
    except Exception as e:
        print(f"[ERROR] Applying manual overrides failed: {e}")


def main():
    if PROBE:
        probe_sources()
        return

    # Normal range fetch (recent/incremental data, per START_DATE/END_DATE).
    fetch_nse_bhavcopy_range(start_date=START_DATE)

    # Historical corporate-action backfill (dates outside START_DATE, e.g. old demergers)
    # only runs when explicitly requested. This keeps regular incremental runs fast and
    # scoped to the START_DATE/END_DATE you actually typed in.
    run_ca_backfill = os.getenv("RUN_CA_BACKFILL", "").strip().lower() in ("1", "true", "yes")
    if run_ca_backfill:
        fetch_targeted_dates_for_corporate_actions()
    else:
        print("[SKIP] Historical CA date backfill skipped (RUN_CA_BACKFILL not set). "
              "Set RUN_CA_BACKFILL=true on a run to backfill missing corporate-action dates.")

    process_all_corporate_actions()   # one-time/periodic static historical file(s)
    fetch_live_corporate_actions()    # automated - runs every time, picks up new announcements
    apply_manual_overrides()
    close_all_databases()


if __name__ == "__main__":
    main()