# RELATORIO FINAL - ENCERRAMENTO DA INVESTIGACAO CIENTIFICA v1.0

## Identificacao

- Projeto: Crypto Trading Bot para Binance Spot
- Estrategia avaliada: ClassicDonchianBreakout@v1.0
- Natureza deste documento: baseline cientifica oficial
- Escopo: consolidacao documental das evidencias produzidas ate o momento
- Restricao central: nenhum algoritmo, filtro, parametro ou componente operacional foi alterado nesta etapa

## Capitulo 1 - Historico da Investigacao

### Objetivo inicial do projeto

O objetivo inicial foi validar se a estrategia ClassicDonchianBreakout mantinha edge estatistico suficiente para sustentar sua utilizacao no ambiente paper/live do projeto, preservando a arquitetura operacional ja validada.

### Hipotese original

A hipotese original era que a perda percebida de desempenho poderia estar associada a uma das seguintes causas:

- regressao de codigo;
- falha de execucao;
- erro de sizing ou risk management;
- problema em SL ou TP;
- perda real de edge da estrategia;
- mudanca de regime de mercado;
- heterogeneidade por ativo ou timeframe.

### Problemas encontrados

Ao longo da investigacao, o principal problema empirico encontrado nao foi uma falha unica de infraestrutura ou de execucao, mas a combinacao entre:

- heterogeneidade forte entre ativos e timeframes;
- amostra curta de trades persistidos da estrategia;
- sinais locais de edge que nao atingem robustez estatistica suficiente para promocao operacional;
- rupturas temporais entre blocos cronologicos em grande parte das combinacoes.

### Metodologia adotada

A metodologia adotada foi estritamente observacional, quantitativa e reprodutivel:

- validacao dos componentes estruturais e operacionais congelados;
- extracao de trades historicos persistidos e enriquecimento com contexto de candles;
- analise de WIN vs LOSS, MFE/MAE, falsos rompimentos, regimes e heterogeneidade por ativo;
- validacao estatistica com bootstrap, intervalos de confianca e effect sizes;
- avaliacao de estabilidade temporal, persistencia mensal e indice composto de confiabilidade;
- consolidacao final em matriz cientifica unica por Ativo + Timeframe.

### Fases executadas

- Fases iniciais de validacao estrutural: regressao, PositionSizer, RiskManager, SL/TP e fluxo operacional.
- Investigacao cientifica de edge: [optimization/results/investigacao_cdb_edge_loss_20260801_112203.md](optimization/results/investigacao_cdb_edge_loss_20260801_112203.md)
- FASE 13 - Validacao Estatistica e Localizacao do Edge: [optimization/results/fase13_validacao_estatistica_edge_20260801_114412.md](optimization/results/fase13_validacao_estatistica_edge_20260801_114412.md)
- FASE 14 - Validacao da Estabilidade Temporal do Edge: [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102.md](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102.md)
- FASE 14B - Consolidacao das Evidencias e Matriz Final de Decisao: [optimization/results/fase14b_consolidacao_evidencias_20260801_122611.md](optimization/results/fase14b_consolidacao_evidencias_20260801_122611.md)

### Linha do tempo resumida

| Marco | Objetivo | Resultado resumido |
|---|---|---|
| Validacao estrutural | Testar regressao e componentes de risco/execucao | Sem evidencia de regressao estrutural |
| Investigacao de edge | Localizar causas da perda de desempenho | Edge heterogeneo por ativo e timeframe |
| FASE 13 | Testar significancia estatistica local | 1h melhor agregado; candle body, candle range e volume com maior poder discriminatorio |
| FASE 14 | Testar estabilidade temporal | Sem base suficiente para afirmar estabilidade longa; rolling longo indisponivel por amostra |
| FASE 14B | Consolidar em matriz final | Nenhuma combinacao robusta; 4 promissoras; gargalo principal = tamanho da amostra |

## Capitulo 2 - Hipoteses Investigadas

