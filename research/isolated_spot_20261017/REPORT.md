# Pesquisa científica isolada — Binance Spot 5m/15m


Data: 17/10/2026, America/Sao_Paulo. Resultado: **nenhuma candidata aprovada para PAPER**. As regras testadas aumentaram a frequência, mas não demonstraram expectativa líquida positiva consistente. Esta conclusão se restringe às duas especificações congeladas e ao histórico usado; não prova que toda a família de estratégias seja inviável.


## Protocolo e reutilização

Foram executados 80 backtests: 16 combinações candidatas (duas regras × quatro ativos × dois timeframes), cada uma em DEV, validação, OOS e OOS com custos elevados; mais quatro referências 4h, no mesmo protocolo. Não houve busca de parâmetros, alteração das regras após observar resultados, aprendizado automático ou seleção antecipada por frequência.


Reutilizados: `backtesting.engine.BacktestEngine/BacktestConfig`, `backtesting.metrics.compute_metrics`, métricas puras de `utils.metrics`, RSI de `indicators.rsi`, `compute_adx` e a classe real `Sma200RegimeGatedStrategy` de `research/external_strategy_replication_strategies.py`; também os padrões de `RiskManager` e `PositionSizer`, sem alteração de seus arquivos ou métodos. O script é uma orquestração com estratégias e adaptadores exclusivos de pesquisa; não cria banco, serviço ou engine alternativa. O serviço científico existente persiste em banco por padrão; por isso não foi chamado. Aplicou-se a convenção cronológica 60/20/20 existente e bootstrap por blocos neste relatório isolado.


## Dados e integridade

Fonte efetivamente usada: `data/binance_spot_extended_klines.csv.gz`, somente leitura. Cada ativo tem 105.120 candles de 5m entre 01/06/2025 e 01/06/2026 exclusivo, totalizando 420.480 candles-base. Não foram detectadas lacunas, duplicatas, OHLC inválido, valores ausentes ou preços não positivos nessa janela. Candles de 15m e 4h foram agregados com intervalos completos, rótulo na abertura e fechamento à esquerda. O manifesto contém SHA-256 do arquivo fonte.

O cache parquet BTC 5m inicialmente inspecionado tinha 8.064 candles ausentes na janela e foi rejeitado. Não houve preenchimento ou interpolação. A tentativa de acessar o arquivo mensal público falhou em DNS; nenhum arquivo mensal foi usado. Uma consulta pública `exchangeInfo` teve sucesso e seus filtros foram arquivados. Nenhuma credencial foi usada. O CSV local já continha todos os quatro pares; a análise não depende de novos dados de mercado.

O histórico a partir de 01/06/2026 permanece fora dos testes, conforme o holdout já reservado no projeto. O CSV compactado foi lido sequencialmente para selecionar linhas: o conteúdo posterior ao corte existe no arquivo fonte, mas não entrou em indicadores, métricas, otimização ou decisões. OOS significa não usado neste protocolo; não se pode garantir que nunca tenha sido visto em pesquisas anteriores do projeto.


## Regras congeladas

**Pullback em tendência:** comprado/sem posição. SMA200 ascendente em 20 barras, close acima de SMA200 e ADX14 ≥ 20. Entrada quando o fechamento cruza EMA20 para cima após fechamento anterior abaixo da EMA20, com RSI14 entre 40 e 65. Saída quando close < EMA20, close < SMA200 ou RSI14 ≥ 70.

**Reversão à média com filtro de regime:** ADX14 < 20 e distância absoluta close/SMA200 − 1 inferior a 5%; entrada com close abaixo de SMA20 − 2 desvios padrão e RSI14 < 35. Saída no retorno à SMA20, ADX14 ≥ 25 ou close < 95% da SMA200.

**Referência:** sinais da implementação existente Sma200RegimeGated v1, com os padrões SMA200/ADX14, limiar ADX20 e inclinação de 20 barras. A referência foi simulada em todos os pares em 4h; a comparação diretamente relevante ao LIVE é BNB 4h. Não representa PnL da conta real.

