import os
import io
import re
import json
import zipfile
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta

# Database & file configurations
DB_NAME = "nse_eod_data.db"
OVERRIDE_FILE = "manual_adjustments.json"

# Input Corporate Action CSV files
CA_FILES = ["bonus.csv", "split.csv", "demerger.csv", "rights.csv"]

# Clean date inputs from environment variables
raw_start = os.getenv("START_DATE", "2023-12-01")
START_DATE = raw_start.replace("'", "").replace('"', '').strip()

raw_end = os.getenv("END_DATE", "")
END_DATE_ENV = raw_end.replace("'", "").replace('"', '').strip()

# Headers for NSE Requests
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com"
}


def init_db(conn):
    """Initializes schema and creates required SQLite tables."""
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

    cursor.execute("PRAGMA table_info(ohlcv)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'is_index' not in columns:
        cursor.execute("ALTER TABLE ohlcv ADD COLUMN is_index INTEGER DEFAULT 0")

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


def get_end_date():
    """Determines target end date based on environment variable or current date."""
    if END_DATE_ENV and len(END_DATE_ENV) == 10:
        try:
            return datetime.strptime(END_DATE_ENV, "%Y-%m-%d")
        except ValueError:
            pass
    return datetime.now()


def generate_date_range(start_date_str):
    """Generates weekday date strings from start_date_str to target end date."""
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d")
    except ValueError:
        start = datetime.strptime("2023-12-01", "%Y-%m-%d")

    target_end = get_end_date()
    date_list = []

    current = start
    while current <= target_end:
        if current.weekday() < 5:  # Monday through Friday
            date_list.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return date_list


def fetch_nse_bhavcopy_range(conn, start_date=START_DATE):
    """Downloads daily Bhavcopies directly from NSE for missing dates."""
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] Session connection warning: {e}")

    date_list = generate_date_range(start_date)
    cursor = conn.cursor()
    total_added = 0

    print(f"[START] Fetching Bhavcopies from {start_date} to present ({len(date_list)} weekdays)...")

    for date_str in date_list:
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

        downloaded = False
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
                    print(f"[SUCCESS] Downloaded Bhavcopy for {date_str}: {len(records)} records.")
                    downloaded = True
                    break
            except Exception:
                continue

    print(f"[COMPLETE] Daily EOD fetch finished. Total new records added: {total_added}")


def parse_purpose_multipliers(purpose_str, row_data):
    """Extracts all multipliers (splits, bonuses, rights) present inside a single PURPOSE string."""
    factors = []
    p_lower = str(purpose_str).lower()

    # 1. Check for Stock Splits (e.g., "Fv Split Rs.10/- To Rs.5/", "Split Us 64 Into 2 Parts")
    split_match = re.search(r"(?:fv\s*)?split.*?\b(\d+)\s*(?:/-)?\s*to\s*(?:re\.?|rs\.?)?\s*(\d+)", p_lower)
    if not split_match:
        split_match = re.search(r"rs\.?\s*(\d+)\s*to\s*rs\.?\s*(\d+)", p_lower)

    if split_match:
        old_fv = float(split_match.group(1))
        new_fv = float(split_match.group(2))
        if old_fv > new_fv > 0:
            factors.append(("SPLIT", new_fv / old_fv))

    # 2. Check for Bonus Issues (e.g., "Bonus 1:1", "Bonus - 1:5", "Div-30%/Bonus 1:1")
    bonus_match = re.search(r"bonus\s*(?:-\s*)?\b(\d+)\s*:\s*(\d+)", p_lower)
    if bonus_match:
        bonus_shares = float(bonus_match.group(1))
        held_shares = float(bonus_match.group(2))
        if bonus_shares + held_shares > 0:
            factors.append(("BONUS", held_shares / (bonus_shares + held_shares)))

    # 3. Check for Rights Issues (e.g., "Rights - 1:2", "Rights-35:12")
    rights_match = re.search(r"rights\s*(?:-\s*)?\b(\d+)\s*:\s*(\d+)", p_lower)
    if rights_match:
        rights_shares = float(rights_match.group(1))
        held_shares = float(rights_match.group(2))
        if rights_shares + held_shares > 0:
            factors.append(("RIGHTS", held_shares / (rights_shares + held_shares)))

    # 4. Fallback for FACTOR/RATIO columns in Demerger CSVs
    if not factors:
        for col_name in ["FACTOR", "RATIO"]:
            if col_name in row_data:
                try:
                    f_val = float(row_data[col_name])
                    if 0.0 < f_val < 1.0:
                        factors.append(("DEMERGER_OR_CUSTOM", f_val))
                except (ValueError, TypeError):
                    pass

    return factors


