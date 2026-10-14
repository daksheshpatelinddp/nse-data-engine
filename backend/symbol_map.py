"""
Symbol renames -> one continuous history per company.

NSE renames tickers (INFOSYSTCH -> INFY on 2011-06-29, ZOMATO -> ETERNAL on 2025-04-09). The daily
files carry the name that was valid on that day, so without help a company's history is cut in
pieces. data/symbol_changes.csv (NSE's "symbolchange.csv": NAME OF THE COMPANY, OLD SYMBOL,
NEW SYMBOL, EFFECTIVE DATE) tells us how to join them.

Rule: a row for symbol S on date D belongs to the company's LATEST name. canonical_symbol(S, D)
follows every rename of S that takes effect AFTER D. A row dated on or after the effective date is
left alone, so a name that is later reused by a different company is never mixed up.
"""
import csv
import os
import sqlite3
from datetime import datetime

DEFAULT_PATH = os.getenv("SYMBOL_CHANGES_FILE", "data/symbol_changes.csv")
_DATE_FORMATS = ("%d-%b-%Y", "%d-%B-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y")


def _parse_date(text):
    text = (text or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def read_rows(path):
    """Reads the CSV into [(old, new, 'YYYY-MM-DD', company)] - tolerant about column order/spacing.
    Rows with a missing/unreadable date, an empty symbol or old == new are dropped."""
    rows = []
    if not path or not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8-sig", newline="") as f:
        rdr = csv.reader(f, skipinitialspace=True)
        header = next(rdr, None)
        if not header:
            return rows
        up = [h.strip().upper() for h in header]

        def col(word, default):
            for i, h in enumerate(up):
                if word in h:
                    return i
            return default
        i_name, i_old, i_new, i_date = 0, col("OLD", 1), col("NEW", 2), col("DATE", 3)
        for r in rdr:
            if len(r) <= max(i_old, i_new, i_date):
                continue
            old, new = r[i_old].strip().upper(), r[i_new].strip().upper()
            date = _parse_date(r[i_date])
            if not old or not new or old == new or not date:
                continue
            rows.append((old, new, date, r[i_name].strip() if len(r) > i_name else ""))
    return rows


def load_symbol_changes(path=None):
    """{old symbol: [(date, new symbol), ...]} sorted by date, duplicates removed."""
    changes = {}
    for old, new, date, _ in read_rows(path or DEFAULT_PATH):
        changes.setdefault(old, set()).add((date, new))
    return {old: sorted(v) for old, v in changes.items()}


def canonical_symbol(symbol, date_str, changes):
    """The company's latest name for a row of `symbol` dated `date_str` (YYYY-MM-DD)."""
    s = symbol
    for _ in range(12):                       # chains are short; the cap also stops any loop in the data
        steps = changes.get(s)
        if not steps:
            break
        nxt = next((new for date, new in steps if date > date_str), None)
        if nxt is None or nxt == s:
            break
        s = nxt
    return s


class SymbolMapper:
    """Convenience wrapper with a cache, for mapping a whole day of rows."""
    def __init__(self, path=None):
        self.changes = load_symbol_changes(path)
        self._cache = {}

    def __bool__(self):
        return bool(self.changes)

    def canonical(self, symbol, date_str):
        key = (symbol, date_str)
        hit = self._cache.get(key)
        if hit is None:
            hit = canonical_symbol(symbol, date_str, self.changes)
            self._cache[key] = hit
        return hit


def migrate_ca_records(conn, mapper, apply=True):
    """Moves corporate-action records to the company's latest name. Returns how many were (or would be) moved.

    These records are the 'already applied' memory of the corporate-action step. If they kept an old name
    while new events are recorded under the new name, an old split would be applied a second time.
    Safe to repeat. Called at the start of every fetch run and by apply_symbol_changes.py."""
    try:
        recs = conn.execute("SELECT symbol, ex_date, action_type FROM corporate_actions").fetchall()
    except sqlite3.OperationalError:
        return 0
    moved = 0
    for sym, ex, typ in recs:
        target = mapper.canonical(sym, ex)
        if target == sym:
            continue
        moved += 1
        if apply:
            cur = conn.execute("UPDATE OR IGNORE corporate_actions SET symbol=? WHERE symbol=? AND ex_date=? AND action_type=?",
                               (target, sym, ex, typ))
            if cur.rowcount == 0:              # the new name already holds this exact record
                conn.execute("DELETE FROM corporate_actions WHERE symbol=? AND ex_date=? AND action_type=?", (sym, ex, typ))
    if apply and moved:
        conn.commit()
    return moved
