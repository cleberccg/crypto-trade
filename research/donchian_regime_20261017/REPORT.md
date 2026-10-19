# Donchian: pesquisa isolada de regime e custos

Data de referência: 17/10/2026, America/Sao_Paulo. Conclusao: **nenhuma candidata aprovada para PAPER**. Nenhum executor contínuo foi iniciado.

## Diagnostico anterior

A pesquisa isolated_spot_20261017 rejeitou 16 combinações de pullback/reversão. Maior frequência, até cerca de quatro trades/dia, acumulou atrito de 0,26% por round trip sem vantagem bruta suficiente. Stops remotos de 99% naquela pesquisa geravam tickets proximos de 10 USDT com capital de 1.000 USDT e inviabilizavam 28 USDT. Aqui o Donchian real usa stop padrão de 2%, alvo de 4%, score 1 e trailing definido pelo motor; portanto a viabilidade de capital deve ser recalculada. Não se compara diretamente o PnL monetário entre regras com exposicoes diferentes.

## Inspecao e hipóteses congeladas

Reutilizados ClassicDonchianBreakoutStrategy v1 (única versão localizada no catálogo), BacktestEngine/BacktestConfig, RiskManager/PositionSizer, métricas existentes, ATR14 Wilder, compute_adx, adaptador de LOT_SIZE e snapshot exchangeInfo da pesquisa anterior. Não foi chamado serviço científico que persiste em banco. So novos arquivos nesta pasta foram escritos. Nenhuma formula existente foi alterada.

A: classe original, janela Donchian 20, entrada close acima do máximo dos 20 candles anteriores. Stop/alvo calculados pela classe real no candle de sinal; saída normal HOLD, gerenciada pelo motor. B: mesma estratégia, com close>SMA200, SMA200 crescente em 20 barras, ADX14é20 e ATR14/close<=3%. C: B mais ATR14/close>=0,52% (duas vezes o custo nominal de ida e volta). ATR é escala de movimento, não previsao de lucro. O piso testa margem de volatilidade sobre custos; o teto evita volatilidade extrema. Valores fixados em protocol.json antes dos testes, sem busca de parâmetros. O piso permanece 0,52% no estresse para isolar o impacto dos custos. Não se testou 5m por falta de justificativa para ampliar a família.

## Dados e desenho causal

Fonte local data/binance_spot_extended_klines.csv.gz; hash e integridade em data_manifest.json. Cada ativo contém 105.120 candles de 5m entre 01/06/2025 e 01/06/2026 exclusivo; agregacao completa produz 35.040 candles de 15m e 8.760 de 1h. Sem interpolação, duplicatas ou lacunas na janela. O período após 01/06/2026 permanece reservado e não entra nos testes.

Separacao cronológica 60/20/20: DEV, validação e OOS (~73 dias, marco-maio de 2026). Capital reiniciado em cada split. Primeiras 220 barras são aquecimento; validação/OOS usam somente passado para aquecer, sem transportar posições. Sinais e níveis do candle fechado são deslocados uma barra; execução na abertura seguinte. Prefixos foram comparados para verificar causalidade. Liquidacao final programada no último open de cada split, com custo.

OOS é fora da amostra deste protocolo, mas o mesmo período já foi consultado por pesquisas anteriores. Portanto não constitui holdout inedito e a correcao estatística desta família não cobre todo o histórico de seleção do projeto. Não há treino/otimização dos parâmetros nesta execução.

## Execução e custos

Capital 1.000 USDT por combinação independente. Padroes do código: risco de 1%, teto de stake de 2% do saldo (cerca de 20 USDT), trailing de 1,5% aplicado pelo motor. Nenhum valor foi lido do .env LIVE. Taxa 0,10%, meio spread 0,01% e slippage 0,02% por lado: 0,13% por lado; estresse aumenta slippage para 0,08%, total 0,19%. Motor debita custos proporcionais, sem simular book. results.csv separa taxa/spread/slippage. PF bruto usa PnL antes desses debitos nos mesmos trades; não é um segundo backtest sem custos. PF líquido usa trades após custos. DD é fração do capital, conforme métricas existentes; multiplicar por 100 para percentual. Win rate também é fração.

Quantidades arredondadas para baixo em Decimal pelo stepSize, com minQty/maxQty e NOTIONAL verificados após o dimensionamento original. O snapshot é atual, não histórico; preço médio móvel de MIN_NOTIONAL, limites dinamicos e fills parciais não são reproduzidos. Entradas/saídas são proxy de mercado: PRICE_FILTER de ordens limit/stop não é simulado. Stops/alvos são eventos internos, não ordens enviadas. Precos historicos mantém sua precisão; quantidade respeita o lote.

## Resultados completos é capital 1.000 USDT

### DEV é custos base

