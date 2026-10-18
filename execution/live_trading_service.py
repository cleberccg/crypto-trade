"""Serviço oficial de negociação ao vivo na Binance Spot com dimensionamento gerenciado por risco."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic, sleep
from typing import Any, Callable

import ccxt
import pandas as pd

from config.settings import settings
from database.connection import get_session
from exchange.binance_client import BinanceClient
from exchange.base_exchange import BaseExchange
from execution.hypothesis_runtime import hypothesis_gate_config_from_payload, wrap_strategy_with_hypothesis
from execution.live_risk_service import LiveRiskService
from execution.order_executor import OrderExecutor
from monitoring.asset_ranking import AssetRankingMonitor
from risk.portfolio_value_provider import BinancePortfolioValueProvider
from risk.position_sizer import PositionSizer
from risk.risk_manager import RiskManager
from strategies.base_strategy import SignalType, StrategySignal
from strategies.factory import create_strategy
from utils.atomic_io import atomic_write_text
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Estado da posição
# ---------------------------------------------------------------------------

@dataclass
class LivePositionState:
    """Acompanha uma posição ao vivo aberta para recuperação após reinicialização."""

    trade_id: int
    symbol: str
    timeframe: str
    strategy: str
    quantity: float
    stake_amount: float
    entry_price: float
    stop_loss: float
    take_profit: float
    opened_at: str           # ISO-8601 UTC
    exchange_order_id: str
    original_quantity: float = 0.0
    entry_fee_usdt: float | None = None
    entry_fee_allocated_usdt: float = 0.0
    realized_gross_pnl: float = 0.0
    realized_exit_fees_usdt: float = 0.0
    realized_net_pnl: float | None = None
    fee_accounting_complete: bool = False
    status: str = "OPEN"  # DUST preserva propriedade; nao representa venda/fechamento.
    dust_reason: str = ""
    last_evaluated_candle: str = ""
    exit_pending: bool = False

    @property
    def context_key(self) -> tuple[str, str, str]:
        return (self.symbol, self.strategy, self.timeframe)


class _LivePositionStore:
    """Persiste posições abertas em um arquivo JSON para recuperação após falha/reinicialização."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, state: LivePositionState) -> None:
        # Salvamento de estado único compatível com versões anteriores.
        self.save_all([state])

    def save_all(self, states: list[LivePositionState]) -> None:
        try:
            payload = json.dumps([asdict(state) for state in states], ensure_ascii=False)
            atomic_write_text(self._path, payload, encoding="utf-8")
            atomic_write_text(self._path.with_suffix(self._path.suffix + ".bak"), payload, encoding="utf-8")
            logger.debug("LivePositionStore.save_all -> %s (count=%d)", self._path, len(states))
        except Exception as exc:
            raise RuntimeError("LIVE state persistence failed; operation blocked") from exc

    def load(self) -> LivePositionState | None:
        # Carregamento de estado único compatível com versões anteriores.
        states = self.load_all()
        return states[0] if states else None

    def load_all(self) -> list[LivePositionState]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            backup = self._path.with_suffix(self._path.suffix + ".bak")
            if not backup.exists():
                raise RuntimeError("Corrupted LIVE state without backup")
            try:
                data = json.loads(backup.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError("Corrupted LIVE state and backup") from exc
        try:
            if isinstance(data, dict):
                # Formato legado de posição única.
                state = LivePositionState(
                    trade_id=int(data["trade_id"]),
                    symbol=str(data["symbol"]),
                    timeframe=str(data.get("timeframe", "15m")),
                    strategy=str(data.get("strategy", "ClassicDonchianBreakout")),
                    quantity=float(data["quantity"]),
                    stake_amount=float(data.get("stake_amount", float(data["quantity"]) * float(data["entry_price"]))),
                    entry_price=float(data["entry_price"]),
                    stop_loss=float(data["stop_loss"]),
                    take_profit=float(data["take_profit"]),
                    opened_at=str(data.get("opened_at") or data.get("entry_ts") or ""),
                    exchange_order_id=str(data.get("exchange_order_id", "")),
                    original_quantity=float(data.get("original_quantity", data.get("quantity", 0.0)) or 0.0),
                    entry_fee_usdt=(float(data["entry_fee_usdt"]) if data.get("entry_fee_usdt") is not None else None),
                    entry_fee_allocated_usdt=float(data.get("entry_fee_allocated_usdt", 0.0) or 0.0),
                    realized_gross_pnl=float(data.get("realized_gross_pnl", 0.0) or 0.0),
                    realized_exit_fees_usdt=float(data.get("realized_exit_fees_usdt", 0.0) or 0.0),
                    realized_net_pnl=(float(data["realized_net_pnl"]) if data.get("realized_net_pnl") is not None else None),
                    fee_accounting_complete=bool(data.get("fee_accounting_complete", False)),
                    status=str(data.get("status", "OPEN")),
                    dust_reason=str(data.get("dust_reason", "")),
                    last_evaluated_candle=str(data.get("last_evaluated_candle", "")),
                    exit_pending=bool(data.get("exit_pending", False)),
                )
                logger.info("LivePositionStore.load_all -> loaded legacy single state")
                return [state]

            states: list[LivePositionState] = []
            if not isinstance(data, list):
                raise ValueError("Invalid LIVE state payload")
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        raise ValueError("Invalid LIVE position")
                    states.append(
                        LivePositionState(
                            trade_id=int(item["trade_id"]),
                            symbol=str(item["symbol"]),
                            timeframe=str(item.get("timeframe", "15m")),
                            strategy=str(item.get("strategy", "ClassicDonchianBreakout")),
                            quantity=float(item.get("quantity", item.get("qty", 0.0))),
                            stake_amount=float(item.get("stake_amount", float(item.get("quantity", item.get("qty", 0.0))) * float(item.get("entry_price", item.get("entry", 0.0))))),
                            entry_price=float(item.get("entry_price", item.get("entry", 0.0))),
                            stop_loss=float(item.get("stop_loss", 0.0)),
                            take_profit=float(item.get("take_profit", 0.0)),
                            opened_at=str(item.get("opened_at", "")),
                            exchange_order_id=str(item.get("exchange_order_id", "")),
                            original_quantity=float(item.get("original_quantity", item.get("quantity", item.get("qty", 0.0))) or 0.0),
                            entry_fee_usdt=(float(item["entry_fee_usdt"]) if item.get("entry_fee_usdt") is not None else None),
                            entry_fee_allocated_usdt=float(item.get("entry_fee_allocated_usdt", 0.0) or 0.0),
                            realized_gross_pnl=float(item.get("realized_gross_pnl", 0.0) or 0.0),
                            realized_exit_fees_usdt=float(item.get("realized_exit_fees_usdt", 0.0) or 0.0),
                            realized_net_pnl=(float(item["realized_net_pnl"]) if item.get("realized_net_pnl") is not None else None),
                            fee_accounting_complete=bool(item.get("fee_accounting_complete", False)),
                            status=str(item.get("status", "OPEN")),
                            dust_reason=str(item.get("dust_reason", "")),
                            last_evaluated_candle=str(item.get("last_evaluated_candle", "")),
                            exit_pending=bool(item.get("exit_pending", False)),
                        )
                    )
            logger.info("LivePositionStore.load_all -> loaded %d position(s)", len(states))
            return states
        except Exception as exc:
            raise RuntimeError("Invalid LIVE position state; reconciliation required") from exc

    def clear(self) -> None:
        try:
            if self._path.exists():
                self._path.unlink()
                logger.debug("LivePositionStore.clear -> %s", self._path)
        except Exception as exc:
            logger.warning(
                "LivePositionStore: failed to clear state file %s: %s", self._path, exc
            )


class _TradeStub:
    """Objeto mínimo semelhante a Trade que contém somente o ID persistido."""

    __slots__ = ("id",)

    def __init__(self, trade_id: int) -> None:
        self.id = trade_id


# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LiveTradingConfig:
    symbol: str
    timeframe: str
    strategy_name: str
    strategy_version: str
    symbols: tuple[str, ...] = ()
    poll_seconds: float = 15.0
    bootstrap_bars: int = 1500
    bootstrap_replay_bars: int = 350
    max_cycles: int = 0
    resume: bool = True
    output_prefix: str = "live"
    state_dir: Path | None = None
    max_frame_bars: int = 3000
    hypothesis_config: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Serviço
# ---------------------------------------------------------------------------

class LiveTradingService:
    """Monitora o mercado ao vivo com ciclo completo de entrada/saída e recuperação após reinicialização."""

    def __init__(
        self,
        base_dir: Path,
        exchange_factory: Callable[[], BaseExchange] | None = None,
        strategy_factory: Callable[..., Any] | None = None,
        sizer_factory: Callable[[], PositionSizer] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
        position_store_factory: Callable[[Path], Any] | None = None,
        live_risk_service_factory: Callable | None = None,
        order_executor_factory: Callable | None = None,
        asset_ranking_monitor_factory: Callable[..., Any] | None = None,
        db_ops: dict[str, Callable] | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._exchange_factory = exchange_factory or BinanceClient
        self._strategy_factory = strategy_factory or create_strategy
        self._sizer_factory = sizer_factory or PositionSizer
        self._sleep_fn = sleep_fn or sleep
        self._position_store_factory = position_store_factory or _LivePositionStore
        self._live_risk_service_factory = live_risk_service_factory
        self._order_executor_factory = order_executor_factory
        self._asset_ranking_monitor_factory = asset_ranking_monitor_factory or AssetRankingMonitor
        self._db_ops: dict[str, Callable] = db_ops or {}
        self._dust_positions: dict[int, LivePositionState] = {}
        self._preserved_positions: dict[int, LivePositionState] = {}
        self._saved_positions: dict[int, LivePositionState] = {}
        self._active_positions: dict[tuple[str, str, str], LivePositionState] = {}
        self._runtime_store: Any = None
        self._sell_blocks: dict[int, str] = {}
        self._last_exit_filled = False
        self._last_terminal_position_summary: tuple[str, ...] | None = None
        self._asset_ranking_has_history: dict[str, bool] = {}
        self._last_terminal_summary_at: float | None = None

    # ------------------------------------------------------------------
    # Ponto de entrada público
    # ------------------------------------------------------------------

    def run(self, cfg: LiveTradingConfig) -> dict[str, Any]:
        exchange = self._exchange_factory()
        try:
            startup = self._initialize_runtime_with_retry(cfg=cfg, exchange=exchange)
            base_frame = startup["frame"]
            live_risk_service = startup["live_risk_service"]
            risk_manager = startup["risk_manager"]
            order_executor = startup["order_executor"]
            last_known_free_usdt = float(startup["free_usdt"])
            cycles = 0

            target_symbols: list[str] = []
            if cfg.symbols:
                for symbol in cfg.symbols:
                    clean = str(symbol).strip()
                    if clean and clean not in target_symbols:
                        target_symbols.append(clean)
            if cfg.symbol and cfg.symbol not in target_symbols:
                target_symbols.insert(0, cfg.symbol)
            if not target_symbols:
                target_symbols = [cfg.symbol]

            target_contexts: set[tuple[str, str, str]] = {
                (symbol, cfg.strategy_name, cfg.timeframe)
                for symbol in target_symbols
            }
            primary_ctx = (target_symbols[0], cfg.strategy_name, cfg.timeframe)

            # Cria o armazenamento de estado para múltiplas posições
            state_dir = cfg.state_dir or (self._base_dir / "optimization" / "results")
            store = self._position_store_factory(
                Path(state_dir) / "live_positions.json"
            )
            for symbol in target_symbols:
                self._migrate_legacy_state_file(
                    symbol=symbol,
                    strategy_name=cfg.strategy_name,
                    timeframe=cfg.timeframe,
                    state_dir=Path(state_dir),
                    store=store,
                )

            # Reconcilia quaisquer posições abertas existentes ao reiniciar
            open_positions = self._reconcile_on_startup(
                cfg=cfg,
                exchange=exchange,
                store=store,
                target_contexts=target_contexts,
            )
            self._store_save_positions(store=store, positions=open_positions)

            context_frames: dict[tuple[str, str, str], pd.DataFrame] = {
                primary_ctx: base_frame
            }
            strategy_cache: dict[tuple[str, str, str], Any] = {
                primary_ctx: startup["strategy"]
            }
            asset_monitor = self._asset_ranking_monitor_factory(
                min_trades_per_symbol=int(settings.trading.min_trades_per_symbol)
            )
            for ctx in target_contexts:
                if ctx == primary_ctx:
                    continue
                strategy_obj = self._build_runtime_strategy(
                    cfg,
                    symbol=ctx[0],
                    timeframe=ctx[2],
                )
                strategy_cache[ctx] = strategy_obj
            last_signals: dict[tuple[str, str, str], str] = {}
            recovery_pending = set(open_positions)
            logged_actions: dict[tuple[str, str, str], str] = {}

            while True:
                cycles += 1
                contexts_to_poll = set(open_positions.keys()) | set(target_contexts)
                cycle_logs: list[tuple[tuple[str, str, str], str]] = []

                for context_key in contexts_to_poll:
                    symbol, strategy_name, timeframe = context_key
                    if not str(timeframe or "").strip():
                        logger.warning(
                            "Contexto LIVE invalido detectado (timeframe vazio) para symbol=%s strategy=%s. Ignorando contexto.",
                            symbol,
                            strategy_name,
                        )
                        open_positions.pop(context_key, None)
                        continue
                    frame = context_frames.get(context_key)
                    last_ts = frame.index[-1] if frame is not None and len(frame.index) else None
                    since = int(last_ts.timestamp() * 1000) + 1 if last_ts is not None else None

                    try:
                        latest = exchange.fetch_ohlcv(
                            symbol, timeframe, since=since, limit=1000
                        )
                    except ccxt.NetworkError as exc:
                        logger.warning(
                            "Falha de rede no polling LIVE (fetch_ohlcv). symbol=%s timeframe=%s erro=%s",
                            symbol, timeframe, exc,
                        )
                        continue
                    latest = self._closed_only(latest, timeframe)

                    if frame is None:
                        try:
                            bootstrap = exchange.fetch_ohlcv(symbol, timeframe, limit=max(100, int(cfg.bootstrap_bars)))
                        except ccxt.NetworkError as exc:
                            logger.warning(
                                "Falha de rede no bootstrap LIVE (fetch_ohlcv). symbol=%s timeframe=%s erro=%s",
                                symbol,
                                timeframe,
                                exc,
                            )
                            continue
                        frame = self._closed_only(bootstrap, timeframe) if bootstrap is not None else pd.DataFrame()

                    if latest is not None and not latest.empty and frame is not None and not frame.empty:
                        latest = latest.loc[latest.index > frame.index[-1]]

                    recovering = context_key in recovery_pending
                    if latest is None or latest.empty:
                        if not recovering:
                            if last_signals.get(context_key) != "SELL_BLOCKED":
                                last_signals[context_key] = "HOLD"
                            continue
                        if frame is None or frame.empty:
                            continue
                        strategy_obj = strategy_cache[context_key]
                        enriched, _ = self._recalculate_indicators(strategy_obj, frame)
                        if enriched is not None:
                            frame = enriched

                    if latest is not None and not latest.empty:
                        latest_ts = latest.index[-1]
                        latest_close = float(latest["close"].iloc[-1])
                        logger.info(
                            "Novo candle recebido - symbol=%s timeframe=%s ts=%s close=%.6f candles=%d",
                            symbol, timeframe, latest_ts, latest_close, len(latest),
                        )

                        frame = pd.concat([frame, latest]).sort_index()
                        frame = frame[~frame.index.duplicated(keep="last")]
                        frame = self._bound_frame(frame, cfg.max_frame_bars)

                        strategy_obj = strategy_cache.get(context_key)
                        if strategy_obj is None:
                            strategy_obj = self._build_runtime_strategy(
                                cfg,
                                symbol=symbol,
                                timeframe=timeframe,
                            )
                            strategy_cache[context_key] = strategy_obj

                        enriched, added_cols = self._recalculate_indicators(
                            strategy=strategy_obj, frame=frame
                        )
                        if enriched is not None:
                            frame = enriched
                        logger.info(
                            "Indicadores recalculados - strategy=%s added_cols=%d total_cols=%d",
                            getattr(strategy_obj, "name", strategy_obj.__class__.__name__),
                            added_cols, len(frame.columns),
                        )

                    context_frames[context_key] = frame

                    frame_for_signal = context_frames.get(context_key)
                    if frame_for_signal is None or frame_for_signal.empty:
                        last_signals[context_key] = "HOLD"
                        cycle_logs.append((context_key, "HOLD"))
                        continue

                    strategy_obj = strategy_cache.get(context_key)
                    if strategy_obj is None:
                        strategy_obj = self._build_runtime_strategy(
                            cfg,
                            symbol=symbol,
                            timeframe=timeframe,
                        )
                        strategy_cache[context_key] = strategy_obj

                        # Posição existente: avalia saídas independentes por contexto.
                    if context_key in open_positions:
                        position = open_positions[context_key]
                        recovery_pending.discard(context_key)
                        candle_key = frame_for_signal.index[-1].isoformat()
                        # Recuperacao so age no fechamento mais recente esperado, nunca em backlog.
                        if not self._is_current_closed_candle(frame_for_signal, timeframe):
                            self._record_sell_block(position, "STALE_CLOSED_CANDLE")
                            last_signals[context_key] = "SELL_BLOCKED"
                            continue
                        if candle_key == position.last_evaluated_candle:
                            last_signals[context_key] = "SELL_BLOCKED" if position.exit_pending else "HOLD"
                            continue
                        should_exit, exit_reason = self._check_exit_conditions(
                            position=position,
                            frame=frame_for_signal,
                            strategy=strategy_obj,
                        )
                        position.last_evaluated_candle = candle_key
                        if should_exit:
                            close_price = float(frame_for_signal["close"].iloc[-1])
                            success = self._try_close_position(
                                position=position,
                                close_price=close_price,
                                reason=exit_reason,
                                order_executor=order_executor,
                            )
                            if success:
                                open_positions.pop(context_key, None)
                                self._store_save_positions(store=store, positions=open_positions)
                                logger.info(
                                    "SELL executed | Open positions: %d/%d",
                                    len(open_positions),
                                    int(settings.trading.max_open_positions),
                                )
                                last_signals[context_key] = "SELL"
                                cycle_logs.append((context_key, "SELL"))
                            else:
                                classified_dust = position.status == "DUST"
                                if classified_dust:
                                    open_positions.pop(context_key, None)
                                self._store_save_positions(store=store, positions=open_positions)
                                if classified_dust:
                                    last_signals[context_key] = (
                                        "DUST_AFTER_PARTIAL_SELL" if self._last_exit_filled else "DUST"
                                    )
                                    cycle_logs.append((context_key, last_signals[context_key]))
                                else:
                                    last_signals[context_key] = "SELL" if self._last_exit_filled else "SELL_BLOCKED"
                        else:
                            if callable(getattr(exchange, "fetch_symbol_trading_filters", None)):
                                try:
                                    dust_reason = self._dust_filter_reason(
                                        exchange, position, float(frame_for_signal["close"].iloc[-1]))
                                except Exception:
                                    dust_reason = ""
                                if dust_reason and not position.exit_pending:
                                    self._validate_sell_ownership(exchange, position)
                                    self._detach_dust(position, dust_reason)
                                    open_positions.pop(context_key, None)
                            if position.status == "DUST":
                                last_signals[context_key] = "DUST"
                                cycle_logs.append((context_key, "DUST"))
                            else:
                                last_signals[context_key] = "HOLD"
                                cycle_logs.append((context_key, "HOLD"))
                            self._store_save_positions(store=store, positions=open_positions)
                        continue

                    if context_key not in target_contexts:
                        last_signals[context_key] = "HOLD"
                        cycle_logs.append((context_key, "HOLD"))
                        continue

                    if not self._is_current_closed_candle(frame_for_signal, timeframe):
                        last_signals[context_key] = "HOLD"
                        continue
                    signal = self._entry_signal(strategy=strategy_obj, frame=frame_for_signal)
                    if signal is None:
                        last_signals[context_key] = "HOLD"
                        cycle_logs.append((context_key, "HOLD"))
                        continue

                    reason = self._signal_reason(signal)
                    logger.info(
                        "Sinal gerado - symbol=%s strategy=%s signal=%s score=%.4f price=%.6f reason=%s",
                        symbol,
                        getattr(strategy_obj, "name", strategy_obj.__class__.__name__),
                        signal.signal.value,
                        float(signal.score),
                        float(signal.price),
                        reason,
                    )

                    if signal.signal == SignalType.BUY:
                        try:
                            free_usdt = self._fetch_free_usdt(exchange)
                            last_known_free_usdt = free_usdt
                        except ccxt.NetworkError as exc:
                            logger.warning(
                                "Falha de rede ao consultar saldo para BUY. symbol=%s erro=%s. "
                                "Pulando entrada neste ciclo.",
                                symbol,
                                exc,
                            )
                            last_signals[context_key] = "HOLD"
                            cycle_logs.append((context_key, "HOLD"))
                            continue
                        committed = self._capital_committed(open_positions)
                        capital_livre = max(0.0, free_usdt)

                        logger.info(
                            "Open positions: %d/%d | Capital livre: %.2f USDT | Capital comprometido: %.2f USDT",
                            len(open_positions),
                            int(settings.trading.max_open_positions),
                            capital_livre,
                            committed,
                        )

                        if len(open_positions) >= int(settings.trading.max_open_positions):
                            logger.info("Maximum open positions reached.")
                            last_signals[context_key] = "HOLD"
                            cycle_logs.append((context_key, "HOLD"))
                        elif context_key in open_positions:
                            logger.info("Context already has an open position. Skipping BUY.")
                            last_signals[context_key] = "HOLD"
                            cycle_logs.append((context_key, "HOLD"))
                        elif self._has_untracked_asset_balance(exchange, symbol):
                            # Um BUY anterior pode ter sido executado apesar de timeout/erro; nunca compra duas vezes.
                            logger.error(
                                "BUY bloqueado: saldo %s nao rastreado na Binance acima do minNotional. "
                                "Reconciliar manualmente antes de nova entrada.",
                                symbol,
                            )
                            last_signals[context_key] = "HOLD"
                            cycle_logs.append((context_key, "HOLD"))
                        else:
                            local_cfg = LiveTradingConfig(
                                symbol=symbol,
                                timeframe=timeframe,
                                strategy_name=strategy_name,
                                strategy_version=cfg.strategy_version,
                                symbols=cfg.symbols,
                                poll_seconds=cfg.poll_seconds,
                                bootstrap_bars=cfg.bootstrap_bars,
                                bootstrap_replay_bars=cfg.bootstrap_replay_bars,
                                max_cycles=cfg.max_cycles,
                                resume=cfg.resume,
                                output_prefix=cfg.output_prefix,
                                state_dir=cfg.state_dir,
                            )
                            new_position = self._try_open_position(
                                cfg=local_cfg,
                                signal=signal,
                                live_risk_service=live_risk_service,
                                risk_manager=risk_manager,
                                exchange=exchange,
                                available_capital=capital_livre,
                            )
                            if new_position is not None:
                                open_positions[new_position.context_key] = new_position
                                self._store_save_positions(store=store, positions=open_positions)
                                logger.info(
                                    "BUY accepted | Open positions: %d/%d",
                                    len(open_positions),
                                    int(settings.trading.max_open_positions),
                                )
                                last_signals[context_key] = "BUY"
                                cycle_logs.append((context_key, "BUY"))
                            else:
                                last_signals[context_key] = "HOLD"
                                cycle_logs.append((context_key, "HOLD"))
                    elif signal.signal == SignalType.SELL:
                        # Sinal SELL sem posicao nao e venda executada.
                        last_signals[context_key] = "HOLD"
                    else:
                        last_signals[context_key] = "HOLD"
                        cycle_logs.append((context_key, "HOLD"))

                # Registra os logs de cada ciclo por símbolo/contexto
                for context_key in sorted(target_contexts, key=lambda item: item[0]):
                    action = last_signals.get(context_key, "HOLD")
                    if action == "HOLD" and context_key not in open_positions and any(
                        position.context_key == context_key
                        for position in self._dust_positions.values()
                    ):
                        action = "HOLD_NO_ACTIVE_POSITION"
                    if logged_actions.get(context_key) != action:
                        logger.info("%s -> %s", context_key[0], action)
                        logged_actions[context_key] = action

                committed = self._capital_committed(open_positions)
                balance_is_current = True
                try:
                    free_usdt = self._fetch_free_usdt(exchange)
                    last_known_free_usdt = free_usdt
                except ccxt.NetworkError as exc:
                    balance_is_current = False
                    free_usdt = last_known_free_usdt
                    logger.warning(
                        "Falha de rede ao atualizar saldo no ciclo: %s. "
                        "Usando ultimo saldo conhecido %.2f USDT.",
                        exc,
                        free_usdt,
                    )
                capital_livre = max(0.0, free_usdt)
                self._log_terminal_position_summary(
                    open_positions=open_positions,
                    committed_usdt=committed,
                    free_usdt=capital_livre if balance_is_current else None,
                    max_open_positions=int(settings.trading.max_open_positions),
                )
                if cycles % int(settings.trading.asset_ranking_refresh_cycles) == 0:
                    self._log_asset_ranking(
                        asset_monitor=asset_monitor,
                        strategy_name=cfg.strategy_name,
                    )

                if cfg.max_cycles > 0 and cycles >= int(cfg.max_cycles):
                    break

                if cfg.max_cycles == 0:
                    self._sleep_fn(max(1.0, float(cfg.poll_seconds)))

            return {
                "status": "completed",
                "mode": "live",
                "exchange": settings.trading.exchange,
                "strategy": cfg.strategy_name,
                "strategy_version": cfg.strategy_version,
                "symbol": cfg.symbol,
                "symbols": target_symbols,
                "timeframe": cfg.timeframe,
                "free_usdt": startup["free_usdt"],
                "risk_per_trade": startup["risk_per_trade"],
                "quantity_calculated": startup["quantity_calculated"],
                "cycles": cycles,
                "open_position": len(open_positions) > 0,
                "open_positions": len(open_positions),
                "dust_positions": len(self._dust_positions),
                "capital_free": capital_livre,
                "capital_committed": committed,
                "residual_cost": sum(p.quantity * p.entry_price for p in self._dust_positions.values()),
                "last_signals": {"|".join(k): v for k, v in last_signals.items()},
            }
        except Exception:
            logger.exception("LIVE service failed during initialization or execution.")
            raise
        finally:
            exchange.disconnect()

    def emergency_exit(
        self,
        *,
        strategy_name: str,
        symbol: str,
        enable_real_orders: bool = False,
        state_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Pré-visualiza ou fecha explicitamente somente a posição de BNB persistida e pertencente ao bot."""
        if symbol != "BNB/USDT":
            raise ValueError("Emergency exit is restricted to BNB/USDT.")

        exchange = self._exchange_factory()
        try:
            exchange.connect()
            resolved_state_dir = state_dir or (self._base_dir / "optimization" / "results")
            store = self._position_store_factory(Path(resolved_state_dir) / "live_positions.json")
            positions = self._store_load_positions(store)
            matching = [
                state for state in positions.values()
                if state.symbol == symbol and state.strategy == strategy_name
            ]

            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker.get("last") or 0.0)
            if current_price <= 0.0:
                raise RuntimeError("Emergency exit blocked: Binance returned no usable BNB price.")

            if len(matching) > 1:
                raise RuntimeError("Emergency exit blocked: multiple matching persisted bot positions.")
            position = matching[0] if matching else None
            if position is None:
                return {
                    "bot_position_quantity": 0.0,
                    "current_price": current_price,
                    "estimated_notional": 0.0,
                    "would_sell": False,
                    "real_order_sent": False,
                    "dry_run": not enable_real_orders,
                }

            cfg = LiveTradingConfig(
                symbol=symbol,
                timeframe=position.timeframe,
                strategy_name=strategy_name,
                strategy_version="v1",
                max_cycles=1,
                poll_seconds=0.0,
                state_dir=Path(resolved_state_dir),
            )
            db_states = self._db_load_all_open_trade_states_with_retry(cfg=cfg)
            db_position = db_states.get(position.trade_id)
            if (
                db_position is None
                or db_position.symbol != position.symbol
                or db_position.strategy != position.strategy
                or abs(float(db_position.quantity) - float(position.quantity))
                > max(1e-10, float(position.quantity) * 1e-6)
            ):
                raise RuntimeError(
                    "Emergency exit blocked: persisted state and OPEN database trade "
                    "do not establish matching ownership."
                )

            free_qty = self._fetch_free_asset_quantity(exchange, symbol)
            if free_qty + max(1e-10, position.quantity * 1e-6) < position.quantity:
                raise RuntimeError(
                    "Emergency exit blocked: Binance free BNB is below bot-owned quantity; "
                    "no residual balance will be substituted."
                )

            result = {
                "bot_position_quantity": float(position.quantity),
                "current_price": current_price,
                "estimated_notional": float(position.quantity) * current_price,
                "would_sell": True,
                "real_order_sent": False,
                "dry_run": not enable_real_orders,
            }
            if not enable_real_orders:
                return result

            order_executor = self._order_executor_factory(exchange) if self._order_executor_factory else OrderExecutor(exchange)
            closed = self._try_close_position(
                position=position,
                close_price=current_price,
                reason="emergency_exit",
                order_executor=order_executor,
            )
            if closed:
                positions.pop(position.context_key, None)
            self._store_save_positions(store=store, positions=positions)
            result["real_order_sent"] = True
            result["dry_run"] = False
            result["closed"] = bool(closed)
            return result
        finally:
            exchange.disconnect()

    def _log_asset_ranking(self, asset_monitor: Any, strategy_name: str) -> None:
        try:
            ranking = asset_monitor.snapshot(strategy_name=strategy_name)
        except Exception as exc:
            logger.warning("Falha ao atualizar ranking de ativos: %s", exc)
            return

        if not ranking:
            if self._asset_ranking_has_history.get(strategy_name) is not False:
                logger.info("Sem trades CLOSED para %s", strategy_name)
            self._asset_ranking_has_history[strategy_name] = False
            return

        self._asset_ranking_has_history[strategy_name] = True
        logger.info("Ranking ativos (estrategia=%s):", strategy_name)
        for row in ranking:
            logger.info(
                "%s | trades=%d wins=%d losses=%d win_rate=%.2f%% pf=%.2f net=%.4f dd=%.4f exp=%.4f status=%s",
                row.symbol,
                row.trades,
                row.wins,
                row.losses,
                row.win_rate_pct,
                row.profit_factor,
                row.net_profit,
                row.max_drawdown,
                row.expectancy,
                row.status,
            )

    def _initialize_runtime_with_retry(
        self,
        cfg: LiveTradingConfig,
        exchange: BaseExchange,
    ) -> dict[str, Any]:
        attempt = 0
        max_attempts = 0 if int(cfg.max_cycles) == 0 else 5
        wait_seconds = max(5.0, float(cfg.poll_seconds))

        while True:
            attempt += 1
            try:
                exchange.connect()
                return self._initialize_runtime(cfg=cfg, exchange=exchange)
            except Exception as exc:
                try:
                    exchange.disconnect()
                except Exception:
                    pass

                if not self._is_transient_network_error(exc):
                    raise

                logger.warning(
                    "Falha de rede na inicializacao LIVE (tentativa %d): %s. "
                    "Novo retry em %.1fs.",
                    attempt,
                    exc,
                    wait_seconds,
                )
                if max_attempts > 0 and attempt >= max_attempts:
                    raise
                self._sleep_fn(wait_seconds)

    # ------------------------------------------------------------------
    # Reconciliação na inicialização
    # ------------------------------------------------------------------

    def _reconcile_on_startup(
        self,
        cfg: LiveTradingConfig,
        exchange: BaseExchange,
        store: Any,
        target_contexts: set[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], LivePositionState]:
        """Restaura posições abertas após falha/reinicialização, verificando o banco e a Binance."""
        self._store_load_positions(store)
        db_states = self._db_load_all_open_trade_states_with_retry(cfg=cfg)
        states = dict(db_states)
        for trade_id, saved in self._saved_positions.items():
            current = states.get(trade_id)
            if current is None:
                if saved.context_key in target_contexts and not self._db_is_trade_open(trade_id):
                    raise RuntimeError("LIVE recovery blocked: JSON/DB trade ownership mismatch")
                states[trade_id] = saved
            else:
                if (current.symbol, current.strategy) != (saved.symbol, saved.strategy):
                    raise RuntimeError("LIVE recovery blocked: conflicting trade ownership")
                # DB tem precedencia para fills; checkpoints incertos continuam bloqueados.
                states[trade_id] = replace(
                    current, timeframe=current.timeframe or saved.timeframe,
                    last_evaluated_candle=saved.last_evaluated_candle,
                    exit_pending=saved.exit_pending,
                    dust_reason=saved.dust_reason if current.status == "DUST" else "",
                )

        self._dust_positions = {}
        self._preserved_positions = {}
        deduped: dict[tuple[str, str, str], LivePositionState] = {}
        totals: dict[str, float] = {}
        for state in states.values():
            state = replace(state, timeframe=state.timeframe or cfg.timeframe,
                            strategy=state.strategy or cfg.strategy_name)
            if state.quantity <= 0 or state.status not in ("OPEN", "DUST"):
                raise RuntimeError("LIVE recovery blocked: invalid persisted quantity/status")
            totals[state.symbol] = totals.get(state.symbol, 0.0) + state.quantity
            if state.context_key not in target_contexts:
                # ETH antigo e contextos alheios nunca sao adotados nem vendidos.
                self._preserved_positions[state.trade_id] = state
            elif state.status == "DUST":
                self._dust_positions[state.trade_id] = state
            elif state.context_key in deduped:
                raise RuntimeError("LIVE recovery blocked: duplicate OPEN context")
            else:
                deduped[state.context_key] = state

        for symbol in {ctx[0] for ctx in target_contexts}:
            expected = totals.get(symbol, 0.0)
            if expected > 0 and not self._binance_has_position(exchange, symbol, expected):
                raise RuntimeError("LIVE recovery blocked: ownership not covered by the Binance balance")
        self._active_positions = deduped
        self._runtime_store = store

        # Nunca declare o saldo da exchange como pertencente ao bot: os saldos são fungíveis e
        # podem incluir ativos preexistentes ou mantidos manualmente. Saldos não rastreados permanecem
        # intactos; a proteção de entrada bloqueia compras até que sejam reconciliados.
        for symbol, strategy_name, timeframe in sorted(target_contexts):
            context_key = (symbol, strategy_name, timeframe)
            if context_key in deduped:
                continue

            asset_qty = max(0.0, self._fetch_total_asset_quantity(exchange, symbol)
                            - totals.get(symbol, 0.0))
            if asset_qty <= 0.0:
                continue
            # Mesma tolerância baseada em minNotional de _has_untracked_asset_balance, para que este
            # log reflita a decisão efetiva da proteção de entrada (poeira versus saldo material).
            try:
                price = float(exchange.fetch_ticker(symbol).get("last") or 0.0)
            except Exception:
                price = 0.0
            notional = asset_qty * price
            min_notional = self._resolve_min_notional(exchange, symbol)
            if notional < min_notional:
                logger.warning(
                    "Saldo Binance nao rastreado preservado sem ownership: symbol=%s qty=%.12f "
                    "notional=%.4f (< min_notional=%.4f -> residuo tolerado, nao bloqueia BUY). "
                    "Nenhuma venda sera inferida.",
                    symbol, asset_qty, notional, min_notional,
                )
            else:
                logger.warning(
                    "Saldo Binance nao rastreado preservado sem ownership: symbol=%s qty=%.12f "
                    "notional=%.4f (>= min_notional=%.4f -> material). "
                    "Novas entradas serao bloqueadas; nenhuma venda sera inferida.",
                    symbol, asset_qty, notional, min_notional,
                )

        logger.info("Reconciliacao: %d posicao(oes) restaurada(s).", len(deduped))
        return deduped

    # ------------------------------------------------------------------
    # Entrada na posição
    # ------------------------------------------------------------------

    def _try_open_position(
        self,
        cfg: LiveTradingConfig,
        signal: StrategySignal,
        live_risk_service: LiveRiskService,
        risk_manager: RiskManager,
        exchange: BaseExchange,
        available_capital: float,
    ) -> LivePositionState | None:
        """Cria Trade e envia uma ordem BUY respeitando o capital disponível."""
        entry_price = float(signal.price)
        stop_loss = (
            float(signal.stop_loss) if signal.stop_loss is not None
            else entry_price * 0.99
        )
        take_profit = (
            float(signal.take_profit) if signal.take_profit is not None
            else entry_price * 1.02
        )

        if available_capital <= 0.0:
            logger.info("Capital livre insuficiente. Skipping BUY.")
            return None

        try:
            preview = risk_manager.evaluate_trade(
                portfolio_value=available_capital,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_pct=signal.trailing_stop_pct,
                strategy_score=float(signal.score or 1.0),
            )
            preview_stake = float(preview.stake_amount)
        except Exception as exc:
            logger.warning("Pre-validacao de risco rejeitou entrada: %s", exc)
            preview_stake = 0.0

        min_reserve = float(settings.trading.min_free_usdt_reserve)
        if preview_stake > 0.0 and (available_capital - preview_stake) < min_reserve:
            logger.info("Capital reserve reached. Skipping BUY.")
            return None

        # Idempotência: interrompe se a Binance já tiver ordens abertas para este símbolo
        try:
            open_orders = exchange.fetch_open_orders(cfg.symbol)
            if open_orders:
                logger.warning(
                    "Idempotencia: %d ordens abertas na Binance para %s. "
                    "Abortando nova entrada.",
                    len(open_orders), cfg.symbol,
                )
                return None
        except Exception as exc:
            logger.warning(
                "Nao foi possivel verificar ordens abertas na Binance (%s). "
                "BUY bloqueado por seguranca.", exc,
            )
            return None

        # Persiste Trade ANTES de enviar a ordem (a chave estrangeira de Order exige trade.id)
        trade_id = self._db_create_trade(
            symbol=cfg.symbol,
            strategy_name=cfg.strategy_name,
            timeframe=cfg.timeframe,
            side="BUY",
            entry_price=entry_price,
            quantity=0.0,
            stake_amount=0.0,
            stop_loss=stop_loss,
            take_profit=take_profit,
            entry_time=datetime.now(tz=timezone.utc),
        )
        if trade_id is None:
            logger.error("Falha ao criar Trade no banco. Abortando entrada.")
            return None

        trade_stub = _TradeStub(trade_id)

        original_provider = getattr(live_risk_service, "_portfolio_value_provider", None)

        class _FixedPortfolioValueProvider:
            def __init__(self, value: float) -> None:
                self._value = Decimal(str(max(0.0, value)))

            def get_available_portfolio_value(self) -> Decimal:
                return self._value

        if original_provider is not None:
            live_risk_service._portfolio_value_provider = _FixedPortfolioValueProvider(available_capital)

        try:
            result = live_risk_service.execute_market_buy_with_risk(
                trade=trade_stub,
                symbol=cfg.symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                trailing_stop_pct=signal.trailing_stop_pct,
                strategy_score=float(signal.score or 1.0),
            )
        except ccxt.InsufficientFunds as exc:
            logger.warning(
                "Entrada rejeitada - saldo insuficiente: %s. Continuando loop.", exc
            )
            self._db_cancel_trade(trade_id)
            return None
        except (ccxt.InvalidOrder, ccxt.BadRequest, ccxt.BadSymbol) as exc:
            logger.warning(
                "Entrada rejeitada - filtro/ordem invalida Binance: %s. "
                "Continuando loop.", exc,
            )
            self._db_cancel_trade(trade_id)
            return None
        except ccxt.NetworkError as exc:
            logger.warning(
                "Falha de rede ao enviar ordem de compra: %s. "
                "Trade id=%d cancelado. Continuando loop.", exc, trade_id,
            )
            self._db_cancel_trade(trade_id)
            return None
        except Exception as exc:
            logger.error(
                "Erro inesperado ao executar compra: %s. "
                "Trade id=%d cancelado. Continuando loop.", exc, trade_id,
            )
            self._db_cancel_trade(trade_id)
            return None
        finally:
            if original_provider is not None:
                live_risk_service._portfolio_value_provider = original_provider

        fill_price = float(result.order.price or entry_price)
        fill_qty = max(0.0, float(result.order.filled_quantity or 0.0))
        if fill_qty <= 0.0:
            logger.error(
                "Ordem BUY sem quantidade executada (status=%s). Trade id=%d cancelado.",
                getattr(result.order, "status", "?"), trade_id,
            )
            self._db_cancel_trade(trade_id)
            return None
        base_fee_qty = max(0.0, float(result.order.base_fee_quantity or 0.0))
        position_qty = max(0.0, fill_qty - base_fee_qty)
        if position_qty <= 0.0:
            logger.error("BUY fill totalmente consumido por fee na moeda base; trade_id=%d bloqueado.", trade_id)
            self._db_cancel_trade(trade_id)
            return None
        stake = float(result.risk_params.stake_amount)
        entry_fee_usdt = result.order.fee_usdt
        fee_complete = entry_fee_usdt is not None and result.order.fee_source not in {"MISSING", "UNCONVERTED"}

        self._db_update_trade_after_buy(
            trade_id=trade_id,
            fill_price=fill_price,
            fill_qty=position_qty,
            stake_amount=stake,
            entry_fee_usdt=entry_fee_usdt,
            fee_accounting_complete=fee_complete,
        )

        state = LivePositionState(
            trade_id=trade_id,
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            strategy=cfg.strategy_name,
            quantity=position_qty,
            stake_amount=stake,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
            exchange_order_id=str(result.order.exchange_order_id or ""),
            original_quantity=position_qty,
            entry_fee_usdt=entry_fee_usdt,
            entry_fee_allocated_usdt=0.0,
            realized_gross_pnl=0.0,
            realized_exit_fees_usdt=0.0,
            realized_net_pnl=0.0 if fee_complete else None,
            fee_accounting_complete=fee_complete,
        )

        logger.info(
            "Posicao aberta - trade_id=%d symbol=%s qty=%.8f "
            "entry=%.4f sl=%.4f tp=%.4f",
            trade_id, cfg.symbol, fill_qty, fill_price, stop_loss, take_profit,
        )
        return state

    # ------------------------------------------------------------------
    # Condições de saída
    # ------------------------------------------------------------------

    def _check_exit_conditions(
        self,
        position: LivePositionState,
        frame: pd.DataFrame,
        strategy: Any,
    ) -> tuple[bool, str]:
        """Retorna (should_exit, reason): SL, TP ou sinal de saída da estratégia."""
        if frame.empty:
            return False, ""

        current_close = float(frame["close"].iloc[-1])

        if current_close <= position.stop_loss:
            logger.info(
                "Stop loss atingido - symbol=%s close=%.4f sl=%.4f",
                position.symbol, current_close, position.stop_loss,
            )
            return True, "stop_loss"

        if current_close >= position.take_profit:
            logger.info(
                "Take profit atingido - symbol=%s close=%.4f tp=%.4f",
                position.symbol, current_close, position.take_profit,
            )
            return True, "take_profit"

        exit_sig_fn = getattr(strategy, "exit_signal", None)
        if callable(exit_sig_fn):
            try:
                exit_sig = exit_sig_fn(frame, position.entry_price)
                if (
                    isinstance(exit_sig, StrategySignal)
                    and exit_sig.signal == SignalType.SELL
                ):
                    logger.info(
                        "Sinal de saida da estrategia - symbol=%s close=%.4f",
                        position.symbol, current_close,
                    )
                    return True, "strategy_exit"
            except Exception as exc:
                logger.warning("Erro ao calcular exit_signal: %s", exc)

        return False, ""

    # ------------------------------------------------------------------
    # Fechamento da posição
    # ------------------------------------------------------------------

    @staticmethod
    def _is_current_closed_candle(frame: pd.DataFrame, timeframe: str) -> bool:
        step = pd.Timedelta(timeframe)
        expected = pd.Timestamp.now(tz="UTC").floor(step) - step
        return not frame.empty and frame.index[-1] == expected

    def _record_sell_block(self, position: LivePositionState, reason: str) -> None:
        key = f"{reason}|{position.quantity:.12f}"
        if self._sell_blocks.get(position.trade_id) != key:
            logger.warning("SELL_BLOCKED trade_id=%d symbol=%s qty=%.12f reason=%s",
                           position.trade_id, position.symbol, position.quantity, reason)
            self._sell_blocks[position.trade_id] = key

    def _dust_filter_reason(self, exchange: BaseExchange, position: LivePositionState,
                            price: float) -> str:
        # Nunca inferir dust com filtros padrao ou indisponiveis.
        filters = exchange.fetch_symbol_trading_filters(position.symbol)
        values = {k: Decimal(str(filters[k])) for k in ("step_size", "min_qty", "min_notional")}
        if any(not v.is_finite() or v <= 0 for v in values.values()):
            raise RuntimeError("FILTERS_UNAVAILABLE")
        quantity = Decimal(str(position.quantity))
        step = values["step_size"]
        tradable = (quantity / step).to_integral_value(rounding=ROUND_DOWN) * step
        if tradable <= 0 or tradable < values["min_qty"]:
            return f"LOT_SIZE stepSize={step} minQty={values['min_qty']} roundedQty={tradable}"
        if price <= 0:
            raise RuntimeError("PRICE_UNAVAILABLE")
        # Para filtros nocionais a referencia atual da exchange tambem deve ser valida.
        ticker = exchange.fetch_ticker(position.symbol)
        ticker_price = float(ticker.get("last") or 0)
        if ticker_price <= 0:
            raise RuntimeError("PRICE_UNAVAILABLE")
        reference = max(price, ticker_price, float(ticker.get("high") or 0))
        if tradable * Decimal(str(reference)) < values["min_notional"]:
            return f"NOTIONAL minNotional={values['min_notional']} roundedQty={tradable}"
        return ""

    def _detach_dust(self, position: LivePositionState, reason: str) -> None:
        if position.exit_pending:
            raise RuntimeError("Uncertain execution cannot be classified as dust")
        override = self._db_ops.get("mark_dust")
        if override:
            override(trade_id=position.trade_id, quantity=position.quantity)
        else:
            from sqlalchemy import text
            with get_session() as session:
                result = session.execute(text(
                    "UPDATE trades SET status='DUST' WHERE id=:id AND is_paper=0 "
                    "AND status='OPEN' AND ABS(quantity-:qty)<=:tolerance"
                ), {"id": position.trade_id, "qty": position.quantity,
                    "tolerance": max(1e-10, position.quantity * 1e-6)})
                if result.rowcount != 1:
                    raise RuntimeError("DUST transition rejected: DB/state mismatch")
        position.status = "DUST"
        position.dust_reason = reason
        self._dust_positions[position.trade_id] = position
        self._record_sell_block(position, reason)
        # Mantem quantidade, stake original, taxas e PnL realizados intactos.

    def _validate_sell_ownership(self, exchange: BaseExchange, position: LivePositionState) -> None:
        protected = {p.trade_id: p for p in [*self._dust_positions.values(),
                     *self._preserved_positions.values(), *self._active_positions.values()]}
        protected[position.trade_id] = position
        expected = sum(p.quantity for p in protected.values()
                       if p.symbol.split("/")[0] == position.symbol.split("/")[0])
        if not self._binance_has_position(exchange, position.symbol, expected):
            raise RuntimeError("OWNERSHIP_BALANCE_MISMATCH")
        if self._fetch_free_asset_quantity(exchange, position.symbol) + 1e-12 < position.quantity:
            raise RuntimeError("OWNERSHIP_FREE_BALANCE_MISMATCH")
        if exchange.fetch_open_orders(position.symbol):
            raise RuntimeError("OPEN_ORDERS_REQUIRE_RECONCILIATION")

    @staticmethod
    def _sell_order_quantity(exchange: BaseExchange, position: LivePositionState) -> float:
        filters = exchange.fetch_symbol_trading_filters(position.symbol)
        step = Decimal(str(filters["step_size"]))
        # BNB paga sua propria comissao. Reserva a tarifa oficial, nunca BNB externo.
        available = Decimal(str(position.quantity))
        if position.symbol.split("/")[0] == "BNB":
            fee_reader = getattr(exchange, "fetch_trading_fee", None)
            if not callable(fee_reader):
                fee_reader = getattr(getattr(exchange, "_client", None), "fetch_trading_fee", None)
            if not callable(fee_reader):
                raise RuntimeError("FEE_RATE_UNAVAILABLE")
            rate = Decimal(str(fee_reader(position.symbol)["taker"]))
            if not rate.is_finite() or rate < 0 or rate >= 1:
                raise RuntimeError("FEE_RATE_UNAVAILABLE")
            available /= 1 + rate
        return float((available / step).to_integral_value(rounding=ROUND_DOWN) * step)

    def _try_close_position(
        self,
        position: LivePositionState,
        close_price: float,
        reason: str,
        order_executor: OrderExecutor,
    ) -> bool:
        """Envia uma ordem SELL e atualiza o banco. Retorna True em caso de sucesso e False para tentar novamente."""
        self._last_exit_filled = False
        if position.status == "DUST":
            self._record_sell_block(position, position.dust_reason or "DUST_RESERVED_NOT_FOR_SALE")
            return False
        if position.exit_pending:
            self._record_sell_block(position, "UNCERTAIN_PREVIOUS_SELL_RECONCILE_REQUIRED")
            return False
        exchange = getattr(order_executor, "exchange", None)
        if exchange is not None:
            try:
                self._validate_sell_ownership(exchange, position)
                dust_reason = self._dust_filter_reason(exchange, position, close_price)
            except Exception as exc:
                detail = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
                self._record_sell_block(position, f"PREFLIGHT_{detail}")
                return False
            if dust_reason:
                self._detach_dust(position, dust_reason)
                return False
        trade_stub = _TradeStub(position.trade_id)
        sell_quantity = float(position.quantity)
        order_quantity = sell_quantity
        if exchange is not None:
            try:
                order_quantity = self._sell_order_quantity(exchange, position)
                rounded = replace(position, quantity=order_quantity)
                blocked = self._dust_filter_reason(exchange, rounded, close_price)
                if blocked:
                    self._record_sell_block(position, "FEE_RESERVE_" + blocked)
                    return False
            except Exception:
                self._record_sell_block(position, "FEE_OR_FILTERS_UNAVAILABLE")
                return False
        position.exit_pending = True
        if self._runtime_store is not None:
            self._store_save_positions(self._runtime_store, self._active_positions)
        try:
            order = order_executor.execute_market_sell(
                trade=trade_stub,
                symbol=position.symbol,
                quantity=order_quantity,
                price=close_price,
            )
        except ccxt.NetworkError as exc:
            self._record_sell_block(position, "NETWORK_UNCERTAIN_SELL_RECONCILE_REQUIRED")
            return False
        except ccxt.InsufficientFunds as exc:
            position.exit_pending = False
            self._record_sell_block(position, "INSUFFICIENT_FREE_BOT_BALANCE")
            logger.warning(
                "Saldo insuficiente para vender somente a quantidade do bot %.12f: %s. "
                "Nenhum retry com saldo total sera feito; posicao preservada.",
                sell_quantity,
                exc,
            )
            return False
        except (ccxt.InvalidOrder, ccxt.BadRequest) as exc:
            position.exit_pending = False
            self._record_sell_block(position, f"ORDER_FILTER_{type(exc).__name__}")
            if self._is_notional_filter_error(exc):
                exchange = getattr(order_executor, "exchange", None)
                available_qty = self._fetch_free_asset_quantity(exchange, position.symbol)
                close_qty = available_qty if 0.0 < available_qty < sell_quantity else sell_quantity
                estimated_notional = max(0.0, close_qty * float(close_price))
                min_notional = self._resolve_min_notional(exchange, position.symbol)

                if estimated_notional < min_notional:
                    logger.warning(
                        "Fechamento local por NOTIONAL minimo: symbol=%s qty=%.8f notional=%.4f min_notional=%.4f. "
                        "Trade permanece OPEN; nenhum fechamento sem fill real sera contabilizado.",
                        position.symbol,
                        close_qty,
                        estimated_notional,
                        min_notional,
                    )
                    return False

                logger.warning(
                    "Falha NOTIONAL ao fechar posicao: %s. notional_estimado=%.4f min_notional=%.4f. "
                    "Mantendo posicao aberta para novo retry.",
                    exc,
                    estimated_notional,
                    min_notional,
                )
                return False

            logger.warning(
                "Erro de ordem ao fechar posicao: %s. Mantendo posicao aberta.",
                exc,
            )
            return False
        except Exception as exc:
            self._record_sell_block(position, "EXECUTION_UNCERTAIN_RECONCILE_REQUIRED")
            return False

        fill_price = float(order.price or close_price)
        fill_qty = min(sell_quantity, max(0.0, float(order.filled_quantity or 0.0)))
        if fill_qty <= 0.0:
            self._record_sell_block(position, "NO_CONFIRMED_FILL_RECONCILE_REQUIRED")
            logger.error(
                "SELL sem fill confirmado para trade_id=%d; mantendo quantidade %.12f aberta.",
                position.trade_id,
                sell_quantity,
            )
            return False
        base_fee_qty = max(0.0, float(getattr(order, "base_fee_quantity", 0.0) or 0.0))
        if float(order.filled_quantity or 0) > order_quantity + 1e-10 or fill_qty + base_fee_qty > sell_quantity + 1e-10:
            raise RuntimeError("Confirmed SELL exceeds owned quantity; reconcile accounting")
        remaining_qty = max(0.0, sell_quantity - fill_qty - base_fee_qty)
        original_quantity = position.original_quantity or sell_quantity
        gross_delta = (fill_price - position.entry_price) * fill_qty
        entry_fee_allocated = position.entry_fee_allocated_usdt
        entry_fee_usdt = position.entry_fee_usdt
        if entry_fee_usdt is not None and original_quantity > 0.0:
            allocation_quantity = fill_qty + base_fee_qty
            entry_fee_allocated += min(
                max(0.0, entry_fee_usdt - entry_fee_allocated),
                entry_fee_usdt * allocation_quantity / original_quantity,
            )
        exit_fee_usdt = getattr(order, "fee_usdt", None)
        exit_fees = position.realized_exit_fees_usdt
        if exit_fee_usdt is not None:
            exit_fees += max(0.0, float(exit_fee_usdt))
        gross_pnl = position.realized_gross_pnl + gross_delta
        fee_complete = (
            position.fee_accounting_complete
            and entry_fee_usdt is not None
            and exit_fee_usdt is not None
        )
        net_pnl = gross_pnl - entry_fee_allocated - exit_fees if fee_complete else None
        total_fees = float(entry_fee_usdt) + exit_fees if fee_complete else None
        fully_closed = remaining_qty == 0.0
        position.quantity = remaining_qty
        position.entry_fee_allocated_usdt = entry_fee_allocated
        position.realized_gross_pnl = gross_pnl
        position.realized_exit_fees_usdt = exit_fees
        position.realized_net_pnl = net_pnl
        position.fee_accounting_complete = fee_complete
        pnl_pct = (
            net_pnl / (position.entry_price * original_quantity) * 100.0
            if net_pnl is not None and position.entry_price > 0 and original_quantity > 0
            else None
        )

        self._db_record_live_exit(
            trade_id=position.trade_id,
            remaining_quantity=position.quantity,
            exit_price=fill_price,
            gross_pnl=gross_pnl,
            pnl=net_pnl,
            pnl_pct=pnl_pct,
            total_fees_usdt=total_fees,
            entry_fee_allocated_usdt=entry_fee_allocated,
            exit_fee_usdt=exit_fees,
            fee_accounting_complete=fee_complete,
            exit_reason=reason,
            exit_time=datetime.now(tz=timezone.utc),
            fully_closed=fully_closed,
        )
        position.exit_pending = str(getattr(order, "status", "CLOSED")).upper() not in (
            "CLOSED", "FILLED", "CANCELED", "CANCELLED", "EXPIRED", "REJECTED")
        self._last_exit_filled = True
        self._sell_blocks.pop(position.trade_id, None)

        if not fully_closed:
            if position.exit_pending:
                self._record_sell_block(position, "ORDER_NOT_TERMINAL_RECONCILE_REQUIRED")
            if exchange is not None and not position.exit_pending:
                try:
                    dust_reason = self._dust_filter_reason(exchange, position, fill_price)
                except Exception:
                    dust_reason = ""
                    self._record_sell_block(position, "POST_FILL_FILTERS_UNAVAILABLE")
                if dust_reason:
                    self._detach_dust(position, dust_reason)
            logger.warning(
                "SELL parcial: trade_id=%d executed=%.12f base_fee=%.12f remaining=%.12f "
                "gross_realized=%.8f net_realized=%s",
                position.trade_id,
                fill_qty,
                base_fee_qty,
                remaining_qty,
                gross_pnl,
                f"{net_pnl:.8f}" if net_pnl is not None else "UNKNOWN_FEES",
            )
            return False

        logger.info(
            "Posicao fechada - trade_id=%d symbol=%s reason=%s exit=%.4f "
            "gross_pnl=%.8f fees_usdt=%s net_pnl=%s",
            position.trade_id, position.symbol, reason,
            fill_price,
            gross_pnl,
            f"{total_fees:.8f}" if total_fees is not None else "UNKNOWN",
            f"{net_pnl:.8f}" if net_pnl is not None else "UNKNOWN",
        )
        return True

    # ------------------------------------------------------------------
    # Auxiliares de banco de dados (SQL direto — evita incompatibilidade com o esquema do ORM)
    # Cada método verifica primeiro self._db_ops para que os testes possam injetar objetos simulados.
    # ------------------------------------------------------------------

    def _db_create_trade(
        self, *, symbol: str, strategy_name: str, timeframe: str, side: str,
        entry_price: float, quantity: float, stake_amount: float,
        stop_loss: float, take_profit: float, entry_time: datetime,
    ) -> int | None:
        override = self._db_ops.get("create_trade")
        if override:
            return override(
                symbol=symbol, strategy_name=strategy_name, side=side,
                timeframe=timeframe,
                entry_price=entry_price, quantity=quantity,
                stake_amount=stake_amount, stop_loss=stop_loss,
                take_profit=take_profit, entry_time=entry_time,
            )
        from sqlalchemy import text
        try:
            with get_session() as session:
                result = session.execute(
                    text(
                        "INSERT INTO trades "
                        "(symbol, strategy_name, timeframe, side, status, is_paper, entry_price, "
                        "quantity, stake_amount, stop_loss, take_profit, fee, "
                        "entry_time, created_at, updated_at) "
                        "VALUES (:symbol, :strategy_name, :timeframe, :side, 'OPEN', 0, :entry_price, "
                        ":quantity, :stake_amount, :stop_loss, :take_profit, 0.0, "
                        ":entry_time, NOW(), NOW())"
                    ),
                    {
                        "symbol": symbol, "strategy_name": strategy_name, "side": side,
                        "timeframe": timeframe,
                        "entry_price": entry_price, "quantity": quantity,
                        "stake_amount": stake_amount, "stop_loss": stop_loss,
                        "take_profit": take_profit, "entry_time": entry_time,
                    },
                )
                return int(result.lastrowid)
        except Exception as exc:
            logger.error("_db_create_trade failed: %s", exc)
            return None

    def _db_update_trade_after_buy(
        self, *, trade_id: int, fill_price: float,
        fill_qty: float, stake_amount: float,
        entry_fee_usdt: float | None,
        fee_accounting_complete: bool,
    ) -> None:
        override = self._db_ops.get("update_after_buy")
        if override:
            override(trade_id=trade_id, fill_price=fill_price,
                     fill_qty=fill_qty, stake_amount=stake_amount,
                     entry_fee_usdt=entry_fee_usdt,
                     fee_accounting_complete=fee_accounting_complete)
            return
        from sqlalchemy import text
        try:
            with get_session() as session:
                session.execute(
                    text(
                        "UPDATE trades SET entry_price=:entry_price, "
                        "quantity=:quantity, stake_amount=:stake_amount, "
                        "original_quantity=:original_quantity, "
                        "entry_fee_usdt=:entry_fee_usdt, entry_fee_allocated_usdt=0.0, "
                        "exit_fee_usdt=0.0, gross_pnl=0.0, "
                        "fee=:fee_total, total_fees_usdt=:total_fees_usdt, "
                        "fee_accounting_complete=:fee_complete, "
                        "pnl=:initial_net_pnl, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=:trade_id"
                    ),
                    {"entry_price": fill_price, "quantity": fill_qty,
                     "stake_amount": stake_amount, "original_quantity": fill_qty,
                     "entry_fee_usdt": entry_fee_usdt,
                     "fee_total": float(entry_fee_usdt or 0.0) if fee_accounting_complete else 0.0,
                     "total_fees_usdt": float(entry_fee_usdt) if fee_accounting_complete and entry_fee_usdt is not None else None,
                     "fee_complete": fee_accounting_complete,
                     "initial_net_pnl": 0.0 if fee_accounting_complete else None,
                     "trade_id": trade_id},
                )
        except Exception as exc:
            logger.error("_db_update_trade_after_buy failed: %s", exc)

    def _db_cancel_trade(self, trade_id: int) -> None:
        override = self._db_ops.get("cancel_trade")
        if override:
            override(trade_id=trade_id)
            return
        from sqlalchemy import text
        try:
            with get_session() as session:
                session.execute(
                    text(
                        "UPDATE trades SET status='CANCELLED', "
                        "updated_at=NOW() WHERE id=:trade_id"
                    ),
                    {"trade_id": trade_id},
                )
        except Exception as exc:
            logger.error("_db_cancel_trade failed: %s", exc)

    def _db_close_trade(
        self, *, trade_id: int, exit_price: float, pnl: float,
        pnl_pct: float, exit_reason: str, exit_time: datetime,
    ) -> None:
        override = self._db_ops.get("close_trade")
        if override:
            override(trade_id=trade_id, exit_price=exit_price, pnl=pnl,
                     pnl_pct=pnl_pct, exit_reason=exit_reason, exit_time=exit_time)
            return
        from sqlalchemy import text
        try:
            with get_session() as session:
                session.execute(
                    text(
                        "UPDATE trades SET status='CLOSED', "
                        "exit_price=:exit_price, pnl=:pnl, pnl_pct=:pnl_pct, "
                        "exit_reason=:exit_reason, exit_time=:exit_time, "
                        "updated_at=NOW() WHERE id=:trade_id"
                    ),
                    {
                        "exit_price": exit_price, "pnl": pnl, "pnl_pct": pnl_pct,
                        "exit_reason": exit_reason, "exit_time": exit_time,
                        "trade_id": trade_id,
                    },
                )
        except Exception as exc:
            logger.error("_db_close_trade failed: %s", exc)

    def _db_record_live_exit(
        self,
        *,
        trade_id: int,
        remaining_quantity: float,
        exit_price: float,
        gross_pnl: float,
        pnl: float | None,
        pnl_pct: float | None,
        total_fees_usdt: float | None,
        entry_fee_allocated_usdt: float,
        exit_fee_usdt: float,
        fee_accounting_complete: bool,
        exit_reason: str,
        exit_time: datetime,
        fully_closed: bool,
    ) -> None:
        override = self._db_ops.get("record_live_exit")
        if override:
            override(
                trade_id=trade_id,
                remaining_quantity=remaining_quantity,
                exit_price=exit_price,
                gross_pnl=gross_pnl,
                pnl=pnl,
                pnl_pct=pnl_pct,
                total_fees_usdt=total_fees_usdt,
                entry_fee_allocated_usdt=entry_fee_allocated_usdt,
                exit_fee_usdt=exit_fee_usdt,
                fee_accounting_complete=fee_accounting_complete,
                exit_reason=exit_reason,
                exit_time=exit_time,
                fully_closed=fully_closed,
            )
            return
        from sqlalchemy import text
        try:
            with get_session() as session:
                result = session.execute(
                    text(
                        "UPDATE trades SET quantity=:remaining_quantity, "
                        "gross_pnl=:gross_pnl, pnl=:pnl, pnl_pct=:pnl_pct, "
                        "fee=:fee_compat, total_fees_usdt=:total_fees_usdt, "
                        "exit_fee_usdt=:exit_fee_usdt, "
                        "entry_fee_allocated_usdt=:entry_fee_allocated_usdt, "
                        "fee_accounting_complete=:fee_accounting_complete, "
                        "status=CASE WHEN :fully_closed THEN 'CLOSED' ELSE status END, "
                        "exit_price=CASE WHEN :fully_closed THEN :exit_price ELSE exit_price END, "
                        "exit_reason=CASE WHEN :fully_closed THEN :exit_reason ELSE exit_reason END, "
                        "exit_time=CASE WHEN :fully_closed THEN :exit_time ELSE exit_time END, "
                        "updated_at=CURRENT_TIMESTAMP WHERE id=:trade_id AND status='OPEN'"
                    ),
                    {
                        "remaining_quantity": remaining_quantity,
                        "gross_pnl": gross_pnl,
                        "pnl": pnl,
                        "pnl_pct": pnl_pct,
                        "fee_compat": total_fees_usdt if total_fees_usdt is not None else 0.0,
                        "total_fees_usdt": total_fees_usdt,
                        "exit_fee_usdt": exit_fee_usdt,
                        "entry_fee_allocated_usdt": entry_fee_allocated_usdt,
                        "fee_accounting_complete": fee_accounting_complete,
                        "fully_closed": fully_closed,
                        "exit_price": exit_price,
                        "exit_reason": exit_reason,
                        "exit_time": exit_time,
                        "trade_id": trade_id,
                    },
                )
                if result.rowcount != 1:
                    raise RuntimeError("Confirmed SELL has no matching OPEN LIVE trade")
        except Exception as exc:
            raise RuntimeError("Confirmed SELL accounting persistence failed; reconcile before retry") from exc

    def _db_update_open_trade_quantity(self, *, trade_id: int, quantity: float) -> None:
        override = self._db_ops.get("update_open_quantity")
        if override:
            override(trade_id=trade_id, quantity=quantity)
            return
        from sqlalchemy import text
        try:
            with get_session() as session:
                session.execute(
                    text(
                        "UPDATE trades SET quantity=:quantity, updated_at=NOW() "
                        "WHERE id=:trade_id AND status='OPEN'"
                    ),
                    {"quantity": quantity, "trade_id": trade_id},
                )
        except Exception as exc:
            logger.error("_db_update_open_trade_quantity failed: %s", exc)

    def _db_is_trade_open(self, trade_id: int) -> bool:
        override = self._db_ops.get("is_trade_open")
        if override:
            return bool(override(trade_id=trade_id))
        from sqlalchemy import text
        with get_session() as session:
            row = session.execute(
                text("SELECT status FROM trades WHERE id=:trade_id"),
                {"trade_id": trade_id},
            ).fetchone()
            return row is not None and str(row[0]).upper() == "OPEN"

    def _db_find_open_trade(self, symbol: str, strategy_name: str) -> int | None:
        override = self._db_ops.get("find_open_trade")
        if override:
            return override(symbol=symbol, strategy_name=strategy_name)
        from sqlalchemy import text
        with get_session() as session:
            row = session.execute(
                text(
                    "SELECT id FROM trades WHERE symbol=:symbol "
                    "AND strategy_name=:strategy_name AND status='OPEN' "
                    "ORDER BY id DESC LIMIT 1"
                ),
                {"symbol": symbol, "strategy_name": strategy_name},
            ).fetchone()
            return int(row[0]) if row else None

    def _db_load_trade_state(self, trade_id: int) -> LivePositionState | None:
        override = self._db_ops.get("load_trade_state")
        if override:
            return override(trade_id=trade_id)
        from sqlalchemy import text
        with get_session() as session:
            row = session.execute(
                text(
                    "SELECT id, symbol, timeframe, strategy_name, quantity, entry_price, "
                    "stake_amount, stop_loss, take_profit, entry_time, original_quantity, "
                    "entry_fee_usdt, entry_fee_allocated_usdt, gross_pnl, exit_fee_usdt, "
                    "fee_accounting_complete, pnl, status FROM trades "
                    "WHERE id=:trade_id AND status IN ('OPEN','DUST') AND is_paper=0"
                ),
                {"trade_id": trade_id},
            ).fetchone()
            if row is None:
                return None
            return LivePositionState(
                trade_id=int(row[0]),
                symbol=str(row[1]),
                timeframe=str(row[2] or ""),
                strategy=str(row[3] or ""),
                quantity=float(row[4] or 0.0),
                entry_price=float(row[5] or 0.0),
                stake_amount=float(row[6] or (float(row[4] or 0.0) * float(row[5] or 0.0))),
                stop_loss=float(row[7] or 0.0),
                take_profit=float(row[8] or 0.0),
                opened_at=str(
                    row[9].isoformat()
                    if hasattr(row[9], "isoformat") else row[9]
                ),
                exchange_order_id="",
                original_quantity=float(row[10] or row[4] or 0.0),
                entry_fee_usdt=float(row[11]) if row[11] is not None else None,
                entry_fee_allocated_usdt=float(row[12] or 0.0),
                realized_gross_pnl=float(row[13] or 0.0),
                realized_exit_fees_usdt=float(row[14] or 0.0),
                fee_accounting_complete=bool(row[15]),
                realized_net_pnl=float(row[16]) if row[16] is not None else None,
                status=str(row[17]),
            )

    def _db_load_all_open_trade_states(self) -> dict[int, LivePositionState]:
        override = self._db_ops.get("load_all_open_trade_states")
        if override:
            raw_states = override()
            if isinstance(raw_states, dict):
                return raw_states
            return {}
        from sqlalchemy import text
        states: dict[int, LivePositionState] = {}
        with get_session() as session:
            rows = session.execute(
                text(
                    "SELECT id, symbol, timeframe, strategy_name, quantity, entry_price, "
                    "stake_amount, stop_loss, take_profit, entry_time, original_quantity, "
                    "entry_fee_usdt, entry_fee_allocated_usdt, gross_pnl, exit_fee_usdt, "
                    "fee_accounting_complete, pnl, status "
                    "FROM trades WHERE status IN ('OPEN','DUST') AND is_paper=0"
                )
            ).fetchall()
            for row in rows:
                trade_id = int(row[0])
                state = LivePositionState(
                    trade_id=trade_id,
                    symbol=str(row[1] or ""),
                    timeframe=str(row[2] or ""),
                    strategy=str(row[3] or ""),
                    quantity=float(row[4] or 0.0),
                    entry_price=float(row[5] or 0.0),
                    stake_amount=float(row[6] or (float(row[4] or 0.0) * float(row[5] or 0.0))),
                    stop_loss=float(row[7] or 0.0),
                    take_profit=float(row[8] or 0.0),
                    opened_at=str(row[9].isoformat() if hasattr(row[9], "isoformat") else row[9]),
                    exchange_order_id="",
                    original_quantity=float(row[10] or row[4] or 0.0),
                    entry_fee_usdt=float(row[11]) if row[11] is not None else None,
                    entry_fee_allocated_usdt=float(row[12] or 0.0),
                    realized_gross_pnl=float(row[13] or 0.0),
                    realized_exit_fees_usdt=float(row[14] or 0.0),
                    fee_accounting_complete=bool(row[15]),
                    realized_net_pnl=float(row[16]) if row[16] is not None else None,
                    status=str(row[17]),
                )
                states[trade_id] = state
        return states

    def _store_load_positions(self, store: Any) -> dict[tuple[str, str, str], LivePositionState]:
        loaded: list[LivePositionState] = []
        if hasattr(store, "load_all") and callable(store.load_all):
            raw = store.load_all()
            if isinstance(raw, list):
                loaded = [s for s in raw if isinstance(s, LivePositionState)]
        else:
            state = store.load() if hasattr(store, "load") else None
            if isinstance(state, LivePositionState):
                loaded = [state]

        positions: dict[tuple[str, str, str], LivePositionState] = {}
        self._saved_positions = {state.trade_id: state for state in loaded}
        if len(self._saved_positions) != len(loaded):
            raise RuntimeError("Duplicate persisted trade ownership")
        for state in loaded:
            if state.status == "DUST":
                self._dust_positions[state.trade_id] = state
                continue
            existing = positions.get(state.context_key)
            if existing is not None:
                raise RuntimeError("Duplicate persisted OPEN context")
            positions[state.context_key] = state
        return positions

    def _store_save_positions(
        self,
        store: Any,
        positions: dict[tuple[str, str, str], LivePositionState],
    ) -> None:
        by_trade = {**self._preserved_positions, **self._dust_positions}
        by_trade.update({p.trade_id: p for p in positions.values()})
        states = list(by_trade.values())
        if hasattr(store, "save_all") and callable(store.save_all):
            store.save_all(states)
            return
        # Armazenamento de estado único compatível com versões anteriores, usado em testes.
        if len(states) == 0:
            if hasattr(store, "clear") and callable(store.clear):
                store.clear()
            return
        if hasattr(store, "save") and callable(store.save):
            store.save(states[0])

    def _migrate_legacy_state_file(
        self,
        symbol: str,
        strategy_name: str,
        timeframe: str,
        state_dir: Path,
        store: Any,
    ) -> None:
        new_positions = self._store_load_positions(store)
        symbol_safe = symbol.replace("/", "_")
        legacy_path = Path(state_dir) / f"live_position_{symbol_safe}_{timeframe}.json"
        if not legacy_path.exists():
            return

        try:
            raw = json.loads(legacy_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            migrated = LivePositionState(
                trade_id=int(raw["trade_id"]),
                symbol=str(raw["symbol"]),
                timeframe=str(raw.get("timeframe", timeframe)),
                strategy=str(raw.get("strategy", strategy_name)),
                quantity=float(raw.get("quantity", 0.0)),
                stake_amount=float(raw.get("stake_amount", float(raw.get("quantity", 0.0)) * float(raw.get("entry_price", 0.0)))),
                entry_price=float(raw.get("entry_price", 0.0)),
                stop_loss=float(raw.get("stop_loss", 0.0)),
                take_profit=float(raw.get("take_profit", 0.0)),
                opened_at=str(raw.get("opened_at") or raw.get("entry_ts") or ""),
                exchange_order_id=str(raw.get("exchange_order_id", "")),
                original_quantity=float(raw.get("original_quantity", raw.get("quantity", 0.0)) or 0.0),
                entry_fee_usdt=(float(raw["entry_fee_usdt"]) if raw.get("entry_fee_usdt") is not None else None),
                entry_fee_allocated_usdt=float(raw.get("entry_fee_allocated_usdt", 0.0) or 0.0),
                realized_gross_pnl=float(raw.get("realized_gross_pnl", 0.0) or 0.0),
                realized_exit_fees_usdt=float(raw.get("realized_exit_fees_usdt", 0.0) or 0.0),
                realized_net_pnl=(float(raw["realized_net_pnl"]) if raw.get("realized_net_pnl") is not None else None),
                fee_accounting_complete=bool(raw.get("fee_accounting_complete", False)),
            )
            new_positions[migrated.context_key] = migrated
            self._store_save_positions(store=store, positions=new_positions)
            logger.info("Migracao de estado legacy concluida: %s -> live_positions.json", legacy_path.name)
        except Exception as exc:
            logger.warning("Falha ao migrar arquivo legacy de posicao: %s", exc)

    def _fetch_free_usdt(self, exchange: BaseExchange) -> float:
        balance = exchange.fetch_balance()
        if not isinstance(balance, dict):
            return 0.0

        free_bucket = balance.get("free")
        if isinstance(free_bucket, dict):
            return float(free_bucket.get("USDT", 0.0) or 0.0)

        by_asset = balance.get("USDT")
        if isinstance(by_asset, dict):
            return float(by_asset.get("free", 0.0) or 0.0)
        return 0.0

    def _fetch_free_asset_quantity(self, exchange: BaseExchange | None, symbol: str) -> float:
        if exchange is None:
            return 0.0

        try:
            balance = exchange.fetch_balance()
        except Exception:
            return 0.0

        if not isinstance(balance, dict):
            return 0.0

        base_asset = str(symbol).split("/")[0].upper()
        by_asset = balance.get(base_asset)
        if isinstance(by_asset, dict):
            return float(by_asset.get("free", 0.0) or 0.0)

        free_bucket = balance.get("free")
        if isinstance(free_bucket, dict):
            return float(free_bucket.get(base_asset, 0.0) or 0.0)
        return 0.0

    def _fetch_total_asset_quantity(self, exchange: BaseExchange | None, symbol: str) -> float:
        if exchange is None:
            return 0.0

        try:
            balance = exchange.fetch_balance()
        except Exception:
            return 0.0

        if not isinstance(balance, dict):
            return 0.0

        base_asset = str(symbol).split("/")[0].upper()
        by_asset = balance.get(base_asset)
        if isinstance(by_asset, dict):
            free_qty = float(by_asset.get("free", 0.0) or 0.0)
            used_qty = float(by_asset.get("used", 0.0) or 0.0)
            return max(0.0, free_qty + used_qty)

        free_bucket = balance.get("free")
        used_bucket = balance.get("used")
        if isinstance(free_bucket, dict) or isinstance(used_bucket, dict):
            free_qty = float((free_bucket or {}).get(base_asset, 0.0) or 0.0)
            used_qty = float((used_bucket or {}).get(base_asset, 0.0) or 0.0)
            return max(0.0, free_qty + used_qty)
        return 0.0

    def _is_notional_filter_error(self, exc: Exception) -> bool:
        message = str(exc)
        return "Filter failure: NOTIONAL" in message or "\"code\":-1013" in message

    def _is_transient_network_error(self, exc: Exception) -> bool:
        if isinstance(exc, ccxt.NetworkError):
            return True

        message = str(exc).lower()
        markers = (
            "getaddrinfo failed",
            "name resolution",
            "failed to resolve",
            "temporary failure",
            "connection aborted",
            "max retries exceeded",
            "httpsconnectionpool",
        )
        return any(marker in message for marker in markers)

    def _resolve_min_notional(
        self,
        exchange: BaseExchange | None,
        symbol: str,
        default: float = 5.0,
    ) -> float:
        if exchange is None:
            return float(default)

        try:
            fetch_filters = getattr(exchange, "fetch_symbol_trading_filters", None)
            if callable(fetch_filters):
                filters = fetch_filters(symbol)
                min_notional = float((filters or {}).get("min_notional", 0.0) or 0.0)
                if min_notional > 0.0:
                    return min_notional
        except Exception:
            pass

        return float(default)

    def _capital_committed(
        self,
        positions: dict[tuple[str, str, str], LivePositionState],
    ) -> float:
        committed = 0.0
        for state in positions.values():
            if state.status != "OPEN":
                continue
            stake = float(state.quantity) * float(state.entry_price)
            committed += max(0.0, stake)
        return committed

    def _log_terminal_position_summary(
        self,
        *,
        open_positions: dict[tuple[str, str, str], LivePositionState],
        committed_usdt: float,
        free_usdt: float | None,
        max_open_positions: int,
    ) -> None:
        """Emite estado OPEN/DUST e contabilidade apenas quando o resumo visível muda."""
        active = sorted(
            (position for position in open_positions.values() if position.status == "OPEN"),
            key=lambda position: position.trade_id,
        )
        dust = sorted(
            (position for position in self._dust_positions.values() if position.status == "DUST"),
            key=lambda position: position.trade_id,
        )
        dust_by_asset: dict[str, float] = {}
        for position in dust:
            asset = position.symbol.split("/")[0]
            dust_by_asset[asset] = dust_by_asset.get(asset, 0.0) + max(0.0, position.quantity)

        dust_quantity = (
            ",".join(f"{asset}:{quantity:.12f}" for asset, quantity in sorted(dust_by_asset.items()))
            if dust_by_asset else "none"
        )
        dust_cost = sum(position.quantity * position.entry_price for position in dust)
        free_text = f"{free_usdt:.8f}" if free_usdt is not None else "UNKNOWN"
        summary = (
            f"LIVE_STATE | OPEN={len(active)}/{max_open_positions} DUST={len(dust)} "
            f"DUST_qty={dust_quantity} capital_committed_usdt={committed_usdt:.2f} "
            f"dust_cost_usdt={dust_cost:.6f} capital_free_usdt={free_text}"
            f" balance_status={'CURRENT' if free_usdt is not None else 'UNAVAILABLE'}"
        )
        trade_lines = tuple(
            "LIVE_TRADE | trade_id=%d status=%s symbol=%s quantity=%.12f realized_pnl_usdt=%s"
            % (
                position.trade_id,
                position.status,
                position.symbol,
                position.quantity,
                f"{position.realized_net_pnl:.6f}"
                if position.realized_net_pnl is not None else "UNKNOWN",
            )
            + (
                f" timeframe={position.timeframe} strategy={position.strategy}"
                f" entry_price={position.entry_price:.8f} stop_loss={position.stop_loss}"
                f" take_profit={position.take_profit} exit_pending={position.exit_pending}"
            )
            for position in [*active, *dust]
        )
        signature = (summary, *trade_lines)
        now = monotonic()
        if (signature == self._last_terminal_position_summary
                and self._last_terminal_summary_at is not None
                and now - self._last_terminal_summary_at < 300.0):
            return

        logger.info("%s", summary)
        for line in trade_lines:
            logger.info("%s", line)
        self._last_terminal_position_summary = signature
        self._last_terminal_summary_at = now

    def _binance_has_position(
        self,
        exchange: BaseExchange,
        symbol: str,
        quantity_hint: float,
    ) -> bool:
        try:
            balance = exchange.fetch_balance()
        except Exception as exc:
            raise RuntimeError(
                f"LIVE recovery blocked: Binance balance unavailable for {symbol}; "
                "no order was sent."
            ) from exc

        if not isinstance(balance, dict):
            raise RuntimeError(
                f"LIVE recovery blocked: invalid Binance balance for {symbol}; no order was sent."
            )

        base_asset = str(symbol).split("/")[0].upper()

        by_asset = balance.get(base_asset)
        if isinstance(by_asset, dict):
            free_qty = float(by_asset.get("free", 0.0) or 0.0)
            used_qty = float(by_asset.get("used", 0.0) or 0.0)
            actual_qty = free_qty + used_qty
            return actual_qty + max(1e-10, float(quantity_hint) * 1e-6) >= float(quantity_hint)

        free_bucket = balance.get("free")
        used_bucket = balance.get("used")
        if isinstance(free_bucket, dict) or isinstance(used_bucket, dict):
            free_qty = float((free_bucket or {}).get(base_asset, 0.0) or 0.0)
            used_qty = float((used_bucket or {}).get(base_asset, 0.0) or 0.0)
            actual_qty = free_qty + used_qty
            return actual_qty + max(1e-10, float(quantity_hint) * 1e-6) >= float(quantity_hint)

        return False

    # ------------------------------------------------------------------
    # Inicialização
    # ------------------------------------------------------------------

    def _initialize_runtime(self, cfg: LiveTradingConfig, exchange: BaseExchange) -> dict[str, Any]:
        strategy = self._build_runtime_strategy(cfg, symbol=cfg.symbol, timeframe=cfg.timeframe)

        exchange.fetch_balance()
        provider = BinancePortfolioValueProvider(exchange)
        free_usdt = float(provider.get_available_portfolio_value())
        if free_usdt <= 0.0:
            raise RuntimeError("Saldo USDT livre indisponivel (<= 0). Operacao LIVE abortada.")

        sizer = self._sizer_factory()
        risk_manager = RiskManager(sizer=sizer)

        if self._order_executor_factory is not None:
            order_executor = self._order_executor_factory(exchange)
        else:
            order_executor = OrderExecutor(exchange)

        if self._live_risk_service_factory is not None:
            live_risk_service = self._live_risk_service_factory(
                order_executor, risk_manager, provider
            )
        else:
            live_risk_service = LiveRiskService(
                order_executor=order_executor,
                risk_manager=risk_manager,
                portfolio_value_provider=provider,
            )

        frame = exchange.fetch_ohlcv(
            cfg.symbol, cfg.timeframe, limit=max(100, int(cfg.bootstrap_bars))
        )
        frame = self._closed_only(frame, cfg.timeframe)
        if frame is None or frame.empty:
            raise RuntimeError(
                f"Sem candles para bootstrap LIVE em {cfg.symbol}/{cfg.timeframe}."
            )
        frame = self._bound_frame(frame, cfg.max_frame_bars)

        ticker = exchange.fetch_ticker(cfg.symbol)
        entry_price = float(ticker.get("last") or float(frame["close"].iloc[-1]))
        preview_stop = entry_price * 0.99
        preview_take = entry_price * 1.02
        preview = risk_manager.evaluate_trade(
            portfolio_value=free_usdt,
            entry_price=entry_price,
            stop_loss=preview_stop,
            take_profit=preview_take,
            strategy_score=1.0,
        )

        logger.info("Modo: LIVE")
        logger.info("Exchange: %s", settings.trading.exchange)
        logger.info("Strategy: %s", cfg.strategy_name)
        logger.info("Strategy Version: %s", cfg.strategy_version)
        logger.info("Simbolo: %s", cfg.symbol)
        logger.info("Timeframe: %s", cfg.timeframe)
        logger.info("Saldo USDT livre: %.6f", free_usdt)
        logger.info("Risco por operacao: %.6f", float(settings.risk.max_risk_per_trade_pct))
        logger.info("Quantidade calculada: %.8f", float(preview.quantity))

        return {
            "strategy": strategy,
            "provider": provider,
            "risk_manager": risk_manager,
            "order_executor": order_executor,
            "live_risk_service": live_risk_service,
            "frame": frame,
            "free_usdt": free_usdt,
            "risk_per_trade": float(settings.risk.max_risk_per_trade_pct),
            "quantity_calculated": float(preview.quantity),
        }

    def _build_runtime_strategy(
        self,
        cfg: LiveTradingConfig,
        *,
        symbol: str,
        timeframe: str,
    ) -> Any:
        hypothesis_payload = cfg.hypothesis_config if isinstance(cfg.hypothesis_config, dict) else {}
        params_raw = hypothesis_payload.get("approved_parameters") if isinstance(hypothesis_payload, dict) else {}
        strategy_params = dict(params_raw) if isinstance(params_raw, dict) else {}

        try:
            strategy = self._strategy_factory(cfg.strategy_name, **strategy_params)
        except TypeError:
            strategy = self._strategy_factory(cfg.strategy_name)

        strategy = wrap_strategy_with_hypothesis(
            strategy,
            hypothesis_gate_config_from_payload(hypothesis_payload),
            symbol=symbol,
            timeframe=timeframe,
        )
        strategy.initialize()
        return strategy

    @staticmethod
    def _bound_frame(frame: pd.DataFrame, max_bars: int) -> pd.DataFrame:
        cap = max(200, int(max_bars or 0))
        if len(frame) <= cap:
            return frame
        return frame.tail(cap).copy()

    @staticmethod
    def _closed_only(frame: pd.DataFrame | None, timeframe: str) -> pd.DataFrame | None:
        if frame is None or frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
            return frame
        from paper_trading.paper_live_service import PaperLiveService

        normalized = frame.copy()
        if normalized.index.tz is None:
            normalized.index = normalized.index.tz_localize("UTC")
        else:
            normalized.index = normalized.index.tz_convert("UTC")
        normalized = normalized.sort_index()
        normalized = normalized[~normalized.index.duplicated(keep="last")]
        return PaperLiveService._closed_candles(normalized, timeframe)

    def _has_untracked_asset_balance(self, exchange: BaseExchange, symbol: str) -> bool:
        if any(p.exit_pending for p in [*self._dust_positions.values(),
               *self._preserved_positions.values()] if p.symbol == symbol):
            return True
        try:
            balance = exchange.fetch_balance()
        except Exception:
            logger.error("BUY blocked: unable to verify %s balance ownership.", symbol)
            return True
        if not isinstance(balance, dict):
            logger.error("BUY blocked: invalid balance response while verifying %s.", symbol)
            return True
        base_asset = str(symbol).split("/")[0].upper()
        by_asset = balance.get(base_asset)
        if isinstance(by_asset, dict):
            qty = float(by_asset.get("free", 0.0) or 0.0) + float(by_asset.get("used", 0.0) or 0.0)
        else:
            free_bucket = balance.get("free") or {}
            used_bucket = balance.get("used") or {}
            qty = float(free_bucket.get(base_asset, 0.0) or 0.0) + float(used_bucket.get(base_asset, 0.0) or 0.0)
        if qty <= 0.0:
            return any(p.quantity > 0 for p in [*self._dust_positions.values(),
                       *self._preserved_positions.values()] if p.symbol == symbol)
        reserved = sum(p.quantity for p in [*self._dust_positions.values(),
                       *self._preserved_positions.values()]
                       if p.symbol.split("/")[0] == base_asset)
        if qty + max(1e-10, reserved * 1e-6) < reserved:
            return True
        qty = max(0.0, qty - reserved)
        try:
            price = float(exchange.fetch_ticker(symbol).get("last") or 0.0)
        except Exception:
            return True
        return qty * price >= self._resolve_min_notional(exchange, symbol)

    def _db_load_all_open_trade_states_with_retry(
        self,
        *,
        cfg: LiveTradingConfig,
    ) -> dict[int, LivePositionState]:
        attempts = 3
        base_wait = max(1.0, float(cfg.poll_seconds))
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._db_load_all_open_trade_states()
            except Exception as exc:  # pragma: no cover - defensive against infra failures
                last_exc = exc
                if attempt >= attempts:
                    break
                wait = min(30.0, base_wait * (2 ** (attempt - 1)))
                logger.warning(
                    "DB indisponivel durante reconciliacao LIVE (tentativa %d/%d): %s. Retry em %.1fs.",
                    attempt,
                    attempts,
                    exc,
                    wait,
                )
                self._sleep_fn(wait)

        raise RuntimeError(
            "Banco indisponivel durante startup/recovery LIVE. "
            "Fail-safe acionado para evitar estado inconsistente."
        ) from last_exc

    # ------------------------------------------------------------------
    # Auxiliares de sinais
    # ------------------------------------------------------------------

    def _recalculate_indicators(
        self, strategy: Any, frame: pd.DataFrame
    ) -> tuple[pd.DataFrame | None, int]:
        calculate = getattr(strategy, "calculate", None)
        if not callable(calculate):
            return None, 0
        before_cols = set(str(col) for col in frame.columns)
        enriched = calculate(frame)
        if enriched is None or not isinstance(enriched, pd.DataFrame):
            return None, 0
        after_cols = set(str(col) for col in enriched.columns)
        return enriched, len(after_cols - before_cols)

    def _entry_signal(self, strategy: Any, frame: pd.DataFrame) -> StrategySignal | None:
        entry_signal_fn = getattr(strategy, "entry_signal", None)
        if not callable(entry_signal_fn):
            return None
        signal = entry_signal_fn(frame)
        return signal if isinstance(signal, StrategySignal) else None

    def _signal_reason(self, signal: StrategySignal) -> str:
        metadata = signal.metadata or {}
        for key in ("entry_reason", "exit_reason", "reason"):
            raw = metadata.get(key)
            if raw:
                return str(raw)
        return "n/a"
