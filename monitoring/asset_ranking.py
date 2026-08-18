from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from sqlalchemy import text

from config.settings import settings
from database.connection import get_session


@dataclass(frozen=True)
class AssetRankingRow:
    symbol: str
    trades: int
    wins: int
    losses: int
    win_rate_pct: float
    profit_factor: float
    net_profit: float
    max_drawdown: float
    expectancy: float
    status: str


class AssetRankingMonitor:
    """Builds per-symbol operational ranking from closed trades."""

    def __init__(
        self,
        session_factory: Callable[..., Any] | None = None,
        min_trades_per_symbol: int | None = None,
    ) -> None:
        self._session_factory = session_factory or get_session
        self._min_trades = int(min_trades_per_symbol or settings.trading.min_trades_per_symbol)

    def snapshot(self, strategy_name: str | None = None) -> list[AssetRankingRow]:
        rows = self._load_closed_trades(strategy_name=strategy_name)
        by_symbol: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            symbol = str(row["symbol"] or "").strip().upper()
            if not symbol:
                continue
            by_symbol.setdefault(symbol, []).append(row)

        ranking: list[AssetRankingRow] = []
        for symbol, trades in by_symbol.items():
            ordered = sorted(trades, key=lambda item: item["exit_time"])
            pnl_values = [float(item["pnl"]) for item in ordered]
            wins = sum(1 for pnl in pnl_values if pnl > 0.0)
            losses = sum(1 for pnl in pnl_values if pnl <= 0.0)
            trades_count = len(pnl_values)
            gross_profit = sum(pnl for pnl in pnl_values if pnl > 0.0)
            gross_loss = abs(sum(pnl for pnl in pnl_values if pnl < 0.0))
            net_profit = sum(pnl_values)
            avg_win = (gross_profit / wins) if wins > 0 else 0.0
            avg_loss = (abs(sum(pnl for pnl in pnl_values if pnl <= 0.0)) / losses) if losses > 0 else 0.0
            win_rate = (wins / trades_count * 100.0) if trades_count > 0 else 0.0
            profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
            expectancy = ((wins / trades_count) * avg_win - (losses / trades_count) * avg_loss) if trades_count > 0 else 0.0

            cumulative = 0.0
            peak = 0.0
            max_drawdown = 0.0
            for pnl in pnl_values:
                cumulative += pnl
                if cumulative > peak:
                    peak = cumulative
                drawdown = peak - cumulative
                if drawdown > max_drawdown:
                    max_drawdown = drawdown

            status = self._classify_status(profit_factor=profit_factor, trades=trades_count)
            ranking.append(
                AssetRankingRow(
                    symbol=symbol,
                    trades=trades_count,
                    wins=wins,
                    losses=losses,
                    win_rate_pct=win_rate,
                    profit_factor=profit_factor,
                    net_profit=net_profit,
                    max_drawdown=max_drawdown,
                    expectancy=expectancy,
                    status=status,
                )
            )

        ranking.sort(key=lambda item: (item.profit_factor, item.net_profit), reverse=True)
        return ranking

    def _classify_status(self, profit_factor: float, trades: int) -> str:
        if trades < self._min_trades:
            return "OBSERVACAO"
        if profit_factor > 1.50:
            return "EXCELENTE"
        if profit_factor >= 1.20:
            return "BOM"
        if profit_factor >= 0.90:
            return "OBSERVACAO"
        return "SUSPENDER"

    def _load_closed_trades(self, strategy_name: str | None = None) -> list[dict[str, Any]]:
        query = (
            "SELECT symbol, exit_time, pnl FROM trades "
            "WHERE status='CLOSED'"
        )
        params: dict[str, Any] = {}
        if strategy_name:
            query += " AND strategy_name=:strategy_name"
            params["strategy_name"] = strategy_name
        query += " ORDER BY symbol ASC, exit_time ASC, id ASC"

        with self._session_factory() as session:
            rows = session.execute(text(query), params).fetchall()

        normalized: list[dict[str, Any]] = []
        for row in rows:
            raw_exit = row[1]
            if hasattr(raw_exit, "timestamp"):
                exit_time = raw_exit
            else:
                exit_time = datetime.min
            normalized.append(
                {
                    "symbol": str(row[0] or ""),
                    "exit_time": exit_time,
                    "pnl": float(row[2] or 0.0),
                }
            )
        return normalized