| symbol | timeframe | strategy | total_trades | trades_per_day | mean_hours_between_entries | gross_profit_factor | profit_factor | net_profit | win_rate | expectancy_unrounded | max_drawdown_pct | total_cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | 151 | 0.69682 | 34.352 | 0.9765 | 0.6862 | -8.09 | 0.3179 | -0.053579 | 0.011 | 7.603 |
| BTCUSDT | 15min | B | 69 | 0.31842 | 76.026 | 1.1276 | 0.8168 | -2.23 | 0.3188 | -0.032328 | 0.0052 | 3.4837 |
| BTCUSDT | 15min | C | 5 | 0.023074 | 1019.4 | 0.19942 | 0.1019 | -0.85 | 0.2 | -0.17086 | 0.0013 | 0.25148 |
| BTCUSDT | 1h | A | 73 | 0.34796 | 68.347 | 0.92843 | 0.6635 | -4.45 | 0.2877 | -0.060938 | 0.0071 | 3.6825 |
| BTCUSDT | 1h | B | 27 | 0.1287 | 186.85 | 1.4803 | 1.1004 | 0.45 | 0.4444 | 0.016682 | 0.002 | 1.3642 |
| BTCUSDT | 1h | C | 13 | 0.061966 | 357.67 | 0.86985 | 0.679 | -1.01 | 0.3077 | -0.077588 | 0.0026 | 0.65857 |
| ETHUSDT | 15min | A | 250 | 1.1537 | 20.806 | 1.1305 | 0.8028 | -8.38 | 0.348 | -0.033501 | 0.0102 | 12.856 |
| ETHUSDT | 15min | B | 120 | 0.55377 | 43.483 | 1.1641 | 0.8488 | -3.3 | 0.3083 | -0.027514 | 0.0071 | 6.1888 |
| ETHUSDT | 15min | C | 58 | 0.26765 | 88.32 | 0.84421 | 0.6227 | -4.5 | 0.2586 | -0.077641 | 0.0048 | 2.9796 |
| ETHUSDT | 1h | A | 106 | 0.50526 | 47.857 | 1.2765 | 0.9601 | -0.81 | 0.3585 | -0.0076435 | 0.0055 | 5.4748 |
| ETHUSDT | 1h | B | 44 | 0.20973 | 116.86 | 1.541 | 1.1722 | 1.41 | 0.3864 | 0.032119 | 0.0016 | 2.2766 |
| ETHUSDT | 1h | C | 43 | 0.20497 | 119.64 | 1.4978 | 1.1414 | 1.15 | 0.3953 | 0.026666 | 0.0016 | 2.2239 |
| SOLUSDT | 15min | A | 294 | 1.3567 | 17.712 | 1.1181 | 0.785 | -10.53 | 0.3231 | -0.035804 | 0.0127 | 15.152 |
| SOLUSDT | 15min | B | 127 | 0.58607 | 40.222 | 0.98293 | 0.7129 | -6.88 | 0.3071 | -0.054151 | 0.0089 | 6.5429 |
| SOLUSDT | 15min | C | 84 | 0.38764 | 60.681 | 0.78932 | 0.568 | -7.21 | 0.2738 | -0.085883 | 0.0073 | 4.328 |
| SOLUSDT | 1h | A | 120 | 0.572 | 42.16 | 1.0621 | 0.7857 | -4.99 | 0.3833 | -0.041606 | 0.006 | 6.2027 |
| SOLUSDT | 1h | B | 47 | 0.22403 | 108.09 | 1.1921 | 0.8968 | -0.96 | 0.3404 | -0.020332 | 0.0029 | 2.4328 |
| SOLUSDT | 1h | C | 47 | 0.22403 | 108.09 | 1.1921 | 0.8968 | -0.96 | 0.3404 | -0.020332 | 0.0029 | 2.4328 |
| BNBUSDT | 15min | A | 202 | 0.93217 | 25.811 | 1.3499 | 0.9651 | -1.13 | 0.3663 | -0.0055991 | 0.009 | 10.301 |
| BNBUSDT | 15min | B | 91 | 0.41994 | 57.208 | 1.6114 | 1.1273 | 1.71 | 0.3407 | 0.018738 | 0.0038 | 4.6471 |
| BNBUSDT | 15min | C | 29 | 0.13383 | 124.79 | 0.57282 | 0.3758 | -3.14 | 0.2069 | -0.10814 | 0.0031 | 1.4638 |
| BNBUSDT | 1h | A | 92 | 0.43853 | 54.637 | 1.5574 | 1.1558 | 2.44 | 0.3587 | 0.026528 | 0.0048 | 4.6967 |
| BNBUSDT | 1h | B | 52 | 0.24786 | 97.804 | 1.2703 | 0.9521 | -0.47 | 0.3269 | -0.0090366 | 0.003 | 2.6453 |
| BNBUSDT | 1h | C | 44 | 0.20973 | 99.209 | 1.1317 | 0.8541 | -1.28 | 0.3182 | -0.029045 | 0.003 | 2.2344 |

### VALIDATION é custos base

