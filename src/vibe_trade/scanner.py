"""Scanner — the orchestrator that runs a complete scan cycle."""

from __future__ import annotations

import logging
import uuid
from datetime import date

from vibe_trade.broker.ib_broker import IBBroker
from vibe_trade.broker.models import OrderRequest
from vibe_trade.config import AppConfig
from vibe_trade.data.provider import DataProvider
from vibe_trade.data.universe import load_universe
from vibe_trade.db.engine import get_session_factory
from vibe_trade.db.repository import (
    DailyPnLRepository,
    ScanLogRepository,
    SignalRepository,
    TradeRepository,
)
from vibe_trade.notify.base import BaseNotifier
from vibe_trade.orders.executor import OrderExecutor
from vibe_trade.risk.manager import RiskManager
from vibe_trade.risk.trailing import evaluate_trailing_stop
from vibe_trade.strategy.base import BaseStrategy, SignalType
from vibe_trade.strategy.indicators import compute_indicators

logger = logging.getLogger(__name__)


async def run_scan_cycle(
    config: AppConfig,
    strategies: list[BaseStrategy],
    notifier: BaseNotifier,
) -> None:
    """Execute a complete scan cycle."""
    scan_id = str(uuid.uuid4())
    session_factory = get_session_factory(config.general.db_path)
    session = session_factory()

    trade_repo = TradeRepository(session)
    signal_repo = SignalRepository(session)
    daily_repo = DailyPnLRepository(session)
    scan_repo = ScanLogRepository(session)

    scan_repo.start_scan(scan_id)
    errors: list[str] = []
    signals_count = 0
    orders_count = 0

    broker = IBBroker(config.broker, mode=config.general.mode)

    try:
        # Step 1: Connect
        await broker.connect()

        # Step 2: Get account state
        account = await broker.get_account_summary()
        positions = await broker.get_positions()
        logger.info(
            f"Account: ${account.net_liquidation:,.2f} | "
            f"Positions: {len(positions)} | Mode: {config.general.mode}"
        )

        # Step 3: Check portfolio-level risk
        risk_mgr = RiskManager(config.risk)
        portfolio_check = risk_mgr.check_portfolio_limits(account, positions)

        # Step 4: Load universe and scan
        symbols = load_universe(config.universe)
        data_provider = DataProvider(broker)
        executor = OrderExecutor(broker, trade_repo, config.risk)

        pending_signals = []

        if portfolio_check.approved:
            for symbol in symbols:
                try:
                    candles = await data_provider.get_candles(
                        symbol=symbol,
                        timeframe=config.strategy.timeframe,
                        lookback_days=config.strategy.lookback_days,
                    )
                    if candles.empty:
                        continue

                    for strategy in strategies:
                        result = strategy.evaluate(symbol, candles)
                        signal_repo.record_signal(
                            symbol=symbol,
                            strategy_name=strategy.name,
                            signal_type=result.signal.value,
                            scan_id=scan_id,
                            confidence=result.confidence,
                            metadata=result.metadata,
                        )
                        signals_count += 1

                        if result.signal in (SignalType.BUY, SignalType.SELL):
                            pending_signals.append(result)

                except Exception as e:
                    logger.error(f"Error scanning {symbol}: {e}")
                    errors.append(f"{symbol}: {e}")
        else:
            logger.warning(f"Portfolio risk gate blocked: {portfolio_check.reason}")

        # Step 5: Risk-check and execute pending signals
        for signal in pending_signals:
            try:
                trade_check = risk_mgr.check_trade(signal, account, positions)
                if trade_check.approved:
                    result = await executor.execute_signal(signal, account)
                    if result and result.status == "FILLED":
                        orders_count += 1
                        await notifier.notify_trade(
                            f"{'Bought' if signal.signal == SignalType.BUY else 'Sold'} "
                            f"{signal.symbol} @ ${result.fill_price or 0:.2f} "
                            f"(strategy: {signal.strategy_name})"
                        )
                else:
                    logger.info(f"Risk rejected {signal.symbol}: {trade_check.reason}")
                    signal_repo.mark_executed(
                        signal_id=0,  # TODO: wire up signal ID properly
                        approved=False,
                        reason=trade_check.reason,
                    )
            except Exception as e:
                logger.error(f"Error executing {signal.symbol}: {e}")
                errors.append(f"Execute {signal.symbol}: {e}")

        # Step 6: Manage trailing stops on open trades
        open_trades = trade_repo.get_open_trades()
        for trade in open_trades:
            try:
                current_price = await broker.get_market_price(trade.symbol)
                candles = await data_provider.get_candles(
                    trade.symbol, config.strategy.timeframe, lookback_days=30
                )
                current_atr = compute_indicators(candles).atr.iloc[-1] if not candles.empty else None

                update = evaluate_trailing_stop(
                    trade, current_price, current_atr, config.risk.trailing_stop
                )

                if update.should_close:
                    logger.info(f"Trailing stop hit for {trade.symbol}: {update.reason}")
                    close_result = await broker.place_market_order(
                        OrderRequest(
                            symbol=trade.symbol,
                            side="SELL",
                            quantity=trade.quantity,
                        )
                    )
                    if close_result.fill_price:
                        trade_repo.close_trade(trade.id, close_result.fill_price)
                    orders_count += 1
                    await notifier.notify_trade(
                        f"Trailing stop closed {trade.symbol} @ ${close_result.fill_price or current_price:.2f} | {update.reason}"
                    )
                elif update.new_stop:
                    trade_repo.update_trailing_stop(trade.id, update.new_stop)

            except Exception as e:
                logger.error(f"Error managing trailing stop for {trade.symbol}: {e}")
                errors.append(f"Trailing {trade.symbol}: {e}")

        # Step 7: Update daily P&L
        try:
            today = date.today()
            open_trades = trade_repo.get_open_trades()
            realized = account.realized_pnl
            unrealized = account.unrealized_pnl
            daily_repo.upsert_daily(
                today=today,
                realized_pnl=realized,
                unrealized_pnl=unrealized,
                trades_opened=orders_count,
                trades_closed=sum(1 for t in open_trades if t.status == "CLOSED"),
                account_value=account.net_liquidation,
            )
        except Exception as e:
            logger.error(f"Error updating daily P&L: {e}")
            errors.append(f"Daily P&L: {e}")

        # Step 8: Send summary notification
        summary = (
            f"Scan complete | Scanned: {len(symbols)} | "
            f"Signals: {signals_count} | Orders: {orders_count} | "
            f"Open: {len(trade_repo.get_open_trades())} | "
            f"Account: ${account.net_liquidation:,.2f}"
        )
        if errors:
            summary += f" | Errors: {len(errors)}"
        await notifier.notify_summary(summary)
        logger.info(summary)

    except Exception as e:
        logger.error(f"Scan cycle failed: {e}")
        errors.append(f"Fatal: {e}")
        await notifier.notify_error(f"Scan cycle failed: {e}")

    finally:
        await broker.disconnect()
        scan_repo.complete_scan(
            scan_id=scan_id,
            symbols_scanned=len(symbols) if "symbols" in dir() else 0,
            signals_generated=signals_count,
            orders_placed=orders_count,
            errors=errors if errors else None,
        )
        session.close()
