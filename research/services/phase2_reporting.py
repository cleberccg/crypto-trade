from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine

BASE_DIR = Path(__file__).resolve().parents[2]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from config.settings import settings


@dataclass(frozen=True)
class SelectionCriteria:
    min_profit_factor: float
    min_sharpe: float
    max_drawdown: float
    min_trades: int
    max_overfit_score: float


def _criteria_from_env() -> SelectionCriteria:
    return SelectionCriteria(
        min_profit_factor=float(os.getenv("RESEARCH_MIN_PROFIT_FACTOR", "1.5")),
        min_sharpe=float(os.getenv("RESEARCH_MIN_SHARPE", "1.0")),
        max_drawdown=float(os.getenv("RESEARCH_MAX_DRAWDOWN", str(settings.validation.max_drawdown_pct))),
        min_trades=int(os.getenv("RESEARCH_MIN_TRADES", str(settings.validation.min_trades))),
        max_overfit_score=float(os.getenv("RESEARCH_MAX_OVERFIT_SCORE", "0.45")),
    )


def _compute_scores(df: pd.DataFrame, c: SelectionCriteria) -> pd.DataFrame:
    if df.empty:
        return df

    trades = df["trades"].fillna(0).clip(lower=0)
    pf = df["profit_factor"].fillna(0)
    sharpe = df["sharpe"].fillna(0)
    dd = df["drawdown"].fillna(999)
    expectancy = df["expectancy"].fillna(0)

    trade_score = (trades / max(float(c.min_trades), 1.0)).clip(upper=2.0) / 2.0
    pf_score = (pf / max(c.min_profit_factor, 0.1)).clip(upper=2.0) / 2.0
    sharpe_score = (sharpe / max(c.min_sharpe, 0.1)).clip(upper=2.0) / 2.0
    dd_score = (1.0 - (dd / max(c.max_drawdown, 1.0))).clip(lower=0.0, upper=1.0)
    expectancy_score = expectancy.rank(pct=True).fillna(0.0)

    df["robustness_score"] = (
        0.30 * trade_score
        + 0.25 * pf_score
        + 0.20 * sharpe_score
        + 0.15 * dd_score
        + 0.10 * expectancy_score
    ).round(4)

    weak_trades = trades < c.min_trades
    extreme_pf = pf > (pf.median(skipna=True) * 3 if pf.notna().any() else 9999)
    unstable_sharpe = sharpe < 0
    extreme_dd = dd > c.max_drawdown
    suspicion = (
        0.35 * weak_trades.astype(float)
        + 0.25 * extreme_pf.astype(float)
        + 0.20 * unstable_sharpe.astype(float)
        + 0.20 * extreme_dd.astype(float)
    ).round(4)
    df["overfit_score"] = suspicion
    df["suspicious"] = df["overfit_score"] > c.max_overfit_score

    df["approved_for_paper"] = (
        (pf >= c.min_profit_factor)
        & (sharpe >= c.min_sharpe)
        & (dd <= c.max_drawdown)
        & (trades >= c.min_trades)
        & (~df["suspicious"])
    )

    return df


def _build_summary(df: pd.DataFrame, c: SelectionCriteria) -> dict:
    if df.empty:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "rows": 0,
            "message": "No research rows available in optimization_results_history.",
            "criteria": c.__dict__,
        }

    best_pf = df.sort_values(["profit_factor", "sharpe"], ascending=[False, False]).head(1)
    best_sharpe = df.sort_values(["sharpe", "profit_factor"], ascending=[False, False]).head(1)
    best_dd = df.sort_values(["drawdown", "profit_factor"], ascending=[True, False]).head(1)
    best_robust = df.sort_values(["robustness_score", "profit_factor"], ascending=[False, False]).head(1)

    by_asset = df.groupby("symbol", dropna=False)["robustness_score"].mean().sort_values(ascending=False)
    by_tf = df.groupby("timeframe", dropna=False)["robustness_score"].mean().sort_values(ascending=False)

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rows": int(len(df)),
        "approved_count": int(df["approved_for_paper"].sum()),
        "criteria": c.__dict__,
        "best_asset": by_asset.index[0] if len(by_asset) else None,
        "best_timeframe": by_tf.index[0] if len(by_tf) else None,
        "best_profit_factor": best_pf.to_dict(orient="records")[0],
        "best_sharpe": best_sharpe.to_dict(orient="records")[0],
        "lowest_drawdown": best_dd.to_dict(orient="records")[0],
        "best_robustness": best_robust.to_dict(orient="records")[0],
    }


