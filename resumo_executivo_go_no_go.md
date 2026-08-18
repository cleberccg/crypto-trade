# RESUMO EXECUTIVO - GO / NO-GO

## Situacao atual do projeto

O projeto possui infraestrutura operacional validada, sem evidencia de regressao estrutural no algoritmo, no PositionSizer, no RiskManager ou em SL/TP. A investigacao cientifica da ClassicDonchianBreakout concluiu que o problema atual nao e uma falha operacional central, mas a combinacao entre edge heterogeneo e base estatistica insuficiente para promover qualquer configuracao como baseline operacional definitiva.

## Principais evidencias

- Melhor combinacao observada: SOL/USDT 1h.
- Melhor Reliability Index: 66.1/100.
- Timeframe com melhor desempenho agregado: 1h.
- Variaveis com maior poder discriminatorio: candle body, candle range e volume.
- 12 de 12 combinacoes possuem menos de 30 trades.
- 0 combinacoes classificadas como ROBUSTA.
- 4 combinacoes classificadas como PROMISSORA.
- 6 combinacoes classificadas como DESCARTADA no baseline atual.
- Rolling longo por combinacao indisponivel por falta de amostra.

## Hipoteses confirmadas

- Existe heterogeneidade relevante entre ativos.
- Existe heterogeneidade relevante entre timeframes.
- O edge nao desapareceu completamente.
- O principal gargalo atual e o tamanho da amostra.

## Hipoteses inconclusivas

- Estabilidade temporal longa de SOL/USDT 1h.
- Estabilidade temporal longa de ETH/USDT 1h.
- Persistencia estrutural dos sinais observados em janela maior.

## Melhor combinacao observada

SOL/USDT 1h foi a melhor combinacao observacional consolidada.

Resumo quantitativo:

- trades: 10
- PF: 2.9960
- Expectancy: 2.0406
- Reliability Index: 66.1/100
- Classificacao: PROMISSORA

Principais bloqueios para classificacao superior:

- menos de 30 trades;
- menos de 50 trades;
- IC95 ainda amplo;
- rolling longo indisponivel;
- ruptura temporal entre blocos cronologicos.

## Principais limitacoes

- Janela de trades persistidos curta: 2026-06-14 a 2026-07-08.
- Todas as combinacoes abaixo de 30 trades.
- Nenhuma combinacao com robustez alta.
- Bootstrap e intervalos de confianca ainda amplos em varias combinacoes.
- Ausencia de rolling longo por combinacao.

## Decisao cientifica

**NO-GO (evidencia insuficiente)**

Fundamentacao:

- nao existe nenhuma combinacao ROBUSTA;
- o melhor indice composto ainda e apenas moderado;
- toda a matriz permanece abaixo do limiar minimo de 30 trades por combinacao;
- a estabilidade temporal longa nao pode ser confirmada;
- as melhores combinacoes permanecem como hipoteses promissoras, nao como conclusoes definitivas.

## Implicacao oficial

O baseline cientifico v1.0 estabelece que a versao atual da ClassicDonchianBreakout nao deve ser reinterpretada como comprovadamente robusta com base na amostra atual. O projeto deve preservar esta baseline e reabrir a investigacao apenas quando os criterios quantitativos de nova evidencia forem atendidos.
