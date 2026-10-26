"""Index download / import checks with a fake NSE (no network).  Run: python tests/test_indices.py"""
import os, shutil, sqlite3, sys, tempfile, types
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
tmp = tempfile.mkdtemp(prefix="idx_")
os.environ["INDEX_NAMES_JSON"] = os.path.join(tmp, "names.json"); os.environ["INDEX_FILES_DIR"] = os.path.join(tmp, "files")
import pandas as pd, indices
FIX = os.path.join(ROOT, "tests", "fixtures", "ind_close_all_23102026.csv")
db = sqlite3.connect(os.path.join(tmp, "nse_2026.db"))
db.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, is_index INTEGER DEFAULT 0, delivery INTEGER, PRIMARY KEY (symbol, date))")
for d in ("2026-10-21", "2026-10-22", "2026-10-23"):
    for k in range(120):
        db.execute("INSERT INTO ohlcv VALUES (?,?,100,101,99,100,10,0,5)", (f"S{k}", d))
db.commit()
calls = []
def get(session, url):
    calls.append(url)
    ok = "23102026" in url
    return types.SimpleNamespace(status_code=200 if ok else 404, text=open(FIX, encoding="utf-8-sig").read() if ok else "Not found")
st = indices.fetch_missing_days(lambda y: db, [2026], None, get, delay=0, log=lambda *a: None, recent_days=0)
assert st["stored_days"] == 1 and st["rows"] == 167 and st["missing"] == 2, st
rows = db.execute("SELECT symbol, open, high, low, close, volume, is_index FROM ohlcv WHERE symbol='^NIFTY50'").fetchall()
assert len(rows) == 1 and rows[0][6] == 1 and rows[0][4] > 1000, rows
assert db.execute("SELECT COUNT(*) FROM ohlcv WHERE is_index=0").fetchone()[0] == 360, "stocks untouched"
assert db.execute("SELECT COUNT(*) FROM ohlcv WHERE close IS NULL OR close <= 0 OR open IS NULL OR open <= 0").fetchone()[0] == 0
n = len(calls)
st2 = indices.fetch_missing_days(lambda y: db, [2026], None, get, delay=0, log=lambda *a: None, recent_days=0)
assert st2["stored_days"] == 0 and st2["skipped_known_missing"] == 2 and len(calls) == n, (st2, len(calls) - n)
st3 = indices.fetch_missing_days(lambda y: db, [2026], None, get, delay=0, log=lambda *a: None, recent_days=0, recheck=True)
assert st3["missing"] == 2 and len(calls) > n, "INDEX_RECHECK asks again"
st4 = indices.fetch_missing_days(lambda y: db, [2026], None, get, delay=0, log=lambda *a: None, date_min="2026-10-23", recheck=True)
assert st4["stored_days"] == 0 and st4["missing"] == 0, "date window respected"
# manual file import (history you downloaded yourself)
os.makedirs(os.environ["INDEX_FILES_DIR"])
pd.DataFrame({"Date": ["02 Jan 2020", "03 Jan 2020"], "Open": [12000, 12100], "High": [12100, 12200], "Low": [11900, 12000], "Close": [12050, 12150]}) \
  .to_csv(os.path.join(os.environ["INDEX_FILES_DIR"], "Nifty 50_Historical_PR_01012020to31012020.csv"), index=False)
dbs = {}
def conn(y):
    if y not in dbs:
        c = sqlite3.connect(os.path.join(tmp, f"nse_{y}.db"))
        c.execute("CREATE TABLE IF NOT EXISTS ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, is_index INTEGER DEFAULT 0, delivery INTEGER, PRIMARY KEY (symbol, date))")
        dbs[y] = c
    return dbs[y]
got = indices.import_index_files(conn, log=print)
r = conn("2020").execute("SELECT symbol, date, close, is_index FROM ohlcv ORDER BY date").fetchall()
assert got == 2 and r[0][:2] == ("^NIFTY50", "2020-01-02") and r[0][3] == 1, (got, r)
assert os.path.exists(os.path.join(os.environ["INDEX_FILES_DIR"], "done", "Nifty 50_Historical_PR_01012020to31012020.csv"))
assert indices.import_index_files(conn, log=print) == 0
# a single manually loaded index must not hide the day from NSE's full daily file
c = sqlite3.connect(":memory:")
c.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, is_index INTEGER DEFAULT 0, delivery INTEGER, PRIMARY KEY (symbol, date))")
for k in range(120): c.execute("INSERT INTO ohlcv VALUES (?,?,1,1,1,1,1,0,1)", (f"S{k}", "2010-01-04"))
c.execute("INSERT INTO ohlcv VALUES ('^NIFTY50','2010-01-04',1,1,1,1,0,1,NULL)"); c.commit()
indices.ensure_tables(c); assert "2010-01-04" not in indices.days_with_indices(c)
print("ALL OK"); shutil.rmtree(tmp, ignore_errors=True)
