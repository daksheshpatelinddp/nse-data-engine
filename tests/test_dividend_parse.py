"""Dividend text parsing. Run: python tests/test_dividend_parse.py"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))
import dividends as d
cases = [("Dividend - Rs 5 Per Share", 10, 5.0), ("Dividend - Rs.2.50 Per Share", 10, 2.5), ("Dividend - Rs3.50 Per Share", 10, 3.5),
         ("Final Dividend - Re 1 Per Share", 1, 1.0), ("Interim Dividend - Rs 10 Per Share/Special Dividend - Rs 5 Per Share", 10, 15.0),
         ("Dividend - 150%", 10, 15.0), ("Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share", 10, None),
         ("Bonus 1:1", 10, None), ("Annual General Meeting", 10, None)]
for text, fv, want in cases:
    got = d.parse_dividend(text, fv)
    tot = round(sum(got), 6) if got else None          # two dividends in one text are two amounts; both go off the price on the same day
    assert tot == want, (text, got, want)
print("ALL OK")
