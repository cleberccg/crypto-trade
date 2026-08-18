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

from database.history_models import ScientificRobustnessValidationRun
from database.history_repositories import ScientificRobustnessValidationRunRepository
from utils.logger import get_logger
from utils.metrics import expectancy_from_pnl, max_drawdown_from_pnl, profit_factor_from_pnl, sharpe_from_pnl, win_rate_from_pnl

logger = get_logger(__name__)

PRE_ENTRY_NUMERIC = ("trend_score", "atr_pct", "distance_to_ema_pct", "relative_volume")
PRE_ENTRY_CAT = ("rsi_bucket", "atr_bucket", "volume_bucket", "bollinger_position", "primary_regime", "primary_profile")
INCONCLUSIVE_STATUS = "VALIDACAO_INCONCLUSIVA"


@dataclass(frozen=True)
class ScientificRobustnessValidationConfig:
    phase6_csv: str = "optimization/results/fase6_discovery2_clusters.csv"
    candidate_csv: str = "optimization/results/fase7_scientific_candidates.csv"
    events_glob: str = "optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv"
    train_ratio: float = 0.60
    validation_ratio: float = 0.20
    min_support: int = 25
    max_rule_coverage: float = 0.95
    min_discrimination_gap: float = 0.04
    min_scientific_score: float = 75.0
    min_generalization_score: float = 0.60
    min_robustness_score: float = 0.55
    min_files: int = 75
    min_events: int = 17_000_000
    min_assets: int = 10
    min_timeframes: int = 4
    min_context_events: int = 100
    min_coverage_days: int = 1000
    min_contexts: int = 2
    output_prefix: str = "scientific_robustness_validation"
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

    train = ordered.iloc[:train_end].copy()
    val = ordered.iloc[train_end:val_end].copy()
    test = ordered.iloc[val_end:].copy()
    return train, val, test


def is_trivial_rule(*, support_share: float, precision: float, base_rate: float, max_rule_coverage: float, min_discrimination_gap: float) -> bool:
    if support_share >= max_rule_coverage:
        return True
    return abs(precision - base_rate) < min_discrimination_gap


def compute_scientific_robustness_score(
    *,
    temporal_robustness: float,
    asset_robustness: float,
    regime_robustness: float,
    generalization_score: float,
    operational_edge_score: float,
    statistical_stability: float,
) -> float:
    op_norm = max(0.0, min(1.0, operational_edge_score / 100.0))
    score = (
        20.0 * temporal_robustness
        + 18.0 * asset_robustness
        + 18.0 * regime_robustness
        + 18.0 * generalization_score
        + 16.0 * op_norm
        + 10.0 * statistical_stability
    )
    return float(score)


def classify_dataset(*, files: int, events: int, assets: int, timeframes: int, min_files: int, min_events: int, min_assets: int, min_timeframes: int) -> str:
    full_files = max(75, min_files)
    full_events = max(17_000_000, min_events)
    full_assets = max(10, min_assets)
    full_timeframes = max(4, min_timeframes)

    if files >= full_files and events >= full_events and assets >= full_assets and timeframes >= full_timeframes:
        return "FULL_DATASET"

    ratios = [
        files / max(1, min_files),
        events / max(1, min_events),
        assets / max(1, min_assets),
        timeframes / max(1, min_timeframes),
    ]
    min_ratio = min(ratios)
    if min_ratio >= 1.0:
        return "REPRESENTATIVE_SAMPLE"
    if min_ratio >= 0.5:
        return "LIMITED_SAMPLE"
    return "INSUFFICIENT_SAMPLE"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return default
    if math.isfinite(out):
        return out
    return default


def _signed_returns(frame: pd.DataFrame, expected_side: str) -> pd.Series:
    values = pd.to_numeric(frame["future_return"], errors="coerce").fillna(0.0)
    return values if expected_side == "LONG" else -values


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


def _metrics_from_returns(returns: pd.Series) -> dict[str, float]:
    clean = pd.to_numeric(returns, errors="coerce").fillna(0.0)
    return {
        "trades": float(len(clean)),
        "profit_factor": _safe_float(profit_factor_from_pnl(clean), 0.0),
        "sharpe": _safe_float(sharpe_from_pnl(clean), 0.0),
        "expectancy": _safe_float(expectancy_from_pnl(clean), 0.0),
        "win_rate": _safe_float(win_rate_from_pnl(clean), 0.0),
        "drawdown": _safe_float(max_drawdown_from_pnl(clean), 0.0),
    }


