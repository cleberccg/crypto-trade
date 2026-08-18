# WORKFLOW_GUIDE

## Fase 12 - Implementacao Controlada da SuperTrend

Esta fase executa a primeira implementacao controlada derivada do ranking da Fase 11.

Pergunta central: **a SuperTrend V1 merece campanha paper agora ou deve ser descartada?**

### Regras
1. Implementar apenas uma estrategia candidata por vez.
2. Reutilizar pipeline existente da plataforma (backtest, optimizer, validation, paper).
3. Executar funil em etapas com early stop para evitar custo desnecessario.
4. Publicar decisao final obrigatoria OPCAO A ou OPCAO B.

### Comando Oficial
- python main.py supertrend-controlled-implementation

### Etapas obrigatorias
1. Smoke test (sinal, risk manager, paper trader).
2. Backtest padronizado.
3. Early stop se claramente inviavel.
4. Otimizacao reduzida apenas se houver potencial.
5. Validation.
6. Qualificacao para paper somente se aprovada.

### Saidas obrigatorias
1. Resultado por etapa do pipeline.
2. Comparacao entre referencias estudadas e implementacao final.
3. Decisao final OPCAO A ou OPCAO B.

## Fase 11 — Pesquisa e Curadoria Cientifica de Estrategias para Cripto

Esta fase e exclusivamente de pesquisa e organizacao de conhecimento.

Pergunta central: **quais estrategias valem entrar no proximo lote de implementacao?**

### Regras
1. Nao implementar estrategias.
2. Nao alterar pipeline cientifico existente.
3. Nao alterar catalogo atual.
4. Classificar e priorizar com base em evidencia publica.

### Comando Oficial
- python main.py crypto-strategy-research

### Saidas obrigatorias
1. Quantidade total de estrategias pesquisadas.
2. Ranking completo.
3. Top 20 mais promissoras.
4. Compatibilidade imediata (SIM/NAO).
5. Necessidades tecnicas para estrategias nao imediatas.
6. Recomendacao de 5 estrategias para o proximo lote.

## Fase 10.2 — Auditoria Cientifica do Pipeline de Avaliacao

A Fase 10.2 valida se o pipeline esta classificando corretamente as estrategias atuais antes de ampliar o catalogo.

Pergunta central: **Por que nenhuma estrategia foi aprovada para Paper Trading?**

### Regras
1. Nao implementar novas estrategias nesta fase.
2. Nao modificar estrategias existentes.
3. Nao remover/substituir funcionalidades da plataforma.
4. Executar auditoria quantitativa completa por adicao de capacidade.

### Comando Oficial
- python main.py strategy-catalog-audit

### Saidas obrigatorias
1. Ranking completo das estrategias.
2. Matriz de eliminacao por etapa.
3. Distancia ate aprovacao por criterio.
4. Distribuicao das metricas PF/Sharpe/Expectancy/Drawdown.
5. Benchmark reduzido por ativo/timeframe (BTC e ETH em 5m/15m/1h).
6. Classificacao em grupos A/B/C.
7. Decisao final OPCAO A ou OPCAO B.

## Fase 10 — Catalogo Cientifico Permanente de Estrategias

A Fase 10 operacionaliza o catalogo para escalar de 10 para 30/50 estrategias sem explosao de custo computacional.

Pergunta central: **Quais estrategias sobrevivem ao funil padronizado e merecem Paper Trading?**

### Regras
1. Nao otimizar todo o universo de estrategias de uma vez.
2. Todas passam por smoke test e backtest padronizado inicial.
3. Estrategias claramente ruins sao eliminadas imediatamente.
4. Otimizacao apenas nas 3 a 5 melhores apos pre-selecao.
5. Apenas aprovadas na validacao seguem para Paper Trading.

### Comando Oficial
- python main.py strategy-catalog-cycle

### Classificacao Obrigatoria do Catalogo
1. Status: Implementada -> Smoke Test -> Backtest -> Otimizada -> Validada -> Paper Trading -> Producao -> Rejeitada
2. Origem: Livro, Paper, Open Source, Descoberta da Plataforma
3. Familia: Tendencia, Reversao, Breakout, Momentum, Volatilidade

