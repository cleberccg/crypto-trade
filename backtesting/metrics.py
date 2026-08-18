"""
Backtesting metrics calculator.

Design decision: Metrics are computed from a list of closed trades and an
equity curve Series.  Pure functions - no I/O, no side effects.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from utils.metrics import expectancy_from_pnl, max_drawdown_from_equity_curve, profit_factor_from_pnl
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class BacktestMetrics:
    """
    Aggregated performance metrics for a completed backtest.

    Attributes:
        total_trades: Total number of closed trades.
        winning_trades: Trades with pnl > 0.
        losing_trades: Trades with pnl <= 0.
        win_rate: winning_trades / total_trades.
        gross_profit: Sum of all positive PnL values.
        gross_loss: Sum of all negative PnL values (positive number).
        net_profit: gross_profit - gross_loss.
        profit_factor: gross_profit / gross_loss.
        max_drawdown: Maximum peak-to-trough decline, reported as a positive magnitude.
        max_drawdown_pct: Max drawdown as a positive fraction of peak equity.
        avg_win: Average PnL of winning trades.
        avg_loss: Average loss of losing trades (positive number).
        expectancy: Expected PnL per trade.
        sharpe_ratio: Risk-adjusted return (annualised, assuming daily returns).
        initial_capital: Starting portfolio value.
        final_capital: Ending portfolio value.
        return_pct: Total return as percentage.
    """

    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    net_profit: float
    profit_factor: float
    max_drawdown: float
    max_drawdown_pct: float
    avg_win: float
    avg_loss: float
    expectancy: float
    sharpe_ratio: float
    initial_capital: float
    final_capital: float
    return_pct: float

    def to_dict(self) -> dict[str, float | int]:
        """Return metrics as a plain dictionary."""
        return {
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(self.win_rate, 4),
            "gross_profit": round(self.gross_profit, 2),
            "gross_loss": round(self.gross_loss, 2),
            "net_profit": round(self.net_profit, 2),
            "profit_factor": round(self.profit_factor, 4),
            "max_drawdown": round(self.max_drawdown, 2),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "avg_win": round(self.avg_win, 2),
            "avg_loss": round(self.avg_loss, 2),
            "expectancy": round(self.expectancy, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "initial_capital": round(self.initial_capital, 2),
            "final_capital": round(self.final_capital, 2),
            "return_pct": round(self.return_pct, 4),
        }

    def __str__(self) -> str:
        lines = [
            "=" * 50,
            "         BACKTEST RESULTS",
            "=" * 50,
            f"  Total trades       : {self.total_trades}",
            f"  Win rate           : {self.win_rate:.2%}",
            f"  Net profit         : {self.net_profit:+.2f}",
            f"  Return             : {self.return_pct:+.2%}",
            f"  Profit factor      : {self.profit_factor:.2f}",
            f"  Max drawdown       : {self.max_drawdown_pct:.2%}",
            f"  Sharpe ratio       : {self.sharpe_ratio:.2f}",
            f"  Expectancy         : {self.expectancy:.4f}",
            f"  Avg win / Avg loss : {self.avg_win:.2f} / {self.avg_loss:.2f}",
            "=" * 50,
        ]
        return "\n".join(lines)


def compute_metrics(
    trades: list[dict],
    equity_curve: pd.Series,
    initial_capital: float,
) -> BacktestMetrics:
    """
    Compute all backtest metrics from a list of trade results and an equity
    curve.

    Args:
        trades: List of dicts with at least a ``pnl`` key (float).
        equity_curve: Series of portfolio values indexed by time.
        initial_capital: Starting portfolio value.

    Returns:
        Populated BacktestMetrics dataclass.
    """
    pnl_values = [t["pnl"] for t in trades if "pnl" in t]

    total_trades = len(pnl_values)
    wins = [p for p in pnl_values if p > 0]
    losses = [p for p in pnl_values if p <= 0]

    winning_trades = len(wins)
    losing_trades = len(losses)
    win_rate = winning_trades / total_trades if total_trades else 0.0

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    net_profit = gross_profit - gross_loss
    profit_factor = profit_factor_from_pnl(pnl_values)

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    expectancy = expectancy_from_pnl(pnl_values)

    # --- Drawdown ---
    max_drawdown, max_drawdown_pct = max_drawdown_from_equity_curve(equity_curve)

    # --- Sharpe Ratio (annualised daily) ---
    daily_returns = equity_curve.pct_change().dropna()
    sharpe_ratio = 0.0
    if len(daily_returns) > 1 and daily_returns.std() > 0:
        sharpe_ratio = float(
            daily_returns.mean() / daily_returns.std() * np.sqrt(252)
        )

    final_capital = float(equity_curve.iloc[-1]) if len(equity_curve) > 0 else initial_capital
    return_pct = (final_capital - initial_capital) / initial_capital

    metrics = BacktestMetrics(
        total_trades=total_trades,
        winning_trades=winning_trades,
        losing_trades=losing_trades,
        win_rate=win_rate,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        net_profit=net_profit,
        profit_factor=profit_factor,
        max_drawdown=max_drawdown,
        max_drawdown_pct=max_drawdown_pct,
        avg_win=avg_win,
        avg_loss=avg_loss,
        expectancy=expectancy,
        sharpe_ratio=sharpe_ratio,
        initial_capital=initial_capital,
        final_capital=final_capital,
        return_pct=return_pct,
    )

    logger.info("Backtest metrics computed: %s", metrics)
    return metrics
