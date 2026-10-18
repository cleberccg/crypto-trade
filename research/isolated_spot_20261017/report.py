from pathlib import Path
import json, math, hashlib
import pandas as pd
OUT=Path(__file__).resolve().parent; ROOT=OUT.parents[1]
rows=json.loads((OUT/'results.json').read_text()); trades=pd.read_csv(OUT/'trades.csv')
assert len(rows)==80
names={'pullback':'Pullback tendência','reversion':'Reversão com regime','baseline':'Sma200RegimeGated v1'}
def group(r): return r['symbol'],r['timeframe'],r['strategy']
def pf(p):
    loss=-p[p<0].sum(); return p[p>0].sum()/loss if loss else (math.inf if (p>0).any() else 0.)
def table(data):
    lines=['| Ativo | TF | Estratégia | Trades | Trades/dia | PF líquido | PnL USDT | Win rate | Exp./trade USDT | DD % | Custos USDT |','|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for r in data:
        lines.append(f"| {r['symbol']} | {r['timeframe']} | {names[r['strategy']]} | {r['total_trades']} | {r['trades_per_day']:.3f} | {r['profit_factor']:.3f} | {r['net_profit']:.2f} | {100*r['win_rate']:.1f}% | {r['expectancy_unrounded']:.5f} | {100*r['max_drawdown_pct']:.2f} | {r['total_cost']:.3f} |")
    return '\n'.join(lines)
oos=[r for r in rows if r['split']=='OOS' and r['cost_per_side']==.0013]
base=[r for r in oos if r['strategy']=='baseline']
candidates=[r for r in oos if r['strategy']!='baseline']
robust=[]; selected=[]
for r in candidates:
    key=group(r); related=[a for a in rows if group(a)==key]
    s=next(a for a in related if a['cost_per_side']==.0019)
    t=trades[(trades.symbol==r['symbol'])&(trades.timeframe==r['timeframe'])&(trades.strategy==r['strategy'])&(trades.split=='OOS')&(trades.cost_per_side==.0013)].copy()
    t['month']=pd.to_datetime(t.exit_time,utc=True).dt.strftime('%Y-%m')
    months=t.groupby('month').pnl.sum(); p=t.pnl.sort_values().iloc[:-3] if len(t)>3 else t.pnl.iloc[:0]
    baseline=next(a for a in base if a['symbol']==r['symbol'])
    ci=r['block_bootstrap_familywise_ci']
    ok=all(a['net_profit']>0 for a in related if a['cost_per_side']==.0013) and r['total_trades']>=30 and r['profit_factor']>=1.2 and s['net_profit']>0 and ci[0] is not None and ci[0]>0 and r['trades_per_day']>baseline['trades_per_day']
    reasons=[]
    if r['expectancy_unrounded']<=0: reasons.append('expectativa OOS negativa')
    if r['total_trades']<30: reasons.append('menos de 30 trades OOS')
    if r['profit_factor']<1.2: reasons.append('PF OOS < 1,20')
    if ci[0] is None or ci[0]<=0: reasons.append('IC inferior não positivo')
    if not all(a['net_profit']>0 for a in related if a['cost_per_side']==.0013): reasons.append('falha DEV/VAL/OOS')
    if s['net_profit']<=0: reasons.append('falha no estresse de custos')
    robust.append({'symbol':r['symbol'],'tf':r['timeframe'],'strategy':r['strategy'],'positive_months':int((months>0).sum()),'months':len(months),'stress_pf':s['profit_factor'],'stress_net':s['net_profit'],'ci':ci,'pf_without_best3':float(pf(p)),'frequency_multiple':r['trades_per_day']/baseline['trades_per_day'] if baseline['trades_per_day'] else None,'accepted':ok,'reasons':reasons})
    if ok: selected.append(key)
assert not selected, 'Update conclusion if any candidate passes'
lines=['# Pesquisa científica isolada — Binance Spot 5m/15m',
'\nData: 17/10/2026, America/Sao_Paulo. Resultado: **nenhuma candidata aprovada para PAPER**. As regras testadas aumentaram a frequência, mas não demonstraram expectativa líquida positiva consistente. Esta conclusão se restringe às duas especificações congeladas e ao histórico usado; não prova que toda a família de estratégias seja inviável.',
'\n## Protocolo e reutilização',
'Foram executados 80 backtests: 16 combinações candidatas (duas regras × quatro ativos × dois timeframes), cada uma em DEV, validação, OOS e OOS com custos elevados; mais quatro referências 4h, no mesmo protocolo. Não houve busca de parâmetros, alteração das regras após observar resultados, aprendizado automático ou seleção antecipada por frequência.',
'\nReutilizados: `backtesting.engine.BacktestEngine/BacktestConfig`, `backtesting.metrics.compute_metrics`, métricas puras de `utils.metrics`, RSI de `indicators.rsi`, `compute_adx` e a classe real `Sma200RegimeGatedStrategy` de `research/external_strategy_replication_strategies.py`; também os padrões de `RiskManager` e `PositionSizer`, sem alteração de seus arquivos ou métodos. O script é uma orquestração com estratégias e adaptadores exclusivos de pesquisa; não cria banco, serviço ou engine alternativa. O serviço científico existente persiste em banco por padrão; por isso não foi chamado. Aplicou-se a convenção cronológica 60/20/20 existente e bootstrap por blocos neste relatório isolado.',
'\n## Dados e integridade',
'Fonte efetivamente usada: `data/binance_spot_extended_klines.csv.gz`, somente leitura. Cada ativo tem 105.120 candles de 5m entre 01/06/2025 e 01/06/2026 exclusivo, totalizando 420.480 candles-base. Não foram detectadas lacunas, duplicatas, OHLC inválido, valores ausentes ou preços não positivos nessa janela. Candles de 15m e 4h foram agregados com intervalos completos, rótulo na abertura e fechamento à esquerda. O manifesto contém SHA-256 do arquivo fonte.',
'O cache parquet BTC 5m inicialmente inspecionado tinha 8.064 candles ausentes na janela e foi rejeitado. Não houve preenchimento ou interpolação. A tentativa de acessar o arquivo mensal público falhou em DNS; nenhum arquivo mensal foi usado. Uma consulta pública `exchangeInfo` teve sucesso e seus filtros foram arquivados. Nenhuma credencial foi usada. O CSV local já continha todos os quatro pares; a análise não depende de novos dados de mercado.',
'O histórico a partir de 01/06/2026 permanece fora dos testes, conforme o holdout já reservado no projeto. O CSV compactado foi lido sequencialmente para selecionar linhas: o conteúdo posterior ao corte existe no arquivo fonte, mas não entrou em indicadores, métricas, otimização ou decisões. OOS significa não usado neste protocolo; não se pode garantir que nunca tenha sido visto em pesquisas anteriores do projeto.',
'\n## Regras congeladas',
'**Pullback em tendência:** comprado/sem posição. SMA200 ascendente em 20 barras, close acima de SMA200 e ADX14 ≥ 20. Entrada quando o fechamento cruza EMA20 para cima após fechamento anterior abaixo da EMA20, com RSI14 entre 40 e 65. Saída quando close < EMA20, close < SMA200 ou RSI14 ≥ 70.',
'**Reversão à média com filtro de regime:** ADX14 < 20 e distância absoluta close/SMA200 − 1 inferior a 5%; entrada com close abaixo de SMA20 − 2 desvios padrão e RSI14 < 35. Saída no retorno à SMA20, ADX14 ≥ 25 ou close < 95% da SMA200.',
'**Referência:** sinais da implementação existente Sma200RegimeGated v1, com os padrões SMA200/ADX14, limiar ADX20 e inclinação de 20 barras. A referência foi simulada em todos os pares em 4h; a comparação diretamente relevante ao LIVE é BNB 4h. Não representa PnL da conta real.',
'As candidatas usam, por compatibilidade com a convenção de pesquisa existente, stop remoto em 1% do preço de entrada, TP em 100× e trailing de 99%, para que prevaleçam as saídas de sinal. Não são proteções propostas para produção. Isso também influencia o dimensionamento: com risco padrão de 1%, a alocação fica próxima de 1,01% do capital, antes do arredondamento de lote. Nenhum parâmetro ou gerenciamento LIVE foi alterado.',
'\n## Execução, custos e capital',
'Capital inicial hipotético: **1.000 USDT por combinação e por split**, reiniciado em cada split. Sem posições simultâneas dentro de uma combinação, sem short, sem alavancagem, sem empréstimo. Os testes de ativos são independentes; não devem ser somados como se utilizassem uma carteira compartilhada. PnLs monetários dependem do capital e do dimensionamento descritos.',
'O processo não carrega o `.env`: importa os padrões do código com credenciais vazias e banco em memória não utilizado. Usa RiskManager/PositionSizer existentes e, depois do cálculo, um adaptador exclusivamente de execução arredonda quantidade para baixo pelo LOT_SIZE e rejeita quantidade/notional abaixo do mínimo ou acima de maxQty. Não há alteração do algoritmo de risco. Os filtros são um snapshot público atual, não um histórico de filtros. As quantidades são muito inferiores aos limites MARKET_LOT_SIZE. MIN_NOTIONAL/NOTIONAL é aproximado pelo preço de execução, sem reproduzir o preço médio móvel da exchange; há margem ampla entre os tickets principais (~9–10 USDT) e o mínimo de 5 USDT.',
'Custo base por lado: taxa 0,10% + meio spread 0,01% + slippage 0,02% = **0,13%**. Ida e volta nominal: **0,26%**. Estresse: 0,10% + 0,01% + 0,08% = **0,19% por lado**, 0,38% ida e volta. Sem desconto BNB ou hipótese maker. O motor contabiliza a soma como débito proporcional ao notional em cada lado; é uma aproximação de custo econômico, não um simulador de book/fills. `results.csv` separa taxa, spread e slippage estimados.',
'O log operacional lido durante a pesquisa registrou **28,25577283 USDT livres** em 17/10 às 21:58:29. Não é saldo autenticado consultado pela pesquisa, nem garantia de saldo atual. Com os padrões de dimensionamento usados aqui, esse capital gera ticket de aproximadamente 0,285 USDT, inferior ao mínimo de 5 USDT: portanto, não executaria as candidatas neste protocolo. Esse caso é verificado separadamente. Não se propõe aumentar stake, risco ou aporte; adequação ao capital e à configuração real continuaria pendente mesmo diante de um backtest positivo.',
'\n## Causalidade e separação temporal',
'DEV usa os primeiros 60% dos candles, validação os 20% seguintes e OOS os últimos 20%. Cada split começa sem posição. DEV perde as primeiras 220 barras de aquecimento; validação e OOS usam 220 barras anteriores apenas para indicadores, sem operar essas barras. O período OOS é 20/03/2026 a 31/05/2026, cerca de 73 dias. Cada posição remanescente é liquidada na última abertura programada do split, com custo; não se transporta posição entre treino e teste.',
'Sinais são calculados exclusivamente em candles fechados, deslocados uma barra e executados na abertura seguinte. Na visão do motor, close/high/low são substituídos pelo open corrente depois de calcular os sinais, evitando uso de high/low futuros da barra de execução. O drawdown reportado é marcado nas aberturas, podendo subestimar extremos intrabar. Os stops remotos não são uma avaliação de execução real em gaps. `verification.json` verifica invariância de prefixos, execução na abertura, rejeição com capital pequeno e reconciliação de trades/PnL/custos.',
'\n## Resultados fora da amostra — custos base',
'PnL, expectativa e custos em USDT; DD é percentual do capital total de 1.000 USDT. Win rate e PF já incluem custos. Nenhum resultado desta tabela é lucro real.',
table(candidates),
'\n### Referência 4h, mesmo histórico e custos',table(base),
'\n## Robustez OOS e decisão',
'O critério congelado exige DEV/validação/OOS positivos, pelo menos 30 trades OOS, PF OOS ≥ 1,20, lucro no estresse, frequência maior que a referência do mesmo ativo e limite inferior de expectativa positivo no bootstrap. Nenhuma candidata passou. Os números não devem ser apresentados como prova formal de edge.',
'Bootstrap: 2.000 reamostragens circulares em blocos de cinco trades consecutivos, seed fixa; percentis ajustados conservadoramente por 16 comparações (0,025/16 e 1−0,025/16). O tamanho de bloco não foi otimizado. Trata dependência local apenas aproximadamente, não substitui validação prospectiva. Os percentis extremos têm baixa resolução nessa quantidade de reamostragens. Retirada dos três melhores trades e contagem de meses OOS positivos são diagnósticos, sem seleção de parâmetros.',
'| Ativo | TF | Estratégia | Meses positivos/observados | PF estresse | PnL estresse | PF sem 3 melhores | IC expectativa ajustado USDT | Frequência / 4h | Decisão |',
'|---|---|---|---:|---:|---:|---:|---|---:|---|']
for r in robust:
    lo,hi=r['ci']; ci='insuficiente' if lo is None else f'[{lo:.5f}; {hi:.5f}]'
    multiple='n/a' if r['frequency_multiple'] is None else f"{r['frequency_multiple']:.1f}×"
    lines.append(f"| {r['symbol']} | {r['tf']} | {names[r['strategy']]} | {r['positive_months']}/{r['months']} | {r['stress_pf']:.3f} | {r['stress_net']:.2f} | {r['pf_without_best3']:.3f} | {ci} | {multiple} | Rejeitada |")
lines+=['\n## Desenvolvimento e validação — resultados completos','Os mesmos custos base e as mesmas métricas foram calculados em todos os splits; não houve interrupção antecipada após resultados ruins. Valores completos e precisão adicional estão nos arquivos CSV/JSON.','\n### DEV',table([r for r in rows if r['split']=='DEV']),'\n### Validação',table([r for r in rows if r['split']=='VALIDATION']),
'\n## Limitações e próxima fase',
'Não foi encontrada candidata convincente para PAPER. Não iniciar PAPER contínuo com estas regras. A maior frequência resultou em aumento do atrito; nenhuma regra foi promovida apenas por operar mais. A simulação da referência também pode falhar nesse período e não constitui motivo para alterar o LIVE.',
'Não foram avaliados 1m/3m ou candles de segundos: a matriz prioritária não demonstrou edge líquido que justifique ampliar volume de dados e exigência de execução. Uma pesquisa futura deveria formular previamente outra hipótese de sinal e sua margem bruta sobre custos, validar fontes/holdouts ainda não explorados e considerar a viabilidade de lotes no capital disponível, sem mudar parâmetros de produção. Se surgir evidência consistente, a próxima etapa seria validação PAPER isolada, com recursos limitados e executor incapaz de negociar em MAINNET; nenhum processo contínuo foi iniciado aqui.',
'Limitações adicionais: um ano de histórico e aproximadamente 73 dias OOS; validação única cronológica em vez de múltiplos ciclos walk-forward; preço/spread/slippage modelados e não medidos por trade; ausência de histórico de filtros e tarifas da conta; ausência de latência, book, fills parciais e falhas de rede; drawdown intrabar não capturado; tamanho monetário pequeno das posições e capital hipotético; sem comprovação independente da origem/integridade econômica de cada candle; múltiplas pesquisas anteriores podem ter consumido o mesmo OOS. Não há promessa de lucro futuro.',
'\n## Isolamento e evidência operacional',
'Escritas restritas por audit hook Python a `research/isolated_spot_20261017`; tentativas de ler `.env` bloqueadas; ambiente do processo sem segredos herdados; credenciais vazias; rede bloqueada por audit hook antes dos imports de backtest; subprocessos proibidos. Logging do processo é apenas nulo/padrão sem inicialização dos handlers compartilhados; diagnóstico do engine redirecionado no processo ao diretório exclusivo. Nenhum registro em banco, estado LIVE, registry, scheduler ou catálogo foi gravado. Não houve compra, venda, cancelamento, reinício ou envio de mensagem externa.',
'Pesquisa sequencial em um único núcleo lógico, prioridade Windows Idle e bibliotecas numéricas com uma thread; limite verificado entre etapas de 900 MiB RSS e piso de 2 GiB de memória livre. Não é limite rígido do sistema operacional nem monitoramento em cada candle; RSS observado durante os backtests ficou próximo de 200 MiB. Os demais serviços compartilham CPU, RAM e disco, mas não houve conexão ao MySQL. PIDs LIVE/daemon verificados entre etapas; API, dashboard e MySQL permaneceram intocados.',
'A confirmação final dos processos, timestamps de início, comandos, avanço de ciclos do daemon e mensagens LIVE está em `operational_final.json`. A presença dos mesmos PIDs e atividade dos logs sustenta continuidade operacional, mas não é auditoria autenticada da exchange. A pesquisa não emitiu nem alterou ordens reais; não se afirma que a estratégia LIVE deixou de operar por conta própria durante a janela.',
'\n## Fontes externas consultadas',
'- [Binance Spot API — filtros](https://developers.binance.com/docs/binance-spot-api-docs/filters): LOT_SIZE, MARKET_LOT_SIZE e NOTIONAL; valores concretos usados constam em `exchange_info.json`.',
'- [Binance Academy — backtesting](https://www.binance.com/en/academy/articles/how-to-backtest-a-trading-strategy): contexto de validação histórica; nenhuma rentabilidade externa foi incorporada aos resultados.',
'- As especificações numéricas das candidatas são hipóteses desta pesquisa, não reprodução integral de um paper ou recomendação da Binance.',
'\n## Artefatos',
'`run.py`: protocolo executável isolado; `protocol.json`: parâmetros congelados; `data_manifest.json`: fonte/hash/qualidade; `exchange_info.json`: filtros públicos; `results.csv`/`results.json`: 80 execuções; `trades.csv`: operações simuladas; `robustness.json`: decisões; `verify.py`/`verification.json`: verificações; `protected_source_hashes.json`: hashes de módulos preservados; `operational_final.json`: evidência operacional final.']
(OUT/'REPORT.md').write_text('\n\n'.join(lines),encoding='utf-8')
(OUT/'robustness.json').write_text(json.dumps(robust,indent=2,allow_nan=False))
print('REPORT_READY',len(candidates),'candidate combinations; accepted',len(selected))
