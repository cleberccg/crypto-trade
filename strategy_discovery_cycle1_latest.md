# Strategy Discovery Cycle 1

Decision: **ALL_FAILED**
Data: 2024-01-01 to 2026-08-18 (960 days)
Configurations tested: **36**

## Controls
- Lookahead bias: `NO`
- Split: 60% DEV / 20% VALIDATION / 20% OOS
- OOS opened only after frozen DEV selection
- Costs: 0.1% per side; stress 0.15% per side

## MOMENTUM
- Hypothesis: Persistencia direcional e aceleracao recente podem continuar apos confirmacao de tendencia.
- Configurations: 12
- Best DEV result: PF=0.6130, expectancy=-1.7331; rejected at DEV gate.

## MEAN_REVERSION
- Hypothesis: Desvios extremos da media revertem quando a inclinacao recente nao indica tendencia forte.
- Configurations: 12
- Best DEV result: PF=0.3773, expectancy=-2.8068; rejected at DEV gate.

## VOLATILITY_BREAKOUT
- Hypothesis: Expansao de volatilidade apos compressao pode gerar rompimento direcional persistente.
- Configurations: 12
- {"compression_ratio": 0.8, "expansion_ratio": 1.2, "range_window": 48} | DEV PF=11.9940 | Validation PF=0.0 | OOS PF=None | OOS expectancy=None | Generalization=CONCENTRATED | Status=REJECTED_VALIDATION_GATE

## Decision

No strategy is promoted automatically. The official CDB Paper Live remains isolated.
