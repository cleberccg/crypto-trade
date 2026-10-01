"""Módulo de validação estatística de resultados de otimização com validação walk-forward."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter
from typing import Callable

import pandas as pd
from sqlalchemy import func

from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.models import Candle
from database.repositories import CandleRepository
from optimizer.optimization_result import OptimizationResult
from strategies.base_strategy import BaseStrategy
from strategies.factory import create_strategy
from utils.validators import validate_symbol, validate_timeframe
from validation.validation_report import ValidationReport
from validation.validation_result import ValidationEntry
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class ValidationCriteria:
    """Limiares mínimos exigidos para os períodos de treinamento e validação."""

    min_trades: int
    min_profit_factor: float
    max_drawdown_pct: float
    min_win_rate_pct: float
    min_expectancy: float
    min_sharpe: float


@dataclass(frozen=True)
class ValidationSummary:
    """Resumo da execução de validação com artefatos e classificação."""

    total_candidates: int
    discarded: int
    passed: int
    best_validated: ValidationEntry | None
    validated_top: list[ValidationEntry]
    output_files: list[str]


@dataclass(frozen=True)
class WalkForwardWindow:
    """Janelas de datas de treinamento/validação para verificações walk-forward."""

    train_start: datetime
    train_end: datetime
    validation_start: datetime
    validation_end: datetime


class OptimizationValidator:
    """Executa a validação estatística pós-otimização e exporta relatórios."""

    def __init__(
        self,
        criteria: ValidationCriteria,
        output_dir: Path | None = None,
        strategy_name: str = "TrendV1",
        strategy_factory: Callable[[dict[str, float | int], str, str], BaseStrategy] | None = None,
    ) -> None:
        self._criteria = criteria
        self._reporter = ValidationReport(output_dir=output_dir)
        self._strategy_name = strategy_name
        self._strategy_factory = strategy_factory

    @staticmethod
    def _get_available_date_range(symbol: str, timeframe: str) -> tuple[datetime | None, datetime | None]:
        """Consulta no banco as datas mínima/máxima reais dos candles disponíveis para um ativo/período.
        
        Returns:
            Tupla (min_date, max_date) ou (None, None) se nenhum candle for encontrado.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)
        with get_session() as session:
            result = session.query(
                func.min(Candle.open_time),
                func.max(Candle.open_time),
            ).filter(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
            ).one_or_none()
            
            if result and result[0]:
                return result[0], result[1]
            return None, None

    def validate_data_availability(
        self,
        symbol: str,
        timeframe: str,
        train_start: datetime,
        train_end: datetime,
        validation_start: datetime,
        validation_end: datetime,
    ) -> None:
        """Pré-validação: verifica se os dados históricos necessários existem antes de iniciar os backtests.
        
        Raises:
            ValueError: com informações detalhadas sobre os dados ausentes.
        """
        min_available, max_available = self._get_available_date_range(symbol, timeframe)
        
        if min_available is None or max_available is None:
            raise ValueError(
                f"NO HISTORICAL DATA FOUND for {symbol}/{timeframe} in database.\n"
                f"Symbol and timeframe combination has no candles available."
            )

        def _as_utc(value: datetime) -> datetime:
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
            return value.astimezone(timezone.utc)
        
        periods = [
            ("train", train_start, train_end),
            ("validation", validation_start, validation_end),
        ]
        
        for period_name, period_start, period_end in periods:
            min_available_utc = _as_utc(min_available)
            max_available_utc = _as_utc(max_available)
            period_start_utc = _as_utc(period_start)
            period_end_utc = _as_utc(period_end)
            
            if period_end_utc < min_available_utc or period_start_utc > max_available_utc:
                raise ValueError(
                    f"Data range gap for {period_name} period:\n"
                    f"  Requested: {period_name} from {period_start_utc.date()} to {period_end_utc.date()}\n"
                    f"  Available: {symbol}/{timeframe} has data from {min_available_utc.date()} to {max_available_utc.date()}\n"
                    f"  Fix: Use dates within available range or download missing data."
                )

    def _load_dataframe(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        with get_session() as session:
            repo = CandleRepository(session)
            candles = repo.get_range(symbol, timeframe, start, end)

        if not candles:
            min_avail, max_avail = self._get_available_date_range(symbol, timeframe)
            if min_avail is None:
                raise ValueError(
                    f"NO HISTORICAL DATA: {symbol}/{timeframe} not in database.\n"
                    f"Requested period: {start.date()} to {end.date()}\n"
                    f"Action: Download data with 'python main.py download --symbol {symbol} --timeframe {timeframe} --start YYYY-MM-DD'"
                )
            else:
                raise ValueError(
                    f"NO DATA IN RANGE: {symbol}/{timeframe} has no candles between {start.date()} and {end.date()}.\n"
                    f"Available data: {min_avail.date()} to {max_avail.date()}\n"
                    f"Adjust date range or download missing data."
                )

        return pd.DataFrame(
            [
                {
                    "open": candle.open,
                    "high": candle.high,
                    "low": candle.low,
                    "close": candle.close,
                    "volume": candle.volume,
                }
                for candle in candles
            ],
            index=pd.DatetimeIndex([candle.open_time for candle in candles], tz="UTC"),
        )

    def _build_strategy(self, parameters: dict[str, float | int], symbol: str, timeframe: str) -> BaseStrategy:
        if self._strategy_factory is not None:
            return self._strategy_factory(parameters, symbol, timeframe)

        strategy_params = dict(parameters)
        strategy_params.setdefault("rsi_period", 14)
        strategy_params.setdefault("atr_period", 14)

        strategy = create_strategy(self._strategy_name, **strategy_params)
        strategy.initialize()
        return strategy

    def _run_backtest_metrics(
        self,
        parameters: dict[str, float | int],
        symbol: str,
        timeframe: str,
        capital: float,
        df: pd.DataFrame,
    ) -> dict[str, float | int]:
        strategy = self._build_strategy(parameters, symbol, timeframe)
        strategy.prepare_dataset(df.copy(), symbol=symbol, timeframe=None)
        engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=capital))
        result = engine.run(df, symbol=symbol, timeframe=timeframe)
        return result.metrics.to_dict()

    def _evaluate_period(
        self,
        metrics: dict[str, float | int],
        period_name: str,
    ) -> list[str]:
        reasons: list[str] = []
        if int(metrics.get("total_trades", 0)) < self._criteria.min_trades:
            reasons.append(f"{period_name}: total_trades<{self._criteria.min_trades}")
        if float(metrics.get("profit_factor", 0.0)) < self._criteria.min_profit_factor:
            reasons.append(f"{period_name}: profit_factor<{self._criteria.min_profit_factor}")

        drawdown_pct = abs(float(metrics.get("max_drawdown_pct", 0.0))) * 100.0
        if drawdown_pct > self._criteria.max_drawdown_pct:
            reasons.append(f"{period_name}: drawdown>{self._criteria.max_drawdown_pct}%")

        win_rate_pct = float(metrics.get("win_rate", 0.0)) * 100.0
        if win_rate_pct < self._criteria.min_win_rate_pct:
            reasons.append(f"{period_name}: win_rate<{self._criteria.min_win_rate_pct}%")

        if float(metrics.get("expectancy", 0.0)) < self._criteria.min_expectancy:
            reasons.append(f"{period_name}: expectancy<{self._criteria.min_expectancy}")

        if float(metrics.get("sharpe_ratio", 0.0)) < self._criteria.min_sharpe:
            reasons.append(f"{period_name}: sharpe<{self._criteria.min_sharpe}")

        return reasons

    def _is_overfitting(self, train: dict[str, float | int], validation: dict[str, float | int]) -> bool:
        train_pf = float(train.get("profit_factor", 0.0))
        val_pf = float(validation.get("profit_factor", 0.0))
        train_sharpe = float(train.get("sharpe_ratio", 0.0))
        val_sharpe = float(validation.get("sharpe_ratio", 0.0))

        if train_pf > 0 and val_pf / train_pf < 0.6:
            return True
        if train_sharpe > 0 and val_sharpe / train_sharpe < 0.6:
            return True
        return False

    def _sort_validated(self, entries: list[ValidationEntry]) -> list[ValidationEntry]:
        ranked = sorted(
            entries,
            key=lambda entry: (
                -float(entry.validation_metrics.get("profit_factor", 0.0)),
                -float(entry.validation_metrics.get("net_profit", 0.0)),
                abs(float(entry.validation_metrics.get("max_drawdown_pct", 0.0))),
                -float(entry.validation_metrics.get("sharpe_ratio", 0.0)),
            ),
        )

        final: list[ValidationEntry] = []
        for index, item in enumerate(ranked, start=1):
            final.append(
                ValidationEntry(
                    rank=index,
                    parameters=item.parameters,
                    train_metrics=item.train_metrics,
                    validation_metrics=item.validation_metrics,
                    passed=item.passed,
                    discard_reasons=item.discard_reasons,
                    overfitting_risk=item.overfitting_risk,
                )
            )
        return final

    def _render_text(
        self,
        summary: ValidationSummary,
        all_entries: list[ValidationEntry],
        window: WalkForwardWindow,
    ) -> str:
        lines = [
            "======================================",
            "VALIDATION REPORT",
            "======================================",
            f"Treino: {window.train_start.date()} ate {window.train_end.date()}",
            f"Validacao: {window.validation_start.date()} ate {window.validation_end.date()}",
            f"Total de configuracoes: {summary.total_candidates}",
            f"Descartadas: {summary.discarded}",
            f"Aprovadas: {summary.passed}",
            "",
            "Melhor configuracao validada:",
        ]

        if summary.best_validated is None:
            lines.append("Nenhuma configuracao aprovada.")
        else:
            best = summary.best_validated
            lines.append(str(best.parameters))
            lines.append(
                "Train PF={:.2f} Win={:.2%} DD={:.2%} | Validation PF={:.2f} Win={:.2%} DD={:.2%}".format(
                    float(best.train_metrics.get("profit_factor", 0.0)),
                    float(best.train_metrics.get("win_rate", 0.0)),
                    abs(float(best.train_metrics.get("max_drawdown_pct", 0.0))),
                    float(best.validation_metrics.get("profit_factor", 0.0)),
                    float(best.validation_metrics.get("win_rate", 0.0)),
                    abs(float(best.validation_metrics.get("max_drawdown_pct", 0.0))),
                )
            )
            lines.append(f"Overfitting: {'SIM' if best.overfitting_risk else 'NAO'}")

        lines.extend(["", "TOP 10 VALIDATED"]) 
        for entry in summary.validated_top[:10]:
            lines.append(
                "Rank {} | PFv {:.2f} | Winv {:.2%} | DDv {:.2%} | Overfit {} | params {}".format(
                    entry.rank,
                    float(entry.validation_metrics.get("profit_factor", 0.0)),
                    float(entry.validation_metrics.get("win_rate", 0.0)),
                    abs(float(entry.validation_metrics.get("max_drawdown_pct", 0.0))),
                    "SIM" if entry.overfitting_risk else "NAO",
                    entry.parameters,
                )
            )

        lines.extend(["", "MOTIVOS DE DESCARTE"])
        discarded_entries = [item for item in all_entries if not item.passed]
        for item in discarded_entries[:20]:
            lines.append(f"- Rank {item.rank}: {', '.join(item.discard_reasons)}")

        lines.append("======================================")
        return "\n".join(lines)

    def validate(
        self,
        optimization_results: list[OptimizationResult],
        symbol: str,
        timeframe: str,
        capital: float,
        train_start: datetime,
        train_end: datetime,
        validation_start: datetime,
        validation_end: datetime,
        top_n: int = 50,
    ) -> ValidationSummary:
        window = WalkForwardWindow(
            train_start=train_start,
            train_end=train_end,
            validation_start=validation_start,
            validation_end=validation_end,
        )

        # Pré-valida a existência dos dados antes de executar backtests custosos
        self.validate_data_availability(
            symbol, timeframe, train_start, train_end, validation_start, validation_end
        )

        train_df = self._load_dataframe(symbol, timeframe, train_start, train_end)
        validation_df = self._load_dataframe(symbol, timeframe, validation_start, validation_end)

        _val_approved = 0
        reason_counter: Counter[str] = Counter()
        entries: list[ValidationEntry] = []
        for index, result in enumerate(optimization_results, start=1):
            logger.info(
                "Validation — candidate %d/%d | evaluating...",
                index, len(optimization_results),
            )
            train_metrics = self._run_backtest_metrics(result.parameters, symbol, timeframe, capital, train_df)
            validation_metrics = self._run_backtest_metrics(result.parameters, symbol, timeframe, capital, validation_df)

            reasons = []
            reasons.extend(self._evaluate_period(train_metrics, "train"))
            reasons.extend(self._evaluate_period(validation_metrics, "validation"))
            overfitting = self._is_overfitting(train_metrics, validation_metrics)
            if overfitting:
                reasons.append("possible_overfitting")
            reason_counter.update(reasons)

            _passed = len(reasons) == 0
            if _passed:
                _val_approved += 1
            entries.append(
                ValidationEntry(
                    rank=index,
                    parameters=result.parameters,
                    train_metrics=train_metrics,
                    validation_metrics=validation_metrics,
                    passed=_passed,
                    discard_reasons=reasons,
                    overfitting_risk=overfitting,
                )
            )
            logger.info(
                "Validation — %d/%d | approved=%d | rejected=%d | %s",
                index, len(optimization_results), _val_approved, index - _val_approved,
                "APROVADO" if _passed else f"REPROVADO: {', '.join(reasons[:2])}",
            )
        if reason_counter:
            top_reasons = reason_counter.most_common(5)
            logger.info("Validation — criterios de reprovacao mais frequentes: %s", top_reasons)

        approved = [entry for entry in entries if entry.passed]
        ranked_approved = self._sort_validated(approved)
        top_validated = ranked_approved[:top_n]

        summary = ValidationSummary(
            total_candidates=len(entries),
            discarded=len(entries) - len(approved),
            passed=len(approved),
            best_validated=ranked_approved[0] if ranked_approved else None,
            validated_top=top_validated,
            output_files=[],
        )

        csv_path = self._reporter.save_csv(entries)
        json_path = self._reporter.save_json(entries)
        txt_path = self._reporter.save_text(self._render_text(summary, entries, window))

        return ValidationSummary(
            total_candidates=summary.total_candidates,
            discarded=summary.discarded,
            passed=summary.passed,
            best_validated=summary.best_validated,
            validated_top=summary.validated_top,
            output_files=[str(csv_path), str(json_path), str(txt_path)],
        )


