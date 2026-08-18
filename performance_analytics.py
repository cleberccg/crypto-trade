from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
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

    trades = (
        _safe_int(merged.get("number_of_trades"))
        or _safe_int(merged.get("total_trades"))
        or _safe_int(merged.get("trades"))
    )
    return {
        "profit_factor": _safe_float(merged.get("profit_factor")),
        "sharpe": _safe_float(merged.get("sharpe")) or _safe_float(merged.get("sharpe_ratio")),
        "expectancy": _safe_float(merged.get("expectancy")),
        "drawdown": _safe_float(merged.get("drawdown_pct")) or _safe_float(merged.get("max_drawdown_pct")),
        "trades": trades,
    }


def _is_rejection_state(state: str) -> bool:
    return state in {
        "REJECTED_BY_PERFORMANCE",
        "REJECTED",
        "REJECTED_AFTER_PAPER_EXPERIMENT",
        "REJECTED_BY_INFRASTRUCTURE",
    }


def _build_strategy_history(campaigns: list[Campaign]) -> dict[str, Any]:
    total_campaigns = len(campaigns)
    strat: dict[str, dict[str, Any]] = {}

    for campaign in campaigns:
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
            score = _safe_float(row.get("queue_score"))
            if score is None:
                score = _safe_float(row.get("score"))

            entry = strat.setdefault(
                name,
                {
                    "strategy": name,
                    "family": family,
                    "campaigns": set(),
                    "first_seen": generated_at,
                    "last_seen": generated_at,
                    "scores": [],
                    "profit_factor": [],
                    "sharpe": [],
                    "expectancy": [],
                    "drawdown": [],
                    "early_stops": 0,
                    "optimizer_probe": 0,
                    "optimizer_full": 0,
                    "validation": 0,
                    "paper_experimental": 0,
                    "paper_candidate": 0,
                    "paper_approved": 0,
                    "rejections": 0,
                    "rejection_reasons": Counter(),
                    "baseline_superou": 0,
                    "baseline_empatou": 0,
                    "baseline_abaixo": 0,
                    "states": Counter(),
                },
            )

            entry["campaigns"].add(campaign_id)
            entry["last_seen"] = generated_at
            if generated_at < entry["first_seen"]:
                entry["first_seen"] = generated_at

            if isinstance(score, float):
                entry["scores"].append(score)
            for k, out_key in (
                ("profit_factor", "profit_factor"),
                ("sharpe", "sharpe"),
                ("expectancy", "expectancy"),
                ("drawdown", "drawdown"),
            ):
                value = metrics.get(k)
                if isinstance(value, float):
                    entry[out_key].append(value)

            entry["states"][state] += 1

            early = row.get("early_stop")
            if isinstance(early, dict) and bool(early.get("triggered")):
                entry["early_stops"] += 1

            if isinstance(row.get("optimizer_probe"), dict):
                entry["optimizer_probe"] += 1
            if isinstance(row.get("optimizer"), dict):
                entry["optimizer_full"] += 1
            if isinstance(row.get("validation"), dict):
                entry["validation"] += 1
            if isinstance(row.get("paper_experimental"), dict):
                entry["paper_experimental"] += 1

            if state == "PAPER_CANDIDATE":
                entry["paper_candidate"] += 1
            if state in {"PAPER_APPROVED", "approved", "in_paper_trading"}:
                entry["paper_approved"] += 1
            if _is_rejection_state(state):
                entry["rejections"] += 1
                reason = str(row.get("rejection_reason") or row.get("state_reason") or "unknown")
                entry["rejection_reasons"][reason] += 1

            baseline = row.get("baseline_comparison")
            if isinstance(baseline, dict):
                decision = str(baseline.get("decision", "")).upper()
                if decision == "SUPEROU":
                    entry["baseline_superou"] += 1
                elif decision == "EMPATOU":
                    entry["baseline_empatou"] += 1
                elif decision == "ABAIXO":
                    entry["baseline_abaixo"] += 1

    out_rows: list[dict[str, Any]] = []
    for data in strat.values():
        campaigns_count = len(data["campaigns"])
        rejection_rate = (float(data["rejections"]) / float(max(1, campaigns_count)))

        pf_mean = _avg(data["profit_factor"])
        sharpe_mean = _avg(data["sharpe"])
        expectancy_mean = _avg(data["expectancy"])
        drawdown_mean = _avg(data["drawdown"])

        pf_std = _std(data["profit_factor"]) or 0.0
        sharpe_std = _std(data["sharpe"]) or 0.0

        pf_stability = 1.0
        if isinstance(pf_mean, float) and abs(pf_mean) > 1e-9:
            pf_stability = max(0.0, 1.0 - min(1.0, abs(pf_std / pf_mean)))

        sharpe_stability = 1.0
        if isinstance(sharpe_mean, float) and abs(sharpe_mean) > 1e-9:
            sharpe_stability = max(0.0, 1.0 - min(1.0, abs(sharpe_std / sharpe_mean)))

        presence_rate = float(campaigns_count) / float(max(1, total_campaigns))
        baseline_total = data["baseline_superou"] + data["baseline_empatou"] + data["baseline_abaixo"]
        baseline_win_rate = float(data["baseline_superou"]) / float(max(1, baseline_total))

        consistency = 100.0 * (
            0.30 * presence_rate
            + 0.25 * (1.0 - rejection_rate)
            + 0.20 * pf_stability
            + 0.15 * sharpe_stability
            + 0.10 * baseline_win_rate
        )

        predominant_reason = None
        if data["rejection_reasons"]:
            predominant_reason = data["rejection_reasons"].most_common(1)[0][0]

        out_rows.append(
            {
                "strategy": data["strategy"],
                "family": data["family"],
                "number_of_campaigns": campaigns_count,
                "first_seen": data["first_seen"],
                "last_seen": data["last_seen"],
                "best_score": max(data["scores"]) if data["scores"] else None,
                "mean_score": _avg(data["scores"]),
                "best_profit_factor": max(data["profit_factor"]) if data["profit_factor"] else None,
                "mean_profit_factor": pf_mean,
                "best_sharpe": max(data["sharpe"]) if data["sharpe"] else None,
                "mean_sharpe": sharpe_mean,
                "best_expectancy": max(data["expectancy"]) if data["expectancy"] else None,
                "mean_expectancy": expectancy_mean,
                "mean_drawdown": drawdown_mean,
                "early_stop_count": int(data["early_stops"]),
                "optimizer_probe_count": int(data["optimizer_probe"]),
                "optimizer_full_count": int(data["optimizer_full"]),
                "validation_count": int(data["validation"]),
                "paper_experimental_count": int(data["paper_experimental"]),
                "paper_candidate_count": int(data["paper_candidate"]),
                "paper_approved_count": int(data["paper_approved"]),
                "rejections_count": int(data["rejections"]),
                "predominant_rejection_reason": predominant_reason,
                "baseline_superou_count": int(data["baseline_superou"]),
                "baseline_empatou_count": int(data["baseline_empatou"]),
                "baseline_abaixo_count": int(data["baseline_abaixo"]),
                "consistency_score": round(consistency, 4),
                "states_counter": dict(data["states"]),
            }
        )

    out_rows.sort(
        key=lambda x: (
            -float(x.get("consistency_score") or 0.0),
            -float(x.get("mean_profit_factor") or 0.0),
            -float(x.get("mean_sharpe") or 0.0),
            -int(x.get("baseline_superou_count") or 0),
            -int(x.get("number_of_campaigns") or 0),
        )
    )
    for idx, row in enumerate(out_rows, start=1):
        row["consistency_rank"] = idx

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "campaigns_considered": total_campaigns,
        "rows": out_rows,
    }


