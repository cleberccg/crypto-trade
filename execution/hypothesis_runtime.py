from __future__ import annotations

from dataclasses import dataclass
import math
import re
from typing import Any

import pandas as pd

from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal


@dataclass(frozen=True)
class HypothesisApprovedContext:
    symbol: str
    timeframe: str
    trend_bucket: str | None = None
    vol_regime: str | None = None


@dataclass(frozen=True)
class HypothesisGateConfig:
    approved_filters: tuple[str, ...] = ()
    approved_contexts: tuple[HypothesisApprovedContext, ...] = ()
    regime: str | None = None


_RULE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(<=|>=|==|!=|<|>)\s*(.+?)\s*$")


def hypothesis_gate_config_from_payload(payload: dict[str, Any] | None) -> HypothesisGateConfig:
    if not isinstance(payload, dict):
        return HypothesisGateConfig()

    filters_raw = payload.get("approved_filters")
    contexts_raw = payload.get("approved_contexts")
    regime_raw = payload.get("regime")

    filters: list[str] = []
    if isinstance(filters_raw, (list, tuple)):
        for item in filters_raw:
            token = str(item or "").strip()
            if token:
                filters.append(token)

    contexts: list[HypothesisApprovedContext] = []
    if isinstance(contexts_raw, (list, tuple)):
        for row in contexts_raw:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").strip()
            timeframe = str(row.get("timeframe") or "").strip()
            if not symbol or not timeframe:
                continue
            contexts.append(
                HypothesisApprovedContext(
                    symbol=symbol,
                    timeframe=timeframe,
                    trend_bucket=(str(row.get("trend_bucket") or "").strip() or None),
                    vol_regime=(str(row.get("vol_regime") or "").strip() or None),
                )
            )

    regime = str(regime_raw).strip() if regime_raw is not None else None
    if regime == "":
        regime = None

    return HypothesisGateConfig(
        approved_filters=tuple(filters),
        approved_contexts=tuple(contexts),
        regime=regime,
    )