As candidatas usam, por compatibilidade com a convenção de pesquisa existente, stop remoto em 1% do preço de entrada, TP em 100× e trailing de 99%, para que prevaleçam as saídas de sinal. Não são proteções propostas para produção. Isso também influencia o dimensionamento: com risco padrão de 1%, a alocação fica próxima de 1,01% do capital, antes do arredondamento de lote. Nenhum parâmetro ou gerenciamento LIVE foi alterado.


## Execução, custos e capital

Capital inicial hipotético: **1.000 USDT por combinação e por split**, reiniciado em cada split. Sem posições simultâneas dentro de uma combinação, sem short, sem alavancagem, sem empréstimo. Os testes de ativos são independentes; não devem ser somados como se utilizassem uma carteira compartilhada. PnLs monetários dependem do capital e do dimensionamento descritos.

O processo não carrega o `.env`: importa os padrões do código com credenciais vazias e banco em memória não utilizado. Usa RiskManager/PositionSizer existentes e, depois do cálculo, um adaptador exclusivamente de execução arredonda quantidade para baixo pelo LOT_SIZE e rejeita quantidade/notional abaixo do mínimo ou acima de maxQty. Não há alteração do algoritmo de risco. Os filtros são um snapshot público atual, não um histórico de filtros. As quantidades são muito inferiores aos limites MARKET_LOT_SIZE. MIN_NOTIONAL/NOTIONAL é aproximado pelo preço de execução, sem reproduzir o preço médio móvel da exchange; há margem ampla entre os tickets principais (~9–10 USDT) e o mínimo de 5 USDT.

Custo base por lado: taxa 0,10% + meio spread 0,01% + slippage 0,02% = **0,13%**. Ida e volta nominal: **0,26%**. Estresse: 0,10% + 0,01% + 0,08% = **0,19% por lado**, 0,38% ida e volta. Sem desconto BNB ou hipótese maker. O motor contabiliza a soma como débito proporcional ao notional em cada lado; é uma aproximação de custo econômico, não um simulador de book/fills. `results.csv` separa taxa, spread e slippage estimados.

O log operacional lido durante a pesquisa registrou **28,25577283 USDT livres** em 17/10 às 21:58:29. Não é saldo autenticado consultado pela pesquisa, nem garantia de saldo atual. Com os padrões de dimensionamento usados aqui, esse capital gera ticket de aproximadamente 0,285 USDT, inferior ao mínimo de 5 USDT: portanto, não executaria as candidatas neste protocolo. Esse caso é verificado separadamente. Não se propõe aumentar stake, risco ou aporte; adequação ao capital e à configuração real continuaria pendente mesmo diante de um backtest positivo.


## Causalidade e separação temporal

DEV usa os primeiros 60% dos candles, validação os 20% seguintes e OOS os últimos 20%. Cada split começa sem posição. DEV perde as primeiras 220 barras de aquecimento; validação e OOS usam 220 barras anteriores apenas para indicadores, sem operar essas barras. O período OOS é 20/03/2026 a 31/05/2026, cerca de 73 dias. Cada posição remanescente é liquidada na última abertura programada do split, com custo; não se transporta posição entre treino e teste.

Sinais são calculados exclusivamente em candles fechados, deslocados uma barra e executados na abertura seguinte. Na visão do motor, close/high/low são substituídos pelo open corrente depois de calcular os sinais, evitando uso de high/low futuros da barra de execução. O drawdown reportado é marcado nas aberturas, podendo subestimar extremos intrabar. Os stops remotos não são uma avaliação de execução real em gaps. `verification.json` verifica invariância de prefixos, execução na abertura, rejeição com capital pequeno e reconciliação de trades/PnL/custos.


## Resultados fora da amostra — custos base

PnL, expectativa e custos em USDT; DD é percentual do capital total de 1.000 USDT. Win rate e PF já incluem custos. Nenhum resultado desta tabela é lucro real.