### Saida Final Obrigatoria
- Quantas estrategias foram adicionadas no lote
- Ranking consolidado
- Top 10
- Top 3 para Paper Trading
- Comparacao classicas vs descobertas automaticamente
- Recomendacao final

## Fase 9.4 — Primeira Melhoria Operacional Controlada (V1.1)

A Fase 9.4 muda o foco para operacao consistente e lucro real em Paper Trading.

Pergunta central: **A V1.1 melhorou objetivamente a V1.0 alterando somente a saida?**

### Regras
1. Nao criar novos labs/discovery/modelos/frameworks.
2. Entrada congelada (mesma logica da V1.0).
3. Modificar apenas a gestao de saida (prioridade: time stop).
4. Uma unica tentativa automatica (sem V1.2/V1.3/V2).

### Validacao Obrigatoria
1. Comparar V1.0 vs V1.1 no mesmo ativo, periodo, timeframe e capital.
2. Medir: Profit Factor, Sharpe, Expectancy, Drawdown, Net Profit, Win Rate.
3. Medir tambem: numero de trades, permanencia media, tempo bloqueado, eficiencia de saida.
4. Aprovar V1.1 somente com melhoria objetiva; caso contrario manter/reverter V1.0.

### Comando Oficial
- python main.py phase9-4-controlled-improvement

### Saida Final Obrigatoria
- A V1.1 melhorou a V1.0? (Sim/Não)
- Quais metricas melhoraram?
- Quais metricas pioraram?
- A estrategia esta pronta para continuar em Paper Trading?
- A recomendacao e continuar operando ou reverter para V1.0?

---

## Fase 9.3 — Trade Lifecycle Audit

A Fase 9.3 responde onde exatamente o desempenho esta sendo perdido, com foco na gestao das posicoes.

Pergunta central: **A estrategia entra corretamente, mas permanece posicionada tempo demais?**

### Etapas
1. Estatisticas das posicoes (duracao em candles, minutos, horas, percentis).
2. Motivos de saida quantificados.
3. Tempo bloqueado: percentual do periodo e setups perdidos por trade.
4. Qualidade das saidas: MFE, MAE, eficiencia de saida (estamos saindo cedo ou tarde?).
5. Simulacoes de tempo ideal (saida 25%/50% mais cedo, time stop, saida no pico).
6. Capacidade operacional: trades adicionais possiveis com posicoes mais curtas.
7. Diagnostico final: gargalo principal + recomendacoes priorizadas.

### Comando Oficial
- python main.py trade-lifecycle-audit

---

## Finalidade
O Trade Outcome Learning Lab inaugura a FASE 8: sair de aprendizado de estados de mercado e passar a aprender diretamente quais decisoes operacionais historicas geram resultado consistente.

A FASE 9 executa a implementacao controlada do candidato aprovado para garantir traducao fiel e preservacao de edge antes de Paper Trading.

A FASE 9.0 otimiza permanentemente o framework de execucao para remover gargalos arquiteturais antes do relancamento automatico da campanha da Fase 9.

Pergunta central do laboratorio:
- Quais decisoes operacionais historicamente produziram lucro consistente?

## Fluxo de Execucao
1. Construcao do dataset supervisionado de oportunidades operacionais.
2. Geracao de outcomes futuros (5/10/20/50 candles, MFE, MAE, drawdown, duracao, PF individual, expectancy individual).
3. Definicao de alvos supervisionados multi-target.
4. Descoberta automatica de regras orientadas ao alvo.
5. Explicabilidade por cobertura, confianca, importancia e estabilidade.
6. Robustez em Train/Validation/Test e por eixo temporal/ativo/regime/timeframe.
7. Calculo do Trade Outcome Score.
8. Decisao automatica de aprovacao ou rejeicao com motivo formal.
9. Persistencia permanente em JSON/CSV/Markdown e banco.

## Comandos Oficiais
Comando principal:
- python main.py trade-outcome-learning

