# Vibe Trade — Project Map

Use this file to understand what each file does, how they connect,
and where to look when debugging.

---

## How The Bot Works (Big Picture)

```mermaid
flowchart TD
    subgraph Entry["Entry Points"]
        style Entry fill:#2563eb,color:#fff,stroke:#1d4ed8
        CLI["cli.py\nvibe-trade scan / start / panic"]
        MAIN["__main__.py"]
    end

    subgraph Config["Configuration"]
        style Config fill:#7c3aed,color:#fff,stroke:#6d28d9
        TOML["config/config.toml"]
        ENV[".env secrets"]
        CFG["config.py\nPydantic validation"]
    end

    subgraph Core["Core Engine"]
        style Core fill:#059669,color:#fff,stroke:#047857
        SCANNER["scanner.py\nOrchestrator"]
        SCHED["scheduler.py\nAPScheduler cron"]
    end

    subgraph Broker["Broker Layer"]
        style Broker fill:#ea580c,color:#fff,stroke:#c2410c
        IB["ib_broker.py\nib_async"]
        TWS["IB TWS / Gateway\npaper:7497 live:7496"]
    end

    subgraph Data["Data Layer"]
        style Data fill:#ea580c,color:#fff,stroke:#c2410c
        PROV["data/provider.py\nOHLCV candles"]
        UNIV["data/universe.py\nS&P 500 symbols"]
    end

    subgraph Strategy["Strategy Layer"]
        style Strategy fill:#059669,color:#fff,stroke:#047857
        BASE_S["strategy/base.py\nBaseStrategy ABC"]
        IND["strategy/indicators.py\nbacktrader engine"]
        MA["ma_crossover.py"]
        RSI["rsi_mean_revert.py"]
        REG["registry.py"]
    end

    subgraph Risk["Risk Layer"]
        style Risk fill:#dc2626,color:#fff,stroke:#b91c1c
        RMGR["risk/manager.py"]
        PSIZ["risk/position_sizer.py"]
        TRAIL["risk/trailing.py"]
        PANIC["risk/panic.py"]
    end

    subgraph Orders["Order Execution"]
        style Orders fill:#059669,color:#fff,stroke:#047857
        EXEC["orders/executor.py\nmarket orders only"]
    end

    subgraph Notify["Notifications"]
        style Notify fill:#7c3aed,color:#fff,stroke:#6d28d9
        TELE["notify/telegram.py"]
        CONS["notify/console.py"]
    end

    subgraph DB["Database"]
        style DB fill:#7c3aed,color:#fff,stroke:#6d28d9
        ENG["db/engine.py"]
        MOD["db/models.py"]
        REPO["db/repository.py"]
        SQLITE[("SQLite\nvibe_trade.db")]
    end

    MAIN --> CLI
    TOML --> CFG
    ENV --> CFG
    CLI --> CFG
    CLI --> SCANNER
    CLI --> SCHED
    SCHED --> SCANNER

    SCANNER --> IB
    IB --> TWS
    SCANNER --> PROV
    PROV --> IB
    SCANNER --> UNIV
    SCANNER --> REG
    REG --> MA & RSI
    MA & RSI --> IND
    MA & RSI --> BASE_S
    SCANNER --> RMGR
    RMGR --> PSIZ
    SCANNER --> TRAIL
    SCANNER --> EXEC
    EXEC --> IB
    EXEC --> REPO
    SCANNER --> TELE & CONS
    SCANNER --> REPO
    REPO --> MOD
    REPO --> ENG
    ENG --> SQLITE
    PANIC --> IB
```

---

## Scan Cycle Sequence

This is the exact flow inside `run_scan_cycle()` — the heart of the bot:

```mermaid
sequenceDiagram
    participant CLI as cli.py
    participant SC as scanner.py
    participant BR as IBBroker
    participant DP as DataProvider
    participant UN as Universe
    participant ST as Strategy
    participant RM as RiskManager
    participant EX as OrderExecutor
    participant TS as TrailingStop
    participant DB as Repository
    participant NF as Notifier

    CLI->>SC: run_scan_cycle(config, strategies, notifier)

    Note over SC: Step 1: Connect
    SC->>BR: connect()
    BR-->>SC: connected

    Note over SC: Step 2: Account State
    SC->>BR: get_account_summary()
    BR-->>SC: AccountSummary ($100k, positions)
    SC->>BR: get_positions()
    BR-->>SC: [Position, Position, ...]

    Note over SC: Step 3: Portfolio Risk Gate
    SC->>RM: check_portfolio_limits(account, positions)
    RM-->>SC: RiskDecision(approved=True)

    Note over SC: Step 4: Scan Universe
    SC->>UN: load_universe(config)
    UN-->>SC: ["AAPL", "MSFT", ... 500 symbols]

    loop For each symbol
        SC->>DP: get_candles(symbol, "1h", 60 days)
        DP->>BR: get_historical_bars()
        BR-->>DP: OHLCV bars
        DP-->>SC: DataFrame

        SC->>ST: evaluate(symbol, candles)
        Note over ST: compute_indicators() via backtrader
        ST-->>SC: SignalResult(BUY, confidence=0.7)

        SC->>DB: record_signal(symbol, strategy, BUY)
    end

    Note over SC: Step 5: Execute Signals
    loop For each BUY/SELL signal
        SC->>RM: check_trade(signal, account, positions)
        RM-->>SC: RiskDecision(approved=True)

        SC->>EX: execute_signal(signal, account)
        EX->>BR: place_market_order(AAPL, BUY, 15 shares)
        BR-->>EX: OrderResult(FILLED, $185.50)
        EX->>DB: create_trade(AAPL, BUY, $185.50, 15)
        EX-->>SC: OrderResult

        SC->>NF: notify_trade("Bought AAPL @ $185.50")
    end

    Note over SC: Step 6: Trailing Stops
    SC->>DB: get_open_trades()
    loop For each open trade
        SC->>BR: get_market_price(symbol)
        SC->>TS: evaluate_trailing_stop(trade, price, ATR)
        alt Stop triggered
            TS-->>SC: should_close=True
            SC->>BR: place_market_order(SELL)
            SC->>DB: close_trade()
            SC->>NF: notify_trade("Stop closed AAPL")
        else Tighten stop
            TS-->>SC: new_stop=$182.00
            SC->>DB: update_trailing_stop()
        end
    end

    Note over SC: Step 7: Daily P&L
    SC->>DB: upsert_daily(realized, unrealized, account_value)

    Note over SC: Step 8: Summary
    SC->>NF: notify_summary("Scanned: 500 | Signals: 12 | Orders: 2")
    SC->>BR: disconnect()
    SC->>DB: complete_scan(scan_id)
```

---

## Module Dependency Graph

Shows which modules import from which — so you know what breaks if you change a file:

```mermaid
graph LR
    config["config.py"]
    cli["cli.py"]
    scanner["scanner.py"]
    scheduler["scheduler.py"]

    ib["broker/ib_broker.py"]
    base_b["broker/base.py"]
    models_b["broker/models.py"]

    provider["data/provider.py"]
    universe["data/universe.py"]

    base_s["strategy/base.py"]
    indicators["strategy/indicators.py"]
    registry["strategy/registry.py"]
    ma["ma_crossover.py"]
    rsi["rsi_mean_revert.py"]

    manager["risk/manager.py"]
    sizer["risk/position_sizer.py"]
    trailing["risk/trailing.py"]
    panic["risk/panic.py"]

    executor["orders/executor.py"]

    base_n["notify/base.py"]
    console["notify/console.py"]
    telegram["notify/telegram.py"]

    engine["db/engine.py"]
    models_d["db/models.py"]
    repo["db/repository.py"]

    cli --> config
    cli --> scanner
    cli --> scheduler
    cli --> registry
    cli --> engine
    cli --> telegram
    cli --> console
    cli --> panic

    scanner --> config
    scanner --> ib
    scanner --> provider
    scanner --> universe
    scanner --> base_s
    scanner --> indicators
    scanner --> manager
    scanner --> trailing
    scanner --> executor
    scanner --> base_n
    scanner --> repo
    scanner --> engine
    scanner --> models_b

    scheduler --> config
    scheduler --> scanner

    ib --> base_b
    ib --> models_b
    ib --> config

    provider --> ib

    ma --> base_s
    ma --> indicators
    rsi --> base_s
    rsi --> indicators
    registry --> ma
    registry --> rsi
    registry --> config

    manager --> config
    manager --> sizer
    manager --> models_b

    trailing --> config

    executor --> ib
    executor --> repo
    executor --> sizer
    executor --> models_b

    panic --> ib

    console --> base_n
    telegram --> base_n

    repo --> models_d
    engine --> models_d

    style config fill:#7c3aed,color:#fff
    style cli fill:#2563eb,color:#fff
    style scanner fill:#059669,color:#fff
    style scheduler fill:#059669,color:#fff
    style ib fill:#ea580c,color:#fff
    style manager fill:#dc2626,color:#fff
    style sizer fill:#dc2626,color:#fff
    style trailing fill:#dc2626,color:#fff
    style panic fill:#dc2626,color:#fff
    style repo fill:#7c3aed,color:#fff
    style engine fill:#7c3aed,color:#fff
    style models_d fill:#7c3aed,color:#fff
```

---

## Database ER Diagram

```mermaid
erDiagram
    trades {
        int id PK
        string symbol
        string side "BUY or SELL"
        string strategy_name
        float entry_price
        datetime entry_time
        float exit_price
        datetime exit_time
        int quantity
        float trailing_stop
        string status "OPEN / CLOSED / CANCELLED"
        float pnl
        float pnl_pct
        int ib_order_id
        text notes
        datetime created_at
        datetime updated_at
    }

    signals {
        int id PK
        string symbol
        string strategy_name
        string signal_type "BUY / SELL / HOLD"
        float confidence
        text metadata_json
        bool risk_approved
        text risk_reason
        bool executed
        string scan_id FK
        datetime created_at
    }

    daily_pnl {
        int id PK
        date date UK
        float realized_pnl
        float unrealized_pnl
        float total_pnl
        int trades_opened
        int trades_closed
        float account_value
        datetime created_at
    }

    scan_log {
        int id PK
        string scan_id UK
        datetime started_at
        datetime completed_at
        int symbols_scanned
        int signals_generated
        int orders_placed
        text errors
        string status "STARTED / SUCCESS / PARTIAL / FAILED"
    }

    scan_log ||--o{ signals : "scan_id"
    trades ||--o{ signals : "symbol + strategy (logical)"
    daily_pnl ||--o{ trades : "date (logical)"
```

---

## Config Loading Flow

Shows how configuration is resolved — highest priority on top:

```mermaid
flowchart TD
    subgraph Sources["Config Sources (highest priority first)"]
        ENV_VAR["1. Environment Variables\nVIBE_TRADE_BROKER_ACCOUNT=DU12345"]
        ENV_FILE["2. .env File\nVIBE_TRADE_TELEGRAM_TOKEN=abc123"]
        TOML["3. config/config.toml\n[general]\nmode = 'paper'"]
        DEFAULTS["4. Pydantic Defaults\nconfig.py hardcoded defaults"]
    end

    ENV_VAR --> MERGE
    ENV_FILE --> MERGE
    TOML --> MERGE
    DEFAULTS --> MERGE

    MERGE["AppConfig(BaseSettings)\nPydantic merges all sources"]

    MERGE --> GEN["GeneralConfig\nmode, log_level, db_path"]
    MERGE --> BRK["BrokerConfig\nhost, ports, client_id, timeout"]
    MERGE --> UNI["UniverseConfig\nsource, custom_symbols"]
    MERGE --> SCH["SchedulerConfig\ninterval, market hours, trading days"]
    MERGE --> STR["StrategyConfig\nactive list, timeframe, per-strategy params"]
    MERGE --> RSK["RiskConfig\nmax risk %, max positions, trailing stop"]
    MERGE --> TEL["TelegramConfig\ntoken, chat_id, notification flags"]

    GEN --> SCANNER["scanner.py"]
    BRK --> BROKER["ib_broker.py"]
    UNI --> UNIVERSE["universe.py"]
    SCH --> SCHEDULER["scheduler.py"]
    STR --> STRATEGIES["registry.py\nma_crossover / rsi_mean_revert"]
    RSK --> RISK["risk/manager.py\nrisk/trailing.py\nrisk/position_sizer.py"]
    TEL --> NOTIFY["telegram.py"]

    style ENV_VAR fill:#dc2626,color:#fff
    style ENV_FILE fill:#ea580c,color:#fff
    style TOML fill:#2563eb,color:#fff
    style DEFAULTS fill:#6b7280,color:#fff
    style MERGE fill:#059669,color:#fff
```

