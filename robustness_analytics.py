from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
MONITOR_DIR = BASE_DIR / "optimization" / "monitor"


@dataclass
class Campaign:
    file_path: Path
    generated_at: datetime
    payload: dict[str, Any]


def _parse_dt(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
        if not math.isfinite(out):
            return None
        return out
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 6) if values else None


def _std(values: list[float]) -> float | None:
    if len(values) < 2:
        return 0.0 if values else None
    return round(pstdev(values), 6)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _family_norm(raw: Any) -> str:
    val = str(raw or "unknown").strip().lower()
    mapping = {
        "classic_catalog": "classic_catalog",
        "breakout": "breakout",
        "momentum": "momentum",
        "reversao": "reversao",
        "reversão": "reversao",
        "tendencia": "tendencia",
        "tendência": "tendencia",
        "trend": "tendencia",
        "volatilidade": "volatilidade",
        "hibridas": "hibridas",
        "hibrida": "hibridas",
        "hybrid": "hibridas",
    }
    return mapping.get(val, val if val else "unknown")


def _load_phase13_campaigns() -> list[Campaign]:
    campaigns: list[Campaign] = []
    for file_path in sorted(RESULTS_DIR.glob("*.json")):
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if str(payload.get("phase")) != "13":
            continue
        if not isinstance(payload.get("backlog"), list):
            continue

        generated = _parse_dt(payload.get("generated_at"))
        if generated == datetime.min.replace(tzinfo=timezone.utc):
            generated = datetime.fromtimestamp(file_path.stat().st_mtime, tz=timezone.utc)
        campaigns.append(Campaign(file_path=file_path, generated_at=generated, payload=payload))

    campaigns.sort(key=lambda c: c.generated_at)
    return campaigns


def _extract_metrics(row: dict[str, Any]) -> dict[str, float | int | None]:
    sources: list[dict[str, Any]] = []
    for key in ("backtest", "backtest_base"):
        val = row.get(key)
        if isinstance(val, dict):
            sources.append(val)

    probe = row.get("optimizer_probe")
    if isinstance(probe, dict) and isinstance(probe.get("best_metrics_raw"), dict):
        sources.append(probe.get("best_metrics_raw") or {})

    early_input = row.get("early_stop_input")
    if isinstance(early_input, dict) and isinstance(early_input.get("metrics"), dict):
        sources.append(early_input.get("metrics") or {})

    merged: dict[str, Any] = {}
    for src in sources:
        merged.update(src)

    return {
        "profit_factor": _safe_float(merged.get("profit_factor")),
        "sharpe": _safe_float(merged.get("sharpe")) or _safe_float(merged.get("sharpe_ratio")),
        "expectancy": _safe_float(merged.get("expectancy")),
        "drawdown": _safe_float(merged.get("drawdown_pct")) or _safe_float(merged.get("max_drawdown_pct")),
    }


def _is_rejection_state(state: str) -> bool:
    return state in {
        "REJECTED_BY_PERFORMANCE",
        "REJECTED",
        "REJECTED_AFTER_PAPER_EXPERIMENT",
        "REJECTED_BY_INFRASTRUCTURE",
    }


def _simple_slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0

    xs = list(range(n))
    x_mean = mean(xs)
    y_mean = mean(values)

    num = 0.0
    den = 0.0
    for i, y in zip(xs, values):
        dx = i - x_mean
        num += dx * (y - y_mean)
        den += dx * dx
    if den <= 0:
        return 0.0
    return num / den


def _ci95_lower(values: list[float]) -> float | None:
    n = len(values)
    if n == 0:
        return None
    mu = mean(values)
    if n == 1:
        return mu
    sigma = pstdev(values)
    se = sigma / math.sqrt(n)
    # Normal approximation for objective, data-only confidence bound.
    return mu - 1.96 * se


def _trend_label(slope: float, eps: float = 0.003) -> str:
    if slope > eps:
        return "Improving"
    if slope < -eps:
        return "Declining"
    return "Stable"


def _trend_to_baseline(mean_delta: float | None, slope: float) -> str:
    if mean_delta is None:
        return "stable"
    if abs(slope) < 1e-9:
        return "stable"

    if mean_delta < 0:
        return "aproximando" if slope > 0 else "afastando"
    if mean_delta > 0:
        return "aproximando" if slope < 0 else "afastando"

    return "stable"


