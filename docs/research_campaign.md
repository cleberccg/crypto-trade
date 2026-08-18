# Research Campaign - Phase 2

## Objetivo
Transicionar a plataforma de foco em infraestrutura para foco em pesquisa quantitativa, usando Execution Manager + Optimizer real e gerando base estatística para o Research Lab.

## Comando de campanha (requisito)
`python main.py execution-manager --pipeline pipelines/research_phase2.yaml`

## Status atual
- Pipeline declarativo criado: `pipelines/research_phase2.yaml`
- Etapa 1 interrompida por erro operacional (execucao cancelada por KeyboardInterrupt)
- Campanha completa bloqueada ate rerun completo da Etapa 1

## Etapa 1 (gate)
- Asset/TF: BTC/USDT 5m
- Combinacoes: 10000
- Workers: 16
- Resultado atual: `INTERRUPTED`
- Evidencia: `optimization/results/research_phase2_stage1_report.txt`
- Incidente: `optimization/results/incidents/INC_20260626_222125/incident.json`

## Campanha planejada (Etapa 2)
1. BTC/USDT 5m 10000 workers=16
2. BTC/USDT 15m 10000 workers=16
3. ETH/USDT 5m 10000 workers=16
4. ETH/USDT 15m 10000 workers=16
5. SOL/USDT 5m 10000 workers=16
6. SOL/USDT 15m 10000 workers=16

## Persistencia e consolidacao
Campos-alvo por resultado:
- execution_id
- strategy
- symbol
- timeframe
- workers
- duration_seconds
- profit_factor
- sharpe
- drawdown
- win_rate
- expectancy
- trades
- best_configuration

Consolidados:
- `optimization/results/research_consolidated.csv`
- `optimization/results/research_top100.csv`

## Robustez e aprovacao para paper
Critérios parametrizados por variáveis de ambiente:
- `RESEARCH_MIN_PROFIT_FACTOR` (default 1.5)
- `RESEARCH_MIN_SHARPE` (default 1.0)
- `RESEARCH_MAX_DRAWDOWN` (default via validation config)
- `RESEARCH_MIN_TRADES` (default via validation config)
- `RESEARCH_MAX_OVERFIT_SCORE` (default 0.45)

## Artefatos já gerados (com base em histórico existente)
- `optimization/results/research_dataset.db`
- `optimization/results/research_dataset.csv`
- `optimization/results/research_consolidated.csv`
- `optimization/results/research_top100.csv`
- `optimization/results/research_summary.json`
- `optimization/results/research_summary.txt`
- `optimization/results/research_summary.html`
- `optimization/results/research_summary.pdf`

Observacao importante:
- `research_dataset.parquet` depende de `pyarrow` ou `fastparquet` e atualmente nao foi materializado no ambiente.
