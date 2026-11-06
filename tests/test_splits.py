"""Offline test of backend/splits.py and backend/find_jumps.py (no R2, no network):  python tests/test_splits.py"""
import os, sqlite3, sys, tempfile, csv
import numpy as np
import pandas as pd
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))
import splits, find_jumps

tmp = tempfile.mkdtemp(); dbd = os.path.join(tmp, "db"); os.makedirs(dbd)
days = pd.bdate_range("2006-11-01", "2008-02-29")
def px(sym, start, drift=0.0003):
    rng = np.random.default_rng(abs(hash(sym)) % 1000)
    return np.round(start * np.exp(np.cumsum(rng.normal(drift, 0.01, len(days)))), 2)
data = {"UNADJ": px("UNADJ", 500), "ADJ": px("ADJ", 100), "BONUS": px("BONUS", 300), "PLAIN": px("PLAIN", 50)}
ex = pd.Timestamp("2007-06-28")
unadj_idx = days < ex
data["UNADJ"] = np.where(unadj_idx, data["UNADJ"] * 5, data["UNADJ"])               # 5-for-1 split that is still in the data
data["BONUS"] = np.where(days < pd.Timestamp("2007-09-19"), data["BONUS"] * 1.5, data["BONUS"])   # 1:2 bonus still in the data
rows = {2006: [], 2007: [], 2008: []}
for s, c in data.items():
    for d, v in zip(days, c):
        rows[d.year].append((s, d.strftime("%Y-%m-%d"), float(v), float(v) * 1.01, float(v) * 0.99, float(v), 1000, 400.0))
rows[2007].append(("^NIFTY50", "2007-06-28", 1.0, 1.0, 1.0, 1.0, 0, None))
for y, r in rows.items():
    c = sqlite3.connect(os.path.join(dbd, f"nse_{y}.db"))
    c.execute("CREATE TABLE ohlcv (symbol TEXT, date TEXT, open REAL, high REAL, low REAL, close REAL, volume INTEGER, delivery REAL)")
    c.executemany("INSERT INTO ohlcv VALUES (?,?,?,?,?,?,?,?)", r); c.commit(); c.close()
f = os.path.join(tmp, "manual_splits.csv")
open(f, "w").write("symbol,ex_date,factor,note\nUNADJ,2007-06-28,5,real\nADJ,2007-06-28,5,already adjusted in the db\nBONUS,2007-09-19,1.5,bonus\nGHOST,2007-01-05,2,no data\nBAD,not-a-date,2,x\n")
ok = fail = 0
def check(c, m):
    global ok, fail
    if c: ok += 1
    else: fail += 1; print("FAIL", m)

a = splits.build_adjuster(dbd, f)
check(sorted(e[0] for e in a.events) == ["BONUS", "GHOST", "UNADJ"], f"events kept: {[e[0] for e in a.events]} (ADJ must be skipped as already adjusted, BAD as unreadable)")
df = pd.concat([pd.read_sql_query("SELECT symbol,date,open,high,low,close,volume,delivery FROM ohlcv", sqlite3.connect(os.path.join(dbd, f"nse_{y}.db"))) for y in (2006, 2007, 2008)])
df = df[~df.symbol.str.startswith("^")].sort_values(["symbol", "date"]).reset_index(drop=True)
out = a.adjust(df)
u = out[out.symbol == "UNADJ"].set_index("date"); u0 = df[df.symbol == "UNADJ"].set_index("date")
check(abs(u.loc["2007-06-27", "close"] - u0.loc["2007-06-27", "close"] / 5) < 1e-9, "before ex-date divided by 5")
check(u.loc["2007-06-28", "close"] == u0.loc["2007-06-28", "close"], "ex-date row untouched")
check(u.loc["2007-06-27", "volume"] == 5000 and str(out.volume.dtype) == "int64", f"volume x5 and still integer: {u.loc['2007-06-27','volume']} {out.volume.dtype}")
check(abs(u.loc["2007-06-27", "delivery"] - 2000.0) < 1e-9, "delivery x5")
r = u["close"].pct_change().abs(); check(r.max() < 0.06, f"no fake jump left in UNADJ (max daily move {r.max():.3f})")
b = out[out.symbol == "BONUS"]["close"].pct_change().abs(); check(b.max() < 0.06, "bonus 1.5 removed")
check((out[out.symbol == "ADJ"].close.values == df[df.symbol == "ADJ"].close.values).all(), "already adjusted stock untouched")
check((out[out.symbol == "PLAIN"].close.values == df[df.symbol == "PLAIN"].close.values).all(), "other stocks untouched")
# a slice with only one year gives the same result for the rows it holds
y07 = df[df.date.str.startswith("2007")]; o07 = a.adjust(y07).set_index(["symbol", "date"])["close"]
check(np.allclose(o07.loc[("UNADJ", "2007-06-27")], u.loc["2007-06-27", "close"]), "yearly slice = full history result")
# two events on one stock multiply
a2 = splits.SplitAdjuster([("X", "2007-01-10", 2, ""), ("X", "2008-01-10", 5, "")])
t = pd.DataFrame({"symbol": ["X"] * 3, "date": ["2006-12-01", "2007-06-01", "2008-06-01"], "open": [1000.0] * 3, "high": [1000.0] * 3, "low": [1000.0] * 3, "close": [1000.0] * 3, "volume": [1, 1, 1]})
check(list(a2.adjust(t).close) == [100.0, 200.0, 1000.0], f"two events compound: {list(a2.adjust(t).close)}")
# chain order and signature
class Div:
    signature = "d1"
    def adjust(self, d): d = d.copy(); d["close"] = d["close"] * 0.5; return d
ch = splits.chain(Div(), a2); check(list(ch.adjust(t).close) == [50.0, 100.0, 500.0], "chain applies both")
check(splits.chain(None, splits.SplitAdjuster([])).signature == "", "empty chain has an empty signature")
check(splits.chain(Div(), a2).signature != splits.chain(Div(), splits.SplitAdjuster([])).signature, "signature changes with the events")
# jump finder
cand = find_jumps.find(find_jumps.load(dbd), splits.read_events(f))
got = {(r.symbol, r.date): r for r in cand.itertuples()}
check(("UNADJ", "2007-06-28") in got and got[("UNADJ", "2007-06-28")].likely_factor == 5, f"UNADJ found with factor 5: {list(got)}")
check(got[("UNADJ", "2007-06-28")].already_listed == "yes", "marked as already listed")
check(("BONUS", "2007-09-19") in got and got[("BONUS", "2007-09-19")].likely_factor in (1.5, 2) or ("BONUS", "2007-09-19") not in got, "bonus 1.5 is under the threshold and may not be listed")
check(not any(s.startswith("^") for s, d in got), "indices ignored")
print(f"{ok} passed, {fail} failed"); sys.exit(1 if fail else 0)
