# STRATEGY_CATALOG

Catálogo científico permanente de estratégias da plataforma.

| Estratégia | Origem | Referência | Família | Indicadores | Parâmetros padrão | Status |
|---|---|---|---|---|---|---|
| ClassicEMACrossover | Livro | Murphy (1999), Technical Analysis of the Financial Markets | Tendência | EMA | {"ema_fast": 12, "ema_slow": 26} | Otimizada |
| ClassicSMACrossover | Livro | Pring (2002), Technical Analysis Explained | Tendência | SMA | {"sma_fast": 10, "sma_slow": 30} | Otimizada |
| ClassicMACDTrend | Livro | Appel (1979), The Moving Average Convergence-Divergence Trading Method | Momentum | MACD | {"macd_fast": 12, "macd_slow": 26, "macd_signal": 9} | Otimizada |
| ClassicRSIMeanReversion | Livro | Wilder (1978), New Concepts in Technical Trading Systems | Reversão | RSI | {"rsi_period": 14, "rsi_buy": 30} | Implementada |
| ClassicBollingerReversal | Livro | Bollinger (2001), Bollinger on Bollinger Bands | Reversão | BollingerBands | {"bb_period": 20, "bb_std": 2.0} | Implementada |
| ClassicDonchianBreakout | Livro | Donchian channels, Turtle Trading rules | Breakout | Donchian | {"donchian_window": 20} | Implementada |
| ClassicATRBreakout | Livro | Wilder ATR applications, public quant implementations | Volatilidade | ATR | {"atr_period": 14, "atr_mult": 1.5} | Implementada |
| ClassicVWAPReversion | Open Source | QuantConnect/Quantpedia style VWAP mean reversion concepts | Reversão | VWAP | {"vwap_dev_pct": 0.3} | Implementada |
| ClassicKeltnerChannel | Livro | Keltner Channel public formulations | Volatilidade | EMA, ATR | {"ema_period": 20, "atr_period": 14, "kc_mult": 2.0} | Implementada |
| ClassicDualMomentum | Livro | Antonacci (2014), Dual Momentum Investing | Momentum | ROC, EMA | {"momentum_window": 20, "ema_trend": 50} | Implementada |
| SuperTrendV1 | Open Source | TradingView/Community SuperTrend formulations | Tendência | ATR, SuperTrend | {"atr_period": 10, "atr_multiplier": 3.0, "trend_confirmation": 1, "stop_atr_multiplier": 2.0, "take_profit_pct": 0.0, "risk_reward_ratio": 2.0} | Implementada |
