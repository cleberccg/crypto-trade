import time
from datetime import datetime, timezone

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.repositories import CandleRepository
from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.rsi import RSI
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy


class OldReversaoStrategy(ReversaoNextGenV1Strategy):
    """Reference implementation (pre-performance-fix) for equivalence checks."""

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        self._assert_initialized()
        result = df.copy()

        result[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]
        result[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]
        result["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]
        result["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]

        bb_df = self._bb.calculate(df)  # type: ignore[union-attr]
        result["bb_middle"] = bb_df["middle"]
        result["bb_upper"] = bb_df["upper"]
        result["bb_lower"] = bb_df["lower"]
        result["bb_percent_b"] = bb_df["percent_b"]

        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]
        result["trend_score"] = ((result[ema_fast_col] - result[ema_slow_col]) / result[ema_slow_col] * 100)

        result["atr_bucket"] = pd.qcut(
            result["atr"],
            q=3,
            labels=["low_atr", "mid_atr", "high_atr"],
            duplicates="drop",
        ).astype(str)

        result["relative_volume"] = result["volume"] / result["volume"].rolling(20).mean()
        result["volume_bucket"] = pd.cut(
            result["relative_volume"],
            bins=[-float("inf"), 0.9, 1.1, float("inf")],
            labels=["low_volume", "normal_volume", "high_volume"],
            include_lowest=True,
        ).astype(str)

        result["bollinger_position"] = result.apply(
            lambda row: self._get_bollinger_position(
                row["close"], row["bb_lower"], row["bb_middle"], row["bb_upper"]
            ),
            axis=1,
        )

        result["trend_score_prev"] = result["trend_score"].shift(1)
        result["regime_reversal"] = (
            (result["trend_score"] * result["trend_score_prev"] < 0)
            | ((result["trend_score"].abs() < 0.2) & (result["trend_score_prev"].abs() > 0.2))
        )

        return result


def load_df(limit_bars: int) -> pd.DataFrame:
    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(
            "BTC/USDT",
            "5m",
            datetime(2024, 1, 1, tzinfo=timezone.utc),
            datetime(2024, 12, 31, tzinfo=timezone.utc),
        )
    if limit_bars:
        candles = candles[:limit_bars]
    return pd.DataFrame(
        [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in candles],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )


def run_backtest(strategy_cls, params: dict, df: pd.DataFrame):
    strategy = strategy_cls(**params)
    strategy.initialize()
    # Keep runtime fair with current path (cache-precompute call)
    strategy.calculate(df.copy())
    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=10_000.0))
    return engine.run(df.copy(), symbol="BTC/USDT")


def main() -> None:
    params = {
        "ema_fast": 15,
        "ema_slow": 45,
        "rsi_period": 14,
        "atr_period": 14,
        "atr_stop_multiplier": 1.5,
        "risk_reward_ratio": 2.5,
        "score_min": 0.6,
        "volume_multiplier_min": 0.8,
        "atr_high_threshold": 1.0,
        "volume_low_threshold": 0.8,
    }

    df = load_df(limit_bars=4000)
    print(f"Dataset bars: {len(df)}")

    t0 = time.perf_counter()
    old_result = run_backtest(OldReversaoStrategy, params, df)
    t_old = time.perf_counter() - t0

    t1 = time.perf_counter()
    new_result = run_backtest(ReversaoNextGenV1Strategy, params, df)
    t_new = time.perf_counter() - t1

    old_trades = old_result.trades
    new_trades = new_result.trades

    same_trade_count = len(old_trades) == len(new_trades)
    same_trades = old_trades == new_trades
    same_metrics = old_result.metrics.to_dict() == new_result.metrics.to_dict()

    print("\n=== Equivalence Check ===")
    print(f"trade_count_old={len(old_trades)} trade_count_new={len(new_trades)}")
    print(f"same_trade_count={same_trade_count}")
    print(f"same_trades={same_trades}")
    print(f"same_metrics={same_metrics}")

    if not same_trades and same_trade_count:
        first_diff = None
        for i, (o, n) in enumerate(zip(old_trades, new_trades), start=1):
            if o != n:
                first_diff = (i, o, n)
                break
        print(f"first_trade_diff={first_diff}")

    print("\n=== Performance ===")
    print(f"old_time_s={t_old:.4f}")
    print(f"new_time_s={t_new:.4f}")
    if t_old > 0:
        gain = (t_old - t_new) / t_old * 100.0
        print(f"speedup_pct={gain:.2f}")

    status = same_trade_count and same_trades and same_metrics
    print(f"equivalence_passed={status}")


if __name__ == "__main__":
    main()
