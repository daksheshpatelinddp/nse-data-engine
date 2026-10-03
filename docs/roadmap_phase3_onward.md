================================================================================
  PHASE 3+ ROADMAP: AMIBROKER-STYLE WEB APP
  NSE Stock Analysis | 100% Free-Tier Stack | Browser-based (R2 + DuckDB-WASM)
================================================================================

## 0. WHERE WE ARE

Phase 2 (data pipeline): 2000-2022 backfill deliberately deferred by choice
(2023-2026 across 2,978 symbols is enough to build and validate against -
good risk call, revisit later only if actually needed). Daily EOD automation
handled by the user directly - to be verified once the workflow file is shared.

Phase 3 is starting from a genuinely solid base: candlestick + line charts,
SMA/EMA/BB overlays, Volume + RSI lower pane, tabs, live OHLC legend, all
confirmed working end-to-end against real R2 data.

--------------------------------------------------------------------------------

## 1. WHAT "AMIBROKER REPLICA" REALISTICALLY MEANS HERE

AmiBroker is a 20+ year old, compiled C++ desktop application with a custom
vectorized scripting language (AFL), multi-broker real-time data feeds, and
institutional-grade optimization (PSO, CMA-ES, walk-forward, Monte Carlo).
Replicating ALL of it in a browser app run by one person on free hosting is
not a realistic goal, and most of it wouldn't actually be useful for your
stated use case (personal NSE end-of-day analysis + backtesting).

This roadmap takes AmiBroker's feature categories and asks, for each one:
"what's the 80/20 version of this that's actually worth building here?"

Explicitly NOT in scope, with reasons:
  - Real-time multi-broker data feeds (eSignal/IQFeed/Interactive Brokers) -
    you have daily bhavcopy EOD data, not live tick data. A "real-time" tab
    is a different project (needs a live broker API + streaming backend).
  - DDE/ODBC plugins - Windows-specific interop, meaningless in a browser.
  - PSO / CMA-ES evolutionary optimization, Monte Carlo simulation - these
    are advanced quant-research features. Worth revisiting only after a
    basic single-pass backtester is solid and you actually want them.
  - AFL as a full custom compiled language - not feasible to replicate a
    20-year-old DSL+compiler. A much simpler JS-based rule builder covers
    most practical strategy testing needs instead (see Phase 4).

--------------------------------------------------------------------------------

