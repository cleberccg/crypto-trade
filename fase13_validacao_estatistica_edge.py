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
    bootstrap_iterations: int = 10_000
    random_seed: int = 42
    rolling_windows: tuple[int, ...] = (20, 30, 40)


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
        if not math.isfinite(f):
            return default
        return f
    except Exception:
        return default


def _profit_factor_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    gp = float(pnl[pnl > 0].sum())
    gl = abs(float(pnl[pnl < 0].sum()))
    if gl <= 0.0:
        return 999.0 if gp > 0.0 else 0.0
    return gp / gl


def _expectancy_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    return float(np.mean(pnl))


def _win_rate_from_pnl(pnl: np.ndarray) -> float:
    if pnl.size == 0:
        return 0.0
    return float(np.mean(pnl > 0))


def _drawdown_from_pnl(pnl: np.ndarray, initial: float = 10_000.0) -> float:
    if pnl.size == 0:
        return 0.0
    equity = initial + np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    peak = np.where(peak == 0.0, np.nan, peak)
    dd = (peak - equity) / peak
    dd = np.nan_to_num(dd, nan=0.0, posinf=0.0, neginf=0.0)
    return float(np.max(dd))


def _stderr(values: np.ndarray) -> float:
    if values.size <= 1:
        return 0.0
    return float(np.std(values, ddof=1) / math.sqrt(values.size))


def _ci95_mean(values: np.ndarray) -> tuple[float, float]:
    if values.size == 0:
        return (np.nan, np.nan)
    mu = float(np.mean(values))
    se = _stderr(values)
    return (mu - 1.96 * se, mu + 1.96 * se)


def _classify_cohens_d(abs_d: float) -> str:
    if abs_d < 0.2:
        return "pequeno"
    if abs_d < 0.5:
        return "medio"
    return "grande"


def _classify_cliffs_delta(abs_delta: float) -> str:
    if abs_delta < 0.147:
        return "pequeno"
    if abs_delta < 0.33:
        return "medio"
    return "grande"


def _cohens_d(win: np.ndarray, loss: np.ndarray) -> float:
    n1, n2 = win.size, loss.size
    if n1 < 2 or n2 < 2:
        return 0.0
    s1 = np.var(win, ddof=1)
    s2 = np.var(loss, ddof=1)
    pooled = ((n1 - 1) * s1 + (n2 - 1) * s2) / max(1, (n1 + n2 - 2))
    if pooled <= 0.0:
        return 0.0
    return float((np.mean(win) - np.mean(loss)) / math.sqrt(pooled))


def _cliffs_delta(win: np.ndarray, loss: np.ndarray) -> float:
    if win.size == 0 or loss.size == 0:
        return 0.0
    # O(n*m) is acceptable for this dataset size.
    greater = 0
    lower = 0
    for w in win:
        greater += int(np.sum(w > loss))
        lower += int(np.sum(w < loss))
    total = win.size * loss.size
    return float((greater - lower) / total)


def _bootstrap_metrics(pnl: np.ndarray, n_iter: int, seed: int) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = pnl.size
    if n == 0:
        return pd.DataFrame(columns=["pf", "wr", "expectancy"])

    idx = rng.integers(0, n, size=(n_iter, n))
    samples = pnl[idx]

    pf = np.empty(n_iter, dtype=float)
    wr = np.empty(n_iter, dtype=float)
    exp = np.empty(n_iter, dtype=float)

    for i in range(n_iter):
        s = samples[i]
        pf[i] = _profit_factor_from_pnl(s)
        wr[i] = _win_rate_from_pnl(s)
        exp[i] = _expectancy_from_pnl(s)

    return pd.DataFrame({"pf": pf, "wr": wr, "expectancy": exp})


def _bootstrap_ci(values: np.ndarray) -> tuple[float, float, float]:
    if values.size == 0:
        return (np.nan, np.nan, np.nan)
    return (
        float(np.mean(values)),
        float(np.quantile(values, 0.025)),
        float(np.quantile(values, 0.975)),
    )