| Hipotese | Motivacao | Metodo utilizado | Resultado | Classificacao |
|---|---|---|---|---|
| Regressao de codigo | Queda de desempenho poderia vir de alteracao indevida | Validacoes anteriores do fluxo e comportamento | Nao foi identificada regressao de codigo | Refutada |
| Problema de execucao | Perda de edge poderia ser artefato de execucao | Auditorias operacionais anteriores e consistencia de persistencia | Nao surgiram evidencias de falha estrutural de execucao | Refutada |
| Problema de sizing | Mau desempenho poderia vir de exposicao incorreta | Validacoes anteriores do PositionSizer | PositionSizer permaneceu validado | Refutada |
| Problema de risco | Losses poderiam estar ligados ao RiskManager | Validacoes anteriores do RiskManager | RiskManager permaneceu validado | Refutada |
| Problema de SL/TP | SL/TP poderiam estar cortando edge indevidamente | Fases anteriores e analise de MFE/MAE | SL/TP permaneceram corretos; nao explicam sozinhos a perda de desempenho | Refutada |
| Perda completa de edge | A estrategia poderia ter deixado de funcionar em toda a base | Analise por ativo, timeframe, bootstrap e matriz final | O edge nao desapareceu completamente; persiste de forma heterogenea | Refutada |
| Diferenca entre ativos | Alguns ativos poderiam sustentar edge e outros nao | Etapa por ativo, FASE 13 e FASE 14B | SOL e ETH concentraram sinais melhores; BTC e BNB piores | Confirmada |
| Diferenca entre timeframes | O desempenho poderia depender do horizonte temporal | FASE 13 e consolidacao | 1h apresentou os melhores sinais agregados | Confirmada |
| Mudanca de regime de mercado | O edge poderia depender do regime corrente | Analise por regimes e sensibilidade temporal | Ha indicios de dependencia contextual, mas sem prova estrutural longa | Parcialmente Confirmada |
| Estabilidade temporal longa | O edge poderia permanecer consistente no tempo | FASE 14 e 14B | Nao ha evidencia suficiente para confirmar estabilidade longa | Inconclusiva |
| Falso rompimento como causa central de degradacao | Breakouts falhos poderiam concentrar perdas | Analise de falsos rompimentos e MFE/MAE | Evidencia de relevancia importante, mas nao unica | Parcialmente Confirmada |

## Capitulo 3 - Evidencias Consolidadas

### Evidencias fortes

- Nao ha evidencia de regressao de codigo, falha estrutural de PositionSizer, RiskManager ou SL/TP. Forca: forte.
- Nao existe nenhuma combinacao Ativo + Timeframe classificada como ROBUSTA na consolidacao final. Forca: forte.
- O principal gargalo quantitativo atual e o tamanho da amostra: 12 de 12 combinacoes ficaram abaixo de 30 trades. Forca: forte.
- BTC/USDT 1h, BTC/USDT 5m, BTC/USDT 15m, BNB/USDT 1h, BNB/USDT 15m e SOL/USDT 5m foram classificadas como DESCARTADA no baseline atual. Forca: forte.

### Evidencias moderadas

- O timeframe 1h foi o melhor agregado na FASE 13: PF 1.4177, WR 33.33%, expectancy 0.4976. Forca: moderada.
- SOL/USDT 1h foi a melhor combinacao observacional: PF 2.9960 na FASE 13 e Reliability Index 66.1/100 na FASE 14/14B. Forca: moderada.
- ETH/USDT 1h apresentou persistencia mensal positiva em 2/2 meses para PF>1 e Expectancy>0, ainda com baixa robustez amostral. Forca: moderada.
- Candle body, candle range e volume diferenciaram WIN vs LOSS com IC95 da diferenca sem cruzar zero. Forca: moderada.
- Effect sizes de volume, candle range, candle body, relative volume, ADX e MACD foram classificados como medios. Forca: moderada.

