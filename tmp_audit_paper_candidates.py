"""Audit PAPER_CANDIDATE pipeline and generate performance analytics."""
import json
from collections import Counter
from pathlib import Path

state_path = Path(r"d:\xampp\htdocs\crypto\optimization\results\phase13_factory_state.json")
state = json.loads(state_path.read_text(encoding="utf-8"))
backlog = state.get("backlog", [])

print("=== DISTRIBUIÇÃO DE ESTADOS NO BACKLOG ===")
states = Counter(x.get("state", "?") for x in backlog)
for s, c in sorted(states.items(), key=lambda x: -x[1]):
    print(f"  {s:<40} {c}")

paper = [x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]
print(f"\n=== PAPER_CANDIDATE: {len(paper)} ===")

records = []
for s in paper:
    hist = s.get("history", []) or []

    # Count distinct run_ids from history
    run_ids = set()
    paper_exp_count = 0
    for ev in hist:
        if isinstance(ev, dict):
            detail = str(ev.get("detail", ""))
            event = str(ev.get("event", ""))
            if "paper" in event.lower():
                paper_exp_count += 1

    probe = s.get("optimizer_probe") or {}
    probe_metrics = probe.get("best_metrics_raw") or {}
    probe_pf = float(probe_metrics.get("profit_factor", 0) or 0)
    probe_sharpe = float(probe_metrics.get("sharpe_ratio", probe_metrics.get("sharpe", 0)) or 0)
    probe_exp = float(probe_metrics.get("expectancy", 0) or 0)
    probe_dd = float(probe_metrics.get("max_drawdown_pct", 0) or 0)
    probe_trades = int(probe_metrics.get("total_trades", probe_metrics.get("trades", 0)) or 0)

    bt = s.get("backtest_base") or s.get("backtest") or {}
    bt_pf = float(bt.get("profit_factor", 0) or 0)
    bt_sharpe = float(bt.get("sharpe", 0) or 0)
    bt_exp = float(bt.get("expectancy", 0) or 0)
    bt_dd = float(bt.get("drawdown_pct", 0) or 0)
    bt_trades = int(bt.get("number_of_trades", 0) or 0)

    baseline_comp = s.get("baseline_comparison") or {}
    baseline_decision = str(baseline_comp.get("decision", "N/A"))

    paper_exp_data = s.get("paper_experimental") or {}
    paper_exp_status = str(paper_exp_data.get("status", "N/A"))
    paper_exp_assessment = str(paper_exp_data.get("assessment", "N/A"))
    paper_exp_trades = int((paper_exp_data.get("closed_trades") or 0))

    candidate_eval = s.get("paper_candidate_evaluation") or {}
    eval_reasons = candidate_eval.get("reasons", [])

    records.append({
        "name": s.get("candidate_name", "?"),
        "family": s.get("family", "?"),
        "score": float(s.get("score", 0) or 0),
        "queue_score": float(s.get("queue_score", 0) or 0),
        "bt_pf": bt_pf,
        "bt_sharpe": bt_sharpe,
        "bt_exp": bt_exp,
        "bt_dd": bt_dd,
        "bt_trades": bt_trades,
        "probe_pf": probe_pf,
        "probe_sharpe": probe_sharpe,
        "probe_exp": probe_exp,
        "probe_dd": probe_dd,
        "probe_trades": probe_trades,
        "baseline_decision": baseline_decision,
        "paper_exp_status": paper_exp_status,
        "paper_exp_assessment": paper_exp_assessment,
        "paper_exp_trades": paper_exp_trades,
        "paper_exp_events": paper_exp_count,
        "eval_reasons": eval_reasons,
        "last_run_id": str(s.get("last_processed_run_id", ""))[:8],
    })

# Sort by probe_pf descending (best performance first)
records.sort(key=lambda x: (-x["probe_pf"], -x["score"]))

