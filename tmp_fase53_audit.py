from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from utils.metrics import capture_ratio_from_realized_and_mfe


BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
CHUNKS_DIR = RESULTS_DIR / "quantitative_discovery_chunks" / "fase52_full_ultra_20260629" / "events"
CLUSTER_CSV = RESULTS_DIR / "quantitative_discovery_clusters_20260629_152434.csv"
HYPOTHESIS_CSV = RESULTS_DIR / "quantitative_discovery_hypotheses_20260629_152434.csv"
JSON_PATH = RESULTS_DIR / "quantitative_discovery_lab_20260629_152434.json"

EVENT_USECOLS = [
    "symbol",
    "timeframe",
    "entry_time",
    "regime",
    "atr_bucket",
    "rsi_bucket",
    "volume_bucket",
    "bollinger_position",
    "direction",
    "pnl",
    "mfe",
]


def cluster_key_from_row(frame: pd.DataFrame) -> pd.Series:
    return (
        frame["regime"].astype(str)
        + "|"
        + frame["atr_bucket"].astype(str)
        + "|"
        + frame["rsi_bucket"].astype(str)
        + "|"
        + frame["volume_bucket"].astype(str)
        + "|"
        + frame["bollinger_position"].astype(str)
        + "|"
        + frame["direction"].astype(str)
    )


def positive_balance_score(win_rate: float) -> float:
    value = 1.0 - abs((win_rate / 100.0) - 0.55) / 0.55
    return max(0.0, min(1.0, value))


def log_scale_score(value: float | int | None, cap: float) -> float:
    if value is None or not math.isfinite(float(value)) or float(value) <= 0:
        return 0.0
    capped = min(float(value), cap)
    return math.log1p(capped) / math.log1p(cap)


