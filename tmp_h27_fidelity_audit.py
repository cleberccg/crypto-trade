from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.feature_selection import mutual_info_classif
from sklearn.inspection import permutation_importance
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import LabelEncoder

from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.repositories import CandleRepository
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy


BASE = Path("optimization/results")
FI_CSV = BASE / "quantitative_discovery_feature_importance_20260629_152434.csv"
CLUSTER_KEY_H27 = "reversao|high_atr|unknown|low_volume|inside_band|SELL"
SYMBOL = "BTC/USDT"
TIMEFRAME = "5m"
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 12, 31, tzinfo=timezone.utc)
CAPITAL = 10_000.0

BEST_PARAMS = {
    "ema_fast": 25,
    "ema_slow": 45,
    "rsi_period": 14,
    "atr_period": 14,
    "atr_stop_multiplier": 2.5,
    "risk_reward_ratio": 3.8,
    "score_min": 0.6,
    "volume_multiplier_min": 0.8,
    "atr_high_threshold": 1.0,
    "volume_low_threshold": 0.8,
}


@dataclass
class Coverage:
    h27_events: int
    strategy_trades: int
    intersection: int
    precision: float
    recall: float
    f1: float


def _safe_div(a: float, b: float) -> float:
    return float(a / b) if b else 0.0


def reconstruct_h27_events(enriched: pd.DataFrame) -> pd.DataFrame:
    """
    Reconstruct H27 events directly from market features on the target dataset.

    H27 reference key:
      reversao | high_atr | unknown | low_volume | inside_band | SELL

    SELL side is interpreted as bearish reversal (prev trend positive and now reversing).
    """
    df = enriched.copy()
    df = df.dropna(subset=["trend_score", "trend_score_prev", "atr_bucket", "volume_bucket", "bollinger_position"])

    bearish_reversal = df["regime_reversal"].astype(bool) & (df["trend_score_prev"] > 0)
    mask = (
        bearish_reversal
        & (df["atr_bucket"].astype(str) == "high_atr")
        & (df["volume_bucket"].astype(str) == "low_volume")
        & (df["bollinger_position"].astype(str) == "inside_band")
    )
    events = df.loc[mask].copy()
    events = events.reset_index().rename(columns={"index": "entry_time"})
    events = events[["entry_time", "trend_score", "trend_score_prev", "atr_bucket", "volume_bucket", "bollinger_position"]]
    events = events.drop_duplicates(subset=["entry_time"]).sort_values("entry_time")
    return events


def load_candles() -> pd.DataFrame:
    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(SYMBOL, TIMEFRAME, START, END)
    df = pd.DataFrame(
        [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )
    return df


def run_strategy(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    strategy = ReversaoNextGenV1Strategy(**BEST_PARAMS)
    strategy.initialize()
    enriched = strategy.calculate(df.copy())
    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=CAPITAL))
    result = engine.run(df.copy(), symbol=SYMBOL)
    trades = pd.DataFrame(result.trades)
    if len(trades):
        trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True)
        trades["exit_time"] = pd.to_datetime(trades["exit_time"], utc=True)
        trades = trades.sort_values("entry_time")
    return enriched, trades


def compute_coverage(h27_events: pd.DataFrame, trades: pd.DataFrame) -> Coverage:
    hset = set(h27_events["entry_time"].tolist())
    tset = set(trades["entry_time"].tolist()) if len(trades) else set()
    inter = len(hset & tset)
    p = _safe_div(inter, len(tset))
    r = _safe_div(inter, len(hset))
    f1 = _safe_div(2 * p * r, p + r)
    return Coverage(
        h27_events=len(hset),
        strategy_trades=len(tset),
        intersection=inter,
        precision=p,
        recall=r,
        f1=f1,
    )


def classify_false_positives(trades: pd.DataFrame, enriched: pd.DataFrame, hset: set[pd.Timestamp]) -> pd.DataFrame:
    if trades.empty:
        return pd.DataFrame(columns=["cause", "count"])
    fps = trades[~trades["entry_time"].isin(hset)].copy()
    if fps.empty:
        return pd.DataFrame(columns=["cause", "count"])

    causes = []
    for ts in fps["entry_time"]:
        if ts not in enriched.index:
            causes.append("timestamp_not_found")
            continue
        row = enriched.loc[ts]
        mismatch = []
        mismatch.append("direction_incompatible")  # strategy is BUY vs H27 SELL
        if str(row.get("atr_bucket", "")) != "high_atr":
            mismatch.append("atr_incompatible")
        if str(row.get("volume_bucket", "")) != "low_volume":
            mismatch.append("volume_incompatible")
        if str(row.get("bollinger_position", "")) != "inside_band":
            mismatch.append("bollinger_incompatible")
        if not bool(row.get("regime_reversal", False)):
            mismatch.append("reversal_incomplete")
        causes.append(mismatch[0] if mismatch else "other")

    out = pd.Series(causes).value_counts().rename_axis("cause").reset_index(name="count")
    return out


