"""
Refreshes data/symbol_changes.csv from NSE (best effort - never stops the daily job).

NSE publishes the list as symbolchange.csv. If the download fails (NSE blocks cloud machines now and
then, or the address changed) the file already in the repo is kept; you can also replace it by hand:
download the file from NSE and upload it as data/symbol_changes.csv. Nothing is ever removed from it:
new rows are merged in.
"""
import csv
import os
import sys
import tempfile

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import symbol_map as sm

URL = os.getenv("SYMBOL_CHANGE_URL", "https://nsearchives.nseindia.com/content/equities/symbolchange.csv")
PATH = sm.DEFAULT_PATH
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
           "Accept": "text/csv,*/*", "Referer": "https://www.nseindia.com/"}


def main():
    have = {(o, n, d): name for o, n, d, name in sm.read_rows(PATH)}
    try:
        s = requests.Session()
        s.headers.update(HEADERS)
        try:
            s.get("https://www.nseindia.com", timeout=10)
        except Exception:
            pass
        r = s.get(URL, timeout=30)
        if r.status_code != 200:
            print(f"[SYMBOLS] Could not download {URL} (HTTP {r.status_code}); keeping {PATH} ({len(have)} renames).")
            return 0
        with tempfile.NamedTemporaryFile("wb", suffix=".csv", delete=False) as tmp:
            tmp.write(r.content)
        fresh = {(o, n, d): name for o, n, d, name in sm.read_rows(tmp.name)}
        os.remove(tmp.name)
    except Exception as e:
        print(f"[SYMBOLS] Download failed ({type(e).__name__}: {e}); keeping {PATH} ({len(have)} renames).")
        return 0

    if len(fresh) < 300:      # a real file has ~1,000 rows: anything much smaller is an error page or a changed layout
        print(f"[SYMBOLS] Downloaded file looks wrong ({len(fresh)} usable rows); keeping {PATH}.")
        return 0
    new_rows = sorted(k for k in fresh if k not in have)
    if not new_rows:
        print(f"[SYMBOLS] NSE list checked: no new renames ({len(have)} known).")
        return 0
    merged = dict(have)
    merged.update({k: fresh[k] for k in new_rows})
    os.makedirs(os.path.dirname(PATH) or ".", exist_ok=True)
    with open(PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["NAME OF THE COMPANY", "OLD SYMBOL", "NEW SYMBOL", "EFFECTIVE DATE"])
        for (o, n, d), name in sorted(merged.items(), key=lambda kv: (kv[0][2], kv[0][0])):
            w.writerow([name, o, n, d])
    print(f"[SYMBOLS] {len(new_rows)} NEW renames added to {PATH}:")
    for o, n, d in new_rows[:40]:
        print(f"      {d}  {o} -> {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
