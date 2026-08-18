# Quick Start - Campanha Noturna

## Executar AGORA

```bash
python run_overnight_campaign.py
```

## Ou com opções customizadas

```bash
python main.py overnight-campaign \
  --symbol BTC/USDT \
  --timeframe 5m \
  --target-paper-candidates 3 \
  --campaign-end-hour 9
```

## Ver Status das 19 Pendências

```bash
python check_pending.py
```

## Monitorar Progresso

```bash
# Terminal 1: Rodando campanha
python run_overnight_campaign.py

# Terminal 2: Ver logs
Get-Content -Tail 20 -Wait logs/crypto_bot.log

# Terminal 3: Ver resultados atualizados
Get-ChildItem -Path optimization/results/ -Filter overnight* | Sort-Object LastWriteTime -Desc | Select-Object -First 1 | ForEach-Object { Get-Content $_ | ConvertFrom-Json | Select-Object @{e={$_.stage_counters}}, stop_reason, paper_candidate_count }
```

## Esperado

- ✓ Processa as 19 estratégias em IMPLEMENTATION_PENDING primeiro
- ✓ Para gracefully às 09:00 (se ainda rodando)
- ✓ Busca 3 PAPER_CANDIDATE se possível
- ✓ Completa qualquer estratégia já iniciada antes de parar
- ✓ Gera relatório em optimization/results/

## Resultado do Teste v2 (2 estratégias)

```
Smoke Test: 2 ✓
Backtest: 0 (rejeitadas cedo)
Stop reason: budget_time_limit ✓
PAPER_CANDIDATE: 0 (como esperado, estratégias ruins)
```

## Próximas Etapas

1. Rodar campanha completa durante madrugada
2. Coletar dados de aprendizado das 19 pendências
3. Iterar com base no `rejection_knowledge`
4. Encontrar primeira PAPER_CANDIDATE

---

**Documentação Completa**: 
- [CAMPANHA_NOTURNA.md](CAMPANHA_NOTURNA.md) - Detalhes técnicos
- [TESTE_CAMPANHA_NOTURNA.md](TESTE_CAMPANHA_NOTURNA.md) - Testes e validação
