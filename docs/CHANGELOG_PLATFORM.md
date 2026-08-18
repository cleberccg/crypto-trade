# CHANGELOG_PLATFORM

## 2026-06-30 - Fase 12: Implementacao Controlada da SuperTrend
- adiciona comando oficial `python main.py supertrend-controlled-implementation`
- adiciona estrategia permanente `SuperTrendV1` com ATR period, ATR multiplier, trend confirmation, stop ATR, take profit e risk/reward
- adiciona modulo permanente `phase12_supertrend_controlled.py` para pipeline em etapas:
  - smoke test
  - backtest padronizado
  - early stop de inviabilidade
  - optimizer reduzido
  - validation
  - qualificacao para campanha paper quando aprovada
- adiciona grade de parametros dedicada no `optimizer/parameter_grid.py` para `SuperTrendV1`
- persiste artefatos em JSON/CSV/Markdown e checkpoint em banco
- Arquivos alterados:
  - main.py
  - strategies/supertrend_v1.py (novo)
  - strategies/__init__.py
  - phase12_supertrend_controlled.py (novo)
  - optimizer/parameter_grid.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/STRATEGY_CATALOG.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 - Fase 11: Pesquisa e Curadoria Cientifica de Estrategias para Cripto
- adiciona comando oficial `python main.py crypto-strategy-research`
- adiciona base permanente de conhecimento em `research/crypto_strategy_knowledge_base/`
- adiciona dataset curado de estrategias com metadados tecnicos, origem e compatibilidade
- adiciona ranking objetivo com prioridade Alta/Media/Baixa e top 20
- adiciona selecao automatica de 5 estrategias recomendadas para o proximo lote
- gera artefatos em JSON/CSV/Markdown e documentacao permanente em `docs/CRYPTO_STRATEGY_RESEARCH.md`
- Arquivos alterados:
  - main.py
  - research/crypto_strategy_knowledge_base/strategies.json (novo)
  - research/crypto_strategy_knowledge_base/service.py (novo)
  - research/crypto_strategy_knowledge_base/__init__.py (novo)
  - docs/CRYPTO_STRATEGY_RESEARCH.md
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 - Fase 10.2: Auditoria Cientifica do Pipeline de Avaliacao
- adiciona comando oficial `python main.py strategy-catalog-audit`
- adiciona modulo permanente `strategy_catalog/audit.py`
- implementa auditoria quantitativa completa para estrategias do catalogo sem alterar regras de estrategia:
  - matriz comparativa de metricas (PF, Sharpe, Expectancy, Net Profit, Drawdown, Win Rate, trades, Recovery Factor, Robustez, Stability, Implementability)
  - analise criterio a criterio com motivo principal de reprovacao
  - calculo de distancia percentual ate aprovacao por criterio
  - matriz de eliminacao por etapa (Smoke, Backtest, Optimizer, Validation, Scientific Validation, Paper Qualification)
  - distribuicao de resultados por faixas de PF/Sharpe/Expectancy/Drawdown
  - benchmark reduzido por contexto (BTC/USDT e ETH/USDT em 5m/15m/1h)
  - classificacao final em grupos A/B/C e decisao automatica OPCAO A/B
- persiste artefatos em JSON/CSV/Markdown e checkpoint em banco
- Arquivos alterados:
  - main.py
  - strategy_catalog/audit.py (novo)
  - strategy_catalog/__init__.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 - Fase 10: Catalogo Cientifico Permanente de Estrategias
- adiciona comando oficial `python main.py strategy-catalog-cycle`
- adiciona pacote permanente `strategy_catalog` com:
  - `strategy_catalog/catalog.py` (metadados permanentes por estrategia)
  - `strategy_catalog/service.py` (orquestracao do ciclo cientifico)
  - `strategy_catalog/__init__.py`
- adiciona lote inicial com 10 estrategias classicas em `strategies/classic_catalog_strategies.py`
- aplica fluxo em etapas para reduzir custo computacional:
  1. Smoke test em todas
  2. Backtest padronizado em todas
  3. Eliminacao imediata das claramente ruins
  4. Otimizacao apenas nas top 3-5
  5. Validacao apenas da shortlist otimizada
  6. Paper Trading apenas para aprovadas
- adiciona classificacao permanente no catalogo:
  - Status (Implementada -> Smoke Test -> Backtest -> Otimizada -> Validada -> Paper Trading -> Producao -> Rejeitada)
  - Origem (Livro, Paper, Open Source, Descoberta da Plataforma)
  - Familia (Tendencia, Reversao, Breakout, Momentum, Volatilidade)