def default_validation_window(
    start: datetime | None,
    end: datetime | None,
    symbol: str = "BTC/USDT",
    timeframe: str = "5m",
) -> WalkForwardWindow:
    """Cria uma janela walk-forward que respeita os dados realmente disponíveis.
    
    Se start/end forem informados explicitamente, usa esses valores para definir
    a janela. Caso contrário, detecta os dados disponíveis e os divide em 70/30
    para treinamento/validação.
    
    Args:
        Args:
        start: Data inicial da otimização (não usada quando os dados são detectados)
        end: Data final da otimização (não usada quando os dados são detectados)
        symbol: Par de negociação usado para verificar a disponibilidade dos dados
        timeframe: Intervalo dos candles usado para verificar a disponibilidade dos dados
    
    Returns:
        WalkForwardWindow com os intervalos de datas de treinamento/validação
    """
    # Se forem fornecidas datas explícitas, usa-as
    if start and end:
        mid_point = start + (end - start) * 0.7
        return WalkForwardWindow(
            train_start=start,
            train_end=mid_point,
            validation_start=mid_point,
            validation_end=end,
        )
    
    # Caso contrário, detecta a disponibilidade real dos dados
    min_date, max_date = OptimizationValidator._get_available_date_range(symbol, timeframe)
    
    if min_date is None or max_date is None:
        raise ValueError(
            f"No historical candles available for {symbol}/{timeframe}. "
            "Run download before validation."
        )
    
    # Divide os dados disponíveis em 70% para treino e 30% para validação
    total_days = (max_date - min_date).days
    if total_days < 2:
        # Dados insuficientes
        return WalkForwardWindow(
            train_start=min_date,
            train_end=max_date,
            validation_start=max_date,
            validation_end=max_date,
        )
    
    split_point = min_date + (max_date - min_date) * 0.7
    return WalkForwardWindow(
        train_start=min_date,
        train_end=split_point,
        validation_start=split_point,
        validation_end=max_date,
    )
