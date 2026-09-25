import sqlite3
import glob
import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Dict, Any

app = FastAPI(title="AmiBroker Data Engine API", version="2.0")

# Enable CORS for browser access
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_files() -> List[str]:
    """Scans and returns all available yearly stock database files."""
    db_files = sorted(glob.glob("nse_*.db"))
    if not db_files and os.path.exists("nse_data.db"):
        db_files = ["nse_data.db"]
    return db_files

@app.get("/api/symbols")
def get_symbols() -> List[str]:
    """Retrieves all distinct ticker symbols across available databases."""
    symbols = set()
    db_files = get_db_files()
    
    if not db_files:
        return []

    for db_path in db_files:
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT Ticker FROM stock_prices")
            rows = cursor.fetchall()
            for row in rows:
                if row[0]:
                    symbols.add(row[0].strip().upper())
            conn.close()
        except Exception:
            continue
            
    return sorted(list(symbols))

@app.get("/api/ohlcv/{symbol}")
def get_ohlcv(
    symbol: str, 
    start_date: str = Query(None, description="Format: YYYY-MM-DD"), 
    end_date: str = Query(None, description="Format: YYYY-MM-DD")
) -> List[Dict[str, Any]]:
    """Fetches combined OHLCV records for a given stock symbol in chronological order."""
    clean_symbol = symbol.strip().upper()
    all_data = []
    db_files = get_db_files()

    if not db_files:
        raise HTTPException(status_code=404, detail="No database files found on server.")

    for db_path in db_files:
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            
            query = """
                SELECT Date, Open, High, Low, Close, Volume 
                FROM stock_prices 
                WHERE Ticker = ?
            """
            params = [clean_symbol]

            if start_date:
                query += " AND Date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND Date <= ?"
                params.append(end_date)

            query += " ORDER BY Date ASC"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            for r in rows:
                raw_date = str(r[0]).replace("-", "")
                if len(raw_date) == 8:
                    formatted_date = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}"
                else:
                    formatted_date = str(r[0])

                all_data.append({
                    "time": formatted_date,
                    "open": float(r[1]),
                    "high": float(r[2]),
                    "low": float(r[3]),
                    "close": float(r[4]),
                    "volume": float(r[5]) if r[5] is not None else 0.0
                })
            conn.close()
        except Exception:
            continue

    if not all_data:
        raise HTTPException(status_code=404, detail=f"No price records found for ticker '{clean_symbol}'.")

    # Deduplicate dates across database overlaps and sort
    unique_data = {item['time']: item for item in all_data}
    sorted_records = [unique_data[k] for k in sorted(unique_data.keys())]

    return sorted_records

@app.get("/")
def serve_index():
    """Serves the interactive charting app index.html from root."""
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "AmiBroker Data Engine API is Live. Visit /docs for swagger docs."}