def _build_hall_of_fame(history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(
        history_rows,
        key=lambda x: (
            -float(x.get("consistency_score") or 0.0),
            -float(x.get("mean_profit_factor") or 0.0),
            -float(x.get("mean_sharpe") or 0.0),
            -int(x.get("baseline_superou_count") or 0),
            -int(x.get("number_of_campaigns") or 0),
        ),
    )
    top = rows[:20]
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "count": len(rows),
        "top_20": top,
    }


def _build_family_analytics(history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    fam: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "strategies": 0,
            "survival_values": [],
            "pf": [],
            "sharpe": [],
            "paper_candidate": 0,
            "paper_approved": 0,
            "consistency": [],
        }
    )

    for row in history_rows:
        family = _family_norm(row.get("family"))
        bucket = fam[family]
        bucket["strategies"] += 1

        n_campaigns = int(row.get("number_of_campaigns") or 0)
        rejections = int(row.get("rejections_count") or 0)
        survival = 1.0 - (float(rejections) / float(max(1, n_campaigns)))
        bucket["survival_values"].append(survival)

        if isinstance(row.get("mean_profit_factor"), float):
            bucket["pf"].append(float(row["mean_profit_factor"]))
        if isinstance(row.get("mean_sharpe"), float):
            bucket["sharpe"].append(float(row["mean_sharpe"]))
        if isinstance(row.get("consistency_score"), (int, float)):
            bucket["consistency"].append(float(row["consistency_score"]))

        if int(row.get("paper_candidate_count") or 0) > 0:
            bucket["paper_candidate"] += 1
        if int(row.get("paper_approved_count") or 0) > 0:
            bucket["paper_approved"] += 1

    rows: list[dict[str, Any]] = []
    for family, data in fam.items():
        rows.append(
            {
                "family": family,
                "strategies": int(data["strategies"]),
                "survival_rate": _avg(data["survival_values"]),
                "profit_factor_mean": _avg(data["pf"]),
                "sharpe_mean": _avg(data["sharpe"]),
                "paper_candidate_strategies": int(data["paper_candidate"]),
                "paper_approved_strategies": int(data["paper_approved"]),
                "consistency_score_mean": _avg(data["consistency"]),
            }
        )

    rows.sort(
        key=lambda x: (
            -float(x.get("consistency_score_mean") or 0.0),
            -float(x.get("profit_factor_mean") or 0.0),
            -float(x.get("survival_rate") or 0.0),
        )
    )
    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "rows": rows,
    }