def _group_robustness(frame: pd.DataFrame, mask: pd.Series, expected_side: str, key: str, min_trades: int = 12) -> tuple[float, list[dict[str, Any]]]:
    selected = frame[mask]
    if selected.empty or key not in selected.columns:
        return 0.0, []

    rows: list[dict[str, Any]] = []
    expectancy_values: list[float] = []
    for name, chunk in selected.groupby(key, observed=True):
        signed = _signed_returns(chunk, expected_side)
        metrics = _metrics_from_returns(signed)
        if int(metrics["trades"]) < min_trades:
            continue
        expectancy_values.append(metrics["expectancy"])
        rows.append({"group": str(name), **metrics})

    if len(expectancy_values) <= 1:
        return 0.45, rows

    arr = np.asarray(expectancy_values, dtype=float)
    mean = float(np.mean(arr))
    if abs(mean) < 1e-12:
        return 0.0, rows
    cv = float(np.std(arr, ddof=0) / abs(mean))
    return float(max(0.0, min(1.0, 1.0 - cv))), rows


def _temporal_bucket(value: Any) -> str:
    try:
        year = pd.Timestamp(value).year
    except Exception:
        return "unknown"
    if year < 2020:
        return "2018-2020"
    if year < 2022:
        return "2020-2022"
    if year < 2024:
        return "2022-2024"
    return "2024-2026"


def _degradation(train: float, other: float, positive_is_better: bool = True) -> float:
    if positive_is_better:
        if train <= 1e-12:
            return 0.0
        return max(0.0, (train - other) / abs(train))
    if abs(train) <= 1e-12:
        return 0.0
    return max(0.0, (other - train) / abs(train))


def _dataset_audit(
    *,
    events_files: list[Path],
    total_events: int,
    assets_seen: set[str],
    timeframes_seen: set[str],
    min_open_time: pd.Timestamp | None,
    max_open_time: pd.Timestamp | None,
    context_occurrences: dict[str, int],
    cfg: ScientificRobustnessValidationConfig,
) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
    coverage_days = 0
    if min_open_time is not None and max_open_time is not None:
        coverage_days = int((max_open_time - min_open_time).days)

    contexts_present = int(sum(1 for v in context_occurrences.values() if v > 0))
    files_count = len(events_files)
    assets_count = len(assets_seen)
    timeframes_count = len(timeframes_seen)

    classification = classify_dataset(
        files=files_count,
        events=total_events,
        assets=assets_count,
        timeframes=timeframes_count,
        min_files=cfg.min_files,
        min_events=cfg.min_events,
        min_assets=cfg.min_assets,
        min_timeframes=cfg.min_timeframes,
    )

    guardrails: list[dict[str, Any]] = [
        {"name": "min_files", "expected": cfg.min_files, "found": files_count, "passed": files_count >= cfg.min_files},
        {"name": "min_events", "expected": cfg.min_events, "found": total_events, "passed": total_events >= cfg.min_events},
        {"name": "min_assets", "expected": cfg.min_assets, "found": assets_count, "passed": assets_count >= cfg.min_assets},
        {"name": "min_timeframes", "expected": cfg.min_timeframes, "found": timeframes_count, "passed": timeframes_count >= cfg.min_timeframes},
        {"name": "min_coverage_days", "expected": cfg.min_coverage_days, "found": coverage_days, "passed": coverage_days >= cfg.min_coverage_days},
        {"name": "min_contexts", "expected": cfg.min_contexts, "found": contexts_present, "passed": contexts_present >= cfg.min_contexts},
    ]

    for ctx, occ in context_occurrences.items():
        guardrails.append(
            {
                "name": f"min_context_events:{ctx}",
                "expected": cfg.min_context_events,
                "found": int(occ),
                "passed": int(occ) >= cfg.min_context_events,
            }
        )

    guardrails_passed = all(item["passed"] for item in guardrails)
    recommendation = (
        "python main.py robustness-validation --events-glob "
        '"optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv"'
    )

    audit = {
        "files": files_count,
        "events": int(total_events),
        "assets": assets_count,
        "timeframes": timeframes_count,
        "contexts": contexts_present,
        "context_occurrences": {k: int(v) for k, v in context_occurrences.items()},
        "time_range": {
            "min_open_time": min_open_time.isoformat() if min_open_time is not None else None,
            "max_open_time": max_open_time.isoformat() if max_open_time is not None else None,
            "coverage_days": coverage_days,
        },
        "classification": classification,
        "guardrails": guardrails,
        "guardrails_passed": guardrails_passed,
        "recommended_command_if_insufficient": recommendation,
        "expected_full_dataset": {
            "files": max(75, cfg.min_files),
            "events": max(17_744_835, cfg.min_events),
            "events_glob": "optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv",
        },
    }
    return audit, guardrails, guardrails_passed


