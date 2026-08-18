"""
Diagnóstico: mede o tempo de 1 backtest isolado da ReversaoNextGenV1
e reporta onde trava (se travar).
"""
import time
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

# Parameters: use the simplest combination from the grid
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

def main():
    t0 = time.time()
    print(f"[{elapsed(t0)}] Iniciando diagnóstico...")
    print(f"Params: {PARAMS}")

    print(f"[{elapsed(t0)}] Carregando candles...")
    from database.connection import get_session
    from database.repositories import CandleRepository
    import pandas as pd

    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(SYMBOL, TIMEFRAME, START, END)

    print(f"[{elapsed(t0)}] Candles carregados: {len(candles)} bars")

    df = pd.DataFrame(
        [{"open": c.open, "high": c.high, "low": c.low, "close": c.close, "volume": c.volume}
         for c in candles],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )
    print(f"[{elapsed(t0)}] DataFrame criado: {len(df)} linhas")

    print(f"[{elapsed(t0)}] Criando estratégia...")
    from strategies.factory import create_strategy
    strategy = create_strategy("ReversaoNextGenV1", **PARAMS)
    strategy.initialize()
    print(f"[{elapsed(t0)}] Estratégia inicializada")

    print(f"[{elapsed(t0)}] Pré-calculando indicadores no dataset completo...")
    _ = strategy.calculate(df.copy())
    print(f"[{elapsed(t0)}] Pré-cálculo concluído")

    print(f"[{elapsed(t0)}] Iniciando BacktestEngine.run()...")
    from backtesting.engine import BacktestEngine, BacktestConfig
    engine = BacktestEngine(strategy, config=BacktestConfig(initial_capital=CAPITAL))

    t_engine = time.time()
    result = engine.run(df.copy(), symbol=SYMBOL)
    t_done = time.time()

    print(f"[{elapsed(t0)}] BacktestEngine.run() CONCLUÍDO em {t_done - t_engine:.2f}s")
    print(f"  Trades: {result.metrics.total_trades}")
    print(f"  Profit Factor: {result.metrics.profit_factor:.4f}")
    print(f"  Sharpe: {result.metrics.sharpe_ratio:.4f}")
    print(f"  Win Rate: {result.metrics.win_rate:.2%}")
    print(f"  Drawdown: {result.metrics.max_drawdown_pct:.4f}")
    print(f"[{elapsed(t0)}] TOTAL: {time.time() - t0:.2f}s")

def elapsed(t0):
    return f"+{time.time()-t0:.1f}s"

if __name__ == "__main__":
    main()
