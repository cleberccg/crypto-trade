# Strategy Discovery Cycle 3

Decision: **ALL_FAILED**
Data: 2024-01-01 to 2026-08-18 (960 days)
Configurations tested: **36**

## Controls
- Lookahead bias: `NO`
- Split: 60% DEV / 20% VALIDATION / 20% OOS
- OOS opened only after frozen DEV selection
- Costs: 0.1% per side; stress 0.15% per side

## MARKET_STRUCTURE
- Hypothesis: A quebra confirmada de uma maxima estrutural recente, acima de uma media de tendencia, pode iniciar continuacao direcional.
- Configurations: 12
- Best DEV result: PF=0.5963, expectancy=-4.7589; rejected at DEV gate.

## VWAP
- Hypothesis: Deslocamentos negativos relevantes em relacao ao VWAP diario tendem a retornar ao VWAP quando nao ha ruptura estrutural persistente.
- Configurations: 12
- Best DEV result: PF=0.5005, expectancy=-4.6289; rejected at DEV gate.

## RELATIVE_STRENGTH
- Hypothesis: Ativos com forca relativa persistente acima da propria tendencia media podem continuar performando no horizonte curto.
- Configurations: 12
- Best DEV result: PF=0.6130, expectancy=-1.7331; rejected at DEV gate.

## Decision

No strategy is promoted automatically. The official CDB Paper Live remains isolated.
