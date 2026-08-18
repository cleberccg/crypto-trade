# Plano de Prontidão Operacional — Soak 24h e 72h

**Papel**: Release Manager + SRE  
**Baseline de código**: P0 endurecido (hipótese parity, persistência atômica, bounded frames, supervisor com restart de hipótese, DB fail-safe)  
**Restrições**: sem nova funcionalidade, sem refatoração, sem labs/estratégias.

---

## Componentes Operacionais (Existentes — Nada Novo a Implementar)

| Componente | Propósito | Invocação |
|---|---|---|
| `paper-live-supervisor` | Orquestração resiliente multi-contexto com auto-restart | `python main.py paper-live-supervisor --campaign-id <id>` |
| `check_paper_live_status.py` | Status real-time (lag, ciclos, trades, performance, desync) | `python check_paper_live_status.py --show-contexts` |
| `monitor_live_trades.py` | Monitor de log + DB para sinais/ordens/erros em tempo real | `python monitor_live_trades.py` |
| `paper-operational-report` | Gera relatórios hour/daily/weekly/monthly por estratégia | `python main.py paper-operational-report --date YYYY-MM-DD` |
| `paper_campaign_coverage_monitor` | Cobertura de contextos aprovados (ativos/stale/stopped) | Via `main.py paper-campaign-coverage-monitor` |
| `check_campaign_hourly.py` | Watchdog de estagnação de processo e heartbeat | `python check_campaign_hourly.py` |
| `soak_monitor.py` *(novo — 100% read-only)* | Relatório horário consolidado com alertas e status APROVADO/REPROVADO | `python soak_monitor.py --campaign-id <id>` |
| `execution_manager/resource_monitor.py` | Snapshots de CPU/RAM/disk (usado internamente pelo soak_monitor) | — |

---

## FASE 1 — Soak 24h

### Objetivo
Confirmar operação estável ininterrupta por 24 horas: sem reinicializações excessivas, sem crescimento de memória, sem degradação de performance de trade, sem erros persistentes.

### Pré-condições (Verificar Antes de Iniciar)

- [ ] Suite de testes P0 passando: `pytest tests/test_live_execution_cycle.py tests/test_live_trading_service.py tests/test_paper_live_supervisor.py tests/test_paper_live_hardening.py tests/test_operational_hypothesis_executability.py -v` → 45 passed
- [ ] Banco MySQL acessível (testar conexão)
- [ ] Disco com ≥ 10 GB livres
- [ ] RAM disponível ≥ 2 GB
- [ ] Estratégia e versão confirmados (ex: `ClassicDonchianBreakout@v1.0`)
- [ ] campaign_id definido (ex: `spc-official-cdb-v1`)

### Execução

**Terminal 1 — Supervisor (processo principal):**
```bash
python main.py paper-live-supervisor \
  --strategy-name ClassicDonchianBreakout \
  --strategy-version v1.0 \
  --campaign-id spc-official-cdb-v1 \
  --max-consecutive-restarts 5 \
  --stuck-timeout-seconds 600 \
  --startup-grace-seconds 120 \
  --supervisor-poll-seconds 10
```

**Terminal 2 — Monitor contínuo de trades/log:**
```bash
python monitor_live_trades.py --poll-seconds 15
```

**Terminal 3 — Verificação de status (ad hoc, a cada ~30min):**
```bash
python check_paper_live_status.py --show-contexts --max-stale-min 10
```

**Terminal 4 — Relato horário de soak (disparar a cada hora ou em cron):**
```bash
python soak_monitor.py \
  --campaign-id spc-official-cdb-v1 \
  --window-hours 1 \
  --max-context-lag-min 10 \
  --max-restarts 5 \
  --max-error-lines 20
```

### Critérios de Aceitação — 24h

