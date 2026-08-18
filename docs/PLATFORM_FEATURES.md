# PLATFORM_FEATURES

## SuperTrend Controlled Implementation (Fase 12)
- Nome: Implementacao Controlada da SuperTrend V1
- Objetivo: implementar a primeira candidata do ranking da Fase 11 com pipeline em etapas e gate cientifico antes de campanha paper
- Escopo desta fase:
  - implementacao permanente da estrategia `SuperTrendV1`
  - smoke test com integracao de sinais/risk manager/paper trader
  - backtest padronizado e early stop quando claramente inviavel
  - otimizacao reduzida e validacao apenas quando houver potencial
  - qualificacao para campanha paper somente se aprovada
- Arquivos principais:
  - strategies/supertrend_v1.py
  - phase12_supertrend_controlled.py
  - optimizer/parameter_grid.py
  - strategies/__init__.py
  - main.py
  - docs/STRATEGY_CATALOG.md
- Comando CLI:
  - python main.py supertrend-controlled-implementation
- Saidas automaticas:
  - resultado por etapa (smoke, backtest, early stop, optimizer, validation)
  - comparacao das referencias da estrategia e implementacao final
  - decisao final OPCAO A ou OPCAO B
- Persistencia:
  - JSON
  - CSV
  - Markdown
  - Banco (execution_sessions + execution_checkpoints)

## Crypto Strategy Research Knowledge Base (Fase 11)
- Nome: Pesquisa e Curadoria Cientifica de Estrategias para Criptomoedas
- Objetivo: construir base permanente de conhecimento para priorizar futuras implementacoes sem alterar o catalogo atual
- Escopo desta fase:
  - sem implementacao de estrategias
  - sem backtest/otimizacao/validacao operacional
  - apenas pesquisa, classificacao e priorizacao
- Arquivos principais:
  - research/crypto_strategy_knowledge_base/strategies.json
  - research/crypto_strategy_knowledge_base/service.py
  - research/crypto_strategy_knowledge_base/__init__.py
  - docs/CRYPTO_STRATEGY_RESEARCH.md
  - main.py
- Comando CLI:
  - python main.py crypto-strategy-research
- Saidas automaticas:
  - ranking consolidado e Top 20 promissoras
  - classificacao Alta/Media/Baixa prioridade
  - compatibilidade imediata (SIM/NAO) com justificativas
  - recomendacao de 5 estrategias para o proximo lote
- Persistencia:
  - JSON
  - CSV
  - Markdown
  - docs/CRYPTO_STRATEGY_RESEARCH.md

## Scientific Audit of Evaluation Pipeline (Fase 10.2)
- Nome: Auditoria Cientifica do Pipeline de Avaliacao do Catalogo
- Objetivo: explicar quantitativamente por que estrategias nao foram aprovadas para Paper Trading sem alterar estrategias existentes
- Arquivos principais:
  - strategy_catalog/audit.py
  - strategy_catalog/__init__.py
  - main.py
- Comando CLI:
  - python main.py strategy-catalog-audit
- Entregas automaticas:
  - Ranking completo com PF, Sharpe, Expectancy, Net Profit, Drawdown, Win Rate, trades, Recovery Factor, Robustez, Stability e Implementability
  - Analise criterio a criterio (pass/fail e criterio principal de reprovacao)
  - Distancia percentual ate aprovacao por criterio e distancia consolidada
  - Matriz de eliminacao por etapa (Smoke, Backtest, Optimizer, Validation, Scientific Validation, Paper Qualification)
  - Distribuicao de metricas (PF, Sharpe, Expectancy, Drawdown)
  - Benchmark reduzido entre contextos (BTC/USDT e ETH/USDT em 5m/15m/1h)
  - Classificacao final por grupos A/B/C
  - Recomendacao automatica OPCAO A ou OPCAO B baseada em dados
- Persistencia:
  - JSON
  - CSV (ranking + matriz de eliminacao)
  - Markdown
  - Banco (execution_sessions + execution_checkpoints)

## Scientific Strategy Catalog Cycle (Fase 10)
- Nome: Catalogo Cientifico Permanente com funil operacional em etapas
- Objetivo: avaliar estrategias de forma escalavel sem otimizar tudo de uma vez
- Arquivos principais:
  - strategy_catalog/catalog.py
  - strategy_catalog/service.py
  - strategy_catalog/__init__.py
  - strategies/classic_catalog_strategies.py
  - main.py
  - docs/STRATEGY_CATALOG.md
- Comando CLI:
  - python main.py strategy-catalog-cycle
- Lote inicial:
  - 10 estrategias classicas implementadas
