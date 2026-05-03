# Session F — Notifications & Logging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire Telegram notifications into the three V2 cron jobs (submit / record / reconcile) and upgrade logging to JSON-to-file with daily rotation (7-day retention).

**Architecture:** All wiring lives in `cli.py`. Job functions stay pure. A small `_get_notifier(config)` helper returns Telegram or Console fallback. Three pure formatter helpers turn result objects into Telegram messages. `_setup_logging` gains a JSON formatter on a `TimedRotatingFileHandler`. Three new read-only repository methods feed reconcile's daily summary.

**Tech Stack:** Python 3.11, `python-telegram-bot` (already installed), `logging.handlers.TimedRotatingFileHandler`, pytest + pytest-asyncio.

**Spec:** [`docs/superpowers/specs/2026-05-03-session-f-notifications-logging-design.md`](../specs/2026-05-03-session-f-notifications-logging-design.md)

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `src/vibe_trade/db/repository.py` | Modify | 3 new read-only query methods |
| `src/vibe_trade/cli.py` | Modify | `_get_notifier`, `_setup_logging` upgrade, formatter helpers, wire 3 CLI wrappers, bug fixes |
| `tests/test_notify_integration.py` | Create | ~15 tests covering formatters, logging, notifier helper |
| `tests/TEST_REGISTRY.csv` | Modify | Append rows for new tests |

No new modules. No changes to job functions, notifier classes, or models.

---

## Task 1: Repository read methods for reconcile summary

The reconcile message needs three queries: trades opened today, trades closed today, today's DailyPnL row. Add them to existing repositories.

**Files:**
- Modify: `src/vibe_trade/db/repository.py`
- Test: `tests/test_db.py`

- [ ] **Step 1.1: Write failing test for `get_trades_opened_today`**

Append to `tests/test_db.py`:

```python
def test_get_trades_opened_today(in_memory_session):
    from datetime import date, datetime, timedelta
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.db.models import Trade

    repo = TradeRepository(in_memory_session)
    today = date(2026, 5, 3)

    # Opened today (entry_time on today, status OPEN)
    t_open_today = Trade(
        symbol="AAPL", side="BUY", strategy_name="donchian",
        requested_quantity=10, filled_quantity=10,
        entry_price=180.0, entry_time=datetime(2026, 5, 3, 9, 35),
        status="OPEN",
    )
    # Opened yesterday (excluded)
    t_open_yest = Trade(
        symbol="MSFT", side="BUY", strategy_name="donchian",
        requested_quantity=5, filled_quantity=5,
        entry_price=400.0, entry_time=datetime(2026, 5, 2, 9, 35),
        status="OPEN",
    )
    # Today but still SUBMITTED (excluded — not yet opened)
    t_submitted_today = Trade(
        symbol="GOOGL", side="BUY", strategy_name="donchian",
        requested_quantity=3,
        submitted_at=datetime(2026, 5, 3, 16, 0),
        status="SUBMITTED",
    )
    # Partially filled today (included)
    t_partial_today = Trade(
        symbol="NVDA", side="BUY", strategy_name="donchian",
        requested_quantity=10, filled_quantity=7,
        entry_price=900.0, entry_time=datetime(2026, 5, 3, 9, 36),
        status="PARTIALLY_FILLED",
    )
    in_memory_session.add_all([t_open_today, t_open_yest, t_submitted_today, t_partial_today])
    in_memory_session.commit()

    result = repo.get_trades_opened_today(today)
    symbols = sorted(t.symbol for t in result)
    assert symbols == ["AAPL", "NVDA"]
```

- [ ] **Step 1.2: Run test, verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_db.py::test_get_trades_opened_today -v`
Expected: FAIL with `AttributeError: 'TradeRepository' object has no attribute 'get_trades_opened_today'`.

- [ ] **Step 1.3: Implement `get_trades_opened_today`**

Add to `TradeRepository` in `src/vibe_trade/db/repository.py` after `get_pending_orders_for_today`:

```python
def get_trades_opened_today(self, today: date) -> list[Trade]:
    """Trades whose BUY filled today (status OPEN or PARTIALLY_FILLED, entry_time on `today`).

    Used by reconcile's Telegram summary. Read-only.
    """
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    return (
        self.session.query(Trade)
        .filter(
            Trade.status.in_(("OPEN", "PARTIALLY_FILLED")),
            Trade.entry_time >= start,
            Trade.entry_time <= end,
        )
        .all()
    )
```

- [ ] **Step 1.4: Run test, verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_db.py::test_get_trades_opened_today -v`
Expected: PASS.

- [ ] **Step 1.5: Write failing test for `get_trades_closed_today`**

Append to `tests/test_db.py`:

```python
def test_get_trades_closed_today(in_memory_session):
    from datetime import date, datetime
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.db.models import Trade

    repo = TradeRepository(in_memory_session)
    today = date(2026, 5, 3)

    # Closed today (exit_time on today, status CLOSED)
    t_closed_today = Trade(
        symbol="GOOGL", side="BUY", strategy_name="donchian",
        requested_quantity=3, filled_quantity=3,
        entry_price=2800.0, entry_time=datetime(2026, 4, 28, 9, 35),
        exit_price=2850.0, exit_time=datetime(2026, 5, 3, 9, 40),
        pnl=150.0, pnl_pct=0.0179,
        status="CLOSED",
    )
    # Closed yesterday (excluded)
    t_closed_yest = Trade(
        symbol="META", side="BUY", strategy_name="donchian",
        requested_quantity=4, filled_quantity=4,
        entry_price=500.0, entry_time=datetime(2026, 4, 25, 9, 35),
        exit_price=510.0, exit_time=datetime(2026, 5, 2, 9, 40),
        pnl=40.0, pnl_pct=0.02,
        status="CLOSED",
    )
    # Open with exit_time NULL (excluded)
    t_open = Trade(
        symbol="AAPL", side="BUY", strategy_name="donchian",
        requested_quantity=10, filled_quantity=10,
        entry_price=180.0, entry_time=datetime(2026, 5, 3, 9, 35),
        status="OPEN",
    )
    in_memory_session.add_all([t_closed_today, t_closed_yest, t_open])
    in_memory_session.commit()

    result = repo.get_trades_closed_today(today)
    symbols = [t.symbol for t in result]
    assert symbols == ["GOOGL"]
```

- [ ] **Step 1.6: Run test, verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_db.py::test_get_trades_closed_today -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 1.7: Implement `get_trades_closed_today`**

Add to `TradeRepository` immediately below `get_trades_opened_today`:

```python
def get_trades_closed_today(self, today: date) -> list[Trade]:
    """Trades whose SELL filled today (status CLOSED, exit_time on `today`).

    Used by reconcile's Telegram summary. Read-only.
    """
    start = datetime.combine(today, datetime.min.time())
    end = datetime.combine(today, datetime.max.time())
    return (
        self.session.query(Trade)
        .filter(
            Trade.status == "CLOSED",
            Trade.exit_time >= start,
            Trade.exit_time <= end,
        )
        .all()
    )
```

- [ ] **Step 1.8: Run test, verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_db.py::test_get_trades_closed_today -v`
Expected: PASS.

- [ ] **Step 1.9: Write failing test for `DailyPnLRepository.get_by_date`**

Append to `tests/test_db.py`:

```python
def test_dailypnl_get_by_date(in_memory_session):
    from datetime import date
    from vibe_trade.db.repository import DailyPnLRepository

    repo = DailyPnLRepository(in_memory_session)
    today = date(2026, 5, 3)

    # No row yet
    assert repo.get_by_date(today) is None

    # Insert via existing upsert_daily
    repo.upsert_daily(
        today=today,
        realized_pnl=124.30,
        unrealized_pnl=50.0,
        trades_opened=2,
        trades_closed=1,
        account_value=102_450.0,
    )
    record = repo.get_by_date(today)
    assert record is not None
    assert record.realized_pnl == 124.30
    assert record.account_value == 102_450.0
```

- [ ] **Step 1.10: Run test, verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_db.py::test_dailypnl_get_by_date -v`
Expected: FAIL with `AttributeError`.

- [ ] **Step 1.11: Implement `DailyPnLRepository.get_by_date`**

Add to `DailyPnLRepository` in `src/vibe_trade/db/repository.py` immediately after `upsert_daily`:

```python
def get_by_date(self, target_date: date) -> DailyPnL | None:
    """Read-only lookup of the DailyPnL row for `target_date`. Used by reconcile's
    Telegram summary."""
    return (
        self.session.query(DailyPnL)
        .filter(DailyPnL.date == target_date)
        .first()
    )
```

- [ ] **Step 1.12: Run all repository tests**

Run: `.venv/Scripts/python -m pytest tests/test_db.py -v`
Expected: ALL PASS, 3 new tests included.

- [ ] **Step 1.13: Append rows to TEST_REGISTRY.csv**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_db.py,unit,test_get_trades_opened_today,Filter trades by entry_time on given date and status OPEN/PARTIALLY_FILLED
test_db.py,unit,test_get_trades_closed_today,Filter trades by exit_time on given date and status CLOSED
test_db.py,unit,test_dailypnl_get_by_date,Read-only DailyPnL lookup by date returns None if missing
```

- [ ] **Step 1.14: Commit**

```bash
git add src/vibe_trade/db/repository.py tests/test_db.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Add read-only repo methods for reconcile Telegram summary

- TradeRepository.get_trades_opened_today
- TradeRepository.get_trades_closed_today
- DailyPnLRepository.get_by_date

3 new tests. All read-only, no side effects.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `_get_notifier` helper + fix panic bug

`cli.py:panic` references `_get_notifier(config)` but the helper is not defined. Implement it.

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Create: `tests/test_notify_integration.py`

- [ ] **Step 2.1: Create new test file with failing test**

Create `tests/test_notify_integration.py`:

```python
"""Tests for Session F: notification wiring and logging upgrade.

These tests are unit-level and require no IB connection or DB.
"""

from __future__ import annotations

import logging
from logging.handlers import TimedRotatingFileHandler

import pytest

from vibe_trade.config import AppConfig, TelegramConfig
from vibe_trade.notify.console import ConsoleNotifier
from vibe_trade.notify.telegram import TelegramNotifier


def test_get_notifier_returns_console_when_disabled():
    from vibe_trade.cli import _get_notifier

    config = AppConfig()
    config.telegram.enabled = False
    notifier = _get_notifier(config)
    assert isinstance(notifier, ConsoleNotifier)


def test_get_notifier_returns_telegram_when_enabled():
    from vibe_trade.cli import _get_notifier

    config = AppConfig()
    config.telegram.enabled = True
    config.telegram.token = "FAKE_TOKEN"
    config.telegram.chat_id = "12345"
    notifier = _get_notifier(config)
    assert isinstance(notifier, TelegramNotifier)
```