| symbol | timeframe | strategy | total_trades | trades_per_day | mean_hours_between_entries | gross_profit_factor | profit_factor | net_profit | win_rate | expectancy_unrounded | max_drawdown_pct | total_cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | 77 | 1.0549 | 22.829 | 0.55074 | 0.3974 | -9.93 | 0.2078 | -0.12902 | 0.01 | 3.8961 |
| BTCUSDT | 15min | B | 29 | 0.39732 | 59.42 | 0.88291 | 0.6008 | -1.92 | 0.3103 | -0.066347 | 0.0029 | 1.4788 |
| BTCUSDT | 15min | C | 12 | 0.16441 | 77.114 | 0.2861 | 0.1516 | -2.04 | 0.3333 | -0.16969 | 0.0024 | 0.61345 |
| BTCUSDT | 1h | A | 26 | 0.35637 | 61.92 | 1.1576 | 0.8731 | -0.65 | 0.3846 | -0.024907 | 0.0026 | 1.3242 |
| BTCUSDT | 1h | B | 8 | 0.10965 | 213.29 | 1.5592 | 1.1563 | 0.2 | 0.5 | 0.025056 | 0.0011 | 0.40477 |
| BTCUSDT | 1h | C | 6 | 0.082239 | 293.4 | 2.6501 | 1.8845 | 0.55 | 0.6667 | 0.091887 | 0.0009 | 0.30866 |
| ETHUSDT | 15min | A | 95 | 1.3016 | 18.348 | 0.67531 | 0.5021 | -10.69 | 0.2526 | -0.11251 | 0.0117 | 4.8731 |
| ETHUSDT | 15min | B | 36 | 0.49322 | 47.414 | 1.1306 | 0.8102 | -1.2 | 0.3056 | -0.033271 | 0.0027 | 1.8603 |
| ETHUSDT | 15min | C | 23 | 0.31511 | 67.432 | 0.97402 | 0.7209 | -1.29 | 0.3478 | -0.056055 | 0.0023 | 1.1892 |
| ETHUSDT | 1h | A | 31 | 0.4249 | 55.3 | 1.1891 | 0.8973 | -0.63 | 0.3226 | -0.020361 | 0.0029 | 1.6029 |
| ETHUSDT | 1h | B | 12 | 0.16448 | 150.82 | 0.61355 | 0.4221 | -1.38 | 0.25 | -0.11518 | 0.0026 | 0.61897 |
| ETHUSDT | 1h | C | 12 | 0.16448 | 150.82 | 0.61355 | 0.4221 | -1.38 | 0.25 | -0.11518 | 0.0026 | 0.61897 |
| SOLUSDT | 15min | A | 100 | 1.3701 | 17.588 | 0.67617 | 0.4949 | -11.05 | 0.28 | -0.11049 | 0.0115 | 5.1447 |
| SOLUSDT | 15min | B | 32 | 0.43842 | 54.605 | 0.71663 | 0.5072 | -3.14 | 0.2812 | -0.097989 | 0.0042 | 1.6566 |
| SOLUSDT | 15min | C | 29 | 0.39732 | 60.152 | 0.45035 | 0.3153 | -4.41 | 0.2069 | -0.15196 | 0.0044 | 1.4968 |
| SOLUSDT | 1h | A | 30 | 0.41119 | 56.552 | 1.9203 | 1.479 | 2.58 | 0.4 | 0.08609 | 0.0019 | 1.5619 |
| SOLUSDT | 1h | B | 12 | 0.16448 | 151 | 0.72223 | 0.5372 | -1.23 | 0.25 | -0.1026 | 0.0018 | 0.62061 |
| SOLUSDT | 1h | C | 12 | 0.16448 | 151 | 0.72223 | 0.5372 | -1.23 | 0.25 | -0.1026 | 0.0018 | 0.62061 |
| BNBUSDT | 15min | A | 74 | 1.0138 | 23.832 | 0.64352 | 0.4751 | -8.5 | 0.2162 | -0.11481 | 0.0096 | 3.7517 |
| BNBUSDT | 15min | B | 24 | 0.32881 | 69.457 | 1.1349 | 0.8135 | -0.78 | 0.2917 | -0.032442 | 0.0025 | 1.2256 |
| BNBUSDT | 15min | C | 4 | 0.054802 | 152 | 1.3635 | 1.0072 | 0.01 | 0.25 | 0.0013078 | 0.0009 | 0.20385 |
| BNBUSDT | 1h | A | 24 | 0.32895 | 70.435 | 1.0729 | 0.7685 | -0.97 | 0.5 | -0.04024 | 0.0032 | 1.2255 |
| BNBUSDT | 1h | B | 11 | 0.15077 | 156.3 | 0.58545 | 0.4261 | -1.49 | 0.3636 | -0.13507 | 0.0028 | 0.55894 |
| BNBUSDT | 1h | C | 6 | 0.082239 | 293.6 | 0.13016 | 0.0679 | -1.27 | 0.1667 | -0.21134 | 0.0015 | 0.30678 |

### OOS é custos base

