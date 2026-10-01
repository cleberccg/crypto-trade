"""
Downloader de dados históricos e em tempo real.

Decisão de projeto: DataDownloader é um serviço de alto nível que coordena o
cliente da exchange e o banco de dados. Gerencia a paginação de grandes
downloads históricos e evita inserções duplicadas por meio de CandleRepository.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

import pandas as pd

from database.connection import get_session
from database.models import Candle
from database.repositories import CandleRepository
from exchange.base_exchange import BaseExchange
from utils.helpers import datetime_to_timestamp_ms, normalize_ohlcv_dataframe
from utils.logger import get_logger
from utils.validators import validate_symbol, validate_timeframe

logger = get_logger(__name__)

# A Binance retorna no máximo 1.000 candles por solicitação
_MAX_CANDLES_PER_REQUEST = 1000


class DataDownloader:
    """
    Baixa dados OHLCV de uma exchange e os persiste no banco de dados.

    Uso::

        client = BinanceClient()
        client.connect()
        downloader = DataDownloader(client)
        df = downloader.download_historical("BTC/USDT", "1h", start, end)
    """

    def __init__(self, exchange: BaseExchange) -> None:
        self._exchange = exchange

    # ------------------------------------------------------------------
    # API publica
    # ------------------------------------------------------------------

    def download_historical(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """
        Baixa e persiste dados históricos OHLCV para um intervalo de datas.

        Faz a paginação automaticamente para contornar o limite da exchange por requisição.

        Argumentos:
            symbol: Par de negociação (por exemplo, ``BTC/USDT``).
            timeframe: Intervalo dos candles (por exemplo, ``1h``).
            start: Data/hora inicial inclusiva (UTC).
            end: Data/hora final inclusiva; por padrão, o momento atual.

        Retorno:
            DataFrame concatenado com todos os candles baixados.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)
        end = end or datetime.now(tz=timezone.utc)

        logger.info(
            "download_historical - symbol=%s tf=%s start=%s end=%s",
            symbol,
            timeframe,
            start.isoformat(),
            end.isoformat(),
        )

        frames: list[pd.DataFrame] = []
        total_inserted = 0

        for batch in self._paginate(symbol, timeframe, start, end):
            frames.append(batch)
            inserted = self._persist_batch(symbol, timeframe, batch)
            total_inserted += inserted

        if not frames:
            logger.warning("download_historical returned no data.")
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])

        result = pd.concat(frames).sort_index()
        result = result[~result.index.duplicated(keep="last")]

        logger.info(
            "download_historical complete - candles=%d new_rows=%d",
            len(result),
            total_inserted,
        )
        return result

    def get_latest_candles(
        self,
        symbol: str,
        timeframe: str,
        limit: int = 500,
    ) -> pd.DataFrame:
        """
        Busca os *limit* candles mais recentes sem persistí-los.

        Útil para gerar sinais de estratégia sem gravar no banco de dados.

        Argumentos:
            symbol: Par de negociação.
            timeframe: Intervalo dos candles.
            limit: Número de candles mais recentes.

        Retorno:
            DataFrame OHLCV.
        """
        symbol = validate_symbol(symbol)
        timeframe = validate_timeframe(timeframe)

        logger.info(
            "get_latest_candles - symbol=%s tf=%s limit=%d", symbol, timeframe, limit
        )
        return self._exchange.fetch_ohlcv(symbol, timeframe, limit=limit)

    # ------------------------------------------------------------------
    # Auxiliares privados
    # ------------------------------------------------------------------

    def _paginate(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Iterator[pd.DataFrame]:
        """
        Produz lotes de DataFrame por meio da paginação da API da exchange.

        Para quando o último timestamp do lote ultrapassar *end* ou quando a
        exchange retornar menos candles do que o solicitado (indicando que os
        dados se esgotaram).
        """
        since_ms = datetime_to_timestamp_ms(start)
        end_ms = datetime_to_timestamp_ms(end)

        while True:
            batch = self._exchange.fetch_ohlcv(
                symbol,
                timeframe,
                since=since_ms,
                limit=_MAX_CANDLES_PER_REQUEST,
            )

            if batch.empty:
                logger.debug("_paginate - empty batch, stopping.")
                break

            # Limita linhas que excedem o tempo final solicitado
            batch = batch[batch.index <= pd.Timestamp(end_ms, unit="ms", tz="UTC")]

            if batch.empty:
                break

            yield batch

            last_ts_ms = int(batch.index[-1].timestamp() * 1000)

            # Para quando atingir o limite final ou receber uma pagina parcial
            if last_ts_ms >= end_ms or len(batch) < _MAX_CANDLES_PER_REQUEST:
                break

            # Avanca o cursor apos o ultimo candle retornado
            since_ms = last_ts_ms + 1

    def _persist_batch(
        self, symbol: str, timeframe: str, df: pd.DataFrame
    ) -> int:
        """Converte um lote de DataFrame em objetos ORM e faz upsert deles."""
        candles = [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                open_time=row.name.to_pydatetime(),
                open=row["open"],
                high=row["high"],
                low=row["low"],
                close=row["close"],
                volume=row["volume"],
            )
            for _, row in df.iterrows()
        ]

        with get_session() as session:
            repo = CandleRepository(session)
            return repo.upsert_many(candles)
