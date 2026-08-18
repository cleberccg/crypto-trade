from __future__ import annotations

import itertools
import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

BASE = Path("optimization/results")
PHASE6_CSV = BASE / "fase6_discovery2_clusters.csv"
EVENTS_GLOB = "quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv"

OUT_JSON = BASE / "fase7_decision_discovery_report.json"
OUT_MD = BASE / "fase7_decision_discovery_report.md"
OUT_CSV = BASE / "fase7_decision_discovery_candidates.csv"

EPS = 0.0002
MIN_SUPPORT = 20
MIN_SUPPORT_SHARE = 0.03
OPERABILITY_THRESHOLD = 72.0


@dataclass
class Condition:
    expr: str
    mask: pd.Series
    complexity: int


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clip01(x: float) -> float:
    return max(0.0, min(1.0, x))


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        out = float(v)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def build_cluster_key(df: pd.DataFrame) -> pd.Series:
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


def direction_from_return(r: float) -> str:
    if r > EPS:
        return "alta"
    if r < -EPS:
        return "baixa"
    return "lateralizacao"


def cv_stability(values: list[float], default: float = 0.4) -> float:
    if len(values) <= 1:
        return default
    arr = np.asarray(values, dtype=float)
    mu = float(np.mean(arr))
    if abs(mu) < 1e-12:
        return default
    cv = float(np.std(arr, ddof=0) / abs(mu))
    return clip01(1.0 - cv)


def normalize_score(series: pd.Series) -> pd.Series:
    smin = float(series.min())
    smax = float(series.max())
    if abs(smax - smin) < 1e-12:
        return pd.Series(np.ones(len(series)) * 0.5, index=series.index)
    return (series - smin) / (smax - smin)


def generate_conditions(df: pd.DataFrame) -> list[Condition]:
    conditions: list[Condition] = []

    numeric_cols = [
        c
        for c in ["trend_score", "atr_pct", "distance_to_ema_pct", "relative_volume", "mfe", "mae", "future_upside", "future_downside"]
        if c in df.columns
    ]
    cat_cols = [c for c in ["rsi_bucket", "atr_bucket", "volume_bucket", "bollinger_position", "primary_regime", "primary_profile"] if c in df.columns]

    for col in numeric_cols:
        vals = pd.to_numeric(df[col], errors="coerce").dropna()
        if vals.empty:
            continue
        qs = sorted(set(float(vals.quantile(q)) for q in [0.2, 0.4, 0.6, 0.8]))
        for q in qs:
            mask_ge = pd.to_numeric(df[col], errors="coerce") >= q
            mask_le = pd.to_numeric(df[col], errors="coerce") <= q
            conditions.append(Condition(expr=f"{col}>={q:.6g}", mask=mask_ge.fillna(False), complexity=1))
            conditions.append(Condition(expr=f"{col}<={q:.6g}", mask=mask_le.fillna(False), complexity=1))

    for col in cat_cols:
        vc = df[col].astype(str).value_counts(dropna=False)
        for value, count in vc.head(5).items():
            if int(count) < MIN_SUPPORT:
                continue
            mask = df[col].astype(str) == str(value)
            conditions.append(Condition(expr=f"{col}=={value}", mask=mask, complexity=1))

    return conditions