def _calculate_robustness_score(
    *,
    consistency_score: float,
    mean_profit_factor: float | None,
    std_profit_factor: float | None,
    mean_sharpe: float | None,
    std_sharpe: float | None,
    mean_expectancy: float | None,
    mean_drawdown: float | None,
    max_drawdown: float | None,
    number_of_campaigns: int,
    paper_candidate_streak: int,
    baseline_win_rate: float,
    early_stop_freq: float,
    inconclusive_freq: float,
) -> float:
    consistency_component = _clamp01(consistency_score / 100.0)

    pf_mean = mean_profit_factor if isinstance(mean_profit_factor, float) else 0.0
    pf_std = std_profit_factor if isinstance(std_profit_factor, float) else 0.0
    pf_quality = _clamp01((pf_mean - 1.0) / 1.0)
    pf_stability = 1.0 - _clamp01(pf_std / max(abs(pf_mean), 0.25))

    sharpe_mean = mean_sharpe if isinstance(mean_sharpe, float) else 0.0
    sharpe_std = std_sharpe if isinstance(std_sharpe, float) else 0.0
    sharpe_quality = _clamp01((sharpe_mean + 0.5) / 1.0)
    sharpe_stability = 1.0 - _clamp01(sharpe_std / max(abs(sharpe_mean), 0.2))

    expectancy_mean = mean_expectancy if isinstance(mean_expectancy, float) else 0.0
    expectancy_quality = _clamp01((expectancy_mean + 1.0) / 4.0)

    dd_mean = mean_drawdown if isinstance(mean_drawdown, float) else 0.30
    dd_max = max_drawdown if isinstance(max_drawdown, float) else 0.35
    drawdown_mean_quality = 1.0 - _clamp01(dd_mean / 0.20)
    drawdown_max_quality = 1.0 - _clamp01(dd_max / 0.25)

    campaigns_quality = _clamp01(number_of_campaigns / 20.0)
    streak_quality = _clamp01(paper_candidate_streak / 6.0)
    baseline_quality = _clamp01(baseline_win_rate)
    early_stop_quality = 1.0 - _clamp01(early_stop_freq)
    inconclusive_quality = 1.0 - _clamp01(inconclusive_freq)

    score = 100.0 * (
        0.16 * consistency_component
        + 0.09 * pf_quality
        + 0.09 * pf_stability
        + 0.08 * sharpe_quality
        + 0.07 * sharpe_stability
        + 0.07 * expectancy_quality
        + 0.08 * drawdown_mean_quality
        + 0.07 * drawdown_max_quality
        + 0.07 * campaigns_quality
        + 0.07 * streak_quality
        + 0.08 * baseline_quality
        + 0.04 * early_stop_quality
        + 0.03 * inconclusive_quality
    )
    return round(score, 4)


