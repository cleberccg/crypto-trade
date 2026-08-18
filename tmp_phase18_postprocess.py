from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

base = Path("optimization/results/market_regime_router")
stamp = "phase18_market_regime_router_20260702_144854"
json_path = base / f"{stamp}.json"
router_map_path = base / f"{stamp}_router_map.csv"
robust_path = base / f"{stamp}_robustness.csv"

report = json.loads(json_path.read_text(encoding="utf-8"))
sim = report["simulation"]

single = sim["single_strategy_aggregate"]
router = sim["router_aggregate"]
rows = []
for key, label in [
    ("profit_factor", "Profit Factor"),
    ("sharpe", "Sharpe"),
    ("expectancy", "Expectancy"),
    ("drawdown_pct", "Drawdown"),
    ("net_profit", "Retorno liquido"),
    ("win_rate", "Win Rate"),
    ("number_of_trades", "Numero de trades"),
]:
    b = float(single.get(key) or 0.0)
    r = float(router.get(key) or 0.0)
    rows.append({"metric": label, "baseline": b, "router": r, "difference": r - b})

metrics_csv = base / f"{stamp}_metrics.csv"
pd.DataFrame(rows).to_csv(metrics_csv, index=False)

router_map = pd.read_csv(router_map_path)

frames: list[pd.DataFrame] = []
for cache_file in sorted((base / "regime_cache").glob("*__*.csv")):
    df = pd.read_csv(cache_file)
    symbol, timeframe = cache_file.stem.split("__", 1)
    symbol = symbol.replace("_", "/")
    df["symbol"] = symbol
    df["timeframe"] = timeframe
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    frames.append(df[["timestamp", "symbol", "timeframe", "trend_bucket", "vol_regime"]])

regimes = pd.concat(frames, ignore_index=True)
joined = regimes.merge(
    router_map,
    on=["symbol", "timeframe", "trend_bucket", "vol_regime"],
    how="left",
)
joined["recommended_strategy"] = joined["recommended_strategy"].fillna(report["baseline_single_strategy"]["strategy"])
joined["recommended_platform_strategy"] = joined["recommended_platform_strategy"].fillna(
    report["baseline_single_strategy"]["platform_strategy_name"]
)
joined = joined.sort_values(["symbol", "timeframe", "timestamp"])

joined["prev_strategy"] = joined.groupby(["symbol", "timeframe"])["recommended_platform_strategy"].shift(1)
joined["strategy_changed"] = (
    (joined["recommended_platform_strategy"] != joined["prev_strategy"]) & joined["prev_strategy"].notna()
)
joined["segment_id"] = joined.groupby(["symbol", "timeframe"])["strategy_changed"].cumsum().fillna(0).astype(int)

seg = (
    joined.groupby(["symbol", "timeframe", "segment_id", "recommended_platform_strategy"], as_index=False)
    .agg(start=("timestamp", "min"), end=("timestamp", "max"), bars=("timestamp", "count"))
)

decisions_csv = base / f"{stamp}_router_decisions.csv"
joined.to_csv(decisions_csv, index=False)

util = (
    joined.groupby("recommended_platform_strategy", as_index=False)
    .agg(uses=("timestamp", "count"))
    .sort_values("uses", ascending=False)
)
util["pct"] = util["uses"] / util["uses"].sum()
util_csv = base / f"{stamp}_router_utilization.csv"
util.to_csv(util_csv, index=False)

switch_count = int(joined["strategy_changed"].sum())
total_bars = int(len(joined))
switch_freq = (switch_count / total_bars) if total_bars else 0.0
avg_segment_bars = float(seg["bars"].mean()) if len(seg) else 0.0

reg_dist = joined.groupby(["trend_bucket", "vol_regime"], as_index=False).agg(bars=("timestamp", "count"))
reg_dist["pct"] = reg_dist["bars"] / reg_dist["bars"].sum()
regime_dist_csv = base / f"{stamp}_regime_distribution.csv"
reg_dist.to_csv(regime_dist_csv, index=False)

rob = pd.read_csv(robust_path)
router_sup = int((rob["router_better"] == True).sum())
total = int(len(rob))
baseline_sup = int((rob["router_better"] == False).sum())
draw = total - router_sup - baseline_sup

summary_lines = [
    "Hipotese:",
    "Uma estrategia adaptativa baseada em regime supera uma estrategia unica.",
    "",
    "Resultado:",
    str(report["hypothesis_decision"]["conclusion"]).upper(),
    "",
    "Motivos:",
    f"- Profit Factor: baseline={single['profit_factor']:.6f} router={router['profit_factor']:.6f} diff={router['profit_factor']-single['profit_factor']:.6f}",
    f"- Sharpe: baseline={single['sharpe']:.6f} router={router['sharpe']:.6f} diff={router['sharpe']-single['sharpe']:.6f}",
    f"- Drawdown: baseline={single['drawdown_pct']:.6f} router={router['drawdown_pct']:.6f} diff={router['drawdown_pct']-single['drawdown_pct']:.6f}",
    f"- Superioridade por contexto: router={router_sup}/{total} ({(router_sup/total if total else 0):.2%})",
    "",
    "Proxima recomendacao cientifica:",
    "Executar revalidacao rolling out-of-sample com os mesmos criterios e janela deslizante para confirmar estabilidade temporal.",
    "",
    "Estabilidade do Router:",
    f"- Mudancas de estrategia: {switch_count}",
    f"- Frequencia de troca (por barra): {switch_freq:.6f}",
    f"- Tempo medio em cada estrategia (barras): {avg_segment_bars:.2f}",
    "",
    "Frequencia de superioridade:",
    f"- Router superior: {router_sup}/{total} ({(router_sup/total if total else 0):.2%})",
    f"- Baseline superior: {baseline_sup}/{total} ({(baseline_sup/total if total else 0):.2%})",
    f"- Empate: {draw}/{total} ({(draw/total if total else 0):.2%})",
]
summary_path = base / f"{stamp}_executive_summary.txt"
summary_path.write_text("\n".join(summary_lines), encoding="utf-8")

print(str(metrics_csv))
print(str(decisions_csv))
print(str(util_csv))
print(str(regime_dist_csv))
print(str(summary_path))
