# Session F — Notifications & Logging Design

**Date:** 2026-05-03  
**Status:** Approved  
**Scope:** Wire Telegram notifications into submit/record/reconcile jobs + structured logging with rotation

---

## 1. Goals

- Each of the three V2 cron jobs sends one Telegram message per run
- Reconcile sends a formatted table of today's filled trades
- Log file uses JSON format; stdout stays plain text
- Logs rotate daily, 7-day retention — database is the source for historical data

---

## 2. Architecture

All changes live in **`src/vibe_trade/cli.py`**. The job functions (`run_submit`, `run_record`, `run_reconcile`) are not modified — they stay broker-agnostic and pure.

Three additions to `cli.py`:

### 2.1 `_get_notifier(config) -> BaseNotifier`

Returns `TelegramNotifier(config.telegram)` if `config.telegram.enabled`, otherwise `ConsoleNotifier()`. Fixes the existing bug where `panic` calls this helper but it is not defined.

### 2.2 `_setup_logging` upgrade

Two handlers on the root logger:

| Handler | Destination | Format | Rotation |
|---|---|---|---|
| `StreamHandler` | stdout | Plain text | None |
| `TimedRotatingFileHandler` | `logs/vibe_trade.log` | JSON (one object per line) | Midnight, 7 backups |

JSON log line shape:
```json
{"time": "2026-05-03T16:00:12", "level": "INFO", "logger": "vibe_trade.jobs.submit", "message": "submit start: 5 held..."}
```

Rotated files: `vibe_trade.log.2026-05-02`, etc.

No new config fields — `GeneralConfig.log_file` and `GeneralConfig.log_level` already exist.

### 2.3 Message formatters (private helpers in cli.py)

Pure functions that take result objects and return formatted strings. Testable without CLI or IB.

| Function | Input | Used by |
|---|---|---|
| `_format_submit_msg(result, today)` | `SubmitResult`, `date` | `_run_submit_cli` |
| `_format_record_msg(result, today)` | `RecordResult`, `date` | `_run_record_cli` |
| `_format_reconcile_msg(result, trades, pnl, today)` | `ReconcileResult`, `list[Trade]`, `DailyPnL\|None`, `date` | `_run_reconcile_cli` |

---

## 3. Notification Messages

Each job calls `notifier.notify_summary()` on success, `notifier.notify_error()` when `result.errors` is non-empty.

### Submit (16:00)
```
[SUBMIT] 2026-05-03
Exits:   2 placed, 0 failed
Entries: 3 placed, 0 failed
```
With errors:
```
[SUBMIT] 2026-05-03
Exits:   2 placed, 1 failed
Entries: 3 placed, 0 failed
1 error(s):
  - exit AAPL: TimeoutError(...)
```

### Record (16:25)
```
[RECORD] 2026-05-03
3 BUYs recorded, 2 SELLs flipped
```

### Reconcile (23:30) — daily summary
Sent as a monospace code block using `parse_mode="Markdown"` (triple-backtick fencing). MarkdownV2 is avoided — it requires escaping `$`, `.`, `+`, `-` which are common in trade messages.
```
[DAILY SUMMARY] 2026-05-03
Opened: 3  Closed: 2

Symbol  Side  Qty  P&L
──────────────────────
AAPL    BUY    10
MSFT    BUY     5
GOOGL   SELL    3  +$142.50
NVDA    SELL    2   -$18.20

Realized P&L: +$124.30
Account:    $102,450.00
```

Edge cases:
- No trades today → `"No trades today."` in place of the table
- `DailyPnL` row is None → omit the Realized P&L / Account lines
- Errors → appended after the table

---

## 4. Data Available for Reconcile Summary

`_run_reconcile_cli` already has access to `TradeRepository` and `DailyPnLRepository`. After `run_reconcile` returns, query:

- `repo.get_trades_opened_today(today)` — BUYs that transitioned to OPEN today (**new repo method**)
- `repo.get_trades_closed_today(today)` — SELLs that transitioned to CLOSED today (**new repo method**)
- `daily_repo.get_by_date(today)` — `DailyPnL` row with `realized_pnl`, `account_value` (**new repo method**)

These are read-only queries on the session already open in `_run_reconcile_cli`. Three new `TradeRepository` / `DailyPnLRepository` methods are required; they have no side effects and are straightforward `WHERE date = today` filters on existing columns.

---

## 5. Bugs Fixed

| Bug | Location | Fix |
|---|---|---|
| `_get_notifier` called but not defined | `cli.py:panic` | Implement the helper |
| `config.strategy.active` — `AppConfig` has no `strategy` field | `cli.py:config_check` | Remove or replace with available config fields |

---

## 6. Files Changed

| File | Change |
|---|---|
| `src/vibe_trade/cli.py` | `_get_notifier`, `_setup_logging` upgrade, formatter helpers, wired calls in `_run_*_cli`, bug fixes |
| `src/vibe_trade/db/repository.py` | 3 new read-only query methods: `get_trades_opened_today`, `get_trades_closed_today`, `DailyPnLRepository.get_by_date` |
| `tests/test_notify_integration.py` | New test file (~15 tests) |
| `tests/TEST_REGISTRY.csv` | New rows for all new tests |

No new source modules. No changes to job functions or notify module.

---

## 7. Tests (~15)

### Formatter tests (~8)
| Test | What it checks |
|---|---|
| `test_format_submit_msg_normal` | Placed orders, no errors → correct counts in message |
| `test_format_submit_msg_with_errors` | Errors → error lines appear in message |
| `test_format_submit_msg_entries_skipped` | Cap hit → entries-skipped line appears |
| `test_format_record_msg` | Buys + sells counts rendered correctly |
| `test_format_reconcile_msg_with_trades` | Table renders; BUYs have no P&L; SELLs have P&L |
| `test_format_reconcile_msg_no_trades` | "No trades today." fallback |
| `test_format_reconcile_msg_errors` | Errors appended after table |
| `test_format_reconcile_msg_no_pnl_row` | DailyPnL is None → account lines omitted |

### Logging tests (~4)
| Test | What it checks |
|---|---|
| `test_setup_logging_stream_handler_is_plain_text` | StreamHandler uses plain formatter |
| `test_setup_logging_file_handler_is_rotating` | File handler is `TimedRotatingFileHandler` |
| `test_setup_logging_file_handler_emits_json` | Log record produces valid JSON line |
| `test_setup_logging_no_file_when_log_file_none` | Only one handler when `log_file=None` |

### Notifier helper tests (~3)
| Test | What it checks |
|---|---|
| `test_get_notifier_returns_telegram_when_enabled` | `enabled=True` → `TelegramNotifier` |
| `test_get_notifier_returns_console_when_disabled` | `enabled=False` → `ConsoleNotifier` |
| `test_get_notifier_panic_bug_fixed` | `panic` can call `_get_notifier` without `NameError` |

---

## 8. Out of Scope

- Telegram bot for ad-hoc queries (Phase 5)
- Per-trade notifications during submit (too noisy; summary is enough)
- `notify_trade` method on `BaseNotifier` is not used by any job in this session
