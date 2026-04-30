"""V2 backtesting framework.

Validates a strategy + sizing combo against historical S&P 500 data before
risking real money. See docs/ROADMAP.md (Session I) for context.

Modules:
- data:    yfinance fetch + CSV cache for historical bars and market caps
- engine:  day-by-day simulation loop (re-uses production strategy + sizer)
- metrics: sharpe / drawdown / win rate / etc. computed from equity curve
"""
