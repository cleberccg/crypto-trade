from __future__ import annotations

import json
import math
import statistics
import time
import tracemalloc
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select

from database.connection import get_session
from database.models import Candle
from utils.metrics import expectancy_from_pnl, max_drawdown_from_pnl, profit_factor_from_pnl, sharpe_from_pnl
from utils.logger import get_logger


logger = get_logger(__name__)


EVENTS_COMPACT_COLUMNS = {
    "operation_id",
    "entry_time",
    "exit_time",
    "timestamp",
    "symbol",
    "timeframe",
    "strategy",
    "direction",
    "side",
    "primary_regime",
    "entry_trend_regime",
    "entry_vol_regime",
    "entry_price",
    "exit_price",
    "stop_loss",
    "take_profit",
    "risk_reward",
    "quantity",
    "pnl",
    "pnl_percent",
    "duration_minutes",
    "score",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "ema_fast",
    "ema_slow",
    "trend_score",
    "entry_trend_score",
    "entry_atr_pct",
    "atr",
    "rsi",
    "rsi_max",
    "entry_candle_close",
    "mfe",
    "mae",
    "first_move",
    "final_move",
    "capture_ratio",
    "mfe_used_pct",
}

EVENTS_COMPACT_DTYPES: dict[str, str] = {
    "operation_id": "string",
    "symbol": "string",
    "timeframe": "string",
    "strategy": "string",
    "direction": "string",
    "side": "string",
    "primary_regime": "string",
    "entry_trend_regime": "string",
    "entry_vol_regime": "string",
    "entry_price": "float32",
    "exit_price": "float32",
    "stop_loss": "float32",
    "take_profit": "float32",
    "risk_reward": "float32",
    "quantity": "float32",
    "pnl": "float32",
    "pnl_percent": "float32",
    "duration_minutes": "float32",
    "score": "float32",
    "open": "float32",
    "high": "float32",
    "low": "float32",
    "close": "float32",
    "volume": "float32",
    "ema_fast": "float32",
    "ema_slow": "float32",
    "trend_score": "float32",
    "entry_trend_score": "float32",
    "entry_atr_pct": "float32",
    "atr": "float32",
    "rsi": "float32",
    "rsi_max": "float32",
    "entry_candle_close": "float32",
    "mfe": "float32",
    "mae": "float32",
    "first_move": "float32",
    "final_move": "float32",
    "capture_ratio": "float32",
    "mfe_used_pct": "float32",
}


@dataclass(frozen=True)
class QuantitativeDiscoveryLabConfig:
    operations_csv_glob: str = "strategy_research_operations_*.csv"
    min_cluster_size: int = 12
    min_hypothesis_trades: int = 20
    top_feature_count: int = 12
    top_hypothesis_count: int = 10
    include_historical_candles: bool = True
    historical_breakout_window: int = 20
    historical_horizon_bars: int = 12
    chunk_days: int = 180
    chunk_overlap_bars: int = 60
    resume_run_id: str | None = None
    persist_intermediate: bool = True
    symbols: tuple[str, ...] | None = None
    timeframes: tuple[str, ...] | None = None
    compute_source_snapshots: bool = False


