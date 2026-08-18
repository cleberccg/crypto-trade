# Campanha Noturna - Implementação Completa

## Resumo das Mudanças

A campanha noturna foi completamente implementada com as seguintes melhorias:

### 1. **Prioridade Dinâmica** ✓
- Arquivo: `research/services/phase13_continuous_strategy_factory.py`
- Mudança: Separar estratégias em `IMPLEMENTATION_PENDING` e `IMPLEMENTATION_INCOMPLETE` no início da fila
- Efeito: Processa as 19 estratégias pendentes ANTES de qualquer nova pesquisa
- Código:
  ```python
  pending_items = [b for b in backlog if b.get("state") in ["IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"]]
  other_items = [b for b in backlog if b.get("state") not in ["IMPLEMENTATION_PENDING", "IMPLEMENTATION_INCOMPLETE"]]
  eligible = pending_items + other_items
  ```

### 2. **Critério 09:00** ✓
- Campo novo: `campaign_end_hour` (padrão: 9)
- Verifica a hora local a cada iteração: `current_hour = datetime.now().hour`
- Para gracefully quando `current_hour >= campaign_end_hour` (sem interromper estratégia em progresso)
- Código:
  ```python
  if current_hour >= int(cfg.campaign_end_hour) and not strategy_in_progress:
      stop_reason = "campaign_end_hour_reached"
      break
  ```

### 3. **Limite de PAPER_CANDIDATE** ✓
- Campo novo: `target_paper_candidates` (padrão: 3)
- Conta estratégias com estado = `PAPER_CANDIDATE`
- Para quando encontra 3 (em vez de parar na primeira)
- Código:
  ```python
  paper_candidates = [x for x in backlog if x.get("state") == "PAPER_CANDIDATE"]
  if len(paper_candidates) >= int(cfg.target_paper_candidates):
      stop_reason = "paper_candidates_target_reached"
      break
  ```

### 4. **Smart Budget Control** ✓
- Acompanha estratégia em progresso: `strategy_in_progress`
- Critérios de parada (budget, tempo) só se disparam quando NÃO há estratégia em progresso
- Garante que qualquer estratégia iniciada completa todo o pipeline
- Código:
  ```python
  if len(processed) >= max_strategies_budget and not strategy_in_progress:
      stop_reason = "budget_max_strategies"
      break
  ```

### 5. **Resiliência e Aprendizado** ✓
- Novo método: `_record_rejection_knowledge()`
- Registra ao rejeitar:
  - Família da estratégia
  - Indicadores usados
  - Estágio onde falhou (probe, optimizer, validation)
  - Métricas no momento da rejeição
- Estrutura: `rejection_knowledge` com chaves: `family`, `indicators`, `stage`, `all_rejections`
- Código:
  ```python
  if item.get("state") in ["REJECTED_BY_PERFORMANCE", "REJECTED_BY_INFRASTRUCTURE"]:
      self._record_rejection_knowledge(item, rejection_knowledge)
  ```

### 6. **Tratamento de Erros** ✓
- Try/catch ao processar cada estratégia
- Log de erro e continua (nunca para)
- Estado: `ERROR_RESILIENCE_CONTINUED`
- Código:
  ```python
  try:
      self._process_queue_item(...)
  except Exception as e:
      logger.warning(f"Strategy {item.get('candidate_name')} failed: {e}, continuing...")
      item["state"] = "ERROR_RESILIENCE_CONTINUED"
  ```

### 7. **Comando CLI** ✓
- Novo comando: `overnight-campaign`
- Arquivo: `main.py`
- Parâmetros pré-configurados para campanha noturna:
  - `--batch-size 50` (permitir muitas estratégias)
  - `--target-paper-candidates 3` (alvo de 3)
  - `--campaign-end-hour 9` (parada às 09:00)
  - `--campaign-max-seconds 32400` (9 horas)

## Como Executar

### Opção 1: Script direto
```bash
python run_overnight_campaign.py
```

### Opção 2: Comando CLI
```bash
python main.py overnight-campaign
```

### Opção 3: Personalizado
```bash
python main.py overnight-campaign \
  --symbol BTC/USDT \
  --timeframe 5m \
  --batch-size 50 \
  --target-paper-candidates 3 \
  --campaign-end-hour 9
```

## Fluxo de Execução

