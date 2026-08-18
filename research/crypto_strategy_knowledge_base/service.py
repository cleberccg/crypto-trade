from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CryptoStrategyResearchConfig:
    output_prefix: str = "crypto_strategy_research"


class CryptoStrategyKnowledgeBaseService:
    """FASE 11 - permanent knowledge curation for crypto strategy research."""

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = base_dir
        self._kb_dir = self._base_dir / "research" / "crypto_strategy_knowledge_base"
        self._results_dir = self._base_dir / "optimization" / "results"
        self._results_dir.mkdir(parents=True, exist_ok=True)

    def run(self, cfg: CryptoStrategyResearchConfig) -> dict[str, Any]:
        strategies = self._load_entries()
        enriched = [self._enrich(s) for s in strategies]
        ranked = sorted(enriched, key=lambda s: float(s["score_total"]), reverse=True)
        for idx, row in enumerate(ranked, start=1):
            row["rank"] = idx

        top20 = ranked[:20]
        immediate = [s for s in ranked if bool(s["can_implement_immediately"]) is True]
        requires_changes = [s for s in ranked if bool(s["can_implement_immediately"]) is False]

        top5 = self._select_top5(ranked)

        report = {
            "phase": "11",
            "generated_at": datetime.now(tz=timezone.utc).isoformat(),
            "total_strategies_researched": len(ranked),
            "ranking": ranked,
            "top20": top20,
            "compatible_immediate": immediate,
            "requires_changes": requires_changes,
            "recommended_next_batch_5": top5,
            "sources_scope": {
                "github_projects": ["Freqtrade", "Lean/QuantConnect", "Backtrader", "Jesse", "Hummingbot"],
                "tradingview_families": [
                    "SuperTrend",
                    "WaveTrend",
                    "QQE",
                    "AlphaTrend",
                    "UT Bot",
                    "HalfTrend",
                    "SSL Hybrid",
                    "Chandelier Exit",
                    "Hull Suite",
                    "Elder Impulse",
                    "Nadaraya-Watson",
                    "VIDYA",
                    "KAMA",
                    "TTM Squeeze"
                ]
            },
            "recommendation": {
                "summary": "Selecionar 5 estrategias para proximo lote de implementacao sem alterar arquitetura.",
                "top_5": [x["name"] for x in top5],
            },
        }

        outputs = self._write_outputs(cfg.output_prefix, report)
        self._write_permanent_doc(report)

        summary = {
            "status": "completed",
            "phase": "11",
            "total_strategies_researched": len(ranked),
            "top_strategy": ranked[0]["name"] if ranked else None,
            "top_5_recommended": [x["name"] for x in top5],
            "compatible_immediate_count": len(immediate),
            "requires_changes_count": len(requires_changes),
        }
        return {"summary": summary, "report": report, "outputs": outputs}

    def _load_entries(self) -> list[dict[str, Any]]:
        path = self._kb_dir / "strategies.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def _enrich(self, item: dict[str, Any]) -> dict[str, Any]:
        score = self._score(item)
        priority = "Alta" if score >= 78.0 else ("Media" if score >= 62.0 else "Baixa")

        can_implement_immediately = (
            not bool(item.get("architecture_change_required", False))
            and not bool(item.get("needs_new_data", False))
            and int(item.get("platform_compatibility", 0)) >= 4
        )

        enriched = dict(item)
        enriched["score_total"] = round(score, 2)
        enriched["priority"] = priority
        enriched["can_implement_immediately"] = can_implement_immediately
        return enriched

    def _score(self, item: dict[str, Any]) -> float:
        robustness = float(item.get("robustness", 0))
        popularity = float(item.get("popularity", 0))
        public_impl = float(item.get("public_implementations_score", 0))
        ease = float(item.get("ease_of_implementation", 0))
        compatibility = float(item.get("platform_compatibility", 0))
        crypto_potential = float(item.get("crypto_potential", 0))
        overfit_risk = float(item.get("overfitting_risk", 0))

        normalized = (
            robustness * 0.17
            + popularity * 0.18
            + public_impl * 0.14
            + ease * 0.16
            + compatibility * 0.17
            + crypto_potential * 0.18
            - overfit_risk * 0.10
        )
        return max(0.0, min(100.0, normalized / 5.0 * 100.0))

    def _select_top5(self, ranked: list[dict[str, Any]]) -> list[dict[str, Any]]:
        immediate = [
            x
            for x in ranked
            if x["can_implement_immediately"]
            and x["priority"] in {"Alta", "Media"}
        ]
        return immediate[:5]

    def _write_outputs(self, prefix: str, report: dict[str, Any]) -> dict[str, str]:
        stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        outputs: dict[str, str] = {}

        json_path = self._results_dir / f"{prefix}_{stamp}.json"
        json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        outputs["json"] = str(json_path)

        csv_path = self._results_dir / f"{prefix}_{stamp}_ranking.csv"
        rows = report.get("ranking", [])
        if rows:
            with csv_path.open("w", newline="", encoding="utf-8") as fh:
                writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "rank",
                        "name",
                        "type",
                        "category",
                        "source_kind",
                        "priority",
                        "score_total",
                        "can_implement_immediately",
                        "platform_compatibility",
                        "crypto_potential",
                        "overfitting_risk",
                        "architecture_change_required",
                    ],
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(rows)
            outputs["csv"] = str(csv_path)

        md_path = self._results_dir / f"{prefix}_{stamp}.md"
        md_path.write_text(self._to_markdown(report), encoding="utf-8")
        outputs["markdown"] = str(md_path)
        return outputs

    def _write_permanent_doc(self, report: dict[str, Any]) -> None:
        path = self._base_dir / "docs" / "CRYPTO_STRATEGY_RESEARCH.md"
        path.write_text(self._to_markdown(report), encoding="utf-8")

    def _to_markdown(self, report: dict[str, Any]) -> str:
        lines = [
            "# CRYPTO_STRATEGY_RESEARCH",
            "",
            "Base permanente de pesquisa e curadoria cientifica de estrategias para criptomoedas (FASE 11).",
            "",
            f"- Total pesquisado: **{report.get('total_strategies_researched', 0)}**",
            f"- Top 5 recomendado para proximo lote: **{', '.join(report.get('recommendation', {}).get('top_5', []))}**",
            "",
            "## Top 20 mais promissoras",
            "",
            "| Rank | Estrategia | Tipo | Prioridade | Score | Implementacao imediata | Compatibilidade | Potencial cripto |",
            "|---|---|---|---|---:|---|---:|---:|",
        ]
        for row in report.get("top20", []):
            lines.append(
                f"| {row.get('rank')} | {row.get('name')} | {row.get('type')} | {row.get('priority')} | "
                f"{float(row.get('score_total', 0.0)):.2f} | {'SIM' if row.get('can_implement_immediately') else 'NAO'} | "
                f"{row.get('platform_compatibility')} | {row.get('crypto_potential')} |"
            )

        lines += [
            "",
            "## Estrategias que podem ser implementadas imediatamente",
            "",
        ]
        for row in report.get("compatible_immediate", []):
            lines.append(f"- {row.get('name')} ({row.get('priority')}, score={row.get('score_total')})")

        lines += [
            "",
            "## Estrategias que exigem alteracoes na plataforma",
            "",
        ]
        for row in report.get("requires_changes", []):
            lines.append(f"- {row.get('name')}: {row.get('missing_requirements')}")

        lines += [
            "",
            "## Proximo lote recomendado (5 estrategias)",
            "",
        ]
        for row in report.get("recommended_next_batch_5", []):
            lines.append(
                f"1. {row.get('name')} - score={row.get('score_total')} - justificativa: alta adocao, "
                f"compatibilidade={row.get('platform_compatibility')}, potencial_cripto={row.get('crypto_potential')}."
            )

        lines += [
            "",
            "## Fontes consideradas",
            "",
            "- GitHub: Freqtrade, Lean/QuantConnect, Backtrader, Jesse, Hummingbot",
            "- TradingView: familias de indicadores e estrategias publicas de alta adocao",
            "- Literatura tecnica e documentacao quant publica",
            "",
        ]
        return "\n".join(lines)
