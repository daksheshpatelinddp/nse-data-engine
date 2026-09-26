import os
import glob
import datetime
import sqlite3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import boto3

# Read credentials from GitHub Secrets
R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")
BUCKET_NAME = "nse-stock-data"

# FORCE_ALL is set to True during manual workflow execution
FORCE_ALL = os.getenv("FORCE_ALL", "false").lower() == "true"

s3_client = boto3.client(
    "s3",
    endpoint_url=R2_ENDPOINT_URL,
    aws_access_key_id=R2_ACCESS_KEY_ID,
    aws_secret_access_key=R2_SECRET_ACCESS_KEY,
    region_name="auto"
)

def convert_and_upload(db_files):
    current_year = str(datetime.datetime.now().year)

    for db_file in db_files:
        if not os.path.exists(db_file):
            continue

        # Extract year digits from filename (e.g., nse_2000.db -> 2000)
        year_str = "".join(filter(str.isdigit, db_file))

        # DAILY AUTOMATED RUN: Only updates current year (e.g., 2026.parquet)
        # MANUAL RUN: Converts every discovered database year from 2000 to present
        if year_str != current_year and not FORCE_ALL:
            print(f"Skipping historical year {year_str} (Daily Run mode).")
            continue

        print(f"Processing {db_file}...")
        try:
            conn = sqlite3.connect(db_file)
            # Defensive filter at the source: only ever ship rows that are a real,
            # priced equity trading day. This guards against any bad rows already
            # sitting in the .db file (e.g. leftover test inserts, or rows written
            # before fetch_data.py's zero-price sanity check existed) ever reaching
            # the parquet files / R2 / the frontend, regardless of how they got in.
            #
            # Explicit denylist: "NSE" (and common index labels) have shown up as a
            # literal symbol value with real-looking price data attached - almost
            # certainly a leftover manual/test row, not an actual tradable ticker.
            # Block these by name since they pass every other numeric sanity check.
            NON_TICKER_SYMBOLS = ('NSE', 'BSE', 'NIFTY', 'NIFTY 50', 'SENSEX')
            df = pd.read_sql_query(
                """
                SELECT symbol, date, open, high, low, close, volume
                FROM ohlcv
                WHERE is_index = 0
                  AND symbol IS NOT NULL AND TRIM(symbol) <> ''
                  AND UPPER(TRIM(symbol)) NOT IN ({placeholders})
                  AND close IS NOT NULL AND close > 0
                  AND open IS NOT NULL AND open > 0
                """.format(placeholders=",".join("?" * len(NON_TICKER_SYMBOLS))),
                conn,
                params=NON_TICKER_SYMBOLS
            )
            conn.close()

            if df.empty:
                print(f"Skipping {db_file}: 'ohlcv' table is empty.")
                continue

            parquet_filename = f"{year_str}.parquet"

            # Convert to compressed Parquet format
            table = pa.Table.from_pandas(df)
            pq.write_table(table, parquet_filename, compression='snappy')

            # Upload directly to R2 bucket
            print(f"Uploading {parquet_filename} to Cloudflare R2...")
            s3_client.upload_file(parquet_filename, BUCKET_NAME, parquet_filename)

            # Cleanup local temporary file
            os.remove(parquet_filename)
            print(f"Successfully converted and uploaded {parquet_filename}!")

        except Exception as e:
            print(f"Error processing {db_file}: {e}")

if __name__ == "__main__":
    # Automatically finds all nse_YYYY.db files present in the repo
    db_list = sorted(glob.glob("nse_*.db"))
    
    if not db_list:
        print("No database files matching pattern 'nse_*.db' found.")
    else:
        print(f"Discovered databases: {db_list}")
        convert_and_upload(db_list)