from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

from config.settings import settings
from database.connection import get_session
from database.models import Candle
from exchange.binance_market_data_client import BinanceMarketDataClient
from exchange.data_downloader import DataDownloader
from utils.logger import get_logger

logger = get_logger(__name__)

MARKET_DATA_PRIORITY_SYMBOLS: tuple[str, ...] = (
    "BTC/USDT",
    "ETH/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "LINK/USDT",
    "DOGE/USDT",
    "AVAX/USDT",
    "ADA/USDT",
    "SUI/USDT",
    "OP/USDT",
    "ARB/USDT",
    "NEAR/USDT",
    "APT/USDT",
    "PEPE/USDT",
)

MARKET_DATA_TIMEFRAMES: tuple[str, ...] = ("5m", "15m", "30m", "1h", "4h")
_STEP = {
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
}


@dataclass(frozen=True)
class MarketDataExpansionConfig:
    symbols: tuple[str, ...] = MARKET_DATA_PRIORITY_SYMBOLS
    timeframes: tuple[str, ...] = MARKET_DATA_TIMEFRAMES
    mode: str = "full"  # incremental | backfill | full
    dry_run: bool = False
    auto_pipeline: bool = False
    continuous: bool = False
    continuous_max_cycles: int = 1
    history_days: int = 365
    min_assets_ready: int = 6
    min_months_diversity: int = 6
    min_regimes: int = 3
    min_quality_pct: float = 85.0


