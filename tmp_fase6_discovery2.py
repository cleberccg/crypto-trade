from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE = Path("optimization/results")
AUDIT_CSV = BASE / "fase53_cluster_audit.csv"
DISCOVERY_JSON = BASE / "quantitative_discovery_lab_20260629_152434.json"
EVENTS_GLOB = "quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv"

OUT_CSV = BASE / "fase6_discovery2_clusters.csv"
OUT_JSON = BASE / "fase6_discovery2_report.json"
OUT_MD = BASE / "fase6_discovery2_report.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        if isinstance(v, str) and (v.strip() == "" or v.lower() in {"nan", "none", "<na>"}):
            return default
        out = float(v)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def percentile_rank(series: pd.Series) -> pd.Series:
    return series.rank(pct=True, method="average")


@dataclass
class ClusterAgg:
    count: int = 0
    wins: int = 0
    losses: int = 0
    futret_sum: float = 0.0
    futret_sumsq: float = 0.0


def load_value_importance(path: Path) -> dict[tuple[str, str], float]:
    data = json.loads(path.read_text(encoding="utf-8"))
    pairs: dict[tuple[str, str], float] = {}

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            if {"feature", "value", "importance_score"}.issubset(obj.keys()):
                feature = str(obj.get("feature"))
                value = str(obj.get("value"))
                score = safe_float(obj.get("importance_score"), 0.0)
                key = (feature, value)
                if key not in pairs or score > pairs[key]:
                    pairs[key] = score
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(data)
    return pairs


def feature_importance_for_cluster(cluster_row: pd.Series, value_imp: dict[tuple[str, str], float]) -> float:
    # Map cluster dimensions to likely discovery feature names.
    candidates = [
        ("primary_regime", str(cluster_row["regime"])),
        ("regime", str(cluster_row["regime"])),
        ("atr_bucket", str(cluster_row["atr_bucket"])),
        ("rsi_bucket", str(cluster_row["rsi_bucket"])),
        ("volume_bucket", str(cluster_row["volume_bucket"])),
        ("bollinger_position", str(cluster_row["bollinger_position"])),
        ("direction", str(cluster_row["direction"])),
    ]
    vals: list[float] = []
    for feature, value in candidates:
        if (feature, value) in value_imp:
            vals.append(value_imp[(feature, value)])
            continue
        # fallback: best score for matching value regardless of feature name
        fallback = [score for (f, v), score in value_imp.items() if v == value]
        vals.append(max(fallback) if fallback else 0.0)
    return float(np.mean(vals)) if vals else 0.0


def compute_cluster_key(df: pd.DataFrame) -> pd.Series:
    return (
        df["regime"].astype(str)
        + "|"
        + df["atr_bucket"].astype(str)
        + "|"
        + df["rsi_bucket"].astype(str)
        + "|"
        + df["volume_bucket"].astype(str)
        + "|"
        + df["bollinger_position"].astype(str)
        + "|"
        + df["direction"].astype(str)
    )


def stdev(values: list[float]) -> float:
    if len(values) <= 1:
        return 0.0
    return float(np.std(np.asarray(values, dtype=float), ddof=0))


def cv_score(values: list[float], default: float = 0.5) -> float:
    if len(values) <= 1:
        return default
    arr = np.asarray(values, dtype=float)
    mu = float(np.mean(arr))
    if abs(mu) < 1e-12:
        return default
    cv = float(np.std(arr, ddof=0) / abs(mu))
    return clip01(1.0 - cv)


def classify_tradability(row: pd.Series) -> str:
    score = safe_float(row["implementability_score"])
    sep = safe_float(row["class_separation"])
    trades = int(safe_float(row["sample_size"]))
    robustness = safe_float(row["robustness_score"])

    if score >= 70 and sep >= 0.12 and trades >= 500 and robustness >= 0.55:
        return "Tradavel"
    if score >= 55 and trades >= 100:
        return "Contextual"
    if score >= 40:
        return "Auxiliar"
    return "Nao tradavel"