| symbol | timeframe | strategy | total_trades | trades_per_day | mean_hours_between_entries | gross_profit_factor | profit_factor | net_profit | win_rate | expectancy_unrounded | max_drawdown_pct | total_cost |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | 55 | 0.75353 | 31.338 | 0.76825 | 0.5368 | -4.67 | 0.2727 | -0.084918 | 0.0049 | 2.7907 |
| BTCUSDT | 15min | B | 32 | 0.43842 | 54.194 | 0.5488 | 0.3808 | -3.96 | 0.25 | -0.12361 | 0.004 | 1.6191 |
| BTCUSDT | 15min | C | 3 | 0.041102 | 203 | 1.2065 | 0.6586 | -0.1 | 0.6667 | -0.034015 | 0.0007 | 0.15337 |
| BTCUSDT | 1h | A | 26 | 0.35637 | 63.8 | 0.82325 | 0.5909 | -2.01 | 0.2308 | -0.077176 | 0.0022 | 1.3174 |
| BTCUSDT | 1h | B | 13 | 0.17818 | 69.417 | 0.55716 | 0.3626 | -1.43 | 0.2308 | -0.10977 | 0.002 | 0.65915 |
| BTCUSDT | 1h | C | 11 | 0.15077 | 70 | 0.39721 | 0.2952 | -1.74 | 0.0909 | -0.15847 | 0.0018 | 0.55655 |
| ETHUSDT | 15min | A | 64 | 0.87684 | 27.75 | 0.74662 | 0.5382 | -5.96 | 0.25 | -0.093183 | 0.006 | 3.2964 |
| ETHUSDT | 15min | B | 27 | 0.36992 | 61.538 | 0.74345 | 0.4706 | -2.27 | 0.2963 | -0.084016 | 0.0029 | 1.3958 |
| ETHUSDT | 15min | C | 7 | 0.095904 | 73.708 | 1.536 | 1.0165 | 0.01 | 0.4286 | 0.0019805 | 0.0015 | 0.36255 |
| ETHUSDT | 1h | A | 29 | 0.39749 | 58.821 | 0.55983 | 0.4047 | -3.8 | 0.2069 | -0.13096 | 0.0042 | 1.4943 |
| ETHUSDT | 1h | B | 13 | 0.17818 | 70 | 0.6016 | 0.4674 | -1.85 | 0.2308 | -0.14227 | 0.0028 | 0.67029 |
| ETHUSDT | 1h | C | 12 | 0.16448 | 76.364 | 0.66868 | 0.5195 | -1.5 | 0.25 | -0.12511 | 0.0025 | 0.61905 |
| SOLUSDT | 15min | A | 66 | 0.90424 | 26.915 | 0.88032 | 0.5984 | -4.48 | 0.2879 | -0.067932 | 0.0046 | 3.4148 |
| SOLUSDT | 15min | B | 27 | 0.36992 | 54.26 | 1.1679 | 0.7539 | -0.93 | 0.2963 | -0.034289 | 0.0018 | 1.4003 |
| SOLUSDT | 15min | C | 10 | 0.13701 | 156.97 | 0.87957 | 0.5876 | -0.67 | 0.3 | -0.067062 | 0.0011 | 0.51831 |
| SOLUSDT | 1h | A | 32 | 0.43861 | 50.71 | 0.83259 | 0.6141 | -2.57 | 0.25 | -0.08034 | 0.0027 | 1.6572 |
| SOLUSDT | 1h | B | 10 | 0.13706 | 87.667 | 1.1731 | 0.9172 | -0.19 | 0.4 | -0.018519 | 0.0011 | 0.51899 |
| SOLUSDT | 1h | C | 10 | 0.13706 | 87.667 | 1.1731 | 0.9172 | -0.19 | 0.4 | -0.018519 | 0.0011 | 0.51899 |
| BNBUSDT | 15min | A | 52 | 0.71243 | 34.074 | 1.062 | 0.7447 | -2.22 | 0.3654 | -0.042747 | 0.0036 | 2.6615 |
| BNBUSDT | 15min | B | 33 | 0.45212 | 51.047 | 1.0355 | 0.7279 | -1.53 | 0.3636 | -0.046302 | 0.0024 | 1.6913 |
| BNBUSDT | 15min | C | 2 | 0.027401 | 9.5 | 0.26502 | 0 | -0.19 | 0 | -0.096075 | 0.0007 | 0.10285 |
| BNBUSDT | 1h | A | 30 | 0.41119 | 59.379 | 1.2819 | 0.9447 | -0.3 | 0.3 | -0.0099916 | 0.0022 | 1.5371 |
| BNBUSDT | 1h | B | 10 | 0.13706 | 125.33 | 2.3703 | 1.6644 | 0.81 | 0.5 | 0.080761 | 0.0009 | 0.51169 |
| BNBUSDT | 1h | C | 9 | 0.12336 | 141 | 1.4688 | 1.0672 | 0.1 | 0.3333 | 0.011237 | 0.0016 | 0.46115 |

### OOS é estresse de custos

| symbol | timeframe | strategy | total_trades | profit_factor | net_profit | expectancy_unrounded | total_cost |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | 55 | 0.4602 | -5.95 | -0.10818 | 4.0726 |
| BTCUSDT | 15min | B | 32 | 0.324 | -4.7 | -0.14696 | 2.3663 |
| BTCUSDT | 15min | C | 3 | 0.4635 | -0.17 | -0.057611 | 0.22416 |
| BTCUSDT | 1h | A | 26 | 0.5132 | -2.61 | -0.10056 | 1.9254 |
| BTCUSDT | 1h | B | 13 | 0.3052 | -1.73 | -0.13317 | 0.96337 |
| BTCUSDT | 1h | C | 11 | 0.2609 | -2 | -0.18182 | 0.81342 |
| ETHUSDT | 15min | A | 64 | 0.4673 | -7.48 | -0.11691 | 4.817 |
| ETHUSDT | 15min | B | 27 | 0.3855 | -2.91 | -0.10767 | 2.0391 |
| ETHUSDT | 15min | C | 7 | 0.8361 | -0.15 | -0.021924 | 0.52988 |
| ETHUSDT | 1h | A | 29 | 0.3522 | -4.48 | -0.1546 | 2.1831 |
| ETHUSDT | 1h | B | 13 | 0.418 | -2.16 | -0.16607 | 0.97966 |
| ETHUSDT | 1h | C | 12 | 0.4646 | -1.79 | -0.14892 | 0.90476 |
| SOLUSDT | 15min | A | 66 | 0.5068 | -6.05 | -0.091731 | 4.987 |
| SOLUSDT | 15min | B | 27 | 0.6286 | -1.57 | -0.05819 | 2.046 |
| SOLUSDT | 15min | C | 10 | 0.4931 | -0.91 | -0.09086 | 0.75722 |
| SOLUSDT | 1h | A | 32 | 0.5402 | -3.33 | -0.10419 | 2.4207 |
| SOLUSDT | 1h | B | 10 | 0.8212 | -0.43 | -0.042538 | 0.75819 |
| SOLUSDT | 1h | C | 10 | 0.8212 | -0.43 | -0.042538 | 0.75819 |
| BNBUSDT | 15min | A | 52 | 0.6376 | -3.43 | -0.065932 | 3.885 |
| BNBUSDT | 15min | B | 33 | 0.6181 | -2.33 | -0.07071 | 2.4693 |
| BNBUSDT | 15min | C | 2 | 0 | -0.24 | -0.11981 | 0.15032 |
| BNBUSDT | 1h | A | 30 | 0.8294 | -1.01 | -0.033639 | 2.2465 |
| BNBUSDT | 1h | B | 10 | 1.4289 | 0.57 | 0.057144 | 0.74785 |
| BNBUSDT | 1h | C | 9 | 0.9321 | -0.11 | -0.012412 | 0.67398 |