def _build_strategy_robustness(campaigns: list[Campaign]) -> dict[str, Any]:
    total_campaigns = len(campaigns)
    strat: dict[str, dict[str, Any]] = {}

    for idx, campaign in enumerate(campaigns):
        generated_at = campaign.generated_at.isoformat()
        campaign_id = str(campaign.payload.get("run_id", campaign.file_path.stem))

        for row in campaign.payload.get("backlog", []):
            if not isinstance(row, dict):
                continue
            name = str(row.get("candidate_name", "")).strip()
            if not name:
                continue

            family = _family_norm(row.get("family"))
            state = str(row.get("state", "unknown"))
            metrics = _extract_metrics(row)
            queue_score = _safe_float(row.get("queue_score"))
            if queue_score is None:
                queue_score = _safe_float(row.get("score"))

            entry = strat.setdefault(
                name,
                {
                    "strategy": name,
                    "family": family,
                    "first_seen": generated_at,
                    "last_seen": generated_at,
                    "campaign_ids": set(),
                    "states": [],
                    "scores": [],
                    "profit_factor": [],
                    "sharpe": [],
                    "expectancy": [],
                    "drawdown": [],
                    "drawdown_max": 0.0,
                    "early_stop_count": 0,
                    "inconclusive_count": 0,
                    "rejections_count": 0,
                    "baseline_superou_count": 0,
                    "baseline_empatou_count": 0,
                    "baseline_abaixo_count": 0,
                    "baseline_delta_return_pct": [],
                    "series_robust_proxy": [],
                    "campaign_index": [],
                    "paper_candidate_streak": 0,
                    "paper_candidate_streak_max": 0,
                },
            )

            entry["campaign_ids"].add(campaign_id)
            entry["last_seen"] = generated_at
            if generated_at < entry["first_seen"]:
                entry["first_seen"] = generated_at

            entry["states"].append(state)
            if isinstance(queue_score, float):
                entry["scores"].append(queue_score)

            pf = metrics.get("profit_factor")
            sh = metrics.get("sharpe")
            ex = metrics.get("expectancy")
            dd = metrics.get("drawdown")

            if isinstance(pf, float):
                entry["profit_factor"].append(pf)
            if isinstance(sh, float):
                entry["sharpe"].append(sh)
            if isinstance(ex, float):
                entry["expectancy"].append(ex)
            if isinstance(dd, float):
                entry["drawdown"].append(dd)
                entry["drawdown_max"] = max(entry["drawdown_max"], dd)

            early = row.get("early_stop")
            if isinstance(early, dict) and bool(early.get("triggered")):
                entry["early_stop_count"] += 1

            if state == "INCONCLUSIVE_LOW_SAMPLE":
                entry["inconclusive_count"] += 1
            if _is_rejection_state(state):
                entry["rejections_count"] += 1

            baseline = row.get("baseline_comparison")
            if isinstance(baseline, dict):
                decision = str(baseline.get("decision", "")).upper()
                if decision == "SUPEROU":
                    entry["baseline_superou_count"] += 1
                elif decision == "EMPATOU":
                    entry["baseline_empatou_count"] += 1
                elif decision == "ABAIXO":
                    entry["baseline_abaixo_count"] += 1

                delta_ret = _safe_float(baseline.get("delta_return_pct"))
                if isinstance(delta_ret, float):
                    entry["baseline_delta_return_pct"].append(delta_ret)

            if state == "PAPER_CANDIDATE":
                entry["paper_candidate_streak"] += 1
                entry["paper_candidate_streak_max"] = max(
                    entry["paper_candidate_streak_max"], entry["paper_candidate_streak"]
                )
            else:
                entry["paper_candidate_streak"] = 0

            proxy = (
                (pf if isinstance(pf, float) else 0.0)
                + (sh if isinstance(sh, float) else 0.0)
                + ((ex if isinstance(ex, float) else 0.0) * 0.1)
                - ((dd if isinstance(dd, float) else 0.0) * 2.0)
            )
            entry["series_robust_proxy"].append(proxy)
            entry["campaign_index"].append(float(idx))

    rows: list[dict[str, Any]] = []
    for entry in strat.values():
        number_of_campaigns = len(entry["campaign_ids"])
        consistency_score = 0.0

        if total_campaigns > 0:
            presence_rate = number_of_campaigns / total_campaigns
            rejection_rate = entry["rejections_count"] / max(1, number_of_campaigns)

            pf_mean = _avg(entry["profit_factor"])
            pf_std = _std(entry["profit_factor"]) or 0.0
            pf_stability = 1.0
            if isinstance(pf_mean, float) and abs(pf_mean) > 1e-9:
                pf_stability = max(0.0, 1.0 - min(1.0, abs(pf_std / pf_mean)))

            sharpe_mean = _avg(entry["sharpe"])
            sharpe_std = _std(entry["sharpe"]) or 0.0
            sharpe_stability = 1.0
            if isinstance(sharpe_mean, float) and abs(sharpe_mean) > 1e-9:
                sharpe_stability = max(0.0, 1.0 - min(1.0, abs(sharpe_std / sharpe_mean)))

            baseline_total = (
                entry["baseline_superou_count"]
                + entry["baseline_empatou_count"]
                + entry["baseline_abaixo_count"]
            )
            baseline_win_rate = entry["baseline_superou_count"] / max(1, baseline_total)

            consistency_score = 100.0 * (
                0.30 * presence_rate
                + 0.25 * (1.0 - rejection_rate)
                + 0.20 * pf_stability
                + 0.15 * sharpe_stability
                + 0.10 * baseline_win_rate
            )

        mean_pf = _avg(entry["profit_factor"])
        pf_std = _std(entry["profit_factor"])
        mean_sharpe = _avg(entry["sharpe"])
        sharpe_std = _std(entry["sharpe"])
        mean_expectancy = _avg(entry["expectancy"])
        mean_drawdown = _avg(entry["drawdown"])
        max_drawdown = entry["drawdown_max"] if entry["drawdown"] else None

        baseline_total = (
            entry["baseline_superou_count"] + entry["baseline_empatou_count"] + entry["baseline_abaixo_count"]
        )
        baseline_win_rate = entry["baseline_superou_count"] / max(1, baseline_total)
        early_stop_freq = entry["early_stop_count"] / max(1, number_of_campaigns)
        inconclusive_freq = entry["inconclusive_count"] / max(1, number_of_campaigns)

        trend_slope = _simple_slope(entry["series_robust_proxy"][-8:])
        trend = _trend_label(trend_slope)

        baseline_delta = entry["baseline_delta_return_pct"]
        baseline_delta_mean = _avg(baseline_delta)
        baseline_delta_slope = _simple_slope(baseline_delta[-8:]) if baseline_delta else 0.0
        baseline_ci_low = _ci95_lower(baseline_delta)

        robustness_score = _calculate_robustness_score(
            consistency_score=consistency_score,
            mean_profit_factor=mean_pf,
            std_profit_factor=pf_std,
            mean_sharpe=mean_sharpe,
            std_sharpe=sharpe_std,
            mean_expectancy=mean_expectancy,
            mean_drawdown=mean_drawdown,
            max_drawdown=max_drawdown,
            number_of_campaigns=number_of_campaigns,
            paper_candidate_streak=entry["paper_candidate_streak_max"],
            baseline_win_rate=baseline_win_rate,
            early_stop_freq=early_stop_freq,
            inconclusive_freq=inconclusive_freq,
        )

        rows.append(
            {
                "strategy": entry["strategy"],
                "family": entry["family"],
                "number_of_campaigns": number_of_campaigns,
                "first_seen": entry["first_seen"],
                "last_seen": entry["last_seen"],
                "current_state": entry["states"][-1] if entry["states"] else "unknown",
                "robustness_score": robustness_score,
                "consistency_score": round(consistency_score, 4),
                "mean_profit_factor": mean_pf,
                "std_profit_factor": pf_std,
                "mean_sharpe": mean_sharpe,
                "std_sharpe": sharpe_std,
                "mean_expectancy": mean_expectancy,
                "mean_drawdown": mean_drawdown,
                "max_drawdown": max_drawdown,
                "paper_candidate_streak": int(entry["paper_candidate_streak_max"]),
                "baseline_superou_count": int(entry["baseline_superou_count"]),
                "baseline_empatou_count": int(entry["baseline_empatou_count"]),
                "baseline_abaixo_count": int(entry["baseline_abaixo_count"]),
                "baseline_win_rate": round(baseline_win_rate, 6),
                "baseline_delta_return_mean": baseline_delta_mean,
                "baseline_delta_return_ci95_low": round(baseline_ci_low, 8) if baseline_ci_low is not None else None,
                "baseline_delta_trend_slope": round(baseline_delta_slope, 8),
                "baseline_trend": _trend_to_baseline(baseline_delta_mean, baseline_delta_slope),
                "early_stop_frequency": round(early_stop_freq, 6),
                "inconclusive_frequency": round(inconclusive_freq, 6),
                "trend_slope": round(trend_slope, 8),
                "trend": trend,
            }
        )

    rows.sort(
        key=lambda x: (
            -float(x.get("robustness_score") or 0.0),
            -float(x.get("consistency_score") or 0.0),
            -float(x.get("mean_profit_factor") or 0.0),
            -float(x.get("mean_sharpe") or 0.0),
        )
    )
    for idx, row in enumerate(rows, start=1):
        row["robustness_rank"] = idx

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "campaigns_considered": total_campaigns,
        "rows": rows,
    }


