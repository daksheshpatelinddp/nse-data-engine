# Missing splits / bonuses in the NSE prices

Some splits and bonuses are not in the prices (the NSE corporate-action files used to adjust the .db files did not have them, or labelled them differently).
They show up as a fake one-day fall, e.g. EIDPARRY -78 % on 2005-05-27 (5-for-1).

Nothing in the .db files is changed. Like dividends, these events are applied when the files for R2 are built (backend/convert.py).

## Files
- `data/corporate_actions/manual_splits.csv` - the events that are applied (9 confirmed ones are in it).
- `backend/splits.py` - applies them (prices before ex_date / factor, volume and delivery x factor); skips an event whose drop is not visible in the data
  (already adjusted), so nothing is adjusted twice.
- `backend/find_jumps.py` + workflow "NSE - find missing splits (report only)" - lists every one-day move below 0.55 or above 1.8 in the .db files.

## How to find and fix the rest
1. Run the workflow "NSE - find missing splits (report only)" (or `python backend/find_jumps.py`). Download `split_candidates.csv`.
2. For each line check the chart (TradingView / NSE). A real split shows a clean drop to about 1/factor on the ex-date.
   Genuine crashes beyond -45 % in one day are rare on NSE; gaps of weeks (column `note`) may be suspensions.
3. Copy the confirmed lines from `split_candidates_paste.csv` into `manual_splits.csv` (fix `factor` if the suggested one is not exact).
4. Run the R2 upload workflow with FORCE_ALL = true. The history is rebuilt once because the adjustment signature changed.
5. Re-run find_jumps: confirmed lines now show `already_listed = yes`.

factor = new shares per old share: 5-for-1 split = 5, 1:1 bonus = 2, 1:2 bonus = 1.5, reverse split 1-for-10 = 0.1.

## Settings (environment variables)
SPLIT_FILE (other csv path), SPLIT_CHECK=false (skip the "already adjusted" check), SPLIT_ADJUST_VOLUME=false (leave volume alone),
JUMP_LOW / JUMP_HIGH (finder thresholds).
