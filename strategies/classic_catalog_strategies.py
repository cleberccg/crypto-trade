"""Classic public strategies reimplemented with the platform standard interface."""
from __future__ import annotations

from datetime import datetime

import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.macd import MACD
from indicators.rsi import RSI
from strategies.base_strategy import SignalType, StrategySignal
from strategies.families import QuantStrategy
from strategies.registry import register_strategy


def _ts(value: object) -> datetime:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return datetime.utcnow()
    return ts.to_pydatetime()


def _risk_levels(price: float) -> tuple[float, float]:
    stop_loss = price * (1.0 - settings.risk.default_stop_loss_pct)
    risk = max(price - stop_loss, 1e-9)
    take_profit = price + (risk * settings.risk.risk_reward_ratio)
    return stop_loss, take_profit


class _BaseClassicStrategy(QuantStrategy):
    @property
    def family(self) -> str:
        return "classic_catalog"

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        return StrategySignal(
            signal=SignalType.HOLD,
            price=float(last["close"]),
            timestamp=_ts(last.name),
            score=0.0,
            metadata={"entry_price": float(entry_price), "reason": "managed_by_engine_risk"},
        )


@register_strategy(
    name="ClassicEMACrossover",
    version="v1",
    family="classic_catalog",
    description="EMA crossover trend following.",
    parameters=["ema_fast", "ema_slow"],
    indicators=["EMA"],
    categories=["classic", "trend"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["ema_crossover_classic"],
)
class ClassicEMACrossoverStrategy(_BaseClassicStrategy):
    def __init__(self, ema_fast: int = 12, ema_slow: int = 26, **_: object) -> None:
        self._fast_period = int(ema_fast)
        self._slow_period = int(max(ema_slow, ema_fast + 1))
        self._ema_fast: EMA | None = None
        self._ema_slow: EMA | None = None

    @property
    def name(self) -> str:
        return "ClassicEMACrossover"

    def initialize(self) -> None:
        self._ema_fast = EMA(self._fast_period)
        self._ema_slow = EMA(self._slow_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out[self._ema_fast.name] = self._ema_fast.calculate(out)  # type: ignore[union-attr]
        out[self._ema_slow.name] = self._ema_slow.calculate(out)  # type: ignore[union-attr]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        fast = float(last[self._ema_fast.name])  # type: ignore[union-attr]
        slow = float(last[self._ema_slow.name])  # type: ignore[union-attr]
        if fast > slow:
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        fast = float(last[self._ema_fast.name])  # type: ignore[union-attr]
        slow = float(last[self._ema_slow.name])  # type: ignore[union-attr]
        return 1.0 if fast > slow else 0.0


@register_strategy(
    name="ClassicSMACrossover",
    version="v1",
    family="classic_catalog",
    description="SMA crossover trend following.",
    parameters=["sma_fast", "sma_slow"],
    indicators=["SMA"],
    categories=["classic", "trend"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["sma_crossover_classic"],
)
class ClassicSMACrossoverStrategy(_BaseClassicStrategy):
    def __init__(self, sma_fast: int = 10, sma_slow: int = 30, **_: object) -> None:
        self._fast = int(sma_fast)
        self._slow = int(max(sma_slow, sma_fast + 1))

    @property
    def name(self) -> str:
        return "ClassicSMACrossover"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma_fast"] = out["close"].rolling(self._fast).mean()
        out["sma_slow"] = out["close"].rolling(self._slow).mean()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if float(last.get("sma_fast", 0.0)) > float(last.get("sma_slow", 0.0)):
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last.get("sma_fast", 0.0)) > float(last.get("sma_slow", 0.0)) else 0.0


@register_strategy(
    name="ClassicMACDTrend",
    version="v1",
    family="classic_catalog",
    description="MACD trend confirmation strategy.",
    parameters=["macd_fast", "macd_slow", "macd_signal"],
    indicators=["MACD"],
    categories=["classic", "momentum"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["macd_trend_classic"],
)
class ClassicMACDTrendStrategy(_BaseClassicStrategy):
    def __init__(self, macd_fast: int = 12, macd_slow: int = 26, macd_signal: int = 9, **_: object) -> None:
        self._macd = MACD(macd_fast, max(macd_slow, macd_fast + 1), macd_signal)

    @property
    def name(self) -> str:
        return "ClassicMACDTrend"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        macd_df = self._macd.calculate(out)
        out["macd"] = macd_df["macd"]
        out["signal"] = macd_df["signal"]
        out["histogram"] = macd_df["histogram"]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if float(last.get("macd", 0.0)) > float(last.get("signal", 0.0)) and float(last.get("histogram", 0.0)) > 0:
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last.get("macd", 0.0)) > float(last.get("signal", 0.0)) else 0.0


@register_strategy(
    name="ClassicRSIMeanReversion",
    version="v1",
    family="classic_catalog",
    description="RSI oversold mean reversion.",
    parameters=["rsi_period", "rsi_buy"],
    indicators=["RSI"],
    categories=["classic", "mean_reversion"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["rsi_mean_reversion_classic"],
)
class ClassicRSIMeanReversionStrategy(_BaseClassicStrategy):
    def __init__(self, rsi_period: int = 14, rsi_buy: float = 30.0, **_: object) -> None:
        self._rsi_period = int(rsi_period)
        self._rsi_buy = float(rsi_buy)
        self._rsi: RSI | None = None

    @property
    def name(self) -> str:
        return "ClassicRSIMeanReversion"

    def initialize(self) -> None:
        self._rsi = RSI(self._rsi_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out[self._rsi.name] = self._rsi.calculate(out)  # type: ignore[union-attr]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        rsi_val = float(last[self._rsi.name])  # type: ignore[union-attr]
        if rsi_val <= self._rsi_buy:
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last[self._rsi.name]) <= self._rsi_buy else 0.0  # type: ignore[union-attr]


@register_strategy(
    name="ClassicBollingerReversal",
    version="v1",
    family="classic_catalog",
    description="Bollinger lower-band reversion strategy.",
    parameters=["bb_period", "bb_std"],
    indicators=["BollingerBands"],
    categories=["classic", "mean_reversion"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["bollinger_reversal_classic"],
)
class ClassicBollingerReversalStrategy(_BaseClassicStrategy):
    def __init__(self, bb_period: int = 20, bb_std: float = 2.0, **_: object) -> None:
        self._bb = BollingerBands(period=bb_period, std_dev=bb_std)

    @property
    def name(self) -> str:
        return "ClassicBollingerReversal"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        bb = self._bb.calculate(out)
        out["bb_upper"] = bb["upper"]
        out["bb_lower"] = bb["lower"]
        out["bb_percent_b"] = bb["percent_b"]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        lower = float(last.get("bb_lower", price))
        if price <= lower:
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last["close"]) <= float(last.get("bb_lower", float("inf"))) else 0.0


@register_strategy(
    name="ClassicDonchianBreakout",
    version="v1",
    family="classic_catalog",
    description="Donchian channel breakout strategy.",
    parameters=["donchian_window"],
    indicators=["Donchian"],
    categories=["classic", "breakout"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["donchian_breakout_classic"],
)
class ClassicDonchianBreakoutStrategy(_BaseClassicStrategy):
    def __init__(self, donchian_window: int = 20, **_: object) -> None:
        self._window = int(max(5, donchian_window))

    @property
    def name(self) -> str:
        return "ClassicDonchianBreakout"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["donchian_high"] = out["high"].rolling(self._window).max().shift(1)
        out["donchian_low"] = out["low"].rolling(self._window).min().shift(1)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if price > float(last.get("donchian_high", float("inf"))):
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last["close"]) > float(last.get("donchian_high", float("inf"))) else 0.0


@register_strategy(
    name="ClassicATRBreakout",
    version="v1",
    family="classic_catalog",
    description="ATR expansion breakout strategy.",
    parameters=["atr_period", "atr_mult"],
    indicators=["ATR"],
    categories=["classic", "breakout", "volatility"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["atr_breakout_classic"],
)
class ClassicATRBreakoutStrategy(_BaseClassicStrategy):
    def __init__(self, atr_period: int = 14, atr_mult: float = 1.5, **_: object) -> None:
        self._atr = ATR(atr_period)
        self._atr_mult = float(atr_mult)

    @property
    def name(self) -> str:
        return "ClassicATRBreakout"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out[self._atr.name] = self._atr.calculate(out)
        out["atr_break_level"] = out["close"].shift(1) + out[self._atr.name].shift(1) * self._atr_mult
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if price > float(last.get("atr_break_level", float("inf"))):
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last["close"]) > float(last.get("atr_break_level", float("inf"))) else 0.0


@register_strategy(
    name="ClassicVWAPReversion",
    version="v1",
    family="classic_catalog",
    description="VWAP reversion strategy.",
    parameters=["vwap_dev_pct"],
    indicators=["VWAP"],
    categories=["classic", "mean_reversion"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["vwap_reversion_classic"],
)
class ClassicVWAPReversionStrategy(_BaseClassicStrategy):
    def __init__(self, vwap_dev_pct: float = 0.3, **_: object) -> None:
        self._vwap_dev_pct = float(vwap_dev_pct)

    @property
    def name(self) -> str:
        return "ClassicVWAPReversion"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        typical = (out["high"] + out["low"] + out["close"]) / 3.0
        pv = typical * out["volume"]
        out["vwap"] = pv.cumsum() / out["volume"].cumsum().replace(0.0, pd.NA)
        out["vwap_dev_pct"] = ((out["close"] - out["vwap"]) / out["vwap"]) * 100.0
        out["vwap_dev_pct"] = pd.to_numeric(out["vwap_dev_pct"], errors="coerce").fillna(0.0)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if float(last.get("vwap_dev_pct", 0.0)) <= -abs(self._vwap_dev_pct):
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        return 1.0 if float(df.iloc[-1].get("vwap_dev_pct", 0.0)) <= -abs(self._vwap_dev_pct) else 0.0


@register_strategy(
    name="ClassicKeltnerChannel",
    version="v1",
    family="classic_catalog",
    description="Keltner channel pullback strategy.",
    parameters=["ema_period", "atr_period", "kc_mult"],
    indicators=["EMA", "ATR"],
    categories=["classic", "trend"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["keltner_channel_classic"],
)
class ClassicKeltnerChannelStrategy(_BaseClassicStrategy):
    def __init__(self, ema_period: int = 20, atr_period: int = 14, kc_mult: float = 2.0, **_: object) -> None:
        self._ema = EMA(int(ema_period))
        self._atr = ATR(int(atr_period))
        self._kc_mult = float(kc_mult)

    @property
    def name(self) -> str:
        return "ClassicKeltnerChannel"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["kc_mid"] = self._ema.calculate(out)
        out["kc_atr"] = self._atr.calculate(out)
        out["kc_upper"] = out["kc_mid"] + out["kc_atr"] * self._kc_mult
        out["kc_lower"] = out["kc_mid"] - out["kc_atr"] * self._kc_mult
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if price > float(last.get("kc_mid", price)) and price <= float(last.get("kc_upper", price)):
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last["close"]) > float(last.get("kc_mid", float("inf"))) else 0.0


@register_strategy(
    name="ClassicDualMomentum",
    version="v1",
    family="classic_catalog",
    description="Dual momentum with EMA trend filter.",
    parameters=["momentum_window", "ema_trend"],
    indicators=["EMA", "ROC"],
    categories=["classic", "momentum"],
    compatibility=["optimizer", "validation", "execution_manager", "database"],
    aliases=["dual_momentum_classic"],
)
class ClassicDualMomentumStrategy(_BaseClassicStrategy):
    def __init__(self, momentum_window: int = 20, ema_trend: int = 50, **_: object) -> None:
        self._window = int(max(5, momentum_window))
        self._ema = EMA(int(max(10, ema_trend)))

    @property
    def name(self) -> str:
        return "ClassicDualMomentum"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["mom_ret"] = out["close"].pct_change(self._window)
        out["ema_trend"] = self._ema.calculate(out)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if float(last.get("mom_ret", 0.0)) > 0.0 and price > float(last.get("ema_trend", price)):
            sl, tp = _risk_levels(price)
            return StrategySignal(SignalType.BUY, price, _ts(last.name), score=1.0, stop_loss=sl, take_profit=tp)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if float(last.get("mom_ret", 0.0)) > 0.0 else 0.0
