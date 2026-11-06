# Missing splits / bonuses in the NSE prices

Some splits and bonuses are not in the prices (the NSE corporate-action files used to adjust the .db files did not have them, or labelled them differently).
They show up as a fake one-day fall, e.g. EIDPARRY -78 % on 2005-05-27 (5-for-1).

Nothing in the .db files is changed. Like dividends, these events are applied when the files for R2 are built (backend/convert.py).

## Files
- `data/corporate_actions/manual_splits.csv` - the events that are applied (9 confirmed ones are in it).
- `backend/splits.py` - applies them (prices before ex_date / factor, volume and delivery x factor); skips an event whose drop is not visible in the data
  (already adjusted), so nothing is adjusted twice.
- `backend/find_jumps.py` + workflow "NSE - find missing splits (report only)" - lists every one-day close move beyond the 20 % price band (below 0.78 or above 1.25) in the .db files.

## How to find and fix the rest
1. Run the workflow "NSE - find missing splits (report only)" (or `python backend/find_jumps.py`). Download `split_candidates.csv`.
2. The finder now sorts candidates by evidence (column `confidence` in `split_candidates.csv`):
   high = clean ratio, no gap, the price level moved for good AND the volume followed the ratio;  medium = same without the volume proof;  low = the rest
   (column `note` says why). Start with `split_high_confidence_paste.csv`, then `split_medium_confidence_paste.csv`.
3. Check the lines on a chart (TradingView / NSE): a real split shows a clean drop to about 1/factor on the ex-date. Copy the confirmed lines into `manual_splits.csv` (fix `factor` if the suggested one is not exact).
4. Run the R2 upload workflow with FORCE_ALL = true. The history is rebuilt once because the adjustment signature changed.
5. Re-run find_jumps: confirmed lines now show `already_listed = yes`.

factor = new shares per old share: 5-for-1 split = 5, 1:1 bonus = 2, 1:2 bonus = 1.5, reverse split 1-for-10 = 0.1.

## Settings (environment variables)
SPLIT_FILE (other csv path), SPLIT_CHECK=false (skip the "already adjusted" check), SPLIT_ADJUST_VOLUME=false (leave volume alone),
JUMP_LOW (0.78) / JUMP_HIGH (1.25): finder thresholds.

## Checking candidates automatically (Yahoo)
`backend/verify_splits.py` + workflow "NSE - verify candidate splits on Yahoo (report only)".
Put the lines to check (symbol,ex_date,factor,note) into `data/review/to_verify.csv`, commit, run the workflow, download "split-verification":
- `verified_apply.csv`  lines Yahoo confirms (or with another ratio): append them to `manual_splits.csv`.
- `verify_report.csv`   every line with its status: confirmed / ratio_differs / not_on_yahoo / no_symbol.
Yahoo misses some old events, so "not_on_yahoo" means "check by hand or leave out", not "false".
