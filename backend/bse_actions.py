"""BSE corporate-action files -> split / bonus events that are really missing from the prices.

Any BSE export dropped into data/corporate_actions/ is read automatically (no file name needed): a csv is taken as a BSE file when its header has a
symbol column (Security Name / Symbol / Scrip ...), a Purpose column and an Ex Date column, and it is not one of the NSE files (CF-CA-*, bonus, split,
demerger, rights) or a generated file (dividends, manual_splits, bse_*). Columns are matched loosely (case, spaces, tabs, order and extra columns do not matter).

What is used: only split, bonus and consolidation lines (dividends, AGMs ... are ignored). The factor comes from the Purpose text:
    Bonus issue 1:1 -> 2      Stock Split From Rs.2/- to Rs.1/- -> 2      consolidation Rs.1 to Rs.10 -> 0.1      several lines of one stock/day multiply.
BSE dates are often not the real ex-date, and many events are already adjusted from the NSE files, so nothing is taken on trust. Each event is
searched +-WINDOW days in the .db prices for a one-day fall of about that factor:
    missing            the fall is still in the prices            -> used, dated on the day of the real fall (same as a manual_splits.csv line)
    already_adjusted   no fall                                      -> ignored
    in_manual_splits   manual_splits.csv already has the stock near -> ignored
    ambiguous / small_factor_check_by_hand / no_prices              -> ignored, only listed in the report (look by hand)
So the same event can never be applied twice, and a bad BSE line cannot damage a stock whose prices look right.

    python backend/bse_actions.py [db_dir] [out_dir]   writes bse_actions_report.csv and bse_missing_paste.csv  (defaults: data/databases data/review)
"""
import csv, datetime, glob, math, os, re, sqlite3, sys
from collections import defaultdict

CA_DIR = os.getenv("CA_DIR", "data/corporate_actions")
WINDOW = int(os.getenv("BSE_WINDOW", "60"))
TOL = 0.12
NUM = r"(\d+(?:\.\d+)?)"
NSE_NAMES = re.compile(r"^(CF-CA-.*|bonus|split|demerger|rights|dividends|manual_splits|bse_.*|dividend-report)\.csv$", re.I)
SYM_KEYS = ("securityname", "symbol", "scripname", "scripid", "securityid", "nsesymbol")
DATE_FORMATS = ("%d-%b-%y", "%d-%b-%Y", "%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d-%B-%Y")


def _key(c):
    return re.sub(r"[^a-z0-9]", "", str(c).lower())


def parse_factor(p):
    p = re.sub(r"\s+", " ", str(p).strip())
    m = re.search(r"bonus(?: issue)? " + NUM + r"\s*:\s*" + NUM, p, re.I)
    if m and float(m[2]) > 0: return 1 + float(m[1]) / float(m[2]), "bonus"
    m = re.search(r"split from rs\.?\s*" + NUM + r"\S*\s*to rs\.?\s*" + NUM, p, re.I)
    if m and float(m[2]) > 0: return float(m[1]) / float(m[2]), "split"
    m = re.search(r"consolidation.*?rs\.?\s*" + NUM + r"\S*\s*to rs\.?\s*" + NUM, p, re.I)
    if m and float(m[1]) > 0 and float(m[2]) > 0: return float(m[1]) / float(m[2]), "consol"
    return None, None


def _date(s):
    s = str(s).strip()
    for f in DATE_FORMATS:
        try: return datetime.datetime.strptime(s, f).date()
        except ValueError: pass
    return None


def bse_files(ca_dir=None):
    out = []
    for p in sorted(glob.glob(os.path.join(ca_dir or CA_DIR, "*.csv"))):
        if NSE_NAMES.match(os.path.basename(p)): continue
        try:
            with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
                head = [_key(c) for c in next(csv.reader(f), [])]
        except Exception:
            continue
        if any(k in head for k in SYM_KEYS) and "purpose" in head and "exdate" in head:
            out.append(p)
    return out


def read_listed(files):
    """{(SYMBOL, listed_date): [factor, [kind+factor ...]]} from the BSE files."""
    ev = defaultdict(lambda: [1.0, []])
    for p in files:
        with open(p, newline="", encoding="utf-8-sig", errors="replace") as f:
            rd = csv.reader(f)
            head = [_key(c) for c in next(rd, [])]
            si = next(head.index(k) for k in SYM_KEYS if k in head)
            pi, di = head.index("purpose"), head.index("exdate")
            for r in rd:
                if len(r) <= max(si, pi, di): continue
                fac, kind = parse_factor(r[pi])
                d = _date(r[di])
                if not fac or math.isclose(fac, 1.0) or not d: continue
                k = (r[si].strip().upper(), d)
                tag = kind + str(round(fac, 6))
                if tag in ev[k][1]: continue                    # same line repeated (or the same event in two files)
                ev[k][0] *= fac; ev[k][1].append(tag)
    return ev


def _manual(path):
    d = defaultdict(list)
    if os.path.exists(path):
        for r in csv.reader(open(path, encoding="utf-8-sig")):
            if r and r[0].strip().lower() != "symbol" and not r[0].startswith("#"):
                try: d[r[0].strip().upper()].append(datetime.date.fromisoformat(r[1].strip()))
                except Exception: pass
    return d


