from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import text

from database.connection import get_session
from database.repositories import CandleRepository
from exchange.binance_market_data_client import BinanceMarketDataClient
from exchange.data_downloader import DataDownloader
from utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_SYMBOLS: tuple[str, ...] = ("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
DEFAULT_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "1h")

_TIMEFRAME_STEP = {
    "1m": timedelta(minutes=1),
    "3m": timedelta(minutes=3),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "6h": timedelta(hours=6),
    "8h": timedelta(hours=8),
    "12h": timedelta(hours=12),
    "1d": timedelta(days=1),
}


@dataclass(frozen=True)
class MarketDataDaemonConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    timeframes: tuple[str, ...] = DEFAULT_TIMEFRAMES
    polling_interval_seconds: float = 30.0
    context_delay_seconds: float = 0.2
    batch_size: int = 1000
    retry_count: int = 5
    retry_delay_seconds: float = 2.0
    bootstrap_days: int = 7
    recent_gap_bars: int = 2000
    report_every_cycles: int = 1
    output_prefix: str = "market_data_daemon"
    max_cycles: int = 0


class MarketDataDaemonService:
    def __init__(
        self,
        base_dir: Path,
        *,
        now_fn: Callable[[], datetime] | None = None,
        sleep_fn: Callable[[float], None] | None = None,
    ) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)
        self._now_fn = now_fn or (lambda: datetime.now(tz=timezone.utc))
        self._sleep = sleep_fn or time.sleep

        self._state: dict[tuple[str, str], dict[str, Any]] = {}
        self._client: BinanceMarketDataClient | None = None
        self._downloader: DataDownloader | None = None

    def run(self, cfg: MarketDataDaemonConfig) -> dict[str, Any]:
        contexts = [(symbol, tf) for symbol in cfg.symbols for tf in cfg.timeframes]
        if not contexts:
            raise RuntimeError("MarketDataDaemon requires at least one symbol/timeframe context.")

        self._client = BinanceMarketDataClient()
        self._client.connect()
        self._downloader = DataDownloader(self._client)

        cycle = 0
        last_outputs: dict[str, str] = {}
        started_at = self._now_fn()

        try:
            while True:
                cycle += 1
                cycle_started = self._now_fn()
                logger.info("market-data-daemon cycle=%d contexts=%d", cycle, len(contexts))

                for index, (symbol, timeframe) in enumerate(contexts):
                    self._ensure_context_state(symbol, timeframe)
                    self._sync_context(symbol, timeframe, cfg)
                    if index < len(contexts) - 1:
                        self._sleep(max(0.0, float(cfg.context_delay_seconds)))

                if cycle % max(1, int(cfg.report_every_cycles)) == 0:
                    report = self._build_report(cfg, cycle, started_at, cycle_started)
                    last_outputs = self._write_report(report, cfg.output_prefix)

                if int(cfg.max_cycles) > 0 and cycle >= int(cfg.max_cycles):
                    break

                self._sleep(max(0.1, float(cfg.polling_interval_seconds)))

        except KeyboardInterrupt:
            logger.warning("market-data-daemon interrupted by user")
        finally:
            if self._client is not None:
                self._client.disconnect()

        final_report = self._build_report(cfg, cycle, started_at, self._now_fn())
        if not last_outputs:
            last_outputs = self._write_report(final_report, cfg.output_prefix)

        return {
            "summary": final_report["summary"],
            "report": final_report,
            "outputs": last_outputs,
        }

    def _ensure_context_state(self, symbol: str, timeframe: str) -> None:
        key = (symbol, timeframe)
        if key in self._state:
            return
        self._state[key] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "candles_inserted": 0,
            "gaps_filled": 0,
            "gap_ranges_filled": 0,
            "retries": 0,
            "failures": 0,
            "last_error": None,
            "last_execution": None,
            "last_candle_persisted": None,
            "last_cycle_inserted": 0,
            "last_cycle_gap_inserted": 0,
            "last_cycle_retries": 0,
        }

    def _sync_context(self, symbol: str, timeframe: str, cfg: MarketDataDaemonConfig) -> None:
        key = (symbol, timeframe)
        ctx = self._state[key]
        now = self._now_fn()
        step = self._step_for_timeframe(timeframe)

        latest_before = self._get_latest_candle_time(symbol, timeframe)
        total_before = self._count_candles(symbol, timeframe)

        retries_before = int(ctx["retries"])
        gap_ranges = self._detect_recent_gap_ranges(symbol, timeframe, max(10, int(cfg.recent_gap_bars)))
        gap_inserted = 0
        gap_ranges_filled = 0

        for gap_start, gap_end in gap_ranges:
            if gap_start > gap_end:
                continue
            self._download_with_retry(symbol, timeframe, gap_start, gap_end, cfg)
            gap_ranges_filled += 1

        if latest_before is None:
            incremental_start = now - timedelta(days=max(1, int(cfg.bootstrap_days)))
        else:
            incremental_start = latest_before + step

        if incremental_start <= now:
            self._download_with_retry(symbol, timeframe, incremental_start, now, cfg)

        total_after = self._count_candles(symbol, timeframe)
        latest_after = self._get_latest_candle_time(symbol, timeframe)

        inserted_total = max(0, total_after - total_before)
        if gap_ranges_filled > 0:
            # Approximation by observed delta in this cycle.
            gap_inserted = inserted_total

        ctx["candles_inserted"] = int(ctx["candles_inserted"]) + int(inserted_total)
        ctx["gaps_filled"] = int(ctx["gaps_filled"]) + int(gap_inserted)
        ctx["gap_ranges_filled"] = int(ctx["gap_ranges_filled"]) + int(gap_ranges_filled)
        ctx["last_execution"] = now.isoformat()
        ctx["last_candle_persisted"] = latest_after.isoformat() if latest_after else None
        ctx["last_cycle_inserted"] = int(inserted_total)
        ctx["last_cycle_gap_inserted"] = int(gap_inserted)
        ctx["last_cycle_retries"] = max(0, int(ctx["retries"]) - retries_before)
        ctx["last_error"] = None

    def _download_with_retry(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        cfg: MarketDataDaemonConfig,
    ) -> None:
        if self._downloader is None or self._client is None:
            raise RuntimeError("MarketDataDaemon downloader/client not initialized")

        key = (symbol, timeframe)
        ctx = self._state[key]
        max_attempts = max(1, int(cfg.retry_count) + 1)

        for attempt in range(1, max_attempts + 1):
            try:
                self._downloader.download_historical(symbol, timeframe, start, end)
                return
            except Exception as exc:
                ctx["failures"] = int(ctx["failures"]) + 1
                ctx["last_error"] = str(exc)
                if attempt >= max_attempts:
                    logger.exception(
                        "market-data-daemon download exhausted retries symbol=%s tf=%s", symbol, timeframe
                    )
                    return

                ctx["retries"] = int(ctx["retries"]) + 1
                delay = max(0.1, float(cfg.retry_delay_seconds)) * (2 ** (attempt - 1))
                logger.warning(
                    "market-data-daemon retry symbol=%s tf=%s attempt=%d/%d delay=%.2fs error=%s",
                    symbol,
                    timeframe,
                    attempt,
                    max_attempts,
                    delay,
                    exc,
                )
                self._safe_reconnect_client()
                self._sleep(delay)

    def _safe_reconnect_client(self) -> None:
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception:
            logger.debug("market-data-daemon reconnect: disconnect failed", exc_info=True)
        try:
            self._client.connect()
        except Exception:
            logger.debug("market-data-daemon reconnect: connect failed", exc_info=True)

    def _build_report(
        self,
        cfg: MarketDataDaemonConfig,
        cycle: int,
        started_at: datetime,
        cycle_started_at: datetime,
    ) -> dict[str, Any]:
        now = self._now_fn()
        contexts: list[dict[str, Any]] = []

        for (symbol, timeframe), ctx in sorted(self._state.items(), key=lambda x: x[0]):
            last_candle = self._parse_dt(ctx.get("last_candle_persisted"))
            lag_minutes = None
            if last_candle is not None:
                lag_minutes = max(0, int((now - last_candle).total_seconds() // 60))

            contexts.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "last_candle_persisted": ctx.get("last_candle_persisted"),
                    "lag_minutes": lag_minutes,
                    "candles_inserted": int(ctx.get("candles_inserted") or 0),
                    "last_cycle_inserted": int(ctx.get("last_cycle_inserted") or 0),
                    "gaps_filled": int(ctx.get("gaps_filled") or 0),
                    "gap_ranges_filled": int(ctx.get("gap_ranges_filled") or 0),
                    "last_execution": ctx.get("last_execution"),
                    "failures": int(ctx.get("failures") or 0),
                    "retries": int(ctx.get("retries") or 0),
                    "last_cycle_retries": int(ctx.get("last_cycle_retries") or 0),
                    "last_error": ctx.get("last_error"),
                }
            )

        total_inserted = sum(int(row["last_cycle_inserted"]) for row in contexts)
        total_failures = sum(int(row["failures"]) for row in contexts)
        total_retries = sum(int(row["last_cycle_retries"]) for row in contexts)

        return {
            "generated_at": now.isoformat(),
            "phase": "MARKET_DATA_DAEMON",
            "config": {
                **asdict(cfg),
                "symbols": list(cfg.symbols),
                "timeframes": list(cfg.timeframes),
            },
            "summary": {
                "cycle": int(cycle),
                "contexts": len(contexts),
                "started_at": started_at.isoformat(),
                "cycle_started_at": cycle_started_at.isoformat(),
                "last_execution": now.isoformat(),
                "total_inserted_last_cycle": int(total_inserted),
                "total_failures": int(total_failures),
                "total_retries_last_cycle": int(total_retries),
            },
            "contexts": contexts,
        }

    def _write_report(self, report: dict[str, Any], output_prefix: str) -> dict[str, str]:
        json_path = self._results_dir / f"{output_prefix}_latest.json"
        md_path = self._results_dir / f"{output_prefix}_latest.md"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")

        return {"json": str(json_path), "md": str(md_path)}

    def _to_markdown(self, report: dict[str, Any]) -> str:
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        lines = [
            "# Market Data Daemon",
            "",
            f"- Cycle: {summary.get('cycle')}",
            f"- Contexts: {summary.get('contexts')}",
            f"- Last execution: {summary.get('last_execution')}",
            f"- Inserted (last cycle): {summary.get('total_inserted_last_cycle')}",
            f"- Failures: {summary.get('total_failures')}",
            f"- Retries (last cycle): {summary.get('total_retries_last_cycle')}",
            "",
            "## Contexts",
        ]

        for row in report.get("contexts", []):
            if not isinstance(row, dict):
                continue
            lines.append(
                "- "
                f"{row.get('symbol')} {row.get('timeframe')}: "
                f"last_candle={row.get('last_candle_persisted')} "
                f"lag_minutes={row.get('lag_minutes')} "
                f"inserted={row.get('last_cycle_inserted')} "
                f"gaps={row.get('gap_ranges_filled')} "
                f"retries={row.get('last_cycle_retries')} "
                f"failures={row.get('failures')}"
            )
        return "\n".join(lines) + "\n"

    def _detect_recent_gap_ranges(
        self,
        symbol: str,
        timeframe: str,
        recent_gap_bars: int,
    ) -> list[tuple[datetime, datetime]]:
        step = self._step_for_timeframe(timeframe)
        timestamps = self._fetch_recent_open_times(symbol, timeframe, recent_gap_bars)
        if len(timestamps) < 2:
            return []

        timestamps = sorted(timestamps)
        ranges: list[tuple[datetime, datetime]] = []
        for prev, curr in zip(timestamps[:-1], timestamps[1:]):
            if curr - prev > step:
                ranges.append((prev + step, curr - step))
        return ranges

    def _fetch_recent_open_times(self, symbol: str, timeframe: str, limit: int) -> list[datetime]:
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT open_time
                    FROM candles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                    ORDER BY open_time DESC
                    LIMIT :limit_rows
                    """
                ),
                {"symbol": symbol, "timeframe": timeframe, "limit_rows": int(limit)},
            ).fetchall()

        out: list[datetime] = []
        for row in rows:
            value = row[0] if isinstance(row, (tuple, list)) else getattr(row, "open_time", None)
            if isinstance(value, datetime):
                out.append(value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        return out

    def _get_latest_candle_time(self, symbol: str, timeframe: str) -> datetime | None:
        with get_session() as session:
            row = CandleRepository(session).get_latest(symbol, timeframe)
        if row is None or not isinstance(row.open_time, datetime):
            return None
        return row.open_time if row.open_time.tzinfo else row.open_time.replace(tzinfo=timezone.utc)

    def _count_candles(self, symbol: str, timeframe: str) -> int:
        with get_session() as session:
            value = session.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM candles
                    WHERE symbol = :symbol
                      AND timeframe = :timeframe
                    """
                ),
                {"symbol": symbol, "timeframe": timeframe},
            ).scalar_one()
        return int(value or 0)

    def _step_for_timeframe(self, timeframe: str) -> timedelta:
        if timeframe not in _TIMEFRAME_STEP:
            raise ValueError(f"Unsupported timeframe for daemon step: {timeframe}")
        return _TIMEFRAME_STEP[timeframe]

    @staticmethod
    def _parse_dt(value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
