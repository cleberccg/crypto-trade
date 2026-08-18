"""
Backtesting result reporter - generates human-readable summaries and charts.
"""
from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from backtesting.engine import BacktestResult
from utils.logger import get_logger

logger = get_logger(__name__)

_RESULTS_DIR = Path(__file__).parent / "results"


class BacktestReporter:
    """
    Generates text summaries and equity curve charts for backtest results.

    Args:
        output_dir: Directory where reports and images are saved.
    """

    def __init__(self, output_dir: Path | None = None) -> None:
        self._output_dir = output_dir or _RESULTS_DIR
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def print_summary(self, result: BacktestResult) -> None:
        """Print a formatted performance summary to stdout."""
        print(f"\nStrategy : {result.strategy_name}")
        print(f"Symbol   : {result.symbol}")
        print(result.metrics)

    def save_equity_chart(self, result: BacktestResult) -> Path:
        """
        Save an equity curve chart as a PNG file.

        Args:
            result: Completed BacktestResult.

        Returns:
            Path to the saved image file.
        """
        fig, axes = plt.subplots(2, 1, figsize=(14, 8), sharex=True)
        fig.suptitle(
            f"{result.strategy_name} - {result.symbol}  "
            f"(Return: {result.metrics.return_pct:+.2%}  |  "
            f"Sharpe: {result.metrics.sharpe_ratio:.2f})",
            fontsize=13,
        )

        # Equity curve
        ax1 = axes[0]
        result.equity_curve.plot(ax=ax1, color="steelblue", linewidth=1.5)
        ax1.axhline(
            result.config.initial_capital,
            color="gray",
            linestyle="--",
            linewidth=0.8,
            label=f"Initial capital ({result.config.initial_capital:,.0f})",
        )
        ax1.set_ylabel("Portfolio Value")
        ax1.set_title("Equity Curve")
        ax1.legend()
        ax1.grid(alpha=0.3)

        # Drawdown
        ax2 = axes[1]
        peak = result.equity_curve.expanding().max()
        drawdown_pct = (result.equity_curve - peak) / peak * 100
        drawdown_pct.plot(ax=ax2, color="crimson", linewidth=1.2)
        ax2.fill_between(drawdown_pct.index, drawdown_pct.values, 0, color="crimson", alpha=0.3)
        ax2.set_ylabel("Drawdown (%)")
        ax2.set_title("Drawdown")
        ax2.grid(alpha=0.3)

        plt.tight_layout()

        filename = (
            f"{result.strategy_name}_{result.symbol.replace('/', '_')}_equity.png"
        )
        output_path = self._output_dir / filename
        fig.savefig(output_path, dpi=120, bbox_inches="tight")
        plt.close(fig)

        logger.info("Equity chart saved: %s", output_path)
        return output_path

    def save_trade_log(self, result: BacktestResult) -> Path:
        """
        Save a CSV log of all trades.

        Returns:
            Path to the saved CSV file.
        """
        if not result.trades:
            logger.warning("No trades to save.")
            return self._output_dir / "empty.csv"

        df = pd.DataFrame(result.trades)
        filename = (
            f"{result.strategy_name}_{result.symbol.replace('/', '_')}_trades.csv"
        )
        output_path = self._output_dir / filename
        df.to_csv(output_path, index=False)
        logger.info("Trade log saved: %s (%d rows)", output_path, len(df))
        return output_path