def _load(db_dir, ev):
    """{SYMBOL: [(date, close)]} for every symbol of the events, each year's database read once, only the years/dates the events need."""
    want = defaultdict(lambda: [datetime.date.max, datetime.date.min])
    for (sym, d) in ev:
        w = want[sym]; w[0] = min(w[0], d - datetime.timedelta(days=WINDOW)); w[1] = max(w[1], d + datetime.timedelta(days=WINDOW))
    years = sorted({y for lo, hi in want.values() for y in range(lo.year, hi.year + 1)})
    out = defaultdict(list)
    syms = sorted(want)
    for y in years:
        p = os.path.join(db_dir, f"nse_{y}.db")
        if not os.path.exists(p): continue
        c = sqlite3.connect(p)
        try:
            for i in range(0, len(syms), 500):
                part = syms[i:i + 500]
                q = "SELECT UPPER(TRIM(symbol)), date, close FROM ohlcv WHERE close>0 AND is_index=0 AND UPPER(TRIM(symbol)) IN (%s)" % ",".join("?" * len(part))
                for sy, dt, cl in c.execute(q, part): out[sy].append((dt, cl))
        finally:
            c.close()
    for v in out.values(): v.sort()
    return out


def analyse(db_dir, files=None, manual_path=None):
    """(report_rows, missing_events). missing_events = [(SYMBOL, 'YYYY-MM-DD', factor, note)]"""
    files = bse_files() if files is None else files
    manual = _manual(manual_path or os.path.join(CA_DIR, "manual_splits.csv"))
    rep, paste = [], []
    ev = read_listed(files)
    prices = _load(db_dir, ev)
    for (sym, d), (f, kinds) in sorted(ev.items(), key=lambda x: (x[0][1], x[0][0])):
        lo, hi = (d - datetime.timedelta(days=WINDOW)).isoformat(), (d + datetime.timedelta(days=WINDOW)).isoformat()
        rows = [x for x in prices.get(sym, []) if lo <= x[0] <= hi]
        status, when, ratio = "no_prices", "", ""
        if len(rows) >= 2:
            best = None
            for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
                r_ = c0 / c1
                if best is None or abs(math.log(r_ / f)) < abs(math.log(best[0] / f)): best = (r_, d1)
            ratio, when = best
            wd = datetime.date.fromisoformat(when)
            near = any(abs((m - wd).days) <= 7 or abs((m - d).days) <= 7 for m in manual.get(sym, []))
            a = [c for dd, c in rows if dd < when][-5:]; b = [c for dd, c in rows if dd >= when][:5]
            level = (sorted(a)[len(a) // 2] / sorted(b)[len(b) // 2]) if a and b else ratio     # a real split stays down for days
            if f < 1.5 and not near: status = "small_factor_check_by_hand"
            elif abs(math.log(ratio / f)) <= math.log(1 + TOL) and abs(math.log(level / f)) <= math.log(1 + 2 * TOL): status = "in_manual_splits" if near else "missing"
            elif abs(math.log(ratio)) < 0.25 * abs(math.log(f)): status = "already_adjusted"
            else: status = "ambiguous"
            if status == "missing": paste.append((sym, when, round(f, 6), f"bse {'+'.join(kinds)} listed {d}; jump {ratio:.2f} on {when}"))
        rep.append((sym, d.isoformat(), round(f, 6), "+".join(kinds), status, when, f"{ratio:.3f}" if ratio != "" else ""))
    return rep, paste


_cache = {}
def events(db_dir):
    """Missing split/bonus events found in the BSE files, for splits.build_adjuster. Empty when there is no BSE file or no database."""
    files = bse_files()
    if not files or not os.path.isdir(db_dir): return []
    key = (os.path.abspath(db_dir), tuple((p, os.path.getmtime(p)) for p in files))
    if key not in _cache:
        rep, paste = analyse(db_dir, files)
        from collections import Counter
        print(f"[BSE] {len(files)} BSE file(s), {len(rep)} split/bonus events: {dict(Counter(r[4] for r in rep))}")
        for r in rep:
            if r[4] == "ambiguous": print(f"[BSE] ambiguous (not applied, check by hand): {r[0]} listed {r[1]} x{r[2]:g} jump {r[6]} on {r[5]}")
        _cache[key] = paste
    return _cache[key]


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else "data/databases"
    out = sys.argv[2] if len(sys.argv) > 2 else "data/review"
    os.makedirs(out, exist_ok=True)
    rep, paste = analyse(db)
    with open(os.path.join(out, "bse_actions_report.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "listed_ex_date", "factor", "kinds", "status", "jump_date", "jump_ratio"]); w.writerows(rep)
    with open(os.path.join(out, "bse_missing_paste.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "ex_date", "factor", "note"]); w.writerows(paste)
    from collections import Counter
    print(Counter(r[4] for r in rep)); print(f"{len(paste)} missing -> {out}/bse_missing_paste.csv")


if __name__ == "__main__":
    main()
