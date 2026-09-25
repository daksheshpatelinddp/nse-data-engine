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


# ==========================================
# PHASE 1: DOWNLOAD AND STORE BASE DATA
# ==========================================

def fetch_nse_bhavcopy_range(start_date=START_DATE):
    """Downloads daily Bhavcopies and stores all raw OHLCV data into DB files first."""
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] Session setup issue: {e}")

    date_list = generate_date_range(start_date)
    total_added = 0

    print(f"[PHASE 1 START] Downloading and storing Bhavcopies from {start_date} to present...")

    for date_str in date_list:
        year_str = date_str[:4]
        conn = get_db_connection(year_str)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM ohlcv WHERE date = ?", (date_str,))
        if cursor.fetchone()[0] > 0:
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
                    break
            except Exception:
                continue

    # Commit and flush DB connections so all tables are fully readable
    close_all_databases()
    print(f"[PHASE 1 COMPLETE] All raw data saved to database files. Added {total_added} new records.")


# ==========================================
# PHASE 2: READ STORED DATA & APPLY ADJUSTMENTS
# ==========================================

def get_all_active_db_years():
    """Returns a sorted list of all active database years from memory and disk."""
    disk_years = {f.replace("nse_", "").replace(".db", "") for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")}
    memory_years = {name.replace("nse_", "").replace(".db", "") for name in db_connections.keys()}
    return sorted(list(disk_years.union(memory_years)))


def get_price_on_or_before(symbol, target_date_str, field="close"):
    """Queries memory buffers and disk databases for the latest recorded price on or before target_date_str."""
    years = sorted([y for y in get_all_active_db_years() if y <= target_date_str[:4]], reverse=True)
    
    for year_str in years:
        conn = get_db_connection(year_str)
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT {field} FROM ohlcv 
            WHERE symbol = ? AND date <= ? AND is_index = 0 AND {field} > 0
            ORDER BY date DESC LIMIT 1
        ''', (symbol, target_date_str))
        row = cursor.fetchone()
        if row and row[0] is not None and row[0] > 0:
            return float(row[0])
            
    return None


def get_price_on_or_after(symbol, target_date_str, field="open"):
    """Queries memory buffers and disk databases for the earliest recorded price on or after target_date_str."""
    years = sorted([y for y in get_all_active_db_years() if y >= target_date_str[:4]])
    
    for year_str in years:
        conn = get_db_connection(year_str)
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT {field} FROM ohlcv 
            WHERE symbol = ? AND date >= ? AND is_index = 0 AND {field} > 0
            ORDER BY date ASC LIMIT 1
        ''', (symbol, target_date_str))
        row = cursor.fetchone()
        if row and row[0] is not None and row[0] > 0:
            return float(row[0])
            
    return None


def get_price_on_or_after(symbol, target_date_str, field="open"):
    """Queries all database files to find earliest available price on or after target_date_str."""
    db_files = sorted([f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")])
    
    for db_file in db_files:
        year_str = db_file.replace("nse_", "").replace(".db", "")
        if year_str < target_date_str[:4]:
            continue
            
        conn = get_db_connection(year_str)
        cursor = conn.cursor()
        cursor.execute(f'''
            SELECT {field} FROM ohlcv 
            WHERE symbol = ? AND date >= ? AND is_index = 0
            ORDER BY date ASC LIMIT 1
        ''', (symbol, target_date_str))
        row = cursor.fetchone()
        if row and row[0] is not None and row[0] > 0:
            return float(row[0])
            
    return None


def action_already_applied(symbol, ex_date, action_type):
    """Checks if corporate action was already processed."""
    db_files = [f for f in os.listdir(".") if f.startswith("nse_") and f.endswith(".db")]
    for db_file in db_files:
        conn = sqlite3.connect(db_file)
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM corporate_actions 
            WHERE symbol = ? AND ex_date = ? AND action_type = ?
        ''', (symbol, ex_date, action_type))
        count = cursor.fetchone()[0]
        conn.close()
        if count > 0:
            return True
    return False


def calculate_demerger_factor(symbol, ex_date):
    """Calculates Adjustment Factor: AF = (Ex-Date Open) / (Cum-Date Close)."""
    if action_already_applied(symbol, ex_date, "DEMERGER"):
        return None

    dt_ex = datetime.strptime(ex_date, "%Y-%m-%d")
    dt_prev = (dt_ex - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # 1. Fetch Cum-Date Close from stored database
    cum_close = get_price_on_or_before(symbol, dt_prev, field="close")
    
    # 2. Fetch Ex-Date Open from stored database
    ex_open = get_price_on_or_after(symbol, ex_date, field="open")
    
    if ex_open and cum_close and cum_close > 0:
        factor = ex_open / cum_close
        if 0.0 < factor < 1.0:
            print(f"[DEMERGER FORMULA] {symbol} @ {ex_date}: Ex-Open({ex_open}) / Cum-Close({cum_close}) = {factor:.4f}")
            return factor
        else:
            print(f"[DEMERGER SKIP] {symbol} @ {ex_date}: Factor {factor:.4f} outside range (0, 1)")
    else:
        print(f"[DEMERGER MISSING DATA] {symbol} @ {ex_date}: Cum-Close={cum_close}, Ex-Open={ex_open}")
            
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
        factor = calculate_demerger_factor(symbol, ex_date)
        if factor:
            factors.append(("DEMERGER", factor))
        else:
            pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", p_lower)
            if pct_match:
                pct = float(pct_match.group(1))
                if 0 < pct < 100:
                    factors.append(("DEMERGER", (100.0 - pct) / 100.0))

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
            WHERE symbol = ? AND date < ? AND is_index = 0
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
                symbol = str(row.get("SYMBOL", "")).strip()
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
    # Phase 1: Download & store raw daily market data
    fetch_nse_bhavcopy_range(start_date=START_DATE)

    # Phase 2: Read stored data, compute dynamic demerger ratios & apply adjustments
    process_all_corporate_actions()
    apply_manual_overrides()

    # Final cleanup
    close_all_databases()


if __name__ == "__main__":
    main()