def _json_safe(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, tuple):
        return tuple(_json_safe(v) for v in obj)
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if hasattr(obj, "item"):
        try:
            return obj.item()
        except Exception:
            return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    return obj


def generate_research_phase2_outputs(base_dir: Path) -> dict[str, str]:
    out_dir = base_dir / "optimization" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    engine = create_engine(settings.database.url, future=True)
    query = """
    SELECT
        execution_id,
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
        return_percent,
        parameters_json,
        approved,
        rejection_reason,
        created_at
    FROM optimization_results_history
    """

    with engine.connect() as conn:
        df = pd.read_sql(query, conn)

    criteria = _criteria_from_env()
    df = _compute_scores(df, criteria)

    rank = df.sort_values(
        ["profit_factor", "sharpe", "drawdown", "expectancy", "robustness_score"],
        ascending=[False, False, True, False, False],
    ).copy()
    top100 = rank.head(100)

    consolidated_path = out_dir / "research_consolidated.csv"
    top100_path = out_dir / "research_top100.csv"
    csv_path = out_dir / "research_dataset.csv"
    parquet_path = out_dir / "research_dataset.parquet"
    db_path = out_dir / "research_dataset.db"

    rank.to_csv(consolidated_path, index=False)
    top100.to_csv(top100_path, index=False)
    df.to_csv(csv_path, index=False)

    parquet_status = "ok"
    try:
        df.to_parquet(parquet_path, index=False)
    except Exception as exc:
        parquet_status = f"failed: {exc}"

    ds_engine = create_engine(f"sqlite:///{db_path}", future=True)
    with ds_engine.begin() as conn:
        df.to_sql("research_dataset", conn, if_exists="replace", index=False)
        top100.to_sql("research_top100", conn, if_exists="replace", index=False)

    summary = _build_summary(df, criteria)
    summary["parquet_status"] = parquet_status

    summary_json = out_dir / "research_summary.json"
    summary_txt = out_dir / "research_summary.txt"
    summary_html = out_dir / "research_summary.html"
    summary_pdf = out_dir / "research_summary.pdf"

    summary_json.write_text(json.dumps(_json_safe(summary), ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        "research_summary",
        "====================",
        f"generated_at={summary.get('generated_at')}",
        f"rows={summary.get('rows')}",
        f"approved_count={summary.get('approved_count')}",
        f"best_asset={summary.get('best_asset')}",
        f"best_timeframe={summary.get('best_timeframe')}",
        f"parquet_status={parquet_status}",
        "outputs=research_dataset.db,research_dataset.csv,research_dataset.parquet,research_consolidated.csv,research_top100.csv",
    ]
    summary_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")

    html = [
        "<html><head><meta charset='utf-8'><title>Research Summary</title></head><body>",
        "<h1>Research Phase 2 Summary</h1>",
        f"<p>Generated at: {summary.get('generated_at')}</p>",
        f"<p>Rows: {summary.get('rows')}</p>",
        f"<p>Approved for paper: {summary.get('approved_count')}</p>",
        f"<p>Best asset: {summary.get('best_asset')}</p>",
        f"<p>Best timeframe: {summary.get('best_timeframe')}</p>",
        f"<p>Parquet status: {parquet_status}</p>",
        "</body></html>",
    ]
    summary_html.write_text("\n".join(html), encoding="utf-8")

    # Minimal valid PDF bytes placeholder without external dependencies.
    summary_pdf.write_bytes(b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Count 0>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF")

    return {
        "dataset_db": str(db_path),
        "dataset_csv": str(csv_path),
        "dataset_parquet": str(parquet_path),
        "consolidated": str(consolidated_path),
        "top100": str(top100_path),
        "summary_json": str(summary_json),
        "summary_txt": str(summary_txt),
        "summary_html": str(summary_html),
        "summary_pdf": str(summary_pdf),
    }


if __name__ == "__main__":
    outputs = generate_research_phase2_outputs(Path(__file__).resolve().parents[2])
    print(json.dumps(outputs, ensure_ascii=False, indent=2))