## Regimes e falsos rompimentos

Falha rápida: fechamento retorna ao canal anterior nas três barras seguintes ao rompimento. é um diagnóstico ex post; não alimenta sinais. Não equivale necessariamente a trade perdedor. Agrupamento abaixo usa o regime observado na decisão de entrada, não informação futura. Em B/C a entrada já exige regime de tendência; não se pode inferir desempenho desses filtros em outros regimes sem trades.

| symbol | timeframe | strategy | regime | trades | pnl | mean_pnl | costs | quick_fail_rate | holding_hours |
|---|---|---|---|---|---|---|---|---|---|
| BNBUSDT | 15min | A | other | 39 | -1.8116 | -0.046452 | 1.9941 | 0.4359 | 28.103 |
| BNBUSDT | 15min | A | trend | 13 | -0.41122 | -0.031632 | 0.66745 | 0.15385 | 8.6923 |
| BNBUSDT | 15min | B | trend | 33 | -1.528 | -0.046302 | 1.6913 | 0.42424 | 16 |
| BNBUSDT | 15min | C | trend | 2 | -0.19215 | -0.096075 | 0.10285 | 0 | 5.25 |
| BNBUSDT | 1h | A | other | 24 | -0.47059 | -0.019608 | 1.2274 | 0.41667 | 26.5 |
| BNBUSDT | 1h | A | trend | 6 | 0.17084 | 0.028473 | 0.30967 | 0.33333 | 17.167 |
| BNBUSDT | 1h | B | trend | 10 | 0.80761 | 0.080761 | 0.51169 | 0.6 | 20.7 |
| BNBUSDT | 1h | C | trend | 9 | 0.10113 | 0.011237 | 0.46115 | 0.66667 | 16.222 |
| BTCUSDT | 15min | A | other | 37 | -0.97612 | -0.026382 | 1.8817 | 0.35135 | 26.48 |
| BTCUSDT | 15min | A | trend | 18 | -3.6944 | -0.20524 | 0.90898 | 0.55556 | 13.097 |
| BTCUSDT | 15min | B | trend | 32 | -3.9554 | -0.12361 | 1.6191 | 0.46875 | 17.547 |
| BTCUSDT | 15min | C | trend | 3 | -0.10205 | -0.034015 | 0.15337 | 0.33333 | 12.583 |
| BTCUSDT | 1h | A | other | 20 | -1.8276 | -0.091382 | 1.0119 | 0.45 | 26.35 |
| BTCUSDT | 1h | A | trend | 6 | -0.17894 | -0.029823 | 0.30547 | 0.16667 | 25.833 |
| BTCUSDT | 1h | B | trend | 13 | -1.427 | -0.10977 | 0.65915 | 0.23077 | 22.077 |
| BTCUSDT | 1h | C | trend | 11 | -1.7432 | -0.15847 | 0.55655 | 0.27273 | 20.727 |
| ETHUSDT | 15min | A | other | 50 | -3.323 | -0.066459 | 2.5771 | 0.4 | 18.23 |
| ETHUSDT | 15min | A | trend | 14 | -2.6408 | -0.18863 | 0.71926 | 0.5 | 10.054 |
| ETHUSDT | 15min | B | trend | 27 | -2.2684 | -0.084016 | 1.3958 | 0.66667 | 14.88 |
| ETHUSDT | 15min | C | trend | 7 | 0.013864 | 0.0019805 | 0.36255 | 0.28571 | 7.3214 |
| ETHUSDT | 1h | A | other | 20 | -2.493 | -0.12465 | 1.0304 | 0.3 | 19.8 |
| ETHUSDT | 1h | A | trend | 9 | -1.3049 | -0.14499 | 0.4639 | 0.33333 | 14.778 |
| ETHUSDT | 1h | B | trend | 13 | -1.8495 | -0.14227 | 0.67029 | 0.53846 | 12.462 |
| ETHUSDT | 1h | C | trend | 12 | -1.5014 | -0.12511 | 0.61905 | 0.5 | 13.25 |
| SOLUSDT | 15min | A | other | 52 | -3.462 | -0.066578 | 2.6908 | 0.51923 | 15.654 |
| SOLUSDT | 15min | A | trend | 14 | -1.0215 | -0.072963 | 0.72403 | 0.42857 | 10.929 |
| SOLUSDT | 15min | B | trend | 27 | -0.92581 | -0.034289 | 1.4003 | 0.44444 | 13.944 |
| SOLUSDT | 15min | C | trend | 10 | -0.67062 | -0.067062 | 0.51831 | 0.3 | 9.325 |
| SOLUSDT | 1h | A | other | 26 | -1.1077 | -0.042606 | 1.3478 | 0.38462 | 19.192 |
| SOLUSDT | 1h | A | trend | 6 | -1.4631 | -0.24385 | 0.30932 | 0.5 | 11.667 |
| SOLUSDT | 1h | B | trend | 10 | -0.18519 | -0.018519 | 0.51899 | 0.6 | 13.6 |
| SOLUSDT | 1h | C | trend | 10 | -0.18519 | -0.018519 | 0.51899 | 0.6 | 13.6 |

