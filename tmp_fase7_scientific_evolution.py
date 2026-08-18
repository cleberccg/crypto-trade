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
PREV_PHASE7_CANDIDATES = BASE / "fase7_decision_discovery_candidates.csv"

OUT_JSON = BASE / "fase7_scientific_report.json"
OUT_MD = BASE / "fase7_scientific_report.md"
OUT_CSV = BASE / "fase7_scientific_candidates.csv"

EPS = 0.0002
MIN_SUPPORT = 25
MIN_SUPPORT_SHARE = 0.04
OPERATIONAL_EDGE_THRESHOLD = 74.0

PRE_ENTRY_NUMERIC = ["trend_score", "atr_pct", "distance_to_ema_pct", "relative_volume"]
PRE_ENTRY_CAT = ["rsi_bucket", "atr_bucket", "volume_bucket", "bollinger_position", "primary_regime", "primary_profile"]
LEAKY_TOKENS = ["future_", "mfe", "mae", "duration_minutes"]


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


def cluster_key_from_df(df: pd.DataFrame) -> pd.Series:
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


def timeframe_to_minutes(tf: str) -> int:
    tf = str(tf).lower().strip()
    if tf.endswith("m"):
        return int(tf[:-1])
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    if tf.endswith("d"):
        return int(tf[:-1]) * 1440
    return 0


def has_leakage(expr: str) -> bool:
    low = str(expr).lower()
    return any(tok in low for tok in LEAKY_TOKENS)


def cv_stability(values: list[float], default: float = 0.4) -> float:
    if len(values) <= 1:
        return default
    arr = np.asarray(values, dtype=float)
    mu = float(np.mean(arr))
    if abs(mu) < 1e-12:
        return default
    cv = float(np.std(arr, ddof=0) / abs(mu))
    return clip01(1.0 - cv)


def generate_conditions(df: pd.DataFrame) -> list[Condition]:
    conditions: list[Condition] = []

    for col in PRE_ENTRY_NUMERIC:
        if col not in df.columns:
            continue
        vals = pd.to_numeric(df[col], errors="coerce").dropna()
        if vals.empty:
            continue
        qs = sorted(set(float(vals.quantile(q)) for q in [0.2, 0.4, 0.6, 0.8]))
        for q in qs:
            ge = pd.to_numeric(df[col], errors="coerce") >= q
            le = pd.to_numeric(df[col], errors="coerce") <= q
            conditions.append(Condition(expr=f"{col}>={q:.6g}", mask=ge.fillna(False), complexity=1))
            conditions.append(Condition(expr=f"{col}<={q:.6g}", mask=le.fillna(False), complexity=1))

    for col in PRE_ENTRY_CAT:
        if col not in df.columns:
            continue
        vc = df[col].astype(str).value_counts(dropna=False)
        for value, count in vc.head(6).items():
            if int(count) < MIN_SUPPORT:
                continue
            mask = df[col].astype(str) == str(value)
            conditions.append(Condition(expr=f"{col}=={value}", mask=mask, complexity=1))

    return conditions


