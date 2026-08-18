"""
Fase 5.4 — Engenharia Reversa do Cluster H27 (reversao_2)

Objetivo: Converter o cluster H27 em uma especificação técnica de estratégia denominada ReversaoNextGenV1.

Etapas:
1. Engenharia reversa do cluster: features, indicadores, importância
2. Perfil estatístico completo
3. Contexto do padrão (regimes, ativos, timeframes, anos, horários)
4. Comportamento pós-evento (MFE, MAE, retornos futuros)
5. Especificação da estratégia (entrada, filtros, stop, TP, saída)
6. Avaliação de implementabilidade
7. Decisão (OPÇÃO A ou B)
"""

from __future__ import annotations

import json
import math
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "optimization" / "results"
CHUNKS_DIR = RESULTS_DIR / "quantitative_discovery_chunks" / "fase52_full_ultra_20260629" / "events"
LAB_JSON = RESULTS_DIR / "quantitative_discovery_lab_20260629_152434.json"

# H27 cluster key: reversao|high_atr|unknown|low_volume|inside_band|SELL
CLUSTER_KEY = "reversao|high_atr|unknown|low_volume|inside_band|SELL"
CLUSTER_ID = "reversao_2"
HYPOTHESIS_ID = "H27"

EVENT_COLUMNS = [
    "symbol", "timeframe", "entry_time", "regime", "atr_bucket", "rsi_bucket",
    "volume_bucket", "bollinger_position", "direction", "pnl", "mfe", "mae",
    "entry_price", "exit_price", "entry_trend_score", "entry_is_breakout",
    "first_move", "final_move", "entry_session", "day_type", "weekday",
    "distance_to_ema_pct", "entry_atr_pct", "relative_volume"
]


def process_cluster_events() -> pd.DataFrame:
    """Extract all events belonging to H27 cluster."""
    print("Processing cluster events...")
    cluster_events = []
    total_processed = 0
    
    for csv_path in sorted(CHUNKS_DIR.glob("events_*.csv")):
        for chunk in pd.read_csv(csv_path, usecols=EVENT_COLUMNS, chunksize=250_000, on_bad_lines="skip"):
            if chunk.empty:
                continue
            
            # Build cluster key for each row
            chunk["cluster_key"] = (
                chunk["regime"].astype(str) + "|" +
                chunk["atr_bucket"].astype(str) + "|" +
                chunk["rsi_bucket"].astype(str) + "|" +
                chunk["volume_bucket"].astype(str) + "|" +
                chunk["bollinger_position"].astype(str) + "|" +
                chunk["direction"].astype(str)
            )
            
            # Filter for H27 cluster
            h27_events = chunk[chunk["cluster_key"] == CLUSTER_KEY].copy()
            if not h27_events.empty:
                # Clean data
                h27_events["entry_time"] = pd.to_datetime(h27_events["entry_time"], utc=True, errors="coerce")
                h27_events = h27_events.dropna(subset=["entry_time"])
                
                # Add temporal features
                h27_events["entry_year"] = h27_events["entry_time"].dt.year
                h27_events["entry_month"] = h27_events["entry_time"].dt.month
                h27_events["entry_hour"] = h27_events["entry_time"].dt.hour
                h27_events["entry_dayofweek"] = h27_events["entry_time"].dt.dayofweek
                
                # Numeric columns
                for col in ["pnl", "mfe", "mae", "entry_price", "exit_price", 
                           "entry_trend_score", "distance_to_ema_pct", "entry_atr_pct", "relative_volume"]:
                    h27_events[col] = pd.to_numeric(h27_events[col], errors="coerce")
                
                h27_events["entry_is_breakout"] = h27_events["entry_is_breakout"].astype(bool)
                h27_events["first_move"] = pd.to_numeric(h27_events["first_move"], errors="coerce")
                h27_events["final_move"] = pd.to_numeric(h27_events["final_move"], errors="coerce")
                
                cluster_events.append(h27_events)
            
            total_processed += len(chunk)
    
    if cluster_events:
        df = pd.concat(cluster_events, ignore_index=True)
        print(f"Total events in H27: {len(df):,} (processed {total_processed:,} raw events)")
        return df
    else:
        print(f"No events found for cluster key: {CLUSTER_KEY}")
        return pd.DataFrame()


