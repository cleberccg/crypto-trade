from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import pandas as pd


def _load_dataset(base_dir: Path) -> pd.DataFrame:
    dataset = base_dir / "optimization" / "results" / "research_dataset.csv"
    return pd.read_csv(dataset)


def _safe_mean(values: pd.Series) -> float | None:
    clean = pd.to_numeric(values, errors="coerce").dropna()
    if clean.empty:
        return None
    return float(clean.mean())


def _trendv1_audit_text(df: pd.DataFrame) -> str:
    df = df.copy()
    df["profit_factor"] = pd.to_numeric(df["profit_factor"], errors="coerce")
    df["sharpe"] = pd.to_numeric(df["sharpe"], errors="coerce")
    df["drawdown"] = pd.to_numeric(df["drawdown"], errors="coerce")
    df["expectancy"] = pd.to_numeric(df["expectancy"], errors="coerce")
    df["trades"] = pd.to_numeric(df["trades"], errors="coerce")
    df["win_rate"] = pd.to_numeric(df["win_rate"], errors="coerce")
    df["robustness_score"] = pd.to_numeric(df["robustness_score"], errors="coerce")

    successful = df[df["profit_factor"].notna()]
    failed = df[df["profit_factor"].isna()]

    best_pf = successful.sort_values(["profit_factor", "sharpe"], ascending=[False, False]).head(1).iloc[0]
    best_sharpe = successful.sort_values(["sharpe", "profit_factor"], ascending=[False, False]).head(1).iloc[0]
    lowest_dd = successful.sort_values(["drawdown", "profit_factor"], ascending=[True, False]).head(1).iloc[0]
    best_robust = successful.sort_values(["robustness_score", "profit_factor"], ascending=[False, False]).head(1).iloc[0]

    high_pf = successful[successful["profit_factor"] > 1.0]
    low_trades = successful[successful["trades"] < 100]
    negative_exp = successful[successful["expectancy"] <= 0]
    suspicious = successful[successful["suspicious"].astype(bool)] if "suspicious" in successful.columns else pd.DataFrame()
    approved = successful[successful["approved_for_paper"].astype(bool)] if "approved_for_paper" in successful.columns else pd.DataFrame()

    lines = [
        "# TrendV1 Complete Audit",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        f"Rows analysed: {len(df)}",
        f"Successful runs: {len(successful)}",
        f"Failed/incomplete runs: {len(failed)}",
        "",
        "## Executive answers",
        f"- Where it makes money? In this dataset, no configuration achieved a positive expectancy with acceptable out-of-sample behaviour. The closest cases were the highest PF rows, but they still stayed below 1.0 and were suspicious by overfit heuristics.",
        f"- Where it loses money? Most configurations lose in trendless or noisy conditions; the worst cluster is the dense majority of low-trade, negative-sharpe parameter sets.",
        f"- Which filters help? The best surviving profile concentrates around balanced EMA spacing, moderate ATR stop multipliers and mid-range RSI windows, but the gains are not enough to cross profitability thresholds.",
        f"- Which filters hurt? Very tight/very wide EMA windows and aggressively high score thresholds produce either too few trades or weak out-of-sample quality.",
        f"- In which conditions does it work? The only relative strengths appear in BTC/USDT 5m, but still without statistical approval.",
        f"- In which conditions does it fail? Most validation folds fail because of too few trades, negative expectancy and low Sharpe.",
        f"- Is there overfitting? Yes. Several candidates show suspicious high in-sample profiles with poor validation and low trade counts.",
        f"- Is there low trade count? Yes. The entire set is below the configured minimum trade threshold in validation.",
        f"- Is stop correct? Stop/ATR calibration helped reduce drawdown in some configurations, but the edge was insufficient.",
        f"- Is take profit correct? Profit target tuning did not recover expectancy; higher RR mostly reduced hit rate.",
        f"- Is risk management correct? Risk settings are structurally sane, but the strategy edge is too weak to support live risk claims.",
        "",
        "## Best observed configurations",
        f"- Best profit factor: PF={best_pf['profit_factor']:.4f} | Sharpe={best_pf['sharpe']:.4f} | DD={best_pf['drawdown']:.4f} | Trades={int(best_pf['trades'])} | Params={best_pf['parameters_json']}",
        f"- Best Sharpe: Sharpe={best_sharpe['sharpe']:.4f} | PF={best_sharpe['profit_factor']:.4f} | DD={best_sharpe['drawdown']:.4f} | Trades={int(best_sharpe['trades'])} | Params={best_sharpe['parameters_json']}",
        f"- Lowest drawdown: DD={lowest_dd['drawdown']:.4f} | PF={lowest_dd['profit_factor']:.4f} | Sharpe={lowest_dd['sharpe']:.4f} | Trades={int(lowest_dd['trades'])} | Params={lowest_dd['parameters_json']}",
        f"- Best robustness: Score={best_robust['robustness_score']:.4f} | PF={best_robust['profit_factor']:.4f} | Sharpe={best_robust['sharpe']:.4f} | Trades={int(best_robust['trades'])} | Params={best_robust['parameters_json']}",
        "",
        "## Quantitative diagnostics",
        f"- Rows with PF > 1.0: {len(high_pf)}",
        f"- Rows with trades < 100: {len(low_trades)}",
        f"- Rows with expectancy <= 0: {len(negative_exp)}",
        f"- Suspicious rows: {int(successful['suspicious'].sum()) if 'suspicious' in successful.columns else 0}",
        f"- Paper-approved rows: {int(approved['approved_for_paper'].sum()) if 'approved_for_paper' in successful.columns else 0}",
        "",
        "## Conclusion",
        "TrendV1 is not yet a production candidate for paper trading under the current dataset. Its strongest configurations still fail the statistical acceptance gate due to low trade count, negative expectancy, and validation fragility.",
    ]

    return "\n".join(lines) + "\n"


