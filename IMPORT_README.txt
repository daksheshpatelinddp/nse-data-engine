AmiBroker import of the nse data  (made 2026-11-14 16:17 UTC)
==========================================================

These CSV files hold the SAME prices as the web app: end-of-day bars of every stock and index, already adjusted for splits, bonus,
rights, demergers AND cash dividends (the dividend adjustment the app applies when it converts the data).
So do NOT let AmiBroker adjust them again (do not add splits in Symbol > Information).

Columns: Ticker, Date (YYYY-MM-DD), Open, High, Low, Close, Volume (shares), Delivery (shares, empty when unknown),
DeliveryPct (Delivery / Volume x 100, empty when unknown). The database holds no other fields (no turnover, no number of trades).
Index names start with ^ (for example ^NIFTY50). One file per year: nse_<year>.csv; a big year is split in monthly files
(nse_<year>-<month>.csv, for example nse_2024-03.csv). Just import all of them.

Import (once):
 1. Download everything from GitHub: open the repository, choose the branch  csv-export , Code > Download ZIP, then unzip it anywhere.
 2. Copy  nse_eod.format  into the  Formats  folder of your AmiBroker installation (next to Broker.exe).
 3. AmiBroker: File > New > Database. Choose a folder and a name, "Local data", then set Base time interval = Daily
    and "Number of bars" to 10000 or more (the files go back to 2000). Press Create.
 4. File > Import ASCII...  In "Files of type" choose  nse_eod ,  select ALL the .csv files (Ctrl+A) and press Open.
    Importing takes a few minutes. Importing the same files again updates the bars.
 5. In Analysis use "Apply to: All symbols" (or a filter) and the same date range as in the web app.

In AFL:  Aux1 = delivery quantity,  Aux2 = delivery %.   Checks: export-report.txt lists rows, stocks and the sum of all closes per year.