class MarketDataExpansionService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, config: MarketDataExpansionConfig | None = None) -> dict[str, Any]:
        config = config or MarketDataExpansionConfig()
        mode = config.mode.lower().strip()
        if mode not in {"incremental", "backfill", "full"}:
            raise ValueError("mode must be one of: incremental, backfill, full")

        if config.continuous:
            cycles = max(1, int(config.continuous_max_cycles))
            runs: list[dict[str, Any]] = []
            for _ in range(cycles):
                one = self._run_once(config)
                runs.append({
                    "generated_at": one["generated_at"],
                    "gate_passed": one["gate"]["passed"],
                    "total_candles": one["metrics"]["total_candles"],
                    "quality_pct": one["metrics"]["quality_pct"],
                })
                if one["gate"]["passed"]:
                    return {"mode": "continuous", "runs": runs, "last_result": one}
            return {"mode": "continuous", "runs": runs, "last_result": self._run_once(MarketDataExpansionConfig(**{**asdict(config), "continuous": False}))}

        return self._run_once(config)

    def _run_once(self, config: MarketDataExpansionConfig) -> dict[str, Any]:
        generated_at = datetime.now(tz=timezone.utc)
        exchange_limits = self._probe_exchange_limits(config)
        before = self._audit_inventory(config, exchange_limits)
        download = self._execute_download(config, exchange_limits)
        after = self._audit_inventory(config, exchange_limits)
        quality = self._build_quality(after, download)
        metrics = self._build_metrics(after, quality)
        gate = self._build_gate(after, metrics, config)
        growth = self._build_growth(before, after)
        previous_quant = self._load_previous_quantitative_summary()
        pipeline = self._run_auto_pipeline(config, gate)
        outputs = self._write_outputs(generated_at, config, before, after, exchange_limits, download, quality, metrics, gate, growth, previous_quant, pipeline)

        return {
            "generated_at": generated_at.isoformat(),
            "config": asdict(config),
            "before": before,
            "after": after,
            "download": download,
            "quality": quality,
            "metrics": metrics,
            "gate": gate,
            "growth": growth,
            "exchange_limits": exchange_limits,
            "previous_quantitative": previous_quant,
            "pipeline": pipeline,
            "outputs": outputs,
        }

    def _probe_exchange_limits(self, config: MarketDataExpansionConfig) -> dict[str, dict[str, Any]]:
        limits: dict[str, dict[str, Any]] = {}
        client = BinanceMarketDataClient()
        try:
            client.connect()
            for symbol in config.symbols:
                supported = client.is_symbol_supported(symbol)
                for tf in config.timeframes:
                    key = f"{symbol}|{tf}"
                    if not supported:
                        limits[key] = {"supported": False, "earliest": None, "max_days": None}
                        continue
                    try:
                        probe = client.fetch_ohlcv(symbol=symbol, timeframe=tf, since=0, limit=1)
                        if probe.empty:
                            limits[key] = {"supported": True, "earliest": None, "max_days": None}
                        else:
                            earliest = probe.index[0]
                            earliest_dt = earliest.to_pydatetime() if hasattr(earliest, "to_pydatetime") else earliest
                            max_days = max(1, (datetime.now(tz=timezone.utc) - earliest_dt).days)
                            limits[key] = {
                                "supported": True,
                                "earliest": earliest_dt.isoformat(),
                                "max_days": max_days,
                            }
                    except Exception:
                        limits[key] = {"supported": True, "earliest": None, "max_days": None}
        finally:
            client.disconnect()
        return limits

    def _audit_inventory(self, config: MarketDataExpansionConfig, limits: dict[str, dict[str, Any]]) -> dict[str, Any]:
        with get_session() as session:
            pair_rows: list[dict[str, Any]] = []
            symbol_rows: list[dict[str, Any]] = []
            overall_dates: set[str] = set()

            for symbol in config.symbols:
                first: datetime | None = None
                last: datetime | None = None
                dates: set[str] = set()
                tfs: list[str] = []
                candles_sum = 0
                missing_sum = 0
                gaps_sum = 0

                for tf in config.timeframes:
                    rows = self._load_all_candles(session, symbol, tf)
                    pair = self._audit_pair(symbol, tf, rows, limits.get(f"{symbol}|{tf}", {}))
                    pair_rows.append(pair)
                    if pair["candles_count"] > 0:
                        tfs.append(tf)
                        dates.update(pair["covered_dates"])
                        overall_dates.update(pair["covered_dates"])
                        pfirst = datetime.fromisoformat(pair["first_candle"]) if pair["first_candle"] else None
                        plast = datetime.fromisoformat(pair["last_candle"]) if pair["last_candle"] else None
                        if pfirst is not None:
                            first = pfirst if first is None else min(first, pfirst)
                        if plast is not None:
                            last = plast if last is None else max(last, plast)
                    candles_sum += pair["candles_count"]
                    missing_sum += pair["missing_candles"]
                    gaps_sum += pair["gaps_found"]

                symbol_rows.append(
                    {
                        "symbol": symbol,
                        "first_candle": first.isoformat() if first else None,
                        "last_candle": last.isoformat() if last else None,
                        "candles_count": candles_sum,
                        "days_covered": len(dates),
                        "coverage_pct": round(self._pct(candles_sum, candles_sum + missing_sum), 4) if candles_sum or missing_sum else 0.0,
                        "missing_candles": missing_sum,
                        "gaps_found": gaps_sum,
                        "timeframes_available": sorted(tfs),
                    }
                )

        for row in pair_rows:
            row.pop("covered_dates", None)
        pair_rows.sort(key=lambda x: (x["symbol"], x["timeframe"]))
        symbol_rows.sort(key=lambda x: x["symbol"])
        timeframes = self._aggregate_timeframes(pair_rows)

        overall = {
            "total_assets": len(symbol_rows),
            "total_timeframes": len(timeframes),
            "total_candles": int(sum(item["candles_count"] for item in pair_rows)),
            "days_available": len(overall_dates),
            "coverage_by_asset": {item["symbol"]: item["coverage_pct"] for item in symbol_rows},
            "coverage_by_timeframe": {item["timeframe"]: item["coverage_pct"] for item in timeframes},
            "missing_candles": int(sum(item["missing_candles"] for item in pair_rows)),
            "duplicate_rows": int(sum(item["duplicate_rows"] for item in pair_rows)),
            "invalid_ohlcv_rows": int(sum(item["invalid_ohlcv_rows"] for item in pair_rows)),
            "invalid_volume_rows": int(sum(item["invalid_volume_rows"] for item in pair_rows)),
            "out_of_order_rows": int(sum(item["out_of_order_rows"] for item in pair_rows)),
            "average_pair_coverage_pct": round(float(pd.DataFrame(pair_rows)["coverage_pct"].mean()) if pair_rows else 0.0, 4),
            "space_used_bytes": self._space_used_bytes(),
        }
        return {"overall": overall, "symbols": symbol_rows, "pairs": pair_rows, "timeframes": timeframes}

    def _aggregate_timeframes(self, pair_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bucket: dict[str, dict[str, Any]] = {}
        for row in pair_rows:
            tf = row["timeframe"]
            agg = bucket.setdefault(tf, {"candles": 0, "missing": 0, "gaps": 0, "symbols": set()})
            agg["candles"] += int(row["candles_count"])
            agg["missing"] += int(row["missing_candles"])
            agg["gaps"] += int(row["gaps_found"])
            if int(row["candles_count"]) > 0:
                agg["symbols"].add(row["symbol"])

        out = []
        for tf, agg in bucket.items():
            out.append(
                {
                    "timeframe": tf,
                    "candles_count": agg["candles"],
                    "coverage_pct": round(self._pct(agg["candles"], agg["candles"] + agg["missing"]), 4) if agg["candles"] or agg["missing"] else 0.0,
                    "missing_candles": agg["missing"],
                    "gaps_found": agg["gaps"],
                    "symbols_covered": sorted(agg["symbols"]),
                    "days_covered": 0,
                }
            )
        out.sort(key=lambda x: x["timeframe"])
        return out

    def _audit_pair(self, symbol: str, timeframe: str, candles: list[Candle], limit_meta: dict[str, Any]) -> dict[str, Any]:
        step = _STEP[timeframe]
        timestamps: list[datetime] = []
        duplicate_rows = 0
        utc_normalized_rows = 0
        invalid_ohlcv_rows = 0
        invalid_volume_rows = 0
        out_of_order_rows = 0
        seen: set[datetime] = set()
        prev: datetime | None = None

        for row in candles:
            ts = self._ensure_utc(row.open_time)
            if prev is not None and ts < prev:
                out_of_order_rows += 1
            prev = ts
            if ts in seen:
                duplicate_rows += 1
                continue
            seen.add(ts)
            if ts != row.open_time:
                utc_normalized_rows += 1
            timestamps.append(ts)

            if not self._valid_ohlcv(row.open, row.high, row.low, row.close):
                invalid_ohlcv_rows += 1
            if not self._valid_volume(row.volume):
                invalid_volume_rows += 1

        timestamps.sort()
        covered_dates = {ts.date().isoformat() for ts in timestamps}
        gaps = self._gap_details(timestamps, step)

        expected = 0
        if limit_meta.get("earliest"):
            expected = self._expected_between(datetime.fromisoformat(limit_meta["earliest"]), datetime.now(tz=timezone.utc), step)
        elif timestamps:
            expected = self._expected_between(timestamps[0], timestamps[-1], step)

        missing = max(0, expected - len(timestamps)) if expected else 0

        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "first_candle": timestamps[0].isoformat() if timestamps else None,
            "last_candle": timestamps[-1].isoformat() if timestamps else None,
            "candles_count": len(timestamps),
            "days_covered": len(covered_dates),
            "coverage_pct": round(self._pct(len(timestamps), expected), 4) if expected else 0.0,
            "missing_candles": int(missing),
            "gaps_found": len(gaps),
            "largest_gap_candles": max((g["missing_candles"] for g in gaps), default=0),
            "largest_gap_minutes": max((g["missing_minutes"] for g in gaps), default=0),
            "duplicate_rows": duplicate_rows,
            "utc_normalized_rows": utc_normalized_rows,
            "invalid_ohlcv_rows": invalid_ohlcv_rows,
            "invalid_volume_rows": invalid_volume_rows,
            "out_of_order_rows": out_of_order_rows,
            "exchange_earliest": limit_meta.get("earliest"),
            "exchange_max_days": limit_meta.get("max_days"),
            "gap_details": gaps,
            "timeframes_available": [timeframe] if timestamps else [],
            "covered_dates": covered_dates,
        }

    def _execute_download(self, config: MarketDataExpansionConfig, limits: dict[str, dict[str, Any]]) -> dict[str, Any]:
        if config.dry_run:
            return {"dry_run": True, "targets": [], "inserted_candles": 0, "duplicates_removed": 0, "utc_normalized": 0, "limitations": []}

        client = BinanceMarketDataClient()
        downloader = DataDownloader(client)
        targets: list[dict[str, Any]] = []
        limitations: list[dict[str, Any]] = []
        inserted_total = 0
        duplicates_removed = 0
        utc_normalized = 0

        try:
            client.connect()
            for symbol in config.symbols:
                supported = client.is_symbol_supported(symbol)
                for tf in config.timeframes:
                    if not supported:
                        limitation = {"symbol": symbol, "timeframe": tf, "reason": "unsupported_on_binance"}
                        limitations.append(limitation)
                        targets.append({"symbol": symbol, "timeframe": tf, "status": "limited", "downloaded_ranges": [], "inserted_candles": 0, "limitation": limitation})
                        continue

                    try:
                        before = self._count_candles(symbol, tf)
                        ranges = self._compute_ranges(symbol, tf, config.mode, limits.get(f"{symbol}|{tf}", {}))
                        downloaded_ranges = []
                        for start, end in ranges:
                            if start > end:
                                continue
                            downloader.download_historical(symbol, tf, start, end)
                            downloaded_ranges.append({"start": start.isoformat(), "end": end.isoformat()})

                        after = self._count_candles(symbol, tf)
                        inserted = max(0, after - before)
                        inserted_total += inserted
                        dedup = self._deduplicate(symbol, tf)
                        norm = self._normalize_utc(symbol, tf)
                        duplicates_removed += dedup
                        utc_normalized += norm

                        targets.append({
                            "symbol": symbol,
                            "timeframe": tf,
                            "status": "updated" if downloaded_ranges else "up_to_date",
                            "downloaded_ranges": downloaded_ranges,
                            "inserted_candles": inserted,
                            "duplicates_removed": dedup,
                            "utc_normalized": norm,
                        })
                    except Exception as exc:
                        limitation = {"symbol": symbol, "timeframe": tf, "reason": str(exc)}
                        limitations.append(limitation)
                        targets.append({"symbol": symbol, "timeframe": tf, "status": "limited", "downloaded_ranges": [], "inserted_candles": 0, "limitation": limitation})
        finally:
            client.disconnect()

        return {
            "dry_run": False,
            "targets": targets,
            "inserted_candles": inserted_total,
            "duplicates_removed": duplicates_removed,
            "utc_normalized": utc_normalized,
            "limitations": limitations,
        }

    def _compute_ranges(self, symbol: str, timeframe: str, mode: str, limit_meta: dict[str, Any]) -> list[tuple[datetime, datetime]]:
        now = datetime.now(tz=timezone.utc)
        step = _STEP[timeframe]
        with get_session() as session:
            rows = self._load_all_candles(session, symbol, timeframe)
        stamps = sorted({self._ensure_utc(row.open_time) for row in rows})

        first = stamps[0] if stamps else None
        last = stamps[-1] if stamps else None
        ranges: list[tuple[datetime, datetime]] = []

        if mode in {"incremental", "full"} and last is not None:
            inc_start = last + step
            if inc_start <= now:
                ranges.append((inc_start, now))

        if mode in {"backfill", "full"}:
            earliest = datetime.fromisoformat(limit_meta["earliest"]) if limit_meta.get("earliest") else None
            if earliest is not None:
                if first is None:
                    ranges.append((earliest, now))
                else:
                    backfill_end = first - step
                    if earliest <= backfill_end:
                        ranges.append((earliest, backfill_end))
                if first is not None and last is not None:
                    ranges.extend(self._internal_gap_ranges(stamps, step))

        return self._merge_ranges(ranges)

    def _internal_gap_ranges(self, stamps: list[datetime], step: timedelta) -> list[tuple[datetime, datetime]]:
        ranges = []
        for prev, cur in zip(stamps[:-1], stamps[1:]):
            if cur - prev > step:
                ranges.append((prev + step, cur - step))
        return ranges

    def _merge_ranges(self, ranges: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
        clean = sorted((s, e) for s, e in ranges if s <= e)
        if not clean:
            return []
        out = [clean[0]]
        for start, end in clean[1:]:
            prev_s, prev_e = out[-1]
            if start <= prev_e + timedelta(seconds=1):
                out[-1] = (prev_s, max(prev_e, end))
            else:
                out.append((start, end))
        return out

    def _build_quality(self, after: dict[str, Any], download: dict[str, Any]) -> dict[str, Any]:
        issues: list[dict[str, Any]] = []
        for row in after["pairs"]:
            if row["gaps_found"] > 0:
                issues.append({"symbol": row["symbol"], "timeframe": row["timeframe"], "issue": "gaps", "count": row["missing_candles"]})
            if row["duplicate_rows"] > 0:
                issues.append({"symbol": row["symbol"], "timeframe": row["timeframe"], "issue": "duplicates", "count": row["duplicate_rows"]})
            if row["invalid_ohlcv_rows"] > 0:
                issues.append({"symbol": row["symbol"], "timeframe": row["timeframe"], "issue": "invalid_ohlc", "count": row["invalid_ohlcv_rows"]})
            if row["invalid_volume_rows"] > 0:
                issues.append({"symbol": row["symbol"], "timeframe": row["timeframe"], "issue": "invalid_volume", "count": row["invalid_volume_rows"]})
            if row["out_of_order_rows"] > 0:
                issues.append({"symbol": row["symbol"], "timeframe": row["timeframe"], "issue": "out_of_order", "count": row["out_of_order_rows"]})

        overall = after["overall"]
        total = max(1, overall["total_candles"])
        completeness = 100.0 * (1.0 - (overall["missing_candles"] / max(1, overall["missing_candles"] + overall["total_candles"])))
        validity = 100.0 * (1.0 - ((overall["invalid_ohlcv_rows"] + overall["invalid_volume_rows"] + overall["out_of_order_rows"]) / total))
        uniqueness = 100.0 * (1.0 - (overall["duplicate_rows"] / total))
        quality_pct = max(0.0, min(100.0, 0.5 * completeness + 0.3 * validity + 0.2 * uniqueness))

        return {
            "status": "warning" if issues else "ok",
            "issues": issues,
            "issue_count": len(issues),
            "quality_breakdown": {
                "completeness_pct": round(completeness, 4),
                "validity_pct": round(validity, 4),
                "uniqueness_pct": round(uniqueness, 4),
            },
            "quality_pct": round(quality_pct, 4),
            "download": download,
        }

    def _build_metrics(self, after: dict[str, Any], quality: dict[str, Any]) -> dict[str, Any]:
        overall = after["overall"]
        return {
            "total_assets": overall["total_assets"],
            "total_timeframes": overall["total_timeframes"],
            "total_candles": overall["total_candles"],
            "days_available": overall["days_available"],
            "coverage_by_asset": after["symbols"],
            "coverage_by_timeframe": after["timeframes"],
            "quality_pct": quality["quality_pct"],
            "space_used_bytes": overall["space_used_bytes"],
        }

    def _build_gate(self, after: dict[str, Any], metrics: dict[str, Any], config: MarketDataExpansionConfig) -> dict[str, Any]:
        assets_ready = [
            row["symbol"]
            for row in after["symbols"]
            if row["candles_count"] > 0 and len(row["timeframes_available"]) >= 3 and row["days_covered"] >= 90
        ]
        temporal_months = self._temporal_diversity_months(after["pairs"])
        regimes = self._market_regime_diversity()
        quality_ok = metrics["quality_pct"] >= float(config.min_quality_pct)

        passed = (
            len(assets_ready) >= int(config.min_assets_ready)
            and temporal_months >= int(config.min_months_diversity)
            and regimes["regime_count"] >= int(config.min_regimes)
            and quality_ok
        )

        missing = []
        if len(assets_ready) < int(config.min_assets_ready):
            missing.append("asset_coverage_insufficient")
        if temporal_months < int(config.min_months_diversity):
            missing.append("temporal_diversity_insufficient")
        if regimes["regime_count"] < int(config.min_regimes):
            missing.append("market_regime_diversity_insufficient")
        if not quality_ok:
            missing.append("data_quality_below_threshold")

        return {
            "passed": passed,
            "missing": missing,
            "assets_ready": assets_ready,
            "assets_ready_count": len(assets_ready),
            "temporal_months": temporal_months,
            "regime_diversity": regimes,
            "quality_pct": metrics["quality_pct"],
            "recommendation": "LIBERAR_NOVA_RODADA_QUANTITATIVA" if passed else "BLOQUEAR_NOVA_RODADA_QUANTITATIVA",
            "ready_when": "A base deve atingir cobertura consistente, diversidade temporal e regimes suficientes com qualidade minima.",
        }

    def _temporal_diversity_months(self, pairs: list[dict[str, Any]]) -> int:
        months: set[str] = set()
        for row in pairs:
            first = row.get("first_candle")
            last = row.get("last_candle")
            if not first or not last:
                continue
            start = datetime.fromisoformat(first)
            end = datetime.fromisoformat(last)
            cursor = start.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            while cursor <= end:
                months.add(cursor.strftime("%Y-%m"))
                cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return len(months)

    def _market_regime_diversity(self) -> dict[str, Any]:
        with get_session() as session:
            rows = (
                session.query(Candle)
                .filter(Candle.symbol == "BTC/USDT", Candle.timeframe == "1h")
                .order_by(Candle.open_time.asc())
                .all()
            )

        if len(rows) < 100:
            return {"regime_count": 0, "distribution": {}, "status": "insufficient_data"}

        df = pd.DataFrame([{"close": row.close} for row in rows])
        df["ret"] = pd.to_numeric(df["close"], errors="coerce").pct_change().fillna(0.0)
        df["vol"] = df["ret"].rolling(48, min_periods=24).std().fillna(0.0)

        labels = []
        for ret, vol in zip(df["ret"], df["vol"]):
            trend = "bull" if ret > 0.001 else "bear" if ret < -0.001 else "side"
            v = "high_vol" if vol > float(df["vol"].quantile(0.67)) else "normal_vol"
            labels.append(f"{trend}_{v}")

        dist = pd.Series(labels).value_counts().to_dict()
        regime_count = len([k for k, v in dist.items() if v >= 20])
        return {"regime_count": regime_count, "distribution": dist, "status": "ok"}

    def _run_auto_pipeline(self, config: MarketDataExpansionConfig, gate: dict[str, Any]) -> dict[str, Any]:
        if not config.auto_pipeline:
            return {"requested": False, "executed": False, "blocked": True, "reason": "auto_pipeline_disabled"}
        if not gate["passed"]:
            return {"requested": True, "executed": False, "blocked": True, "reason": "gate_blocked"}

        quantitative = self._run_quantitative()
        h1_audit = self._run_h1_audit(quantitative)
        pipeline_report = self._write_pipeline_report(gate, quantitative, h1_audit)
        return {
            "requested": True,
            "executed": True,
            "blocked": False,
            "quantitative": quantitative,
            "h1_audit": h1_audit,
            "report": pipeline_report,
        }

    def _run_quantitative(self) -> dict[str, Any]:
        from research.services.quantitative_discovery_lab import QuantitativeDiscoveryLab, QuantitativeDiscoveryLabConfig

        lab = QuantitativeDiscoveryLab(self._base_dir)
        result = lab.run(QuantitativeDiscoveryLabConfig())
        return {"ok": True, "summary": result.get("summary", {}), "outputs": result.get("outputs", {})}

    def _run_h1_audit(self, quantitative: dict[str, Any]) -> dict[str, Any]:
        csv_path = quantitative.get("outputs", {}).get("hypotheses_csv")
        if not csv_path or not Path(csv_path).exists():
            return {"decision": "OPCAO_B", "status": "blocked", "reason": "hypotheses_csv_not_found"}

        frame = pd.read_csv(csv_path)
        if frame.empty:
            return {"decision": "OPCAO_B", "status": "blocked", "reason": "empty_hypotheses"}

        h1 = frame[frame["hypothesis_id"] == "H1"]
        if h1.empty:
            h1 = frame.sort_values(["rank"], ascending=[True]).head(1)
        row = h1.iloc[0]

        confidence = float(row.get("confidence", 0.0) or 0.0)
        sample_size = int(row.get("sample_size", 0) or 0)
        priority = float(row.get("priority", 0.0) or 0.0)
        approved = confidence >= 0.70 and sample_size >= 120 and priority >= 0.60

        return {
            "decision": "OPCAO_A" if approved else "OPCAO_B",
            "status": "approved" if approved else "blocked",
            "confidence": confidence,
            "sample_size": sample_size,
            "priority": priority,
            "family": row.get("family"),
            "hypothesis_id": row.get("hypothesis_id"),
            "reason": "H1 atende criterios minimos de robustez." if approved else "H1 ainda nao atende criterios minimos de robustez.",
        }

    def _write_pipeline_report(self, gate: dict[str, Any], quantitative: dict[str, Any], h1: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"historical_platform_pipeline_{stamp}.json"
        md_path = self._results_dir / f"historical_platform_pipeline_{stamp}.md"

        payload = {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "gate": gate,
            "quantitative": quantitative,
            "h1_audit": h1,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        md = [
            "# Historical Data Platform Pipeline",
            "",
            f"Gate recommendation: {gate.get('recommendation')}",
            f"Quant family: {quantitative.get('summary', {}).get('recommended_family')}",
            f"Quant hypothesis: {quantitative.get('summary', {}).get('recommended_hypothesis')}",
            f"H1 decision: {h1.get('decision')}",
            f"H1 reason: {h1.get('reason')}",
        ]
        md_path.write_text("\n".join(md) + "\n", encoding="utf-8")
        return {"json": str(json_path), "markdown": str(md_path)}

    def _build_growth(self, before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
        return {
            "candles_delta": int(after["overall"]["total_candles"] - before["overall"]["total_candles"]),
            "days_delta": int(after["overall"]["days_available"] - before["overall"]["days_available"]),
            "missing_candles_delta": int(after["overall"]["missing_candles"] - before["overall"]["missing_candles"]),
            "coverage_delta": round(float(after["overall"]["average_pair_coverage_pct"] - before["overall"]["average_pair_coverage_pct"]), 4),
        }

    def _write_outputs(
        self,
        generated_at: datetime,
        config: MarketDataExpansionConfig,
        before: dict[str, Any],
        after: dict[str, Any],
        limits: dict[str, dict[str, Any]],
        download: dict[str, Any],
        quality: dict[str, Any],
        metrics: dict[str, Any],
        gate: dict[str, Any],
        growth: dict[str, Any],
        previous_quant: dict[str, Any] | None,
        pipeline: dict[str, Any],
    ) -> dict[str, str]:
        stamp = generated_at.strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"historical_data_platform_{stamp}.json"
        md_path = self._results_dir / f"historical_data_platform_{stamp}.md"
        symbols_csv = self._results_dir / f"historical_data_symbols_{stamp}.csv"
        pairs_csv = self._results_dir / f"historical_data_pairs_{stamp}.csv"
        timeframes_csv = self._results_dir / f"historical_data_timeframes_{stamp}.csv"
        quality_csv = self._results_dir / f"historical_data_quality_{stamp}.csv"
        coverage_csv = self._results_dir / f"historical_data_coverage_dashboard_{stamp}.csv"

        payload = {
            "generated_at": generated_at.isoformat(),
            "config": asdict(config),
            "before": before,
            "after": after,
            "exchange_limits": limits,
            "download": download,
            "quality": quality,
            "metrics": metrics,
            "gate": gate,
            "growth": growth,
            "previous_quantitative": previous_quant,
            "pipeline": pipeline,
        }
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

        pd.DataFrame(after["symbols"]).to_csv(symbols_csv, index=False)
        pair_df = pd.DataFrame(after["pairs"])
        if not pair_df.empty and "gap_details" in pair_df.columns:
            pair_df["gap_details"] = pair_df["gap_details"].apply(lambda x: json.dumps(x, ensure_ascii=False, default=str))
        pair_df.to_csv(pairs_csv, index=False)
        pd.DataFrame(after["timeframes"]).to_csv(timeframes_csv, index=False)
        pd.DataFrame(quality["issues"]).to_csv(quality_csv, index=False)
        pd.DataFrame(after["symbols"]).to_csv(coverage_csv, index=False)

        lines = [
            "# Historical Data Platform",
            "",
            f"Generated at: {generated_at.isoformat()}",
            "",
            "## Coverage",
            f"- Total assets: {metrics['total_assets']}",
            f"- Total timeframes: {metrics['total_timeframes']}",
            f"- Total candles: {metrics['total_candles']}",
            f"- Days covered: {metrics['days_available']}",
            f"- Quality: {metrics['quality_pct']}%",
            "",
            "## Growth",
            f"- Candles added: {growth['candles_delta']}",
            f"- Days delta: {growth['days_delta']}",
            f"- Missing candles delta: {growth['missing_candles_delta']}",
            "",
            "## Problems",
            f"- Found: {quality['issue_count']}",
            f"- Corrected duplicates: {download.get('duplicates_removed', 0)}",
            f"- Corrected UTC: {download.get('utc_normalized', 0)}",
            "",
            "## Statistical Gate",
            f"- Passed: {gate['passed']}",
            f"- Recommendation: {gate['recommendation']}",
            f"- Missing: {', '.join(gate['missing']) if gate['missing'] else 'none'}",
            "",
            "## Readiness",
            f"- Assets ready: {gate['assets_ready_count']}",
            f"- Temporal months: {gate['temporal_months']}",
            f"- Regime count: {gate['regime_diversity'].get('regime_count')}",
            "",
            "No new strategy implementation should proceed while the gate remains blocked.",
        ]
        md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        return {
            "json": str(json_path),
            "markdown": str(md_path),
            "symbols_csv": str(symbols_csv),
            "pairs_csv": str(pairs_csv),
            "timeframes_csv": str(timeframes_csv),
            "quality_csv": str(quality_csv),
            "coverage_dashboard_csv": str(coverage_csv),
        }

    def _load_previous_quantitative_summary(self) -> dict[str, Any] | None:
        files = sorted(self._results_dir.glob("quantitative_discovery_lab_*.json"))
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text(encoding="utf-8"))
        except Exception as exc:
            return {"path": str(files[-1]), "error": str(exc)}

    def _load_all_candles(self, session: Any, symbol: str, timeframe: str) -> list[Candle]:
        return (
            session.query(Candle)
            .filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.asc(), Candle.id.asc())
            .all()
        )

    def _count_candles(self, symbol: str, timeframe: str) -> int:
        with get_session() as session:
            return session.query(Candle).filter(Candle.symbol == symbol, Candle.timeframe == timeframe).count()

    def _deduplicate(self, symbol: str, timeframe: str) -> int:
        removed = 0
        with get_session() as session:
            rows = self._load_all_candles(session, symbol, timeframe)
            seen: set[datetime] = set()
            for row in rows:
                ts = self._ensure_utc(row.open_time)
                if ts in seen:
                    session.delete(row)
                    removed += 1
                else:
                    seen.add(ts)
        return removed

    def _normalize_utc(self, symbol: str, timeframe: str) -> int:
        normalized = 0
        with get_session() as session:
            rows = self._load_all_candles(session, symbol, timeframe)
            for row in rows:
                open_time = self._ensure_utc(row.open_time)
                if open_time != row.open_time:
                    row.open_time = open_time
                    normalized += 1
                if row.close_time is not None:
                    close_time = self._ensure_utc(row.close_time)
                    if close_time != row.close_time:
                        row.close_time = close_time
                        normalized += 1
        return normalized

    def _gap_details(self, timestamps: list[datetime], step: timedelta) -> list[dict[str, Any]]:
        gaps = []
        for prev, cur in zip(timestamps[:-1], timestamps[1:]):
            missing = int((cur - prev).total_seconds() // step.total_seconds()) - 1
            if missing > 0:
                gaps.append({
                    "start": (prev + step).isoformat(),
                    "end": (cur - step).isoformat(),
                    "missing_candles": missing,
                    "missing_minutes": int(missing * step.total_seconds() // 60),
                })
        return gaps

    def _expected_between(self, start: datetime, end: datetime, step: timedelta) -> int:
        if end < start:
            return 0
        return int((end - start).total_seconds() // step.total_seconds()) + 1

    def _ensure_utc(self, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _valid_ohlcv(self, open_price: Any, high: Any, low: Any, close: Any) -> bool:
        try:
            op = float(open_price)
            hi = float(high)
            lo = float(low)
            cl = float(close)
        except Exception:
            return False
        values = [op, hi, lo, cl]
        if any(math.isnan(v) or math.isinf(v) for v in values):
            return False
        if min(values) <= 0:
            return False
        return hi >= max(op, cl, lo) and lo <= min(op, cl, hi)

    def _valid_volume(self, volume: Any) -> bool:
        try:
            v = float(volume)
        except Exception:
            return False
        return not (math.isnan(v) or math.isinf(v) or v < 0)

    def _pct(self, numerator: int, denominator: int) -> float:
        if denominator <= 0:
            return 0.0
        return (numerator / denominator) * 100.0

    def _space_used_bytes(self) -> int:
        total = 0
        db_url = settings.database.url
        if db_url.startswith("sqlite:///"):
            db_path = Path(db_url.replace("sqlite:///", "", 1))
            if db_path.exists():
                total += db_path.stat().st_size
        else:
            try:
                engine = create_engine(db_url, future=True)
                with engine.connect() as conn:
                    val = conn.execute(
                        text(
                            "SELECT COALESCE(SUM(data_length + index_length), 0) FROM information_schema.tables WHERE table_schema = DATABASE()"
                        )
                    ).scalar()
                total += int(val or 0)
                engine.dispose()
            except Exception:
                pass

        for path in self._results_dir.glob("historical_data_*"):
            if path.is_file():
                total += path.stat().st_size
        return total
