import os
import io
import zipfile
import sqlite3
import pandas as pd
import requests
from datetime import datetime, timedelta

DB_FILE = "nse_eod_data.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
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
            PRIMARY KEY (symbol, date)
        )
    ''')
    conn.commit()
    conn.close()

def get_bhavcopy(target_date):
    date_str = target_date.strftime('%Y%m%d')
    url = f"https://archives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=10)
        if response.status_code == 200:
            z = zipfile.ZipFile(io.BytesIO(response.content))
            df = pd.read_csv(z.open(z.namelist()[0]))
            
            if 'SctySrs' in df.columns:
                df = df[df['SctySrs'] == 'EQ']
                clean_df = df[['TckrSymb', 'TradDt', 'OpnPrc', 'HghtPrc', 'LwtPrc', 'ClsPrc', 'TtlTradgVol']]
                clean_df.columns = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume']
                return clean_df
    except Exception as e:
        pass
    return None

def fetch_date_range(start_date_str, end_date_str):
    init_db()
    conn = sqlite3.connect(DB_FILE)
    
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d')
    
    current_date = start_date
    success_count = 0
    
    print(f"--- Starting Download Range: {start_date_str} to {end_date_str} ---")
    
    while current_date <= end_date:
        # Skip Saturday (5) and Sunday (6)
        if current_date.weekday() < 5:
            df = get_bhavcopy(current_date)
            if df is not None and not df.empty:
                df.to_sql('temp_ohlcv', conn, if_exists='replace', index=False)
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT OR IGNORE INTO ohlcv (symbol, date, open, high, low, close, volume)
                    SELECT symbol, date, open, high, low, close, volume FROM temp_ohlcv
                ''')
                conn.commit()
                print(f"[SUCCESS] Ingested Bhavcopy for {current_date.strftime('%Y-%m-%d')}")
                success_count += 1
            else:
                print(f"[SKIP/HOLIDAY] No data for {current_date.strftime('%Y-%m-%d')}")
        
        current_date += timedelta(days=1)
        
    conn.close()
    print(f"--- Finished! Ingested {success_count} trading days into {DB_FILE} ---")

if __name__ == "__main__":
    # Check if custom dates were passed from GitHub Action parameters
    start_env = os.getenv("START_DATE")
    end_env = os.getenv("END_DATE")
    
    if start_env and end_env:
        fetch_date_range(start_env, end_env)
    else:
        # Default behaviour: Fetch past 3 days (covers weekends/holidays automatically)
        end_date = datetime.now()
        start_date = end_date - timedelta(days=3)
        fetch_date_range(start_date.strftime('%Y-%m-%d'), end_date.strftime('%Y-%m-%d'))