### Efeito dos filtros e consumo por custos

| symbol | timeframe | strategy | expectancy_delta_vs_A | gross_pnl | costs | net_pnl | cost_div_abs_gross | frequency_delta_vs_A |
|---|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | 0 | -1.8798 | 2.7907 | -4.67 | 1.4845 | 0 |
| BTCUSDT | 15min | B | -0.038689 | -2.3364 | 1.6191 | -3.96 | 0.69299 | -0.31511 |
| BTCUSDT | 15min | C | 0.050903 | 0.051328 | 0.15337 | -0.1 | 2.9881 | -0.71243 |
| BTCUSDT | 1h | A | 0 | -0.68917 | 1.3174 | -2.01 | 1.9116 | 0 |
| BTCUSDT | 1h | B | -0.032597 | -0.76789 | 0.65915 | -1.43 | 0.85838 | -0.17818 |
| BTCUSDT | 1h | C | -0.081293 | -1.1866 | 0.55655 | -1.74 | 0.46903 | -0.2056 |
| ETHUSDT | 15min | A | 0 | -2.6674 | 3.2964 | -5.96 | 1.2358 | 0 |
| ETHUSDT | 15min | B | 0.0091677 | -0.87267 | 1.3958 | -2.27 | 1.5994 | -0.50692 |
| ETHUSDT | 15min | C | 0.095164 | 0.37642 | 0.36255 | 0.01 | 0.96317 | -0.78093 |
| ETHUSDT | 1h | A | 0 | -2.3035 | 1.4943 | -3.8 | 0.64872 | 0 |
| ETHUSDT | 1h | B | -0.011311 | -1.1792 | 0.67029 | -1.85 | 0.56841 | -0.2193 |
| ETHUSDT | 1h | C | 0.0058484 | -0.88231 | 0.61905 | -1.5 | 0.70162 | -0.23301 |
| SOLUSDT | 15min | A | 0 | -1.0687 | 3.4148 | -4.48 | 3.1954 | 0 |
| SOLUSDT | 15min | B | 0.033643 | 0.47451 | 1.4003 | -0.93 | 2.9511 | -0.53432 |
| SOLUSDT | 15min | C | 0.00086965 | -0.15231 | 0.51831 | -0.67 | 3.4029 | -0.76723 |
| SOLUSDT | 1h | A | 0 | -0.9137 | 1.6572 | -2.57 | 1.8137 | 0 |
| SOLUSDT | 1h | B | 0.061821 | 0.3338 | 0.51899 | -0.19 | 1.5548 | -0.30154 |
| SOLUSDT | 1h | C | 0.061821 | 0.3338 | 0.51899 | -0.19 | 1.5548 | -0.30154 |
| BNBUSDT | 15min | A | 0 | 0.43868 | 2.6615 | -2.22 | 6.0671 | 0 |
| BNBUSDT | 15min | B | -0.0035555 | 0.16334 | 1.6913 | -1.53 | 10.355 | -0.26031 |
| BNBUSDT | 15min | C | -0.053328 | -0.0893 | 0.10285 | -0.19 | 1.1517 | -0.68503 |
| BNBUSDT | 1h | A | 0 | 1.2373 | 1.5371 | -0.3 | 1.2423 | 0 |
| BNBUSDT | 1h | B | 0.090753 | 1.3193 | 0.51169 | 0.81 | 0.38785 | -0.27413 |
| BNBUSDT | 1h | C | 0.021228 | 0.56228 | 0.46115 | 0.1 | 0.82014 | -0.28784 |

Diferencas de expectativa comparam o resultado médio de cada conjunto de trades, sem pareamento causal. Reduzir prejuízo total ao operar menos não prova melhorar o sinal. A razão custos/|PnL bruto| indica escala do atrito; quando o bruto já é negativo, custos apenas aprofundam a perda. Não é percentagem de lucro consumido nesse caso.

## Robustez e seleção

Critérios anteriores preservados: todos os splits positivos, OOS>=30 trades, PF>=1,20, estresse positivo, frequência maior que referência 4h e limite inferior de IC positivo. Bootstrap circular por blocos de cinco trades, 2.000 reamostragens, seed fixa; quantis Bonferroni 0,025/24 e 1é0,025/24 para 24 combinações. Para menos de cinco trades não se estima IC. Intervalos condicionais e exploratorios: dependência de longo prazo e múltiplas pesquisas não são totalmente cobertas; caudas extremas têm baixa resolução. Não são prova formal de edge. Não houve novas variantes após observar resultados.