- [ ] **Step 2.2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v`
Expected: FAIL with `ImportError: cannot import name '_get_notifier' from 'vibe_trade.cli'`.

- [ ] **Step 2.3: Implement `_get_notifier`**

Add to `src/vibe_trade/cli.py` immediately after the `_setup_logging` function (around line 30):

```python
def _get_notifier(config) -> "BaseNotifier":  # noqa: F821 -- forward ref for clarity
    """Return Telegram if configured, else Console fallback (no-op-friendly).

    Console is also what the `panic` command falls back to in dev where
    Telegram credentials may not be set.
    """
    from vibe_trade.notify.base import BaseNotifier  # noqa: F401
    from vibe_trade.notify.console import ConsoleNotifier
    from vibe_trade.notify.telegram import TelegramNotifier

    if config.telegram.enabled:
        return TelegramNotifier(config.telegram)
    return ConsoleNotifier()
```

- [ ] **Step 2.4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v`
Expected: PASS.

- [ ] **Step 2.5: Append registry rows**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_notify_integration.py,unit,test_get_notifier_returns_console_when_disabled,Telegram off -> ConsoleNotifier returned
test_notify_integration.py,unit,test_get_notifier_returns_telegram_when_enabled,Telegram on -> TelegramNotifier returned
```

- [ ] **Step 2.6: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_notify_integration.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Add _get_notifier helper (fixes panic command bug)

Returns TelegramNotifier when enabled, ConsoleNotifier otherwise.
The panic command was already calling this helper but it was never
defined — calling panic would have raised NameError.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Logging upgrade (JSON to file + daily rotation)

Replace the plain `FileHandler` in `_setup_logging` with a `TimedRotatingFileHandler` that writes JSON. Stdout `StreamHandler` keeps its current plain format.

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Test: `tests/test_notify_integration.py`

- [ ] **Step 3.1: Write failing tests for logging setup**

Append to `tests/test_notify_integration.py`:

```python
def test_setup_logging_stream_handler_is_plain_text(tmp_path):
    from vibe_trade.cli import _setup_logging

    log_file = tmp_path / "vibe_trade.log"
    # Reset root logger between tests
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", str(log_file))

    stream_handlers = [
        h for h in root.handlers if isinstance(h, logging.StreamHandler)
        and not isinstance(h, logging.FileHandler)
    ]
    assert len(stream_handlers) == 1
    fmt_str = stream_handlers[0].formatter._fmt
    assert "%(asctime)s" in fmt_str
    assert "%(message)s" in fmt_str
    # Plain format does NOT use JSON
    assert "{" not in fmt_str


def test_setup_logging_file_handler_is_rotating(tmp_path):
    from vibe_trade.cli import _setup_logging

    log_file = tmp_path / "vibe_trade.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", str(log_file))

    file_handlers = [h for h in root.handlers if isinstance(h, TimedRotatingFileHandler)]
    assert len(file_handlers) == 1
    handler = file_handlers[0]
    assert handler.when == "MIDNIGHT"
    assert handler.backupCount == 7

    # Cleanup so the file lock releases before tmp_path is removed
    handler.close()


def test_setup_logging_file_handler_emits_json(tmp_path):
    import json
    from vibe_trade.cli import _setup_logging

    log_file = tmp_path / "vibe_trade.log"
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", str(log_file))
    logging.getLogger("test").info("hello world")

    # Flush + close handlers to release file lock on Windows
    for h in list(root.handlers):
        h.flush()
        h.close()
        root.removeHandler(h)

    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert payload["message"] == "hello world"
    assert "time" in payload


def test_setup_logging_no_file_when_log_file_none(tmp_path):
    from vibe_trade.cli import _setup_logging

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    _setup_logging("INFO", None)

    file_handlers = [h for h in root.handlers if isinstance(h, logging.FileHandler)]
    assert file_handlers == []
```

- [ ] **Step 3.2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "setup_logging"`
Expected: FAIL — current `_setup_logging` uses `FileHandler` not `TimedRotatingFileHandler`, and there's no JSON formatter.

- [ ] **Step 3.3: Implement upgraded `_setup_logging`**

Replace the `_setup_logging` function in `src/vibe_trade/cli.py` (currently around lines 22-29) with:

```python
class _JsonFormatter(logging.Formatter):
    """One JSON object per log record. Used only on the file handler."""

    def format(self, record: logging.LogRecord) -> str:
        import json
        from datetime import datetime

        payload = {
            "time": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def _setup_logging(level: str, log_file: str | None = None) -> None:
    """Configure root logger with plain stdout + JSON-rotating file handler.

    File handler rotates at midnight, keeps 7 backups (week of history).
    Database is the source of truth for historical analytics; logs are
    only for short-term operational debugging.
    """
    from logging.handlers import TimedRotatingFileHandler

    plain_fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    # Reset existing handlers so re-running CLI doesn't double-log
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    root.setLevel(getattr(logging, level, logging.INFO))

    stream = logging.StreamHandler()
    stream.setFormatter(logging.Formatter(plain_fmt))
    root.addHandler(stream)

    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_file,
            when="midnight",
            backupCount=7,
            encoding="utf-8",
        )
        file_handler.setFormatter(_JsonFormatter())
        root.addHandler(file_handler)
```

