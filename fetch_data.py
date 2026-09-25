import os
import io
import re
import json
import zipfile
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta

# Config
OVERRIDE_FILE = "manual_adjustments.json"
CA_FILES = ["bonus.csv", "split.csv", "demerger.csv", "rights.csv"]

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

        # Only skip if a full daily trading set (> 500 records) is already stored for this date
        cursor.execute("SELECT COUNT(*) FROM ohlcv WHERE date = ?", (date_str,))
        if cursor.fetchone()[0] > 500:
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

                    if series_col:
                        df = df[df[series_col].isin(['EQ', 'BE'])]

                    df['symbol'] = df[symbol_col]
                    df['open'] = df['OPNPRC'] if 'OPNPRC' in df.columns else df.get('OPEN', 0.0)
                    df['high'] = df['HGHPRC'] if 'HGHPRC' in df.columns else df.get('HIGH', 0.0)
                    df['low'] = df['LWPRC'] if 'LWPRC' in df.columns else df.get('LOW', 0.0)
                    df['close'] = df['CLSPRC'] if 'CLSPRC' in df.columns else df.get('CLOSE', 0.0)
                    df['volume'] = df['TTLTRDQTY'] if 'TTLTRDQTY' in df.columns else df.get('TOTTRDQTY', 0)
                    df['date'] = date_str
                    df['is_index'] = 0

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
            cursor.execute(f'''
                SELECT {field} FROM ohlcv 
                WHERE symbol = ? COLLATE NOCASE AND date <= ? AND is_index = 0
                ORDER BY date DESC LIMIT 1
            ''', (symbol, target_date_str))
            row = cursor.fetchone()
            conn.close()
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
            cursor.execute(f'''
                SELECT {field} FROM ohlcv 
                WHERE symbol = ? COLLATE NOCASE AND date >= ? AND is_index = 0
                ORDER BY date ASC LIMIT 1
            ''', (symbol, target_date_str))
            row = cursor.fetchone()
            conn.close()
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
        print(f"[DEBUG] Found {len(found_rows)}+ nearby row(s) for '{symbol}' via LIKE match: {sample}")
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

    m = re.search(r"(?:rs|re|\.|\s)*(\d+(?:\.\d+)?)\s*(?:/-)?\s*to\s*(?:rs|re|\.|\s)*(\d+(?:\.\d+)?)", p_lower)
    if m:
        try:
            v1, v2 = float(m.group(1)), float(m.group(2))
            if v1 > v2 > 0:
                return v2 / v1
        except ValueError:
            pass

    old_fv = row_dict.get('FACE_VALUE', row_dict.get('FACEVALUE', row_dict.get('OLD_FV', None)))
    new_fv = row_dict.get('NEW_FACE_VALUE', row_dict.get('NEW_FV', row_dict.get('NEW_FACEVALUE', None)))

    if old_fv is not None and new_fv is not None:
        try:
            v1, v2 = float(old_fv), float(new_fv)
            if v1 > v2 > 0:
                return v2 / v1
        except (ValueError, TypeError):
            pass

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
    rights_match = re.search(r"rights\s*(?:-\s*)?\b(\d+)\s*:\s*(\d+)", p_lower)
    if rights_match:
        r_shares = float(rights_match.group(1))
        h_shares = float(rights_match.group(2))
        if r_shares + h_shares > 0:
            factors.append(("RIGHTS", h_shares / (r_shares + h_shares)))

    return factors


def apply_factor_across_all_dbs(symbol, ex_date, factor, purpose, action_type):
    """Applies multiplier adjustments to pre-ex_date historical records in database files."""
    db_files = [f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")]
    
    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        conn = get_db_connection(year_str)
        cursor = conn.cursor()

        cursor.execute('''
            INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, purpose, ratio, action_type)
            VALUES (?, ?, ?, ?, ?)
        ''', (symbol, ex_date, purpose, factor, action_type))

        cursor.execute('''
            UPDATE ohlcv 
            SET open = ROUND(open * ?, 2),
                high = ROUND(high * ?, 2),
                low = ROUND(low * ?, 2),
                close = ROUND(close * ?, 2)
            WHERE symbol = ? COLLATE NOCASE AND date < ? AND is_index = 0
        ''', (factor, factor, factor, factor, symbol, ex_date))

        conn.commit()


def process_all_corporate_actions():
    """Phase 2 execution: Reads populated tables, calculates factors, and updates database."""
    print("[PHASE 2 START] Reading stored database records and processing corporate action files...")
    applied_count = 0

    for file_path in CA_FILES:
        if not os.path.exists(file_path):
            continue

        print(f"[PROCESSING] Reading '{file_path}'...")
        try:
            df = pd.read_csv(file_path, skipinitialspace=True)
            df.columns = [str(c).strip().upper().replace(" ", "_") for c in df.columns]

            for _, row in df.iterrows():
                # Uppercase to match NSE bhavcopy symbol casing exactly - a case
                # mismatch here silently returns "no data" even when the price
                # exists in the db for that date, since SQLite '=' is case-sensitive.
                symbol = str(row.get("SYMBOL", "")).strip().upper()
                ex_date_raw = str(row.get("EX-DATE", row.get("EX_DATE", row.get("EXDATE", "")))).strip()
                purpose = str(row.get("PURPOSE", "")).strip()

                if not symbol or symbol in ["nan", ""] or not ex_date_raw or ex_date_raw in ["-", "nan", ""]:
                    continue

                try:
                    ex_date = pd.to_datetime(ex_date_raw, dayfirst=True).strftime("%Y-%m-%d")
                except Exception:
                    continue

                row_dict = row.to_dict()
                events = parse_purpose_multipliers(purpose, row_dict, symbol, ex_date)

                for action_type, factor in events:
                    if 0.0 < factor < 1.0:
                        apply_factor_across_all_dbs(symbol, ex_date, factor, purpose, action_type)
                        applied_count += 1
                        print(f"[{action_type}] Adjusted {symbol} prior to {ex_date} with factor {factor:.4f}")

        except Exception as e:
            print(f"[ERROR] Failed to process {file_path}: {e}")

    print(f"[PHASE 2 COMPLETE] Corporate action batch processing finished ({applied_count} actions applied).")


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

    process_all_corporate_actions()
    apply_manual_overrides()
    close_all_databases()


if __name__ == "__main__":
    main()