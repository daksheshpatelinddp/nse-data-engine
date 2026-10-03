================================================================================
  PHASE 3: AMIBROKER CHARTING ENGINE - FULL CLONE ROADMAP (v2)
  Built from AmiBroker's actual documentation: charting guide, drawing tools
  reference, chart sheets/layouts, preferences, categories, and watchlists.
  This version supersedes the v1 draft - expanded per detailed requirements.
================================================================================

## 0. SCOPE & SEQUENCING

Everything in this document must be substantially complete BEFORE starting:
scanning, exploration, backtesting, backtest results, or the AFL editor /
formula generator. Those are Phase 4+ and depend on the foundations below
(especially the category/watchlist system and the formula engine) already
existing.

Explicitly OUT OF SCOPE even for this expanded Phase 3 (confirmed not pure
charting, belongs to later phases or isn't applicable to this stack):
  - The AFL EDITOR itself (syntax-highlighted code editor, auto-complete,
    debugger) - Phase 4. What IS in scope here is the underlying FORMULA
    ENGINE that both chart overlays and the future editor will run on (see
    Part E) - building the engine now, the editor UI later, is the right
    order since the engine is the harder, foundational piece.
  - Full scanning/exploration/backtesting UI - Phase 4/5. What IS in scope:
    making categories/watchlists and Study IDs ready to BE the inputs those
    future features consume.
  - Multi-monitor floating windows, real-time tick refresh tuning, currency
    conversion - still out of scope, as noted in v1 (not applicable to a
    mobile-first, EOD-data app).

--------------------------------------------------------------------------------

## PART A: CHART NAVIGATION & INTERACTION

(Unchanged from v1 - scrolling, zooming, Y-axis drag/shrink/reset, log/linear
toggle, quote-selection crosshair with title-bar readout, range marking with
zoom-to-range. See suggested build order in Part I for where this lands.)

--------------------------------------------------------------------------------

## PART B: DRAWING TOOLS ENGINE

All tools from v1 remain (Trendline/Ray/Extended/H-line/V-line, Rectangle,
Triangle, Ellipse/Arc, Text Box, Fibonacci family, Gann family, Regression
Channels, Andrews' Pitchfork, Parallel Lines, Zig-Zag, Cycles, Arrow) - tiered
by build priority as before. Confirmed explicitly from your message: "at
least all default" tools, full set, not a subset.

### B.1 Study ID -> Formula Engine -> Scanning/Backtest (the critical link)

This is the piece that makes drawing tools more than decoration. Every
drawing gets a Study ID field (e.g. "F2" for a Fibonacci level, "T1" for a
trendline). The Formula Engine (Part E) can reference a drawing's current
value via a `study("ID")` -style function - meaning a hand-drawn trendline or
Fibonacci level becomes a usable input to:
  - A chart overlay formula ("plot a signal when price crosses my trendline")
  - A future scanner rule ("find symbols where price is within 2% of their
    drawn F2 level") - Phase 4/5, but the DATA MODEL must exist now
  - A future backtest rule using the same mechanism

Build requirement: drawings must be stored with enough structure (type,
anchor points in date/price space, Study ID, parameters) that a formula can
resolve "the current value of Study ID X" at any bar - not just rendered as
static pixels. This is the architectural difference between "a drawing" and
"a usable study," and it needs to be designed in from the start rather than
retrofitted.

--------------------------------------------------------------------------------

## PART C: MULTI-PANE & MULTI-SHEET SYSTEM (expanded)

### C.1 Panes - fully user-managed, not fixed
    - Add any number of panes to a sheet (not a fixed upper/lower split) -
      e.g. Price + Volume + RSI + MACD + a custom formula pane, all stacked
    - Remove any pane
    - Reorder panes (drag to reposition)
    - Resize panes (drag the divider between two panes, like AmiBroker's
      Y-axis drag behavior extended to pane-height dragging)
    - Each pane independently choose: chart type (for the main price pane)
      or indicator/formula (for sub-panes), its own Y-axis scale (linear/log,
      auto or manual range)

### C.2 Sheets - unlimited, user-managed
    - A "sheet" = one named, saved arrangement of panes (matches AmiBroker's
      chart-sheet-as-tab concept)
    - Add / remove sheets freely (not capped at 4-6 as v1 suggested - your
      requirement is explicitly "as much as user need")
    - Rename a sheet (tap-and-hold or long-press the tab, matches
      AmiBroker's right-click-to-rename)
    - Reorder sheets (drag tabs)
    - Each sheet is really a named pane-layout template - switching sheets
      on the SAME symbol instantly swaps the whole pane arrangement

### C.3 Sheet Lock
    - A per-sheet toggle that pins its pane arrangement so it can't be
      accidentally modified (add/remove/resize panes) until unlocked -
      protects a carefully-tuned analysis setup from a stray tap on mobile

### C.4 Sheet Link / Chart ID
    - "Chart ID": every open chart (tab) gets a stable identifier, independent
      of which symbol is currently loaded in it. This is what makes linking
      meaningful - AmiBroker's Study ID formulas use a chart ID to reference
      "the Fibonacci level drawn in chart 2" regardless of what symbol chart
      2 currently shows
    - "Sheet Link": a link-color mechanism (as in v1's 3C.3, now formalized
      under the sheet system) - linked charts change symbol and/or interval
      together. With Chart IDs in place, this also enables cross-chart
      formula references later (Phase 4+)

--------------------------------------------------------------------------------

## PART D: CHART TYPES & TIMEFRAME SYSTEM (expanded)

### D.1 Chart types
    - Candlestick, Line, Bar (OHLC), Area, Heikin-Ashi (as in v1)

### D.2 Timeframe / interval - fully customizable, mobile-optimized UI
    Base units, each independently customizable to any N:
      - Tick (N ticks per bar) - see note below on data availability
      - Minute (N minutes: 1, 5, 15, custom N)
      - Hourly (N hours)
      - Daily (N days)
      - Weekly (N weeks)
      - Monthly (N months)
      - Yearly (N years)
    UI requirement (explicit from your message): this must NOT be a row of
    buttons eating screen width - use a compact dropdown/menu system (e.g.
    one "Interval" dropdown that opens a picker: unit + N), matching your
    priority of maximizing actual chart area on a phone screen. The same
    principle applies to the chart-style selector and indicator toggles -
    consolidate into dropdowns/menus rather than always-visible button rows
    once the number of options grows (drawing tools especially - a toolbar
    of 20 icons is a desktop pattern, not a mobile one; a categorized
    drawing-tool picker menu is the right mobile equivalent).

    Data availability note: Daily/Weekly/Monthly/Yearly are all "free" -
    pure resampling of the EOD data you already have. Minute/Hourly/Tick
    intervals require an intraday data source you don't currently have
    (same constraint noted in v1) - the UI/menu system should be built to
    support them now (so adding a data source later is just "fill in the
    data"), but they'll show "no data available" until/unless that's added.

### D.3 Crosshair
    - Confirmed from your message: crosshair also gets its own compact
      menu/toggle rather than a persistent on-screen control, for the same
      screen-space reason as D.2.

--------------------------------------------------------------------------------

## PART E: CUSTOM FORMULA ENGINE (AFL-lite) - foundational, not optional

Your message is clear this needs to exist now, not deferred entirely to
Phase 4, because drawings' Study IDs (Part B.1) and category/watchlist-driven
analysis (Part F) both need SOMETHING to execute against.

### E.1 What this is NOT (yet)
    - Not the full AFL language (a 20+ year custom compiled DSL) - see the
      honest scoping from the original roadmap: not realistic to clone
      entirely
    - Not the AFL Editor UI (syntax highlighting, debugger) - Phase 4

### E.2 What this IS - the engine itself
    - A small, real expression language that runs client-side (JS, same
      place your SMA/EMA/RSI calculations already run) over OHLCV arrays:
      arithmetic, the indicators you already have (SMA/EMA/RSI/BB/MACD etc.
      from the earlier roadmap), comparison/crossover functions (`cross(a,
      b)`), and - critically - a `study("ID")` function that resolves a
      drawing's value at each bar (the link from Part B.1)
    - This engine is "built once, consumed by everything": chart overlays
      now, scanner rules later (Phase 4/5), backtest entry/exit rules later
      (Phase 4) - all the same formula execution, just different callers
    - A formula can be saved, named, and re-applied to any chart/symbol -
      this is the seed of the "automatic AFL generator" you mentioned as a
      later phase; the generator (whatever form that takes - likely a
      guided/templated formula builder rather than literal AFL code
      generation) sits ON TOP of this engine, once it exists

### E.3 Why this belongs in Phase 3, not Phase 4
    Scanning/backtesting (Phase 4) without a formula engine would mean
    building throwaway rule logic that gets rebuilt once the real engine
    exists. Building the engine now - even with a small initial function
    library - means Phase 4 is "build a UI on top of the engine," not
    "build the engine AND the UI."

--------------------------------------------------------------------------------

## PART F: DATABASE / CATEGORIES / WATCHLIST SYSTEM
    (modeled directly on AmiBroker's real system - verified against their
    actual documentation, not assumed)

### F.1 The two kinds of category (this distinction matters - copy it exactly)

**Exclusive-membership categories** (a symbol belongs to exactly ONE at a
time, matching AmiBroker precisely):
    - Market (e.g. NSE, BSE, US) - ties directly into the multi-market R2
      folder structure already planned (nse/, bse/, us/)
    - Group (user-defined, e.g. "Nifty 50", "Nifty 200")
    - Sector (user-defined or templated)
    - Industry (belongs to a sector, same parent-child relationship as
      AmiBroker: assigning a symbol to an industry automatically implies
      its sector)
    To move a symbol between, say, two sectors, you reassign it - you can't
    remove the assignment entirely without moving it to an explicit
    "Unassigned" bucket, exactly as AmiBroker does.

**Free-membership categories** (a symbol can belong to any number at once,
including zero):
    - Watchlists (unlimited, user-named)
    - Favorites (a special, always-present watchlist-like category)

**Special built-in view:**
    - "ALL" - every symbol in the current database, regardless of category

### F.2 Category Manager (UI)
    - Create / rename / delete Groups, Sectors, Industries, Watchlists
    - Assign a symbol to a category: single-symbol mode (pick one symbol,
      set its Market/Group/Sector/Industry) and bulk mode (select many
      symbols, reassign all at once) - matches AmiBroker's "Organize
      Assignments" dialog
    - For Watchlists specifically (free membership): add/remove a symbol
      to/from any number of watchlists via a multi-select checklist, exactly
      like AmiBroker's watchlist-selector popup

### F.3 Bulk import - confirmed format from your request, matches AmiBroker exactly
    - Accept `.txt`, `.tls`, or `.csv` files with ONE SYMBOL PER LINE, no
      other fields - this is AmiBroker's real `.TLS` format, so your
      instinct here is exactly right, not a simplification
    - Import target: any watchlist, or (for exclusive categories) a bulk
      "assign all these symbols to Sector X" action
    - Export: same format, one symbol per line, for backing up or sharing
      a watchlist

### F.4 Using categories/watchlists to open charts
    - Tapping a symbol inside a category/watchlist view opens it on the
      current (or a new) chart tab directly - matches your requirement
      exactly ("user opens symbol on chart direct from specific category or
      watchlist on click")

### F.5 The forward-looking requirement (why this matters beyond charting)
    Categories and watchlists must be built as a genuine, queryable DATA
    LAYER (not just a UI list) from day one, because Phase 4/5 scanning,
    exploration, and backtesting will all take "which symbols to run
    against" as an input - and that input IS a category or watchlist
    selection. Confirmed directly from your message: "user directly
    [selects a] category or watchlist in scanner, explorer, backtest... so
    use that category or watchlist for scanning, exploration or backtest of
    multiple symbols." Building this as a proper data layer now (symbol ->
    [market, group, sector, industry, watchlist-memberships]) means Phase 4
    just queries it, rather than needing categories rebuilt at that point.

--------------------------------------------------------------------------------

## PART G: SETTINGS & PREFERENCES SYSTEM

(Unchanged from v1 - charting defaults, drawing defaults, appearance/color,
tooltips - all still apply, now also covering: default sheet/pane layout for
new symbols, default timeframe, and category/watchlist display preferences
such as hiding empty watchlists, matching AmiBroker's own preference for
that.)

--------------------------------------------------------------------------------

## PART H: MOBILE UI PRINCIPLE (cross-cutting, applies to everything above)

Stated explicitly in your message and worth calling out as a standing design
rule for all of Part B-G, not a one-off: **options that would be an
always-visible toolbar on desktop become a dropdown/menu on mobile.** This
applies to: the drawing tool palette (20+ tools -> categorized picker, not a
20-icon row), timeframe/interval selection, crosshair toggle, and chart-style
selection. The goal stated directly: maximize actual chart/pane area on a
phone screen. Every new feature added from here should be evaluated against
this principle before deciding its UI placement.

--------------------------------------------------------------------------------

## PART I: REVISED SUGGESTED BUILD ORDER

Given the real dependency chain (category/watchlist data layer and the
formula engine are now foundational, not polish):

  1. Part F.1-F.2 (category/watchlist DATA LAYER + manager UI) - build this
     early even though it's not "charting" in the traditional sense, because
     everything else (opening charts from a watchlist, future scanning)
     depends on it existing
  2. Part C.1-C.2 (user-managed panes + sheets) - foundational structure
     that Part B (drawings) and Part D (chart types) both render into
  3. Part A (navigation polish) + Part G's charting-defaults settings
  4. Part B Tier 1 drawing tools (Select, lines, rectangle, text box) +
     Part B.1's Study ID data model (even before the formula engine
     consumes it, store it now)
  5. Part E.1-E.2 (formula engine core - arithmetic, existing indicators,
     cross(), study()) - unlocks real value from Study IDs already stored
  6. Part B Tier 2 (Fibonacci family, parallel lines, arrow)
  7. Part D.1-D.2 (more chart types, daily/weekly/monthly/yearly resampling,
     the dropdown-based timeframe UI)
  8. Part C.3-C.4 (sheet lock, sheet link, Chart ID) - once multi-sheet
     usage is a daily habit
  9. Part F.3-F.4 (bulk import/export, click-to-open polish)
  10. Part B Tier 3 then Tier 4 (specialist drawing tools) + Part G
      remainder (color/appearance polish) - built opportunistically

  Once steps 1-6 are solid, that's a reasonable "Phase 3 complete enough"
  milestone to begin Phase 4 (AFL editor UI, scanning, backtesting) in
  parallel with finishing steps 7-10.

================================================================================