def _classify_promotion_parecer(row: dict[str, Any]) -> str:
    robustness = float(row.get("robustness_score") or 0.0)
    baseline_ci_low = _safe_float(row.get("baseline_delta_return_ci95_low"))
    trend = str(row.get("trend") or "Stable")

    if baseline_ci_low is not None and baseline_ci_low > 0 and robustness >= 72.0 and trend != "Declining":
        return "Elegivel para futura PAPER_APPROVED"
    if robustness >= 65.0 and trend == "Improving":
        return "Recomendada para observacao prolongada"
    if robustness >= 55.0:
        return "Continua apenas em observacao"
    return "Nao apresenta robustez suficiente"


def _build_candidates_classification(strategy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [r for r in strategy_rows if str(r.get("current_state")) == "PAPER_CANDIDATE"]

    for row in rows:
        row["scientific_promotion_parecer"] = _classify_promotion_parecer(row)

    rows.sort(
        key=lambda x: (
            -float(x.get("robustness_score") or 0.0),
            -float(x.get("consistency_score") or 0.0),
            -float(x.get("mean_profit_factor") or 0.0),
        )
    )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "paper_candidate_count": len(rows),
        "rows": rows,
    }


def _build_edge_detection(strategy_rows: list[dict[str, Any]], family_rows: list[dict[str, Any]]) -> dict[str, Any]:
    strategy_evidence: list[dict[str, Any]] = []
    for row in strategy_rows:
        ci_low = _safe_float(row.get("baseline_delta_return_ci95_low"))
        if ci_low is None:
            continue

        if (
            ci_low > 0
            and int(row.get("number_of_campaigns") or 0) >= 8
            and float(row.get("mean_profit_factor") or 0.0) > 1.0
            and float(row.get("mean_sharpe") or 0.0) > 0.0
        ):
            strategy_evidence.append(
                {
                    "strategy": row.get("strategy"),
                    "family": row.get("family"),
                    "campaigns": row.get("number_of_campaigns"),
                    "robustness_score": row.get("robustness_score"),
                    "baseline_delta_return_ci95_low": row.get("baseline_delta_return_ci95_low"),
                    "mean_profit_factor": row.get("mean_profit_factor"),
                    "mean_sharpe": row.get("mean_sharpe"),
                }
            )

    family_evidence: list[dict[str, Any]] = []
    for row in family_rows:
        ci_low = _safe_float(row.get("baseline_delta_return_ci95_low"))
        if ci_low is None:
            continue
        if (
            ci_low > 0
            and float(row.get("robustness_mean") or 0.0) >= 60.0
            and int(row.get("campaigns_total") or 0) >= 12
        ):
            family_evidence.append(
                {
                    "family": row.get("family"),
                    "robustness_mean": row.get("robustness_mean"),
                    "baseline_delta_return_ci95_low": row.get("baseline_delta_return_ci95_low"),
                    "survival_rate": row.get("survival_rate"),
                    "profit_factor_mean": row.get("profit_factor_mean"),
                    "sharpe_mean": row.get("sharpe_mean"),
                }
            )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "edge_exists_strategy_level": len(strategy_evidence) > 0,
        "edge_exists_family_level": len(family_evidence) > 0,
        "edge_exists_global": len(strategy_evidence) > 0 or len(family_evidence) > 0,
        "strategy_evidence": strategy_evidence,
        "family_evidence": family_evidence,
    }


