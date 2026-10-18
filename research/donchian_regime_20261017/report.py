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
    if a.trades_per_day<=reference.loc[a.symbol,'trades_per_day']: reasons.append('frequ?ncia n?o supera refer?ncia 4h')
    rob.append({'symbol':a.symbol,'timeframe':a.timeframe,'strategy':a.strategy,'approved':not reasons,'reasons':reasons,'ci':ci,'stress_pnl':s.net_profit,'frequency_multiple_4h':a.trades_per_day/reference.loc[a.symbol,'trades_per_day']})
(out/'robustness.json').write_text(json.dumps(rob,indent=2,ensure_ascii=False),encoding='utf-8')
def table(frame,cols):
    q=frame[cols].copy()
    for col in q.columns:
        if pd.api.types.is_numeric_dtype(q[col]): q[col]=q[col].map(lambda x: '?' if pd.isna(x) else f'{x:.5g}')
    return '| '+' | '.join(cols)+' |\n|'+ '|'.join(['---']*len(cols))+'|\n'+'\n'.join('| '+' | '.join(str(x) for x in row)+' |' for row in q.itertuples(index=False,name=None))
lines=['# Donchian: pesquisa isolada de regime e custos',
'Data de refer?ncia: 17/10/2026, America/Sao_Paulo. Conclus?o: **'+('h? candidata condicional; consultar crit?rios' if any(x['approved'] for x in rob) else 'nenhuma candidata aprovada para PAPER')+'**. Nenhum executor cont?nuo foi iniciado.',
'## Diagn?stico anterior',
'A pesquisa isolated_spot_20261017 rejeitou 16 combina??es de pullback/revers?o. Maior frequ?ncia, at? cerca de quatro trades/dia, acumulou atrito de 0,26% por round trip sem vantagem bruta suficiente. Stops remotos de 99% naquela pesquisa geravam tickets pr?ximos de 10 USDT com capital de 1.000 USDT e inviabilizavam 28 USDT. Aqui o Donchian real usa stop padr?o de 2%, alvo de 4%, score 1 e trailing definido pelo motor; portanto a viabilidade de capital deve ser recalculada. N?o se compara diretamente o PnL monet?rio entre regras com exposi??es diferentes.',
'## Inspe??o e hip?teses congeladas',
'Reutilizados ClassicDonchianBreakoutStrategy v1 (?nica vers?o localizada no cat?logo), BacktestEngine/BacktestConfig, RiskManager/PositionSizer, m?tricas existentes, ATR14 Wilder, compute_adx, adaptador de LOT_SIZE e snapshot exchangeInfo da pesquisa anterior. N?o foi chamado servi?o cient?fico que persiste em banco. S? novos arquivos nesta pasta foram escritos. Nenhuma f?rmula existente foi alterada.',
'A: classe original, janela Donchian 20, entrada close acima do m?ximo dos 20 candles anteriores. Stop/alvo calculados pela classe real no candle de sinal; sa?da normal HOLD, gerenciada pelo motor. B: mesma estrat?gia, com close>SMA200, SMA200 crescente em 20 barras, ADX14?20 e ATR14/close?3%. C: B mais ATR14/close?0,52% (duas vezes o custo nominal de ida e volta). ATR ? escala de movimento, n?o previs?o de lucro. O piso testa margem de volatilidade sobre custos; o teto evita volatilidade extrema. Valores fixados em protocol.json antes dos testes, sem busca de par?metros. O piso permanece 0,52% no estresse para isolar o impacto dos custos. N?o se testou 5m por falta de justificativa para ampliar a fam?lia.',
'## Dados e desenho causal',
'Fonte local data/binance_spot_extended_klines.csv.gz; hash e integridade em data_manifest.json. Cada ativo cont?m 105.120 candles de 5m entre 01/06/2025 e 01/06/2026 exclusivo; agrega??o completa produz 35.040 candles de 15m e 8.760 de 1h. Sem interpola??o, duplicatas ou lacunas na janela. O per?odo ap?s 01/06/2026 permanece reservado e n?o entra nos testes.',
'Separa??o cronol?gica 60/20/20: DEV, valida??o e OOS (~73 dias, mar?o?maio de 2026). Capital reiniciado em cada split. Primeiras 220 barras s?o aquecimento; valida??o/OOS usam somente passado para aquecer, sem transportar posi??es. Sinais e n?veis do candle fechado s?o deslocados uma barra; execu??o na abertura seguinte. Prefixos foram comparados para verificar causalidade. Liquida??o final programada no ?ltimo open de cada split, com custo.',
'OOS ? fora da amostra deste protocolo, mas o mesmo per?odo j? foi consultado por pesquisas anteriores. Portanto n?o constitui holdout in?dito e a corre??o estat?stica desta fam?lia n?o cobre todo o hist?rico de sele??o do projeto. N?o h? treino/otimiza??o dos par?metros nesta execu??o.',
'## Execu??o e custos',
'Capital 1.000 USDT por combina??o independente. Taxa 0,10%, meio spread 0,01% e slippage 0,02% por lado: 0,13% por lado; estresse aumenta slippage para 0,08%, total 0,19%. Motor debita custos proporcionais, sem simular book. results.csv separa taxa/spread/slippage. PF bruto usa PnL antes desses d?bitos nos mesmos trades; n?o ? um segundo backtest sem custos. PF l?quido usa trades ap?s custos. DD ? fra??o do capital, conforme m?tricas existentes; multiplicar por 100 para percentual. Win rate tamb?m ? fra??o.',
'Quantidades arredondadas para baixo em Decimal pelo stepSize, com minQty/maxQty e NOTIONAL verificados ap?s o dimensionamento original. O snapshot ? atual, n?o hist?rico; pre?o m?dio m?vel de MIN_NOTIONAL, limites din?micos e fills parciais n?o s?o reproduzidos. Entradas/sa?das s?o proxy de mercado: PRICE_FILTER de ordens limit/stop n?o ? simulado. Stops/alvos s?o eventos internos, n?o ordens enviadas. Pre?os hist?ricos mant?m sua precis?o; quantidade respeita o lote.',
'## Resultados completos ? capital 1.000 USDT']
cols=['symbol','timeframe','strategy','total_trades','trades_per_day','mean_hours_between_entries','gross_profit_factor','profit_factor','net_profit','win_rate','expectancy_unrounded','max_drawdown_pct','total_cost']
for split in ('DEV','VALIDATION','OOS'):
    lines+=['### '+split+' ? custos base',table(r[(r.split==split)&(r.cost_per_side==.0013)],cols)]
