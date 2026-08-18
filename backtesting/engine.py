"""
Backtesting engine.

Design decision: A pure-Python event-driven engine rather than delegating to
the `backtesting.py` library gives us full control over risk management
integration (trailing stops, dynamic position sizing) and allows the same
strategies to run in paper trading and live trading without any adaptation.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backtesting.metrics import BacktestMetrics, compute_metrics
from risk.risk_manager import RiskManager
from strategies.base_strategy import BaseStrategy, SignalType
from utils.helpers import utc_now
from utils.logger import get_logger
from utils.validators import validate_positive_float

logger = get_logger(__name__)

_DEFAULT_FEE_PCT = 0.001  # 0.1% Binance taker fee
_EXEC_DIAG_PATH = Path(__file__).parent / "results" / "execution_diagnostic.txt"


@dataclass
class BacktestConfig:
    """Configuration parameters for a single backtest run."""

    initial_capital: float = 10_000.0
    fee_pct: float = _DEFAULT_FEE_PCT
    # Minimo de barras necessario antes de o motor iniciar o trading
    warmup_bars: int = 50
    use_prepared_dataset: bool = True
    progress_log_interval_bars: int = 5_000


@dataclass
class _Position:
    """Tracks an open simulated position during the backtest."""

    entry_price: float
    quantity: float
    stop_loss: float
    take_profit: float
    trailing_stop_pct: float
    max_holding_minutes: int | None
    entry_bar: int
    entry_time: datetime
    highest_price: float = field(init=False)

    def __post_init__(self) -> None:
        self.highest_price = self.entry_price


class BacktestEngine:
    """
    Event-driven backtesting engine.

    Iterates bar by bar over historical OHLCV data, applying a strategy's
    entry/exit signals and simulating trade execution with configurable fees.

    Usage::

        strategy = TrendV1Strategy()
        strategy.initialize()
        engine = BacktestEngine(strategy)
        result = engine.run(df, symbol="BTC/USDT")
        print(result.metrics)
    """

    def __init__(
        self,
        strategy: BaseStrategy,
        config: BacktestConfig | None = None,
        risk_manager: RiskManager | None = None,
    ) -> None:
        self._strategy = strategy
        self._config = config or BacktestConfig()
        self._risk_manager = risk_manager or RiskManager()

    def _write_execution_diagnostic(
        self,
        counters: dict[str, int],
        rejection_reasons: dict[str, int],
        flow_log: list[str],
    ) -> None:
        _EXEC_DIAG_PATH.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []
        lines.append("=" * 40)
        lines.append("EXECUTION DIAGNOSTIC")
        lines.append("=" * 40)
        lines.append(f"BUY Signals Gerados: {counters['buy_signals_generated']}")
        lines.append(f"BUY Recebidos pelo Engine: {counters['buy_received_by_engine']}")
        lines.append(f"BUY Aceitos: {counters['buy_accepted']}")
        lines.append(f"BUY Rejeitados: {counters['buy_rejected']}")
        lines.append(f"Posicoes Criadas: {counters['positions_created']}")
        lines.append(f"Posicoes Fechadas: {counters['positions_closed']}")
        lines.append(f"Trades Registrados: {counters['trades_registered']}")
        lines.append("=" * 40)
        lines.append("REJECTION REASONS")
        lines.append("=" * 40)

        default_reason_keys = [
            "quantidade_zero",
            "saldo_insuficiente",
            "lote_minimo",
            "posicao_existente",
            "risk_manager_rejeitou",
            "erro_interno",
            "validacao_desconhecida",
        ]
        for key in default_reason_keys:
            value = rejection_reasons.get(key, 0)
            lines.append(f"{key}: {value}")

        extras = [k for k in rejection_reasons.keys() if k not in default_reason_keys]
        for key in sorted(extras):
            lines.append(f"{key}: {rejection_reasons[key]}")

        lines.append("=" * 40)
        lines.append("FLOW LOG")
        lines.append("=" * 40)
        lines.extend(flow_log)

        _EXEC_DIAG_PATH.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Execution diagnostic saved: %s", _EXEC_DIAG_PATH)

    def run(self, df: pd.DataFrame, symbol: str = "UNKNOWN", timeframe: str | None = None) -> "BacktestResult":
        """
        Execute a full backtest over *df*.

        Args:
            df: OHLCV DataFrame with DatetimeIndex (must be pre-sorted).
            symbol: Symbol label used in logging.

        Returns:
            BacktestResult containing trades, equity curve, and metrics.
        """
        validate_positive_float(self._config.initial_capital, "initial_capital")

        logger.info(
            "BacktestEngine.run - strategy=%s symbol=%s bars=%d capital=%.2f",
            self._strategy.name,
            symbol,
            len(df),
            self._config.initial_capital,
        )

        prep_started = time.perf_counter()
        prepared_df: pd.DataFrame | None = None
        if self._config.use_prepared_dataset:
            prepared_df = self._strategy.prepare_dataset(df, symbol=symbol, timeframe=timeframe)
        prep_elapsed = time.perf_counter() - prep_started
        logger.info(
            "BacktestEngine preprocessing - strategy=%s prepared=%s elapsed=%.2fs bars=%d",
            self._strategy.name,
            bool(prepared_df is not None),
            prep_elapsed,
            len(df),
        )

        cash = self._config.initial_capital
        position: _Position | None = None
        trades: list[dict[str, Any]] = []
        equity: list[float] = []
        first_trade_elapsed: float | None = None

        counters = {
            "buy_signals_generated": 0,
            "buy_received_by_engine": 0,
            "buy_accepted": 0,
            "buy_rejected": 0,
            "positions_created": 0,
            "positions_closed": 0,
            "trades_registered": 0,
        }
        rejection_reasons: dict[str, int] = {}
        flow_log: list[str] = []

        def _reject(reason: str, detail: str) -> None:
            counters["buy_rejected"] += 1
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
            line = f"BUY rejeitado | motivo={reason} | detalhe={detail}"
            flow_log.append(line)
            logger.warning(line)

        _bt_start = time.perf_counter()
        _bt_total = max(1, len(df) - self._config.warmup_bars)
        for i in range(self._config.warmup_bars, len(df)):
            if prepared_df is not None:
                enriched = prepared_df.iloc[: i + 1]
            else:
                window = df.iloc[: i + 1]
                enriched = self._strategy.calculate(window)
            current_bar = enriched.iloc[-1]
            current_price = float(current_bar["close"])
            timestamp: datetime = current_bar.name.to_pydatetime()  # type: ignore[union-attr]

            equity_recorded = False

            def _record_equity_once() -> None:
                nonlocal equity_recorded
                if equity_recorded:
                    return
                position_value_local = 0.0
                if position:
                    position_value_local = position.quantity * current_price
                equity.append(cash + position_value_local)
                equity_recorded = True

            # --- Gerenciar posicao aberta ---
            if position is not None:
                position.highest_price = max(position.highest_price, current_price)

                # Verifica stops antes dos sinais de saida da estrategia
                stop_hit = current_price <= position.stop_loss
                tp_hit = current_price >= position.take_profit
                trailing_hit = self._risk_manager.check_trailing_stop(
                    position.entry_price,
                    current_price,
                    position.highest_price,
                    position.trailing_stop_pct,
                )
                time_stop_hit = False
                if position.max_holding_minutes is not None and position.max_holding_minutes > 0:
                    elapsed_minutes = max(0.0, (timestamp - position.entry_time).total_seconds() / 60.0)
                    time_stop_hit = elapsed_minutes >= float(position.max_holding_minutes)
                exit_signal = self._strategy.exit_signal(enriched, position.entry_price)

                close_reason: str | None = None
                close_price = current_price
                if stop_hit:
                    close_reason = "stop_loss"
                    close_price = position.stop_loss
                elif tp_hit:
                    close_reason = "take_profit"
                    close_price = position.take_profit
                elif trailing_hit:
                    close_reason = "trailing_stop"
                elif time_stop_hit:
                    close_reason = "time_stop"
                elif exit_signal.signal == SignalType.SELL:
                    close_reason = exit_signal.metadata.get("exit_reason", "strategy_exit")

                if close_reason:
                    fee = position.quantity * close_price * self._config.fee_pct
                    proceeds = position.quantity * close_price - fee
                    pnl = proceeds - position.quantity * position.entry_price
                    cash += proceeds

                    trade_record = {
                        "entry_price": position.entry_price,
                        "entry_time": position.entry_time,
                        "exit_price": close_price,
                        "quantity": position.quantity,
                        "pnl": pnl,
                        "pnl_pct": pnl / (position.quantity * position.entry_price),
                        "exit_reason": close_reason,
                        "entry_bar": position.entry_bar,
                        "exit_bar": i,
                        "exit_time": timestamp,
                    }
                    trades.append(trade_record)
                    counters["positions_closed"] += 1
                    counters["trades_registered"] += 1
                    if first_trade_elapsed is None:
                        first_trade_elapsed = time.perf_counter() - _bt_start
                        logger.info(
                            "BacktestEngine first trade - strategy=%s bar=%d elapsed=%.2fs",
                            self._strategy.name,
                            i + 1,
                            first_trade_elapsed,
                        )
                    flow_log.append(
                        f"Trade fechado | reason={close_reason} | pnl={pnl:.2f} | cash={cash:.2f}"
                    )

                    logger.debug(
                        "Trade closed - reason=%s pnl=%.2f cash=%.2f",
                        close_reason,
                        pnl,
                        cash,
                    )
                    position = None

            # --- Buscar entrada ---
            if position is None:
                entry_signal = self._strategy.entry_signal(enriched)
                if entry_signal.signal == SignalType.BUY:
                    counters["buy_signals_generated"] += 1
                    counters["buy_received_by_engine"] += 1

                    flow_log.append(
                        "BUY recebido pelo engine | "
                        f"price={current_price:.6f} score={entry_signal.score:.4f} "
                        f"stop={entry_signal.stop_loss} take={entry_signal.take_profit} "
                        f"atr={current_bar.get('atr', 'n/a')} cash={cash:.2f}"
                    )

                    if position is not None:
                        _reject("posicao_existente", "Ja existe posicao aberta")
                        _record_equity_once()
                        continue

                    try:
                        logger.info("BacktestEngine chamando RiskManager para validar BUY")
                        strategy_rr_min = RiskManager.resolve_min_risk_reward_ratio(self._strategy)
                        if strategy_rr_min is None:
                            strategy_rr_min = RiskManager.infer_min_risk_reward_ratio_from_levels(
                                entry_price=current_price,
                                stop_loss=entry_signal.stop_loss,
                                take_profit=entry_signal.take_profit,
                            )
                        risk_params = self._risk_manager.evaluate_trade(
                            portfolio_value=cash,
                            entry_price=current_price,
                            stop_loss=entry_signal.stop_loss,
                            take_profit=entry_signal.take_profit,
                            trailing_stop_pct=entry_signal.trailing_stop_pct,
                            strategy_score=entry_signal.score,
                            min_risk_reward_ratio=strategy_rr_min,
                        )
                    except ValueError as exc:
                        _reject("risk_manager_rejeitou", str(exc))
                        _record_equity_once()
                        continue
                    except Exception as exc:  # pragma: no cover - defensive guard
                        _reject("erro_interno", str(exc))
                        _record_equity_once()
                        continue
                    else:
                        lot_min_qty = 1e-8
                        precision = 8
                        qty_suggested = risk_params.quantity_suggested
                        qty_after_cap = risk_params.quantity_after_cap
                        qty_final = round(qty_after_cap, precision)
                        trade_value = qty_final * current_price
                        min_value = 0.0
                        max_stake = risk_params.max_stake

                        flow_log.append(
                            "BUY sizing details | "
                            f"capital={cash:.2f} risk_allowed={risk_params.risk_amount:.2f} "
                            f"qty_suggested={qty_suggested:.8f} qty_after_cap={qty_after_cap:.8f} "
                            f"qty_final={qty_final:.8f} trade_value={trade_value:.2f} "
                            f"min_value={min_value:.2f} lot_min={lot_min_qty:.8f} precision={precision} "
                            f"max_stake={max_stake:.2f}"
                        )

                        if qty_final <= 0:
                            _reject("quantidade_zero", "Quantidade final igual a zero")
                            _record_equity_once()
                            continue

                        if qty_final < lot_min_qty:
                            _reject(
                                "lote_minimo",
                                f"Quantidade final {qty_final:.8f} abaixo do lote minimo {lot_min_qty:.8f}",
                            )
                            _record_equity_once()
                            continue

                        fee = risk_params.stake_amount * self._config.fee_pct
                        total_cost = risk_params.stake_amount + fee
                        if total_cost > cash:
                            _reject(
                                "saldo_insuficiente",
                                f"Custo total {total_cost:.2f} > saldo disponivel {cash:.2f}",
                            )
                            _record_equity_once()
                            continue

                        fee = risk_params.stake_amount * self._config.fee_pct
                        cash -= risk_params.stake_amount + fee

                        position = _Position(
                            entry_price=current_price,
                            quantity=qty_final,
                            stop_loss=risk_params.stop_loss,
                            take_profit=risk_params.take_profit,
                            trailing_stop_pct=risk_params.trailing_stop_pct or 0.015,
                            max_holding_minutes=(
                                int(entry_signal.metadata.get("max_holding_minutes"))
                                if isinstance(entry_signal.metadata, dict)
                                and entry_signal.metadata.get("max_holding_minutes") is not None
                                else None
                            ),
                            entry_bar=i,
                            entry_time=timestamp,
                        )
                        counters["buy_accepted"] += 1
                        counters["positions_created"] += 1
                        flow_log.append(
                            f"Trade aberto | price={current_price:.6f} qty={qty_final:.8f} "
                            f"sl={risk_params.stop_loss:.6f} tp={risk_params.take_profit:.6f} cash={cash:.2f}"
                        )
                        logger.debug(
                            "Trade opened - price=%.4f qty=%.6f sl=%.4f tp=%.4f",
                            current_price,
                            qty_final,
                            risk_params.stop_loss,
                            risk_params.take_profit,
                        )

            # --- Record equity ---
            _record_equity_once()

            # --- Progress logging every 5 000 bars ---
            _bt_done = i - self._config.warmup_bars + 1
            interval = max(1, int(self._config.progress_log_interval_bars))
            if _bt_done % interval == 0:
                _bt_elapsed = time.perf_counter() - _bt_start
                _bt_rate = _bt_done / _bt_elapsed if _bt_elapsed > 0 else 0.0
                _bt_eta = (_bt_total - _bt_done) / _bt_rate if _bt_rate > 0 else 0.0
                logger.info(
                    "BacktestEngine — bar %d/%d (%.1f%%) | %.0f bars/s | ETA %.0fs | trades=%d | prepared=%s | prep_elapsed=%.2fs",
                    i + 1, len(df), _bt_done / _bt_total * 100,
                    _bt_rate, _bt_eta, len(trades), bool(prepared_df is not None), prep_elapsed,
                )

        equity_series = pd.Series(
            equity,
            index=df.index[self._config.warmup_bars :],
            name="equity",
        )

        metrics = compute_metrics(trades, equity_series, self._config.initial_capital)

        self._write_execution_diagnostic(counters, rejection_reasons, flow_log)

        logger.info(
            "Backtest complete - trades=%d net_profit=%.2f return=%.2f%%",
            metrics.total_trades,
            metrics.net_profit,
            metrics.return_pct * 100,
        )

        total_elapsed = time.perf_counter() - _bt_start
        processed_bars = max(0, len(df) - self._config.warmup_bars)
        bars_per_second = processed_bars / total_elapsed if total_elapsed > 0 else 0.0

        return BacktestResult(
            strategy_name=self._strategy.name,
            symbol=symbol,
            trades=trades,
            equity_curve=equity_series,
            metrics=metrics,
            config=self._config,
            diagnostics={
                "used_prepared_dataset": bool(prepared_df is not None),
                "preprocessing_elapsed_seconds": prep_elapsed,
                "execution_elapsed_seconds": total_elapsed,
                "bars_processed": processed_bars,
                "bars_per_second": bars_per_second,
                "first_trade_elapsed_seconds": first_trade_elapsed,
            },
        )


@dataclass
class BacktestResult:
    """Container for all backtesting outputs."""

    strategy_name: str
    symbol: str
    trades: list[dict[str, Any]]
    equity_curve: pd.Series
    metrics: BacktestMetrics
    config: BacktestConfig
    diagnostics: dict[str, Any] = field(default_factory=dict)