def _build_baseline_evolution(campaigns: list[Campaign]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals = {"superou": 0, "empatou": 0, "abaixo": 0}

    for c in campaigns:
        baseline = c.payload.get("baseline_official")
        if isinstance(baseline, dict):
            superou = int(baseline.get("superou") or 0)
            empatou = int(baseline.get("empatou") or 0)
            abaixo = int(baseline.get("abaixo") or 0)
        else:
            superou = 0
            empatou = 0
            abaixo = 0
            for row in c.payload.get("backlog", []):
                if not isinstance(row, dict):
                    continue
                comp = row.get("baseline_comparison")
                if not isinstance(comp, dict):
                    continue
                dec = str(comp.get("decision", "")).upper()
                if dec == "SUPEROU":
                    superou += 1
                elif dec == "EMPATOU":
                    empatou += 1
                elif dec == "ABAIXO":
                    abaixo += 1

        totals["superou"] += superou
        totals["empatou"] += empatou
        totals["abaixo"] += abaixo

        rows.append(
            {
                "campaign_file": c.file_path.name,
                "run_id": c.payload.get("run_id"),
                "generated_at": c.generated_at.isoformat(),
                "superou": superou,
                "empatou": empatou,
                "abaixo": abaixo,
            }
        )

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "campaigns": rows,
        "totals": totals,
    }