lines+=['### OOS ? estresse de custos',table(stress,['symbol','timeframe','strategy','total_trades','profit_factor','net_profit','expectancy_unrounded','total_cost']),
'## Regimes e falsos rompimentos',
'Falha r?pida: fechamento retorna ao canal anterior nas tr?s barras seguintes ao rompimento. ? um diagn?stico ex post; n?o alimenta sinais. N?o equivale necessariamente a trade perdedor. Agrupamento abaixo usa o regime observado na decis?o de entrada, n?o informa??o futura. Em B/C a entrada j? exige regime de tend?ncia; n?o se pode inferir desempenho desses filtros em outros regimes sem trades.']
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
lines+=['### Efeito dos filtros e consumo por custos',table(comp,list(comp.columns)), 'Diferen?as de expectativa comparam o resultado m?dio de cada conjunto de trades, sem pareamento causal. Reduzir preju?zo total ao operar menos n?o prova melhorar o sinal. A raz?o custos/|PnL bruto| indica escala do atrito; quando o bruto j? ? negativo, custos apenas aprofundam a perda. N?o ? percentagem de lucro consumido nesse caso.']
lines+=['## Robustez e sele??o',
'Crit?rios anteriores preservados: todos os splits positivos, OOS?30 trades, PF?1,20, estresse positivo, frequ?ncia maior que refer?ncia 4h e limite inferior de IC positivo. Bootstrap circular por blocos de cinco trades, 2.000 reamostragens, seed fixa; quantis Bonferroni 0,025/24 e 1?0,025/24 para 24 combina??es. Para menos de cinco trades n?o se estima IC. Intervalos condicionais e explorat?rios: depend?ncia de longo prazo e m?ltiplas pesquisas n?o s?o totalmente cobertas; caudas extremas t?m baixa resolu??o. N?o s?o prova formal de edge. N?o houve novas variantes ap?s observar resultados.']
rr=pd.DataFrame([{**x,'ci':str(x['ci']),'reasons':'; '.join(x['reasons'])} for x in rob]); lines.append(table(rr,['symbol','timeframe','strategy','ci','stress_pnl','frequency_multiple_4h','approved','reasons']))
lines+=['## Buy-and-hold e capital pequeno',
'Buy-and-hold compra na primeira abertura negoci?vel e vende na ?ltima, com custo base, saldo residual e lote arredondado. Usa capital integral dispon?vel; diferen?as de exposi??o impedem atribuir todo diferencial a qualidade de sinal. ? um ?nico trade por split: PF e signific?ncia da expectativa n?o s?o informativos. DD marcado nas aberturas.',table(bh[(bh.split=='OOS')&(bh.capital==1000)],['symbol','timeframe','net_pnl','return_fraction','costs','max_drawdown_fraction']),
'Foram executados 24 backtests OOS separados com 28 USDT, sem mudar risco, stake ou prote??es. Rejei??es s?o tentativas barradas pelo lote/notional, n?o erros nem ordens. Execut?vel n?o significa rent?vel e n?o garante execu??o em MAINNET com filtros e saldo futuros.',table(sm,['symbol','timeframe','strategy','total_trades','lot_rejections','profit_factor','net_profit','final_capital']),
'## Limita??es materiais',
'O motor existente verifica stop/alvo e trailing usando OHLC. Nesta pesquisa OHLC de execu??o ? substitu?do pelo open, eliminando ambiguidade intrabar/look-ahead na entrada. Consequ?ncia: barreiras tocadas somente intrabar n?o s?o capturadas e o drawdown pode ser subestimado; gaps podem ser preenchidos no n?vel do stop/alvo pelo motor, pre?o otimista no caso de stop. N?o se alterou o algoritmo cient?fico existente. A reprodu??o A ? fiel aos sinais e n?veis originais, mas usa esta conven??o de execu??o, n?o uma reprodu??o de fills reais. Qualquer resultado positivo exige motor validado para gaps e intrabar antes de promo??o. O uso de stop calculado no fechamento anterior mant?m risco sujeito ao gap at? a abertura.',
'Um ano e um ?nico OOS s?o insuficientes para validar todos os regimes. Spread/slippage s?o hip?teses fixas. N?o h? lat?ncia, impacto, medi??o de book, auditoria autenticada de conta ou garantia da proced?ncia econ?mica dos candles. M?tricas pequenas refletem dimensionamento e saldo n?o alocado; n?o representam retorno de uma carteira multiestrat?gia.',
'## Decis?o e pr?xima etapa',
'Nenhuma variante deve ser promovida automaticamente. Consultar robustness.json para cada motivo de rejei??o; resultado positivo isolado, poucos trades ou melhoria relativa sobre A n?o bastam. Aumentar frequ?ncia sem melhorar expectativa l?quida n?o atende ao objetivo. A refer?ncia de frequ?ncia ? a simula??o hist?rica Sma200RegimeGated 4h anterior (BNB ? o par do LIVE), n?o uma estimativa autenticada de trades da conta.',
'Pr?xima etapa: manter todas sem implanta??o; primeiro validar execu??o intrabar/gaps em ambiente separado e obter per?odo prospectivo ainda n?o consumido por sele??o. N?o ampliar variantes ou timeframes para perseguir resultado positivo. Nenhum PAPER cont?nuo ou LIVE foi iniciado.',
'## Isolamento e evid?ncia operacional',
'Ambiente sem segredos herdados, .env bloqueado, rede e subprocessos bloqueados no processo cient?fico, escritas limitadas a esta pasta por audit hook, uma CPU l?gica, prioridade Idle e bibliotecas num?ricas com uma thread. Limites de 900 MiB RSS e 2 GiB livres conferidos entre etapas, sem limite r?gido por candle. Backtests sequenciais. operational_initial.json e operational_final.json registram PIDs e cria??o dos processos; hashes verificam m?dulos preservados. Nenhuma conex?o ao MySQL, emiss?o, cancelamento ou altera??o de ordem pela pesquisa. O worker LIVE pode ter negociado normalmente; n?o foi feita auditoria autenticada da Binance.',
'## Reprodu??o e arquivos',
'Na raiz do projeto, executar sequencialmente `python -B research/donchian_regime_20261017/run.py`, `python -B research/donchian_regime_20261017/analyze.py`, `python -B research/donchian_regime_20261017/check_operations.py` e `python -B research/donchian_regime_20261017/report.py`. O protocolo interrompe a pesquisa se os PIDs originais n?o estiverem presentes; em outra sess?o, adaptar somente os PIDs ap?s inspe??o operacional, nunca iniciar executores.',
'96 backtests principais + 24 de capital pequeno; 48 refer?ncias anal?ticas buy-and-hold (4 ativos ? 2 timeframes ? 3 splits ? 2 capitais). results.csv cont?m IS/OOS/custos, trades.csv os trades completos, trade_diagnostics.csv/regime_summary.csv os diagn?sticos, small_capital.csv e buy_and_hold.csv as refer?ncias; verification.json registra verifica??es causais/cont?beis.',
'Fonte normativa consultada: [Binance Spot ? filtros](https://github.com/binance/binance-spot-api-docs/blob/master/filters.md). Valores concretos usados est?o em exchange_info.json.']
(out/'REPORT.md').write_text('\n\n'.join(lines)+'\n',encoding='utf-8')
print('REPORT generated; approvals',sum(x['approved'] for x in rob))