def evaluate_conditions(df: pd.DataFrame, conditions: list[Condition], side: str) -> list[dict[str, Any]]:
    assert side in {"LONG", "SHORT"}
    if side == "LONG":
        target = (df["future_return"] > EPS).astype(int)
        signed_return = df["future_return"]
    else:
        target = (df["future_return"] < -EPS).astype(int)
        signed_return = -df["future_return"]

    base_rate = float(target.mean()) if len(target) else 0.0
    results: list[dict[str, Any]] = []

    for cond in conditions:
        mask = cond.mask
        support = int(mask.sum())
        if support < MIN_SUPPORT or support / max(1, len(df)) < MIN_SUPPORT_SHARE:
            continue

        precision = float(target[mask].mean()) if support > 0 else 0.0
        lift = precision / base_rate if base_rate > 1e-12 else 0.0
        avg_ret = float(signed_return[mask].mean()) if support > 0 else 0.0
        score = precision * max(1.0, lift) * math.sqrt(support / max(1, len(df))) * max(0.0, avg_ret + 0.001)

        results.append(
            {
                "rule_expr": cond.expr,
                "complexity": cond.complexity,
                "support": support,
                "support_share": support / max(1, len(df)),
                "precision": precision,
                "base_rate": base_rate,
                "lift": lift,
                "avg_signed_return": avg_ret,
                "raw_score": score,
                "mask": mask,
            }
        )

    results.sort(key=lambda x: (x["raw_score"], x["precision"], x["support"]), reverse=True)

    # Compose pairwise conditions from the best single rules.
    top_single = results[:8]
    pairwise: list[dict[str, Any]] = []
    for a, b in itertools.combinations(top_single, 2):
        mask = a["mask"] & b["mask"]
        support = int(mask.sum())
        if support < MIN_SUPPORT or support / max(1, len(df)) < MIN_SUPPORT_SHARE:
            continue
        precision = float(target[mask].mean()) if support > 0 else 0.0
        lift = precision / base_rate if base_rate > 1e-12 else 0.0
        avg_ret = float(signed_return[mask].mean()) if support > 0 else 0.0
        score = precision * max(1.0, lift) * math.sqrt(support / max(1, len(df))) * max(0.0, avg_ret + 0.001)
        pairwise.append(
            {
                "rule_expr": f"({a['rule_expr']}) AND ({b['rule_expr']})",
                "complexity": 2,
                "support": support,
                "support_share": support / max(1, len(df)),
                "precision": precision,
                "base_rate": base_rate,
                "lift": lift,
                "avg_signed_return": avg_ret,
                "raw_score": score,
                "mask": mask,
            }
        )

    all_rules = results + pairwise
    all_rules.sort(key=lambda x: (x["raw_score"], x["precision"], x["support"]), reverse=True)
    return all_rules


def exit_rules_from_mask(df: pd.DataFrame, mask: pd.Series, side: str) -> dict[str, Any]:
    sub = df[mask].copy()
    if sub.empty:
        return {
            "stop_loss_pct": None,
            "take_profit_pct": None,
            "max_hold_minutes": None,
            "cancel_conditions": [],
        }

    sub["future_upside"] = pd.to_numeric(sub["future_upside"], errors="coerce").fillna(0.0)
    sub["future_downside"] = pd.to_numeric(sub["future_downside"], errors="coerce").fillna(0.0)
    sub["duration_minutes"] = pd.to_numeric(sub["duration_minutes"], errors="coerce").fillna(0.0)

    if side == "LONG":
        adverse = (-sub["future_downside"]).clip(lower=0.0)
        favorable = sub["future_upside"].clip(lower=0.0)
        losers = sub[sub["future_return"] <= EPS]
    else:
        adverse = sub["future_upside"].clip(lower=0.0)
        favorable = (-sub["future_downside"]).clip(lower=0.0)
        losers = sub[sub["future_return"] >= -EPS]

    stop_loss_pct = float(adverse.quantile(0.80))
    take_profit_pct = float(favorable.quantile(0.60))
    max_hold_minutes = int(max(5, float(sub["duration_minutes"].quantile(0.75))))

    cancel_conditions: list[str] = []
    if "primary_profile" in losers.columns and len(losers) > 0:
        vc = losers["primary_profile"].astype(str).value_counts(normalize=True)
        for profile, share in vc.items():
            if share >= 0.20:
                cancel_conditions.append(f"primary_profile={profile} ({share:.1%} dos losers)")

    return {
        "stop_loss_pct": round(stop_loss_pct, 6),
        "take_profit_pct": round(take_profit_pct, 6),
        "max_hold_minutes": max_hold_minutes,
        "cancel_conditions": cancel_conditions[:3],
    }


def robustness_components(df: pd.DataFrame, mask: pd.Series, side: str) -> tuple[float, float, float]:
    if side == "LONG":
        y = (df["future_return"] > EPS).astype(int)
    else:
        y = (df["future_return"] < -EPS).astype(int)

    sub = df[mask]
    if sub.empty:
        return 0.0, 0.0, 0.0

    def grouped_precision(key: str, min_n: int = 10) -> list[float]:
        vals: list[float] = []
        if key not in sub.columns:
            return vals
        for g, idx in sub.groupby(key, observed=True).groups.items():
            if len(idx) >= min_n:
                vals.append(float(y.loc[idx].mean()))
        return vals

    temporal = cv_stability(grouped_precision("year", 20), default=0.45)
    asset = cv_stability(grouped_precision("symbol", 20), default=0.40)
    regime = cv_stability(grouped_precision("primary_regime", 20), default=0.50)
    return temporal, asset, regime