def analyze_feature_statistics(df: pd.DataFrame) -> dict:
    """Calculate statistical profiles for all features."""
    print("\nCalculating feature statistics...")
    
    stats = {}
    
    # Continuous features
    continuous_features = [
        "entry_price", "exit_price", "pnl", "mfe", "mae",
        "entry_trend_score", "distance_to_ema_pct", "entry_atr_pct", "relative_volume",
        "first_move", "final_move"
    ]
    
    for feature in continuous_features:
        if feature in df.columns:
            valid = pd.to_numeric(df[feature], errors="coerce").dropna()
            if not valid.empty:
                stats[feature] = {
                    "count": int(valid.count()),
                    "mean": float(valid.mean()),
                    "median": float(valid.median()),
                    "std": float(valid.std()),
                    "min": float(valid.min()),
                    "max": float(valid.max()),
                    "p10": float(valid.quantile(0.10)),
                    "p25": float(valid.quantile(0.25)),
                    "p50": float(valid.quantile(0.50)),
                    "p75": float(valid.quantile(0.75)),
                    "p90": float(valid.quantile(0.90)),
                }
    
    # Categorical features
    categorical_features = ["regime", "atr_bucket", "rsi_bucket", "volume_bucket", 
                           "bollinger_position", "direction", "entry_session", 
                           "day_type", "entry_is_breakout", "symbol", "timeframe"]
    
    for feature in categorical_features:
        if feature in df.columns:
            value_counts = df[feature].value_counts()
            total = value_counts.sum()
            stats[feature] = {
                "distribution": {
                    str(k): {"count": int(v), "pct": float(v / total * 100)}
                    for k, v in value_counts.items()
                }
            }
    
    return stats


def analyze_pattern_context(df: pd.DataFrame) -> dict:
    """Analyze in which contexts H27 appears most frequently."""
    print("\nAnalyzing pattern context...")
    
    context = {}
    
    # By regime
    regime_dist = df["regime"].value_counts()
    context["by_regime"] = {str(k): int(v) for k, v in regime_dist.items()}
    
    # By asset
    asset_dist = df["symbol"].value_counts()
    context["by_asset"] = {str(k): int(v) for k, v in asset_dist.head(20).items()}
    
    # By timeframe
    tf_dist = df["timeframe"].value_counts()
    context["by_timeframe"] = {str(k): int(v) for k, v in tf_dist.items()}
    
    # By year
    year_dist = df["entry_year"].value_counts().sort_index()
    context["by_year"] = {str(int(k)): int(v) for k, v in year_dist.items()}
    
    # By month
    month_dist = df["entry_month"].value_counts().sort_index()
    context["by_month"] = {int(k): int(v) for k, v in month_dist.items()}
    
    # By hour
    hour_dist = df["entry_hour"].value_counts().sort_index()
    context["by_hour"] = {int(k): int(v) for k, v in hour_dist.items()}
    
    # By day of week
    dow_dist = df["entry_dayofweek"].value_counts().sort_index()
    context["by_day_of_week"] = {int(k): int(v) for k, v in dow_dist.items()}
    
    # By breakout
    context["is_breakout"] = {
        "yes": int(df["entry_is_breakout"].sum()),
        "no": int((~df["entry_is_breakout"]).sum())
    }
    
    return context


