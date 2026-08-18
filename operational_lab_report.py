from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

RESULTS_DIR = Path("optimization/results")


@dataclass
class CampaignSnapshot:
    path: Path
    generated_at: datetime
    symbol: str
    timeframe: str
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
        parsed = float(value)
        if not math.isfinite(parsed):
            return None
        return parsed
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_phase13_campaigns() -> list[CampaignSnapshot]:
    campaigns: list[CampaignSnapshot] = []
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

        campaigns.append(
            CampaignSnapshot(
                path=file_path,
                generated_at=_parse_dt(payload.get("generated_at")),
                symbol=str(payload.get("symbol", "")),
                timeframe=str(payload.get("timeframe", "")),
                payload=payload,
            )
        )

    campaigns.sort(key=lambda c: c.generated_at)
    return campaigns


def _extract_metrics(row: dict[str, Any]) -> dict[str, float | int | None]:
    sources: list[dict[str, Any]] = []

    for key in ("backtest", "backtest_base"):
        val = row.get(key)
        if isinstance(val, dict):
            sources.append(val)

    early_input = row.get("early_stop_input")
    if isinstance(early_input, dict) and isinstance(early_input.get("metrics"), dict):
        sources.append(early_input["metrics"])

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
        "drawdown_pct": _safe_float(merged.get("drawdown_pct")) or _safe_float(merged.get("max_drawdown_pct")),
        "win_rate": _safe_float(merged.get("win_rate")) or _safe_float(merged.get("win_rate_pct")),
        "return_pct": _safe_float(merged.get("return_pct")),
        "number_of_trades": trades,
    }


def _avg(values: list[float]) -> float | None:
    return round(mean(values), 6) if values else None


def _stability(returns: list[float]) -> float | None:
    if len(returns) < 2:
        return None
    mu = mean(returns)
    sigma = pstdev(returns)
    denom = abs(mu) + 1e-6
    score = 1.0 - (sigma / denom)
    return round(max(0.0, min(1.0, score)), 6)


def _state_groups(row_state: str) -> str:
    state = row_state.upper()
    if state == "NO_TRADES":
        return "NO_TRADES"
    if state == "INCONCLUSIVE_LOW_SAMPLE":
        return "INCONCLUSIVE_LOW_SAMPLE"
    if state in {"REJECTED_BY_PERFORMANCE", "REJECTED", "REJECTED_AFTER_PAPER_EXPERIMENT"}:
        return "REJECTED_BY_PERFORMANCE"
    if state == "REJECTED_BY_INFRASTRUCTURE":
        return "REJECTED_BY_INFRASTRUCTURE"
    return state