def discover_for_context(cluster_row: pd.Series, context_df: pd.DataFrame) -> dict[str, Any]:
    cid = str(cluster_row["cluster_id"])
    direction = str(cluster_row["direction"]).upper()

    if context_df.empty:
        return {
            "cluster_id": cid,
            "context_stats": {},
            "confirmations": [],
            "entry_rules": [],
            "exit_rules": [],
            "candidates": [],
        }

    context_df = context_df.copy()
    context_df["future_return"] = pd.to_numeric(context_df["future_return"], errors="coerce").fillna(0.0)
    context_df["year"] = context_df["open_time"].astype(str).str.slice(0, 4)

    # ETAPA 1: context behavior after cluster
    dir_labels = context_df["future_return"].map(direction_from_return)
    direction_prevalence = dir_labels.value_counts(normalize=True).to_dict()

    if direction == "BUY":
        continuation = float((context_df["future_return"] > EPS).mean())
        reversal = float((context_df["future_return"] < -EPS).mean())
    else:
        continuation = float((context_df["future_return"] < -EPS).mean())
        reversal = float((context_df["future_return"] > EPS).mean())

    context_stats = {
        "sample_size": int(len(context_df)),
        "assets": int(context_df["symbol"].nunique()),
        "timeframes": int(context_df["timeframe"].nunique()),
        "years": int(context_df["year"].nunique()),
        "regimes": int(context_df["primary_regime"].nunique()) if "primary_regime" in context_df.columns else 0,
        "delay_minutes_median": float(pd.to_numeric(context_df["duration_minutes"], errors="coerce").fillna(0.0).median()),
        "delay_minutes_p75": float(pd.to_numeric(context_df["duration_minutes"], errors="coerce").fillna(0.0).quantile(0.75)),
        "direction_prevalence": {k: float(v) for k, v in direction_prevalence.items()},
        "prob_continuation": continuation,
        "prob_reversal": reversal,
    }

    # ETAPA 2 + 3: automatic confirmations and entry rules
    conditions = generate_conditions(context_df)
    long_rules = evaluate_conditions(context_df, conditions, "LONG")
    short_rules = evaluate_conditions(context_df, conditions, "SHORT")

    selected = []
    for side, rules in [("LONG", long_rules), ("SHORT", short_rules)]:
        for item in rules[:5]:
            mask = item["mask"]
            temporal, asset, regime = robustness_components(context_df, mask, side)
            simplicity = 1.0 if item["complexity"] == 1 else 0.72
            edge = clip01((item["precision"] - item["base_rate"] + max(0.0, item["avg_signed_return"])) / 0.35)
            ease = clip01(0.7 * simplicity + 0.3 * min(1.0, item["support"] / 400.0))
            stability = clip01(0.6 * min(1.0, item["support"] / 500.0) + 0.4 * ((temporal + asset + regime) / 3.0))

            operability = (
                25 * edge
                + 15 * simplicity
                + 15 * temporal
                + 12 * asset
                + 10 * regime
                + 13 * ease
                + 10 * stability
            )

            exit_rules = exit_rules_from_mask(context_df, mask, side)

            rec_conf = "Alta" if operability >= 75 else ("Media" if operability >= 60 else "Baixa")
            would_be_operational = bool(
                operability >= OPERABILITY_THRESHOLD
                and temporal >= 0.55
                and asset >= 0.50
                and regime >= 0.55
                and item["support"] >= 80
            )

            selected.append(
                {
                    "cluster_id": cid,
                    "hypothesis_id": str(cluster_row.get("hypothesis_id", "")),
                    "family": str(cluster_row.get("family", "")),
                    "context_direction": direction,
                    "side": side,
                    "confirmation_rule": item["rule_expr"],
                    "rule_complexity": int(item["complexity"]),
                    "support": int(item["support"]),
                    "support_share": float(item["support_share"]),
                    "precision": float(item["precision"]),
                    "lift": float(item["lift"]),
                    "avg_signed_return": float(item["avg_signed_return"]),
                    "robust_temporal": float(temporal),
                    "robust_asset": float(asset),
                    "robust_regime": float(regime),
                    "simplicity": float(simplicity),
                    "operability_score": float(operability),
                    "operational": would_be_operational,
                    "depends_manual_tuning": bool(item["complexity"] > 1 or operability < 70),
                    "recommendation_confidence": rec_conf,
                    "entry_rule": f"SE contexto {cid} E {item['rule_expr']} ENTAO entrada {side}",
                    "exit_rule": exit_rules,
                }
            )

    selected.sort(key=lambda x: x["operability_score"], reverse=True)
    selected = selected[:10]

    return {
        "cluster_id": cid,
        "context_stats": context_stats,
        "confirmations": [
            {
                "side": r["side"],
                "rule": r["confirmation_rule"],
                "support": r["support"],
                "precision": r["precision"],
                "lift": r["lift"],
            }
            for r in selected[:6]
        ],
        "entry_rules": [r["entry_rule"] for r in selected[:6]],
        "exit_rules": [r["exit_rule"] for r in selected[:6]],
        "candidates": selected,
    }


