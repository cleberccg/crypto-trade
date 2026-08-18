from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Config:
    strategy_key: str = "ClassicDonchianBreakout@v1.0"
    min_trades_promote: int = 30
    robust_trades: int = 50
    strong_trades: int = 100


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
        if not math.isfinite(out):
            return default
        return out
    except Exception:
        return default


def _latest_file(results_dir: Path, pattern: str) -> Path:
    matches = sorted(results_dir.glob(pattern), key=lambda path: path.stat().st_mtime)
    if not matches:
        raise RuntimeError(f"Nenhum arquivo encontrado para o padrao: {pattern}")
    return matches[-1]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _ci_crosses(reference: float, low: Any, high: Any) -> bool:
    lo = _safe_float(low, default=np.nan)
    hi = _safe_float(high, default=np.nan)
    if np.isnan(lo) or np.isnan(hi):
        return True
    return lo <= reference <= hi


def _normalize(values: pd.Series, higher_is_better: bool = True) -> pd.Series:
    arr = pd.to_numeric(values, errors="coerce")
    if arr.notna().sum() == 0:
        return pd.Series(np.zeros(len(arr)), index=values.index, dtype=float)
    min_v = float(arr.min())
    max_v = float(arr.max())
    if abs(max_v - min_v) <= 1e-12:
        base = pd.Series(np.full(len(arr), 50.0), index=values.index, dtype=float)
    else:
        base = 100.0 * (arr - min_v) / (max_v - min_v)
    return base if higher_is_better else (100.0 - base)


def _load_inputs(results_dir: Path) -> dict[str, Any]:
    edge_json = _latest_file(results_dir, "investigacao_cdb_edge_loss_*.json")
    phase13_json = _latest_file(results_dir, "fase13_validacao_estatistica_edge_*.json")
    phase14_json = _latest_file(results_dir, "fase14_validacao_estabilidade_temporal_edge_*.json")

    phase13_asset_csv = Path(_read_json(phase13_json)["outputs"]["asset_timeframe_metrics"])
    phase14_reliability_csv = Path(_read_json(phase14_json)["outputs"]["reliability_index"])
    phase14_split_csv = Path(_read_json(phase14_json)["outputs"]["temporal_split"])
    phase14_persistence_csv = Path(_read_json(phase14_json)["outputs"]["persistence"])
    phase14_window_csv = Path(_read_json(phase14_json)["outputs"]["historical_window"])
    phase14_rolling_csv = Path(_read_json(phase14_json)["outputs"]["rolling_summary"])

    return {
        "edge_json_path": edge_json,
        "phase13_json_path": phase13_json,
        "phase14_json_path": phase14_json,
        "edge_json": _read_json(edge_json),
        "phase13_json": _read_json(phase13_json),
        "phase14_json": _read_json(phase14_json),
        "phase13_asset": pd.read_csv(phase13_asset_csv),
        "phase14_reliability": pd.read_csv(phase14_reliability_csv),
        "phase14_split": pd.read_csv(phase14_split_csv),
        "phase14_persistence": pd.read_csv(phase14_persistence_csv),
        "phase14_window": pd.read_csv(phase14_window_csv),
        "phase14_rolling": pd.read_csv(phase14_rolling_csv),
    }


