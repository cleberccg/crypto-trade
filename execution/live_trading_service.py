"""Official live trading service for Binance Spot using risk-managed sizing."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
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
# Position state
# ---------------------------------------------------------------------------

@dataclass
class LivePositionState:
    """Tracks an open live position for restart recovery."""

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

    @property
    def context_key(self) -> tuple[str, str, str]:
        return (self.symbol, self.strategy, self.timeframe)


class _LivePositionStore:
    """Persists open positions to a JSON file for crash/restart recovery."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def save(self, state: LivePositionState) -> None:
        # Backward-compatible single-state save.
        self.save_all([state])

    def save_all(self, states: list[LivePositionState]) -> None:
        try:
            payload = json.dumps([asdict(state) for state in states], ensure_ascii=False)
            atomic_write_text(self._path, payload, encoding="utf-8")
            atomic_write_text(self._path.with_suffix(self._path.suffix + ".bak"), payload, encoding="utf-8")
            logger.debug("LivePositionStore.save_all -> %s (count=%d)", self._path, len(states))
        except Exception as exc:
            logger.warning("LivePositionStore: failed to save state: %s", exc)

    def load(self) -> LivePositionState | None:
        # Backward-compatible single-state load.
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
                return []
            try:
                data = json.loads(backup.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.warning(
                    "LivePositionStore: failed to load backup state from %s: %s",
                    backup,
                    exc,
                )
                return []
        try:
            if isinstance(data, dict):
                # Legacy single-position format.
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
                )
                logger.info("LivePositionStore.load_all -> loaded legacy single state")
                return [state]

            states: list[LivePositionState] = []
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
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
                        )
                    )
            logger.info("LivePositionStore.load_all -> loaded %d position(s)", len(states))
            return states
        except Exception as exc:
            logger.warning(
                "LivePositionStore: failed to load state from %s: %s", self._path, exc
            )
            return []

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
    """Minimal Trade-like object carrying only the persisted ID."""

    __slots__ = ("id",)

    def __init__(self, trade_id: int) -> None:
        self.id = trade_id


# ---------------------------------------------------------------------------
# Config
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
# Service
# ---------------------------------------------------------------------------

