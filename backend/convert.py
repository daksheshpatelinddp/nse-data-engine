import os
import re
import sys
import json
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

def _delivery_expr(conn):
    """'delivery' if the database has that column, otherwise NULL (older databases)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(ohlcv)").fetchall()]
    return "delivery" if "delivery" in cols else "NULL AS delivery"


FAILED = []   # years that did not convert/upload
DONE = []     # (year, rows, MB, row_groups) for the summary

def convert_and_upload(db_files):
    current_year = str(datetime.datetime.now().year)

    # newest year of each database: its yearly file also gets a small symbols.json (search list in the app)
    latest_year = {}
    for _f in db_files:
        _m = re.match(r"^([A-Za-z0-9]+)_(\d{4})\.db$", os.path.basename(_f))
        if _m:
            latest_year[_m.group(1).lower()] = max(latest_year.get(_m.group(1).lower(), 0), int(_m.group(2)))

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
                SELECT symbol, date, open, high, low, close, volume, {delivery_col}
                FROM ohlcv
                WHERE is_index = 0
                  AND symbol IS NOT NULL AND TRIM(symbol) <> ''
                  AND UPPER(TRIM(symbol)) NOT IN ({placeholders})
                  AND close IS NOT NULL AND close > 0
                  AND open IS NOT NULL AND open > 0
                ORDER BY symbol, date
                """.format(placeholders=",".join("?" * len(NON_TICKER_SYMBOLS)), delivery_col=_delivery_expr(conn)),
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

            if int(year_str) == latest_year.get(db_id):
                # one small file with every stock name of the newest year: the app's search list reads this
                # instead of scanning the parquet file (1 request instead of dozens)
                names = sorted(set(df["symbol"].astype(str)))
                s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/symbols.json",
                                     Body=json.dumps({"year": int(year_str), "symbols": names}, separators=(",", ":")).encode(),
                                     ContentType="application/json", CacheControl="no-cache")
                print(f"Uploaded {db_id}/symbols.json ({len(names)} symbols)")

        except Exception as e:
            print(f"Error processing {db_file}: {e}")
            FAILED.append(f"{db_id}/{year_str}")

# =====================================================================================
# HISTORY LAYOUT  (what makes a chart open fast, however many years there are)
#
# Yearly files hold ALL stocks of one year. Opening one stock then touches every yearly file
# (27 files, ~80 requests, ~7 MB). Instead, all years BEFORE the current one are also written
# as "history buckets": each bucket file holds ~64 stocks, and every stock's whole history
# (2000 -> last year) is ONE block inside it. Opening a stock = 1 small header + 1 block
# (~0.3 MB), plus the current year's yearly file (refreshed daily as before).
#
#   <db>/h/<fingerprint>/b000.parquet ... bNNN.parquet   (history, rebuilt only when it changes)
#   <db>/manifest.json                                   (which stock is in which bucket)
#   <db>/<year>.parquet                                  (yearly files, unchanged)
#
# R2 writes: the history is rebuilt only when a past-year database file changed (for example a
# split/bonus adjusts old prices) - about 70 files then, none on a normal day.
# =====================================================================================
import hashlib
import json
import tempfile
import shutil

HIST_BUCKET_SIZE = int(os.getenv("HIST_BUCKET_SIZE", "64") or 64)
HIST_LAYOUT_VERSION = 2
HIST_SCHEMA = pa.schema([
    ("symbol", pa.string()), ("date", pa.string()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()), ("close", pa.float64()),
    ("volume", pa.int64()), ("delivery", pa.float64()),
])
NON_TICKER_SYMBOLS = ('NSE', 'BSE', 'NIFTY', 'NIFTY 50', 'SENSEX')   # same denylist as the yearly files
_ROW_FILTER = ("is_index = 0 AND symbol IS NOT NULL AND TRIM(symbol) <> '' "
               "AND close IS NOT NULL AND close > 0 AND open IS NOT NULL AND open > 0")


def _file_sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def history_fingerprint(files):
    """Changes whenever any past-year database changes (or the layout version/bucket size changes)."""
    h = hashlib.sha1()
    for year, path in files:
        h.update(f"{year}:{_file_sha1(path)};".encode())
    h.update(f"bucket{HIST_BUCKET_SIZE};layout{HIST_LAYOUT_VERSION}".encode())
    return h.hexdigest()


def read_manifest(db_id):
    try:
        obj = s3_client.get_object(Bucket=BUCKET_NAME, Key=f"{db_id}/manifest.json")
        return json.loads(obj["Body"].read())
    except Exception:
        return None          # no manifest yet (or unreadable): treat as "history must be built"