def build_leaderboard(campaigns: list[CampaignSnapshot]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    strategy_stats: dict[str, dict[str, Any]] = {}
    family_rollup: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "strategies": set(),
            "campaigns_executed": 0,
            "profit_factor": [],
            "sharpe": [],
            "expectancy": [],
            "approved": 0,
            "paper_candidate": 0,
            "rejected": 0,
            "total_rows": 0,
        }
    )
    context_rollup: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "strategies": set(),
            "campaigns": 0,
            "profit_factor": [],
            "sharpe": [],
            "expectancy": [],
        }
    )

    latest = campaigns[-1]
    previous = campaigns[-2] if len(campaigns) >= 2 else None

    for campaign in campaigns:
        ranking_pos = {
            str(x.get("candidate_name")): int(x.get("rank", 999999))
            for x in campaign.payload.get("ranking_updated", [])
            if isinstance(x, dict) and x.get("candidate_name")
        }

        for row in campaign.payload.get("backlog", []):
            if not isinstance(row, dict):
                continue
            name = str(row.get("candidate_name", "")).strip()
            if not name:
                continue

            family = str(row.get("family", "unknown") or "unknown")
            state = str(row.get("state", "unknown") or "unknown")
            grouped_state = _state_groups(state)
            metrics = _extract_metrics(row)

            stats = strategy_stats.setdefault(
                name,
                {
                    "name": name,
                    "family": family,
                    "origin": row.get("origin"),
                    "asset": campaign.symbol,
                    "timeframe": campaign.timeframe,
                    "campaigns_executed": 0,
                    "backtests_realizados": 0,
                    "validacoes": 0,
                    "paper_candidate_count": 0,
                    "paper_experimental_count": 0,
                    "profit_factor_values": [],
                    "sharpe_values": [],
                    "expectancy_values": [],
                    "drawdown_values": [],
                    "win_rate_values": [],
                    "return_values": [],
                    "trades_values": [],
                    "states": Counter(),
                    "best_campaign": None,
                    "worst_campaign": None,
                    "position_current": None,
                    "campaign_history": [],
                },
            )

            stats["campaigns_executed"] += 1
            stats["states"][grouped_state] += 1

            if isinstance(row.get("backtest"), dict) or isinstance(row.get("backtest_base"), dict):
                stats["backtests_realizados"] += 1
            if isinstance(row.get("validation"), dict):
                stats["validacoes"] += 1
            if state == "PAPER_CANDIDATE":
                stats["paper_candidate_count"] += 1
            if isinstance(row.get("paper_experimental"), dict):
                stats["paper_experimental_count"] += 1

            ret = metrics.get("return_pct")
            if isinstance(ret, float):
                stats["return_values"].append(ret)
                best = stats["best_campaign"]
                if best is None or ret > float(best.get("return_pct", -999999.0)):
                    stats["best_campaign"] = {
                        "file": campaign.path.name,
                        "generated_at": campaign.generated_at.isoformat(),
                        "return_pct": ret,
                    }
                worst = stats["worst_campaign"]
                if worst is None or ret < float(worst.get("return_pct", 999999.0)):
                    stats["worst_campaign"] = {
                        "file": campaign.path.name,
                        "generated_at": campaign.generated_at.isoformat(),
                        "return_pct": ret,
                    }

            for metric_key, bucket in (
                ("profit_factor", "profit_factor_values"),
                ("sharpe", "sharpe_values"),
                ("expectancy", "expectancy_values"),
                ("drawdown_pct", "drawdown_values"),
                ("win_rate", "win_rate_values"),
                ("number_of_trades", "trades_values"),
            ):
                value = metrics.get(metric_key)
                if isinstance(value, (int, float)):
                    stats[bucket].append(float(value))

            stats["campaign_history"].append(
                {
                    "campaign_file": campaign.path.name,
                    "generated_at": campaign.generated_at.isoformat(),
                    "state": state,
                    "state_group": grouped_state,
                    "return_pct": metrics.get("return_pct"),
                    "profit_factor": metrics.get("profit_factor"),
                    "sharpe": metrics.get("sharpe"),
                    "expectancy": metrics.get("expectancy"),
                    "number_of_trades": metrics.get("number_of_trades"),
                }
            )

            if campaign is latest:
                stats["position_current"] = ranking_pos.get(name)
                stats["family"] = family
                stats["origin"] = row.get("origin")
                stats["asset"] = campaign.symbol
                stats["timeframe"] = campaign.timeframe

            fam = family_rollup[family]
            fam["strategies"].add(name)
            fam["campaigns_executed"] += 1
            fam["total_rows"] += 1
            if state in {"approved", "PAPER_APPROVED"}:
                fam["approved"] += 1
            if state == "PAPER_CANDIDATE":
                fam["paper_candidate"] += 1
            if grouped_state in {"REJECTED_BY_PERFORMANCE", "REJECTED_BY_INFRASTRUCTURE"}:
                fam["rejected"] += 1
            for key in ("profit_factor", "sharpe", "expectancy"):
                value = metrics.get(key)
                if isinstance(value, float):
                    fam[key].append(value)

            context_key = f"{campaign.symbol}|{campaign.timeframe}|N/A"
            ctx = context_rollup[context_key]
            ctx["strategies"].add(name)
            ctx["campaigns"] += 1
            for key in ("profit_factor", "sharpe", "expectancy"):
                value = metrics.get(key)
                if isinstance(value, float):
                    ctx[key].append(value)

    leaderboard_rows: list[dict[str, Any]] = []
    for stats in strategy_stats.values():
        avg_pf = _avg(stats["profit_factor_values"])
        avg_sharpe = _avg(stats["sharpe_values"])
        avg_expectancy = _avg(stats["expectancy_values"])
        avg_dd = _avg(stats["drawdown_values"])
        avg_wr = _avg(stats["win_rate_values"])
        avg_trades = _avg(stats["trades_values"])
        stability = _stability(stats["return_values"])

        perf_score = 0.0
        if isinstance(avg_pf, float):
            perf_score += min(avg_pf, 5.0) * 25.0
        if isinstance(avg_sharpe, float):
            perf_score += max(min(avg_sharpe, 3.0), -1.0) * 10.0
        if isinstance(avg_expectancy, float):
            perf_score += max(min(avg_expectancy, 5.0), -5.0) * 4.0
        if isinstance(stability, float):
            perf_score += stability * 20.0

        row = {
            "name": stats["name"],
            "family": stats["family"],
            "origin": stats["origin"],
            "asset": stats["asset"],
            "timeframe": stats["timeframe"],
            "campaigns_executed": stats["campaigns_executed"],
            "backtests_realizados": stats["backtests_realizados"],
            "validacoes": stats["validacoes"],
            "paper_candidate_count": stats["paper_candidate_count"],
            "paper_experimental_count": stats["paper_experimental_count"],
            "profit_factor_mean": avg_pf,
            "sharpe_mean": avg_sharpe,
            "expectancy_mean": avg_expectancy,
            "drawdown_mean": avg_dd,
            "win_rate_mean": avg_wr,
            "trades_total": int(sum(stats["trades_values"])) if stats["trades_values"] else 0,
            "stability_between_campaigns": stability,
            "best_campaign": stats["best_campaign"],
            "worst_campaign": stats["worst_campaign"],
            "position_current": stats["position_current"],
            "states_counter": dict(stats["states"]),
            "ranking_score": round(perf_score, 4),
        }
        leaderboard_rows.append(row)

    leaderboard_rows.sort(
        key=lambda x: (
            -float(x.get("ranking_score") or 0.0),
            int(x.get("position_current") or 999999),
            -int(x.get("campaigns_executed") or 0),
        )
    )
    for idx, row in enumerate(leaderboard_rows, start=1):
        row["ranking_position"] = idx

    family_rows: list[dict[str, Any]] = []
    for family, data in family_rollup.items():
        total = max(1, int(data["total_rows"]))
        family_rows.append(
            {
                "family": family,
                "strategies": len(data["strategies"]),
                "campaigns_executed": int(data["campaigns_executed"]),
                "profit_factor_mean": _avg(data["profit_factor"]),
                "sharpe_mean": _avg(data["sharpe"]),
                "expectancy_mean": _avg(data["expectancy"]),
                "approval_rate": round(float(data["approved"]) / float(total), 6),
                "paper_candidate_rate": round(float(data["paper_candidate"]) / float(total), 6),
                "rejection_rate": round(float(data["rejected"]) / float(total), 6),
            }
        )
    family_rows.sort(
        key=lambda x: (
            -float(x.get("paper_candidate_rate") or 0.0),
            -float(x.get("profit_factor_mean") or 0.0),
            -float(x.get("sharpe_mean") or 0.0),
        )
    )

    context_rows: list[dict[str, Any]] = []
    for context_key, data in context_rollup.items():
        symbol, timeframe, regime = context_key.split("|")
        context_rows.append(
            {
                "symbol": symbol,
                "timeframe": timeframe,
                "market_regime": regime,
                "strategies": len(data["strategies"]),
                "campaigns_executed": int(data["campaigns"]),
                "profit_factor_mean": _avg(data["profit_factor"]),
                "sharpe_mean": _avg(data["sharpe"]),
                "expectancy_mean": _avg(data["expectancy"]),
            }
        )
    context_rows.sort(
        key=lambda x: (
            -float(x.get("profit_factor_mean") or 0.0),
            -float(x.get("sharpe_mean") or 0.0),
        )
    )

    latest_backlog = {
        str(r.get("candidate_name")): r
        for r in latest.payload.get("backlog", [])
        if isinstance(r, dict) and r.get("candidate_name")
    }
    previous_backlog = {
        str(r.get("candidate_name")): r
        for r in (previous.payload.get("backlog", []) if previous else [])
        if isinstance(r, dict) and r.get("candidate_name")
    }

    changed_states: list[dict[str, Any]] = []
    for name, latest_row in latest_backlog.items():
        prev_row = previous_backlog.get(name)
        if not prev_row:
            continue
        prev_state = str(prev_row.get("state", ""))
        latest_state = str(latest_row.get("state", ""))
        if prev_state != latest_state:
            changed_states.append(
                {
                    "name": name,
                    "family": latest_row.get("family"),
                    "previous_state": prev_state,
                    "current_state": latest_state,
                }
            )

    baseline_superou = [
        r
        for r in latest.payload.get("backlog", [])
        if isinstance(r, dict)
        and isinstance(r.get("baseline_comparison"), dict)
        and str((r.get("baseline_comparison") or {}).get("decision", "")).upper() == "SUPEROU"
    ]

    promoted: list[str] = []
    archived: list[str] = []
    for row in leaderboard_rows:
        paper_count = int(row.get("paper_candidate_count") or 0)
        campaigns_executed = int(row.get("campaigns_executed") or 0)
        avg_pf = row.get("profit_factor_mean")
        avg_exp = row.get("expectancy_mean")
        rej_perf = int((row.get("states_counter") or {}).get("REJECTED_BY_PERFORMANCE", 0))

        if (
            paper_count >= 2
            and campaigns_executed >= 2
            and isinstance(avg_pf, float)
            and isinstance(avg_exp, float)
            and avg_pf >= 1.0
            and avg_exp > 0.0
        ):
            promoted.append(str(row.get("name")))

        if (
            campaigns_executed >= 3
            and rej_perf >= 3
            and isinstance(avg_pf, float)
            and isinstance(avg_exp, float)
            and avg_pf < 1.0
            and avg_exp < 0.0
        ):
            archived.append(str(row.get("name")))

    executive = {
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "latest_campaign_file": latest.path.name,
        "previous_campaign_file": previous.path.name if previous else None,
        "top_10_strategies": leaderboard_rows[:10],
        "top_5_families": family_rows[:5],
        "promoted_strategies": promoted,
        "archived_strategies": archived,
        "paper_experimental_strategies": [
            str(r.get("candidate_name"))
            for r in latest.payload.get("backlog", [])
            if isinstance(r, dict) and isinstance(r.get("paper_experimental"), dict)
        ],
        "baseline_superou_count": len(baseline_superou),
        "baseline_superou_strategies": [str(r.get("candidate_name")) for r in baseline_superou],
        "changed_vs_previous_count": len(changed_states),
        "changed_vs_previous": changed_states,
        "top_rejection_reasons": latest.payload.get("top_rejection_reasons", []),
        "operational_bugs_detected": [],
        "daily_questions": {
            "leading_strategy": leaderboard_rows[0]["name"] if leaderboard_rows else None,
            "leading_family": family_rows[0]["family"] if family_rows else None,
            "significant_evolution": [x["name"] for x in changed_states[:5]],
            "performance_drop": [
                x["name"]
                for x in changed_states
                if str(x.get("current_state", "")).startswith("REJECTED")
            ][:5],
            "superou_baseline": len(baseline_superou),
            "deserve_promotion": promoted,
            "should_archive": archived,
            "operational_bug": "NAO" if not [] else "SIM",
        },
    }

    return (
        {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "campaigns_considered": len(campaigns),
            "rows": leaderboard_rows,
        },
        {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "campaigns_considered": len(campaigns),
            "rows": family_rows,
        },
        {
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "campaigns_considered": len(campaigns),
            "rows": context_rows,
        },
        executive,
    )