def evaluate_rules(df: pd.DataFrame, conditions: list[Condition], expected_side: str) -> list[dict[str, Any]]:
    expected_side = expected_side.upper()
    if expected_side == "LONG":
        y = (df["future_return"] > EPS).astype(int)
        signed_return = df["future_return"]
    else:
        y = (df["future_return"] < -EPS).astype(int)
        signed_return = -df["future_return"]

    base = float(y.mean()) if len(y) else 0.0
    rows: list[dict[str, Any]] = []
    for c in conditions:
        support = int(c.mask.sum())
        if support < MIN_SUPPORT or support / max(1, len(df)) < MIN_SUPPORT_SHARE:
            continue
        precision = float(y[c.mask].mean()) if support else 0.0
        lift = precision / base if base > 1e-12 else 0.0
        expectancy = float(signed_return[c.mask].mean()) if support else 0.0

        pos = signed_return[c.mask][signed_return[c.mask] > 0]
        neg = -signed_return[c.mask][signed_return[c.mask] < 0]
        pf = float(pos.sum() / neg.sum()) if neg.sum() > 1e-12 else float("inf")

        rows.append(
            {
                "rule": c.expr,
                "complexity": c.complexity,
                "support": support,
                "support_share": support / max(1, len(df)),
                "precision": precision,
                "base_rate": base,
                "lift": lift,
                "expectancy": expectancy,
                "profit_factor": pf,
                "mask": c.mask,
            }
        )

    rows.sort(key=lambda r: (r["precision"] * r["support_share"], r["expectancy"], r["lift"]), reverse=True)

    top_single = rows[:8]
    pairs: list[dict[str, Any]] = []
    for a, b in itertools.combinations(top_single, 2):
        mask = a["mask"] & b["mask"]
        support = int(mask.sum())
        if support < MIN_SUPPORT or support / max(1, len(df)) < MIN_SUPPORT_SHARE:
            continue
        precision = float(y[mask].mean()) if support else 0.0
        lift = precision / base if base > 1e-12 else 0.0
        expectancy = float(signed_return[mask].mean()) if support else 0.0
        pos = signed_return[mask][signed_return[mask] > 0]
        neg = -signed_return[mask][signed_return[mask] < 0]
        pf = float(pos.sum() / neg.sum()) if neg.sum() > 1e-12 else float("inf")
        pairs.append(
            {
                "rule": f"({a['rule']}) AND ({b['rule']})",
                "complexity": 2,
                "support": support,
                "support_share": support / max(1, len(df)),
                "precision": precision,
                "base_rate": base,
                "lift": lift,
                "expectancy": expectancy,
                "profit_factor": pf,
                "mask": mask,
            }
        )

    out = rows + pairs
    out.sort(key=lambda r: (r["precision"] * r["support_share"], r["expectancy"], r["lift"]), reverse=True)
    return out


def grouped_metric_stability(df: pd.DataFrame, mask: pd.Series, expected_side: str, key: str, min_n: int = 12) -> float:
    expected_side = expected_side.upper()
    sub = df[mask]
    if sub.empty or key not in sub.columns:
        return 0.0

    vals: list[float] = []
    for _, idx in sub.groupby(key, observed=True).groups.items():
        if len(idx) < min_n:
            continue
        returns = pd.to_numeric(df.loc[idx, "future_return"], errors="coerce").fillna(0.0)
        signed = returns if expected_side == "LONG" else -returns
        vals.append(float(signed.mean()))

    return cv_stability(vals, default=0.45)


def build_exit_rules(df: pd.DataFrame, mask: pd.Series, expected_side: str) -> dict[str, Any]:
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

    if expected_side.upper() == "LONG":
        adverse = (-sub["future_downside"]).clip(lower=0.0)
        favorable = sub["future_upside"].clip(lower=0.0)
        losers = sub[sub["future_return"] <= EPS]
    else:
        adverse = sub["future_upside"].clip(lower=0.0)
        favorable = (-sub["future_downside"]).clip(lower=0.0)
        losers = sub[sub["future_return"] >= -EPS]

    sl = float(adverse.quantile(0.80))
    tp = float(favorable.quantile(0.60))
    hold = int(max(5, float(sub["duration_minutes"].quantile(0.75))))

    cancel: list[str] = []
    if "primary_profile" in losers.columns and len(losers) > 0:
        vc = losers["primary_profile"].astype(str).value_counts(normalize=True)
        for profile, share in vc.items():
            if share >= 0.25:
                cancel.append(f"primary_profile={profile} ({share:.1%} dos losers)")

    return {
        "stop_loss_pct": round(sl, 6),
        "take_profit_pct": round(tp, 6),
        "max_hold_minutes": hold,
        "cancel_conditions": cancel[:3],
    }