def _rolling_metrics(df: pd.DataFrame, window: int) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    if len(df) < window:
        return pd.DataFrame(records)

    for i in range(window - 1, len(df)):
        part = df.iloc[i - window + 1 : i + 1]
        pnl = pd.to_numeric(part["pnl"], errors="coerce").fillna(0.0).to_numpy()
        records.append(
            {
                "window": window,
                "end_idx": i,
                "end_time": part["exit_time"].iloc[-1],
                "profit_factor": _profit_factor_from_pnl(pnl),
                "win_rate": _win_rate_from_pnl(pnl),
                "expectancy": _expectancy_from_pnl(pnl),
                "drawdown": _drawdown_from_pnl(pnl),
            }
        )
    return pd.DataFrame(records)


def _timeframe_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for tf, chunk in df.groupby("timeframe", dropna=False):
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0).to_numpy()
        pnl_pct = pd.to_numeric(chunk["pnl_percent"], errors="coerce").fillna(0.0).to_numpy()
        mfe = pd.to_numeric(chunk.get("mfe"), errors="coerce").to_numpy()
        mae = pd.to_numeric(chunk.get("mae"), errors="coerce").to_numpy()
        rows.append(
            {
                "timeframe": str(tf),
                "trades": int(len(chunk)),
                "win_rate": _win_rate_from_pnl(pnl),
                "profit_factor": _profit_factor_from_pnl(pnl),
                "expectancy": _expectancy_from_pnl(pnl),
                "drawdown": _drawdown_from_pnl(pnl),
                "avg_mfe": float(np.nanmean(mfe)) if mfe.size else np.nan,
                "avg_mae": float(np.nanmean(mae)) if mae.size else np.nan,
                "mean_return": float(np.mean(pnl_pct)) if pnl_pct.size else np.nan,
                "std_return": float(np.std(pnl_pct, ddof=1)) if pnl_pct.size > 1 else 0.0,
            }
        )
    return pd.DataFrame(rows).sort_values("profit_factor", ascending=False).reset_index(drop=True)


def _asset_timeframe_metrics(df: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (symbol, timeframe), chunk in df.groupby(["symbol", "timeframe"], dropna=False):
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0).to_numpy()
        rows.append(
            {
                "symbol": str(symbol),
                "timeframe": str(timeframe),
                "trades": int(len(chunk)),
                "profit_factor": _profit_factor_from_pnl(pnl),
                "win_rate": _win_rate_from_pnl(pnl),
                "expectancy": _expectancy_from_pnl(pnl),
                "drawdown": _drawdown_from_pnl(pnl),
            }
        )
    return pd.DataFrame(rows).sort_values(["profit_factor", "expectancy"], ascending=[False, False]).reset_index(drop=True)


