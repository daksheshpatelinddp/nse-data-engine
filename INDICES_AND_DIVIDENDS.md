# Indices and dividends - what was added

## Indices (Nifty 50, Nifty 500, all NSE indices)
* `fetch_data.py` now also downloads NSE's daily file `ind_close_all_<ddmmyyyy>.csv` for every trading day that has stock data
  and stores it in the same yearly `.db` files (`is_index = 1`). Names: `^NIFTY50`, `^NIFTY500`, `^NIFTYBANK`, `^INDIAVIX` ...
  (official name, letters and digits only, so no spaces: symbol lists in the app are split at spaces).
* Missing open/high/low become the close (29 of 167 indices have no OHLC at NSE), missing volume becomes 0.
* `convert.py` puts the indices in the yearly parquet files, history packs and recent pack, writes `nse/indices.json` (readable names)
  and adds typed names (`NIFTY50`, `NSEI`, `BANKNIFTY` ...) to the manifest aliases.
* Older years: run the workflow "Daily NSE Fetcher" with `index_only` ticked. Days NSE has no file for are remembered
  (`index_recheck` asks again). History NSE does not serve: drop CSV files into `data/index_files/` (see the README there) or try
  `index_api_backfill` (niftyindices.com, experimental, probably blocked from GitHub).
* Run the `probe` option first to see how far back NSE's daily index files exist.
* In the app: `Foreign("^NIFTY50","C")`, chart symbol `^NIFTY50` (or `NIFTY50`), "All stocks" scans leave indices out.

## Dividends
* `backend/dividends.py` reads the NSE corporate-action files (+ the live list) and writes `data/corporate_actions/dividends.csv`.
* At conversion time prices before each ex-date are multiplied by `1 - dividend / cum-dividend close` (close before the ex-date,
  with later splits undone). Volume is not changed. The `.db` files stay raw-plus-splits, so nothing is applied twice.
* A new dividend changes older prices, so the history is rebuilt (the fingerprint contains the dividend list) and the runner's cache refreshes.
* Turn off with `DIVIDEND_ADJUST=false`. Dividends bigger than half the price are skipped (`DIVIDEND_MIN_FACTOR`).
* `dividend-report.txt` (artifact of the convert workflow) lists what was applied/skipped.
* The daily convert only rebuilds the current year's yearly parquet; run "Convert DB to Parquet & Upload R2" by hand (it converts all years)
  after the first deployment so the old yearly files are dividend-adjusted too. The app reads the history packs, which are rebuilt automatically.

## Tests (no network needed)
`python tests/test_convert_e2e.py`, `python tests/test_indices.py`, `python tests/test_dividend_parse.py`
(without pyarrow/boto3 installed the first one uses small stand-ins from `tests/stubs`).
