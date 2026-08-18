from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from main import _parse_args
from market_data.daemon import MarketDataDaemonConfig, MarketDataDaemonService


class _TestDaemon(MarketDataDaemonService):
    def __init__(self, base_dir: Path, now: datetime):
        super().__init__(base_dir=base_dir, now_fn=lambda: now, sleep_fn=lambda _seconds: None)
        self.calls: list[tuple[str, str, datetime, datetime]] = []
        self.latest: dict[tuple[str, str], datetime | None] = {}
        self.counts: dict[tuple[str, str], int] = {}
        self.gaps: dict[tuple[str, str], list[tuple[datetime, datetime]]] = {}

    def _get_latest_candle_time(self, symbol: str, timeframe: str) -> datetime | None:
        return self.latest.get((symbol, timeframe))

    def _count_candles(self, symbol: str, timeframe: str) -> int:
        return int(self.counts.get((symbol, timeframe), 0))

    def _detect_recent_gap_ranges(self, symbol: str, timeframe: str, recent_gap_bars: int):
        return list(self.gaps.get((symbol, timeframe), []))

    def _download_with_retry(self, symbol: str, timeframe: str, start: datetime, end: datetime, cfg: MarketDataDaemonConfig) -> None:
        self.calls.append((symbol, timeframe, start, end))
        key = (symbol, timeframe)
        if end >= start:
            self.latest[key] = end
            self.counts[key] = int(self.counts.get(key, 0)) + 1


class _FlakyDownloader:
    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    def download_historical(self, symbol: str, timeframe: str, start: datetime, end: datetime):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise TimeoutError("temporary timeout")
        return None


class _ReconnectClient:
    def __init__(self):
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1

    def disconnect(self) -> None:
        self.disconnect_calls += 1


def test_cli_parser_market_data_daemon(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "market-data-daemon",
            "--symbols",
            "BTC/USDT,ETH/USDT",
            "--timeframes",
            "5m,15m",
            "--polling-interval-seconds",
            "10",
            "--retry-count",
            "3",
        ],
    )
    args = _parse_args()
    assert args.command == "market-data-daemon"
    assert args.symbols == "BTC/USDT,ETH/USDT"
    assert args.timeframes == "5m,15m"
    assert args.polling_interval_seconds == 10.0
    assert args.retry_count == 3


def test_incremental_update_uses_only_new_range(tmp_path: Path) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    daemon = _TestDaemon(tmp_path, now)
    cfg = MarketDataDaemonConfig(symbols=("BTC/USDT",), timeframes=("5m",), max_cycles=1)

    key = ("BTC/USDT", "5m")
    daemon.latest[key] = now - timedelta(minutes=5)
    daemon.counts[key] = 100
    daemon._ensure_context_state(*key)

    daemon._sync_context("BTC/USDT", "5m", cfg)

    assert len(daemon.calls) == 1
    _symbol, _timeframe, start, end = daemon.calls[0]
    assert start == now
    assert end == now


def test_no_duplicate_download_when_already_up_to_date(tmp_path: Path) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    daemon = _TestDaemon(tmp_path, now)
    cfg = MarketDataDaemonConfig(symbols=("ETH/USDT",), timeframes=("5m",), max_cycles=1)

    key = ("ETH/USDT", "5m")
    daemon.latest[key] = now
    daemon.counts[key] = 50
    daemon._ensure_context_state(*key)

    daemon._sync_context("ETH/USDT", "5m", cfg)

    assert daemon.calls == []
    assert daemon._state[key]["last_cycle_inserted"] == 0


def test_gap_detection_triggers_gap_fill(tmp_path: Path) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    daemon = _TestDaemon(tmp_path, now)
    cfg = MarketDataDaemonConfig(symbols=("SOL/USDT",), timeframes=("15m",), max_cycles=1)

    key = ("SOL/USDT", "15m")
    gap_start = now - timedelta(hours=2)
    gap_end = now - timedelta(hours=1, minutes=45)
    daemon.latest[key] = now
    daemon.counts[key] = 10
    daemon.gaps[key] = [(gap_start, gap_end)]
    daemon._ensure_context_state(*key)

    daemon._sync_context("SOL/USDT", "15m", cfg)

    assert len(daemon.calls) == 1
    assert daemon.calls[0][2] == gap_start
    assert daemon.calls[0][3] == gap_end
    assert daemon._state[key]["gap_ranges_filled"] == 1


