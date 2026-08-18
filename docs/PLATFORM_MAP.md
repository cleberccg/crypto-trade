# PLATFORM_MAP

## SuperTrend Controlled Implementation (Fase 12)
- Funcionalidades: implementacao permanente da estrategia SuperTrendV1 com pipeline controlado em etapas (smoke -> backtest -> early stop -> optimizer reduzido -> validation -> paper qualification), decisao OPCAO A/B e artefatos auditaveis.
- Comando:
  - python main.py supertrend-controlled-implementation
- Modulos:
  - strategies/supertrend_v1.py
  - phase12_supertrend_controlled.py
  - optimizer/parameter_grid.py
  - docs/STRATEGY_CATALOG.md

## Crypto Strategy Research Knowledge Base (Fase 11)
- Funcionalidades: base permanente de pesquisa de estrategias para cripto, com curadoria de fontes publicas, score objetivo, ranking top 20, compatibilidade e recomendacao de 5 candidatas para o proximo lote.
- Comando:
  - python main.py crypto-strategy-research
- Modulos:
  - research/crypto_strategy_knowledge_base/strategies.json
  - research/crypto_strategy_knowledge_base/service.py
  - docs/CRYPTO_STRATEGY_RESEARCH.md

## Scientific Audit of Evaluation Pipeline (Fase 10.2)
- Funcionalidades: auditoria quantitativa permanente do pipeline de avaliacao do catalogo, com matriz de eliminacao, distancia ate aprovacao, benchmark entre contextos e decisao OPCAO A/B baseada em dados.
- Comando:
  - python main.py strategy-catalog-audit
- Modulos:
  - strategy_catalog/audit.py
  - strategy_catalog/catalog.py
  - strategy_catalog/service.py
  - main.py

## Scientific Strategy Catalog Cycle (Fase 10)
- Funcionalidades: catalogo permanente com 10 estrategias classicas, avaliacao em funil (smoke -> backtest -> eliminacao -> otimizacao top 3-5 -> validacao -> paper), ranking consolidado e top 3 para campanha paper.
- Comando:
  - python main.py strategy-catalog-cycle
- Modulos:
  - strategy_catalog/catalog.py
  - strategy_catalog/service.py
  - strategies/classic_catalog_strategies.py
  - docs/STRATEGY_CATALOG.md

## First Controlled Operational Improvement (Fase 9.4)
- Funcionalidades: comparacao automatica e controlada V1.0 vs V1.1 com mudanca exclusiva de saida (time stop), decisao objetiva de aprovacao/reversao e campanha Paper Trading automatica quando aprovado.
- Comando:
  - python main.py phase9-4-controlled-improvement
- Modulos:
  - phase94_controlled_improvement.py
  - strategies/trade_outcome_nextgen_v1_1.py
  - backtesting/engine.py
  - paper_trading/paper_trader.py

## Trade Lifecycle Audit (Fase 9.3)
- Funcionalidades: auditoria permanente do ciclo de vida das posicoes, diagnostico quantitativo do gargalo principal (entrada/score/risk/posicao/saida), simulacoes comparativas, recomendacao priorizada.
- Comando:
  - python main.py trade-lifecycle-audit
- Modulo:
  - trade_lifecycle_audit.py

## Operational Strategy Diagnostics (Fase 9.2)
- Funcionalidades: auditoria permanente da cadeia operacional da estrategia, ranking de bloqueios, heatmap por ativo/timeframe e decisao quantitativa OPCAO A/B.
- Comando:
  - python main.py strategy-diagnostics
- Artefatos:
  - strategy_diagnostics_*.json
  - strategy_diagnostics_*.csv
  - strategy_diagnostics_*.md
  - strategy_diagnostics_*.heatmap.csv
- Dependencias:
  - strategy_diagnostics.py
  - paper_trading/paper_live_service.py
  - paper_trading/paper_trader.py
  - database/history_service.py
  - database/history_models.py

## Market Data
- Funcionalidades: download historico, expansao da base, auditoria de cobertura.
- Comandos: python main.py download, python main.py market-data-expansion
- Artefatos: inventario e relatarios em optimization/results
- Dependencias: exchange/, database/, research/services/market_data_expansion.py

## Discovery
- Funcionalidades: catalogo de familias e recomendacao da proxima familia.
- Comandos: python main.py strategy-discovery
- Artefatos: CSV/JSON de auditoria e ranking em optimization/results
- Dependencias: research/services/strategy_discovery_pipeline.py, database/history_models.py

## Quantitative Lab
- Funcionalidades: clusterizacao e hipotese quantitativa em larga escala.
- Comandos: integrado via pipeline da plataforma
- Artefatos: quantitative_discovery_*.json/csv/md e chunks de eventos
- Dependencias: research/services/quantitative_discovery_lab.py

## Strategy Research Lab
- Funcionalidades: autopsia quantitativa de estrategias e hipoteses.
- Comandos: python main.py strategy-research-lab
- Artefatos: strategy_research_*.csv/json/md
- Dependencias: research/services/strategy_research_lab.py

