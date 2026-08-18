from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config.settings import settings

ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "optimization" / "results"


def _latest_phase13_report() -> Path:
    files = sorted(RESULTS_DIR.glob("phase13_continuous_strategy_factory_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError("No phase13_continuous_strategy_factory report found")
    return files[-1]


def _has_event(item: dict[str, Any], prefix: str) -> bool:
    for ev in item.get("history", []):
        if str(ev.get("event", "")).startswith(prefix):
            return True
    return False


def _bool_mark(value: bool) -> str:
    return "SIM" if value else "NAO"


def _parse_rr_reason(text: str) -> tuple[float | None, float | None]:
    m = re.search(r"Risk/reward ratio\s+([0-9]+\.[0-9]+)\s+is below.*minimum of\s+([0-9]+\.[0-9]+)", text)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def _rule_evidence(item: dict[str, Any], symbol: str, timeframe: str) -> dict[str, Any]:
    state = str(item.get("state", ""))
    reason = str(item.get("state_reason", ""))
    early = item.get("early_stop") if isinstance(item.get("early_stop"), dict) else {}
    backtest = item.get("backtest") if isinstance(item.get("backtest"), dict) else {}

    if state == "REJECTED_BY_PERFORMANCE" and early.get("triggered"):
        reasons = [str(x) for x in early.get("reasons", [])]
        if "profit_factor_too_low" in reasons:
            observed = float(backtest.get("profit_factor", 0.0))
            minimum = max(0.30, float(settings.validation.min_profit_factor) * 0.5)
            diff_pct = ((observed - minimum) / minimum) * 100.0 if minimum > 0 else None
            return {
                "motivo": "profit_factor_too_low",
                "regra": "if pf < max(0.30, settings.validation.min_profit_factor * 0.5)",
                "arquivo": "research/services/phase13_continuous_strategy_factory.py",
                "linha": 611,
                "valor_observado": observed,
                "valor_minimo": minimum,
                "diferenca_percentual": diff_pct,
                "contexto": f"{symbol} {timeframe}",
            }
        if "no_trades" in reasons:
            observed = float(backtest.get("number_of_trades", 0.0))
            minimum = 1.0
            diff_pct = ((observed - minimum) / minimum) * 100.0
            return {
                "motivo": "no_trades",
                "regra": "if trades <= 0",
                "arquivo": "research/services/phase13_continuous_strategy_factory.py",
                "linha": 609,
                "valor_observado": observed,
                "valor_minimo": minimum,
                "diferenca_percentual": diff_pct,
                "contexto": f"{symbol} {timeframe}",
            }
        if "negative_profile" in reasons:
            return {
                "motivo": "negative_profile",
                "regra": "if expectancy < 0 and sharpe < 0 and pf < 1.0",
                "arquivo": "research/services/phase13_continuous_strategy_factory.py",
                "linha": 613,
                "valor_observado": {
                    "expectancy": float(backtest.get("expectancy", 0.0)),
                    "sharpe": float(backtest.get("sharpe", 0.0)),
                    "profit_factor": float(backtest.get("profit_factor", 0.0)),
                },
                "valor_minimo": {
                    "expectancy": 0.0,
                    "sharpe": 0.0,
                    "profit_factor": 1.0,
                },
                "diferenca_percentual": None,
                "contexto": f"{symbol} {timeframe}",
            }

    if "risk_error:" in reason and "Risk/reward ratio" in reason:
        rr_obs, rr_min = _parse_rr_reason(reason)
        diff_pct = None
        if rr_obs is not None and rr_min is not None and rr_min > 0:
            diff_pct = ((rr_obs - rr_min) / rr_min) * 100.0
        return {
            "motivo": "risk_manager_rr",
            "regra": "if rr_ratio < cfg.risk_reward_ratio",
            "arquivo": "risk/risk_manager.py",
            "linha": 192,
            "valor_observado": rr_obs,
            "valor_minimo": rr_min,
            "diferenca_percentual": diff_pct,
            "contexto": f"{symbol} {timeframe}",
        }

    if reason == "no_market_data":
        return {
            "motivo": "infra_no_market_data",
            "regra": "if market_df.empty",
            "arquivo": "research/services/phase13_continuous_strategy_factory.py",
            "linha": 223,
            "valor_observado": 0,
            "valor_minimo": 1,
            "diferenca_percentual": -100.0,
            "contexto": f"{symbol} {timeframe}",
        }

    if reason == "NOT_IMPLEMENTED_IN_PLATFORM":
        return {
            "motivo": "implementation_missing",
            "regra": "return ... NOT_IMPLEMENTED_IN_PLATFORM",
            "arquivo": "research/services/phase13_continuous_strategy_factory.py",
            "linha": 495,
            "valor_observado": 0,
            "valor_minimo": 1,
            "diferenca_percentual": -100.0,
            "contexto": f"{symbol} {timeframe}",
        }

    return {
        "motivo": "outros",
        "regra": "state transition without explicit mapped metric",
        "arquivo": "research/services/phase13_continuous_strategy_factory.py",
        "linha": 190,
        "valor_observado": None,
        "valor_minimo": None,
        "diferenca_percentual": None,
        "contexto": f"{symbol} {timeframe}",
    }


def _category_from_motivo(motivo: str) -> str:
    if motivo in {"profit_factor_too_low"}:
        return "Profit Factor"
    if motivo in {"negative_profile"}:
        return "Expectancy/Sharpe"
    if motivo in {"no_trades"}:
        return "Numero insuficiente de trades"
    if motivo in {"risk_manager_rr"}:
        return "Risk Manager"
    if motivo in {"infra_no_market_data"}:
        return "Infraestrutura"
    if motivo in {"implementation_missing"}:
        return "Erro de implementacao"
    return "Outros"


def _contrafactual(item: dict[str, Any], evidence: dict[str, Any]) -> tuple[str, str]:
    motivo = str(evidence.get("motivo", ""))
    if motivo == "no_trades":
        return "Muito improvavel", "Sem trades no backtest; otimizar parametros tende a nao criar edge sem sinal base"
    if motivo == "profit_factor_too_low":
        observed = evidence.get("valor_observado")
        minimum = evidence.get("valor_minimo")
        if isinstance(observed, float) and isinstance(minimum, float) and minimum > 0:
            gap = (minimum - observed) / minimum
            if gap <= 0.15:
                return "Possivel", "Profit factor proximo ao gate; uma busca rapida poderia melhorar marginalmente"
            return "Muito improvavel", "Profit factor muito abaixo do gate de early stop"
    if motivo == "negative_profile":
        return "Muito improvavel", "Perfil simultaneamente negativo em expectancy, sharpe e PF"
    if motivo == "risk_manager_rr":
        diff = evidence.get("diferenca_percentual")
        if isinstance(diff, float) and diff >= -2.0:
            return "Possivel", "Razao risco-retorno muito proxima do limite minimo"
        return "Muito improvavel", "Razao risco-retorno distante do limite minimo"
    if motivo in {"infra_no_market_data", "implementation_missing"}:
        return "Possivel", "Bloqueio estrutural; com dados/implementacao disponiveis poderia avancar"
    return "Possivel", "Dados insuficientes para inferencia forte"


def run() -> dict[str, Any]:
    report_path = _latest_phase13_report()
    data = json.loads(report_path.read_text(encoding="utf-8"))

    backlog = data.get("backlog", [])
    symbol = str(data.get("symbol", "BTC/USDT"))
    timeframe = str(data.get("timeframe", "5m"))

    implemented = [x for x in backlog if _has_event(x, "implementation:completed")]

    rows: list[dict[str, Any]] = []
    eliminadas_pre_optimizer: list[dict[str, Any]] = []

    for item in implemented:
        name = str(item.get("candidate_name", ""))
        state = str(item.get("state", ""))
        family = str(item.get("family", ""))
        indicators = item.get("indicators", [])

        smoke = bool(item.get("smoke")) or _has_event(item, "smoke:")
        backtest = bool(item.get("backtest")) or _has_event(item, "backtest:")
        early = bool(item.get("early_stop")) or _has_event(item, "early_stop:")
        optimizer = bool(item.get("optimizer")) or _has_event(item, "optimizer:")
        validation = bool(item.get("validation")) or _has_event(item, "validation:")
        paper = bool(item.get("paper_trading")) or _has_event(item, "paper_qualification:") or state in {"in_paper_trading", "approved"}

        eliminated_pre = (not optimizer) and state in {
            "REJECTED_BY_PERFORMANCE",
            "REJECTED_BY_INFRASTRUCTURE",
            "SMOKE_FAILED",
            "BACKTEST_FAILED",
            "INCONCLUSIVE",
            "INCONCLUSIVE_RESOURCE_LIMIT",
        }

        row = {
            "strategy": name,
            "family": family,
            "indicators": ", ".join([str(x) for x in indicators]),
            "asset": symbol,
            "timeframe": timeframe,
            "smoke": _bool_mark(smoke),
            "backtest": _bool_mark(backtest),
            "early_stop": _bool_mark(early),
            "eliminated_before_optimizer": _bool_mark(eliminated_pre),
            "optimizer": _bool_mark(optimizer),
            "validation": _bool_mark(validation),
            "paper": _bool_mark(paper),
            "final_state": state,
            "state_reason": str(item.get("state_reason", "")),
        }

        if eliminated_pre:
            evidence = _rule_evidence(item, symbol, timeframe)
            cat = _category_from_motivo(str(evidence.get("motivo", "outros")))
            contra, contra_just = _contrafactual(item, evidence)
            detail = {
                "strategy": name,
                "family": family,
                "indicators": indicators,
                "asset": symbol,
                "timeframe": timeframe,
                "motivo_exato": str(item.get("state_reason", "")) or str(evidence.get("motivo", "")),
                "regra_responsavel": evidence.get("regra"),
                "arquivo": evidence.get("arquivo"),
                "linha": evidence.get("linha"),
                "valor_observado": evidence.get("valor_observado"),
                "valor_minimo_exigido": evidence.get("valor_minimo"),
                "diferenca_percentual": evidence.get("diferenca_percentual"),
                "categoria": cat,
                "contrafactual": contra,
                "contrafactual_justificativa": contra_just,
                "eliminada_antes_optimizer": True,
            }
            eliminadas_pre_optimizer.append(detail)

        rows.append(row)

    stage_counts = {
        "morreram_smoke": len([r for r in rows if r["smoke"] == "SIM" and r["backtest"] == "NAO"]),
        "morreram_backtest": len([r for r in rows if r["backtest"] == "SIM" and r["optimizer"] == "NAO" and r["final_state"] == "BACKTEST_FAILED"]),
        "morreram_early_stop": len([r for r in eliminadas_pre_optimizer if "profit_factor_too_low" in str(r.get("motivo_exato", "")) or "no_trades" in str(r.get("motivo_exato", "")) or "negative_profile" in str(r.get("motivo_exato", ""))]),
        "chegaram_optimizer": len([r for r in rows if r["optimizer"] == "SIM"]),
        "chegaram_validation": len([r for r in rows if r["validation"] == "SIM"]),
        "chegaram_paper": len([r for r in rows if r["paper"] == "SIM"]),
    }

    motivo_freq = Counter([str(x.get("categoria", "Outros")) for x in eliminadas_pre_optimizer])
    regra_freq = Counter([str(x.get("regra_responsavel", "")) for x in eliminadas_pre_optimizer])
    ranking_motivos = [{"motivo": k, "count": v} for k, v in motivo_freq.most_common()]
    ranking_regras = [{"regra": k, "count": v} for k, v in regra_freq.most_common()]

    early_rows = []
    for x in eliminadas_pre_optimizer:
        if "phase13_continuous_strategy_factory.py" in str(x.get("arquivo", "")) and int(x.get("linha", 0)) in {609, 611, 613}:
            quality = "baixa qualidade evidente"
            if x.get("motivo_exato") == "no_trades;profit_factor_too_low":
                quality = "falta de oportunidade de otimizacao"
            early_rows.append(
                {
                    "strategy": x.get("strategy"),
                    "regra": x.get("regra_responsavel"),
                    "metrica": x.get("motivo_exato"),
                    "trades_suficientes": "SIM" if "no_trades" not in str(x.get("motivo_exato", "")) else "NAO",
                    "eliminada_antes_optimizer": "SIM",
                    "classificacao": quality,
                }
            )

    optimizer_audit = {
        "executou_optimizer": stage_counts["chegaram_optimizer"] > 0,
        "condicao": "nao existiam estrategias elegiveis apos early stop" if stage_counts["chegaram_optimizer"] == 0 else "optimizer_executado",
        "evidencia": "Todas as implementadas com backtest foram eliminadas por regras do early stop antes da chamada _run_optimizer",
        "arquivo": "research/services/phase13_continuous_strategy_factory.py",
        "linha_condicao": 251,
    }

    could_benefit = any(x.get("contrafactual") in {"Possivel", "Provavel"} for x in eliminadas_pre_optimizer)

    final = {
        "phase": "13.3",
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_report": str(report_path),
        "implemented_strategies_audited": len(implemented),
        "matrix": rows,
        "elimination_details": eliminadas_pre_optimizer,
        "ranking_motivos": ranking_motivos,
        "ranking_regras": ranking_regras,
        "early_stop_audit": early_rows,
        "optimizer_audit": optimizer_audit,
        "summary": {
            "quantas_morreram_smoke": stage_counts["morreram_smoke"],
            "quantas_morreram_backtest": stage_counts["morreram_backtest"],
            "quantas_morreram_early_stop": stage_counts["morreram_early_stop"],
            "quantas_chegaram_optimizer": stage_counts["chegaram_optimizer"],
            "quantas_chegaram_validation": stage_counts["chegaram_validation"],
            "quantas_chegaram_paper": stage_counts["chegaram_paper"],
            "principal_motivo_eliminacao": ranking_motivos[0]["motivo"] if ranking_motivos else "N/A",
            "regra_que_mais_eliminou": ranking_regras[0]["regra"] if ranking_regras else "N/A",
            "ha_evidencia_beneficio_otimizacao_antes_reprovacao": "SIM" if could_benefit else "NAO",
            "decisao_final_gargalo": "Early Stop" if stage_counts["chegaram_optimizer"] == 0 else "Outro",
        },
        "propostas_correcao": [],
    }

    if ranking_motivos:
        final["propostas_correcao"] = [
            {
                "problema": "Eliminacao predominante no Early Stop por Profit Factor/negative profile",
                "evidencia_quantitativa": ranking_motivos[:3],
                "impacto_esperado": "Aumentar quantidade que chega ao Optimizer sem mudar criterios cientificos",
                "risco": "Moderado: maior custo computacional",
                "modulos_afetados": [
                    "research/services/phase13_continuous_strategy_factory.py"
                ],
            },
            {
                "problema": "Bloqueio estrutural por NOT_IMPLEMENTED_IN_PLATFORM",
                "evidencia_quantitativa": [x for x in ranking_motivos if x["motivo"] in {"Erro de implementacao"}],
                "impacto_esperado": "Mais estrategias acessando smoke/backtest",
                "risco": "Baixo a moderado: aumento de manutencao de implementacoes",
                "modulos_afetados": [
                    "strategies/*",
                    "research/services/phase13_continuous_strategy_factory.py"
                ],
            },
            {
                "problema": "Falhas de Risk Manager RR no smoke (margem fina)",
                "evidencia_quantitativa": [x for x in ranking_motivos if x["motivo"] in {"Risk Manager"}],
                "impacto_esperado": "Menos perdas no gate infra anterior ao backtest",
                "risco": "Baixo: apenas melhoria de instrumentacao/precisao de sinais",
                "modulos_afetados": [
                    "research/services/phase13_continuous_strategy_factory.py"
                ],
            },
        ]

    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_json = RESULTS_DIR / f"phase13_3_pre_optimizer_audit_{stamp}.json"
    out_csv = RESULTS_DIR / f"phase13_3_pre_optimizer_audit_{stamp}_matrix.csv"
    out_md = RESULTS_DIR / f"phase13_3_pre_optimizer_audit_{stamp}.md"

    out_json.write_text(json.dumps(final, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    with out_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(
            fh,
            fieldnames=[
                "strategy",
                "family",
                "indicators",
                "asset",
                "timeframe",
                "smoke",
                "backtest",
                "early_stop",
                "eliminated_before_optimizer",
                "optimizer",
                "validation",
                "paper",
                "final_state",
                "state_reason",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    md_lines = [
        "# FASE 13.3 - Auditoria do Gargalo Pre-Optimizer",
        "",
        f"- Relatorio base: {report_path.name}",
        f"- Estrategias implementadas auditadas: {len(implemented)}",
        "",
        "## Respostas obrigatorias",
        "",
        f"- Quantas estrategias morreram no Smoke? {final['summary']['quantas_morreram_smoke']}",
        f"- Quantas morreram no Backtest? {final['summary']['quantas_morreram_backtest']}",
        f"- Quantas morreram no Early Stop? {final['summary']['quantas_morreram_early_stop']}",
        f"- Quantas chegaram ao Optimizer? {final['summary']['quantas_chegaram_optimizer']}",
        f"- Quantas chegaram ao Validation? {final['summary']['quantas_chegaram_validation']}",
        f"- Quantas chegaram ao Paper? {final['summary']['quantas_chegaram_paper']}",
        f"- Qual foi o principal motivo de eliminacao? {final['summary']['principal_motivo_eliminacao']}",
        f"- Qual regra eliminou mais estrategias? {final['summary']['regra_que_mais_eliminou']}",
        f"- Existe evidencia de beneficio por otimizacao antes da reprovacao? {final['summary']['ha_evidencia_beneficio_otimizacao_antes_reprovacao']}",
        "",
        "## Decisao final",
        "",
        f"- Gargalo atual: {final['summary']['decisao_final_gargalo']}",
        f"- Auditoria do Optimizer: {optimizer_audit['condicao']}",
        "",
        "## Ranking de motivos",
        "",
    ]
    for row in ranking_motivos:
        md_lines.append(f"- {row['motivo']}: {row['count']}")

    out_md.write_text("\n".join(md_lines), encoding="utf-8")

    return {
        "json": str(out_json),
        "csv": str(out_csv),
        "markdown": str(out_md),
        "summary": final["summary"],
    }


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
