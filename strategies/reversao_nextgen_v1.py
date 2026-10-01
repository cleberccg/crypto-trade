"""
ReversaoNextGenV1 — estratégia Reversal Edge do Cluster H27.

Hipótese: H27 / Cluster: reversao_2
Confiança: 75% | Prioridade: 0.625 | Classificação: #4
Gerada a partir da reversão de engenharia da fase 5.4.

Visão geral da lógica
---------------------
Condições de entrada (SHORT/SELL) (todas devem ser verdadeiras):
1. Regime = reversao (reversão de tendência detectada por cruzamento de EMA/reversão de trend_score)
2. Faixa de ATR = high_atr (nível de volatilidade no tercil superior)
3. Faixa de RSI = unknown (RSI neutro — sem filtro específico)
4. Faixa de volume = low_volume (volume relativo abaixo da média)
5. Posição de Bollinger = inside_band (preço dentro das bandas de 2 desvios-padrão)

Direção: SHORT (venda na reversão)

Condições de saída (qualquer uma aciona a saída):
1. Sair quando a meta de lucro for atingida
2. Sair quando o stop loss for acionado
3. Sair quando o padrão de reversão falhar (o regime voltar à tendência)

Stop-loss / Take-profit
-----------------------
- Stop-loss: entrada + (ATR × ATR_STOP_MULTIPLIER) [posição vendida, portanto acima da entrada]
- Risco: stop_loss - entrada
- Retorno: risco × RISK_REWARD_RATIO
- Take-profit: entrada - retorno

Isso garante que o RR realizado seja igual ao RISK_REWARD_RATIO configurado.

Desempenho histórico (da fase 5.4):
- Tamanho da amostra: 1,933,669 operações
- Taxa de acerto: 100.0%
- Sharpe: 249.48
- Expectativa: $25.00 por operação
- Drawdown: 0.0%
- Risco/retorno: 3.18:1 (MFE/MAE)
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from config.settings import settings
from indicators.atr import ATR
from indicators.bollinger import BollingerBands
from indicators.ema import EMA
from indicators.rsi import RSI
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal
from strategies.families import ReversalEdgeStrategy
from strategies.registry import register_strategy
from utils.helpers import utc_now
from utils.logger import get_logger

logger = get_logger(__name__)


@register_strategy(
    name="ReversaoNextGenV1",
    version="v1",
    family="reversal_edge",
    description="Reversal Edge strategy using regime detection, ATR, volume, and Bollinger filters.",
    parameters=[
        "ema_fast",
        "ema_slow",
        "rsi_period",
        "atr_period",
        "atr_stop_multiplier",
        "risk_reward_ratio",
        "score_min",
        "volume_multiplier_min",
        "atr_high_threshold",
        "volume_low_threshold",
    ],
    indicators=["EMA", "RSI", "BollingerBands", "ATR"],
    categories=["reversal", "short", "mean_reversion"],
    compatibility=[
        "optimizer",
        "validation",
        "research_lab",
        "trade_management_lab",
        "execution_manager",
        "database",
        "checkpoints",
        "resume",
        "recovery",
    ],
    aliases=["reversao_v1", "reversao_next_gen", "h27"],
    parameter_aliases={
        "ema_mid": "ema_slow",
        "volume_multiplier": "volume_multiplier_min",
    },
)
class ReversaoNextGenV1Strategy(ReversalEdgeStrategy):
    """
    Estratégia Reversal Edge do cluster H27 da fase 5.4.

    Detecta padrões de reversão com base na mudança de regime, volatilidade (ATR),
    perfil de volume (volume baixo) e posicionamento das Bandas de Bollinger.

    Direção: SHORT (venda nos pontos de reversão)

    Argumentos:
        ema_fast: Período da EMA rápida para detectar tendências (padrão 20).
        ema_slow: Período da EMA lenta para detectar tendências (padrão 50).
        rsi_period: Período do RSI (padrão 14, não usado na entrada, mas disponível).
        atr_period: Período do ATR para calcular o stop-loss (padrão 14).
        atr_stop_multiplier: Multiplicador do stop loss baseado em ATR (padrão 2.0).
        risk_reward_ratio: Relação risco/retorno desejada (padrão 3.18).
        score_min: Pontuação mínima de confiança para operar (padrão 0.6).
        volume_multiplier_min: Volume relativo mínimo (padrão 0.7).
        atr_high_threshold: Percentil de ATR para classificar como "alto" (padrão 0.67).
        volume_low_threshold: Percentil de volume para classificar como "baixo" (padrão 0.40).
    """

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        atr_period: int = 14,
        atr_stop_multiplier: float = 2.0,
        risk_reward_ratio: float = 3.18,
        score_min: float = 0.6,
        volume_multiplier_min: float = 0.7,
        atr_high_threshold: float = 0.67,
        volume_low_threshold: float = 0.40,
    ) -> None:
        self._ema_fast_period = ema_fast
        self._ema_slow_period = ema_slow
        self._rsi_period = rsi_period
        self._atr_period = atr_period
        self._atr_stop_multiplier = atr_stop_multiplier
        self._risk_reward_ratio = risk_reward_ratio
        self._score_min = score_min
        self._volume_multiplier_min = volume_multiplier_min
        self._atr_high_threshold = atr_high_threshold
        self._volume_low_threshold = volume_low_threshold

        # Inicializado em initialize()
        self._ema_fast: EMA | None = None
        self._ema_slow: EMA | None = None
        self._rsi: RSI | None = None
        self._bb: BollingerBands | None = None
        self._atr: ATR | None = None
        # Cache pré-calculado do DataFrame enriquecido — evita O(n²) em BacktestEngine
        self._enriched_cache: pd.DataFrame | None = None

    @property
    def name(self) -> str:
        return "ReversaoNextGenV1"

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Instancia todos os objetos de indicadores."""
        self._ema_fast = EMA(period=self._ema_fast_period)
        self._ema_slow = EMA(period=self._ema_slow_period)
        self._rsi = RSI(period=self._rsi_period)
        self._bb = BollingerBands()
        self._atr = ATR(period=self._atr_period)
        self._enriched_cache = None  # Redefine o cache na reinicialização
        logger.info("%s — initialized.", self.name)

    # ------------------------------------------------------------------
    # Cálculo
    # ------------------------------------------------------------------

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Adiciona todas as colunas de indicadores a uma cópia de *df*.

        Desempenho: usa um cache interno para que chamadas repetidas com fatias
        de prefixo crescentes (como ocorre no BacktestEngine barra a barra) sejam
        atendidas em O(1). A primeira chamada com o conjunto de dados completo
        executa uma única passagem vetorizada O(n).

        Operações lentas substituídas:
        - pd.qcut per bar  -> expanding().rank(pct=True) + np.where  [O(n) vectorised]
        - apply(axis=1)    -> np.where on numpy arrays               [O(n) vectorised]

        Colunas adicionadas: ema_fast, ema_slow, rsi, bb_middle, bb_upper, bb_lower,
        bb_percent_b, atr, trend_score, atr_bucket, volume_bucket,
        bollinger_position, regime_reversal.
        """
        self._assert_initialized()
        n = len(df)

        # --- Cache encontrado: retorna o recorte pré-calculado (O(1)) ---
        if self._enriched_cache is not None and n <= len(self._enriched_cache):
            if n > 0 and df.index[-1] == self._enriched_cache.index[n - 1]:
                return self._enriched_cache.iloc[:n]

        # --- Cálculo vetorizado completo (O(n)) ---
        _t0 = time.perf_counter()
        logger.debug("%s — calculate: computing indicators for %d bars", self.name, n)
        result = df.copy()

        result[self._ema_fast.name] = self._ema_fast.calculate(df)  # type: ignore[union-attr]
        result[self._ema_slow.name] = self._ema_slow.calculate(df)  # type: ignore[union-attr]
        logger.debug("%s — EMA done (%.3fs)", self.name, time.perf_counter() - _t0)

        _t1 = time.perf_counter()
        result["rsi"] = self._rsi.calculate(df)  # type: ignore[union-attr]
        logger.debug("%s — RSI done (%.3fs)", self.name, time.perf_counter() - _t1)

        _t2 = time.perf_counter()
        result["atr"] = self._atr.calculate(df)  # type: ignore[union-attr]
        logger.debug("%s — ATR done (%.3fs)", self.name, time.perf_counter() - _t2)

        _t3 = time.perf_counter()
        bb_df = self._bb.calculate(df)  # type: ignore[union-attr]
        result["bb_middle"] = bb_df["middle"]
        result["bb_upper"] = bb_df["upper"]
        result["bb_lower"] = bb_df["lower"]
        result["bb_percent_b"] = bb_df["percent_b"]
        logger.debug("%s — Bollinger done (%.3fs)", self.name, time.perf_counter() - _t3)

        # Pontuação de tendência (baseada em EMA)
        ema_fast_col = self._ema_fast.name  # type: ignore[union-attr]
        ema_slow_col = self._ema_slow.name  # type: ignore[union-attr]
        result["trend_score"] = (
            (result[ema_fast_col] - result[ema_slow_col]) / result[ema_slow_col] * 100
        )

        _t4 = time.perf_counter()
        # Terços do ATR: expanding-rank substitui pd.qcut (matematicamente equivalente)
        _atr_rank = result["atr"].expanding(min_periods=3).rank(pct=True).to_numpy()
        result["atr_bucket"] = np.where(
            np.isnan(_atr_rank), "mid_atr",
            np.where(_atr_rank <= 1 / 3, "low_atr",
            np.where(_atr_rank <= 2 / 3, "mid_atr", "high_atr")),
        )
        logger.debug("%s — ATR buckets done (%.3fs)", self.name, time.perf_counter() - _t4)

        # Faixa de volume: limiares fixos, já em O(n)
        result["relative_volume"] = result["volume"] / result["volume"].rolling(20).mean()
        result["volume_bucket"] = pd.cut(
            result["relative_volume"],
            bins=[-float("inf"), 0.9, 1.1, float("inf")],
            labels=["low_volume", "normal_volume", "high_volume"],
            include_lowest=True,
        ).astype(str)

        _t5 = time.perf_counter()
        # Posição de Bollinger: numpy.where substitui apply(axis=1)
        _close = result["close"].to_numpy()
        _bb_upper = result["bb_upper"].to_numpy()
        _bb_lower = result["bb_lower"].to_numpy()
        result["bollinger_position"] = np.where(
            _close > _bb_upper, "above_upper",
            np.where(_close < _bb_lower, "below_lower", "inside_band"),
        )
        logger.debug("%s — Bollinger positions done (%.3fs)", self.name, time.perf_counter() - _t5)

        # Regime: detecta reversão (trend_score muda de sinal ou cruza zero)
        result["trend_score_prev"] = result["trend_score"].shift(1)
        result["regime_reversal"] = (
            (result["trend_score"] * result["trend_score_prev"] < 0)  # Mudança de sinal
            | (
                (result["trend_score"].abs() < 0.2)
                & (result["trend_score_prev"].abs() > 0.2)
            )  # Entrada em consolidação
        )

        _total = time.perf_counter() - _t0
        logger.info("%s — indicators pre-computed: %d bars in %.2fs", self.name, n, _total)

        # Cache para consultas subsequentes de prefixos por BacktestEngine
        if self._enriched_cache is None or n > len(self._enriched_cache):
            self._enriched_cache = result

        return result

    @staticmethod
    def _get_bollinger_position(
        close: float, bb_lower: float, bb_middle: float, bb_upper: float
    ) -> str:
        """Determina a posição nas Bandas de Bollinger."""
        if close > bb_upper:
            return "above_upper"
        elif close < bb_lower:
            return "below_lower"
        else:
            return "inside_band"

    # ------------------------------------------------------------------
    # Geração de sinais
    # ------------------------------------------------------------------

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        """
                Gera um sinal BUY quando há alinhamento com uma reversão de alta (H27).

                Diferença principal em relação à versão SHORT original:
                - Exigimos prev_trend_score < 0 (a tendência era de baixa), portanto entramos
                    somente em reversões de baixa para alta, não de alta para baixa.
                - O stop fica ABAIXO da entrada; o take-profit, ACIMA.
        """
        self._assert_initialized()

        last = df.iloc[-1]
        price = float(last["close"])
        atr = float(last["atr"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        # --- Condições de entrada ---
        # As 5 condições devem ser TRUE para um sinal BUY

        # 1. Reversão de regime de alta: a tendência estava DOWN e agora está revertendo para UP
        regime_reversal = bool(last.get("regime_reversal", False))
        trend_score = float(last.get("trend_score", 0.0))
        prev_trend_score = float(last.get("trend_score_prev", 0.0))

        # Reversão de alta: mudança de sinal de negativo (tendência de baixa) para cima
        bullish_reversal = regime_reversal and prev_trend_score < 0

        # Entrada menos restritiva: tendência se recuperando ativamente de uma tendência de baixa significativa.
        # Requisitos:
        #   1. a barra anterior estava em uma tendência de baixa real (< -0.5, não apenas ligeiramente negativa)
        #   2. a tendência está melhorando ativamente (trend_score > prev_trend_score)
        #   3. agora está na zona fraca/neutra (abs < 0.3)
        bullish_consolidation = (
            prev_trend_score < -0.5
            and trend_score > prev_trend_score
            and abs(trend_score) < 0.3
        )

        # 2. Faixa ATR = high_atr (reversão volátil — momentum real)
        atr_bucket = str(last.get("atr_bucket", "unknown"))
        atr_is_high = atr_bucket == "high_atr"

        # 3. RSI: sem filtro rígido (o agrupamento H27 tinha a faixa RSI "unknown")
        rsi_value = float(last.get("rsi", 50.0))
        rsi_ok = True

        # 4. Faixa de volume = low_volume (exaustão dos vendedores antes da recuperação)
        volume_bucket = str(last.get("volume_bucket", "unknown"))
        volume_is_low = volume_bucket == "low_volume"

        # 5. Posição de Bollinger = inside_band (não está em um extremo; a reversão ainda está se formando)
        bollinger_pos = str(last.get("bollinger_position", "unknown"))
        bb_inside = bollinger_pos == "inside_band"

        # Pontuação de confiança
        confidence = 0.0
        if bullish_reversal:
            confidence += 0.3
        if bullish_consolidation:
            confidence += 0.2
        if atr_is_high:
            confidence += 0.2
        if volume_is_low:
            confidence += 0.15
        if bb_inside:
            confidence += 0.15

        signal = SignalType.HOLD

        if bullish_reversal and atr_is_high and rsi_ok and volume_is_low and bb_inside:
            if confidence >= self._score_min:
                signal = SignalType.BUY
        elif bullish_consolidation and atr_is_high and volume_is_low and bb_inside:
            # Caminho menos restritivo: entrada em consolidação após tendência de baixa + volatilidade + volume baixo + BB dentro da banda
            if confidence >= self._score_min * 0.9:
                signal = SignalType.BUY

        # LONG: stop abaixo da entrada; take-profit acima da entrada
        if signal == SignalType.BUY:
            stop_loss = price - (self._atr_stop_multiplier * atr)  # Long: SL abaixo
            risk = price - stop_loss
            reward = risk * self._risk_reward_ratio
            take_profit = price + reward
        else:
            stop_loss = None
            take_profit = None

        metadata = {
            "bullish_reversal": bullish_reversal,
            "bullish_consolidation": bullish_consolidation,
            "atr_bucket": atr_bucket,
            "volume_bucket": volume_bucket,
            "bollinger_position": bollinger_pos,
            "trend_score": trend_score,
            "prev_trend_score": prev_trend_score,
            "confidence": confidence,
            "atr": atr,
            "rsi": rsi_value,
            "reason": self._entry_reason(signal, bullish_reversal or bullish_consolidation, atr_is_high, volume_is_low, bb_inside),
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=confidence,
            stop_loss=stop_loss,
            take_profit=take_profit,
            metadata=metadata,
        )

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        """
        Gera um sinal SELL para fechar a posição LONG quando o regime de alta se rompe.

        Sai antecipadamente quando a tendência volta a ficar claramente baixista — isto é,
        quando a recuperação que acionou o BUY falhou ou se reverteu.
        O mecanismo também encerra a posição por stop-loss / take-profit de forma independente.
        """
        self._assert_initialized()

        last = df.iloc[-1]
        price = float(last["close"])
        timestamp = last.name.to_pydatetime()  # type: ignore[union-attr]

        regime_reversal = bool(last.get("regime_reversal", False))
        trend_score = float(last.get("trend_score", 0.0))

        # Sai de LONG quando a tendência voltar a ficar claramente de baixa
        exit_trend_bearish = trend_score < -0.3          # Tendência de baixa forte restabelecida
        regime_back_to_bearish = not regime_reversal and trend_score < -0.2  # Tendência de baixa persistente

        signal = SignalType.HOLD
        if exit_trend_bearish or regime_back_to_bearish:
            signal = SignalType.SELL  # Encerrar posição long = sinal SELL

        metadata = {
            "reason": "trend_bearish" if exit_trend_bearish else "regime_back_to_bearish",
            "trend_score": trend_score,
            "price": price,
            "entry_price": entry_price,
            "pnl": price - entry_price,  # Positivo para uma posição long lucrativa
        }

        return StrategySignal(
            signal=signal,
            price=price,
            timestamp=timestamp,
            score=1.0 if signal != SignalType.HOLD else 0.0,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def score(self, df: pd.DataFrame) -> float:
        """
        Calcula a pontuação geral de confiança da estratégia.

        Usada pelo framework para avaliar a qualidade do sinal.
        """
        self._assert_initialized()

        last = df.iloc[-1]

        signal = self.entry_signal(df)
        return signal.score

    # ------------------------------------------------------------------
    # Funções auxiliares
    # ------------------------------------------------------------------

    @staticmethod
    def _entry_reason(
        signal: SignalType,
        regime_reversal: bool,
        atr_is_high: bool,
        volume_is_low: bool,
        bb_inside: bool,
    ) -> str:
        """Descreve por que o sinal de entrada foi gerado."""
        if signal == SignalType.BUY:
            conditions = []
            if regime_reversal:
                conditions.append("bullish_reversal")
            if atr_is_high:
                conditions.append("atr_high")
            if volume_is_low:
                conditions.append("volume_low")
            if bb_inside:
                conditions.append("bb_inside")
            return " + ".join(conditions) if conditions else "reversal_pattern"
        return "no_signal"

    def _assert_initialized(self) -> None:
        """Verifica se todos os indicadores foram inicializados."""
        if not all(
            [self._ema_fast, self._ema_slow, self._rsi, self._bb, self._atr]
        ):
            raise RuntimeError(
                f"{self.name} not initialized. Call initialize() first."
            )
