import os
import re
import sys
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

FAILED = []   # years that did not convert/upload
DONE = []     # (year, rows, MB, row_groups) for the summary

def convert_and_upload(db_files):
    current_year = str(datetime.datetime.now().year)

    for db_file in db_files:
        if not os.path.exists(db_file):
            continue

        # File name pattern: <database>_<year>.db, e.g. nse_2000.db -> database "nse", year 2000.
        # Each database gets its own folder on R2:  nse/2000.parquet, bse/2000.parquet, ...
        m = re.match(r"^([A-Za-z0-9]+)_(\d{4})\.db$", os.path.basename(db_file))
        if not m:
            print(f"Skipping {db_file}: name must look like <database>_<year>.db")
            continue
        db_id, year_str = m.group(1).lower(), m.group(2)

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
                SELECT symbol, date, open, high, low, close, volume, delivery
                FROM ohlcv
                WHERE is_index = 0
                  AND symbol IS NOT NULL AND TRIM(symbol) <> ''
                  AND UPPER(TRIM(symbol)) NOT IN ({placeholders})
                  AND close IS NOT NULL AND close > 0
                  AND open IS NOT NULL AND open > 0
                ORDER BY symbol, date
                """.format(placeholders=",".join("?" * len(NON_TICKER_SYMBOLS))),
                conn,
                params=NON_TICKER_SYMBOLS
            )
            conn.close()

            if df.empty:
                print(f"Skipping {db_file}: 'ohlcv' table is empty.")
                continue

            # Delivery quantity is NULL for days NSE has no delivery file for. Store it as a
            # plain number column (NaN = missing) so the file always has the same layout.
            df["delivery"] = pd.to_numeric(df["delivery"], errors="coerce").astype("float64")

            parquet_filename = f"{db_id}_{year_str}.parquet"   # temporary local name
            r2_key = f"{db_id}/{year_str}.parquet"               # name inside the bucket

            # Convert to compressed Parquet format
            # Rows are sorted by (symbol, date) and written in small row groups, so the
            # app (DuckDB) can fetch just one stock's rows instead of the whole file.
            df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
            table = pa.Table.from_pandas(df, preserve_index=False)
            pq.write_table(table, parquet_filename, compression='snappy', row_group_size=5000)

            size_mb = os.path.getsize(parquet_filename) / 1048576
            n_groups = pq.ParquetFile(parquet_filename).num_row_groups

            # Upload directly to R2 bucket
            print(f"Uploading {parquet_filename} to Cloudflare R2 as {r2_key}...")
            s3_client.upload_file(parquet_filename, BUCKET_NAME, r2_key)

            # Cleanup local temporary file
            os.remove(parquet_filename)
            print(f"Successfully converted and uploaded {r2_key}!")
            DONE.append((f"{db_id}/{year_str}", len(df), round(size_mb, 1), n_groups))

        except Exception as e:
            print(f"Error processing {db_file}: {e}")
            FAILED.append(f"{db_id}/{year_str}")

if __name__ == "__main__":
    # Automatically finds every <database>_<year>.db file in the repo (nse_2026.db, bse_2026.db, ...)
    # (see docs/repo_structure.md - database files live under data/databases/)
    DB_DIR = "data/databases"
    db_list = sorted(glob.glob(os.path.join(DB_DIR, "*_[0-9][0-9][0-9][0-9].db")))

    if not db_list:
        print("No database files matching pattern '<database>_<year>.db' found.")
    else:
        print(f"Discovered databases: {db_list}")
        convert_and_upload(db_list)

    print("\n===== SUMMARY =====")
    for year, rows, mb, groups in DONE:
        print(f"OK     {year}: {rows} rows, {mb} MB, {groups} row groups")
    for year in FAILED:
        print(f"FAILED {year}")
    if not DONE and not FAILED:
        print("Nothing was converted (no matching database file / current year only).")
    if FAILED:
        sys.exit(1)  # makes the GitHub run show a red cross instead of a green tick