class ScientificRobustnessValidationService:
    def __init__(self, session: Session, base_dir: Path) -> None:
        self._session = session
        self._base_dir = base_dir

    def _persist_run(
        self,
        *,
        run_id: str,
        status: str,
        decision: str,
        approved: bool,
        report: dict[str, Any],
        outputs: dict[str, str],
        best: dict[str, Any] | None,
        rejection_reason: str | None,
        cfg: ScientificRobustnessValidationConfig,
    ) -> None:
        if not cfg.persist_to_db:
            return
        repo = ScientificRobustnessValidationRunRepository(self._session)
        repo.save(
            ScientificRobustnessValidationRun(
                run_id=run_id,
                status=status,
                decision=decision,
                approved=approved,
                candidate_cluster_id=None if best is None else str(best.get("cluster_id")),
                candidate_rule=None if best is None else str(best.get("rule")),
                scientific_robustness_score=0.0 if best is None else _safe_float(best.get("scientific_robustness_score")),
                operational_edge_score=0.0 if best is None else _safe_float(best.get("operational_edge_score")),
                temporal_robustness=0.0 if best is None else _safe_float(best.get("temporal_robustness")),
                asset_robustness=0.0 if best is None else _safe_float(best.get("asset_robustness")),
                regime_robustness=0.0 if best is None else _safe_float(best.get("regime_robustness")),
                generalization_score=0.0 if best is None else _safe_float(best.get("generalization_score")),
                statistical_stability=0.0 if best is None else _safe_float(best.get("statistical_stability")),
                rejection_reason=rejection_reason,
                artifacts_json=json.dumps(outputs, ensure_ascii=True),
                summary_json=json.dumps(report, ensure_ascii=True),
            )
        )

    def run(self, config: ScientificRobustnessValidationConfig | None = None) -> dict[str, Any]:
        cfg = config or ScientificRobustnessValidationConfig()
        phase6_path = self._base_dir / cfg.phase6_csv
        events_files = sorted(self._base_dir.glob(cfg.events_glob))
        if not events_files:
            raise ValueError(f"No event files found for glob: {cfg.events_glob}")

        phase6 = pd.read_csv(phase6_path)
        contextual = phase6[phase6["tradability_class"].astype(str) == "Contextual"].copy()
        if contextual.empty:
            raise ValueError("No contextual clusters available in fase6_discovery2_clusters.csv")

        key_to_cluster = dict(zip(contextual["cluster_key"].astype(str), contextual["cluster_id"].astype(str), strict=False))

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
            "primary_regime",
            "primary_profile",
            "future_return",
            "future_upside",
            "future_downside",
            "regime",
        ]

        by_cluster: dict[str, list[pd.DataFrame]] = {str(cid): [] for cid in contextual["cluster_id"].astype(str).tolist()}
        context_occurrences: dict[str, int] = {str(cid): 0 for cid in contextual["cluster_id"].astype(str).tolist()}
        assets_seen: set[str] = set()
        timeframes_seen: set[str] = set()
        min_open_time: pd.Timestamp | None = None
        max_open_time: pd.Timestamp | None = None
        total_events = 0

        for path in events_files:
            for chunk in pd.read_csv(path, usecols=usecols, chunksize=250_000, low_memory=False):
                total_events += int(len(chunk))
                if "symbol" in chunk.columns:
                    assets_seen.update(chunk["symbol"].astype(str).dropna().unique().tolist())
                if "timeframe" in chunk.columns:
                    timeframes_seen.update(chunk["timeframe"].astype(str).dropna().unique().tolist())
                ot = pd.to_datetime(chunk["open_time"], errors="coerce", utc=True)
                if not ot.dropna().empty:
                    local_min = ot.min()
                    local_max = ot.max()
                    min_open_time = local_min if min_open_time is None else min(min_open_time, local_min)
                    max_open_time = local_max if max_open_time is None else max(max_open_time, local_max)

                chunk = chunk.dropna(subset=["regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "direction"])
                if chunk.empty:
                    continue
                cluster_key = (
                    chunk["regime"].astype(str)
                    + "|"
                    + chunk["atr_bucket"].astype(str)
                    + "|"
                    + chunk["rsi_bucket"].astype(str)
                    + "|"
                    + chunk["volume_bucket"].astype(str)
                    + "|"
                    + chunk["bollinger_position"].astype(str)
                    + "|"
                    + chunk["direction"].astype(str)
                )
                chunk["cluster_id"] = cluster_key.map(key_to_cluster)
                subset = chunk[chunk["cluster_id"].notna()].copy()
                if subset.empty:
                    continue
                for cid, grp in subset.groupby("cluster_id", observed=True):
                    sid = str(cid)
                    by_cluster[sid].append(grp)
                    context_occurrences[sid] += int(len(grp))

        audit, guardrails, guardrails_passed = _dataset_audit(
            events_files=events_files,
            total_events=total_events,
            assets_seen=assets_seen,
            timeframes_seen=timeframes_seen,
            min_open_time=min_open_time,
            max_open_time=max_open_time,
            context_occurrences=context_occurrences,
            cfg=cfg,
        )

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        run_id = str(uuid4())
        out_json = self._base_dir / "optimization" / "results" / f"{cfg.output_prefix}_{ts}.json"
        out_csv = self._base_dir / "optimization" / "results" / f"{cfg.output_prefix}_{ts}.csv"
        out_md = self._base_dir / "optimization" / "results" / f"{cfg.output_prefix}_{ts}.md"
        out_json.parent.mkdir(parents=True, exist_ok=True)

        if not guardrails_passed:
            failed = [g for g in guardrails if not g["passed"]]
            report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "phase": "FASE 7.1.1 - Guardrails Cientificos",
                "status": INCONCLUSIVE_STATUS,
                "dataset_audit": audit,
                "failed_guardrails": failed,
                "decision": None,
                "recommendation": {
                    "message": "Amostra insuficiente para conclusao cientifica. Rode com corpus completo.",
                    "command": audit["recommended_command_if_insufficient"],
                },
            }
            pd.DataFrame().to_csv(out_csv, index=False)
            out_json.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

            md = [
                "# Scientific Robustness Validation",
                "",
                f"## Status\n- {INCONCLUSIVE_STATUS}",
                "",
                "## Dataset Audit",
                f"- files: {audit['files']}",
                f"- events: {audit['events']}",
                f"- assets: {audit['assets']}",
                f"- timeframes: {audit['timeframes']}",
                f"- classification: {audit['classification']}",
                "",
                "## Guardrails Failed",
            ]
            for g in failed:
                md.append(f"- {g['name']}: expected={g['expected']} found={g['found']}")
            md.append("")
            md.append("## Recomendacao")
            md.append(f"- comando: {audit['recommended_command_if_insufficient']}")
            out_md.write_text("\n".join(md), encoding="utf-8")

            outputs = {"csv": str(out_csv), "json": str(out_json), "md": str(out_md)}
            self._persist_run(
                run_id=run_id,
                status="inconclusive",
                decision=INCONCLUSIVE_STATUS,
                approved=False,
                report=report,
                outputs=outputs,
                best=None,
                rejection_reason="Guardrails failed; scientific validation inconclusive.",
                cfg=cfg,
            )
            logger.info("Scientific robustness validation blocked by guardrails | classification=%s", audit["classification"])
            return {
                "summary": {
                    "run_id": run_id,
                    "status": INCONCLUSIVE_STATUS,
                    "dataset_classification": audit["classification"],
                    "failed_guardrails": len(failed),
                    "decision": None,
                    "approved": False,
                },
                "outputs": outputs,
                "report": report,
            }

        candidate_rows: list[dict[str, Any]] = []
        discarded_rows: list[dict[str, Any]] = []
        contexts_summary: list[dict[str, Any]] = []

        for _, ctx in contextual.iterrows():
            cid = str(ctx["cluster_id"])
            expected_side = "LONG" if str(ctx["direction"]).upper() == "BUY" else "SHORT"
            frame = pd.concat(by_cluster[cid], ignore_index=True) if by_cluster[cid] else pd.DataFrame(columns=usecols + ["cluster_id"])
            if frame.empty:
                continue

            train, val, test = temporal_split_frame(frame, cfg.train_ratio, cfg.validation_ratio)
            train_target = (_signed_returns(train, expected_side) > 0).astype(int)
            base_rate = float(train_target.mean()) if len(train_target) else 0.0

            contexts_summary.append(
                {
                    "cluster_id": cid,
                    "expected_side": expected_side,
                    "sample_size": int(len(frame)),
                    "train": int(len(train)),
                    "validation": int(len(val)),
                    "test": int(len(test)),
                    "first_open_time": str(train["open_time"].min()) if not train.empty else None,
                    "last_open_time": str(test["open_time"].max()) if not test.empty else None,
                }
            )

            conditions: list[AtomicCondition] = []
            for col in PRE_ENTRY_NUMERIC:
                if col not in train.columns:
                    continue
                vals = pd.to_numeric(train[col], errors="coerce").dropna()
                if vals.empty:
                    continue
                for q in (0.35, 0.50, 0.65):
                    thr = float(vals.quantile(q))
                    conditions.append(AtomicCondition(col, ">=", round(thr, 6)))
                    conditions.append(AtomicCondition(col, "<=", round(thr, 6)))

            for col in PRE_ENTRY_CAT:
                if col not in train.columns:
                    continue
                vc = train[col].astype(str).value_counts(dropna=False)
                for value, count in vc.head(5).items():
                    if int(count) < cfg.min_support:
                        continue
                    conditions.append(AtomicCondition(col, "==", str(value)))

            for cond in conditions:
                train_mask = _rule_mask(train, cond)
                support = int(train_mask.sum())
                if support < cfg.min_support:
                    continue
                support_share = support / max(1, len(train))
                precision = float(train_target[train_mask].mean()) if support else 0.0
                lift = precision / base_rate if base_rate > 1e-12 else 0.0

                if is_trivial_rule(
                    support_share=support_share,
                    precision=precision,
                    base_rate=base_rate,
                    max_rule_coverage=cfg.max_rule_coverage,
                    min_discrimination_gap=cfg.min_discrimination_gap,
                ):
                    reason = "coverage_excessiva" if support_share >= cfg.max_rule_coverage else "baixo_poder_discriminatorio"
                    discarded_rows.append(
                        {
                            "cluster_id": cid,
                            "expected_side": expected_side,
                            "rule": cond.expr,
                            "support": support,
                            "support_share": support_share,
                            "precision": precision,
                            "base_rate": base_rate,
                            "reason": reason,
                        }
                    )
                    continue

                train_returns = _signed_returns(train[train_mask], expected_side)
                train_metrics = _metrics_from_returns(train_returns)

                full_mask = _rule_mask(frame, cond)
                sub = frame[full_mask].copy()
                if sub.empty:
                    continue
                if expected_side == "LONG":
                    adverse = (-pd.to_numeric(sub["future_downside"], errors="coerce").fillna(0.0)).clip(lower=0.0)
                    favorable = pd.to_numeric(sub["future_upside"], errors="coerce").fillna(0.0).clip(lower=0.0)
                else:
                    adverse = pd.to_numeric(sub["future_upside"], errors="coerce").fillna(0.0).clip(lower=0.0)
                    favorable = (-pd.to_numeric(sub["future_downside"], errors="coerce").fillna(0.0)).clip(lower=0.0)

                exit_rule = {
                    "stop_loss_pct": round(float(adverse.quantile(0.80)), 6),
                    "take_profit_pct": round(float(favorable.quantile(0.60)), 6),
                    "max_hold_minutes": int(max(5, pd.to_numeric(sub["duration_minutes"], errors="coerce").fillna(0.0).quantile(0.75))),
                }

                candidate_rows.append(
                    {
                        "cluster_id": cid,
                        "expected_side": expected_side,
                        "rule": cond.expr,
                        "support": support,
                        "support_share": support_share,
                        "precision": precision,
                        "base_rate": base_rate,
                        "lift": lift,
                        "train_profit_factor": train_metrics["profit_factor"],
                        "train_sharpe": train_metrics["sharpe"],
                        "train_expectancy": train_metrics["expectancy"],
                        "train_win_rate": train_metrics["win_rate"],
                        "train_drawdown": train_metrics["drawdown"],
                        "train_trades": train_metrics["trades"],
                        "exit_rule": exit_rule,
                        "condition_column": cond.column,
                        "condition_op": cond.op,
                        "condition_value": cond.value,
                    }
                )

        if not candidate_rows:
            report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "phase": "FASE 7.1 - Validacao de Robustez Cientifica",
                "status": "COMPLETED",
                "dataset_audit": audit,
                "etapa1_temporal_split": {
                    "train_ratio": cfg.train_ratio,
                    "validation_ratio": cfg.validation_ratio,
                    "test_ratio": round(1.0 - cfg.train_ratio - cfg.validation_ratio, 4),
                    "contexts": contexts_summary,
                },
                "etapa5_trivial_rules_elimination": {
                    "discarded_count": int(len(discarded_rows)),
                    "discarded_rules": discarded_rows[:500],
                },
                "best_candidate": None,
                "decision": "B",
                "rejection_reason": "Nenhuma regra nao-trivial encontrada no Train apos eliminacao automatica de regras triviais.",
            }
            pd.DataFrame().to_csv(out_csv, index=False)
            out_json.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")
            out_md.write_text(
                "\n".join(
                    [
                        "# Scientific Robustness Validation",
                        "",
                        "## Dataset Audit",
                        f"- files: {audit['files']}",
                        f"- events: {audit['events']}",
                        f"- assets: {audit['assets']}",
                        f"- timeframes: {audit['timeframes']}",
                        f"- classification: {audit['classification']}",
                        "",
                        "## Summary",
                        "- Decision: B",
                        "- Motivo: Nenhuma regra nao-trivial encontrada no Train.",
                    ]
                ),
                encoding="utf-8",
            )
            outputs = {"csv": str(out_csv), "json": str(out_json), "md": str(out_md)}
            self._persist_run(
                run_id=run_id,
                status="completed",
                decision="B",
                approved=False,
                report=report,
                outputs=outputs,
                best=None,
                rejection_reason="No non-trivial candidates found after train-only discovery.",
                cfg=cfg,
            )
            return {
                "summary": {
                    "run_id": run_id,
                    "status": "COMPLETED",
                    "decision": "B",
                    "approved": False,
                    "best_candidate": None,
                    "best_rule": None,
                    "scientific_robustness_score": 0.0,
                    "operational_edge_score": 0.0,
                    "discarded_trivial_rules": int(len(discarded_rows)),
                    "evaluated_candidates": 0,
                    "dataset_classification": audit["classification"],
                },
                "outputs": outputs,
                "report": report,
            }

        candidates = pd.DataFrame(candidate_rows)
        candidates = candidates.sort_values(["train_expectancy", "support", "lift"], ascending=[False, False, False]).reset_index(drop=True)
        candidates["rank"] = np.arange(1, len(candidates) + 1)

        evaluations: list[dict[str, Any]] = []
        for _, row in candidates.iterrows():
            cid = str(row["cluster_id"])
            expected_side = str(row["expected_side"])
            frame = pd.concat(by_cluster[cid], ignore_index=True) if by_cluster[cid] else pd.DataFrame(columns=usecols + ["cluster_id"])
            train, val, test = temporal_split_frame(frame, cfg.train_ratio, cfg.validation_ratio)

            cond = AtomicCondition(str(row["condition_column"]), str(row["condition_op"]), row["condition_value"])
            train_mask = _rule_mask(train, cond)
            val_mask = _rule_mask(val, cond)
            test_mask = _rule_mask(test, cond)

            train_metrics = _metrics_from_returns(_signed_returns(train[train_mask], expected_side))
            val_metrics = _metrics_from_returns(_signed_returns(val[val_mask], expected_side))
            test_metrics = _metrics_from_returns(_signed_returns(test[test_mask], expected_side))

            full = frame.copy()
            full["period_bucket"] = pd.to_datetime(full["open_time"], errors="coerce", utc=True).map(_temporal_bucket)
            full_mask = _rule_mask(full, cond)

            temporal_rob, temporal_rows = _group_robustness(full, full_mask, expected_side, "period_bucket", min_trades=8)
            asset_rob, asset_rows = _group_robustness(full, full_mask, expected_side, "symbol", min_trades=12)
            regime_rob, regime_rows = _group_robustness(full, full_mask, expected_side, "primary_regime", min_trades=12)

            pf_deg = (_degradation(train_metrics["profit_factor"], val_metrics["profit_factor"]) + _degradation(train_metrics["profit_factor"], test_metrics["profit_factor"])) / 2.0
            sharpe_deg = (_degradation(train_metrics["sharpe"], val_metrics["sharpe"]) + _degradation(train_metrics["sharpe"], test_metrics["sharpe"])) / 2.0
            exp_deg = (_degradation(train_metrics["expectancy"], val_metrics["expectancy"]) + _degradation(train_metrics["expectancy"], test_metrics["expectancy"])) / 2.0
            dd_deg = (_degradation(train_metrics["drawdown"], val_metrics["drawdown"], positive_is_better=False) + _degradation(train_metrics["drawdown"], test_metrics["drawdown"], positive_is_better=False)) / 2.0
            avg_deg = (pf_deg + sharpe_deg + exp_deg + dd_deg) / 4.0
            generalization_score = float(max(0.0, min(1.0, 1.0 - avg_deg)))

            stability = float(max(0.0, min(1.0, (temporal_rob + asset_rob + regime_rob) / 3.0)))
            operational_edge_score = float(max(0.0, min(100.0, 35.0 * min(2.0, train_metrics["profit_factor"]) / 2.0 + 35.0 * min(1.0, train_metrics["expectancy"] + 0.2) + 30.0 * stability)))

            sci_score = compute_scientific_robustness_score(
                temporal_robustness=temporal_rob,
                asset_robustness=asset_rob,
                regime_robustness=regime_rob,
                generalization_score=generalization_score,
                operational_edge_score=operational_edge_score,
                statistical_stability=stability,
            )

            edge_train = train_metrics["profit_factor"] > 1.0 and train_metrics["expectancy"] > 0 and train_metrics["trades"] >= cfg.min_support
            edge_val = val_metrics["profit_factor"] > 1.0 and val_metrics["expectancy"] > 0 and val_metrics["trades"] >= 8
            edge_test = test_metrics["profit_factor"] > 1.0 and test_metrics["expectancy"] > 0 and test_metrics["trades"] >= 8

            approved = bool(
                edge_train
                and edge_val
                and edge_test
                and generalization_score >= cfg.min_generalization_score
                and min(temporal_rob, asset_rob, regime_rob) >= cfg.min_robustness_score
                and sci_score >= cfg.min_scientific_score
            )

            reasons: list[str] = []
            if not edge_train:
                reasons.append("edge_train_falhou")
            if not edge_val:
                reasons.append("edge_validation_falhou")
            if not edge_test:
                reasons.append("edge_test_falhou")
            if generalization_score < cfg.min_generalization_score:
                reasons.append("generalizacao_insuficiente")
            if min(temporal_rob, asset_rob, regime_rob) < cfg.min_robustness_score:
                reasons.append("robustez_insuficiente")
            if sci_score < cfg.min_scientific_score:
                reasons.append("scientific_score_abaixo_do_limiar")

            evaluations.append(
                {
                    "rank": int(row["rank"]),
                    "cluster_id": cid,
                    "expected_side": expected_side,
                    "rule": str(row["rule"]),
                    "support": int(row["support"]),
                    "support_share": float(row["support_share"]),
                    "precision": float(row["precision"]),
                    "lift": float(row["lift"]),
                    "train_profit_factor": train_metrics["profit_factor"],
                    "train_sharpe": train_metrics["sharpe"],
                    "train_expectancy": train_metrics["expectancy"],
                    "train_drawdown": train_metrics["drawdown"],
                    "train_trades": train_metrics["trades"],
                    "val_profit_factor": val_metrics["profit_factor"],
                    "val_sharpe": val_metrics["sharpe"],
                    "val_expectancy": val_metrics["expectancy"],
                    "val_drawdown": val_metrics["drawdown"],
                    "val_trades": val_metrics["trades"],
                    "test_profit_factor": test_metrics["profit_factor"],
                    "test_sharpe": test_metrics["sharpe"],
                    "test_expectancy": test_metrics["expectancy"],
                    "test_drawdown": test_metrics["drawdown"],
                    "test_trades": test_metrics["trades"],
                    "temporal_robustness": temporal_rob,
                    "asset_robustness": asset_rob,
                    "regime_robustness": regime_rob,
                    "generalization_score": generalization_score,
                    "statistical_stability": stability,
                    "operational_edge_score": operational_edge_score,
                    "scientific_robustness_score": sci_score,
                    "approved": approved,
                    "rejection_reason": ";".join(reasons),
                    "edge_train": edge_train,
                    "edge_validation": edge_val,
                    "edge_test": edge_test,
                    "exit_rule": row["exit_rule"],
                    "regime_metrics": regime_rows,
                    "asset_metrics": asset_rows,
                    "temporal_metrics": temporal_rows,
                }
            )

        evaluated = pd.DataFrame(evaluations)
        evaluated = evaluated.sort_values(["scientific_robustness_score", "support"], ascending=[False, False]).reset_index(drop=True)
        evaluated["rank"] = np.arange(1, len(evaluated) + 1)
        best = evaluated.iloc[0]

        decision = "A" if bool(best["approved"]) else "B"
        report = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "phase": "FASE 7.1 - Validacao de Robustez Cientifica",
            "status": "COMPLETED",
            "dataset_audit": audit,
            "etapa1_temporal_split": {
                "train_ratio": cfg.train_ratio,
                "validation_ratio": cfg.validation_ratio,
                "test_ratio": round(1.0 - cfg.train_ratio - cfg.validation_ratio, 4),
                "contexts": contexts_summary,
            },
            "etapa2_regime_robustness": evaluated[["cluster_id", "rule", "regime_robustness"]].head(50).to_dict(orient="records"),
            "etapa3_asset_robustness": evaluated[["cluster_id", "rule", "asset_robustness"]].head(50).to_dict(orient="records"),
            "etapa4_temporal_robustness": evaluated[["cluster_id", "rule", "temporal_robustness"]].head(50).to_dict(orient="records"),
            "etapa5_trivial_rules_elimination": {
                "discarded_count": int(len(discarded_rows)),
                "discarded_rules": discarded_rows[:500],
            },
            "etapa6_generalization": evaluated[
                [
                    "cluster_id",
                    "rule",
                    "train_profit_factor",
                    "val_profit_factor",
                    "test_profit_factor",
                    "train_expectancy",
                    "val_expectancy",
                    "test_expectancy",
                    "train_drawdown",
                    "val_drawdown",
                    "test_drawdown",
                    "generalization_score",
                ]
            ].head(50).to_dict(orient="records"),
            "etapa7_scientific_score": {
                "threshold": cfg.min_scientific_score,
                "top_candidates": evaluated[
                    [
                        "rank",
                        "cluster_id",
                        "rule",
                        "scientific_robustness_score",
                        "operational_edge_score",
                        "approved",
                        "rejection_reason",
                    ]
                ].head(50).to_dict(orient="records"),
            },
            "best_candidate": {
                "cluster_id": str(best["cluster_id"]),
                "rule": str(best["rule"]),
                "expected_side": str(best["expected_side"]),
                "scientific_robustness_score": float(best["scientific_robustness_score"]),
                "operational_edge_score": float(best["operational_edge_score"]),
                "temporal_robustness": float(best["temporal_robustness"]),
                "asset_robustness": float(best["asset_robustness"]),
                "regime_robustness": float(best["regime_robustness"]),
                "generalization_score": float(best["generalization_score"]),
                "approved": bool(best["approved"]),
                "rejection_reason": str(best["rejection_reason"]),
                "exit_rule": best["exit_rule"],
            },
            "decision": decision,
        }

        flat = evaluated.copy()
        for complex_col in ["exit_rule", "regime_metrics", "asset_metrics", "temporal_metrics"]:
            flat[complex_col] = flat[complex_col].map(lambda x: json.dumps(x, ensure_ascii=True))

        flat.to_csv(out_csv, index=False)
        out_json.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

        md_lines = [
            "# Scientific Robustness Validation",
            "",
            "## Dataset Audit",
            f"- files: {audit['files']}",
            f"- events: {audit['events']}",
            f"- assets: {audit['assets']}",
            f"- timeframes: {audit['timeframes']}",
            f"- classification: {audit['classification']}",
            "",
            "## Summary",
            f"- Contextual clusters: {len(contextual)}",
            f"- Candidates after trivial-rule elimination: {len(evaluated)}",
            f"- Trivial rules discarded: {len(discarded_rows)}",
            f"- Best candidate: {best['cluster_id']} | rule={best['rule']}",
            f"- Scientific Robustness Score: {float(best['scientific_robustness_score']):.2f}",
            f"- Operational Edge Score: {float(best['operational_edge_score']):.2f}",
            f"- Decision: {decision}",
            "",
            "## Top 10",
        ]
        for _, row in evaluated.head(10).iterrows():
            md_lines.append(
                f"- #{int(row['rank'])} {row['cluster_id']} {row['expected_side']} | score={float(row['scientific_robustness_score']):.2f} | "
                f"gen={float(row['generalization_score']):.3f} | robust(T/A/R)=({float(row['temporal_robustness']):.3f}/{float(row['asset_robustness']):.3f}/{float(row['regime_robustness']):.3f}) | approved={bool(row['approved'])}"
            )

        if discarded_rows:
            md_lines.append("")
            md_lines.append("## Discarded Trivial Rules (sample)")
            for row in discarded_rows[:20]:
                md_lines.append(f"- {row['cluster_id']} {row['rule']} | reason={row['reason']} | coverage={row['support_share']:.3f}")

        out_md.write_text("\n".join(md_lines), encoding="utf-8")

        outputs = {
            "csv": str(out_csv),
            "json": str(out_json),
            "md": str(out_md),
        }
        self._persist_run(
            run_id=run_id,
            status="completed",
            decision=decision,
            approved=bool(best["approved"]),
            report=report,
            outputs=outputs,
            best=best.to_dict(),
            rejection_reason=str(best["rejection_reason"]),
            cfg=cfg,
        )

        logger.info(
            "Scientific robustness validation completed | decision=%s | score=%.2f | approved=%s",
            decision,
            float(best["scientific_robustness_score"]),
            bool(best["approved"]),
        )

        return {
            "summary": {
                "run_id": run_id,
                "status": "COMPLETED",
                "dataset_classification": audit["classification"],
                "guardrails_passed": True,
                "decision": decision,
                "approved": bool(best["approved"]),
                "best_candidate": str(best["cluster_id"]),
                "best_rule": str(best["rule"]),
                "scientific_robustness_score": float(best["scientific_robustness_score"]),
                "operational_edge_score": float(best["operational_edge_score"]),
                "discarded_trivial_rules": int(len(discarded_rows)),
                "evaluated_candidates": int(len(evaluated)),
            },
            "outputs": outputs,
            "report": report,
        }