---

## File-by-File Reference

### Root Files

| File | What It Does |
|------|-------------|
| `pyproject.toml` | Project metadata, dependencies, CLI entry point (`vibe-trade` command). Build system is hatchling. |
| `.gitignore` | Excludes: .venv, __pycache__, .env, config.toml, *.db, logs/, data/ |
| `.env.example` | Template for secrets (Telegram token, IB account). Copy to `.env`. |
| `config/config.example.toml` | Full config template with all sections and comments. Copy to `config/config.toml`. |

### Entry Points

| File | What It Does |
|------|-------------|
| `src/vibe_trade/__init__.py` | Package version (`__version__ = "0.1.0"`). |
| `src/vibe_trade/__main__.py` | Makes `python -m vibe_trade` work. Just calls `cli.app()`. |

### Config (`config.py`)

**What:** Loads `config/config.toml` + `.env` file, validates everything with Pydantic.

**Key classes:**
- `AppConfig` — top-level, holds all config sections
- `GeneralConfig` — mode (paper/live), log level, db path
- `BrokerConfig` — IB host, ports, client ID. `get_port(mode)` returns 7497 or 7496
- `UniverseConfig` — "sp500" or "custom" symbol list
- `SchedulerConfig` — interval, market hours, timezone, trading days
- `StrategyConfig` — which strategies are active, timeframe, per-strategy params
- `RiskConfig` — max risk per trade, max positions, exposure limits, trailing stop settings
- `TelegramConfig` — bot token, chat ID, notification preferences

**Validation:** Rejects negative percentages, invalid timeframes, bad time formats, slow_period <= fast_period, overbought <= oversold.

**Debug tip:** Run `vibe-trade config-check` to validate your config.

### CLI (`cli.py`)

**What:** Typer-based command line interface. All user-facing commands live here.

**Commands:**
| Command | What It Does |
|---------|-------------|
| `vibe-trade scan` | Run one scan cycle (connect to IB, scan, trade, disconnect) |
| `vibe-trade start` | Start the scheduler (periodic scans during market hours) |
| `vibe-trade status` | Show open positions + today's P&L from the database |
| `vibe-trade trades` | Show recent trades table |
| `vibe-trade config-check` | Validate config file and show key settings |
| `vibe-trade panic --yes` | EMERGENCY: close ALL positions via market orders |

**Debug tip:** Each command loads config, sets up logging, and initializes the DB independently.

### Scanner (`scanner.py`)

**What:** The orchestrator — `run_scan_cycle()` is the heart of the entire bot. It connects all modules together into one atomic scan.

**Flow:** Connect -> Account state -> Risk gates -> Scan universe -> Execute signals -> Trailing stops -> Daily P&L -> Notify -> Disconnect

**Depends on:** broker, data, strategy, risk, orders, notify, db (everything)

**Debug tip:** Each symbol is scanned in a try/except so one failure doesn't kill the whole scan. Check `scan_log` table for error history.

**Known issue:** Signal ID wiring is incomplete (line 124, `signal_id=0`).

### Scheduler (`scheduler.py`)

**What:** APScheduler wrapper. Fires `run_scan_cycle()` at configured intervals during market hours.

**How:** Uses `BlockingScheduler` with `CronTrigger`. Converts trading days (mon/tue/etc) to cron day numbers. Each scan runs in its own `asyncio.run()` call.