- [ ] **Step 3.4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "setup_logging"`
Expected: PASS for all 4 logging tests.

- [ ] **Step 3.5: Run full test suite**

Run: `.venv/Scripts/python -m pytest`
Expected: 215+ tests pass (212 prior + 3 from Task 1 + 4 here, with 2 from Task 2 = 221 actually). No regressions.

- [ ] **Step 3.6: Append registry rows**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_notify_integration.py,unit,test_setup_logging_stream_handler_is_plain_text,stdout handler uses plain format string (no JSON)
test_notify_integration.py,unit,test_setup_logging_file_handler_is_rotating,File handler is TimedRotatingFileHandler when=midnight backupCount=7
test_notify_integration.py,unit,test_setup_logging_file_handler_emits_json,Log records produce parseable JSON lines
test_notify_integration.py,unit,test_setup_logging_no_file_when_log_file_none,log_file=None means no file handler attached
```

- [ ] **Step 3.7: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_notify_integration.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Upgrade _setup_logging: JSON-to-file + daily rotation (7-day retention)

Stdout keeps plain text. File handler is TimedRotatingFileHandler with
midnight rotation and 7 backups. Each log record becomes one JSON line.

DB is the source of truth for analytics — logs only for short-term ops.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Submit message formatter + wiring

Pure formatter helper, then call it from `_run_submit_cli`.

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Test: `tests/test_notify_integration.py`

- [ ] **Step 4.1: Write failing tests for `_format_submit_msg`**

Append to `tests/test_notify_integration.py`:

```python
from datetime import date


def test_format_submit_msg_normal():
    from vibe_trade.cli import _format_submit_msg
    from vibe_trade.jobs.submit import SubmitResult

    result = SubmitResult(
        universe_size=100, held_count=5,
        exits_evaluated=5, exits_signaled=2, exits_placed=2, exits_failed=0,
        entries_evaluated=95, entries_signaled=3, entries_placed=3,
        entries_skipped_sizing=0, entries_failed=0,
    )
    msg = _format_submit_msg(result, date(2026, 5, 3))
    assert "[SUBMIT] 2026-05-03" in msg
    assert "Exits:   2 placed, 0 failed" in msg
    assert "Entries: 3 placed, 0 failed" in msg
    assert "error" not in msg.lower()


def test_format_submit_msg_with_errors():
    from vibe_trade.cli import _format_submit_msg
    from vibe_trade.jobs.submit import SubmitResult

    result = SubmitResult(
        exits_placed=2, exits_failed=1, entries_placed=3,
        errors=["exit AAPL: TimeoutError(...)", "exit MSFT: ValueError(...)"],
    )
    msg = _format_submit_msg(result, date(2026, 5, 3))
    assert "2 error(s):" in msg
    assert "exit AAPL: TimeoutError" in msg
    assert "exit MSFT: ValueError" in msg


def test_format_submit_msg_entries_skipped():
    from vibe_trade.cli import _format_submit_msg
    from vibe_trade.jobs.submit import SubmitResult

    result = SubmitResult(
        exits_evaluated=3, exits_placed=1,
        entries_phase_skipped=True, cap_reason="At max positions (50)",
    )
    msg = _format_submit_msg(result, date(2026, 5, 3))
    assert "Entries phase skipped: At max positions (50)" in msg
```

- [ ] **Step 4.2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "format_submit"`
Expected: FAIL with `ImportError`.

- [ ] **Step 4.3: Implement `_format_submit_msg`**

Add to `src/vibe_trade/cli.py` (place all formatter helpers together after `_get_notifier`):

```python
def _format_submit_msg(result, today) -> str:
    """Build the Telegram message for a submit run. Pure function."""
    lines = [f"[SUBMIT] {today.isoformat()}"]
    lines.append(
        f"Exits:   {result.exits_placed} placed, {result.exits_failed} failed"
    )
    if result.entries_phase_skipped:
        lines.append(f"Entries phase skipped: {result.cap_reason}")
    else:
        lines.append(
            f"Entries: {result.entries_placed} placed, {result.entries_failed} failed"
        )
    if result.errors:
        lines.append(f"{len(result.errors)} error(s):")
        for err in result.errors[:10]:
            lines.append(f"  - {err}")
    return "\n".join(lines)
```

- [ ] **Step 4.4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "format_submit"`
Expected: PASS.

- [ ] **Step 4.5: Wire into `_run_submit_cli`**

In `src/vibe_trade/cli.py`, modify `_run_submit_cli` to send the message after the result is printed. Locate the function (around line 47) and add notifier creation + send. Replace the function body with:

```python
async def _run_submit_cli(config) -> None:
    from datetime import date as _date

    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.data.provider import DataProvider
    from vibe_trade.data.universe import load_universe
    from vibe_trade.jobs.submit import SUBMIT_CLIENT_ID, run_submit
    from vibe_trade.risk.manager import RiskManager
    from vibe_trade.strategy.examples.donchian import DonchianStrategy

    broker_config = config.broker.model_copy()
    broker_config.client_id = SUBMIT_CLIENT_ID

    broker = IBBroker(broker_config, mode=config.general.mode)
    universe = load_universe(config.universe)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Submit[/bold] mode={config.general.mode} "
        f"client_id={SUBMIT_CLIENT_ID} universe_size={len(universe)}"
    )
    console.print(
        f"Connecting to {broker_config.host}:"
        f"{broker_config.get_port(config.general.mode)}..."
    )

    await broker.connect()
    try:
        result = await run_submit(
            broker=broker,
            strategy=DonchianStrategy(),
            data_provider=DataProvider(),
            risk_manager=RiskManager(config.risk),
            universe=universe,
            pct_per_position=config.risk.pct_per_position,
            max_positions=config.risk.max_open_positions,
        )
    finally:
        await broker.disconnect()

    _print_submit_summary(result)

    msg = _format_submit_msg(result, _date.today())
    if result.errors:
        await notifier.notify_error(msg)
    else:
        await notifier.notify_summary(msg)
```

- [ ] **Step 4.6: Write integration test for submit wiring**

Append to `tests/test_notify_integration.py`:

```python
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


def test_run_submit_cli_calls_notify_summary_on_success(monkeypatch):
    """_run_submit_cli should call notifier.notify_summary when result has no errors."""
    from vibe_trade.cli import _run_submit_cli
    from vibe_trade.jobs.submit import SubmitResult

    fake_notifier = MagicMock()
    fake_notifier.notify_summary = AsyncMock()
    fake_notifier.notify_error = AsyncMock()
    monkeypatch.setattr("vibe_trade.cli._get_notifier", lambda cfg: fake_notifier)

    fake_broker = MagicMock()
    fake_broker.connect = AsyncMock()
    fake_broker.disconnect = AsyncMock()
    monkeypatch.setattr("vibe_trade.broker.ib_broker.IBBroker", lambda *a, **kw: fake_broker)

    fake_run = AsyncMock(return_value=SubmitResult(exits_placed=1, entries_placed=2))
    monkeypatch.setattr("vibe_trade.jobs.submit.run_submit", fake_run)

    monkeypatch.setattr(
        "vibe_trade.data.universe.load_universe", lambda cfg: ["AAPL", "MSFT"]
    )

    config = AppConfig()
    asyncio.run(_run_submit_cli(config))

    fake_notifier.notify_summary.assert_called_once()
    fake_notifier.notify_error.assert_not_called()
```

- [ ] **Step 4.7: Run integration test**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py::test_run_submit_cli_calls_notify_summary_on_success -v`
Expected: PASS.

- [ ] **Step 4.8: Append registry rows**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_notify_integration.py,unit,test_format_submit_msg_normal,Submit message renders Exits/Entries counts with date header
test_notify_integration.py,unit,test_format_submit_msg_with_errors,Submit message lists up to 10 errors when present
test_notify_integration.py,unit,test_format_submit_msg_entries_skipped,Submit message shows cap_reason when entries phase skipped
test_notify_integration.py,integration,test_run_submit_cli_calls_notify_summary_on_success,Wire-through: _run_submit_cli calls notifier.notify_summary on no-error result
```

- [ ] **Step 4.9: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_notify_integration.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Wire Telegram notification into submit CLI

Adds _format_submit_msg pure helper + calls notifier.notify_summary
(or notify_error when errors are present) at end of _run_submit_cli.
Job function unchanged.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: Record message formatter + wiring

Same shape as Task 4. Smaller because record's result is simpler.

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Test: `tests/test_notify_integration.py`

- [ ] **Step 5.1: Write failing tests for `_format_record_msg`**

Append to `tests/test_notify_integration.py`:

```python
def test_format_record_msg():
    from vibe_trade.cli import _format_record_msg
    from vibe_trade.jobs.record import RecordResult

    result = RecordResult(buys_inserted=3, sells_flipped=2)
    msg = _format_record_msg(result, date(2026, 5, 3))
    assert "[RECORD] 2026-05-03" in msg
    assert "3 BUYs recorded, 2 SELLs flipped" in msg


def test_format_record_msg_with_errors():
    from vibe_trade.cli import _format_record_msg
    from vibe_trade.jobs.record import RecordResult

    result = RecordResult(buys_inserted=1, errors=["perm_id=42: ValueError(...)"])
    msg = _format_record_msg(result, date(2026, 5, 3))
    assert "1 error(s):" in msg
    assert "perm_id=42" in msg
```

- [ ] **Step 5.2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "format_record"`
Expected: FAIL with `ImportError`.

- [ ] **Step 5.3: Implement `_format_record_msg`**

Add to `src/vibe_trade/cli.py` next to `_format_submit_msg`:

```python
def _format_record_msg(result, today) -> str:
    """Build the Telegram message for a record run. Pure function."""
    lines = [
        f"[RECORD] {today.isoformat()}",
        f"{result.buys_inserted} BUYs recorded, "
        f"{result.sells_flipped} SELLs flipped",
    ]
    if result.errors:
        lines.append(f"{len(result.errors)} error(s):")
        for err in result.errors[:10]:
            lines.append(f"  - {err}")
    return "\n".join(lines)
```

- [ ] **Step 5.4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "format_record"`
Expected: PASS.

- [ ] **Step 5.5: Wire into `_run_record_cli`**

In `src/vibe_trade/cli.py`, modify `_run_record_cli` (around line 149) to add notifier wiring. Replace the function with:

```python
async def _run_record_cli(config) -> None:
    from datetime import date as _date

    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.db.engine import init_db as _init
    from vibe_trade.db.repository import TradeRepository
    from vibe_trade.jobs.record import run_record
    from vibe_trade.jobs.submit import RECORD_CLIENT_ID

    broker_config = config.broker.model_copy()
    broker_config.client_id = RECORD_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    session_factory = _init(config.general.db_path)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Record[/bold] mode={config.general.mode} "
        f"client_id={RECORD_CLIENT_ID}"
    )

    await broker.connect()
    session = session_factory()
    try:
        # Give ib_async a beat to hydrate the fill cache after connect.
        await asyncio.sleep(1.0)
        repo = TradeRepository(session)
        result = await run_record(broker=broker, repo=repo)
    finally:
        session.close()
        await broker.disconnect()

    table = Table(title="Record Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right")
    table.add_row("fills seen", str(result.fills_seen))
    table.add_row("unique permIds", str(result.perm_ids_seen))
    table.add_row("BUYs inserted", str(result.buys_inserted))
    table.add_row("BUYs skipped (dup)", str(result.buys_skipped_dup))
    table.add_row("SELLs flipped to PENDING_CLOSE", str(result.sells_flipped))
    table.add_row("SELLs skipped (dup)", str(result.sells_skipped_dup))
    table.add_row("SELLs skipped (no OPEN match)", str(result.sells_skipped_no_open))
    console.print(table)
    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/red]")
        for e in result.errors[:10]:
            console.print(f"  - {e}")

    msg = _format_record_msg(result, _date.today())
    if result.errors:
        await notifier.notify_error(msg)
    else:
        await notifier.notify_summary(msg)
```

- [ ] **Step 5.6: Run full test suite to confirm no regression**

Run: `.venv/Scripts/python -m pytest`
Expected: All pass.

- [ ] **Step 5.7: Append registry rows**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_notify_integration.py,unit,test_format_record_msg,Record message renders BUY/SELL counts with date header
test_notify_integration.py,unit,test_format_record_msg_with_errors,Record message lists errors when present
```

- [ ] **Step 5.8: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_notify_integration.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Wire Telegram notification into record CLI

Adds _format_record_msg pure helper + calls notifier.notify_summary
(or notify_error when errors present) at end of _run_record_cli.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Reconcile message formatter + wiring (the daily summary table)

The most complex of the three. Builds a monospace table of today's filled trades.

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Test: `tests/test_notify_integration.py`

- [ ] **Step 6.1: Write failing tests for `_format_reconcile_msg`**

Append to `tests/test_notify_integration.py`:

```python
def _make_trade(symbol, side, qty, **kwargs):
    """Lightweight builder: a stand-in shaped like vibe_trade.db.models.Trade."""
    from types import SimpleNamespace
    return SimpleNamespace(symbol=symbol, side=side, filled_quantity=qty, **kwargs)


def test_format_reconcile_msg_with_trades():
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    opened = [
        _make_trade("AAPL", "BUY", 10, pnl=None),
        _make_trade("MSFT", "BUY", 5, pnl=None),
    ]
    closed = [
        _make_trade("GOOGL", "BUY", 3, pnl=142.50),
        _make_trade("NVDA", "BUY", 2, pnl=-18.20),
    ]
    pnl = SimpleNamespace(realized_pnl=124.30, account_value=102_450.0)
    result = ReconcileResult(opened=2, closed=2)

    msg = _format_reconcile_msg(result, opened, closed, pnl, date(2026, 5, 3))
    # Header
    assert "[DAILY SUMMARY] 2026-05-03" in msg
    assert "Opened: 2" in msg
    assert "Closed: 2" in msg
    # Code block fencing
    assert "```" in msg
    # Rows
    assert "AAPL" in msg and "BUY" in msg
    assert "GOOGL" in msg and "+$142.50" in msg
    assert "NVDA" in msg and "-$18.20" in msg
    # Totals
    assert "Realized P&L: +$124.30" in msg
    assert "Account:" in msg and "$102,450.00" in msg


def test_format_reconcile_msg_no_trades():
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    result = ReconcileResult()
    msg = _format_reconcile_msg(result, [], [], None, date(2026, 5, 3))
    assert "No trades today." in msg


def test_format_reconcile_msg_errors():
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    result = ReconcileResult(errors=["perm_id=99: SomeError(...)"])
    msg = _format_reconcile_msg(result, [], [], None, date(2026, 5, 3))
    assert "1 error(s):" in msg
    assert "perm_id=99" in msg


def test_format_reconcile_msg_no_pnl_row():
    """When DailyPnL row is missing, omit the totals lines but still render trades."""
    from vibe_trade.cli import _format_reconcile_msg
    from vibe_trade.jobs.reconcile import ReconcileResult

    closed = [_make_trade("X", "BUY", 1, pnl=10.0)]
    result = ReconcileResult(closed=1)
    msg = _format_reconcile_msg(result, [], closed, None, date(2026, 5, 3))
    assert "X" in msg
    assert "Realized P&L:" not in msg
    assert "Account:" not in msg
```

(`SimpleNamespace` is imported once at top of test file: `from types import SimpleNamespace`. If not yet imported there, add it.)

- [ ] **Step 6.2: Run tests, verify they fail**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "reconcile_msg"`
Expected: FAIL with `ImportError`.