class LiveTradingService:
    """Runs live market monitoring with full entry/exit cycle and restart recovery."""

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

    # ------------------------------------------------------------------
    # Public entry point
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

            # Build multi-position state store
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

            # Reconcile any existing open positions on restart
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
                        frame = bootstrap if bootstrap is not None else pd.DataFrame()

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

                    # Existing position: evaluate independent exits per context.
                    if context_key in open_positions:
                        position = open_positions[context_key]
                        should_exit, exit_reason = self._check_exit_conditions(
                            position=position,
                            frame=frame_for_signal,
                            strategy=strategy_obj,
                        )
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
                                last_signals[context_key] = "HOLD"
                                cycle_logs.append((context_key, "HOLD"))
                        else:
                            last_signals[context_key] = "HOLD"
                            cycle_logs.append((context_key, "HOLD"))
                        continue

                    if context_key not in target_contexts:
                        last_signals[context_key] = "HOLD"
                        cycle_logs.append((context_key, "HOLD"))
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
                        capital_livre = max(0.0, free_usdt - committed)

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
                        last_signals[context_key] = "SELL"
                        cycle_logs.append((context_key, "SELL"))
                    else:
                        last_signals[context_key] = "HOLD"
                        cycle_logs.append((context_key, "HOLD"))

                # Cycle logs by symbol/context
                for context_key in sorted(target_contexts, key=lambda item: item[0]):
                    action = last_signals.get(context_key, "HOLD")
                    logger.info("%s -> %s", context_key[0], action)

                open_ctx_labels = [
                    f"{pos.symbol} OPEN"
                    for pos in sorted(open_positions.values(), key=lambda item: (item.symbol, item.timeframe, item.strategy))
                ]
                committed = self._capital_committed(open_positions)
                try:
                    free_usdt = self._fetch_free_usdt(exchange)
                    last_known_free_usdt = free_usdt
                except ccxt.NetworkError as exc:
                    free_usdt = last_known_free_usdt
                    logger.warning(
                        "Falha de rede ao atualizar saldo no ciclo: %s. "
                        "Usando ultimo saldo conhecido %.2f USDT.",
                        exc,
                        free_usdt,
                    )
                capital_livre = max(0.0, free_usdt - committed)
                logger.info("Open positions: %d/%d", len(open_positions), int(settings.trading.max_open_positions))
                for label in open_ctx_labels:
                    logger.info(label)
                logger.info("Capital comprometido: %.2f USDT", committed)
                logger.info("Capital livre: %.2f USDT", capital_livre)
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
            }
        except Exception:
            logger.exception("LIVE service failed during initialization or execution.")
            raise
        finally:
            exchange.disconnect()

    def _log_asset_ranking(self, asset_monitor: Any, strategy_name: str) -> None:
        try:
            ranking = asset_monitor.snapshot(strategy_name=strategy_name)
        except Exception as exc:
            logger.warning("Falha ao atualizar ranking de ativos: %s", exc)
            return

        if not ranking:
            logger.info("Ranking ativos: sem trades fechados para analise.")
            return

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
    # Startup reconciliation
    # ------------------------------------------------------------------

    def _reconcile_on_startup(
        self,
        cfg: LiveTradingConfig,
        exchange: BaseExchange,
        store: Any,
        target_contexts: set[tuple[str, str, str]],
    ) -> dict[tuple[str, str, str], LivePositionState]:
        """Restore open positions after crash/restart with DB/Binance checks."""
        restored: dict[tuple[str, str, str], LivePositionState] = {}
        target_symbols = {ctx[0] for ctx in target_contexts}

        def _normalize_context(state: LivePositionState) -> LivePositionState:
            timeframe = str(state.timeframe or "").strip() or cfg.timeframe
            strategy = str(state.strategy or "").strip() or cfg.strategy_name
            return LivePositionState(
                trade_id=state.trade_id,
                symbol=state.symbol,
                timeframe=timeframe,
                strategy=strategy,
                quantity=state.quantity,
                stake_amount=state.stake_amount,
                entry_price=state.entry_price,
                stop_loss=state.stop_loss,
                take_profit=state.take_profit,
                opened_at=state.opened_at,
                exchange_order_id=state.exchange_order_id,
            )

        saved_positions = self._store_load_positions(store)
        db_positions_by_trade = self._db_load_all_open_trade_states_with_retry(cfg=cfg)

        # Reconcile each saved position against DB and Binance.
        for saved in saved_positions.values():
            saved = _normalize_context(saved)
            if saved.context_key not in target_contexts:
                continue
            db_state = db_positions_by_trade.get(saved.trade_id)
            if db_state is not None and (not str(db_state.timeframe or "").strip()):
                db_state = LivePositionState(
                    trade_id=db_state.trade_id,
                    symbol=db_state.symbol,
                    timeframe=saved.timeframe,
                    strategy=str(db_state.strategy or "").strip() or saved.strategy,
                    quantity=db_state.quantity,
                    stake_amount=db_state.stake_amount,
                    entry_price=db_state.entry_price,
                    stop_loss=db_state.stop_loss,
                    take_profit=db_state.take_profit,
                    opened_at=db_state.opened_at,
                    exchange_order_id=db_state.exchange_order_id,
                )
                logger.warning(
                    "Reconciliacao: trade_id=%d com timeframe vazio no DB; usando timeframe salvo=%s.",
                    db_state.trade_id,
                    saved.timeframe,
                )
            if db_state is not None:
                db_state = _normalize_context(db_state)
            binance_exists = self._binance_has_position(
                exchange=exchange,
                symbol=saved.symbol,
                quantity_hint=saved.quantity,
            )

            if db_state is None and not binance_exists:
                logger.warning(
                    "Reconciliacao: removendo posicao orfa trade_id=%d (DB/Binance ausentes).",
                    saved.trade_id,
                )
                continue

            if db_state is not None and not binance_exists:
                restored[db_state.context_key] = db_state
                continue

            if db_state is None and binance_exists:
                restored[saved.context_key] = saved
                continue

            if db_state is not None:
                restored[db_state.context_key] = db_state

        # Include DB OPEN trades that were not in saved file.
        for db_state in db_positions_by_trade.values():
            db_state = _normalize_context(db_state)
            if db_state.symbol not in target_symbols:
                continue
            if db_state.strategy != cfg.strategy_name:
                continue
            if db_state.context_key not in target_contexts:
                continue
            restored.setdefault(db_state.context_key, db_state)

        # Keep only one position per context (latest trade_id wins).
        deduped: dict[tuple[str, str, str], LivePositionState] = {}
        for state in restored.values():
            existing = deduped.get(state.context_key)
            if existing is None or state.trade_id > existing.trade_id:
                deduped[state.context_key] = state

        # Fallback: recover Binance-held assets not tracked in DB/store.
        for symbol, strategy_name, timeframe in sorted(target_contexts):
            context_key = (symbol, strategy_name, timeframe)
            if context_key in deduped:
                continue

            asset_qty = self._fetch_total_asset_quantity(exchange, symbol)
            if asset_qty <= 0.0:
                continue

            try:
                ticker = exchange.fetch_ticker(symbol)
                market_price = float(ticker.get("last") or 0.0)
            except Exception as exc:
                logger.warning(
                    "Reconciliacao Binance orphan: falha ao obter ticker %s: %s",
                    symbol,
                    exc,
                )
                continue

            if market_price <= 0.0:
                continue

            notional = asset_qty * market_price
            min_notional = self._resolve_min_notional(exchange, symbol)
            if notional < min_notional:
                logger.warning(
                    "Reconciliacao Binance orphan: ignorando saldo poeira %s qty=%.8f notional=%.4f min_notional=%.4f",
                    symbol,
                    asset_qty,
                    notional,
                    min_notional,
                )
                continue

            stop_loss = market_price * 0.99
            take_profit = market_price * 1.02
            recovered_trade_id = self._db_create_trade(
                symbol=symbol,
                strategy_name=strategy_name,
                timeframe=timeframe,
                side="BUY",
                entry_price=market_price,
                quantity=asset_qty,
                stake_amount=notional,
                stop_loss=stop_loss,
                take_profit=take_profit,
                entry_time=datetime.now(tz=timezone.utc),
            )
            if recovered_trade_id is None:
                logger.warning(
                    "Reconciliacao Binance orphan: falha ao criar trade OPEN sintetico para %s",
                    symbol,
                )
                continue

            recovered_state = LivePositionState(
                trade_id=recovered_trade_id,
                symbol=symbol,
                timeframe=timeframe,
                strategy=strategy_name,
                quantity=asset_qty,
                stake_amount=notional,
                entry_price=market_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                opened_at=datetime.now(tz=timezone.utc).isoformat(),
                exchange_order_id="",
            )
            deduped[context_key] = recovered_state
            logger.warning(
                "Reconciliacao Binance orphan: posicao recuperada symbol=%s qty=%.8f "
                "price=%.6f trade_id=%d",
                symbol,
                asset_qty,
                market_price,
                recovered_trade_id,
            )

        logger.info("Reconciliacao: %d posicao(oes) restaurada(s).", len(deduped))
        return deduped

    # ------------------------------------------------------------------
    # Position entry
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
        """Create Trade and send BUY order with available-capital constraints."""
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

        # Idempotency: abort if Binance already has open orders for this symbol
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
                "Prosseguindo com entrada.", exc,
            )

        # Persist Trade BEFORE sending order (Order FK requires trade.id)
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
        fill_qty = float(
            result.order.filled_quantity
            if result.order.filled_quantity
            else result.risk_params.quantity
        )
        stake = float(result.risk_params.stake_amount)

        self._db_update_trade_after_buy(
            trade_id=trade_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            stake_amount=stake,
        )

        state = LivePositionState(
            trade_id=trade_id,
            symbol=cfg.symbol,
            timeframe=cfg.timeframe,
            strategy=cfg.strategy_name,
            quantity=fill_qty,
            stake_amount=stake,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            opened_at=datetime.now(tz=timezone.utc).isoformat(),
            exchange_order_id=str(result.order.exchange_order_id or ""),
        )

        logger.info(
            "Posicao aberta - trade_id=%d symbol=%s qty=%.8f "
            "entry=%.4f sl=%.4f tp=%.4f",
            trade_id, cfg.symbol, fill_qty, fill_price, stop_loss, take_profit,
        )
        return state

    # ------------------------------------------------------------------
    # Exit conditions
    # ------------------------------------------------------------------

    def _check_exit_conditions(
        self,
        position: LivePositionState,
        frame: pd.DataFrame,
        strategy: Any,
    ) -> tuple[bool, str]:
        """Return (should_exit, reason): SL, TP, or strategy exit signal."""
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
    # Position close
    # ------------------------------------------------------------------

    def _try_close_position(
        self,
        position: LivePositionState,
        close_price: float,
        reason: str,
        order_executor: OrderExecutor,
    ) -> bool:
        """Send SELL order and update DB. Returns True on success, False to retry."""
        trade_stub = _TradeStub(position.trade_id)
        sell_quantity = float(position.quantity)
        try:
            order = order_executor.execute_market_sell(
                trade=trade_stub,
                symbol=position.symbol,
                quantity=sell_quantity,
                price=close_price,
            )
        except ccxt.NetworkError as exc:
            logger.warning(
                "Falha de rede ao fechar posicao: %s. "
                "Mantendo posicao aberta para retry.", exc,
            )
            return False
        except ccxt.InsufficientFunds as exc:
            exchange = getattr(order_executor, "exchange", None)
            available_qty = self._fetch_free_asset_quantity(exchange, position.symbol)
            min_retry_qty = max(0.0, sell_quantity * 0.5)
            if available_qty > 0.0 and available_qty < sell_quantity and available_qty >= min_retry_qty:
                logger.warning(
                    "Saldo insuficiente ao fechar posicao: %s. "
                    "Tentando novamente com saldo livre real %.8f em vez de %.8f.",
                    exc,
                    available_qty,
                    sell_quantity,
                )
                try:
                    order = order_executor.execute_market_sell(
                        trade=trade_stub,
                        symbol=position.symbol,
                        quantity=available_qty,
                        price=close_price,
                    )
                except Exception as retry_exc:
                    logger.warning(
                        "Retry de fechamento com saldo livre real falhou: %s. "
                        "Mantendo posicao aberta.",
                        retry_exc,
                    )
                    return False
            else:
                logger.warning(
                    "Saldo insuficiente ao fechar posicao: %s. "
                    "Saldo livre atual %.8f insuficiente para retry. Mantendo posicao aberta.",
                    exc,
                    available_qty,
                )
                return False
        except (ccxt.InvalidOrder, ccxt.BadRequest) as exc:
            if self._is_notional_filter_error(exc):
                exchange = getattr(order_executor, "exchange", None)
                available_qty = self._fetch_free_asset_quantity(exchange, position.symbol)
                close_qty = available_qty if 0.0 < available_qty < sell_quantity else sell_quantity
                estimated_notional = max(0.0, close_qty * float(close_price))
                min_notional = self._resolve_min_notional(exchange, position.symbol)

                if estimated_notional < min_notional:
                    pnl = (float(close_price) - position.entry_price) * close_qty
                    pnl_pct = (
                        pnl / (position.entry_price * close_qty) * 100.0
                        if position.entry_price > 0 and close_qty > 0
                        else 0.0
                    )
                    self._db_close_trade(
                        trade_id=position.trade_id,
                        exit_price=float(close_price),
                        pnl=pnl,
                        pnl_pct=pnl_pct,
                        exit_reason=f"{reason}_dust_notional",
                        exit_time=datetime.now(tz=timezone.utc),
                    )
                    logger.warning(
                        "Fechamento local por NOTIONAL minimo: symbol=%s qty=%.8f notional=%.4f min_notional=%.4f. "
                        "Posicao removida para evitar loop de retry.",
                        position.symbol,
                        close_qty,
                        estimated_notional,
                        min_notional,
                    )
                    return True

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
            logger.error(
                "Erro inesperado ao fechar posicao: %s. "
                "Mantendo posicao aberta.", exc,
            )
            return False

        fill_price = float(order.price or close_price)
        fill_qty = float(order.filled_quantity or position.quantity)
        pnl = (fill_price - position.entry_price) * fill_qty
        pnl_pct = (
            pnl / (position.entry_price * fill_qty) * 100.0
            if position.entry_price > 0 else 0.0
        )

        self._db_close_trade(
            trade_id=position.trade_id,
            exit_price=fill_price,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            exit_time=datetime.now(tz=timezone.utc),
        )

        logger.info(
            "Posicao fechada - trade_id=%d symbol=%s reason=%s "
            "exit=%.4f pnl=%.4f (%.2f%%)",
            position.trade_id, position.symbol, reason,
            fill_price, pnl, pnl_pct,
        )
        return True

    # ------------------------------------------------------------------
    # DB helpers (raw SQL — avoids ORM schema mismatch)
    # Each method checks self._db_ops first so tests can inject fakes.
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
    ) -> None:
        override = self._db_ops.get("update_after_buy")
        if override:
            override(trade_id=trade_id, fill_price=fill_price,
                     fill_qty=fill_qty, stake_amount=stake_amount)
            return
        from sqlalchemy import text
        try:
            with get_session() as session:
                session.execute(
                    text(
                        "UPDATE trades SET entry_price=:entry_price, "
                        "quantity=:quantity, stake_amount=:stake_amount, "
                        "updated_at=NOW() WHERE id=:trade_id"
                    ),
                    {"entry_price": fill_price, "quantity": fill_qty,
                     "stake_amount": stake_amount, "trade_id": trade_id},
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
                    "stake_amount, stop_loss, take_profit, entry_time FROM trades "
                    "WHERE id=:trade_id AND status='OPEN'"
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
                    "stake_amount, stop_loss, take_profit, entry_time "
                    "FROM trades WHERE status='OPEN'"
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
        for state in loaded:
            existing = positions.get(state.context_key)
            if existing is None or state.trade_id > existing.trade_id:
                positions[state.context_key] = state
        return positions

    def _store_save_positions(
        self,
        store: Any,
        positions: dict[tuple[str, str, str], LivePositionState],
    ) -> None:
        states = list(positions.values())
        if hasattr(store, "save_all") and callable(store.save_all):
            store.save_all(states)
            return
        # Backward-compatible single-state store for tests.
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
            stake = float(state.stake_amount or 0.0)
            if stake <= 0.0:
                stake = float(state.quantity) * float(state.entry_price)
            committed += max(0.0, stake)
        return committed

    def _binance_has_position(
        self,
        exchange: BaseExchange,
        symbol: str,
        quantity_hint: float,
    ) -> bool:
        try:
            balance = exchange.fetch_balance()
        except Exception:
            return False

        if not isinstance(balance, dict):
            return False

        base_asset = str(symbol).split("/")[0].upper()
        qty_threshold = max(0.0, float(quantity_hint) * 0.25)

        by_asset = balance.get(base_asset)
        if isinstance(by_asset, dict):
            free_qty = float(by_asset.get("free", 0.0) or 0.0)
            used_qty = float(by_asset.get("used", 0.0) or 0.0)
            if (free_qty + used_qty) > qty_threshold:
                return True

        free_bucket = balance.get("free")
        used_bucket = balance.get("used")
        if isinstance(free_bucket, dict) or isinstance(used_bucket, dict):
            free_qty = float((free_bucket or {}).get(base_asset, 0.0) or 0.0)
            used_qty = float((used_bucket or {}).get(base_asset, 0.0) or 0.0)
            return (free_qty + used_qty) > qty_threshold

        return False

    # ------------------------------------------------------------------
    # Initialization
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
    # Signal helpers
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
