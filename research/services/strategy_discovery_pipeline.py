from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pandas as pd
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from config.settings import settings
from database.history_models import StrategyFamilyCatalog, StrategyFamilyDiscoveryRun
from database.history_repositories import StrategyFamilyCatalogRepository, StrategyFamilyDiscoveryRunRepository
from strategies.registry import discover_strategies, get_registration, list_strategy_families
from utils.logger import get_logger

logger = get_logger(__name__)

STATUS_NOT_TESTED = "NOT_TESTED"
STATUS_RUNNING = "RUNNING"
STATUS_APPROVED = "APPROVED"
STATUS_REJECTED = "REJECTED"
STATUS_RETEST = "RETEST"
STATUS_ARCHIVED = "ARCHIVED"

_OFFICIAL_STATUS = {
    STATUS_NOT_TESTED,
    STATUS_RUNNING,
    STATUS_APPROVED,
    STATUS_REJECTED,
    STATUS_RETEST,
    STATUS_ARCHIVED,
}


@dataclass(frozen=True)
class DiscoveryWeights:
    validation_robustness: float = 0.40
    research_lab: float = 0.20
    trade_management_lab: float = 0.20
    profit_factor: float = 0.10
    sharpe: float = 0.10

    def normalized(self) -> dict[str, float]:
        raw = asdict(self)
        total = sum(raw.values()) or 1.0
        return {key: float(value) / total for key, value in raw.items()}


@dataclass(frozen=True)
class DiscoveryPilotPlan:
    symbol: str = "BTC/USDT"
    timeframe: str = "5m"
    combinations: int = 500
    workers: int = 16


FAMILY_PRIORS: dict[str, tuple[float, str]] = {
    "breakout": (0.98, "Cheapest first pilot: existing research already identifies breakout/falso rompimento regimes."),
    "trend": (0.90, "Mature and already covered by existing strategies, but not the next discovery target."),
    "mean_reversion": (0.84, "Scientifically validated earlier, so it is lower priority for the next implementation slot."),
    "momentum": (0.78, "Useful market family, but more redundant with trend for the current phase."),
    "range": (0.72, "Environment-sensitive and often secondary to explicit breakout/trend logic."),
    "vwap": (0.68, "Useful intraday family, but data- and session-dependent."),
    "opening_range": (0.65, "Strong intraday pattern, but narrower market window than breakout."),
    "liquidity_sweep": (0.60, "Promising, but more microstructure-heavy and harder to validate quickly."),
    "market_structure": (0.58, "Conceptually important, but best layered on top of simpler families after the first pilot."),
}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return str(value)
    return value


def _normalize_status(value: Any, default: str = STATUS_NOT_TESTED) -> str:
    raw = str(value or "").strip().upper().replace(" ", "_")
    legacy = {
        "APROVADA": STATUS_APPROVED,
        "APPROVED": STATUS_APPROVED,
        "REPROVADA": STATUS_REJECTED,
        "REJECTED": STATUS_REJECTED,
        "NOT_TESTED": STATUS_NOT_TESTED,
        "RUNNING": STATUS_RUNNING,
        "RETEST": STATUS_RETEST,
        "ARCHIVED": STATUS_ARCHIVED,
    }
    normalized = legacy.get(raw, raw)
    return normalized if normalized in _OFFICIAL_STATUS else default


def _load_history_frame() -> pd.DataFrame:
    engine = create_engine(settings.database.url, future=True)
    query = """
    SELECT
        strategy,
        symbol,
        timeframe,
        trades,
        win_rate,
        profit_factor,
        sharpe,
        drawdown,
        expectancy,
        net_profit,
        approved,
        rejection_reason,
        created_at
    FROM optimization_results_history
    """
    with engine.connect() as connection:
        return pd.read_sql(query, connection)


def _strategy_to_family(strategy_name: str) -> tuple[str, str, str]:
    try:
        registration = get_registration(strategy_name)
        return registration.family, registration.name, registration.version
    except Exception:
        canonical = strategy_name.strip().lower().replace("_", "")
        return canonical, strategy_name, "v1"