- [ ] **Step 6.3: Implement `_format_reconcile_msg`**

Add to `src/vibe_trade/cli.py` next to the other formatter helpers:

```python
def _format_reconcile_msg(result, opened, closed, pnl, today) -> str:
    """Build the daily summary message. Pure function.

    `opened`: list of Trade rows whose entry_time is today (BUYs only — V2 has no shorts).
    `closed`: list of Trade rows whose exit_time is today (each with `pnl` set).
    `pnl`: DailyPnL row for `today`, or None if reconcile didn't write one.
    """
    lines = [
        f"[DAILY SUMMARY] {today.isoformat()}",
        f"Opened: {result.opened}  Closed: {result.closed}",
        "",
    ]

    if not opened and not closed:
        lines.append("No trades today.")
    else:
        # Monospace block — Markdown parse_mode triple-backtick fencing
        lines.append("```")
        lines.append("Symbol  Side  Qty  P&L")
        lines.append("-" * 26)
        for t in opened:
            lines.append(f"{t.symbol:<7} BUY  {t.filled_quantity:>4}")
        for t in closed:
            sign = "+" if (t.pnl or 0) >= 0 else "-"
            amount = abs(t.pnl or 0)
            pnl_str = f"{sign}${amount:,.2f}"
            lines.append(
                f"{t.symbol:<7} SELL {t.filled_quantity:>4}  {pnl_str}"
            )
        lines.append("```")

    if pnl is not None:
        sign = "+" if (pnl.realized_pnl or 0) >= 0 else "-"
        rp = abs(pnl.realized_pnl or 0)
        lines.append("")
        lines.append(f"Realized P&L: {sign}${rp:,.2f}")
        if pnl.account_value is not None:
            lines.append(f"Account:    ${pnl.account_value:,.2f}")

    if result.errors:
        lines.append("")
        lines.append(f"{len(result.errors)} error(s):")
        for err in result.errors[:10]:
            lines.append(f"  - {err}")

    return "\n".join(lines)
```

- [ ] **Step 6.4: Run tests, verify they pass**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py -v -k "reconcile_msg"`
Expected: PASS.

- [ ] **Step 6.5: Wire into `_run_reconcile_cli`**

In `src/vibe_trade/cli.py`, modify `_run_reconcile_cli` (around line 211). Replace the function body with:

```python
async def _run_reconcile_cli(config) -> None:
    from datetime import date as _date

    from vibe_trade.broker.ib_broker import IBBroker
    from vibe_trade.db.engine import init_db as _init
    from vibe_trade.db.repository import (
        DailyPnLRepository,
        PortfolioSnapshotRepository,
        TradeRepository,
    )
    from vibe_trade.jobs.reconcile import run_reconcile
    from vibe_trade.jobs.submit import RECONCILE_CLIENT_ID

    broker_config = config.broker.model_copy()
    broker_config.client_id = RECONCILE_CLIENT_ID
    broker = IBBroker(broker_config, mode=config.general.mode)
    session_factory = _init(config.general.db_path)
    notifier = _get_notifier(config)

    console.print(
        f"[bold]Reconcile[/bold] mode={config.general.mode} "
        f"client_id={RECONCILE_CLIENT_ID}"
    )

    await broker.connect()
    session = session_factory()
    try:
        await asyncio.sleep(1.0)
        trade_repo = TradeRepository(session)
        daily_repo = DailyPnLRepository(session)
        result = await run_reconcile(
            broker=broker,
            trade_repo=trade_repo,
            snap_repo=PortfolioSnapshotRepository(session),
            daily_repo=daily_repo,
        )
        # Read for the summary message before closing the session
        today = _date.today()
        opened_today = trade_repo.get_trades_opened_today(today)
        closed_today = trade_repo.get_trades_closed_today(today)
        daily_row = daily_repo.get_by_date(today)
    finally:
        session.close()
        await broker.disconnect()

    table = Table(title="Reconcile Result")
    table.add_column("Metric", style="cyan")
    table.add_column("Count", justify="right")
    table.add_row("pending DB rows", str(result.pending_count))
    table.add_row("opened (SUBMITTED -> OPEN/PARTIAL)", str(result.opened))
    table.add_row("closed (PENDING_CLOSE -> CLOSED/PARTIAL)", str(result.closed))
    table.add_row("cancelled", str(result.cancelled))
    table.add_row("skipped (still working)", str(result.skipped_still_working))
    table.add_row("portfolio_snapshot rows", str(result.snapshot_rows))
    console.print(table)
    if result.errors:
        console.print(f"\n[red]{len(result.errors)} error(s):[/red]")
        for e in result.errors[:10]:
            console.print(f"  - {e}")

    msg = _format_reconcile_msg(result, opened_today, closed_today, daily_row, today)
    if result.errors:
        await notifier.notify_error(msg)
    else:
        await notifier.notify_summary(msg)
```

- [ ] **Step 6.6: Run full test suite to confirm no regression**

Run: `.venv/Scripts/python -m pytest`
Expected: All pass.