def _win_loss_ci_effects(df: pd.DataFrame, features: list[str], n_iter: int, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    win = df[df["result"] == "WIN"]
    loss = df[df["result"] == "LOSS"]

    rows_ci: list[dict[str, Any]] = []
    rows_eff: list[dict[str, Any]] = []

    rng = np.random.default_rng(seed)

    for feat in features:
        w = pd.to_numeric(win[feat], errors="coerce").dropna().to_numpy()
        l = pd.to_numeric(loss[feat], errors="coerce").dropna().to_numpy()

        w_mean = float(np.mean(w)) if w.size else np.nan
        l_mean = float(np.mean(l)) if l.size else np.nan
        w_med = float(np.median(w)) if w.size else np.nan
        l_med = float(np.median(l)) if l.size else np.nan
        w_se = _stderr(w) if w.size else np.nan
        l_se = _stderr(l) if l.size else np.nan
        w_ci = _ci95_mean(w) if w.size else (np.nan, np.nan)
        l_ci = _ci95_mean(l) if l.size else (np.nan, np.nan)

        diff = w_mean - l_mean if (w.size and l.size) else np.nan

        # Bootstrap CI for mean difference.
        if w.size and l.size:
            iw = rng.integers(0, w.size, size=(n_iter, w.size))
            il = rng.integers(0, l.size, size=(n_iter, l.size))
            dw = w[iw].mean(axis=1)
            dl = l[il].mean(axis=1)
            d = dw - dl
            d_lo = float(np.quantile(d, 0.025))
            d_hi = float(np.quantile(d, 0.975))
            relevant = not (d_lo <= 0.0 <= d_hi)
        else:
            d_lo = np.nan
            d_hi = np.nan
            relevant = False

        rows_ci.append(
            {
                "feature": feat,
                "win_n": int(w.size),
                "loss_n": int(l.size),
                "win_mean": w_mean,
                "win_median": w_med,
                "win_se": w_se,
                "win_ci95_low": w_ci[0],
                "win_ci95_high": w_ci[1],
                "loss_mean": l_mean,
                "loss_median": l_med,
                "loss_se": l_se,
                "loss_ci95_low": l_ci[0],
                "loss_ci95_high": l_ci[1],
                "mean_diff_win_minus_loss": diff,
                "diff_ci95_low": d_lo,
                "diff_ci95_high": d_hi,
                "statistically_relevant": bool(relevant),
            }
        )

        d = _cohens_d(w, l)
        cd = _cliffs_delta(w, l)
        rows_eff.append(
            {
                "feature": feat,
                "cohens_d": d,
                "cohens_d_abs": abs(d),
                "cohens_d_class": _classify_cohens_d(abs(d)),
                "cliffs_delta": cd,
                "cliffs_delta_abs": abs(cd),
                "cliffs_delta_class": _classify_cliffs_delta(abs(cd)),
                "discriminatory_power": (
                    "grande"
                    if (_classify_cohens_d(abs(d)) == "grande" or _classify_cliffs_delta(abs(cd)) == "grande")
                    else "medio"
                    if (_classify_cohens_d(abs(d)) == "medio" or _classify_cliffs_delta(abs(cd)) == "medio")
                    else "pequeno"
                ),
            }
        )

    ci_df = pd.DataFrame(rows_ci).sort_values("statistically_relevant", ascending=False).reset_index(drop=True)
    eff_df = pd.DataFrame(rows_eff).sort_values(["cohens_d_abs", "cliffs_delta_abs"], ascending=[False, False]).reset_index(drop=True)
    return ci_df, eff_df


def _overfitting_check(
    df: pd.DataFrame,
    bootstrap_summary: pd.DataFrame,
    rolling_all: pd.DataFrame,
    asset_tf: pd.DataFrame,
) -> dict[str, Any]:
    # Split check
    ordered = df.sort_values("exit_time").reset_index(drop=True)
    mid = len(ordered) // 2
    first = ordered.iloc[:mid]
    second = ordered.iloc[mid:]

    def metrics(chunk: pd.DataFrame) -> dict[str, float]:
        pnl = pd.to_numeric(chunk["pnl"], errors="coerce").fillna(0.0).to_numpy()
        return {
            "pf": _profit_factor_from_pnl(pnl),
            "wr": _win_rate_from_pnl(pnl),
            "exp": _expectancy_from_pnl(pnl),
        }

    m_first = metrics(first)
    m_second = metrics(second)

    # Consistency with bootstrap CI.
    b_pf = bootstrap_summary.loc[bootstrap_summary["metric"] == "profit_factor"].iloc[0]
    b_wr = bootstrap_summary.loc[bootstrap_summary["metric"] == "win_rate"].iloc[0]
    b_ex = bootstrap_summary.loc[bootstrap_summary["metric"] == "expectancy"].iloc[0]

    full = metrics(ordered)
    inside_bootstrap = {
        "pf_inside_ci95": bool(b_pf["ci95_low"] <= full["pf"] <= b_pf["ci95_high"]),
        "wr_inside_ci95": bool(b_wr["ci95_low"] <= full["wr"] <= b_wr["ci95_high"]),
        "exp_inside_ci95": bool(b_ex["ci95_low"] <= full["exp"] <= b_ex["ci95_high"]),
    }

    # Rolling trend: slope of expectancy by end index for each window.
    trend_rows: list[dict[str, Any]] = []
    for w, part in rolling_all.groupby("window"):
        if len(part) < 3:
            slope = np.nan
        else:
            x = part["end_idx"].to_numpy(dtype=float)
            y = part["expectancy"].to_numpy(dtype=float)
            x_mean = x.mean()
            y_mean = y.mean()
            den = np.sum((x - x_mean) ** 2)
            slope = float(np.sum((x - x_mean) * (y - y_mean)) / den) if den > 0 else np.nan
        trend_rows.append({"window": int(w), "expectancy_slope": slope})

    trend_df = pd.DataFrame(trend_rows)
    deteriorating = bool((trend_df["expectancy_slope"] < 0).sum() >= max(1, len(trend_df) // 2))

    # Asset-level check for prior conclusion.
    asset_summary = (
        asset_tf.groupby("symbol", as_index=False)
        .agg(
            trades=("trades", "sum"),
            profit_factor=("profit_factor", "mean"),
            expectancy=("expectancy", "mean"),
            win_rate=("win_rate", "mean"),
        )
        .sort_values("profit_factor", ascending=False)
        .reset_index(drop=True)
    )

    return {
        "sample_split": {
            "first_half": m_first,
            "second_half": m_second,
            "consistent_direction": bool((m_second["pf"] <= m_first["pf"]) and (m_second["exp"] <= m_first["exp"])),
        },
        "bootstrap_consistency": inside_bootstrap,
        "rolling_expectancy_trend": trend_df.to_dict(orient="records"),
        "rolling_indicates_deterioration": deteriorating,
        "asset_summary": asset_summary.to_dict(orient="records"),
    }


def _load_latest_enriched(results_dir: Path) -> tuple[pd.DataFrame, Path]:
    files = sorted(results_dir.glob("investigacao_cdb_edge_loss_*_enriched_trades.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise RuntimeError("Arquivo enriched_trades da investigacao anterior nao encontrado.")
    latest = files[-1]
    df = pd.read_csv(latest)
    if "entry_time" in df.columns:
        df["entry_time"] = pd.to_datetime(df["entry_time"], utc=True, errors="coerce")
    if "exit_time" in df.columns:
        df["exit_time"] = pd.to_datetime(df["exit_time"], utc=True, errors="coerce")
    if "result" not in df.columns:
        df["result"] = np.where(pd.to_numeric(df.get("pnl"), errors="coerce").fillna(0.0) > 0, "WIN", "LOSS")
    return df, latest


def _plot_rolling(rolling_all: pd.DataFrame, out_png: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return

    if rolling_all.empty:
        return

    fig, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=False)
    metrics = ["profit_factor", "win_rate", "expectancy", "drawdown"]
    titles = ["Profit Factor", "Win Rate", "Expectancy", "Drawdown"]

    for ax, metric, title in zip(axes.ravel(), metrics, titles):
        for w, part in rolling_all.groupby("window"):
            ax.plot(part["end_idx"], part[metric], label=f"w={w}")
        ax.set_title(title)
        ax.grid(alpha=0.3)
        ax.legend()

    plt.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


def _bootstrap_histograms(boot_df: pd.DataFrame, out_png: Path) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return

    if boot_df.empty:
        return

    fig, axes = plt.subplots(1, 3, figsize=(16, 4))
    cols = ["pf", "wr", "expectancy"]
    titles = ["Bootstrap Profit Factor", "Bootstrap Win Rate", "Bootstrap Expectancy"]

    for ax, col, title in zip(axes, cols, titles):
        ax.hist(boot_df[col], bins=60, alpha=0.8)
        ax.set_title(title)
        ax.grid(alpha=0.3)

    plt.tight_layout()
    fig.savefig(out_png, dpi=150)
    plt.close(fig)


def _executive_report(
    timeframe_df: pd.DataFrame,
    bootstrap_summary: pd.DataFrame,
    ci_df: pd.DataFrame,
    effects_df: pd.DataFrame,
    rolling_all: pd.DataFrame,
    asset_tf: pd.DataFrame,
    overfit: dict[str, Any],
) -> str:
    lines: list[str] = []
    lines.append("# FASE 13 - Validacao Estatistica e Localizacao do Edge")
    lines.append("")
    lines.append(f"- generated_at_utc: {_now_utc().isoformat()}")
    lines.append("")

    lines.append("## 1. Principais descobertas")
    if not timeframe_df.empty:
        best_tf = timeframe_df.iloc[0]
        lines.append(
            f"- Timeframe com melhor PF na amostra: {best_tf['timeframe']} "
            f"(PF={_safe_float(best_tf['profit_factor']):.4f}, WR={_safe_float(best_tf['win_rate'])*100.0:.2f}%, trades={int(best_tf['trades'])})."
        )
    if not asset_tf.empty:
        best_ctx = asset_tf.iloc[0]
        lines.append(
            f"- Melhor combinacao Ativo+Timeframe: {best_ctx['symbol']} {best_ctx['timeframe']} "
            f"(PF={_safe_float(best_ctx['profit_factor']):.4f}, Expectancy={_safe_float(best_ctx['expectancy']):.4f}, trades={int(best_ctx['trades'])})."
        )
    if not rolling_all.empty:
        trend = overfit.get("rolling_indicates_deterioration")
        lines.append(f"- Evolucao rolling do edge: {'deteriorando' if trend else 'nao deteriorando claramente'}.")

    lines.append("")
    lines.append("## 2. Evidencias fortes")
    strong_eff = effects_df[effects_df["discriminatory_power"] == "grande"]["feature"].tolist()
    if strong_eff:
        lines.append(f"- Variaveis com alto poder discriminatorio (effect size): {strong_eff}.")
    strong_ci = ci_df[ci_df["statistically_relevant"] == True]["feature"].tolist()
    if strong_ci:
        lines.append(f"- Diferencas WIN vs LOSS com IC95% da diferenca sem cruzar zero: {strong_ci}.")

    lines.append("")
    lines.append("## 3. Evidencias moderadas")
    medium_eff = effects_df[effects_df["discriminatory_power"] == "medio"]["feature"].tolist()
    lines.append(f"- Variaveis com efeito medio: {medium_eff}.")

    lines.append("")
    lines.append("## 4. Evidencias fracas")
    weak_eff = effects_df[effects_df["discriminatory_power"] == "pequeno"]["feature"].tolist()
    lines.append(f"- Variaveis com baixo poder discriminatorio: {weak_eff}.")

    lines.append("")
    lines.append("## 5. Limitacoes da amostra")
    lines.append("- Amostra concentrada em contexto operacional recente da campanha oficial.")
    lines.append("- Possivel desbalanceamento por ativo/timeframe em numero de trades.")
    lines.append("- Analises bootstrap e rolling reduzem risco de overfitting, mas nao eliminam mudanca estrutural futura de mercado.")

    lines.append("")
    lines.append("## 6. Grau de confianca por conclusao")
    bpf = bootstrap_summary[bootstrap_summary["metric"] == "profit_factor"].iloc[0]
    bwr = bootstrap_summary[bootstrap_summary["metric"] == "win_rate"].iloc[0]
    bex = bootstrap_summary[bootstrap_summary["metric"] == "expectancy"].iloc[0]
    lines.append(
        "- Robustez global da amostra (bootstrap): "
        f"PF mean={_safe_float(bpf['bootstrap_mean']):.4f} IC95=[{_safe_float(bpf['ci95_low']):.4f},{_safe_float(bpf['ci95_high']):.4f}], "
        f"WR mean={_safe_float(bwr['bootstrap_mean'])*100.0:.2f}% IC95=[{_safe_float(bwr['ci95_low'])*100.0:.2f}%,{_safe_float(bwr['ci95_high'])*100.0:.2f}%], "
        f"Expectancy mean={_safe_float(bex['bootstrap_mean']):.4f} IC95=[{_safe_float(bex['ci95_low']):.4f},{_safe_float(bex['ci95_high']):.4f}]."
    )
    lines.append(
        "- Consistencia entre metodos (split/bootstrap/rolling): "
        f"{overfit.get('sample_split', {}).get('consistent_direction')} / {overfit.get('bootstrap_consistency')} / "
        f"rolling_deterioration={overfit.get('rolling_indicates_deterioration')}"
    )

    lines.append("")
    lines.append("## 7. Recomendacoes para futuras investigacoes")
    lines.append("- Ampliar horizonte temporal da campanha para aumentar poder estatistico por ativo+timeframe.")
    lines.append("- Repetir FASE 13 periodicamente com janela deslizante para monitorar deriva de edge.")
    lines.append("- Avaliar estabilidade dos resultados por sessao/execucao_id para isolar efeitos de microestrutura.")

    return "\n".join(lines) + "\n"


def run(cfg: Config) -> dict[str, Any]:
    base_dir = Path(__file__).resolve().parent
    results_dir = base_dir / "optimization" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    df, source_file = _load_latest_enriched(results_dir)
    df = df.sort_values("exit_time").reset_index(drop=True)

    timeframe_df = _timeframe_metrics(df)

    pnl = pd.to_numeric(df["pnl"], errors="coerce").fillna(0.0).to_numpy()
    boot_df = _bootstrap_metrics(pnl, cfg.bootstrap_iterations, cfg.random_seed)
    b_pf = _bootstrap_ci(boot_df["pf"].to_numpy())
    b_wr = _bootstrap_ci(boot_df["wr"].to_numpy())
    b_ex = _bootstrap_ci(boot_df["expectancy"].to_numpy())

    bootstrap_summary = pd.DataFrame(
        [
            {
                "metric": "profit_factor",
                "bootstrap_mean": b_pf[0],
                "ci95_low": b_pf[1],
                "ci95_high": b_pf[2],
            },
            {
                "metric": "win_rate",
                "bootstrap_mean": b_wr[0],
                "ci95_low": b_wr[1],
                "ci95_high": b_wr[2],
            },
            {
                "metric": "expectancy",
                "bootstrap_mean": b_ex[0],
                "ci95_low": b_ex[1],
                "ci95_high": b_ex[2],
            },
        ]
    )

    compare_features = ["adx", "relative_volume", "macd", "atr", "candle_body", "candle_range", "volume"]
    compare_features = [f for f in compare_features if f in df.columns]
    ci_df, effects_df = _win_loss_ci_effects(df, compare_features, cfg.bootstrap_iterations, cfg.random_seed)

    rolling_tables: list[pd.DataFrame] = []
    for w in cfg.rolling_windows:
        rolling_tables.append(_rolling_metrics(df, w))
    rolling_all = pd.concat(rolling_tables, ignore_index=True) if rolling_tables else pd.DataFrame()

    asset_tf = _asset_timeframe_metrics(df)

    overfit = _overfitting_check(df, bootstrap_summary, rolling_all, asset_tf)

    ts = _now_utc().strftime("%Y%m%d_%H%M%S")
    prefix = f"fase13_validacao_estatistica_edge_{ts}"

    out_timeframe = results_dir / f"{prefix}_timeframe_metrics.csv"
    out_boot_dist = results_dir / f"{prefix}_bootstrap_distribution.csv"
    out_boot_summary = results_dir / f"{prefix}_bootstrap_summary.csv"
    out_ci = results_dir / f"{prefix}_win_loss_ci.csv"
    out_eff = results_dir / f"{prefix}_effect_sizes.csv"
    out_roll = results_dir / f"{prefix}_rolling_windows.csv"
    out_asset_tf = results_dir / f"{prefix}_asset_timeframe_metrics.csv"
    out_json = results_dir / f"{prefix}.json"
    out_md = results_dir / f"{prefix}.md"
    out_png_roll = results_dir / f"{prefix}_rolling.png"
    out_png_boot = results_dir / f"{prefix}_bootstrap_hist.png"

    timeframe_df.to_csv(out_timeframe, index=False)
    boot_df.to_csv(out_boot_dist, index=False)
    bootstrap_summary.to_csv(out_boot_summary, index=False)
    ci_df.to_csv(out_ci, index=False)
    effects_df.to_csv(out_eff, index=False)
    rolling_all.to_csv(out_roll, index=False)
    asset_tf.to_csv(out_asset_tf, index=False)

    _plot_rolling(rolling_all, out_png_roll)
    _bootstrap_histograms(boot_df, out_png_boot)

    exec_report = _executive_report(
        timeframe_df=timeframe_df,
        bootstrap_summary=bootstrap_summary,
        ci_df=ci_df,
        effects_df=effects_df,
        rolling_all=rolling_all,
        asset_tf=asset_tf,
        overfit=overfit,
    )

    payload = {
        "generated_at": _now_utc().isoformat(),
        "source_enriched_file": str(source_file),
        "sample_size": int(len(df)),
        "timeframes": timeframe_df.to_dict(orient="records"),
        "bootstrap_summary": bootstrap_summary.to_dict(orient="records"),
        "overfitting_check": overfit,
        "outputs": {
            "timeframe_metrics": str(out_timeframe),
            "bootstrap_distribution": str(out_boot_dist),
            "bootstrap_summary": str(out_boot_summary),
            "win_loss_ci": str(out_ci),
            "effect_sizes": str(out_eff),
            "rolling_windows": str(out_roll),
            "asset_timeframe_metrics": str(out_asset_tf),
            "rolling_plot": str(out_png_roll),
            "bootstrap_hist_plot": str(out_png_boot),
            "report_json": str(out_json),
            "report_md": str(out_md),
        },
    }

    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    out_md.write_text(exec_report, encoding="utf-8")

    return payload


if __name__ == "__main__":
    result = run(Config())
    print(json.dumps(result, ensure_ascii=False, indent=2))
