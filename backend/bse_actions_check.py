"""Reads a BSE corporate-actions csv, finds split / bonus / consolidation events, and checks each one against the prices in the .db files.

    python backend/bse_actions_check.py BSE.csv [db_dir] [out_dir]      (defaults: data/databases  data/review)

For every event the factor is worked out from the purpose text (Bonus 1:1 -> 2, Split Rs.2 -> Rs.1 -> 2, consolidation Rs.1 -> Rs.10 -> 0.1).
Events of one stock on one date are multiplied (bonus 1:1 + split 2->1 on the same day = 4).
The prices are then searched +-WINDOW days around the listed date for a one-day fall of about that factor (the exchanges sometimes list a different date than the real ex-date).
Status:
    already_adjusted   no jump of that size near the date                      -> nothing to do
    in_manual_splits   manual_splits.csv already has the stock near the date   -> nothing to do
    missing            the jump is in the prices                              -> line written to bse_missing_paste.csv (manual_splits.csv format, date of the real drop)
    ambiguous          a jump exists but its size does not match               -> look by hand (bse_actions_report.csv)
Nothing is applied automatically.
"""
import csv, datetime, glob, math, os, re, sqlite3, sys
from collections import defaultdict

WINDOW = int(os.getenv("BSE_WINDOW", "60"))
TOL = 0.12            # ratio match tolerance
NUM = r"(\d+(?:\.\d+)?)"

def parse_factor(p):
    p = re.sub(r"\s+", " ", p.strip())
    m = re.search(r"bonus issue " + NUM + r"\s*:\s*" + NUM, p, re.I)
    if m: return 1 + float(m[1]) / float(m[2]), "bonus"
    m = re.search(r"split from rs\.?\s*" + NUM + r"\S*\s*to rs\.?\s*" + NUM, p, re.I)
    if m and float(m[2]) > 0: return float(m[1]) / float(m[2]), "split"
    m = re.search(r"consolidation.*?rs\.?\s*" + NUM + r"\S*\s*to rs\.?\s*" + NUM, p, re.I)
    if m and float(m[1]) > 0: return float(m[1]) / float(m[2]), "consol"
    return None, None

def load_manual(path):
    d = defaultdict(list)
    if os.path.exists(path):
        for r in csv.reader(open(path, encoding="utf-8-sig")):
            if r and r[0].strip().lower() not in ("symbol",) and not r[0].startswith("#"):
                try: d[r[0].strip().upper()].append(datetime.date.fromisoformat(r[1].strip()))
                except Exception: pass
    return d

def closes(db_dir, sym, lo, hi):
    out = []
    for y in range(lo.year, hi.year + 1):
        p = os.path.join(db_dir, f"nse_{y}.db")
        if not os.path.exists(p): continue
        c = sqlite3.connect(p)
        out += c.execute("SELECT date, close FROM ohlcv WHERE UPPER(TRIM(symbol))=? AND date>=? AND date<=? AND close>0 ORDER BY date",
                         (sym, lo.isoformat(), hi.isoformat())).fetchall()
        c.close()
    return sorted(out)

def main():
    src = sys.argv[1]; db = sys.argv[2] if len(sys.argv) > 2 else "data/databases"; out = sys.argv[3] if len(sys.argv) > 3 else "data/review"
    os.makedirs(out, exist_ok=True)
    manual = load_manual(os.path.join("data", "corporate_actions", "manual_splits.csv"))
    ev = defaultdict(lambda: [1.0, []])
    for r in csv.DictReader(open(src, encoding="utf-8-sig")):
        r = {k.strip(): v for k, v in r.items() if k}
        f, kind = parse_factor(r.get("Purpose", ""))
        if not f or math.isclose(f, 1.0): continue
        try: d = datetime.datetime.strptime(r["Ex Date"].strip(), "%d-%b-%y").date()
        except Exception: continue
        k = (r["Security Name"].strip().upper(), d)
        if kind + str(f) in ev[k][1]: continue            # duplicate row
        ev[k][0] *= f; ev[k][1].append(kind + str(f))
    rep, paste = [], []
    for (sym, d), (f, kinds) in sorted(ev.items(), key=lambda x: (x[0][1], x[0][0])):
        lo, hi = d - datetime.timedelta(days=WINDOW), d + datetime.timedelta(days=WINDOW)
        rows = closes(db, sym, lo, hi)
        if len(rows) < 2: status, when, ratio = "no_prices", "", ""
        else:
            best = None
            for (d0, c0), (d1, c1) in zip(rows, rows[1:]):
                r_ = c0 / c1
                if best is None or abs(math.log(r_ / f)) < abs(math.log(best[0] / f)): best = (r_, d1)
            ratio, when = best
            near = any(abs((m - datetime.date.fromisoformat(when)).days) <= 7 or abs((m - d).days) <= 7 for m in manual.get(sym, []))
            j = [c for dd, c in rows if dd < when][-5:], [c for dd, c in rows if dd >= when][:5]
            level = (sorted(j[0])[len(j[0]) // 2] / sorted(j[1])[len(j[1]) // 2]) if j[0] and j[1] else ratio   # median of 5 closes before / after: a real split stays down
            if f < 1.5 and not near: status = "small_factor_check_by_hand"      # a 1.1-1.4 bonus looks like an ordinary move; cannot be told from the prices
            elif abs(math.log(ratio / f)) <= math.log(1 + TOL) and abs(math.log(level / f)) <= math.log(1 + 2 * TOL): status = "in_manual_splits" if near else "missing"
            elif abs(math.log(ratio)) < 0.25 * abs(math.log(f)): status = "already_adjusted"
            else: status = "ambiguous"
            if status == "missing": paste.append((sym, when, round(f, 6), f"bse {'+'.join(kinds)} listed {d}; jump {ratio:.2f} on {when}"))
        rep.append((sym, d.isoformat(), round(f, 6), "+".join(kinds), status, when, f"{ratio:.3f}" if ratio != "" else ""))
    with open(os.path.join(out, "bse_actions_report.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "listed_ex_date", "factor", "kinds", "status", "jump_date", "jump_ratio"]); w.writerows(rep)
    with open(os.path.join(out, "bse_missing_paste.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(["symbol", "ex_date", "factor", "note"]); w.writerows(paste)
    from collections import Counter
    print(Counter(r[4] for r in rep)); print(f"{len(paste)} missing -> {out}/bse_missing_paste.csv")

main()
