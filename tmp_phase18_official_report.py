from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

base = Path("optimization/results/market_regime_router")
stamp = "phase18_market_regime_router_20260702_144854"

json_path = base / f"{stamp}.json"
metrics_path = base / f"{stamp}_metrics.csv"
robust_path = base / f"{stamp}_robustness.csv"
router_map_path = base / f"{stamp}_router_map.csv"
profiles_path = base / f"{stamp}_profiles.csv"
decisions_path = base / f"{stamp}_router_decisions.csv"
util_path = base / f"{stamp}_router_utilization.csv"
regime_dist_path = base / f"{stamp}_regime_distribution.csv"

report = json.loads(json_path.read_text(encoding="utf-8"))
metrics = pd.read_csv(metrics_path)
robust = pd.read_csv(robust_path)
router_map = pd.read_csv(router_map_path)
profiles = pd.read_csv(profiles_path)
decisions = pd.read_csv(decisions_path)
util = pd.read_csv(util_path)
regime_dist = pd.read_csv(regime_dist_path)

# Gains / losses by asset/timeframe
by_asset = []
for asset, g in robust.groupby("symbol"):
    total = len(g)
    wins = int(g["router_better"].sum())
    losses = total - wins
    by_asset.append({
        "asset": asset,
        "router_superior": wins,
        "baseline_superior": losses,
        "router_superior_pct": wins / total if total else 0.0,
    })

by_timeframe = []
for tf, g in robust.groupby("timeframe"):
    total = len(g)
    wins = int(g["router_better"].sum())
    losses = total - wins
    by_timeframe.append({
        "timeframe": tf,
        "router_superior": wins,
        "baseline_superior": losses,
        "router_superior_pct": wins / total if total else 0.0,
    })

# Context-level win/loss lists
wins_ctx = robust[robust["router_better"] == True][["symbol", "timeframe"]].to_dict("records")
loss_ctx = robust[robust["router_better"] == False][["symbol", "timeframe"]].to_dict("records")

# For each loss context, inspect dominant regimes from timeline
loss_regimes = []
for row in loss_ctx:
    sub = decisions[(decisions["symbol"] == row["symbol"]) & (decisions["timeframe"] == row["timeframe"])]
    if sub.empty:
        continue
    reg = (
        sub.groupby(["trend_bucket", "vol_regime"], as_index=False)
        .size()
        .sort_values("size", ascending=False)
        .head(3)
    )
    loss_regimes.append(
        {
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "top_regimes": reg.to_dict("records"),
        }
    )

# Router diversity / concentration
total_uses = int(util["uses"].sum()) if len(util) else 0
util = util.sort_values("uses", ascending=False)
util_records = util.to_dict("records")
strategies_used = int(len(util))
dominant = util_records[0] if util_records else None
rare = [r for r in util_records if float(r.get("pct") or 0.0) < 0.01]

# Stability
decisions = decisions.sort_values(["symbol", "timeframe", "timestamp"])
if "strategy_changed" in decisions.columns:
    switch_count = int(decisions["strategy_changed"].astype(bool).sum())
else:
    prev = decisions.groupby(["symbol", "timeframe"])['recommended_platform_strategy'].shift(1)
    switch_count = int((decisions['recommended_platform_strategy'] != prev).fillna(False).sum())

bars_total = int(len(decisions))
switch_frequency = (switch_count / bars_total) if bars_total else 0.0

# Average hold duration (bars) by run lengths
prev = decisions.groupby(["symbol", "timeframe"])['recommended_platform_strategy'].shift(1)
changed = (decisions['recommended_platform_strategy'] != prev) & prev.notna()
segment = changed.groupby([decisions['symbol'], decisions['timeframe']]).cumsum().fillna(0).astype(int)
seg_df = decisions.copy()
seg_df["segment"] = segment
seg = seg_df.groupby(["symbol", "timeframe", "segment", "recommended_platform_strategy"], as_index=False).size()
avg_hold_bars = float(seg["size"].mean()) if len(seg) else 0.0

# Regime percentages for trend and volatility
trend_pct = (
    decisions.groupby("trend_bucket", as_index=False)
    .size()
    .rename(columns={"size": "bars"})
)
trend_pct["pct"] = trend_pct["bars"] / trend_pct["bars"].sum()

vol_pct = (
    decisions.groupby("vol_regime", as_index=False)
    .size()
    .rename(columns={"size": "bars"})
)
vol_pct["pct"] = vol_pct["bars"] / vol_pct["bars"].sum()

# Frequency superiority
router_sup = int((robust["router_better"] == True).sum())
base_sup = int((robust["router_better"] == False).sum())
total_ctx = int(len(robust))
draw = total_ctx - router_sup - base_sup

