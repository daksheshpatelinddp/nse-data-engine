import os
import json
import sqlite3
from datetime import datetime

# Database Configuration
DB_NAME = "nse_eod_data.db"
OVERRIDE_FILE = "manual_adjustments.json"


def init_db(conn):
    """Initializes the database schema if tables do not exist and handles schema migrations."""
    cursor = conn.cursor()

    # Create OHLCV Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            is_index INTEGER DEFAULT 0,
            PRIMARY KEY (symbol, date)
        )
    ''')

    # Migration Check: Ensure 'is_index' column exists in existing databases
    cursor.execute("PRAGMA table_info(ohlcv)")
    columns = [row[1] for row in cursor.fetchall()]
    if 'is_index' not in columns:
        print("[MIGRATION] Adding missing 'is_index' column to existing ohlcv table...")
        cursor.execute("ALTER TABLE ohlcv ADD COLUMN is_index INTEGER DEFAULT 0")

    # Create Corporate Actions Log Table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS corporate_actions (
            symbol TEXT,
            ex_date TEXT,
            purpose TEXT,
            ratio REAL,
            action_type TEXT,
            PRIMARY KEY (symbol, ex_date, action_type)
        )
    ''')

    conn.commit()


def apply_manual_overrides(conn):
    """Reads manual_adjustments.json and applies retroactive price adjustments."""
    if not os.path.exists(OVERRIDE_FILE):
        print(f"[NOTE] No {OVERRIDE_FILE} file found. Skipping overrides.")
        return

    try:
        with open(OVERRIDE_FILE, 'r') as f:
            overrides = json.load(f)

        cursor = conn.cursor()

        for symbol, events in overrides.items():
            for event in events:
                ex_date = event.get('ex_date')
                factor = float(event.get('factor', 1.0))
                purpose = event.get('description', 'Manual Adjustment')

                if factor < 1.0 and ex_date:
                    # Log event to database corporate_actions table
                    cursor.execute('''
                        INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, purpose, ratio, action_type)
                        VALUES (?, ?, ?, ?, 'MANUAL_OVERRIDE')
                    ''', (symbol, ex_date, purpose, factor))

                    # Apply factor with rounding precision to pre-event historical candles
                    cursor.execute('''
                        UPDATE ohlcv 
                        SET open = ROUND(open * ?, 2),
                            high = ROUND(high * ?, 2),
                            low = ROUND(low * ?, 2),
                            close = ROUND(close * ?, 2)
                        WHERE symbol = ? AND date < ? AND is_index = 0
                    ''', (factor, factor, factor, factor, symbol, ex_date))

                    print(f"[MANUAL ADJUSTMENT APPLIED] {symbol} before {ex_date} with factor {factor}")

        conn.commit()
        print("[SUCCESS] Manual adjustments processed successfully.")
    except Exception as e:
        print(f"[ERROR] Failed to apply manual adjustments: {e}")


def main():
    """Main execution pipeline."""
    conn = sqlite3.connect(DB_NAME)

    # Initialize tables and migration check
    init_db(conn)

    # Apply Manual Corporate Actions Adjustments
    apply_manual_overrides(conn)

    conn.close()


if __name__ == "__main__":
    main()