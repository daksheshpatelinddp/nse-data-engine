import json

def apply_manual_overrides(conn):
    override_file = "manual_adjustments.json"
    
    if not os.path.exists(override_file):
        print("[NOTE] No manual_adjustments.json file found. Skipping overrides.")
        return

    try:
        with open(override_file, 'r') as f:
            overrides = json.load(f)
            
        cursor = conn.cursor()
        
        for symbol, events in overrides.items():
            for event in events:
                ex_date = event.get('ex_date')
                factor = float(event.get('factor', 1.0))
                purpose = event.get('description', 'Manual Adjustment')
                
                if factor < 1.0 and ex_date:
                    # Log event to database
                    cursor.execute('''
                        INSERT OR IGNORE INTO corporate_actions (symbol, ex_date, purpose, ratio, action_type)
                        VALUES (?, ?, ?, ?, 'MANUAL_OVERRIDE')
                    ''', (symbol, ex_date, purpose, factor))
                    
                    # Apply factor to pre-event historical candles
                    cursor.execute('''
                        UPDATE ohlcv 
                        SET open = open * ?,
                            high = high * ?,
                            low = low * ?,
                            close = close * ?
                        WHERE symbol = ? AND date < ? AND is_index = 0
                    ''', (factor, factor, factor, factor, symbol, ex_date))
                    
                    print(f"[MANUAL ADJUSTMENT APPLIED] {symbol} before {ex_date} with factor {factor}")
                    
        conn.commit()
    except Exception as e:
        print(f"[ERROR] Failed to apply manual adjustments: {e}")