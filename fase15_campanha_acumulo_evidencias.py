from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import text

from database.connection import get_session
from database.history_models import ScientificReadinessHistory
from database.history_service import HistoryPersistenceService
from fase14_validacao_estabilidade_temporal_edge import _enrich_all_trades
from fase14_validacao_estabilidade_temporal_edge import _historical_window_table
from fase14_validacao_estabilidade_temporal_edge import _load_all_trades
from fase14_validacao_estabilidade_temporal_edge import _load_candle_coverage
from investigacao_cdb_edge_loss import _classify_regimes


@dataclass(frozen=True)
class Config:
    strategy_key: str = "ClassicDonchianBreakout@v1.0"
    target_trades: tuple[int, ...] = (30, 50, 100)
    readiness_temporal_days_target: int = 180
    regime_shift_window: int = 30
    persistent_pf_window: int = 30
    persistent_pf_threshold: float = 0.90


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if not math.isfinite(out):
            return default
        return out
    except Exception:
        return default


def _profit_factor_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = abs(float(pnl[pnl < 0].sum()))
    if gross_loss <= 0.0:
        return 999.0 if gross_profit > 0.0 else 0.0
    return gross_profit / gross_loss


def _latest_file(results_dir: Path, pattern: str) -> Path:
    matches = sorted(results_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
    if not matches:
        raise RuntimeError(f"Nenhum arquivo encontrado para o padrao: {pattern}")
    return matches[-1]


def _load_baseline_matrix(results_dir: Path) -> pd.DataFrame:
    matrix_path = _latest_file(results_dir, "fase14b_consolidacao_evidencias_*_master_edge_matrix.csv")
    return pd.read_csv(matrix_path)


def _load_scientific_snapshot_audit(strategy_key: str) -> pd.DataFrame:
    query = text(
        """
        SELECT
            trade_history_id,
            entry_reason,
            market_regime,
            indicator_context_json,
            snapshot_complete,
            missing_fields_json
        FROM scientific_trade_snapshots
        WHERE strategy = :strategy
          AND trade_history_id IS NOT NULL
        """
    )
    try:
        with get_session() as session:
            rows = session.execute(query, {"strategy": strategy_key}).mappings().all()
    except Exception:
        return pd.DataFrame(columns=["trade_history_id", "entry_reason_snapshot", "market_regime_snapshot", "snapshot_has_indicator_context", "snapshot_complete", "missing_fields_json"])
    out = pd.DataFrame([dict(row) for row in rows])
    if out.empty:
        return pd.DataFrame(columns=["trade_history_id", "entry_reason_snapshot", "market_regime_snapshot", "snapshot_has_indicator_context", "snapshot_complete", "missing_fields_json"])
    out = out.rename(
        columns={
            "entry_reason": "entry_reason_snapshot",
            "market_regime": "market_regime_snapshot",
        }
    )
    out["snapshot_has_indicator_context"] = out["indicator_context_json"].fillna("{}").astype(str).str.strip().ne("{}").astype(bool)
    out["snapshot_complete"] = out["snapshot_complete"].fillna(False).astype(bool)
    return out[["trade_history_id", "entry_reason_snapshot", "market_regime_snapshot", "snapshot_has_indicator_context", "snapshot_complete", "missing_fields_json"]]


def _field_presence_summary(enriched: pd.DataFrame, snapshot_audit: pd.DataFrame) -> pd.DataFrame:
    merged = enriched.merge(snapshot_audit, left_on="id", right_on="trade_history_id", how="left") if not snapshot_audit.empty else enriched.copy()
    required_fields = [
        ("entry_time", "trade_history", "obrigatorio"),
        ("exit_time", "trade_history", "obrigatorio"),
        ("symbol", "trade_history", "obrigatorio"),
        ("timeframe", "trade_history", "obrigatorio"),
        ("entry_reason", "scientific_trade_snapshots", "obrigatorio"),
        ("exit_reason", "trade_history", "obrigatorio"),
        ("pnl", "trade_history", "obrigatorio"),
        ("pnl_percent", "trade_history", "obrigatorio"),
        ("mfe", "enriquecido", "obrigatorio_para_investigacao"),
        ("mae", "enriquecido", "obrigatorio_para_investigacao"),
        ("snap_ema_fast", "indicator_snapshots", "obrigatorio_para_contexto"),
        ("snap_ema_slow", "indicator_snapshots", "obrigatorio_para_contexto"),
        ("snap_ema_trend", "indicator_snapshots", "obrigatorio_para_contexto"),
        ("snap_rsi", "indicator_snapshots", "obrigatorio_para_contexto"),
        ("snap_atr", "indicator_snapshots", "obrigatorio_para_contexto"),
        ("snap_volume", "indicator_snapshots", "obrigatorio_para_contexto"),
        ("market_regime", "signal_snapshots", "obrigatorio_para_contexto"),
        ("trend_regime", "enriquecido", "contexto_investigativo"),
        ("vol_regime", "enriquecido", "contexto_investigativo"),
        ("compression_regime", "enriquecido", "contexto_investigativo"),
    ]

    rows: list[dict[str, Any]] = []
    total = max(1, len(enriched))
    for field, source, criticality in required_fields:
        if field == "entry_reason":
            exists = "entry_reason_snapshot" in merged.columns
            null_count = int(merged.get("entry_reason_snapshot", pd.Series([None] * len(merged))).isna().sum()) if exists else total
        elif field == "market_regime":
            exists = (field in merged.columns) or ("market_regime_snapshot" in merged.columns)
            base = merged[field] if field in merged.columns else pd.Series([None] * len(merged))
            snap = merged.get("market_regime_snapshot", pd.Series([None] * len(merged)))
            null_count = int(((base.fillna("").astype(str).str.strip() == "") & (snap.fillna("").astype(str).str.strip() == "")).sum()) if exists else total
        elif field.startswith("snap_"):
            exists = field in merged.columns
            base = merged[field] if exists else pd.Series([None] * len(merged))
            snap_ctx = merged.get("snapshot_has_indicator_context", pd.Series([False] * len(merged))).astype(bool)
            null_count = int((base.isna() & (~snap_ctx)).sum()) if exists else total
        else:
            exists = field in merged.columns
            null_count = int(merged[field].isna().sum()) if exists else total
        rows.append(
            {
                "field": field,
                "source": source,
                "criticality": criticality,
                "exists": bool(exists),
                "missing_count": null_count,
                "missing_pct": 100.0 * null_count / total,
                "status": "missing_schema" if not exists else ("complete" if null_count == 0 else "partial"),
            }
        )
    return pd.DataFrame(rows)


def _duplicate_flags(trades: pd.DataFrame) -> pd.Series:
    return trades.duplicated(subset=["execution_id", "symbol", "timeframe", "entry_time", "side"], keep=False)


def _integrity_trade_history(enriched: pd.DataFrame, snapshot_audit: pd.DataFrame) -> pd.DataFrame:
    df = enriched.merge(snapshot_audit, left_on="id", right_on="trade_history_id", how="left") if not snapshot_audit.empty else enriched.copy()
    if "entry_reason_snapshot" not in df.columns:
        df["entry_reason_snapshot"] = pd.Series([None] * len(df))
    if "snapshot_complete" not in df.columns:
        df["snapshot_complete"] = False
    if "market_regime_snapshot" not in df.columns:
        df["market_regime_snapshot"] = pd.Series([None] * len(df))
    if "snapshot_has_indicator_context" not in df.columns:
        df["snapshot_has_indicator_context"] = False
    df["duplicate_trade"] = _duplicate_flags(df)
    df["missing_exit_time"] = df["exit_time"].isna()
    df["missing_exit_reason"] = df["exit_reason"].isna() | (df["exit_reason"].astype(str).str.strip() == "")
    df["missing_signal_snapshot"] = df["signal_id"].isna()
    base_market_missing = df["market_regime"].isna() | (df["market_regime"].astype(str).str.strip() == "")
    snapshot_market_missing = df.get("market_regime_snapshot", pd.Series([None] * len(df))).fillna("").astype(str).str.strip() == ""
    df["missing_market_regime"] = base_market_missing & snapshot_market_missing
    legacy_context_missing = df[["snap_ema_fast", "snap_ema_slow", "snap_ema_trend", "snap_rsi", "snap_atr", "snap_volume"]].isna().any(axis=1)
    snapshot_context_missing = ~df.get("snapshot_has_indicator_context", pd.Series([False] * len(df))).astype(bool)
    df["missing_indicator_context"] = legacy_context_missing & snapshot_context_missing
    df["missing_entry_reason"] = df.get("entry_reason_snapshot", pd.Series([None] * len(df))).isna()
    df["missing_scientific_snapshot"] = df.get("snapshot_complete", pd.Series([False] * len(df))).fillna(False).astype(bool) == False
    df["missing_mfe_mae"] = df[["mfe", "mae"]].isna().any(axis=1)
    df["invalid_timestamp_order"] = pd.to_datetime(df["exit_time"], utc=True) < pd.to_datetime(df["entry_time"], utc=True)
    df["invalid_price"] = (
        pd.to_numeric(df["entry_price"], errors="coerce").fillna(0.0) <= 0.0
    ) | (
        pd.to_numeric(df["exit_price"], errors="coerce").fillna(0.0) <= 0.0
    )
    df["invalid_quantity"] = pd.to_numeric(df["quantity"], errors="coerce").fillna(0.0) <= 0.0
    df["invalid_duration"] = pd.to_numeric(df["duration_minutes"], errors="coerce").fillna(-1.0) < 0.0
    df["invalid_pnl_return_pair"] = (
        pd.to_numeric(df["pnl"], errors="coerce").isna()
        | pd.to_numeric(df["pnl_percent"], errors="coerce").isna()
    )
    df["has_integrity_issue"] = df[
        [
            "duplicate_trade",
            "missing_exit_time",
            "missing_exit_reason",
            "missing_signal_snapshot",
            "missing_entry_reason",
            "missing_scientific_snapshot",
            "missing_market_regime",
            "missing_indicator_context",
            "missing_mfe_mae",
            "invalid_timestamp_order",
            "invalid_price",
            "invalid_quantity",
            "invalid_duration",
            "invalid_pnl_return_pair",
        ]
    ].any(axis=1)
    return df[
        [
            "id",
            "execution_id",
            "symbol",
            "timeframe",
            "entry_time",
            "exit_time",
            "exit_reason",
            "pnl",
            "pnl_percent",
            "mfe",
            "mae",
            "market_regime",
            "entry_reason_snapshot",
            "snapshot_complete",
            "duplicate_trade",
            "missing_exit_time",
            "missing_exit_reason",
            "missing_signal_snapshot",
            "missing_entry_reason",
            "missing_scientific_snapshot",
            "missing_market_regime",
            "missing_indicator_context",
            "missing_mfe_mae",
            "invalid_timestamp_order",
            "invalid_price",
            "invalid_quantity",
            "invalid_duration",
            "invalid_pnl_return_pair",
            "has_integrity_issue",
        ]
    ].copy()


def _data_quality_summary(integrity_df: pd.DataFrame, field_summary: pd.DataFrame) -> dict[str, Any]:
    total = max(1, len(integrity_df))
    issue_counts = {
        "duplicate_trade": int(integrity_df["duplicate_trade"].sum()),
        "missing_exit_time": int(integrity_df["missing_exit_time"].sum()),
        "missing_exit_reason": int(integrity_df["missing_exit_reason"].sum()),
        "missing_signal_snapshot": int(integrity_df["missing_signal_snapshot"].sum()),
        "missing_market_regime": int(integrity_df["missing_market_regime"].sum()),
        "missing_indicator_context": int(integrity_df["missing_indicator_context"].sum()),
        "missing_mfe_mae": int(integrity_df["missing_mfe_mae"].sum()),
        "invalid_timestamp_order": int(integrity_df["invalid_timestamp_order"].sum()),
        "invalid_price": int(integrity_df["invalid_price"].sum()),
        "invalid_quantity": int(integrity_df["invalid_quantity"].sum()),
        "invalid_duration": int(integrity_df["invalid_duration"].sum()),
        "invalid_pnl_return_pair": int(integrity_df["invalid_pnl_return_pair"].sum()),
    }
    missing_schema = field_summary[field_summary["status"] == "missing_schema"]
    return {
        "total_trades": int(len(integrity_df)),
        "trades_with_any_issue": int(integrity_df["has_integrity_issue"].sum()),
        "issue_rate_pct": 100.0 * float(integrity_df["has_integrity_issue"].mean()),
        "field_missing_in_schema": missing_schema["field"].astype(str).tolist(),
        "issue_counts": issue_counts,
        "integrity_ratio": max(0.0, 1.0 - (int(integrity_df["has_integrity_issue"].sum()) / total)),
    }


def _sample_growth_dashboard(enriched: pd.DataFrame, baseline: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    window_table = _historical_window_table(enriched, _load_candle_coverage(enriched))
    baseline_cols = baseline[["symbol", "timeframe", "scientific_classification", "research_priority"]].copy()
    dashboard = window_table.merge(baseline_cols, on=["symbol", "timeframe"], how="left")
    for target in cfg.target_trades:
        dashboard[f"trades_to_{target}"] = (target - pd.to_numeric(dashboard["trades"], errors="coerce").fillna(0)).clip(lower=0)
        dashboard[f"pct_to_{target}"] = (100.0 * pd.to_numeric(dashboard["trades"], errors="coerce").fillna(0) / target).clip(upper=100.0)
    dashboard["trades_per_day"] = pd.to_numeric(dashboard["trades"], errors="coerce").fillna(0.0) / dashboard["trade_window_days"].replace(0, np.nan)
    dashboard["trades_per_day"] = dashboard["trades_per_day"].replace([np.inf, -np.inf], np.nan).fillna(0.0)
    for target in cfg.target_trades:
        gap = dashboard[f"trades_to_{target}"]
        rate = dashboard["trades_per_day"].replace(0.0, np.nan)
        dashboard[f"estimated_days_to_{target}"] = (gap / rate).replace([np.inf, -np.inf], np.nan)
    return dashboard.sort_values(["pct_to_30", "trades"], ascending=[False, False]).reset_index(drop=True)


def _criterion_recent_pf(enriched: pd.DataFrame, window: int, threshold: float) -> dict[str, Any]:
    ordered = enriched.sort_values("exit_time").reset_index(drop=True)
    if len(ordered) < window * 2:
        return {
            "evaluable": False,
            "criterion_met": False,
            "reason": f"menos_de_{window * 2}_trades_globais_para_duas_janelas",
        }
    last_a = ordered.iloc[-window:]
    last_b = ordered.iloc[-window * 2 : -window]
    pf_a = _profit_factor_from_pnl(pd.to_numeric(last_a["pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float))
    pf_b = _profit_factor_from_pnl(pd.to_numeric(last_b["pnl"], errors="coerce").fillna(0.0).to_numpy(dtype=float))
    return {
        "evaluable": True,
        "criterion_met": bool(pf_a < threshold and pf_b < threshold),
        "recent_window_pf": pf_a,
        "previous_window_pf": pf_b,
        "threshold": threshold,
    }


def _criterion_regime_shift(enriched: pd.DataFrame, window: int) -> dict[str, Any]:
    ordered = enriched.sort_values("entry_time").reset_index(drop=True)
    if len(ordered) < window * 2:
        return {
            "evaluable": False,
            "criterion_met": False,
            "reason": f"menos_de_{window * 2}_trades_para_comparar_regimes",
        }
    current = ordered.iloc[-window:]["market_regime"].fillna("MISSING").astype(str)
    previous = ordered.iloc[-window * 2 : -window]["market_regime"].fillna("MISSING").astype(str)
    current_dist = current.value_counts(normalize=True)
    previous_dist = previous.value_counts(normalize=True)
    labels = sorted(set(current_dist.index).union(previous_dist.index))
    max_shift = 0.0
    shifts: dict[str, float] = {}
    for label in labels:
        cur = float(current_dist.get(label, 0.0))
        prev = float(previous_dist.get(label, 0.0))
        shift = abs(cur - prev)
        shifts[label] = shift
        max_shift = max(max_shift, shift)
    return {
        "evaluable": True,
        "criterion_met": bool(max_shift >= 0.25),
        "max_regime_share_shift": max_shift,
        "regime_share_shift": shifts,
    }


def _criteria_monitor(dashboard: pd.DataFrame, enriched: pd.DataFrame, cfg: Config) -> dict[str, Any]:
    coverage_days = float((pd.to_datetime(enriched["exit_time"], utc=True).max() - pd.to_datetime(enriched["entry_time"], utc=True).min()).total_seconds() / 86400.0)
    per_combo_30 = dashboard[pd.to_numeric(dashboard["trades"], errors="coerce").fillna(0) >= 30][["symbol", "timeframe", "trades"]]
    per_combo_50 = dashboard[pd.to_numeric(dashboard["trades"], errors="coerce").fillna(0) >= 50][["symbol", "timeframe", "trades"]]
    return {
        "criteria_30_trades_reached": per_combo_30.to_dict(orient="records"),
        "criteria_50_trades_reached": per_combo_50.to_dict(orient="records"),
        "criteria_180_days_history": {
            "criterion_met": bool(coverage_days >= cfg.readiness_temporal_days_target),
            "coverage_days": coverage_days,
            "target_days": cfg.readiness_temporal_days_target,
        },
        "rolling_long_possible": {
            "criterion_met": bool(len(per_combo_30) > 0),
            "eligible_combinations": per_combo_30.to_dict(orient="records"),
        },
        "persistent_pf_drop": _criterion_recent_pf(enriched, cfg.persistent_pf_window, cfg.persistent_pf_threshold),
        "relevant_regime_change": _criterion_regime_shift(enriched, cfg.regime_shift_window),
    }


def _scientific_readiness_score(dashboard: pd.DataFrame, quality: dict[str, Any], criteria: dict[str, Any], cfg: Config) -> dict[str, Any]:
    mean_progress_30 = float(pd.to_numeric(dashboard["pct_to_30"], errors="coerce").fillna(0.0).mean()) if not dashboard.empty else 0.0
    sample_score = min(100.0, mean_progress_30)
    coverage_days = _safe_float(criteria["criteria_180_days_history"].get("coverage_days"), default=0.0)
    temporal_score = min(100.0, 100.0 * coverage_days / cfg.readiness_temporal_days_target)
    integrity_score = 100.0 * _safe_float(quality.get("integrity_ratio"), default=0.0)

    score = 0.5 * sample_score + 0.25 * temporal_score + 0.25 * integrity_score
    if score < 40.0:
        label = "Insuficiente"
    elif score < 70.0:
        label = "Em coleta"
    elif score < 90.0:
        label = "Proximo da reavaliacao"
    else:
        label = "Pronto para reabrir FASES 13-14B"
    return {
        "score": score,
        "label": label,
        "components": {
            "sample_score": sample_score,
            "temporal_score": temporal_score,
            "integrity_score": integrity_score,
        },
    }


def _dashboard_export(dashboard: pd.DataFrame, integrity_df: pd.DataFrame) -> pd.DataFrame:
    issues = (
        integrity_df.groupby(["symbol", "timeframe"], dropna=False)
        .agg(
            total_integrity_issues=("has_integrity_issue", "sum"),
            missing_context=("missing_indicator_context", "sum"),
            missing_regime=("missing_market_regime", "sum"),
            duplicates=("duplicate_trade", "sum"),
            invalid_trades=("invalid_timestamp_order", "sum"),
        )
        .reset_index()
    )
    out = dashboard.merge(issues, on=["symbol", "timeframe"], how="left")
    for col in ["total_integrity_issues", "missing_context", "missing_regime", "duplicates", "invalid_trades"]:
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype(int)
    return out


def _append_readiness_history(
    *,
    base_dir: Path,
    cfg: Config,
    dashboard: pd.DataFrame,
    readiness: dict[str, Any],
    criteria: dict[str, Any],
    outputs: dict[str, str],
) -> tuple[Path, Path, Path]:
    history_csv = base_dir / "scientific_readiness_history.csv"
    history_json = base_dir / "scientific_readiness_history.json"
    dashboard_csv = base_dir / "scientific_dashboard.csv"

    trades_by_asset = dashboard.groupby("symbol", dropna=False)["trades"].sum().to_dict()
    trades_by_timeframe = dashboard.groupby("timeframe", dropna=False)["trades"].sum().to_dict()
    criteria_hit: list[str] = []
    criteria_pending: list[str] = []
    if criteria["criteria_30_trades_reached"]:
        criteria_hit.append(">=30_trades_por_Ativo_Timeframe")
    else:
        criteria_pending.append(">=30_trades_por_Ativo_Timeframe")
    if criteria["criteria_50_trades_reached"]:
        criteria_hit.append(">=50_trades_por_Ativo_Timeframe")
    else:
        criteria_pending.append(">=50_trades_por_Ativo_Timeframe")
    if criteria["criteria_180_days_history"]["criterion_met"]:
        criteria_hit.append(">=180_dias_de_historico")
    else:
        criteria_pending.append(">=180_dias_de_historico")
    if criteria["rolling_long_possible"]["criterion_met"]:
        criteria_hit.append("rolling_longo_possivel")
    else:
        criteria_pending.append("rolling_longo_possivel")
    if criteria["persistent_pf_drop"].get("criterion_met", False):
        criteria_hit.append("queda_persistente_do_PF_registrada")
    else:
        criteria_pending.append("queda_persistente_do_PF")
    if criteria["relevant_regime_change"].get("criterion_met", False):
        criteria_hit.append("mudanca_relevante_de_regime_registrada")
    else:
        criteria_pending.append("mudanca_relevante_de_regime")

    row = {
        "generated_at": _now_utc().isoformat(),
        "strategy_key": cfg.strategy_key,
        "total_trades": int(pd.to_numeric(dashboard["trades"], errors="coerce").fillna(0).sum()),
        "trades_by_asset_json": json.dumps(trades_by_asset, ensure_ascii=False),
        "trades_by_timeframe_json": json.dumps(trades_by_timeframe, ensure_ascii=False),
        "coverage_days": float(pd.to_numeric(dashboard["trade_window_days"], errors="coerce").max()) if not dashboard.empty else 0.0,
        "readiness_score": float(readiness["score"]),
        "readiness_label": str(readiness["label"]),
        "criteria_hit_json": json.dumps(criteria_hit, ensure_ascii=False),
        "criteria_pending_json": json.dumps(criteria_pending, ensure_ascii=False),
        "outputs_json": json.dumps(outputs, ensure_ascii=False),
    }

    history_df = pd.DataFrame([row])
    if history_csv.exists():
        previous = pd.read_csv(history_csv)
        history_df = pd.concat([previous, history_df], ignore_index=True)
    history_df.to_csv(history_csv, index=False)
    history_json.write_text(history_df.to_json(orient="records", force_ascii=False, indent=2), encoding="utf-8")

    dashboard_history_rows: list[dict[str, Any]] = []
    for hist_row in history_df.to_dict(orient="records"):
        generated_at = hist_row.get("generated_at")
        dashboard_history_rows.append(
            {
                "generated_at": generated_at,
                "metric": "total_trades",
                "dimension": "global",
                "value": _safe_float(hist_row.get("total_trades")),
            }
        )
        dashboard_history_rows.append(
            {
                "generated_at": generated_at,
                "metric": "coverage_days",
                "dimension": "global",
                "value": _safe_float(hist_row.get("coverage_days")),
            }
        )
        dashboard_history_rows.append(
            {
                "generated_at": generated_at,
                "metric": "readiness_score",
                "dimension": "global",
                "value": _safe_float(hist_row.get("readiness_score")),
            }
        )
        trades_asset = json.loads(str(hist_row.get("trades_by_asset_json") or "{}"))
        for key, value in trades_asset.items():
            dashboard_history_rows.append(
                {
                    "generated_at": generated_at,
                    "metric": "trades_by_asset",
                    "dimension": str(key),
                    "value": _safe_float(value),
                }
            )
        trades_tf = json.loads(str(hist_row.get("trades_by_timeframe_json") or "{}"))
        for key, value in trades_tf.items():
            dashboard_history_rows.append(
                {
                    "generated_at": generated_at,
                    "metric": "trades_by_timeframe",
                    "dimension": str(key),
                    "value": _safe_float(value),
                }
            )
    pd.DataFrame(dashboard_history_rows).to_csv(dashboard_csv, index=False)

    with get_session() as session:
        HistoryPersistenceService(session).save_scientific_readiness_history(
            ScientificReadinessHistory(
                generated_at=pd.to_datetime(row["generated_at"], utc=True).to_pydatetime(),
                strategy_key=cfg.strategy_key,
                total_trades=int(row["total_trades"]),
                trades_by_asset_json=str(row["trades_by_asset_json"]),
                trades_by_timeframe_json=str(row["trades_by_timeframe_json"]),
                coverage_days=float(row["coverage_days"]),
                readiness_score=float(row["readiness_score"]),
                readiness_label=str(row["readiness_label"]),
                criteria_hit_json=str(row["criteria_hit_json"]),
                criteria_pending_json=str(row["criteria_pending_json"]),
                outputs_json=str(row["outputs_json"]),
            )
        )

    return history_csv, history_json, dashboard_csv


def _build_status_markdown(
    total_trades: int,
    dashboard: pd.DataFrame,
    quality: dict[str, Any],
    readiness: dict[str, Any],
    criteria: dict[str, Any],
    field_summary: pd.DataFrame,
) -> str:
    trades_by_asset = dashboard.groupby("symbol", dropna=False)["trades"].sum().sort_values(ascending=False)
    trades_by_tf = dashboard.groupby("timeframe", dropna=False)["trades"].sum().sort_values(ascending=False)
    criteria_hit: list[str] = []
    criteria_pending: list[str] = []

    if criteria["criteria_30_trades_reached"]:
        criteria_hit.append(">=30_trades_por_Ativo_Timeframe")
    else:
        criteria_pending.append(">=30_trades_por_Ativo_Timeframe")

    if criteria["criteria_50_trades_reached"]:
        criteria_hit.append(">=50_trades_por_Ativo_Timeframe")
    else:
        criteria_pending.append(">=50_trades_por_Ativo_Timeframe")

    if criteria["criteria_180_days_history"]["criterion_met"]:
        criteria_hit.append(">=180_dias_de_historico")
    else:
        criteria_pending.append(">=180_dias_de_historico")

    if criteria["rolling_long_possible"]["criterion_met"]:
        criteria_hit.append("rolling_longo_possivel")
    else:
        criteria_pending.append("rolling_longo_possivel")

    if criteria["persistent_pf_drop"].get("criterion_met", False):
        criteria_hit.append("queda_persistente_do_PF_registrada")
    else:
        criteria_pending.append("queda_persistente_do_PF")

    if criteria["relevant_regime_change"].get("criterion_met", False):
        criteria_hit.append("mudanca_relevante_de_regime_registrada")
    else:
        criteria_pending.append("mudanca_relevante_de_regime")

    top_progress = dashboard.sort_values(["pct_to_30", "trades"], ascending=[False, False]).head(8)
    missing_schema = field_summary[field_summary["status"] == "missing_schema"]["field"].astype(str).tolist()
    lines: list[str] = []
    lines.append("# Coleta de Evidencias - Status")
    lines.append("")
    lines.append(f"- generated_at_utc: {_now_utc().isoformat()}")
    lines.append(f"- quantidade_total_de_trades: {total_trades}")
    lines.append(f"- scientific_readiness_score: {readiness['score']:.1f} ({readiness['label']})")
    lines.append("")
    lines.append("## Trades por ativo")
    for symbol, trades in trades_by_asset.items():
        lines.append(f"- {symbol}: {int(trades)}")
    lines.append("")
    lines.append("## Trades por timeframe")
    for timeframe, trades in trades_by_tf.items():
        lines.append(f"- {timeframe}: {int(trades)}")
    lines.append("")
    lines.append("## Cobertura temporal")
    lines.append(
        f"- cobertura_global_dias: {_safe_float(criteria['criteria_180_days_history'].get('coverage_days')):.2f} / {criteria['criteria_180_days_history'].get('target_days')}"
    )
    lines.append("")
    lines.append("## Qualidade da base")
    lines.append(f"- trades_com_alguma_inconsistencia: {quality['trades_with_any_issue']}")
    lines.append(f"- taxa_de_inconsistencia_pct: {quality['issue_rate_pct']:.2f}")
    lines.append(f"- campos_ausentes_no_schema: {missing_schema if missing_schema else 'nenhum'}")
    lines.append("")
    lines.append("## Progresso por combinacao")
    for row in top_progress.itertuples(index=False):
        lines.append(
            f"- {row.symbol} {row.timeframe}: {int(row.trades)}/30 ({row.pct_to_30:.1f}%), {int(row.trades)}/50 ({row.pct_to_50:.1f}%), {int(row.trades)}/100 ({row.pct_to_100:.1f}%)."
        )
    lines.append("")
    lines.append("## Criterios ja atingidos")
    if criteria_hit:
        for item in criteria_hit:
            lines.append(f"- {item}")
    else:
        lines.append("- nenhum")
    lines.append("")
    lines.append("## Criterios pendentes")
    for item in criteria_pending:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Previsao aproximada")
    forecast = dashboard.sort_values(["estimated_days_to_30", "trades_to_30"], ascending=[True, True]).head(6)
    for row in forecast.itertuples(index=False):
        est_30 = "indisponivel" if pd.isna(row.estimated_days_to_30) else f"{row.estimated_days_to_30:.1f} dias"
        est_50 = "indisponivel" if pd.isna(row.estimated_days_to_50) else f"{row.estimated_days_to_50:.1f} dias"
        est_100 = "indisponivel" if pd.isna(row.estimated_days_to_100) else f"{row.estimated_days_to_100:.1f} dias"
        lines.append(
            f"- {row.symbol} {row.timeframe}: ate 30 trades em {est_30}, ate 50 trades em {est_50}, ate 100 trades em {est_100}."
        )
    return "\n".join(lines) + "\n"


def run(cfg: Config) -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    results_dir = base_dir / "optimization" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    baseline = _load_baseline_matrix(results_dir)
    trades = _load_all_trades(cfg.strategy_key)
    if trades.empty:
        raise RuntimeError("Nenhum trade encontrado em trade_history para a estrategia informada.")

    enriched = _classify_regimes(_enrich_all_trades(trades))
    snapshot_audit = _load_scientific_snapshot_audit(cfg.strategy_key)
    field_summary = _field_presence_summary(enriched, snapshot_audit)
    integrity_df = _integrity_trade_history(enriched, snapshot_audit)
    quality = _data_quality_summary(integrity_df, field_summary)
    dashboard = _sample_growth_dashboard(enriched, baseline, cfg)
    criteria = _criteria_monitor(dashboard, enriched, cfg)
    readiness = _scientific_readiness_score(dashboard, quality, criteria, cfg)
    dashboard_export = _dashboard_export(dashboard, integrity_df)

    status_md = base_dir / "coleta_evidencias_status.md"
    dashboard_csv = base_dir / "coleta_evidencias_dashboard.csv"
    readiness_json = base_dir / "scientific_readiness.json"
    integrity_csv = base_dir / "integridade_trade_history.csv"

    status_md.write_text(
        _build_status_markdown(
            total_trades=int(len(enriched)),
            dashboard=dashboard_export,
            quality=quality,
            readiness=readiness,
            criteria=criteria,
            field_summary=field_summary,
        ),
        encoding="utf-8",
    )
    dashboard_export.to_csv(dashboard_csv, index=False)
    integrity_df.to_csv(integrity_csv, index=False)

    payload = {
        "generated_at": _now_utc().isoformat(),
        "strategy_key": cfg.strategy_key,
        "total_trades": int(len(enriched)),
        "coverage": {
            "first_trade": str(pd.to_datetime(enriched["entry_time"], utc=True).min()),
            "last_trade": str(pd.to_datetime(enriched["exit_time"], utc=True).max()),
            "coverage_days": _safe_float(criteria["criteria_180_days_history"].get("coverage_days")),
        },
        "data_quality": quality,
        "criteria": criteria,
        "scientific_readiness": readiness,
        "alerts": {
            "integrity_alert_required": bool(quality["trades_with_any_issue"] > 0 or len(quality["field_missing_in_schema"]) > 0),
            "integrity_alert_types": [
                key for key, value in quality["issue_counts"].items() if int(value) > 0
            ] + (["missing_schema_fields"] if quality["field_missing_in_schema"] else []),
        },
        "outputs": {
            "coleta_evidencias_status": str(status_md),
            "coleta_evidencias_dashboard": str(dashboard_csv),
            "scientific_readiness": str(readiness_json),
            "integridade_trade_history": str(integrity_csv),
        },
    }
    history_csv, history_json, evolutive_dashboard_csv = _append_readiness_history(
        base_dir=base_dir,
        cfg=cfg,
        dashboard=dashboard_export,
        readiness=readiness,
        criteria=criteria,
        outputs=payload["outputs"],
    )
    payload["outputs"]["scientific_readiness_history_csv"] = str(history_csv)
    payload["outputs"]["scientific_readiness_history_json"] = str(history_json)
    payload["outputs"]["scientific_dashboard"] = str(evolutive_dashboard_csv)
    readiness_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def main() -> None:
    payload = run(Config())
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()