# Percent gain from aggregate metrics
def pct_gain(b: float, r: float) -> float | None:
    if b == 0:
        return None
    return (r - b) / abs(b)

single = report["simulation"]["single_strategy_aggregate"]
router_agg = report["simulation"]["router_aggregate"]

gains = {
    "profit_factor_pct": pct_gain(float(single["profit_factor"]), float(router_agg["profit_factor"])),
    "sharpe_pct": pct_gain(float(single["sharpe"]), float(router_agg["sharpe"])),
    "expectancy_pct": pct_gain(float(single["expectancy"]), float(router_agg["expectancy"])),
    "drawdown_pct": pct_gain(float(single["drawdown_pct"]), float(router_agg["drawdown_pct"])),
    "return_pct": pct_gain(float(single["return_pct"]), float(router_agg["return_pct"])),
}

result = {
    "official_command": "python main.py market-regime-router --symbols BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT --timeframes 5m,15m,1h --window-days 120 --max-bars 3500 --min-trades-per-regime 5 --baseline-strategy ClassicEMACrossover",
    "baseline_strategy": report["baseline_single_strategy"],
    "metrics_table": metrics.to_dict("records"),
    "robustness_by_asset": by_asset,
    "robustness_by_timeframe": by_timeframe,
    "contexts_router_won": wins_ctx,
    "contexts_router_lost": loss_ctx,
    "loss_context_dominant_regimes": loss_regimes,
    "router_utilization": util_records,
    "router_diversity": {
        "strategies_used": strategies_used,
        "dominant": dominant,
        "rare_lt_1pct": rare,
        "concentration_top1_pct": float(dominant.get("pct") or 0.0) if dominant else 0.0,
    },
    "router_stability": {
        "switch_count": switch_count,
        "switch_frequency_per_bar": switch_frequency,
        "avg_hold_bars": avg_hold_bars,
        "regime_distribution_joint": regime_dist.to_dict("records"),
        "trend_distribution": trend_pct.to_dict("records"),
        "volatility_distribution": vol_pct.to_dict("records"),
    },
    "superiority_frequency": {
        "router_superior": {"count": router_sup, "pct": router_sup / total_ctx if total_ctx else 0.0},
        "baseline_superior": {"count": base_sup, "pct": base_sup / total_ctx if total_ctx else 0.0},
        "draw": {"count": draw, "pct": draw / total_ctx if total_ctx else 0.0},
    },
    "average_gains_pct": gains,
    "hypothesis_decision": report["hypothesis_decision"],
    "notes": {
        "statistical_relevance_rule": "No hypothesis test is implemented in current pipeline outputs; relevance flagged by consistency across contexts (majority win >= 60%).",
    },
}

json_out = base / f"{stamp}_official_summary.json"
json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

# Markdown report
lines = []
lines.append("# FASE 18 - Execucao Oficial")
lines.append("")
lines.append("## 1. O Router agrega valor?")
lines.append("")
lines.append("| Metrica | Baseline | Router | Diferenca |")
lines.append("|---|---:|---:|---:|")
for row in metrics.to_dict("records"):
    lines.append(f"| {row['metric']} | {row['baseline']:.6f} | {row['router']:.6f} | {row['difference']:.6f} |")

lines.append("")
lines.append("## 2. Onde o Router ganhou?")
for row in by_asset:
    lines.append(f"- Ativo {row['asset']}: router_superior={row['router_superior']}/{row['router_superior']+row['baseline_superior']} ({row['router_superior_pct']:.2%})")
for row in by_timeframe:
    lines.append(f"- Timeframe {row['timeframe']}: router_superior={row['router_superior']}/{row['router_superior']+row['baseline_superior']} ({row['router_superior_pct']:.2%})")
lines.append("- Contextos ganhos: " + ", ".join([f"{x['symbol']} {x['timeframe']}" for x in wins_ctx]))
lines.append("")
lines.append("## 3. Onde perdeu?")
lines.append("- Contextos perdidos: " + ", ".join([f"{x['symbol']} {x['timeframe']}" for x in loss_ctx]))
for item in loss_regimes:
    top = ", ".join([f"{r['trend_bucket']}|{r['vol_regime']} ({r['size']})" for r in item['top_regimes']])
    lines.append(f"- {item['symbol']} {item['timeframe']} regimes dominantes: {top}")

lines.append("")
lines.append("## 4. Estrategias realmente utilizadas")
lines.append("")
lines.append("| Estrategia escolhida | Nº utilizacoes | % |")
lines.append("|---|---:|---:|")
for row in util_records:
    lines.append(f"| {row['recommended_platform_strategy']} | {int(row['uses'])} | {float(row['pct']):.2%} |")