def classify_false_negatives(h27_events: pd.DataFrame, trades: pd.DataFrame, enriched: pd.DataFrame) -> pd.DataFrame:
    if h27_events.empty:
        return pd.DataFrame(columns=["cause", "count"])
    tset = set(trades["entry_time"].tolist()) if len(trades) else set()
    fn = h27_events[~h27_events["entry_time"].isin(tset)].copy()

    causes = []
    for ts in fn["entry_time"]:
        if ts not in enriched.index:
            causes.append("timestamp_not_found")
            continue
        row = enriched.loc[ts]
        trend = float(row.get("trend_score", 0.0))
        prev = float(row.get("trend_score_prev", 0.0))
        bull_rev = bool(row.get("regime_reversal", False)) and (prev < 0)
        bull_cons = (prev < -0.5) and (trend > prev) and (abs(trend) < 0.3)
        atr_ok = str(row.get("atr_bucket", "")) == "high_atr"
        vol_ok = str(row.get("volume_bucket", "")) == "low_volume"
        bb_ok = str(row.get("bollinger_position", "")) == "inside_band"

        if not (bull_rev or bull_cons):
            causes.append("regime_translation_filter")
        elif not atr_ok:
            causes.append("atr_filter")
        elif not vol_ok:
            causes.append("volume_filter")
        elif not bb_ok:
            causes.append("bollinger_filter")
        else:
            causes.append("confidence_or_engine_constraint")

    out = pd.Series(causes).value_counts().rename_axis("cause").reset_index(name="count")
    return out


def feature_importance_quant(enriched: pd.DataFrame, hset: set[pd.Timestamp]) -> dict:
    df = enriched.copy()
    df = df.dropna(subset=["trend_score", "atr", "bb_percent_b", "relative_volume", "rsi"])
    df["target_h27"] = df.index.isin(hset).astype(int)

    # Features oriented to H27 structure
    feat = pd.DataFrame(
        {
            "trend_score": df["trend_score"].astype(float),
            "atr": df["atr"].astype(float),
            "rsi": df["rsi"].astype(float),
            "bb_percent_b": df["bb_percent_b"].astype(float),
            "relative_volume": df["relative_volume"].astype(float),
            "atr_bucket": df["atr_bucket"].astype(str),
            "volume_bucket": df["volume_bucket"].astype(str),
            "bollinger_position": df["bollinger_position"].astype(str),
            "regime_reversal": df["regime_reversal"].astype(int),
        }
    )
    y = df["target_h27"].astype(int)

    # Encode categoricals
    feat_enc = feat.copy()
    for c in ["atr_bucket", "volume_bucket", "bollinger_position"]:
        le = LabelEncoder()
        feat_enc[c] = le.fit_transform(feat_enc[c])

    # Mutual Information (proxy for IG on mixed features)
    mi_vals = mutual_info_classif(feat_enc, y, discrete_features=[False, False, False, False, False, True, True, True, True], random_state=42)
    mi = dict(zip(feat_enc.columns, mi_vals))

    # Information Gain (binary target entropy reduction approximation)
    # For practicality here, use MI values as IG approximation in nats.
    ig = mi.copy()

    # Permutation importance
    X_train, X_test, y_train, y_test = train_test_split(feat_enc, y, test_size=0.25, random_state=42, stratify=y)
    rf = RandomForestClassifier(n_estimators=120, random_state=42, class_weight="balanced_subsample", n_jobs=-1)
    rf.fit(X_train, y_train)
    perm = permutation_importance(rf, X_test, y_test, n_repeats=6, random_state=42, n_jobs=-1)
    perm_map = dict(zip(feat_enc.columns, perm.importances_mean))

    shap_map = {}
    shap_available = False
    try:
        import shap  # type: ignore

        shap_available = True
        sample = X_test.sample(min(len(X_test), 5000), random_state=42)
        explainer = shap.TreeExplainer(rf)
        shap_values = explainer.shap_values(sample)
        if isinstance(shap_values, list):
            sv = np.abs(shap_values[1]).mean(axis=0)
        else:
            sv = np.abs(shap_values).mean(axis=0)
        shap_map = dict(zip(feat_enc.columns, sv.tolist()))
    except Exception:
        shap_map = {}

    rank = []
    for c in feat_enc.columns:
        score = float(mi.get(c, 0.0) + max(0.0, perm_map.get(c, 0.0)))
        if score >= 0.05:
            cls = "Essencial"
        elif score >= 0.015:
            cls = "Importante"
        elif score >= 0.003:
            cls = "Pouco relevante"
        else:
            cls = "Ruido"
        rank.append({
            "feature": c,
            "information_gain": float(ig.get(c, 0.0)),
            "mutual_information": float(mi.get(c, 0.0)),
            "permutation_importance": float(perm_map.get(c, 0.0)),
            "shap_importance": float(np.asarray(shap_map.get(c, 0.0), dtype=float).mean()) if shap_available else None,
            "classification": cls,
        })

    rank = sorted(rank, key=lambda x: (x["mutual_information"] + x["permutation_importance"]), reverse=True)
    return {
        "rows": rank,
        "shap_available": shap_available,
        "class_balance": {
            "h27_positive": int(y.sum()),
            "total": int(len(y)),
            "positive_rate": float(y.mean()),
        },
    }


