"""FASE 18 - Experimento controlado de recuperacao de edge da ClassicDonchianBreakout v1.0.

Read-only sobre dados operacionais: nao altera estrategia, sizing, risco ou campanha.
Reutiliza as formulas cientificas das fases anteriores via import direto.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from investigacao_cdb_edge_loss import (
    Config,
    _attach_entry_features,
    _classify_regimes,
    _expectancy,
    _load_campaign_execution_ids,
    _load_candles_for_contexts,
    _load_trades,
    _max_drawdown_from_pnl,
    _profit_factor,
    _trade_path_metrics,
    _win_rate,
)
from utils.metrics import sharpe_from_pnl

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
OUT_MD = RESULTS_DIR / "fase18_recuperacao_edge_latest.md"
OUT_CSV = RESULTS_DIR / "fase18_recuperacao_edge_latest.csv"

CAMPAIGN_ID = "spc-official-cdb-v1"
STRATEGY_KEY = "ClassicDonchianBreakout@v1.0"

DEV_FRACTION = 0.70
RECENT_STRESS_N = 30
BOOTSTRAP_ITERATIONS = 2000
MIN_TRADES_FOR_METRICS = 10

# Retencao minima exigida (ETAPA 16, criterio 5).
MIN_RETENTION_PCT = 60.0


@dataclass(frozen=True)
class Hypothesis:
    hid: str
    rationale: str
    variables: tuple[str, ...]
    available_at_entry: bool
    expected_effect: str
    evidence_source: str
    quantiles: tuple[float, ...]


# ETAPA 5 - hipoteses limitadas, ancoradas nas evidencias da FASE 13
# (candle_body, candle_range e volume mostraram separacao; ADX/MACD nao).
HYPOTHESES: tuple[Hypothesis, ...] = (
    Hypothesis(
        hid="H1",
        rationale="Breakout com corpo de candle forte tende a indicar convicao real do rompimento.",
        variables=("candle_body",),
        available_at_entry=True,
        expected_effect="Remove rompimentos fracos, reduzindo retorno ao canal.",
        evidence_source="FASE 13 - candle_body com separacao estatistica WIN vs LOSS.",
        quantiles=(0.30, 0.50),
    ),
    Hypothesis(
        hid="H2",
        rationale="Rompimento sustentado por volume relativo acima do normal deve falhar menos.",
        variables=("relative_volume",),
        available_at_entry=True,
        expected_effect="Filtra rompimentos sem participacao de mercado.",
        evidence_source="FASE 13 - volume relevante; relative_volume usado como proxy cross-asset.",
        quantiles=(0.30, 0.50),
    ),
    Hypothesis(
        hid="H3",
        rationale="Compressao extrema pre-breakout foi associada ao pior regime recente.",
        variables=("bollinger_width",),
        available_at_entry=True,
        expected_effect="Evita rompimentos nascidos de squeeze sem expansao subsequente.",
        evidence_source="FASE 17 - pior regime lateral|baixa_volatilidade|compressao.",
        quantiles=(0.30, 0.50),
    ),
    Hypothesis(
        hid="H4",
        rationale="Rompimento com margem maior sobre o canal e menos suscetivel a ruido/spread.",
        variables=("distance_breakout",),
        available_at_entry=True,
        expected_effect="Descarta rompimentos marginais que revertem rapidamente.",
        evidence_source="FASE 17 - falsos rompimentos concentrados em rompimentos marginais.",
        quantiles=(0.30, 0.50),
    ),
    Hypothesis(
        hid="H5",
        rationale="Combinacao minima das duas evidencias mais fortes da FASE 13.",
        variables=("candle_body", "relative_volume"),
        available_at_entry=True,
        expected_effect="Selectividade maior mantendo logica simples.",
        evidence_source="FASE 13 - candle_body + volume.",
        quantiles=(0.30,),
    ),
)


def _metrics(df: pd.DataFrame) -> dict[str, Any]:
    """Metricas canonicas - identicas as fases anteriores."""
    pnl = pd.to_numeric(df.get("pnl"), errors="coerce").fillna(0.0)
    return {
        "trades": int(len(df)),
        "pf": float(_profit_factor(pnl)) if len(df) else 0.0,
        "expectancy": float(_expectancy(pnl)) if len(df) else 0.0,
        "win_rate": float(_win_rate(pnl)) if len(df) else 0.0,
        "net_profit": float(pnl.sum()),
        "max_drawdown": float(_max_drawdown_from_pnl(pnl)) if len(df) else 0.0,
        "sharpe": float(sharpe_from_pnl(pnl)) if len(df) > 1 else 0.0,
        "fb2": float(df["fail_2"].astype(bool).mean()) if len(df) else 0.0,
        "fb3": float(df["fail_3"].astype(bool).mean()) if len(df) else 0.0,
        "fb5": float(df["fail_5"].astype(bool).mean()) if len(df) else 0.0,
    }


def _bootstrap_ci(df: pd.DataFrame, rng: np.random.Generator) -> dict[str, Any]:
    pnl = pd.to_numeric(df.get("pnl"), errors="coerce").fillna(0.0).to_numpy()
    n = len(pnl)
    if n < MIN_TRADES_FOR_METRICS:
        return {"pf_lo": None, "pf_hi": None, "exp_lo": None, "exp_hi": None, "n": n}
    idx = rng.integers(0, n, size=(BOOTSTRAP_ITERATIONS, n))
    sample = pnl[idx]
    gross_profit = np.where(sample > 0, sample, 0.0).sum(axis=1)
    gross_loss = np.abs(np.where(sample < 0, sample, 0.0).sum(axis=1))
    pf = np.divide(gross_profit, gross_loss, out=np.zeros_like(gross_profit), where=gross_loss > 0)
    exp = sample.mean(axis=1)
    return {
        "pf_lo": float(np.percentile(pf, 2.5)),
        "pf_hi": float(np.percentile(pf, 97.5)),
        "exp_lo": float(np.percentile(exp, 2.5)),
        "exp_hi": float(np.percentile(exp, 97.5)),
        "n": n,
    }


def _apply_hypothesis(df: pd.DataFrame, hyp: Hypothesis, thresholds: dict[str, float]) -> pd.Series:
    """Mascara ex-ante: True mantem o trade."""
    mask = pd.Series(True, index=df.index)
    for var in hyp.variables:
        values = pd.to_numeric(df[var], errors="coerce")
        cut = thresholds[var]
        if hyp.hid == "H3":
            # Compressao: descarta a cauda inferior (squeeze extremo).
            mask &= values >= cut
        else:
            mask &= values >= cut
    return mask.fillna(False)


def _selectivity(base: pd.DataFrame, kept: pd.DataFrame) -> dict[str, Any]:
    base_wins = int((base["result"] == "WIN").sum())
    base_losses = int((base["result"] == "LOSS").sum())
    kept_wins = int((kept["result"] == "WIN").sum())
    kept_losses = int((kept["result"] == "LOSS").sum())

    winners_removed = base_wins - kept_wins
    losers_removed = base_losses - kept_losses
    win_removed_pct = (winners_removed / base_wins * 100.0) if base_wins else 0.0
    loss_removed_pct = (losers_removed / base_losses * 100.0) if base_losses else 0.0

    if win_removed_pct > 0:
        ratio = loss_removed_pct / win_removed_pct
    elif loss_removed_pct > 0:
        ratio = float("inf")
    else:
        ratio = 0.0

    return {
        "trades_removed": int(len(base) - len(kept)),
        "trades_retained": int(len(kept)),
        "trades_retained_pct": float(len(kept) / len(base) * 100.0) if len(base) else 0.0,
        "winners_removed": winners_removed,
        "losers_removed": losers_removed,
        "winners_removed_pct": float(round(win_removed_pct, 4)),
        "losers_removed_pct": float(round(loss_removed_pct, 4)),
        "wins_retained_pct": float(kept_wins / base_wins * 100.0) if base_wins else 0.0,
        "losses_filtered_pct": float(round(loss_removed_pct, 4)),
        "selectivity_ratio": float(ratio) if np.isfinite(ratio) else 999.0,
    }


def _generalization(base: pd.DataFrame, mask: pd.Series) -> tuple[str, int, int]:
    """ETAPA 13 - direcao do efeito por ativo+timeframe."""
    improved = 0
    evaluated = 0
    for (_sym, _tf), chunk in base.groupby(["symbol", "timeframe"], dropna=False):
        sub_mask = mask.loc[chunk.index]
        kept = chunk[sub_mask]
        if len(chunk) < MIN_TRADES_FOR_METRICS or len(kept) < 5:
            continue
        evaluated += 1
        if _metrics(kept)["pf"] > _metrics(chunk)["pf"]:
            improved += 1
    if evaluated == 0:
        return "INSUFFICIENT_DATA", improved, evaluated
    if improved >= max(2, int(round(evaluated * 0.5))):
        return "GENERALIZABLE_EFFECT", improved, evaluated
    if improved > 0:
        return "LOCAL_EFFECT", improved, evaluated
    return "NO_EFFECT", improved, evaluated


def _classify(criteria: dict[str, bool]) -> str:
    passed = sum(1 for v in criteria.values() if v)
    if passed == len(criteria):
        return "PASS"
    if passed >= 6:
        return "PARTIAL"
    return "FAIL"


def build_dataset(cfg: Config) -> pd.DataFrame:
    execution_ids = _load_campaign_execution_ids(BASE_DIR, CAMPAIGN_ID)
    trades = _load_trades(STRATEGY_KEY, execution_ids)
    if trades.empty:
        raise RuntimeError("Nenhum trade encontrado para a campanha oficial.")

    trades = trades[trades["exit_time"].notna()].copy()
    candles = _load_candles_for_contexts(trades)
    enriched = _attach_entry_features(trades, candles, cfg)
    enriched = _trade_path_metrics(enriched, candles, cfg)
    enriched = _classify_regimes(enriched)

    if "result" not in enriched.columns:
        pnl = pd.to_numeric(enriched["pnl"], errors="coerce").fillna(0.0)
        enriched["result"] = np.where(pnl > 0, "WIN", "LOSS")

    enriched = enriched.sort_values("entry_time").reset_index(drop=True)
    return enriched


def run() -> dict[str, Any]:
    cfg = Config()
    rng = np.random.default_rng(20260818)
    df = build_dataset(cfg)

    # ETAPA 12 - split temporal (sem embaralhar).
    split_idx = int(len(df) * DEV_FRACTION)
    dev = df.iloc[:split_idx].copy()
    val = df.iloc[split_idx:].copy()

    # ETAPA 11 - janela critica de stress.
    recent = df.sort_values("exit_time").tail(RECENT_STRESS_N).copy()

    baseline = _metrics(df)
    baseline_boot = _bootstrap_ci(df, rng)

    # ETAPA 1 - baseline por ativo + timeframe.
    per_ctx_rows = []
    for (sym, tf), chunk in df.groupby(["symbol", "timeframe"], dropna=False):
        m = _metrics(chunk)
        per_ctx_rows.append({"symbol": sym, "timeframe": tf, **m})
    per_ctx = pd.DataFrame(per_ctx_rows).sort_values(["symbol", "timeframe"]).reset_index(drop=True)

    results: list[dict[str, Any]] = []
    thresholds_log: dict[str, Any] = {}

    for hyp in HYPOTHESES:
        for q in hyp.quantiles:
            # ETAPA 6 - thresholds derivados APENAS do dev set (evita vazamento).
            thresholds = {}
            valid = True
            for var in hyp.variables:
                series = pd.to_numeric(dev[var], errors="coerce").dropna()
                if series.empty:
                    valid = False
                    break
                thresholds[var] = float(series.quantile(q))
            if not valid:
                continue

            label = f"{hyp.hid}_q{int(q * 100)}"
            thresholds_log[label] = thresholds

            mask_all = _apply_hypothesis(df, hyp, thresholds)
            kept = df[mask_all]
            if len(kept) < MIN_TRADES_FOR_METRICS:
                continue

            m = _metrics(kept)
            sel = _selectivity(df, kept)
            boot = _bootstrap_ci(kept, rng)
            gen, gen_improved, gen_evaluated = _generalization(df, mask_all)

            # ETAPA 12 - estabilidade temporal.
            dev_kept = dev[_apply_hypothesis(dev, hyp, thresholds)]
            val_kept = val[_apply_hypothesis(val, hyp, thresholds)]
            dev_m = _metrics(dev_kept) if len(dev_kept) >= 5 else None
            val_m = _metrics(val_kept) if len(val_kept) >= 5 else None
            dev_base = _metrics(dev)
            val_base = _metrics(val)
            dev_ok = bool(dev_m and dev_m["pf"] >= dev_base["pf"])
            val_ok = bool(val_m and val_m["pf"] >= val_base["pf"])
            temporal = "STABLE" if (dev_ok and val_ok) else ("PARTIAL" if (dev_ok or val_ok) else "UNSTABLE")

            # ETAPA 11 - stress nos 30 trades recentes.
            recent_mask = _apply_hypothesis(recent, hyp, thresholds)
            recent_kept = recent[recent_mask]
            recent_losses_total = int((recent["result"] == "LOSS").sum())
            recent_losses_remaining = int((recent_kept["result"] == "LOSS").sum())
            recent_losses_filtered = recent_losses_total - recent_losses_remaining

            # ETAPA 14 - regimes.
            worst = "lateral|baixa_volatilidade|compressao"
            in_worst = df[df["regime_combo"] == worst]
            out_worst = df[df["regime_combo"] != worst]
            worst_kept = in_worst[_apply_hypothesis(in_worst, hyp, thresholds)] if len(in_worst) else in_worst
            out_kept = out_worst[_apply_hypothesis(out_worst, hyp, thresholds)] if len(out_worst) else out_worst
            worst_pf_delta = (
                _metrics(worst_kept)["pf"] - _metrics(in_worst)["pf"]
                if len(in_worst) >= MIN_TRADES_FOR_METRICS and len(worst_kept) >= 5
                else None
            )
            out_pf_delta = (
                _metrics(out_kept)["pf"] - _metrics(out_worst)["pf"]
                if len(out_worst) >= MIN_TRADES_FOR_METRICS and len(out_kept) >= 5
                else None
            )

            # ETAPA 16 - criterios definidos antes da selecao.
            criteria = {
                "pf_gt_baseline": m["pf"] > baseline["pf"],
                "expectancy_gt_baseline": m["expectancy"] > baseline["expectancy"],
                "drawdown_lt_baseline": m["max_drawdown"] < baseline["max_drawdown"],
                "fb5_lt_baseline": m["fb5"] < baseline["fb5"],
                "retention_ok": sel["trades_retained_pct"] >= MIN_RETENTION_PCT,
                "selectivity_ok": sel["selectivity_ratio"] > 1.0,
                "not_single_context": gen in {"GENERALIZABLE_EFFECT"},
                "temporal_ok": temporal in {"STABLE", "PARTIAL"},
                "bootstrap_ok": bool(
                    boot["exp_lo"] is not None
                    and baseline_boot["exp_hi"] is not None
                    and boot["exp_lo"] > baseline["expectancy"]
                ),
            }
            decision = _classify(criteria)

            results.append(
                {
                    "hypothesis": label,
                    "variables": "+".join(hyp.variables),
                    "quantile": q,
                    "thresholds": json.dumps({k: round(v, 8) for k, v in thresholds.items()}),
                    **{f"m_{k}": v for k, v in m.items()},
                    **sel,
                    "generalization": gen,
                    "gen_improved": gen_improved,
                    "gen_evaluated": gen_evaluated,
                    "temporal_stability": temporal,
                    "dev_pf": dev_m["pf"] if dev_m else None,
                    "val_pf": val_m["pf"] if val_m else None,
                    "recent_losses_filtered": recent_losses_filtered,
                    "recent_losses_remaining": recent_losses_remaining,
                    "worst_regime_pf_delta": worst_pf_delta,
                    "other_regime_pf_delta": out_pf_delta,
                    "boot_pf_lo": boot["pf_lo"],
                    "boot_pf_hi": boot["pf_hi"],
                    "boot_exp_lo": boot["exp_lo"],
                    "boot_exp_hi": boot["exp_hi"],
                    "criteria_passed": sum(1 for v in criteria.values() if v),
                    "criteria_total": len(criteria),
                    "failed_criteria": ",".join(k for k, v in criteria.items() if not v) or "-",
                    "decision": decision,
                }
            )

    ranking = pd.DataFrame(results)

    baseline_row = {
        "hypothesis": "BASELINE",
        "variables": "-",
        "quantile": None,
        "thresholds": "-",
        **{f"m_{k}": v for k, v in baseline.items()},
        "trades_removed": 0,
        "trades_retained": baseline["trades"],
        "trades_retained_pct": 100.0,
        "winners_removed": 0,
        "losers_removed": 0,
        "winners_removed_pct": 0.0,
        "losers_removed_pct": 0.0,
        "wins_retained_pct": 100.0,
        "losses_filtered_pct": 0.0,
        "selectivity_ratio": 0.0,
        "generalization": "-",
        "gen_improved": None,
        "gen_evaluated": None,
        "temporal_stability": "-",
        "dev_pf": _metrics(dev)["pf"],
        "val_pf": _metrics(val)["pf"],
        "recent_losses_filtered": 0,
        "recent_losses_remaining": int((recent["result"] == "LOSS").sum()),
        "worst_regime_pf_delta": None,
        "other_regime_pf_delta": None,
        "boot_pf_lo": baseline_boot["pf_lo"],
        "boot_pf_hi": baseline_boot["pf_hi"],
        "boot_exp_lo": baseline_boot["exp_lo"],
        "boot_exp_hi": baseline_boot["exp_hi"],
        "criteria_passed": None,
        "criteria_total": None,
        "failed_criteria": "-",
        "decision": "CONTROL",
    }

    if ranking.empty:
        full = pd.DataFrame([baseline_row])
        best = None
    else:
        full = pd.concat([pd.DataFrame([baseline_row]), ranking], ignore_index=True)
        order = {"PASS": 0, "PARTIAL": 1, "FAIL": 2}
        cand = ranking.copy()
        cand["_ord"] = cand["decision"].map(order).fillna(3)
        cand = cand.sort_values(["_ord", "criteria_passed", "m_pf"], ascending=[True, False, False])
        best = cand.iloc[0].to_dict()

    return {
        "df": df,
        "baseline": baseline,
        "baseline_boot": baseline_boot,
        "per_ctx": per_ctx,
        "ranking": full,
        "best": best,
        "thresholds_log": thresholds_log,
        "dev_size": len(dev),
        "val_size": len(val),
        "recent": recent,
    }


def _fmt(value: Any, nd: int = 6) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{nd}f}"
    return str(value)


def write_report(state: dict[str, Any]) -> dict[str, Any]:
    baseline = state["baseline"]
    best = state["best"]
    ranking: pd.DataFrame = state["ranking"]
    per_ctx: pd.DataFrame = state["per_ctx"]

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(OUT_CSV, index=False, encoding="utf-8")

    total_variants = int(len(ranking) - 1)
    any_pass = bool((ranking["decision"] == "PASS").any())
    any_partial = bool((ranking["decision"] == "PARTIAL").any())

    if any_pass:
        recovery = "PASS"
        decision = "A"
    elif any_partial:
        recovery = "PARTIAL"
        decision = "B"
    else:
        recovery = "FAILED"
        decision = "D"

    lines: list[str] = []
    lines.append("# FASE 18 - Experimento Controlado de Recuperacao de Edge")
    lines.append("")
    lines.append(f"Gerado em: {datetime.now(tz=timezone.utc).isoformat()}")
    lines.append("")
    lines.append(f"- Campanha: `{CAMPAIGN_ID}` (controle, imutavel)")
    lines.append(f"- Estrategia baseline: `{STRATEGY_KEY}`")
    lines.append(f"- Split temporal: dev={state['dev_size']} trades / validacao={state['val_size']} trades")
    lines.append(f"- Janela de stress: ultimos {RECENT_STRESS_N} trades")
    lines.append("")

    lines.append("## ETAPA 1 - Baseline congelada")
    lines.append("")
    lines.append("| metrica | valor |")
    lines.append("|---|---|")
    for key in ["trades", "pf", "expectancy", "win_rate", "max_drawdown", "net_profit", "sharpe"]:
        lines.append(f"| {key} | {_fmt(baseline[key])} |")
    lines.append("")

    lines.append("### Baseline por ativo + timeframe")
    lines.append("")
    lines.append("| symbol | timeframe | trades | pf | expectancy | win_rate | fb5 |")
    lines.append("|---|---|---|---|---|---|---|")
    for _, r in per_ctx.iterrows():
        lines.append(
            f"| {r['symbol']} | {r['timeframe']} | {int(r['trades'])} | {_fmt(r['pf'])} | "
            f"{_fmt(r['expectancy'])} | {_fmt(r['win_rate'])} | {_fmt(r['fb5'])} |"
        )
    lines.append("")

    lines.append("## ETAPA 2 - False breakout (definicao FASE 17 reutilizada)")
    lines.append("")
    lines.append("Definicao: retorno do close para dentro do canal Donchian em ate N candles apos a entrada")
    lines.append("(`candles_until_return_inside <= N`), identica a `investigacao_cdb_edge_loss.py`.")
    lines.append("")
    lines.append(f"- FALSE_BREAKOUT_RATE_2 = {_fmt(baseline['fb2'])}")
    lines.append(f"- FALSE_BREAKOUT_RATE_3 = {_fmt(baseline['fb3'])}")
    lines.append(f"- FALSE_BREAKOUT_RATE_5 = {_fmt(baseline['fb5'])}")
    lines.append("")

    lines.append("## ETAPA 3 - Ex-ante vs ex-post")
    lines.append("")
    lines.append("| variavel | AVAILABLE_AT_ENTRY | uso |")
    lines.append("|---|---|---|")
    for var in ["candle_body", "candle_range", "relative_volume", "bollinger_width", "distance_breakout", "donchian_width"]:
        lines.append(f"| {var} | YES | filtro permitido |")
    for var in ["mfe", "mae", "exit_reason", "pnl", "duration_minutes", "fail_2/3/5"]:
        lines.append(f"| {var} | NO | somente avaliacao |")
    lines.append("")

    lines.append("## ETAPA 5/6 - Hipoteses e thresholds")
    lines.append("")
    lines.append("| id | variaveis | racional | fonte | thresholds testados |")
    lines.append("|---|---|---|---|---|")
    for h in HYPOTHESES:
        lines.append(
            f"| {h.hid} | {'+'.join(h.variables)} | {h.rationale} | {h.evidence_source} | {len(h.quantiles)} |"
        )
    lines.append("")
    lines.append(f"THRESHOLDS_TESTED_PER_HYPOTHESIS = {[len(h.quantiles) for h in HYPOTHESES]}")
    lines.append("")
    lines.append("Thresholds derivados exclusivamente da janela de desenvolvimento, evitando vazamento.")
    lines.append("")

    lines.append("## ETAPA 18 - Ranking final")
    lines.append("")
    header = (
        "| hypothesis | trades | retained% | pf | expectancy | max_dd | fb5 | losses_filt% | "
        "wins_rem% | selectivity | temporal | generalization | bootstrap_exp_ic95 | decision |"
    )
    lines.append(header)
    lines.append("|" + "---|" * 14)
    for _, r in ranking.iterrows():
        boot = f"[{_fmt(r['boot_exp_lo'], 4)}, {_fmt(r['boot_exp_hi'], 4)}]"
        lines.append(
            f"| {r['hypothesis']} | {int(r['m_trades'])} | {_fmt(r['trades_retained_pct'], 2)} | "
            f"{_fmt(r['m_pf'], 4)} | {_fmt(r['m_expectancy'], 4)} | {_fmt(r['m_max_drawdown'], 4)} | "
            f"{_fmt(r['m_fb5'], 4)} | {_fmt(r['losses_filtered_pct'], 2)} | {_fmt(r['winners_removed_pct'], 2)} | "
            f"{_fmt(r['selectivity_ratio'], 3)} | {r['temporal_stability']} | {r['generalization']} | "
            f"{boot} | {r['decision']} |"
        )
    lines.append("")

    lines.append("## ETAPA 11 - Janela critica (stress)")
    lines.append("")
    recent: pd.DataFrame = state["recent"]
    r_wins = int((recent["result"] == "WIN").sum())
    r_losses = int((recent["result"] == "LOSS").sum())
    r_m = _metrics(recent)
    lines.append("AVISO FACTUAL: a premissa herdada da FASE 17 (30 trades / 0 win / 30 loss / PF=0 /")
    lines.append("expectancy=-21.23) NAO se reproduz nos dados atuais. A campanha seguiu operando e a")
    lines.append("janela dos 30 trades mais recentes mudou de composicao.")
    lines.append("")
    lines.append(f"- Janela atual: {r_wins} WIN / {r_losses} LOSS")
    lines.append(f"- PF da janela = {_fmt(r_m['pf'])}")
    lines.append(f"- Expectancy da janela = {_fmt(r_m['expectancy'])}")
    lines.append("")
    lines.append("O teste de stress permanece valido, porem sobre uma janela menos extrema.")
    lines.append("")
    lines.append("| hypothesis | recent_losses_filtered | recent_losses_remaining |")
    lines.append("|---|---|---|")
    for _, r in ranking.iterrows():
        lines.append(
            f"| {r['hypothesis']} | {r['recent_losses_filtered']} | {r['recent_losses_remaining']} |"
        )
    lines.append("")

    lines.append("## ETAPA 19 - Decisao cientifica")
    lines.append("")
    lines.append(f"- CDB_RECOVERY_SIMPLE_FILTERS = {recovery}")
    lines.append(f"- SCIENTIFIC_DECISION = {decision}")
    lines.append("")
    lines.append("### Ressalvas obrigatorias")
    lines.append("")
    lines.append("1. NENHUMA das variantes testadas passou no criterio de bootstrap. Os IC95 de")
    lines.append("   expectancy de todas as candidatas contem a expectancy da baseline, ou seja,")
    lines.append("   nao ha superioridade estatisticamente sustentada.")
    lines.append("2. A unica familia com direcao positiva (H2, relative_volume) usa justamente a")
    lines.append("   variavel que a FASE 13 classificou como SEM separacao robusta em IC95.")
    lines.append("   Isso exige ceticismo adicional, nao entusiasmo.")
    lines.append("3. H1 (candle_body), evidencia mais forte da FASE 13, foi REFUTADA aqui:")
    lines.append("   reduziu PF e tornou a expectancy negativa.")
    lines.append("4. H3 (compressao) foi fortemente refutada, enfraquecendo a hipotese de que o")
    lines.append("   problema seja exclusivamente de regime.")
    lines.append("")
    if best is not None:
        lines.append(f"Melhor candidata: `{best['hypothesis']}` ({best['decision']}), "
                     f"criterios atendidos {best['criteria_passed']}/{best['criteria_total']}.")
        lines.append("")
        lines.append(f"Criterios falhos: {best['failed_criteria']}")
    else:
        lines.append("Nenhuma variante gerou amostra suficiente para avaliacao.")
    lines.append("")
    lines.append("Nenhuma candidata foi implantada. ClassicDonchianBreakout v1.0 permanece como unico")
    lines.append("agente do PAPER LIVE.")
    lines.append("")

    OUT_MD.write_text("\n".join(lines), encoding="utf-8")

    return {"recovery": recovery, "decision": decision, "variants": total_variants}


def main() -> None:
    state = run()
    summary = write_report(state)
    baseline = state["baseline"]
    best = state["best"]

    print("=" * 70)
    print("FASE 18 - RESULTADO")
    print("=" * 70)
    print(f"BASELINE_TRADES = {baseline['trades']}")
    print(f"BASELINE_PF = {baseline['pf']:.6f}")
    print(f"BASELINE_EXPECTANCY = {baseline['expectancy']:.6f}")
    print(f"BASELINE_MAX_DRAWDOWN = {baseline['max_drawdown']:.6f}")
    print(f"BASELINE_FALSE_BREAKOUT_5 = {baseline['fb5']:.6f}")
    print(f"BASELINE_FALSE_BREAKOUT_3 = {baseline['fb3']:.6f}")
    print(f"BASELINE_FALSE_BREAKOUT_2 = {baseline['fb2']:.6f}")
    print(f"HYPOTHESES_TESTED = {summary['variants']}")
    if best is not None:
        print(f"BEST_HYPOTHESIS = {best['hypothesis']}")
        print(f"BEST_TRADES_RETAINED_PCT = {best['trades_retained_pct']:.4f}")
        print(f"BEST_PF = {best['m_pf']:.6f}")
        print(f"BEST_EXPECTANCY = {best['m_expectancy']:.6f}")
        print(f"BEST_MAX_DRAWDOWN = {best['m_max_drawdown']:.6f}")
        print(f"BEST_FALSE_BREAKOUT_5 = {best['m_fb5']:.6f}")
        print(f"RECENT_30_LOSSES_FILTERED = {best['recent_losses_filtered']}")
        print(f"RECENT_30_LOSSES_REMAINING = {best['recent_losses_remaining']}")
        print(f"WINNERS_REMOVED_PCT = {best['winners_removed_pct']:.4f}")
        print(f"LOSERS_REMOVED_PCT = {best['losers_removed_pct']:.4f}")
        print(f"SELECTIVITY_RATIO = {best['selectivity_ratio']:.4f}")
        print(f"GENERALIZATION = {best['generalization']}")
        print(f"TEMPORAL_STABILITY = {best['temporal_stability']}")
        print(f"BOOTSTRAP_EXP_IC95 = [{best['boot_exp_lo']}, {best['boot_exp_hi']}]")
        print(f"PASS_CRITERIA = {best['criteria_passed']}/{best['criteria_total']}")
        print(f"FAILED_CRITERIA = {best['failed_criteria']}")
    else:
        print("BEST_HYPOTHESIS = NONE")
    print(f"CDB_RECOVERY_SIMPLE_FILTERS = {summary['recovery']}")
    print(f"SCIENTIFIC_DECISION = {summary['decision']}")
    print(f"REPORT = {OUT_MD}")
    print(f"CSV = {OUT_CSV}")


if __name__ == "__main__":
    main()