### Evidencias fracas

- O bootstrap global da FASE 13 mostrou PF medio 0.9880 com IC95 [0.5975, 1.4951] e expectancy media -0.0287 com IC95 [-0.4921, 0.4706], portanto sem conclusao forte global. Forca: fraca.
- A persistencia mensal observada para algumas combinacoes se apoia em apenas 2 meses civis. Forca: fraca.
- O rolling longo por combinacao ficou indisponivel porque nenhuma serie Ativo + Timeframe atingiu 30 trades. Forca: fraca para qualquer afirmacao de estabilidade longa.
- A maioria das combinacoes exibiu ruptura temporal entre blocos cronologicos na FASE 14. Isso e evidenciario, mas ainda apoiado por amostras pequenas. Forca: fraca a moderada.

### Indicadores quantitativos consolidados

- Profit Factor: melhor sinal local em SOL/USDT 1h; sinais fracos ou negativos em BTC e boa parte de BNB.
- Win Rate: insuficiente isoladamente para promover qualquer combinacao; em varios casos WR nao acompanhou PF e expectancy.
- Expectancy: positiva apenas em parte das combinacoes promissoras; negativa nas descartadas.
- Drawdown: baixo em termos absolutos no recorte atual, mas pouco informativo dada a pequena amostra.
- Bootstrap: util para mostrar probabilidade de PF>1 e Expectancy>0, mas ainda com IC amplo.
- Intervalos de confianca: ainda largos e frequentemente cruzando os limiares de decisao.
- Rolling windows: apenas rolling curto na FASE 13; rolling longo indisponivel na FASE 14 por falta de trades.
- Effect size: melhor poder discriminatorio em candle body, candle range e volume; ATR fraco.
- Reliability Index: melhor valor em SOL/USDT 1h = 66.1/100, ainda abaixo de evidência forte.

## Capitulo 4 - Estado Atual da Estrategia

### Existe edge?

Sim, existe evidencia de edge parcial e heterogeneo. Nao existe evidencia de desaparecimento completo do edge.

### Onde existe?

Os melhores sinais observacionais ficaram concentrados em:

- SOL/USDT 1h
- ETH/USDT 1h
- ETH/USDT 15m
- BNB/USDT 5m

Entre esses, a melhor combinacao observada foi SOL/USDT 1h. Ainda assim, nenhuma combinacao atingiu robustez suficiente para classificacao definitiva como ROBUSTA.

### Onde nao existe?

Os resultados atuais nao sustentam edge utilizavel nas combinacoes classificadas como DESCARTADA no baseline atual:

- SOL/USDT 5m
- BNB/USDT 1h
- BNB/USDT 15m
- BTC/USDT 15m
- BTC/USDT 5m
- BTC/USDT 1h

### Quais ativos continuam promissores?

- SOL/USDT, especialmente em 1h
- ETH/USDT, especialmente em 1h e 15m

### Quais ativos devem permanecer em observacao?

- SOL/USDT 15m
- ETH/USDT 5m
- BNB/USDT 5m

Essa observacao deve ser entendida apenas como prioridade de coleta de evidencia, nao como autorizacao operacional.

### Quais ativos apresentaram desempenho insuficiente?

- BTC/USDT em todos os timeframes avaliados
- BNB/USDT em 1h e 15m
- SOL/USDT em 5m

## Capitulo 5 - Limitacoes Estatisticas

As limitacoes estatisticas atuais impedem conclusoes definitivas por quatro motivos principais.

### 1. Tamanho da amostra

- Todas as 12 combinacoes Ativo + Timeframe possuem menos de 30 trades.
- Nenhuma combinacao atingiu 50 trades.
- Nenhuma combinacao permite classificacao de superioridade com base robusta.

### 2. Limitacao temporal

- O historico de candles e amplo, mas o historico de trades persistidos da estrategia cobre apenas 2026-06-14 a 2026-07-08.
- Isso representa uma janela curta demais para afirmar estabilidade estrutural de longo prazo.