- Fluxo obrigatorio (funil):
  1. Implementar estrategias
  2. Smoke test em todas
  3. Backtest padronizado em todas (mesmo ativo, periodo, timeframe e capital)
  4. Eliminar imediatamente estrategias claramente ruins
  5. Otimizar apenas top 3-5
  6. Validar apenas shortlist otimizada
  7. Levar somente aprovadas para Paper Trading
- Classificacao permanente no catalogo:
  - Status: Implementada -> Smoke Test -> Backtest -> Otimizada -> Validada -> Paper Trading -> Producao -> Rejeitada
  - Origem: Livro, Paper, Open Source, Descoberta da Plataforma
  - Familia: Tendencia, Reversao, Breakout, Momentum, Volatilidade
- Persistencia:
  - JSON
  - CSV
  - Markdown
  - docs/STRATEGY_CATALOG.md
  - Banco (execution_sessions + execution_checkpoints)

## First Controlled Operational Improvement (Fase 9.4)
- Nome: Primeira Melhoria Operacional Controlada V1.1
- Objetivo: melhorar apenas a gestao de saida da estrategia atual, mantendo a entrada identica a V1.0.
- Arquivos principais:
  - phase94_controlled_improvement.py
  - strategies/trade_outcome_nextgen_v1_1.py
  - backtesting/engine.py
  - paper_trading/paper_trader.py
  - main.py
- Comando CLI:
  - python main.py phase9-4-controlled-improvement
- Escopo controlado:
  - entrada congelada (mesmas regras da V1.0)
  - melhoria apenas em saida: time stop (principal), sem novos labs/discovery/frameworks
  - uma unica tentativa automatica (sem V1.2/V1.3/V2)
- Validacao automatica obrigatoria (mesmo ativo, periodo, timeframe e capital):
  - V1.0 vs V1.1
  - Profit Factor, Sharpe, Expectancy, Drawdown, Net Profit, Win Rate
  - Numero de trades, permanencia media, tempo bloqueado, eficiencia de saida
- Criterio de aprovacao:
  - V1.1 so substitui V1.0 se houver melhoria objetiva
  - caso contrario, recomendacao automatica para manter/reverter V1.0
- Persistencia:
  - JSON
  - CSV
  - Markdown
  - Banco (execution_sessions + execution_checkpoints)
- Paper Trading:
  - se aprovado, inicia campanha Paper Trading exclusivamente com V1.1

## Trade Lifecycle Audit (Fase 9.3)
- Nome: Trade Lifecycle Audit permanente
- Objetivo: auditar o ciclo de vida completo das posicoes para identificar onde o desempenho e perdido — entrada, score, risk manager, gestao da posicao ou gestao da saida.
- Arquivos principais:
  - trade_lifecycle_audit.py
  - database/history_models.py (TradeHistory, SignalSnapshot)
  - database/repositories.py (CandleRepository)
  - main.py
- Comando CLI:
  - python main.py trade-lifecycle-audit
- Parametros opcionais:
  - --strategy-name, --symbol, --timeframe, --execution-id
  - --window-days (padrao: 30)
  - --output-prefix
  - --no-db
- Etapas auditadas (7):
  1. Estatisticas de duracao (candles, minutos, horas, percentis)
  2. Motivos de saida (take_profit, stop_loss, strategy_exit, etc.)
  3. Tempo bloqueado por posicao aberta (% do periodo, setups perdidos por trade)
  4. Qualidade das saidas (MFE, MAE, eficiencia de saida)
  5. Simulacoes de tempo ideal (saida 25%/50% mais cedo, time stop, reversao)
  6. Capacidade operacional (trades adicionais possiveis, nova frequencia estimada)
  7. Diagnostico final do gargalo + recomendacoes priorizadas
- Persistencia:
  - JSON (relatorio completo)
  - CSV (trade-level MFE/MAE + cenarios de simulacao)
  - Markdown (relatorio executivo)
  - Banco (execution_sessions + execution_checkpoints)

## Operational Strategy Diagnostics (Fase 9.2)
- Nome: Strategy Diagnostics permanente
- Objetivo: auditar quantitativamente por que uma estrategia operou ou nao operou, sem scripts temporarios.
- Arquivos principais:
  - strategy_diagnostics.py
  - paper_trading/paper_live_service.py
  - paper_trading/paper_trader.py
  - database/history_service.py
  - database/history_models.py
  - main.py
- Comando CLI:
  - python main.py strategy-diagnostics
- Entradas automaticas:
  - ultima sessao Paper Live disponivel
  - ultimas 24 horas
  - ultimos 7 dias
- Persistencia no banco:
  - execution_sessions (sessao do diagnostico)
  - execution_checkpoints (resumo e artefatos)
- Artefatos gerados:
  - JSON
  - CSV
  - Markdown
  - heatmap operacional por ativo/timeframe