| KPI | Limiar APROVADO | Limiar ATENÇÃO | Limiar REPROVADO |
|---|---|---|---|
| Lag máximo de contexto | ≤ 10 min | 10–30 min | > 30 min |
| Reinicializações totais (supervisor) | ≤ 5 | 6–10 | > 10 |
| Falhas permanentes de contexto | 0 | — | ≥ 1 |
| Linhas de erro no log (por hora) | ≤ 10 | 11–20 | > 20 |
| Crescimento de artefatos (por hora) | qualquer | — | sem relato por > 2h |
| RAM processo | ≤ 80% | 80–95% | > 95% |
| Disco | ≤ 90% | 90–95% | > 95% |
| Contextos ativos (%) | ≥ 90% | 75–90% | < 75% |

### Procedimentos de Alerta e Rollback

| Alerta | Causa Provável | Ação |
|---|---|---|
| `CONTEXT_STALE` por > 30 min | Worker travado | `check_paper_live_status.py` → identificar contexto → reiniciar manualmente o supervisor |
| `RESTART_STORM` (> 5 restarts em < 1h) | Contexto instável | Isolar contexto com `--contexts` excluindo o problemático; reportar para bug tracking |
| `RAM_HIGH` (> 95%) | Possível vazamento | Reinicialização segura do supervisor (trades abertos são preservados via state atômico) |
| `DISK_HIGH` (> 95%) | Acúmulo de artefatos | Remover arquivos de relatório antigos (`optimization/results/*_report_*.json` com mtime > 7 dias) |
| `ERROR_RATE_HIGH` em log | Erros de rede/exchange | Verificar conectividade; se persiste > 30 min, encerrar e investigar |
| Desync de posição detectado | State divergiu do broker | `check_paper_live_status.py` mostra `desync_candidates`; reiniciar contexto afetado |

### Entrega — Relatório de Fechamento 24h

Ao final de 24h, executar:
```bash
# Relatório de performance de trades
python main.py paper-operational-report \
  --date YYYY-MM-DD \
  --strategy-name ClassicDonchianBreakout \
  --strategy-version v1.0

# Snapshots finais
python check_paper_live_status.py --show-contexts

# Relatório de soak final
python soak_monitor.py --campaign-id spc-official-cdb-v1 --window-hours 24 \
  --output-prefix soak_24h_closeout_report
```

---

## FASE 2 — Soak 72h

### Objetivo
Confirmar sustentabilidade de longa duração: sem crescimento de memória acumulado, sem degradação de edge ao longo do tempo, semelhança de P&L entre períodos.

### Execução
Mesmos comandos da Fase 1, mantendo continuidade (supervisor não é reiniciado entre as fases).

**Adicionalmente, verificação de estabilidade de edge a cada 24h:**
```bash
# 24h-check (D1, D2, D3)
python check_paper_live_status.py --show-contexts
python soak_monitor.py --campaign-id spc-official-cdb-v1 --window-hours 24 \
  --output-prefix soak_72h_daily_check_D<N>
```

### Critérios de Aceitação — 72h (adicionais)

| KPI | Limiar APROVADO |
|---|---|
| Reinicializações totais em 72h | ≤ 15 |
| Degradação de profit_factor em relação ao backtest | ≤ 35% |
| Ciclos totais por contexto | progressão monotônica (sem travamento) |
| Sem perda de memória de hipótese entre restarts | verificado via state files (hypothesis_payload persistido) |
| Variação de RAM máxima entre D1 e D3 | ≤ 20% absoluto |

### Critérios de Parada Antecipada (STOP Soak)

Interromper imediatamente se qualquer condição abaixo for detectada:
- **Falha permanente de contexto sem recovery** (permanent_failures ≥ 1 sem resolução em 1h)
- **Perda de state persistido** (arquivo `.json` de contexto corrompido ou ausente por > 15 min)
- **Erro de DB não-recuperável** (após todos os retries configurados)
- **RAM > 95% por mais de 30 min consecutivos**
- **Divergência de hipótese detectada** (parity check entre Paper e live config retorna False)

---

## Relatório Executivo de Aprovação/Reprovação

### Template — Preencher ao Final de Cada Fase

