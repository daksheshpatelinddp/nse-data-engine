import sqlite3
import glob
import os
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from typing import List, Dict, Any

app = FastAPI(title="AmiBroker Data Engine API", version="2.0")

# Enable CORS for web/mobile browsers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db_files() -> List[str]:
    """Finds all yearly database files (e.g. nse_2023.db, nse_2024.db, etc.)."""
    db_files = sorted(glob.glob("nse_*.db"))
    if not db_files and os.path.exists("nse_data.db"):
        db_files = ["nse_data.db"]
    return db_files

def get_table_name(cursor) -> str:
    """Detects whether the table is named 'ohlcv' or 'stock_prices'."""
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = [row[0] for row in cursor.fetchall()]
    if "ohlcv" in tables:
        return "ohlcv"
    elif "stock_prices" in tables:
        return "stock_prices"
    return tables[0] if tables else "ohlcv"

@app.get("/api/symbols")
def get_symbols() -> List[str]:
    """Retrieves all distinct stock symbols across available databases."""
    symbols = set()
    db_files = get_db_files()
    
    if not db_files:
        return []

    for db_path in db_files:
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            table = get_table_name(cursor)
            
            # Check for column name (symbol vs ticker)
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [col[1].lower() for col in cursor.fetchall()]
            sym_col = "symbol" if "symbol" in cols else "ticker"

            cursor.execute(f"SELECT DISTINCT {sym_col} FROM {table}")
            rows = cursor.fetchall()
            for row in rows:
                if row[0]:
                    symbols.add(str(row[0]).strip().upper())
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
        raise HTTPException(status_code=404, detail="No database files found on backend.")

    for db_path in db_files:
        try:
            conn = sqlite3.connect(db_path)
            cursor = conn.cursor()
            table = get_table_name(cursor)

            cursor.execute(f"PRAGMA table_info({table})")
            cols = [col[1].lower() for col in cursor.fetchall()]
            sym_col = "symbol" if "symbol" in cols else "ticker"

            query = f"""
                SELECT date, open, high, low, close, volume 
                FROM {table} 
                WHERE upper({sym_col}) = ?
            """
            params = [clean_symbol]

            if start_date:
                query += " AND date >= ?"
                params.append(start_date)
            if end_date:
                query += " AND date <= ?"
                params.append(end_date)

            query += " ORDER BY date ASC"

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
        raise HTTPException(status_code=404, detail=f"No stock price data found for '{clean_symbol}'.")

    unique_data = {item['time']: item for item in all_data}
    sorted_records = [unique_data[k] for k in sorted(unique_data.keys())]

    return sorted_records

@app.get("/")
def serve_index():
    if os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"status": "AmiBroker Data Engine API is Live."}