lines.append(f"- Quantidade de estrategias utilizadas: {strategies_used}")
if dominant:
    lines.append(f"- Estrategia dominante: {dominant['recommended_platform_strategy']} ({float(dominant['pct']):.2%})")
if rare:
    lines.append("- Estrategias quase nunca escolhidas (<1%): " + ", ".join([r['recommended_platform_strategy'] for r in rare]))
else:
    lines.append("- Estrategias quase nunca escolhidas (<1%): nenhuma")

lines.append("")
lines.append("## 5. Diversidade do Router")
lines.append(f"- Estrategias diferentes participantes: {strategies_used}")
lines.append(f"- Concentracao top1: {(float(dominant['pct']) if dominant else 0.0):.2%}")
lines.append("- Usa apenas uma estrategia na pratica?: " + ("SIM" if strategies_used == 1 else "NAO"))
lines.append("- Ha equilibrio entre estrategias?: " + ("SIM" if (float(dominant['pct']) if dominant else 0.0) < 0.50 else "NAO"))

lines.append("")
lines.append("## 6. Estabilidade do Router")
lines.append(f"- Quantidade de mudancas de estrategia: {switch_count}")
lines.append(f"- Tempo medio em cada estrategia (barras): {avg_hold_bars:.2f}")
lines.append(f"- Frequencia de troca (por barra): {switch_frequency:.6f}")
lines.append("- Excesso de alternancia?: " + ("SIM" if switch_frequency > 0.10 else "NAO"))
lines.append("- Distribuicao de tendencia: " + ", ".join([f"{r['trend_bucket']}={r['pct']:.2%}" for r in trend_pct.to_dict('records')]))
lines.append("- Distribuicao de volatilidade: " + ", ".join([f"{r['vol_regime']}={r['pct']:.2%}" for r in vol_pct.to_dict('records')]))

lines.append("")
lines.append("## 7. Robustez")
lines.append("- Generalizou por ativos/timeframes?: " + ("SIM" if (router_sup / total_ctx if total_ctx else 0) >= 0.60 else "NAO"))

lines.append("")
lines.append("## 8. Frequencia de superioridade")
lines.append(f"- Router superior: {(router_sup/total_ctx if total_ctx else 0):.2%}")
lines.append(f"- Baseline superior: {(base_sup/total_ctx if total_ctx else 0):.2%}")
lines.append(f"- Empate: {(draw/total_ctx if total_ctx else 0):.2%}")

lines.append("")
lines.append("## 9. Ganho medio")
lines.append(f"- Profit Factor: {gains['profit_factor_pct']:.2%}" if gains['profit_factor_pct'] is not None else "- Profit Factor: n/a")
lines.append(f"- Sharpe: {gains['sharpe_pct']:.2%}" if gains['sharpe_pct'] is not None else "- Sharpe: n/a")
lines.append(f"- Expectancy: {gains['expectancy_pct']:.2%}" if gains['expectancy_pct'] is not None else "- Expectancy: n/a")
lines.append(f"- Drawdown: {gains['drawdown_pct']:.2%}" if gains['drawdown_pct'] is not None else "- Drawdown: n/a")
lines.append(f"- Retorno: {gains['return_pct']:.2%}" if gains['return_pct'] is not None else "- Retorno: n/a")

lines.append("")
lines.append("## Decisao")
lines.append(f"- Classificacao automatica: {str(report['hypothesis_decision']['conclusion']).upper()}")
lines.append(f"- Hipotese com maior evidencia: {report['hypothesis_decision']['hypothesis_with_more_evidence']}")
lines.append("")
lines.append("## Resumo executivo")
lines.append("")
lines.append("Hipotese:")
lines.append("Uma estrategia adaptativa baseada em regime supera uma estrategia unica.")
lines.append("")
lines.append("Resultado:")
lines.append(str(report['hypothesis_decision']['conclusion']).upper())
lines.append("")
lines.append("Motivos:")
lines.append(f"- Router foi superior em {router_sup}/{total_ctx} contextos ({(router_sup/total_ctx if total_ctx else 0):.2%}).")
lines.append(f"- Profit Factor agregado: {single['profit_factor']:.6f} -> {router_agg['profit_factor']:.6f}.")
lines.append(f"- Drawdown agregado: {single['drawdown_pct']:.6f} -> {router_agg['drawdown_pct']:.6f}.")
lines.append("")
lines.append("Proxima recomendacao cientifica:")
lines.append("Executar validacao rolling out-of-sample temporal para confirmar persistencia do ganho.")

md_out = base / f"{stamp}_official_summary.md"
md_out.write_text("\n".join(lines) + "\n", encoding="utf-8")

print(str(json_out))
print(str(md_out))
