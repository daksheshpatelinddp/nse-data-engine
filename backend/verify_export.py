"""
Safety check run by the "Publish all" workflow after the R2 upload and the CSV export, in the SAME job on the SAME checkout.
It re-reads every yearly .db with convert.py's own reader + the same adjuster (dividends, then manual splits), and compares the totals per year
(rows, symbols, last date, sum of closes) with what is in the CSV files that AmiBroker will import. Any difference = the job fails.
It also writes DATA_STAMP.txt (commit, last data date, split-list signature, per-year totals) so you can see, on both sides, that it is one build.
usage: python backend/verify_export.py --csv-dir DIR [--db nse] [--db-dir data/databases]
"""
import argparse, glob, os, re, subprocess, sys
import pandas as pd
import dividends, splits, convert, export_csv


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="nse"); ap.add_argument("--csv-dir", required=True)
    ap.add_argument("--db-dir", default=os.getenv("DB_DIR", "data/databases"))
    a = ap.parse_args(argv)
    convert.ADJ = splits.chain(dividends.build_adjuster(a.db_dir), splits.build_adjuster(a.db_dir))
    csv_files = sorted(glob.glob(os.path.join(a.csv_dir, f"{a.db}_*.csv")))
    parts = []
    for f in csv_files:
        d = pd.read_csv(f, usecols=["Ticker", "Date", "Close"], dtype={"Ticker": str, "Date": str})
        parts.append(d)
    csv = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["Ticker", "Date", "Close"])
    csv["year"] = csv["Date"].str[:4]
    lines, bad, last = [], 0, ""
    for p in sorted(glob.glob(os.path.join(a.db_dir, f"{a.db}_[0-9][0-9][0-9][0-9].db"))):
        year = re.search(r"_(\d{4})\.db$", p).group(1)
        df = export_csv.read_year(p)
        c = csv[csv["year"] == year]
        if df.empty and c.empty:
            continue
        exp = (len(df), df["symbol"].nunique(), df["date"].max(), round(float(df["close"].sum()), 2))
        got = (len(c), c["Ticker"].nunique(), c["Date"].max(), round(float(c["Close"].sum()), 2))
        ok = exp[:3] == got[:3] and abs(exp[3] - got[3]) <= 0.00005 * max(exp[0], 1) + 0.01   # CSV closes are rounded to 4 decimals: at most 0.00005 per row
        bad += 0 if ok else 1
        last = max(last, str(exp[2]))
        lines.append(f"{year}: {exp[0]:>8} rows {exp[1]:>5} symbols last {exp[2]} sum closes {exp[3]:,.2f}  {'OK' if ok else 'MISMATCH csv=' + str(got)}")
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        sha = "?"
    stamp = (f"DATA STAMP\ncommit: {sha}\nlast data date: {last}\n"
             f"adjustment signature: {convert.ADJ.signature if convert.ADJ else 'none'}\n\n" + "\n".join(lines) + "\n")
    open(os.path.join(a.csv_dir, "DATA_STAMP.txt"), "w", newline="\r\n").write(stamp)
    print(stamp)
    if bad:
        print(f"[FAIL] {bad} year(s) differ between the database and the CSV"); return 1
    print("[OK] CSV matches what convert.py ships to R2"); return 0


if __name__ == "__main__":
    sys.exit(main())