- Pipeline auditado:
  - candles analisados
  - setups encontrados
  - filtros aprovados
  - score aprovado
  - Risk Manager aprovado
  - ordens enviadas
  - ordens executadas
  - trades abertos
  - trades fechados
- Saida final obrigatoria:
  - OPCAO A ou OPCAO B com o gargalo principal e ranking de bloqueios

## Continuous Paper Operation (Fase 9.1)
- Nome: Continuous Paper Operation + Strategy Version Manager
- Objetivo: operar continuamente em paper trading, versionar estrategia sem sobrescrever historico e evoluir com base em dados reais.
- Arquivos principais:
  - paper_trading/paper_live_service.py
  - paper_trading/paper_trader.py
  - paper_trading/daily_report.py
  - database/session_models.py
  - database/history_models.py
  - main.py
- Comandos CLI:
  - python main.py paper-live
  - python main.py paper-operational-report
  - python main.py strategy-version-compare
- Parametros principais:
  - paper-live: --strategy-name --strategy-version --poll-seconds --max-cycles --resume --min-trades-before-change
  - paper-operational-report: --date --strategy-name --strategy-version --output-prefix
  - strategy-version-compare: --strategy-name --current-version --base-version --window-days
- Relatorios gerados automaticamente:
  - por operacao
  - horario
  - diario
  - semanal
  - mensal
- Persistencia no banco:
  - strategy_versions (historico e versao ativa)
  - execution_sessions (sessao continua e retomada)
  - trade_history (operacoes executadas)
  - signal_snapshots (sinais aceitos/rejeitados)
  - portfolio_snapshots (curva de patrimonio)
- Fluxo de execucao continuo:
  1. Le novos candles da base consolidada.
  2. Atualiza indicadores.
  3. Gera sinais.
  4. Aplica validacao do risk manager.
  5. Executa ordens paper.
  6. Persiste operacoes e sinais.
  7. Gera relatorios operacionais.
  8. Mantem checkpoint para interrupcao e retomada.

## Execution Framework Optimization (Fase 9.0)
- Nome: Execution Framework Optimization
- Objetivo: eliminar permanentemente recálculo O(n²) no loop bar a bar por meio de pré-processamento único de dataset, cache reutilizável e observabilidade nativa de performance.
- Arquivos principais:
  - research/services/execution_framework_optimization.py
  - strategies/base_strategy.py
  - backtesting/engine.py
  - optimizer/optimizer.py
  - validation/validator.py
  - database/history_models.py
  - database/history_repositories.py
  - main.py
- Comando CLI:
  - python main.py execution-framework-optimization
- Parametros principais:
  - --strategy-name
  - --benchmark-symbol
  - --benchmark-timeframe
  - --benchmark-bars
  - --initial-capital
  - --output-prefix
  - --skip-phase9-rerun
  - --no-db
- Arquivos gerados:
  - optimization/results/<output_prefix>_<timestamp>.json
  - optimization/results/<output_prefix>_<timestamp>.csv
  - optimization/results/<output_prefix>_<timestamp>.md
- Persistencia no banco:
  - tabela execution_framework_optimization_runs
- Fluxo de execucao:
  1. Audita estrategias e identifica padroes de recálculo/caching.
  2. Executa benchmark de equivalencia antes/depois com a infraestrutura de dataset preparado.
  3. Confirma same_trades, same_metrics e equivalence_passed.
  4. Mede bars/s, tempo total e ETA estimada de campanha.
  5. Relanca automaticamente a campanha oficial da Fase 9 com o mesmo comando original.

## Controlled Implementation Audit (Fase 9)
- Nome: Controlled Implementation Audit
- Objetivo: implementar estrategia permanente fiel ao candidato aprovado e validar preservacao do edge ate a decisao final OPCAO A/B.
- Arquivos principais:
  - strategies/trade_outcome_nextgen_v1.py
  - research/services/trade_outcome_controlled_implementation.py
  - database/history_models.py
  - database/history_repositories.py
  - main.py
- Comando CLI:
  - python main.py phase9-controlled-implementation
- Parametros principais:
  - --events-glob
  - --trade-outcome-csv
  - --strategy-name
  - --target-name
  - --approved-rule
  - --distance-threshold
  - --fidelity-min-f1
  - --optimizer-max-combinations
  - --optimizer-workers
  - --optimizer-capital
  - --output-prefix
  - --skip-optimizer-validation
  - --skip-research-labs
  - --no-db
- Arquivos gerados:
  - optimization/results/<output_prefix>_<timestamp>.json
  - optimization/results/<output_prefix>_<timestamp>.csv
  - optimization/results/<output_prefix>_<timestamp>.md
- Persistencia no banco:
  - tabela trade_outcome_implementation_runs