```
==========================================================================
RELATÓRIO EXECUTIVO — SOAK [24h|72h]
Data: YYYY-MM-DD HH:MM UTC
Release Manager: [nome]
==========================================================================

VEREDICTO: [APROVADO | APROVADO_COM_RESTRICOES | REPROVADO]

RESUMO
------
- Duração total: XXh
- Estratégia: ClassicDonchianBreakout@v1.0
- Campaign ID: spc-official-cdb-v1
- Contextos monitorados: N
- Contextos com falha permanente: N

KPIs FINAIS
-----------
- Lag máximo observado: XXmin
- Reinicializações totais: N
- Linhas de erro/hora médio: N
- RAM pico: XX%
- Disk ao final: XX%
- Cobertura de contextos ativa: XX%
- Ciclos totais (média): N

ISSUES ENCONTRADOS
------------------
Severidade | Código | Descrição | Resolução
-----------|--------|-----------|----------
high       |        |           |
medium     |        |           |
low        |        |           |

BUGS CRÍTICOS (impedem promoção para Live): (none|lista)
BUGS MÉDIOS (aceitáveis com acompanhamento): (none|lista)
BUGS BAIXOS (backlog): (none|lista)

RECOMENDAÇÕES PRÉ-LIVE
------------------------
1.
2.
3.

CHECKLIST DE PRONTIDÃO
-----------------------
[ ] Suite P0 passando (45 testes)
[ ] Soak 24h aprovado
[ ] Soak 72h aprovado
[ ] Nenhuma falha permanente de contexto
[ ] Nenhum crescimento de RAM não controlado
[ ] Edge (profit_factor) ≥ 65% do backtest referência
[ ] State de hipótese preservado após ≥ 1 restart de supervisor
[ ] Desync de posição = 0 ao final
[ ] Responsável SRE designado para suporte live

DECISÃO FINAL: [AUTORIZADO_PARA_LIVE | PENDENTE_CORREÇÕES | BLOQUEADO]
==========================================================================
```

---

## Mapa de Fontes de KPI por Ferramenta

| KPI | Fonte Principal |
|---|---|
| Lag de contexto (seg) | `check_paper_live_status.py` (`lag_avg`, `lag_max`) / `soak_monitor.py` (`contexts.lag_seconds_max`) |
| Ciclos por contexto | `check_paper_live_status.py` (`cycles_min/avg/max`) |
| Reinicializações totais | `soak_monitor.py` (`supervisor.total_restarts`) |
| Falhas permanentes | `soak_monitor.py` (`supervisor.permanent_failures`) |
| Linhas de erro no log | `soak_monitor.py` (`logs.error_lines_last_window`) |
| Crescimento de artefatos | `soak_monitor.py` (`artifacts.growth_bytes_last_window`) |
| CPU / RAM / Disk | `soak_monitor.py` (`resources.*`) via psutil |
| Cobertura de contextos | `paper_campaign_coverage_monitor` ou `check_paper_live_status.py` (contexts_active/total) |
| Desync de posição | `check_paper_live_status.py` (`desync_candidates`) |
| P&L / profit_factor / win_rate | `paper-operational-report` (`optimization/results/*_operation_report_*.md`) |
| State de hipótese | `paper_live_state__*.json` → campo `hypothesis_payload` no `runtime_state` |

---

## Artifacts Gerados por Soak

| Artefato | Padrão de Nome | Propósito |
|---|---|---|
| Relatório horário de soak | `soak_hourly_report_<stamp>.json/md` | Status consolidado por hora |
| Relatório de fechamento | `soak_24h_closeout_report_<stamp>.json/md` | Síntese final de 24h |
| Relatório de operações diário | `scientific_paper_live_daily_*.json/md` | P&L por trade |
| Relatório horário de P&L | `scientific_paper_live_hourly_report_*.json` | P&L por hora |
| Status de status de supervisor | `paper_live_supervisor_*.json` | Estado e contagem de restarts |
| State de contexto | `paper_live_state__*.json` | Estado de cada contexto para recovery |
| Audit de supervisor | `paper_live_supervisor_audit_*.jsonl` | Log auditável de todos os eventos de supervisão |

---

*Documento gerado automaticamente pelo assistente Release/SRE.*  
*Data do documento: baseado no estado do código em 2026-08-05.*
