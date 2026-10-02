import os
import io
import re
import glob
import json
import zipfile
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta

# Config
OVERRIDE_FILE = "manual_adjustments.json"
# Historical corporate-action file(s): matches any CSV named like NSE's own export
# (e.g. "CF-CA-equities-01-01-2020-to-01-10-2026.csv"), so the 2020-onward backfill
# can be split across multiple files (by year, by chunk, whatever's convenient to
# upload) without needing each filename hardcoded here. Going forward from today,
# fetch_live_corporate_actions() handles new events automatically - these static
# file(s) only need to cover the historical gap once.
CA_FILES = sorted(glob.glob("CF-CA-*.csv")) + sorted(glob.glob("bonus.csv")) + \
           sorted(glob.glob("split.csv")) + sorted(glob.glob("demerger.csv")) + \
           sorted(glob.glob("rights.csv"))

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

def get_db_connection(year_str):
    """Returns or creates a connection for a specific yearly SQLite database file safely."""
    db_name = f"nse_{year_str}.db"
    
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
    """Generates weekday date strings from start_date_str to target end date."""
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
        if current.weekday() < 5:  # Monday to Friday
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

def fetch_nse_bhavcopy_range(start_date=START_DATE, date_list=None):
    """Downloads daily Bhavcopies and stores all raw OHLCV data into DB files first.

    If date_list is given, only those specific dates are fetched (targeted mode).
    Otherwise every weekday from start_date to today is fetched (full backfill mode).
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] Session setup issue: {e}")

    if date_list is None:
        date_list = generate_date_range(start_date)
        print(f"[PHASE 1 START] Downloading and storing Bhavcopies from {start_date} to present...")
    else:
        print(f"[PHASE 1 START] Downloading and storing Bhavcopies for {len(date_list)} targeted date(s)...")

    total_added = 0

    for date_str in date_list:
        year_str = date_str[:4]
        conn = get_db_connection(year_str)
        cursor = conn.cursor()

        # Skip only if a full daily trading set is already stored AND the prices are
        # real (not the zero-price corruption from a column-name mismatch). This lets
        # a re-run automatically repair previously-corrupted dates via INSERT OR
        # REPLACE, without needing a separate manual cleanup pass.
        cursor.execute(
            "SELECT COUNT(*), SUM(CASE WHEN close > 0 THEN 1 ELSE 0 END) FROM ohlcv WHERE date = ?",
            (date_str,)
        )
        total_rows, nonzero_rows = cursor.fetchone()
        if total_rows and total_rows > 500 and nonzero_rows and nonzero_rows > 100:
            continue

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        year = dt.strftime("%Y")
        month = dt.strftime("%b").upper()
        day_str = dt.strftime("%d")
        date_udiff = dt.strftime("%Y%m%d")

        urls = [
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_udiff}_F_0000.csv.zip",
            f"https://archives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{day_str}{month}{year}bhav.csv.zip"
        ]

        fetched_ok = False
        last_error = None
        for url in urls:
            try:
                res = session.get(url, timeout=10)
                if res.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                        csv_name = z.namelist()[0]
                        with z.open(csv_name) as f:
                            df = pd.read_csv(f)

                    df.columns = [c.strip().upper() for c in df.columns]

                    symbol_col = 'TCKRSYMB' if 'TCKRSYMB' in df.columns else ('SYMBOL' if 'SYMBOL' in df.columns else None)
                    series_col = 'SCTYSRS' if 'SCTYSRS' in df.columns else ('SERIES' if 'SERIES' in df.columns else None)

                    if not symbol_col:
                        last_error = f"no symbol column found in {url}"
                        continue

                    # A missing series column must NOT mean "accept every row" - that
                    # silently lets index/segment-summary rows (e.g. a daily "NSE"
                    # total line) into the dataset as if they were tradable equities,
                    # and they then accumulate real-looking OHLCV history over years.
                    # Treat an undetected series column as a parse failure for this
                    # day, same as the zero-price sanity check below, instead of
                    # proceeding unfiltered.
                    if not series_col:
                        last_error = (f"no series column (SCTYSRS/SERIES) found in {url} - "
                                      f"refusing to insert unfiltered rows, columns were: "
                                      f"{list(df.columns)[:15]}")
                        continue

                    df = df[df[series_col].isin(['EQ', 'BE'])]

                    # NSE fully switched to the UDiFF bhavcopy format on 2024-07-08
                    # (Circular 62424), which uses OpnPric/HghPric/LwPric/ClsPric/
                    # TtlTradgVol - NOT OpnPrc/ClsPrc/TtlTrdQty. The old pre-UDiFF
                    # archive format (used for the historical URL pattern) used
                    # OPEN/HIGH/LOW/CLOSE/TOTTRDQTY. Check UDiFF names first, then
                    # the old names, so both eras of file map correctly instead of
                    # silently defaulting every price to 0.0.
                    df['symbol'] = df[symbol_col]
                    df['open'] = df['OPNPRIC'] if 'OPNPRIC' in df.columns else df.get('OPEN', pd.NA)
                    df['high'] = df['HGHPRIC'] if 'HGHPRIC' in df.columns else df.get('HIGH', pd.NA)
                    df['low'] = df['LWPRIC'] if 'LWPRIC' in df.columns else df.get('LOW', pd.NA)
                    df['close'] = df['CLSPRIC'] if 'CLSPRIC' in df.columns else df.get('CLOSE', pd.NA)
                    df['volume'] = df['TTLTRADGVOL'] if 'TTLTRADGVOL' in df.columns else df.get('TOTTRDQTY', pd.NA)
                    df['date'] = date_str
                    df['is_index'] = 0

                    # Sanity check: if every row's close price is null/zero, we failed
                    # to find the right columns for this file's format - store nothing
                    # rather than silently writing a day of zeroed-out prices, and
                    # surface it as a failure so it's visible in the log.
                    close_numeric = pd.to_numeric(df['close'], errors='coerce').fillna(0)
                    if len(df) > 0 and (close_numeric > 0).sum() == 0:
                        last_error = (f"parsed {len(df)} rows from {url} but every close price "
                                      f"is 0/null - unrecognized column format, columns were: "
                                      f"{list(df.columns)[:15]}")
                        continue

                    records = df[['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'is_index']].to_dict('records')

                    cursor.executemany('''
                        INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, is_index)
                        VALUES (:symbol, :date, :open, :high, :low, :close, :volume, :is_index)
                    ''', records)

                    conn.commit()
                    total_added += len(records)
                    print(f"[STORED] Downloaded {date_str} -> nse_{year_str}.db ({len(records)} records)")
                    fetched_ok = True
                    break
                else:
                    last_error = f"HTTP {res.status_code} from {url}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e} ({url})"
                continue

        if not fetched_ok:
            print(f"[FETCH FAILED] {date_str}: both URL patterns failed. Last error: {last_error}")

    close_all_databases()
    print(f"[PHASE 1 COMPLETE] All raw data saved to database files. Added {total_added} new records.")


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
        [f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")],
        key=sort_key, reverse=True
    )

    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        # Only skip based on year if it's actually a 4-digit year we can compare.
        # A file like nse_eod_data.db must never be silently skipped.
        if year_str.isdigit() and year_str > target_year:
            continue
            
        try:
            conn = sqlite3.connect(db_file)
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
        [f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")],
        key=sort_key
    )

    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        # Only skip based on year if it's actually a 4-digit year we can compare.
        # A file like nse_eod_data.db must never be silently skipped.
        if year_str.isdigit() and year_str < target_year:
            continue
            
        try:
            conn = sqlite3.connect(db_file)
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
    db_files = [f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")]
    for db_file in db_files:
        try:
            conn = sqlite3.connect(db_file)
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

    db_files = sorted([f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")])
    found_rows = []

    for db_file in db_files:
        try:
            conn = sqlite3.connect(db_file)
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
    db_files = [f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")]
    
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