## 2. PHASE 3: CHARTING ENGINE - GRANULAR CHECKLIST
   (maps to AmiBroker's "Charting" feature category, broken into small,
   one-at-a-time buildable pieces based on the actual AmiBroker charting
   guide: https://www.amibroker.com/guide/h_charting.html)

Goal: take the working chart from "functional" to "genuinely pleasant to use
daily." Each [ ] below is meant to be one sitting's worth of work, not a
whole feature category - tackle them in order, check them off as we go.

### GROUP A - Core interaction model (do first - other groups build on this)
  [ ] A1. Tap-to-pin OHLC legend (currently shows on drag; confirm/fix it
          stays pinned after finger lift, since mobile has no hover)
  [ ] A2. Step one bar at a time from a pinned selection (prev/next buttons,
          since there's no keyboard on mobile - AmiBroker uses arrow keys)
  [ ] A3. Y-axis manual drag to shift price scale up/down
  [ ] A4. Y-axis pinch or two-finger drag to expand/shrink scale
  [ ] A5. Double-tap Y-axis to reset scale to auto
  [ ] A6. Range marking - tap two points, show change/high/low stats for
          that range, with a "zoom to this range" option

### GROUP B - Drawing tools (build one shape at a time; this is the
    single most-requested AmiBroker charting feature)
  [ ] B1. Drawing toolbar shell (tool palette UI, active-tool state)
  [ ] B2. Trend line tool: tap two points to draw, tap the line to select
  [ ] B3. Select tool: move a drawn line by dragging its endpoints
  [ ] B4. Delete a selected drawing (× button or delete icon)
  [ ] B5. Horizontal line tool
  [ ] B6. Vertical line tool
  [ ] B7. Ray / extended line tool
  [ ] B8. Rectangle tool
  [ ] B9. Text annotation tool
  [ ] B10. Fibonacci retracement tool
  [ ] B11. Object style panel - color/thickness/dotted for selected drawing
  [ ] B12. Copy/cut/paste a drawing object
  [ ] B13. Persist drawings per symbol+tab in IndexedDB, restore on reopen
  [ ] B14. (stretch, later) Parallel lines, regression channel, Fib fan/arc

### GROUP C - Chart types & periodicity
  [ ] C1. Bar (OHLC) chart style
  [ ] C2. Heikin-Ashi chart style (free - just a transform of existing data)
  [ ] C3. Area chart style
  [ ] C4. Weekly aggregation (GROUP BY resample of daily data already in
          DuckDB - no new data needed)
  [ ] C5. Monthly aggregation (same approach as C4)
  [ ] C6. Periodicity toggle UI matching AmiBroker's d/w/m toolbar concept

### GROUP D - More indicators (same pattern as existing SMA/EMA/BB/RSI)
  [ ] D1. MACD
  [ ] D2. Stochastic Oscillator
  [ ] D3. ATR
  [ ] D4. ADX/DMI
  [ ] D5. Parabolic SAR (drawn directly on price, like AmiBroker's default)

### GROUP E - Multi-chart linking (AmiBroker's colored "S"/"I" buttons)
  [ ] E1. Symbol-Link toggle - linked tabs change symbol together
  [ ] E2. Interval-Link toggle - linked tabs change periodicity together
  [ ] E3. Symbol Lock (padlock) - prevent a tab's symbol from changing

### GROUP F - Saved layouts ("chart sheets" in AmiBroker)
  [ ] F1. Save current tab's indicators/drawings/style as a named layout
  [ ] F2. Quick-switch between saved layouts
  [ ] F3. Watchlist panel (persistent symbol list, tap to load) - also the
          foundation Phase 5's scanner will build on

--------------------------------------------------------------------------------

## 2B. PHASE 3.5: APP SHELL - GRANULAR CHECKLIST
    (new addition - not an AmiBroker feature per se, but the right foundation
    to build early since later features will plug into it)

Goal: stop feeling like "a website you reload" and start feeling like an
actual app you open, configure once, and trust to remember your setup.

### GROUP G - Installable PWA
  [ ] G1. Write manifest.json (name, icon, theme_color, start_url,
          display: standalone)
  [ ] G2. Add an app icon (one source image, a couple of sizes)
  [ ] G3. Register a minimal service worker caching the app shell itself
          (index.html + JS deps) - natural extension of the IndexedDB
          parquet-caching pattern already built
  [ ] G4. Confirm Chrome/Opera actually offer "Add to Home Screen"
  [ ] G5. Confirm launching from the home-screen icon opens full-screen,
          no browser address bar

### GROUP H - Settings / Preferences panel
  [ ] H1. Settings button + a simple modal/slide-in settings screen shell
  [ ] H2. Default chart type on load (Candles/Line/Bar/Heikin-Ashi), persisted
  [ ] H3. Default indicators-to-auto-apply setting, persisted
  [ ] H4. Light/dark theme toggle, persisted
  [ ] H5. Volume/number display format setting (lakhs/crores vs raw number)
  [ ] H6. Wire saved settings into app startup so they actually take effect

### GROUP I - Per-symbol/tab memory
  [ ] I1. Save active indicators + chart style per symbol whenever changed
  [ ] I2. Restore saved indicators + style when reopening that symbol
  [ ] I3. (depends on Group B) restore saved drawings per symbol too

### GROUP J - Touch-first polish
  [ ] J1. Audit and enlarge touch targets on all toolbar buttons
  [ ] J2. Two-finger pinch-to-zoom tuned specifically for chart panes
  [ ] J3. (nice-to-have) Light haptic/visual feedback on button taps

--------------------------------------------------------------------------------

## 3. PHASE 4: BACKTESTING ENGINE - GRANULAR CHECKLIST
   (grounded directly in AmiBroker's actual backtester mechanics:
   https://www.amibroker.com/guide/h_backtest.html)

Goal: single-symbol backtesting first, proven solid, before ever touching
portfolio-level (multi-symbol) - that order matters, don't skip ahead.

### GROUP K - Rule builder foundation ("AFL-lite")
  [ ] K1. Data model for one condition: [indicator/price] [operator]
          [indicator/price/value]
  [ ] K2. "Crosses above / crosses below" operator - AmiBroker's cross()
          equivalent, the single most common entry/exit trigger
  [ ] K3. Simple comparison operators (>, <, >=, <=, ==)
  [ ] K4. Combine two conditions with AND
  [ ] K5. Combine two conditions with OR
  [ ] K6. UI: pick indicator + params from dropdowns, not typed formulas
  [ ] K7. Separate Buy-rule and Sell-rule builders (matches AmiBroker's
          reserved 'buy'/'sell' variables)
  [ ] K8. (stretch) Short/Cover rule builders for short-side testing

### GROUP L - Single-symbol backtest core
  [ ] L1. Bar-by-bar simulation loop over tab.data evaluating buy/sell rules
  [ ] L2. Entry/exit price model - default to next-bar open (avoids
          lookahead bias) - matches AmiBroker's buyprice/sellprice concept
  [ ] L3. (stretch) Let user choose entry price timing: this-bar close /
          next-bar open / next-bar close
  [ ] L4. Track one open position at a time, produce a trade list (entry
          date/price, exit date/price, P&L, % return)
  [ ] L5. Compute core metrics from the trade list: win rate, profit
          factor, average win/loss, max drawdown, CAGR, total return
  [ ] L6. Results panel UI - trade list table + metrics summary

### GROUP M - Stops (each independent, add one at a time)
  [ ] M1. Fixed % profit-target stop
  [ ] M2. Fixed % max-loss stop
  [ ] M3. Trailing stop (%-based)
  [ ] M4. ATR-based dynamic stop (AmiBroker's ApplyStop volatility example:
          stop = entry - 2*ATR(20))
  [ ] M5. N-bar time stop (exit after N bars regardless of price)

### GROUP N - Equity curve & visuals
  [ ] N1. Equity curve series, plotted by reusing Phase 3's existing line
          chart component
  [ ] N2. Buy/sell markers overlaid directly on the price chart
  [ ] N3. Drawdown chart (underwater equity curve)

### GROUP O - Position sizing (single-symbol first)
  [ ] O1. Fixed-dollar position size per trade
  [ ] O2. Fixed-% of equity position size
  [ ] O3. Round lot size setting (NSE = whole shares; likely always 1, but
          keep it configurable rather than hardcoded)

### GROUP P - Portfolio-level (multi-symbol) - build only after K-O are solid
  [ ] P1. Run the same Buy/Sell rules across all 2,978 symbols for a date
          range, not just one
  [ ] P2. Max concurrent open positions limit (portfolio-level constraint)
  [ ] P3. Shared capital pool across all symbols (vs. unlimited-per-symbol)
  [ ] P4. Portfolio-level equity curve + aggregate metrics
  [ ] P5. Performance check - 2,978 symbols x ~3-4 years of daily bars needs
          to run acceptably fast in a MOBILE browser. Test on a 50-symbol
          subset first before ever scaling to the full universe - this is
          the single most likely place for a "why is my phone frozen" bug

--------------------------------------------------------------------------------

## 4. PHASE 5: SCANNING, WATCHLISTS & ALERTS - GRANULAR CHECKLIST

### GROUP Q - Scanner
  [ ] Q1. Run one Buy-rule (from Group K) against just "today" (the latest
          bar) across all symbols, list matches
  [ ] Q2. Sort/filter scanner results (by symbol, by indicator value)
  [ ] Q3. Tap a scanner result to open that symbol's chart directly

### GROUP R - Categorization
  [ ] R1. Tag symbols into custom groups/favorites, stored locally
  [ ] R2. Filter watchlist/scanner results by tag

### GROUP S - Daily digest alerts (scoped down from AmiBroker's real-time
    alerts, since you only have daily EOD data, not live ticks)
  [ ] S1. Reuse your existing BSE-monitor Telegram bot infrastructure to
          send one daily message after EOD automation completes
  [ ] S2. Message content = today's scanner matches for a saved rule, run
          server-side (a GitHub Action step after convert.py)

--------------------------------------------------------------------------------

## 5. SUGGESTED ORDER

  1. GROUP G + H (installable PWA + settings panel) - cheap now, everything
     later plugs into this storage/settings foundation
  2. GROUP A (core interaction model) - other charting groups build on this
  3. GROUP B (drawing tools) - highest daily-use visible value
  4. GROUP C + D (chart types + more indicators) - quick, same pattern as
     existing SMA/EMA/BB/RSI
  5. GROUP I + J (per-symbol memory + touch polish)
  6. GROUP K + L (rule builder + single-symbol backtest core) - the real
     start of "system testing," foundation for everything after
  7. GROUP M + N + O (stops, equity curve, position sizing) - extend the
     single-symbol backtester
  8. GROUP E + F (multi-chart linking + saved layouts) - polish once the
     above is solid
  9. GROUP P (portfolio-level backtest) - only after single-symbol is proven
 10. GROUP Q + R + S (scanning, categorization, alerts) - mostly wiring
     together pieces that already exist by this point

  Phase 2's two deferred items (historical backfill, confirmed cron) remain
  independent of all of the above and can be picked up whenever, in parallel.

================================================================================