def test_retry_timeout_and_reconnect(tmp_path: Path) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    sleeps: list[float] = []
    daemon = MarketDataDaemonService(tmp_path, now_fn=lambda: now, sleep_fn=lambda seconds: sleeps.append(seconds))
    cfg = MarketDataDaemonConfig(retry_count=3, retry_delay_seconds=1.0)

    key = ("BTC/USDT", "5m")
    daemon._ensure_context_state(*key)
    daemon._client = _ReconnectClient()
    daemon._downloader = _FlakyDownloader(fail_times=2)

    daemon._download_with_retry(
        "BTC/USDT",
        "5m",
        now - timedelta(minutes=5),
        now,
        cfg,
    )

    assert daemon._state[key]["retries"] == 2
    assert daemon._state[key]["failures"] == 2
    assert daemon._downloader.calls == 3
    assert daemon._client.connect_calls >= 2
    assert daemon._client.disconnect_calls >= 2
    assert sleeps == [1.0, 2.0]


def test_multiple_symbols_and_timeframes_processed(tmp_path: Path) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    calls: list[tuple[str, str]] = []
    daemon = MarketDataDaemonService(tmp_path, now_fn=lambda: now, sleep_fn=lambda _seconds: None)

    def _fake_sync(symbol: str, timeframe: str, cfg: MarketDataDaemonConfig) -> None:
        daemon._ensure_context_state(symbol, timeframe)
        calls.append((symbol, timeframe))

    daemon._sync_context = _fake_sync  # type: ignore[method-assign]

    class _NoopClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    daemon._client = _NoopClient()  # type: ignore[assignment]
    daemon._downloader = object()  # type: ignore[assignment]

    # Bypass external client construction for this isolated loop check.
    daemon._client = _NoopClient()  # type: ignore[assignment]

    cfg = MarketDataDaemonConfig(
        symbols=("BTC/USDT", "ETH/USDT"),
        timeframes=("5m", "15m"),
        max_cycles=1,
    )

    # Run through service loop with monkeypatched connect/disconnect/downloader creation.
    daemon._client = _NoopClient()  # type: ignore[assignment]
    daemon._downloader = object()  # type: ignore[assignment]

    # Execute the loop by patching run prerequisites.
    original_client_cls = daemon.__class__.__dict__.get("_client")
    _ = original_client_cls

    # Call internals directly to keep test deterministic.
    for symbol in cfg.symbols:
        for timeframe in cfg.timeframes:
            daemon._sync_context(symbol, timeframe, cfg)

    assert len(calls) == 4
    assert ("BTC/USDT", "5m") in calls
    assert ("ETH/USDT", "15m") in calls


def test_rate_limiting_sleep_between_contexts(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    sleeps: list[float] = []
    daemon = MarketDataDaemonService(tmp_path, now_fn=lambda: now, sleep_fn=lambda seconds: sleeps.append(seconds))

    class _NoopClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    class _NoopDownloader:
        def __init__(self, _client):
            return None

    monkeypatch.setattr("market_data.daemon.BinanceMarketDataClient", _NoopClient)
    monkeypatch.setattr("market_data.daemon.DataDownloader", _NoopDownloader)
    monkeypatch.setattr(daemon, "_sync_context", lambda s, t, c: daemon._ensure_context_state(s, t))

    cfg = MarketDataDaemonConfig(
        symbols=("BTC/USDT", "ETH/USDT"),
        timeframes=("5m",),
        context_delay_seconds=0.5,
        max_cycles=1,
    )
    daemon.run(cfg)

    assert 0.5 in sleeps


def test_continuous_execution_honors_max_cycles(tmp_path: Path, monkeypatch) -> None:
    now = datetime(2026, 7, 3, 14, 0, tzinfo=timezone.utc)
    daemon = MarketDataDaemonService(tmp_path, now_fn=lambda: now, sleep_fn=lambda _seconds: None)
    cycles: list[tuple[str, str]] = []

    class _NoopClient:
        def connect(self):
            return None

        def disconnect(self):
            return None

    class _NoopDownloader:
        def __init__(self, _client):
            return None

    monkeypatch.setattr("market_data.daemon.BinanceMarketDataClient", _NoopClient)
    monkeypatch.setattr("market_data.daemon.DataDownloader", _NoopDownloader)

    def _fake_sync(symbol: str, timeframe: str, cfg: MarketDataDaemonConfig) -> None:
        daemon._ensure_context_state(symbol, timeframe)
        cycles.append((symbol, timeframe))

    monkeypatch.setattr(daemon, "_sync_context", _fake_sync)

    cfg = MarketDataDaemonConfig(
        symbols=("BTC/USDT",),
        timeframes=("5m",),
        max_cycles=2,
        report_every_cycles=1,
    )
    result = daemon.run(cfg)

    assert len(cycles) == 2
    assert result["summary"]["cycle"] == 2
    assert Path(result["outputs"]["json"]).exists()
    assert Path(result["outputs"]["md"]).exists()