def build_history(db_id, files):
    """files = [(year, path), ...] of one database, sorted by year. Returns a summary tuple or None."""
    cur_year = files[-1][0]
    hist = [(y, p) for y, p in files if y < cur_year]
    if not hist:
        print(f"[HISTORY] {db_id}: no past-year databases yet, nothing to build.")
        return None

    fp = history_fingerprint(hist)
    old = read_manifest(db_id)
    if (not FORCE_ALL and old and old.get("format") == HIST_LAYOUT_VERSION
            and old.get("fingerprint") == fp and old.get("currentYear") == cur_year):
        print(f"[HISTORY] {db_id}: past years unchanged (fingerprint {fp[:10]}), nothing to rebuild.")
        return None

    print(f"[HISTORY] {db_id}: building history buckets for {hist[0][0]}-{hist[-1][0]} "
          f"({'forced' if FORCE_ALL else 'past years changed or no manifest'})...")
    conns = [(y, sqlite3.connect(p)) for y, p in hist]
    tmp = tempfile.mkdtemp(prefix="hist_")
    try:
        deny = ",".join("?" * len(NON_TICKER_SYMBOLS))
        symbols = set()
        expected_rows = 0
        for _, c in conns:
            symbols.update(r[0] for r in c.execute(
                f"SELECT DISTINCT symbol FROM ohlcv WHERE {_ROW_FILTER} AND UPPER(TRIM(symbol)) NOT IN ({deny})",
                NON_TICKER_SYMBOLS))
            expected_rows += c.execute(
                f"SELECT COUNT(*) FROM ohlcv WHERE {_ROW_FILTER} AND UPPER(TRIM(symbol)) NOT IN ({deny})",
                NON_TICKER_SYMBOLS).fetchone()[0]
        symbols = sorted(symbols)
        buckets = [symbols[i:i + HIST_BUCKET_SIZE] for i in range(0, len(symbols), HIST_BUCKET_SIZE)]
        fp10 = fp[:10]
        manifest_buckets, total_rows, total_bytes, total_groups = {}, 0, 0, 0

        for n, names in enumerate(buckets):
            bname = f"b{n:03d}"
            marks = ",".join("?" * len(names))
            frames = [pd.read_sql_query(
                f"SELECT symbol, date, open, high, low, close, volume, {_delivery_expr(c)} FROM ohlcv "
                f"WHERE {_ROW_FILTER} AND symbol IN ({marks})", c, params=names) for _, c in conns]
            df = pd.concat(frames, ignore_index=True)
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
            df["delivery"] = pd.to_numeric(df["delivery"], errors="coerce").astype("float64")
            df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

            local = os.path.join(tmp, f"{bname}.parquet")
            with pq.ParquetWriter(local, HIST_SCHEMA, compression="snappy") as writer:
                for sym, g in df.groupby("symbol", sort=True):
                    # one stock = exactly one row group, so the app reads just that block
                    writer.write_table(pa.Table.from_pandas(g, schema=HIST_SCHEMA, preserve_index=False),
                                       row_group_size=len(g))
            size = os.path.getsize(local)
            groups = pq.ParquetFile(local).num_row_groups
            if groups != df["symbol"].nunique():
                raise RuntimeError(f"{bname}: expected {df['symbol'].nunique()} row groups, got {groups}")
            s3_client.upload_file(local, BUCKET_NAME, f"{db_id}/h/{fp10}/{bname}.parquet")
            os.remove(local)
            manifest_buckets[bname] = {"size": size, "symbols": names}
            total_rows += len(df); total_bytes += size; total_groups += groups
            if (n + 1) % 10 == 0 or n + 1 == len(buckets):
                print(f"[HISTORY]   uploaded {n + 1}/{len(buckets)} buckets ({total_rows} rows so far)")

        if total_rows != expected_rows:
            raise RuntimeError(f"row count mismatch: databases have {expected_rows} rows, buckets {total_rows}")

        manifest = {
            "format": HIST_LAYOUT_VERSION, "db": db_id,
            "firstYear": hist[0][0], "histThrough": hist[-1][0], "currentYear": cur_year,
            "fingerprint": fp, "histPath": f"h/{fp10}", "bucketSize": HIST_BUCKET_SIZE,
            "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
            "rows": total_rows, "buckets": manifest_buckets,
        }
        # The manifest goes up LAST: until then the app keeps using the previous, complete history.
        s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/manifest.json",
                             Body=json.dumps(manifest, separators=(",", ":")).encode(),
                             ContentType="application/json", CacheControl="no-cache")

        # Remove the previous history (any other fingerprint folder). Not fatal if it fails.
        try:
            stale, token = [], None
            while True:
                kw = {"Bucket": BUCKET_NAME, "Prefix": f"{db_id}/h/"}
                if token:
                    kw["ContinuationToken"] = token
                page = s3_client.list_objects_v2(**kw)
                stale += [o["Key"] for o in page.get("Contents", []) if not o["Key"].startswith(f"{db_id}/h/{fp10}/")]
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
            for i in range(0, len(stale), 500):
                s3_client.delete_objects(Bucket=BUCKET_NAME,
                                         Delete={"Objects": [{"Key": k} for k in stale[i:i + 500]]})
            if stale:
                print(f"[HISTORY]   removed {len(stale)} files of the previous history")
        except Exception as e:
            print(f"[HISTORY]   (could not remove old history files: {e})")

        print(f"[HISTORY] {db_id}: done - {len(symbols)} stocks in {len(buckets)} bucket files, "
              f"{total_rows} rows, {total_bytes / 1048576:.1f} MB")
        return (f"{db_id}/history", total_rows, round(total_bytes / 1048576, 1), total_groups)
    finally:
        for _, c in conns:
            c.close()
        shutil.rmtree(tmp, ignore_errors=True)


def build_all_histories(db_list):
    groups = {}
    for path in db_list:
        m = re.match(r"^([A-Za-z0-9]+)_(\d{4})\.db$", os.path.basename(path))
        if m:
            groups.setdefault(m.group(1).lower(), []).append((int(m.group(2)), path))
    for db_id, files in groups.items():
        try:
            res = build_history(db_id, sorted(files))
            if res:
                DONE.append(res)
        except Exception as e:
            print(f"[HISTORY] ERROR building history for {db_id}: {e}")
            FAILED.append(f"{db_id}/history")



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
        build_all_histories(db_list)

    print("\n===== SUMMARY =====")
    for year, rows, mb, groups in DONE:
        print(f"OK     {year}: {rows} rows, {mb} MB, {groups} row groups")
    for year in FAILED:
        print(f"FAILED {year}")
    if not DONE and not FAILED:
        print("Nothing was converted (no matching database file / current year only).")
    if FAILED:
        sys.exit(1)  # makes the GitHub run show a red cross instead of a green tick