class HypothesisGatedStrategy(BaseStrategy):
    """External gate that enforces hypothesis rules before strategy entry."""

    def __init__(
        self,
        base_strategy: BaseStrategy,
        gate: HypothesisGateConfig,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> None:
        self._base = base_strategy
        self._gate = gate
        self._symbol = symbol
        self._timeframe = timeframe

    @property
    def name(self) -> str:
        return self._base.name

    def set_execution_context(self, *, symbol: str | None, timeframe: str | None) -> None:
        self._symbol = symbol
        self._timeframe = timeframe

    def initialize(self) -> None:
        self._base.initialize()

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        return self._base.calculate(df)

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        signal = self._base.entry_signal(df)
        if signal.signal != SignalType.BUY:
            return signal

        if not self._context_allows_entry():
            return StrategySignal(
                signal=SignalType.HOLD,
                price=signal.price,
                timestamp=signal.timestamp,
                score=signal.score,
                metadata={"hypothesis_gate": "context_rejected"},
            )

        if not self._regime_allows_entry(df):
            return StrategySignal(
                signal=SignalType.HOLD,
                price=signal.price,
                timestamp=signal.timestamp,
                score=signal.score,
                metadata={"hypothesis_gate": "regime_rejected"},
            )

        if not self._filters_allow_entry(df):
            return StrategySignal(
                signal=SignalType.HOLD,
                price=signal.price,
                timestamp=signal.timestamp,
                score=signal.score,
                metadata={"hypothesis_gate": "filter_rejected"},
            )

        return signal

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        return self._base.exit_signal(df, entry_price)

    def score(self, df: pd.DataFrame) -> float:
        return self._base.score(df)

    def prepare_dataset(
        self,
        df: pd.DataFrame,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
    ) -> pd.DataFrame:
        prepared = self._base.prepare_dataset(df, symbol=symbol, timeframe=timeframe)
        if symbol is not None:
            self._symbol = symbol
        if timeframe is not None:
            self._timeframe = timeframe
        return prepared

    def invalidate_prepared_dataset(self) -> None:
        self._base.invalidate_prepared_dataset()

    def cache_payload(self, name: str, payload: Any) -> None:
        self._base.cache_payload(name, payload)

    def cached_payload(self, name: str, default: Any = None) -> Any:
        return self._base.cached_payload(name, default)

    def execution_cache_signature(self) -> tuple[tuple[str, Any], ...]:
        base_sig = self._base.execution_cache_signature()
        gate_sig = (
            ("approved_filters", tuple(self._gate.approved_filters)),
            (
                "approved_contexts",
                tuple((c.symbol, c.timeframe, c.trend_bucket, c.vol_regime) for c in self._gate.approved_contexts),
            ),
            ("regime", self._gate.regime),
            ("symbol", self._symbol),
            ("timeframe", self._timeframe),
        )
        return base_sig + gate_sig

    def _context_allows_entry(self) -> bool:
        if not self._gate.approved_contexts:
            return True
        if not self._symbol or not self._timeframe:
            return False
        for ctx in self._gate.approved_contexts:
            if ctx.symbol == self._symbol and ctx.timeframe == self._timeframe:
                return True
        return False

    def _regime_allows_entry(self, df: pd.DataFrame) -> bool:
        expected = self._expected_regime()
        if expected is None:
            return True
        actual = self._detect_regime(df)
        if actual is None:
            return False
        exp_trend, exp_vol = expected
        act_trend, act_vol = actual
        if exp_trend and act_trend != exp_trend:
            return False
        if exp_vol and act_vol != exp_vol:
            return False
        return True

    def _filters_allow_entry(self, df: pd.DataFrame) -> bool:
        if not self._gate.approved_filters:
            return True
        if df.empty:
            return False
        row = df.iloc[-1]
        for rule in self._gate.approved_filters:
            if not _evaluate_rule(row, rule):
                return False
        return True

    def _expected_regime(self) -> tuple[str | None, str | None] | None:
        if self._symbol and self._timeframe and self._gate.approved_contexts:
            for ctx in self._gate.approved_contexts:
                if ctx.symbol == self._symbol and ctx.timeframe == self._timeframe:
                    if ctx.trend_bucket or ctx.vol_regime:
                        return (ctx.trend_bucket, ctx.vol_regime)

        if not self._gate.regime:
            return None
        return _parse_regime_hint(self._gate.regime)

    def _detect_regime(self, df: pd.DataFrame) -> tuple[str, str] | None:
        if df.empty:
            return None

        if "trend_bucket" in df.columns and "vol_regime" in df.columns:
            trend = str(df.iloc[-1].get("trend_bucket") or "").strip()
            vol = str(df.iloc[-1].get("vol_regime") or "").strip()
            if trend and vol:
                return trend, vol

        if "regime_key" in df.columns:
            regime_key = str(df.iloc[-1].get("regime_key") or "").strip()
            if "|" in regime_key:
                left, right = regime_key.split("|", 1)
                if left.strip() and right.strip():
                    return left.strip(), right.strip()

        if not all(col in df.columns for col in ("close", "high", "low")):
            return None

        close = pd.to_numeric(df["close"], errors="coerce")
        high = pd.to_numeric(df["high"], errors="coerce")
        low = pd.to_numeric(df["low"], errors="coerce")
        if close.empty or close.iloc[-1] == 0 or pd.isna(close.iloc[-1]):
            return None

        ema_fast = close.ewm(span=20, adjust=False).mean()
        ema_slow = close.ewm(span=50, adjust=False).mean()
        trend_score = (ema_fast - ema_slow) / close.replace(0, pd.NA)
        abs_trend = pd.to_numeric(trend_score, errors="coerce").abs().dropna()
        moderate_thr = float(abs_trend.quantile(0.55)) if not abs_trend.empty else 0.0006
        moderate_thr = max(moderate_thr, 0.0006)

        trend_value = float(trend_score.iloc[-1]) if not pd.isna(trend_score.iloc[-1]) else 0.0
        trend_bucket = "sideways"
        if trend_value >= moderate_thr:
            trend_bucket = "bullish"
        elif trend_value <= -moderate_thr:
            trend_bucket = "bearish"

        prev_close = close.shift(1)
        tr = pd.concat(
            [
                (high - low).abs(),
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)
        atr14 = tr.rolling(14, min_periods=8).mean()
        atr_pct = atr14 / close.replace(0, pd.NA)

        atr_clean = pd.to_numeric(atr_pct, errors="coerce").dropna()
        low_thr = float(atr_clean.quantile(0.30)) if not atr_clean.empty else 0.0
        high_thr = float(atr_clean.quantile(0.70)) if not atr_clean.empty else 0.0
        atr_last = float(atr_pct.iloc[-1]) if not pd.isna(atr_pct.iloc[-1]) else 0.0
        vol_regime = "normal_volatility"
        if atr_last <= low_thr:
            vol_regime = "low_volatility"
        elif atr_last >= high_thr:
            vol_regime = "high_volatility"

        return trend_bucket, vol_regime


def wrap_strategy_with_hypothesis(
    base_strategy: BaseStrategy,
    gate_config: HypothesisGateConfig,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> BaseStrategy:
    if not gate_config.approved_filters and not gate_config.approved_contexts and not gate_config.regime:
        return base_strategy
    return HypothesisGatedStrategy(
        base_strategy=base_strategy,
        gate=gate_config,
        symbol=symbol,
        timeframe=timeframe,
    )


def _parse_regime_hint(raw: str) -> tuple[str | None, str | None] | None:
    token = str(raw or "").strip().lower()
    if not token:
        return None
    if "|" in token:
        left, right = token.split("|", 1)
        return (left.strip() or None), (right.strip() or None)

    trend = None
    vol = None
    if "bull" in token:
        trend = "bullish"
    elif "bear" in token:
        trend = "bearish"
    elif "side" in token:
        trend = "sideways"

    if "high_volatility" in token:
        vol = "high_volatility"
    elif "low_volatility" in token:
        vol = "low_volatility"
    elif "normal_volatility" in token:
        vol = "normal_volatility"

    if trend is None and vol is None:
        return None
    return trend, vol


def _evaluate_rule(row: pd.Series, rule: str) -> bool:
    match = _RULE_RE.match(str(rule or ""))
    if not match:
        return False
    column, operator, raw_value = match.groups()
    if column not in row.index:
        return False

    left = row.get(column)
    if left is None or (isinstance(left, float) and math.isnan(left)):
        return False

    right = _parse_literal(raw_value)

    if operator in ("<", "<=", ">", ">="):
        left_num = _to_float(left)
        right_num = _to_float(right)
        if left_num is None or right_num is None:
            return False
        if operator == "<":
            return left_num < right_num
        if operator == "<=":
            return left_num <= right_num
        if operator == ">":
            return left_num > right_num
        return left_num >= right_num

    left_text = str(left)
    right_text = str(right)
    if operator == "==":
        return left_text == right_text
    if operator == "!=":
        return left_text != right_text
    return False


def _parse_literal(raw: str) -> Any:
    token = str(raw).strip()
    if len(token) >= 2 and ((token[0] == '"' and token[-1] == '"') or (token[0] == "'" and token[-1] == "'")):
        return token[1:-1]

    lowered = token.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    number = _to_float(token)
    if number is not None:
        return number
    return token


def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except (TypeError, ValueError):
        return None