def _build_family_robustness(strategy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    fam: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "strategies": 0,
            "survival": [],
            "pf": [],
            "sharpe": [],
            "drawdown": [],
            "robustness": [],
            "campaigns": 0,
            "paper_candidates": 0,
            "baseline_superou": 0,
            "trend_score": [],
            "baseline_delta": [],
        }
    )

    for row in strategy_rows:
        family = _family_norm(row.get("family"))
        bucket = fam[family]
        bucket["strategies"] += 1

        n_campaigns = int(row.get("number_of_campaigns") or 0)
        rejections = int(row.get("baseline_abaixo_count") or 0)
        survival = 1.0 - (float(rejections) / float(max(1, n_campaigns)))
        bucket["survival"].append(survival)

        pf = _safe_float(row.get("mean_profit_factor"))
        sh = _safe_float(row.get("mean_sharpe"))
        dd = _safe_float(row.get("mean_drawdown"))
        rb = _safe_float(row.get("robustness_score"))
        delta = _safe_float(row.get("baseline_delta_return_mean"))

        if pf is not None:
            bucket["pf"].append(pf)
        if sh is not None:
            bucket["sharpe"].append(sh)
        if dd is not None:
            bucket["drawdown"].append(dd)
        if rb is not None:
            bucket["robustness"].append(rb)
        if delta is not None:
            bucket["baseline_delta"].append(delta)

        bucket["campaigns"] += n_campaigns
        bucket["baseline_superou"] += int(row.get("baseline_superou_count") or 0)

        if str(row.get("current_state")) == "PAPER_CANDIDATE":
            bucket["paper_candidates"] += 1

        trend = str(row.get("trend") or "Stable")
        trend_score = 1.0 if trend == "Improving" else -1.0 if trend == "Declining" else 0.0
        bucket["trend_score"].append(trend_score)

    rows: list[dict[str, Any]] = []
    for family, bucket in fam.items():
        trend_avg = _avg(bucket["trend_score"]) or 0.0
        trend = "Improving" if trend_avg > 0.2 else "Declining" if trend_avg < -0.2 else "Stable"
        baseline_ci_low = _ci95_lower(bucket["baseline_delta"]) if bucket["baseline_delta"] else None

        rows.append(
            {
                "family": family,
                "strategies": int(bucket["strategies"]),
                "survival_rate": _avg(bucket["survival"]),
                "profit_factor_mean": _avg(bucket["pf"]),
                "sharpe_mean": _avg(bucket["sharpe"]),
                "drawdown_mean": _avg(bucket["drawdown"]),
                "robustness_mean": _avg(bucket["robustness"]),
                "campaigns_total": int(bucket["campaigns"]),
                "paper_candidate_strategies": int(bucket["paper_candidates"]),
                "baseline_superou_total": int(bucket["baseline_superou"]),
                "trend": trend,
                "baseline_delta_return_ci95_low": round(baseline_ci_low, 8) if baseline_ci_low is not None else None,
            }
        )

    rows.sort(
        key=lambda x: (
            -float(x.get("robustness_mean") or 0.0),
            -float(x.get("survival_rate") or 0.0),
            -float(x.get("profit_factor_mean") or 0.0),
        )
    )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "rows": rows,
    }


