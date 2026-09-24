import os
import io
import json
import zipfile
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta

# Database & file configurations
DB_NAME = "nse_eod_data.db"
OVERRIDE_FILE = "manual_adjustments.json"

# Start Date for initial historical backfill (Format: YYYY-MM-DD)
START_DATE = "2024-01-01"  # Change to "2020-01-01" for a deeper backfill

# HTTP Headers configured with a real browser User-Agent
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports"
}


def init_db(conn):
    """Initializes tables and manages schema column migrations."""
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

    # Migration check: Ensure 'is_index' exists
    cursor.execute("PRAGMA table_info(ohlcv)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'is_index' not in columns:
        print("[MIGRATION] Adding missing 'is_index' column to ohlcv table...")
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


def generate_date_range(start_date_str):
    """Generates a list of weekday date strings (YYYY-MM-DD) from start_date to today."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    today = datetime.now()
    date_list = []

    current = start
    while current <= today:
        if current.weekday() < 5:  # Monday to Friday
            date_list.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return date_list


def fetch_nse_bhavcopy_range(conn, start_date=START_DATE):
    """Downloads official daily NSE Bhavcopy files directly from NSE endpoints."""
    session = requests.Session()
    session.headers.update(HEADERS)

    # Establish cookies with standard homepage visit
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] NSE Session connection error: {e}")

    date_list = generate_date_range(start_date)
    cursor = conn.cursor()
    total_added = 0

    print(f"[START] Processing NSE Bhavcopies from {start_date} to today ({len(date_list)} weekdays)...")

    for date_str in date_list:
        dt = datetime.strptime(date_str, "%Y-%m-%d")

        # Skip if date is already in the database
        cursor.execute("SELECT COUNT(*) FROM ohlcv WHERE date = ?", (date_str,))
        if cursor.fetchone()[0] > 0:
            continue

        year = dt.strftime("%Y")
        month = dt.strftime("%b").upper()
        day_str = dt.strftime("%d")
        date_udiff = dt.strftime("%Y%m%d")

        # Potential NSE download URL patterns (UDiFF & Legacy Formats)
        urls = [
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_udiff}_F_0000.csv.zip",
            f"https://archives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{day_str}{month}{year}bhav.csv.zip",
            f"https://www.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{day_str}{month}{year}bhav.csv.zip"
        ]

        downloaded = False
        for url in urls:
            try:
                res = session.get(url, timeout=10)
                if res.status_code == 200:
                    content_type = res.headers.get('Content-Type', '')

                    # Handle Zip Files
                    if 'zip' in content_type or url.endswith('.zip'):
                        with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                            csv_name = z.namelist()[0]
                            with z.open(csv_name) as f:
                                df = pd.read_csv(f)
                    # Handle Plain CSV Files
                    else:
                        df = pd.read_csv(io.BytesIO(res.content))

                    df.columns = [c.strip().upper() for c in df.columns]

                    # Map standard columns across legacy and UDiFF CSV formats
                    symbol_col = 'TCKRSYMB' if 'TCKRSYMB' in df.columns else ('SYMBOL' if 'SYMBOL' in df.columns else None)
                    series_col = 'SCTYSRS' if 'SCTYSRS' in df.columns else ('SERIES' if 'SERIES' in df.columns else None)

                    if not symbol_col:
                        continue

                    # Filter for Equity series
                    if series_col:
                        df = df[df[series_col].isin(['EQ', 'BE'])]

                    # Normalize columns
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
                    print(f"[SUCCESS] Downloaded Bhavcopy for {date_str}: {len(records)} stocks added.")
                    downloaded = True
                    break

            except Exception:
                continue

        if not downloaded:
            # Silence logging for weekend/holiday dates with no data
            pass

    print(f"[COMPLETE] Total EOD records added/updated: {total_added}")


def apply_manual_overrides(conn):
    """Applies retroactive price adjustments from manual_adjustments.json."""
    if not os.path.exists(OVERRIDE_FILE):
        print(f"[NOTE] No {OVERRIDE_FILE} file found. Skipping overrides.")
        return

    try:
        with open(OVERRIDE_FILE, 'r') as f:
            overrides = json.load(f)

        cursor = conn.cursor()

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

                    cursor.execute('''
                        UPDATE ohlcv 
                        SET open = ROUND(open * ?, 2),
                            high = ROUND(high * ?, 2),
                            low = ROUND(low * ?, 2),
                            close = ROUND(close * ?, 2)
                        WHERE symbol = ? AND date < ? AND is_index = 0
                    ''', (factor, factor, factor, factor, symbol, ex_date))

                    print(f"[MANUAL ADJUSTMENT] Applied factor {factor} for {symbol} prior to {ex_date}")

        conn.commit()
        print("[SUCCESS] Corporate action manual overrides applied.")
    except Exception as e:
        print(f"[ERROR] Failed applying overrides: {e}")


def main():
    """Main execution pipeline."""
    conn = sqlite3.connect(DB_NAME)

    init_db(conn)
    fetch_nse_bhavcopy_range(conn)
    apply_manual_overrides(conn)

    conn.close()


if __name__ == "__main__":
    main()