**Debug tip:** If the scheduler seems to not fire, check timezone config and market hours. It only runs between `market_open` and `market_close` on `trading_days`.

---

### Broker Module (`broker/`)

| File | What It Does |
|------|-------------|
| `broker/base.py` | Abstract `BaseBroker` interface. All broker implementations must have: `connect()`, `disconnect()`, `get_account_summary()`, `get_positions()`, `place_market_order()`, `get_market_price()`, `cancel_all_orders()` |
| `broker/models.py` | Data classes: `AccountSummary` (account value, buying power, P&L), `Position` (symbol, qty, cost, market value), `OrderRequest` (symbol, side, qty), `OrderResult` (order ID, status, fill price) |
| `broker/ib_broker.py` | Interactive Brokers implementation using `ib_async` library. Connects to TWS/Gateway. Paper = port 7497, Live = port 7496. Also has `get_historical_bars()` for fetching OHLCV data. |

**Debug tip:** If connection fails, check that TWS/IB Gateway is running and API connections are enabled in TWS settings (Edit > Global Configuration > API > Settings).

**Debug tip:** `place_market_order()` waits up to 5 seconds (10 x 0.5s) for fill. If IB is slow, the order may show as "SUBMITTED" instead of "FILLED" — it's still working, just not confirmed yet.

### Data Module (`data/`)

| File | What It Does |
|------|-------------|
| `data/provider.py` | `DataProvider` — fetches OHLCV candles from IB via the broker, returns pandas DataFrames. Maps timeframes: "1h" -> "1 hour", "4h" -> "4 hours", "1d" -> "1 day". |
| `data/universe.py` | Loads stock list. "sp500" returns ~500 hardcoded S&P 500 tickers. "custom" reads from `config.universe.custom_symbols`. |

**Debug tip:** IB has rate limits on historical data requests (~6 requests per 2 seconds). With 500 S&P symbols, a full scan will take several minutes. No rate limiting is implemented yet.

**Debug tip:** `universe.py` has a hardcoded S&P 500 list. It will go stale as companies are added/removed. Consider updating periodically.

### Strategy Module (`strategy/`)

| File | What It Does |
|------|-------------|
| `strategy/base.py` | Abstract `BaseStrategy` class + `SignalResult` dataclass + `SignalType` enum (BUY/SELL/HOLD). Every strategy must implement `name`, `required_candles`, and `evaluate()`. |
| `strategy/indicators.py` | Technical indicators powered by backtrader. `compute_indicators()` runs all indicators in one pass. Also has convenience functions: `sma()`, `ema()`, `rsi()`, `atr()`, `macd()`, `bollinger_bands()`. Each convenience function runs a full backtrader Cerebro — for performance, use `compute_indicators()` when you need multiple indicators. |
| `strategy/registry.py` | Maps config names to strategy classes. `load_strategies()` reads `config.strategy.active` list, instantiates each with its config params. To add a new strategy: add it to `STRATEGY_MAP` and add config wiring. |
| `strategy/examples/ma_crossover.py` | Moving average crossover. BUY when fast SMA crosses above slow SMA and price > fast SMA. SELL when fast crosses below. Trailing stop set at `price - 2*ATR`. |
| `strategy/examples/rsi_mean_revert.py` | RSI mean reversion. BUY when RSI crosses back above oversold level. SELL when RSI crosses back below overbought level. Trailing stop set at `price - 2*ATR`. |

**Debug tip:** To add your own strategy:
1. Create a new file in `strategy/examples/`
2. Extend `BaseStrategy`, implement `name`, `required_candles`, `evaluate()`
3. Add it to `STRATEGY_MAP` in `registry.py`
4. Add config section in `config.py` and `config.example.toml`

### Risk Module (`risk/`)