| symbol | timeframe | strategy | ci | stress_pnl | frequency_multiple_4h | approved | reasons |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | [-0.19704742363742436, 0.02338183211525233] | -5.95 | 6.098 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BTCUSDT | 15min | B | [-0.21796088743561526, -0.007546984280740457] | -4.7 | 3.5479 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BTCUSDT | 15min | C | [None, None] | -0.17 | 0.33262 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo; frequência não supera referência 4h |
| BTCUSDT | 1h | A | [-0.2638590698563282, 0.1300173753620078] | -2.61 | 2.8839 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BTCUSDT | 1h | B | [-0.26830715560085044, 0.05523642740323019] | -1.73 | 1.442 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BTCUSDT | 1h | C | [-0.2978860570227831, 0.04788600640341102] | -2 | 1.2201 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| ETHUSDT | 15min | A | [-0.2016870477228035, 0.02278068146932414] | -7.48 | 7.9829 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| ETHUSDT | 15min | B | [-0.24548197539782654, 0.08233222130557713] | -2.91 | 3.3678 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| ETHUSDT | 15min | C | [-0.21849102149999974, 0.22245211520971372] | -0.15 | 0.87313 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo; frequência não supera referência 4h |
| ETHUSDT | 1h | A | [-0.26996625917537886, 0.05280748033691325] | -4.48 | 3.6188 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| ETHUSDT | 1h | B | [-0.3469619798573308, 0.1503025086237987] | -2.16 | 1.6222 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| ETHUSDT | 1h | C | [-0.3529661049290188, 0.15753063192399983] | -1.79 | 1.4974 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| SOLUSDT | 15min | A | [-0.19774546065460286, 0.09845351668524234] | -6.05 | 13.172 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| SOLUSDT | 15min | B | [-0.1793551248988037, 0.12558125876533147] | -1.57 | 5.3884 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| SOLUSDT | 15min | C | [-0.22868281187199946, 0.09455809871199991] | -0.91 | 1.9957 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| SOLUSDT | 1h | A | [-0.2451345949536976, 0.09020197433526463] | -3.33 | 6.389 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| SOLUSDT | 1h | B | [-0.1783569398439976, 0.14131882396800013] | -0.43 | 1.9966 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| SOLUSDT | 1h | C | [-0.1783569398439976, 0.14131882396800013] | -0.43 | 1.9966 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BNBUSDT | 15min | A | [-0.15553194371154222, 0.07890477507577522] | -3.43 | 3.9914 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BNBUSDT | 15min | B | [-0.19070708533952668, 0.12622228450639758] | -2.33 | 2.533 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BNBUSDT | 15min | C | [None, None] | -0.24 | 0.15352 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo; frequência não supera referência 4h |
| BNBUSDT | 1h | A | [-0.19313723999703736, 0.2187941688154581] | -1.01 | 2.3037 | 0 | PnL negativo em pelo menos um split/custo; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo |
| BNBUSDT | 1h | B | [-0.1044585756000009, 0.26598040671199863] | 0.57 | 0.76791 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; IC ajustado sem limite inferior positivo; frequência não supera referência 4h |
| BNBUSDT | 1h | C | [-0.2937778497777777, 0.31625152612444285] | -0.11 | 0.69112 | 0 | PnL negativo em pelo menos um split/custo; menos de 30 trades OOS; PF OOS abaixo de 1,20; IC ajustado sem limite inferior positivo; frequência não supera referência 4h |

### Diagnostico da melhor observacao OOS

Maior PnL OOS: BNBUSDT 1h, variante B, 10 trades, PF líquido 1.6644, PnL 0.81 USDT e 0.137 trades/dia. Isto não constitui aprovacao. BNB 1h B teve desenvolvimento e validação negativos, apenas dez trades OOS e frequência inferior é referência BNB 4h de aproximadamente 0,178 trades/dia. ETH 15m C teve apenas sete trades e perdeu no estresse; BNB 1h C teve nove trades e também perdeu no estresse. Todas as demais observacoes tiveram PnL OOS base negativo. Nenhuma satisfaz expectativa positiva robusta com maior frequência.

## Buy-and-hold e capital pequeno

Buy-and-hold compra na primeira abertura negociavel e vende na última, com custo base, saldo residual e lote arredondado. Usa capital integral disponível; diferencas de exposicao impedem atribuir todo diferencial a qualidade de sinal. é um único trade por split: PF e significancia da expectativa não são informativos. DD marcado nas aberturas.

| symbol | timeframe | net_pnl | return_fraction | costs | max_drawdown_fraction |
|---|---|---|---|---|---|
| BTCUSDT | 15min | 49.524 | 0.049524 | 2.6642 | 0.12136 |
| BTCUSDT | 1h | 54.658 | 0.054658 | 2.6709 | 0.11625 |
| ETHUSDT | 15min | -65.438 | -0.065438 | 2.5145 | 0.19655 |
| ETHUSDT | 1h | -61.09 | -0.06109 | 2.5202 | 0.19285 |
| SOLUSDT | 15min | -78.881 | -0.078881 | 2.4971 | 0.18188 |
| SOLUSDT | 1h | -74.17 | -0.07417 | 2.5032 | 0.17811 |
| BNBUSDT | 15min | 104.43 | 0.10443 | 2.7351 | 0.12359 |
| BNBUSDT | 1h | 110.19 | 0.11019 | 2.7426 | 0.11739 |

Foram executados 24 backtests OOS separados com 28 USDT, sem mudar risco, stake ou proteções. Todos terminaram com zero trades: o teto de stake de 2% gera ticket de no máximo 0,56 USDT, abaixo do mínimo de 5 USDT nos quatro pares, antes mesmo do arredondamento. Alterar risco/stake para contornar isso está fora do protocolo. PnL zero e PF zero nesses testes significam ausência de operações, não desempenho neutro de uma estratégia executável. Rejeicoes são tentativas barradas pelo lote/notional, não erros nem ordens. Executavel não significa rentavel e não garante execução em MAINNET com filtros e saldo futuros.

