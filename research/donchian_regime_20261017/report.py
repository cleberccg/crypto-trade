from pathlib import Path
import json
import pandas as pd
import numpy as np
out=Path(__file__).resolve().parent
r=pd.read_csv(out/'results.csv'); t=pd.read_csv(out/'trade_diagnostics.csv'); sm=pd.read_csv(out/'small_capital.csv'); bh=pd.read_csv(out/'buy_and_hold.csv')
raw=json.loads((out/'results.json').read_text()); old=pd.read_csv(out.parent/'isolated_spot_20261017/results.csv')
oos=r[(r.split=='OOS')&(r.cost_per_side==.0013)].copy(); stress=r[(r.split=='OOS')&(r.cost_per_side==.0019)]
reference=old[(old.strategy=='baseline')&(old.split=='OOS')&(old.cost_per_side==.0013)].set_index('symbol')
rob=[]
for _,a in oos.iterrows():
    family=r[(r.symbol==a.symbol)&(r.timeframe==a.timeframe)&(r.strategy==a.strategy)]
    z=next(x for x in raw if x['symbol']==a.symbol and x['timeframe']==a.timeframe and x['strategy']==a.strategy and x['split']=='OOS' and x['cost_per_side']==.0013)
    ci=z['block_bootstrap_familywise_ci']; s=stress[(stress.symbol==a.symbol)&(stress.timeframe==a.timeframe)&(stress.strategy==a.strategy)].iloc[0]
    reasons=[]
    if not (family.net_profit>0).all(): reasons.append('PnL negativo em pelo menos um split/custo')
    if a.total_trades<30: reasons.append('menos de 30 trades OOS')
    if a.profit_factor<1.2: reasons.append('PF OOS abaixo de 1,20')
    if ci[0] is None or ci[0]<=0: reasons.append('IC ajustado sem limite inferior positivo')
    if a.trades_per_day<=reference.loc[a.symbol,'trades_per_day']: reasons.append('frequência não supera referência 4h')
    rob.append({'symbol':a.symbol,'timeframe':a.timeframe,'strategy':a.strategy,'approved':not reasons,'reasons':reasons,'ci':ci,'stress_pnl':s.net_profit,'frequency_multiple_4h':a.trades_per_day/reference.loc[a.symbol,'trades_per_day']})
(out/'robustness.json').write_text(json.dumps(rob,indent=2,ensure_ascii=False),encoding='utf-8')
def table(frame,cols):
    q=frame[cols].copy()
    for col in q.columns:
        if pd.api.types.is_numeric_dtype(q[col]): q[col]=q[col].map(lambda x: 'é' if pd.isna(x) else f'{x:.5g}')
    return '| '+' | '.join(cols)+' |\n|'+ '|'.join(['---']*len(cols))+'|\n'+'\n'.join('| '+' | '.join(str(x) for x in row)+' |' for row in q.itertuples(index=False,name=None))
