# Guia de Teste - Campanha Noturna

## Testes Rápidos

### Teste 1: Verificar Sintaxe
```bash
python -m py_compile main.py research/services/phase13_continuous_strategy_factory.py
```

### Teste 2: Listar Estratégias Pendentes
```bash
python check_pending.py
```
Esperado: 19 estratégias em IMPLEMENTATION_PENDING

### Teste 3: Campanha Mínima (2 estratégias, 180 segundos)
```bash
python main.py overnight-campaign \
  --batch-size 2 \
  --campaign-max-seconds 180 \
  --output-prefix test_overnight_minimal
```
Esperado: Processa 2 estratégias rapidamente

### Teste 4: Campanha Pequena (5 estratégias, 600 segundos)
```bash
python main.py overnight-campaign \
  --batch-size 5 \
  --campaign-max-seconds 600 \
  --output-prefix test_overnight_small
```
Esperado: Processa 5 estratégias em ~10 minutos

### Teste 5: Campanha Completa (Todas as 19, 9 horas)
```bash
python run_overnight_campaign.py
```
Esperado: Processa as 19 estratégias até encontrar PAPER_CANDIDATE ou atingir 09:00

## Validações Esperadas

### Após Teste 1
- ✓ Sem erros de sintaxe
- ✓ Ambos arquivos compilam corretamente

### Após Teste 2
```
Total backlog: 51
IMPLEMENTATION_PENDING: 19
IMPLEMENTATION_INCOMPLETE: 0
Total pending + incomplete: 19
```

### Após Teste 3
```
Stop reason: budget_time_limit (ou campaign_end_hour_reached se for depois de 09:00)
Smoke Test: 2 (processadas)
Estrategias em PAPER_CANDIDATE: 0 ou mais
```

### Após Teste 5
```
- Campanha começou com as 19 pendências em primeiro
- Se hora < 09:00, continua até achar PAPER_CANDIDATE ou 09:00
- Se hora > 09:00, trata como "already passed" - pode rodar normalmente até próximas 23:59
- Relatório com:
  * Estratégias pesquisadas
  * Implementadas
  * Avaliadas (backtest_reached)
  * Reprovadas
  * PAPER_CANDIDATE encontradas
```

## Arquivos de Saída

Localização: `optimization/results/`

Para cada run:
- `{prefix}_YYYYMMDD_HHMMSS.json` - Relatório completo JSON
- `{prefix}_YYYYMMDD_HHMMSS_backlog.csv` - Backlog atualizado
- `{prefix}_YYYYMMDD_HHMMSS.md` - Relatório Markdown

## Logs Importantes

Procure por:
```
strategy selected from active queue
error - continued
PAPER_CANDIDATE
campaign_end_hour_reached
paper_candidates_target_reached
budget_max_strategies
```

## Monitorar em Tempo Real

Durante a campanha, verifique:
```bash
# Em outra janela, a cada 10 segundos:
watch -n 10 "tail -20 $(ls -t logs/* | head -1)"
```

Ou inspecione JSON em tempo real:
```bash
# Encontre o arquivo mais recente
ls -t optimization/results/*.json | head -1 | xargs cat | jq '.stage_counters'
```

## Estrutura do Relatório Final

```json
{
  "phase": "13",
  "run_id": "uuid",
  "stop_reason": "campaign_end_hour_reached | paper_candidates_target_reached | ...",
  "implemented_count": N,
  "rejected_count": N,
  "paper_candidate_count": N,
  "stage_counters": {
    "reprocessed_total": N,
    "backtest_reached": N,
    "optimizer_probe_reached": N,
    "optimizer_reached": N,
    "validation_reached": N,
    "paper_qualification_reached": N
  },
  "top_rejection_reasons": [...],
  "rejection_knowledge": {
    "family": {...},
    "indicators": {...},
    "stage": {...},
    "all_rejections": [...]
  },
  "ranking_updated": [...]
}
```

## Troubleshooting

| Problema | Solução |
|----------|---------|
| Campanha para imediatamente | Verifique hora (deve ser antes de 09:00) |
| Smoke Test = 0 | Verificar se backlog tem estratégias IMPLEMENTATION_PENDING |
| Muita CPU/memória | Reduzir `--optimizer-workers` ou `--batch-size` |
| Sem PAPER_CANDIDATE | Normal, pode precisar de mais tempo ou melhor tuning |
| Erro de conexão DB | Verificar MySQL está rodando |

## Sucesso! 🎉

Quando ver:
- ✓ IMPLEMENTATION_PENDING começou a ser processado
- ✓ Smoke Test stage_counters > 0
- ✓ Pelo menos uma estratégia atingiu Backtest ou além
- ✓ Relatório gerado sem erros

A campanha está funcionando corretamente!
