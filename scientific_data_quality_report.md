# Scientific Data Quality Report

## Escopo

Relatorio observacional da implantacao da FASE 16, focado exclusivamente em qualidade de persistencia cientifica e prontidao da base para futuras investigacoes.

## Estado atual

- total_trades_auditados: 148
- cobertura_temporal_dias: 23.77
- scientific_readiness_score: 23.9
- readiness_label: Insuficiente
- scientific_readiness_history_rows: 1
- scientific_trade_snapshot_rows_no_momento_do_relatorio: 0

## Integridade transacional

- duplicidades: 0
- exit_time ausente: 0
- exit_reason ausente: 0
- timestamps invalidos: 0
- preco invalido: 0
- quantidade invalida: 0
- duracao invalida: 0
- par pnl/retorno invalido: 0

## Lacunas cientificas historicas identificadas

As lacunas abaixo referem-se ao historico legado anterior a implantacao do warehouse cientifico:

- entry_reason ausente no historico auditado atual
- market_regime ausente nos 148 trades historicos auditados
- indicator context legado ausente nos 148 trades historicos auditados

## Interpretacao correta

- Nao foi encontrada corrupcao de dados transacionais.
- Nao foi encontrada duplicidade de trade.
- Nao foi encontrada inconsistência estrutural de PnL ou timestamps.
- A limitacao atual e de completude cientifica do historico legado, nao de falha operacional do robo.

## Estado do warehouse cientifico

- schema implantado com tabelas scientific_trade_snapshots e scientific_readiness_history
- readiness historico persistente habilitado
- dashboard evolutivo habilitado
- campanha de coleta reiniciada em modo continuo no supervisor paper-live

## Proxima expectativa

Os novos trades gerados apos a implantacao devem passar a carregar:

- entry_reason
- market_regime
- indicator context
- scientific snapshot de entrada
- scientific snapshot de saida

Enquanto isso nao ocorrer em volume suficiente, os alertas de qualidade ainda refletirao a heranca do historico anterior.
