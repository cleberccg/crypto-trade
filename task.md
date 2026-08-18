# TAREFA CRÍTICA — Eliminar o Gargalo de Logging Multiprocess no Windows

## CONTEXTO

A auditoria da execução real identificou o principal bloqueador da plataforma.

Erro observado:

PermissionError [WinError 32]

Arquivo envolvido:

risk.risk_manager.log

A plataforma já executa:

* múltiplos workers;
* multiprocessing;
* Execution Manager;
* Heartbeat;
* Watchdog;
* Progress Tracker.

Entretanto, o sistema de logging ainda utiliza uma abordagem incompatível com escrita concorrente em Windows.

O objetivo desta tarefa é tornar o logging totalmente seguro para execução multiprocess.

---

# PRIORIDADE

Esta tarefa possui prioridade máxima.

Nenhuma nova funcionalidade deverá ser desenvolvida antes da conclusão desta correção.

A plataforma somente poderá ser considerada pronta para execuções longas após eliminar completamente este problema.

---

# ETAPA 1 — Auditoria

Localizar TODOS os pontos onde exista:

FileHandler

RotatingFileHandler

TimedRotatingFileHandler

basicConfig

logging.basicConfig

logging.FileHandler

logging.handlers

Logger singleton compartilhado

Gerar:

logging_audit.txt

Informar:

Arquivo

Classe

Função

Linha

Tipo de Handler

---

# ETAPA 2 — Arquitetura

Substituir o modelo atual por arquitetura multiprocess-safe.

Utilizar:

QueueHandler

QueueListener

Logging Queue

Worker Queue

Log Dispatcher

Todos os processos deverão enviar mensagens para uma fila.

Somente um processo deverá escrever em disco.

Nunca permitir que vários processos gravem diretamente no mesmo arquivo.

---

# ETAPA 3 — Logging Service

Criar:

logging_service/

logger_manager.py

queue_logger.py

listener.py

handlers.py

formatter.py

logging_config.py

shutdown.py

Toda a plataforma deverá utilizar este serviço.

---

# ETAPA 4 — Centralização

Execution Manager

Optimizer

Risk Manager

Research

Scheduler

Jobs

Recovery

Watchdog

Heartbeat

Dashboard

Todos deverão utilizar o mesmo Logging Service.

Nenhum módulo poderá criar FileHandler diretamente.

---

# ETAPA 5 — Rotação

Implementar rotação segura.

Nunca trocar arquivos enquanto outro processo escreve.

Permitir:

compressão

retenção

limpeza

rotação por tamanho

rotação por data

Sem gerar WinError 32.

---

# ETAPA 6 — Shutdown

Ao finalizar:

Flush.

Fechar fila.

Fechar Listener.

Fechar Handlers.

Garantir encerramento limpo.

---

# ETAPA 7 — Testes

Criar testes com:

2 workers

4 workers

8 workers

16 workers

32 workers

Todos escrevendo simultaneamente.

Executar milhares de mensagens.

Validar:

Nenhum PermissionError.

Nenhuma perda de logs.

Nenhuma duplicação.

Nenhuma corrupção.

---

# ETAPA 8 — Stress Test

Executar:

Optimizer

Heartbeat

Watchdog

Progress

Research

Todos produzindo logs simultaneamente.

Gerar:

logging_stress_test.txt

---

# ETAPA 9 — Performance

Medir:

Mensagens por segundo.

Latência.

Tempo de Flush.

Tempo de Rotação.

Uso de CPU.

Uso de RAM.

---

# ETAPA 10 — Dashboard

Adicionar:

Logging Status

Queue Size

Listener

Workers

Arquivos ativos

Mensagens/s

Falhas

Última rotação

---

# ETAPA 11 — APIs

Criar:

GET /api/v1/logging/status

GET /api/v1/logging/performance

GET /api/v1/logging/queue

GET /api/v1/logging/listener

GET /api/v1/logging/files

---

# ETAPA 12 — Incidentes

Caso ocorra qualquer falha de logging:

Nunca interromper o Optimizer.

Registrar em memória.

Persistir posteriormente.

Logging nunca poderá derrubar a execução.

---

# ETAPA 13 — Compatibilidade

Garantir funcionamento em:

Windows 11

Linux

WSL

Docker

Sem alterações de código.

---

# ETAPA 14 — Critérios de Aprovação

Executar uma otimização real utilizando múltiplos workers.

Confirmar:

Zero PermissionError.

Zero WinError 32.

Zero perda de logs.

Zero corrupção.

Zero travamento causado pelo logging.

Execution Manager funcionando normalmente.

---

# DOCUMENTAÇÃO

Atualizar:

architecture.md

logging.md

execution_manager.md

task_details.txt

checklist.md

Adicionar:

"Arquitetura Multiprocess Safe"

---

# OBJETIVO FINAL

A plataforma somente poderá ser considerada pronta para otimizações longas quando o sistema de logging suportar escrita concorrente de dezenas de processos sem gerar bloqueios, corrupção de arquivos ou interrupções da execução.

O logging deve deixar de ser um ponto único de falha e passar a ser um serviço centralizado, resiliente e compatível com multiprocessing no Windows.
