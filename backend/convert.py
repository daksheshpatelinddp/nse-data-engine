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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dividends          # cash-dividend adjustment (computed here from data/corporate_actions/dividends.csv, never written into the .db files)
import indices            # NSE index data (names like ^NIFTY50) travels in the same files as the stocks

ADJ = None                # set in __main__: the dividend adjuster (does nothing when there are no dividends or DIVIDEND_ADJUST=false)


def _adj(df):
    """Dividend-adjusted copy of a frame (symbol, date, open, high, low, close ...). Indices are never touched."""
    return ADJ.adjust(df) if ADJ else df

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
                WHERE symbol IS NOT NULL AND TRIM(symbol) <> ''
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
            df = _adj(df)

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
                                     Body=json.dumps({"year": int(year_str), "symbols": names,
                                                      "indices": [n for n in names if n.startswith("^")]}, separators=(",", ":")).encode(),
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
_ROW_FILTER = ("symbol IS NOT NULL AND TRIM(symbol) <> '' "
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
    h.update(f"bucket{HIST_BUCKET_SIZE};layout{HIST_LAYOUT_VERSION};pack{PACK_VERSION}".encode())   # a new pack format rebuilds the history once
    if ADJ and ADJ.signature:
        h.update(f"dividends:{ADJ.signature}".encode())      # a new dividend changes every older price of that stock: rebuild the history
    return h.hexdigest()



# =====================================================================================
# PACKS (format 3): the same history in a compact form for fast loading and bulk work.
#
# Every stock's history is ONE zlib-compressed block (dates and prices delta-coded as 32-bit integers,
# prices in paise). Blocks of ~64 stocks are concatenated into one file per bucket and the byte position
# of every block is listed in manifest.json, so the app fetches a stock with a single range request:
# no file header to read first. A block is ~5x smaller than the same stock in parquet. A second, daily
# file ("recent") holds the last 400 bars of every active stock: scans read just that one file.
#
#   <db>/p/<fingerprint>/b000.pack ...   history packs     (rebuilt together with the history)
#   <db>/r/<token>.pack + recent.json    last 400 bars     (rebuilt on every conversion run)
# =====================================================================================
import zlib
import struct
import numpy as np

PACK_VERSION = 2          # v2: volume/delivery as int32 (-1 = unknown), bytes regrouped into 4 byte-planes; readers still accept v1 blocks
RECENT_BARS = int(os.getenv("RECENT_BARS", "400") or 400)


def encode_pack_block_v1(dates, o, h, l, c, v, dq):
    """One stock -> compressed bytes. Layout before compression (little endian):
       'AB', version, mode, n(u32), scale(i32) | dates: i32 deltas | close: i32 deltas |
       open-close, high-close, low-close: i32 | padding to 8 bytes | volume f64 | delivery f64.
       mode 1 = prices as integers (price*scale, exact); mode 2 = prices as float64 (never expected)."""
    n = len(dates)
    days = np.array(dates, dtype="datetime64[D]").astype("int64")
    arrs = [np.asarray(a, dtype="float64") for a in (o, h, l, c)]
    scale = 0
    for sc in (100, 10000):
        cents = [np.rint(a * sc) for a in arrs]
        if all(np.all(np.abs(a * sc - x) < 1e-5) and np.all(np.abs(x) < 2 ** 30) for a, x in zip(arrs, cents)):
            scale = sc
            break

    def delta(a):
        d = np.empty(len(a), dtype="int64")
        d[0] = a[0]
        d[1:] = np.diff(a)
        return d

    parts = [struct.pack("<2sBBIi", b"AB", 1, 1 if scale else 2, n, scale),
             delta(days).astype("<i4").tobytes()]
    if scale:
        oc, hc, lc, cc = cents
        parts.append(delta(cc).astype("<i4").tobytes())
        for x in (oc, hc, lc):
            parts.append((x - cc).astype("<i4").tobytes())
    else:
        for a in (arrs[3], arrs[0], arrs[1], arrs[2]):
            parts.append(a.astype("<f8").tobytes())
    parts.append(b"\0" * ((-sum(len(x) for x in parts)) % 8))
    parts.append(np.asarray(v, dtype="<f8").tobytes())
    parts.append(np.asarray(dq, dtype="<f8").tobytes())
    return zlib.compress(b"".join(parts), 9)


def encode_pack_block(dates, o, h, l, c, v, dq):
    """One stock -> compressed bytes, pack format v2 (little endian), before compression:
       'AB', 2, mode(1), n(u32), scale(i32)  = 12-byte header, then 7 int32 arrays of n values each:
       date deltas | close deltas | open-close | high-close | low-close | volume | delivery  (volume/delivery: -1 = unknown)
       and those 7*n int32 values are stored as 4 BYTE-PLANES: all first bytes, then all second bytes, ...
       (the high bytes are nearly always 0 or 255, so zlib squeezes them to almost nothing).
       When a block cannot be stored this way (prices not exact, or volume does not fit int32) the v1 layout is written
       instead; readers accept both."""
    n = len(dates)
    days = np.array(dates, dtype="datetime64[D]").astype("int64")
    arrs = [np.asarray(a, dtype="float64") for a in (o, h, l, c)]
    scale = 0
    for sc in (100, 10000):
        cents = [np.rint(a * sc) for a in arrs]
        if all(np.all(np.abs(a * sc - x) < 1e-5) and np.all(np.abs(x) < 2 ** 30) for a, x in zip(arrs, cents)):
            scale = sc
            break

    def ints_or_unknown(a):
        a = np.asarray(a, dtype="float64")
        bad = np.isnan(a)
        x = np.where(bad, -1.0, np.rint(a))
        ok = bool(np.all(np.abs(a[~bad] - x[~bad]) < 1e-9) and np.all(x >= -1) and np.all(x < 2 ** 31 - 1)
                  and not np.any((x == -1) & ~bad))
        return x, ok

    vol, vol_ok = ints_or_unknown(v)
    dlv, dlv_ok = ints_or_unknown(dq)
    if not (scale and vol_ok and dlv_ok):
        return encode_pack_block_v1(dates, o, h, l, c, v, dq)

    def delta(a):
        d = np.empty(len(a), dtype="int64")
        d[0] = a[0]
        d[1:] = np.diff(a)
        return d

    oc, hc, lc, cc = cents
    body = np.concatenate([delta(days), delta(cc), oc - cc, hc - cc, lc - cc, vol, dlv]).astype("<i4")
    planes = np.frombuffer(body.tobytes(), dtype=np.uint8).reshape(-1, 4).T.tobytes()
    return zlib.compress(struct.pack("<2sBBIi", b"AB", 2, 1, n, scale) + planes, 9)


def pack_group(g):
    return encode_pack_block(g["date"].tolist(), g["open"].values, g["high"].values, g["low"].values,
                             g["close"].values, g["volume"].values.astype("float64"),
                             g["delivery"].values.astype("float64"))


def _symbol_aliases(known_symbols):
    """old name -> latest name, for old names that no longer exist as a symbol (the app uses it for typed names)."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import symbol_map as sm
        changes = sm.load_symbol_changes()
    except Exception:
        return {}
    known = set(known_symbols)
    out = {}
    for old in changes:
        final = sm.canonical_symbol(old, "0000-00-00", changes)
        if old not in known and final in known and final != old:
            out[old] = final
    return out


def build_recent(db_id, files):
    """Last RECENT_BARS bars of every active stock in ONE file (scanners, explorations, charts' newest part)."""
    cur_year = files[-1][0]
    newest = files[-3:]                                   # two to three years are plenty for 400 bars
    deny = ",".join("?" * len(NON_TICKER_SYMBOLS))
    frames, active = [], set()
    for year, path in newest:
        c = sqlite3.connect(path)
        try:
            frames.append(pd.read_sql_query(
                f"SELECT symbol, date, open, high, low, close, volume, {_delivery_expr(c)} FROM ohlcv "
                f"WHERE {_ROW_FILTER} AND UPPER(TRIM(symbol)) NOT IN ({deny})", c, params=NON_TICKER_SYMBOLS))
            if year == cur_year:
                active = set(frames[-1]["symbol"])
        finally:
            c.close()
    df = pd.concat(frames, ignore_index=True)
    df = df[df["symbol"].isin(active)]
    df = _adj(df)
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    df["delivery"] = pd.to_numeric(df["delivery"], errors="coerce").astype("float64")
    df = df.sort_values(["symbol", "date"]).groupby("symbol", sort=True).tail(RECENT_BARS)

    idx, parts, off = {}, [], 0
    for sym, g in df.groupby("symbol", sort=True):
        blob = pack_group(g)
        idx[sym] = [off, len(blob), len(g)]
        parts.append(blob)
        off += len(blob)
    pack = b"".join(parts)
    token = hashlib.sha1(pack).hexdigest()[:12]

    old = None
    try:
        old = json.loads(s3_client.get_object(Bucket=BUCKET_NAME, Key=f"{db_id}/recent.json")["Body"].read())
    except Exception:
        pass
    if old and old.get("token") == token and not FORCE_ALL:
        print(f"[RECENT] {db_id}: unchanged ({len(idx)} stocks, token {token}).")
        return None
    s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/r/{token}.pack", Body=pack, ContentType="application/octet-stream")
    meta = {"format": 1, "db": db_id, "token": token, "file": f"r/{token}.pack", "size": len(pack),
            "bars": RECENT_BARS, "currentYear": cur_year, "stocks": len(idx),
            "built": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "idx": idx}
    s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/recent.json",
                         Body=json.dumps(meta, separators=(",", ":")).encode(),
                         ContentType="application/json", CacheControl="no-cache")
    try:                                                   # remove older recent packs
        page = s3_client.list_objects_v2(Bucket=BUCKET_NAME, Prefix=f"{db_id}/r/")
        stale = [o["Key"] for o in page.get("Contents", []) if o["Key"] != f"{db_id}/r/{token}.pack"]
        if stale:
            s3_client.delete_objects(Bucket=BUCKET_NAME, Delete={"Objects": [{"Key": k} for k in stale]})
    except Exception as e:
        print(f"[RECENT]   (could not remove old recent packs: {e})")
    print(f"[RECENT] {db_id}: {len(idx)} stocks x last {RECENT_BARS} bars, {len(pack) / 1048576:.1f} MB (token {token})")
    return (f"{db_id}/recent", int(sum(v[2] for v in idx.values())), round(len(pack) / 1048576, 1), len(idx))


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
        pack_buckets, pack_total = {}, 0

        for n, names in enumerate(buckets):
            bname = f"b{n:03d}"
            marks = ",".join("?" * len(names))
            frames = [pd.read_sql_query(
                f"SELECT symbol, date, open, high, low, close, volume, {_delivery_expr(c)} FROM ohlcv "
                f"WHERE {_ROW_FILTER} AND symbol IN ({marks})", c, params=names) for _, c in conns]
            df = pd.concat(frames, ignore_index=True)
            df = _adj(df)
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
            df["delivery"] = pd.to_numeric(df["delivery"], errors="coerce").astype("float64")
            df = df.sort_values(["symbol", "date"]).reset_index(drop=True)

            local = os.path.join(tmp, f"{bname}.parquet")
            pack_idx, pack_parts, pack_off = {}, [], 0
            with pq.ParquetWriter(local, HIST_SCHEMA, compression="snappy") as writer:
                for sym, g in df.groupby("symbol", sort=True):
                    # one stock = exactly one row group, so the app reads just that block
                    writer.write_table(pa.Table.from_pandas(g, schema=HIST_SCHEMA, preserve_index=False),
                                       row_group_size=len(g))
                    blob = pack_group(g)                          # the same stock in the compact pack format
                    pack_idx[sym] = [pack_off, len(blob), len(g)]
                    pack_parts.append(blob)
                    pack_off += len(blob)
            pack_bytes = b"".join(pack_parts)
            s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/p/{fp10}/{bname}.pack", Body=pack_bytes,
                                 ContentType="application/octet-stream")
            pack_buckets[bname] = {"size": len(pack_bytes), "idx": pack_idx}
            pack_total += len(pack_bytes)
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
            "packs": {"version": PACK_VERSION, "path": f"p/{fp10}", "buckets": pack_buckets},
            "aliases": {**_symbol_aliases(symbols),
                        **indices.alias_map([n for n in symbols if n.startswith("^")], [n for n in symbols if not n.startswith("^")])},
            "indices": [n for n in symbols if n.startswith("^")],
        }
        # The manifest goes up LAST: until then the app keeps using the previous, complete history.
        s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/manifest.json",
                             Body=json.dumps(manifest, separators=(",", ":")).encode(),
                             ContentType="application/json", CacheControl="no-cache")

        # Remove the previous history (any other fingerprint folder). Not fatal if it fails.
        try:
            stale = []
            for folder in ("h", "p"):
                token = None
                while True:
                    kw = {"Bucket": BUCKET_NAME, "Prefix": f"{db_id}/{folder}/"}
                    if token:
                        kw["ContinuationToken"] = token
                    page = s3_client.list_objects_v2(**kw)
                    stale += [o["Key"] for o in page.get("Contents", [])
                              if not o["Key"].startswith(f"{db_id}/{folder}/{fp10}/")]
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
              f"{total_rows} rows, {total_bytes / 1048576:.1f} MB parquet + {pack_total / 1048576:.1f} MB packs")
        return (f"{db_id}/history", total_rows, round(total_bytes / 1048576, 1), total_groups)
    finally:
        for _, c in conns:
            c.close()
        shutil.rmtree(tmp, ignore_errors=True)


