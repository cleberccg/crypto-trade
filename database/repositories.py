"""
Repositórios de acesso a dados.

Decisão de projeto: o padrão Repository separa a lógica de consultas da lógica
de domínio. Cada repositório encapsula um único modelo e expõe métodos de
consulta tipados, mantendo o SQLAlchemy bruto isolado do restante da aplicação.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from database.models import Candle, Order, PortfolioSnapshot, Signal, Trade
from utils.logger import get_logger

logger = get_logger(__name__)


class CandleRepository:
    """Operações CRUD para registros Candle."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def upsert_many(self, candles: list[Candle]) -> int:
        """
        Insere candles que ainda não existem (sem duplicação).

        Retorna o número de linhas efetivamente inseridas.
        """
        if not candles:
            return 0

        # Os lotes do downloader são por símbolo/timeframe; busca os timestamps existentes
        # em uma consulta, em vez de executar uma consulta por candle.
        symbol = candles[0].symbol
        timeframe = candles[0].timeframe
        open_times = [candle.open_time for candle in candles]

        existing_rows = (
            self._session.query(Candle.open_time)
            .filter(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time.in_(open_times),
            )
            .all()
        )
        existing_times = {row[0] for row in existing_rows}

        to_insert = [candle for candle in candles if candle.open_time not in existing_times]
        if to_insert:
            self._session.add_all(to_insert)
        inserted = len(to_insert)
        self._session.flush()
        logger.debug("CandleRepository.upsert_many - inserted %d rows.", inserted)
        return inserted

    def get_range(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        """Retorna os candles no intervalo [start, end] para o ativo/período informado."""
        return (
            self._session.query(Candle)
            .filter(
                Candle.symbol == symbol,
                Candle.timeframe == timeframe,
                Candle.open_time >= start,
                Candle.open_time <= end,
            )
            .order_by(Candle.open_time)
            .all()
        )

    def get_latest(self, symbol: str, timeframe: str) -> Optional[Candle]:
        """Retorna o candle mais recente para o ativo/período informado."""
        return (
            self._session.query(Candle)
            .filter_by(symbol=symbol, timeframe=timeframe)
            .order_by(Candle.open_time.desc())
            .first()
        )


class TradeRepository:
    """Operações CRUD para registros Trade."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, trade: Trade) -> Trade:
        """Persiste uma nova operação e a retorna com seu ID gerado automaticamente."""
        self._session.add(trade)
        self._session.flush()
        logger.info("TradeRepository.create - trade id=%d symbol=%s", trade.id, trade.symbol)
        return trade

    def get_open_trades(self, symbol: Optional[str] = None) -> list[Trade]:
        """Retorna todas as operações com status OPEN, opcionalmente filtradas por ativo."""
        query = self._session.query(Trade).filter_by(status="OPEN")
        if symbol:
            query = query.filter_by(symbol=symbol)
        return query.all()

    def update(self, trade: Trade) -> Trade:
        """Mescla as alterações de volta à sessão."""
        self._session.merge(trade)
        self._session.flush()
        return trade

    def get_by_id(self, trade_id: int) -> Optional[Trade]:
        """Retorna uma operação pela chave primária."""
        return self._session.get(Trade, trade_id)

    def get_closed_trades(self, strategy_name: Optional[str] = None) -> list[Trade]:
        """Retorna todas as operações fechadas, opcionalmente filtradas por estratégia."""
        query = self._session.query(Trade).filter_by(status="CLOSED")
        if strategy_name:
            query = query.filter_by(strategy_name=strategy_name)
        return query.order_by(Trade.entry_time).all()


class SignalRepository:
    """Operações CRUD para registros Signal."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, signal: Signal) -> Signal:
        """Persiste um novo sinal."""
        self._session.add(signal)
        self._session.flush()
        return signal

    def get_recent(self, symbol: str, strategy_name: str, limit: int = 10) -> list[Signal]:
        """Retorna os sinais mais recentes para o ativo e a estratégia informados."""
        return (
            self._session.query(Signal)
            .filter_by(symbol=symbol, strategy_name=strategy_name)
            .order_by(Signal.timestamp.desc())
            .limit(limit)
            .all()
        )


class PortfolioSnapshotRepository:
    """Operações CRUD para registros PortfolioSnapshot."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def create(self, snapshot: PortfolioSnapshot) -> PortfolioSnapshot:
        """Persiste um novo registro pontual da carteira."""
        self._session.add(snapshot)
        self._session.flush()
        return snapshot

    def get_range(self, source: str, start: datetime, end: datetime) -> list[PortfolioSnapshot]:
        """Retorna os registros pontuais no intervalo [start, end] para a origem informada."""
        return (
            self._session.query(PortfolioSnapshot)
            .filter(
                PortfolioSnapshot.source == source,
                PortfolioSnapshot.timestamp >= start,
                PortfolioSnapshot.timestamp <= end,
            )
            .order_by(PortfolioSnapshot.timestamp)
            .all()
        )
