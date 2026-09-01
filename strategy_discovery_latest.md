# Strategy Discovery Latest

Data da auditoria: 2026-08-18. Inventário read-only seguido de consolidação de resultados existentes.

## Decisão

- `EXISTING_STRATEGIES = 19` implementadas/registradas.
- `EXISTING_EXPERIMENTS =` catálogo científico, auditoria, discovery, quantitative discovery, edge discovery, validação externa, Fase 13 factory e rolling OOS Fase 18.5.
- `REUSABLE_INFRASTRUCTURE =` registry/factory, `BacktestEngine`, optimizer, validation, bootstrap, candles comuns, warehouse científico e monitoramento Paper Live.
- `EXISTING_PROMISING_STRATEGY = YES`, somente como shortlist para validação: `ClassicEMACrossover`, `ClassicATRBreakout`, `ClassicMACDTrend`.
- `READY_FOR_PAPER = NO`. Nenhuma candidata demonstrou edge independente e generalizável.

A shortlist é contraditória: os probes de otimização mostram PF alto, mas os backtests-base da mesma campanha são negativos. Portanto, os probes são tratados como risco de overfitting, não como resultado promovível.

## Baseline CDB

- `TRADES = 446`
- `PF = 1.000983`
- `EXPECTANCY = +0.010224`
- `MAX_DD = 0.070645`
- `SHARPE = UNKNOWN`

A CDB permanece grupo controle. O status operacional atual observado foi `CAMPAIGN_RUNNING=YES`, `CONTEXTS_ACTIVE=12/12`, `CONTEXTS_STALE=0`, `DESYNC=0`, `CYCLES_ADVANCING=YES`.

## Ranking De Validação

| # | Strategy | Family | Base trades | Base PF | Base expectancy | Probe trades | Probe PF | Probe expectancy | OOS PF | OOS expectancy | Bootstrap | Generalization | Status |
|---|---|---|---:|---:|---:|---:|---:|---:|---|---|---|---|---|
| 1 | ClassicEMACrossover | TREND_FOLLOWING | 15 | 0.2829 | -0.8206 | 152 | 2.8304 | 2.8866 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | FAILED |
| 2 | ClassicATRBreakout | VOLATILITY_BREAKOUT | 8 | 0.4000 | -0.6754 | 125 | 2.4916 | 2.3152 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | FAILED |
| 3 | ClassicMACDTrend | MOMENTUM | 19 | 0.3523 | -0.8885 | 199 | 2.0562 | 3.2503 | UNKNOWN | UNKNOWN | UNKNOWN | UNKNOWN | FAILED |

## Inventário De Estratégias

| Strategy | File | Family | Status | Backtest | Results | Optimization |
|---|---|---|---|---|---|---|
| ClassicEMACrossover | strategies/classic_catalog_strategies.py | TREND_FOLLOWING | shortlist, failed fast gate | YES | YES | YES |
| ClassicSMACrossover | strategies/classic_catalog_strategies.py | TREND_FOLLOWING | rejected for paper | YES | YES | YES |
| ClassicMACDTrend | strategies/classic_catalog_strategies.py | MOMENTUM | shortlist, failed fast gate | YES | YES | YES |
| ClassicRSIMeanReversion | strategies/classic_catalog_strategies.py | MEAN_REVERSION | rejected | YES | YES | YES |
| ClassicBollingerReversal | strategies/classic_catalog_strategies.py | MEAN_REVERSION | rejected | YES | YES | YES |
| ClassicDonchianBreakout | strategies/classic_catalog_strategies.py | CONTROL | benchmark | YES | YES | YES |
| ClassicATRBreakout | strategies/classic_catalog_strategies.py | VOLATILITY_BREAKOUT | shortlist, failed fast gate | YES | YES | YES |
| ClassicVWAPReversion | strategies/classic_catalog_strategies.py | MEAN_REVERSION | rejected for paper | YES | YES | YES |
| ClassicKeltnerChannel | strategies/classic_catalog_strategies.py | VOLATILITY_BREAKOUT | rejected for paper | YES | YES | YES |
| ClassicDualMomentum | strategies/classic_catalog_strategies.py | MOMENTUM | rejected for paper | YES | YES | YES |
| TrendV1 / TrendV2 | strategies/trend_v1.py, strategies/trend_v2.py | TREND_FOLLOWING | rejected | YES | YES | YES |
| MeanReversionV1 | strategies/mean_reversion_v1.py | MEAN_REVERSION | rejected | YES | YES | YES |
| BreakoutV1 | strategies/breakout_v1.py | VOLATILITY_BREAKOUT | rejected | YES | YES | YES |
| ReversaoNextGenV1 / V2 | strategies/reversao_nextgen_v1.py, strategies/reversao_nextgen_v2.py | MEAN_REVERSION | inconclusive/failed | YES | YES | YES |
| SuperTrendV1 | strategies/supertrend_v1.py | VOLATILITY_BREAKOUT | rejected | YES | YES | YES |
| TradeOutcomeNextGenV1 / V1.1 | strategies/trade_outcome_nextgen_v1.py, strategies/trade_outcome_nextgen_v1_1.py | HYBRID | rejected | YES | YES | YES |

## Rejeições

- `MeanReversionV1`: PF 0.9075, expectancy -0.0341; validação externa reprovada.
- `BreakoutV1`: PF 0.8721, expectancy -0.0319; validação externa reprovada.
- `SuperTrendV1`: PF 0.6188, expectancy -0.1766; validação externa reprovada.
- `TradeOutcomeNextGenV1`: PF 0.8027, expectancy -0.7812; validação externa reprovada.
- `ReversaoNextGenV1/V2`: amostra insuficiente e PF/expectancy negativos nos resultados de campanha.
- Os clássicos restantes têm probes positivos, mas backtests-base negativos e não possuem OOS independente documentado.

## Próximo Experimento

Como `READY_FOR_PAPER = NO`, iniciar um novo ciclo de Strategy Discovery, sem criar Fase 19: no máximo três famílias, `MOMENTUM`, `MEAN_REVERSION` e `VOLATILITY_BREAKOUT`, usando o mesmo dataset comum de BTC/ETH/SOL/BNB em 5m/15m/1h, custos e execução fixos. Cada hipótese deve ser simples, documentada antes do backtest e eliminada imediatamente se PF < 1 ou expectancy < 0. Só finalistas recebem validação temporal, OOS e bootstrap IC95.

Nenhuma estratégia foi implantada em Paper Live ou Live.
