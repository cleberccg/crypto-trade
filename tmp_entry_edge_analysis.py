import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from utils.metrics import capture_ratio_from_realized_and_mfe

ops_path = Path("optimization/results/strategy_research_operations_20260628_173351.csv")
df = pd.read_csv(ops_path)

n = len(df)
for col in ["mfe", "mae", "first_move", "final_move", "pnl"]:
    df[col] = pd.to_numeric(df[col], errors="coerce")

# No reconstructed stop_loss / take_profit available, so use per-trade |MAE| as risk proxy.
df["risk_unit_proxy"] = df["mae"].abs().replace(0, np.nan)
df["mfe_r_proxy"] = df["mfe"] / df["risk_unit_proxy"]
df["final_r_proxy"] = df["final_move"] / df["risk_unit_proxy"]

etapa1 = {
    "total_trades": int(n),
    "avg_mfe": float(df["mfe"].mean()),
    "avg_mae": float(df["mae"].mean()),
    "median_mfe": float(df["mfe"].median()),
    "median_mae": float(df["mae"].median()),
    "mfe_gt_abs_mae_pct": float((df["mfe"] > df["mae"].abs()).mean() * 100.0),
}

hit_profit_before_inversion = int(((df["first_move"] > 0) & (df["final_move"] < 0)).sum())
immediate_inversion = int((df["first_move"] < 0).sum())
never_positive = int(((df["mfe"] <= 0) | df["mfe"].isna()).sum())

etapa2 = {
    "hit_profit_before_inversion": {
        "count": hit_profit_before_inversion,
        "pct": float(hit_profit_before_inversion * 100.0 / n),
    },
    "immediate_inversion": {
        "count": immediate_inversion,
        "pct": float(immediate_inversion * 100.0 / n),
    },
    "never_positive": {
        "count": never_positive,
        "pct": float(never_positive * 100.0 / n),
    },
    "risk_multiple_definition": "Proxy R = |MAE| por trade (stop_loss indisponivel na reconstrucao).",
    "reached_0_5R": {
        "count": int((df["mfe_r_proxy"] >= 0.5).sum()),
        "pct": float((df["mfe_r_proxy"] >= 0.5).sum() * 100.0 / n),
    },
    "reached_1R": {
        "count": int((df["mfe_r_proxy"] >= 1.0).sum()),
        "pct": float((df["mfe_r_proxy"] >= 1.0).sum() * 100.0 / n),
    },
    "reached_2R": {
        "count": int((df["mfe_r_proxy"] >= 2.0).sum()),
        "pct": float((df["mfe_r_proxy"] >= 2.0).sum() * 100.0 / n),
    },
    "reached_3R": {
        "count": int((df["mfe_r_proxy"] >= 3.0).sum()),
        "pct": float((df["mfe_r_proxy"] >= 3.0).sum() * 100.0 / n),
    },
}

pos = df[df["mfe"] > 0].copy()
actual_avg_move = float(pos["final_move"].mean()) if not pos.empty else 0.0
capture_ratio = (
    pos.apply(lambda row: capture_ratio_from_realized_and_mfe(float(row["final_move"]), float(row["mfe"])), axis=1)
    .replace([np.inf, -np.inf], np.nan)
    .mean()
    if not pos.empty
    else 0.0
)

if not pos.empty:
    earlier = np.minimum(pos["final_move"], 0.5 * pos["mfe"])
    later = pos["mfe"]
    trailing = np.maximum(pos["final_move"], 0.5 * pos["mfe"])
    break_even = np.where(pos["mfe_r_proxy"] >= 0.5, np.maximum(pos["final_move"], 0.0), pos["final_move"])
    moving_stop = np.maximum(pos["final_move"], 0.35 * pos["mfe"])
else:
    earlier = np.array([0.0])
    later = np.array([0.0])
    trailing = np.array([0.0])
    break_even = np.array([0.0])
    moving_stop = np.array([0.0])

etapa3 = {
    "sample_positive_mfe": int(len(pos)),
    "avg_final_move_actual": actual_avg_move,
    "avg_capture_ratio_actual": float(capture_ratio),
    "scenario_earlier_avg_move": float(np.nanmean(earlier)),
    "scenario_later_upper_bound_avg_move": float(np.nanmean(later)),
    "scenario_trailing_stop_avg_move": float(np.nanmean(trailing)),
    "scenario_break_even_avg_move": float(np.nanmean(break_even)),
    "scenario_moving_stop_avg_move": float(np.nanmean(moving_stop)),
    "delta_trailing_vs_actual": float(np.nanmean(trailing) - actual_avg_move),
    "delta_break_even_vs_actual": float(np.nanmean(break_even) - actual_avg_move),
    "delta_moving_stop_vs_actual": float(np.nanmean(moving_stop) - actual_avg_move),
}

avg_mfe = etapa1["avg_mfe"]
avg_abs_mae = abs(etapa1["avg_mae"])
never_pos_pct = etapa2["never_positive"]["pct"]

if avg_mfe > 0 and etapa1["mfe_gt_abs_mae_pct"] >= 35 and capture_ratio < 0.45 and never_pos_pct < 20:
    diag = "B"
    diag_reason = "MFE positivo frequente e baixa captura do movimento pela saida."
else:
    diag = "A"
    diag_reason = "Excursao adversa domina e/ou entrada sofre inversao precoce em excesso."

etapa4 = {
    "option": diag,
    "rationale": diag_reason,
}

