# Telegram Monitoring

Modulo desacoplado para monitoramento e comandos operacionais via Telegram.

## Configuracao (.env)

- TELEGRAM_ENABLED=false
- TELEGRAM_BOT_TOKEN=
- TELEGRAM_CHAT_ID=
- TELEGRAM_ADMIN_CHAT_ID=
- TELEGRAM_SEND_PROGRESS=true
- TELEGRAM_PROGRESS_INTERVAL_MINUTES=30
- TELEGRAM_SEND_ERRORS=true
- TELEGRAM_SEND_SUCCESS=true
- TELEGRAM_SEND_REPORTS=true

## Arquitetura

- notifications/telegram_service.py: envio HTTP para Telegram Bot API.
- notifications/telegram_templates.py: templates de mensagens.
- notifications/telegram_commands.py: comandos administrativos.
- notifications/telegram_scheduler.py: relatorio periodico.
- notifications/telegram_router.py: roteamento de comandos.
- notifications/telegram_repository.py: persistencia e status.
- notifications/telegram_listener.py: integracao com EventBus.
- notifications/notification_service.py: fila central e orquestracao.

## Eventos monitorados

Eventos do optimizer via EventBus:

- optimizer_started
- optimizer_finished
- checkpoint
- combination_finished

Eventos criticos podem ser publicados pelo NotificationService para admin chat.

## Endpoints

- GET /api/v1/notifications/telegram/status
- POST /api/v1/notifications/telegram/command

## Comandos suportados

- /start
- /help
- /status
- /health
- /execution
- /progress
- /ranking
- /metrics
- /incidents
- /logs
- /checkpoints
- /artifacts
- /version

## Seguranca

- Tokens nunca sao logados.
- Comandos administrativos exigem chat_id autorizado.
- Modulo desabilitado por padrao (TELEGRAM_ENABLED=false).
