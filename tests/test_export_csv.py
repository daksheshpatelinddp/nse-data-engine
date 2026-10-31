"""
Check of backend/export_csv.py with synthetic databases (no network):  python tests/test_export_csv.py
The CSV must hold exactly the numbers that convert.py ships to R2 (same rows, same split-era prices, same dividend adjustment), in the layout
AmiBroker's ASCII importer reads. Hand-calculated expectations are the ones of test_convert_e2e.py (AAA: dividends Rs4 ex 2024-03-01 and Rs5 ex 2025-09-01), but here the price is 50 on every day and there is no split, so the factors are 1-4/50 = 0.92 and 1-5/50 = 0.90.
"""
import csv, io, json, os, sqlite3, sys, tempfile, zipfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
import pandas as pd, numpy                  # (before the stand-ins are put on the path)
try:
    import pyarrow, boto3
    STUB = False
except ImportError:
    sys.path.insert(0, os.path.join(ROOT, "tests", "stubs")); STUB = True
tmp = tempfile.mkdtemp(prefix="exp_")
os.environ.update(CA_DIR=os.path.join(tmp, "ca"), DB_DIR=os.path.join(tmp, "db"), INDEX_NAMES_JSON=os.path.join(tmp, "index_names.json"), FORCE_ALL="true")
os.makedirs(os.environ["CA_DIR"]); os.makedirs(os.environ["DB_DIR"])
import dividends, convert, export_csv

SCHEMA = """CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, is_index INTEGER DEFAULT 0, delivery INTEGER, PRIMARY KEY (symbol, date));
CREATE TABLE corporate_actions (symbol TEXT, ex_date TEXT, purpose TEXT, ratio REAL, action_type TEXT, PRIMARY KEY (symbol, ex_date, action_type));"""
def make(year, days, old=None):
    p = os.path.join(os.environ["DB_DIR"], f"nse_{year}.db"); c = sqlite3.connect(p); c.executescript(SCHEMA)
    for d in days:
        c.execute("INSERT INTO ohlcv VALUES ('AAA',?,50,51,49,50,1000,0,500)", (d,))
        c.execute("INSERT INTO ohlcv VALUES ('BBB',?,200,210,190,205.12346,5000,0,NULL)", (d,))
        c.execute("INSERT INTO ohlcv VALUES ('^NIFTY50',?,20000,20100,19900,20050,0,1,NULL)", (d,))
    c.execute("INSERT INTO ohlcv VALUES ('NSE','%s',1,1,1,1,1,0,NULL)" % days[0])           # on the denylist: must never be exported
    c.execute("INSERT INTO ohlcv VALUES ('BAD','%s',0,0,0,0,1,0,NULL)" % days[0])           # zero prices: filtered like convert.py does
    c.commit(); c.close(); return p
dbs = [make(2024, ["2024-01-02", "2024-03-04", "2024-12-31"]), make(2025, ["2025-01-01", "2025-08-29", "2025-09-01"]), make(2026, ["2026-01-01", "2026-10-23"])]
pd.DataFrame({"symbol": ["AAA", "AAA"], "ex_date": ["2024-03-01", "2025-09-01"], "amount": [4.0, 5.0],
              "purpose": ["Dividend - Rs 4 Per Share", "Dividend - Rs 5 Per Share"]}).to_csv(dividends.DIVIDENDS_CSV, index=False)

class NoR2:                                   # the export must never touch R2 (no class A / class B operations)
    def __getattr__(self, n): raise AssertionError(f"export tried to use R2: s3_client.{n}")
convert.s3_client = NoR2()
out = os.path.join(tmp, "export")
assert export_csv.main(["--db", "nse", "--db-dir", os.environ["DB_DIR"], "--out", out]) == 0
names = sorted(os.listdir(out)); print(names)
assert names == ["IMPORT_README.txt", "export-report.txt", "nse_2024.csv", "nse_2025.csv", "nse_2026.csv", "nse_eod.format"], names      # small years stay one file each; no zip unless asked

