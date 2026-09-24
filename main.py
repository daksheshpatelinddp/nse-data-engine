import sqlite3
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from typing import List, Optional
import os

app = FastAPI(
    title="AmiBroker Replica EOD API Engine",
    description="High-performance SQLite data engine for Flutter charting canvas",
    version="1.0.0"
)

# Enable CORS for Flutter web/mobile requests
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

DB_PATH = os.getenv("DB_PATH", "nse_eod_data.db")

def get_db_connection():
    if not os.path.exists(DB_PATH):
        raise HTTPException(status_code=500, detail=f"Database file '{DB_PATH}' not found.")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/health")
def health_check():
    return {"status": "online", "database": DB_PATH}

@app.get("/api/v1/symbols")
def get_symbols():
    """Fetch list of all available stock/index tickers."""
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT DISTINCT symbol FROM ohlcv ORDER BY symbol ASC")
        rows = cursor.fetchall()
        symbols = [row["symbol"] for row in rows]
        return {"count": len(symbols), "symbols": symbols}
    finally:
        conn.close()

@app.get("/api/v1/chart/ohlcv")
def get_ohlcv(
    symbol: str = Query(..., description="Stock symbol, e.g., RELIANCE"),
    start_date: Optional[str] = Query(None, description="Format: YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="Format: YYYY-MM-DD")
):
    """
    Returns time-series OHLCV array optimized for mobile chart rendering.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        query = "SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = ?"
        params = [symbol]

        if start_date:
            query += " AND date >= ?"
            params.append(start_date)
        if end_date:
            query += " AND date <= ?"
            params.append(end_date)

        query += " ORDER BY date ASC"
        cursor.execute(query, params)
        rows = cursor.fetchall()

        if not rows:
            raise HTTPException(status_code=404, detail=f"No data found for symbol: {symbol}")

        # Returns array formatted for high-performance memory consumption in Flutter
        dates, opens, highs, lows, closes, volumes = [], [], [], [], [], []
        for r in rows:
            dates.append(r["date"])
            opens.append(float(r["open"]))
            highs.append(float(r["high"]))
            lows.append(float(r["low"]))
            closes.append(float(r["close"]))
            volumes.append(int(r["volume"]))

        return {
            "symbol": symbol,
            "total_bars": len(dates),
            "dates": dates,
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": volumes
        }
    finally:
        conn.close()