def upload_index_names(db_id="nse"):
    """indices.json = {"^NIFTY50": "NIFTY 50", ...}: the readable names of the index symbols (written by the fetch step)."""
    try:
        with open(indices.INDEX_NAMES_JSON, encoding="utf-8") as f:
            names = json.load(f)
    except (OSError, ValueError):
        return
    if names:
        s3_client.put_object(Bucket=BUCKET_NAME, Key=f"{db_id}/indices.json", Body=json.dumps(names, separators=(",", ":")).encode(),
                             ContentType="application/json", CacheControl="no-cache")
        print(f"Uploaded {db_id}/indices.json ({len(names)} index names)")


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
        try:
            res = build_recent(db_id, sorted(files))
            if res:
                DONE.append(res)
        except Exception as e:
            print(f"[RECENT] ERROR building the recent pack for {db_id}: {e}")
            FAILED.append(f"{db_id}/recent")



if __name__ == "__main__":
    # Automatically finds every <database>_<year>.db file in the repo (nse_2026.db, bse_2026.db, ...)
    # (see docs/repo_structure.md - database files live under data/databases/)
    DB_DIR = "data/databases"
    db_list = sorted(glob.glob(os.path.join(DB_DIR, "*_[0-9][0-9][0-9][0-9].db")))

    if not db_list:
        print("No database files matching pattern '<database>_<year>.db' found.")
    else:
        print(f"Discovered databases: {db_list}")
        ADJ = dividends.build_adjuster(DB_DIR)
        convert_and_upload(db_list)
        build_all_histories(db_list)
        upload_index_names()

    print("\n===== SUMMARY =====")
    for year, rows, mb, groups in DONE:
        print(f"OK     {year}: {rows} rows, {mb} MB, {groups} row groups")
    for year in FAILED:
        print(f"FAILED {year}")
    if not DONE and not FAILED:
        print("Nothing was converted (no matching database file / current year only).")
    if FAILED:
        sys.exit(1)  # makes the GitHub run show a red cross instead of a green tick