Ajuda de parametros:
- python main.py trade-outcome-learning --help


A FASE 9 executa a implementacao controlada do candidato aprovado para garantir traducao fiel e preservacao de edge antes de Paper Trading.

A FASE 9.0 otimiza permanentemente o framework de execucao para remover gargalos arquiteturais antes do relancamento automatico da campanha da Fase 9.

Pergunta central do laboratorio:
- Quais decisoes operacionais historicamente produziram lucro consistente?

## Fluxo de Execucao
1. Construcao do dataset supervisionado de oportunidades operacionais.
2. Geracao de outcomes futuros (5/10/20/50 candles, MFE, MAE, drawdown, duracao, PF individual, expectancy individual).
3. Definicao de alvos supervisionados multi-target.
4. Descoberta automatica de regras orientadas ao alvo.
5. Explicabilidade por cobertura, confianca, importancia e estabilidade.
6. Robustez em Train/Validation/Test e por eixo temporal/ativo/regime/timeframe.
7. Calculo do Trade Outcome Score.
8. Decisao automatica de aprovacao ou rejeicao com motivo formal.
9. Persistencia permanente em JSON/CSV/Markdown e banco.

## Comandos Oficiais
Comando principal:
- python main.py trade-outcome-learning

Ajuda de parametros:
- python main.py trade-outcome-learning --help

Exemplo com alvos customizados:
- python main.py trade-outcome-learning --targets winner,return_above,risk_adjusted --trade-outcome-score-threshold 75

## Parametros Principais
- --events-glob
- --targets
- --return-above-threshold
- --return-below-threshold
- --risk-adjusted-threshold
- --train-ratio
- --validation-ratio
- --min-support
- --max-rule-coverage
- --min-precision-gain
- --min-generalization-score
- --min-robustness-score
- --max-overfit-gap
- --trade-outcome-score-threshold
- --top-k-candidates
- --output-prefix
- --no-db

## Artefatos
- optimization/results/trade_outcome_learning_<timestamp>.json
- optimization/results/trade_outcome_learning_<timestamp>.csv
- optimization/results/trade_outcome_learning_<timestamp>.md

## Persistencia no Banco
- tabela: trade_outcome_learning_runs
- conteudo persistido: decisao, score, robustez, generalizacao, sinais de overfitting, motivo de reprovacao e ponteiros para artefatos.

## Integracao com a Plataforma
- Entrada CLI: main.py
- Modulo do laboratorio: research/labs/trade_outcome_learning_lab.py
- Modelo ORM: database/history_models.py (TradeOutcomeLearningRun)
- Repositorio: database/history_repositories.py (TradeOutcomeLearningRunRepository)
- Dependencias de metricas: utils/metrics.py

## FASE 9 - Implementacao Controlada
Comando principal:
- python main.py phase9-controlled-implementation

Objetivo:
- implementar estrategia permanente fiel ao candidato aprovado e auditar a preservacao de edge.

Etapas automatizadas:
1. Traduzir regra aprovada para estrategia permanente.
2. Auditoria de fidelidade laboratorio x estrategia (precision, recall, F1, cobertura, intersecao, FP/FN).
3. Gate de fidelidade (F1 >= 95% por padrao).
4. Backtest de eventos com comparacao esperado x observado (PF, Sharpe, Expectancy, Drawdown, Win Rate).
5. Execucao automatica de Optimizer, Validation, Strategy Research Lab e Trade Management Lab.
6. Consolidacao laboratorio -> estrategia -> backtest -> validation.
7. Decisao final OPCAO A ou OPCAO B.

Artefatos:
- optimization/results/trade_outcome_controlled_implementation_<timestamp>.json
- optimization/results/trade_outcome_controlled_implementation_<timestamp>.csv
- optimization/results/trade_outcome_controlled_implementation_<timestamp>.md

Persistencia:
- tabela trade_outcome_implementation_runs

## FASE 9.0 - Otimizacao do Framework de Execucao
Comando principal:
- python main.py execution-framework-optimization