def _build_baseline_challenge(strategy_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for row in strategy_rows:
        superou = int(row.get("baseline_superou_count") or 0)
        empatou = int(row.get("baseline_empatou_count") or 0)
        abaixo = int(row.get("baseline_abaixo_count") or 0)
        total = superou + empatou + abaixo
        pct = (superou / total) if total > 0 else 0.0

        mean_delta = _safe_float(row.get("baseline_delta_return_mean"))
        slope = _safe_float(row.get("baseline_delta_trend_slope")) or 0.0

        rows.append(
            {
                "strategy": row.get("strategy"),
                "family": row.get("family"),
                "baseline_superou_count": superou,
                "baseline_total_count": total,
                "baseline_superou_pct": round(pct, 6),
                "baseline_distance_mean": mean_delta,
                "baseline_distance_trend": _trend_to_baseline(mean_delta, slope),
                "baseline_distance_slope": round(slope, 8),
                "robustness_score": row.get("robustness_score"),
            }
        )

    rows.sort(
        key=lambda x: (
            -float(x.get("baseline_superou_pct") or 0.0),
            -float(x.get("baseline_distance_mean") or -999.0),
            -float(x.get("robustness_score") or 0.0),
        )
    )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "rows": rows,
    }


def _build_promotion_window(candidates_rows: list[dict[str, Any]]) -> dict[str, Any]:
    robustness_values = [float(r.get("robustness_score") or 0.0) for r in candidates_rows]
    rb_mean = mean(robustness_values) if robustness_values else 0.0
    rb_std = pstdev(robustness_values) if len(robustness_values) > 1 else 0.0

    eligible_rows: list[dict[str, Any]] = []
    for row in candidates_rows:
        rb = float(row.get("robustness_score") or 0.0)
        ci_low = _safe_float(row.get("baseline_delta_return_ci95_low"))
        trend = str(row.get("trend") or "Stable")

        rb_z = (rb - rb_mean) / rb_std if rb_std > 1e-9 else 0.0
        is_eligible = rb_z >= 0.25 and ci_low is not None and ci_low > 0 and trend != "Declining"

        detail = {
            "strategy": row.get("strategy"),
            "family": row.get("family"),
            "robustness_score": rb,
            "robustness_zscore": round(rb_z, 6),
            "baseline_delta_return_ci95_low": ci_low,
            "trend": trend,
            "eligible": is_eligible,
            "justificativa_matematica": {
                "robustness_threshold": round(rb_mean + (0.25 * rb_std), 6),
                "criterio_1_robustness_zscore_ge_0_25": rb_z >= 0.25,
                "criterio_2_baseline_ci95_low_gt_0": ci_low is not None and ci_low > 0,
                "criterio_3_trend_not_declining": trend != "Declining",
            },
        }
        if is_eligible:
            eligible_rows.append(detail)

    eligible_rows.sort(
        key=lambda x: (
            -float(x.get("robustness_score") or 0.0),
            -float(x.get("baseline_delta_return_ci95_low") or -999.0),
        )
    )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "criteria": {
            "robustness_mean": round(rb_mean, 6),
            "robustness_std": round(rb_std, 6),
            "required_robustness_zscore": 0.25,
            "required_baseline_ci95_low_gt": 0.0,
            "required_trend": "not Declining",
        },
        "would_be_promoted_count": len(eligible_rows),
        "would_be_promoted": [r["strategy"] for r in eligible_rows],
        "details": eligible_rows,
    }