### 3. Ausencia de rolling longo

- Nenhuma serie Ativo + Timeframe atingiu 30 trades.
- Consequentemente, a FASE 14 nao conseguiu testar rolling longo por combinacao em 30, 50, 75 e 100 trades.
- Sem rolling longo, nao e possivel afirmar se a trajetoria do edge e persistente ou apenas um artefato local da janela observada.

### 4. Limitacoes de bootstrap e IC

- Os intervalos de confianca ainda sao amplos e frequentemente cruzam os limiares decisorios de PF=1 e Expectancy=0.
- O bootstrap mostra probabilidades uteis, mas ainda sob alta variancia porque a base e curta.
- Em combinacoes pequenas, metricas como PF podem inflar ou colapsar rapidamente por poucos trades.

Essas limitacoes impedem conclusoes definitivas porque reduzem fortemente o poder estatistico. Em termos práticos, a evidencia atual suporta observacoes locais e prioridades de pesquisa, mas nao promocao cientifica definitiva de nenhuma combinacao.

## Capitulo 6 - Matriz Cientifica Final

| Ativo | Timeframe | Classificacao Final | Trades | Reliability Index | Nivel de Evidencia | Status | Justificativa objetiva |
|---|---|---:|---:|---:|---|---|---|
| SOL/USDT | 1h | PROMISSORA | 10 | 66.1 | Moderada | PROMISSORA | PF>1, Expectancy>0, melhor indice da base, mas <30 trades, IC amplo, rolling indisponivel e ruptura temporal |
| ETH/USDT | 15m | PROMISSORA | 12 | 50.6 | Fraca | PROMISSORA | PF>1, Expectancy>0 e bootstrap razoavel, mas base curta e instabilidade temporal |
| BNB/USDT | 5m | PROMISSORA | 11 | 46.2 | Fraca | PROMISSORA | Sinal local positivo, mas contradito por baixa robustez, IC amplo e classificacao observacional negativa do ativo |
| ETH/USDT | 1h | PROMISSORA | 10 | 45.6 | Fraca | PROMISSORA | Persistencia mensal positiva em 2/2 meses, mas bootstrap e IC ainda insuficientes |
| SOL/USDT | 15m | AMOSTRA INSUFICIENTE | 17 | 47.0 | Fraca | AMOSTRA INSUFICIENTE | Sinais mistos, indice abaixo de 60 e bootstrap instavel |
| ETH/USDT | 5m | AMOSTRA INSUFICIENTE | 18 | 32.0 | Insuficiente | AMOSTRA INSUFICIENTE | PF<1 e Expectancy negativa, mas sem base suficiente para descarte forte definitivo |
| SOL/USDT | 5m | DESCARTADA | 24 | 21.7 | Insuficiente | DESCARTADA | PF<1, Expectancy negativa, bootstrap fraco, ruptura temporal e indice baixo |
| BNB/USDT | 1h | DESCARTADA | 7 | 28.3 | Insuficiente | DESCARTADA | PF<1, Expectancy negativa, bootstrap fraco e baixa robustez extrema |
| BNB/USDT | 15m | DESCARTADA | 12 | 21.1 | Insuficiente | DESCARTADA | PF<1, Expectancy negativa, IC fraco, bootstrap instavel e ruptura temporal |
| BTC/USDT | 15m | DESCARTADA | 7 | 25.1 | Insuficiente | DESCARTADA | PF<1, Expectancy negativa, baixa probabilidade bootstrap e fraca recorrencia |
| BTC/USDT | 5m | DESCARTADA | 14 | 10.9 | Insuficiente | DESCARTADA | Pior bloco de metrics entre os recortes maiores; bootstrap e expectancy fortemente negativos |
| BTC/USDT | 1h | DESCARTADA | 6 | 13.5 | Insuficiente | DESCARTADA | PF muito baixo, expectancy negativa e ausencia de recorrencia positiva |