## Trade Management Lab
- Funcionalidades: replay de entradas e comparacao de cenarios de gestao.
- Comandos: python main.py trade-management-research-lab
- Artefatos: trade_management_*.csv/json/md
- Dependencias: research/services/trade_management_research_lab.py

## Validation
- Funcionalidades: validacao estatistica de candidatos de otimizacao.
- Comandos: python main.py validate
- Artefatos: validation_* em optimization/results e tabela validation_runs
- Dependencias: validation/, database/history_models.py

## Scientific Robustness Validation
- Funcionalidades: validacao cientifica Train/Validation/Test, robustez por regime/ativo/periodo, eliminacao de regras triviais, Scientific Robustness Score, dataset audit e guardrails de adequacao de corpus com bloqueio inconclusivo.
- Comandos: python main.py robustness-validation
- Artefatos: scientific_robustness_validation_*.json/csv/md e tabela scientific_robustness_validation_runs
- Dependencias: research/services/scientific_robustness_validation.py, utils/metrics.py, database/history_repositories.py

## Trade Outcome Learning Lab
- Funcionalidades: descoberta supervisionada de decisoes operacionais, targets configuraveis, explicabilidade, robustez e Trade Outcome Score com aprovacao/reprovacao automatica.
- Comandos: python main.py trade-outcome-learning
- Artefatos: trade_outcome_learning_*.json/csv/md e tabela trade_outcome_learning_runs
- Dependencias: research/labs/trade_outcome_learning_lab.py, utils/metrics.py, database/history_repositories.py

## Execution Framework Optimization (Fase 9.0)
- Funcionalidades: auditoria de complexidade das estrategias, pre-processamento permanente de dataset, cache reutilizavel, benchmark de equivalencia/performance e relancamento automatico da Fase 9.
- Comandos: python main.py execution-framework-optimization
- Artefatos: execution_framework_optimization_*.json/csv/md e tabela execution_framework_optimization_runs
- Dependencias: research/services/execution_framework_optimization.py, strategies/base_strategy.py, backtesting/engine.py, optimizer/optimizer.py

## Controlled Implementation Audit (Fase 9)
- Funcionalidades: traducao fiel do candidato aprovado para estrategia permanente, auditoria de fidelidade, comparacao esperado x observado, execucao automatica de optimizer/validation/labs e decisao final OPCAO A/B.
- Comandos: python main.py phase9-controlled-implementation
- Artefatos: trade_outcome_controlled_implementation_*.json/csv/md e tabela trade_outcome_implementation_runs
- Dependencias: strategies/trade_outcome_nextgen_v1.py, research/services/trade_outcome_controlled_implementation.py, validation/, optimizer/, database/history_repositories.py

## Continuous Paper Operation (Fase 9.1)
- Funcionalidades: operacao paper-live continua com resume/checkpoint, strategy version manager, comparacao automatica entre versoes e relatorios por operacao/horario/diario/semanal/mensal.
- Comandos:
  - python main.py paper-live
  - python main.py paper-operational-report
  - python main.py strategy-version-compare
- Artefatos:
  - paper_live_state.json
  - paper_live_*_operation_report_*.json/md
  - paper_live_*_hourly_report_*.json
  - paper_live_*_weekly_report_*.json
  - paper_live_*_monthly_report_*.json
  - paper_live_daily_*.json/md/operations.csv
- Dependencias:
  - paper_trading/paper_live_service.py
  - paper_trading/paper_trader.py
  - paper_trading/daily_report.py
  - database/session_models.py
  - database/history_models.py

## Optimizer
- Funcionalidades: busca de parametros e ranking de combinacoes.
- Comandos: python main.py optimize
- Artefatos: optimization/results e optimization_results_history
- Dependencias: optimizer/, database/

## Backtesting
- Funcionalidades: simulacao de estrategia e relatorios.
- Comandos: python main.py backtest
- Artefatos: relatorios de backtest
- Dependencias: backtesting/, strategies/

## Dashboard/API
- Funcionalidades: APIs e visao operacional.
- Comandos: python main.py api
- Artefatos: endpoints webapi
- Dependencias: webapi/, frontend/

## Utilities
- Funcionalidades: metricas compartilhadas, logging, validadores.
- Comandos: usados internamente
- Artefatos: logs e utilitarios de suporte
- Dependencias: utils/, logging_service/

## Commands (Entry Point)
- Arquivo central: main.py
- Comandos principais:
  - download
  - market-data-expansion
  - backtest
  - paper
  - optimize
  - validate
  - strategy-research-lab
  - trade-management-research-lab
  - strategy-discovery
  - robustness-validation
  - trade-outcome-learning
  - execution-framework-optimization
  - phase9-controlled-implementation
  - paper-live
  - paper-operational-report
  - strategy-version-compare
  - strategy-catalog-cycle
  - strategy-catalog-audit
  - crypto-strategy-research
  - supertrend-controlled-implementation
  - execution-manager
  - execution-manager-rc1
  - api
