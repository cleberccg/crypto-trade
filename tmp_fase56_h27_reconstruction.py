from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from joblib import dump
from sklearn.compose import ColumnTransformer
from sklearn.metrics import f1_score, precision_score, recall_score
from sklearn.model_selection import GridSearchCV, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.tree import DecisionTreeClassifier, export_text

from database.connection import get_session
from database.repositories import CandleRepository
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy


BASE = Path("optimization/results")
SYMBOL = "BTC/USDT"
TIMEFRAME = "5m"
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 12, 31, tzinfo=timezone.utc)
CAPITAL = 10_000.0

# Use the same indicator configuration as the current research setup so the
# reconstruction is driven by the same feature surface as the existing lab.
LAB_PARAMS = {
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

EMA_FAST_COL = f"ema_{LAB_PARAMS['ema_fast']}"
EMA_SLOW_COL = f"ema_{LAB_PARAMS['ema_slow']}"

FIDELITY_TARGET = 0.90


@dataclass
class Fidelity:
    h27_events: int
    predicted_events: int
    intersection: int
    precision: float
    recall: float
    f1: float


def safe_div(numerator: float, denominator: float) -> float:
    return float(numerator / denominator) if denominator else 0.0


def load_candles() -> pd.DataFrame:
    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(SYMBOL, TIMEFRAME, START, END)

    df = pd.DataFrame(
        [
            {
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in candles
        ],
        index=pd.DatetimeIndex([candle.open_time for candle in candles], tz="UTC"),
    )
    return df


def run_lab_features(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    strategy = ReversaoNextGenV1Strategy(**LAB_PARAMS)
    strategy.initialize()
    enriched = strategy.calculate(df.copy())
    return enriched, pd.DataFrame()


def build_h27_labels(enriched: pd.DataFrame) -> pd.DataFrame:
    df = enriched.copy()
    df = df.dropna(
        subset=[
            "trend_score",
            "trend_score_prev",
            "atr_bucket",
            "volume_bucket",
            "bollinger_position",
        ]
    )

    bearish_reversal = df["regime_reversal"].astype(bool) & (df["trend_score_prev"] > 0)
    bearish_consolidation = (
        (df["trend_score_prev"] > 0.5)
        & (df["trend_score"] < df["trend_score_prev"])
        & (df["trend_score"].abs() < 0.3)
    )

    mask = (
        (bearish_reversal | bearish_consolidation)
        & (df["atr_bucket"].astype(str) == "high_atr")
        & (df["volume_bucket"].astype(str) == "low_volume")
        & (df["bollinger_position"].astype(str) == "inside_band")
    )

    h27 = df.loc[mask].copy()
    if "entry_time" in h27.columns:
        h27 = h27.drop(columns=["entry_time"])
    h27 = h27.reset_index().rename(columns={"index": "entry_time"})
    h27["active"] = SYMBOL
    h27["timeframe"] = TIMEFRAME
    h27["h27_label"] = 1
    h27["regime"] = "reversao"
    h27["direction"] = "SELL"
    h27["primary_regime"] = "reversao"
    h27["weekday"] = pd.to_datetime(h27["entry_time"]).dt.day_name()
    h27["day_type"] = np.where(pd.to_datetime(h27["entry_time"]).dt.dayofweek < 5, "weekday", "weekend")
    h27["hour"] = pd.to_datetime(h27["entry_time"]).dt.hour
    h27["entry_session"] = np.select(
        [
            h27["hour"].between(0, 4),
            h27["hour"].between(5, 10),
            h27["hour"].between(11, 16),
            h27["hour"].between(17, 23),
        ],
        ["open_asia", "europe", "us_open", "us_late"],
        default="overnight",
    )
    h27["distance_to_ema_pct"] = (h27["close"] - h27[EMA_SLOW_COL]) / h27[EMA_SLOW_COL] * 100.0
    return h27


def summarize_numeric(df: pd.DataFrame) -> dict[str, Any]:
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    stats: dict[str, Any] = {}
    for col in numeric_cols:
        series = df[col].dropna().astype(float)
        if series.empty:
            continue
        stats[col] = {
            "count": int(series.shape[0]),
            "mean": float(series.mean()),
            "median": float(series.median()),
            "variance": float(series.var(ddof=1)) if series.shape[0] > 1 else 0.0,
            "std": float(series.std(ddof=1)) if series.shape[0] > 1 else 0.0,
            "p05": float(series.quantile(0.05)),
            "p10": float(series.quantile(0.10)),
            "p25": float(series.quantile(0.25)),
            "p50": float(series.quantile(0.50)),
            "p75": float(series.quantile(0.75)),
            "p90": float(series.quantile(0.90)),
            "p95": float(series.quantile(0.95)),
            "min": float(series.min()),
            "max": float(series.max()),
        }
    return stats


def summarize_categories(df: pd.DataFrame) -> dict[str, Any]:
    categorical_cols = df.select_dtypes(include=["object", "category", "bool"]).columns.tolist()
    summaries: dict[str, Any] = {}
    for col in categorical_cols:
        counts = df[col].astype(str).value_counts(dropna=False)
        total = int(counts.sum())
        summaries[col] = [
            {
                "value": str(value),
                "count": int(count),
                "share": safe_div(int(count), total),
            }
            for value, count in counts.items()
        ]
    return summaries


def build_model_frame(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.Series, list[str], list[str]]:
    base = df.copy()
    base = base.dropna(
        subset=[
            "trend_score",
            "trend_score_prev",
            "atr",
            "rsi",
            "bb_percent_b",
            "relative_volume",
            "distance_to_ema_pct",
        ]
    )
    base["label"] = base["entry_time"].isin(set(build_h27_labels(df)["entry_time"])).astype(int)

    numeric_features = [
        "trend_score",
        "trend_score_prev",
        "atr",
        "rsi",
        "bb_percent_b",
        "relative_volume",
        "distance_to_ema_pct",
        "hour",
    ]
    categorical_features = [
        "atr_bucket",
        "volume_bucket",
        "bollinger_position",
        "weekday",
        "day_type",
        "entry_session",
    ]

    feature_frame = base[numeric_features + categorical_features].copy()
    y = base["label"].astype(int)
    return feature_frame, y, numeric_features, categorical_features


def percentile_rank(series: pd.Series, value: float) -> float:
    clean = series.dropna().astype(float)
    if clean.empty:
        return 0.0
    return float((clean <= value).mean())


def discover_rules(features: pd.DataFrame, y: pd.Series, numeric_features: list[str], categorical_features: list[str]) -> dict[str, Any]:
    X_train, X_test, y_train, y_test = train_test_split(
        features,
        y,
        test_size=0.30,
        random_state=42,
        stratify=y,
    )

    preprocessor = ColumnTransformer(
        transformers=[
            ("num", "passthrough", numeric_features),
            (
                "cat",
                OneHotEncoder(handle_unknown="ignore", sparse_output=False),
                categorical_features,
            ),
        ],
        remainder="drop",
    )

    pipeline = Pipeline(
        steps=[
            ("prep", preprocessor),
            ("tree", DecisionTreeClassifier(class_weight="balanced", random_state=42)),
        ]
    )

    param_grid = {
        "tree__max_depth": [2, 3, 4, 5, 6, 7, 8],
        "tree__min_samples_leaf": [1, 2, 5, 10, 20],
        "tree__min_samples_split": [2, 5, 10, 20],
    }

    search = GridSearchCV(
        pipeline,
        param_grid=param_grid,
        scoring="f1",
        cv=5,
        n_jobs=-1,
        refit=True,
    )
    search.fit(X_train, y_train)

    best_model = search.best_estimator_
    y_pred = best_model.predict(X_test)

    precision = float(precision_score(y_test, y_pred, zero_division=0))
    recall = float(recall_score(y_test, y_pred, zero_division=0))
    f1 = float(f1_score(y_test, y_pred, zero_division=0))

    prep: ColumnTransformer = best_model.named_steps["prep"]
    tree: DecisionTreeClassifier = best_model.named_steps["tree"]

    encoded_feature_names = prep.get_feature_names_out()
    tree_text = export_text(tree, feature_names=list(encoded_feature_names))

    return {
        "best_params": search.best_params_,
        "cv_best_f1": float(search.best_score_),
        "test_precision": precision,
        "test_recall": recall,
        "test_f1": f1,
        "tree_text": tree_text,
        "encoded_feature_names": [str(name) for name in encoded_feature_names],
        "model": best_model,
        "predictions": y_pred,
        "y_test": y_test,
        "split_test_index": X_test.index,
    }


def derive_condition_origin(df: pd.DataFrame, feature: str, threshold: float) -> dict[str, Any]:
    if feature in df.columns and pd.api.types.is_numeric_dtype(df[feature]):
        series = df[feature].dropna().astype(float)
        return {
            "feature": feature,
            "threshold": float(threshold),
            "h27_percentile": percentile_rank(series, threshold),
            "p25": float(series.quantile(0.25)),
            "p50": float(series.quantile(0.50)),
            "p75": float(series.quantile(0.75)),
        }
    return {"feature": feature, "threshold": float(threshold)}


def main() -> None:
    BASE.mkdir(parents=True, exist_ok=True)

    candles = load_candles()
    enriched, trades = run_lab_features(candles)
    h27 = build_h27_labels(enriched)

    h27_index = set(pd.to_datetime(h27["entry_time"], utc=True).tolist())
    labeled = enriched.copy()
    labeled["entry_time"] = labeled.index
    labeled["h27_label"] = labeled["entry_time"].isin(h27_index).astype(int)
    labeled["weekday"] = labeled.index.day_name()
    labeled["day_type"] = np.where(labeled.index.dayofweek < 5, "weekday", "weekend")
    labeled["hour"] = labeled.index.hour
    labeled["entry_session"] = np.select(
        [
            labeled["hour"].between(0, 4),
            labeled["hour"].between(5, 10),
            labeled["hour"].between(11, 16),
            labeled["hour"].between(17, 23),
        ],
        ["open_asia", "europe", "us_open", "us_late"],
        default="overnight",
    )
    labeled["distance_to_ema_pct"] = (labeled["close"] - labeled[EMA_SLOW_COL]) / labeled[EMA_SLOW_COL] * 100.0

    positive = labeled[labeled["h27_label"] == 1].copy()
    positive = positive.reset_index(drop=False).rename(columns={"index": "timestamp"})

    numeric_profile = summarize_numeric(positive)
    categorical_profile = summarize_categories(positive)

    features, y, numeric_features, categorical_features = build_model_frame(labeled)
    rule_result = discover_rules(features, y, numeric_features, categorical_features)

    test_index = rule_result["split_test_index"]
    y_test = rule_result["y_test"]
    y_pred = rule_result["predictions"]

    true_test_index = test_index[y_test.to_numpy() == 1]
    predicted_test_index = test_index[y_pred == 1]

    hset = set(pd.to_datetime(labeled.loc[true_test_index, "entry_time"], utc=True).tolist())
    predicted_times = set(pd.to_datetime(labeled.loc[predicted_test_index, "entry_time"], utc=True).tolist())
    inter = len(hset & predicted_times)
    fidelity = Fidelity(
        h27_events=len(hset),
        predicted_events=len(predicted_times),
        intersection=inter,
        precision=safe_div(inter, len(predicted_times)),
        recall=safe_div(inter, len(hset)),
        f1=safe_div(2 * safe_div(inter, len(predicted_times)) * safe_div(inter, len(hset)), safe_div(inter, len(predicted_times)) + safe_div(inter, len(hset))),
    )

    # Build a small rule-origin summary from the top splits of the tree text.
    rule_origin_rows: list[dict[str, Any]] = []
    for line in rule_result["tree_text"].splitlines():
        stripped = line.strip()
        if "<=" not in stripped or "class:" in stripped:
            continue
        feature_part, threshold_part = stripped.split(" <= ", 1)
        feature_name = feature_part.split("|---")[-1].strip()
        try:
            threshold_value = float(threshold_part.split()[0])
        except ValueError:
            continue
        origin = derive_condition_origin(labeled, feature_name.replace("num__", "").replace("cat__", ""), threshold_value)
        origin["encoded_feature"] = feature_name
        rule_origin_rows.append(origin)
        if len(rule_origin_rows) >= 12:
            break

    dataset_path = BASE / "fase56_h27_dataset.csv"
    positive_path = BASE / "fase56_h27_positive_events.csv"
    stats_path = BASE / "fase56_h27_profile.json"
    fidelity_path = BASE / "fase56_h27_fidelity.json"
    rules_path = BASE / "fase56_h27_rules.json"
    rules_md_path = BASE / "fase56_h27_rules.md"
    labeled_path = BASE / "fase56_h27_labeled_universe.csv"
    model_path = BASE / "fase56_h27_rule_model.joblib"

    labeled.to_csv(labeled_path, index=False)
    positive.to_csv(positive_path, index=False)
    positive.to_csv(dataset_path, index=False)

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "symbol": SYMBOL,
        "timeframe": TIMEFRAME,
        "h27_events": len(h27),
        "universe_rows": int(len(labeled)),
        "numeric_profile": numeric_profile,
        "categorical_profile": categorical_profile,
    }
    stats_path.write_text(json.dumps(stats, indent=2, ensure_ascii=True), encoding="utf-8")

    fidelity_json = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target_precision": FIDELITY_TARGET,
        "target_recall": FIDELITY_TARGET,
        "target_f1": FIDELITY_TARGET,
        "fidelity": asdict(fidelity),
        "best_params": rule_result["best_params"],
        "cv_best_f1": rule_result["cv_best_f1"],
        "test_precision": rule_result["test_precision"],
        "test_recall": rule_result["test_recall"],
        "test_f1": rule_result["test_f1"],
        "passes_threshold": bool(
            rule_result["test_precision"] >= FIDELITY_TARGET
            and rule_result["test_recall"] >= FIDELITY_TARGET
            and rule_result["test_f1"] >= FIDELITY_TARGET
        ),
        "tree_text": rule_result["tree_text"],
        "top_rule_origins": rule_origin_rows,
    }
    fidelity_path.write_text(json.dumps(fidelity_json, indent=2, ensure_ascii=True), encoding="utf-8")
    dump(rule_result["model"], model_path)

    rules_summary = {
        "entry": {
            "direction": "SELL",
            "regime": "reversao",
            "confirmed_filters": [
                "atr_bucket=high_atr",
                "volume_bucket=low_volume",
                "bollinger_position=inside_band",
            ],
            "source": "H27 cluster reconstruction from candle-enriched features",
        },
        "exit": {
            "direction": "BUY-to-cover / HOLD until invalidation",
            "source": "Not implemented yet; requires next phase pilot and excursion profiling",
        },
        "management": {
            "source": "Deferred until fidelity gate passes and excursion distributions are computed",
        },
        "fidelity": fidelity_json,
    }
    rules_path.write_text(json.dumps(rules_summary, indent=2, ensure_ascii=True), encoding="utf-8")

    lines = []
    lines.append("# FASE 5.6 - Reconstrução Fiel do H27")
    lines.append("")
    lines.append(f"- H27 events: {fidelity.h27_events}")
    lines.append(f"- Rules predicted events: {fidelity.predicted_events}")
    lines.append(f"- Intersection: {fidelity.intersection}")
    lines.append(f"- Precision: {fidelity.precision:.6f}")
    lines.append(f"- Recall: {fidelity.recall:.6f}")
    lines.append(f"- F1: {fidelity.f1:.6f}")
    lines.append("")
    lines.append("## Rule discovery")
    lines.append(f"- Best params: {rule_result['best_params']}")
    lines.append(f"- CV best F1: {rule_result['cv_best_f1']:.6f}")
    lines.append(f"- Test precision: {rule_result['test_precision']:.6f}")
    lines.append(f"- Test recall: {rule_result['test_recall']:.6f}")
    lines.append(f"- Test F1: {rule_result['test_f1']:.6f}")
    lines.append("")
    lines.append("## Fidelity gate")
    lines.append("- PASS" if fidelity_json["passes_threshold"] else "- FAIL")
    lines.append("")
    lines.append("## Artifacts")
    lines.append(f"- Labeled universe: {labeled_path.name}")
    lines.append(f"- Positive H27 dataset: {dataset_path.name}")
    lines.append(f"- Profile: {stats_path.name}")
    lines.append(f"- Fidelity: {fidelity_path.name}")
    lines.append(f"- Rules: {rules_path.name}")
    lines.append(f"- Model: {model_path.name}")

    rules_md_path.write_text("\n".join(lines), encoding="utf-8")

    print("WROTE", labeled_path)
    print("WROTE", positive_path)
    print("WROTE", dataset_path)
    print("WROTE", stats_path)
    print("WROTE", fidelity_path)
    print("WROTE", rules_path)
    print("WROTE", rules_md_path)
    print("WROTE", model_path)


if __name__ == "__main__":
    main()