- Fluxo de execucao:
  1. Carrega candidato aprovado da Fase 8.1.
  2. Executa traducao fiel da regra operacional na estrategia permanente.
  3. Audita fidelidade laboratorio x estrategia (precision, recall, F1, cobertura, intersecao, FP, FN).
  4. Aplica gate de fidelidade (F1 minimo configuravel, default 95%).
  5. Executa backtest de eventos para comparar metricas esperadas x observadas.
  6. Executa optimizer e validation com a estrategia implementada.
  7. Executa Strategy Research Lab e Trade Management Lab com a estrategia implementada.
  8. Consolida comparativos e decide OPCAO A/OPCAO B.

## Trade Outcome Learning Lab (Fase 8)
- Nome: Trade Outcome Learning Lab
- Objetivo: aprender diretamente decisoes operacionais lucrativas via abordagem supervisionada multi-target, com explicabilidade, robustez e score de aprovacao de implementacao.
- Arquivos principais:
  - research/labs/trade_outcome_learning_lab.py
  - database/history_models.py
  - database/history_repositories.py
  - main.py
- Comando CLI:
  - python main.py trade-outcome-learning
- Parametros principais:
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
- Arquivos gerados:
  - optimization/results/<output_prefix>_<timestamp>.json
  - optimization/results/<output_prefix>_<timestamp>.csv
  - optimization/results/<output_prefix>_<timestamp>.md
- Persistencia no banco:
  - tabela trade_outcome_learning_runs
- Fluxo de execucao:
  1. Construcao do dataset supervisionado de oportunidades.
  2. Geracao de outcomes futuros (5/10/20/50 candles, MFE, MAE, drawdown, duracao, PF/expectancy individual).
  3. Definicao de alvos supervisionados configuraveis.
  4. Descoberta automatica de regras (entrada, filtros, contexto, saida).
  5. Explicabilidade por importancia, cobertura, confianca e estabilidade.
  6. Robustez em Train/Validation/Test e por ativo/regime/timeframe/tempo.
  7. Calculo do Trade Outcome Score.
  8. Aprovacao/reprovacao automatica com sinalizacao de overfitting.
  9. Persistencia permanente de execucao e artefatos.

## Scientific Robustness Validation (Fase 7.1 / 7.1.1)
- Nome: Scientific Robustness Validation
- Objetivo: validar candidato de edge com isolamento temporal Train/Validation/Test, robustez por regime/ativo/periodo, eliminacao de regras triviais, score cientifico consolidado e guardrails de adequacao de corpus.
- Arquivos principais:
  - research/services/scientific_robustness_validation.py
  - database/history_models.py
  - database/history_repositories.py
  - main.py
- Comando CLI:
  - python main.py robustness-validation
- Parametros principais:
  - --phase6-csv
  - --candidate-csv
  - --events-glob
  - --train-ratio
  - --validation-ratio
  - --min-support
  - --max-rule-coverage
  - --min-discrimination-gap
  - --min-scientific-score
  - --min-generalization-score
  - --min-robustness-score
  - --min-files
  - --min-events
  - --min-assets
  - --min-timeframes
  - --min-context-events
  - --min-coverage-days
  - --min-contexts
  - --output-prefix
  - --no-db
- Arquivos gerados:
  - optimization/results/<output_prefix>_<timestamp>.json
  - optimization/results/<output_prefix>_<timestamp>.csv
  - optimization/results/<output_prefix>_<timestamp>.md
- Persistencia no banco:
  - tabela scientific_robustness_validation_runs
- Fluxo de execucao:
  1. Carrega clusters contextuais da Fase 6.
  2. Carrega eventos e executa dataset audit completo (arquivos, eventos, ativos, timeframes, cobertura temporal e ocorrencias por contexto).
  3. Classifica a execucao: FULL_DATASET, REPRESENTATIVE_SAMPLE, LIMITED_SAMPLE ou INSUFFICIENT_SAMPLE.
  4. Valida guardrails configurados; se falhar, retorna STATUS=VALIDACAO_INCONCLUSIVA (bloqueia decisao A/B) e recomenda comando oficial com corpus completo.
  5. Gera regras apenas no Train (sem vazamento).
  6. Elimina regras triviais automaticamente.
  7. Avalia regra em Train/Validation/Test isolados.
  8. Calcula robustez por regime, ativo e periodo.
  9. Calcula degradacao de generalizacao e Scientific Robustness Score.
  10. Persistencia em JSON, CSV, Markdown e banco.
- Dependencias:
  - pandas
  - numpy
  - sqlalchemy
  - utils.metrics
- Exemplos:
  - python main.py robustness-validation
  - python main.py robustness-validation --min-scientific-score 80 --output-prefix scientific_robustness_v2
