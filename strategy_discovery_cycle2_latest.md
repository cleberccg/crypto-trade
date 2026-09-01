# Strategy Discovery Cycle 2

Decision: **ALL_FAILED**
Data: 2024-01-01 to 2026-08-18 (960 days)
Configurations tested: **36**

## Controls
- Lookahead bias: `NO`
- Split: 60% DEV / 20% VALIDATION / 20% OOS
- OOS opened only after frozen DEV selection
- Costs: 0.1% per side; stress 0.15% per side

## RANGE
- Hypothesis: Em mercados laterais, compras proximas ao limite inferior do range tendem a retornar ao centro antes de uma ruptura persistente.
- Configurations: 12
- Best DEV result: PF=0.5652, expectancy=-19.8999; rejected at DEV gate.

## OPENING_RANGE
- Hypothesis: A quebra do range formado no inicio de cada dia UTC pode capturar a primeira expansao direcional intradiaria.
- Configurations: 12
- Best DEV result: PF=0.4393, expectancy=-19.9124; rejected at DEV gate.

## LIQUIDITY_SWEEP
- Hypothesis: Um falso rompimento do fundo recente seguido de fechamento de recuperacao pode indicar absorcao e reversao curta.
- Configurations: 12
- Best DEV result: PF=0.5141, expectancy=-21.2409; rejected at DEV gate.

## Decision

No strategy is promoted automatically. The official CDB Paper Live remains isolated.
