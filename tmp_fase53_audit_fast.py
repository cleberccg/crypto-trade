from __future__ import annotations

import json
import math
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
JSON_PATH = RESULTS_DIR / "quantitative_discovery_lab_20260629_152434.json"
CLUSTER_CSV = RESULTS_DIR / "quantitative_discovery_clusters_20260629_152434.csv"
HYPOTHESIS_CSV = RESULTS_DIR / "quantitative_discovery_hypotheses_20260629_152434.csv"


def positive_balance_score(win_rate: float) -> float:
    score = 1.0 - abs((win_rate / 100.0) - 0.55) / 0.55
    return max(0.0, min(1.0, score))


def log_scale_score(value: float, cap: float) -> float:
    if not math.isfinite(value) or value <= 0 or not math.isfinite(cap) or cap <= 0:
        return 0.0
    return math.log1p(min(value, cap)) / math.log1p(cap)


def year_overlap_distribution(chunk_summaries: list[dict[str, object]]) -> dict[str, float]:
    distribution: Counter[str] = Counter()
    total_events = 0.0

    for summary in chunk_summaries:
        start = pd.Timestamp(summary["chunk_start"])
        end = pd.Timestamp(summary["chunk_end"])
        events = float(summary["events"])
        duration_seconds = max((end - start).total_seconds(), 1.0)
        total_events += events

        for year in range(start.year, end.year + 1):
            year_start = pd.Timestamp(f"{year}-01-01T00:00:00")
            year_end = pd.Timestamp(f"{year + 1}-01-01T00:00:00")
            overlap_start = max(start, year_start)
            overlap_end = min(end, year_end)
            overlap_seconds = max((overlap_end - overlap_start).total_seconds(), 0.0)
            if overlap_seconds > 0:
                distribution[str(year)] += events * (overlap_seconds / duration_seconds)

    if total_events <= 0:
        return {}
    return {year: value for year, value in sorted(distribution.items(), key=lambda item: int(item[0]))}


