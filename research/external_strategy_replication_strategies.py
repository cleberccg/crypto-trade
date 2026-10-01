"""Replicação de estratégias externas: reproduz especificações publicadas de
seguimento de tendência (BTC 4H SMA200, Apex No-Pyramid, Quattro Donchian,
5 EMA Weekly Filter, Multi-Asset Vol-Normalized Trend) e as avalia no fluxo
DEV -> Validation -> OOS usando nosso conjunto de dados Binance Spot.

Reutiliza, sem modificações:
- BacktestEngine / BacktestConfig (backtesting/engine.py)
- padrões de RiskManager (risk/risk_manager.py) -- não alterado nem estendido por subclassing
- compute_metrics (backtesting/metrics.py)
- CandleRepository (database/repositories.py) sobre os candles MySQL existentes
- convenção FINAL_HOLDOUT já usada em outras partes deste repositório: linhas com
    timestamp >= 2026-06-01 NUNCA são carregadas aqui (por construção, não apenas
    filtradas posteriormente).

Não altera Paper Live, CDB (ClassicDonchianBreakout), RiskManager nem
PositionSizer. As novas classes de estratégia são subclasses diretas de
BaseStrategy definidas abaixo (não registradas em strategies/registry.py) e,
portanto, não aparecem no catálogo de estratégias do otimizador/paper-live.

Granularidade OHLCV de base: 15m (o histórico comum mais longo por ativo no
banco de dados), reamostrado com agregação OHLCV padrão para 4h/1D/1W-MON,
conforme exigido por cada estratégia. Isso NÃO é uma nova coleta de dados --
é uma agregação determinística de candles já validados e armazenados pelo projeto.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine, BacktestResult
from backtesting.metrics import compute_metrics
from database.connection import get_session
from database.repositories import CandleRepository
from indicators.bollinger import BollingerBands
from indicators.rsi import RSI
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.mean_reversion_v1 import MeanReversionV1Strategy

BASE_DIR = Path(__file__).resolve().parent
OUT_JSON = BASE_DIR / "external_strategy_replication_latest.json"

FINAL_HOLDOUT_START = pd.Timestamp("2026-06-01T00:00:00Z")
BASE_FEE = 0.001      # 0,1% por lado, valor-base do projeto (backtesting/engine.py _DEFAULT_FEE_PCT)
STRESS_FEE = 0.0015   # 0,15% por lado, mesma convenção de run_autonomous_strategy_research_v3.py
SLIPPAGE_BPS = 2.0    # custo adicional de estresse em uma direção, mesma convenção de run_autonomous_strategy_research_v3.py
CAPITAL = 10_000.0
BASE_TIMEFRAME = "15m"

# Neutraliza o stop/TP/trailing percentual integrado ao mecanismo quando a
# especificação publicada da estratégia não prevê esse mecanismo (ou implementa o próprio via exit_signal).
# Os valores são escolhidos para, na prática, nunca serem atingidos.
NEVER_STOP_FRACTION = 0.01     # stop_loss = entry * 0.01 (movimento adverso de 99%)
NEVER_TP_MULTIPLE = 100.0      # take_profit = entry * 100
NEVER_TRAILING_PCT = 0.99      # Recuo de 99% em relação ao pico

TARGET_CANDIDATES = 1
CANDIDATE_MIN_NET_PF = 1.20
CANDIDATE_MIN_TRADES_PER_SPLIT = 10


def _log(message: str) -> None:
    print(message, flush=True)


def _ts(value: object) -> datetime:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return datetime.now(tz=timezone.utc)
    return ts.to_pydatetime()


# ---------------------------------------------------------------------------
# Dados: carrega candles de 15m do banco existente, reamostra, divide e bloqueia o holdout
# ---------------------------------------------------------------------------

def load_base_candles(symbol: str) -> pd.DataFrame:
    """Candles de 15m até (mas sem incluir) FINAL_HOLDOUT_START. Nunca carrega o holdout."""
    with get_session() as session:
        repo = CandleRepository(session)
        start = datetime(2015, 1, 1, tzinfo=timezone.utc)
        end = FINAL_HOLDOUT_START.to_pydatetime() - pd.Timedelta(minutes=15)
        rows = repo.get_range(symbol, BASE_TIMEFRAME, start, end)
    if not rows:
        raise RuntimeError(f"No {BASE_TIMEFRAME} candles for {symbol}")
    df = pd.DataFrame(
        [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume} for c in rows],
        index=pd.DatetimeIndex([c.open_time for c in rows], tz="UTC"),
    )
    df = df.sort_index()
    df = df[df.index < FINAL_HOLDOUT_START]
    return df


_RESAMPLE_RULE = {"1h": "1h", "4h": "4h", "1d": "1D", "1w": "1W-MON"}


def resample_ohlcv(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    rule = _RESAMPLE_RULE[timeframe]
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(rule, label="left", closed="left").agg(agg)
    return out.dropna(subset=["open", "high", "low", "close"])


@dataclass(frozen=True)
class Split:
    name: str
    frame: pd.DataFrame


def dev_val_oos_split(df: pd.DataFrame) -> list[Split]:
    """Divisão 60/20/20 pela quantidade de candles, em ordem temporal (sem embaralhamento); FINAL_HOLDOUT já foi excluído anteriormente."""
    n = len(df)
    dev_end = int(n * 0.60)
    val_end = int(n * 0.80)
    return [
        Split("DEV", df.iloc[:dev_end]),
        Split("VALIDATION", df.iloc[dev_end:val_end]),
        Split("OOS", df.iloc[val_end:]),
    ]


# ---------------------------------------------------------------------------
# Estratégia 1 -- tendência SMA200 de BTC em 4h (iolufemi/crypto-trend-research)
# ---------------------------------------------------------------------------

class Sma200TrendStrategy(BaseStrategy):
    """Comprada/sem posição: mantém posição comprada enquanto close > SMA200 e
    fica sem posição quando close < SMA200. Não há take-profit fixo nem trailing
    (a especificação publicada não prevê esses mecanismos); stop/TP/trailing do
    mecanismo são neutralizados para que a inversão da SMA seja a única saída."""

    def __init__(self, sma_period: int = 200) -> None:
        self._period = int(sma_period)

    @property
    def name(self) -> str:
        return f"ExtRepl_SMA{self._period}TrendBTC4H"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["close"].rolling(self._period).mean()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        sma = last.get("sma")
        if sma is not None and not pd.isna(sma) and price > float(sma):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        sma = last.get("sma")
        if sma is not None and not pd.isna(sma) and price < float(sma):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "sma_flip"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        sma = last.get("sma")
        if sma is None or pd.isna(sma):
            return 0.0
        return 1.0 if float(last["close"]) > float(sma) else 0.0


class Sma200VolTargetStrategy(Sma200TrendStrategy):
    """Inversão simples da SMA200, mas score (=multiplicador do tamanho da
    posição via RiskManager) é escalado inversamente à volatilidade realizada
    (desvio padrão dos retornos em 20 candles), aproximando o overlay original
    de vol-targeting sem alterar RiskManager/PositionSizer -- somente a saída
    `score` da própria estratégia."""

    def __init__(self, sma_period: int = 200, vol_window: int = 20, target_vol: float = 0.02) -> None:
        super().__init__(sma_period)
        self._vol_window = int(vol_window)
        self._target_vol = float(target_vol)

    @property
    def name(self) -> str:
        return f"ExtRepl_SMA{self._period}VolTargetBTC4H"

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().calculate(df)
        ret = out["close"].pct_change()
        out["realized_vol"] = ret.rolling(self._vol_window).std()
        return out

    def _vol_score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        vol = last.get("realized_vol")
        if vol is None or pd.isna(vol) or vol <= 0:
            return 1.0
        return float(np.clip(self._target_vol / float(vol), 0.25, 1.5))

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        signal = super().entry_signal(df)
        if signal.signal == SignalType.BUY:
            signal.score = self._vol_score(df)
        return signal

    def score(self, df: pd.DataFrame) -> float:
        base = super().score(df)
        return base * self._vol_score(df) if base > 0 else 0.0


def compute_adx(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """ADX de Wilder, causal (ewm considera apenas dados anteriores). Compartilhado
    pelas estratégias com filtro de regime e por diagnose_oos_failure.py -- uma
    única implementação, sem duplicação."""
    high, low, close = df["high"], df["low"], df["close"]
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    prev_close = close.shift(1)
    tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    plus_di = 100.0 * pd.Series(plus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    minus_di = 100.0 * pd.Series(minus_dm, index=df.index).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean() / atr.replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


class Sma200RegimeGatedStrategy(Sma200TrendStrategy):
    """Filtro experimental de regime aplicado à regra SMA200 INALTERADA: a
    posição comprada só é permitida quando regime == TRENDING_BULL
    (close>SMA200 AND SMA200 rising AND ADX(14)>=adx_threshold); qualquer outro
    regime -> CASH (sem posição). Não altera a lógica de preço de entrada/saída
    nem otimiza parâmetros -- apenas filtra o mesmo sinal, de forma causal (ADX via
    ewm, inclinação via diff), sem lookahead."""

    def __init__(self, sma_period: int = 200, adx_period: int = 14, adx_threshold: float = 20.0, slope_lookback: int = 20) -> None:
        super().__init__(sma_period)
        self._adx_period = int(adx_period)
        self._adx_threshold = float(adx_threshold)
        self._slope_lookback = int(slope_lookback)

    @property
    def name(self) -> str:
        return "Sma200RegimeGated"

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = super().calculate(df)
        out["adx"] = compute_adx(out, self._adx_period)
        out["sma_slope_up"] = out["sma"] > out["sma"].shift(self._slope_lookback)
        out["regime_bull"] = (out["close"] > out["sma"]) & out["sma_slope_up"].fillna(False) & (out["adx"] >= self._adx_threshold)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("regime_bull", False)):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if not bool(last.get("regime_bull", False)):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "regime_not_bull"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if bool(last.get("regime_bull", False)) else 0.0


class RegimeAdaptiveStrategy(BaseStrategy):
    """TRENDING_BULL -> SMA200 (regra inalterada); SIDEWAYS (ADX<20, não bull) ->
    delega para a MeanReversionV1Strategy existente e inalterada; qualquer outro
    regime (TRENDING_BEAR / incerto) -> CASH. Só é testada se o filtro de regime
    isolado (Sma200RegimeGatedStrategy) já tiver mostrado uma melhora clara
    (critério de aprovação da etapa 5). Reutiliza MeanReversionV1Strategy tal
    como está (não registrada, não modificada nem escolhida entre várias -- é
    exatamente uma estratégia de reversão à média)."""

    def __init__(self, sma_period: int = 200, adx_period: int = 14, adx_threshold: float = 20.0, slope_lookback: int = 20) -> None:
        self._period = int(sma_period)
        self._adx_period = int(adx_period)
        self._adx_threshold = float(adx_threshold)
        self._slope_lookback = int(slope_lookback)
        self._mr = MeanReversionV1Strategy()

    @property
    def name(self) -> str:
        return "ExtRepl_RegimeAdaptive_SMA200_MR"

    def initialize(self) -> None:
        self._mr.initialize()

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma"] = out["close"].rolling(self._period).mean()
        out["adx"] = compute_adx(out, self._adx_period)
        out["sma_slope_up"] = out["sma"] > out["sma"].shift(self._slope_lookback)
        out["regime_bull"] = (out["close"] > out["sma"]) & out["sma_slope_up"].fillna(False) & (out["adx"] >= self._adx_threshold)
        out["regime_sideways"] = (~out["regime_bull"]) & (out["adx"] < self._adx_threshold)
        mr_cols = self._mr.calculate(df)
        for col in mr_cols.columns:
            if col not in out.columns:
                out[col] = mr_cols[col]
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("regime_bull", False)):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        if bool(last.get("regime_sideways", False)):
            return self._mr.entry_signal(df)
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("regime_bull", False)):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name))
        if bool(last.get("regime_sideways", False)):
            return self._mr.exit_signal(df, entry_price)
        return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "regime_bear_or_uncertain"})

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        if bool(last.get("regime_bull", False)):
            return 1.0
        if bool(last.get("regime_sideways", False)):
            return self._mr.score(df)
        return 0.0


class SimpleBollingerRsiMeanReversionStrategy(BaseStrategy):
    """Modelo público de reversão à média: compra quando close está abaixo da
    banda inferior de Bollinger e o RSI está sobrevendido; sai quando o preço
    retorna à banda do meio ou o RSI fica sobrecomprado. Sem filtros de
    tendência, volume, ML, posição vendida, alavancagem, livro de ofertas ou
    otimização."""

    def __init__(
        self,
        bb_period: int = 20,
        bb_std_dev: float = 2.0,
        rsi_period: int = 14,
        rsi_entry: float = 30.0,
        rsi_exit: float = 70.0,
    ) -> None:
        self._bb_period = int(bb_period)
        self._bb_std_dev = float(bb_std_dev)
        self._rsi_period = int(rsi_period)
        self._rsi_entry = float(rsi_entry)
        self._rsi_exit = float(rsi_exit)
        self._bb: BollingerBands | None = None
        self._rsi: RSI | None = None

    @property
    def name(self) -> str:
        return "ExtRepl_BollingerRsiMeanReversion"

    def initialize(self) -> None:
        self._bb = BollingerBands(period=self._bb_period, std_dev=self._bb_std_dev)
        self._rsi = RSI(period=self._rsi_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        if self._bb is None or self._rsi is None:
            raise RuntimeError(f"{self.name} not initialized. Call initialize() first.")
        out = df.copy()
        bb = self._bb.calculate(df)
        out["bb_middle"] = bb["middle"]
        out["bb_upper"] = bb["upper"]
        out["bb_lower"] = bb["lower"]
        out["bb_percent_b"] = bb["percent_b"]
        out["rsi"] = self._rsi.calculate(df)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if any(pd.isna(last.get(col)) for col in ("bb_lower", "rsi")):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        if price < float(last["bb_lower"]) and float(last["rsi"]) <= self._rsi_entry:
            return StrategySignal(
                SignalType.BUY,
                price,
                _ts(last.name),
                score=self.score(df),
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
                metadata={"entry_reason": "bb_lower_plus_rsi_oversold"},
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if any(pd.isna(last.get(col)) for col in ("bb_middle", "rsi")):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        if price >= float(last["bb_middle"]):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), score=self.score(df), metadata={"exit_reason": "reverted_to_middle_band"})
        if float(last["rsi"]) >= self._rsi_exit:
            return StrategySignal(SignalType.SELL, price, _ts(last.name), score=self.score(df), metadata={"exit_reason": "rsi_overbought"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        rsi = float(last.get("rsi", 100.0))
        percent_b = float(last.get("bb_percent_b", 1.0))
        rsi_score = max(0.0, min(1.0, (self._rsi_entry - rsi) / max(self._rsi_entry, 1e-9)))
        band_score = max(0.0, min(1.0, -percent_b))
        return round(float(max(0.1, 0.6 * rsi_score + 0.4 * band_score)), 4)


class ZScoreMeanReversionStrategy(BaseStrategy):
    """Reversão à média pública por z-score: compra quando close está duas
    unidades de desvio padrão móvel abaixo da média móvel; sai quando o preço
    retorna à média."""

    def __init__(self, lookback: int = 20, entry_z: float = -2.0, exit_z: float = 0.0) -> None:
        self._lookback = int(lookback)
        self._entry_z = float(entry_z)
        self._exit_z = float(exit_z)

    @property
    def name(self) -> str:
        return "ExtRepl_ZScoreMeanReversion"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        mean = out["close"].rolling(self._lookback).mean()
        std = out["close"].rolling(self._lookback).std(ddof=0)
        out["z_mean"] = mean
        out["z_score"] = (out["close"] - mean) / std.replace(0.0, np.nan)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        z_score = last.get("z_score")
        if z_score is not None and not pd.isna(z_score) and float(z_score) <= self._entry_z:
            return StrategySignal(
                SignalType.BUY,
                price,
                _ts(last.name),
                score=self.score(df),
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
                metadata={"entry_reason": "z_score_below_minus_2"},
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        z_score = last.get("z_score")
        if z_score is not None and not pd.isna(z_score) and float(z_score) >= self._exit_z:
            return StrategySignal(SignalType.SELL, price, _ts(last.name), score=self.score(df), metadata={"exit_reason": "z_score_reverted_to_mean"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        z_score = float(df.iloc[-1].get("z_score", 0.0))
        return round(float(max(0.1, min(1.0, abs(min(0.0, z_score)) / max(abs(self._entry_z), 1e-9)))), 4)


class MovingAverageDeviationReversalStrategy(BaseStrategy):
    """Reversão pública por desvio da média móvel: compra quando close está pelo
    menos 5% abaixo da SMA de 50 períodos; sai quando o preço retorna à SMA."""

    def __init__(self, ma_period: int = 50, entry_deviation: float = -0.05, exit_deviation: float = 0.0) -> None:
        self._ma_period = int(ma_period)
        self._entry_deviation = float(entry_deviation)
        self._exit_deviation = float(exit_deviation)

    @property
    def name(self) -> str:
        return "ExtRepl_MaDeviationReversal"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["ma"] = out["close"].rolling(self._ma_period).mean()
        out["ma_deviation"] = (out["close"] / out["ma"]) - 1.0
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        deviation = last.get("ma_deviation")
        if deviation is not None and not pd.isna(deviation) and float(deviation) <= self._entry_deviation:
            return StrategySignal(
                SignalType.BUY,
                price,
                _ts(last.name),
                score=self.score(df),
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
                metadata={"entry_reason": "close_5pct_below_sma50"},
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        deviation = last.get("ma_deviation")
        if deviation is not None and not pd.isna(deviation) and float(deviation) >= self._exit_deviation:
            return StrategySignal(SignalType.SELL, price, _ts(last.name), score=self.score(df), metadata={"exit_reason": "returned_to_sma50"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def score(self, df: pd.DataFrame) -> float:
        deviation = float(df.iloc[-1].get("ma_deviation", 0.0))
        return round(float(max(0.1, min(1.0, abs(min(0.0, deviation)) / max(abs(self._entry_deviation), 1e-9)))), 4)


# ---------------------------------------------------------------------------
# Candidatas de 1h (públicas, regras congeladas, somente posições compradas no mercado spot)
# ---------------------------------------------------------------------------

class _FrozenRuleStrategy(BaseStrategy):
    """Colunas booleanas de entrada/saída calculadas causalmente em calculate();
    stop/TP/trailing do mecanismo são neutralizados para que somente a regra de
    saída publicada encerre a posição."""

    _label = "FrozenRule"

    @property
    def name(self) -> str:
        return self._label

    def initialize(self) -> None:
        return None

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("entry_ok", False)):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
                metadata={"entry_reason": self._label},
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if bool(last.get("exit_ok", False)):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": f"{self._label}_exit"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        return 1.0 if bool(df.iloc[-1].get("entry_ok", False)) else 0.0


def _wilder_rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0.0)).ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(100.0)


class ConnorsRsi2Strategy(_FrozenRuleStrategy):
    """RSI(2) de Connors & Alvarez: compra quando close>SMA200 e RSI(2)<10; sai quando close>SMA5."""

    _label = "ConnorsRsi2"

    def __init__(self, trend_period: int = 200, rsi_period: int = 2, rsi_entry: float = 10.0, exit_period: int = 5) -> None:
        self._trend_period = int(trend_period)
        self._rsi_period = int(rsi_period)
        self._rsi_entry = float(rsi_entry)
        self._exit_period = int(exit_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        trend = out["close"].rolling(self._trend_period).mean()
        exit_ma = out["close"].rolling(self._exit_period).mean()
        rsi = _wilder_rsi(out["close"], self._rsi_period)
        out["entry_ok"] = (out["close"] > trend) & (rsi < self._rsi_entry)
        out["exit_ok"] = out["close"] > exit_ma
        return out


class DonchianTurtleS1Strategy(_FrozenRuleStrategy):
    """Turtle System 1: compra quando close supera a máxima anterior de 20 candles; sai quando close fica abaixo da mínima anterior de 10 candles."""

    _label = "DonchianTurtleS1"

    def __init__(self, entry_period: int = 20, exit_period: int = 10) -> None:
        self._entry_period = int(entry_period)
        self._exit_period = int(exit_period)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        upper = out["high"].rolling(self._entry_period).max().shift(1)
        lower = out["low"].rolling(self._exit_period).min().shift(1)
        out["entry_ok"] = out["close"] > upper
        out["exit_ok"] = out["close"] < lower
        return out


class IbsMeanReversionStrategy(_FrozenRuleStrategy):
    """Reversão por Internal Bar Strength: compra quando IBS<0.2; sai quando IBS>0.8."""

    _label = "IbsMeanReversion"

    def __init__(self, entry_ibs: float = 0.2, exit_ibs: float = 0.8) -> None:
        self._entry_ibs = float(entry_ibs)
        self._exit_ibs = float(exit_ibs)

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        rng = (out["high"] - out["low"]).replace(0.0, np.nan)
        ibs = (out["close"] - out["low"]) / rng
        out["entry_ok"] = ibs < self._entry_ibs
        out["exit_ok"] = ibs > self._exit_ibs
        return out


# ---------------------------------------------------------------------------
# Estratégia 2 -- APEX sem pirâmide (EstebanSP23/crypto_systematic_research)
# ---------------------------------------------------------------------------

class ApexNoPyramidStrategy(BaseStrategy):
    """Rompimento em 4h da máxima de aproximadamente 6 meses, filtro de
    tendência SMA50>SMA200 e volume>1.5x da média.
    DESVIO DA ESPECIFICAÇÃO PUBLICADA (documentado, não simplificado em
    silêncio): o BacktestEngine da plataforma mantém uma única posição integral
    por ativo e não permite fechamentos parciais; portanto, não é possível
    reproduzir exatamente a regra publicada 'reduzir 50% da posição em 2R +
    aplicar trailing no restante pela mínima de 20 candles'. Aproximamos com
    uma única saída integral por trailing stop da mínima de 20 candles (o
    componente trailing da regra original), o
    comportamento mais fiel permitido pelo mecanismo. A ausência de pirâmide é
    garantida estruturalmente (o mecanismo nunca abre uma segunda posição
    enquanto já existe uma), correspondendo exatamente à regra de operar sem pirâmide.
    """

    def __init__(self, breakout_bars: int = 1095, sma_fast: int = 50, sma_slow: int = 200, volume_window: int = 20, volume_multiple: float = 1.5, trail_bars: int = 20) -> None:
        self._breakout_bars = int(breakout_bars)
        self._sma_fast = int(sma_fast)
        self._sma_slow = int(sma_slow)
        self._volume_window = int(volume_window)
        self._volume_multiple = float(volume_multiple)
        self._trail_bars = int(trail_bars)

    @property
    def name(self) -> str:
        return "ExtRepl_ApexNoPyramid4H"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["breakout_high"] = out["high"].rolling(self._breakout_bars).max().shift(1)
        out["sma_fast"] = out["close"].rolling(self._sma_fast).mean()
        out["sma_slow"] = out["close"].rolling(self._sma_slow).mean()
        out["avg_volume"] = out["volume"].rolling(self._volume_window).mean()
        out["trail_low"] = out["low"].rolling(self._trail_bars).min().shift(1)
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        cols = ["breakout_high", "sma_fast", "sma_slow", "avg_volume", "trail_low"]
        if any(pd.isna(last.get(c)) for c in cols):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        trend_ok = float(last["sma_fast"]) > float(last["sma_slow"])
        breakout_ok = price > float(last["breakout_high"])
        volume_ok = float(last["volume"]) > self._volume_multiple * float(last["avg_volume"])
        if trend_ok and breakout_ok and volume_ok:
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        trail = last.get("trail_low")
        if trail is not None and not pd.isna(trail) and price < float(trail):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "trail_20bar_low"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        if any(pd.isna(last.get(c)) for c in ("sma_fast", "sma_slow")):
            return 0.0
        return 1.0 if float(last["sma_fast"]) > float(last["sma_slow"]) else 0.0


# ---------------------------------------------------------------------------
# Estratégia 3 -- QUATTRO DONCHIAN (EstebanSP23/crypto_systematic_research)
# ---------------------------------------------------------------------------

class QuattroDonchianStrategy(BaseStrategy):
    """Rompimento Donchian(20) em BTC 4h, filtro de alta da EMA200 diária,
    ATR(14), trailing stop chandelier de 2xATR e stop de emergência de 5%.
    DESVIO DA ESPECIFICAÇÃO PUBLICADA (documentado): a estratégia original
    adiciona até 4 unidades em intervalos de +0.5N. O mecanismo da plataforma
    aceita exatamente uma posição aberta por ativo (sem adicionar à posição),
    portanto a pirâmide NÃO é reproduzida -- este teste cobre somente o caso
    básico de uma unidade. Não exige alavancagem nem contratos perpétuos; opera
    como uma posição spot simples, comprada ou sem posição, sem distorção por
    alavancagem.
    """

    def __init__(self, donchian_window: int = 20, atr_period: int = 14, atr_multiple: float = 2.0, catastrophe_stop_pct: float = 0.05) -> None:
        self._window = int(donchian_window)
        self._atr_period = int(atr_period)
        self._atr_multiple = float(atr_multiple)
        self._catastrophe_stop_pct = float(catastrophe_stop_pct)
        self._peak_since_entry: float | None = None

    @property
    def name(self) -> str:
        return "ExtRepl_QuattroDonchianBTC4H"

    def initialize(self) -> None:
        self._peak_since_entry = None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["donchian_high"] = out["high"].rolling(self._window).max().shift(1)
        high, low, close = out["high"], out["low"], out["close"]
        prev_close = close.shift(1)
        tr = pd.concat([(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        out["atr"] = tr.ewm(alpha=1.0 / self._atr_period, adjust=False, min_periods=self._atr_period).mean()
        # Filtro EMA200 diário, calculado a partir da reamostragem diária deste símbolo dos
        # MESMOS candles já carregados (sem dados novos), combinado causalmente (as-of, sem olhar para o futuro).
        daily = out[["open", "high", "low", "close", "volume"]].resample("1D", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
        ).dropna()
        daily["ema200"] = daily["close"].ewm(span=200, adjust=False).mean()
        daily["ema200_rising"] = daily["ema200"] > daily["ema200"].shift(20)
        merged = pd.merge_asof(out.reset_index(), daily[["ema200_rising"]].reset_index().rename(columns={"index": "day"}),
                                left_on="index", right_on="day", direction="backward")
        out["ema200_rising"] = merged["ema200_rising"].to_numpy()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if pd.isna(last.get("donchian_high")) or pd.isna(last.get("atr")) or not bool(last.get("ema200_rising", False)):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        if price > float(last["donchian_high"]):
            self._peak_since_entry = price
            catastrophe_stop = price * (1.0 - self._catastrophe_stop_pct)
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=catastrophe_stop,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        atr = last.get("atr")
        if atr is None or pd.isna(atr):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name))
        if self._peak_since_entry is None:
            self._peak_since_entry = max(price, entry_price)
        self._peak_since_entry = max(self._peak_since_entry, price)
        chandelier = self._peak_since_entry - self._atr_multiple * float(atr)
        if price < chandelier:
            self._peak_since_entry = None
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "chandelier_2atr"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        return 1.0 if bool(last.get("ema200_rising", False)) else 0.0


# ---------------------------------------------------------------------------
# Estratégia 4 -- filtro de tendência com EMA 5 (semanal)
# ---------------------------------------------------------------------------

class FiveEmaWeeklyFilterStrategy(BaseStrategy):
    """Em BTC, close semanal > EMA5 semanal E EMA200 diária em alta em relação
    a 20 dias antes. Opera comprada ou sem posição e sai quando a condição
    semanal deixa de ser satisfeita. A baixa frequência é intencional."""

    def __init__(self, weekly_ema_period: int = 5, daily_ema_period: int = 200, daily_lookback: int = 20) -> None:
        self._weekly_ema_period = int(weekly_ema_period)
        self._daily_ema_period = int(daily_ema_period)
        self._daily_lookback = int(daily_lookback)

    @property
    def name(self) -> str:
        return "ExtRepl_5EMAWeeklyFilterBTC"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        # df aqui já é o dataframe SEMANAL (consulte prepare_weekly_frame abaixo);
        # "daily_ema200_rising" é combinado previamente como coluna antes desta chamada.
        out = df.copy()
        out["weekly_ema5"] = out["close"].ewm(span=self._weekly_ema_period, adjust=False).mean()
        return out

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        ema = last.get("weekly_ema5")
        daily_rising = bool(last.get("daily_ema200_rising", False))
        if ema is not None and not pd.isna(ema) and price > float(ema) and daily_rising:
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=1.0,
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        ema = last.get("weekly_ema5")
        daily_rising = bool(last.get("daily_ema200_rising", False))
        if (ema is not None and not pd.isna(ema) and price < float(ema)) or not daily_rising:
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "weekly_condition_lost"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        ema = last.get("weekly_ema5")
        if ema is None or pd.isna(ema):
            return 0.0
        return 1.0 if float(last["close"]) > float(ema) and bool(last.get("daily_ema200_rising", False)) else 0.0


def prepare_weekly_frame(base_15m: pd.DataFrame, daily_ema_period: int, daily_lookback: int) -> pd.DataFrame:
    daily = resample_ohlcv(base_15m, "1d")
    daily["ema200"] = daily["close"].ewm(span=daily_ema_period, adjust=False).mean()
    daily["ema200_rising"] = daily["ema200"] > daily["ema200"].shift(daily_lookback)
    weekly = resample_ohlcv(base_15m, "1w")
    merged = pd.merge_asof(
        weekly.reset_index(), daily[["ema200_rising"]].reset_index().rename(columns={"index": "day"}),
        left_on="index", right_on="day", direction="backward",
    )
    weekly["daily_ema200_rising"] = merged["ema200_rising"].to_numpy()
    return weekly


# ---------------------------------------------------------------------------
# Estratégia 5 -- tendência multiativo normalizada por volatilidade
# ---------------------------------------------------------------------------

class VolNormalizedTrendStrategy(BaseStrategy):
    """Seguimento de tendência com SMA50/SMA200 diária; o score da posição é
    escalado inversamente à volatilidade realizada de 20 dias (proxy de
    dimensionamento normalizado por volatilidade via `score`).
    NOTA: a especificação pública original ('medias moveis / trend following',
    'sizing de portfolio', 'walk-forward') não informa os períodos exatos das
    médias; usamos a convenção padrão de cruzamento dourado 50/200 e
    documentamos explicitamente essa escolha, em vez de supor parâmetros não
    especificados. O BacktestEngine de ativo único NÃO oferece alocação de
    capital em nível de portfólio entre BTC/ETH/BNB/ADA; cada ativo é executado
    independentemente e os resultados são agrupados/agregados depois como
    aproximação do mecanismo de portfólio -- isso é documentado, sem apresentar
    silenciosamente a aproximação como idêntica ao original."""

    def __init__(self, sma_fast: int = 50, sma_slow: int = 200, vol_window: int = 20, target_vol: float = 0.02) -> None:
        self._fast = int(sma_fast)
        self._slow = int(sma_slow)
        self._vol_window = int(vol_window)
        self._target_vol = float(target_vol)

    @property
    def name(self) -> str:
        return "ExtRepl_VolNormalizedTrendDaily"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["sma_fast"] = out["close"].rolling(self._fast).mean()
        out["sma_slow"] = out["close"].rolling(self._slow).mean()
        out["realized_vol"] = out["close"].pct_change().rolling(self._vol_window).std()
        return out

    def _vol_score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        vol = last.get("realized_vol")
        if vol is None or pd.isna(vol) or vol <= 0:
            return 1.0
        return float(np.clip(self._target_vol / float(vol), 0.25, 1.5))

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)
        if float(last["sma_fast"]) > float(last["sma_slow"]):
            return StrategySignal(
                SignalType.BUY, price, _ts(last.name), score=self._vol_score(df),
                stop_loss=price * NEVER_STOP_FRACTION,
                take_profit=price * NEVER_TP_MULTIPLE,
                trailing_stop_pct=NEVER_TRAILING_PCT,
            )
        return StrategySignal(SignalType.HOLD, price, _ts(last.name), score=0.0)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        last = df.iloc[-1]
        price = float(last["close"])
        if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
            return StrategySignal(SignalType.HOLD, price, _ts(last.name))
        if float(last["sma_fast"]) < float(last["sma_slow"]):
            return StrategySignal(SignalType.SELL, price, _ts(last.name), metadata={"exit_reason": "sma_cross_down"})
        return StrategySignal(SignalType.HOLD, price, _ts(last.name))

    def score(self, df: pd.DataFrame) -> float:
        last = df.iloc[-1]
        if pd.isna(last.get("sma_fast")) or pd.isna(last.get("sma_slow")):
            return 0.0
        return self._vol_score(df) if float(last["sma_fast"]) > float(last["sma_slow"]) else 0.0