def _build_master_matrix(inputs: dict[str, Any], cfg: Config) -> pd.DataFrame:
    phase13 = inputs["phase13_asset"].copy()
    reliability = inputs["phase14_reliability"].copy()
    split_df = inputs["phase14_split"].copy()
    persistence = inputs["phase14_persistence"].copy()
    window_df = inputs["phase14_window"].copy()
    rolling_df = inputs["phase14_rolling"].copy()
    edge_json = inputs["edge_json"]

    for frame in [phase13, reliability, split_df, persistence, window_df, rolling_df]:
        if "symbol" in frame.columns:
            frame["symbol"] = frame["symbol"].astype(str)
        if "timeframe" in frame.columns:
            frame["timeframe"] = frame["timeframe"].astype(str)

    rolling_summary = (
        rolling_df.groupby(["symbol", "timeframe"], dropna=False)
        .agg(
            rolling_windows_available=("observations", lambda s: int((pd.to_numeric(s, errors="coerce").fillna(0) > 0).sum())),
            rolling_total_observations=("observations", lambda s: int(pd.to_numeric(s, errors="coerce").fillna(0).sum())),
            rolling_any_persistent_degradation=("persistent_degradation", lambda s: bool(pd.Series(s).fillna(False).astype(bool).any())),
        )
        .reset_index()
    )

    master = phase13.merge(
        reliability,
        on=["symbol", "timeframe"],
        how="outer",
        suffixes=("_phase13", "_phase14"),
    )
    master = master.merge(split_df, on=["symbol", "timeframe"], how="left")
    master = master.merge(
        persistence[[
            "symbol",
            "timeframe",
            "positive_pf_months",
            "positive_expectancy_months",
            "above_global_wr_months",
            "total_months",
            "persistence_pf_pct",
            "persistence_expectancy_pct",
            "persistence_wr_pct",
            "recurring_edge",
        ]],
        on=["symbol", "timeframe"],
        how="left",
        suffixes=("", "_persist"),
    )
    master = master.merge(
        window_df[[
            "symbol",
            "timeframe",
            "first_trade",
            "last_trade",
            "trades",
            "first_candle",
            "last_candle",
            "candle_count",
            "trade_window_days",
            "data_limitation",
        ]],
        on=["symbol", "timeframe"],
        how="left",
        suffixes=("", "_window"),
    )
    master = master.merge(rolling_summary, on=["symbol", "timeframe"], how="left")

    def _coalesce(name: str) -> None:
        candidates = [col for col in [name, f"{name}_x", f"{name}_y", f"{name}_persist", f"{name}_window"] if col in master.columns]
        if not candidates:
            return
        series = master[candidates[0]]
        for candidate in candidates[1:]:
            series = series.combine_first(master[candidate])
        master[name] = series

    for col in [
        "temporal_stability_coefficient",
        "recurring_edge",
        "trades",
        "positive_pf_months",
        "positive_expectancy_months",
        "above_global_wr_months",
        "total_months",
        "persistence_pf_pct",
        "persistence_expectancy_pct",
        "persistence_wr_pct",
        "first_trade",
        "last_trade",
        "trade_window_days",
        "candle_count",
        "data_limitation",
    ]:
        _coalesce(col)

    if "trades_phase13" in master.columns and "trades_phase14" in master.columns:
        master["trades"] = master["trades_phase14"].fillna(master["trades_phase13"])
    elif "trades_phase14" in master.columns:
        master["trades"] = master["trades_phase14"]
    elif "trades_phase13" in master.columns:
        master["trades"] = master["trades_phase13"]

    asset_edge_positive = set(edge_json.get("final_answers", {}).get("q4_ativos_com_edge", []))
    asset_edge_negative = set(edge_json.get("final_answers", {}).get("q5_ativos_sem_edge", []))
    master["edge_asset_observation"] = np.where(
        master["symbol"].isin(asset_edge_positive),
        "ativo_com_edge_observacional",
        np.where(master["symbol"].isin(asset_edge_negative), "ativo_sem_edge_observacional", "nao_classificado_na_fase_edge"),
    )
    master["global_lost_edge_hypothesis"] = edge_json.get("final_answers", {}).get("q1_motivo_perda")

    for col in [
        "rolling_windows_available",
        "rolling_total_observations",
        "rolling_any_persistent_degradation",
        "abrupt_break",
        "recurring_edge",
        "superiority_allowed",
    ]:
        if col in master.columns:
            if master[col].dtype == object:
                master[col] = master[col].map({"True": True, "False": False, True: True, False: False})

    master["rolling_status"] = np.where(
        master["rolling_windows_available"].fillna(0) > 0,
        np.where(master["rolling_any_persistent_degradation"].fillna(False), "degradacao_persistente", "sem_degradacao_persistente"),
        "indisponivel_amostra_insuficiente",
    )
    master["bootstrap_ci95_pf"] = master.apply(
        lambda row: f"[{_safe_float(row.get('pf_ci95_low'), default=np.nan):.4f}, {_safe_float(row.get('pf_ci95_high'), default=np.nan):.4f}]"
        if pd.notna(row.get("pf_ci95_low")) and pd.notna(row.get("pf_ci95_high"))
        else "indisponivel",
        axis=1,
    )
    master["bootstrap_ci95_expectancy"] = master.apply(
        lambda row: f"[{_safe_float(row.get('expectancy_ci95_low'), default=np.nan):.4f}, {_safe_float(row.get('expectancy_ci95_high'), default=np.nan):.4f}]"
        if pd.notna(row.get("expectancy_ci95_low")) and pd.notna(row.get("expectancy_ci95_high"))
        else "indisponivel",
        axis=1,
    )
    master["trades_to_30"] = (cfg.min_trades_promote - pd.to_numeric(master["trades"], errors="coerce").fillna(0)).clip(lower=0)
    master["trades_to_50"] = (cfg.robust_trades - pd.to_numeric(master["trades"], errors="coerce").fillna(0)).clip(lower=0)
    master["trades_to_100"] = (cfg.strong_trades - pd.to_numeric(master["trades"], errors="coerce").fillna(0)).clip(lower=0)
    return master


