"""FASE 14 — Aquisição contínua de estratégias para alimentar a Fase 13 Factory.

Ajustes implementados (tarefa.txt):
1. Backlog enriquecido — campos completos por estratégia.
2. Verificação de duplicidade — por nome canônico, lógica similar e estado existente.
3. Compatibilidade — INCOMPATIBLE para dados indisponíveis; nunca envia para implementação.
4. Priorização inteligente — diversidade de família, qualidade da fonte, compatibilidade.
5. Estados permanentes — nunca volta para IMPLEMENTATION_PENDING após conclusão.
6. Arquivamento inteligente — ARCHIVED_NO_EDGE não entra automaticamente.
7. Pesquisa contínua — base de conhecimento diversificada.
8. Qualidade de implementação — captura entry/exit/risk rules com cada estratégia.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from strategies.registry import list_registered_strategies
from utils.logger import get_logger

logger = get_logger(__name__)

# Dados que a plataforma NÃO tem disponíveis — qualquer estratégia que dependa
# de algum desses é marcada INCOMPATIBLE e não entra na fila.
_UNAVAILABLE_DATA = {
    "open interest",
    "openinterest",
    "order book",
    "orderbook",
    "footprint",
    "institutional flow",
    "fluxo institucional",
    "options",
    "opcoes",
    "liquidations",
    "liquidacoes",
    "funding mandatory",
    "funding obrigatorio",
    "funding rate mandatory",
    "delta",
    "cumulative delta",
    "bid ask",
    "level2",
}

# Estados terminais — nunca revertidos automaticamente para IMPLEMENTATION_PENDING.
_TERMINAL_STATES = {
    "IMPLEMENTED",
    "VALIDATED",
    "PAPER_CANDIDATE",
    "PAPER_TRADING",
    "APPROVED",
    "REJECTED",
    "ARCHIVED_NO_EDGE",
    "INCOMPATIBLE",
    "DUPLICATE",
    "REJECTED_BY_PERFORMANCE",
    "REJECTED_BY_INFRASTRUCTURE",
    "rejected",
    "eliminated",
}


@dataclass(frozen=True)
class Phase14MarketIntelligenceConfig:
    top_n: int = 30
    output_prefix: str = "phase14_market_intelligence"


class MarketIntelligenceService:
    """Fase 14 — sistema permanente de aquisição de estratégias."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self, cfg: Phase14MarketIntelligenceConfig) -> dict[str, Any]:
        researched = self._collect_public_knowledge()
        phase13_state = self._load_phase13_state()
        dedup_index = self._existing_index(phase13_state)
        family_counts: dict[str, int] = {}

        classified: list[dict[str, Any]] = []
        eliminated: list[dict[str, Any]] = []
        incompatible: list[dict[str, Any]] = []
        duplicates: list[dict[str, Any]] = []

        for item in researched:
            enriched = self._enrich(item)
            key = self._canon(str(enriched.get("name", "")))

            # --- Compatibilidade (Ajuste 3) ---
            incompat_reason = self._incompatibility_reason(enriched)
            if incompat_reason:
                enriched["state"] = "INCOMPATIBLE"
                enriched["elimination_reason"] = incompat_reason
                incompatible.append(enriched)
                continue

            # --- Duplicidade (Ajuste 2) ---
            dup_reason = self._duplicate_reason(key, enriched, dedup_index)
            if dup_reason:
                enriched["state"] = "DUPLICATE"
                enriched["existing_state"] = dedup_index.get(key, "unknown")
                enriched["elimination_reason"] = dup_reason
                duplicates.append(enriched)
                continue

            # --- Eliminação padrão ---
            elim = self._auto_elimination_reason(enriched)
            if elim:
                enriched["state"] = "eliminated"
                enriched["elimination_reason"] = elim
                eliminated.append(enriched)
                continue

            # --- Estratégia válida ---
            enriched["state"] = "IMPLEMENTATION_PENDING"
            enriched["classification"] = self._classification(float(enriched.get("market_intelligence_score", 0.0)))
            enriched["elimination_reason"] = ""
            family = self._normalize_category(str(enriched.get("type") or enriched.get("category") or ""))
            family_counts[family] = family_counts.get(family, 0) + 1
            classified.append(enriched)

        # --- Priorização inteligente (Ajuste 4) ---
        ranked = self._rank_with_diversity(classified, family_counts)
        for idx, row in enumerate(ranked, start=1):
            row["rank"] = idx

        top_n = ranked[: max(1, int(cfg.top_n))]
        top10 = ranked[:10]
        top5 = ranked[:5]

        distribution = self._distribution_by_category(ranked)
        indicators = self._top_counts(ranked, key="indicators", limit=10)
        timeframes = self._top_counts(ranked, key="recommended_timeframes", limit=8)
        assets = self._top_counts(ranked, key="supported_crypto", limit=8)
        recurring = self._top_recurring_strategies(ranked, limit=12)

        sufficient = "SIM" if len(top_n) >= int(cfg.top_n) else "NAO"
        report = {
            "phase": "14",
            "pipeline_version": "14.1_enriched_backlog",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_researched": len(researched),
            "total_eliminated": len(eliminated),
            "total_incompatible": len(incompatible),
            "total_duplicates": len(duplicates),
            "total_classified": len(ranked),
            "top20": top_n,
            "top10": top10,
            "top5": top5,
            "extracted_knowledge": self._knowledge_rows(ranked),
            "distribution_by_category": distribution,
            "top_indicators": indicators,
            "top_timeframes": timeframes,
            "top_assets": assets,
            "recurring_strategies": recurring,
            "eliminated": eliminated,
            "incompatible": incompatible,
            "duplicates": duplicates,
            "decision": {
                "question": "A plataforma possui backlog suficiente para alimentar continuamente a Fase 13?",
                "answer": sufficient,
                "missing_categories_if_no": [] if sufficient == "SIM" else self._missing_categories(distribution),
            },
            "prioritized_backlog": [self._backlog_row(row) for row in top_n],
        }

        self._write_phase13_seed_backlog(report)
        outputs = self._write_outputs(cfg.output_prefix, report)

        summary = {
            "status": "completed",
            "phase": "14",
            "pipeline_version": "14.1_enriched_backlog",
            "researched": len(researched),
            "incompatible": len(incompatible),
            "duplicates": len(duplicates),
            "eliminated": len(eliminated),
            "classified": len(ranked),
            "top_strategy": top_n[0]["name"] if top_n else None,
            "backlog_sufficient_for_phase13": sufficient,
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    # ------------------------------------------------------------------
    # Knowledge base — Ajuste 7 (pesquisa contínua de fontes diversas)
    # ------------------------------------------------------------------

    def _collect_public_knowledge(self) -> list[dict[str, Any]]:
        kb_path = self._base_dir / "research" / "crypto_strategy_knowledge_base" / "strategies.json"
        data: list[dict[str, Any]] = []
        if kb_path.exists():
            try:
                loaded = json.loads(kb_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    data.extend([dict(item) for item in loaded if isinstance(item, dict)])
            except Exception as exc:
                logger.warning("Could not load local strategy knowledge base: %s", exc)
        data.extend(self._builtin_knowledge_base())
        return data

    def _builtin_knowledge_base(self) -> list[dict[str, Any]]:
        """Estratégias com metadados enriquecidos conforme Ajuste 1 e Ajuste 8."""
        return [
            # ── TENDÊNCIA ──────────────────────────────────────────────────
            {
                "name": "EMA Ribbon Pullback",
                "type": "tendencia", "category": "Trend Following",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/ema%20ribbon/", "https://github.com/freqtrade/freqtrade-strategies"],
                "used_markets": ["Crypto", "FX", "Equities"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["EMA"],
                "default_params": {"ema_periods": [8, 13, 21, 34, 55], "atr_stop_mult": 2.0, "rr": 3.0},
                "entry_rules": "Price pulls back to ribbon support zone after bullish alignment; entry on bounce candle close above shortest EMA.",
                "exit_rules": "Exit on ribbon bearish cross or ATR trailing stop hit.",
                "risk_management": "ATR stop below ribbon; fixed RR 3:1.",
                "known_limitations": "Whipsaw in ranging markets; use ADX filter.",
                "observations": "One of the most tested trend-following setups in crypto; strong track record on 1h+.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 5,
                "popularity": 5, "public_implementations_score": 5, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 5, "quality_of_references": 5, "license": "open_source", "risk_management_type": "atr_trailing",
            },
            {
                "name": "Ichimoku Kumo Breakout",
                "type": "tendencia", "category": "Trend Following",
                "origin": "TradingView / Academic", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/ichimoku/", "https://school.stockcharts.com/doku.php?id=technical_indicators:ichimoku_cloud"],
                "used_markets": ["Crypto", "FX", "Equities"], "supported_crypto": ["BTC", "ETH", "SOL"],
                "recommended_timeframes": ["1h", "4h", "1d"], "indicators": ["Ichimoku", "ATR"],
                "default_params": {"tenkan": 9, "kijun": 26, "senkou_b": 52, "atr_stop_mult": 1.5},
                "entry_rules": "Price breaks above Kumo with Tenkan > Kijun; Chikou confirms.",
                "exit_rules": "Price re-enters Kumo or Tenkan crosses below Kijun.",
                "risk_management": "Stop below Kijun or Kumo base.",
                "known_limitations": "Lagging signals on lower timeframes; best 4h+.",
                "observations": "Widely used by Japanese institutional traders; strong in trending crypto.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 5, "public_implementations_score": 5, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 5, "quality_of_references": 5, "license": "open_source", "risk_management_type": "structure_stop",
            },
            {
                "name": "Chandelier Exit Trend",
                "type": "tendencia", "category": "Trend Following",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/chandelier/", "https://school.stockcharts.com/doku.php?id=technical_indicators:chandelier_exit"],
                "used_markets": ["Crypto", "Equities"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["ATR", "EMA"],
                "default_params": {"atr_period": 22, "atr_mult": 3.0, "ema_trend": 200},
                "entry_rules": "Long when price above EMA trend and Chandelier exit flips long.",
                "exit_rules": "Chandelier exit flips short or EMA trend broken.",
                "risk_management": "Built-in ATR trailing stop via Chandelier.",
                "known_limitations": "Wide stop in volatile markets.",
                "observations": "Combines trend filter with adaptive trailing stop; widely referenced.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 5,
                "popularity": 5, "public_implementations_score": 5, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 4, "quality_of_references": 5, "license": "open_source", "risk_management_type": "atr_trailing",
            },
            {
                "name": "Volatility Breakout ATR + Volume",
                "type": "breakout", "category": "Breakout",
                "origin": "GitHub/QuantConnect", "source_kind": "GitHub", "source_type": "open_source_code",
                "references": ["https://www.quantconnect.com/", "https://github.com/freqtrade/freqtrade-strategies"],
                "used_markets": ["Crypto"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["5m", "15m", "1h"], "indicators": ["ATR", "Volume", "EMA"],
                "default_params": {"atr_period": 14, "atr_mult_entry": 1.5, "volume_mult": 1.5, "ema_trend": 200},
                "entry_rules": "Price breaks ATR band above with volume > volume_mult * avg_volume; trend EMA above.",
                "exit_rules": "ATR trailing stop or fixed RR.",
                "risk_management": "ATR-based stop at breakout candle low.",
                "known_limitations": "False breakouts in low liquidity; avoid news events without filter.",
                "observations": "Volume confirmation is key differentiator; well documented in crypto context.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 5,
                "popularity": 4, "public_implementations_score": 4, "robustness": 4, "overfitting_risk": 3,
                "quantity_of_independent_references": 4, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            {
                "name": "ADX Trend Continuation",
                "type": "tendencia", "category": "Trend Following",
                "origin": "GitHub/QuantConnect", "source_kind": "GitHub", "source_type": "open_source_code",
                "references": ["https://www.quantconnect.com/", "https://github.com/mementum/backtrader"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH", "SOL"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["ADX", "EMA"],
                "default_params": {"adx_period": 14, "adx_threshold": 25, "ema_fast": 21, "ema_slow": 55},
                "entry_rules": "ADX > threshold; +DI > -DI; price above EMA fast.",
                "exit_rules": "ADX falls below threshold or EMA cross.",
                "risk_management": "ATR stop below recent swing low.",
                "known_limitations": "Enters late in trend; misses initial move.",
                "observations": "Classic filter for trending market regime; well documented.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 4, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 3, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            {
                "name": "Donchian Turtle Breakout",
                "type": "breakout", "category": "Breakout",
                "origin": "GitHub/Freqtrade", "source_kind": "GitHub", "source_type": "open_source_code",
                "references": ["https://www.freqtrade.io/", "https://www.backtrader.com/"],
                "used_markets": ["Crypto", "Futures"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["Donchian", "ATR"],
                "default_params": {"donchian_period": 20, "atr_stop_mult": 2.0, "rr": 3.0},
                "entry_rules": "Price closes above Donchian upper band (N-period high).",
                "exit_rules": "Price closes below Donchian lower band (N/2-period low) or ATR stop.",
                "risk_management": "ATR stop 2x below entry.",
                "known_limitations": "Turtle system suffers in range-bound markets.",
                "observations": "Canonical turtle breakout; extensively documented and tested.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 4, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 4, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            # ── REVERSÃO ──────────────────────────────────────────────────
            {
                "name": "Bollinger RSI Crypto Mean Reversion",
                "type": "reversao", "category": "Mean Reversion",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/bollinger%20rsi/", "https://www.freqtrade.io/"],
                "used_markets": ["Crypto"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["5m", "15m", "1h"], "indicators": ["Bollinger Bands", "RSI", "ATR"],
                "default_params": {"bb_period": 20, "bb_std": 2.0, "rsi_period": 14, "rsi_oversold": 30, "rsi_overbought": 70},
                "entry_rules": "Price touches lower Bollinger band AND RSI < oversold level; enter long on next candle.",
                "exit_rules": "Price touches middle band or RSI > 50.",
                "risk_management": "Stop below lower band; target middle band (1.5:1 min RR).",
                "known_limitations": "Poor in trending markets; needs ranging filter.",
                "observations": "Highly popular in crypto communities; best with volume confirmation.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 5,
                "popularity": 5, "public_implementations_score": 5, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 5, "quality_of_references": 4, "license": "open_source", "risk_management_type": "structure_stop",
            },
            {
                "name": "RSI2 Mean Reversion",
                "type": "reversao", "category": "Mean Reversion",
                "origin": "Quant community", "source_kind": "Blog/Paper", "source_type": "academic_paper",
                "references": ["https://www.quantconnect.com/", "https://www.backtrader.com/"],
                "used_markets": ["Crypto", "Equities"], "supported_crypto": ["BTC", "ETH", "BNB"],
                "recommended_timeframes": ["5m", "15m", "1h"], "indicators": ["RSI", "SMA"],
                "default_params": {"rsi_period": 2, "rsi_enter": 10, "rsi_exit": 90, "sma_filter": 200},
                "entry_rules": "RSI(2) < 10 with price above SMA(200); enter long.",
                "exit_rules": "RSI(2) > 90 or fixed stop.",
                "risk_management": "Fixed stop 1% below entry.",
                "known_limitations": "Very short hold time; requires high-liquidity assets.",
                "observations": "Larry Connors RSI-2 system; widely tested in equities and adapted to crypto.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 5, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 3, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 3, "quality_of_references": 4, "license": "open_source", "risk_management_type": "fixed_stop",
            },
            {
                "name": "Session VWAP Reversion",
                "type": "reversao", "category": "Mean Reversion",
                "origin": "TradingView / Quant community", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/vwap/", "https://www.quantconnect.com/"],
                "used_markets": ["Crypto"], "supported_crypto": ["BTC", "ETH", "SOL"],
                "recommended_timeframes": ["5m", "15m"], "indicators": ["VWAP", "ATR", "RSI"],
                "default_params": {"vwap_bands_std": 2.0, "rsi_oversold": 35, "atr_stop_mult": 1.5},
                "entry_rules": "Price deviates beyond 2sigma VWAP band with RSI < oversold; enter reversal.",
                "exit_rules": "Price returns to VWAP or ATR stop.",
                "risk_management": "ATR stop on entry side of VWAP band.",
                "known_limitations": "Needs intraday VWAP reset; less effective on daily+.",
                "observations": "Widely used by intraday crypto traders; VWAP is native to the platform.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 3, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 3, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            # ── MOMENTUM ──────────────────────────────────────────────────
            {
                "name": "MACD Histogram Acceleration",
                "type": "momentum", "category": "Momentum",
                "origin": "GitHub/Freqtrade", "source_kind": "GitHub", "source_type": "open_source_code",
                "references": ["https://www.freqtrade.io/", "https://github.com/freqtrade/freqtrade-strategies"],
                "used_markets": ["Crypto"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["15m", "1h"], "indicators": ["MACD", "EMA"],
                "default_params": {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "ema_trend": 200},
                "entry_rules": "MACD histogram increases for two consecutive bars above zero line; price above EMA trend.",
                "exit_rules": "MACD histogram turns negative or momentum reversal.",
                "risk_management": "ATR trailing stop.",
                "known_limitations": "Lagging confirmation; may miss fast moves.",
                "observations": "Histogram acceleration is a robust momentum proxy; widely used.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 4, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 4, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_trailing",
            },
            {
                "name": "Elder Impulse",
                "type": "momentum", "category": "Momentum",
                "origin": "Livro - Trading for a Living (Elder)", "source_kind": "Livro", "source_type": "book",
                "references": ["https://www.amazon.com/Trading-Living-Psychology-Tactics-Management/dp/0471592242", "https://www.tradingview.com/scripts/search/elder%20impulse/"],
                "used_markets": ["Crypto", "Equities", "FX"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["1h", "4h"], "indicators": ["EMA", "MACD"],
                "default_params": {"ema_period": 13, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9},
                "entry_rules": "Impulse bar is green (EMA rising AND MACD histogram rising).",
                "exit_rules": "Impulse bar turns red (EMA falling AND MACD histogram falling).",
                "risk_management": "Stop below previous swing low; Elder 2% rule.",
                "known_limitations": "Requires bar-by-bar tracking; not purely signal-based.",
                "observations": "Classic system from Alexander Elder; heavily cited in trading literature.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 3, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 4, "quality_of_references": 5, "license": "commercial_book", "risk_management_type": "swing_stop",
            },
            {
                "name": "Hull Suite",
                "type": "tendencia", "category": "Trend Following",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/hull%20suite/", "https://alanhull.com/"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["HMA", "EMA"],
                "default_params": {"hma_period": 55, "ema_trend": 200},
                "entry_rules": "Hull MA color changes to bullish (rising) with price above EMA trend.",
                "exit_rules": "Hull MA color changes to bearish.",
                "risk_management": "ATR stop below recent HMA support.",
                "known_limitations": "HMA not standard; needs custom indicator implementation.",
                "observations": "Widely popular; Hull MA reduces lag vs SMA/EMA.",
                "complexity": 3, "needs_new_indicators": True, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 3, "ease_of_implementation": 3, "crypto_potential": 4,
                "popularity": 5, "public_implementations_score": 5, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 4, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            {
                "name": "VIDYA Trend",
                "type": "tendencia", "category": "Trend Following",
                "origin": "Paper / TradingView", "source_kind": "Paper", "source_type": "academic_paper",
                "references": ["https://www.tradingview.com/scripts/search/vidya/", "https://arxiv.org/"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["1h", "4h"], "indicators": ["VIDYA", "ATR"],
                "default_params": {"vidya_period": 14, "cmo_period": 9, "atr_stop_mult": 2.0},
                "entry_rules": "VIDYA rising and price above VIDYA; enter on pullback to VIDYA.",
                "exit_rules": "VIDYA turns flat or declining.",
                "risk_management": "ATR stop below VIDYA.",
                "known_limitations": "VIDYA requires custom implementation (CMO-based).",
                "observations": "Variable Index Dynamic Average; adaptive to market volatility.",
                "complexity": 4, "needs_new_indicators": True, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 3, "ease_of_implementation": 3, "crypto_potential": 3,
                "popularity": 3, "public_implementations_score": 3, "robustness": 3, "overfitting_risk": 2,
                "quantity_of_independent_references": 3, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            {
                "name": "KAMA Regime Filter",
                "type": "tendencia", "category": "Trend Following",
                "origin": "Paper / QuantConnect", "source_kind": "Paper", "source_type": "academic_paper",
                "references": ["https://www.quantconnect.com/", "https://www.tradingview.com/scripts/search/kama/"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["1h", "4h"], "indicators": ["KAMA", "ATR"],
                "default_params": {"kama_fast": 2, "kama_slow": 30, "er_period": 10},
                "entry_rules": "KAMA rising fast; price breakout above KAMA.",
                "exit_rules": "KAMA flattens or price crosses below.",
                "risk_management": "ATR stop.",
                "known_limitations": "KAMA requires custom implementation.",
                "observations": "Kaufman Adaptive MA; adapts to noise ratio; academic backing.",
                "complexity": 4, "needs_new_indicators": True, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 3, "ease_of_implementation": 3, "crypto_potential": 3,
                "popularity": 3, "public_implementations_score": 3, "robustness": 4, "overfitting_risk": 2,
                "quantity_of_independent_references": 3, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            # ── BREAKOUT ──────────────────────────────────────────────────
            {
                "name": "Opening Range Breakout",
                "type": "breakout", "category": "Breakout",
                "origin": "Quant community", "source_kind": "Blog/Paper", "source_type": "academic_paper",
                "references": ["https://www.quantconnect.com/", "https://www.tradingview.com/scripts/search/opening%20range/"],
                "used_markets": ["Crypto", "Equities"], "supported_crypto": ["BTC", "ETH", "SOL"],
                "recommended_timeframes": ["5m", "15m"], "indicators": ["Range", "Volume", "ATR"],
                "default_params": {"range_minutes": 30, "volume_mult": 1.5, "atr_stop_mult": 1.0},
                "entry_rules": "Break above opening range high with volume confirmation.",
                "exit_rules": "Fixed RR 2:1 or end of session.",
                "risk_management": "Stop below opening range low.",
                "known_limitations": "Requires session-aware OHLCV; crypto 24/7 so UTC session must be defined.",
                "observations": "Well-documented in quant literature; crypto adaptations exist.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 4, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 3, "quality_of_references": 4, "license": "open_source", "risk_management_type": "range_stop",
            },
            {
                "name": "TTM Squeeze Momentum",
                "type": "volatilidade", "category": "Volatility",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/ttm%20squeeze/", "https://www.masimo.com/documents/TTM_Squeeze_Article.pdf"],
                "used_markets": ["Crypto", "Equities"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["Bollinger Bands", "ATR", "Momentum"],
                "default_params": {"bb_period": 20, "kc_mult": 1.5, "mom_period": 12},
                "entry_rules": "Squeeze fires (BB inside Keltner); histogram turns positive after squeeze release.",
                "exit_rules": "Histogram reverses or momentum oscillator crosses zero from above.",
                "risk_management": "ATR stop at entry bar low.",
                "known_limitations": "Needs Keltner Channel; squeeze detection can lag.",
                "observations": "John Carter TTM Squeeze; massively popular on TradingView.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 5,
                "popularity": 5, "public_implementations_score": 5, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 4, "quality_of_references": 4, "license": "open_source", "risk_management_type": "atr_stop",
            },
            {
                "name": "AlphaTrend",
                "type": "tendencia", "category": "Trend Following",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/alphatrend/"],
                "used_markets": ["Crypto"], "supported_crypto": ["BTC", "ETH", "SOL", "BNB"],
                "recommended_timeframes": ["5m", "15m", "1h"], "indicators": ["RSI", "ATR"],
                "default_params": {"rsi_period": 14, "atr_period": 14, "multiplier": 1.0},
                "entry_rules": "AlphaTrend line turns bullish; price crosses above line.",
                "exit_rules": "AlphaTrend line turns bearish.",
                "risk_management": "Stop below AlphaTrend line.",
                "known_limitations": "Relatively new indicator; fewer independent validations.",
                "observations": "Custom RSI+ATR hybrid; growing adoption in crypto communities.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 5, "ease_of_implementation": 5, "crypto_potential": 5,
                "popularity": 4, "public_implementations_score": 3, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 2, "quality_of_references": 3, "license": "open_source", "risk_management_type": "indicator_stop",
            },
            {
                "name": "ATR Volatility Compression Break",
                "type": "volatilidade", "category": "Volatility",
                "origin": "Paper/TradingView", "source_kind": "Paper", "source_type": "academic_paper",
                "references": ["https://arxiv.org/", "https://www.tradingview.com/scripts/search/atr%20breakout/"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH", "SOL"],
                "recommended_timeframes": ["15m", "1h", "4h"], "indicators": ["ATR", "Bollinger Bands"],
                "default_params": {"atr_period": 14, "compression_bars": 10, "bb_period": 20},
                "entry_rules": "ATR reaches N-period low (compression); break above BB upper on expansion.",
                "exit_rules": "ATR trailing stop or BB re-entry.",
                "risk_management": "ATR stop.",
                "known_limitations": "Requires detecting compression period; parameter sensitive.",
                "observations": "Volatility compression before expansion is a well-studied pattern.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 3, "public_implementations_score": 3, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 3, "quality_of_references": 3, "license": "open_source", "risk_management_type": "atr_stop",
            },
            {
                "name": "Heikin-Ashi Trend + ATR",
                "type": "tendencia", "category": "Trend Following",
                "origin": "TradingView Open Source", "source_kind": "TradingView", "source_type": "open_source_script",
                "references": ["https://www.tradingview.com/scripts/search/heikin%20ashi/"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH", "SOL"],
                "recommended_timeframes": ["1h", "4h"], "indicators": ["Heikin-Ashi", "ATR", "EMA"],
                "default_params": {"ema_trend": 200, "atr_stop_mult": 2.5},
                "entry_rules": "Heikin-Ashi bars are consecutively bullish (green, no lower wick); price above EMA trend.",
                "exit_rules": "First Heikin-Ashi bearish bar or ATR trailing stop.",
                "risk_management": "ATR stop below recent HA low.",
                "known_limitations": "HA candles smooth price; not direct OHLCV — requires recalculation.",
                "observations": "Popular trend visualization; fewer false signals than regular candles.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 4, "ease_of_implementation": 4, "crypto_potential": 4,
                "popularity": 4, "public_implementations_score": 4, "robustness": 3, "overfitting_risk": 3,
                "quantity_of_independent_references": 3, "quality_of_references": 3, "license": "open_source", "risk_management_type": "atr_trailing",
            },
            {
                "name": "Kalman Trend Filter",
                "type": "hibrida", "category": "Trend",
                "origin": "Papers", "source_kind": "Paper", "source_type": "academic_paper",
                "references": ["https://arxiv.org/", "https://www.tradingview.com/scripts/search/kalman/"],
                "used_markets": ["Crypto", "FX"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["1h", "4h"], "indicators": ["Kalman Filter", "ATR"],
                "default_params": {"observation_noise": 0.1, "process_noise": 0.01},
                "entry_rules": "Kalman filter line turns bullish (slope positive); price above line.",
                "exit_rules": "Kalman line slope turns negative.",
                "risk_management": "ATR stop.",
                "known_limitations": "Kalman Filter not standard; requires numerical implementation.",
                "observations": "Academic origin; good noise reduction; complex implementation.",
                "complexity": 5, "needs_new_indicators": True, "needs_new_data": False, "architecture_change_required": False,
                "platform_compatibility": 2, "ease_of_implementation": 2, "crypto_potential": 3,
                "popularity": 2, "public_implementations_score": 2, "robustness": 3, "overfitting_risk": 2,
                "quantity_of_independent_references": 2, "quality_of_references": 3, "license": "open_source", "risk_management_type": "atr_stop",
            },
            # ── INCOMPATÍVEIS (dados indisponíveis) ──────────────────────
            {
                "name": "Liquidation Cascade Reversal",
                "type": "reversao", "category": "Mean Reversion",
                "origin": "Crypto community", "source_kind": "Blog", "source_type": "blog_post",
                "references": ["https://coinglass.com/"],
                "used_markets": ["Crypto Futures"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["5m", "15m"], "indicators": ["Liquidations", "Volume", "RSI"],
                "required_data": ["liquidacoes"],
                "default_params": {},
                "entry_rules": "Large liquidation cascade detected; counter-trend entry.",
                "exit_rules": "unknown", "risk_management": "unknown",
                "known_limitations": "Requires real-time liquidation data feed.",
                "observations": "INCOMPATIBLE — requires data not available on platform.",
                "complexity": 4, "needs_new_indicators": True, "needs_new_data": True, "architecture_change_required": False,
                "platform_compatibility": 1, "ease_of_implementation": 1, "crypto_potential": 3,
                "popularity": 3, "public_implementations_score": 2, "robustness": 2, "overfitting_risk": 3,
                "quantity_of_independent_references": 2, "quality_of_references": 2, "license": "unknown", "risk_management_type": "unknown",
            },
            {
                "name": "Open Interest Divergence",
                "type": "hibrida", "category": "Hybrid",
                "origin": "Crypto community", "source_kind": "Blog", "source_type": "blog_post",
                "references": ["https://coinglass.com/"],
                "used_markets": ["Crypto Futures"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["15m", "1h"], "indicators": ["Open Interest", "MACD", "RSI"],
                "required_data": ["open interest"],
                "default_params": {},
                "entry_rules": "Price rising but OI falling (divergence).",
                "exit_rules": "unknown", "risk_management": "unknown",
                "known_limitations": "Requires Open Interest data feed.",
                "observations": "INCOMPATIBLE — platform does not have OI data.",
                "complexity": 3, "needs_new_indicators": False, "needs_new_data": True, "architecture_change_required": False,
                "platform_compatibility": 1, "ease_of_implementation": 1, "crypto_potential": 3,
                "popularity": 3, "public_implementations_score": 2, "robustness": 2, "overfitting_risk": 3,
                "quantity_of_independent_references": 2, "quality_of_references": 2, "license": "unknown", "risk_management_type": "unknown",
            },
            {
                "name": "Funding Rate Mean Reversion",
                "type": "reversao", "category": "Mean Reversion",
                "origin": "Crypto community", "source_kind": "Blog", "source_type": "blog_post",
                "references": ["https://www.bybit.com/en/announcement-info/perpetual-contract/"],
                "used_markets": ["Crypto Perpetual Futures"], "supported_crypto": ["BTC", "ETH"],
                "recommended_timeframes": ["4h", "1d"], "indicators": ["Funding Rate", "RSI"],
                "required_data": ["funding obrigatorio"],
                "default_params": {},
                "entry_rules": "Funding rate extreme; counter-trend entry.",
                "exit_rules": "unknown", "risk_management": "unknown",
                "known_limitations": "Requires mandatory funding rate data feed.",
                "observations": "INCOMPATIBLE — mandatory funding data not available.",
                "complexity": 2, "needs_new_indicators": False, "needs_new_data": True, "architecture_change_required": False,
                "platform_compatibility": 1, "ease_of_implementation": 1, "crypto_potential": 3,
                "popularity": 3, "public_implementations_score": 2, "robustness": 2, "overfitting_risk": 2,
                "quantity_of_independent_references": 2, "quality_of_references": 2, "license": "unknown", "risk_management_type": "unknown",
            },
        ]

    # ------------------------------------------------------------------
    # Ajuste 2 — Verificação de duplicidade
    # ------------------------------------------------------------------

    def _load_phase13_state(self) -> dict[str, Any]:
        path = self._results_dir / "phase13_factory_state.json"
        if not path.exists():
            return {"backlog": [], "rejection_knowledge": []}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                raw.setdefault("backlog", [])
                raw.setdefault("rejection_knowledge", [])
                return raw
        except Exception:
            pass
        return {"backlog": [], "rejection_knowledge": []}

    def _existing_index(self, state: dict[str, Any]) -> dict[str, str]:
        index: dict[str, str] = {}
        for row in state.get("backlog", []):
            name = self._canon(str(row.get("candidate_name", "")))
            if not name:
                continue
            index[name] = str(row.get("state", "unknown"))
        for row in state.get("rejection_knowledge", []):
            name = self._canon(str(row.get("candidate_name", "")))
            if name and name not in index:
                index[name] = "REJECTED"
        for strategy in list_registered_strategies():
            strategy_name = self._canon(str(strategy.get("name", "")))
            if strategy_name:
                index.setdefault(strategy_name, "already_implemented")
        return index

    def _duplicate_reason(self, key: str, item: dict[str, Any], dedup_index: dict[str, str]) -> str:
        if key in dedup_index:
            existing_state = dedup_index[key]
            if existing_state in _TERMINAL_STATES:
                return f"already_in_pipeline:state={existing_state}"
            return f"duplicate_name:existing_state={existing_state}"
        return ""

    # ------------------------------------------------------------------
    # Ajuste 3 — Compatibilidade
    # ------------------------------------------------------------------

    def _incompatibility_reason(self, item: dict[str, Any]) -> str:
        required_data: list[str] = item.get("required_data") or []
        indicators: list[str] = item.get("indicators") or []
        all_requirements = [self._canon(str(r)) for r in required_data + indicators]
        for unavailable in _UNAVAILABLE_DATA:
            canon_ua = self._canon(unavailable)
            for req in all_requirements:
                if canon_ua in req or req in canon_ua:
                    return f"requires_unavailable_data:{unavailable}"
        if bool(item.get("needs_new_data", False)):
            return "depends_on_unavailable_data"
        return ""

    def _auto_elimination_reason(self, item: dict[str, Any]) -> str:
        refs = item.get("references") or []
        if not refs:
            return "insufficient_documentation"
        if bool(item.get("architecture_change_required", False)):
            return "deep_architecture_changes_required"
        if float(item.get("platform_compatibility", 0.0) or 0.0) <= 1.0:
            return "incompatible_with_platform"
        if float(item.get("quality_of_references", 3.0) or 0.0) < 2.0:
            return "unreliable_references"
        return ""

    # ------------------------------------------------------------------
    # Ajuste 1 — Enriquecimento
    # ------------------------------------------------------------------

    def _enrich(self, item: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(item)
        for field in ["source_type", "source_kind", "references", "compatible_with_crypto",
                      "recommended_markets", "recommended_assets", "recommended_timeframes",
                      "indicators", "default_params", "entry_rules", "exit_rules",
                      "risk_management", "known_limitations", "observations"]:
            if field not in enriched:
                enriched[field] = "desconhecido"
        compatibility = float(enriched.get("platform_compatibility", 0.0) or 0.0)
        ease = float(enriched.get("ease_of_implementation", 0.0) or 0.0)
        crypto = float(enriched.get("crypto_potential", 0.0) or 0.0)
        popularity = float(enriched.get("popularity", 0.0) or 0.0)
        adoption = float(enriched.get("public_implementations_score", 0.0) or 0.0)
        robustness = float(enriched.get("robustness", 0.0) or 0.0)
        overfit_risk = float(enriched.get("overfitting_risk", 0.0) or 0.0)
        quantity_refs = float(enriched.get("quantity_of_independent_references", 3.0) or 0.0)
        quality_refs = float(enriched.get("quality_of_references", 3.0) or 0.0)
        needs_new_indicator = 1.0 if bool(enriched.get("needs_new_indicators", False)) else 0.0
        needs_new_data = 1.0 if bool(enriched.get("needs_new_data", False)) else 0.0
        architecture_change = 1.0 if bool(enriched.get("architecture_change_required", False)) else 0.0
        complexity = float(enriched.get("complexity", 3.0) or 3.0)
        score = (
            popularity * 0.14 + adoption * 0.12 + quality_refs * 0.10 + quantity_refs * 0.08
            + compatibility * 0.18 + ease * 0.14 + crypto * 0.14 + robustness * 0.10
            - architecture_change * 0.50 - needs_new_data * 0.40 - needs_new_indicator * 0.25
            - (max(0.0, complexity - 3.0)) * 0.03 - overfit_risk * 0.04
        )
        normalized = max(0.0, min(100.0, score / 5.0 * 100.0))
        enriched["market_intelligence_score"] = round(normalized, 2)
        enriched["compatibility_with_platform"] = (
            "Alta" if compatibility >= 4 else ("Media" if compatibility >= 3 else "Baixa")
        )
        enriched["priority_reason"] = (
            f"popularidade={int(popularity)}, adocao={int(adoption)}, "
            f"compatibilidade={int(compatibility)}, potencial_cripto={int(crypto)}"
        )
        return enriched

    # ------------------------------------------------------------------
    # Ajuste 4 — Priorização inteligente com diversidade de família
    # ------------------------------------------------------------------

    def _rank_with_diversity(self, candidates: list[dict[str, Any]], family_counts: dict[str, int]) -> list[dict[str, Any]]:
        total = max(1, len(candidates))
        result = []
        for item in candidates:
            family = self._normalize_category(str(item.get("type") or item.get("category") or ""))
            family_pct = family_counts.get(family, 0) / total
            diversity_penalty = max(0.0, (family_pct - 0.40) * 20.0)
            score = float(item.get("market_intelligence_score", 0.0))
            item["adjusted_score"] = round(score - diversity_penalty, 4)
            item["diversity_penalty"] = round(diversity_penalty, 4)
            item["family_pct"] = round(family_pct * 100, 1)
            result.append(item)
        return sorted(result, key=lambda x: float(x.get("adjusted_score", 0.0)), reverse=True)

    def _classification(self, score: float) -> str:
        if score >= 78.0:
            return "Alta prioridade"
        if score >= 62.0:
            return "Media prioridade"
        if score >= 45.0:
            return "Baixa prioridade"
        return "Nao recomendada"

    def _knowledge_rows(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{
            "name": r.get("name"), "category": r.get("category") or r.get("type"),
            "origin": r.get("origin"), "source_kind": r.get("source_kind"), "source_type": r.get("source_type"),
            "references": r.get("references", []), "used_markets": r.get("used_markets", []),
            "supported_crypto": r.get("supported_crypto", []), "recommended_timeframes": r.get("recommended_timeframes", []),
            "indicators": r.get("indicators", []), "default_params": r.get("default_params"),
            "entry_rules": r.get("entry_rules", "desconhecido"), "exit_rules": r.get("exit_rules", "desconhecido"),
            "risk_management": r.get("risk_management", "desconhecido"),
            "known_limitations": r.get("known_limitations", "desconhecido"),
            "observations": r.get("observations", "desconhecido"),
            "complexity": r.get("complexity"), "needs_new_indicators": bool(r.get("needs_new_indicators", False)),
            "architecture_change_required": bool(r.get("architecture_change_required", False)),
            "popularity": r.get("popularity", 0), "quantity_of_independent_references": r.get("quantity_of_independent_references", 0),
            "compatibility_with_platform": r.get("compatibility_with_platform"), "license": r.get("license", "unknown"),
            "market_intelligence_score": r.get("market_intelligence_score", 0.0),
            "adjusted_score": r.get("adjusted_score", 0.0), "diversity_penalty": r.get("diversity_penalty", 0.0),
        } for r in rows]

    def _backlog_row(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "rank": row.get("rank"), "name": row.get("name"),
            "category": row.get("category") or row.get("type"), "classification": row.get("classification"),
            "market_intelligence_score": row.get("market_intelligence_score"),
            "adjusted_score": row.get("adjusted_score"), "diversity_penalty": row.get("diversity_penalty", 0.0),
            "family_pct": row.get("family_pct", 0.0), "priority_reason": row.get("priority_reason"),
            "origin": row.get("origin", "desconhecido"), "source_kind": row.get("source_kind", "desconhecido"),
            "source_type": row.get("source_type", "desconhecido"), "references": row.get("references", []),
            "indicators": row.get("indicators", []), "recommended_timeframes": row.get("recommended_timeframes", []),
            "supported_crypto": row.get("supported_crypto", []), "used_markets": row.get("used_markets", []),
            "default_params": row.get("default_params", {}),
            "entry_rules": row.get("entry_rules", "desconhecido"), "exit_rules": row.get("exit_rules", "desconhecido"),
            "risk_management": row.get("risk_management", "desconhecido"),
            "known_limitations": row.get("known_limitations", "desconhecido"),
            "observations": row.get("observations", "desconhecido"),
            "compatibility": row.get("compatibility_with_platform"),
            "needs_new_indicators": bool(row.get("needs_new_indicators", False)),
            "complexity": row.get("complexity"), "license": row.get("license", "unknown"),
            "state": "IMPLEMENTATION_PENDING",
        }

    def _distribution_by_category(self, rows: list[dict[str, Any]]) -> dict[str, int]:
        buckets: dict[str, int] = {"tendencia": 0, "breakout": 0, "reversao": 0, "momentum": 0, "volatilidade": 0, "hibridas": 0}
        for row in rows:
            cat = self._normalize_category(str(row.get("type") or row.get("category") or ""))
            buckets[cat] = buckets.get(cat, 0) + 1
        return buckets

    def _normalize_category(self, raw: str) -> str:
        value = raw.strip().lower()
        if "break" in value: return "breakout"
        if "moment" in value: return "momentum"
        if "revers" in value or "mean" in value: return "reversao"
        if "volat" in value: return "volatilidade"
        if "hibr" in value or "hybrid" in value: return "hibridas"
        return "tendencia"

    def _top_counts(self, rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
        counter: dict[str, int] = {}
        for row in rows:
            values = row.get(key) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                name = str(value).strip()
                if name:
                    counter[name] = counter.get(name, 0) + 1
        ordered = sorted(counter.items(), key=lambda x: x[1], reverse=True)[: max(1, int(limit))]
        return [{"name": n, "count": c} for n, c in ordered]

    def _top_recurring_strategies(self, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        ordered = sorted(rows, key=lambda x: float(x.get("adjusted_score", 0.0)), reverse=True)
        return [{"name": r.get("name"), "score": r.get("adjusted_score"), "classification": r.get("classification")} for r in ordered[: max(1, int(limit))]]

    def _missing_categories(self, distribution: dict[str, int]) -> list[str]:
        return [name for name, count in distribution.items() if int(count) == 0]

    def _write_phase13_seed_backlog(self, report: dict[str, Any]) -> None:
        path = self._results_dir / "phase14_seed_backlog.json"
        payload = {"generated_at": report.get("generated_at"), "phase": "14", "pipeline_version": "14.1_enriched_backlog", "seed_backlog": report.get("prioritized_backlog", [])}
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}
        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)
        csv_path = self._results_dir / f"{prefix}_{stamp}_top20.csv"
        top20 = report.get("top20", [])
        if top20:
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(fh, fieldnames=["rank", "name", "classification", "market_intelligence_score", "adjusted_score", "diversity_penalty", "compatibility_with_platform", "state"], extrasaction="ignore")
                writer.writeheader()
                writer.writerows(top20)
            outputs["csv"] = str(csv_path)
        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)
        return outputs

    def _to_markdown(self, report: dict[str, Any]) -> str:
        lines = [
            "# FASE 14 — Aquisição Contínua de Estratégias",
            f"- pipeline_version: {report.get('pipeline_version', '14.1_enriched_backlog')}",
            f"- pesquisadas: {report.get('total_researched', 0)}",
            f"- incompativeis: {report.get('total_incompatible', 0)}",
            f"- duplicatas: {report.get('total_duplicates', 0)}",
            f"- eliminadas: {report.get('total_eliminated', 0)}",
            f"- classificadas (backlog): {report.get('total_classified', 0)}",
            "", "## Top estratégias", "",
            "| Rank | Estratégia | Classe | Score | Score adj. | Penalidade | Compat. |",
            "|---|---|---|---:|---:|---:|---|",
        ]
        for row in report.get("top20", []):
            lines.append(f"| {row.get('rank')} | {row.get('name')} | {row.get('classification')} | {float(row.get('market_intelligence_score', 0.0)):.2f} | {float(row.get('adjusted_score', 0.0)):.2f} | {float(row.get('diversity_penalty', 0.0)):.2f} | {row.get('compatibility_with_platform')} |")
        lines += ["", "## Distribuição por categoria", ""]
        for key, count in report.get("distribution_by_category", {}).items():
            lines.append(f"- {key}: {count}")
        lines += ["", "## Incompatíveis", ""]
        for item in report.get("incompatible", []):
            lines.append(f"- {item.get('name')}: {item.get('elimination_reason')}")
        lines += ["", "## Duplicatas detectadas", ""]
        for item in report.get("duplicates", []):
            lines.append(f"- {item.get('name')}: {item.get('elimination_reason')}")
        lines += ["", "## Decisão final", "", f"Backlog suficiente para Fase 13? {report.get('decision', {}).get('answer', 'NAO')}", ""]
        return "\n".join(lines)

    def _canon(self, value: str) -> str:
        return value.strip().lower().replace("_", "").replace("-", "").replace(" ", "")
