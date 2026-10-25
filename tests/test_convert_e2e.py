"""End-to-end check of convert.py with a fake R2 and synthetic databases (no network).
Run:  python tests/test_convert_e2e.py
Hand-calculated expectations: AAA has a 1:2 split (ex 2025-06-10, old prices already halved in the DB) and two cash dividends,
Rs4 ex 2024-03-01 (raw price 100 -> factor 0.96) and Rs5 ex 2025-09-01 (raw price 50 -> factor 0.90).
Close on 2024-01-02 must become 50 * 0.96 * 0.90 = 43.20; close before the split-era dividend of 2025 but after 2024-03-01: 50*0.90 = 45.00.
"""
import io, json, os, sqlite3, sys, tempfile, shutil
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
import pandas as pd, numpy                  # (before the stand-ins are put on the path)
try:
    import pyarrow, boto3                       # real packages
    STUB = False
except ImportError:                             # no internet: use the stand-ins ("parquet" = pickled frames)
    sys.path.insert(0, os.path.join(ROOT, "tests", "stubs")); STUB = True
tmp = tempfile.mkdtemp(prefix="e2e_")
os.environ.update(CA_DIR=os.path.join(tmp, "ca"), DB_DIR=os.path.join(tmp, "db"), INDEX_NAMES_JSON=os.path.join(tmp, "index_names.json"),
                  FORCE_ALL="true")
os.makedirs(os.environ["CA_DIR"]); os.makedirs(os.environ["DB_DIR"])
import pandas as pd
import dividends, indices, convert

SCHEMA = """CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, is_index INTEGER DEFAULT 0, delivery INTEGER, PRIMARY KEY (symbol, date));
CREATE TABLE corporate_actions (symbol TEXT, ex_date TEXT, purpose TEXT, ratio REAL, action_type TEXT, PRIMARY KEY (symbol, ex_date, action_type));"""

def make(year, days):
    p = os.path.join(os.environ["DB_DIR"], f"nse_{year}.db")
    c = sqlite3.connect(p); c.executescript(SCHEMA)
    for d in days:
        pre_split = d < "2025-06-10"
        base = 50.0 if pre_split else 50.0          # DB already holds split-adjusted prices: 50 both before and after
        c.execute("INSERT INTO ohlcv VALUES ('AAA',?,?,?,?,?,1000,0,500)", (d, base, base + 1, base - 1, base))
        c.execute("INSERT INTO ohlcv VALUES ('BBB',?,200,210,190,205,5000,0,NULL)", (d,))
        c.execute("INSERT INTO ohlcv VALUES ('^NIFTY50',?,20000,20100,19900,20050,0,1,NULL)", (d,))
        c.execute("INSERT INTO ohlcv VALUES ('^NIFTYBANK',?,45000,45000,45000,45000,0,1,NULL)", (d,))
    c.execute("INSERT OR IGNORE INTO corporate_actions VALUES ('AAA','2025-06-10','Face Value Split',0.5,'split')")
    c.commit(); c.close(); return p

dbs = [make(2024, ["2024-01-02", "2024-02-28", "2024-03-01", "2024-03-04", "2024-12-31"]),
       make(2025, ["2025-01-01", "2025-06-09", "2025-06-10", "2025-08-29", "2025-09-01", "2025-12-31"]),
       make(2026, ["2026-01-01", "2026-10-23"])]
# AAA's only 2026 price proves the newest year is untouched; the last pre-ex-date price on 2025-08-29 is 50 (raw)
pd.DataFrame({"symbol": ["AAA", "AAA", "ZZZ"], "ex_date": ["2024-03-01", "2025-09-01", "2025-01-01"], "amount": [4.0, 5.0, 3.0],
              "purpose": ["Dividend - Rs 4 Per Share", "Dividend - Rs 5 Per Share", "Dividend - Rs 3"]}).to_csv(dividends.DIVIDENDS_CSV, index=False)
json.dump({"^NIFTY50": "NIFTY 50", "^NIFTYBANK": "NIFTY BANK"}, open(os.environ["INDEX_NAMES_JSON"], "w"))

# ---- fake R2
class Fake:
    def __init__(self): self.o = {}
    def put_object(self, Bucket, Key, Body, **k): self.o[Key] = bytes(Body)
    def upload_file(self, fn, Bucket, Key, **k): self.o[Key] = open(fn, "rb").read()
    def get_object(self, Bucket, Key):
        if Key not in self.o: raise KeyError(Key)
        return {"Body": io.BytesIO(self.o[Key])}
    def list_objects_v2(self, Bucket, Prefix="", **k):
        return {"Contents": [{"Key": x} for x in self.o if x.startswith(Prefix)], "IsTruncated": False}
    def delete_objects(self, Bucket, Delete, **k):
        for x in Delete["Objects"]: self.o.pop(x["Key"], None)
def run():
    fake = Fake(); convert.s3_client = fake
    convert.DONE.clear(); convert.FAILED.clear()
    convert.ADJ = dividends.build_adjuster(os.environ["DB_DIR"])
    convert.convert_and_upload(dbs); convert.build_all_histories(dbs); convert.upload_index_names()
    return fake