| Ativo | TF | Estratégia | Trades | Trades/dia | PF líquido | PnL USDT | Win rate | Exp./trade USDT | DD % | Custos USDT |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 5min | Pullback tendência | 296 | 4.055 | 0.079 | -8.08 | 9.1% | -0.02730 | 0.81 | 7.442 |
| BTCUSDT | 5min | Reversão com regime | 97 | 1.329 | 0.145 | -2.16 | 18.6% | -0.02230 | 0.22 | 2.464 |
| BTCUSDT | 15min | Pullback tendência | 124 | 1.699 | 0.307 | -3.01 | 16.9% | -0.02426 | 0.30 | 3.123 |
| BTCUSDT | 15min | Reversão com regime | 23 | 0.315 | 0.619 | -0.21 | 39.1% | -0.00895 | 0.03 | 0.583 |
| ETHUSDT | 5min | Pullback tendência | 272 | 3.726 | 0.151 | -7.43 | 8.8% | -0.02731 | 0.76 | 7.038 |
| ETHUSDT | 5min | Reversão com regime | 91 | 1.247 | 0.223 | -2.06 | 19.8% | -0.02263 | 0.21 | 2.364 |
| ETHUSDT | 15min | Pullback tendência | 98 | 1.343 | 0.291 | -2.91 | 16.3% | -0.02973 | 0.30 | 2.540 |
| ETHUSDT | 15min | Reversão com regime | 33 | 0.452 | 0.470 | -0.67 | 54.5% | -0.02031 | 0.09 | 0.857 |
| SOLUSDT | 5min | Pullback tendência | 268 | 3.671 | 0.135 | -7.80 | 10.8% | -0.02910 | 0.79 | 6.980 |
| SOLUSDT | 5min | Reversão com regime | 120 | 1.644 | 0.271 | -2.57 | 33.3% | -0.02143 | 0.27 | 3.136 |
| SOLUSDT | 15min | Pullback tendência | 100 | 1.370 | 0.393 | -2.52 | 19.0% | -0.02525 | 0.26 | 2.611 |
| SOLUSDT | 15min | Reversão com regime | 26 | 0.356 | 0.364 | -0.72 | 38.5% | -0.02764 | 0.08 | 0.680 |
| BNBUSDT | 5min | Pullback tendência | 251 | 3.439 | 0.117 | -6.13 | 11.9% | -0.02443 | 0.62 | 6.358 |
| BNBUSDT | 5min | Reversão com regime | 106 | 1.452 | 0.093 | -2.74 | 18.9% | -0.02582 | 0.28 | 2.698 |
| BNBUSDT | 15min | Pullback tendência | 88 | 1.206 | 0.354 | -2.03 | 14.8% | -0.02312 | 0.21 | 2.247 |
| BNBUSDT | 15min | Reversão com regime | 31 | 0.425 | 0.180 | -0.99 | 29.0% | -0.03200 | 0.10 | 0.785 |


### Referência 4h, mesmo histórico e custos

| Ativo | TF | Estratégia | Trades | Trades/dia | PF líquido | PnL USDT | Win rate | Exp./trade USDT | DD % | Custos USDT |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 4h | Sma200RegimeGated v1 | 9 | 0.124 | 0.804 | -0.15 | 22.2% | -0.01649 | 0.07 | 0.226 |
| ETHUSDT | 4h | Sma200RegimeGated v1 | 8 | 0.110 | 0.310 | -1.13 | 37.5% | -0.14071 | 0.15 | 0.207 |
| SOLUSDT | 4h | Sma200RegimeGated v1 | 5 | 0.069 | 0.500 | -0.30 | 20.0% | -0.06039 | 0.14 | 0.130 |
| BNBUSDT | 4h | Sma200RegimeGated v1 | 13 | 0.178 | 0.732 | -0.23 | 23.1% | -0.01779 | 0.10 | 0.326 |


## Robustez OOS e decisão