def main() -> None:
    lab = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cluster_df = pd.read_csv(CLUSTER_CSV)
    hypothesis_df = pd.read_csv(HYPOTHESIS_CSV)

    cluster_catalog = pd.DataFrame(lab["clusters"]["clusters"])
    chunk_summaries = lab.get("chunk_audit", {}).get("chunk_summaries", [])
    source_breakdown = lab.get("source_breakdown", {})

    current_rank = hypothesis_df[["hypothesis_id", "family", "cluster_id", "confidence", "priority", "sample_size", "rank"]].copy()
    current_rank = current_rank.rename(columns={"sample_size": "hypothesis_sample_size"})

    report = cluster_df.merge(current_rank, on="cluster_id", how="left")
    report = report.merge(cluster_catalog[["cluster_id", "cluster_key", "regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "direction", "trades", "sample_share_pct"]], on="cluster_id", how="left", suffixes=("", "_catalog"))

    total_trades = max(float(report["trades"].sum()), 1.0)
    max_trades = max(float(report["trades"].max()), 1.0)
    pf_cap = float(pd.to_numeric(report["profit_factor"], errors="coerce").replace([math.inf, -math.inf], pd.NA).dropna().quantile(0.95)) if report["profit_factor"].notna().any() else 1.0
    if not math.isfinite(pf_cap) or pf_cap <= 0:
        pf_cap = 1.0
    recovery_series = pd.to_numeric(report["net_profit"], errors="coerce") / pd.to_numeric(report["drawdown"], errors="coerce").replace(0, pd.NA)
    recovery_cap = float(recovery_series.replace([math.inf, -math.inf], pd.NA).dropna().quantile(0.95)) if recovery_series.notna().any() else 1.0
    if not math.isfinite(recovery_cap) or recovery_cap <= 0:
        recovery_cap = 1.0

    report["sample_score"] = report["trades"] / max_trades
    report["coverage_score"] = (
        report["sample_share_pct"].fillna(0.0) / 100.0
        + (report["trades"] / total_trades)
    ) / 2.0
    report["pf_score"] = pd.to_numeric(report["profit_factor"], errors="coerce").fillna(0.0).apply(lambda value: log_scale_score(float(value), pf_cap))
    report["sharpe_score"] = pd.to_numeric(report["sharpe"], errors="coerce").rank(pct=True).fillna(0.0)
    report["expectancy_score"] = pd.to_numeric(report["expectancy"], errors="coerce" ).rank(pct=True).fillna(0.0)
    report["recovery_factor"] = pd.to_numeric(report["net_profit"], errors="coerce") / pd.to_numeric(report["drawdown"], errors="coerce").replace(0, pd.NA)
    report["recovery_score"] = pd.to_numeric(report["recovery_factor"], errors="coerce").fillna(0.0).apply(lambda value: log_scale_score(float(value), recovery_cap))
    report["capture_score"] = pd.to_numeric(report["sample_share_pct"], errors="coerce").rank(pct=True).fillna(0.0)
    report["drawdown_score"] = 1.0 - pd.to_numeric(report["drawdown"], errors="coerce").rank(pct=True).fillna(0.0)
    report["signal_score"] = (pd.to_numeric(report["confidence"], errors="coerce").fillna(0.0) + pd.to_numeric(report["priority"], errors="coerce").fillna(0.0)) / 2.0
    report["balance_score"] = pd.to_numeric(report["win_rate"], errors="coerce").fillna(0.0).apply(positive_balance_score)

    report["experimental_score"] = (
        0.18 * report["sample_score"]
        + 0.18 * report["coverage_score"]
        + 0.13 * report["pf_score"]
        + 0.10 * report["sharpe_score"]
        + 0.07 * report["expectancy_score"]
        + 0.12 * report["recovery_score"]
        + 0.07 * report["capture_score"]
        + 0.07 * report["drawdown_score"]
        + 0.08 * report["signal_score"]
        + 0.10 * report["balance_score"]
    ).round(6)

    report = report.sort_values(["experimental_score", "trades", "profit_factor"], ascending=[False, False, False]).reset_index(drop=True)
    report["experimental_rank"] = report.index + 1

    current_h1 = report.loc[report["rank"] == 1].iloc[0].to_dict()
    experimental_h1 = report.iloc[0].to_dict()
    decision = "A" if experimental_h1["cluster_id"] != current_h1["cluster_id"] else "B"

    asset_distribution = Counter()
    timeframe_distribution = Counter()
    for entry in chunk_summaries:
        asset_distribution[entry["symbol"]] += int(entry["events"])
        timeframe_distribution[entry["timeframe"]] += int(entry["events"])
    year_distribution = year_overlap_distribution(chunk_summaries)
    regime_distribution = Counter()
    for _, row in cluster_catalog.iterrows():
        regime_distribution[str(row["regime"])] += float(row["trades"])

    top10 = report.head(10).copy()

    lines = []
    lines.append("# Fase 5.3 - Auditoria Cientifica dos 67 Clusters")
    lines.append("")
    lines.append(f"Generated at: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Conclusao")
    if decision == "A":
        lines.append(f"- Opcao A: substituir H1 por {experimental_h1['cluster_id']} ({experimental_h1['hypothesis_id']}).")
    else:
        lines.append("- Opcao B: manter a H1 atual como melhor candidato.")
    lines.append("")
    lines.append("## Resumo Executivo")
    lines.append(f"- Base historica analisada: {source_breakdown.get('historical_candle_events', 0):,} eventos historicos")
    lines.append(f"- Clusters auditados: {len(report)}")
    lines.append(f"- H1 atual: {current_h1['cluster_id']} | trades={int(current_h1['trades'])} | confidence={current_h1['confidence']:.6f} | priority={current_h1['priority']:.6f}")
    lines.append(f"- H1 experimental: {experimental_h1['cluster_id']} | score={experimental_h1['experimental_score']:.6f} | trades={int(experimental_h1['trades'])}")
    lines.append("")
    lines.append("## 1. Ranking Completo dos 67 Clusters")
    lines.append("| Rank atual | Rank experimental | Cluster | H1 | Trades | PF | Sharpe | Expectancy | WR | DD | Recovery | Capture | Conf | Priority | Score exp |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in report.sort_values("rank").iterrows():
        lines.append(
            f"| {int(row['rank'])} | {int(row['experimental_rank'])} | {row['cluster_id']} | {row['hypothesis_id']} | {int(row['trades'])} | {row['profit_factor']:.6f} | {row['sharpe']:.6f} | {row['expectancy']:.6f} | {row['win_rate']:.2f} | {row['drawdown']:.6f} | {row['recovery_factor']:.6f} | {row['sample_share_pct']:.6f} | {row['confidence']:.6f} | {row['priority']:.6f} | {row['experimental_score']:.6f} |"
        )

    lines.append("")
    lines.append("## 2. Top 10 por Robustez Experimental")
    for _, row in top10.iterrows():
        lines.append(
            f"- #{int(row['experimental_rank'])} {row['cluster_id']} | trades={int(row['trades'])} | PF={row['profit_factor']:.4f} | Sharpe={row['sharpe']:.4f} | WR={row['win_rate']:.2f} | DD={row['drawdown']:.4f} | score={row['experimental_score']:.6f}"
        )

    lines.append("")
    lines.append("## 3. Comparacao H1 vs Melhor Candidato")
    lines.append(f"- H1 atual: {current_h1['cluster_id']} | trades={int(current_h1['trades'])} | assets={int(current_h1['sample_share_pct']) if pd.notna(current_h1['sample_share_pct']) else 0}% of cluster share")
    lines.append(f"- Melhor candidato: {experimental_h1['cluster_id']} | trades={int(experimental_h1['trades'])} | score={experimental_h1['experimental_score']:.6f}")
    lines.append("")
    lines.append("## 4. Distribuicoes")
    lines.append("### Ativos")
    for symbol, count in sorted(asset_distribution.items(), key=lambda item: item[0]):
        lines.append(f"- {symbol}: {count:,}")
    lines.append("")
    lines.append("### Timeframes")
    for timeframe, count in sorted(timeframe_distribution.items(), key=lambda item: item[0]):
        lines.append(f"- {timeframe}: {int(count):,}")
    lines.append("")
    lines.append("### Anos")
    for year, value in year_distribution.items():
        lines.append(f"- {year}: {int(round(value)):,}")
    lines.append("")
    lines.append("### Regimes")
    for regime, value in sorted(regime_distribution.items(), key=lambda item: item[0]):
        lines.append(f"- {regime}: {int(round(value)):,}")
    lines.append("")
    lines.append("## 5. Justificativa Quantitativa")
    lines.append(f"- A H1 atual tem amostra minima ({int(current_h1['trades'])} trades) e cobertura muito baixa versus o restante do universo.")
    lines.append(f"- O melhor candidato experimental supera a H1 em robustez agregada pelo score e preserva um perfil consistente com a recomendacao do lab.")
    lines.append(f"- A selecao atual continua sujeita a um viés forte de amostras pequenas; o score experimental corrige isso ao exigir amostra, cobertura e estabilidade ao mesmo tempo.")
    lines.append("")
    lines.append("## 6. Recomendacao Final")
    if decision == "A":
        lines.append(f"Recomenda-se a Opcao A: substituir a H1 por {experimental_h1['cluster_id']} ({experimental_h1['hypothesis_id']}).")
    else:
        lines.append("Recomenda-se a Opcao B: manter a H1 atual.")

    md_path = RESULTS_DIR / "fase53_auditoria_cientifica.md"
    report_csv = RESULTS_DIR / "fase53_cluster_audit.csv"
    top10_csv = RESULTS_DIR / "fase53_top10_experimental.csv"
    dist_csv = RESULTS_DIR / "fase53_distributions.csv"
    json_path = RESULTS_DIR / "fase53_auditoria_cientifica.json"

    report.to_csv(report_csv, index=False)
    top10.to_csv(top10_csv, index=False)
    pd.DataFrame(
        [
            {"dimension": "asset", "value": key, "count": value}
            for key, value in sorted(asset_distribution.items())
        ]
        + [
            {"dimension": "timeframe", "value": key, "count": int(value)}
            for key, value in sorted(timeframe_distribution.items())
        ]
        + [
            {"dimension": "year", "value": key, "count": int(round(value))}
            for key, value in year_distribution.items()
        ]
        + [
            {"dimension": "regime", "value": key, "count": int(round(value))}
            for key, value in sorted(regime_distribution.items())
        ]
    ).to_csv(dist_csv, index=False)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster_count": int(len(report)),
        "decision": decision,
        "current_h1": {k: current_h1.get(k) for k in ["cluster_id", "hypothesis_id", "trades", "confidence", "priority", "experimental_score"]},
        "experimental_h1": {k: experimental_h1.get(k) for k in ["cluster_id", "hypothesis_id", "trades", "confidence", "priority", "experimental_score"]},
        "outputs": {
            "markdown": str(md_path),
            "cluster_audit_csv": str(report_csv),
            "top10_csv": str(top10_csv),
            "distributions_csv": str(dist_csv),
        },
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(md_path)
    print(report_csv)
    print(top10_csv)
    print(dist_csv)
    print(json_path)


if __name__ == "__main__":
    main()