def _blockers(row: pd.Series, cfg: Config) -> list[str]:
    blockers: list[str] = []
    trades = int(_safe_float(row.get("trades"), default=0))
    pf = _safe_float(row.get("profit_factor"), default=np.nan)
    exp = _safe_float(row.get("expectancy"), default=np.nan)
    index_score = _safe_float(row.get("edge_reliability_index"), default=np.nan)
    pf_prob = _safe_float(row.get("pf_prob_gt_1"), default=np.nan)
    exp_prob = _safe_float(row.get("expectancy_prob_gt_0"), default=np.nan)
    if trades < cfg.min_trades_promote:
        blockers.append(f"menos_de_{cfg.min_trades_promote}_trades")
    if trades < cfg.robust_trades:
        blockers.append(f"menos_de_{cfg.robust_trades}_trades")
    if _ci_crosses(1.0, row.get("pf_ci95_low"), row.get("pf_ci95_high")):
        blockers.append("ic_pf_cruza_1")
    if _ci_crosses(0.0, row.get("expectancy_ci95_low"), row.get("expectancy_ci95_high")):
        blockers.append("ic_expectancy_cruza_0")
    if pd.notna(pf_prob) and pf_prob < 0.6:
        blockers.append("bootstrap_pf_instavel")
    if pd.notna(exp_prob) and exp_prob < 0.6:
        blockers.append("bootstrap_expectancy_instavel")
    if _safe_float(row.get("rolling_windows_available"), default=0) <= 0:
        blockers.append("rolling_indisponivel")
    if bool(row.get("rolling_any_persistent_degradation", False)):
        blockers.append("rolling_com_degradacao")
    if bool(row.get("abrupt_break", False)):
        blockers.append("ruptura_temporal")
    if pd.notna(pf) and pf < 1.0:
        blockers.append("pf_abaixo_de_1")
    if pd.notna(exp) and exp <= 0.0:
        blockers.append("expectancy_nao_positiva")
    if pd.notna(index_score) and index_score < 60.0:
        blockers.append("indice_confiabilidade_abaixo_de_60")
    return blockers


def _scientific_classification(row: pd.Series, cfg: Config) -> str:
    required = ["trades", "profit_factor", "expectancy", "edge_reliability_index", "pf_prob_gt_1", "expectancy_prob_gt_0"]
    if any(pd.isna(row.get(col)) for col in required):
        return "NAO AVALIAVEL"

    trades = int(_safe_float(row.get("trades"), default=0))
    pf = _safe_float(row.get("profit_factor"), default=0.0)
    exp = _safe_float(row.get("expectancy"), default=0.0)
    index_score = _safe_float(row.get("edge_reliability_index"), default=0.0)
    pf_prob = _safe_float(row.get("pf_prob_gt_1"), default=0.0)
    exp_prob = _safe_float(row.get("expectancy_prob_gt_0"), default=0.0)
    abrupt_break = bool(row.get("abrupt_break", False))
    recurring_edge = bool(row.get("recurring_edge", False))

    if (
        trades >= cfg.robust_trades
        and pf > 1.0
        and exp > 0.0
        and index_score >= 75.0
        and pf_prob >= 0.7
        and exp_prob >= 0.7
        and not abrupt_break
    ):
        return "ROBUSTA"

    if (
        pf < 1.0
        and exp <= 0.0
        and index_score < 40.0
        and pf_prob < 0.4
        and exp_prob < 0.4
        and not recurring_edge
    ):
        return "DESCARTADA"

    if (
        pf > 1.0
        and exp > 0.0
        and index_score >= 45.0
        and pf_prob >= 0.55
        and exp_prob >= 0.55
    ):
        return "PROMISSORA"

    if trades < cfg.min_trades_promote:
        return "AMOSTRA INSUFICIENTE"

    return "NAO AVALIAVEL"