lines=['# Donchian: pesquisa isolada de regime e custos',
'Data de referência: 17/10/2026, America/Sao_Paulo. Conclusao: **'+('há candidata condicional; consultar criterios' if any(x['approved'] for x in rob) else 'nenhuma candidata aprovada para PAPER')+'**. Nenhum executor contínuo foi iniciado.',
'## Diagnostico anterior',
'A pesquisa isolated_spot_20261017 rejeitou 16 combinações de pullback/reversão. Maior frequência, até cerca de quatro trades/dia, acumulou atrito de 0,26% por round trip sem vantagem bruta suficiente. Stops remotos de 99% naquela pesquisa geravam tickets proximos de 10 USDT com capital de 1.000 USDT e inviabilizavam 28 USDT. Aqui o Donchian real usa stop padrão de 2%, alvo de 4%, score 1 e trailing definido pelo motor; portanto a viabilidade de capital deve ser recalculada. Não se compara diretamente o PnL monetário entre regras com exposicoes diferentes.',
'## Inspecao e hipóteses congeladas',
'Reutilizados ClassicDonchianBreakoutStrategy v1 (única versão localizada no catálogo), BacktestEngine/BacktestConfig, RiskManager/PositionSizer, métricas existentes, ATR14 Wilder, compute_adx, adaptador de LOT_SIZE e snapshot exchangeInfo da pesquisa anterior. Não foi chamado serviço científico que persiste em banco. So novos arquivos nesta pasta foram escritos. Nenhuma formula existente foi alterada.',
'A: classe original, janela Donchian 20, entrada close acima do máximo dos 20 candles anteriores. Stop/alvo calculados pela classe real no candle de sinal; saída normal HOLD, gerenciada pelo motor. B: mesma estratégia, com close>SMA200, SMA200 crescente em 20 barras, ADX14é20 e ATR14/close<=3%. C: B mais ATR14/close>=0,52% (duas vezes o custo nominal de ida e volta). ATR é escala de movimento, não previsao de lucro. O piso testa margem de volatilidade sobre custos; o teto evita volatilidade extrema. Valores fixados em protocol.json antes dos testes, sem busca de parâmetros. O piso permanece 0,52% no estresse para isolar o impacto dos custos. Não se testou 5m por falta de justificativa para ampliar a família.',
'## Dados e desenho causal',
'Fonte local data/binance_spot_extended_klines.csv.gz; hash e integridade em data_manifest.json. Cada ativo contém 105.120 candles de 5m entre 01/06/2025 e 01/06/2026 exclusivo; agregacao completa produz 35.040 candles de 15m e 8.760 de 1h. Sem interpolação, duplicatas ou lacunas na janela. O período após 01/06/2026 permanece reservado e não entra nos testes.',
'Separacao cronológica 60/20/20: DEV, validação e OOS (~73 dias, marco-maio de 2026). Capital reiniciado em cada split. Primeiras 220 barras são aquecimento; validação/OOS usam somente passado para aquecer, sem transportar posições. Sinais e níveis do candle fechado são deslocados uma barra; execução na abertura seguinte. Prefixos foram comparados para verificar causalidade. Liquidacao final programada no último open de cada split, com custo.',
'OOS é fora da amostra deste protocolo, mas o mesmo período já foi consultado por pesquisas anteriores. Portanto não constitui holdout inedito e a correcao estatística desta família não cobre todo o histórico de seleção do projeto. Não há treino/otimização dos parâmetros nesta execução.',
'## Execução e custos',
'Capital 1.000 USDT por combinação independente. Padroes do código: risco de 1%, teto de stake de 2% do saldo (cerca de 20 USDT), trailing de 1,5% aplicado pelo motor. Nenhum valor foi lido do .env LIVE. Taxa 0,10%, meio spread 0,01% e slippage 0,02% por lado: 0,13% por lado; estresse aumenta slippage para 0,08%, total 0,19%. Motor debita custos proporcionais, sem simular book. results.csv separa taxa/spread/slippage. PF bruto usa PnL antes desses debitos nos mesmos trades; não é um segundo backtest sem custos. PF líquido usa trades após custos. DD é fração do capital, conforme métricas existentes; multiplicar por 100 para percentual. Win rate também é fração.',
'Quantidades arredondadas para baixo em Decimal pelo stepSize, com minQty/maxQty e NOTIONAL verificados após o dimensionamento original. O snapshot é atual, não histórico; preço médio móvel de MIN_NOTIONAL, limites dinamicos e fills parciais não são reproduzidos. Entradas/saídas são proxy de mercado: PRICE_FILTER de ordens limit/stop não é simulado. Stops/alvos são eventos internos, não ordens enviadas. Precos historicos mantém sua precisão; quantidade respeita o lote.',
'## Resultados completos é capital 1.000 USDT']
cols=['symbol','timeframe','strategy','total_trades','trades_per_day','mean_hours_between_entries','gross_profit_factor','profit_factor','net_profit','win_rate','expectancy_unrounded','max_drawdown_pct','total_cost']
for split in ('DEV','VALIDATION','OOS'):
    lines+=['### '+split+' é custos base',table(r[(r.split==split)&(r.cost_per_side==.0013)],cols)]