def main() -> None:
    phase6 = pd.read_csv(PHASE6_CSV)
    contexts = phase6[phase6["tradability_class"] == "Contextual"].copy()
    if contexts.empty:
        raise RuntimeError("No contextual clusters found in fase6_discovery2_clusters.csv")

    key_to_context = dict(zip(contexts["cluster_key"].astype(str), contexts["cluster_id"].astype(str), strict=False))
    context_rows = {str(r["cluster_id"]): r for _, r in contexts.iterrows()}

    # Collect only rows that belong to contextual clusters.
    columns = [
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
        "mfe",
        "mae",
        "regime",
    ]

    bucket: dict[str, list[pd.DataFrame]] = {str(cid): [] for cid in contexts["cluster_id"].astype(str).tolist()}

    event_paths = sorted((BASE / "quantitative_discovery_chunks/fase52_full_ultra_20260629/events").glob("events_*.csv"))
    for path in event_paths:
        for chunk in pd.read_csv(path, usecols=columns, chunksize=250_000, low_memory=False):
            chunk = chunk.dropna(subset=["regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "direction"])
            if chunk.empty:
                continue
            ck = (
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
            chunk["cluster_key"] = ck
            chunk["cluster_id"] = chunk["cluster_key"].map(key_to_context)
            sub = chunk[chunk["cluster_id"].notna()].copy()
            if sub.empty:
                continue
            for cid, g in sub.groupby("cluster_id", observed=True):
                bucket[str(cid)].append(g)

    context_reports: list[dict[str, Any]] = []
    candidates_rows: list[dict[str, Any]] = []

    for cid, parts in bucket.items():
        dfc = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=columns + ["cluster_key", "cluster_id"])
        rep = discover_for_context(context_rows[cid], dfc)
        context_reports.append(rep)
        candidates_rows.extend(rep["candidates"])

    candidates = pd.DataFrame(candidates_rows)
    if candidates.empty:
        raise RuntimeError("No rule candidates discovered for contextual clusters")

    candidates = candidates.sort_values(["operability_score", "support", "precision"], ascending=[False, False, False]).reset_index(drop=True)
    candidates["rank"] = np.arange(1, len(candidates) + 1)

    best = candidates.iloc[0]

    # ETAPA 7 criteria
    meets_criteria = bool(
        best["operability_score"] >= OPERABILITY_THRESHOLD
        and best["operational"]
        and best["robust_temporal"] >= 0.55
        and best["robust_asset"] >= 0.55
        and best["robust_regime"] >= 0.55
    )

    decision = "A" if meets_criteria else "B"

    report = {
        "generated_at": now_iso(),
        "contexts_analyzed": [
            {
                "cluster_id": r["cluster_id"],
                "hypothesis_id": r.get("hypothesis_id"),
                "family": r.get("family"),
                "sample_size": r["context_stats"].get("sample_size"),
                "prob_continuation": r["context_stats"].get("prob_continuation"),
                "prob_reversal": r["context_stats"].get("prob_reversal"),
            }
            for r in context_reports
        ],
        "confirmations_discovered": [
            {
                "cluster_id": r["cluster_id"],
                "rules": r["confirmations"],
            }
            for r in context_reports
        ],
        "entry_rules": [
            {
                "cluster_id": r["cluster_id"],
                "rules": r["entry_rules"],
            }
            for r in context_reports
        ],
        "exit_rules": [
            {
                "cluster_id": r["cluster_id"],
                "rules": r["exit_rules"],
            }
            for r in context_reports
        ],
        "operability_threshold": OPERABILITY_THRESHOLD,
        "best_candidate": {
            "cluster_id": str(best["cluster_id"]),
            "side": str(best["side"]),
            "confirmation_rule": str(best["confirmation_rule"]),
            "entry_rule": str(best["entry_rule"]),
            "exit_rule": best["exit_rule"],
            "operability_score": float(best["operability_score"]),
            "support": int(best["support"]),
            "precision": float(best["precision"]),
            "lift": float(best["lift"]),
            "robust_temporal": float(best["robust_temporal"]),
            "robust_asset": float(best["robust_asset"]),
            "robust_regime": float(best["robust_regime"]),
            "recommendation_confidence": str(best["recommendation_confidence"]),
        },
        "decision": decision,
        "justification": (
            "Existe conjunto completo e robusto" if decision == "A" else "Evidencia ainda insuficiente para implementacao sem ajustes manuais"
        ),
    }

    # Persist
    out_cols = [
        "rank",
        "cluster_id",
        "hypothesis_id",
        "family",
        "side",
        "confirmation_rule",
        "entry_rule",
        "operability_score",
        "support",
        "precision",
        "lift",
        "robust_temporal",
        "robust_asset",
        "robust_regime",
        "simplicity",
        "operational",
        "depends_manual_tuning",
        "recommendation_confidence",
    ]
    candidates[out_cols].to_csv(OUT_CSV, index=False)
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

    top10 = candidates.head(10)
    lines: list[str] = []
    lines.append("# FASE 7 - Decision Discovery Lab")
    lines.append("")
    lines.append("## Contextos Analisados")
    for r in report["contexts_analyzed"]:
        lines.append(
            f"- {r['cluster_id']} | sample={r['sample_size']} | p_continuacao={safe_float(r['prob_continuation']):.4f} | p_reversao={safe_float(r['prob_reversal']):.4f}"
        )
    lines.append("")

    lines.append("## Ranking de Candidatos (Top 10)")
    for _, r in top10.iterrows():
        lines.append(
            "- "
            + f"#{int(r['rank'])} {r['cluster_id']} {r['side']} | operability={safe_float(r['operability_score']):.2f}"
            + f" | support={int(r['support'])} | precision={safe_float(r['precision']):.4f}"
            + f" | lift={safe_float(r['lift']):.3f} | robust(T/A/R)=({safe_float(r['robust_temporal']):.3f}/{safe_float(r['robust_asset']):.3f}/{safe_float(r['robust_regime']):.3f})"
        )
    lines.append("")

    lines.append("## Melhor Candidato")
    bc = report["best_candidate"]
    lines.append(f"- cluster: {bc['cluster_id']}")
    lines.append(f"- lado: {bc['side']}")
    lines.append(f"- regra de confirmacao: {bc['confirmation_rule']}")
    lines.append(f"- regra de entrada: {bc['entry_rule']}")
    lines.append(f"- regra de saida: {json.dumps(bc['exit_rule'], ensure_ascii=True)}")
    lines.append(f"- Operability Score: {safe_float(bc['operability_score']):.2f}")
    lines.append(f"- Confianca: {bc['recommendation_confidence']}")
    lines.append("")

    lines.append("## Decisao Final")
    if decision == "A":
        lines.append("- OPCAO A: Existe conjunto de regras completo e robusto para implementacao.")
    else:
        lines.append("- OPCAO B: Ainda nao ha evidencia suficiente; continuar evoluindo o laboratorio antes de criar outra estrategia.")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print("WROTE", OUT_CSV)
    print("WROTE", OUT_JSON)
    print("WROTE", OUT_MD)


if __name__ == "__main__":
    main()