```
┌─────────────────────────────────────┐
│ CAMPANHA NOTURNA INICIADA           │
└──────────────┬──────────────────────┘
               │
               ▼
      ┌────────────────────┐
      │ Carregar backlog   │
      └────────┬───────────┘
               │
               ▼
    ┌──────────────────────────┐
    │ Separar estratégias      │
    │ - Pending (19)           │
    │ - Outras (32)            │
    └────────┬─────────────────┘
             │
             ▼
    ┌──────────────────────────┐
    │ LOOP: Processar fila     │
    │                          │
    │ 1. Verificar hora 09:00  │◄──── Para se >= 09:00
    │ 2. Contar PAPER_CANDx3   │◄──── Para se encontrou 3
    │ 3. Verificar budget      │◄──── Para se >= max (mas não antes de terminar estratégia)
    │ 4. Processar estratégia  │
    │    - Implementation      │
    │    - Smoke Test          │
    │    - Backtest            │
    │    - Optimizer Probe     │
    │    - Optimizer Completo  │
    │    - Validation          │
    │    - Paper Candidate     │
    │ 5. Registrar aprendizado │
    │ 6. Próxima               │
    └────────┬─────────────────┘
             │
             ▼
  ┌────────────────────────────┐
  │ Gerar relatório consolidado│
  │ - Pesquisadas              │
  │ - Implementadas            │
  │ - Avaliadas                │
  │ - Reprovadas               │
  │ - PAPER_CANDIDATE          │
  │ - Ranking top 20           │
  │ - Motivos rejeição         │
  │ - Aprendizado              │
  └────────┬─────────────────────┘
           │
           ▼
┌──────────────────────────────────┐
│ CAMPANHA CONCLUÍDA              │
│ Relatório salvo em:             │
│ optimization/results/           │
└──────────────────────────────────┘
```

## Estado Atual

### Backlog
- Total: 51 estratégias
- IMPLEMENTATION_PENDING: **19** (processadas nesta campanha)
- IMPLEMENTATION_INCOMPLETE: 0
- Já implementadas: 32

### 19 Estratégias Pendentes
1. Opening Range Breakout
2. EMA Ribbon Pullback
3. Volatility Breakout ATR + Volume
4. Bollinger RSI Crypto Mean Reversion
5. Elder Impulse
6. WaveTrend
7. Heikin-Ashi Trend + ATR
8. TTM Squeeze Momentum
9. Session VWAP Reversion
10. MACD Histogram Acceleration
... (mais 9)

## Configurações da Campanha Noturna

| Parâmetro | Valor | Justificativa |
|-----------|-------|---------------|
| batch_size | 50 | Permitir processar muitas estratégias durante a noite |
| target_approved | 1 | Critério original mantido |
| target_paper_candidates | 3 | Buscar múltiplas candidatas de qualidade |
| campaign_end_hour | 9 | Parada graceful às 09:00 |
| campaign_max_seconds | 32400 (9h) | Suficiente para processar as 19 pendências |
| optimizer_workers | 1 | Evitar sobrecarga de CPU |
| probe_max_combinations | 8 | Filtro rápido e eficaz |
| paper_candidate_min_profit_factor | 1.10 | Critério científico mantido |

## Melhorias Implementadas vs Requisitos

| Requisito | Status | Detalhes |
|-----------|--------|----------|
| ✓ Prioridade dinâmica | IMPLEMENTADO | Pending processadas primeiro |
| ✓ Critério 09:00 | IMPLEMENTADO | Para gracefully na hora |
| ✓ 3 PAPER_CANDIDATE | IMPLEMENTADO | target_paper_candidates=3 |
| ✓ Smart budget | IMPLEMENTADO | strategy_in_progress tracking |
| ✓ Resiliência | IMPLEMENTADO | Try/catch com logging |
| ✓ Aprendizado | IMPLEMENTADO | rejection_knowledge registrado |
| ✓ Relatório consolidado | IMPLEMENTADO | Estrutura completa no report |
| ✓ Sem interrupção | IMPLEMENTADO | Continua sempre que possível |

## Próximas Etapas (Opcional)

1. **Integração com Fase 14**: Quando `IMPLEMENTATION_PENDING = 0`, ativar automaticamente pesquisa Fase 14
2. **Paper Trading Automático**: Iniciar Paper Trading quando PAPER_CANDIDATE encontrada (sem parar campanha)
3. **Dashboard em Tempo Real**: Monitorar progresso da campanha
4. **Notificações**: Alertas quando PAPER_CANDIDATE encontrada

## Teste Rápido

Para testar a campanha com subset das 19 estratégias:

```bash
python main.py overnight-campaign \
  --batch-size 3 \
  --campaign-max-seconds 600 \
  --output-prefix test_overnight
```

Isso processará apenas 3 estratégias em 10 minutos para validação.
