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

# Headers mimicking modern desktop browsers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/"
}


def init_db(conn):
    """Initializes table schema and runs migrations."""
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

    # Migration check for is_index
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


def seed_initial_data_if_empty(conn):
    """Seeds baseline records so API endpoints are never empty on setup."""
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM ohlcv")
    count = cursor.fetchone()[0]

    if count == 0:
        print("[SEEDING] Database is empty. Inserting baseline tickers...")
        sample_data = [
            ("RELIANCE", "2024-01-02", 2580.0, 2610.0, 2570.0, 2600.0, 4500000, 0),
            ("RELIANCE", "2023-07-19", 2800.0, 2850.0, 2790.0, 2840.0, 5200000, 0),
            ("ITC", "2023-12-29", 460.0, 468.0, 458.0, 465.0, 8900000, 0),
            ("TCS", "2024-01-02", 3750.0, 3800.0, 3720.0, 3790.0, 2100000, 0),
            ("INFY", "2024-01-02", 1520.0, 1550.0, 1510.0, 1540.0, 3100000, 0)
        ]
        cursor.executemany('''
            INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, is_index)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', sample_data)
        conn.commit()


def get_latest_trading_days(num_days=5):
    """Returns recent weekday dates formatted YYYY-MM-DD."""
    dates = []
    current = datetime.now()
    while len(dates) < num_days:
        if current.weekday() < 5:
            dates.append(current.strftime("%Y-%m-%d"))
        current -= timedelta(days=1)
    return dates


def fetch_nse_bhavcopy(conn):
    """Downloads daily NSE Bhavcopy files (UDiFF and legacy formats)."""
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        session.get("https://www.nseindia.com", timeout=10)
    except Exception as e:
        print(f"[WARNING] NSE Session connection error: {e}")

    target_dates = get_latest_trading_days(7)
    records_added = 0
    cursor = conn.cursor()

    for date_str in target_dates:
        dt = datetime.strptime(date_str, "%Y-%m-%d")

        cursor.execute("SELECT COUNT(*) FROM ohlcv WHERE date = ?", (date_str,))
        if cursor.fetchone()[0] > 0:
            print(f"[SKIP] Data already present for {date_str}")
            continue

        year = dt.strftime("%Y")
        month = dt.strftime("%b").upper()
        day_str = dt.strftime("%d")
        date_udiff = dt.strftime("%Y%m%d")

        # Potential NSE download URL patterns (UDiFF & Legacy)
        urls = [
            f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_udiff}_F_0000.csv.zip",
            f"https://archives.nseindia.com/content/historical/EQUITIES/{year}/{month}/cm{day_str}{month}{year}bhav.csv.zip"
        ]

        downloaded = False
        for url in urls:
            try:
                res = session.get(url, timeout=12)
                if res.status_code == 200:
                    with zipfile.ZipFile(io.BytesIO(res.content)) as z:
                        csv_name = z.namelist()[0]
                        with z.open(csv_name) as f:
                            df = pd.read_csv(f)

                    df.columns = [c.strip().upper() for c in df.columns]

                    # Standardize column mappings across Bhavcopy versions
                    symbol_col = 'TckrSymb' if 'TckrSymb' in df.columns else ('SYMBOL' if 'SYMBOL' in df.columns else None)
                    series_col = 'SctySrs' if 'SctySrs' in df.columns else ('SERIES' if 'SERIES' in df.columns else None)

                    if series_col:
                        df = df[df[series_col].isin(['EQ', 'BE'])]

                    if symbol_col:
                        df['symbol'] = df[symbol_col]
                        df['open'] = df['OpnPrc'] if 'OpnPrc' in df.columns else df.get('OPEN', 0.0)
                        df['high'] = df['HghPrc'] if 'HghPrc' in df.columns else df.get('HIGH', 0.0)
                        df['low'] = df['LwPrc'] if 'LwPrc' in df.columns else df.get('LOW', 0.0)
                        df['close'] = df['ClsPrc'] if 'ClsPrc' in df.columns else df.get('CLOSE', 0.0)
                        df['volume'] = df['TtlTrdQty'] if 'TtlTrdQty' in df.columns else df.get('TOTTRDQTY', 0)
                        df['date'] = date_str
                        df['is_index'] = 0

                        records = df[['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'is_index']].to_dict('records')

                        cursor.executemany('''
                            INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, is_index)
                            VALUES (:symbol, :date, :open, :high, :low, :close, :volume, :is_index)
                        ''', records)

                        conn.commit()
                        records_added += len(records)
                        print(f"[SUCCESS] Downloaded & saved {len(records)} stocks for {date_str}")
                        downloaded = True
                        break
            except Exception as e:
                continue

        if not downloaded:
            print(f"[INFO] No download available for {date_str}")

    return records_added


def apply_manual_overrides(conn):
    """Applies retroactive adjustments from manual_adjustments.json."""
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

    init_db(conn)
    seed_initial_data_if_empty(conn)
    fetch_nse_bhavcopy(conn)
    apply_manual_overrides(conn)

    conn.close()


if __name__ == "__main__":
    main()