Fonte consolidada da matriz: [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_master_edge_matrix.csv](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_master_edge_matrix.csv)

## Capitulo 7 - Licoes Aprendidas

### O que aprendemos?

- O problema nao e uma regressao estrutural simples do robo.
- O edge nao desapareceu por completo; ele se distribui de forma desigual por ativo e timeframe.
- O timeframe 1h e o melhor candidato agregado no baseline atual.
- Variaveis de estrutura de candle e volume informam mais do que ATR para diferenciar WIN vs LOSS.
- A estrategia pode apresentar bons sinais locais e ainda assim permanecer cientificamente nao promovivel por falta de base.

### Quais hipoteses estavam erradas?

- A hipotese de regressao de codigo como causa principal.
- A hipotese de falha estrutural de PositionSizer ou RiskManager.
- A hipotese de que o edge desapareceu completamente em todos os ativos e timeframes.

### Quais hipoteses permanecem abertas?

- Se SOL/USDT 1h mantera vantagem quando atingir amostra estatisticamente relevante.
- Se ETH/USDT 1h preservara persistencia em horizonte temporal mais longo.
- Se a heterogeneidade observada e estrutural ou apenas produto da janela atual.
- Se os falsos rompimentos continuarao sendo o principal mecanismo de degradacao em amostras maiores.

### Quais decisoes futuras passam a ser orientadas pelos dados?

- Nenhuma combinacao deve ser promovida como configuracao definitiva sem nova evidencia.
- Prioridade de coleta deve se concentrar primeiro nas combinacoes PROMISSORA.
- O baseline oficial passa a tratar BTC como sinal negativo no recorte atual.
- A reabertura da investigacao deve obedecer a gatilhos quantitativos objetivos.

## Capitulo 8 - Criterios para Reabrir a Investigacao

Reexecutar as FASES 13, 14 e 14B somente quando ao menos uma das condicoes abaixo for atendida:

1. Alguma combinacao Ativo + Timeframe atingir pelo menos 50 trades persistidos em `trade_history`.
2. O intervalo entre o primeiro e o ultimo trade persistido da estrategia atingir pelo menos 180 dias.
3. Alguma combinacao atingir pelo menos 30 trades, viabilizando rolling longo por combinacao.
4. O Profit Factor agregado dos ultimos 30 trades persistidos da estrategia cair abaixo de 0.90 em dois fechamentos mensais consecutivos.
5. Houver alteracao planejada e aprovada na estrategia ClassicDonchianBreakout, em suas regras de sinal, ou em qualquer componente diretamente ligado a geracao/gestao cientifica do trade.
6. Houver mudanca significativa de regime de mercado definida por reclassificacao material dos regimes usados no estudo e refletida em novo bloco de trades persistidos.

Esses criterios sao quantitativos e reproduziveis porque dependem de contagem de trades, duracao da serie, rolling viabilizado, limiar numerico de PF e alteracoes controladas de estrategia.

## Capitulo 9 - Conclusao Cientifica

### 1. O algoritmo apresentou regressao?

Nao. A investigacao nao encontrou evidencia de regressao de codigo como causa central do problema.

### 2. Existe evidencia de falha estrutural?

Nao ha evidencia de falha estrutural em PositionSizer, RiskManager, SL/TP ou fluxo operacional. O problema observado e predominantemente estatistico e contextual.

### 3. O edge desapareceu completamente?

Nao. O edge nao desapareceu completamente. Ele persiste de forma heterogenea, com melhores sinais em SOL/USDT 1h e ETH/USDT 1h, mas sem robustez suficiente para promocao definitiva.

### 4. Qual e o principal gargalo atual?

O principal gargalo atual e o tamanho da amostra.

Justificativa quantitativa:

- 12 de 12 combinacoes abaixo de 30 trades;
- 0 combinacoes com robustez alta;
- rolling longo indisponivel por combinacao;
- intervalos de confianca amplos;
- bootstrap ainda instavel em varias combinacoes.

Portanto, o baseline cientifico oficial conclui que a limitacao atual e predominantemente amostral, e nao uma prova de falha estrutural do algoritmo.

## Capitulo 10 - Recomendacoes

### Acoes recomendadas agora

- Preservar este documento como baseline oficial da versao atual.
- Utilizar a matriz final como referencia primaria para futuras comparacoes.
- Priorizar apenas a coleta de nova evidencia nas combinacoes classificadas como PROMISSORA.
- Manter monitoramento documental das novas amostras em `trade_history`.

### Acoes proibidas neste momento

- Promover qualquer combinacao como configuracao operacional definitiva.
- Alterar parametros da estrategia com base apenas na amostra atual.
- Criar filtros novos usando os achados desta investigacao como gatilho operacional imediato.
- Reinterpretar sinais promissores como confirmacao de robustez estatistica.

### Acoes futuras condicionadas a novas evidencias

- Reabrir FASES 13, 14 e 14B quando os criterios do Capitulo 8 forem atingidos.
- Reavaliar SOL/USDT 1h e ETH/USDT 1h com amostra maior.
- Recomparar toda a matriz quando houver nova janela temporal suficiente para rolling longo por combinacao.

## Anexos

### Relatorios principais

- [optimization/results/investigacao_cdb_edge_loss_20260801_112203.md](optimization/results/investigacao_cdb_edge_loss_20260801_112203.md)
- [optimization/results/fase13_validacao_estatistica_edge_20260801_114412.md](optimization/results/fase13_validacao_estatistica_edge_20260801_114412.md)
- [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102.md](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102.md)
- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611.md](optimization/results/fase14b_consolidacao_evidencias_20260801_122611.md)

### Matrizes e tabelas finais

- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_master_edge_matrix.csv](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_master_edge_matrix.csv)
- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_summary_table.csv](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_summary_table.csv)
- [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_reliability_index.csv](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_reliability_index.csv)
- [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_temporal_split.csv](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_temporal_split.csv)
- [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_persistence.csv](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_persistence.csv)
- [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_historical_window.csv](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_historical_window.csv)
- [optimization/results/fase13_validacao_estatistica_edge_20260801_114412_asset_timeframe_metrics.csv](optimization/results/fase13_validacao_estatistica_edge_20260801_114412_asset_timeframe_metrics.csv)

### Graficos e dashboards

- [optimization/results/fase13_validacao_estatistica_edge_20260801_114412_rolling.png](optimization/results/fase13_validacao_estatistica_edge_20260801_114412_rolling.png)
- [optimization/results/fase13_validacao_estatistica_edge_20260801_114412_bootstrap_hist.png](optimization/results/fase13_validacao_estatistica_edge_20260801_114412_bootstrap_hist.png)
- [optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_heatmap.png](optimization/results/fase14_validacao_estabilidade_temporal_edge_20260801_122102_heatmap.png)
- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_heatmap.png](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_heatmap.png)
- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_reliability_bar.png](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_reliability_bar.png)
- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_trades_bar.png](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_trades_bar.png)
- [optimization/results/fase14b_consolidacao_evidencias_20260801_122611_radar.png](optimization/results/fase14b_consolidacao_evidencias_20260801_122611_radar.png)

## Encerramento formal

Esta investigacao cientifica v1.0 fica formalmente encerrada como baseline oficial da versao atual da estrategia ClassicDonchianBreakout no projeto. Ate nova reabertura sob criterios quantitativos definidos, a interpretacao oficial e a seguinte:

- nao ha regressao estrutural demonstrada;
- nao ha falha estrutural comprovada do algoritmo;
- existe edge parcial e heterogeneo;
- nao existe evidencia suficiente para promocao definitiva de qualquer combinacao;
- o principal gargalo atual e o tamanho da amostra.