def rows(year):
    with open(os.path.join(out, f"nse_{year}.csv"), newline="") as f: return list(csv.DictReader(f))
r24, r25, r26 = rows(2024), rows(2025), rows(2026)
assert list(r24[0].keys()) == ["Ticker", "Date", "Open", "High", "Low", "Close", "Volume", "Delivery", "DeliveryPct"]
assert len(r24) == 9 and len(r25) == 9 and len(r26) == 6, (len(r24), len(r25), len(r26))              # 3 symbols x days; NSE and BAD are not there
assert {r["Ticker"] for r in r24} == {"AAA", "BBB", "^NIFTY50"}
def get(rs, t, d): return next(r for r in rs if r["Ticker"] == t and r["Date"] == d)
# hand calculation: 50 * 0.92 * 0.90 = 41.40 before both dividends, 50 * 0.90 = 45.00 between them, 50 after
assert get(r24, "AAA", "2024-01-02")["Close"] == "41.4000", get(r24, "AAA", "2024-01-02")
assert get(r24, "AAA", "2024-03-04")["Close"] == "45.0000" and get(r25, "AAA", "2025-08-29")["Close"] == "45.0000"
assert get(r25, "AAA", "2025-09-01")["Close"] == "50.0000" and get(r26, "AAA", "2026-10-23")["Close"] == "50.0000"
assert get(r24, "^NIFTY50", "2024-01-02")["Close"] == "20050.0000", "indices are never dividend adjusted"
assert get(r24, "BBB", "2024-01-02")["Close"] == "205.1235" and get(r24, "BBB", "2024-01-02")["Delivery"] == "", "4 decimals; unknown delivery stays empty"
assert get(r24, "AAA", "2024-01-02")["Delivery"] == "500" and get(r24, "AAA", "2024-01-02")["Volume"] == "1000"
assert float(get(r24, "AAA", "2024-01-02")["DeliveryPct"]) == 50.0 and get(r24, "BBB", "2024-01-02")["DeliveryPct"] == "" and get(r24, "^NIFTY50", "2024-01-02")["DeliveryPct"] == "", "delivery % = 500 / 1000 x 100; empty when delivery is unknown or volume is 0"
print("hand-calculated prices ok")

# ---- the SAME data as the files for R2: run convert.py's own conversion and compare every row
class Fake:
    def __init__(self): self.o = {}
    def put_object(self, Bucket, Key, Body, **k): self.o[Key] = bytes(Body)
    def upload_file(self, fn, Bucket, Key, **k): self.o[Key] = open(fn, "rb").read()
    def get_object(self, Bucket, Key): return {"Body": io.BytesIO(self.o[Key])}
    def list_objects_v2(self, Bucket, Prefix="", **k): return {"Contents": [{"Key": x} for x in self.o if x.startswith(Prefix)], "IsTruncated": False}
    def delete_objects(self, Bucket, Delete, **k): pass
import io
fake = Fake(); convert.s3_client = fake; convert.DONE.clear(); convert.FAILED.clear()
convert.ADJ = dividends.build_adjuster(os.environ["DB_DIR"])
convert.convert_and_upload(dbs)
def parquet(key): return pd.read_pickle(io.BytesIO(fake.o[key])) if STUB else pd.read_parquet(io.BytesIO(fake.o[key]))
bad = 0
for y in (2024, 2025, 2026):
    pq_df = parquet(f"nse/{y}.parquet").sort_values(["symbol", "date"]).reset_index(drop=True); cs = pd.DataFrame(rows(y))
    assert len(pq_df) == len(cs), (y, len(pq_df), len(cs))
    assert list(pq_df["symbol"]) == list(cs["Ticker"]) and list(pq_df["date"]) == list(cs["Date"])
    for a, b in (("open", "Open"), ("high", "High"), ("low", "Low"), ("close", "Close")):
        bad += int((abs(pq_df[a].astype(float) - cs[b].astype(float)) > 5.1e-5).sum())
    bad += int((pq_df["volume"].astype("int64") != cs["Volume"].astype("int64")).sum())
    d1 = pq_df["delivery"].round(0); d2 = pd.to_numeric(cs["Delivery"].replace("", numpy.nan))
    bad += int(((d1.isna() != d2.isna()) | ((d1 - d2).abs() > 0.5)).sum())
