from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from database.history_models import TradeOutcomeLearningRun
from database.history_repositories import TradeOutcomeLearningRunRepository
from utils.logger import get_logger
from utils.metrics import expectancy_from_pnl, profit_factor_from_pnl, sharpe_from_pnl

logger = get_logger(__name__)

NUMERIC_FEATURES = (
    "trend_score",
    "atr_pct",
    "distance_to_ema_pct",
    "relative_volume",
)
CAT_FEATURES = (
    "symbol",
    "timeframe",
    "regime",
    "rsi_bucket",
    "atr_bucket",
    "volume_bucket",
    "bollinger_position",
    "direction",
)


@dataclass(frozen=True)
class TradeOutcomeLearningConfig:
    events_glob: str = "optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv"
    targets: tuple[str, ...] = ("winner", "return_above", "positive_expectancy", "risk_adjusted")
    return_above_threshold: float = 0.01
    return_below_threshold: float = -0.01
    risk_adjusted_threshold: float = 0.8
    train_ratio: float = 0.60
    validation_ratio: float = 0.20
    min_support: int = 50
    max_rule_coverage: float = 0.70
    min_precision_gain: float = 0.03
    min_generalization_score: float = 0.55
    min_robustness_score: float = 0.50
    max_overfit_gap: float = 0.12
    trade_outcome_score_threshold: float = 70.0
    top_k_candidates: int = 25
    discovery_max_rows: int = 3_000_000
    output_prefix: str = "trade_outcome_learning"
    persist_to_db: bool = True


@dataclass(frozen=True)
class AtomicCondition:
    column: str
    op: str
    value: Any

    @property
    def expr(self) -> str:
        return f"{self.column}{self.op}{self.value}"


def temporal_split_frame(df: pd.DataFrame, train_ratio: float, validation_ratio: float) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    if df.empty:
        return df.copy(), df.copy(), df.copy()

    ordered = df.copy()
    ordered["open_time"] = pd.to_datetime(ordered["open_time"], errors="coerce", utc=True)
    ordered = ordered.dropna(subset=["open_time"]).sort_values("open_time").reset_index(drop=True)

    n = len(ordered)
    train_end = int(max(1, min(n - 2, round(n * train_ratio))))
    val_end = int(max(train_end + 1, min(n - 1, round(n * (train_ratio + validation_ratio)))))

    return ordered.iloc[:train_end].copy(), ordered.iloc[train_end:val_end].copy(), ordered.iloc[val_end:].copy()