lines+=['### OOS é estresse de custos',table(stress,['symbol','timeframe','strategy','total_trades','profit_factor','net_profit','expectancy_unrounded','total_cost']),
'## Regimes e falsos rompimentos',
'Falha rápida: fechamento retorna ao canal anterior nas três barras seguintes ao rompimento. é um diagnóstico ex post; não alimenta sinais. Não equivale necessariamente a trade perdedor. Agrupamento abaixo usa o regime observado na decisão de entrada, não informação futura. Em B/C a entrada já exige regime de tendência; não se pode inferir desempenho desses filtros em outros regimes sem trades.']
d=t[t.split=='OOS'].groupby(['symbol','timeframe','strategy','regime']).agg(trades=('pnl','size'),pnl=('pnl','sum'),mean_pnl=('pnl','mean'),costs=('cost','sum'),quick_fail_rate=('quick_failure_3_bars','mean'),holding_hours=('holding_hours','mean')).reset_index()
d.to_csv(out/'regime_summary.csv',index=False); lines.append(table(d,list(d.columns)))
comparison=[]
for symbol in oos.symbol.unique():
    for tf in ('15min','1h'):
        group=oos[(oos.symbol==symbol)&(oos.timeframe==tf)].set_index('strategy')
        for kind in ('A','B','C'):
            a=group.loc[kind]; b=group.loc['A']
            comparison.append({'symbol':symbol,'timeframe':tf,'strategy':kind,'expectancy_delta_vs_A':a.expectancy_unrounded-b.expectancy_unrounded,'gross_pnl':a.gross_pnl,'costs':a.total_cost,'net_pnl':a.net_profit,'cost_div_abs_gross':a.total_cost/abs(a.gross_pnl) if abs(a.gross_pnl)>1e-12 else None,'frequency_delta_vs_A':a.trades_per_day-b.trades_per_day})
