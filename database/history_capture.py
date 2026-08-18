"""Helpers to capture historical execution data without changing core engines."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pandas as pd

from strategies.base_strategy import SignalType


@dataclass
class ExecutionHistoryRecorder:
    execution_id: str
    strategy: str
    symbol: str
    timeframe: str
    signals: list[dict[str, Any]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)

    def record_signal(
        self,
        signal_name: str,
        signal: Any,
        frame: pd.DataFrame,
        accepted: bool,
        rejection_reason: str | None = None,
        market_regime: str | None = None,
    ) -> None:
        last = frame.iloc[-1]
        timestamp = frame.index[-1]
        timestamp_value = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
        self.signals.append(
            {
                "execution_id": self.execution_id,
                "strategy": self.strategy,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "timestamp": timestamp_value,
                "signal": signal_name,
                "score": getattr(signal, "score", None),
                "entry_price": getattr(signal, "price", None),
                "stop_loss": getattr(signal, "stop_loss", None),
                "take_profit": getattr(signal, "take_profit", None),
                "rr": _extract_rr(signal),
                "accepted": accepted,
                "rejection_reason": rejection_reason,
                "market_regime": market_regime,
                "ema_fast": _safe_get(last, "ema_fast"),
                "ema_slow": _safe_get(last, "ema_slow"),
                "ema_trend": _safe_get(last, "ema_trend"),
                "rsi": _safe_get(last, "rsi"),
                "atr": _safe_get(last, "atr"),
                "volume": _safe_get(last, "volume"),
                "volume_average": _safe_get(last, "volume_average"),
                "close": _safe_get(last, "close"),
                "high": _safe_get(last, "high"),
                "low": _safe_get(last, "low"),
            }
        )

    def record_trade(self, trade: dict[str, Any]) -> None:
        record = dict(trade)
        record.setdefault("execution_id", self.execution_id)
        record.setdefault("strategy", self.strategy)
        record.setdefault("symbol", self.symbol)
        record.setdefault("timeframe", self.timeframe)
        self.trades.append(record)


class RecordingStrategyProxy:
    def __init__(self, strategy: Any, recorder: ExecutionHistoryRecorder) -> None:
        self._strategy = strategy
        self._recorder = recorder

    @property
    def name(self) -> str:
        return self._strategy.name

    def initialize(self) -> None:
        return self._strategy.initialize()

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._strategy.calculate(df)

    def entry_signal(self, df: pd.DataFrame):
        signal = self._strategy.entry_signal(df)
        self._recorder.record_signal(
            signal_name=str(getattr(signal, "signal", SignalType.HOLD).value if hasattr(signal, "signal") else signal),
            signal=signal,
            frame=df,
            accepted=getattr(signal, "signal", None) in {SignalType.BUY, SignalType.SELL},
            rejection_reason=None if getattr(signal, "signal", None) in {SignalType.BUY, SignalType.SELL} else "strategy_hold",
        )
        return signal

    def exit_signal(self, df: pd.DataFrame, entry_price: float):
        signal = self._strategy.exit_signal(df, entry_price)
        self._recorder.record_signal(
            signal_name=str(getattr(signal, "signal", SignalType.HOLD).value if hasattr(signal, "signal") else signal),
            signal=signal,
            frame=df,
            accepted=getattr(signal, "signal", None) in {SignalType.BUY, SignalType.SELL},
            rejection_reason=None if getattr(signal, "signal", None) in {SignalType.BUY, SignalType.SELL} else "strategy_hold",
        )
        return signal

    def score(self, df: pd.DataFrame) -> float:
        return self._strategy.score(df)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._strategy, item)


class RecordingRiskManagerProxy:
    def __init__(self, risk_manager: Any, recorder: ExecutionHistoryRecorder) -> None:
        self._risk_manager = risk_manager
        self._recorder = recorder

    def evaluate_trade(self, *args: Any, **kwargs: Any):
        try:
            return self._risk_manager.evaluate_trade(*args, **kwargs)
        except Exception:
            raise

    def check_trailing_stop(self, *args: Any, **kwargs: Any):
        return self._risk_manager.check_trailing_stop(*args, **kwargs)

    def __getattr__(self, item: str) -> Any:
        return getattr(self._risk_manager, item)


def _safe_get(row: Any, key: str) -> float | None:
    value = row.get(key)
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def _extract_rr(signal: Any) -> float | None:
    metadata = getattr(signal, "metadata", None)
    if isinstance(metadata, dict):
        rr = metadata.get("rr_ratio") or metadata.get("rr")
        try:
            return None if rr is None else float(rr)
        except (TypeError, ValueError):
            return None
    return None