def _build_executive(
    campaigns: list[Campaign],
    strategy_rows: list[dict[str, Any]],
    candidates_rows: list[dict[str, Any]],
    family_rows: list[dict[str, Any]],
    edge_detection: dict[str, Any],
    promotion_window: dict[str, Any],
) -> dict[str, Any]:
    today = datetime.now(tz=timezone.utc).date()
    campaigns_today = [c for c in campaigns if c.generated_at.date() == today]

    throughput_values: list[float] = []
    stage_backtest_values: list[int] = []
    for c in campaigns_today:
        stage = c.payload.get("stage_counters") if isinstance(c.payload.get("stage_counters"), dict) else {}
        backtest_reached = int((stage or {}).get("backtest_reached") or 0)
        stage_backtest_values.append(backtest_reached)
        secs = _safe_float(c.payload.get("total_campaign_seconds")) or 0.0
        if secs > 0:
            throughput_values.append((backtest_reached / secs) * 60.0)

    avg_throughput = _avg(throughput_values)
    throughput_std = _std(throughput_values) or 0.0
    throughput_cv = (throughput_std / avg_throughput) if isinstance(avg_throughput, float) and avg_throughput else 0.0

    errors_today = 0
    err_path = MONITOR_DIR / "consistency_errors.log"
    if err_path.exists():
        for line in err_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if today.isoformat() in line:
                errors_today += 1

    # Campaign artifacts currently do not expose host utilization fields.
    machine_utilization_avg_pct = None
    machine_utilization_data_available = False

    sorted_by_robust = sorted(strategy_rows, key=lambda x: -float(x.get("robustness_score") or 0.0))
    leader_strategy = sorted_by_robust[0] if sorted_by_robust else None

    improving = sorted(
        strategy_rows,
        key=lambda x: (
            -1.0 if str(x.get("trend")) == "Improving" else 0.0,
            -float(x.get("trend_slope") or 0.0),
        ),
    )
    regressing = sorted(strategy_rows, key=lambda x: float(x.get("trend_slope") or 0.0))

    most_evolved = improving[0] if improving else None
    most_regressed = regressing[0] if regressing else None
    family_leader = family_rows[0] if family_rows else None

    if errors_today > 0:
        bottleneck = "monitor_consistency_attention"
    elif throughput_cv > 0.30:
        bottleneck = "throughput_instability"
    else:
        bottleneck = "none_detected"

    edge_exists = bool(edge_detection.get("edge_exists_global"))
    edge_gap = None
    if not edge_exists:
        edge_gap = {
            "missing_condition": "positive_baseline_confidence",
            "description": "Nenhuma estrategia/familia apresentou limite inferior CI95 de delta_return acima de zero com robustez suficiente.",
        }

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "platform": {
            "continua_estavel": errors_today == 0,
            "algum_gargalo_novo": bottleneck != "none_detected",
            "gargalo": bottleneck,
            "throughput_avg_strategies_per_minute": avg_throughput,
            "throughput_std": round(throughput_std, 6),
            "throughput_cv": round(throughput_cv, 6),
            "campaigns_executed_today": len(campaigns_today),
            "campaigns_considered_total": len(campaigns),
            "machine_utilization_avg_pct": machine_utilization_avg_pct,
            "machine_utilization_data_available": machine_utilization_data_available,
            "monitor_consistency_errors_today": errors_today,
        },
        "research": {
            "estrategia_mais_robusta": leader_strategy.get("strategy") if leader_strategy else None,
            "estrategia_que_mais_evoluiu": most_evolved.get("strategy") if most_evolved else None,
            "estrategia_que_mais_regrediu": most_regressed.get("strategy") if most_regressed else None,
            "familia_lider": family_leader.get("family") if family_leader else None,
            "paper_candidates": len(candidates_rows),
        },
        "edge": {
            "existe_edge_estatisticamente_consistente": edge_exists,
            "evidencias_estrategias": edge_detection.get("strategy_evidence", []),
            "evidencias_familias": edge_detection.get("family_evidence", []),
            "lacuna_atual": edge_gap,
        },
        "promotion_window": {
            "promovidas_simuladas": promotion_window.get("would_be_promoted_count"),
            "estrategias": promotion_window.get("would_be_promoted", []),
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_md(path: Path, executive: dict[str, Any]) -> None:
    platform = executive.get("platform", {})
    research = executive.get("research", {})
    edge = executive.get("edge", {})
    promo = executive.get("promotion_window", {})

    lines: list[str] = []
    lines.append("# Robustness Analytics - Executive")
    lines.append("")
    lines.append(f"- generated_at: {executive.get('generated_at')}")
    lines.append("")
    lines.append("## Plataforma")
    lines.append(f"- continua_estavel: {platform.get('continua_estavel')}")
    lines.append(f"- algum_gargalo_novo: {platform.get('algum_gargalo_novo')}")
    lines.append(f"- gargalo: {platform.get('gargalo')}")
    lines.append(f"- throughput_avg_strategies_per_minute: {platform.get('throughput_avg_strategies_per_minute')}")
    lines.append(f"- machine_utilization_avg_pct: {platform.get('machine_utilization_avg_pct')}")
    lines.append("")
    lines.append("## Pesquisa")
    lines.append(f"- estrategia_mais_robusta: {research.get('estrategia_mais_robusta')}")
    lines.append(f"- estrategia_que_mais_evoluiu: {research.get('estrategia_que_mais_evoluiu')}")
    lines.append(f"- estrategia_que_mais_regrediu: {research.get('estrategia_que_mais_regrediu')}")
    lines.append(f"- familia_lider: {research.get('familia_lider')}")
    lines.append("")
    lines.append("## Edge")
    lines.append(
        f"- existe_edge_estatisticamente_consistente: {edge.get('existe_edge_estatisticamente_consistente')}"
    )
    lines.append("")
    lines.append("## Promocao Simulada")
    lines.append(f"- promovidas_simuladas: {promo.get('promovidas_simuladas')}")
    lines.append(f"- estrategias: {promo.get('estrategias')}")

    path.write_text("\n".join(lines), encoding="utf-8")


def run_robustness_analytics() -> dict[str, str]:
    campaigns = _load_phase13_campaigns()
    if not campaigns:
        raise RuntimeError("No phase 13 campaigns found in optimization/results")

    strategy_robustness = _build_strategy_robustness(campaigns)
    strategy_rows = strategy_robustness.get("rows", []) if isinstance(strategy_robustness.get("rows"), list) else []

    candidates = _build_candidates_classification(strategy_rows)
    candidates_rows = candidates.get("rows", []) if isinstance(candidates.get("rows"), list) else []

    family = _build_family_robustness(strategy_rows)
    family_rows = family.get("rows", []) if isinstance(family.get("rows"), list) else []

    baseline_challenge = _build_baseline_challenge(strategy_rows)
    edge_detection = _build_edge_detection(strategy_rows, family_rows)
    promotion_window = _build_promotion_window(candidates_rows)
    executive = _build_executive(
        campaigns=campaigns,
        strategy_rows=strategy_rows,
        candidates_rows=candidates_rows,
        family_rows=family_rows,
        edge_detection=edge_detection,
        promotion_window=promotion_window,
    )

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    strategy_path = RESULTS_DIR / "robustness_analytics_history.json"
    candidates_path = RESULTS_DIR / "robustness_analytics_paper_candidates.json"
    family_path = RESULTS_DIR / "robustness_analytics_family_stats.json"
    baseline_path = RESULTS_DIR / "robustness_analytics_baseline_challenge.json"
    edge_path = RESULTS_DIR / "robustness_analytics_edge_detection.json"
    promotion_path = RESULTS_DIR / "robustness_analytics_promotion_window.json"
    executive_json_path = RESULTS_DIR / f"robustness_analytics_executive_{stamp}.json"
    executive_md_path = RESULTS_DIR / f"robustness_analytics_executive_{stamp}.md"

    _write_json(strategy_path, strategy_robustness)
    _write_json(candidates_path, candidates)
    _write_json(family_path, family)
    _write_json(baseline_path, baseline_challenge)
    _write_json(edge_path, edge_detection)
    _write_json(promotion_path, promotion_window)
    _write_json(executive_json_path, executive)
    _write_md(executive_md_path, executive)

    return {
        "strategy_history": str(strategy_path),
        "paper_candidates": str(candidates_path),
        "family_stats": str(family_path),
        "baseline_challenge": str(baseline_path),
        "edge_detection": str(edge_path),
        "promotion_window": str(promotion_path),
        "executive_json": str(executive_json_path),
        "executive_md": str(executive_md_path),
    }


def main() -> None:
    outputs = run_robustness_analytics()
    print("ROBUSTNESS ANALYTICS - OK")
    for key, value in outputs.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