def _build_executive_daily(
    campaigns: list[Campaign],
    history_rows: list[dict[str, Any]],
    hall_of_fame: dict[str, Any],
    family_analytics: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    today = datetime.now(tz=timezone.utc).date()
    campaigns_today = [c for c in campaigns if c.generated_at.date() == today]

    throughput_values: list[float] = []
    for c in campaigns_today:
        report = c.payload
        stage = report.get("stage_counters") if isinstance(report.get("stage_counters"), dict) else {}
        backtest_reached = int((stage or {}).get("backtest_reached") or 0)
        secs = _safe_float(report.get("total_campaign_seconds")) or 0.0
        if secs > 0:
            throughput_values.append((backtest_reached / secs) * 60.0)

    avg_throughput = _avg(throughput_values)

    errors_today = 0
    err_path = MONITOR_DIR / "consistency_errors.log"
    if err_path.exists():
        for line in err_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            if today.isoformat() in line:
                errors_today += 1

    latest = campaigns[-1] if campaigns else None
    prev = campaigns[-2] if len(campaigns) >= 2 else None

    latest_candidates: set[str] = set()
    prev_candidates: set[str] = set()
    if latest:
        latest_candidates = {
            str(r.get("candidate_name"))
            for r in latest.payload.get("backlog", [])
            if isinstance(r, dict) and str(r.get("state")) == "PAPER_CANDIDATE"
        }
    if prev:
        prev_candidates = {
            str(r.get("candidate_name"))
            for r in prev.payload.get("backlog", [])
            if isinstance(r, dict) and str(r.get("state")) == "PAPER_CANDIDATE"
        }

    new_candidates = sorted(latest_candidates - prev_candidates)
    removed_candidates = sorted(prev_candidates - latest_candidates)

    family_rows = family_analytics.get("rows", []) if isinstance(family_analytics.get("rows"), list) else []
    hof_rows = hall_of_fame.get("top_20", []) if isinstance(hall_of_fame.get("top_20"), list) else []

    prolonged_observation = [
        r["strategy"]
        for r in history_rows
        if float(r.get("consistency_score") or 0.0) >= 70.0 and int(r.get("paper_candidate_count") or 0) >= 2
    ][:10]

    continuous_paper_candidates = [
        r["strategy"]
        for r in history_rows
        if float(r.get("consistency_score") or 0.0) >= 75.0
        and float(r.get("mean_profit_factor") or 0.0) >= 1.2
        and int(r.get("baseline_superou_count") or 0) >= 2
    ][:10]

    return {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "platform": {
            "campaigns_executed_today": len(campaigns_today),
            "campaigns_considered_total": len(campaigns),
            "stability": "stable" if errors_today == 0 else "attention",
            "monitor_consistency_errors_today": errors_today,
            "throughput_avg_strategies_per_minute": avg_throughput,
        },
        "research": {
            "paper_candidate_current": len(latest_candidates),
            "paper_candidate_new": len(new_candidates),
            "paper_candidate_removed": len(removed_candidates),
            "paper_candidate_new_names": new_candidates,
            "paper_candidate_removed_names": removed_candidates,
            "hall_of_fame_top_10": hof_rows[:10],
            "family_leaders": family_rows[:5],
        },
        "intelligence": {
            "highest_potential_strategy": hof_rows[0]["strategy"] if hof_rows else None,
            "highest_consistency_family": family_rows[0]["family"] if family_rows else None,
            "ready_for_prolonged_observation": prolonged_observation,
            "candidate_for_continuous_paper_trading": continuous_paper_candidates,
            "baseline_totals": baseline.get("totals", {}),
        },
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _write_md(path: Path, executive: dict[str, Any]) -> None:
    plat = executive.get("platform", {})
    research = executive.get("research", {})
    intel = executive.get("intelligence", {})

    lines: list[str] = []
    lines.append("# Performance Analytics - Executive Daily")
    lines.append("")
    lines.append(f"- generated_at: {executive.get('generated_at')}")
    lines.append("")
    lines.append("## Plataforma")
    lines.append(f"- campaigns_executed_today: {plat.get('campaigns_executed_today')}")
    lines.append(f"- campaigns_considered_total: {plat.get('campaigns_considered_total')}")
    lines.append(f"- stability: {plat.get('stability')}")
    lines.append(f"- monitor_consistency_errors_today: {plat.get('monitor_consistency_errors_today')}")
    lines.append(f"- throughput_avg_strategies_per_minute: {plat.get('throughput_avg_strategies_per_minute')}")
    lines.append("")
    lines.append("## Pesquisa")
    lines.append(f"- paper_candidate_current: {research.get('paper_candidate_current')}")
    lines.append(f"- paper_candidate_new: {research.get('paper_candidate_new')}")
    lines.append(f"- paper_candidate_removed: {research.get('paper_candidate_removed')}")
    lines.append("")
    lines.append("## Inteligencia")
    lines.append(f"- highest_potential_strategy: {intel.get('highest_potential_strategy')}")
    lines.append(f"- highest_consistency_family: {intel.get('highest_consistency_family')}")
    lines.append(f"- ready_for_prolonged_observation: {intel.get('ready_for_prolonged_observation')}")
    lines.append(f"- candidate_for_continuous_paper_trading: {intel.get('candidate_for_continuous_paper_trading')}")

    path.write_text("\n".join(lines), encoding="utf-8")


def run_performance_analytics() -> dict[str, str]:
    campaigns = _load_phase13_campaigns()
    if not campaigns:
        raise RuntimeError("No phase 13 campaigns found in optimization/results")

    history = _build_strategy_history(campaigns)
    history_rows = history.get("rows", []) if isinstance(history.get("rows"), list) else []

    hall_of_fame = _build_hall_of_fame(history_rows)
    family_analytics = _build_family_analytics(history_rows)
    baseline = _build_baseline_evolution(campaigns)
    executive = _build_executive_daily(campaigns, history_rows, hall_of_fame, family_analytics, baseline)

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")

    history_path = RESULTS_DIR / "performance_analytics_history.json"
    hof_path = RESULTS_DIR / "performance_analytics_hall_of_fame.json"
    family_path = RESULTS_DIR / "performance_analytics_family_stats.json"
    baseline_path = RESULTS_DIR / "performance_analytics_baseline_evolution.json"
    exec_json_path = RESULTS_DIR / f"performance_analytics_executive_{stamp}.json"
    exec_md_path = RESULTS_DIR / f"performance_analytics_executive_{stamp}.md"

    _write_json(history_path, history)
    _write_json(hof_path, hall_of_fame)
    _write_json(family_path, family_analytics)
    _write_json(baseline_path, baseline)
    _write_json(exec_json_path, executive)
    _write_md(exec_md_path, executive)

    return {
        "history": str(history_path),
        "hall_of_fame": str(hof_path),
        "family_stats": str(family_path),
        "baseline_evolution": str(baseline_path),
        "executive_json": str(exec_json_path),
        "executive_md": str(exec_md_path),
    }


def main() -> None:
    outputs = run_performance_analytics()
    print("PERFORMANCE ANALYTICS - OK")
    for key, value in outputs.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
