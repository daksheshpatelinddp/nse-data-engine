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

# Dynamic Environment Variables passed from daily_cron.yml
START_DATE = os.getenv("START_DATE", "2023-12-01")
END_DATE_ENV = os.getenv("END_DATE", "")

# Standard Browser Headers to bypass NSE anti-scraping
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-actions"
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
    """Determines the target end date based on environment variable or current date."""
    if END_DATE_ENV and len(END_DATE_ENV.strip()) == 10:
        return datetime.strptime(END_DATE_ENV.strip(), "%Y-%m-%d")
    return datetime.now()


def generate_date_range(start_date_str):
    """Generates weekday date strings from start_date_str to target end date."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d")
    target_end = get_end_date()
    date_list = []

    current = start
    while current <= target_end:
        if current.weekday() < 5:  # Monday to Friday
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
        # Fast skipping: Check if daily data already exists in database
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


def auto_adjust_splits_and_bonuses(conn, start_date=START_DATE):
    """Fetches full historical corporate action data from START_DATE to today and applies split/bonus multipliers."""
    dt_start = datetime.strptime(start_date, "%Y-%m-%d")
    from_date_str = dt_start.strftime("%d-%m-%Y")
    to_date_str = get_end_date().strftime("%d-%m-%Y")

    print(f"[CORPORATE ACTIONS] Fetching historical Splits & Bonuses from {from_date_str} to {to_date_str}...")

    session = requests.Session()
    session.headers.update(HEADERS)

    url = f"https://www.nseindia.com/api/corporate-actions?index=equities&from_date={from_date_str}&to_date={to_date_str}"

    try:
        session.get("https://www.nseindia.com", timeout=10)
        res = session.get(url, timeout=15)

        if res.status_code != 200:
            print(f"[WARNING] Could not fetch corporate actions endpoint (Status: {res.status_code}).")
            return

        actions = res.json()
        cursor = conn.cursor()
        applied_count = 0

        for item in actions:
            symbol = item.get("symbol") or item.get("SYMBOL")
            purpose = item.get("subject") or item.get("PURPOSE") or ""
            ex_date_str = item.get("exDate") or item.get("EX_DATE")

            if not symbol or not ex_date_str or ex_date_str in ["-", ""]:
                continue

            try:
                ex_date = pd.to_datetime(ex_date_str).strftime("%Y-%m-%d")
            except Exception:
                continue

            factor = None
            action_type = None
            purpose_lower = purpose.lower()

            # 1. Parse Splits (e.g., "From Rs 2/- Per Share To Re 1/-", "From Rs 10 To Rs 2")
            if "split" in purpose_lower or "sub-division" in purpose_lower:
                split_match = re.search(r"(\d+)\s*/?-\s*to\s*(?:re\.?|rs\.?)?\s*(\d+)", purpose_lower)
                if not split_match:
                    split_match = re.search(r"rs\.?\s*(\d+)\s*to\s*rs\.?\s*(\d+)", purpose_lower)

                if split_match:
                    old_fv = float(split_match.group(1))
                    new_fv = float(split_match.group(2))
                    if old_fv > 0 and new_fv > 0 and new_fv < old_fv:
                        factor = new_fv / old_fv
                        action_type = "AUTO_SPLIT"

            # 2. Parse Bonuses (e.g., "Bonus 1:1", "Bonus 1:2")
            if "bonus" in purpose_lower:
                bonus_match = re.search(r"bonus.*?\b(\d+)\s*:\s*(\d+)", purpose_lower)
                if bonus_match:
                    bonus_shares = float(bonus_match.group(1))
                    held_shares = float(bonus_match.group(2))
                    if held_shares + bonus_shares > 0:
                        factor = held_shares / (bonus_shares + held_shares)
                        action_type = "AUTO_BONUS"

            if factor and factor < 1.0:
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
                        print(f"[{action_type}] Adjusted {symbol} prior to {ex_date} with factor {factor:.4f}")

        conn.commit()
        print(f"[SUCCESS] Auto-adjusted {applied_count} split/bonus events.")

    except Exception as e:
        print(f"[ERROR] Auto corporate action calculation failed: {e}")


def apply_manual_overrides(conn):
    """Applies manual adjustments from manual_adjustments.json."""
    if not os.path.exists(OVERRIDE_FILE):
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

                    print(f"[MANUAL OVERRIDE] Applied factor {factor} for {symbol} prior to {ex_date}")

        conn.commit()
        print("[SUCCESS] Manual corporate action overrides applied.")
    except Exception as e:
        print(f"[ERROR] Manual overrides failed: {e}")


def main():
    """Main execution pipeline."""
    conn = sqlite3.connect(DB_NAME)

    init_db(conn)
    fetch_nse_bhavcopy_range(conn, start_date=START_DATE)
    auto_adjust_splits_and_bonuses(conn, start_date=START_DATE)
    apply_manual_overrides(conn)

    conn.close()


if __name__ == "__main__":
    main()