import os
import datetime
import sqlite3
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import boto3

R2_ACCESS_KEY_ID = os.getenv("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = os.getenv("R2_SECRET_ACCESS_KEY")
R2_ENDPOINT_URL = os.getenv("R2_ENDPOINT_URL")
BUCKET_NAME = "nse-stock-data"

# Check if FORCE_ALL is passed from GitHub Actions
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
            print(f"Skipping {db_file}: File not found.")
            continue

        year_str = "".join(filter(str.isdigit, db_file))

        # DAILY RUN: Process only current year's file to save quota
        # MANUAL RUN: Convert all files (FORCE_ALL is True)
        if year_str != current_year and not FORCE_ALL:
            print(f"Skipping historical year {year_str} (Daily Run mode).")
            continue

        print(f"Processing {db_file}...")
        conn = sqlite3.connect(db_file)
        
        # Read from 'ohlcv' table matching fetch_data.py structure
        df = pd.read_sql_query("SELECT symbol, date, open, high, low, close, volume FROM ohlcv", conn)
        conn.close()

        if df.empty:
            print(f"Skipping {db_file}: Table 'ohlcv' is empty.")
            continue

        parquet_filename = f"{year_str}.parquet"

        # Convert DataFrame to Snappy Parquet
        table = pa.Table.from_pandas(df)
        pq.write_table(table, parquet_filename, compression='snappy')

        # Upload to Cloudflare R2
        print(f"Uploading {parquet_filename} to R2...")
        s3_client.upload_file(parquet_filename, BUCKET_NAME, parquet_filename)

        os.remove(parquet_filename)
        print(f"Successfully processed and uploaded {parquet_filename}!")

if __name__ == "__main__":
    db_list = ["nse_2023.db", "nse_2024.db", "nse_2025.db", "nse_2026.db"]
    convert_and_upload(db_list)