import os
import io
import json
import zipfile
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta

# Database and file configurations
DB_NAME = "nse_eod_data.db"
OVERRIDE_FILE = "manual_adjustments.json"

# Request Headers to mimic a real browser session for NSE India
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
}


def init_db(conn):
    """Initializes schema and verifies table migrations."""
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

    # Ensure is_index exists for legacy DB files
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


def get_latest_trading_days(num_days=5):
    """Generates dates (YYYY-MM-DD) for recent weekdays to search for Bhavcopy files."""
    dates = []
    current = datetime.now()
    while len(dates) < num_days:
        if current.weekday() < 5:  # Monday to Friday
            dates.append(current.strftime("%Y-%m-%d"))
        current -= timedelta(days=1)
    return dates


def fetch_nse_bhavcopy(conn):
    """Downloads daily NSE Bhavcopy files and populates the database."""
    session = requests.Session()
    session.headers.update(HEADERS)
    
    # Establish session cookies with NSE home page
    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] Session initialization failed: {e}")

    target_dates = get_latest_trading_days(7)
    records_added = 0

    cursor = conn.cursor()

    for date_str in target_dates:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        
        # Check if records exist for this date
        cursor.execute("SELECT COUNT(*) FROM ohlcv WHERE date = ?", (date_str,))
        if cursor.fetchone()[0] > 0:
            print(f"[SKIP] Data already exists for {date_str}")
            continue

        # Construct NSE Bhavcopy URL (UDiFF / Standard Format)
        year = dt.strftime("%Y")
        month = dt.strftime("%b").upper()
        day_str = dt.strftime("%d")
        
        # Standard NSE Bhavcopy URL structure
        url = f"https://archives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{day_str}{month}{year}bhav.csv.zip"

        try:
            res = session.get(url, timeout=15)
            if res.status_code == 200:
                with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                    csv_name = z.namelist()[0]
                    with z.open(csv_name) as f:
                        df = pd.read_csv(f)

                # Filter for Equity Series (EQ)
                if 'SERIES' in df.columns:
                    df = df[df['SERIES'] == 'EQ']

                # Normalize column names across schema variations
                df.columns = [c.strip().upper() for c in df.columns]
                
                rename_map = {
                    'SYMBOL': 'symbol',
                    'OPEN': 'open',
                    'HIGH': 'high',
                    'LOW': 'low',
                    'CLOSE': 'close',
                    'TOTTRDQTY': 'volume'
                }
                
                df = df.rename(columns=rename_map)
                df['date'] = date_str
                df['is_index'] = 0

                records = df[['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'is_index']].to_dict('records')

                cursor.executemany('''
                    INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, is_index)
                    VALUES (:symbol, :date, :open, :high, :low, :close, :volume, :is_index)
                ''', records)

                conn.commit()
                records_added += len(records)
                print(f"[SUCCESS] Downloaded & inserted {len(records)} stocks for {date_str}")
            else:
                print(f"[INFO] No Bhavcopy available for {date_str} (Status: {res.status_code})")
        except Exception as e:
            print(f"[ERROR] Failed downloading data for {date_str}: {e}")

    return records_added


def apply_manual_overrides(conn):
    """Applies adjustments from manual_adjustments.json to historical stock prices."""
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
        print("[SUCCESS] Corporate action manual overrides processed.")
    except Exception as e:
        print(f"[ERROR] Failed applying overrides: {e}")


def main():
    """Execution Pipeline."""
    conn = sqlite3.connect(DB_NAME)

    # 1. Initialize DB Schema
    init_db(conn)

    # 2. Fetch Daily NSE Market Data
    fetch_nse_bhavcopy(conn)

    # 3. Apply Retrospective Price Adjustments
    apply_manual_overrides(conn)

    conn.close()


if __name__ == "__main__":
    main()