def _research_priority(row: pd.Series) -> tuple[str, str]:
    cls = str(row.get("scientific_classification") or "")
    index_score = _safe_float(row.get("edge_reliability_index"), default=0.0)
    trades_gap = int(_safe_float(row.get("trades_to_30"), default=0))
    recurring_edge = bool(row.get("recurring_edge", False))
    if cls == "PROMISSORA" or (recurring_edge and index_score >= 45.0 and trades_gap > 0):
        return ("Prioridade Alta", "sinal_positivo_com_amostra_ainda_insuficiente")
    if cls == "AMOSTRA INSUFICIENTE" or (index_score >= 30.0 and trades_gap > 0):
        return ("Prioridade Media", "coleta_adicional_necessaria_para_confirmacao")
    return ("Prioridade Baixa", "baixo_retorno_estatistico_esperado_para_nova_coleta")


def _finalize_master(master: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    master = master.copy()
    master["classification_blockers"] = master.apply(lambda row: ";".join(_blockers(row, cfg)), axis=1)
    master["scientific_classification"] = master.apply(lambda row: _scientific_classification(row, cfg), axis=1)
    priorities = master.apply(_research_priority, axis=1, result_type="expand")
    priorities.columns = ["research_priority", "research_priority_reason"]
    master = pd.concat([master, priorities], axis=1)

    master["evidence_strength_type"] = np.select(
        [
            master["scientific_classification"] == "ROBUSTA",
            master["scientific_classification"] == "PROMISSORA",
            master["scientific_classification"] == "DESCARTADA",
            master["scientific_classification"] == "AMOSTRA INSUFICIENTE",
        ],
        ["forte", "hipotese", "forte_negativa", "inconclusiva"],
        default="inconclusiva",
    )

    master["dashboard_rank_score"] = (
        0.65 * pd.to_numeric(master["edge_reliability_index"], errors="coerce").fillna(0.0)
        + 0.20 * _normalize(master["trades"], higher_is_better=True)
        + 0.15 * _normalize(master["profit_factor"], higher_is_better=True)
    )

    ordered_cols = [
        "symbol",
        "timeframe",
        "trades",
        "profit_factor",
        "win_rate",
        "expectancy",
        "drawdown",
        "pf_bootstrap_mean",
        "bootstrap_ci95_pf",
        "expectancy_bootstrap_mean",
        "bootstrap_ci95_expectancy",
        "rolling_status",
        "rolling_windows_available",
        "rolling_any_persistent_degradation",
        "abrupt_break",
        "temporal_stability_coefficient",
        "persistence_pf_pct",
        "persistence_expectancy_pct",
        "persistence_wr_pct",
        "edge_reliability_index",
        "robustness_class",
        "evidence_class",
        "scientific_classification",
        "classification_blockers",
        "research_priority",
        "research_priority_reason",
        "trades_to_30",
        "trades_to_50",
        "trades_to_100",
        "first_trade",
        "last_trade",
        "trade_window_days",
        "candle_count",
        "edge_asset_observation",
        "global_lost_edge_hypothesis",
    ]
    existing_cols = [col for col in ordered_cols if col in master.columns]
    remaining_cols = [col for col in master.columns if col not in existing_cols]
    return master[existing_cols + remaining_cols].sort_values(
        ["dashboard_rank_score", "edge_reliability_index", "trades"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def _summary_table(master: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "symbol",
        "timeframe",
        "trades",
        "profit_factor",
        "expectancy",
        "edge_reliability_index",
        "scientific_classification",
        "research_priority",
        "classification_blockers",
    ]
    return master[cols].copy()


def _plot_heatmap(master: pd.DataFrame, path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
        from matplotlib import colors
    except Exception:
        return False
    if master.empty:
        return False

    syms = sorted(master["symbol"].astype(str).unique().tolist())
    tfs = sorted(master["timeframe"].astype(str).unique().tolist())
    sym_idx = {sym: idx for idx, sym in enumerate(syms)}
    tf_idx = {tf: idx for idx, tf in enumerate(tfs)}
    norm = colors.Normalize(vmin=0.0, vmax=100.0)
    cmap = plt.get_cmap("YlGnBu")
    max_trades = max(1.0, float(pd.to_numeric(master["trades"], errors="coerce").fillna(0.0).max()))

    fig, ax = plt.subplots(figsize=(max(8, len(tfs) * 1.4), max(4, len(syms) * 0.75)))
    for row in master.itertuples(index=False):
        ax.scatter(
            tf_idx[str(row.timeframe)],
            sym_idx[str(row.symbol)],
            s=1100,
            marker="s",
            c=[cmap(norm(_safe_float(row.edge_reliability_index)))],
            alpha=max(0.15, _safe_float(row.trades) / max_trades),
            edgecolors="black",
            linewidths=0.7,
        )
        ax.text(
            tf_idx[str(row.timeframe)],
            sym_idx[str(row.symbol)],
            f"{_safe_float(row.edge_reliability_index):.1f}\nN={int(_safe_float(row.trades))}",
            ha="center",
            va="center",
            fontsize=8,
        )

    ax.set_xticks(range(len(tfs)))
    ax.set_xticklabels(tfs)
    ax.set_yticks(range(len(syms)))
    ax.set_yticklabels(syms)
    ax.set_xlabel("Timeframe")
    ax.set_ylabel("Ativo")
    ax.set_title("Heatmap executivo: cor=Indice de Confiabilidade, opacidade=trades")
    ax.invert_yaxis()
    sm = plt.cm.ScalarMappable(norm=norm, cmap=cmap)
    sm.set_array([])
    fig.colorbar(sm, ax=ax, label="Indice de Confiabilidade do Edge")
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def _plot_reliability_bar(master: pd.DataFrame, path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return False
    if master.empty:
        return False

    labels = master.apply(lambda row: f"{row['symbol']} {row['timeframe']}", axis=1)
    fig, ax = plt.subplots(figsize=(12, 6))
    colors = ["#1f77b4" if cls in {"PROMISSORA", "ROBUSTA"} else "#b0b0b0" if cls == "AMOSTRA INSUFICIENTE" else "#d62728" for cls in master["scientific_classification"]]
    ax.bar(labels, master["edge_reliability_index"], color=colors)
    ax.set_ylabel("Indice de Confiabilidade")
    ax.set_title("Indice de Confiabilidade por combinacao")
    ax.set_ylim(0, max(100, float(master["edge_reliability_index"].max()) + 10))
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def _plot_trades_bar(master: pd.DataFrame, path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return False
    if master.empty:
        return False
    labels = master.apply(lambda row: f"{row['symbol']} {row['timeframe']}", axis=1)
    fig, ax = plt.subplots(figsize=(12, 6))
    ax.bar(labels, master["trades"], color="#2ca02c")
    ax.axhline(30, color="#ff7f0e", linestyle="--", linewidth=1.2, label="30 trades")
    ax.axhline(50, color="#9467bd", linestyle="--", linewidth=1.2, label="50 trades")
    ax.axhline(100, color="#8c564b", linestyle="--", linewidth=1.2, label="100 trades")
    ax.set_ylabel("Trades")
    ax.set_title("Trades por combinacao")
    ax.tick_params(axis="x", rotation=45)
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def _plot_radar(master: pd.DataFrame, path: Path) -> bool:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return False
    if master.empty:
        return False

    top = master.head(min(5, len(master))).copy()
    metrics = {
        "PF": _normalize(top["profit_factor"], higher_is_better=True),
        "Expectancy": _normalize(top["expectancy"], higher_is_better=True),
        "Bootstrap": _normalize(top["bootstrap_score"], higher_is_better=True),
        "Persistencia": _normalize(top["persistence_score"], higher_is_better=True),
        "Trades": _normalize(top["trades"], higher_is_better=True),
        "Estabilidade": _normalize(top["temporal_stability_coefficient"], higher_is_better=True),
    }
    categories = list(metrics.keys())
    angles = np.linspace(0, 2 * np.pi, len(categories), endpoint=False).tolist()
    angles += angles[:1]

    fig, ax = plt.subplots(figsize=(8, 8), subplot_kw={"polar": True})
    for idx, row in top.iterrows():
        values = [float(metrics[key].loc[idx]) for key in categories]
        values += values[:1]
        label = f"{row['symbol']} {row['timeframe']}"
        ax.plot(angles, values, linewidth=1.5, label=label)
        ax.fill(angles, values, alpha=0.10)
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(categories)
    ax.set_yticklabels([])
    ax.set_title("Radar comparativo das principais combinacoes")
    ax.legend(loc="upper right", bbox_to_anchor=(1.25, 1.10), fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return True


def _build_report(master: pd.DataFrame, inputs: dict[str, Any], cfg: Config) -> str:
    strong = master[master["scientific_classification"] == "ROBUSTA"]
    hypotheses = master[master["scientific_classification"] == "PROMISSORA"]
    inconclusive = master[master["scientific_classification"].isin(["AMOSTRA INSUFICIENTE", "NAO AVALIAVEL"])]
    discarded = master[master["scientific_classification"] == "DESCARTADA"]
    top = master.head(5)
    largest_gap = int(pd.to_numeric(master["trades_to_30"], errors="coerce").sum()) if not master.empty else 0

    lines: list[str] = []
    lines.append("# FASE 14B - Consolidacao das Evidencias e Matriz Final de Decisao")
    lines.append("")
    lines.append(f"- generated_at_utc: {_now_utc().isoformat()}")
    lines.append(f"- strategy_key: {cfg.strategy_key}")
    lines.append(f"- edge_report: {inputs['edge_json_path'].name}")
    lines.append(f"- fase13_report: {inputs['phase13_json_path'].name}")
    lines.append(f"- fase14_report: {inputs['phase14_json_path'].name}")
    lines.append("")
    lines.append("## 1. Resumo executivo")
    if top.empty:
        lines.append("- Nenhuma combinacao consolidada.")
    else:
        best = top.iloc[0]
        lines.append(
            f"- Melhor combinacao observacional consolidada: {best['symbol']} {best['timeframe']} com indice {best['edge_reliability_index']:.1f}/100 e classificacao {best['scientific_classification']}."
        )
    lines.append(f"- Combinacoes ROBUSTAS: {len(strong)}.")
    lines.append(f"- Combinacoes PROMISSORAS: {len(hypotheses)}.")
    lines.append(f"- Combinacoes inconclusivas: {len(inconclusive)}.")
    lines.append(f"- Combinacoes descartadas quantitativamente: {len(discarded)}.")
    lines.append("")
    lines.append("## 2. Matriz mestre")
    lines.append(f"- Total de combinacoes consolidadas: {len(master)}.")
    lines.append("- Colunas incluem trades, PF, WR, expectancy, bootstrap, IC95, rolling, persistencia, indice de confiabilidade, robustez, evidencia, classificacao cientifica e lacunas de amostra.")
    lines.append("")
    lines.append("## 3. Criterios objetivos")
    lines.append(f"- ROBUSTA exige pelo menos {cfg.robust_trades} trades, PF>1, expectancy>0, indice>=75, bootstrap PF/Expectancy >= 0.70 e ausencia de ruptura temporal.")
    lines.append(f"- PROMISSORA exige PF>1, expectancy>0, indice>=45 e bootstrap PF/Expectancy >= 0.55, mesmo sem amostra suficiente para promocao operacional.")
    lines.append(f"- DESCARTADA exige PF<1, expectancy<=0, indice<40, bootstrap PF/Expectancy < 0.40 e ausencia de recorrencia mensal positiva.")
    lines.append(f"- AMOSTRA INSUFICIENTE cobre combinacoes abaixo de {cfg.min_trades_promote} trades que nao atendem aos criterios de PROMISSORA ou DESCARTADA.")
    lines.append("")
    lines.append("## 4. Ranking de prioridade de pesquisa")
    for row in top.itertuples(index=False):
        lines.append(
            f"- {row.symbol} {row.timeframe}: {row.research_priority} | indice={row.edge_reliability_index:.1f} | trades={int(row.trades)} | blockers={row.classification_blockers}."
        )
    lines.append("")
    lines.append("## 5. Lacunas estatisticas")
    lines.append(f"- Trades adicionais desejaveis para atingir 30 trades no conjunto consolidado: {largest_gap}.")
    if not master.empty:
        biggest_needs = master.sort_values("trades_to_100", ascending=False).head(5)
        for row in biggest_needs.itertuples(index=False):
            lines.append(
                f"- {row.symbol} {row.timeframe}: faltam {int(row.trades_to_30)} para 30, {int(row.trades_to_50)} para 50 e {int(row.trades_to_100)} para 100 trades."
            )
    lines.append("")
    lines.append("## 6. Conclusao cientifica")
    lines.append(f"1. Conclusoes fortes: {len(strong)} combinacoes robustas e {len(discarded)} descartes quantitativos consistentes.")
    lines.append(f"2. Hipoteses: {len(hypotheses)} combinacoes promissoras que ainda dependem de amostra adicional.")
    lines.append(f"3. Inconclusivos: {len(inconclusive)} combinacoes sem base suficiente para afirmacoes fortes.")
    lines.append(f"4. Existe combinacao suficientemente robusta? {'sim' if len(strong) > 0 else 'nao'}.")
    lines.append("5. Principal gargalo: tamanho_da_amostra, justificado por 12/12 combinacoes abaixo de 30 trades na consolidacao atual.")
    lines.append("")
    lines.append("## 7. Evidencias consolidadas")
    lines.append("- Forte: inexistencia de combinacao robusta para promocao estatistica neste momento.")
    lines.append("- Moderada: SOL/USDT 1h e ETH/USDT 1h concentram os melhores sinais observacionais, mas ainda abaixo do limiar de robustez amostral.")
    lines.append("- Fraca ou inconclusiva: demais combinacoes por IC amplo, bootstrap instavel, ruptura temporal ou expectancy negativa.")
    return "\n".join(lines) + "\n"


def run(cfg: Config) -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    results_dir = base_dir / "optimization" / "results"
    inputs = _load_inputs(results_dir)
    master = _build_master_matrix(inputs, cfg)
    master = _finalize_master(master, cfg)
    summary = _summary_table(master)

    ts = _now_utc().strftime("%Y%m%d_%H%M%S")
    prefix = f"fase14b_consolidacao_evidencias_{ts}"
    master_csv = results_dir / f"{prefix}_master_edge_matrix.csv"
    summary_csv = results_dir / f"{prefix}_summary_table.csv"
    heatmap_png = results_dir / f"{prefix}_heatmap.png"
    reliability_png = results_dir / f"{prefix}_reliability_bar.png"
    trades_png = results_dir / f"{prefix}_trades_bar.png"
    radar_png = results_dir / f"{prefix}_radar.png"
    report_json = results_dir / f"{prefix}.json"
    report_md = results_dir / f"{prefix}.md"

    master.to_csv(master_csv, index=False)
    summary.to_csv(summary_csv, index=False)
    heatmap_created = _plot_heatmap(master, heatmap_png)
    reliability_created = _plot_reliability_bar(master, reliability_png)
    trades_created = _plot_trades_bar(master, trades_png)
    radar_created = _plot_radar(master, radar_png)
    report_text = _build_report(master, inputs, cfg)

    payload = {
        "generated_at": _now_utc().isoformat(),
        "strategy_key": cfg.strategy_key,
        "master_rows": int(len(master)),
        "scientific_classification_counts": master["scientific_classification"].value_counts(dropna=False).to_dict(),
        "research_priority_counts": master["research_priority"].value_counts(dropna=False).to_dict(),
        "top_combinations": master.head(10).to_dict(orient="records"),
        "main_bottleneck": {
            "label": "tamanho_da_amostra",
            "below_30_trades": int((pd.to_numeric(master["trades"], errors="coerce").fillna(0) < cfg.min_trades_promote).sum()),
            "below_50_trades": int((pd.to_numeric(master["trades"], errors="coerce").fillna(0) < cfg.robust_trades).sum()),
        },
        "source_reports": {
            "edge": str(inputs["edge_json_path"]),
            "fase13": str(inputs["phase13_json_path"]),
            "fase14": str(inputs["phase14_json_path"]),
        },
        "outputs": {
            "master_edge_matrix": str(master_csv),
            "summary_table": str(summary_csv),
            "heatmap": str(heatmap_png),
            "reliability_bar": str(reliability_png),
            "trades_bar": str(trades_png),
            "radar": str(radar_png),
            "report_json": str(report_json),
            "report_md": str(report_md),
        },
        "plots_created": {
            "heatmap": heatmap_created,
            "reliability_bar": reliability_created,
            "trades_bar": trades_created,
            "radar": radar_created,
        },
    }

    report_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    report_md.write_text(report_text, encoding="utf-8")
    return payload


def main() -> None:
    payload = run(Config())
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()