def process_all_corporate_actions(conn):
    """Processes all uploaded corporate action CSV files cleanly with flexible date parsing."""
    cursor = conn.cursor()
    applied_count = 0

    for file_path in CA_FILES:
        if not os.path.exists(file_path):
            continue

        print(f"[PROCESSING] Reading '{file_path}'...")
        try:
            df = pd.read_csv(file_path)
            df.columns = [c.strip().upper().replace(" ", "_") for c in df.columns]

            for _, row in df.iterrows():
                symbol = str(row.get("SYMBOL", "")).strip()
                ex_date_raw = str(row.get("EX_DATE", row.get("EXDATE", row.get("EX-DATE", "")))).strip()
                purpose = str(row.get("PURPOSE", "")).strip()

                if not symbol or symbol in ["nan", ""] or not ex_date_raw or ex_date_raw in ["-", "nan", ""]:
                    continue

                # Support multiple date formats (e.g. "25-Oct-2002", "2002-10-25", "25/10/2002")
                try:
                    ex_date = pd.to_datetime(ex_date_raw, dayfirst=True).strftime("%Y-%m-%d")
                except Exception:
                    continue

                row_dict = row.to_dict()
                events = parse_purpose_multipliers(purpose, row_dict)

                for action_type, factor in events:
                    if 0.0 < factor < 1.0:
                        cursor.execute('''
                            INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, purpose, ratio, action_type)
                            VALUES (?, ?, ?, ?, ?)
                        ''', (symbol, ex_date, purpose, factor, action_type))

                        if cursor.rowcount > 0:
                            cursor.execute('''
                                UPDATE ohlcv 
                                SET open = ROUND(open * ?, 2),
                                    high = ROUND(high * ?, 2),
                                    low = ROUND(low * ?, 2),
                                    close = ROUND(close * ?, 2)
                                WHERE symbol = ? AND date < ? AND is_index = 0
                            ''', (factor, factor, factor, factor, symbol, ex_date))

                            if cursor.rowcount > 0:
                                applied_count += 1
                                print(f"[{action_type}] Adjusted {symbol} prior to {ex_date} with factor {factor:.4f} ({purpose})")

        except Exception as e:
            print(f"[ERROR] Failed to process {file_path}: {e}")

    conn.commit()
    print(f"[SUCCESS] Corporate action CSV batch processing complete ({applied_count} actions applied).")


def apply_manual_overrides(conn):
    """Applies custom adjustments from manual_adjustments.json."""
    if not os.path.exists(OVERRIDE_FILE):
        return

    try:
        with open(OVERRIDE_FILE, 'r') as f:
            overrides = json.load(f)

        cursor = conn.cursor()
        applied_count = 0

        for symbol, events in overrides.items():
            for event in events:
                ex_date = event.get('ex_date')
                factor = float(event.get('factor', 1.0))
                purpose = event.get('description', 'Manual Adjustment')

                if factor < 1.0 and ex_date:
                    cursor.execute('''
                        INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, purpose, ratio, action_type)
                        VALUES (?, ?, ?, ?, 'MANUAL_OVERRIDE')
                    ''', (symbol, ex_date, purpose, factor))

                    if cursor.rowcount > 0:
                        cursor.execute('''
                            UPDATE ohlcv 
                            SET open = ROUND(open * ?, 2),
                                high = ROUND(high * ?, 2),
                                low = ROUND(low * ?, 2),
                                close = ROUND(close * ?, 2)
                            WHERE symbol = ? AND date < ? AND is_index = 0
                        ''', (factor, factor, factor, factor, symbol, ex_date))

                        if cursor.rowcount > 0:
                            applied_count += 1

        conn.commit()
        print(f"[SUCCESS] Manual overrides applied ({applied_count} events).")
    except Exception as e:
        print(f"[ERROR] Applying manual corporate action overrides failed: {e}")


def main():
    """Main execution pipeline."""
    conn = sqlite3.connect(DB_NAME)

    init_db(conn)
    fetch_nse_bhavcopy_range(conn, start_date=START_DATE)
    process_all_corporate_actions(conn)
    apply_manual_overrides(conn)

    conn.close()


if __name__ == "__main__":
    main()