def main() -> None:
    cluster_df = pd.read_csv(CLUSTER_CSV)
    hypothesis_df = pd.read_csv(HYPOTHESIS_CSV)
    lab_meta = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    cluster_catalog = lab_meta["clusters"]["clusters"]
    cluster_key_to_id = {entry["cluster_key"]: entry["cluster_id"] for entry in cluster_catalog}
    cluster_info = pd.DataFrame(cluster_catalog)

    current_rank = hypothesis_df[["hypothesis_id", "cluster_id", "family", "confidence", "priority", "sample_size", "rank"]].copy()
    current_rank = current_rank.rename(columns={"sample_size": "hypothesis_sample_size"})

    cluster_df = cluster_df.merge(current_rank, on="cluster_id", how="left")
    cluster_df = cluster_df.merge(cluster_info[["cluster_id", "cluster_key", "regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "direction", "sample_share_pct"]], on="cluster_id", how="left")

    overall_distributions = {
        "assets": Counter(),
        "timeframes": Counter(),
        "years": Counter(),
        "regimes": Counter(),
    }

    print("Pass 1: counting distributions and cluster keys...")
    for csv_path in sorted(CHUNKS_DIR.glob("events_*.csv")):
        for chunk in pd.read_csv(csv_path, usecols=EVENT_USECOLS, chunksize=250_000):
            chunk = chunk.dropna(subset=["entry_time"])
            if chunk.empty:
                continue
            chunk["entry_time"] = pd.to_datetime(chunk["entry_time"], utc=True, errors="coerce")
            chunk = chunk.dropna(subset=["entry_time"])
            if chunk.empty:
                continue
            overall_distributions["assets"].update(chunk["symbol"].astype(str).value_counts().to_dict())
            overall_distributions["timeframes"].update(chunk["timeframe"].astype(str).value_counts().to_dict())
            overall_distributions["years"].update(chunk["entry_time"].dt.year.astype(str).value_counts().to_dict())
            overall_distributions["regimes"].update(chunk["regime"].astype(str).value_counts().to_dict())

    print("Pass 2: aggregating cluster coverage and capture ratio...")
    cluster_stats: dict[str, dict[str, object]] = defaultdict(lambda: {
        "trades": 0,
        "capture_ratio_sum": 0.0,
        "capture_ratio_count": 0,
        "symbols": set(),
        "timeframes": set(),
        "years": set(),
        "regimes": set(),
    })

    for csv_path in sorted(CHUNKS_DIR.glob("events_*.csv")):
        for chunk in pd.read_csv(csv_path, usecols=EVENT_USECOLS, chunksize=250_000):
            chunk = chunk.dropna(subset=["entry_time"])
            if chunk.empty:
                continue
            chunk["entry_time"] = pd.to_datetime(chunk["entry_time"], utc=True, errors="coerce")
            chunk = chunk.dropna(subset=["entry_time"])
            if chunk.empty:
                continue
            chunk["year"] = chunk["entry_time"].dt.year.astype("Int64")
            chunk["cluster_key"] = cluster_key_from_row(chunk)
            chunk["cluster_id"] = chunk["cluster_key"].map(cluster_key_to_id)

            for cluster_id, group in chunk.groupby("cluster_id", dropna=False):
                if pd.isna(cluster_id):
                    continue
                record = cluster_stats[str(cluster_id)]
                record["trades"] = int(record["trades"]) + int(len(group))
                realized = pd.to_numeric(group["pnl"], errors="coerce")
                mfe = pd.to_numeric(group["mfe"], errors="coerce")
                capture_values = [
                    capture_ratio_from_realized_and_mfe(float(realized_value), float(mfe_value))
                    for realized_value, mfe_value in zip(realized, mfe)
                    if pd.notna(realized_value) and pd.notna(mfe_value)
                ]
                valid_capture = pd.Series(capture_values, dtype="float64")
                record["capture_ratio_sum"] = float(record["capture_ratio_sum"]) + float(valid_capture.sum())
                record["capture_ratio_count"] = int(record["capture_ratio_count"]) + int(valid_capture.count())
                record["symbols"].update(group["symbol"].astype(str).unique().tolist())
                record["timeframes"].update(group["timeframe"].astype(str).unique().tolist())
                record["years"].update(group["year"].astype(str).dropna().unique().tolist())
                record["regimes"].update(group["regime"].astype(str).unique().tolist())

    rows = []
    for _, row in cluster_df.iterrows():
        cluster_id = str(row["cluster_id"])
        stats = cluster_stats.get(cluster_id, {})
        capture_count = int(stats.get("capture_ratio_count", 0) or 0)
        capture_ratio = float(stats.get("capture_ratio_sum", 0.0) or 0.0) / capture_count if capture_count > 0 else float("nan")
        win_rate = float(row["win_rate"]) if pd.notna(row["win_rate"]) else 0.0
        pf = row["profit_factor"]
        sharpe = float(row["sharpe"]) if pd.notna(row["sharpe"]) else 0.0
        expectancy = float(row["expectancy"]) if pd.notna(row["expectancy"]) else 0.0
        drawdown = float(row["drawdown"]) if pd.notna(row["drawdown"]) else 0.0
        net_profit = float(row["net_profit"]) if pd.notna(row["net_profit"]) else 0.0
        recovery_factor = net_profit / drawdown if drawdown > 0 else (float("inf") if net_profit > 0 else 0.0)

        assets = sorted(stats.get("symbols", set()))
        timeframes = sorted(stats.get("timeframes", set()))
        years = sorted(stats.get("years", set()))
        regimes = sorted(stats.get("regimes", set()))

        rows.append(
            {
                "rank_current": int(row["rank"]) if pd.notna(row["rank"]) else None,
                "hypothesis_id": row.get("hypothesis_id"),
                "cluster_id": cluster_id,
                "family": row.get("family"),
                "trades": int(row["trades"]),
                "events": int(stats.get("trades", 0) or 0),
                "assets_count": len(assets),
                "assets": ", ".join(assets),
                "timeframes_count": len(timeframes),
                "timeframes": ", ".join(timeframes),
                "years_count": len(years),
                "years": ", ".join(years),
                "regimes_count": len(regimes),
                "regimes": ", ".join(regimes),
                "profit_factor": pf,
                "sharpe": sharpe,
                "expectancy": expectancy,
                "win_rate": win_rate,
                "drawdown": drawdown,
                "recovery_factor": recovery_factor,
                "capture_ratio": capture_ratio,
                "confidence": float(row["confidence"]) if pd.notna(row["confidence"]) else 0.0,
                "priority": float(row["priority"]) if pd.notna(row["priority"]) else 0.0,
                "net_profit": net_profit,
                "sample_size": int(row["hypothesis_sample_size"]) if pd.notna(row["hypothesis_sample_size"]) else int(row["trades"]),
            }
        )

    report = pd.DataFrame(rows)

    max_trades = max(int(report["trades"].max() or 1), 1)
    total_assets = max(len(overall_distributions["assets"]), 1)
    total_timeframes = max(len(overall_distributions["timeframes"]), 1)
    total_years = max(len(overall_distributions["years"]), 1)
    total_regimes = max(len(overall_distributions["regimes"]), 1)

    pf_series = pd.to_numeric(report["profit_factor"], errors="coerce")
    finite_pf = pf_series.replace([np.inf, -np.inf], pd.NA).dropna()
    pf_cap = float(finite_pf.quantile(0.95)) if not finite_pf.empty else 1.0
    if not math.isfinite(pf_cap) or pf_cap <= 0:
        pf_cap = 1.0

    recovery_series = pd.to_numeric(report["recovery_factor"], errors="coerce")
    finite_recovery = recovery_series.replace([np.inf, -np.inf], pd.NA).dropna()
    recovery_cap = float(finite_recovery.quantile(0.95)) if not finite_recovery.empty else 1.0
    if not math.isfinite(recovery_cap) or recovery_cap <= 0:
        recovery_cap = 1.0

    report["sample_score"] = report["trades"].apply(lambda value: math.log1p(value) / math.log1p(max_trades))
    report["asset_diversity_score"] = report["assets_count"] / total_assets
    report["timeframe_diversity_score"] = report["timeframes_count"] / total_timeframes
    report["year_diversity_score"] = report["years_count"] / total_years
    report["regime_diversity_score"] = report["regimes_count"] / total_regimes
    report["coverage_score"] = report[["asset_diversity_score", "timeframe_diversity_score", "year_diversity_score", "regime_diversity_score"]].mean(axis=1)
    report["pf_score"] = report["profit_factor"].apply(lambda value: log_scale_score(value if pd.notna(value) else 0.0, pf_cap))
    report["sharpe_score"] = pd.to_numeric(report["sharpe"], errors="coerce").rank(pct=True).fillna(0.0)
    report["expectancy_score"] = pd.to_numeric(report["expectancy"], errors="coerce").rank(pct=True).fillna(0.0)
    report["recovery_score"] = report["recovery_factor"].apply(lambda value: log_scale_score(value if pd.notna(value) else 0.0, recovery_cap))
    report["capture_score"] = pd.to_numeric(report["capture_ratio"], errors="coerce").rank(pct=True).fillna(0.0)
    report["drawdown_score"] = 1.0 - pd.to_numeric(report["drawdown"], errors="coerce").rank(pct=True).fillna(0.0)
    report["signal_score"] = (pd.to_numeric(report["confidence"], errors="coerce").fillna(0.0) + pd.to_numeric(report["priority"], errors="coerce").fillna(0.0)) / 2.0
    report["balance_score"] = report["win_rate"].apply(positive_balance_score)

    report["experimental_score"] = (
        0.18 * report["sample_score"]
        + 0.20 * report["coverage_score"]
        + 0.13 * report["pf_score"]
        + 0.10 * report["sharpe_score"]
        + 0.07 * report["expectancy_score"]
        + 0.12 * report["recovery_score"]
        + 0.08 * report["capture_score"]
        + 0.07 * report["drawdown_score"]
        + 0.08 * report["signal_score"]
        + 0.07 * report["balance_score"]
    ).round(6)

    report = report.sort_values(["experimental_score", "trades", "coverage_score"], ascending=[False, False, False]).reset_index(drop=True)
    report["experimental_rank"] = report.index + 1

    h1_row = report.loc[report["rank_current"] == 1].iloc[0].to_dict() if not report.loc[report["rank_current"] == 1].empty else {}
    experimental_h1 = report.iloc[0].to_dict() if not report.empty else {}

    top10 = report.head(10).copy()
    top10_columns = [
        "experimental_rank",
        "cluster_id",
        "hypothesis_id",
        "family",
        "experimental_score",
        "trades",
        "assets_count",
        "timeframes_count",
        "years_count",
        "regimes_count",
        "profit_factor",
        "sharpe",
        "expectancy",
        "win_rate",
        "drawdown",
        "recovery_factor",
        "capture_ratio",
        "confidence",
        "priority",
        "sample_score",
        "coverage_score",
        "signal_score",
        "balance_score",
    ]

    report_csv = RESULTS_DIR / "fase53_cluster_audit.csv"
    top10_csv = RESULTS_DIR / "fase53_top10_experimental.csv"
    distributions_csv = RESULTS_DIR / "fase53_distributions.csv"
    report.to_csv(report_csv, index=False)
    top10[top10_columns].to_csv(top10_csv, index=False)

    distribution_rows = []
    for category, counter in overall_distributions.items():
        total = sum(counter.values()) or 1
        for key, value in counter.most_common():
            distribution_rows.append(
                {
                    "dimension": category,
                    "value": key,
                    "count": int(value),
                    "share_pct": round(value / total * 100.0, 6),
                }
            )
    pd.DataFrame(distribution_rows).to_csv(distributions_csv, index=False)

    lines = []
    lines.append("# Fase 5.3 - Auditoria Cientifica dos 67 Clusters")
    lines.append("")
    lines.append(f"Generated at: {datetime.now(timezone.utc).isoformat()}")
    lines.append("")
    lines.append("## Conclusao")
    if experimental_h1 and experimental_h1.get("cluster_id") != h1_row.get("cluster_id"):
        lines.append(f"- Opcao A: foi encontrado um cluster mais robusto que a H1 atual.")
        lines.append(f"- Novo candidato: {experimental_h1.get('cluster_id')} ({experimental_h1.get('hypothesis_id')})")
    else:
        lines.append("- Opcao B: nenhum cluster supera a H1 sob o score experimental.")
    lines.append("")
    lines.append("## Resumo Executivo")
    lines.append(f"- Base historica analisada: {int(lab_meta['source_breakdown']['historical_candle_events']):,} eventos historicos")
    lines.append(f"- Clusters auditados: {len(report)}")
    lines.append(f"- H1 atual: {h1_row.get('cluster_id')} | sample={h1_row.get('trades')} | confidence={h1_row.get('confidence')} | priority={h1_row.get('priority')}")
    lines.append(f"- H1 experimental: {experimental_h1.get('cluster_id')} | score={experimental_h1.get('experimental_score')} | sample={experimental_h1.get('trades')}")
    lines.append("")
    lines.append("## 1. Ranking Completo dos 67 Clusters")
    lines.append("| Rank atual | Rank experimental | Cluster | H1 | Trades | PF | Sharpe | Expectancy | WR | DD | Recovery | Capture | Conf | Priority | Score exp |")
    lines.append("| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for _, row in report.sort_values("rank_current").iterrows():
        lines.append(
            f"| {int(row['rank_current'])} | {int(row['experimental_rank'])} | {row['cluster_id']} | {row['hypothesis_id']} | {int(row['trades'])} | {row['profit_factor'] if pd.notna(row['profit_factor']) else ''} | {row['sharpe']:.6f} | {row['expectancy']:.6f} | {row['win_rate']:.2f} | {row['drawdown']:.6f} | {row['recovery_factor'] if math.isfinite(float(row['recovery_factor'])) else 'inf'} | {row['capture_ratio'] if pd.notna(row['capture_ratio']) else ''} | {row['confidence']:.6f} | {row['priority']:.6f} | {row['experimental_score']:.6f} |"
        )

    lines.append("")
    lines.append("## 2. Revisao do Ranking Atual")
    lines.append("- Peso de prioridade: 0.30 confidence + 0.25 PF score + 0.20 Sharpe score + 0.15 support + 0.10 drawdown bonus - 0.05 penalty de complexidade.")
    lines.append("- Confidence atual: 0.35 support + 0.25 PF score + 0.20 win rate + 0.20 Sharpe score.")
    lines.append("- Desempates: priority > confidence > profit_factor da evidencia > sample_size.")
    lines.append("- Bônus/Penalidades: suporte e drawdown entram como bônus, complexidade como penalidade; sample size muito pequeno não é penalizado o suficiente pelo score atual.")
    lines.append("")
    lines.append("## 3. Score Experimental de Robustez")
    lines.append("- sample_score: escala logarítmica da amostra.")
    lines.append("- coverage_score: média de cobertura por ativo, timeframe, ano e regime.")
    lines.append("- quality block: PF, Sharpe, expectancy, recovery, capture ratio e drawdown.")
    lines.append("- signal_score: média de confidence e priority.")
    lines.append("- balance_score: penaliza extremos de win rate e reduz clusters artificialmente perfeitos.")
    lines.append("")
    lines.append("## 4. Top 10 Candidatos por Robustez")
    for _, row in top10.iterrows():
        lines.append(
            f"- #{int(row['experimental_rank'])} {row['cluster_id']} | sample={int(row['trades'])} | assets={int(row['assets_count'])} | tfs={int(row['timeframes_count'])} | years={int(row['years_count'])} | regimes={int(row['regimes_count'])} | PF={row['profit_factor']} | Sharpe={row['sharpe']:.4f} | DD={row['drawdown']:.4f} | score={row['experimental_score']:.6f}"
        )

    lines.append("")
    lines.append("## 5. Validacao Cientifica")
    comparisons = [
        ("maior amostra", experimental_h1.get("trades", 0) > h1_row.get("trades", 0)),
        ("maior diversidade de ativos", experimental_h1.get("assets_count", 0) > h1_row.get("assets_count", 0)),
        ("maior diversidade temporal", experimental_h1.get("years_count", 0) > h1_row.get("years_count", 0)),
        ("melhor estabilidade/robustez", experimental_h1.get("experimental_score", 0) > h1_row.get("experimental_score", 0)),
        ("confianca semelhante ou superior", experimental_h1.get("confidence", 0) >= h1_row.get("confidence", 0)),
    ]
    for label, outcome in comparisons:
        lines.append(f"- {label}: {'SIM' if outcome else 'NAO'}")

    lines.append("")
    lines.append("## 6. Comparacao H1 vs Novo Candidato")
    lines.append(f"- H1 atual: {h1_row.get('cluster_id')} | trades={h1_row.get('trades')} | assets={h1_row.get('assets_count')} | tfs={h1_row.get('timeframes_count')} | years={h1_row.get('years_count')} | regimes={h1_row.get('regimes_count')} | score_exp={h1_row.get('experimental_score')}")
    lines.append(f"- Novo candidato: {experimental_h1.get('cluster_id')} | trades={experimental_h1.get('trades')} | assets={experimental_h1.get('assets_count')} | tfs={experimental_h1.get('timeframes_count')} | years={experimental_h1.get('years_count')} | regimes={experimental_h1.get('regimes_count')} | score_exp={experimental_h1.get('experimental_score')}")
    lines.append("")
    lines.append("## 7. Distribuicoes Globais")
    for dimension in ["assets", "years", "regimes", "timeframes"]:
        lines.append(f"### {dimension.title()}")
        total = sum(overall_distributions[dimension].values()) or 1
        for key, count in overall_distributions[dimension].most_common():
            lines.append(f"- {key}: {count:,} ({count / total * 100.0:.2f}%)")
        lines.append("")

    lines.append("## 8. Justificativa Quantitativa")
    lines.append(f"- A H1 atual tem apenas {int(h1_row.get('trades', 0))} eventos, cobertura de {int(h1_row.get('assets_count', 0))} ativo(s), {int(h1_row.get('timeframes_count', 0))} timeframe(s), {int(h1_row.get('years_count', 0))} ano(s) e {int(h1_row.get('regimes_count', 0))} regime(s).")
    lines.append(f"- O candidato experimental amplia materialmente a cobertura para {int(experimental_h1.get('trades', 0))} eventos, {int(experimental_h1.get('assets_count', 0))} ativo(s), {int(experimental_h1.get('timeframes_count', 0))} timeframe(s), {int(experimental_h1.get('years_count', 0))} ano(s) e {int(experimental_h1.get('regimes_count', 0))} regime(s).")
    lines.append("- O ranking atual privilegia prioridade/confiança de clusters pequenos e pode superestimar amostras minúsculas com PF extremo.")
    lines.append("")
    lines.append("## 9. Recomendacao Cientifica Final")
    if experimental_h1 and experimental_h1.get("cluster_id") != h1_row.get("cluster_id"):
        lines.append(f"Recomenda-se substituir a H1 atual por {experimental_h1.get('cluster_id')} ({experimental_h1.get('hypothesis_id')}) para a proxima etapa de pesquisa.")
    else:
        lines.append("A H1 atual permanece como melhor candidato disponível sob o score experimental.")

    md_path = RESULTS_DIR / "fase53_auditoria_cientifica.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    summary = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_run_id": lab_meta.get("run_id"),
        "cluster_count": int(len(report)),
        "current_h1": h1_row,
        "experimental_h1": experimental_h1,
        "decision": "A" if experimental_h1 and experimental_h1.get("cluster_id") != h1_row.get("cluster_id") else "B",
        "outputs": {
            "markdown": str(md_path),
            "cluster_audit_csv": str(report_csv),
            "top10_csv": str(top10_csv),
            "distributions_csv": str(distributions_csv),
        },
    }
    summary_path = RESULTS_DIR / "fase53_auditoria_cientifica.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print(md_path)
    print(report_csv)
    print(top10_csv)
    print(distributions_csv)


if __name__ == "__main__":
    main()