def main() -> None:
    candles = load_candles()
    enriched, trades = run_strategy(candles)
    h27 = reconstruct_h27_events(enriched)
    hset = set(h27["entry_time"].tolist())

    cov = compute_coverage(h27, trades)
    fp = classify_false_positives(trades, enriched, hset)
    fn = classify_false_negatives(h27, trades, enriched)
    fi_quant = feature_importance_quant(enriched, hset)

    fi_table = pd.read_csv(FI_CSV)

    out = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster_h27_key": CLUSTER_KEY_H27,
        "cluster_h27_events_2024_btc_5m": int(len(h27)),
        "strategy_trades_2024_btc_5m": int(len(trades)),
        "coverage": asdict(cov),
        "top_false_positives": fp.to_dict(orient="records"),
        "top_false_negatives": fn.to_dict(orient="records"),
        "feature_importance_original_top20": fi_table.head(20).to_dict(orient="records"),
        "feature_importance_quantitative": fi_quant,
        "implemented_filters": [
            "atr_bucket=high_atr",
            "volume_bucket=low_volume",
            "bollinger_position=inside_band",
            "regime_reversal (translated to bullish reversal)",
        ],
        "simplified_or_changed_filters": [
            "Direction changed from SELL(H27) to BUY (LONG adaptation)",
            "regime filter converted to bullish_reversal/bullish_consolidation",
            "RSI remains effectively unrestricted (unknown)",
            "confidence thresholding added (score_min)",
        ],
        "missing_or_not_used_from_h27": [
            "Original H27 direction SELL not represented in entry execution",
            "Direct cluster-key matching not enforced in runtime",
            "No explicit operation-mapping gate to H27-only events",
        ],
    }

    json_path = BASE / "fase55_h27_fidelity_audit.json"
    md_path = BASE / "fase55_h27_fidelity_audit.md"
    fp_csv = BASE / "fase55_false_positives.csv"
    fn_csv = BASE / "fase55_false_negatives.csv"
    fiq_csv = BASE / "fase55_feature_importance_quant.csv"

    json_path.write_text(json.dumps(out, indent=2, ensure_ascii=True), encoding="utf-8")

    lines = []
    lines.append("# FASE 5.5 - Auditoria de Fidelidade H27")
    lines.append("")
    lines.append(f"- H27 events (2024 BTC/USDT 5m): {cov.h27_events}")
    lines.append(f"- Strategy trades (2024 BTC/USDT 5m): {cov.strategy_trades}")
    lines.append(f"- Intersection: {cov.intersection}")
    lines.append(f"- Precision: {cov.precision:.6f}")
    lines.append(f"- Recall: {cov.recall:.6f}")
    lines.append(f"- F1: {cov.f1:.6f}")
    lines.append("")
    lines.append("## Top false positives")
    for row in fp.head(10).to_dict(orient="records"):
        lines.append(f"- {row['cause']}: {row['count']}")
    lines.append("")
    lines.append("## Top false negatives")
    for row in fn.head(10).to_dict(orient="records"):
        lines.append(f"- {row['cause']}: {row['count']}")
    lines.append("")
    lines.append("## Filters")
    lines.append("### Implemented")
    for item in out["implemented_filters"]:
        lines.append(f"- {item}")
    lines.append("### Simplified/Changed")
    for item in out["simplified_or_changed_filters"]:
        lines.append(f"- {item}")
    lines.append("### Missing")
    for item in out["missing_or_not_used_from_h27"]:
        lines.append(f"- {item}")

    md_path.write_text("\n".join(lines), encoding="utf-8")

    fp.to_csv(fp_csv, index=False)
    fn.to_csv(fn_csv, index=False)
    pd.DataFrame(fi_quant["rows"]).to_csv(fiq_csv, index=False)

    print("WROTE", json_path)
    print("WROTE", md_path)
    print("WROTE", fp_csv)
    print("WROTE", fn_csv)
    print("WROTE", fiq_csv)


if __name__ == "__main__":
    main()