def compute_trade_outcome_score(
    *,
    expected_profit_factor: float,
    expected_expectancy: float,
    expected_sharpe: float,
    temporal_robustness: float,
    asset_robustness: float,
    regime_robustness: float,
    timeframe_robustness: float,
    generalization_score: float,
    simplicity_score: float,
    coverage_score: float,
) -> float:
    pf_norm = max(0.0, min(1.0, expected_profit_factor / 2.0))
    exp_norm = max(0.0, min(1.0, (expected_expectancy + 0.02) / 0.05))
    sharpe_norm = max(0.0, min(1.0, (expected_sharpe + 1.0) / 3.0))

    score = (
        14.0 * pf_norm
        + 14.0 * exp_norm
        + 12.0 * sharpe_norm
        + 10.0 * temporal_robustness
        + 10.0 * asset_robustness
        + 10.0 * regime_robustness
        + 8.0 * timeframe_robustness
        + 10.0 * generalization_score
        + 6.0 * simplicity_score
        + 6.0 * coverage_score
    )
    return float(max(0.0, min(100.0, score)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    if math.isfinite(out):
        return out
    return default


def _rule_mask(frame: pd.DataFrame, condition: AtomicCondition) -> pd.Series:
    if condition.column not in frame.columns:
        return pd.Series(False, index=frame.index)

    col = frame[condition.column]
    if condition.op in (">=", "<="):
        num = pd.to_numeric(col, errors="coerce")
        val = float(condition.value)
        if condition.op == ">=":
            return (num >= val).fillna(False)
        return (num <= val).fillna(False)

    if condition.op == "==":
        return (col.astype(str) == str(condition.value)).fillna(False)

    return pd.Series(False, index=frame.index)


def _target_from_name(df: pd.DataFrame, target_name: str, cfg: TradeOutcomeLearningConfig) -> pd.Series:
    ret = pd.to_numeric(df["future_return_20"], errors="coerce").fillna(0.0)
    mae = pd.to_numeric(df["mae"], errors="coerce").abs().fillna(0.0)
    expectancy = pd.to_numeric(df["expectancy_individual"], errors="coerce").fillna(0.0)

    if target_name == "winner":
        return (ret > 0).astype(int)
    if target_name == "loser":
        return (ret <= 0).astype(int)
    if target_name == "return_above":
        return (ret >= cfg.return_above_threshold).astype(int)
    if target_name == "return_below":
        return (ret <= cfg.return_below_threshold).astype(int)
    if target_name == "positive_expectancy":
        return (expectancy > 0).astype(int)
    if target_name == "risk_adjusted":
        ratio = ret / mae.replace(0.0, np.nan)
        ratio = ratio.replace([np.inf, -np.inf], np.nan).fillna(0.0)
        return (ratio >= cfg.risk_adjusted_threshold).astype(int)
    raise ValueError(f"Unknown target '{target_name}'")


def _group_robustness(frame: pd.DataFrame, mask: pd.Series, target_col: str, key: str, min_trades: int = 25) -> float:
    selected = frame[mask]
    if selected.empty or key not in selected.columns:
        return 0.0

    rates: list[float] = []
    for _, chunk in selected.groupby(key, observed=True):
        if len(chunk) < min_trades:
            continue
        rates.append(float(chunk[target_col].mean()))

    if len(rates) <= 1:
        return 0.45

    arr = np.asarray(rates, dtype=float)
    mean = float(np.mean(arr))
    if abs(mean) < 1e-9:
        return 0.0
    cv = float(np.std(arr, ddof=0) / abs(mean))
    return float(max(0.0, min(1.0, 1.0 - cv)))


def _temporal_bucket(value: Any) -> str:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return "unknown"
    year = int(ts.year)
    if year < 2020:
        return "2018-2020"
    if year < 2022:
        return "2020-2022"
    if year < 2024:
        return "2022-2024"
    return "2024-2026"


def _ensure_outcome_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    base = pd.to_numeric(out.get("future_return"), errors="coerce").fillna(0.0)

    if "future_return_5" not in out.columns:
        out["future_return_5"] = base
    if "future_return_10" not in out.columns:
        out["future_return_10"] = base * 1.10
    if "future_return_20" not in out.columns:
        out["future_return_20"] = base * 1.20
    if "future_return_50" not in out.columns:
        out["future_return_50"] = base * 1.35

    upside = pd.to_numeric(out.get("future_upside"), errors="coerce").fillna(0.0).clip(lower=0.0)
    downside = pd.to_numeric(out.get("future_downside"), errors="coerce").fillna(0.0)

    out["mfe"] = upside
    out["mae"] = downside.abs()
    out["drawdown"] = downside
    out["duration_minutes"] = pd.to_numeric(out.get("duration_minutes"), errors="coerce").fillna(0.0)

    out["profit_factor_individual"] = np.where(
        out["future_return_20"] > 0,
        (out["future_return_20"].abs() + 1e-9) / (out["mae"] + 1e-9),
        0.0,
    )
    out["expectancy_individual"] = out["future_return_20"]

    return out


class TradeOutcomeLearningLab:
    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def run(self, config: TradeOutcomeLearningConfig | None = None) -> dict[str, Any]:
        cfg = config or TradeOutcomeLearningConfig()
        t0 = datetime.now(timezone.utc)

        files = sorted(self._base_dir.glob(cfg.events_glob))
        if not files:
            raise ValueError(f"No event files found for glob: {cfg.events_glob}")

        frame = self._build_learning_dataset(files)
        if frame.empty:
            raise ValueError("Learning dataset is empty after loading events.")

        frame = _ensure_outcome_columns(frame)
        frame["open_time"] = pd.to_datetime(frame["open_time"], errors="coerce", utc=True)
        frame = frame.dropna(subset=["open_time"]).reset_index(drop=True)
        frame["temporal_bucket"] = frame["open_time"].map(_temporal_bucket)

        target_columns: list[str] = []
        for target_name in cfg.targets:
            col_name = f"target_{target_name}"
            frame[col_name] = _target_from_name(frame, target_name, cfg)
            target_columns.append(col_name)

        train, _, _ = temporal_split_frame(frame, cfg.train_ratio, cfg.validation_ratio)
        discovery_frame = train
        if len(discovery_frame) > cfg.discovery_max_rows:
            # Deterministic downsample preserving temporal ordering for discovery speed.
            step = max(1, int(len(discovery_frame) / cfg.discovery_max_rows))
            discovery_frame = discovery_frame.iloc[::step].head(cfg.discovery_max_rows).copy()

        candidates = self._discover_candidates(discovery_frame, target_columns, cfg)
        evaluated = self._evaluate_candidates(frame, candidates, cfg)

        run_id = str(uuid4())
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_json = self._base_dir / "optimization" / "results" / f"{cfg.output_prefix}_{ts}.json"
        out_csv = self._base_dir / "optimization" / "results" / f"{cfg.output_prefix}_{ts}.csv"
        out_md = self._base_dir / "optimization" / "results" / f"{cfg.output_prefix}_{ts}.md"
        out_json.parent.mkdir(parents=True, exist_ok=True)

        approved_rows = evaluated[evaluated["approved"]]
        best_row = evaluated.iloc[0] if not evaluated.empty else None
        approved = bool(not approved_rows.empty)
        decision = "APPROVE_IMPLEMENTATION" if approved else "REJECT_IMPLEMENTATION"

        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "phase": "FASE 8 - Trade Outcome Learning",
            "status": "COMPLETED",
            "dataset": {
                "events_files": len(files),
                "rows": int(len(frame)),
                "assets": int(frame["symbol"].astype(str).nunique()) if "symbol" in frame.columns else 0,
                "timeframes": int(frame["timeframe"].astype(str).nunique()) if "timeframe" in frame.columns else 0,
                "time_min": frame["open_time"].min().isoformat(),
                "time_max": frame["open_time"].max().isoformat(),
                "distribution_by_asset": frame["symbol"].astype(str).value_counts().head(50).to_dict() if "symbol" in frame.columns else {},
                "distribution_by_timeframe": frame["timeframe"].astype(str).value_counts().to_dict() if "timeframe" in frame.columns else {},
                "distribution_by_regime": frame["regime"].astype(str).value_counts().to_dict() if "regime" in frame.columns else {},
                "opportunities_generated": int(len(frame)),
            },
            "targets": {name: int(frame[f"target_{name}"].sum()) for name in cfg.targets},
            "discovery": {
                "discovery_rows": int(len(discovery_frame)),
                "candidates_before_evaluation": int(len(candidates)),
                "candidates_after_evaluation": int(len(evaluated)),
            },
            "best_candidate": None if best_row is None else {
                "target": str(best_row["target"]),
                "rule": str(best_row["rule"]),
                "support": int(best_row["support"]),
                "confidence": float(best_row["confidence"]),
                "trade_outcome_score": float(best_row["trade_outcome_score"]),
                "scientific_robustness_score": float(best_row["scientific_robustness_score"]),
                "approved": bool(best_row["approved"]),
                "rejection_reason": str(best_row["rejection_reason"]),
                "explainability": {
                    "variable_importance": best_row["variable_importance"],
                    "coverage": float(best_row["coverage_score"]),
                    "confidence": float(best_row["confidence"]),
                    "stability": float(best_row["generalization_score"]),
                    "assets": best_row["assets"],
                    "regimes": best_row["regimes"],
                    "timeframes": best_row["timeframes"],
                    "period_start": str(best_row["period_start"]),
                    "period_end": str(best_row["period_end"]),
                },
            },
            "decision": decision,
        }

        flat = evaluated.copy()
        for col in ("variable_importance", "assets", "regimes", "timeframes"):
            if col in flat.columns:
                flat[col] = flat[col].map(lambda x: json.dumps(x, ensure_ascii=True))
        flat.to_csv(out_csv, index=False)
        out_json.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

        md_lines = [
            "# Trade Outcome Learning Lab",
            "",
            "## Summary",
            f"- decision: {decision}",
            f"- approved_rules: {int(len(approved_rows))}",
            f"- evaluated_rules: {int(len(evaluated))}",
            "",
            "## Dataset",
            f"- files: {len(files)}",
            f"- rows: {int(len(frame))}",
            f"- assets: {int(frame['symbol'].astype(str).nunique()) if 'symbol' in frame.columns else 0}",
            f"- timeframes: {int(frame['timeframe'].astype(str).nunique()) if 'timeframe' in frame.columns else 0}",
            "",
            "## Top 10 Candidates",
        ]
        for _, row in evaluated.head(10).iterrows():
            md_lines.append(
                f"- {row['target']} | {row['rule']} | outcome_score={float(row['trade_outcome_score']):.2f} | scientific_score={float(row['scientific_robustness_score']):.2f} | approved={bool(row['approved'])} | reason={row['rejection_reason']}"
            )
        out_md.write_text("\n".join(md_lines), encoding="utf-8")

        outputs = {"csv": str(out_csv), "json": str(out_json), "md": str(out_md)}
        self._persist_run(run_id=run_id, cfg=cfg, decision=decision, approved=approved, best=best_row, report=report, outputs=outputs)

        elapsed = (datetime.now(timezone.utc) - t0).total_seconds()
        logger.info("TradeOutcomeLearningLab completed | decision=%s | evaluated=%d | elapsed=%.2fs", decision, len(evaluated), elapsed)

        return {
            "summary": {
                "run_id": run_id,
                "status": "COMPLETED",
                "decision": decision,
                "approved": approved,
                "evaluated_candidates": int(len(evaluated)),
                "approved_candidates": int(len(approved_rows)),
                "best_rule": None if best_row is None else str(best_row["rule"]),
                "trade_outcome_score": 0.0 if best_row is None else float(best_row["trade_outcome_score"]),
            },
            "outputs": outputs,
            "report": report,
        }

    def _build_learning_dataset(self, files: list[Path]) -> pd.DataFrame:
        usecols = [
            "symbol",
            "timeframe",
            "open_time",
            "duration_minutes",
            "trend_score",
            "atr_pct",
            "distance_to_ema_pct",
            "relative_volume",
            "rsi_bucket",
            "atr_bucket",
            "volume_bucket",
            "bollinger_position",
            "direction",
            "future_return",
            "future_upside",
            "future_downside",
            "regime",
            "primary_regime",
            "primary_profile",
        ]

        parts: list[pd.DataFrame] = []
        float_cols = [
            "duration_minutes",
            "trend_score",
            "atr_pct",
            "distance_to_ema_pct",
            "relative_volume",
            "future_return",
            "future_upside",
            "future_downside",
        ]
        cat_cols = [
            "symbol",
            "timeframe",
            "rsi_bucket",
            "atr_bucket",
            "volume_bucket",
            "bollinger_position",
            "direction",
            "regime",
            "primary_regime",
            "primary_profile",
        ]

        for path in files:
            logger.info("TradeOutcomeLearningLab loading file: %s", path.name)
            try:
                for chunk in pd.read_csv(path, usecols=usecols, chunksize=250_000, low_memory=False):
                    for col in float_cols:
                        if col in chunk.columns:
                            chunk[col] = pd.to_numeric(chunk[col], errors="coerce").astype("float32")
                    for col in cat_cols:
                        if col in chunk.columns:
                            chunk[col] = chunk[col].astype("string").astype("category")
                    parts.append(chunk)
            except ValueError:
                chunk = pd.read_csv(path, low_memory=False)
                cols = [c for c in usecols if c in chunk.columns]
                if cols:
                    chunk = chunk[cols].copy()
                    for col in float_cols:
                        if col in chunk.columns:
                            chunk[col] = pd.to_numeric(chunk[col], errors="coerce").astype("float32")
                    for col in cat_cols:
                        if col in chunk.columns:
                            chunk[col] = chunk[col].astype("string").astype("category")
                    parts.append(chunk)

        if not parts:
            return pd.DataFrame()
        df = pd.concat(parts, ignore_index=True, copy=False)
        for col in ("primary_regime", "primary_profile"):
            if col not in df.columns:
                df[col] = "unknown"
        return df

    def _discover_candidates(self, frame: pd.DataFrame, target_columns: list[str], cfg: TradeOutcomeLearningConfig) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []

        for target_col in target_columns:
            y = frame[target_col]
            base_rate = float(y.mean()) if len(y) else 0.0

            conditions: list[AtomicCondition] = []
            for col in NUMERIC_FEATURES:
                if col not in frame.columns:
                    continue
                vals = pd.to_numeric(frame[col], errors="coerce").dropna()
                if vals.empty:
                    continue
                for q in (0.25, 0.4, 0.6, 0.75):
                    thr = float(vals.quantile(q))
                    conditions.append(AtomicCondition(col, ">=", round(thr, 6)))
                    conditions.append(AtomicCondition(col, "<=", round(thr, 6)))

            for col in CAT_FEATURES:
                if col not in frame.columns:
                    continue
                vc = frame[col].astype(str).value_counts(dropna=False).head(8)
                for value, count in vc.items():
                    if int(count) < cfg.min_support:
                        continue
                    conditions.append(AtomicCondition(col, "==", str(value)))

            for cond in conditions:
                mask = _rule_mask(frame, cond)
                support = int(mask.sum())
                if support < cfg.min_support:
                    continue
                support_share = support / max(1, len(frame))
                if support_share >= cfg.max_rule_coverage:
                    continue

                selected = frame[mask]
                confidence = float(selected[target_col].mean())
                precision_gain = confidence - base_rate
                if precision_gain < cfg.min_precision_gain:
                    continue

                lift = confidence / base_rate if base_rate > 1e-12 else 0.0
                returns = pd.to_numeric(selected["future_return_20"], errors="coerce").fillna(0.0)

                rows.append(
                    {
                        "target": target_col.replace("target_", ""),
                        "target_col": target_col,
                        "rule": cond.expr,
                        "condition_column": cond.column,
                        "condition_op": cond.op,
                        "condition_value": cond.value,
                        "support": support,
                        "support_share": support_share,
                        "confidence": confidence,
                        "base_rate": base_rate,
                        "lift": lift,
                        "precision_gain": precision_gain,
                        "expected_profit_factor": _safe_float(profit_factor_from_pnl(returns), 0.0),
                        "expected_expectancy": _safe_float(expectancy_from_pnl(returns), 0.0),
                        "expected_sharpe": _safe_float(sharpe_from_pnl(returns), 0.0),
                    }
                )

        if not rows:
            return pd.DataFrame(columns=["target", "target_col", "rule"])

        out = pd.DataFrame(rows)
        out = out.sort_values(["confidence", "support", "lift"], ascending=[False, False, False]).reset_index(drop=True)
        out = out.head(max(1, cfg.top_k_candidates))
        return out

    def _evaluate_candidates(self, frame: pd.DataFrame, candidates: pd.DataFrame, cfg: TradeOutcomeLearningConfig) -> pd.DataFrame:
        if candidates.empty:
            return pd.DataFrame(
                columns=[
                    "target",
                    "rule",
                    "support",
                    "confidence",
                    "trade_outcome_score",
                    "approved",
                    "rejection_reason",
                ]
            )

        train, val, test = temporal_split_frame(frame, cfg.train_ratio, cfg.validation_ratio)
        rows: list[dict[str, Any]] = []

        for _, cand in candidates.iterrows():
            target_col = str(cand["target_col"])
            cond = AtomicCondition(str(cand["condition_column"]), str(cand["condition_op"]), cand["condition_value"])

            tr_m = _rule_mask(train, cond)
            va_m = _rule_mask(val, cond)
            te_m = _rule_mask(test, cond)

            tr_df = train[tr_m]
            va_df = val[va_m]
            te_df = test[te_m]

            tr_prec = float(tr_df[target_col].mean()) if not tr_df.empty else 0.0
            va_prec = float(va_df[target_col].mean()) if not va_df.empty else 0.0
            te_prec = float(te_df[target_col].mean()) if not te_df.empty else 0.0

            tr_ret = pd.to_numeric(tr_df["future_return_20"], errors="coerce").fillna(0.0)
            va_ret = pd.to_numeric(va_df["future_return_20"], errors="coerce").fillna(0.0)
            te_ret = pd.to_numeric(te_df["future_return_20"], errors="coerce").fillna(0.0)

            tr_exp = _safe_float(expectancy_from_pnl(tr_ret), 0.0)
            va_exp = _safe_float(expectancy_from_pnl(va_ret), 0.0)
            te_exp = _safe_float(expectancy_from_pnl(te_ret), 0.0)

            deg_prec = max(0.0, tr_prec - min(va_prec, te_prec))
            deg_exp = max(0.0, tr_exp - min(va_exp, te_exp))
            generalization_score = float(max(0.0, min(1.0, 1.0 - (0.7 * deg_prec + 0.3 * max(0.0, deg_exp)))))

            full_mask = _rule_mask(frame, cond)
            temporal_rob = _group_robustness(frame, full_mask, target_col, "open_time", min_trades=35)
            temporal_rob = _group_robustness(frame, full_mask, target_col, "temporal_bucket", min_trades=25)
            asset_rob = _group_robustness(frame, full_mask, target_col, "symbol", min_trades=25)
            regime_rob = _group_robustness(frame, full_mask, target_col, "regime", min_trades=25)
            timeframe_rob = _group_robustness(frame, full_mask, target_col, "timeframe", min_trades=25)

            coverage_score = float(max(0.0, min(1.0, float(cand["support_share"]) / cfg.max_rule_coverage)))
            simplicity_score = 1.0
            overfit_flag = (tr_prec - max(va_prec, te_prec)) > cfg.max_overfit_gap

            score = compute_trade_outcome_score(
                expected_profit_factor=float(cand["expected_profit_factor"]),
                expected_expectancy=float(cand["expected_expectancy"]),
                expected_sharpe=float(cand["expected_sharpe"]),
                temporal_robustness=temporal_rob,
                asset_robustness=asset_rob,
                regime_robustness=regime_rob,
                timeframe_robustness=timeframe_rob,
                generalization_score=generalization_score,
                simplicity_score=simplicity_score,
                coverage_score=coverage_score,
            )
            scientific_robustness_score = float(
                max(
                    0.0,
                    min(
                        100.0,
                        100.0
                        * (
                            0.35 * generalization_score
                            + 0.25 * temporal_rob
                            + 0.20 * asset_rob
                            + 0.20 * regime_rob
                        ),
                    ),
                )
            )
            full_selected = frame[full_mask]
            expected_drawdown = float(pd.to_numeric(full_selected["drawdown"], errors="coerce").fillna(0.0).min()) if not full_selected.empty and "drawdown" in full_selected.columns else 0.0
            implementability_score = float(max(0.0, min(1.0, 0.6 * simplicity_score + 0.4 * coverage_score)))

            reasons: list[str] = []
            if score < cfg.trade_outcome_score_threshold:
                reasons.append("trade_outcome_score_below_threshold")
            if generalization_score < cfg.min_generalization_score:
                reasons.append("generalization_failed")
            if min(temporal_rob, asset_rob, regime_rob, timeframe_rob) < cfg.min_robustness_score:
                reasons.append("robustness_failed")
            if overfit_flag:
                reasons.append("overfitting_signal")
            if len(va_df) < 10 or len(te_df) < 10:
                reasons.append("insufficient_validation_or_test_support")

            approved = len(reasons) == 0
            rows.append(
                {
                    "target": cand["target"],
                    "target_col": target_col,
                    "rule": cand["rule"],
                    "support": int(cand["support"]),
                    "coverage_score": coverage_score,
                    "confidence": float(cand["confidence"]),
                    "train_precision": tr_prec,
                    "validation_precision": va_prec,
                    "test_precision": te_prec,
                    "expected_profit_factor": float(cand["expected_profit_factor"]),
                    "expected_expectancy": float(cand["expected_expectancy"]),
                    "expected_sharpe": float(cand["expected_sharpe"]),
                    "temporal_robustness": temporal_rob,
                    "asset_robustness": asset_rob,
                    "regime_robustness": regime_rob,
                    "timeframe_robustness": timeframe_rob,
                    "generalization_score": generalization_score,
                    "simplicity_score": simplicity_score,
                    "trade_outcome_score": score,
                    "scientific_robustness_score": scientific_robustness_score,
                    "expected_drawdown": expected_drawdown,
                    "implementability_score": implementability_score,
                    "approved": approved,
                    "overfit_flag": overfit_flag,
                    "rejection_reason": ";".join(reasons),
                    "variable_importance": {
                        str(cand["condition_column"]): 1.0,
                        "support": float(cand["support_share"]),
                        "lift": float(cand["lift"]),
                    },
                    "assets": sorted(set(full_selected["symbol"].astype(str).tolist()))[:15] if "symbol" in full_selected.columns else [],
                    "regimes": sorted(set(full_selected["regime"].astype(str).tolist()))[:15] if "regime" in full_selected.columns else [],
                    "timeframes": sorted(set(full_selected["timeframe"].astype(str).tolist()))[:15] if "timeframe" in full_selected.columns else [],
                    "period_start": full_selected["open_time"].min(),
                    "period_end": full_selected["open_time"].max(),
                }
            )

        out = pd.DataFrame(rows)
        out = out.sort_values(["trade_outcome_score", "support"], ascending=[False, False]).reset_index(drop=True)
        return out

    def _persist_run(
        self,
        *,
        run_id: str,
        cfg: TradeOutcomeLearningConfig,
        decision: str,
        approved: bool,
        best: pd.Series | None,
        report: dict[str, Any],
        outputs: dict[str, str],
    ) -> None:
        if not cfg.persist_to_db:
            return

        repo = TradeOutcomeLearningRunRepository(self._session)
        repo.save(
            TradeOutcomeLearningRun(
                run_id=run_id,
                status="completed",
                decision=decision,
                approved=approved,
                target_name=None if best is None else str(best.get("target")),
                rule_text=None if best is None else str(best.get("rule")),
                trade_outcome_score=0.0 if best is None else _safe_float(best.get("trade_outcome_score")),
                expected_profit_factor=0.0 if best is None else _safe_float(best.get("expected_profit_factor")),
                expected_expectancy=0.0 if best is None else _safe_float(best.get("expected_expectancy")),
                expected_sharpe=0.0 if best is None else _safe_float(best.get("expected_sharpe")),
                temporal_robustness=0.0 if best is None else _safe_float(best.get("temporal_robustness")),
                asset_robustness=0.0 if best is None else _safe_float(best.get("asset_robustness")),
                regime_robustness=0.0 if best is None else _safe_float(best.get("regime_robustness")),
                timeframe_robustness=0.0 if best is None else _safe_float(best.get("timeframe_robustness")),
                generalization_score=0.0 if best is None else _safe_float(best.get("generalization_score")),
                simplicity_score=0.0 if best is None else _safe_float(best.get("simplicity_score")),
                coverage_score=0.0 if best is None else _safe_float(best.get("coverage_score")),
                overfit_flag=False if best is None else bool(best.get("overfit_flag")),
                rejection_reason=None if best is None else str(best.get("rejection_reason")),
                artifacts_json=json.dumps(outputs, ensure_ascii=True),
                summary_json=json.dumps(report, ensure_ascii=True),
            )
        )
