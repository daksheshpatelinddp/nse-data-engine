"""
Exports the yearly databases (data/databases/<db>_<year>.db) to CSV files for the ORIGINAL AmiBroker, with exactly the same prices as the
files the web app reads from R2: the same rows (same filters and name denylist as convert.py), the same split / bonus / rights / demerger
adjustment already stored in the .db files, and the same cash-dividend adjustment that convert.py applies while converting.
Because it uses convert.py's own reader and adjuster, a change there changes both places the same way.

  python backend/export_csv.py [--db nse] [--from-year 2000] [--to-year 2026] [--out export] [--split-mb 40] [--zip]

Writes into --out:   <db>_<year>.csv (one per year; a year bigger than --split-mb MB is written as one file per month: <db>_<year>-<month>.csv),
<db>_eod.format (AmiBroker import definition), IMPORT_README.txt, export-report.txt.   --zip also makes <db>-amibroker-csv.zip.
Nothing is sent anywhere: the workflow (export-csv.yml) commits the folder to a branch of the repository. No R2 is used.
Columns:  Ticker, Date (YYYY-MM-DD), Open, High, Low, Close (4 decimals), Volume (shares), Delivery (shares, empty when NSE has no figure),
DeliveryPct (= Delivery / Volume x 100, empty when either is unknown). In AmiBroker: Aux1 = Delivery, Aux2 = DeliveryPct.
"""
import argparse
import glob
import os
import re
import sqlite3
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pandas as pd
import dividends
import convert

COLUMNS = ["Ticker", "Date", "Open", "High", "Low", "Close", "Volume", "Delivery", "DeliveryPct"]
MAX_FILE_BYTES = 95e6                      # GitHub refuses files over 100 MB

FORMAT_FILE = """$FORMAT Ticker, Date_YMD, Open, High, Low, Close, Volume, Aux1, Aux2
$SKIPLINES 1
$SEPARATOR ,
$CONT 1
$GROUP 255
$AUTOADD 1
"""

README = """AmiBroker import of the {db} data  (made {stamp})
==========================================================

These CSV files hold the SAME prices as the web app: end-of-day bars of every stock and index, already adjusted for splits, bonus,
rights, demergers AND cash dividends (the dividend adjustment the app applies when it converts the data).
So do NOT let AmiBroker adjust them again (do not add splits in Symbol > Information).

Columns: Ticker, Date (YYYY-MM-DD), Open, High, Low, Close, Volume (shares), Delivery (shares, empty when unknown),
DeliveryPct (Delivery / Volume x 100, empty when unknown). The database holds no other fields (no turnover, no number of trades).
Index names start with ^ (for example ^NIFTY50). One file per year: {db}_<year>.csv; a big year is split in monthly files
({db}_<year>-<month>.csv, for example {db}_2024-03.csv). Just import all of them.

Import (once):
 1. Download everything from GitHub: open the repository, choose the branch  csv-export , Code > Download ZIP, then unzip it anywhere.
 2. Copy  {db}_eod.format  into the  Formats  folder of your AmiBroker installation (next to Broker.exe).
 3. AmiBroker: File > New > Database. Choose a folder and a name, "Local data", then set Base time interval = Daily
    and "Number of bars" to 10000 or more (the files go back to {first_year}). Press Create.
 4. File > Import ASCII...  In "Files of type" choose  {db}_eod ,  select ALL the .csv files (Ctrl+A) and press Open.
    Importing takes a few minutes. Importing the same files again updates the bars.
 5. In Analysis use "Apply to: All symbols" (or a filter) and the same date range as in the web app.

In AFL:  Aux1 = delivery quantity,  Aux2 = delivery %.   Checks: export-report.txt lists rows, stocks and the sum of all closes per year.
"""


def read_year(db_file):
    """The rows of one yearly database exactly as convert.py ships them (filters, denylist, dividend adjustment)."""
    conn = sqlite3.connect(db_file)
    try:
        deny = ",".join("?" * len(convert.NON_TICKER_SYMBOLS))
        df = pd.read_sql_query(
            f"SELECT symbol, date, open, high, low, close, volume, {convert._delivery_expr(conn)} FROM ohlcv "
            f"WHERE {convert._ROW_FILTER} AND UPPER(TRIM(symbol)) NOT IN ({deny}) ORDER BY symbol, date",
            conn, params=convert.NON_TICKER_SYMBOLS)
    finally:
        conn.close()
    if df.empty:
        return df
    df = convert._adj(df)
    df["delivery"] = pd.to_numeric(df["delivery"], errors="coerce").astype("float64")
    return df.sort_values(["symbol", "date"]).reset_index(drop=True)