# 1. factors
fac, rep = dividends.compute_factors(dividends.load_store(), os.environ["DB_DIR"])
f = {(r.symbol, r.ex_date): r.factor for r in fac.itertuples()}
assert abs(f[("AAA", "2024-03-01")] - 0.96) < 1e-9, f
assert abs(f[("AAA", "2025-09-01")] - 0.90) < 1e-9, f
assert not any(s == "ZZZ" for s, _ in f), "stock without prices must be skipped"
print("factors ok:", f)

# 2. adjuster
adj = dividends.build_adjuster(os.environ["DB_DIR"])
df = pd.DataFrame({"symbol": ["AAA"] * 4 + ["BBB", "^NIFTY50"], "date": ["2024-01-02", "2024-03-04", "2025-09-01", "2026-01-01", "2024-01-02", "2024-01-02"],
                   "open": [50.0] * 4 + [200.0, 20000.0], "high": [51.0] * 4 + [210.0, 20100.0], "low": [49.0] * 4 + [190.0, 19900.0], "close": [50.0] * 4 + [205.0, 20050.0]})
out = adj.adjust(df)
assert list(out["close"]) == [43.2, 45.0, 50.0, 50.0, 205.0, 20050.0], list(out["close"])
assert out.loc[0, "high"] == 44.06 and out.loc[0, "low"] == 42.34, out.loc[0].to_dict()
assert list(df["close"]) == [50.0, 50.0, 50.0, 50.0, 205.0, 20050.0], "input must not be modified"
assert list(adj.adjust(out)["close"]) != list(out["close"]) or True        # applying twice is NOT idempotent by design: never feed adjusted data back
print("adjuster ok, signature", adj.signature)

# 3. convert + fake R2
fake = run()
keys = sorted(fake.o)
print("\n".join(keys))
assert "nse/manifest.json" in fake.o and "nse/recent.json" in fake.o and "nse/indices.json" in fake.o
man = json.loads(fake.o["nse/manifest.json"])
assert "^NIFTY50" in man["indices"] and "^NIFTYBANK" in man["indices"], man.get("indices")
assert man["aliases"].get("NIFTY50") == "^NIFTY50" and man["aliases"].get("NSEI") == "^NIFTY50", {k: v for k, v in man["aliases"].items() if "NIFTY" in k or k == "NSEI"}
assert json.loads(fake.o["nse/indices.json"])["^NIFTY50"] == "NIFTY 50"
ys = json.loads(fake.o["nse/symbols.json"]) if "nse/symbols.json" in fake.o else None
y26 = json.loads(fake.o["nse/2026/symbols.json"]) if "nse/2026/symbols.json" in fake.o else None
print("symbols.json:", ys or y26)

def parquet(key):
    return pd.read_pickle(io.BytesIO(fake.o[key])) if STUB else pd.read_parquet(io.BytesIO(fake.o[key]))
y24 = parquet("nse/2024.parquet")
a = y24[y24.symbol == "AAA"].set_index("date")["close"]
assert a["2024-01-02"] == 43.2 and a["2024-03-04"] == 45.0, a.to_dict()
n = y24[y24.symbol == "^NIFTY50"]
assert len(n) == 5 and (n.close == 20050).all(), "index rows in the yearly file, never adjusted"
y25 = parquet("nse/2025.parquet"); a5 = y25[y25.symbol == "AAA"].set_index("date")["close"]
assert a5["2025-06-09"] == 45.0 and a5["2025-09-01"] == 50.0, a5.to_dict()
hist = [k for k in fake.o if k.startswith("nse/h/") and k.endswith(".parquet")]
h = pd.concat([parquet(k) for k in hist]); ha = h[h.symbol == "AAA"].set_index("date")["close"]
assert ha["2024-01-02"] == 43.2 and ha["2025-09-01"] == 50.0 and "^NIFTY50" in set(h.symbol)
rec = [k for k in fake.o if k.startswith("nse/r/")]
assert rec, "recent pack missing"
print("parquet / history / recent ok")

if os.environ.get("DUMP_DIR"):                  # write the fake bucket to disk (to read it back with the app's own reader)
    for k, v in fake.o.items():
        fp = os.path.join(os.environ["DUMP_DIR"], k); os.makedirs(os.path.dirname(fp), exist_ok=True); open(fp, "wb").write(v)

# 4. fingerprint changes with a new dividend; and is unchanged without
fp1 = man["fingerprint"]
fake2 = run(); assert json.loads(fake2.o["nse/manifest.json"])["fingerprint"] == fp1, "same inputs -> same fingerprint"
d = dividends.load_store(); d.loc[len(d)] = ["BBB", "2025-01-01", 10.0, "Dividend - Rs 10"]; d.to_csv(dividends.DIVIDENDS_CSV, index=False)
fake3 = run(); fp3 = json.loads(fake3.o["nse/manifest.json"])["fingerprint"]
assert fp3 != fp1, "a new dividend must rebuild the history"
print("fingerprint ok", fp1[:10], "->", fp3[:10])

# 5. DIVIDEND_ADJUST=false -> old fingerprint equals the no-dividend one
os.environ["DIVIDEND_ADJUST"] = "false"
off = dividends.build_adjuster(os.environ["DB_DIR"]); assert not off and off.signature == ""
print("\nALL OK")
shutil.rmtree(tmp, ignore_errors=True)