O critério congelado exige DEV/validação/OOS positivos, pelo menos 30 trades OOS, PF OOS ≥ 1,20, lucro no estresse, frequência maior que a referência do mesmo ativo e limite inferior de expectativa positivo no bootstrap. Nenhuma candidata passou. Os números não devem ser apresentados como prova formal de edge.

Bootstrap: 2.000 reamostragens circulares em blocos de cinco trades consecutivos, seed fixa; percentis ajustados conservadoramente por 16 comparações (0,025/16 e 1−0,025/16). O tamanho de bloco não foi otimizado. Trata dependência local apenas aproximadamente, não substitui validação prospectiva. Os percentis extremos têm baixa resolução nessa quantidade de reamostragens. Retirada dos três melhores trades e contagem de meses OOS positivos são diagnósticos, sem seleção de parâmetros.

| Ativo | TF | Estratégia | Meses positivos/observados | PF estresse | PnL estresse | PF sem 3 melhores | IC expectativa ajustado USDT | Frequência / 4h | Decisão |

|---|---|---|---:|---:|---:|---:|---|---:|---|

| BTCUSDT | 5min | Pullback tendência | 0/3 | 0.034 | -11.50 | 0.059 | [-0.03026; -0.02422] | 32.8× | Rejeitada |

| BTCUSDT | 5min | Reversão com regime | 0/3 | 0.059 | -3.30 | 0.078 | [-0.03158; -0.01132] | 10.8× | Rejeitada |

| BTCUSDT | 15min | Pullback tendência | 0/3 | 0.197 | -4.43 | 0.231 | [-0.03740; -0.01307] | 13.7× | Rejeitada |

| BTCUSDT | 15min | Reversão com regime | 1/3 | 0.332 | -0.48 | 0.196 | [-0.03183; 0.01101] | 2.6× | Rejeitada |

| ETHUSDT | 5min | Pullback tendência | 0/3 | 0.089 | -10.66 | 0.106 | [-0.03213; -0.02205] | 33.9× | Rejeitada |

| ETHUSDT | 5min | Reversão com regime | 0/3 | 0.117 | -3.15 | 0.133 | [-0.03467; -0.00881] | 11.3× | Rejeitada |

| ETHUSDT | 15min | Pullback tendência | 0/3 | 0.199 | -4.08 | 0.149 | [-0.04332; -0.01246] | 12.2× | Rejeitada |

| ETHUSDT | 15min | Reversão com regime | 1/3 | 0.276 | -1.07 | 0.282 | [-0.06344; 0.01509] | 4.1× | Rejeitada |

| SOLUSDT | 5min | Pullback tendência | 0/3 | 0.075 | -11.00 | 0.106 | [-0.03378; -0.02379] | 53.5× | Rejeitada |

| SOLUSDT | 5min | Reversão com regime | 0/3 | 0.124 | -4.02 | 0.209 | [-0.03223; -0.01011] | 23.9× | Rejeitada |

| SOLUSDT | 15min | Pullback tendência | 0/3 | 0.275 | -3.73 | 0.254 | [-0.04390; -0.00378] | 20.0× | Rejeitada |

| SOLUSDT | 15min | Reversão com regime | 0/3 | 0.222 | -1.03 | 0.186 | [-0.07522; 0.00658] | 5.2× | Rejeitada |

| BNBUSDT | 5min | Pullback tendência | 0/3 | 0.055 | -9.04 | 0.077 | [-0.02814; -0.02050] | 19.3× | Rejeitada |

| BNBUSDT | 5min | Reversão com regime | 0/3 | 0.027 | -3.98 | 0.058 | [-0.03569; -0.01631] | 8.1× | Rejeitada |

| BNBUSDT | 15min | Pullback tendência | 0/3 | 0.237 | -3.07 | 0.215 | [-0.03451; -0.01135] | 6.8× | Rejeitada |

| BNBUSDT | 15min | Reversão com regime | 0/3 | 0.080 | -1.35 | 0.093 | [-0.05154; -0.01524] | 2.4× | Rejeitada |