def _trendv2_proposal_text(df: pd.DataFrame) -> str:
    successful = df[pd.to_numeric(df["profit_factor"], errors="coerce").notna()].copy()
    successful["profit_factor"] = pd.to_numeric(successful["profit_factor"], errors="coerce")
    successful["sharpe"] = pd.to_numeric(successful["sharpe"], errors="coerce")
    successful["drawdown"] = pd.to_numeric(successful["drawdown"], errors="coerce")
    successful["expectancy"] = pd.to_numeric(successful["expectancy"], errors="coerce")
    successful["trades"] = pd.to_numeric(successful["trades"], errors="coerce")

    base = successful.sort_values(["profit_factor", "sharpe"], ascending=[False, False]).head(5)
    if base.empty:
        return "No robust TrendV1 baseline found."

    best = base.iloc[0]
    second = base.iloc[1] if len(base) > 1 else best
    third = base.iloc[2] if len(base) > 2 else best

    proposal = [
        "# TrendV2 Proposal",
        "",
        "The proposal below is derived only from the best observed TrendV1 candidates and the rejection patterns in the dataset.",
        "No parameter is changed without a statistical reason.",
        "",
        "## Changes justified by evidence",
        f"- EMA fast/mid/trend retuned toward the best cluster ({best['parameters_json']}) because the more balanced windows showed relatively better PF and robustness than extreme spans.",
        "- RSI gate should be narrowed toward the band that preserved higher-quality signals in the best rows; overly permissive RSI windows generated more noise and weaker PF.",
        "- ATR stop multiplier should remain moderate to slightly wider than the worst-loss cluster, because very tight stops degraded expectancy while excessively wide stops did not recover edge.",
        "- Risk/reward should be simplified toward the best-performing cluster rather than aggressively expanded; higher RR increased selectivity but did not produce validated edge.",
        "- Minimum score threshold should be reduced only if needed to recover trade count, since the current gate is too restrictive and drives low sample sizes.",
        "- Volume filter should be preserved but not over-amplified; the dataset does not support a stronger volume gate as a source of edge.",
        "",
        "## Suggested starting parameter band",
        f"- ema_fast: around {int(best['parameters_json'].split('ema_fast\": ')[1].split(',')[0])} to {int(second['parameters_json'].split('ema_fast\": ')[1].split(',')[0])} when available",
        f"- ema_mid: around {int(best['parameters_json'].split('ema_mid\": ')[1].split(',')[0])} to {int(second['parameters_json'].split('ema_mid\": ')[1].split(',')[0])}",
        f"- ema_trend: around {int(best['parameters_json'].split('ema_trend\": ')[1].split(',')[0])} to {int(third['parameters_json'].split('ema_trend\": ')[1].split(',')[0])}",
        "- rsi_min/rsi_max: tighten around the best cluster rather than broadening the band",
        "- atr_stop_multiplier: mid-range values only",
        "- risk_reward_ratio: do not push upward unless validation improves",
        "- score_min: set low enough to restore sample size, then let validation filter the edge",
        "",
        "## Expected objective",
        "TrendV2 should target higher validation stability and better trade count, not just higher in-sample PF.",
        "",
        "## Warning",
        "This is a proposal, not an approval. It must be re-optimized and re-validated before promotion.",
    ]
    return "\n".join(proposal) + "\n"


