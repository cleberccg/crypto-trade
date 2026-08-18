# Notification Service

Servico central de notificacoes da plataforma.

## Objetivo

Fornecer envio desacoplado de notificacoes sem dependencia direta de estrategias, optimizer ou risk manager.

## Fluxo

1. EventBus publica evento de lifecycle.
2. TelegramEventListener converte evento em notificacao.
3. NotificationService enfileira mensagem.
4. Worker assíncrono envia para Telegram.
5. Resultado persiste em notification_history.

## Persistencia

Tabela: notification_history

Campos principais:

- notification_type
- title
- message
- execution_id
- status
- destination
- channel
- delivery_ms
- error_message
- created_at

## Relatorio periodico

Scheduler envia progresso em intervalo configuravel por TELEGRAM_PROGRESS_INTERVAL_MINUTES.

## Observabilidade

Dashboard e API exibem:

- online/offline
- ultimo envio
- mensagens enviadas
- mensagens com erro
- tamanho da fila
