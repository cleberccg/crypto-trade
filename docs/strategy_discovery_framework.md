# Strategy Discovery Framework

## Nova arquitetura implementada

A arquitetura de descoberta de estrategias agora e baseada em registro declarativo e descoberta automatica por modulo.

Componentes:
- `strategies/registry.py`: registro central com metadata, aliases, normalizacao de parametros e filtros de assinatura.
- `strategies/families.py`: classes-base por familia (trend, mean_reversion, breakout, momentum, range, vwap, opening_range, liquidity_sweep, market_structure).
- `strategies/factory.py`: construcao via registry (sem if/else hardcoded).
- `strategies/__init__.py`: API publica para listar estrategias e comparar familias.

## Compatibilidade preservada

- Nomes legados continuam validos para criacao:
  - TrendV1: `TrendV1`, `trend_v1`, `v1`
  - TrendV2: `TrendV2`, `trend_v2`, `v2`
- Parametros legados continuam aceitos:
  - `ema_mid` mapeia para `ema_slow`
  - `volume_multiplier` mapeia para `volume_multiplier_min`
- Optimizer e Validator continuam operando com o mesmo contrato principal (`create_strategy`), agora de forma agnostica de familia.

## Estrategias suportadas atualmente

- TrendV1 (familia: trend)
- TrendV2 (familia: trend)
- MeanReversionV1 (familia: mean_reversion)

## Estrutura pronta para adicionar novas familias

Checklist para adicionar nova estrategia/familia:
1. Criar modulo em `strategies/`.
2. Implementar classe derivando de uma familia em `strategies/families.py`.
3. Decorar com `@register_strategy(...)` informando metadata e aliases.
4. Definir aliases de parametros para manter compatibilidade com historico, se necessario.
5. Rodar optimize/validate/research sem mudancas no core.

## Recomendacao fundamentada da primeira nova familia

Familia recomendada para ser a primeira apos trend: **mean reversion volatilidade-adaptativa**.

Justificativa tecnica:
- Cripto apresenta frequentes fases de compressao, exaustao e retorno parcial a media, especialmente intraday em pares liquidos.
- Sinais de desvio de banda (Bollinger), exaustao de oscilador (RSI) e controle de risco por ATR sao amplamente estudados e transparentes para auditoria.
- Integracao operacional simples no stack atual: mesmos dados OHLCV, mesmos blocos de risco e mesmo mecanismo de backtest/validacao.

Regimes-alvo:
- Lateral a levemente direcional.
- Volatilidade media/alta com reversoes rapidas apos excesso.

Limitacoes conhecidas:
- Sofre em trend forte sem filtro de regime.
- Exige disciplina de stops e limite de exposicao por cluster de perdas.

## Plano de validacao da nova familia no pipeline existente

1. Otimizacao inicial com janela rolling e checkpoints.
2. Validacao walk-forward com criterio identico ao usado em trend.
3. Strategy Research Lab para diagnostico de edge por regime e friccao de execucao.
4. Trade Management Research Lab com as mesmas entradas para testar robustez de saida.
5. Gate final de implantacao:
   - PF e Sharpe superiores ao baseline com IC bootstrap sem cruzar zero no delta de retorno.
   - Sem degradacao severa em drawdown e sem colapso por subperiodo.
