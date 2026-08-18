"""Diagnóstico rápido de sinais gerados pela ReversaoNextGenV1 LONG."""
from datetime import datetime, timezone
import pandas as pd
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
from database.connection import get_session
from database.repositories import CandleRepository

PARAMS = {
    "ema_fast": 15, "ema_slow": 45, "rsi_period": 14, "atr_period": 14,
    "atr_stop_multiplier": 1.5, "risk_reward_ratio": 2.5, "score_min": 0.6,
    "volume_multiplier_min": 0.8, "atr_high_threshold": 1.0, "volume_low_threshold": 0.8,
}

with get_session() as session:
    repo = CandleRepository(session)
    candles = repo.get_range("BTC/USDT", "5m",
                             datetime(2024, 1, 1, tzinfo=timezone.utc),
                             datetime(2024, 12, 31, tzinfo=timezone.utc))
df = pd.DataFrame(
    [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
     for c in candles],
    index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
)

strategy = ReversaoNextGenV1Strategy(**PARAMS)
strategy.initialize()
enriched = strategy.calculate(df.copy())

from strategies.base_strategy import SignalType
signals = []
for i in range(50, len(enriched)):
    window = enriched.iloc[:i+1]
    sig = strategy.entry_signal(window)
    if sig.signal == SignalType.BUY:
        signals.append({
            "ts": sig.timestamp,
            "price": sig.price,
            "score": sig.score,
            "bullish_reversal": sig.metadata.get("bullish_reversal"),
            "bullish_consolidation": sig.metadata.get("bullish_consolidation"),
            "trend_score": sig.metadata.get("trend_score"),
            "prev_trend_score": sig.metadata.get("prev_trend_score"),
            "atr_bucket": sig.metadata.get("atr_bucket"),
            "volume_bucket": sig.metadata.get("volume_bucket"),
            "bollinger_position": sig.metadata.get("bollinger_position"),
        })

sdf = pd.DataFrame(signals)
print(f"Total BUY signals: {len(sdf)}")
if len(sdf) > 0:
    print("\n--- Score distribution ---")
    print(sdf["score"].describe())
    print("\n--- Entry path breakdown ---")
    print("via bullish_reversal:", sdf["bullish_reversal"].sum())
    print("via bullish_consolidation only:", (~sdf["bullish_reversal"] & sdf["bullish_consolidation"]).sum())
    print("\n--- Sample signals ---")
    print(sdf.head(5).to_string())
