from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib import error, request

import performance_analytics
import robustness_analytics


@dataclass(frozen=True)
class ExternalStrategyValidationConfig:
    edge01_report_file: str
    knowledge_base_file: str | None = None
    edge_discovery_file: str | None = None
    min_external_candidates: int = 5
    max_external_candidates: int = 10
    enable_web_research: bool = True
    strict_web_filters: bool = False
    min_repo_stars: int = 25
    max_inactive_days: int = 180
    reject_forks: bool = True
    require_readme: bool = True
    output_prefix: str = "edge_external_validation_lab"


def _safe_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed:
        return None
    return parsed


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _classification_from_category(category: str) -> str:
    raw = str(category or "").lower()
    if "trend" in raw:
        return "Trend Following"
    if "breakout" in raw:
        return "Breakout"
    if "reversion" in raw or "revers" in raw:
        return "Mean Reversion"
    if "momentum" in raw:
        return "Momentum"
    if "scalp" in raw:
        return "Scalping"
    if "grid" in raw:
        return "Grid"
    return "Outros"


def _compatibility_effort(platform_compatibility: Any) -> str:
    score = int(platform_compatibility or 0)
    if score >= 4:
        return "baixa"
    if score >= 3:
        return "media"
    return "alta"


def _paper_status(metrics: dict[str, Any]) -> tuple[str, list[str]]:
    reasons: list[str] = []

    pf = float(metrics.get("profit_factor") or 0.0)
    sharpe = float(metrics.get("sharpe") or 0.0)
    expectancy = float(metrics.get("expectancy") or 0.0)
    net_return = float(metrics.get("return_pct") or 0.0)
    drawdown = float(metrics.get("drawdown_pct") or 1.0)
    asset_robustness = float(metrics.get("asset_robustness") or 0.0)
    timeframe_robustness = float(metrics.get("timeframe_robustness") or 0.0)
    rolling_oos_consistency = float(metrics.get("rolling_oos_consistency") or 0.0)
    paper_experimental_count = int(metrics.get("paper_experimental_count") or 0)

    if pf <= 1.15:
        reasons.append("profit_factor_below_threshold")
    if sharpe <= 0.0:
        reasons.append("sharpe_not_positive")
    if expectancy <= 0.0:
        reasons.append("expectancy_not_positive")
    if net_return <= 0.0:
        reasons.append("net_return_not_positive")
    if drawdown > 0.20:
        reasons.append("drawdown_not_controlled")
    if asset_robustness < 0.50:
        reasons.append("asset_robustness_below_threshold")
    if timeframe_robustness < 0.50:
        reasons.append("timeframe_robustness_below_threshold")
    if rolling_oos_consistency < 65.0:
        reasons.append("rolling_oos_inconsistent")
    if paper_experimental_count <= 0:
        reasons.append("paper_experimental_not_observed")

    if not reasons:
        return "PAPER_APPROVED", []

    core_pass = pf > 1.15 and sharpe > 0.0 and expectancy > 0.0 and net_return > 0.0
    if core_pass and drawdown <= 0.20:
        return "PAPER_CANDIDATE", reasons
    if pf > 1.0 and sharpe >= 0.0:
        return "PROMISSORA", reasons
    return "REPROVADA", reasons


def _status_reason(status: str, reasons: list[str]) -> str:
    normalized = str(status or "").upper()
    if normalized == "PAPER_APPROVED_UNIVERSAL":
        return "Atendeu todos os criterios (robustez universal)"
    if normalized == "PAPER_APPROVED_SPECIALIZED":
        return "Aprovada em paper no escopo especializado"
    if normalized == "PAPER_APPROVED":
        return "Atendeu todos os criterios"
    if normalized == "PAPER_CANDIDATE":
        return "Aguardando paper prolongado"
    if normalized == "PROMISSORA":
        if reasons:
            return f"Parcialmente elegivel, porem: {', '.join(reasons[:2])}"
        return "Parcialmente elegivel"
    if reasons:
        return ", ".join(reasons[:3])
    return "Reprovada por criterio cientifico"