def analyze_post_event_behavior(df: pd.DataFrame) -> dict:
    """Analyze MFE, MAE, and future returns after the event."""
    print("\nAnalyzing post-event behavior...")
    
    behavior = {}
    
    # MFE (Maximum Favorable Excursion)
    mfe_valid = pd.to_numeric(df["mfe"], errors="coerce").dropna()
    if not mfe_valid.empty:
        behavior["mfe"] = {
            "mean": float(mfe_valid.mean()),
            "median": float(mfe_valid.median()),
            "std": float(mfe_valid.std()),
            "p10": float(mfe_valid.quantile(0.10)),
            "p25": float(mfe_valid.quantile(0.25)),
            "p50": float(mfe_valid.quantile(0.50)),
            "p75": float(mfe_valid.quantile(0.75)),
            "p90": float(mfe_valid.quantile(0.90)),
        }
    
    # MAE (Maximum Adverse Excursion)
    mae_valid = pd.to_numeric(df["mae"], errors="coerce").dropna()
    if not mae_valid.empty:
        behavior["mae"] = {
            "mean": float(mae_valid.mean()),
            "median": float(mae_valid.median()),
            "std": float(mae_valid.std()),
            "p10": float(mae_valid.quantile(0.10)),
            "p25": float(mae_valid.quantile(0.25)),
            "p50": float(mae_valid.quantile(0.50)),
            "p75": float(mae_valid.quantile(0.75)),
            "p90": float(mae_valid.quantile(0.90)),
        }
    
    # PnL analysis (return)
    pnl_valid = pd.to_numeric(df["pnl"], errors="coerce").dropna()
    if not pnl_valid.empty:
        behavior["pnl"] = {
            "mean": float(pnl_valid.mean()),
            "median": float(pnl_valid.median()),
            "std": float(pnl_valid.std()),
            "p10": float(pnl_valid.quantile(0.10)),
            "p25": float(pnl_valid.quantile(0.25)),
            "p50": float(pnl_valid.quantile(0.50)),
            "p75": float(pnl_valid.quantile(0.75)),
            "p90": float(pnl_valid.quantile(0.90)),
            "pct_positive": float((pnl_valid > 0).sum() / len(pnl_valid) * 100),
        }
    
    # First move (early movement after entry)
    first_move = pd.to_numeric(df["first_move"], errors="coerce").dropna()
    if not first_move.empty:
        behavior["first_move"] = {
            "mean": float(first_move.mean()),
            "median": float(first_move.median()),
            "std": float(first_move.std()),
            "pct_positive": float((first_move > 0).sum() / len(first_move) * 100),
        }
    
    # Final move (late movement)
    final_move = pd.to_numeric(df["final_move"], errors="coerce").dropna()
    if not final_move.empty:
        behavior["final_move"] = {
            "mean": float(final_move.mean()),
            "median": float(final_move.median()),
            "std": float(final_move.std()),
            "pct_positive": float((final_move > 0).sum() / len(final_move) * 100),
        }
    
    # Continuity vs reversal
    behavior["early_move_direction"] = {
        "moves_in_trade_direction": int((first_move > 0).sum()) if not first_move.empty else 0,
        "moves_against_trade": int((first_move <= 0).sum()) if not first_move.empty else 0,
    }
    
    return behavior


def load_lab_h27_data() -> dict:
    """Load H27 hypothesis data from lab JSON."""
    print("\nLoading lab H27 data...")
    lab_data = json.loads(LAB_JSON.read_text(encoding="utf-8"))
    
    # Find H27 hypothesis
    for hyp in lab_data.get("hypotheses", []):
        if hyp.get("hypothesis_id") == HYPOTHESIS_ID:
            return hyp
    
    return {}


def main():
    print(f"=== FASE 5.4: Engenharia Reversa do Cluster H27 (reversao_2) ===\n")
    
    # Load lab data
    lab_h27 = load_lab_h27_data()
    if not lab_h27:
        print("ERROR: H27 not found in lab JSON")
        return
    
    print(f"H27 Metrics from Lab:")
    print(f"  - Trades: {lab_h27['evidence'].get('trades'):,}")
    print(f"  - Win Rate: {lab_h27['evidence'].get('win_rate')}%")
    print(f"  - Sharpe: {lab_h27['evidence'].get('sharpe'):.4f}")
    print(f"  - Expectancy: {lab_h27['evidence'].get('expectancy'):.6f}")
    print(f"  - Avg MFE: {lab_h27['evidence'].get('avg_mfe'):.6f}")
    print(f"  - Avg MAE: {lab_h27['evidence'].get('avg_mae'):.6f}\n")
    
    # Extract cluster events
    df = process_cluster_events()
    if df.empty:
        print("No events to analyze")
        return
    
    # Analyses
    feature_stats = analyze_feature_statistics(df)
    context = analyze_pattern_context(df)
    behavior = analyze_post_event_behavior(df)
    
    # Compile report
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cluster": {
            "id": CLUSTER_ID,
            "hypothesis_id": HYPOTHESIS_ID,
            "key": CLUSTER_KEY,
            "total_events": len(df),
        },
        "lab_metrics": lab_h27.get("evidence", {}),
        "top_patterns": lab_h27.get("top_patterns", []),
        "feature_statistics": feature_stats,
        "pattern_context": context,
        "post_event_behavior": behavior,
    }
    
    # Save report
    report_path = RESULTS_DIR / "fase54_h27_engineering.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nReport saved to: {report_path}")
    
    # Print summary
    print("\n=== Summary ===")
    print(f"Total events analyzed: {len(df):,}")
    print(f"Feature statistics calculated: {len(feature_stats)}")
    print(f"Pattern contexts identified: {len(context)}")
    print(f"Post-event behavior metrics: {len(behavior)}")


if __name__ == "__main__":
    main()
