"""Checks candidate splits / bonuses against Yahoo Finance's split history, one request per stock.

    python backend/verify_splits.py [in_csv] [out_dir]        (defaults: data/review/to_verify.csv  data/review)

in_csv has the columns  symbol,ex_date,factor[,note]   (the same format as manual_splits.csv; paste files 2 and 3 into it).
For every line Yahoo's events for SYMBOL.NS are looked at, within +-7 days of ex_date:
    confirmed       Yahoo has a split/bonus there and its ratio matches the factor (within 12 %)      -> safe to add
    ratio_differs   Yahoo has an event there with another ratio                                         -> added with Yahoo's ratio, look at it once
    not_on_yahoo    Yahoo knows the stock but lists no event there (older events are often missing)    -> check by hand or leave out
    no_symbol       Yahoo has no data for that symbol                                                   -> check by hand
Writes   verified_apply.csv   (confirmed + ratio_differs: the date of the drop in your data and Yahoo's exact factor, manual_splits.csv format)
         verify_report.csv    (every line with its status and what Yahoo said)
Yahoo is unofficial and sometimes misses old or odd events, so 'not_on_yahoo' does NOT mean the event is false.
Yahoo quotes a split as numerator:denominator = new shares : old shares (5:1 -> factor 5, bonus 1:2 -> 3:2 -> factor 1.5).
"""
import csv
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

WINDOW_DAYS = 7
TOLERANCE = 0.12                      # |ln(yahoo factor / candidate factor)|
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; split-check)"}


def fetch_splits(symbol, retries=3):
    """[(date_iso, factor, 'n:d')] from Yahoo for SYMBOL.NS, or None when the symbol is unknown."""
    url = ("https://query1.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(symbol + ".NS", safe="") +
           "?period1=946684800&period2=" + str(int(time.time()) + 86400) + "&interval=1mo&events=split")
    for k in range(retries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as r:
                j = json.load(r)
            res = (j.get("chart") or {}).get("result")
            if not res:
                return None
            ev = (res[0].get("events") or {}).get("splits") or {}
            out = []
            for v in ev.values():
                n, d = float(v["numerator"]), float(v["denominator"])
                out.append((datetime.fromtimestamp(int(v["date"]), timezone.utc).date().isoformat(), n / d, f"{n:g}:{d:g}"))
            return sorted(out)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            time.sleep(2 * (k + 1))
        except Exception:
            time.sleep(2 * (k + 1))
    return None


def classify(ex_date, factor, yahoo):
    """(status, yahoo_date, yahoo_factor, yahoo_ratio) for one candidate."""
    if yahoo is None:
        return "no_symbol", "", "", ""
    d0 = date.fromisoformat(ex_date)
    near = [(abs((date.fromisoformat(d) - d0).days), d, f, t) for d, f, t in yahoo if abs((date.fromisoformat(d) - d0).days) <= WINDOW_DAYS]
    if not near:
        return "not_on_yahoo", "", "", ""
    near.sort()
    for gap, d, f, t in near:
        if abs(math.log(f / factor)) <= TOLERANCE:
            return "confirmed", d, f, t
    gap, d, f, t = near[0]
    return "ratio_differs", d, f, t


def run(in_csv, out_dir, fetch=fetch_splits, pause=0.4):
    with open(in_csv, newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if r.get("symbol")]
    cache, report, apply = {}, [], []
    for i, r in enumerate(rows, 1):
        sym = r["symbol"].strip().upper()
        if sym not in cache:
            cache[sym] = fetch(sym)
            if fetch is fetch_splits:
                time.sleep(pause)
        st, yd, yf, yt = classify(r["ex_date"].strip(), float(r["factor"]), cache[sym])
        report.append({"symbol": sym, "ex_date": r["ex_date"], "factor": r["factor"], "status": st, "yahoo_date": yd, "yahoo_ratio": yt, "note": r.get("note", "")})
        if st in ("confirmed", "ratio_differs"):
            apply.append([sym, r["ex_date"].strip(), f"{yf:.6g}", f"yahoo {yt} on {yd} ({st}); candidate x{r['factor']}"])   # the date of the drop in OUR data, Yahoo's exact factor
        if i % 50 == 0:
            print(f"{i}/{len(rows)} lines, {len(cache)} stocks looked up")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "verify_report.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(report[0].keys()) if report else ["symbol"]); w.writeheader(); w.writerows(report)
    with open(os.path.join(out_dir, "verified_apply.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "ex_date", "factor", "note"]); w.writerows(sorted(apply, key=lambda x: (x[1], x[0])))
    counts = {}
    for r in report:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("result:", counts)
    return report, apply


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else os.path.join("data", "review", "to_verify.csv"),
        sys.argv[2] if len(sys.argv) > 2 else os.path.join("data", "review"))
