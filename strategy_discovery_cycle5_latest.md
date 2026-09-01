# Strategy Discovery Cycle 5

Decision: **ALL_FAILED**
Data: 2024-01-01 to 2026-08-18 (960 days)
Configurations tested: **36**

## Controls
- Lookahead bias: `NO`
- Split: 60% DEV / 20% VALIDATION / 20% OOS
- OOS opened only after frozen DEV selection
- Costs: 0.1% per side; stress 0.15% per side

## CROSS_SECTIONAL_STRENGTH
- Hypothesis: O ativo lider do retorno recente entre os quatro pares pode continuar relativamente forte no proximo horizonte.
- Configurations: 12
- Best DEV result: PF=0.6189, expectancy=-2.1351; rejected at DEV gate.

## IMPULSE_PULLBACK
- Hypothesis: Apos um impulso de alta, um recuo controlado ate a media seguido de recuperacao pode oferecer entrada com melhor assimetria.
- Configurations: 12
- Best DEV result: PF=0.5457, expectancy=-3.1540; rejected at DEV gate.

## VOLUME_CONFIRMED_BREAKOUT
- Hypothesis: Rompimento de maxima recente acompanhado de volume relativo acima da media pode representar participacao nova e continuidade.
- Configurations: 12
- Best DEV result: PF=0.6094, expectancy=-4.7295; rejected at DEV gate.

## Decision

No strategy is promoted automatically. The official CDB Paper Live remains isolated.