## Desenvolvimento e validação — resultados completos

Os mesmos custos base e as mesmas métricas foram calculados em todos os splits; não houve interrupção antecipada após resultados ruins. Valores completos e precisão adicional estão nos arquivos CSV/JSON.


### DEV

| Ativo | TF | Estratégia | Trades | Trades/dia | PF líquido | PnL USDT | Win rate | Exp./trade USDT | DD % | Custos USDT |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 5min | Pullback tendência | 860 | 3.941 | 0.101 | -21.67 | 10.3% | -0.02520 | 2.17 | 21.140 |
| BTCUSDT | 5min | Reversão com regime | 277 | 1.269 | 0.178 | -5.79 | 22.7% | -0.02089 | 0.58 | 6.885 |
| BTCUSDT | 15min | Pullback tendência | 272 | 1.255 | 0.238 | -6.68 | 11.4% | -0.02456 | 0.67 | 6.734 |
| BTCUSDT | 15min | Reversão com regime | 71 | 0.328 | 0.251 | -2.33 | 29.6% | -0.03281 | 0.24 | 1.768 |
| BTCUSDT | 4h | Sma200RegimeGated v1 | 11 | 0.060 | 1.602 | 0.56 | 36.4% | 0.05064 | 0.10 | 0.271 |
| ETHUSDT | 5min | Pullback tendência | 940 | 4.307 | 0.224 | -24.53 | 11.9% | -0.02609 | 2.45 | 23.950 |
| ETHUSDT | 5min | Reversão com regime | 265 | 1.214 | 0.365 | -5.72 | 34.0% | -0.02159 | 0.57 | 6.829 |
| ETHUSDT | 15min | Pullback tendência | 356 | 1.643 | 0.435 | -8.73 | 17.4% | -0.02453 | 0.91 | 9.148 |
| ETHUSDT | 15min | Reversão com regime | 59 | 0.272 | 0.325 | -2.17 | 33.9% | -0.03675 | 0.23 | 1.522 |
| ETHUSDT | 4h | Sma200RegimeGated v1 | 11 | 0.060 | 4.242 | 4.05 | 54.5% | 0.36813 | 0.22 | 0.288 |
| SOLUSDT | 5min | Pullback tendência | 833 | 3.817 | 0.291 | -21.37 | 14.6% | -0.02565 | 2.14 | 21.454 |
| SOLUSDT | 5min | Reversão com regime | 315 | 1.443 | 0.417 | -7.05 | 40.9% | -0.02239 | 0.71 | 8.172 |
| SOLUSDT | 15min | Pullback tendência | 315 | 1.454 | 0.418 | -8.78 | 16.2% | -0.02788 | 0.89 | 8.164 |
| SOLUSDT | 15min | Reversão com regime | 72 | 0.332 | 0.360 | -2.84 | 34.7% | -0.03951 | 0.30 | 1.871 |
| SOLUSDT | 4h | Sma200RegimeGated v1 | 13 | 0.071 | 1.970 | 1.85 | 53.8% | 0.14237 | 0.26 | 0.341 |
| BNBUSDT | 5min | Pullback tendência | 938 | 4.298 | 0.164 | -23.48 | 10.9% | -0.02503 | 2.35 | 23.296 |
| BNBUSDT | 5min | Reversão com regime | 306 | 1.402 | 0.159 | -8.73 | 25.2% | -0.02854 | 0.87 | 7.658 |
| BNBUSDT | 15min | Pullback tendência | 322 | 1.486 | 0.362 | -7.59 | 17.1% | -0.02358 | 0.76 | 8.066 |
| BNBUSDT | 15min | Reversão com regime | 89 | 0.411 | 0.271 | -3.59 | 31.5% | -0.04039 | 0.36 | 2.230 |
| BNBUSDT | 4h | Sma200RegimeGated v1 | 15 | 0.082 | 2.799 | 4.03 | 40.0% | 0.26861 | 0.35 | 0.381 |


