"""Tests for vibe-trade report (reports/ module + CLI command)."""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta

import pytest

from vibe_trade.reports.data import ClosedTrade, DailyRow, HoldingRow
from vibe_trade.reports.metrics import (
    ClosedTradeStats,
    ReportMetrics,
    compute_closed_trade_stats,
    compute_metrics,
)


# ============================================================ compute_metrics


def _row(d: date, av: float | None, real: float = 0.0, unr: float = 0.0,
         pos: int | None = 50) -> DailyRow:
    return DailyRow(date=d, realized_pnl=real, unrealized_pnl=unr,
                    account_value=av, open_positions_count=pos)


def test_compute_metrics_empty_returns_zeroed_dataclass():
    m = compute_metrics([])
    assert m.sample_size == 0
    assert m.start_value is None
    assert m.end_value is None
    assert m.total_return_pct == 0.0
    assert m.cagr_pct is None
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
    assert m.best_day_pnl == 0.0
    assert m.worst_day_pnl == 0.0


def test_compute_metrics_single_row_has_zero_return_no_nan():
    m = compute_metrics([_row(date(2026, 5, 1), 100_000.0)])
    assert m.sample_size == 1
    assert m.start_value == 100_000.0
    assert m.end_value == 100_000.0
    assert m.total_return_pct == 0.0
    assert m.cagr_pct is None  # span < 1 day
    assert m.sharpe == 0.0
    assert m.max_drawdown_pct == 0.0
    assert not math.isnan(m.sharpe)


def test_compute_metrics_flat_account_value_gives_zero_sharpe():
    rows = [_row(date(2026, 5, d), 100_000.0) for d in (1, 2, 3, 4, 5)]
    m = compute_metrics(rows)
    assert m.sharpe == 0.0
    assert m.total_return_pct == 0.0
    assert m.max_drawdown_pct == 0.0


def test_compute_metrics_drops_rows_with_null_account_value():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), None),  # should be skipped
        _row(date(2026, 5, 3), 101_000.0),
    ]
    m = compute_metrics(rows)
    assert m.sample_size == 2
    assert m.start_value == 100_000.0
    assert m.end_value == 101_000.0


def test_compute_metrics_monotonically_increasing_positive_sharpe_no_dd():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 101_000.0),
        _row(date(2026, 5, 3), 102_000.0),
        _row(date(2026, 5, 4), 103_000.0),
        _row(date(2026, 5, 5), 104_000.0),
    ]
    m = compute_metrics(rows)
    assert m.total_return_pct > 0
    assert m.sharpe > 0
    assert m.max_drawdown_pct == 0.0
    assert m.max_dd_peak_date is None
    assert m.max_dd_trough_date is None


def test_compute_metrics_monotonically_decreasing_negative_return_and_dd():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 99_000.0),
        _row(date(2026, 5, 3), 98_000.0),
        _row(date(2026, 5, 4), 97_000.0),
    ]
    m = compute_metrics(rows)
    assert m.total_return_pct < 0
    assert m.max_drawdown_pct < 0
    assert m.max_dd_peak_date == date(2026, 5, 1)
    assert m.max_dd_trough_date == date(2026, 5, 4)


def test_compute_metrics_drawdown_identifies_correct_peak_and_trough():
    rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 2), 105_000.0),  # peak
        _row(date(2026, 5, 3), 102_000.0),
        _row(date(2026, 5, 4), 95_000.0),   # trough (worst from 105k peak)
        _row(date(2026, 5, 5), 98_000.0),
        _row(date(2026, 5, 6), 110_000.0),  # new high, drawdown resets
    ]
    m = compute_metrics(rows)
    # peak is the 105k bar on 5/2; trough is the 95k bar on 5/4
    assert m.max_dd_peak_date == date(2026, 5, 2)
    assert m.max_dd_trough_date == date(2026, 5, 4)
    assert m.max_drawdown_pct == pytest.approx((95_000 / 105_000 - 1) * 100, abs=1e-6)


def test_compute_metrics_best_worst_day_pnl_from_realized_plus_unrealized():
    rows = [
        _row(date(2026, 5, 1), 100_000.0, real=0.0, unr=500.0),   # +500
        _row(date(2026, 5, 2), 101_000.0, real=100.0, unr=900.0), # +1000 BEST
        _row(date(2026, 5, 3), 100_500.0, real=-50.0, unr=-450.0),# -500 WORST
        _row(date(2026, 5, 4), 100_800.0, real=0.0, unr=300.0),   # +300
    ]
    m = compute_metrics(rows)
    assert m.best_day_pnl == 1000.0
    assert m.worst_day_pnl == -500.0