| symbol | timeframe | strategy | total_trades | lot_rejections | profit_factor | net_profit | final_capital |
|---|---|---|---|---|---|---|---|
| BTCUSDT | 15min | A | 0 | 315 | 0 | 0 | 28 |
| BTCUSDT | 15min | B | 0 | 128 | 0 | 0 | 28 |
| BTCUSDT | 15min | C | 0 | 7 | 0 | 0 | 28 |
| BTCUSDT | 1h | A | 0 | 68 | 0 | 0 | 28 |
| BTCUSDT | 1h | B | 0 | 35 | 0 | 0 | 28 |
| BTCUSDT | 1h | C | 0 | 22 | 0 | 0 | 28 |
| ETHUSDT | 15min | A | 0 | 258 | 0 | 0 | 28 |
| ETHUSDT | 15min | B | 0 | 87 | 0 | 0 | 28 |
| ETHUSDT | 15min | C | 0 | 16 | 0 | 0 | 28 |
| ETHUSDT | 1h | A | 0 | 58 | 0 | 0 | 28 |
| ETHUSDT | 1h | B | 0 | 19 | 0 | 0 | 28 |
| ETHUSDT | 1h | C | 0 | 18 | 0 | 0 | 28 |
| SOLUSDT | 15min | A | 0 | 287 | 0 | 0 | 28 |
| SOLUSDT | 15min | B | 0 | 112 | 0 | 0 | 28 |
| SOLUSDT | 15min | C | 0 | 25 | 0 | 0 | 28 |
| SOLUSDT | 1h | A | 0 | 81 | 0 | 0 | 28 |
| SOLUSDT | 1h | B | 0 | 26 | 0 | 0 | 28 |
| SOLUSDT | 1h | C | 0 | 24 | 0 | 0 | 28 |
| BNBUSDT | 15min | A | 0 | 400 | 0 | 0 | 28 |
| BNBUSDT | 15min | B | 0 | 165 | 0 | 0 | 28 |
| BNBUSDT | 15min | C | 0 | 4 | 0 | 0 | 28 |
| BNBUSDT | 1h | A | 0 | 103 | 0 | 0 | 28 |
| BNBUSDT | 1h | B | 0 | 37 | 0 | 0 | 28 |
| BNBUSDT | 1h | C | 0 | 22 | 0 | 0 | 28 |

## Limitações materiais

O motor existente verifica stop/alvo e trailing usando OHLC. Nesta pesquisa OHLC de execução é substituido pelo open, eliminando ambiguidade intrabar/look-ahead na entrada. Consequencia: barreiras tocadas somente intrabar não são capturadas e o drawdown pode ser subestimado; gaps podem ser preenchidos no nível do stop/alvo pelo motor, preço otimista no caso de stop. Não se alterou o algoritmo científico existente. A reprodução A é fiel aos sinais e níveis originais, mas usa esta convenção de execução, não uma reprodução de fills reais. Qualquer resultado positivo exige motor validado para gaps e intrabar antes de promocao. O uso de stop calculado no fechamento anterior mantém risco sujeito ao gap até a abertura.

Um ano e um único OOS são insuficientes para validar todos os regimes. Spread/slippage são hipóteses fixas. Não há latência, impacto, medicao de book, auditoria autenticada de conta ou garantia da procedencia econômica dos candles. Métricas pequenas refletem dimensionamento e saldo não alocado; não representam retorno de uma carteira multiestrategia.

## Decisão e próxima etapa

Nenhuma variante deve ser promovida automaticamente. Consultar robustness.json para cada motivo de rejeição; resultado positivo isolado, poucos trades ou melhoria relativa sobre A não bastam. Aumentar frequência sem melhorar expectativa líquida não atende ao objetivo. A referência de frequência é a simulação histórica Sma200RegimeGated 4h anterior (BNB é o par do LIVE), não uma estimativa autenticada de trades da conta.

Próxima etapa: manter todas sem implantacao; primeiro validar execução intrabar/gaps em ambiente separado e obter período prospectivo ainda não consumido por seleção. Não ampliar variantes ou timeframes para perseguir resultado positivo. Nenhum PAPER contínuo ou LIVE foi iniciado.

## Isolamento e evidência operacional

Ambiente sem segredos herdados, .env bloqueado, rede e subprocessos bloqueados no processo científico, escritas limitadas a esta pasta por audit hook, uma CPU lógica, prioridade Idle e bibliotecas numéricas com uma thread. Limites de 900 MiB RSS e 2 GiB livres conferidos entre etapas, sem limite rígido por candle. Backtests sequenciais. operational_initial.json e operational_final.json registram PIDs e criacao dos processos; hashes verificam módulos preservados. Nenhuma conexão ao MySQL, emissao, cancelamento ou alteração de ordem pela pesquisa. O worker LIVE pode ter negociado normalmente; não foi feita auditoria autenticada da Binance.

## Reproducao e arquivos

Na raiz do projeto, executar sequencialmente `python -B research/donchian_regime_20261017/run.py`, `python -B research/donchian_regime_20261017/analyze.py`, `python -B research/donchian_regime_20261017/check_operations.py` e `python -B research/donchian_regime_20261017/report.py`. O protocolo interrompe a pesquisa se os PIDs originais não estiverem presentes; em outra sessão, adaptar somente os PIDs após inspeção operacional, nunca iniciar executores.

96 backtests principais + 24 de capital pequeno; 48 referências analiticas buy-and-hold (4 ativos é 2 timeframes é 3 splits é 2 capitais). results.csv contém IS/OOS/custos, trades.csv os trades completos, trade_diagnostics.csv/regime_summary.csv os diagnósticos, small_capital.csv e buy_and_hold.csv as referências; verification.json registra verificações causais/contabeis.

Fonte normativa consultada: [Binance Spot é filtros](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md). Valores concretos usados estão em exchange_info.json.