def _load_trade_counts() -> pd.DataFrame:
    engine = create_engine(settings.database.url, future=True)
    query = """
    SELECT strategy, COUNT(*) AS trade_count
    FROM trade_history
    GROUP BY strategy
    """
    with engine.connect() as connection:
        frame = pd.read_sql(query, connection)

    if frame.empty:
        return pd.DataFrame(columns=["family", "trade_count"])

    frame["family"] = frame["strategy"].apply(lambda value: _strategy_to_family(str(value))[0])
    return frame.groupby("family", dropna=False)["trade_count"].sum().reset_index()


def _family_evidence_scores(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    frame = df.copy()
    for column in ["profit_factor", "sharpe", "drawdown", "expectancy", "win_rate", "net_profit"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame["approved"] = frame["approved"].fillna(False).astype(bool)
    frame["family"] = frame["strategy"].apply(lambda value: _strategy_to_family(str(value))[0])

    grouped = frame.groupby("family", dropna=False)
    summary = grouped.agg(
        tested_count=("strategy", "count"),
        approved_count=("approved", "sum"),
        best_profit_factor=("profit_factor", "max"),
        best_sharpe=("sharpe", "max"),
        best_expectancy=("expectancy", "max"),
        average_win_rate=("win_rate", "mean"),
        average_drawdown=("drawdown", "mean"),
        total_net_profit=("net_profit", "sum"),
    ).reset_index()

    summary["validation_robustness"] = (summary["approved_count"] / summary["tested_count"].replace(0, pd.NA)).fillna(0.0)
    for column in ["best_profit_factor", "best_sharpe", "best_expectancy", "average_win_rate", "average_drawdown", "total_net_profit"]:
        summary[column] = summary[column].fillna(0.0)
    return summary


def _build_catalog_rows(
    summary: pd.DataFrame,
    trade_counts: pd.DataFrame,
    pilot: DiscoveryPilotPlan,
    weights: DiscoveryWeights,
    existing_statuses: dict[str, str],
) -> pd.DataFrame:
    discover_strategies()
    families = pd.DataFrame(list_strategy_families())
    families["market_fit_score"] = families["family"].map(lambda value: FAMILY_PRIORS.get(str(value), (0.45, "Unranked family."))[0])
    families["market_reason"] = families["family"].map(lambda value: FAMILY_PRIORS.get(str(value), (0.45, "Unranked family."))[1])

    merged = families.merge(summary, on="family", how="left")
    merged = merged.merge(trade_counts, on="family", how="left")
    merged["trade_count"] = merged["trade_count"].fillna(0).astype(int)
    for column in ["tested_count", "approved_count"]:
        merged[column] = merged[column].fillna(0).astype(int)
    for column in ["best_profit_factor", "best_sharpe", "best_expectancy", "validation_robustness"]:
        merged[column] = merged[column].fillna(0.0)

    tested_max = max(int(merged["tested_count"].max() or 0), 1)
    trade_max = max(int(merged["trade_count"].max() or 0), 1)

    merged["research_lab_score"] = (merged["tested_count"] / tested_max).clip(lower=0.0, upper=1.0)
    merged["trade_management_lab_score"] = (merged["trade_count"] / trade_max).clip(lower=0.0, upper=1.0)
    merged["profit_factor_score"] = merged["best_profit_factor"].rank(pct=True, ascending=False).fillna(0.0)
    merged["sharpe_score"] = merged["best_sharpe"].rank(pct=True, ascending=False).fillna(0.0)

    merged["research_status"] = merged["tested_count"].apply(
        lambda value: STATUS_NOT_TESTED if int(value) == 0 else STATUS_APPROVED
    )
    merged["validation_status"] = merged.apply(
        lambda row: STATUS_APPROVED
        if row["approved_count"] > 0 and row["best_profit_factor"] > 1.0 and row["best_sharpe"] >= 0
        else (STATUS_NOT_TESTED if row["tested_count"] == 0 else STATUS_REJECTED),
        axis=1,
    )
    merged["trade_management_status"] = merged["trade_count"].apply(
        lambda value: STATUS_APPROVED if int(value) > 0 else STATUS_NOT_TESTED
    )
    merged["status"] = merged.apply(
        lambda row: STATUS_NOT_TESTED
        if row["tested_count"] == 0
        else (STATUS_APPROVED if row["validation_status"] == STATUS_APPROVED else STATUS_REJECTED),
        axis=1,
    )

    normalized_weights = weights.normalized()
    merged["discovery_score"] = (
        normalized_weights["validation_robustness"] * merged["validation_robustness"].fillna(0.0)
        + normalized_weights["research_lab"] * merged["research_lab_score"].fillna(0.0)
        + normalized_weights["trade_management_lab"] * merged["trade_management_lab_score"].fillna(0.0)
        + normalized_weights["profit_factor"] * merged["profit_factor_score"].fillna(0.0)
        + normalized_weights["sharpe"] * merged["sharpe_score"].fillna(0.0)
    ).round(4)

    merged["discard_reason"] = ""
    for index in merged.index:
        family = str(merged.at[index, "family"])
        historical_status = _normalize_status(existing_statuses.get(family), default="")
        if historical_status == STATUS_REJECTED:
            merged.at[index, "status"] = STATUS_REJECTED
            merged.at[index, "validation_status"] = STATUS_REJECTED
            merged.at[index, "discard_reason"] = "Historical lock: REJECTED families require explicit RETEST/reset before recommendation."
        elif historical_status == STATUS_ARCHIVED:
            merged.at[index, "status"] = STATUS_ARCHIVED
            merged.at[index, "discard_reason"] = "Historical lock: ARCHIVED family is excluded from active recommendation queue."
        elif historical_status == STATUS_RETEST:
            merged.at[index, "status"] = STATUS_RETEST
            merged.at[index, "discard_reason"] = "Explicit RETEST status set; family can be considered after NOT_TESTED and APPROVED."

        if merged.at[index, "validation_status"] == STATUS_REJECTED and not merged.at[index, "discard_reason"]:
            merged.at[index, "discard_reason"] = "Validation gate failed; optimizer metrics cannot compensate a rejected validation state."

    merged["reason"] = merged.apply(
        lambda row: row["market_reason"]
        if row["tested_count"] == 0
        else f"best_pf={row['best_profit_factor']:.4f}; best_sharpe={row['best_sharpe']:.4f}; validation={row['validation_status']}; trade_management={row['trade_management_status']}",
        axis=1,
    )
    merged["pilot_symbol"] = pilot.symbol
    merged["pilot_timeframe"] = pilot.timeframe
    merged["pilot_combinations"] = pilot.combinations
    merged["pilot_workers"] = pilot.workers

    status_priority = {
        STATUS_NOT_TESTED: 0,
        STATUS_APPROVED: 1,
        STATUS_RETEST: 2,
        STATUS_RUNNING: 3,
        STATUS_REJECTED: 4,
        STATUS_ARCHIVED: 5,
    }
    merged["priority"] = merged["status"].map(status_priority).fillna(99)
    merged = merged.sort_values(
        ["priority", "discovery_score", "market_fit_score", "family"],
        ascending=[True, False, False, True],
    ).reset_index(drop=True)
    merged["rank_position"] = merged.index + 1
    return merged


def _select_recommendation(catalog_frame: pd.DataFrame) -> tuple[pd.Series | None, str]:
    if catalog_frame.empty:
        return None, "No registered families available for recommendation."

    not_tested = catalog_frame[catalog_frame["status"] == STATUS_NOT_TESTED]
    if not not_tested.empty:
        selected = not_tested.sort_values(["market_fit_score", "discovery_score"], ascending=[False, False]).iloc[0]
        return selected, "Rule 4 gate applied: at least one NOT_TESTED family exists, so recommendation is forced to NOT_TESTED queue."

    approved = catalog_frame[catalog_frame["status"] == STATUS_APPROVED]
    if not approved.empty:
        selected = approved.sort_values(["discovery_score", "market_fit_score"], ascending=[False, False]).iloc[0]
        return selected, "Rule 2 priority applied: no NOT_TESTED families left, selected from APPROVED for expansion."

    retest = catalog_frame[catalog_frame["status"] == STATUS_RETEST]
    if not retest.empty:
        selected = retest.sort_values(["discovery_score", "market_fit_score"], ascending=[False, False]).iloc[0]
        return selected, "Rule 2 priority applied: selected explicit RETEST candidate after NOT_TESTED and APPROVED queues."

    return None, "No recommendable family found: only REJECTED/ARCHIVED/RUNNING remain in catalog."


def run_strategy_discovery_pipeline(
    session: Session,
    base_dir: Path,
    pilot: DiscoveryPilotPlan | None = None,
    weights: DiscoveryWeights | None = None,
) -> dict[str, Any]:
    pilot = pilot or DiscoveryPilotPlan()
    weights = weights or DiscoveryWeights()

    summary_dir = base_dir / "optimization" / "results"
    summary_dir.mkdir(parents=True, exist_ok=True)

    history = _load_history_frame()
    trade_counts = _load_trade_counts()
    weighted = _family_evidence_scores(history)

    catalog_repo = StrategyFamilyCatalogRepository(session)
    run_repo = StrategyFamilyDiscoveryRunRepository(session)
    existing_rows = catalog_repo.list_all()
    existing_statuses = {str(row.family): _normalize_status(row.status, default="") for row in existing_rows}

    catalog_frame = _build_catalog_rows(weighted, trade_counts, pilot, weights, existing_statuses)

    recommended_row, recommendation_reason = _select_recommendation(catalog_frame)
    recommended_family = str(recommended_row["family"]) if recommended_row is not None else None
    recommended_strategy = None
    recommended_version = None
    if recommended_family == "breakout":
        recommended_strategy = "BreakoutV1"
        recommended_version = "v1"
    elif recommended_family == "trend":
        recommended_strategy = "TrendV1"
        recommended_version = "v1"
    elif recommended_family == "mean_reversion":
        recommended_strategy = "MeanReversionV1"
        recommended_version = "v1"

    generated_at = datetime.now(timezone.utc)
    for _, row in catalog_frame.iterrows():
        family_name = str(row["family"])
        catalog_repo.upsert(
            StrategyFamilyCatalog(
                family=family_name,
                strategy_name=recommended_strategy if family_name == recommended_family else None,
                strategy_version=recommended_version if family_name == recommended_family else None,
                rank_position=int(row["rank_position"]),
                status=_normalize_status(row["status"]),
                validation_status=_normalize_status(row["validation_status"]),
                research_status=_normalize_status(row["research_status"]),
                trade_management_status=_normalize_status(row["trade_management_status"]),
                best_profit_factor=float(row["best_profit_factor"]),
                best_sharpe=float(row["best_sharpe"]),
                best_expectancy=float(row["best_expectancy"]),
                discovery_score=float(row["discovery_score"]),
                market_fit_score=float(row["market_fit_score"]),
                evidence_score=float(row.get("validation_robustness", 0.0)),
                tested_count=int(row["tested_count"]),
                approved_count=int(row["approved_count"]),
                pilot_symbol=str(row["pilot_symbol"]),
                pilot_timeframe=str(row["pilot_timeframe"]),
                pilot_combinations=int(row["pilot_combinations"]),
                pilot_workers=int(row["pilot_workers"]),
                reason=str(row["reason"]),
                selected_at=generated_at if family_name == recommended_family else None,
            )
        )

    ranking_records = catalog_frame.to_dict(orient="records")
    discarded_families: list[dict[str, Any]] = []
    for row in ranking_records:
        if recommended_family and str(row.get("family")) == recommended_family:
            continue
        discard_reason = str(row.get("discard_reason") or "Lower priority queue under Rule 2.")
        discarded_families.append(
            {
                "family": row.get("family"),
                "status": _normalize_status(row.get("status")),
                "discard_reason": discard_reason,
            }
        )

    recommendation_by_family = {
        str(row.get("family")): recommendation_reason if recommended_family and str(row.get("family")) == recommended_family else "Not selected."
        for row in ranking_records
    }

    audit_columns = [
        "family",
        "status",
        "discovery_score",
        "validation_status",
        "research_status",
        "trade_management_status",
        "reason",
        "discard_reason",
    ]
    audit_table = catalog_frame[audit_columns].copy() if not catalog_frame.empty else pd.DataFrame(columns=audit_columns)
    if not audit_table.empty:
        audit_table.rename(
            columns={
                "family": "Family",
                "status": "Status",
                "discovery_score": "Discovery Score",
                "validation_status": "Validation",
                "research_status": "Research",
                "trade_management_status": "Trade Management",
                "reason": "Classification Reason",
                "discard_reason": "Discard Reason",
            },
            inplace=True,
        )
        audit_table["Recommendation Reason"] = audit_table["Family"].map(recommendation_by_family)

    summary = {
        "generated_at": generated_at.isoformat(),
        "pilot": asdict(pilot),
        "weights": weights.normalized(),
        "recommended_family": recommended_family,
        "recommended_strategy": recommended_strategy,
        "recommended_version": recommended_version,
        "recommendation_reason": recommendation_reason,
        "discarded_families": discarded_families,
        "families_ranked": len(ranking_records),
        "top_3": ranking_records[:3],
        "status_counts": catalog_frame["status"].value_counts().to_dict() if not catalog_frame.empty else {},
        "pilot_standard": {
            "symbol": pilot.symbol,
            "timeframe": pilot.timeframe,
            "combinations": pilot.combinations,
            "workers": pilot.workers,
        },
    }

    run_repo.save(
        StrategyFamilyDiscoveryRun(
            run_id=str(uuid4()),
            recommended_family=recommended_family,
            recommended_strategy=recommended_strategy,
            recommended_version=recommended_version,
            symbol=pilot.symbol,
            timeframe=pilot.timeframe,
            pilot_combinations=pilot.combinations,
            pilot_workers=pilot.workers,
            weights_json=json.dumps(_json_safe(weights.normalized()), ensure_ascii=False),
            ranking_json=json.dumps(_json_safe(ranking_records), ensure_ascii=False),
            summary_json=json.dumps(_json_safe(summary), ensure_ascii=False),
        )
    )

    ranking_csv = summary_dir / "family_discovery_ranking.csv"
    ranking_json_path = summary_dir / "family_discovery_ranking.json"
    summary_json_path = summary_dir / "family_discovery_summary.json"
    summary_txt_path = summary_dir / "family_discovery_summary.txt"
    audit_txt_path = summary_dir / "family_discovery_audit_table.txt"
    audit_json_path = summary_dir / "family_discovery_audit.json"

    catalog_frame.to_csv(ranking_csv, index=False)
    ranking_json_path.write_text(json.dumps(_json_safe(ranking_records), ensure_ascii=False, indent=2), encoding="utf-8")
    summary_json_path.write_text(json.dumps(_json_safe(summary), ensure_ascii=False, indent=2), encoding="utf-8")
    summary_txt_path.write_text(
        "\n".join(
            [
                "strategy_discovery_pipeline",
                "============================",
                f"generated_at={summary['generated_at']}",
                f"recommended_family={recommended_family}",
                f"recommended_strategy={recommended_strategy}",
                f"recommended_version={recommended_version}",
                f"recommendation_reason={recommendation_reason}",
                f"pilot={json.dumps(summary['pilot'], ensure_ascii=False)}",
                f"weights={json.dumps(summary['weights'], ensure_ascii=False)}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    if not audit_table.empty:
        audit_txt_path.write_text(audit_table.to_string(index=False), encoding="utf-8")
    else:
        audit_txt_path.write_text("No audit rows generated.\n", encoding="utf-8")
    audit_json_path.write_text(
        json.dumps(
            {
                "recommendation_reason": recommendation_reason,
                "rows": _json_safe(audit_table.to_dict(orient="records")) if not audit_table.empty else [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info("Strategy discovery completed: recommended_family=%s ranked=%d", recommended_family, len(ranking_records))
    return {
        "summary": summary,
        "ranking": ranking_records,
        "audit": {
            "table": _json_safe(audit_table.to_dict(orient="records")) if not audit_table.empty else [],
            "json": {
                "recommendation_reason": recommendation_reason,
                "rows": _json_safe(audit_table.to_dict(orient="records")) if not audit_table.empty else [],
            },
        },
        "outputs": {
            "ranking_csv": str(ranking_csv),
            "ranking_json": str(ranking_json_path),
            "summary_json": str(summary_json_path),
            "summary_txt": str(summary_txt_path),
            "audit_txt": str(audit_txt_path),
            "audit_json": str(audit_json_path),
        },
    }