### Validação

| Ativo | TF | Estratégia | Trades | Trades/dia | PF líquido | PnL USDT | Win rate | Exp./trade USDT | DD % | Custos USDT |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| BTCUSDT | 5min | Pullback tendência | 230 | 3.151 | 0.334 | -4.59 | 15.7% | -0.01998 | 0.46 | 5.803 |
| BTCUSDT | 5min | Reversão com regime | 110 | 1.507 | 0.340 | -2.03 | 30.9% | -0.01844 | 0.20 | 2.784 |
| BTCUSDT | 15min | Pullback tendência | 79 | 1.082 | 0.205 | -2.54 | 11.4% | -0.03220 | 0.26 | 1.996 |
| BTCUSDT | 15min | Reversão com regime | 32 | 0.438 | 0.478 | -0.75 | 31.2% | -0.02347 | 0.08 | 0.805 |
| BTCUSDT | 4h | Sma200RegimeGated v1 | 4 | 0.055 | 0.000 | -1.02 | 0.0% | -0.25610 | 0.11 | 0.097 |
| ETHUSDT | 5min | Pullback tendência | 240 | 3.288 | 0.365 | -5.11 | 16.7% | -0.02130 | 0.52 | 6.217 |
| ETHUSDT | 5min | Reversão com regime | 92 | 1.260 | 0.242 | -2.86 | 31.5% | -0.03103 | 0.30 | 2.382 |
| ETHUSDT | 15min | Pullback tendência | 83 | 1.137 | 0.388 | -2.36 | 16.9% | -0.02847 | 0.26 | 2.156 |
| ETHUSDT | 15min | Reversão com regime | 23 | 0.315 | 0.357 | -0.71 | 26.1% | -0.03080 | 0.08 | 0.596 |
| ETHUSDT | 4h | Sma200RegimeGated v1 | 4 | 0.055 | 0.060 | -1.07 | 25.0% | -0.26855 | 0.16 | 0.102 |
| SOLUSDT | 5min | Pullback tendência | 234 | 3.206 | 0.361 | -5.38 | 16.7% | -0.02299 | 0.54 | 6.101 |
| SOLUSDT | 5min | Reversão com regime | 108 | 1.480 | 0.481 | -2.06 | 39.8% | -0.01910 | 0.22 | 2.819 |
| SOLUSDT | 15min | Pullback tendência | 64 | 0.877 | 0.530 | -1.40 | 20.3% | -0.02181 | 0.15 | 1.672 |
| SOLUSDT | 15min | Reversão com regime | 28 | 0.384 | 0.148 | -1.76 | 32.1% | -0.06268 | 0.18 | 0.730 |
| SOLUSDT | 4h | Sma200RegimeGated v1 | 5 | 0.069 | 0.528 | -0.30 | 40.0% | -0.06017 | 0.10 | 0.130 |
| BNBUSDT | 5min | Pullback tendência | 226 | 3.096 | 0.236 | -5.01 | 15.0% | -0.02218 | 0.50 | 5.747 |
| BNBUSDT | 5min | Reversão com regime | 99 | 1.356 | 0.283 | -2.30 | 30.3% | -0.02326 | 0.24 | 2.518 |
| BNBUSDT | 15min | Pullback tendência | 76 | 1.041 | 0.226 | -2.28 | 14.5% | -0.03004 | 0.24 | 1.931 |
| BNBUSDT | 15min | Reversão com regime | 32 | 0.438 | 0.265 | -1.08 | 34.4% | -0.03371 | 0.11 | 0.809 |
| BNBUSDT | 4h | Sma200RegimeGated v1 | 9 | 0.124 | 0.029 | -1.50 | 11.1% | -0.16664 | 0.16 | 0.227 |


## Limitações e próxima fase

