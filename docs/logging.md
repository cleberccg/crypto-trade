# Arquitetura Multiprocess Safe

## Objetivo
Eliminar contenção de logging em multiprocessing (Windows/Linux/WSL/Docker) e remover logging como ponto único de falha.

## Arquitetura
- Todos os processos usam QueueHandler (`SafeQueueHandler`) para enviar logs para uma fila compartilhada.
- Apenas o processo principal mantém `QueueListener` com handlers de disco.
- Workers não escrevem diretamente em arquivo.

## Componentes
- `logging_service/logging_config.py`: configuração central
- `logging_service/formatter.py`: formato padronizado
- `logging_service/handlers.py`: handlers de rotação (tamanho e opcionalmente data)
- `logging_service/queue_logger.py`: `SafeQueueHandler` com proteção a overflow
- `logging_service/listener.py`: `QueueListener` central
- `logging_service/logger_manager.py`: bootstrap, binding de workers, status/performance
- `logging_service/shutdown.py`: fechamento limpo

## Fluxo
1. Processo principal inicializa serviço (`initialize_logging_service`).
2. `QueueListener` inicia e abre handlers de disco.
3. Módulos chamam `get_logger`, recebendo logger com QueueHandler.
4. Workers de optimizer recebem `bind_worker_logging_queue(queue)` no initializer.
5. No shutdown: flush + stop listener + close handlers.

## Endpoints
- `GET /api/v1/logging/status`
- `GET /api/v1/logging/performance`
- `GET /api/v1/logging/queue`
- `GET /api/v1/logging/listener`
- `GET /api/v1/logging/files`

## Resiliência
- Overflow de fila não derruba execução; incrementa contador de dropped/errors.
- Falhas de logging devem ser reportadas por métricas e não interromper optimizer.

## Windows Rotation Hardening

- Rotating handlers now use safe wrappers that catch rollover failures.
- Gzip rotator retries with small backoff on transient file lock contention.
- If file remains locked, rotator degrades gracefully (fallback copy + truncate) and continues.
- Logging failures no longer block optimizer/execution pipeline.

## Validação
- Matriz de workers em `tests/test_logging_multiprocess.py` (2/4/8/16/32).
- Verificação de endpoints em `tests/test_logging_api.py`.
- Auditoria de handlers em `optimization/results/logging_audit.txt`.
