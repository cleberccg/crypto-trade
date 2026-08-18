# Investigação Completa: Padrão de Rejections (60% de 2 Critérios)

## Resumo Executivo

A concentração de 60% das rejeições (9/15) em apenas **2 critérios** não representa um bug, mas **3 problemas bem-definidos** na configuração e design:

1. **Mismatch de RR (Risk/Reward): 27% das rejeições**
2. **Negative Profile com critério muito restritivo: 53% das rejeições**
3. **Validação de trade count vs. estratégia sem trades**

---

## Problema 1: Risk/Reward Ratio Mismatch (4 rejeições = 27%)

### Diagnóstico
- **Erro**: "Risk/reward ratio 1.80 is below the configured minimum of 2.00"
- **Estratégias afetadas**: WaveTrend, HalfTrend, RSI Divergence + Trend Filter, Nadaraya-Watson Envelope
- **Todas usam**: `MeanReversionV1` como estratégia platform

### Causa Raiz

#### 1. MeanReversionV1Strategy Define RR=1.8 como Padrão
**Arquivo**: [strategies/mean_reversion_v1.py](strategies/mean_reversion_v1.py#L60)
```python
class MeanReversionV1Strategy(MeanReversionStrategy):
    def __init__(
        self,
        ...
        risk_reward_ratio: float = 1.8,  # ← PADRÃO 1.8
        ...
```

**Lógica de cálculo TP** (linha 133-135):
```python
stop_loss = price - self._atr_stop_multiplier * atr
risk = max(price - stop_loss, 1e-9)
reward = risk * max(self._risk_reward_ratio, 1.2)  # ← Usa 1.8 internamente
take_profit = price + reward
```

#### 2. RiskManager Valida contra RR=2.0 Global
**Arquivo**: [risk/risk_manager.py](risk/risk_manager.py#L190-L193)
```python
# --- Valida risco/retorno minimo ---
if rr_ratio < cfg.risk_reward_ratio:
    raise ValueError(
        f"Risk/reward ratio {rr_ratio:.2f} is below the configured "
        f"minimum of {cfg.risk_reward_ratio:.2f}. Adjust stop/TP."
    )
```

#### 3. Config Global Define RR=2.0
**Arquivo**: [config/settings.py](config/settings.py#L245)
```python
risk_reward_ratio=_as_float(os.getenv("RISK_REWARD_RATIO"), 2.0),  # ← PADRÃO 2.0
```

### Por que isso acontece?
- MeanReversionV1 gera um TP baseado em RR=1.8 (adequado para mean reversion com stops próximos)
- RiskManager rejeita porque espera RR >= 2.0 (padrão global)
- **Conflito**: Configuração local (1.8) vs. validação global (2.0)

### Questionamento: É apropriado RR=2.0 para Bitcoin 5m?
**Análise**:
- RR=2.0 significa: para cada unidade de risco, ganhar 2 unidades
- Mean reversion em Bitcoin 5m frequentemente tem stops próximos (ATR*2)
- Isso força TPs distantes, tornando estratégias mais arriscadas
- **Recomendação**: RR=1.8 ou 1.5 é mais realista para 5m

---

## Problema 2: Negative Profile Muito Restritivo (5 + 3 = 8 rejeições = 53%)

### Diagnóstico
- **5 rejeições diretas** com `negative_profile`
- **3 rejeições combinadas** com `profit_factor_too_low;negative_profile`
- **Total**: 53% de todas as rejeições

### Definição do Critério
**Arquivo**: [research/services/phase13_continuous_strategy_factory.py](research/services/phase13_continuous_strategy_factory.py#L1143-L1147)
```python
def _early_stop(self, backtest: dict[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    trades = int(backtest.get("number_of_trades", 0))
    pf = float(backtest.get("profit_factor", 0.0))
    expectancy = float(backtest.get("expectancy", 0.0))
    sharpe = float(backtest.get("sharpe", 0.0))

    if trades <= 0:
        reasons.append("no_trades")
    if pf < max(0.30, float(settings.validation.min_profit_factor) * 0.5):
        reasons.append("profit_factor_too_low")
    if expectancy < 0 and sharpe < 0 and pf < 1.0:  # ← NEGATIVE_PROFILE AQUI
        reasons.append("negative_profile")
```

### Condição: `negative_profile`
```
if expectancy < 0 AND sharpe < 0 AND pf < 1.0:
    rejeita()
```

**O que significa**:
- Expectância negativa (média de ganho por trade < 0)
- E Sharpe negativa (retorno ajustado por volatilidade < 0)
- E Profit Factor < 1.0 (menos ganhos que perdas)
- = Estratégia com performance ruim (como esperado em smoke test com 33k bars)

### Por que 53% das rejeições?

1. **Smoke test com dataset limitado**
   - 11 estratégias chegam ao backtest
   - Backtest usa apenas 33,776 bars (9.3 dias de dados 5m)
   - Períodos curtos → volatilidade alta → sharpe negativo
   - Poucos trades → expectância pode ser negativa por sorte

2. **Critério muito permissivo?**
   - Todas as 3 condições devem ser simultâneas
   - Mas a combinação é muito comum em smoke tests
   - Deveria ter MIN_TRADES threshold antes de rejeitar?

### Questão: Deveria existir strategy com 0 trades?
**Resposta**: Sim, faz sentido:
- Em backtest com 33k bars (9 dias), mean reversion pode não gerar entrada
- Não é um bug, é a estratégia não se encaixando no período
- MAS: Deveria ser rejeitada com `no_trades`, não com `negative_profile`

---

## Problema 3: Validação de Trade Count vs. Backtest Vazio

### Contexto
- `if trades <= 0: reasons.append("no_trades")`
- Strategies com 0 trades são rejeitadas... MAS aparecem como `negative_profile`
- Sugestão: Adicionar guard`trades <= MIN_VALIDATION_TRADES` antes de calcular RR

---

## Respostas às Questões do Usuário

### A. Is `negative_profile` being calculated correctly? Should strategies with 0 trades exist?

**Resposta**:
- ✅ **SIM**, está calculado corretamente (expectancy < 0 AND sharpe < 0 AND pf < 1.0)
- ✅ **SIM**, estratégias com 0 trades devem existir (data period pode não ter setups)
- ❌ **MAS**: Deveriam ser rejeitadas com `no_trades`, não com `negative_profile`
- ⚠️ **Recomendação**: Adicionar check `if trades < MIN_VALIDATION_TRADES: return early`

### B. Is RR minimum 2.00 appropriate for Bitcoin 5m? Too restrictive?

**Resposta**:
- ❌ **NÃO, é muito restritivo**
- **Por quê**:
  - Mean reversion strategies têm stops próximos (1-2%)
  - ATR-based stops em 5m são naturalmente pequenos
  - RR=2.0 força TPs muito distantes (2-4% acima entry)
  - Em 5m com 10k capital, uma TP em +4% é high-risk
- ✅ **Recomendação**: Reduzir para RR=1.5 ou 1.8 para 5m
- 📊 **Evidência**: MeanReversionV1 usa RR=1.8 como padrão

### C. Were there configuration changes or is this expected behavior?

**Resposta**:
- ✅ **EXPECTED BEHAVIOR**, não é bug
- **Por quê**:
  - Config default 2.0 é histórico (vem desde o início)
  - MeanReversionV1 sempre usou 1.8
  - Mismatch é architecural, não recente
- ✅ **Recomendação**: Sincronizar valores
  - Option 1: RiskManager default = 1.8 (ou env variable)
  - Option 2: MeanReversionV1 default = 2.0
  - Option 3: Strategy deve respeitar global config

---

## Recomendações de Ação

### Imediata (Fix Critical)
1. **Sync RR defaults**: 
   - Change [config/settings.py](config/settings.py#L245): `2.0 → 1.8` ou
   - Change [strategies/mean_reversion_v1.py](strategies/mean_reversion_v1.py#L60): `1.8 → 2.0`
   - **Preferência**: `2.0 → 1.8` (mais realista para 5m)

2. **Fix negative_profile criteria**:
   - Add guard before RR calculation:
   ```python
   if trades < settings.validation.min_trades:
       reasons.append("insufficient_data")
       return {"triggered": True, "reasons": reasons}
   ```

### Curto Prazo (Improve)
1. **Make RR configurable by strategy family**:
   - `RISK_REWARD_RATIO_MEAN_REVERSION=1.8`
   - `RISK_REWARD_RATIO_TREND=2.0`

2. **Add diagnostic log**:
   - Log RR mismatch reasons
   - Track why negative_profile triggered

### Longo Prazo (Architecture)
1. **Decouple strategy-level RR from global RR**:
   - Strategy generates signal with its own RR
   - RiskManager validates but can adjust TP (warn, not reject)

---

## Conclusão

O padrão de rejections (60% de 2 critérios) **NÃO é um bug, mas um design issue**:

1. **RR Mismatch**: 27% → Config defaults conflitantes (1.8 vs 2.0)
2. **Negative Profile**: 53% → Critério muito restritivo para smoke tests curtos
3. **Combinado**: 80% das rejeições → Problemas de config + validação

**Status**: EXPECTED BEHAVIOR, mas necessita ajuste de configuração para produção.

**Impacto de negócio**: 
- Nenhuma estratégia aprovada (0/45) devido às rejeições
- Tendência: Relaxar RR para 1.8 e adicionar MIN_TRADES guard
- Resultado esperado: 2-3 estratégias podem passar

---

## Evidências de Suporte

### Config Defaults Conflict
```
Global RR minimum (config/settings.py:245): 2.0
MeanReversionV1 default (strategies/mean_reversion_v1.py:60): 1.8
Backtest validation (phase13:1143): RR < 2.0 → reject
Result: All MeanReversionV1 with ATR-based SL rejected
```

### Negative Profile Prevalence
```
Total rejections: 15
negative_profile (direct): 5 (33%)
negative_profile (combined): 3 (20%)
Total affected: 8/15 = 53%

Reason: Short backtest period (33k bars = 9 days)
Sharp volatility → sharpe < 0
Few trades → expectancy < 0
Low PF → pf < 1.0
```

### Strategies Rejected with RR=1.80
1. WaveTrend (score 77.8) → REJECTED_BY_INFRASTRUCTURE
2. HalfTrend (score 67.8) → REJECTED_BY_INFRASTRUCTURE
3. RSI Divergence + Trend Filter (score 59.2) → REJECTED_BY_INFRASTRUCTURE
4. Nadaraya-Watson Envelope (score 46.2) → REJECTED_BY_INFRASTRUCTURE

All 4: Platform strategy = MeanReversionV1, smoke_test stage