- adiciona geracao/atualizacao automatica de `docs/STRATEGY_CATALOG.md`
- registra backlog de segundo lote com estrategia SuperTrend (nao implementada nesta fase)
- Arquivos alterados:
  - main.py
  - strategies/classic_catalog_strategies.py (novo)
  - strategies/__init__.py
  - strategy_catalog/catalog.py (novo)
  - strategy_catalog/service.py (novo)
  - strategy_catalog/__init__.py (novo)
  - docs/STRATEGY_CATALOG.md
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 - Fase 9.4: Primeira Melhoria Operacional Controlada (V1.1)
- adiciona comando oficial `python main.py phase9-4-controlled-improvement`
- adiciona modulo permanente `phase94_controlled_improvement.py` para comparacao automatica V1.0 vs V1.1
- adiciona nova estrategia `TradeOutcomeNextGenV1.1` com mesma entrada da V1.0 e mudanca apenas de saida (time stop)
- adiciona suporte generico a `time_stop` no `backtesting/engine.py` e `paper_trading/paper_trader.py`
- implementa criterio de aprovacao objetiva: PF, Expectancy, Net Profit, Drawdown, trades e tempo bloqueado
- aplica regra de tentativa unica (sem iteracao automatica para V1.2/V1.3/V2)
- se aprovado, inicia campanha de Paper Trading exclusivamente com V1.1
- persiste artefatos em JSON/CSV/Markdown e checkpoint no banco
- Arquivos alterados:
  - main.py
  - phase94_controlled_improvement.py (novo)
  - strategies/trade_outcome_nextgen_v1_1.py (novo)
  - strategies/__init__.py
  - backtesting/engine.py
  - paper_trading/paper_trader.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/CHANGELOG_PLATFORM.md
  - docs/WORKFLOW_GUIDE.md

## 2026-06-30 - Fase 9.3: Trade Lifecycle Audit
- adiciona comando oficial `python main.py trade-lifecycle-audit`
- adiciona `trade_lifecycle_audit.py` como modulo permanente de auditoria do ciclo de vida das posicoes
- implementa 7 etapas: duracao, motivos de saida, tempo bloqueado, qualidade MFE/MAE, simulacoes, capacidade operacional, diagnostico do gargalo
- responde objetivamente se o gargalo e entrada, score, risk manager, gestao da posicao ou gestao da saida
- simula saidas 25% mais cedo, 50% mais cedo, time stop e saida no pico (MFE) sem alterar a estrategia
- estima quantos setups adicionais seriam aproveitados com posicoes mais curtas
- persiste relatorio completo em JSON, CSV, Markdown e banco de dados
- Arquivos alterados:
  - main.py
  - trade_lifecycle_audit.py (novo)
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/CHANGELOG_PLATFORM.md
  - docs/WORKFLOW_GUIDE.md

## 2026-06-30 - Fase 9.2: Diagnostico Operacional Permanente
- adiciona comando oficial `python main.py strategy-diagnostics`
- adiciona `strategy_diagnostics.py` como modulo permanente de auditoria operacional
- executa diagnostico sobre a ultima sessao Paper Live, ultimas 24h e ultimos 7 dias
- gera ranking de bloqueios, heatmap operacional e decisao quantitativa OPCAO A/B
- persiste o resumo em `execution_sessions` e `execution_checkpoints`

## 2026-06-30
- Funcionalidade criada: Scientific Robustness Validation (Fase 7.1)
- Motivo: validar robustez cientifica de candidatos antes de qualquer implementacao de estrategia e tornar a capacidade permanente da plataforma.
- Impacto:
  - adiciona particionamento temporal Train/Validation/Test sem vazamento
  - adiciona robustez por regime, ativo e periodo
  - adiciona eliminacao automatica de regras triviais
  - adiciona Scientific Robustness Score e criterio formal de aprovacao/reprovacao
  - adiciona persistencia em JSON/CSV/Markdown e banco
- Comandos adicionados:
  - python main.py robustness-validation
- Arquivos alterados:
  - main.py
  - database/history_models.py
  - database/history_repositories.py
  - research/services/__init__.py
  - research/services/scientific_robustness_validation.py
  - tests/test_scientific_robustness_validation.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 (FASE 7.1.1)
- Funcionalidade criada: Guardrails Cientificos + Validacao Completa do Corpus.
- Impacto:
  - adiciona dataset audit automatico (arquivos, eventos, ativos, timeframes, cobertura temporal e ocorrencias por contexto)
  - adiciona classificacao de escopo: FULL_DATASET, REPRESENTATIVE_SAMPLE, LIMITED_SAMPLE, INSUFFICIENT_SAMPLE
  - adiciona bloqueio de decisao A/B com status VALIDACAO_INCONCLUSIVA quando guardrails falham
  - adiciona recomendacao automatica do comando oficial para corpus completo
  - adiciona parametros CLI para limiares minimos de guardrail
- Arquivos alterados:
  - main.py
  - research/services/scientific_robustness_validation.py
  - tests/test_scientific_robustness_validation.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 (FASE 8)