Objetivo:
- eliminar recálculo completo por barra e fornecer infraestrutura reutilizável para todas as estrategias.

Entregas permanentes:
1. API base de dataset preparado em strategies/base_strategy.py.
2. Backtest consumindo apenas slices de dataset já preparado.
3. Cache reutilizável por dataset/simbolo/timeframe/parametros.
4. Observabilidade de preprocessing, bars/s, ETA e tempo até primeiro resultado.
5. Benchmark de equivalência e performance.
6. Relançamento automático da campanha oficial da Fase 9.

Persistencia:
- tabela execution_framework_optimization_runs

## Boas Praticas Operacionais
- Nao usar scripts temporarios para execucao real.
- Usar apenas comandos oficiais da plataforma.
- Registrar todos os resultados via artefatos e banco para reproducibilidade.

## Modo Operacional Contínuo (Paper Trading)
Objetivo desta fase:
- operar diariamente com a melhor estrategia disponivel hoje;
- registrar cada decisao operacional;
- gerar relatorio diario para aprendizado incremental guiado por dados reais.

Comandos oficiais:
- python main.py paper --symbol BTC/USDT --timeframe 5m --start 2026-06-01 --end 2026-06-30 --strategy-name TradeOutcomeNextGenV1
- python main.py paper-daily-report --date 2026-06-30 --strategy-name TradeOutcomeNextGenV1

Persistencia operacional utilizada:
- trade_history: operacoes fechadas com stop/take, duracao, pnl, motivo de saida;
- signal_snapshots: entradas aceitas/rejeitadas, score, rr, regime, motivo de rejeicao;
- portfolio_snapshots: curva de patrimonio intra-dia;
- backtest_runs: consolidacao de cada execucao paper.

Artefatos diarios:
- optimization/results/paper_trading_daily_report_<data>_<timestamp>.json
- optimization/results/paper_trading_daily_report_<data>_<timestamp>.md
- optimization/results/paper_trading_daily_report_<data>_<timestamp>.operations.csv

## FASE 9.1 - Operacao Continua e Versionamento
Comando principal de execucao continua:
- python main.py paper-live --symbol BTC/USDT --timeframe 5m --strategy-name TradeOutcomeNextGenV1 --strategy-version v1.0

Comandos de suporte:
- python main.py paper-operational-report --date 2026-06-30 --strategy-name TradeOutcomeNextGenV1 --strategy-version v1.0
- python main.py strategy-version-compare --strategy-name TradeOutcomeNextGenV1 --current-version v1.1 --base-version v1.0

Regras operacionais permanentes:
1. Nenhuma mudanca de versao e aplicada sem minimo de trades acumulados na versao anterior (gate configuravel).
2. Toda versao e persistida em strategy_versions; versoes antigas nunca sao sobrescritas.
3. Comparacao entre versoes e automatica e classifica: improved, worsened, inconclusive.
4. Execucao continua mantem checkpoint em paper_live_state.json para resume apos interrupcao.

## FASE 9.2 - Diagnostico Operacional Permanente
Comando oficial:
- python main.py strategy-diagnostics

Escopo automatico:
1. Ultima sessao Paper Live disponivel.
2. Ultimas 24 horas.
3. Ultimos 7 dias, quando houver historico.

Regras permanentes:
1. O diagnostico deve mostrar quantitativamente onde os sinais morreram.
2. O resultado deve listar motivos de rejeicao, ranking de bloqueios e gargalo principal.
3. A decisao final deve ser apenas OPCAO A ou OPCAO B.
4. O resultado e persistido em banco, JSON, CSV e Markdown.

Relatorios obrigatorios da fase:
- Por operacao: contexto, motivo de entrada/saida, score e resultado.
- Horario: numero de operacoes, lucro, win rate, PF por faixa horaria.
- Diario: PnL, PF, Sharpe proxy, Expectancy, Drawdown e diagnostico objetivo.
- Semanal: comparacao ultimos 7 dias vs semana anterior.
- Mensal: comparacao ultimos 30 dias vs periodo anterior.