def difficulty_label(score: float, simplicity: float) -> str:
    if score >= 80 and simplicity >= 0.7:
        return "Muito facil"
    if score >= 70:
        return "Facil"
    if score >= 55:
        return "Media"
    if score >= 40:
        return "Dificil"
    return "Muito dificil"


def main() -> None:
    BASE.mkdir(parents=True, exist_ok=True)

    audit = pd.read_csv(AUDIT_CSV)
    value_imp = load_value_importance(DISCOVERY_JSON)

    # Normalized cluster key map from audited 67 clusters.
    key_to_cluster = dict(zip(audit["cluster_key"].astype(str), audit["cluster_id"].astype(str), strict=False))
    cluster_ids = set(audit["cluster_id"].astype(str).tolist())

    agg: dict[str, ClusterAgg] = {cid: ClusterAgg() for cid in cluster_ids}
    assets_by_cluster: dict[str, Counter[str]] = defaultdict(Counter)
    tfs_by_cluster: dict[str, Counter[str]] = defaultdict(Counter)
    years_by_cluster: dict[str, Counter[str]] = defaultdict(Counter)
    regimes_by_cluster: dict[str, Counter[str]] = defaultdict(Counter)
    asset_win: dict[tuple[str, str], int] = defaultdict(int)
    asset_n: dict[tuple[str, str], int] = defaultdict(int)
    year_win: dict[tuple[str, str], int] = defaultdict(int)
    year_n: dict[tuple[str, str], int] = defaultdict(int)

    event_paths = sorted(Path("optimization/results").glob(EVENTS_GLOB))

    usecols = [
        "symbol",
        "timeframe",
        "open_time",
        "regime",
        "atr_bucket",
        "rsi_bucket",
        "volume_bucket",
        "bollinger_position",
        "direction",
        "win_flag",
        "loss_flag",
        "future_return",
    ]

    for path in event_paths:
        for chunk in pd.read_csv(path, usecols=usecols, chunksize=250_000, low_memory=False):
            chunk = chunk.dropna(subset=["regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "direction"])
            if chunk.empty:
                continue

            chunk["cluster_key"] = compute_cluster_key(chunk)
            chunk["cluster_id"] = chunk["cluster_key"].map(key_to_cluster)
            chunk = chunk[chunk["cluster_id"].notna()].copy()
            if chunk.empty:
                continue

            chunk["year"] = chunk["open_time"].astype(str).str.slice(0, 4)
            chunk["win_flag"] = pd.to_numeric(chunk["win_flag"], errors="coerce").fillna(0.0)
            chunk["loss_flag"] = pd.to_numeric(chunk["loss_flag"], errors="coerce").fillna(0.0)
            chunk["future_return"] = pd.to_numeric(chunk["future_return"], errors="coerce").fillna(0.0)

            for cid, g in chunk.groupby("cluster_id", observed=True):
                cid = str(cid)
                wins = int(g["win_flag"].sum())
                losses = int(g["loss_flag"].sum())
                cnt = int(len(g))
                fr = g["future_return"].to_numpy(dtype=float)

                a = agg[cid]
                a.count += cnt
                a.wins += wins
                a.losses += losses
                a.futret_sum += float(fr.sum())
                a.futret_sumsq += float(np.square(fr).sum())

                assets_by_cluster[cid].update(g["symbol"].astype(str).tolist())
                tfs_by_cluster[cid].update(g["timeframe"].astype(str).tolist())
                years_by_cluster[cid].update(g["year"].astype(str).tolist())
                regimes_by_cluster[cid].update(g["regime"].astype(str).tolist())

                for sym, gg in g.groupby("symbol", observed=True):
                    k = (cid, str(sym))
                    asset_n[k] += int(len(gg))
                    asset_win[k] += int(gg["win_flag"].sum())

                for yr, gg in g.groupby("year", observed=True):
                    k = (cid, str(yr))
                    year_n[k] += int(len(gg))
                    year_win[k] += int(gg["win_flag"].sum())

    total_events = sum(v.count for v in agg.values())
    total_wins = sum(v.wins for v in agg.values())
    baseline_win_rate = (total_wins / total_events) if total_events > 0 else 0.5

    out = audit.copy()
    out["sample_size"] = out["cluster_id"].astype(str).map(lambda c: agg.get(c, ClusterAgg()).count)
    out["assets_covered"] = out["cluster_id"].astype(str).map(lambda c: len(assets_by_cluster.get(c, {})))
    out["timeframes_covered"] = out["cluster_id"].astype(str).map(lambda c: len(tfs_by_cluster.get(c, {})))
    out["years_covered"] = out["cluster_id"].astype(str).map(lambda c: len(years_by_cluster.get(c, {})))
    out["regimes_covered"] = out["cluster_id"].astype(str).map(lambda c: len(regimes_by_cluster.get(c, {})))

    purity_vals: list[float] = []
    sep_vals: list[float] = []
    temp_vals: list[float] = []
    asset_vals: list[float] = []
    regime_vals: list[float] = []
    fi_vals: list[float] = []

    for _, row in out.iterrows():
        cid = str(row["cluster_id"])
        a = agg.get(cid, ClusterAgg())
        n = max(1, a.count)
        wr = a.wins / n
        lr = a.losses / n if a.losses > 0 else max(0.0, 1.0 - wr)

        purity = max(wr, lr)
        class_sep = abs(wr - baseline_win_rate)

        # temporal stability as inverse CV of per-year win rates (with minimum support)
        year_rates: list[float] = []
        for y in years_by_cluster.get(cid, {}):
            n_y = year_n.get((cid, y), 0)
            if n_y >= 100:
                year_rates.append(year_win.get((cid, y), 0) / n_y)
        temporal_stability = cv_score(year_rates, default=0.4 if len(year_rates) == 0 else 0.6)

        # asset consistency as inverse CV of per-asset win rates (with minimum support)
        asset_rates: list[float] = []
        for sym in assets_by_cluster.get(cid, {}):
            n_s = asset_n.get((cid, sym), 0)
            if n_s >= 100:
                asset_rates.append(asset_win.get((cid, sym), 0) / n_s)
        asset_consistency = cv_score(asset_rates, default=0.35 if len(asset_rates) == 0 else 0.6)

        # cluster is usually mono-regime by design; consistency is high when concentrated.
        reg_counter = regimes_by_cluster.get(cid, Counter())
        reg_total = sum(reg_counter.values())
        if reg_total > 0:
            regime_consistency = max(reg_counter.values()) / reg_total
        else:
            regime_consistency = 0.0

        feature_imp = feature_importance_for_cluster(row, value_imp)

        purity_vals.append(float(purity))
        sep_vals.append(float(class_sep))
        temp_vals.append(float(temporal_stability))
        asset_vals.append(float(asset_consistency))
        regime_vals.append(float(regime_consistency))
        fi_vals.append(float(feature_imp))

    out["cluster_purity"] = purity_vals
    out["class_separation"] = sep_vals
    out["temporal_stability"] = temp_vals
    out["asset_consistency"] = asset_vals
    out["regime_consistency"] = regime_vals
    out["feature_importance"] = fi_vals

    # Simplicity / implementability components
    def simplicity_from_row(r: pd.Series) -> float:
        active = 2  # regime + direction
        if str(r["atr_bucket"]) not in {"mid_atr", "unknown"}:
            active += 1
        if str(r["rsi_bucket"]) not in {"unknown", "mid_rsi", "neutral"}:
            active += 1
        if str(r["volume_bucket"]) not in {"normal_volume", "unknown"}:
            active += 1
        if str(r["bollinger_position"]) not in {"inside_band", "unknown"}:
            active += 1
        return clip01(1.0 - ((active - 2) / 4.0))

    out["simplicity"] = out.apply(simplicity_from_row, axis=1)

    out["threshold_clarity"] = 0.9
    out["separability_score"] = out["class_separation"].map(lambda v: clip01(float(v) / 0.35))
    out["historical_coverage"] = out.apply(
        lambda r: clip01((safe_float(r["years_covered"]) / 10.0) * (math.log10(max(1.0, safe_float(r["sample_size"])) + 1.0) / 6.0)),
        axis=1,
    )
    out["statistical_stability"] = out["sample_size"].map(lambda n: clip01(math.log10(max(1.0, float(n)) + 1.0) / 6.0))
    out["parameter_sensitivity"] = out["balance_score"].map(lambda x: clip01(safe_float(x)))

    out["robustness_score"] = (
        0.45 * out["temporal_stability"] + 0.3 * out["asset_consistency"] + 0.25 * out["regime_consistency"]
    )

    out["implementability_score"] = (
        15 * out["simplicity"]
        + 10 * out["threshold_clarity"]
        + 15 * out["separability_score"]
        + 15 * out["temporal_stability"]
        + 10 * out["asset_consistency"]
        + 10 * out["regime_consistency"]
        + 10 * out["parameter_sensitivity"]
        + 10 * out["historical_coverage"]
        + 5 * out["statistical_stability"]
    )

    pf_rank = percentile_rank(out["profit_factor"].fillna(0.0).map(safe_float))
    sharpe_rank = percentile_rank(out["sharpe"].fillna(0.0).map(safe_float))
    expectancy_rank = percentile_rank(out["expectancy"].fillna(0.0).map(safe_float))
    edge_rank = (pf_rank + sharpe_rank + expectancy_rank) / 3.0
    out["edge_statistical"] = edge_rank

    out["ranking_score"] = (
        0.20 * out["edge_statistical"]
        + 0.25 * (out["implementability_score"] / 100.0)
        + 0.15 * out["temporal_stability"]
        + 0.10 * out["asset_consistency"]
        + 0.10 * out["regime_consistency"]
        + 0.10 * out["simplicity"]
        + 0.05 * out["historical_coverage"]
        + 0.05 * out["statistical_stability"]
    )

    out["tradability_class"] = out.apply(classify_tradability, axis=1)

    out = out.sort_values(["ranking_score", "implementability_score", "sample_size"], ascending=[False, False, False]).reset_index(drop=True)
    out["rank_fase6"] = np.arange(1, len(out) + 1)

    top20 = out.head(20).copy()
    top20["implementation_difficulty"] = top20.apply(
        lambda r: difficulty_label(safe_float(r["implementability_score"]), safe_float(r["simplicity"])), axis=1
    )

    top10 = out.head(10).copy()

    best = out.iloc[0]
    decision = "A" if (best["tradability_class"] == "Tradavel" and safe_float(best["implementability_score"]) >= 72.0 and safe_float(best["robustness_score"]) >= 0.60) else "B"

    best_cluster = {
        "cluster_id": str(best["cluster_id"]),
        "hypothesis_id": str(best["hypothesis_id"]),
        "family": str(best["family"]),
        "rank_fase6": int(best["rank_fase6"]),
        "implementability_score": round(safe_float(best["implementability_score"]), 4),
        "ranking_score": round(safe_float(best["ranking_score"]), 6),
        "tradability_class": str(best["tradability_class"]),
        "edge_statistical": round(safe_float(best["edge_statistical"]), 6),
        "robustness_score": round(safe_float(best["robustness_score"]), 6),
        "sample_size": int(safe_float(best["sample_size"])),
        "assets_covered": int(safe_float(best["assets_covered"])),
        "timeframes_covered": int(safe_float(best["timeframes_covered"])),
        "years_covered": int(safe_float(best["years_covered"])),
        "regimes_covered": int(safe_float(best["regimes_covered"])),
    }

    # Stage-7 pre-implementation questionnaire.
    stage7 = {
        "q1_tradavel": "Sim" if best_cluster["tradability_class"] == "Tradavel" else "Nao claramente",
        "q2_context_only": "Sim" if best_cluster["tradability_class"] in {"Contextual", "Auxiliar"} else "Nao",
        "q3_needs_confirmation": "Sim" if best_cluster["tradability_class"] != "Tradavel" else "Parcial",
        "q4_confirmation_type": "Confirmacao de direcao (trend_score/EMA), momentum (RSI) e volatilidade (ATR buckets) com filtro de volume.",
        "q5_existing_indicators": [
            "EMA",
            "RSI",
            "ATR",
            "Bollinger Bands",
            "Relative Volume",
            "Trend Score",
        ],
    }

    report = {
        "generated_at": now_iso(),
        "source": {
            "cluster_audit_csv": str(AUDIT_CSV),
            "discovery_json": str(DISCOVERY_JSON),
            "events_glob": EVENTS_GLOB,
            "event_files_scanned": len(event_paths),
        },
        "counts": {
            "clusters": int(len(out)),
            "top20": int(len(top20)),
            "top10": int(len(top10)),
            "total_events_mapped": int(total_events),
        },
        "best_cluster": best_cluster,
        "decision": decision,
        "stage7_validation": stage7,
        "notes": [
            "Implementability Score prioriza simplicidade, robustez e cobertura, nao apenas PF/Sharpe.",
            "Classificacao tradability: Tradavel, Contextual, Auxiliar, Nao tradavel.",
            "Top20 recebeu simulacao de dificuldade de implementacao sem gerar estrategia.",
        ],
    }

    out.to_csv(OUT_CSV, index=False)
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

    lines: list[str] = []
    lines.append("# FASE 6 - Discovery 2.0 (Implementability-First)")
    lines.append("")
    lines.append("## Resultado")
    lines.append(f"- Clusters auditados: {len(out)}")
    lines.append(f"- Eventos mapeados para clusters: {total_events}")
    lines.append(f"- Melhor candidato: {best_cluster['cluster_id']} ({best_cluster['tradability_class']})")
    lines.append(f"- Implementability Score (melhor): {best_cluster['implementability_score']:.2f}")
    lines.append(f"- Ranking Score (melhor): {best_cluster['ranking_score']:.4f}")
    lines.append("")

    lines.append("## Top 10 Candidatos")
    for _, r in top10.iterrows():
        lines.append(
            "- "
            + f"#{int(r['rank_fase6'])} {r['cluster_id']} | impl={safe_float(r['implementability_score']):.2f}"
            + f" | class={r['tradability_class']} | edge={safe_float(r['edge_statistical']):.3f}"
            + f" | robust={safe_float(r['robustness_score']):.3f}"
        )
    lines.append("")

    lines.append("## Top 20 - Dificuldade de Implementacao (simulada)")
    for _, r in top20.iterrows():
        lines.append(
            "- "
            + f"{r['cluster_id']} | dificuldade={r['implementation_difficulty']}"
            + f" | impl={safe_float(r['implementability_score']):.2f} | simplicidade={safe_float(r['simplicity']):.2f}"
        )
    lines.append("")

    lines.append("## Classificacao de Tradabilidade")
    class_counts = out["tradability_class"].value_counts().to_dict()
    for k in ["Tradavel", "Contextual", "Auxiliar", "Nao tradavel"]:
        lines.append(f"- {k}: {int(class_counts.get(k, 0))}")
    lines.append("")

    lines.append("## Riscos Esperados")
    lines.append("- Overfitting de clusters com amostra pequena e PF extremo.")
    lines.append("- Clusters mono-regime podem descrever contexto e nao gatilho operacional completo.")
    lines.append("- Robustez entre ativos reduzida em clusters com baixa cobertura de simbolos.")
    lines.append("")

    lines.append("## Validacao Pre-Implementacao (Etapa 7)")
    lines.append(f"- 1. Tradavel? {stage7['q1_tradavel']}")
    lines.append(f"- 2. Apenas contexto? {stage7['q2_context_only']}")
    lines.append(f"- 3. Precisa confirmacao adicional? {stage7['q3_needs_confirmation']}")
    lines.append(f"- 4. Qual confirmacao? {stage7['q4_confirmation_type']}")
    lines.append("- 5. Indicadores existentes: " + ", ".join(stage7["q5_existing_indicators"]))
    lines.append("")

    lines.append("## Decisao Final")
    if decision == "A":
        lines.append("- OPCAO A: Existe cluster superior e implementavel; prosseguir para Fase 7.")
    else:
        lines.append("- OPCAO B: Nenhum cluster atingiu qualidade/implementabilidade robusta suficiente para nova implementacao agora.")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print("WROTE", OUT_CSV)
    print("WROTE", OUT_JSON)
    print("WROTE", OUT_MD)


if __name__ == "__main__":
    main()