Não foi encontrada candidata convincente para PAPER. Não iniciar PAPER contínuo com estas regras. A maior frequência resultou em aumento do atrito; nenhuma regra foi promovida apenas por operar mais. A simulação da referência também pode falhar nesse período e não constitui motivo para alterar o LIVE.

Não foram avaliados 1m/3m ou candles de segundos: a matriz prioritária não demonstrou edge líquido que justifique ampliar volume de dados e exigência de execução. Uma pesquisa futura deveria formular previamente outra hipótese de sinal e sua margem bruta sobre custos, validar fontes/holdouts ainda não explorados e considerar a viabilidade de lotes no capital disponível, sem mudar parâmetros de produção. Se surgir evidência consistente, a próxima etapa seria validação PAPER isolada, com recursos limitados e executor incapaz de negociar em MAINNET; nenhum processo contínuo foi iniciado aqui.

Limitações adicionais: um ano de histórico e aproximadamente 73 dias OOS; validação única cronológica em vez de múltiplos ciclos walk-forward; preço/spread/slippage modelados e não medidos por trade; ausência de histórico de filtros e tarifas da conta; ausência de latência, book, fills parciais e falhas de rede; drawdown intrabar não capturado; tamanho monetário pequeno das posições e capital hipotético; sem comprovação independente da origem/integridade econômica de cada candle; múltiplas pesquisas anteriores podem ter consumido o mesmo OOS. Não há promessa de lucro futuro.


## Isolamento e evidência operacional

Escritas restritas por audit hook Python a `research/isolated_spot_20261017`; tentativas de ler `.env` bloqueadas; ambiente do processo sem segredos herdados; credenciais vazias; rede bloqueada por audit hook antes dos imports de backtest; subprocessos proibidos. Logging do processo é apenas nulo/padrão sem inicialização dos handlers compartilhados; diagnóstico do engine redirecionado no processo ao diretório exclusivo. Nenhum registro em banco, estado LIVE, registry, scheduler ou catálogo foi gravado. Não houve compra, venda, cancelamento, reinício ou envio de mensagem externa.

Pesquisa sequencial em um único núcleo lógico, prioridade Windows Idle e bibliotecas numéricas com uma thread; limite verificado entre etapas de 900 MiB RSS e piso de 2 GiB de memória livre. Não é limite rígido do sistema operacional nem monitoramento em cada candle; RSS observado durante os backtests ficou próximo de 200 MiB. Os demais serviços compartilham CPU, RAM e disco, mas não houve conexão ao MySQL. PIDs LIVE/daemon verificados entre etapas; API, dashboard e MySQL permaneceram intocados.

A confirmação final dos processos, timestamps de início, comandos, avanço de ciclos do daemon e mensagens LIVE está em `operational_final.json`. A presença dos mesmos PIDs e atividade dos logs sustenta continuidade operacional, mas não é auditoria autenticada da exchange. A pesquisa não emitiu nem alterou ordens reais; não se afirma que a estratégia LIVE deixou de operar por conta própria durante a janela.


## Fontes externas consultadas

- [Binance Spot API — filtros](https://developers.binance.com/docs/binance-spot-api-docs/filters): LOT_SIZE, MARKET_LOT_SIZE e NOTIONAL; valores concretos usados constam em `exchange_info.json`.

- [Binance Academy — backtesting](https://www.binance.com/en/academy/articles/how-to-backtest-a-trading-strategy): contexto de validação histórica; nenhuma rentabilidade externa foi incorporada aos resultados.

- As especificações numéricas das candidatas são hipóteses desta pesquisa, não reprodução integral de um paper ou recomendação da Binance.


## Artefatos

`run.py`: protocolo executável isolado; `protocol.json`: parâmetros congelados; `data_manifest.json`: fonte/hash/qualidade; `exchange_info.json`: filtros públicos; `results.csv`/`results.json`: 80 execuções; `trades.csv`: operações simuladas; `robustness.json`: decisões; `verify.py`/`verification.json`: verificações; `protected_source_hashes.json`: hashes de módulos preservados; `operational_final.json`: evidência operacional final.