- Funcionalidade criada: Trade Outcome Learning Lab (nova geracao do laboratorio quantitativo).
- Impacto:
  - muda o foco de aprendizado de contexto para aprendizado supervisionado de decisoes operacionais
  - adiciona construcao automatica de dataset de oportunidades com outcomes futuros
  - adiciona alvos multi-target configuraveis
  - adiciona descoberta automatica de regras com explicabilidade
  - adiciona robustez Train/Validation/Test + temporal/ativo/regime/timeframe
  - adiciona Trade Outcome Score como indicador permanente de aprovacao
  - adiciona persistencia em JSON/CSV/Markdown e banco
- Comandos adicionados:
  - python main.py trade-outcome-learning
- Arquivos alterados:
  - main.py
  - database/history_models.py
  - database/history_repositories.py
  - research/labs/__init__.py
  - research/labs/trade_outcome_learning_lab.py
  - docs/WORKFLOW_GUIDE.md
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 (FASE 8.1)
- Execucao cientifica completa do Trade Outcome Learning concluida no corpus integral via comando oficial da plataforma.
- Ajuste permanente aplicado para escalabilidade/reprodutibilidade em base completa:
  - descoberta limitada ao subconjunto temporal de treino com limite interno deterministico (discovery_max_rows)
  - avaliacao e robustez preservadas no corpus completo
  - enriquecimento de auditoria de dataset (distribuicoes por ativo, timeframe e regime)
  - inclusao de scientific_robustness_score e implementability_score no ranking final
- Arquivos alterados:
  - research/labs/trade_outcome_learning_lab.py
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 (FASE 9)
- Funcionalidade criada: Implementacao Controlada do candidato aprovado da Fase 8.1.
- Impacto:
  - adiciona estrategia permanente TradeOutcomeNextGenV1 com traducao fiel da regra aprovada (distance_to_ema_pct<=0.162026)
  - adiciona comando oficial da plataforma para campanha completa de fidelidade/validacao: phase9-controlled-implementation
  - adiciona auditoria automatica de fidelidade (precision, recall, f1, cobertura, intersecao, fp/fn)
  - adiciona comparacao quantitativa esperado x observado (PF, Sharpe, Expectancy, Drawdown)
  - adiciona execucao automatica de optimizer, validation, strategy research lab e trade management lab
  - adiciona persistencia permanente em JSON/CSV/Markdown e banco
- Comandos adicionados:
  - python main.py phase9-controlled-implementation
- Arquivos alterados:
  - strategies/trade_outcome_nextgen_v1.py
  - research/services/trade_outcome_controlled_implementation.py
  - database/history_models.py
  - database/history_repositories.py
  - main.py
  - tests/test_phase9_controlled_implementation.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 (FASE 9.0)
- Funcionalidade criada: Otimizacao permanente do framework de execucao das estrategias.
- Impacto:
  - adiciona API base prepare_dataset/invalidate_prepared_dataset/cache_payload em strategies/base_strategy.py
  - remove recálculo completo de indicadores durante o loop bar a bar do backtest
  - faz o BacktestEngine consumir dataset preparado uma unica vez por execucao
  - adiciona observabilidade de preprocessing, bars/s, ETA e tempo ate primeiro resultado
  - adiciona auditoria de estrategias, benchmark de equivalencia/performance e persistencia permanente
  - relanca automaticamente a campanha oficial da Fase 9 apos a otimizacao
- Comandos adicionados:
  - python main.py execution-framework-optimization
- Arquivos alterados:
  - strategies/base_strategy.py
  - backtesting/engine.py
  - optimizer/optimizer.py
  - validation/validator.py
  - research/services/strategy_research_lab.py
  - research/services/execution_framework_optimization.py
  - database/history_models.py
  - database/history_repositories.py
  - main.py
  - tests/test_execution_framework_optimization.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/CHANGELOG_PLATFORM.md

## 2026-06-30 (FASE 9.1)
- Funcionalidade criada: Operacao continua em paper trading com evolucao baseada em dados reais.
- Impacto:
  - adiciona comando oficial paper-live para execucao continua com interrupcao e retomada
  - adiciona Strategy Version Manager integrado (registro, ativacao, gate de mudanca por minimo de trades, rollback por selecao de versao)
  - adiciona comparacao automatica entre versoes (PF, Sharpe proxy, Expectancy, Drawdown proxy, Win Rate, Net Profit, trades, estabilidade, robustez)
  - adiciona geracao de relatorios por operacao, horario, diario, semanal e mensal
  - reforca persistencia operacional em trade_history/signal_snapshots/portfolio_snapshots/execution_sessions
- Comandos adicionados:
  - python main.py paper-live
  - python main.py paper-operational-report
  - python main.py strategy-version-compare
- Arquivos alterados:
  - main.py
  - paper_trading/paper_trader.py
  - paper_trading/daily_report.py
  - paper_trading/__init__.py
  - docs/PLATFORM_FEATURES.md
  - docs/PLATFORM_MAP.md
  - docs/WORKFLOW_GUIDE.md
  - docs/CHANGELOG_PLATFORM.md
- Arquivos criados:
  - paper_trading/paper_live_service.py