def test_compute_metrics_known_fixture_sharpe_and_drawdown():
    # Hand-computed: account_value [100, 102, 101, 103, 105]
    # daily returns: 0.02, -0.00980392, 0.01980198, 0.01941748
    # mean = 0.01235389
    # variance (sample, N-1=3) = 2.182e-4 -> std = 0.014773
    # sharpe = mean/std * sqrt(252) = 0.8362 * 15.8745 ~= 13.27
    rows = [
        _row(date(2026, 5, 1), 100.0),
        _row(date(2026, 5, 2), 102.0),
        _row(date(2026, 5, 3), 101.0),
        _row(date(2026, 5, 4), 103.0),
        _row(date(2026, 5, 5), 105.0),
    ]
    m = compute_metrics(rows)
    assert m.sharpe == pytest.approx(13.27, abs=0.05)
    # drawdown: 101 vs peak 102 -> -0.98%
    assert m.max_drawdown_pct == pytest.approx(-0.9803921, abs=1e-4)
    # CAGR over 4 days, factor 1.05 -- huge annualized number
    assert m.cagr_pct is not None and m.cagr_pct > 100.0


# ============================================================ compute_closed_trade_stats


def _ct(symbol: str, entry: date, exit_: date, pnl: float) -> ClosedTrade:
    return ClosedTrade(
        symbol=symbol,
        entry_time=datetime.combine(entry, datetime.min.time()),
        exit_time=datetime.combine(exit_, datetime.min.time()),
        pnl=pnl,
        pnl_pct=None,
    )


def test_compute_closed_trade_stats_empty_returns_zeros_no_nan():
    s = compute_closed_trade_stats([])
    assert s.n == 0
    assert s.win_rate == 0.0
    assert s.avg_win == 0.0
    assert s.avg_loss == 0.0
    assert s.profit_factor == 0.0
    assert s.avg_holding_days == 0.0


def test_compute_closed_trade_stats_mixed_wins_and_losses():
    trades = [
        _ct("AAPL", date(2026, 5, 1), date(2026, 5, 11), pnl=+200.0),   # 10d
        _ct("MSFT", date(2026, 5, 2), date(2026, 5, 22), pnl=+400.0),   # 20d
        _ct("GOOG", date(2026, 5, 3), date(2026, 5, 18), pnl=-100.0),   # 15d
        _ct("AMZN", date(2026, 5, 4), date(2026, 5, 19), pnl=-300.0),   # 15d
    ]
    s = compute_closed_trade_stats(trades)
    assert s.n == 4
    assert s.win_rate == 0.5
    assert s.avg_win == 300.0           # (200+400)/2
    assert s.avg_loss == -200.0         # (-100 + -300)/2
    assert s.profit_factor == pytest.approx(600.0 / 400.0)
    assert s.avg_holding_days == pytest.approx((10 + 20 + 15 + 15) / 4)


def test_compute_closed_trade_stats_all_wins_profit_factor_inf():
    trades = [
        _ct("AAPL", date(2026, 5, 1), date(2026, 5, 5), pnl=+100.0),
        _ct("MSFT", date(2026, 5, 2), date(2026, 5, 6), pnl=+200.0),
    ]
    s = compute_closed_trade_stats(trades)
    assert s.n == 2
    assert s.win_rate == 1.0
    assert s.profit_factor == float("inf")


# ============================================================ load_daily_pnl