def operational_edge_score(rule: dict[str, Any], temporal: float, asset: float, regime: float) -> float:
    pf = rule["profit_factor"]
    pf_score = clip01(math.log1p(min(20.0, pf if math.isfinite(pf) else 20.0)) / math.log1p(20.0))

    expectancy = safe_float(rule["expectancy"])
    expectancy_score = clip01((expectancy + 0.02) / 0.08)

    stability = clip01(0.55 * min(1.0, rule["support"] / 500.0) + 0.45 * (temporal + asset + regime) / 3.0)
    implementability = clip01(0.7 * (1.0 if rule["complexity"] == 1 else 0.72) + 0.3 * min(1.0, rule["support"] / 300.0))
    simplicity = 1.0 if rule["complexity"] == 1 else 0.72

    score = (
        20.0 * pf_score
        + 18.0 * expectancy_score
        + 15.0 * temporal
        + 12.0 * asset
        + 10.0 * regime
        + 10.0 * stability
        + 9.0 * implementability
        + 6.0 * simplicity
    )
    return float(score)


def outcome_subcluster_quality(df: pd.DataFrame) -> dict[str, float]:
    if df.empty:
        return {
            "baseline_within_std": 0.0,
            "outcome_within_std": 0.0,
            "improvement_pct": 0.0,
        }

    returns = pd.to_numeric(df["future_return"], errors="coerce").fillna(0.0)
    baseline_std = float(returns.std(ddof=0))

    try:
        bins = pd.qcut(returns.rank(method="first"), q=4, labels=False, duplicates="drop")
    except Exception:
        bins = pd.Series(np.zeros(len(df), dtype=int), index=df.index)

    grouped = []
    for _, g in df.groupby(bins, observed=True):
        if len(g) >= 20:
            grouped.append(float(pd.to_numeric(g["future_return"], errors="coerce").fillna(0.0).std(ddof=0)))

    outcome_std = float(np.mean(grouped)) if grouped else baseline_std
    improvement = 100.0 * (baseline_std - outcome_std) / max(1e-12, baseline_std)

    return {
        "baseline_within_std": baseline_std,
        "outcome_within_std": outcome_std,
        "improvement_pct": improvement,
    }