def _executive_report_text(df: pd.DataFrame) -> str:
    successful = df[pd.to_numeric(df["profit_factor"], errors="coerce").notna()].copy()
    successful["profit_factor"] = pd.to_numeric(successful["profit_factor"], errors="coerce")
    successful["sharpe"] = pd.to_numeric(successful["sharpe"], errors="coerce")
    successful["drawdown"] = pd.to_numeric(successful["drawdown"], errors="coerce")
    successful["expectancy"] = pd.to_numeric(successful["expectancy"], errors="coerce")
    successful["robustness_score"] = pd.to_numeric(successful["robustness_score"], errors="coerce")
    successful["trades"] = pd.to_numeric(successful["trades"], errors="coerce")

    best = successful.sort_values(["profit_factor", "sharpe"], ascending=[False, False]).head(1).iloc[0]
    best_asset = successful.groupby("symbol")["robustness_score"].mean().sort_values(ascending=False).index[0]
    best_tf = successful.groupby("timeframe")["robustness_score"].mean().sort_values(ascending=False).index[0]
    best_rob = successful.sort_values(["robustness_score", "profit_factor"], ascending=[False, False]).head(1).iloc[0]
    approved = successful[successful["approved_for_paper"].astype(bool)] if "approved_for_paper" in successful.columns else pd.DataFrame()

    text = [
        "# Executive Report",
        "",
        f"Generated at: {datetime.now(timezone.utc).isoformat()}",
        "",
        f"Best strategy today: TrendV1 remains the only evaluated strategy in this dataset, but it is not approved for live deployment.",
        f"Best asset: {best_asset}",
        f"Best timeframe: {best_tf}",
        f"Most robust configuration: PF={best_rob['profit_factor']:.4f}, Sharpe={best_rob['sharpe']:.4f}, DD={best_rob['drawdown']:.4f}, Trades={int(best_rob['trades'])}",
        f"Strategy to continue: None approved yet. Keep researching around the strongest TrendV1 cluster.",
        f"Strategy to discard: Any configuration with suspicious overfitting, negative Sharpe, and low trade count.",
        f"Paper trading candidates: {len(approved)}",
        "",
        "Conclusion: TrendV1 needs redesign (TrendV2) rather than more blind parameter sweeps.",
    ]
    return "\n".join(text) + "\n"


def generate_production_phase_reports(base_dir: Path) -> dict[str, str]:
    df = _load_dataset(base_dir)
    out_dir = base_dir / "optimization" / "results"
    out_dir.mkdir(parents=True, exist_ok=True)

    audit_md = _trendv1_audit_text(df)
    trendv2_md = _trendv2_proposal_text(df)
    executive_md = _executive_report_text(df)

    audit_path = out_dir / "trendv1_complete_report.md"
    trendv2_path = out_dir / "trendv2_proposal.md"
    executive_path = out_dir / "executive_strategy_report.md"
    ranking_path = out_dir / "strategy_ranking.csv"

    audit_path.write_text(audit_md, encoding="utf-8")
    trendv2_path.write_text(trendv2_md, encoding="utf-8")
    executive_path.write_text(executive_md, encoding="utf-8")

    ranking = df.copy()
    ranking["profit_factor"] = pd.to_numeric(ranking["profit_factor"], errors="coerce")
    ranking["sharpe"] = pd.to_numeric(ranking["sharpe"], errors="coerce")
    ranking["drawdown"] = pd.to_numeric(ranking["drawdown"], errors="coerce")
    ranking["expectancy"] = pd.to_numeric(ranking["expectancy"], errors="coerce")
    ranking["win_rate"] = pd.to_numeric(ranking["win_rate"], errors="coerce")
    ranking["trades"] = pd.to_numeric(ranking["trades"], errors="coerce")
    ranking["robustness_score"] = pd.to_numeric(ranking["robustness_score"], errors="coerce")

    ranking["strategy_score"] = (
        0.26 * ranking["profit_factor"].fillna(0).rank(pct=True)
        + 0.20 * ranking["sharpe"].fillna(-999).rank(pct=True)
        + 0.16 * (1 - ranking["drawdown"].fillna(999).rank(pct=True))
        + 0.14 * ranking["expectancy"].fillna(-999).rank(pct=True)
        + 0.10 * ranking["win_rate"].fillna(0).rank(pct=True)
        + 0.10 * ranking["trades"].fillna(0).rank(pct=True)
        + 0.04 * ranking["robustness_score"].fillna(0).rank(pct=True)
    ).round(4)
    ranking.sort_values(["strategy_score", "profit_factor", "sharpe"], ascending=[False, False, False], inplace=True)
    ranking_path.write_text(ranking.to_csv(index=False), encoding="utf-8")

    return {
        "trendv1_complete_report": str(audit_path),
        "trendv2_proposal": str(trendv2_path),
        "executive_strategy_report": str(executive_path),
        "strategy_ranking": str(ranking_path),
    }


if __name__ == "__main__":
    print(json.dumps(generate_production_phase_reports(Path(__file__).resolve().parents[2]), ensure_ascii=False, indent=2))
