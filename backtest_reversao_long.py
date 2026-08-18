"""
Backtest e auditoria de filtros para a ReversaoNextGenV1 (versão LONG).
Executa o backtest completo em 2024 e, se trades=0, mostra quantas barras
passam em cada filtro individual e em combinações.
"""
import time
from datetime import datetime, timezone

import pandas as pd

from backtesting.engine import BacktestConfig, BacktestEngine
from database.connection import get_session
from database.repositories import CandleRepository
from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy

PARAMS = {
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

SYMBOL = "BTC/USDT"
TIMEFRAME = "5m"
START = datetime(2024, 1, 1, tzinfo=timezone.utc)
END = datetime(2024, 12, 31, tzinfo=timezone.utc)
CAPITAL = 10_000.0


def load_df() -> pd.DataFrame:
    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(SYMBOL, TIMEFRAME, START, END)
    return pd.DataFrame(
        [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
         for c in candles],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )


def audit_filters(enriched: pd.DataFrame) -> None:
    df = enriched.dropna(subset=["regime_reversal", "atr_bucket", "volume_bucket",
                                  "bollinger_position", "trend_score_prev"])
    N = len(df)
    print(f"\nTotal de barras (após warmup NaN): {N}")

    f_any_reversal = df["regime_reversal"].astype(bool)
    f_bullish_rev = f_any_reversal & (df["trend_score_prev"] < 0)
    f_bull_consol = (df["trend_score"].abs() < 0.3) & (df["trend_score_prev"] < -0.2)
    f_either = f_bullish_rev | f_bull_consol
    f_atr_high = df["atr_bucket"] == "high_atr"
    f_vol_low = df["volume_bucket"] == "low_volume"
    f_bb_inside = df["bollinger_position"] == "inside_band"

    print("\n--- Filtros individuais ---")
    print(f"  any regime_reversal:          {f_any_reversal.sum():>7}  ({f_any_reversal.mean()*100:.1f}%)")
    print(f"  bullish_reversal (prev<0):    {f_bullish_rev.sum():>7}  ({f_bullish_rev.mean()*100:.1f}%)")
    print(f"  bullish_consolidation:        {f_bull_consol.sum():>7}  ({f_bull_consol.mean()*100:.1f}%)")
    print(f"  bullish_reversal OR consol:   {f_either.sum():>7}  ({f_either.mean()*100:.1f}%)")
    print(f"  atr_bucket = high_atr:        {f_atr_high.sum():>7}  ({f_atr_high.mean()*100:.1f}%)")
    print(f"  volume_bucket = low_volume:   {f_vol_low.sum():>7}  ({f_vol_low.mean()*100:.1f}%)")
    print(f"  bollinger = inside_band:      {f_bb_inside.sum():>7}  ({f_bb_inside.mean()*100:.1f}%)")

    print("\n--- Combinações parciais ---")
    c1 = f_either & f_atr_high
    c2 = f_either & f_vol_low
    c3 = f_either & f_bb_inside
    c4 = f_either & f_atr_high & f_vol_low
    c5 = f_either & f_atr_high & f_bb_inside
    c6 = f_either & f_vol_low & f_bb_inside
    c_all = f_either & f_atr_high & f_vol_low & f_bb_inside

    print(f"  bullish + atr_high:                              {c1.sum():>7}")
    print(f"  bullish + vol_low:                               {c2.sum():>7}")
    print(f"  bullish + bb_inside:                             {c3.sum():>7}")
    print(f"  bullish + atr_high + vol_low:                    {c4.sum():>7}")
    print(f"  bullish + atr_high + bb_inside:                  {c5.sum():>7}")
    print(f"  bullish + vol_low + bb_inside:                   {c6.sum():>7}")
    print(f"  bullish + atr_high + vol_low + bb_inside (ALL):  {c_all.sum():>7}")

    if c_all.sum() > 0:
        sample = df[c_all].head(3)[["close", "trend_score", "trend_score_prev",
                                     "atr_bucket", "volume_bucket", "bollinger_position"]]
        print("\nExemplos de barras que passam em todos os filtros:")
        print(sample.to_string())


def main() -> None:
    t0 = time.perf_counter()
    print("Carregando candles...")
    df = load_df()
    print(f"  {len(df)} barras carregadas")

    print("Inicializando estratégia...")
    strategy = ReversaoNextGenV1Strategy(**PARAMS)
    strategy.initialize()

    print("Pré-calculando indicadores...")
    enriched = strategy.calculate(df.copy())
    print(f"  Pré-cálculo: {time.perf_counter()-t0:.2f}s")

    print("Executando backtest completo 2024-01-01 .. 2024-12-31...")
    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=CAPITAL))
    result = engine.run(df.copy(), symbol=SYMBOL)

    m = result.metrics
    print("\n==============================================")
    print("BACKTEST RESULTS — ReversaoNextGenV1 (LONG)")
    print("==============================================")
    print(f"  Trades:         {m.total_trades}")
    print(f"  Win Rate:       {m.win_rate:.2%}")
    print(f"  Profit Factor:  {m.profit_factor:.4f}")
    print(f"  Sharpe:         {m.sharpe_ratio:.4f}")
    print(f"  Expectancy:     {m.expectancy:.4f}")
    print(f"  Drawdown:       {m.max_drawdown_pct:.4f}")
    print(f"  Net Profit:     {m.net_profit:.2f}")
    print(f"  Total time:     {time.perf_counter()-t0:.2f}s")

    if m.total_trades == 0:
        print("\n⚠ Zero trades — auditando filtros...")
        audit_filters(enriched)


if __name__ == "__main__":
    main()