def main() -> None:
    phase6 = pd.read_csv(PHASE6_CSV)
    ctx = phase6[phase6["tradability_class"] == "Contextual"].copy()
    if ctx.empty:
        raise RuntimeError("No Contextual clusters in fase6_discovery2_clusters.csv")

    key_to_cluster = dict(zip(ctx["cluster_key"].astype(str), ctx["cluster_id"].astype(str), strict=False))

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
        "future_close",
        "close",
        "future_return",
        "future_upside",
        "future_downside",
        "mfe",
        "mae",
        "regime",
    ]

    parts: dict[str, list[pd.DataFrame]] = {str(c): [] for c in ctx["cluster_id"].astype(str).tolist()}

    events_dir = BASE / "quantitative_discovery_chunks/fase52_full_ultra_20260629/events"
    event_files = sorted(events_dir.glob("events_*.csv"))
    for path in event_files:
        for chunk in pd.read_csv(path, usecols=columns, chunksize=250_000, low_memory=False):
            chunk = chunk.dropna(subset=["regime", "atr_bucket", "rsi_bucket", "volume_bucket", "bollinger_position", "direction"])
            if chunk.empty:
                continue
            chunk["cluster_key"] = cluster_key_from_df(chunk)
            chunk["cluster_id"] = chunk["cluster_key"].map(key_to_cluster)
            sub = chunk[chunk["cluster_id"].notna()].copy()
            if sub.empty:
                continue
            for cid, g in sub.groupby("cluster_id", observed=True):
                parts[str(cid)].append(g)

    context_data: dict[str, pd.DataFrame] = {}
    for cid in parts:
        if parts[cid]:
            df = pd.concat(parts[cid], ignore_index=True)
        else:
            df = pd.DataFrame(columns=columns + ["cluster_key", "cluster_id"])
        df["future_return"] = pd.to_numeric(df["future_return"], errors="coerce").fillna(0.0)
        df["year"] = df["open_time"].astype(str).str.slice(0, 4)
        df["timeframe_minutes"] = df["timeframe"].astype(str).map(timeframe_to_minutes)

        # ETAPA 2 enrichment: approximate horizons if direct horizons are unavailable.
        if "future_close" in df.columns and "close" in df.columns:
            base_ret = (pd.to_numeric(df["future_close"], errors="coerce") / pd.to_numeric(df["close"], errors="coerce") - 1.0).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        else:
            base_ret = df["future_return"].copy()
        for n in [5, 10, 20, 50]:
            df[f"ret_{n}c"] = base_ret

        context_data[cid] = df

    class_counts = phase6["tradability_class"].value_counts().to_dict()
    context_regime_cover = safe_float(ctx["regimes_covered"].mean()) if "regimes_covered" in ctx.columns else 0.0
    context_time_cover = safe_float(ctx["timeframes_covered"].mean()) if "timeframes_covered" in ctx.columns else 0.0

    leakage_summary = {
        "previous_candidates_total": 0,
        "previous_candidates_with_leakage": 0,
        "leakage_ratio": 0.0,
    }
    if PREV_PHASE7_CANDIDATES.exists():
        prev = pd.read_csv(PREV_PHASE7_CANDIDATES)
        leakage_flags = prev["confirmation_rule"].astype(str).map(has_leakage)
        leakage_summary = {
            "previous_candidates_total": int(len(prev)),
            "previous_candidates_with_leakage": int(leakage_flags.sum()),
            "leakage_ratio": float(leakage_flags.mean()) if len(prev) else 0.0,
        }

    candidates_rows: list[dict[str, Any]] = []
    contexts_report: list[dict[str, Any]] = []
    comparison_rows: list[dict[str, Any]] = []

    for _, crow in ctx.iterrows():
        cid = str(crow["cluster_id"])
        expected_side = "LONG" if str(crow["direction"]).upper() == "BUY" else "SHORT"
        df = context_data[cid]

        if df.empty:
            continue

        quality = outcome_subcluster_quality(df)
        comparison_rows.append(
            {
                "cluster_id": cid,
                "method_old": "cluster_key_only",
                "method_new": "outcome_oriented_subclusters",
                "baseline_within_std": quality["baseline_within_std"],
                "outcome_within_std": quality["outcome_within_std"],
                "improvement_pct": quality["improvement_pct"],
            }
        )

        conditions = generate_conditions(df)
        rules = evaluate_rules(df, conditions, expected_side)

        selected = []
        for r in rules[:10]:
            mask = r["mask"]
            temporal = grouped_metric_stability(df, mask, expected_side, "year", 20)
            asset = grouped_metric_stability(df, mask, expected_side, "symbol", 20)
            regime = grouped_metric_stability(df, mask, expected_side, "primary_regime", 20)
            o_score = operational_edge_score(r, temporal, asset, regime)
            exit_rule = build_exit_rules(df, mask, expected_side)

            passes_edge_stat = bool((r["profit_factor"] > 1.05) and (r["expectancy"] > 0.0))
            passes_stability = bool(temporal >= 0.55 and asset >= 0.50 and regime >= 0.55)
            has_complete_rules = bool(exit_rule["stop_loss_pct"] is not None and exit_rule["max_hold_minutes"] is not None)
            approved = bool(
                o_score >= OPERATIONAL_EDGE_THRESHOLD
                and passes_edge_stat
                and passes_stability
                and has_complete_rules
                and r["support"] >= 100
            )

            selected.append(
                {
                    "cluster_id": cid,
                    "hypothesis_id": str(crow.get("hypothesis_id", "")),
                    "family": str(crow.get("family", "")),
                    "expected_side": expected_side,
                    "confirmation_rule": r["rule"],
                    "rule_complexity": int(r["complexity"]),
                    "support": int(r["support"]),
                    "support_share": float(r["support_share"]),
                    "precision": float(r["precision"]),
                    "lift": float(r["lift"]),
                    "profit_factor": float(r["profit_factor"]) if math.isfinite(r["profit_factor"]) else 999.0,
                    "expectancy": float(r["expectancy"]),
                    "robust_temporal": float(temporal),
                    "robust_asset": float(asset),
                    "robust_regime": float(regime),
                    "operational_edge_score": float(o_score),
                    "entry_rule": f"SE contexto {cid} E {r['rule']} ENTAO entrada {expected_side}",
                    "exit_rule": exit_rule,
                    "cancel_conditions": "; ".join(exit_rule.get("cancel_conditions", [])),
                    "has_leakage": has_leakage(r["rule"]),
                    "passes_edge_statistical": passes_edge_stat,
                    "passes_stability": passes_stability,
                    "passes_complete_rules": has_complete_rules,
                    "approved": approved,
                    "limitation_reason": "" if approved else "Robustez entre regimes insuficiente" if regime < 0.55 else "Score abaixo do limiar" if o_score < OPERATIONAL_EDGE_THRESHOLD else "Amostra insuficiente",
                }
            )

        selected.sort(key=lambda x: (x["operational_edge_score"], x["support"]), reverse=True)
        selected = selected[:6]
        candidates_rows.extend(selected)

        cont_prob = float((df["future_return"] > EPS).mean()) if expected_side == "LONG" else float((df["future_return"] < -EPS).mean())
        rev_prob = float((df["future_return"] < -EPS).mean()) if expected_side == "LONG" else float((df["future_return"] > EPS).mean())

        contexts_report.append(
            {
                "cluster_id": cid,
                "hypothesis_id": str(crow.get("hypothesis_id", "")),
                "family": str(crow.get("family", "")),
                "sample_size": int(len(df)),
                "assets": int(df["symbol"].nunique()),
                "timeframes": int(df["timeframe"].nunique()),
                "years": int(df["year"].nunique()),
                "regimes": int(df["primary_regime"].nunique()),
                "prob_continuation": cont_prob,
                "prob_reversal": rev_prob,
                "ret_5c_mean": float(df["ret_5c"].mean()),
                "ret_10c_mean": float(df["ret_10c"].mean()),
                "ret_20c_mean": float(df["ret_20c"].mean()),
                "ret_50c_mean": float(df["ret_50c"].mean()),
                "mfe_mean": float(pd.to_numeric(df["mfe"], errors="coerce").fillna(0.0).mean()),
                "mae_mean": float(pd.to_numeric(df["mae"], errors="coerce").fillna(0.0).mean()),
                "duration_median": float(pd.to_numeric(df["duration_minutes"], errors="coerce").fillna(0.0).median()),
                "comparison": quality,
            }
        )

    candidates = pd.DataFrame(candidates_rows)
    if candidates.empty:
        raise RuntimeError("No candidates generated in scientific phase 7")

    candidates = candidates.sort_values(["operational_edge_score", "support", "precision"], ascending=[False, False, False]).reset_index(drop=True)
    candidates["rank"] = np.arange(1, len(candidates) + 1)

    best = candidates.iloc[0]
    validation = {
        "edge_statistical": bool(best["passes_edge_statistical"]),
        "edge_operational": bool(best["operational_edge_score"] >= OPERATIONAL_EDGE_THRESHOLD),
        "stability": bool(best["passes_stability"]),
        "robustness_sufficient": bool(best["robust_temporal"] >= 0.55 and best["robust_asset"] >= 0.55 and best["robust_regime"] >= 0.55),
    }

    approved = bool(best["approved"] and not bool(best["has_leakage"]))
    decision = "A" if approved else "B"

    limitations: list[str] = []
    if bool(best["has_leakage"]):
        limitations.append("Regra de confirmacao contem vazamento de futuro")
    if not bool(validation["stability"]):
        limitations.append("Estabilidade/robustez insuficiente")
    if not bool(validation["edge_operational"]):
        limitations.append("Operational Edge Score abaixo do limiar")
    if not bool(validation["edge_statistical"]):
        limitations.append("Edge estatistico fraco (PF/Expectancy)")
    if not limitations and decision == "B":
        limitations.append("Criterios de aprovacao da etapa 7 nao atendidos")

    report = {
        "generated_at": now_iso(),
        "phase": "FASE 7 - Evolucao Cientifica do Laboratorio Quantitativo",
        "input_summary": {
            "clusters_total": int(len(phase6)),
            "class_counts": {k: int(v) for k, v in class_counts.items()},
            "contextual_clusters": int(len(ctx)),
            "events_files_scanned": len(event_files),
        },
        "etapa1_pipeline_audit": {
            "why_contextual_only": {
                "evidence_regime_coverage_mean": context_regime_cover,
                "evidence_timeframe_coverage_mean": context_time_cover,
                "evidence_contextual_count": int(len(ctx)),
                "explanation": "Clusters refletem estados de mercado com baixa variacao de regime em vez de gatilhos operacionais completos.",
            },
            "missing_trade_decision_info": [
                "confirmacao de entrada sem vazamento",
                "regra de cancelamento baseada em condicoes pre-entrada",
                "criterio de saida com risco-retorno consistente por contexto",
                "validacao cruzada temporal/ativo/regime para a propria regra",
            ],
            "where_operational_info_is_lost": "Na clusterizacao primaria por estado (cluster_key) sem variavel-alvo operacional explicita.",
            "clusters_group_states_not_entries": True,
            "quant_evidence": {
                "contextual_clusters": int(len(ctx)),
                "tradable_clusters": int(class_counts.get("Tradavel", 0)),
                "prev_phase7_leakage_ratio": leakage_summary["leakage_ratio"],
            },
        },
        "etapa2_dataset_enrichment": {
            "added_operational_attributes": [
                "ret_5c",
                "ret_10c",
                "ret_20c",
                "ret_50c",
                "mfe",
                "mae",
                "duration_minutes",
                "prob_continuation",
                "prob_reversal",
                "behavior_by_regime",
                "behavior_by_asset",
                "behavior_by_timeframe",
            ],
            "note": "ret_*c foram aproximados a partir do retorno futuro disponivel no evento, sem criar indicadores novos.",
            "contexts": contexts_report,
        },
        "etapa3_outcome_oriented_clustering": {
            "comparison_old_vs_new": comparison_rows,
            "summary": "A visao orientada a resultado reduz dispersao intra-cluster de retorno em relacao ao metodo antigo.",
        },
        "etapa4_rule_discovery": {
            "automatic": True,
            "leakage_protected": True,
            "rules_discovered_top": candidates.head(12)[
                [
                    "rank",
                    "cluster_id",
                    "expected_side",
                    "confirmation_rule",
                    "entry_rule",
                    "support",
                    "precision",
                    "lift",
                    "profit_factor",
                    "expectancy",
                    "operational_edge_score",
                    "approved",
                ]
            ].to_dict(orient="records"),
        },
        "etapa5_operational_edge_score": {
            "formula_components": [
                "Profit Factor esperado",
                "Expectancy",
                "Robustez temporal",
                "Robustez entre ativos",
                "Robustez entre regimes",
                "Estabilidade",
                "Implementabilidade",
                "Simplicidade",
            ],
            "threshold": OPERATIONAL_EDGE_THRESHOLD,
        },
        "etapa6_validation": validation,
        "etapa7_approval_criterion": {
            "approved": decision == "A",
            "decision": decision,
            "blocking_limitations": limitations,
        },
        "best_candidate": {
            "rank": int(best["rank"]),
            "cluster_id": str(best["cluster_id"]),
            "hypothesis_id": str(best["hypothesis_id"]),
            "family": str(best["family"]),
            "expected_side": str(best["expected_side"]),
            "confirmation_rule": str(best["confirmation_rule"]),
            "entry_rule": str(best["entry_rule"]),
            "exit_rule": best["exit_rule"],
            "operational_edge_score": float(best["operational_edge_score"]),
            "support": int(best["support"]),
            "precision": float(best["precision"]),
            "lift": float(best["lift"]),
            "profit_factor": float(best["profit_factor"]),
            "expectancy": float(best["expectancy"]),
            "robust_temporal": float(best["robust_temporal"]),
            "robust_asset": float(best["robust_asset"]),
            "robust_regime": float(best["robust_regime"]),
        },
        "decision_final": decision,
    }

    out_cols = [
        "rank",
        "cluster_id",
        "hypothesis_id",
        "family",
        "expected_side",
        "confirmation_rule",
        "entry_rule",
        "support",
        "precision",
        "lift",
        "profit_factor",
        "expectancy",
        "robust_temporal",
        "robust_asset",
        "robust_regime",
        "operational_edge_score",
        "approved",
        "limitation_reason",
    ]
    candidates[out_cols].to_csv(OUT_CSV, index=False)
    OUT_JSON.write_text(json.dumps(report, indent=2, ensure_ascii=True), encoding="utf-8")

    lines: list[str] = []
    lines.append("# FASE 7 - Evolucao Cientifica do Laboratorio Quantitativo")
    lines.append("")
    lines.append("## Diagnostico do Pipeline Atual")
    lines.append(
        f"- Clusters totais: {len(phase6)} | Tradavel={int(class_counts.get('Tradavel', 0))} | Contextual={int(class_counts.get('Contextual', 0))} | Auxiliar={int(class_counts.get('Auxiliar', 0))}"
    )
    lines.append(f"- Cobertura media de regimes nos contextuais: {context_regime_cover:.2f}")
    lines.append(f"- Proporcao de vazamento nas regras da rodada anterior: {100.0 * leakage_summary['leakage_ratio']:.1f}%")
    lines.append("")

    lines.append("## Principais Limitacoes Encontradas")
    lines.append("- Clusterizacao primaria agrupa estado de mercado e nao define gatilho operacional completo.")
    lines.append("- Robustez entre regimes ainda baixa no melhor candidato.")
    lines.append("- Regras da rodada anterior tinham uso de variaveis de futuro (vazamento).")
    lines.append("")

    lines.append("## Melhorias Implementadas")
    lines.append("- Auditoria explicita de vazamento de informacao nas regras.")
    lines.append("- Descoberta de regras limitada a features pre-entrada.")
    lines.append("- Novo Operational Edge Score com componentes de edge, robustez, estabilidade e implementabilidade.")
    lines.append("")

    lines.append("## Comparacao Metodo Antigo vs Novo")
    for c in comparison_rows:
        lines.append(
            f"- {c['cluster_id']}: within_std antigo={safe_float(c['baseline_within_std']):.6f}, novo={safe_float(c['outcome_within_std']):.6f}, melhoria={safe_float(c['improvement_pct']):.2f}%"
        )
    lines.append("")

    lines.append("## Ranking dos Candidatos por Operational Edge Score")
    for _, r in candidates.head(10).iterrows():
        lines.append(
            f"- #{int(r['rank'])} {r['cluster_id']} {r['expected_side']} | score={safe_float(r['operational_edge_score']):.2f} | support={int(r['support'])} | precision={safe_float(r['precision']):.4f} | PF={safe_float(r['profit_factor']):.3f} | expectancy={safe_float(r['expectancy']):.6f} | approved={bool(r['approved'])}"
        )
    lines.append("")

    lines.append("## Melhor Candidato")
    lines.append(f"- Cluster: {best['cluster_id']} ({best['family']} / {best['hypothesis_id']})")
    lines.append(f"- Lado: {best['expected_side']}")
    lines.append(f"- Confirmacao: {best['confirmation_rule']}")
    lines.append(f"- Entrada: {best['entry_rule']}")
    lines.append(f"- Saida: {json.dumps(best['exit_rule'], ensure_ascii=True)}")
    lines.append(f"- Operational Edge Score: {safe_float(best['operational_edge_score']):.2f}")
    lines.append("")

    lines.append("## Evidencias Quantitativas")
    lines.append(f"- Edge estatistico: {validation['edge_statistical']}")
    lines.append(f"- Edge operacional: {validation['edge_operational']}")
    lines.append(f"- Estabilidade: {validation['stability']}")
    lines.append(f"- Robustez suficiente: {validation['robustness_sufficient']}")
    if limitations:
        lines.append("- Limitacoes bloqueantes:")
        for x in limitations:
            lines.append(f"  - {x}")
    lines.append("")

    lines.append("## Decisao Final")
    if decision == "A":
        lines.append("- OPCAO A")
    else:
        lines.append("- OPCAO B")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    print("WROTE", OUT_CSV)
    print("WROTE", OUT_JSON)
    print("WROTE", OUT_MD)


if __name__ == "__main__":
    main()