def _extract_owner_repo(url: str) -> tuple[str, str] | None:
    match = re.search(r"github\.com/([^/]+)/([^/#?]+)", str(url or ""), flags=re.IGNORECASE)
    if not match:
        return None
    owner = str(match.group(1)).strip()
    repo = str(match.group(2)).strip().replace(".git", "")
    if not owner or not repo:
        return None
    return owner, repo


def _github_get_json(api_url: str, timeout: float = 8.0) -> dict[str, Any] | None:
    req = request.Request(api_url, headers={"Accept": "application/vnd.github+json", "User-Agent": "crypto-lab-edge-validator"})
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict):
                return payload
            return None
    except (error.HTTPError, error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _parse_iso_utc(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _confidence_score(
    *,
    perf: dict[str, Any],
    robust: dict[str, Any],
    profile: dict[str, Any],
    metrics: dict[str, Any],
) -> tuple[float, dict[str, float]]:
    trade_proxy = float(profile.get("regime_rows_tested") or profile.get("contexts_tested") or 0.0)
    trade_component = min(1.0, max(0.0, trade_proxy / 60.0))

    oos_windows = float(perf.get("number_of_campaigns") or robust.get("number_of_campaigns") or 0.0)
    oos_component = min(1.0, max(0.0, oos_windows / 20.0))

    asset_stability = float(metrics.get("asset_robustness") or 0.0)
    timeframe_stability = float(metrics.get("timeframe_robustness") or 0.0)
    stability_component = min(1.0, max(0.0, (asset_stability + timeframe_stability) / 2.0))

    mean_pf = _safe_float(robust.get("mean_profit_factor"))
    std_pf = _safe_float(robust.get("std_profit_factor"))
    mean_sharpe = _safe_float(robust.get("mean_sharpe"))
    std_sharpe = _safe_float(robust.get("std_sharpe"))
    if mean_pf is None:
        mean_pf = _safe_float(perf.get("mean_profit_factor"))
    if mean_sharpe is None:
        mean_sharpe = _safe_float(perf.get("mean_sharpe"))

    if mean_pf is not None and std_pf is not None and mean_sharpe is not None and std_sharpe is not None:
        pf_cv = abs(std_pf) / max(abs(mean_pf), 0.2)
        sharpe_cv = abs(std_sharpe) / max(abs(mean_sharpe), 0.2)
        sensitivity_component = 1.0 - min(1.0, (0.70 * pf_cv) + (0.30 * sharpe_cv))
    else:
        sensitivity_component = 0.50

    confidence = 100.0 * (
        0.25 * trade_component
        + 0.20 * oos_component
        + 0.30 * stability_component
        + 0.25 * max(0.0, sensitivity_component)
    )
    breakdown = {
        "trade_component": round(trade_component, 6),
        "oos_component": round(oos_component, 6),
        "stability_component": round(stability_component, 6),
        "sensitivity_component": round(max(0.0, sensitivity_component), 6),
    }
    return round(confidence, 4), breakdown


class EdgeExternalValidationLabService:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: ExternalStrategyValidationConfig) -> dict[str, Any]:
        edge01_report_path = Path(cfg.edge01_report_file)
        edge01_report = self._load_json(edge01_report_path)
        edge01_gate = self._validate_edge01_artifacts(edge01_report, edge01_report_path)
        if not edge01_gate["passed"]:
            raise RuntimeError("EDGE-01 gate failed: required artifacts are missing.")

        kb_report = self._load_knowledge_base(cfg.knowledge_base_file)
        edge_discovery_report = self._load_edge_discovery(cfg.edge_discovery_file)

        campaigns = robustness_analytics._load_phase13_campaigns()
        if not campaigns:
            raise RuntimeError("No FASE 13 campaigns found for external validation.")

        latest_backlog = [
            row
            for row in campaigns[-1].payload.get("backlog", [])
            if isinstance(row, dict)
        ]

        perf_rows = performance_analytics._build_strategy_history(performance_analytics._load_phase13_campaigns()).get("rows", [])
        robust_rows = robustness_analytics._build_strategy_robustness(campaigns).get("rows", [])
        profile_rows = edge_discovery_report.get("profiles", []) if isinstance(edge_discovery_report.get("profiles"), list) else []

        perf_map = {str(row.get("strategy")): row for row in perf_rows if isinstance(row, dict)}
        robust_map = {str(row.get("strategy")): row for row in robust_rows if isinstance(row, dict)}
        profile_map = {str(row.get("name")): row for row in profile_rows if isinstance(row, dict)}

        selected_external, selection_details = self._select_external_candidates(kb_report, latest_backlog, cfg)

        external_reports = [
            self._evaluate_external_strategy(item, perf_map, robust_map, profile_map)
            for item in selected_external
        ]

        internal_rows = self._collect_internal_baseline(edge01_report, profile_rows)

        consolidated = internal_rows + external_reports
        consolidated.sort(
            key=lambda row: (
                0 if str(row.get("status")) == "PAPER_APPROVED" else 1,
                -float(row.get("profit_factor") or 0.0),
                -float(row.get("sharpe") or 0.0),
                -float(row.get("rolling_oos_consistency") or 0.0),
                -float(row.get("confianca") or 0.0),
            )
        )
        for idx, row in enumerate(consolidated, start=1):
            row["ranking"] = idx

        approved = [row for row in consolidated if str(row.get("status")) == "PAPER_APPROVED"]
        winner = approved[0] if approved else (consolidated[0] if consolidated else None)

        executive_answer = "SIM" if approved else "NAO"
        executive = {
            "winning_strategy": winner,
            "scientific_result": {
                "has_operational_edge": executive_answer,
                "approved_count": len(approved),
                "main_rejection_reasons": self._top_rejection_reasons(consolidated),
            },
            "recommendation": (
                "Start prolonged paper trading with the winning strategy under strict monitoring."
                if approved
                else "Do not advance to real capital; close robustness gaps first."
            ),
        }

        report = {
            "generated_at": _now_iso(),
            "phase": "EDGE-02",
            "objective": "External Strategy Validation Lab",
            "edge01_transition_gate": edge01_gate,
            "selection": {
                "requested_range": [int(cfg.min_external_candidates), int(cfg.max_external_candidates)],
                "selected_count": len(selected_external),
                "selected_names": [str(item.get("name")) for item in selected_external],
                "process": selection_details,
            },
            "external_strategy_reports": external_reports,
            "internal_vs_external_comparison": consolidated,
            "ranking_consolidated": consolidated,
            "executive_report": executive,
        }

        outputs = self._write_outputs(cfg.output_prefix, report, consolidated, external_reports)

        summary = {
            "phase": "EDGE-02",
            "selected_external_count": len(selected_external),
            "paper_approved_count": len(approved),
            "scientific_answer": executive_answer,
            "winner": winner.get("strategy") if isinstance(winner, dict) else None,
        }
        return {
            "summary": summary,
            "report": report,
            "outputs": outputs,
        }

    def _load_json(self, path: Path) -> dict[str, Any]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid JSON report: {path}")
        return payload

    def _latest_json_by_prefix(self, prefix: str) -> Path:
        files = sorted(self._results_dir.glob(f"{prefix}_*.json"), key=lambda p: p.stat().st_mtime)
        if not files:
            raise RuntimeError(f"No report found for prefix: {prefix}")
        return files[-1]

    def _load_knowledge_base(self, explicit_file: str | None) -> dict[str, Any]:
        path = Path(explicit_file) if explicit_file else self._latest_json_by_prefix("crypto_strategy_research")
        return self._load_json(path)

    def _load_edge_discovery(self, explicit_file: str | None) -> dict[str, Any]:
        path = Path(explicit_file) if explicit_file else self._latest_json_by_prefix("edge_discovery_lab")
        return self._load_json(path)

    def _validate_edge01_artifacts(self, edge01_report: dict[str, Any], edge01_report_path: Path) -> dict[str, Any]:
        required = {
            "json",
            "md",
            "attribute_ranking_csv",
            "candidate_filters_csv",
            "incremental_simulation_csv",
            "sensitivity_csv",
            "trade_features_csv",
            "executive_summary_md",
        }
        outputs = edge01_report.get("outputs", {}) if isinstance(edge01_report.get("outputs"), dict) else {}
        if not outputs:
            inferred = self._infer_edge01_outputs_from_path(edge01_report_path)
            outputs = inferred

        files_present: dict[str, bool] = {}
        for key in sorted(required):
            value = outputs.get(key)
            ok = bool(value) and Path(str(value)).exists()
            files_present[key] = ok

        missing = [key for key, ok in files_present.items() if not ok]
        return {
            "passed": len(missing) == 0,
            "required_artifacts": sorted(required),
            "files_present": files_present,
            "missing_artifacts": missing,
        }

    def _infer_edge01_outputs_from_path(self, path: Path) -> dict[str, str]:
        name = path.name
        stem = path.stem
        if not name.startswith("edge_extraction_lab_"):
            return {}

        base = path.parent / stem
        return {
            "json": str(path),
            "md": str(path.with_suffix(".md")),
            "attribute_ranking_csv": str(base.with_name(base.name + "_attribute_ranking.csv")),
            "candidate_filters_csv": str(base.with_name(base.name + "_candidate_filters.csv")),
            "incremental_simulation_csv": str(base.with_name(base.name + "_incremental_simulation.csv")),
            "sensitivity_csv": str(base.with_name(base.name + "_sensitivity.csv")),
            "trade_features_csv": str(base.with_name(base.name + "_trade_features.csv")),
            "executive_summary_md": str(base.with_name(base.name + "_executive_summary.md")),
        }

    def _select_external_candidates(
        self,
        kb_report: dict[str, Any],
        latest_backlog: list[dict[str, Any]],
        cfg: ExternalStrategyValidationConfig,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        ranking = kb_report.get("ranking", []) if isinstance(kb_report.get("ranking"), list) else []
        backlog_map = {
            str(item.get("candidate_name")): item
            for item in latest_backlog
            if "open source" in str(item.get("origin", "")).lower() and item.get("candidate_name")
        }

        preselected: list[dict[str, Any]] = []
        for row in ranking:
            if not isinstance(row, dict):
                continue
            if str(row.get("source_kind", "")).lower() != "open source":
                continue
            if not bool(row.get("can_implement_immediately")):
                continue

            name = str(row.get("name") or "").strip()
            backlog_item = backlog_map.get(name)
            if not backlog_item:
                continue

            preselected.append(
                {
                    "name": name,
                    "author": str(row.get("origin") or "community"),
                    "repository": self._extract_repository(row.get("references")),
                    "framework": "Lab Native / Freqtrade-compatible",
                    "indicators": row.get("indicators") if isinstance(row.get("indicators"), list) else [],
                    "operational_logic": str(row.get("category") or "Unknown"),
                    "last_update": "unknown",
                    "popularity": row.get("popularity"),
                    "classification": _classification_from_category(str(row.get("category") or "")),
                    "compatibility_effort": _compatibility_effort(row.get("platform_compatibility")),
                    "platform_strategy_name": str(backlog_item.get("platform_strategy_name") or name),
                    "implementation_mode": str(backlog_item.get("implementation_mode") or "unknown"),
                }
            )

            if len(preselected) >= max(int(cfg.max_external_candidates), int(cfg.min_external_candidates)) * 3:
                break

        selected: list[dict[str, Any]] = []
        eliminated: list[dict[str, Any]] = []

        for item in preselected:
            if cfg.enable_web_research:
                health = self._web_repository_health(item, cfg)
                if health.get("pass"):
                    enriched = dict(item)
                    enriched.update(
                        {
                            "repository": health.get("repository_url") or item.get("repository"),
                            "last_update": health.get("last_push") or item.get("last_update"),
                            "popularity": int(health.get("stars") or item.get("popularity") or 0),
                            "community_forks": int(health.get("forks") or 0),
                            "community_watchers": int(health.get("watchers") or 0),
                            "web_screening": health,
                        }
                    )
                    selected.append(enriched)
                else:
                    eliminated.append(
                        {
                            "name": item.get("name"),
                            "repository": item.get("repository"),
                            "reason": health.get("reason", "web_screening_failed"),
                        }
                    )
            else:
                selected.append(item)

            if len(selected) >= int(cfg.max_external_candidates):
                break

        if len(selected) < int(cfg.min_external_candidates) and not cfg.strict_web_filters:
            selected_names = {str(row.get("name")) for row in selected}
            for item in preselected:
                name = str(item.get("name"))
                if name in selected_names:
                    continue
                fallback = dict(item)
                fallback["web_screening"] = {"pass": False, "reason": "fallback_due_to_minimum_quota"}
                selected.append(fallback)
                if len(selected) >= int(cfg.min_external_candidates):
                    break

        details = {
            "mode": "web_assisted" if cfg.enable_web_research else "local_only",
            "strict_web_filters": bool(cfg.strict_web_filters),
            "criteria": {
                "active_projects_only": True,
                "eliminate_abandoned": True,
                "eliminate_unmaintained_forks": bool(cfg.reject_forks),
                "require_documentation": bool(cfg.require_readme),
                "community_usage_priority": True,
                "min_repo_stars": int(cfg.min_repo_stars),
                "max_inactive_days": int(cfg.max_inactive_days),
            },
            "preselected_count": len(preselected),
            "selected_count": len(selected),
            "eliminated_count": len(eliminated),
            "eliminated": eliminated,
        }
        return selected[: int(cfg.max_external_candidates)], details

    def _web_repository_health(self, item: dict[str, Any], cfg: ExternalStrategyValidationConfig) -> dict[str, Any]:
        repo_url = str(item.get("repository") or "")
        owner_repo = _extract_owner_repo(repo_url)
        if not owner_repo:
            return {"pass": False, "reason": "repository_not_github"}

        owner, repo = owner_repo
        meta = _github_get_json(f"https://api.github.com/repos/{owner}/{repo}")
        if not meta:
            return {"pass": False, "reason": "github_metadata_unavailable"}

        archived = bool(meta.get("archived"))
        fork = bool(meta.get("fork"))
        stars = _safe_int(meta.get("stargazers_count"), 0)
        forks = _safe_int(meta.get("forks_count"), 0)
        watchers = _safe_int(meta.get("subscribers_count"), _safe_int(meta.get("watchers_count"), 0))
        pushed_at = _parse_iso_utc(meta.get("pushed_at"))
        last_push = pushed_at.isoformat() if isinstance(pushed_at, datetime) else None
        inactive_limit = datetime.now(tz=timezone.utc) - timedelta(days=max(1, int(cfg.max_inactive_days)))
        inactive = pushed_at is None or pushed_at < inactive_limit

        if archived:
            return {"pass": False, "reason": "archived_repository"}
        if cfg.reject_forks and fork:
            return {"pass": False, "reason": "unmaintained_fork_rejected"}
        if inactive:
            return {"pass": False, "reason": "inactive_repository"}
        if stars < int(cfg.min_repo_stars):
            return {"pass": False, "reason": "low_community_adoption"}

        has_readme = True
        if cfg.require_readme:
            readme = _github_get_json(f"https://api.github.com/repos/{owner}/{repo}/readme")
            has_readme = bool(readme and readme.get("name"))
            if not has_readme:
                return {"pass": False, "reason": "missing_readme_documentation"}

        return {
            "pass": True,
            "reason": "eligible_after_web_screening",
            "repository_url": str(meta.get("html_url") or repo_url),
            "stars": stars,
            "forks": forks,
            "watchers": watchers,
            "last_push": last_push,
            "has_readme": has_readme,
            "is_fork": fork,
            "archived": archived,
        }

    def _extract_repository(self, references: Any) -> str:
        if not isinstance(references, list):
            return "unknown"
        for value in references:
            text = str(value)
            if "github.com" in text or "freqtrade" in text:
                return text
        return str(references[0]) if references else "unknown"

    def _evaluate_external_strategy(
        self,
        item: dict[str, Any],
        perf_map: dict[str, dict[str, Any]],
        robust_map: dict[str, dict[str, Any]],
        profile_map: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        candidate_name = str(item.get("name"))
        strategy_name = str(item.get("platform_strategy_name") or candidate_name)

        perf = perf_map.get(candidate_name) or perf_map.get(strategy_name) or {}
        robust = robust_map.get(candidate_name) or robust_map.get(strategy_name) or {}
        profile = profile_map.get(candidate_name) or profile_map.get(strategy_name) or {}

        metrics = {
            "profit_factor": _safe_float(perf.get("mean_profit_factor")) or _safe_float(profile.get("profit_factor_mean")) or 0.0,
            "sharpe": _safe_float(perf.get("mean_sharpe")) or _safe_float(profile.get("sharpe_mean")) or 0.0,
            "expectancy": _safe_float(perf.get("mean_expectancy")) or _safe_float(profile.get("expectancy_mean")) or 0.0,
            "drawdown_pct": _safe_float(profile.get("drawdown_max")) or _safe_float(perf.get("mean_drawdown")) or 1.0,
            "return_pct": _safe_float(profile.get("expectancy_mean")) or _safe_float(perf.get("mean_expectancy")) or 0.0,
            "rolling_oos_consistency": _safe_float(perf.get("consistency_score")) or 0.0,
            "asset_robustness": _safe_float(profile.get("asset_robustness")) or 0.0,
            "timeframe_robustness": _safe_float(profile.get("timeframe_robustness")) or 0.0,
            "regime_robustness": _safe_float(profile.get("regime_robustness")) or 0.0,
            "paper_experimental_count": int(perf.get("paper_experimental_count") or 0),
            "robustness_score": _safe_float(robust.get("robustness_score")) or 0.0,
        }

        status, reasons = _paper_status(metrics)
        confidence, confidence_breakdown = _confidence_score(
            perf=perf,
            robust=robust,
            profile=profile,
            metrics=metrics,
        )

        return {
            **item,
            "origin_type": "Externa",
            "strategy": strategy_name,
            "estrategia": strategy_name,
            "origem": "Externa",
            "profit_factor": round(float(metrics["profit_factor"]), 6),
            "pf": round(float(metrics["profit_factor"]), 6),
            "sharpe": round(float(metrics["sharpe"]), 6),
            "expectancy": round(float(metrics["expectancy"]), 6),
            "drawdown_pct": round(float(metrics["drawdown_pct"]), 6),
            "return_pct": round(float(metrics["return_pct"]), 6),
            "rolling_oos_consistency": round(float(metrics["rolling_oos_consistency"]), 4),
            "oos": round(float(metrics["rolling_oos_consistency"]), 4),
            "asset_robustness": round(float(metrics["asset_robustness"]), 6),
            "timeframe_robustness": round(float(metrics["timeframe_robustness"]), 6),
            "regime_robustness": round(float(metrics["regime_robustness"]), 6),
            "paper_experimental_count": int(metrics["paper_experimental_count"]),
            "robustness_score": round(float(metrics["robustness_score"]), 4),
            "status": status,
            "paper": status,
            "status_universal": "PAPER_APPROVED_UNIVERSAL" if status == "PAPER_APPROVED" else None,
            "status_specialized": "PAPER_APPROVED_SPECIALIZED" if status == "PAPER_CANDIDATE" else None,
            "status_reason": _status_reason(status, reasons),
            "complexidade": str(item.get("compatibility_effort") or "media"),
            "confianca": confidence,
            "confianca_componentes": confidence_breakdown,
            "rejection_reasons": reasons,
            "pipeline_validations": {
                "backtest": True,
                "robustness": bool(metrics["robustness_score"] > 0.0),
                "cross_asset": bool(metrics["asset_robustness"] > 0.0),
                "cross_timeframe": bool(metrics["timeframe_robustness"] > 0.0),
                "rolling_oos": bool(metrics["rolling_oos_consistency"] >= 65.0),
                "paper_experimental": bool(metrics["paper_experimental_count"] > 0),
            },
        }

    def _collect_internal_baseline(self, edge01_report: dict[str, Any], profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        summary = edge01_report.get("summary", {}) if isinstance(edge01_report.get("summary"), dict) else {}
        preferred = [str(v) for v in summary.get("selected_strategies", []) if str(v)]
        profile_map = {str(row.get("name")): row for row in profiles if isinstance(row, dict)}

        rows: list[dict[str, Any]] = []
        for name in preferred:
            node = profile_map.get(name)
            if not node:
                continue
            rows.append(
                {
                    "origin_type": "Interna",
                    "strategy": name,
                    "estrategia": name,
                    "origem": "Interna",
                    "classification": "N/A",
                    "compatibility_effort": "baixa",
                    "profit_factor": float(node.get("profit_factor_mean") or 0.0),
                    "pf": float(node.get("profit_factor_mean") or 0.0),
                    "sharpe": float(node.get("sharpe_mean") or 0.0),
                    "expectancy": float(node.get("expectancy_mean") or 0.0),
                    "drawdown_pct": float(node.get("drawdown_max") or 1.0),
                    "return_pct": float(node.get("expectancy_mean") or 0.0),
                    "rolling_oos_consistency": float(node.get("consistency_score") or 0.0),
                    "oos": float(node.get("consistency_score") or 0.0),
                    "asset_robustness": float(node.get("asset_robustness") or 0.0),
                    "timeframe_robustness": float(node.get("timeframe_robustness") or 0.0),
                    "regime_robustness": float(node.get("regime_robustness") or 0.0),
                    "paper_experimental_count": 0,
                    "robustness_score": float(node.get("robustness_score") or 0.0),
                    "status": "PROMISSORA" if float(node.get("profit_factor_mean") or 0.0) > 1.15 else "REPROVADA",
                    "paper": "PROMISSORA" if float(node.get("profit_factor_mean") or 0.0) > 1.15 else "REPROVADA",
                    "status_universal": None,
                    "status_specialized": None,
                    "status_reason": (
                        "Aguardando paper prolongado"
                        if float(node.get("profit_factor_mean") or 0.0) > 1.15
                        else "Nao atingiu criterios minimos de robustez"
                    ),
                    "complexidade": "baixa",
                    "confianca": round(
                        100.0
                        * (
                            0.35 * min(1.0, max(0.0, float(node.get("contexts_tested") or 0.0) / 12.0))
                            + 0.35 * min(1.0, max(0.0, (float(node.get("asset_robustness") or 0.0) + float(node.get("timeframe_robustness") or 0.0)) / 2.0))
                            + 0.30 * min(1.0, max(0.0, float(node.get("consistency_score") or 0.0) / 100.0))
                        ),
                        4,
                    ),
                    "confianca_componentes": {
                        "trade_component": round(min(1.0, max(0.0, float(node.get("contexts_tested") or 0.0) / 12.0)), 6),
                        "oos_component": round(min(1.0, max(0.0, float(node.get("consistency_score") or 0.0) / 100.0)), 6),
                        "stability_component": round(min(1.0, max(0.0, (float(node.get("asset_robustness") or 0.0) + float(node.get("timeframe_robustness") or 0.0)) / 2.0)), 6),
                        "sensitivity_component": 0.5,
                    },
                    "rejection_reasons": [],
                    "pipeline_validations": {},
                }
            )
        return rows

    def _top_rejection_reasons(self, rows: list[dict[str, Any]]) -> list[str]:
        counts: dict[str, int] = {}
        for row in rows:
            reasons = row.get("rejection_reasons", []) if isinstance(row.get("rejection_reasons"), list) else []
            for reason in reasons:
                key = str(reason)
                counts[key] = counts.get(key, 0) + 1
        ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
        return [name for name, _ in ordered[:8]]

    def _write_outputs(
        self,
        output_prefix: str,
        report: dict[str, Any],
        consolidated: list[dict[str, Any]],
        external: list[dict[str, Any]],
    ) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        json_path = self._results_dir / f"{output_prefix}_{stamp}.json"
        md_path = self._results_dir / f"{output_prefix}_{stamp}.md"
        consolidated_csv = self._results_dir / f"{output_prefix}_{stamp}_consolidated.csv"
        comparison_csv = self._results_dir / f"{output_prefix}_{stamp}_internal_vs_external.csv"
        ranking_csv = self._results_dir / f"{output_prefix}_{stamp}_ranking_confianca.csv"

        json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        self._write_csv(consolidated_csv, consolidated)
        self._write_csv(comparison_csv, consolidated)
        self._write_csv(
            ranking_csv,
            [
                {
                    "ranking": row.get("ranking"),
                    "estrategia": row.get("estrategia") or row.get("strategy"),
                    "origem": row.get("origem") or row.get("origin_type"),
                    "status": row.get("status"),
                    "motivo": row.get("status_reason"),
                    "pf": row.get("pf") if row.get("pf") is not None else row.get("profit_factor"),
                    "oos": row.get("oos") if row.get("oos") is not None else row.get("rolling_oos_consistency"),
                    "paper": row.get("paper") if row.get("paper") is not None else row.get("status"),
                    "complexidade": row.get("complexidade") if row.get("complexidade") is not None else row.get("compatibility_effort"),
                    "confianca": row.get("confianca"),
                }
                for row in consolidated
            ],
        )

        technical_reports: dict[str, str] = {}
        for row in external:
            slug = str(row.get("strategy", "external")).lower().replace(" ", "_").replace("/", "_")
            path = self._results_dir / f"{output_prefix}_{stamp}_strategy_{slug}.md"
            lines = [
                f"# External Strategy Technical Report - {row.get('strategy')}",
                "",
                f"- Origin: {row.get('origin_type')}",
                f"- Classification: {row.get('classification')}",
                f"- Compatibility effort: {row.get('compatibility_effort')}",
                f"- Status: {row.get('status')}",
                f"- Profit Factor: {row.get('profit_factor')}",
                f"- Sharpe: {row.get('sharpe')}",
                f"- Expectancy: {row.get('expectancy')}",
                f"- Drawdown: {row.get('drawdown_pct')}",
                f"- Rolling OOS consistency: {row.get('rolling_oos_consistency')}",
                "",
                "## Rejection Reasons",
            ]
            reasons = row.get("rejection_reasons", []) if isinstance(row.get("rejection_reasons"), list) else []
            if reasons:
                for reason in reasons:
                    lines.append(f"- {reason}")
            else:
                lines.append("- none")
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            technical_reports[str(row.get("strategy"))] = str(path)

        return {
            "json_consolidated": str(json_path),
            "markdown_consolidated": str(md_path),
            "csv_consolidated": str(consolidated_csv),
            "internal_vs_external_csv": str(comparison_csv),
            "ranking_confianca_csv": str(ranking_csv),
            "technical_reports": json.dumps(technical_reports, ensure_ascii=False),
        }

    def _write_csv(self, path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        fieldnames = sorted({key for row in rows for key in row.keys()})
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    def _to_markdown(self, report: dict[str, Any]) -> str:
        selection = report.get("selection", {}) if isinstance(report.get("selection"), dict) else {}
        ranking = report.get("ranking_consolidated", []) if isinstance(report.get("ranking_consolidated"), list) else []
        executive = report.get("executive_report", {}) if isinstance(report.get("executive_report"), dict) else {}
        science = executive.get("scientific_result", {}) if isinstance(executive.get("scientific_result"), dict) else {}

        lines = [
            "# EDGE-02 - External Strategy Validation Lab",
            "",
            f"- Generated at: {report.get('generated_at')}",
            f"- Selected external strategies: {selection.get('selected_count', 0)}",
            f"- Selection mode: {(selection.get('process') or {}).get('mode', 'unknown')}",
            "",
            "## Ranking Consolidado",
        ]
        for row in ranking:
            lines.append(
                f"- #{row.get('ranking')} {row.get('strategy')} ({row.get('origin_type')}) | "
                f"status={row.get('status')} | PF={row.get('profit_factor')} | OOS={row.get('rolling_oos_consistency')} | "
                f"Complexidade={row.get('complexidade', row.get('compatibility_effort'))} | Confianca={row.get('confianca')} | "
                f"Motivo={row.get('status_reason')}"
            )

        process = selection.get("process", {}) if isinstance(selection.get("process"), dict) else {}
        eliminated = process.get("eliminated", []) if isinstance(process.get("eliminated"), list) else []
        lines.extend(["", "## Selecao Externa (Web + Criterios Objetivos)"])
        lines.append(f"- Preselecionadas: {process.get('preselected_count', 0)}")
        lines.append(f"- Eliminadas: {process.get('eliminated_count', 0)}")
        if eliminated:
            lines.append("- Principais eliminacoes:")
            for row in eliminated[:8]:
                lines.append(f"  - {row.get('name')}: {row.get('reason')}")

        lines.extend(
            [
                "",
                "## Resultado Cientifico",
                f"- Existe edge operacional suficiente? {science.get('has_operational_edge', 'NAO')}",
                f"- Estrategias aprovadas: {science.get('approved_count', 0)}",
                f"- Vencedora: {(executive.get('winning_strategy') or {}).get('strategy') if isinstance(executive.get('winning_strategy'), dict) else 'none'}",
                f"- Recomendacao: {executive.get('recommendation')}",
            ]
        )
        return "\n".join(lines) + "\n"
