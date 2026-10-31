Export CSV for the original AmiBroker (new; nothing else changes, no R2 is used)
================================================================================
Add these 3 files to your nse-data-engine repo (same folders):
  .github/workflows/export-csv.yml
  backend/export_csv.py
  tests/test_export_csv.py            (only a check, not used by the workflow)
Run:  GitHub > your nse-data-engine repo > Actions > "Export CSV for AmiBroker" > Run workflow  (leave the options as they are).
The CSV files are committed to the branch  csv-export  of the same repository (one new commit per run, the old one is replaced, so the repo does
not keep growing). A year bigger than 40 MB is split in monthly files (nse_2024-03.csv ...); smaller years stay one file (nse_2019.csv).
Download on your laptop: the run summary shows the link  https://github.com/<owner>/<repo>/archive/refs/heads/csv-export.zip
(or open the repo, switch to the branch csv-export, Code > Download ZIP). Unzip, then follow IMPORT_README.txt inside.
The prices are the same as the web app reads: it uses convert.py's own reader and dividend adjuster, so both places always agree.
Works with your current convert.py and with the new one. Never merge the csv-export branch into main.