| File | What It Does |
|------|-------------|
| `risk/manager.py` | `RiskManager` — two checks: `check_portfolio_limits()` (max positions, max exposure %) and `check_trade()` (already holding symbol? concentration too high?). Returns `RiskDecision(approved=True/False, reason="...")`. |
| `risk/position_sizer.py` | `calculate_position_size()` — formula: `shares = (account * risk_pct / 100) / abs(entry - stop)`. Floors to whole shares. Caps at account value. Returns 0 if invalid. |
| `risk/trailing.py` | `evaluate_trailing_stop()` — checks each open trade: if price <= stop, close it. If price went up, tighten stop (never loosen). Uses ATR-based or percentage-based method from config. |
| `risk/panic.py` | `panic_close_all()` — cancels all open orders, then sells every position via market orders. Used by `vibe-trade panic`. |

**Debug tip:** Position sizer returns 0 (skip trade) if entry == stop price or if the position would exceed account value. Check logs for "Position size is 0" messages.

### Orders Module (`orders/`)

| File | What It Does |
|------|-------------|
| `orders/executor.py` | `OrderExecutor` — takes a `SignalResult`, calculates position size, places a market order via the broker, records the trade in the database. For SELL signals, looks up the existing open trade for that symbol. |

**Debug tip:** If a BUY signal has no `trailing_stop_price` in the SignalResult, the executor will skip it and log an error.

### Notify Module (`notify/`)

| File | What It Does |
|------|-------------|
| `notify/base.py` | Abstract `BaseNotifier` — four methods: `notify_trade()`, `notify_summary()`, `notify_error()`, `notify_panic()`. |
| `notify/console.py` | `ConsoleNotifier` — logs to stdout via Python logging. Always available as fallback. |
| `notify/telegram.py` | `TelegramNotifier` — sends messages to Telegram chat via bot API. Token/chat_id from config or env vars. Respects `notify_on_trade`, `notify_on_error`, `daily_summary` config flags. |

**Debug tip:** If Telegram is enabled but token is empty, messages are silently skipped (logged as warning).

### Database Module (`db/`)

| File | What It Does |
|------|-------------|
| `db/engine.py` | Creates SQLAlchemy engine + session factory. `init_db()` creates all tables on first run. Uses SQLite at the path from `config.general.db_path`. |
| `db/models.py` | Four ORM tables: `trades` (open/closed positions), `signals` (every signal generated), `daily_pnl` (one row per day), `scan_log` (one row per scan cycle). |
| `db/repository.py` | CRUD classes: `TradeRepository` (create, close, update stop, list), `SignalRepository` (record, mark executed), `DailyPnLRepository` (upsert daily), `ScanLogRepository` (start, complete). |

**Tables:**
| Table | Key Columns | Purpose |
|-------|-------------|---------|
| `trades` | symbol, side, entry/exit price, quantity, trailing_stop, status, pnl | Track every trade open to close |
| `signals` | symbol, strategy, signal_type, confidence, risk_approved, scan_id | Log every signal from every scan |
| `daily_pnl` | date, realized/unrealized pnl, account_value | One row per trading day |
| `scan_log` | scan_id, started/completed_at, symbols_scanned, errors | Audit trail for each scan cycle |

**Debug tip:** Database file lives at `data/vibe_trade.db`. You can open it with any SQLite browser (DB Browser for SQLite, DBeaver, etc.) to inspect trades and signals.

---

## Known Issues & TODOs

| Issue | File | Line | Description |
|-------|------|------|-------------|
| TODO | `scanner.py` | 124 | `signal_id=0` — signal ID not wired up after recording |
| MISSING | - | - | No Dockerfile |
| MISSING | - | - | No retry logic for IB connection failures |
| MISSING | - | - | No IB rate limiting for historical data requests |
| MISSING | - | - | No graceful shutdown handling |
| MISSING | - | - | No market holiday calendar |

**Status:**
- 41 config tests passing (`pytest tests/`)
- Git repo initialized
- Both strategies use `compute_indicators()` correctly (fixed from initial draft)

---

## Config Priority (highest wins)

1. Environment variables (`VIBE_TRADE_*`)
2. `.env` file
3. `config/config.toml`
4. Pydantic defaults in `config.py`

See the [Config Loading Flow diagram](#config-loading-flow) above for the visual version.