- [ ] **Step 6.7: Append registry rows**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_notify_integration.py,unit,test_format_reconcile_msg_with_trades,Reconcile message renders monospace table; BUYs no P&L; SELLs with signed P&L
test_notify_integration.py,unit,test_format_reconcile_msg_no_trades,Reconcile message shows 'No trades today.' when opened+closed both empty
test_notify_integration.py,unit,test_format_reconcile_msg_errors,Reconcile message appends error list when result.errors present
test_notify_integration.py,unit,test_format_reconcile_msg_no_pnl_row,Reconcile message omits totals lines when DailyPnL row is None
```

- [ ] **Step 6.8: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_notify_integration.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Wire Telegram daily summary into reconcile CLI

Adds _format_reconcile_msg with monospace table of today's filled
trades (BUYs sans P&L, SELLs with signed P&L). Calls notify_summary
(or notify_error when errors present) at end of _run_reconcile_cli.

Trade reads use the new repo methods on the existing session before
disconnect.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Fix `config-check` strategy.active bug

`config-check` references `config.strategy.active` but `AppConfig` has no `strategy` field. Remove the broken line so the command runs.

**Files:**
- Modify: `src/vibe_trade/cli.py`
- Test: `tests/test_notify_integration.py`

- [ ] **Step 7.1: Write failing test demonstrating the bug**

Append to `tests/test_notify_integration.py`:

```python
def test_config_check_does_not_reference_nonexistent_strategy_field():
    """config-check must not reference config.strategy.active — AppConfig has no strategy field."""
    import inspect
    from vibe_trade import cli

    source = inspect.getsource(cli.config_check)
    assert "config.strategy.active" not in source
```

- [ ] **Step 7.2: Run test, verify it fails**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py::test_config_check_does_not_reference_nonexistent_strategy_field -v`
Expected: FAIL — current `config_check` body still contains `config.strategy.active`.

- [ ] **Step 7.3: Remove the broken line in `config_check`**

In `src/vibe_trade/cli.py`, locate the `config_check` function (around line 649). Remove this single line from inside the try block:

```python
        console.print(f"  Strategies: {config.strategy.active}")
```

(It is the only line referencing `config.strategy`.)

- [ ] **Step 7.4: Run test, verify it passes**

Run: `.venv/Scripts/python -m pytest tests/test_notify_integration.py::test_config_check_does_not_reference_nonexistent_strategy_field -v`
Expected: PASS.

- [ ] **Step 7.5: Smoke-test `config-check` command runs end-to-end**

Run: `.venv/Scripts/python -m vibe_trade config-check`
Expected: prints "Config is valid" plus the remaining fields, no exception.

- [ ] **Step 7.6: Append registry row**

Append to `tests/TEST_REGISTRY.csv`:

```csv
test_notify_integration.py,unit,test_config_check_does_not_reference_nonexistent_strategy_field,Regression guard: config_check no longer references config.strategy.active (AppConfig has no strategy field)
```

- [ ] **Step 7.7: Commit**

```bash
git add src/vibe_trade/cli.py tests/test_notify_integration.py tests/TEST_REGISTRY.csv
git commit -m "$(cat <<'EOF'
Fix config-check: remove reference to non-existent strategy field

AppConfig has no `strategy` attribute (V1 leftover). Calling
config-check would have raised AttributeError. Regression test added.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Final verification + PROJECT_MASTER_STATE update

- [ ] **Step 8.1: Run full test suite**

Run: `.venv/Scripts/python -m pytest`
Expected: All tests pass. Total ≈ 212 + 16 = ~228.

- [ ] **Step 8.2: Smoke-test the CLI commands that didn't need IB**

```bash
.venv/Scripts/python -m vibe_trade --help
.venv/Scripts/python -m vibe_trade config-check
```
Both should run cleanly.

- [ ] **Step 8.3: Update `PROJECT_MASTER_STATE.md`**

Edit `PROJECT_MASTER_STATE.md` per the protocol in section 9:

- Update header: `Last updated: 2026-05-03 (end of Session F)`, new HEAD commit hash, new test count
- Section 2 "Done" table: add a row for Session F with the commit hash and outcome ("Telegram notifications wired into submit/record/reconcile + JSON-rotating logs")
- Section 2 "Not started": remove "Session F"
- Section 7 "Immediate next concrete deliverable": replace with Session G (cron deployment scaffolding)

- [ ] **Step 8.4: Commit the state update**

```bash
git add PROJECT_MASTER_STATE.md
git commit -m "$(cat <<'EOF'
Update PROJECT_MASTER_STATE.md for Session F completion

Session F (Telegram notifications + JSON-rotating logs) done.
Next: Session G — cron / systemd-timer deployment scaffolding.

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review Notes

**Spec coverage:**
- §2.1 `_get_notifier` → Task 2 ✓
- §2.2 `_setup_logging` JSON + rotation → Task 3 ✓
- §2.3 formatters → Tasks 4, 5, 6 ✓
- §3 message formats → Tasks 4, 5, 6 (test cases assert exact strings) ✓
- §4 new repo methods → Task 1 ✓
- §5 panic bug → Task 2; config-check bug → Task 7 ✓
- §7 test plan (~15) → 16 tests across Tasks 1–7 ✓

**Type/name consistency:**
- `_format_submit_msg(result, today)`, `_format_record_msg(result, today)`, `_format_reconcile_msg(result, opened, closed, pnl, today)` — names match between definition, tests, and call sites
- `get_trades_opened_today` / `get_trades_closed_today` / `DailyPnLRepository.get_by_date` — same names in repo, tests, and `_run_reconcile_cli`

**No placeholders:** All steps include exact code, file paths, and commands.