class QuantitativeDiscoveryLab:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir

    def run(self, config: QuantitativeDiscoveryLabConfig | None = None) -> dict[str, Any]:
        config = config or QuantitativeDiscoveryLabConfig()
        run_started = time.perf_counter()
        tracemalloc.start()
        profile: dict[str, float] = defaultdict(float)
        logger.info(
            "Quantitative Discovery Lab started | include_historical=%s | min_cluster_size=%s | min_hypothesis_trades=%s",
            config.include_historical_candles,
            config.min_cluster_size,
            config.min_hypothesis_trades,
        )

        state = self._initialize_incremental_state(config)
        profile["state_init"] += state["init_seconds"]

        operations = self._load_operations(config)
        operations_count = int(len(operations))
        logger.info("Loaded optimizer operations | rows=%s", operations_count)
        historical_events, chunk_audit = self._load_historical_events_incremental(config=config, state=state, profile=profile)
        historical_events_count = int(len(historical_events))
        logger.info("Loaded candle events | rows=%s", historical_events_count)

        if operations.empty and historical_events.empty:
            raise ValueError("Nenhum arquivo strategy_research_operations_*.csv encontrado com dados validos.")

        operations_snapshot = None
        historical_snapshot = None
        if config.compute_source_snapshots:
            t_snapshot = time.perf_counter()
            operations_snapshot = self._analysis_snapshot(operations, config, profile=profile, stage_prefix="operations") if not operations.empty else None
            historical_snapshot = self._analysis_snapshot(historical_events, config, profile=profile, stage_prefix="historical") if not historical_events.empty else None
            profile["snapshot_sources"] += time.perf_counter() - t_snapshot
        combined_source = pd.concat([frame for frame in [operations, historical_events] if not frame.empty], ignore_index=True, copy=False)
        if not config.compute_source_snapshots:
            # Keep only one large frame alive during the heavy analysis stage.
            historical_events = pd.DataFrame()
        logger.info("Combined dataset assembled | rows=%s", len(combined_source))

        combined_snapshot = self._analysis_snapshot(combined_source, config, profile=profile, stage_prefix="combined")
        enriched = combined_snapshot["dataset"]
        feature_summary = combined_snapshot["feature_summary"]
        clusters = combined_snapshot["clusters"]
        cluster_metrics = combined_snapshot["cluster_metrics"]
        ranked_hypotheses = combined_snapshot["ranked_hypotheses"]
        recommendation = combined_snapshot["recommendation"]
        patterns = combined_snapshot["patterns"]
        source_breakdown = {
            "optimizer_operations": operations_count,
            "historical_candle_events": historical_events_count,
            "combined": int(len(enriched)),
        }
        h1_comparison = self._compare_h1_snapshots(operations_snapshot, historical_snapshot, combined_snapshot)
        report = self._build_report(
            operations=enriched,
            feature_summary=feature_summary,
            clusters=clusters,
            cluster_metrics=cluster_metrics,
            hypotheses=ranked_hypotheses,
            recommendation=recommendation,
            patterns=patterns,
            source_breakdown=source_breakdown,
            h1_comparison=h1_comparison,
        )

        elapsed_total = time.perf_counter() - run_started
        _, peak_memory = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        old_reference = self._load_previous_monolithic_reference(state["run_id"])
        equivalence = self._build_equivalence_check(old_reference, combined_snapshot)
        scalability_audit = self._build_scalability_audit(
            profile=profile,
            chunk_audit=chunk_audit,
            total_candles=chunk_audit.get("candles_processed", 0),
            total_events=historical_events_count,
            optimizer_events=operations_count,
            combined_events=int(len(enriched)),
            elapsed_total=elapsed_total,
            peak_memory_bytes=int(peak_memory),
            old_reference=old_reference,
        )

        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "config": {
                "operations_csv_glob": config.operations_csv_glob,
                "min_cluster_size": config.min_cluster_size,
                "min_hypothesis_trades": config.min_hypothesis_trades,
                "top_feature_count": config.top_feature_count,
                "top_hypothesis_count": config.top_hypothesis_count,
                "include_historical_candles": config.include_historical_candles,
                "historical_breakout_window": config.historical_breakout_window,
                "historical_horizon_bars": config.historical_horizon_bars,
                "chunk_days": config.chunk_days,
                "chunk_overlap_bars": config.chunk_overlap_bars,
                "resume_run_id": config.resume_run_id,
                "persist_intermediate": config.persist_intermediate,
                "symbols": list(config.symbols) if config.symbols else None,
                "timeframes": list(config.timeframes) if config.timeframes else None,
                "compute_source_snapshots": config.compute_source_snapshots,
            },
            "run_id": state["run_id"],
            "source_breakdown": source_breakdown,
            "dataset_size": int(len(enriched)),
            "operations_snapshot": self._snapshot_payload(operations_snapshot),
            "historical_snapshot": self._snapshot_payload(historical_snapshot),
            "combined_snapshot": self._snapshot_payload(combined_snapshot),
            "h1_comparison": h1_comparison,
            "chunk_audit": chunk_audit,
            "profiling": self._profiling_payload(profile, elapsed_total),
            "scalability_audit": scalability_audit,
            "scientific_equivalence": equivalence,
            "pattern_summary": patterns,
            "feature_importance": feature_summary,
            "clusters": clusters,
            "cluster_metrics": cluster_metrics,
            "hypotheses": ranked_hypotheses,
            "recommendation": recommendation,
            "executive_report": report,
        }

        t_write = time.perf_counter()
        outputs = self._write_outputs(payload, enriched, feature_summary, clusters, cluster_metrics, ranked_hypotheses)
        profile["report_generation"] += time.perf_counter() - t_write
        logger.info(
            "Quantitative Discovery Lab finished | total_operations=%s | historical_events=%s | hypotheses=%s",
            len(enriched),
            historical_events_count,
            len(ranked_hypotheses),
        )
        return {
            "summary": {
                "total_operations": int(len(enriched)),
                "historical_events": historical_events_count,
                "feature_importance_count": len(feature_summary),
                "cluster_count": len(cluster_metrics),
                "hypothesis_count": len(ranked_hypotheses),
                "elapsed_seconds": round(elapsed_total, 6),
                "peak_memory_mb": round(int(peak_memory) / (1024 * 1024), 3),
                "recommended_family": recommendation.get("family"),
                "recommended_hypothesis": recommendation.get("hypothesis_id"),
            },
            "report": report,
            "outputs": outputs,
        }

    def _load_operations(self, config: QuantitativeDiscoveryLabConfig) -> pd.DataFrame:
        result_dir = self._base_dir / "optimization" / "results"
        frames: list[pd.DataFrame] = []
        for path in sorted(result_dir.glob(config.operations_csv_glob)):
            frame = pd.read_csv(path)
            if frame.empty:
                continue
            frames.append(frame)

        if not frames:
            return pd.DataFrame()

        combined = pd.concat(frames, ignore_index=True)
        return combined.drop_duplicates(subset=[c for c in ["operation_id", "execution_id", "entry_time", "exit_time", "entry_price", "exit_price", "pnl"] if c in combined.columns]).reset_index(drop=True)

    def _initialize_incremental_state(self, config: QuantitativeDiscoveryLabConfig) -> dict[str, Any]:
        t0 = time.perf_counter()
        run_id = config.resume_run_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        root = self._base_dir / "optimization" / "results" / "quantitative_discovery_chunks" / run_id
        events_dir = root / "events"
        stats_dir = root / "stats"
        features_dir = root / "features"
        root.mkdir(parents=True, exist_ok=True)
        events_dir.mkdir(parents=True, exist_ok=True)
        stats_dir.mkdir(parents=True, exist_ok=True)
        features_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = root / "manifest.json"

        if manifest_path.exists():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        else:
            manifest = {
                "run_id": run_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "blocks": {},
                "config": {
                    "chunk_days": config.chunk_days,
                    "chunk_overlap_bars": config.chunk_overlap_bars,
                    "historical_breakout_window": config.historical_breakout_window,
                    "historical_horizon_bars": config.historical_horizon_bars,
                },
            }
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

        return {
            "run_id": run_id,
            "root": root,
            "events_dir": events_dir,
            "stats_dir": stats_dir,
            "features_dir": features_dir,
            "manifest_path": manifest_path,
            "manifest": manifest,
            "init_seconds": time.perf_counter() - t0,
        }

    def _load_historical_events_incremental(
        self,
        config: QuantitativeDiscoveryLabConfig,
        state: dict[str, Any],
        profile: dict[str, float],
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        if not config.include_historical_candles:
            return pd.DataFrame(), {"enabled": False}

        chunk_summaries: list[dict[str, Any]] = []
        candles_processed = 0
        events_processed = 0
        events_files: list[Path] = []

        t_pairs = time.perf_counter()
        with get_session() as session:
            pairs = session.execute(
                select(Candle.symbol, Candle.timeframe).distinct().order_by(Candle.symbol, Candle.timeframe)
            ).all()

        if config.symbols:
            allowed_symbols = {str(item) for item in config.symbols}
            pairs = [(symbol, timeframe) for symbol, timeframe in pairs if str(symbol) in allowed_symbols]
        if config.timeframes:
            allowed_timeframes = {str(item) for item in config.timeframes}
            pairs = [(symbol, timeframe) for symbol, timeframe in pairs if str(timeframe) in allowed_timeframes]
        profile["db_pair_discovery"] += time.perf_counter() - t_pairs

        for symbol, timeframe in pairs:
            windows = self._build_chunk_windows(symbol, timeframe, config, profile)
            for idx, window in enumerate(windows, start=1):
                block_key = f"{symbol}|{timeframe}|{window['chunk_start']}|{window['chunk_end']}"
                if self._block_already_completed(state, block_key):
                    restored = self._restore_block_summary(state, block_key)
                    if restored:
                        chunk_summaries.append(restored)
                        candles_processed += int(restored.get("candles", 0))
                        events_processed += int(restored.get("events", 0))
                        events_path = state["events_dir"] / restored.get("events_file", "")
                        if events_path.exists():
                            events_files.append(events_path)
                    continue

                summary = self._process_single_chunk(
                    symbol=symbol,
                    timeframe=timeframe,
                    window=window,
                    block_index=idx,
                    config=config,
                    state=state,
                    profile=profile,
                )
                chunk_summaries.append(summary)
                candles_processed += int(summary.get("candles", 0))
                events_processed += int(summary.get("events", 0))
                events_path = state["events_dir"] / summary.get("events_file", "")
                if events_path.exists():
                    events_files.append(events_path)

        t_agg = time.perf_counter()
        frames: list[pd.DataFrame] = []
        dedupe_applied = False
        dedupe_strategy = "none"
        for path in events_files:
            frame = self._read_events_csv_compact(path)
            if frame.empty:
                continue
            frames.append(frame)
        if frames:
            combined = pd.concat(frames, ignore_index=True)
            dedupe_cols = [col for col in ["operation_id", "entry_time", "symbol", "timeframe", "direction", "regime"] if col in combined.columns]
            # Full-base runs can exceed available memory when factorizing tens of millions of rows.
            if dedupe_cols and len(combined) <= 2_000_000:
                combined = combined.drop_duplicates(subset=dedupe_cols)
                dedupe_applied = True
                dedupe_strategy = "global_drop_duplicates"
            else:
                dedupe_strategy = "skipped_large_dataset"
            historical = combined.reset_index(drop=True)
        else:
            historical = pd.DataFrame()
            dedupe_strategy = "no_frames"
        profile["events_aggregation"] += time.perf_counter() - t_agg

        chunk_audit = {
            "enabled": True,
            "run_id": state["run_id"],
            "blocks": len(chunk_summaries),
            "candles_processed": int(candles_processed),
            "events_before_dedup": int(events_processed),
            "events_after_dedup": int(len(historical)),
            "dedupe_applied": dedupe_applied,
            "dedupe_strategy": dedupe_strategy,
            "events_per_asset_avg": self._avg_per_key(historical, "symbol"),
            "events_per_timeframe_avg": self._avg_per_key(historical, "timeframe"),
            "chunk_time_seconds": [round(float(item.get("total_seconds", 0.0)), 6) for item in chunk_summaries],
            "chunk_summaries": chunk_summaries,
        }
        return historical, chunk_audit

    def _read_events_csv_compact(self, path: Path) -> pd.DataFrame:
        header = pd.read_csv(path, nrows=0)
        available_columns = set(str(col) for col in header.columns)
        usecols = [col for col in header.columns if str(col) in EVENTS_COMPACT_COLUMNS]

        if not usecols:
            return pd.DataFrame()

        dtype_map = {key: value for key, value in EVENTS_COMPACT_DTYPES.items() if key in available_columns}

        return pd.read_csv(
            path,
            usecols=usecols,
            dtype=dtype_map,
            low_memory=True,
        )

    def _build_chunk_windows(
        self,
        symbol: str,
        timeframe: str,
        config: QuantitativeDiscoveryLabConfig,
        profile: dict[str, float],
    ) -> list[dict[str, Any]]:
        t0 = time.perf_counter()
        with get_session() as session:
            min_time, max_time = session.execute(
                select(Candle.open_time, Candle.open_time)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.open_time.asc())
                .limit(1)
            ).first() or (None, None)

            if min_time is None:
                profile["db_window_discovery"] += time.perf_counter() - t0
                return []

            last_time = session.execute(
                select(Candle.open_time)
                .where(Candle.symbol == symbol, Candle.timeframe == timeframe)
                .order_by(Candle.open_time.desc())
                .limit(1)
            ).scalar_one_or_none()

        profile["db_window_discovery"] += time.perf_counter() - t0
        if last_time is None:
            return []

        windows: list[dict[str, Any]] = []
        cursor = min_time
        step = timedelta(days=max(int(config.chunk_days), 1))
        overlap_minutes = max(int(config.chunk_overlap_bars), 0) * max(self._timeframe_to_minutes(timeframe), 1)
        overlap_delta = timedelta(minutes=overlap_minutes)
        while cursor <= last_time:
            chunk_end = min(cursor + step, last_time + timedelta(minutes=max(self._timeframe_to_minutes(timeframe), 1)))
            query_start = cursor - overlap_delta
            query_end = chunk_end + overlap_delta
            windows.append(
                {
                    "chunk_start": cursor.isoformat(),
                    "chunk_end": chunk_end.isoformat(),
                    "query_start": query_start.isoformat(),
                    "query_end": query_end.isoformat(),
                }
            )
            cursor = chunk_end
        return windows

    def _process_single_chunk(
        self,
        symbol: str,
        timeframe: str,
        window: dict[str, Any],
        block_index: int,
        config: QuantitativeDiscoveryLabConfig,
        state: dict[str, Any],
        profile: dict[str, float],
    ) -> dict[str, Any]:
        block_started = time.perf_counter()
        query_start = datetime.fromisoformat(window["query_start"])
        query_end = datetime.fromisoformat(window["query_end"])
        chunk_start = self._to_utc_timestamp(window["chunk_start"])
        chunk_end = self._to_utc_timestamp(window["chunk_end"])

        t_db = time.perf_counter()
        with get_session() as session:
            rows = session.execute(
                select(Candle.open_time, Candle.open, Candle.high, Candle.low, Candle.close, Candle.volume)
                .where(
                    Candle.symbol == symbol,
                    Candle.timeframe == timeframe,
                    Candle.open_time >= query_start,
                    Candle.open_time < query_end,
                )
                .order_by(Candle.open_time.asc())
            ).all()
        profile["db_chunk_read"] += time.perf_counter() - t_db

        t_load = time.perf_counter()
        candles = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume"])
        candles["symbol"] = symbol
        candles["timeframe"] = timeframe
        profile["candle_loading"] += time.perf_counter() - t_load

        t_mine = time.perf_counter()
        events = self._build_candle_events(candles, config)
        profile["event_mining"] += time.perf_counter() - t_mine

        t_filter = time.perf_counter()
        if not events.empty:
            events["entry_time"] = pd.to_datetime(events["entry_time"], utc=True, errors="coerce")
            events = events[(events["entry_time"] >= chunk_start) & (events["entry_time"] < chunk_end)].copy()
        profile["event_filtering"] += time.perf_counter() - t_filter

        t_persist = time.perf_counter()
        safe_symbol = self._safe_token(symbol)
        safe_timeframe = self._safe_token(timeframe)
        block_id = f"{safe_symbol}_{safe_timeframe}_{block_index:04d}"
        events_file = f"events_{block_id}.csv"
        stats_file = f"stats_{block_id}.json"
        features_file = f"features_{block_id}.csv"

        if config.persist_intermediate:
            events_path = state["events_dir"] / events_file
            stats_path = state["stats_dir"] / stats_file
            features_path = state["features_dir"] / features_file
            events.to_csv(events_path, index=False)

            feature_cols = [
                col
                for col in [
                    "operation_id",
                    "symbol",
                    "timeframe",
                    "entry_time",
                    "regime",
                    "direction",
                    "entry_session",
                    "weekday",
                    "atr_bucket",
                    "rsi_bucket",
                    "volume_bucket",
                    "bollinger_position",
                    "trend_bucket",
                    "pnl",
                    "pnl_percent",
                ]
                if col in events.columns
            ]
            if feature_cols:
                events[feature_cols].to_csv(features_path, index=False)
            else:
                pd.DataFrame().to_csv(features_path, index=False)

            stats_payload = {
                "symbol": symbol,
                "timeframe": timeframe,
                "block_index": block_index,
                "chunk_start": window["chunk_start"],
                "chunk_end": window["chunk_end"],
                "candles": int(len(candles)),
                "events": int(len(events)),
                "events_by_regime": events["regime"].astype(str).value_counts().to_dict() if not events.empty else {},
                "events_by_direction": events["direction"].astype(str).value_counts().to_dict() if not events.empty else {},
            }
            stats_path.write_text(json.dumps(stats_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        profile["intermediate_persistence"] += time.perf_counter() - t_persist

        summary = {
            "symbol": symbol,
            "timeframe": timeframe,
            "block_index": block_index,
            "chunk_start": window["chunk_start"],
            "chunk_end": window["chunk_end"],
            "candles": int(len(candles)),
            "events": int(len(events)),
            "events_file": events_file,
            "stats_file": stats_file,
            "features_file": features_file,
            "total_seconds": round(time.perf_counter() - block_started, 6),
        }
        self._mark_block_completed(state, summary)
        return summary

    def _block_already_completed(self, state: dict[str, Any], block_key: str) -> bool:
        blocks = state.get("manifest", {}).get("blocks", {})
        return bool(blocks.get(block_key, {}).get("completed"))

    def _mark_block_completed(self, state: dict[str, Any], summary: dict[str, Any]) -> None:
        block_key = f"{summary['symbol']}|{summary['timeframe']}|{summary['chunk_start']}|{summary['chunk_end']}"
        manifest = state["manifest"]
        manifest.setdefault("blocks", {})[block_key] = {
            "completed": True,
            "summary": summary,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        state["manifest_path"].write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    def _restore_block_summary(self, state: dict[str, Any], block_key: str) -> dict[str, Any] | None:
        entry = state.get("manifest", {}).get("blocks", {}).get(block_key)
        if not entry:
            return None
        return entry.get("summary")

    def _avg_per_key(self, df: pd.DataFrame, column: str) -> float:
        if df.empty or column not in df.columns:
            return 0.0
        counts = df[column].astype(str).value_counts(dropna=False)
        if counts.empty:
            return 0.0
        return round(float(counts.mean()), 6)

    def _to_utc_timestamp(self, value: Any) -> pd.Timestamp:
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            return ts.tz_localize("UTC")
        return ts.tz_convert("UTC")

    def _safe_token(self, value: Any) -> str:
        text = str(value)
        for char in ["/", "\\", ":", " ", "|", "*"]:
            text = text.replace(char, "_")
        return text

    def _profiling_payload(self, profile: dict[str, float], elapsed_total: float) -> dict[str, Any]:
        ordered = sorted(profile.items(), key=lambda item: item[1], reverse=True)
        return {
            "total_seconds": round(elapsed_total, 6),
            "stages": [{"stage": name, "seconds": round(value, 6)} for name, value in ordered],
            "bottleneck_ranking": [name for name, _ in ordered],
        }

    def _load_previous_monolithic_reference(self, current_run_id: str) -> dict[str, Any] | None:
        result_dir = self._base_dir / "optimization" / "results"
        files = sorted(result_dir.glob("quantitative_discovery_lab_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files:
            if current_run_id in path.name:
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            config = payload.get("config", {})
            if "chunk_days" in config:
                continue
            return {
                "path": str(path),
                "dataset_size": int(payload.get("dataset_size", 0)),
                "cluster_count": len(payload.get("cluster_metrics", [])),
                "hypothesis_count": len(payload.get("hypotheses", [])),
                "h1": (payload.get("hypotheses", []) or [{}])[0],
                "generated_at": payload.get("generated_at"),
                "elapsed_seconds": payload.get("profiling", {}).get("total_seconds"),
            }
        return None

    def _build_equivalence_check(self, old_reference: dict[str, Any] | None, combined_snapshot: dict[str, Any]) -> dict[str, Any]:
        current_h1 = self._hypothesis_focus(combined_snapshot)
        if not old_reference:
            return {
                "comparable": False,
                "reason": "No previous monolithic report found for direct equivalence check.",
                "current_h1": current_h1,
            }

        old_h1 = old_reference.get("h1", {})
        return {
            "comparable": True,
            "old_report": old_reference.get("path"),
            "h1_same_family": old_h1.get("family") == (current_h1 or {}).get("family"),
            "h1_same_cluster": old_h1.get("cluster_id") == (current_h1 or {}).get("cluster_id"),
            "delta_confidence": round(float((current_h1 or {}).get("confidence") or 0.0) - float(old_h1.get("confidence") or 0.0), 6),
            "delta_priority": round(float((current_h1 or {}).get("priority") or 0.0) - float(old_h1.get("priority") or 0.0), 6),
            "delta_sample_size": int((current_h1 or {}).get("sample_size") or 0) - int(old_h1.get("sample_size") or 0),
            "old_h1": old_h1,
            "current_h1": current_h1,
        }

    def _build_scalability_audit(
        self,
        profile: dict[str, float],
        chunk_audit: dict[str, Any],
        total_candles: int,
        total_events: int,
        optimizer_events: int,
        combined_events: int,
        elapsed_total: float,
        peak_memory_bytes: int,
        old_reference: dict[str, Any] | None,
    ) -> dict[str, Any]:
        chunk_times = [float(value) for value in chunk_audit.get("chunk_time_seconds", [])]
        monolithic_runtime = None if old_reference is None else old_reference.get("elapsed_seconds")
        return {
            "candles_processed": int(total_candles),
            "events_mined": int(total_events),
            "events_added_vs_optimizer": int(total_events - optimizer_events),
            "events_aggregated_total": int(combined_events),
            "average_events_per_asset": float(chunk_audit.get("events_per_asset_avg", 0.0)),
            "average_events_per_timeframe": float(chunk_audit.get("events_per_timeframe_avg", 0.0)),
            "chunk_time_seconds_avg": round(float(statistics.mean(chunk_times)) if chunk_times else 0.0, 6),
            "chunk_time_seconds_max": round(float(max(chunk_times)) if chunk_times else 0.0, 6),
            "peak_memory_mb": round(peak_memory_bytes / (1024 * 1024), 6),
            "elapsed_seconds": round(elapsed_total, 6),
            "monolithic_elapsed_seconds": monolithic_runtime,
            "time_gain_seconds_vs_monolithic": None if monolithic_runtime is None else round(monolithic_runtime - elapsed_total, 6),
            "bottleneck_ranking": [name for name, _ in sorted(profile.items(), key=lambda item: item[1], reverse=True)],
        }

    def _build_candle_events(self, candles: pd.DataFrame, config: QuantitativeDiscoveryLabConfig) -> pd.DataFrame:
        work = candles.copy().reset_index(drop=True)
        if work.empty:
            return work

        for column in ["open_time", "open", "high", "low", "close", "volume"]:
            if column in work.columns and column == "open_time":
                work[column] = pd.to_datetime(work[column], utc=True, errors="coerce")
            elif column in work.columns:
                work[column] = pd.to_numeric(work[column], errors="coerce")

        work["close_prev"] = work["close"].shift(1)
        work["true_range"] = pd.concat(
            [
                (work["high"] - work["low"]),
                (work["high"] - work["close_prev"]).abs(),
                (work["low"] - work["close_prev"]).abs(),
            ],
            axis=1,
        ).max(axis=1)
        work["atr"] = work["true_range"].rolling(window=14, min_periods=14).mean()
        work["atr_pct"] = (work["atr"] / work["close"]).replace([float("inf"), -float("inf")], pd.NA) * 100.0
        work["ema_fast"] = work["close"].ewm(span=12, adjust=False).mean()
        work["ema_slow"] = work["close"].ewm(span=26, adjust=False).mean()
        work["trend_score"] = ((work["ema_fast"] - work["ema_slow"]) / work["ema_slow"].replace(0, pd.NA)) * 100.0
        work["trend_score"] = work["trend_score"].fillna(0.0)
        work["entry_trend_regime"] = work["trend_score"].apply(self._trend_regime_from_score)
        work["entry_vol_regime"] = pd.cut(
            pd.to_numeric(work["atr_pct"], errors="coerce").fillna(0.0),
            bins=[-float("inf"), 0.4, 1.2, float("inf")],
            labels=["low_volatility", "normal_volatility", "high_volatility"],
            include_lowest=True,
        ).astype(str)
        work["entry_atr_pct"] = work["atr_pct"]
        work["hour"] = work["open_time"].dt.hour.fillna(-1).astype(int)
        work["weekday"] = work["open_time"].dt.day_name().fillna("Unknown")
        work["entry_session"] = work["hour"].apply(self._hour_bucket)
        work["day_type"] = work["weekday"].apply(lambda value: "weekend" if value in {"Saturday", "Sunday"} else "weekday")
        work["distance_to_ema_pct"] = ((work["close"] - work["ema_slow"]) / work["ema_slow"].replace(0, pd.NA)) * 100.0
        rolling_mean = work["close"].rolling(window=20, min_periods=20).mean()
        rolling_std = work["close"].rolling(window=20, min_periods=20).std(ddof=0)
        upper_band = rolling_mean + 2.0 * rolling_std
        lower_band = rolling_mean - 2.0 * rolling_std
        band_span = (upper_band - lower_band).replace(0, pd.NA)
        work["bollinger_position"] = pd.Series(["unknown"] * len(work), index=work.index)
        work.loc[work["close"] > upper_band, "bollinger_position"] = "above_upper"
        work.loc[work["close"].between(lower_band, upper_band, inclusive="both"), "bollinger_position"] = "inside_band"
        work.loc[work["close"] < lower_band, "bollinger_position"] = "below_lower"
        delta = work["close"].diff()
        gain = delta.clip(lower=0.0)
        loss = -delta.clip(upper=0.0)
        avg_gain = gain.rolling(window=14, min_periods=14).mean()
        avg_loss = loss.rolling(window=14, min_periods=14).mean().replace(0, pd.NA)
        rs = avg_gain / avg_loss
        rsi = 100.0 - (100.0 / (1.0 + rs))
        work["rsi_bucket"] = rsi.apply(self._rsi_bucket)
        work["relative_volume"] = work["volume"] / work["volume"].rolling(window=20, min_periods=20).mean().replace(0, pd.NA)
        work["volume_bucket"] = pd.cut(
            pd.to_numeric(work["relative_volume"], errors="coerce").fillna(1.0),
            bins=[-float("inf"), 0.9, 1.1, float("inf")],
            labels=["low_volume", "normal_volume", "high_volume"],
            include_lowest=True,
        ).astype(str)
        work["distance_to_close_pct"] = 0.0
        work["trend_bucket"] = pd.cut(
            work["trend_score"].abs().fillna(0.0),
            bins=[-float("inf"), 0.15, 0.4, float("inf")],
            labels=["flat", "moderate_trend", "strong_trend"],
            include_lowest=True,
        ).astype(str)

        lookback = max(int(config.historical_breakout_window), 5)
        horizon = max(int(config.historical_horizon_bars), 3)
        work["prev_high"] = work["high"].shift(1).rolling(window=lookback, min_periods=lookback).max()
        work["prev_low"] = work["low"].shift(1).rolling(window=lookback, min_periods=lookback).min()
        work["future_close"] = work["close"].shift(-horizon)
        future_high = work["high"].shift(-1).rolling(window=horizon, min_periods=horizon).max().shift(-(horizon - 1))
        future_low = work["low"].shift(-1).rolling(window=horizon, min_periods=horizon).min().shift(-(horizon - 1))
        work["future_high"] = future_high
        work["future_low"] = future_low
        work["future_return"] = (work["future_close"] - work["close"]) / work["close"].replace(0, pd.NA) * 100.0
        work["future_upside"] = (work["future_high"] - work["close"]) / work["close"].replace(0, pd.NA) * 100.0
        work["future_downside"] = (work["close"] - work["future_low"]) / work["close"].replace(0, pd.NA) * 100.0
        work["breakout_up"] = work["close"] > work["prev_high"]
        work["breakout_down"] = work["close"] < work["prev_low"]
        work["compression"] = work["atr_pct"] <= work["atr_pct"].rolling(window=lookback, min_periods=lookback).median()
        work["expansion"] = (work["future_upside"].abs() + work["future_downside"].abs()) > work["atr_pct"].fillna(0.0)

        event_frames: list[pd.DataFrame] = []
        event_frames.append(self._build_event_subset(work, work["breakout_up"], "rompimento", "BUY", "breakout_up"))
        event_frames.append(self._build_event_subset(work, work["breakout_down"], "rompimento", "SELL", "breakout_down"))
        event_frames.append(self._build_event_subset(work, work["breakout_up"] & (work["future_return"] <= 0), "falso_rompimento", "BUY", "false_breakout_up"))
        event_frames.append(self._build_event_subset(work, work["breakout_down"] & (work["future_return"] >= 0), "falso_rompimento", "SELL", "false_breakout_down"))
        event_frames.append(self._build_event_subset(work, work["compression"] & work["breakout_up"], "compressao", "BUY", "compression_breakout_up"))
        event_frames.append(self._build_event_subset(work, work["compression"] & work["breakout_down"], "compressao", "SELL", "compression_breakout_down"))
        event_frames.append(self._build_event_subset(work, (work["trend_score"] > 0) & (work["future_return"] > 0), "continuacao", "BUY", "continuation_up"))
        event_frames.append(self._build_event_subset(work, (work["trend_score"] < 0) & (work["future_return"] < 0), "continuacao", "SELL", "continuation_down"))
        event_frames.append(self._build_event_subset(work, (work["trend_score"] < 0) & (work["future_return"] > 0), "reversao", "BUY", "reversal_up"))
        event_frames.append(self._build_event_subset(work, (work["trend_score"] > 0) & (work["future_return"] < 0), "reversao", "SELL", "reversal_down"))

        event_frames = [frame for frame in event_frames if not frame.empty]
        if not event_frames:
            return pd.DataFrame()

        events = pd.concat(event_frames, ignore_index=True)
        dedupe_cols = [col for col in ["operation_id", "entry_time", "symbol", "timeframe", "direction", "regime"] if col in events.columns]
        if dedupe_cols:
            events = events.drop_duplicates(subset=dedupe_cols)
        return events.reset_index(drop=True)

    def _build_event_subset(self, work: pd.DataFrame, mask: pd.Series, regime: str, direction: str, event_type: str) -> pd.DataFrame:
        event_columns = [
            "symbol",
            "timeframe",
            "open_time",
            "close",
            "high",
            "low",
            "volume",
            "atr",
            "atr_pct",
            "ema_slow",
            "trend_score",
            "entry_trend_regime",
            "entry_vol_regime",
            "entry_atr_pct",
            "hour",
            "weekday",
            "entry_session",
            "day_type",
            "distance_to_ema_pct",
            "bollinger_position",
            "rsi_bucket",
            "relative_volume",
            "volume_bucket",
            "trend_bucket",
            "breakout_up",
            "breakout_down",
            "future_close",
            "future_high",
            "future_low",
            "future_return",
            "future_upside",
            "future_downside",
        ]
        subset = work.loc[mask, [col for col in event_columns if col in work.columns]].copy()
        if subset.empty:
            return subset

        subset["operation_id"] = (
            "hist_"
            + subset["symbol"].astype(str)
            + "_"
            + subset["timeframe"].astype(str)
            + "_"
            + pd.to_datetime(subset["open_time"], utc=True, errors="coerce").dt.strftime("%Y%m%d%H%M%S")
            + "_"
            + event_type
        )
        subset["execution_id"] = None
        subset["strategy"] = "historical_candle_discovery"
        subset["parameters_json"] = None
        subset["entry_time"] = subset["open_time"]
        horizon_minutes = subset["timeframe"].map(self._timeframe_to_minutes).fillna(0).astype(float) * 12.0
        subset["exit_time"] = subset["entry_time"] + pd.to_timedelta(horizon_minutes, unit="m")
        subset["entry_price"] = subset["close"]
        subset["exit_price"] = subset["future_close"]
        if direction == "SELL":
            subset["stop_loss"] = subset["entry_price"] + (subset["atr"].fillna(0.0) * 1.5)
            subset["take_profit"] = subset["entry_price"] - (subset["atr"].fillna(0.0) * 2.0)
        else:
            subset["stop_loss"] = subset["entry_price"] - (subset["atr"].fillna(0.0) * 1.5)
            subset["take_profit"] = subset["entry_price"] + (subset["atr"].fillna(0.0) * 2.0)
        subset["risk_reward"] = (subset["take_profit"] - subset["entry_price"]).abs() / (subset["entry_price"] - subset["stop_loss"]).abs().replace(0, pd.NA)
        subset["quantity"] = 1.0
        if direction == "BUY":
            subset["pnl"] = subset["future_close"] - subset["entry_price"]
        else:
            subset["pnl"] = subset["entry_price"] - subset["future_close"]
        subset["pnl_percent"] = (subset["pnl"] / subset["entry_price"].replace(0, pd.NA)) * 100.0
        subset["duration_minutes"] = horizon_minutes
        subset["exit_reason"] = event_type
        subset["entry_candle_close"] = subset["close"]
        subset["entry_trend_regime"] = subset["entry_trend_regime"]
        subset["entry_vol_regime"] = subset["entry_vol_regime"]
        subset["entry_trend_score"] = subset["trend_score"]
        subset["entry_is_breakout"] = subset["breakout_up"] | subset["breakout_down"]
        subset["first_move"] = subset["future_return"]
        subset["final_move"] = subset["future_return"]
        subset["mfe"] = subset["future_upside"] if direction == "BUY" else subset["future_downside"]
        subset["mae"] = subset["future_downside"] if direction == "BUY" else subset["future_upside"]
        subset["ganho_rapido"] = subset["pnl"] > 0
        subset["perda_rapida"] = subset["pnl"] < 0
        subset["stop_curto"] = subset["mae"] > (subset["atr_pct"].fillna(0.0) * 1.5)
        subset["take_atingido"] = subset["mfe"] > subset["mae"]
        subset["stop_por_volatilidade"] = subset["atr_pct"].fillna(0.0) > subset["atr_pct"].median(skipna=True)
        subset["reversao"] = regime == "reversao"
        subset["continuacao"] = regime == "continuacao"
        subset["falso_rompimento"] = regime == "falso_rompimento"
        subset["rompimento"] = regime == "rompimento"
        subset["regime_tendencia_forte_alta"] = subset["entry_trend_regime"] == "trend_forte_alta"
        subset["regime_tendencia_moderada_alta"] = subset["entry_trend_regime"] == "trend_moderada_alta"
        subset["regime_tendencia_forte_baixa"] = subset["entry_trend_regime"] == "trend_forte_baixa"
        subset["regime_tendencia_moderada_baixa"] = subset["entry_trend_regime"] == "trend_moderada_baixa"
        subset["regime_consolidacao"] = subset["entry_trend_regime"] == "consolidacao"
        subset["regime_alta_volatilidade"] = subset["entry_vol_regime"] == "high_volatility"
        subset["regime_baixa_volatilidade"] = subset["entry_vol_regime"] == "low_volatility"
        subset["regime_rompimento"] = regime == "rompimento"
        subset["regime_falso_rompimento"] = regime == "falso_rompimento"
        subset["primary_regime"] = regime
        subset["primary_profile"] = event_type
        subset["win_flag"] = subset["pnl"] > 0
        subset["loss_flag"] = subset["pnl"] < 0
        subset["hour"] = subset["entry_time"].dt.hour.fillna(-1).astype(int)
        subset["weekday"] = subset["entry_time"].dt.day_name().fillna("Unknown")
        subset["entry_session"] = subset["hour"].apply(self._hour_bucket)
        subset["regime"] = regime
        subset["vol_bucket"] = subset["entry_vol_regime"]
        subset["direction"] = direction
        subset["distance_to_ema_pct"] = ((subset["entry_price"] - subset["ema_slow"]) / subset["ema_slow"].replace(0, pd.NA)) * 100.0
        subset["bollinger_position"] = subset["bollinger_position"]
        subset["rsi_bucket"] = subset["rsi_bucket"]
        subset["atr_bucket"] = pd.cut(
            pd.to_numeric(subset["entry_atr_pct"], errors="coerce").fillna(0.0),
            bins=[-float("inf"), 0.4, 1.2, float("inf")],
            labels=["low_atr", "mid_atr", "high_atr"],
            include_lowest=True,
        ).astype(str)
        subset["relative_volume"] = subset["relative_volume"]
        subset["volume_bucket"] = subset["volume_bucket"]
        subset["distance_to_close_pct"] = 0.0
        subset["day_type"] = subset["weekday"].apply(lambda value: "weekend" if value in {"Saturday", "Sunday"} else "weekday")
        subset["trend_bucket"] = subset["trend_bucket"]
        subset["event_source"] = "historical_candles"
        subset["event_type"] = event_type
        return subset

    def _analysis_snapshot(
        self,
        df: pd.DataFrame,
        config: QuantitativeDiscoveryLabConfig,
        profile: dict[str, float] | None = None,
        stage_prefix: str = "combined",
    ) -> dict[str, Any]:
        t_prepare = time.perf_counter()
        enriched = self._prepare_dataset(df)
        if profile is not None:
            profile[f"{stage_prefix}_prepare_dataset"] += time.perf_counter() - t_prepare

        t_feature = time.perf_counter()
        feature_summary = self._feature_importance(enriched, top_n=config.top_feature_count)
        if profile is not None:
            profile[f"{stage_prefix}_feature_importance"] += time.perf_counter() - t_feature

        t_cluster = time.perf_counter()
        clusters = self._cluster_operations(enriched, min_cluster_size=config.min_cluster_size)
        if profile is not None:
            profile[f"{stage_prefix}_clusterization"] += time.perf_counter() - t_cluster

        t_cluster_metrics = time.perf_counter()
        cluster_metrics = self._cluster_metrics(enriched, clusters)
        if profile is not None:
            profile[f"{stage_prefix}_cluster_metrics"] += time.perf_counter() - t_cluster_metrics

        t_hyp = time.perf_counter()
        hypotheses = self._generate_hypotheses(enriched, clusters, cluster_metrics, min_trades=config.min_hypothesis_trades)
        ranked_hypotheses = self._rank_hypotheses(hypotheses)
        if profile is not None:
            profile[f"{stage_prefix}_hypothesis_generation"] += time.perf_counter() - t_hyp

        t_pattern = time.perf_counter()
        recommendation = self._recommend_next_family(ranked_hypotheses)
        patterns = self._pattern_summary(enriched)
        if profile is not None:
            profile[f"{stage_prefix}_pattern_report"] += time.perf_counter() - t_pattern
        return {
            "dataset": enriched,
            "feature_summary": feature_summary,
            "clusters": clusters,
            "cluster_metrics": cluster_metrics,
            "hypotheses": hypotheses,
            "ranked_hypotheses": ranked_hypotheses,
            "recommendation": recommendation,
            "patterns": patterns,
        }

    def _snapshot_payload(self, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        return {
            "dataset_size": int(len(snapshot.get("dataset", []))),
            "feature_importance_count": len(snapshot.get("feature_summary", [])),
            "cluster_count": len(snapshot.get("cluster_metrics", [])),
            "hypothesis_count": len(snapshot.get("ranked_hypotheses", [])),
            "recommended_family": snapshot.get("recommendation", {}).get("family"),
            "recommended_hypothesis": snapshot.get("recommendation", {}).get("hypothesis_id"),
        }

    def _compare_h1_snapshots(
        self,
        operations_snapshot: dict[str, Any] | None,
        historical_snapshot: dict[str, Any] | None,
        combined_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "previous_run_h1": self._hypothesis_focus(operations_snapshot),
            "historical_h1": self._hypothesis_focus(historical_snapshot),
            "combined_h1": self._hypothesis_focus(combined_snapshot),
        }

    def _hypothesis_focus(self, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if not snapshot:
            return None
        ranked = snapshot.get("ranked_hypotheses", [])
        if not ranked:
            return None
        best = ranked[0]
        return {
            "hypothesis_id": best.get("hypothesis_id"),
            "family": best.get("family"),
            "cluster_id": best.get("cluster_id"),
            "confidence": best.get("confidence"),
            "priority": best.get("priority"),
            "sample_size": best.get("sample_size"),
            "statement": best.get("statement"),
        }

    def _trend_regime_from_score(self, score: Any) -> str:
        try:
            value = float(score)
        except Exception:
            return "consolidacao"
        if value >= 0.6:
            return "trend_forte_alta"
        if value >= 0.2:
            return "trend_moderada_alta"
        if value <= -0.6:
            return "trend_forte_baixa"
        if value <= -0.2:
            return "trend_moderada_baixa"
        return "consolidacao"

    def _timeframe_to_minutes(self, timeframe: Any) -> int:
        if timeframe is None:
            return 0
        value = str(timeframe).strip().lower()
        if value.endswith("m"):
            try:
                return int(value[:-1])
            except Exception:
                return 0
        if value.endswith("h"):
            try:
                return int(value[:-1]) * 60
            except Exception:
                return 0
        if value.endswith("d"):
            try:
                return int(value[:-1]) * 1440
            except Exception:
                return 0
        return 0

    def _prepare_dataset(self, df: pd.DataFrame) -> pd.DataFrame:
        # Avoid deep copies for multi-million-row runs; we only need a mutable working view.
        work = df.copy(deep=False)

        def _series_or_default(column: str, default_value: Any) -> pd.Series:
            if column in work.columns:
                return work[column]
            return pd.Series([default_value] * len(work), index=work.index)

        for col in ["entry_time", "exit_time", "timestamp"]:
            if col in work.columns:
                work[col] = pd.to_datetime(work[col], utc=True, errors="coerce")

        numeric_cols = [
            "entry_price",
            "exit_price",
            "stop_loss",
            "take_profit",
            "risk_reward",
            "quantity",
            "pnl",
            "pnl_percent",
            "duration_minutes",
            "score",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "ema_fast",
            "ema_slow",
            "trend_score",
            "entry_trend_score",
            "entry_atr_pct",
            "mfe",
            "mae",
            "first_move",
            "final_move",
            "capture_ratio",
            "mfe_used_pct",
        ]
        for col in numeric_cols:
            if col in work.columns:
                work[col] = pd.to_numeric(work[col], errors="coerce", downcast="float")

        if "duration_minutes" in work.columns:
            missing = work["duration_minutes"].isna() & work["entry_time"].notna() & work["exit_time"].notna()
            work.loc[missing, "duration_minutes"] = (
                (work.loc[missing, "exit_time"] - work.loc[missing, "entry_time"]).dt.total_seconds() / 60.0
            )

        if "symbol" not in work.columns:
            work["symbol"] = "UNKNOWN"
        if "timeframe" not in work.columns:
            work["timeframe"] = "UNKNOWN"
        if "strategy" not in work.columns:
            work["strategy"] = "UNKNOWN"
        if "pnl" not in work.columns:
            work["pnl"] = 0.0

        work["pnl"] = pd.to_numeric(work["pnl"], errors="coerce").fillna(0.0)
        work["win_flag"] = work["pnl"] > 0
        work["loss_flag"] = work["pnl"] < 0
        work["hour"] = work["entry_time"].dt.hour.fillna(-1).astype(int)
        work["weekday"] = work["entry_time"].dt.day_name().fillna("Unknown")
        work["entry_session"] = work["hour"].apply(self._hour_bucket)
        work["regime"] = _series_or_default("primary_regime", "unknown").fillna("unknown")
        if "primary_regime" not in work.columns:
            work["regime"] = _series_or_default("entry_trend_regime", "unknown").fillna("unknown")
        work["vol_bucket"] = _series_or_default("entry_vol_regime", "unknown").fillna("unknown")
        work["direction"] = _series_or_default("direction", None)
        if work["direction"].isna().all():
            work["direction"] = _series_or_default("side", "BUY")
        work["direction"] = work["direction"].fillna("BUY")

        if "entry_price" in work.columns and "ema_slow" in work.columns:
            denom = work["ema_slow"].replace(0, pd.NA)
            work["distance_to_ema_pct"] = ((work["entry_price"] - work["ema_slow"]) / denom) * 100.0
        elif "entry_price" in work.columns and "entry_candle_close" in work.columns:
            denom = work["entry_candle_close"].replace(0, pd.NA)
            work["distance_to_ema_pct"] = ((work["entry_price"] - work["entry_candle_close"]) / denom) * 100.0
        else:
            work["distance_to_ema_pct"] = pd.NA

        if "entry_price" in work.columns and "close" in work.columns:
            ratio = work["entry_price"] / work["close"].replace(0, pd.NA)
            work["bollinger_position"] = pd.cut(
                ratio.fillna(1.0),
                bins=[-float("inf"), 0.985, 1.015, float("inf")],
                labels=["below_lower", "inside_band", "above_upper"],
                include_lowest=True,
            ).astype(str)
        else:
            work["bollinger_position"] = "unknown"

        if "rsi_max" in work.columns:
            rsi_source = work["rsi_max"]
        elif "rsi" in work.columns:
            rsi_source = work["rsi"]
        else:
            rsi_source = pd.Series([pd.NA] * len(work), index=work.index)
        work["rsi_bucket"] = rsi_source.apply(self._rsi_bucket)

        if "entry_atr_pct" in work.columns:
            work["atr_bucket"] = pd.cut(
                pd.to_numeric(work["entry_atr_pct"], errors="coerce").fillna(0.0),
                bins=[-float("inf"), 0.0015, 0.0035, float("inf")],
                labels=["low_atr", "mid_atr", "high_atr"],
                include_lowest=True,
            ).astype(str)
        elif "atr" in work.columns:
            work["atr_bucket"] = pd.cut(
                pd.to_numeric(work["atr"], errors="coerce").fillna(0.0),
                bins=[-float("inf"), 50, 200, float("inf")],
                labels=["low_atr", "mid_atr", "high_atr"],
                include_lowest=True,
            ).astype(str)
        else:
            work["atr_bucket"] = "unknown"

        if "volume" in work.columns and "volume" in work.columns:
            vol = pd.to_numeric(work["volume"], errors="coerce")
            med = float(vol.median(skipna=True)) if vol.notna().any() else 1.0
            work["relative_volume"] = (vol / med).replace([pd.NA, pd.NaT], pd.NA)
        else:
            work["relative_volume"] = pd.NA

        work["volume_bucket"] = pd.cut(
            pd.to_numeric(work["relative_volume"], errors="coerce").fillna(1.0),
            bins=[-float("inf"), 0.9, 1.1, float("inf")],
            labels=["low_volume", "normal_volume", "high_volume"],
            include_lowest=True,
        ).astype(str)

        if "entry_candle_close" in work.columns and "entry_price" in work.columns:
            diff = (pd.to_numeric(work["entry_price"], errors="coerce") - pd.to_numeric(work["entry_candle_close"], errors="coerce")).abs()
            denom = pd.to_numeric(work["entry_candle_close"], errors="coerce").replace(0, pd.NA)
            work["distance_to_close_pct"] = (diff / denom) * 100.0
        else:
            work["distance_to_close_pct"] = pd.NA

        work["day_type"] = work["weekday"].apply(lambda value: "weekend" if value in {"Saturday", "Sunday"} else "weekday")
        work["trend_bucket"] = pd.cut(
            pd.to_numeric(_series_or_default("entry_trend_score", 0.0), errors="coerce").fillna(0.0).abs(),
            bins=[-float("inf"), 0.001, 0.003, float("inf")],
            labels=["flat", "moderate_trend", "strong_trend"],
            include_lowest=True,
        ).astype(str)

        return work.reset_index(drop=True)

    def _feature_importance(self, df: pd.DataFrame, top_n: int) -> list[dict[str, Any]]:
        candidates = {
            "primary_regime": df["regime"],
            "entry_trend_regime": df.get("entry_trend_regime", pd.Series(["unknown"] * len(df))),
            "entry_vol_regime": df.get("entry_vol_regime", pd.Series(["unknown"] * len(df))),
            "trend_bucket": df["trend_bucket"],
            "atr_bucket": df["atr_bucket"],
            "volume_bucket": df["volume_bucket"],
            "rsi_bucket": df["rsi_bucket"],
            "bollinger_position": df["bollinger_position"],
            "weekday": df["weekday"],
            "entry_session": df["entry_session"],
            "direction": df["direction"],
            "day_type": df["day_type"],
            "hour": df["hour"].astype(str),
            "distance_to_ema_pct_bucket": pd.cut(
                pd.to_numeric(df["distance_to_ema_pct"], errors="coerce").fillna(0.0),
                bins=[-float("inf"), -1.0, 1.0, float("inf")],
                labels=["below_ema", "near_ema", "far_from_ema"],
                include_lowest=True,
            ).astype(str),
        }

        rows: list[dict[str, Any]] = []
        for feature, series in candidates.items():
            s = series.astype(str).fillna("unknown")
            summary = self._categorical_edge_summary(df, s, feature)
            rows.append(summary)

        rows.sort(key=lambda item: (item["importance_score"], item["lift_win_rate"], item["profit_factor"], item["sample_size"]), reverse=True)
        return rows[:top_n]

    def _categorical_edge_summary(self, df: pd.DataFrame, series: pd.Series, feature_name: str) -> dict[str, Any]:
        total_wins = max(int((df["pnl"] > 0).sum()), 1)
        total_losses = max(int((df["pnl"] < 0).sum()), 1)

        grouped = []
        for value, group in df.groupby(series, dropna=False, sort=False, observed=True):
            pnl = pd.to_numeric(group["pnl"], errors="coerce").fillna(0.0)
            wins = pnl[pnl > 0]
            losses = pnl[pnl < 0]
            gross_profit = float(wins.sum())
            gross_loss = float(abs(losses.sum()))
            profit_factor = profit_factor_from_pnl(pnl)
            win_rate = float((pnl > 0).mean() * 100.0)
            expectancy = expectancy_from_pnl(pnl)
            drawdown = max_drawdown_from_pnl(pnl)
            support = int(len(group))
            win_share = float(len(wins) / total_wins * 100.0)
            loss_share = float(len(losses) / total_losses * 100.0)
            lift_win_rate = win_rate - float((df["pnl"] > 0).mean() * 100.0)
            importance_score = self._importance_score(support, win_rate, expectancy, profit_factor, drawdown)

            grouped.append(
                {
                    "feature": feature_name,
                    "value": str(value),
                    "sample_size": support,
                    "win_rate": round(win_rate, 6),
                    "expectancy": round(expectancy, 6),
                    "profit_factor": round(float(profit_factor), 6) if math.isfinite(float(profit_factor)) else None,
                    "drawdown": round(drawdown, 6),
                    "win_share_pct": round(win_share, 6),
                    "loss_share_pct": round(loss_share, 6),
                    "lift_win_rate": round(lift_win_rate, 6),
                    "importance_score": round(importance_score, 6),
                }
            )

        if not grouped:
            return {
                "feature": feature_name,
                "best_value": None,
                "sample_size": 0,
                "win_rate": 0.0,
                "expectancy": 0.0,
                "profit_factor": None,
                "drawdown": 0.0,
                "lift_win_rate": 0.0,
                "importance_score": 0.0,
            }

        grouped.sort(key=lambda item: (item["importance_score"], item["lift_win_rate"], item["sample_size"]), reverse=True)
        best = grouped[0]
        best["all_values"] = [dict(item) for item in grouped[:5]]
        return best

    def _importance_score(self, sample_size: int, win_rate: float, expectancy: float, profit_factor: float, drawdown: float) -> float:
        pf_score = 0.0 if not math.isfinite(profit_factor) else min(max((profit_factor - 0.9) / 2.0, 0.0), 1.0)
        wr_score = min(max(win_rate / 100.0, 0.0), 1.0)
        exp_score = min(max((expectancy + 1.0) / 2.0, 0.0), 1.0)
        dd_score = 1.0 - min(max(abs(drawdown), 0.0), 1.0)
        support_score = min(sample_size / 100.0, 1.0)
        return 0.28 * pf_score + 0.24 * wr_score + 0.18 * exp_score + 0.18 * dd_score + 0.12 * support_score

    def _cluster_operations(self, df: pd.DataFrame, min_cluster_size: int) -> dict[str, Any]:
        work = df
        work["cluster_key"] = (
            work.get("regime", pd.Series(["unknown"] * len(work), index=work.index)).astype(str)
            + "|"
            + work.get("atr_bucket", pd.Series(["unknown"] * len(work), index=work.index)).astype(str)
            + "|"
            + work.get("rsi_bucket", pd.Series(["unknown"] * len(work), index=work.index)).astype(str)
            + "|"
            + work.get("volume_bucket", pd.Series(["unknown"] * len(work), index=work.index)).astype(str)
            + "|"
            + work.get("bollinger_position", pd.Series(["unknown"] * len(work), index=work.index)).astype(str)
            + "|"
            + work.get("direction", pd.Series(["unknown"] * len(work), index=work.index)).astype(str)
        )

        counts = work["cluster_key"].value_counts().to_dict()
        labels: dict[str, str] = {}
        taxonomy = ["trend_forte", "reversao", "lateral", "alta_volatilidade", "compressao", "breakout"]
        for idx, (key, count) in enumerate(sorted(counts.items(), key=lambda item: item[1], reverse=True), start=1):
            labels[key] = taxonomy[(idx - 1) % len(taxonomy)] + f"_{idx}"

        cluster_rows: list[dict[str, Any]] = []
        for key, group in work.groupby("cluster_key", dropna=False, sort=False, observed=True):
            if len(group) < min_cluster_size:
                continue
            pnl = pd.to_numeric(group["pnl"], errors="coerce").fillna(0.0)
            cluster_rows.append(
                {
                    "cluster_id": labels.get(key, key),
                    "cluster_key": key,
                    "trades": int(len(group)),
                    "regime": self._mode_or_unknown(group["regime"]),
                    "atr_bucket": self._mode_or_unknown(group["atr_bucket"]),
                    "rsi_bucket": self._mode_or_unknown(group["rsi_bucket"]),
                    "volume_bucket": self._mode_or_unknown(group["volume_bucket"]),
                    "bollinger_position": self._mode_or_unknown(group["bollinger_position"]),
                    "direction": self._mode_or_unknown(group["direction"]),
                    "sample_share_pct": round(len(group) / max(len(work), 1) * 100.0, 6),
                }
            )

        work["cluster_id"] = work["cluster_key"].map(labels).fillna("misc")
        return {"clusters": cluster_rows}

    def _cluster_metrics(self, df: pd.DataFrame, clusters: dict[str, Any]) -> list[dict[str, Any]]:
        if "cluster_id" not in df.columns:
            return []
        rows: list[dict[str, Any]] = []
        for cluster_id, group in df.groupby("cluster_id", dropna=False, sort=False, observed=True):
            pnl = pd.to_numeric(group["pnl"], errors="coerce").fillna(0.0)
            profit_factor = profit_factor_from_pnl(pnl)
            rows.append(
                {
                    "cluster_id": str(cluster_id),
                    "trades": int(len(group)),
                    "profit_factor": round(float(profit_factor), 6) if math.isfinite(float(profit_factor)) else None,
                    "sharpe": round(sharpe_from_pnl(pnl), 6),
                    "win_rate": round(float((pnl > 0).mean() * 100.0), 6),
                    "expectancy": round(expectancy_from_pnl(pnl), 6),
                    "drawdown": round(max_drawdown_from_pnl(pnl), 6),
                    "net_profit": round(float(pnl.sum()), 6),
                }
            )

        rows.sort(key=lambda item: (item["profit_factor"] or 0.0, item["sharpe"], item["expectancy"], -item["drawdown"]), reverse=True)
        return rows

    def _generate_hypotheses(
        self,
        df: pd.DataFrame,
        clusters: dict[str, Any],
        cluster_metrics: list[dict[str, Any]],
        min_trades: int,
    ) -> list[dict[str, Any]]:
        hypotheses: list[dict[str, Any]] = []
        top_clusters = [item for item in cluster_metrics if item.get("trades", 0) >= min_trades]

        if "cluster_id" not in df.columns:
            return [self._fallback_hypothesis(df)]

        for idx, cluster in enumerate(top_clusters, start=1):
            cluster_id = cluster["cluster_id"]
            subset = df[df["cluster_id"] == cluster_id]
            if subset.empty:
                continue

            top_patterns = self._top_patterns_for_subset(subset)
            evidence = self._hypothesis_evidence(subset)
            family = self._family_from_cluster(subset, top_patterns)
            confidence = self._confidence_from_subset(subset, cluster)
            priority = self._priority_from_cluster(cluster, confidence, len(top_patterns))

            hypotheses.append(
                {
                    "hypothesis_id": f"H{idx}",
                    "family": family,
                    "cluster_id": cluster_id,
                    "statement": self._statement_for_family(family, top_patterns),
                    "justification": self._justification_for_cluster(subset, cluster, top_patterns),
                    "evidence": evidence,
                    "confidence": round(confidence, 6),
                    "priority": round(priority, 6),
                    "sample_size": int(len(subset)),
                    "cluster_metrics": cluster,
                    "top_patterns": top_patterns,
                }
            )

        if not hypotheses:
            fallback = self._fallback_hypothesis(df)
            hypotheses.append(fallback)

        return hypotheses

    def _top_patterns_for_subset(self, subset: pd.DataFrame) -> list[dict[str, Any]]:
        patterns = []
        categorical_cols = ["regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "weekday", "entry_session", "direction", "day_type"]
        for feature in categorical_cols:
            s = subset[feature].astype(str).fillna("unknown")
            summary = self._categorical_edge_summary(subset, s, feature)
            patterns.append(summary)
        patterns.sort(key=lambda item: (item["importance_score"], item["lift_win_rate"], item["sample_size"]), reverse=True)
        return patterns[:5]

    def _hypothesis_evidence(self, subset: pd.DataFrame) -> dict[str, Any]:
        pnl = pd.to_numeric(subset["pnl"], errors="coerce").fillna(0.0)
        return {
            "trades": int(len(subset)),
            "profit_factor": round(float(profit_factor_from_pnl(pnl)), 6) if math.isfinite(float(profit_factor_from_pnl(pnl))) else None,
            "sharpe": round(sharpe_from_pnl(pnl), 6),
            "win_rate": round(float((pnl > 0).mean() * 100.0), 6),
            "expectancy": round(expectancy_from_pnl(pnl), 6),
            "drawdown": round(max_drawdown_from_pnl(pnl), 6),
            "net_profit": round(float(pnl.sum()), 6),
            "avg_mfe": round(float(pd.to_numeric(subset.get("mfe", pd.Series(dtype=float)), errors="coerce").mean()), 6) if "mfe" in subset.columns else None,
            "avg_mae": round(float(pd.to_numeric(subset.get("mae", pd.Series(dtype=float)), errors="coerce").mean()), 6) if "mae" in subset.columns else None,
        }

    def _family_from_cluster(self, subset: pd.DataFrame, patterns: list[dict[str, Any]]) -> str:
        primary = Counter(subset["regime"].astype(str).fillna("unknown")).most_common(1)
        if primary:
            regime = primary[0][0]
            if "rompimento" in regime or any("breakout" in str(p.get("feature")) for p in patterns):
                return "BreakoutNextGen"
            if "reversao" in regime or any("reversao" in str(p.get("value")) for p in patterns):
                return "ReversalEdge"
            if "tendencia" in regime:
                return "TrendContinuation"
            if "consolidacao" in regime:
                return "CompressionMeanReversion"
        return "QuantEdgeCore"

    def _statement_for_family(self, family: str, patterns: list[dict[str, Any]]) -> str:
        best = patterns[0] if patterns else {"feature": "unknown", "value": "unknown"}
        if family == "BreakoutNextGen":
            return f"Operar rompimentos quando {best['feature']} = {best['value']} e o cluster mostra edge consistente."
        if family == "ReversalEdge":
            return f"Operar reversao apenas sob a configuracao estatisticamente forte de {best['feature']} = {best['value']}."
        if family == "TrendContinuation":
            return f"Operar continuacao apenas em tendencia validada com {best['feature']} = {best['value']}."
        if family == "CompressionMeanReversion":
            return f"Operar reversao em compressao quando {best['feature']} = {best['value']} e o retorno medio compensa o drawdown."
        return f"Explorar padrao quantitativo dominante com {best['feature']} = {best['value']}."

    def _justification_for_cluster(self, subset: pd.DataFrame, cluster: dict[str, Any], patterns: list[dict[str, Any]]) -> str:
        top = patterns[0] if patterns else {}
        return (
            f"Cluster {cluster['cluster_id']} com {cluster['trades']} trades, PF={cluster.get('profit_factor')}, "
            f"Sharpe={cluster.get('sharpe')}, WR={cluster.get('win_rate')}%. "
            f"Melhor padrao: {top.get('feature')}={top.get('value')}"
        )

    def _confidence_from_subset(self, subset: pd.DataFrame, cluster: dict[str, Any]) -> float:
        trades = max(int(len(subset)), 1)
        wr = float((subset["pnl"] > 0).mean())
        pf = float(cluster.get("profit_factor") or 0.0)
        sharp = float(cluster.get("sharpe") or 0.0)
        support = min(trades / 100.0, 1.0)
        pf_score = min(max((pf - 1.0) / 2.0, 0.0), 1.0)
        sharpe_score = min(max((sharp + 1.0) / 2.0, 0.0), 1.0)
        return 0.35 * support + 0.25 * pf_score + 0.20 * wr + 0.20 * sharpe_score

    def _priority_from_cluster(self, cluster: dict[str, Any], confidence: float, pattern_count: int) -> float:
        trades = float(cluster.get("trades", 0))
        pf = float(cluster.get("profit_factor") or 0.0)
        sharpe = float(cluster.get("sharpe") or 0.0)
        drawdown = float(cluster.get("drawdown") or 0.0)
        support = min(trades / 100.0, 1.0)
        complexity_penalty = min(pattern_count / 5.0, 1.0)
        return 0.30 * confidence + 0.25 * min(max((pf - 1.0) / 2.5, 0.0), 1.0) + 0.20 * min(max((sharpe + 1.0) / 2.0, 0.0), 1.0) + 0.15 * support + 0.10 * (1.0 - min(abs(drawdown), 1.0)) - 0.05 * complexity_penalty

    def _fallback_hypothesis(self, df: pd.DataFrame) -> dict[str, Any]:
        pnl = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0)
        return {
            "hypothesis_id": "H1",
            "family": "QuantEdgeCore",
            "cluster_id": "misc",
            "statement": "Nao foi possivel confirmar um padrao dominante; usar o conjunto de features com maior consistencia estatistica.",
            "justification": "A amostra ainda nao formou um cluster suficientemente forte para aprovacao direta.",
            "evidence": self._hypothesis_evidence(df),
            "confidence": round(max(float((pnl > 0).mean()), 0.0), 6),
            "priority": 0.0,
            "sample_size": int(len(df)),
            "cluster_metrics": {
                "trades": int(len(df)),
                "profit_factor": None,
                "sharpe": round(sharpe_from_pnl(pnl), 6),
                "win_rate": round(float((pnl > 0).mean() * 100.0), 6),
                "expectancy": round(expectancy_from_pnl(pnl), 6),
                "drawdown": round(max_drawdown_from_pnl(pnl), 6),
                "net_profit": round(float(pnl.sum()), 6),
            },
            "top_patterns": self._top_patterns_for_subset(df),
        }

    def _rank_hypotheses(self, hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        ranked = sorted(
            hypotheses,
            key=lambda item: (
                float(item.get("priority", 0.0)),
                float(item.get("confidence", 0.0)),
                float(item.get("evidence", {}).get("profit_factor") or 0.0),
                float(item.get("sample_size", 0)),
            ),
            reverse=True,
        )
        for idx, item in enumerate(ranked, start=1):
            item["rank"] = idx
        return ranked

    def _recommend_next_family(self, ranked_hypotheses: list[dict[str, Any]]) -> dict[str, Any]:
        if not ranked_hypotheses:
            return {"family": None, "hypothesis_id": None, "reason": "Sem hipoteses disponiveis."}
        best = ranked_hypotheses[0]
        return {
            "family": best.get("family"),
            "hypothesis_id": best.get("hypothesis_id"),
            "reason": best.get("justification"),
            "confidence": best.get("confidence"),
            "priority": best.get("priority"),
        }

    def _pattern_summary(self, df: pd.DataFrame) -> dict[str, Any]:
        wins = df[df["pnl"] > 0].copy()
        losses = df[df["pnl"] < 0].copy()
        return {
            "winning_patterns": self._top_pattern_blocks(wins),
            "losing_patterns": self._top_pattern_blocks(losses),
        }

    def _top_pattern_blocks(self, subset: pd.DataFrame) -> list[dict[str, Any]]:
        if subset.empty:
            return []
        fields = ["regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "weekday", "entry_session", "direction"]
        blocks: list[dict[str, Any]] = []
        for field in fields:
            counts = subset[field].astype(str).value_counts(dropna=False).head(3)
            for value, count in counts.items():
                blocks.append({"feature": field, "value": str(value), "count": int(count), "share_pct": round(count / len(subset) * 100.0, 6)})
        blocks.sort(key=lambda item: item["count"], reverse=True)
        return blocks[:12]

    def _build_report(
        self,
        operations: pd.DataFrame,
        feature_summary: list[dict[str, Any]],
        clusters: dict[str, Any],
        cluster_metrics: list[dict[str, Any]],
        hypotheses: list[dict[str, Any]],
        recommendation: dict[str, Any],
        patterns: dict[str, Any],
        source_breakdown: dict[str, Any],
        h1_comparison: dict[str, Any],
    ) -> str:
        lines = [
            "# Quantitative Strategy Discovery Lab",
            "",
            f"Generated at: {datetime.now(timezone.utc).isoformat()}",
            f"Operations analyzed: {len(operations)}",
            f"Source breakdown: {source_breakdown}",
            f"H1 comparison: {h1_comparison}",
            "",
            "## 1. Winning patterns",
        ]
        for item in patterns.get("winning_patterns", [])[:12]:
            lines.append(f"- {item['feature']}={item['value']} | count={item['count']} | share={item['share_pct']:.2f}%")
        lines.append("")
        lines.append("## 2. Losing patterns")
        for item in patterns.get("losing_patterns", [])[:12]:
            lines.append(f"- {item['feature']}={item['value']} | count={item['count']} | share={item['share_pct']:.2f}%")
        lines.append("")
        lines.append("## 3. Feature importance")
        for item in feature_summary:
            lines.append(
                f"- {item['feature']} => {item.get('value')} | score={item.get('importance_score')} | PF={item.get('profit_factor')} | WR={item.get('win_rate')} | support={item.get('sample_size')}"
            )
        lines.append("")
        lines.append("## 4. Clusters")
        for item in cluster_metrics:
            lines.append(
                f"- {item['cluster_id']} | trades={item['trades']} | PF={item['profit_factor']} | Sharpe={item['sharpe']} | WR={item['win_rate']} | Expectancy={item['expectancy']} | DD={item['drawdown']}"
            )
        lines.append("")
        lines.append("## 5. Hypothesis ranking")
        for item in hypotheses:
            lines.append(
                f"- #{item['rank']} {item['hypothesis_id']} | family={item['family']} | priority={item['priority']} | confidence={item['confidence']} | sample={item['sample_size']} | statement={item['statement']}"
            )
        lines.append("")
        lines.append("## 6. Recommended next family")
        lines.append(f"- family: {recommendation.get('family')}")
        lines.append(f"- hypothesis: {recommendation.get('hypothesis_id')}")
        lines.append(f"- reason: {recommendation.get('reason')}")
        return "\n".join(lines) + "\n"

    def _write_outputs(
        self,
        payload: dict[str, Any],
        operations: pd.DataFrame,
        feature_summary: list[dict[str, Any]],
        clusters: dict[str, Any],
        cluster_metrics: list[dict[str, Any]],
        hypotheses: list[dict[str, Any]],
    ) -> dict[str, str]:
        out = self._base_dir / "optimization" / "results"
        out.mkdir(parents=True, exist_ok=True)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = out / f"quantitative_discovery_lab_{stamp}.json"
        md_path = out / f"quantitative_discovery_lab_{stamp}.md"
        ops_csv = out / f"quantitative_discovery_operations_{stamp}.csv"
        feature_csv = out / f"quantitative_discovery_feature_importance_{stamp}.csv"
        cluster_csv = out / f"quantitative_discovery_clusters_{stamp}.csv"
        hypothesis_csv = out / f"quantitative_discovery_hypotheses_{stamp}.csv"

        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        operations.to_csv(ops_csv, index=False)
        pd.DataFrame(feature_summary).to_csv(feature_csv, index=False)
        pd.DataFrame(cluster_metrics).to_csv(cluster_csv, index=False)
        pd.DataFrame(hypotheses).to_csv(hypothesis_csv, index=False)
        md_path.write_text(payload.get("executive_report", ""), encoding="utf-8")

        return {
            "report_json": str(json_path),
            "report_markdown": str(md_path),
            "operations_csv": str(ops_csv),
            "feature_importance_csv": str(feature_csv),
            "cluster_metrics_csv": str(cluster_csv),
            "hypotheses_csv": str(hypothesis_csv),
        }

    def _hour_bucket(self, hour: int) -> str:
        if 6 <= hour < 10:
            return "open_asia"
        if 10 <= hour < 14:
            return "europe"
        if 14 <= hour < 18:
            return "us_open"
        if 18 <= hour < 22:
            return "us_late"
        return "overnight"

    def _rsi_bucket(self, value: Any) -> str:
        try:
            rsi = float(value)
        except Exception:
            return "unknown"
        if math.isnan(rsi):
            return "unknown"
        if rsi < 30:
            return "oversold"
        if rsi > 70:
            return "overbought"
        return "neutral"

    def _mode_or_unknown(self, series: pd.Series) -> str:
        clean = series.astype(str).replace({"nan": "unknown"}).dropna()
        if clean.empty:
            return "unknown"
        return str(clean.mode().iloc[0])

def run_quantitative_discovery_lab(base_dir: Path) -> dict[str, Any]:
    return QuantitativeDiscoveryLab(base_dir=base_dir).run()