entry_late_score = immediate_inversion / n
stop_inadequate_score = float((df["mae"].abs() > df["mfe"]).mean())
tp_score = float(((df["mfe"] > 0) & (df["final_move"].div(df["mfe"]).replace([np.inf, -np.inf], np.nan) < 0.35)).mean())
overtrade_score = float((df["mfe_r_proxy"] < 0.5).mean())
position_management_score = float(max(0.0, 1.0 - float(capture_ratio)))
false_break_score = float(df["falso_rompimento"].fillna(False).astype(bool).mean())

ranking = [
    ("Gestao da posicao (baixa captura de MFE)", position_management_score),
    ("Stops inadequados (MAE > MFE)", stop_inadequate_score),
    ("Entradas tardias (inversao imediata)", entry_late_score),
    ("Excesso de operacoes sem edge (<0.5R proxy)", overtrade_score),
    ("Take Profit/saida (captura <35% do MFE)", tp_score),
    ("Falso rompimento", false_break_score),
]

etapa5 = [
    {"factor": factor, "score": float(score)}
    for factor, score in sorted(ranking, key=lambda item: item[1], reverse=True)
]

final_answers = {
    "entradas_possuem_vantagem_estatistica": bool(diag == "B"),
    "gestao_destroi_vantagem": bool(diag == "B"),
    "vale_evoluir_trendv2": bool(diag == "B"),
    "recomendacao": (
        "Evoluir gestao de posicao mantendo logica de entrada para novo ciclo de validacao."
        if diag == "B"
        else "Abandonar esta logica de entrada e pesquisar nova familia de estrategias."
    ),
}

result = {
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "source_file": str(ops_path),
    "sample_size": int(n),
    "etapa1_mfe_mae": etapa1,
    "etapa2_entry_quality": etapa2,
    "etapa3_exit_quality": etapa3,
    "etapa4_diagnosis": etapa4,
    "etapa5_ranking": etapa5,
    "final_answers": final_answers,
}

json_out = Path("optimization/results/entry_edge_analysis_20260628_173351.json")
md_out = Path("optimization/results/entry_edge_analysis_20260628_173351.md")
json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

lines = [
    "# Edge Estatistico das Entradas",
    "",
    f"Generated at: {result['generated_at']}",
    "",
    f"Amostra: {n} trades",
    "",
    "## Etapa 1 - MFE/MAE",
    f"- MFE medio: {etapa1['avg_mfe']:.6f}",
    f"- MAE medio: {etapa1['avg_mae']:.6f}",
    f"- Mediana MFE: {etapa1['median_mfe']:.6f}",
    f"- Mediana MAE: {etapa1['median_mae']:.6f}",
    f"- % trades com MFE > |MAE|: {etapa1['mfe_gt_abs_mae_pct']:.2f}%",
    "",
    "## Etapa 2 - Qualidade da Entrada",
    f"- Lucro antes de inverter: {etapa2['hit_profit_before_inversion']['count']} ({etapa2['hit_profit_before_inversion']['pct']:.2f}%)",
    f"- Inversao imediata: {etapa2['immediate_inversion']['count']} ({etapa2['immediate_inversion']['pct']:.2f}%)",
    f"- Nunca ficaram positivas: {etapa2['never_positive']['count']} ({etapa2['never_positive']['pct']:.2f}%)",
    f"- >=0.5R proxy: {etapa2['reached_0_5R']['count']} ({etapa2['reached_0_5R']['pct']:.2f}%)",
    f"- >=1R proxy: {etapa2['reached_1R']['count']} ({etapa2['reached_1R']['pct']:.2f}%)",
    f"- >=2R proxy: {etapa2['reached_2R']['count']} ({etapa2['reached_2R']['pct']:.2f}%)",
    f"- >=3R proxy: {etapa2['reached_3R']['count']} ({etapa2['reached_3R']['pct']:.2f}%)",
    "",
    "## Etapa 3 - Qualidade da Saida (MFE > 0)",
    f"- Trades com MFE positivo: {etapa3['sample_positive_mfe']}",
    f"- Captura media atual de MFE: {etapa3['avg_capture_ratio_actual']:.4f}",
    f"- Delta trailing vs atual: {etapa3['delta_trailing_vs_actual']:.6f}",
    f"- Delta break-even vs atual: {etapa3['delta_break_even_vs_actual']:.6f}",
    f"- Delta stop movel vs atual: {etapa3['delta_moving_stop_vs_actual']:.6f}",
    "",
    "## Etapa 4 - Diagnostico",
    f"- Opcao: {diag}",
    f"- Justificativa: {diag_reason}",
    "",
    "## Etapa 5 - Ranking",
]
for idx, item in enumerate(etapa5, start=1):
    lines.append(f"- {idx}. {item['factor']} -> score {item['score']:.4f}")

lines.extend(
    [
        "",
        "## Respostas Finais",
        f"1. Entradas possuem vantagem estatistica? {'SIM' if final_answers['entradas_possuem_vantagem_estatistica'] else 'NAO'}",
        f"2. A gestao destroi essa vantagem? {'SIM' if final_answers['gestao_destroi_vantagem'] else 'NAO'}",
        f"3. Vale evoluir a TrendV2? {'SIM' if final_answers['vale_evoluir_trendv2'] else 'NAO'}",
        f"4. Recomendacao: {final_answers['recomendacao']}",
        "",
        "## Nota metodologica",
        "- Stop-loss e take-profit nao estao preenchidos nos trades reconstruidos; os multiplos R foram calculados por proxy usando |MAE| por trade.",
    ]
)

md_out.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(json_out)
print(md_out)
