# Execution Manager

## Objetivo

Substituir o Night Runner por uma camada profissional de orquestracao, resiliente e observavel para execucoes longas (10h a 48h+) sem alterar os motores de negocio.

## Escopo

- Control plane no pacote `execution_manager/`
- Fila persistente de jobs
- Heartbeat periodico
- Watchdog com alerta/critico
- Recuperacao com validacao de checkpoint
- Relatorios continuos
- Endpoints REST e WebSocket para acompanhamento operacional

## Entrypoint

Executar:

`python main.py execution-manager`

Campanha Fase 2 (requisito operacional):

`python main.py execution-manager --pipeline pipelines/research_phase2.yaml`

Arquivo de pipeline:

- `pipelines/research_phase2.yaml`

Status atual da campanha:

- Etapa 1 interrompida por cancelamento (`KeyboardInterrupt`) na execucao `20db298e-b348-458b-a066-bdcca6915010`.
- Relatorio de interrupcao: `optimization/results/research_phase2_stage1_report.txt`

## Persistencia de estado

- `optimization/results/execution_jobs_queue.json`
- `optimization/results/execution_state.json`
- `optimization/results/execution_heartbeat.json`
- `optimization/results/execution_report.json`
- `optimization/results/execution_report.txt`
- `optimization/results/execution_report.html`

## Logs dedicados

- `logs/execution/execution_manager.log`
- `logs/execution/heartbeat.log`
- `logs/execution/watchdog.log`
- `logs/execution/progress.log`
- `logs/execution/performance.log`
- `logs/execution/jobs.log`
- `logs/execution/scheduler.log`
- `logs/execution/recovery.log`
- `logs/execution/errors.log`

## Incidentes (bundle completo)

Quando ocorre falha ou watchdog critico:

- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/incident.json`
- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/stacktrace.txt`
- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/environment.txt`
- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/execution_state.json`
- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/jobs_state.json`
- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/checkpoint_reference.txt`
- `optimization/results/incidents/INC_YYYYMMDD_HHMMSS/logs.zip`

## Estados de fila

- Waiting
- Running
- Paused
- Completed
- Failed
- Cancelled
- Recovered
- Retried

## Estados de execucao

- CREATED
- STARTING
- RUNNING
- CHECKPOINTING
- FINALIZING
- COMPLETED
- FAILED
- RECOVERING
- STALLED
- ABANDONED

## API

- GET `/api/v1/execution`
- GET `/api/v1/execution/status`
- GET `/api/v1/execution/jobs`
- GET `/api/v1/execution/progress`
- GET `/api/v1/execution/performance`
- GET `/api/v1/execution/heartbeat`
- GET `/api/v1/execution/watchdog`
- GET `/api/v1/execution/report`
- GET `/api/v1/execution/incidents`
- POST `/api/v1/execution/pause`
- POST `/api/v1/execution/resume`
- POST `/api/v1/execution/cancel`
- POST `/api/v1/execution/retry`
- GET `/api/v1/notifications/telegram/status`
- POST `/api/v1/notifications/telegram/command`

## WebSockets

- `/api/v1/ws/execution`
- `/api/v1/ws/progress`
- `/api/v1/ws/jobs`
- `/api/v1/ws/performance`
- `/api/v1/ws/heartbeat`

## Frontend

Area dedicada adicionada no frontend:

- `/execution-manager`
- `/execution-manager/heartbeat`
- `/execution-manager/watchdog`
- `/execution-manager/incidents`

## Execution Replay

APIs:

- `/api/v1/execution/{execution_id}`
- `/api/v1/execution/{execution_id}/timeline`
- `/api/v1/execution/{execution_id}/jobs`
- `/api/v1/execution/{execution_id}/metrics`
- `/api/v1/execution/{execution_id}/artifacts`

Frontend:

- `/execution-manager/replay`

## Execution Metrics

Tabela `execution_metrics` persistida automaticamente ao final de cada execucao com:

- tempo total
- jobs totais/falhos
- throughput (combinacoes por segundo)
- media por combinacao
- cpu/ram/disco medio e maximo
- checkpoints/heartbeats/incidentes/retries

Frontend:

- `/execution-manager/performance`
- `/execution-manager/comparison`

## RC1 Real Validation

Comando:

- `python main.py execution-manager-rc1`

Artefatos:

- `optimization/results/validation_execution_500.txt`
- `optimization/results/failure_recovery_report.txt`
- `optimization/results/execution_validation_report.{txt,json,html,pdf}`
- `optimization/results/release_candidate_rc1.json`

## Observacoes

- A implementacao atual respeita a regra critica: nao altera optimizer/strategy/risk/backtest/research/dashboard existente.
- O Execution Manager atua como camada de orquestracao, observabilidade e recuperacao.
- O monitoramento Telegram opera de forma desacoplada via NotificationService + EventBus listener.
- Watchdog agora detecta no-progress e heartbeat stall, com recuperacao automatica.

## Fluxo de Execucao Real

Fluxo validado em execucao real (sem mock no caminho principal do optimizer):

1. Download real por simbolo/timeframe via cliente Binance e DataDownloader.
2. Stage de optimizer chama StrategyOptimizer real.
3. BacktestEngine e RiskManager reais sao executados por combinacao.
4. Eventos de optimizer alimentam persistencia historica (runs/resultados/checkpoints).
5. Heartbeat e estado operacional sao atualizados continuamente.
6. Relatorios operacionais sao materializados em `optimization/results`.

Evidencias observadas durante a validacao:

- `optimization/results/execution_jobs_queue.json` com jobs de download concluindo (`downloaded=6726`) e optimizer em andamento.
- `optimization/results/execution_heartbeat.json` com CPU/RAM/disco e heartbeat recente.
- `optimization/results/execution_state.json` com `status=Running`, progresso e ETA.
- Banco MySQL com incremento de entidades reais de execucao e historico.

## Arquitetura Multiprocess Safe

Logging operacional do Execution Manager e dos workers foi consolidado com arquitetura de fila centralizada:

- `SafeQueueHandler` nos produtores
- `QueueListener` unico para escrita em disco
- bind explicito de fila compartilhada em workers de optimizer

Resultado operacional:

- Remocao do ponto de falha de rollover concorrente em Windows.
- Reducao de risco de interrupcao por `PermissionError [WinError 32]` em execucoes paralelas longas.

Artefatos de validacao:

- `optimization/results/logging_audit.txt`
- `optimization/results/logging_stress_test.txt`
- `optimization/results/logging_pytest_output.txt`
- `optimization/results/logging_performance.txt`
