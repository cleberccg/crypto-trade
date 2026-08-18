from compare_reversao_old_new import load_df, run_backtest, OldReversaoStrategy
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
import time

params = {
    "ema_fast": 15,
    "ema_slow": 45,
    "rsi_period": 14,
    "atr_period": 14,
    "atr_stop_multiplier": 1.5,
    "risk_reward_ratio": 2.5,
    "score_min": 0.6,
    "volume_multiplier_min": 0.8,
    "atr_high_threshold": 1.0,
    "volume_low_threshold": 0.8,
}

df = load_df(limit_bars=1500)
print(f"Dataset bars: {len(df)}")

t0 = time.perf_counter()
old_result = run_backtest(OldReversaoStrategy, params, df)
t_old = time.perf_counter() - t0

t1 = time.perf_counter()
new_result = run_backtest(ReversaoNextGenV1Strategy, params, df)
t_new = time.perf_counter() - t1

same_trade_count = len(old_result.trades) == len(new_result.trades)
same_trades = old_result.trades == new_result.trades
same_metrics = old_result.metrics.to_dict() == new_result.metrics.to_dict()

print("trade_count_old", len(old_result.trades))
print("trade_count_new", len(new_result.trades))
print("same_trade_count", same_trade_count)
print("same_trades", same_trades)
print("same_metrics", same_metrics)
print("old_time_s", round(t_old, 4))
print("new_time_s", round(t_new, 4))
print("speedup_pct", round(((t_old - t_new) / t_old * 100.0) if t_old > 0 else 0.0, 2))
print("equivalence_passed", same_trade_count and same_trades and same_metrics)