def to_csv_frame(df):
    vol = pd.to_numeric(df["volume"], errors="coerce")
    pct = (df["delivery"] / vol.where(vol > 0) * 100).round(2)          # empty when delivery or volume is unknown
    out = pd.DataFrame({
        "Ticker": df["symbol"].astype(str), "Date": df["date"].astype(str),
        "Open": df["open"].round(4), "High": df["high"].round(4), "Low": df["low"].round(4), "Close": df["close"].round(4),
        "Volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64"),
        "Delivery": df["delivery"].round(0).astype("Int64"),                        # nullable integer: empty when unknown
        "DeliveryPct": pct,
    })
    return out[COLUMNS]


def export(db_id, db_files, out_dir, from_year=None, to_year=None, split_mb=40):
    """One CSV per year; a year bigger than split_mb MB becomes one file per month (GitHub warns at 50 MB and refuses 100 MB)."""
    os.makedirs(out_dir, exist_ok=True)
    report, written, years = [], [], []
    for year, path in db_files:
        if (from_year and year < from_year) or (to_year and year > to_year):
            continue
        df = read_year(path)
        if df.empty:
            report.append(f"{year}: empty, skipped")
            continue
        cf = to_csv_frame(df)
        name = f"{db_id}_{year}.csv"
        full = os.path.join(out_dir, name)
        cf.to_csv(full, index=False, float_format="%.4f", lineterminator="\r\n")
        parts = [name]
        if os.path.getsize(full) > split_mb * 1e6:                      # too big for one file: monthly files instead
            os.remove(full); parts = []
            for month, g in cf.groupby(cf["Date"].str[:7], sort=True):
                mname = f"{db_id}_{month}.csv"
                g.to_csv(os.path.join(out_dir, mname), index=False, float_format="%.4f", lineterminator="\r\n")
                if os.path.getsize(os.path.join(out_dir, mname)) > MAX_FILE_BYTES:
                    raise RuntimeError(f"{mname} is over 95 MB: GitHub would refuse it")
                parts.append(mname)
        report.append(f"{year}: {len(df):>8} rows, {df['symbol'].nunique():>5} symbols, {df['date'].min()} .. {df['date'].max()}, sum of closes {df['close'].sum():,.2f}"
                      + (f"  [{len(parts)} monthly files]" if len(parts) > 1 else ""))
        written.extend(parts); years.append(year)
        print(f"[CSV] {year}: {len(df)} rows -> {len(parts)} file{'s' if len(parts) > 1 else ''}")
    return written, years, report


def package(db_id, out_dir, written, years, report, make_zip=False):
    import datetime
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fmt = f"{db_id}_eod.format"
    open(os.path.join(out_dir, fmt), "w", newline="\r\n").write(FORMAT_FILE)
    open(os.path.join(out_dir, "IMPORT_README.txt"), "w", newline="\r\n").write(README.format(db=db_id, stamp=stamp, first_year=min(years)))
    adj = "cash dividends applied (dividends.csv)" if convert.ADJ else "NO dividend adjustment (DIVIDEND_ADJUST=false or no dividends.csv)"
    open(os.path.join(out_dir, "export-report.txt"), "w", newline="\r\n").write(
        f"{db_id} export, {stamp}\nPrices: split/bonus/rights/demerger adjusted (stored in the .db files) + {adj}\n\n" + "\n".join(report) + "\n")
    if not make_zip:
        return None
    zip_path = os.path.join(out_dir, f"{db_id}-amibroker-csv.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for n in written + [fmt, "IMPORT_README.txt", "export-report.txt"]:
            z.write(os.path.join(out_dir, n), n)
    return zip_path


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="nse"); ap.add_argument("--from-year", type=int); ap.add_argument("--to-year", type=int)
    ap.add_argument("--out", default="export"); ap.add_argument("--split-mb", type=float, default=40.0); ap.add_argument("--zip", action="store_true")
    ap.add_argument("--db-dir", default=os.getenv("DB_DIR", "data/databases"))
    a = ap.parse_args(argv)
    files = []
    for p in sorted(glob.glob(os.path.join(a.db_dir, f"{a.db}_[0-9][0-9][0-9][0-9].db"))):
        m = re.match(rf"^{re.escape(a.db)}_(\d{{4}})\.db$", os.path.basename(p))
        if m:
            files.append((int(m.group(1)), p))
    if not files:
        print(f"No {a.db}_<year>.db files in {a.db_dir}"); return 1
    convert.ADJ = dividends.build_adjuster(a.db_dir)                    # the same adjuster convert.py builds
    written, years, report = export(a.db, files, a.out, a.from_year, a.to_year, a.split_mb)
    if not written:
        print("Nothing exported (check the year range)."); return 1
    zip_path = package(a.db, a.out, written, years, report, a.zip)
    total = sum(os.path.getsize(os.path.join(a.out, n)) for n in written) / 1048576
    print(f"[DONE] {len(written)} CSV files, {total:.0f} MB in {a.out}" + (f"; zip {zip_path}" if zip_path else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
