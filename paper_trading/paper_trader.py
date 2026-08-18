"""
Paper Trader - orchestrates strategy signals and paper execution.

Design decision: operation persistence is based on history tables
(trade_history/signal_snapshots), which are stable in production schema.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import pandas as pd

from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.models import PortfolioSnapshot
from database.repositories import PortfolioSnapshotRepository
from paper_trading.paper_broker import PaperBroker
from risk.portfolio_value_provider import PaperPortfolioValueProvider, PortfolioValueProvider
from risk.risk_manager import RiskManager
from scientific_data_warehouse import build_entry_snapshot
from scientific_data_warehouse import build_exit_snapshot
from strategies.base_strategy import BaseStrategy, SignalType
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class OpenPosition:
    symbol: str
    entry_price: float
    quantity: float
    stake_amount: float
    stop_loss: float | None
    take_profit: float | None
    trailing_stop: float | None
    max_holding_minutes: int | None
    risk_reward: float | None
    score: float | None
    entry_time: datetime
    scientific_snapshot_id: int | None = None


class PaperTrader:
    """Runs a strategy in paper trading mode."""

    def __init__(
        self,
        strategy: BaseStrategy,
        broker: PaperBroker | None = None,
        risk_manager: RiskManager | None = None,
        portfolio_value_provider: PortfolioValueProvider | None = None,
        execution_id: str | None = None,
        timeframe: str | None = None,
        strategy_version: str = "v1.0",
        campaign_id: str | None = None,
    ) -> None:
        self._strategy = strategy
        self._broker = broker or PaperBroker()
        self._risk_manager = risk_manager or RiskManager()
        self._portfolio_value_provider = (
            portfolio_value_provider or PaperPortfolioValueProvider(self._broker)
        )
        self._open_trade: OpenPosition | None = None
        self._highest_price_since_entry: float = 0.0
        self._lowest_price_since_entry: float = 0.0
        self._execution_id = execution_id or str(uuid4())
        self._timeframe = timeframe
        self._strategy_version = str(strategy_version)
        self._campaign_id = str(campaign_id) if campaign_id else None
        self._versioned_strategy_name = f"{self._strategy.name}@{self._strategy_version}"
        self._stats: dict[str, float | int] = {
            "entries": 0,
            "closed_trades": 0,
            "rejected_entries": 0,
            "cancelled_orders": 0,
            "net_profit": 0.0,
        }

    @property
    def versioned_strategy_name(self) -> str:
        return self._versioned_strategy_name

    def restore_open_trade(self, symbol: str) -> bool:
        """Deprecated DB restore path. Runtime state restore is used in paper-live."""
        logger.info("PaperTrader restore_open_trade skipped for symbol=%s (state-file resume mode)", symbol)
        return False

    def export_runtime_state(self) -> dict[str, Any]:
        if self._open_trade is None:
            return {
                "open_trade": None,
                "highest_price_since_entry": self._highest_price_since_entry,
                "broker": self._broker.export_runtime_state(),
            }
        return {
            "open_trade": {
                "symbol": self._open_trade.symbol,
                "entry_price": self._open_trade.entry_price,
                "quantity": self._open_trade.quantity,
                "stake_amount": self._open_trade.stake_amount,
                "stop_loss": self._open_trade.stop_loss,
                "take_profit": self._open_trade.take_profit,
                "trailing_stop": self._open_trade.trailing_stop,
                "max_holding_minutes": self._open_trade.max_holding_minutes,
                "risk_reward": self._open_trade.risk_reward,
                "score": self._open_trade.score,
                "entry_time": self._open_trade.entry_time.isoformat(),
                "scientific_snapshot_id": self._open_trade.scientific_snapshot_id,
            },
            "highest_price_since_entry": self._highest_price_since_entry,
            "lowest_price_since_entry": self._lowest_price_since_entry,
            "broker": self._broker.export_runtime_state(),
        }

    def import_runtime_state(self, state: dict[str, Any] | None) -> None:
        if not state:
            return
        self._broker.import_runtime_state(state.get("broker") if isinstance(state, dict) else None)

        open_trade = state.get("open_trade")
        if not open_trade:
            self._open_trade = None
            self._highest_price_since_entry = float(state.get("highest_price_since_entry") or 0.0)
            self._lowest_price_since_entry = float(state.get("lowest_price_since_entry") or 0.0)
            return

        entry_time_raw = str(open_trade.get("entry_time"))
        entry_time = datetime.fromisoformat(entry_time_raw.replace("Z", "+00:00"))
        self._open_trade = OpenPosition(
            symbol=str(open_trade.get("symbol")),
            entry_price=float(open_trade.get("entry_price")),
            quantity=float(open_trade.get("quantity")),
            stake_amount=float(open_trade.get("stake_amount")),
            stop_loss=float(open_trade.get("stop_loss")) if open_trade.get("stop_loss") is not None else None,
            take_profit=float(open_trade.get("take_profit")) if open_trade.get("take_profit") is not None else None,
            trailing_stop=float(open_trade.get("trailing_stop")) if open_trade.get("trailing_stop") is not None else None,
            max_holding_minutes=int(open_trade.get("max_holding_minutes")) if open_trade.get("max_holding_minutes") is not None else None,
            risk_reward=float(open_trade.get("risk_reward")) if open_trade.get("risk_reward") is not None else None,
            score=float(open_trade.get("score")) if open_trade.get("score") is not None else None,
            entry_time=entry_time,
            scientific_snapshot_id=int(open_trade.get("scientific_snapshot_id")) if open_trade.get("scientific_snapshot_id") is not None else None,
        )
        self._highest_price_since_entry = float(state.get("highest_price_since_entry") or self._open_trade.entry_price)
        self._lowest_price_since_entry = float(state.get("lowest_price_since_entry") or self._open_trade.entry_price)

        # Guard against stale or partial state where open_trade exists but broker has no base asset.
        available = self._broker.get_position_quantity(self._open_trade.symbol)
        if available + 1e-12 < float(self._open_trade.quantity):
            logger.error(
                "PaperTrader runtime state desync detected on import: symbol=%s required=%.6f available=%.6f. "
                "Open trade will be dropped to preserve runtime consistency.",
                self._open_trade.symbol,
                self._open_trade.quantity,
                available,
            )
            self._stats["cancelled_orders"] = int(self._stats["cancelled_orders"]) + 1
            self._open_trade = None
            self._highest_price_since_entry = 0.0

    def run(self, df: pd.DataFrame, symbol: str, timeframe: str | None = None) -> dict[str, float | int]:
        logger.info(
            "PaperTrader.run - strategy=%s symbol=%s bars=%d",
            self._strategy.name,
            symbol,
            len(df),
        )
        if timeframe:
            self._timeframe = timeframe

        for i in range(50, len(df)):
            window = df.iloc[: i + 1]
            self.on_bar(window, symbol)

        logger.info("PaperTrader.run complete.")
        return dict(self._stats)

    def on_bar(self, df: pd.DataFrame, symbol: str) -> None:
        enriched = self._strategy.calculate(df)
        last = enriched.iloc[-1]
        current_price = float(last["close"])
        timestamp: datetime = last.name.to_pydatetime()  # type: ignore[union-attr]

        if self._open_trade is not None:
            self._highest_price_since_entry = max(self._highest_price_since_entry, current_price)
            self._lowest_price_since_entry = min(self._lowest_price_since_entry, current_price)

            stop_hit = current_price <= (self._open_trade.stop_loss or 0.0)
            tp_hit = current_price >= (self._open_trade.take_profit or float("inf"))
            trailing_hit = (
                self._open_trade.trailing_stop is not None
                and self._risk_manager.check_trailing_stop(
                    self._open_trade.entry_price,
                    current_price,
                    self._highest_price_since_entry,
                    self._open_trade.trailing_stop,
                )
            )
            time_stop_hit = False
            if self._open_trade.max_holding_minutes is not None and self._open_trade.max_holding_minutes > 0:
                elapsed_minutes = max(0.0, (timestamp - self._open_trade.entry_time).total_seconds() / 60.0)
                time_stop_hit = elapsed_minutes >= float(self._open_trade.max_holding_minutes)
            exit_sig = self._strategy.exit_signal(enriched, self._open_trade.entry_price)

            exit_reason: str | None = None
            if stop_hit:
                exit_reason = "stop_loss"
            elif tp_hit:
                exit_reason = "take_profit"
            elif trailing_hit:
                exit_reason = "trailing_stop"
            elif time_stop_hit:
                exit_reason = "time_stop"
            elif exit_sig.signal == SignalType.SELL:
                exit_reason = exit_sig.metadata.get("exit_reason", "strategy_exit")

            if exit_reason:
                self._close_trade(symbol, current_price, timestamp, exit_reason)

        if self._open_trade is None:
            entry_sig = self._strategy.entry_signal(enriched)
            if entry_sig.signal == SignalType.BUY:
                self._open_trade_action(symbol, current_price, timestamp, entry_sig, enriched)

        self._save_portfolio_snapshot(symbol, current_price, timestamp)

    def _open_trade_action(self, symbol: str, price: float, timestamp: datetime, signal, frame: pd.DataFrame) -> None:
        portfolio_value = float(self._portfolio_value_provider.get_available_portfolio_value())
        try:
            strategy_rr_min = RiskManager.resolve_min_risk_reward_ratio(self._strategy)
            if strategy_rr_min is None:
                strategy_rr_min = RiskManager.infer_min_risk_reward_ratio_from_levels(
                    entry_price=price,
                    stop_loss=signal.stop_loss,
                    take_profit=signal.take_profit,
                )
            risk_params = self._risk_manager.evaluate_trade(
                portfolio_value=portfolio_value,
                entry_price=price,
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                trailing_stop_pct=signal.trailing_stop_pct,
                strategy_score=signal.score,
                min_risk_reward_ratio=strategy_rr_min,
            )
        except ValueError as exc:
            self._stats["rejected_entries"] = int(self._stats["rejected_entries"]) + 1
            self._save_entry_signal(
                symbol=symbol,
                timestamp=timestamp,
                signal=signal,
                frame=frame,
                accepted=False,
                rejection_reason=str(exc),
                rr=None,
            )
            logger.warning("Trade rejected by risk manager: %s", exc)
            return

        self._broker.create_market_buy(symbol, risk_params.quantity, price)

        self._open_trade = OpenPosition(
            symbol=symbol,
            entry_price=price,
            quantity=risk_params.quantity,
            stake_amount=risk_params.stake_amount,
            stop_loss=risk_params.stop_loss,
            take_profit=risk_params.take_profit,
            trailing_stop=risk_params.trailing_stop_pct,
            max_holding_minutes=(
                int(signal.metadata.get("max_holding_minutes"))
                if hasattr(signal, "metadata")
                and isinstance(signal.metadata, dict)
                and signal.metadata.get("max_holding_minutes") is not None
                else None
            ),
            risk_reward=risk_params.risk_reward_ratio,
            score=float(signal.score or 0.0),
            entry_time=timestamp,
        )

        scientific_snapshot_id = self._save_entry_signal(
            symbol=symbol,
            timestamp=timestamp,
            signal=signal,
            frame=frame,
            accepted=True,
            rejection_reason=None,
            rr=risk_params.risk_reward_ratio,
        )
        if self._open_trade is not None:
            self._open_trade.scientific_snapshot_id = scientific_snapshot_id

        self._stats["entries"] = int(self._stats["entries"]) + 1
        self._highest_price_since_entry = price
        self._lowest_price_since_entry = price

    def _close_trade(self, symbol: str, price: float, timestamp: datetime, reason: str) -> None:
        if self._open_trade is None:
            return

        try:
            self._broker.create_market_sell(symbol, self._open_trade.quantity, price)
        except ValueError as exc:
            # Keep process alive in long campaigns when persisted state is inconsistent.
            logger.error(
                "PaperTrader close desync: symbol=%s qty=%.6f reason=%s error=%s",
                symbol,
                self._open_trade.quantity,
                reason,
                exc,
            )
            self._stats["cancelled_orders"] = int(self._stats["cancelled_orders"]) + 1
            self._open_trade = None
            self._highest_price_since_entry = 0.0
            return

        pnl = (price - self._open_trade.entry_price) * self._open_trade.quantity
        pnl_pct = pnl / self._open_trade.stake_amount
        duration_minutes = max(0.0, (timestamp - self._open_trade.entry_time).total_seconds() / 60.0)

        self._persist_trade_history(
            symbol=symbol,
            entry_price=self._open_trade.entry_price,
            exit_price=price,
            stop_loss=self._open_trade.stop_loss,
            take_profit=self._open_trade.take_profit,
            risk_reward=self._open_trade.risk_reward,
            quantity=self._open_trade.quantity,
            pnl=pnl,
            pnl_pct=pnl_pct,
            entry_time=self._open_trade.entry_time,
            exit_time=timestamp,
            duration_minutes=duration_minutes,
            exit_reason=reason,
            score=self._open_trade.score,
            scientific_snapshot_id=self._open_trade.scientific_snapshot_id,
        )

        self._stats["closed_trades"] = int(self._stats["closed_trades"]) + 1
        self._stats["net_profit"] = float(self._stats["net_profit"]) + float(pnl)
        self._open_trade = None
        self._highest_price_since_entry = 0.0
        self._lowest_price_since_entry = 0.0

    def _save_entry_signal(
        self,
        symbol: str,
        timestamp: datetime,
        signal,
        frame: pd.DataFrame,
        accepted: bool,
        rejection_reason: str | None,
        rr: float | None,
    ) -> int | None:
        with get_session() as session:
            history = HistoryPersistenceService(session)
            scientific_entry = build_entry_snapshot(
                frame=frame,
                symbol=symbol,
                timeframe=self._timeframe or "unknown",
                execution_id=self._execution_id,
                strategy_name=self._strategy.name,
                strategy_version=self._strategy_version,
                strategy_key=self._versioned_strategy_name,
                campaign_id=self._campaign_id,
                signal=signal,
                accepted=accepted,
                rejection_reason=rejection_reason,
                risk_reward=rr,
            )
            signal_row = history.save_signal_snapshot(
                execution_id=self._execution_id,
                strategy=self._versioned_strategy_name,
                symbol=symbol,
                timeframe=self._timeframe or "unknown",
                timestamp=timestamp,
                signal=signal.signal.value,
                score=float(signal.score or 0.0),
                entry_price=float(signal.price),
                stop_loss=signal.stop_loss,
                take_profit=signal.take_profit,
                rr=rr,
                accepted=accepted,
                rejection_reason=rejection_reason,
                market_regime=scientific_entry.signal_market_regime,
                indicator_payload=scientific_entry.signal_indicator_payload,
            )
            snapshot_row = scientific_entry.warehouse_row
            snapshot_row.signal_snapshot_id = int(signal_row.id)
            history.save_scientific_trade_snapshot(snapshot_row)
            return int(snapshot_row.id)

    def _persist_trade_history(
        self,
        *,
        symbol: str,
        entry_price: float,
        exit_price: float,
        stop_loss: float | None,
        take_profit: float | None,
        risk_reward: float | None,
        quantity: float,
        pnl: float,
        pnl_pct: float,
        entry_time: datetime,
        exit_time: datetime,
        duration_minutes: float,
        exit_reason: str,
        score: float | None,
        scientific_snapshot_id: int | None,
    ) -> None:
        trade_payload = {
            "side": "BUY",
            "entry_time": entry_time,
            "exit_time": exit_time,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "risk_reward": risk_reward,
            "quantity": quantity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "duration_minutes": duration_minutes,
            "exit_reason": exit_reason,
            "score": score,
        }
        with get_session() as session:
            history = HistoryPersistenceService(session)
            trade_row = history.save_trade_row(
                execution_id=self._execution_id,
                strategy=self._versioned_strategy_name,
                symbol=symbol,
                timeframe=self._timeframe or "unknown",
                trade=trade_payload,
            )
            if scientific_snapshot_id is not None:
                exit_snapshot = build_exit_snapshot(
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    exit_price=exit_price,
                    exit_reason=exit_reason,
                    exit_time=exit_time,
                    duration_minutes=duration_minutes,
                    pnl=pnl,
                    pnl_pct=pnl_pct,
                    mfe=((self._highest_price_since_entry - entry_price) / entry_price) if entry_price > 0 else None,
                    mae=((self._lowest_price_since_entry - entry_price) / entry_price) if entry_price > 0 else None,
                )
                history.update_scientific_trade_snapshot_exit(
                    snapshot_id=scientific_snapshot_id,
                    trade_history_id=int(trade_row.id),
                    exit_snapshot=exit_snapshot,
                    snapshot_complete=True,
                    missing_fields=[],
                )

    def _save_portfolio_snapshot(self, symbol: str, current_price: float, timestamp: datetime) -> None:
        base_asset = symbol.split("/")[0]
        prices = {base_asset: current_price}
        total = self._broker.get_portfolio_value(prices)
        cash = self._broker.get_balance().cash
        positions_value = total - cash
        open_trades = 1 if self._open_trade is not None else 0

        snapshot = PortfolioSnapshot(
            timestamp=timestamp,
            total_value=total,
            cash=cash,
            positions_value=positions_value,
            open_trades=open_trades,
            source="paper",
        )

        with get_session() as session:
            PortfolioSnapshotRepository(session).create(snapshot)