@pytest.fixture
def session():
    """Fresh in-memory SQLite session for each data-layer test."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from vibe_trade.db.models import Base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


def test_load_daily_pnl_respects_days_window(session):
    from vibe_trade.db.models import DailyPnL
    from vibe_trade.reports.data import load_daily_pnl

    today = date(2026, 5, 26)
    # Insert 10 rows, one every 6 calendar days, spanning ~54 days back
    for i in range(10):
        session.add(DailyPnL(
            date=today - timedelta(days=i * 6),
            realized_pnl=0.0, unrealized_pnl=0.0,
            account_value=100_000.0 + i * 100,
            open_positions_count=50,
        ))
    session.commit()

    rows = load_daily_pnl(session, days=30, today=today)
    # rows with date >= today-30 -> i*6 <= 30 -> i in {0,1,2,3,4,5} -> 6 rows
    assert len(rows) == 6
    # sorted by date ascending
    assert rows[0].date < rows[-1].date
    # all account_value is float
    assert all(isinstance(r.account_value, float) for r in rows)


# ============================================================ load_latest_holdings


def test_load_latest_holdings_returns_only_max_date_rows(session):
    from vibe_trade.db.models import PortfolioSnapshot
    from vibe_trade.reports.data import load_latest_holdings

    # Day 1: 2 holdings; Day 2: 3 holdings (the latest)
    older = date(2026, 5, 20)
    latest = date(2026, 5, 25)
    for sym in ("AAPL", "MSFT"):
        session.add(PortfolioSnapshot(
            date=older, symbol=sym, quantity=10,
            avg_cost=100.0, market_price=101.0,
            market_value=1010.0, unrealized_pnl=10.0,
        ))
    for sym in ("AAPL", "MSFT", "GOOG"):
        session.add(PortfolioSnapshot(
            date=latest, symbol=sym, quantity=20,
            avg_cost=100.0, market_price=105.0,
            market_value=2100.0, unrealized_pnl=100.0,
        ))
    session.commit()

    snapshot_date, holdings = load_latest_holdings(session)
    assert snapshot_date == latest
    assert len(holdings) == 3
    assert all(h.quantity == 20 for h in holdings)


def test_load_latest_holdings_empty_db_returns_none_and_empty_list(session):
    from vibe_trade.reports.data import load_latest_holdings

    snapshot_date, holdings = load_latest_holdings(session)
    assert snapshot_date is None
    assert holdings == []


# ============================================================ load_trade_activity / load_closed_trades


def _trade(session, symbol: str, entry: datetime, status: str = "OPEN",
           exit_: datetime | None = None, pnl: float | None = None):
    from vibe_trade.db.models import Trade
    session.add(Trade(
        symbol=symbol, side="BUY", strategy_name="donchian",
        entry_time=entry, exit_time=exit_,
        entry_price=100.0, exit_price=(110.0 if exit_ else None),
        requested_quantity=10, filled_quantity=10,
        status=status, pnl=pnl,
    ))


def test_load_trade_activity_groups_by_entry_date_within_window(session):
    from vibe_trade.reports.data import load_trade_activity

    today = date(2026, 5, 26)
    # 3 entries on 5/20, 2 entries on 5/25, 1 entry 60 days ago (outside window)
    for i in range(3):
        _trade(session, f"S{i}", datetime(2026, 5, 20, 14, i))
    for i in range(2):
        _trade(session, f"T{i}", datetime(2026, 5, 25, 14, i))
    _trade(session, "OLD", datetime(2026, 3, 1, 14, 0))
    session.commit()

    activity = load_trade_activity(session, days=30, today=today)
    assert activity == {date(2026, 5, 20): 3, date(2026, 5, 25): 2}


def test_load_closed_trades_filters_status_and_exit_time_window(session):
    from vibe_trade.reports.data import load_closed_trades

    today = date(2026, 5, 26)
    # CLOSED with exit_time inside window -> included
    _trade(session, "AAPL", datetime(2026, 5, 1, 14, 0),
           status="CLOSED",
           exit_=datetime(2026, 5, 20, 14, 0), pnl=200.0)
    # CLOSED but exit_time outside window -> excluded
    _trade(session, "OLD", datetime(2026, 3, 1, 14, 0),
           status="CLOSED",
           exit_=datetime(2026, 3, 20, 14, 0), pnl=100.0)
    # OPEN (no exit) -> excluded
    _trade(session, "MSFT", datetime(2026, 5, 10, 14, 0), status="OPEN")
    session.commit()

    closed = load_closed_trades(session, days=30, today=today)
    assert len(closed) == 1
    assert closed[0].symbol == "AAPL"
    assert closed[0].pnl == 200.0


# ============================================================ detect_outlier_days


def test_detect_outlier_days_flags_positions_zero_with_realized_nonzero():
    rows = [
        _row(date(2026, 5, 12), 100_000.0, real=0.0, unr=10.0, pos=50),     # normal
        _row(date(2026, 5, 13), 100_000.0, real=4056.0, unr=0.0, pos=0),    # OUTLIER
        _row(date(2026, 5, 14), 100_000.0, real=0.0, unr=20.0, pos=0),      # positions=0 but realized=0 -> not outlier
        _row(date(2026, 5, 15), 100_000.0, real=100.0, unr=0.0, pos=45),    # realized>0 but positions>0 -> not outlier
    ]
    from vibe_trade.reports.data import detect_outlier_days
    outliers = detect_outlier_days(rows)
    assert outliers == {date(2026, 5, 13)}


def test_data_loaders_on_empty_db_return_empty_containers(session):
    from vibe_trade.reports.data import (
        detect_outlier_days, load_closed_trades, load_daily_pnl,
        load_latest_holdings, load_trade_activity,
    )

    today = date(2026, 5, 26)
    assert load_daily_pnl(session, days=30, today=today) == []
    snap_date, holdings = load_latest_holdings(session)
    assert snap_date is None and holdings == []
    assert load_trade_activity(session, days=30, today=today) == {}
    assert load_closed_trades(session, days=30, today=today) == []
    assert detect_outlier_days([]) == set()


# ============================================================ render_report


def _holding(symbol: str, pnl: float) -> HoldingRow:
    return HoldingRow(
        symbol=symbol, quantity=10,
        avg_cost=100.0, market_price=100.0 + pnl / 10,
        market_value=1000.0 + pnl, unrealized_pnl=pnl,
    )


def test_render_report_full_data_emits_key_sections(capsys):
    from rich.console import Console

    from vibe_trade.reports.render import render_report

    today = date(2026, 5, 26)
    daily_rows = [
        _row(date(2026, 5, 1), 100_000.0),
        _row(date(2026, 5, 5), 102_000.0),
    ]
    holdings = [_holding("AAPL", +300.0), _holding("MSFT", -200.0)]
    metrics = compute_metrics(daily_rows)
    closed_stats = compute_closed_trade_stats([])
    console = Console(force_terminal=False, no_color=True, width=120)

    render_report(
        metrics=metrics,
        daily_rows=daily_rows,
        holdings=holdings,
        holdings_as_of=date(2026, 5, 5),
        activity={date(2026, 5, 1): 2, date(2026, 5, 5): 1},
        closed_stats=closed_stats,
        outliers=set(),
        window_days=30,
        today=today,
        console=console,
    )
    out = capsys.readouterr().out
    assert "Account value" in out
    assert "Sharpe" in out
    assert "AAPL" in out
    assert "no closed trades" in out  # since closed_stats.n == 0


def test_render_report_empty_prints_no_daily_pnl_sentinel(capsys):
    from rich.console import Console

    from vibe_trade.reports.render import render_report

    today = date(2026, 5, 26)
    metrics = compute_metrics([])
    closed_stats = compute_closed_trade_stats([])
    console = Console(force_terminal=False, no_color=True, width=120)

    render_report(
        metrics=metrics,
        daily_rows=[],
        holdings=[],
        holdings_as_of=None,
        activity={},
        closed_stats=closed_stats,
        outliers=set(),
        window_days=30,
        today=today,
        console=console,
    )
    out = capsys.readouterr().out
    assert "No daily P&L data" in out


# ============================================================ CLI integration


def test_cli_report_exits_zero_and_emits_header(tmp_path, monkeypatch):
    """End-to-end: seed a tmp DB, run `vibe-trade report --days 7 --config X`."""
    from typer.testing import CliRunner

    from vibe_trade.cli import app
    from vibe_trade.db.engine import init_db
    from vibe_trade.db.models import DailyPnL

    # 1. Build a minimal config file pointing at a tmp DB.
    # AppConfig has default_factory for every sub-model, so only the
    # general.db_path override is required.
    db_path = tmp_path / "report.db"
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        f'[general]\ndb_path = "{db_path.as_posix()}"\n'
    )

    # 2. Seed the tmp DB with a couple of daily_pnl rows.
    factory = init_db(str(db_path))
    s = factory()
    today = date.today()
    for i, av in enumerate([100_000.0, 101_000.0]):
        s.add(DailyPnL(
            date=today - timedelta(days=(1 - i)),
            realized_pnl=0.0, unrealized_pnl=10.0,
            account_value=av, open_positions_count=10,
        ))
    s.commit()
    s.close()

    # 3. Invoke the CLI.
    runner = CliRunner()
    result = runner.invoke(
        app, ["report", "--days", "7", "--config", str(config_path)],
    )
    assert result.exit_code == 0, result.output
    assert "vibe_trade report" in result.output
    assert "Account value" in result.output