comp=pd.DataFrame(comparison); comp.to_csv(out/'filter_comparison.csv',index=False)
lines+=['### Efeito dos filtros e consumo por custos',table(comp,list(comp.columns)), 'Diferencas de expectativa comparam o resultado médio de cada conjunto de trades, sem pareamento causal. Reduzir prejuízo total ao operar menos não prova melhorar o sinal. A razão custos/|PnL bruto| indica escala do atrito; quando o bruto já é negativo, custos apenas aprofundam a perda. Não é percentagem de lucro consumido nesse caso.']
lines+=['## Robustez e seleção',
'Critérios anteriores preservados: todos os splits positivos, OOS>=30 trades, PF>=1,20, estresse positivo, frequência maior que referência 4h e limite inferior de IC positivo. Bootstrap circular por blocos de cinco trades, 2.000 reamostragens, seed fixa; quantis Bonferroni 0,025/24 e 1é0,025/24 para 24 combinações. Para menos de cinco trades não se estima IC. Intervalos condicionais e exploratorios: dependência de longo prazo e múltiplas pesquisas não são totalmente cobertas; caudas extremas têm baixa resolução. Não são prova formal de edge. Não houve novas variantes após observar resultados.']
rr=pd.DataFrame([{**x,'ci':str(x['ci']),'reasons':'; '.join(x['reasons'])} for x in rob]); lines.append(table(rr,['symbol','timeframe','strategy','ci','stress_pnl','frequency_multiple_4h','approved','reasons']))
best=oos.sort_values('net_profit',ascending=False).iloc[0]
lines+=['### Diagnostico da melhor observacao OOS',f"Maior PnL OOS: {best.symbol} {best.timeframe}, variante {best.strategy}, {int(best.total_trades)} trades, PF líquido {best.profit_factor:.4f}, PnL {best.net_profit:.2f} USDT e {best.trades_per_day:.3f} trades/dia. Isto não constitui aprovacao. BNB 1h B teve desenvolvimento e validação negativos, apenas dez trades OOS e frequência inferior é referência BNB 4h de aproximadamente 0,178 trades/dia. ETH 15m C teve apenas sete trades e perdeu no estresse; BNB 1h C teve nove trades e também perdeu no estresse. Todas as demais observacoes tiveram PnL OOS base negativo. Nenhuma satisfaz expectativa positiva robusta com maior frequência."]
lines+=['## Buy-and-hold e capital pequeno',
'Buy-and-hold compra na primeira abertura negociavel e vende na última, com custo base, saldo residual e lote arredondado. Usa capital integral disponível; diferencas de exposicao impedem atribuir todo diferencial a qualidade de sinal. é um único trade por split: PF e significancia da expectativa não são informativos. DD marcado nas aberturas.',table(bh[(bh.split=='OOS')&(bh.capital==1000)],['symbol','timeframe','net_pnl','return_fraction','costs','max_drawdown_fraction']),
'Foram executados 24 backtests OOS separados com 28 USDT, sem mudar risco, stake ou proteções. Todos terminaram com zero trades: o teto de stake de 2% gera ticket de no máximo 0,56 USDT, abaixo do mínimo de 5 USDT nos quatro pares, antes mesmo do arredondamento. Alterar risco/stake para contornar isso está fora do protocolo. PnL zero e PF zero nesses testes significam ausência de operações, não desempenho neutro de uma estratégia executável. Rejeicoes são tentativas barradas pelo lote/notional, não erros nem ordens. Executavel não significa rentavel e não garante execução em MAINNET com filtros e saldo futuros.',table(sm,['symbol','timeframe','strategy','total_trades','lot_rejections','profit_factor','net_profit','final_capital']),
'## Limitações materiais',
'O motor existente verifica stop/alvo e trailing usando OHLC. Nesta pesquisa OHLC de execução é substituido pelo open, eliminando ambiguidade intrabar/look-ahead na entrada. Consequencia: barreiras tocadas somente intrabar não são capturadas e o drawdown pode ser subestimado; gaps podem ser preenchidos no nível do stop/alvo pelo motor, preço otimista no caso de stop. Não se alterou o algoritmo científico existente. A reprodução A é fiel aos sinais e níveis originais, mas usa esta convenção de execução, não uma reprodução de fills reais. Qualquer resultado positivo exige motor validado para gaps e intrabar antes de promocao. O uso de stop calculado no fechamento anterior mantém risco sujeito ao gap até a abertura.',
'Um ano e um único OOS são insuficientes para validar todos os regimes. Spread/slippage são hipóteses fixas. Não há latência, impacto, medicao de book, auditoria autenticada de conta ou garantia da procedencia econômica dos candles. Métricas pequenas refletem dimensionamento e saldo não alocado; não representam retorno de uma carteira multiestrategia.',
'## Decisão e próxima etapa',
'Nenhuma variante deve ser promovida automaticamente. Consultar robustness.json para cada motivo de rejeição; resultado positivo isolado, poucos trades ou melhoria relativa sobre A não bastam. Aumentar frequência sem melhorar expectativa líquida não atende ao objetivo. A referência de frequência é a simulação histórica Sma200RegimeGated 4h anterior (BNB é o par do LIVE), não uma estimativa autenticada de trades da conta.',
'Próxima etapa: manter todas sem implantacao; primeiro validar execução intrabar/gaps em ambiente separado e obter período prospectivo ainda não consumido por seleção. Não ampliar variantes ou timeframes para perseguir resultado positivo. Nenhum PAPER contínuo ou LIVE foi iniciado.',
'## Isolamento e evidência operacional',
'Ambiente sem segredos herdados, .env bloqueado, rede e subprocessos bloqueados no processo científico, escritas limitadas a esta pasta por audit hook, uma CPU lógica, prioridade Idle e bibliotecas numéricas com uma thread. Limites de 900 MiB RSS e 2 GiB livres conferidos entre etapas, sem limite rígido por candle. Backtests sequenciais. operational_initial.json e operational_final.json registram PIDs e criacao dos processos; hashes verificam módulos preservados. Nenhuma conexão ao MySQL, emissao, cancelamento ou alteração de ordem pela pesquisa. O worker LIVE pode ter negociado normalmente; não foi feita auditoria autenticada da Binance.',
'## Reproducao e arquivos',
'Na raiz do projeto, executar sequencialmente `python -B research/donchian_regime_20261017/run.py`, `python -B research/donchian_regime_20261017/analyze.py`, `python -B research/donchian_regime_20261017/check_operations.py` e `python -B research/donchian_regime_20261017/report.py`. O protocolo interrompe a pesquisa se os PIDs originais não estiverem presentes; em outra sessão, adaptar somente os PIDs após inspeção operacional, nunca iniciar executores.',
'96 backtests principais + 24 de capital pequeno; 48 referências analiticas buy-and-hold (4 ativos é 2 timeframes é 3 splits é 2 capitais). results.csv contém IS/OOS/custos, trades.csv os trades completos, trade_diagnostics.csv/regime_summary.csv os diagnósticos, small_capital.csv e buy_and_hold.csv as referências; verification.json registra verificações causais/contabeis.',
'Fonte normativa consultada: [Binance Spot é filtros](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md). Valores concretos usados estão em exchange_info.json.']
(out/'REPORT.md').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')
print('REPORT generated; approvals',sum(x['approved'] for x in rob))