assert bad == 0, f"{bad} values differ between the R2 files and the CSV"
print("every row of the CSV equals the R2 file (prices within 0.00005)")

# ---- the package (--zip)
convert.s3_client = NoR2()
out_z = os.path.join(tmp, "export_zip"); assert export_csv.main(["--db", "nse", "--db-dir", os.environ["DB_DIR"], "--out", out_z, "--zip"]) == 0
z = zipfile.ZipFile(os.path.join(out_z, "nse-amibroker-csv.zip")); zn = z.namelist()
assert set(zn) == set(names), zn
fmt = z.read("nse_eod.format").decode()
assert fmt.splitlines()[0] == "$FORMAT Ticker, Date_YMD, Open, High, Low, Close, Volume, Aux1, Aux2" and "$SKIPLINES 1" in fmt and "$SEPARATOR ," in fmt, fmt
assert b"\r\n" in z.read("nse_2024.csv")
readme = z.read("IMPORT_README.txt").decode(); assert "csv-export" in readme and "Import ASCII" in readme and "do NOT let AmiBroker adjust" in readme, readme
rep = z.read("export-report.txt").decode(); assert "2024:" in rep and "cash dividends applied" in rep, rep
print("package ok:\n" + rep)

# ---- a year that is too big for one file is split in monthly files (threshold made tiny for the test)
out_m = os.path.join(tmp, "export_month")
assert export_csv.main(["--db", "nse", "--db-dir", os.environ["DB_DIR"], "--out", out_m, "--split-mb", "0.0002"]) == 0
mf = sorted(f for f in os.listdir(out_m) if f.endswith(".csv")); print(mf)
assert mf == ["nse_2024-01.csv", "nse_2024-03.csv", "nse_2024-12.csv", "nse_2025-01.csv", "nse_2025-08.csv", "nse_2025-09.csv", "nse_2026-01.csv", "nse_2026-10.csv"], mf
def mrows(f):
    with open(os.path.join(out_m, f), newline="") as fh: return list(csv.DictReader(fh))
assert all(list(mrows(f)[0].keys()) == export_csv.COLUMNS for f in mf), "every monthly file has the header (AmiBroker skips the first line of each)"
assert sum(len(mrows(f)) for f in mf if f.startswith("nse_2024")) == len(r24) and all(r["Date"].startswith("2024-03") for r in mrows("nse_2024-03.csv"))
joined = [r for f in mf if f.startswith("nse_2025") for r in mrows(f)]; assert sorted((r["Ticker"], r["Date"], r["Close"]) for r in joined) == sorted((r["Ticker"], r["Date"], r["Close"]) for r in r25), "months together = the year"
assert "[3 monthly files]" in open(os.path.join(out_m, "export-report.txt")).read()
assert not any(f in os.listdir(out_m) for f in ("nse_2024.csv", "nse_2025.csv")), "no yearly file left next to the monthly ones"
# year range, and no files for another database
out2 = os.path.join(tmp, "export2"); assert export_csv.main(["--db", "nse", "--db-dir", os.environ["DB_DIR"], "--out", out2, "--from-year", "2025", "--to-year", "2025"]) == 0
assert sorted(f for f in os.listdir(out2) if f.endswith(".csv")) == ["nse_2025.csv"], os.listdir(out2)
assert export_csv.main(["--db", "bse", "--db-dir", os.environ["DB_DIR"], "--out", os.path.join(tmp, "x")]) == 1, "no files for that database"
print("EXPORT TESTS PASSED")
