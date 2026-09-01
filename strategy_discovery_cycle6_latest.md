# Strategy Discovery Cycle 6

Decision: **ALL_FAILED**
Data: 2024-01-01 to 2026-08-18 (960 days)
Configurations tested: **36**

## Controls
- Lookahead bias: `NO`
- Split: 60% DEV / 20% VALIDATION / 20% OOS
- OOS opened only after frozen DEV selection
- Costs: 0.1% per side; stress 0.15% per side

## BTC_LEAD_LAG
- Hypothesis: Um movimento recente do BTC pode se propagar aos demais ativos com atraso quando o ativo ainda nao acompanhou o retorno do lider.
- Configurations: 12
- Best DEV result: PF=0.5220, expectancy=-3.4925; rejected at DEV gate.

## MARKET_BREADTH_ALIGNMENT
- Hypothesis: A continuidade de alta de um ativo pode ser mais provavel quando a participacao direcional e ampla entre os quatro pares, em vez de isolada.
- Configurations: 12
- Best DEV result: PF=0.5642, expectancy=-1.6062; rejected at DEV gate.

## CORRELATION_DECOUPLING
- Hypothesis: Um ativo com retorno positivo e correlacao recente baixa com o ativo de referencia pode refletir fluxo idiossincratico com persistencia curta.
- Configurations: 12
- Best DEV result: PF=0.5570, expectancy=-4.3039; rejected at DEV gate.

## Decision

No strategy is promoted automatically. The official CDB Paper Live remains isolated.