def write_outputs(
    leaderboard: dict[str, Any],
    family_ranking: dict[str, Any],
    context_ranking: dict[str, Any],
    executive: dict[str, Any],
) -> dict[str, str]:
    timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    leaderboard_path = RESULTS_DIR / "leaderboard_permanente.json"
    family_path = RESULTS_DIR / "ranking_familias_operacional.json"
    context_path = RESULTS_DIR / "ranking_contexto_operacional.json"
    executive_json_path = RESULTS_DIR / f"relatorio_executivo_operacional_{timestamp}.json"
    executive_md_path = RESULTS_DIR / f"relatorio_executivo_operacional_{timestamp}.md"

    leaderboard_path.write_text(json.dumps(leaderboard, indent=2, ensure_ascii=False), encoding="utf-8")
    family_path.write_text(json.dumps(family_ranking, indent=2, ensure_ascii=False), encoding="utf-8")
    context_path.write_text(json.dumps(context_ranking, indent=2, ensure_ascii=False), encoding="utf-8")
    executive_json_path.write_text(json.dumps(executive, indent=2, ensure_ascii=False), encoding="utf-8")

    lines: list[str] = []
    lines.append("# Relatorio Executivo Operacional")
    lines.append("")
    lines.append(f"- Gerado em: {executive.get('generated_at')}")
    lines.append(f"- Campanha atual: {executive.get('latest_campaign_file')}")
    lines.append(f"- Campanha anterior: {executive.get('previous_campaign_file')}")
    lines.append("")
    lines.append("## Top 10 Estrategias")
    for row in executive.get("top_10_strategies", []):
        lines.append(
            f"- {row.get('ranking_position')}. {row.get('name')} | familia={row.get('family')} | score={row.get('ranking_score')} | state={max((row.get('states_counter') or {}).items(), key=lambda x: x[1])[0] if row.get('states_counter') else 'N/A'}"
        )
    lines.append("")
    lines.append("## Top 5 Familias")
    for row in executive.get("top_5_families", []):
        lines.append(
            f"- {row.get('family')} | estrategias={row.get('strategies')} | pf_medio={row.get('profit_factor_mean')} | sharpe_medio={row.get('sharpe_mean')} | taxa_paper_candidate={row.get('paper_candidate_rate')}"
        )
    lines.append("")
    lines.append(f"## Estrategias Promovidas ({len(executive.get('promoted_strategies', []))})")
    for name in executive.get("promoted_strategies", []):
        lines.append(f"- {name}")
    lines.append("")
    lines.append(f"## Estrategias Arquivadas ({len(executive.get('archived_strategies', []))})")
    for name in executive.get("archived_strategies", []):
        lines.append(f"- {name}")
    lines.append("")
    lines.append(f"## Estrategias em Paper Experimental ({len(executive.get('paper_experimental_strategies', []))})")
    for name in executive.get("paper_experimental_strategies", []):
        lines.append(f"- {name}")
    lines.append("")
    lines.append("## Comparacao com Baseline")
    lines.append(f"- Quantas superaram baseline: {executive.get('baseline_superou_count')}")
    if executive.get("baseline_superou_strategies"):
        lines.append("- Estrategias:")
        for name in executive.get("baseline_superou_strategies", []):
            lines.append(f"  - {name}")
    lines.append("")
    lines.append("## Evolucao vs Campanha Anterior")
    lines.append(f"- Mudancas de classificacao: {executive.get('changed_vs_previous_count')}")
    for row in executive.get("changed_vs_previous", [])[:20]:
        lines.append(
            f"- {row.get('name')} | {row.get('previous_state')} -> {row.get('current_state')} | familia={row.get('family')}"
        )
    lines.append("")
    lines.append("## Principais Motivos de Rejeicao")
    for reason, count in executive.get("top_rejection_reasons", [])[:10]:
        lines.append(f"- {reason}: {count}")
    lines.append("")
    lines.append("## Principais Aprendizados")
    lines.append("- Operacao em modo continuo sem alteracao dos criterios cientificos.")
    lines.append("- Leaderboard permanente atualizado com historico acumulado.")
    lines.append("- Ranking de familias e contexto atualizado para direcionar pesquisa futura.")

    executive_md_path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "leaderboard": str(leaderboard_path),
        "family_ranking": str(family_path),
        "context_ranking": str(context_path),
        "executive_json": str(executive_json_path),
        "executive_md": str(executive_md_path),
    }


def main() -> None:
    campaigns = _load_phase13_campaigns()
    if not campaigns:
        raise SystemExit("Nenhuma campanha Fase 13 encontrada em optimization/results.")

    leaderboard, family_ranking, context_ranking, executive = build_leaderboard(campaigns)
    outputs = write_outputs(leaderboard, family_ranking, context_ranking, executive)

    print("LABORATORIO OPERACIONAL CONTINUO - OK")
    print(f"Campanhas consideradas: {len(campaigns)}")
    for key, value in outputs.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