print(f"\n{'#':<3} {'Nome':<45} {'Fam':<12} {'Score':>6} {'BT_PF':>6} {'Probe_PF':>8} {'Sharpe':>7} {'Exp':>7} {'DD%':>5} {'Trades':>6} {'Baseline':<8} {'PaperExp'}")
print("-" * 160)
for i, r in enumerate(records, 1):
    print(
        f"{i:<3} {r['name']:<45} {r['family']:<12} {r['score']:>6.1f} "
        f"{r['bt_pf']:>6.3f} {r['probe_pf']:>8.3f} {r['probe_sharpe']:>7.3f} "
        f"{r['probe_exp']:>7.4f} {r['probe_dd']:>5.2f} {r['probe_trades']:>6} "
        f"{r['baseline_decision']:<8} {r['paper_exp_status']}/{r['paper_exp_assessment']}"
    )

print("\n=== AUDITORIA DO PIPELINE - FLUXO ESPERADO vs OBSERVADO ===")
print("""
FLUXO FASE 13:
  Implementação → Smoke → Backtest → Optimizer Probe → Early Stop?
    SE triggered: → REJECTED_BY_PERFORMANCE / INCONCLUSIVE_LOW_SAMPLE / NO_TRADES
    SE NOT triggered: → Optimizer Completo → Validation → Paper Qualification
                                                                → PAPER_APPROVED
                                          → se NÃO aprovado → Avaliação PAPER_CANDIDATE
                                                                → SE elegível → PAPER_CANDIDATE + Paper Experimental

O estado PAPER_CANDIDATE é ACUMULATIVO no backlog entre campanhas.
Nesta campanha: 33 estratégias foram reprocessadas e TODAS ativaram early_stop.
Logo: Optimizer Completo = 0, Validation = 0, Paper Qualification = 0.

Os 11 PAPER_CANDIDATEs existentes foram classificados em campanhas ANTERIORES e persistem no state.
""")

print("\n=== DIAGNÓSTICO: POR QUE TODAS ATIVARAM EARLY STOP? ===")
rejected = [x for x in backlog if x.get("state") == "REJECTED_BY_PERFORMANCE"]
inconclusive = [x for x in backlog if x.get("state") == "INCONCLUSIVE_LOW_SAMPLE"]
error_continued = [x for x in backlog if x.get("state") == "ERROR_RESILIENCE_CONTINUED"]
print(f"  REJECTED_BY_PERFORMANCE:  {len(rejected)}")
print(f"  INCONCLUSIVE_LOW_SAMPLE:  {len(inconclusive)}")
print(f"  ERROR_RESILIENCE_CONTINUED: {len(error_continued)}")

# What is the typical probe_pf for rejected ones?
rejected_probe_pfs = []
for s in rejected:
    probe = s.get("optimizer_probe") or {}
    metrics = probe.get("best_metrics_raw") or {}
    pf = float(metrics.get("profit_factor", 0) or 0)
    rejected_probe_pfs.append(pf)
if rejected_probe_pfs:
    avg_pf = sum(rejected_probe_pfs) / len(rejected_probe_pfs)
    print(f"\n  Probe PF médio dos REJECTED: {avg_pf:.3f}")

print("\n=== CONTAGEM DE ESTRATÉGIAS NÃO PROCESSADAS NESTA CAMPANHA ===")
not_processed_states = {"PAPER_CANDIDATE", "REJECTED_BY_PERFORMANCE", "INCONCLUSIVE_LOW_SAMPLE",
                        "NO_TRADES", "REJECTED_BY_INFRASTRUCTURE", "IMPLEMENTATION_PENDING"}
# Actually all were processed; check last_processed_run_id
latest_run = "37dba937-1cae-4689-960a-ff98758edf50"
processed_this_run = [x for x in backlog if str(x.get("last_processed_run_id","")) == latest_run]
not_processed_this_run = [x for x in backlog if str(x.get("last_processed_run_id","")) != latest_run]
print(f"  Processadas nesta campanha (run_id={latest_run[:8]}): {len(processed_this_run)}")
print(f"  Não processadas nesta campanha: {len(not_processed_this_run)}")
for x in not_processed_this_run:
    print(f"    {x.get('candidate_name','?'):<45} state={x.get('state','?')}")
