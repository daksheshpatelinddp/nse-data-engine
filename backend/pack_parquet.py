"""
Makes the saved Parquet files safe for GitHub: a yearly file bigger than --max-mb MB is replaced by one file per month
(<year>-<MM>.parquet, same rows, same columns). Smaller years stay as they are. The files that go to R2 are NOT touched: the web app keeps
reading whole yearly files there; this only changes the copy that is saved in the repository (branch parquet-data).

  python backend/pack_parquet.py --dir parquet [--db nse] [--max-mb 45]

Whole files and monthly files are both understood by export_csv.py (--source parquet). Running it twice changes nothing.
"""
import argparse
import os
import re
import sys

import pyarrow as pa
import pyarrow.parquet as pq

SPLIT_COMPRESSION = "snappy"
ROW_GROUP = 5000


def pack(directory, max_mb=45.0):
    """Returns [(year, size_mb, n_files)] for the years that were split."""
    done = []
    limit = max_mb * 1e6
    for name in sorted(os.listdir(directory)):
        m = re.match(r"^(\d{4})\.parquet$", name)
        if not m:
            continue
        path = os.path.join(directory, name)
        size = os.path.getsize(path)
        if size <= limit:
            continue
        year = m.group(1)
        df = pq.read_table(path).to_pandas()
        df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
        months = df["date"].astype(str).str[:7]
        parts = []
        for month, g in df.groupby(months, sort=True):
            out = os.path.join(directory, f"{month}.parquet")
            pq.write_table(pa.Table.from_pandas(g.reset_index(drop=True), preserve_index=False), out,
                           compression=SPLIT_COMPRESSION, row_group_size=ROW_GROUP)
            if os.path.getsize(out) > limit:
                raise RuntimeError(f"{out} is still over {max_mb:g} MB")
            parts.append(out)
        if sum(len(pq.read_table(p)) for p in parts) != len(df):          # never lose a row
            raise RuntimeError(f"{year}: monthly files do not add up to {len(df)} rows")
        os.remove(path)
        done.append((year, round(size / 1e6, 1), len(parts)))
        print(f"[PACK] {year}.parquet {size / 1e6:.1f} MB -> {len(parts)} monthly files")
    return done


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=os.getenv("PARQUET_DIR", "parquet"))
    ap.add_argument("--db", default="nse")
    ap.add_argument("--max-mb", type=float, default=45.0)
    a = ap.parse_args(argv)
    d = os.path.join(a.dir, a.db)
    if not os.path.isdir(d):
        print(f"No folder {d}"); return 1
    done = pack(d, a.max_mb)
    print(f"[PACK] {len(done)} year(s) split" if done else f"[PACK] every file is under {a.max_mb:g} MB: nothing to split")
    return 0


if __name__ == "__main__":
    sys.exit(main())
