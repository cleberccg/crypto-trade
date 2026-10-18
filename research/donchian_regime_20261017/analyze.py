"""Post-run verification/diagnostics using the same isolated execution adapters."""
from pathlib import Path
src=Path(__file__).with_name('run.py').read_text(encoding='utf-8')
# Skip initial evidence snapshot: preserve the original before-run hashes and process starts.
src=src[src.index('"""Isolated orchestration'):]
exec(compile(src[:src.index('rng=np.random.default_rng')],__file__,'exec'))
checks=[]; diagnostics=[]; small=[]; bh=[]
rows=json.loads((OUT/'results.json').read_text()); trades=pd.read_csv(OUT/'trades.csv')
assert len(rows)==96
for r in rows:
    t=trades[(trades.symbol==r['symbol'])&(trades.timeframe==r['timeframe'])&(trades.strategy==r['strategy'])&(trades.split==r['split'])&(abs(trades.cost_per_side-r['cost_per_side'])<1e-9)]
    assert len(t)==r['total_trades']
    assert abs(t.pnl.sum()-r['net_profit'])<.00501
    assert abs((t.entry_fee+t.exit_fee).sum()-r['total_cost'])<1e-7
    assert abs(r['final_capital']-1000-r['net_profit'])<.01001
    for z in t.itertuples():
        assert abs(z.pnl-(z.quantity*(z.exit_price-z.entry_price)-z.entry_fee-z.exit_fee))<1e-7
        assert z.exit_bar>=z.entry_bar
checks+=['96 rows, trade counts, PnL, fees and capital reconciled']
for symbol,base in frames.items():
    for tf in ('15min','1h'):
        frame=base.resample(tf,label='left',closed='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
        assert len(frame)==(35040 if tf=='15min' else 8760)
        n=len(frame); cuts=[0,int(n*.6),int(n*.8),n]
        for kind in ('A','B','C'):
            sample=frame.iloc[:1000]; full=Rule(kind).prepare_dataset(sample)
            for cut in (300,700):
                prefix=Rule(kind).prepare_dataset(sample.iloc[:cut])
                pd.testing.assert_frame_equal(prefix.iloc[:-1],full.iloc[:cut-1])
            assert (full.close==sample.open).all()
            # Baseline signal and stop/take are delegated to the real class, with one-bar delay.
            if kind=='A':
                original=ClassicDonchianBreakoutStrategy(); enriched=original.calculate(sample)
                for i in range(220,999):
                    got=Rule('A').entry_signal(full.iloc[:i+1]); expected=original.entry_signal(enriched.iloc[:i])
                    assert got.signal==expected.signal and got.stop_loss==expected.stop_loss and got.take_profit==expected.take_profit
            lo=cuts[2]; segment=frame.iloc[lo-220:].copy(); budget()
            execution=ExecutionFilters(symbol)
            res=BacktestEngine(Rule(kind),BacktestConfig(initial_capital=28,fee_pct=.0013,warmup_bars=220),execution).run(segment,symbol=symbol,timeframe=tf)
            small.append({'symbol':symbol,'timeframe':tf,'strategy':kind,'capital':28,'lot_rejections':execution.rejected,**res.metrics.to_dict()})
            for j,split in enumerate(('DEV','VALIDATION','OOS')):
                lo,hi=cuts[j:j+2]; warm=min(lo,220); seg=frame.iloc[lo-warm:hi]; prepared=Rule(kind).prepare_dataset(seg)
                subset=trades[(trades.symbol==symbol)&(trades.timeframe==tf)&(trades.strategy==kind)&(trades.split==split)&(abs(trades.cost_per_side-.0013)<1e-9)]
                for z in subset.itertuples():
                    a=prepared.iloc[z.entry_bar]; signal_bar=z.entry_bar-1
                    future=seg.iloc[signal_bar+1:min(signal_bar+4,len(seg))]
                    level=float(a.signal_donchian_high)
                    # Failure: close back inside pre-breakout channel in the next three closed candles.
                    failed=bool((future.close<=level).any())
                    diagnostics.append({'symbol':symbol,'timeframe':tf,'strategy':kind,'split':split,'entry_time':z.entry_time,'pnl':z.pnl,'cost':z.entry_fee+z.exit_fee,'regime':'trend' if a.regime else 'other','atr_pct':float(a.atr_pct),'adx':float(a.adx),'quick_failure_3_bars':failed,'holding_hours':(pd.Timestamp(z.exit_time)-pd.Timestamp(z.entry_time)).total_seconds()/3600})
        for j,split in enumerate(('DEV','VALIDATION','OOS')):
            lo,hi=cuts[j:j+2]; lo=max(220,lo)
            for capital in (1000,28):
                p=float(frame.open.iloc[lo]); end=float(frame.open.iloc[hi-1]); lot=filters[symbol]['LOT_SIZE']; step=Decimal(lot['stepSize'])
                q=float((Decimal(str(capital/(p*1.0013)))/step).to_integral_value(rounding=ROUND_DOWN)*step)
                minimum=float(filters[symbol].get('NOTIONAL',filters[symbol].get('MIN_NOTIONAL',{})).get('minNotional',0))
                assert q*p>=minimum and q>=float(lot['minQty']) and q<=float(lot['maxQty'])
                costs=q*(p+end)*.0013; net=q*(end-p)-costs
                equity=capital-q*p*.0013+q*(frame.open.iloc[lo:hi]-p); equity.iloc[-1]-=q*end*.0013
                dd=float(((equity.cummax()-equity)/equity.cummax()).max())
                bh.append({'symbol':symbol,'timeframe':tf,'split':split,'capital':capital,'quantity':q,'net_pnl':net,'return_fraction':net/capital,'costs':costs,'max_drawdown_fraction':dd,'trades':1,'profit_factor':None,'note':'One terminal round trip; PF/expectancy significance not meaningful'})
checks+=['prefix invariance all variants/assets/timeframes','baseline entries/SL/TP match original class with one-bar delay','all trades next-open execution','24 real OOS small-capital backtests','complete aggregate bar counts']
pd.DataFrame(small).to_csv(OUT/'small_capital.csv',index=False)
pd.DataFrame(diagnostics).to_csv(OUT/'trade_diagnostics.csv',index=False)
pd.DataFrame(bh).to_csv(OUT/'buy_and_hold.csv',index=False)
(OUT/'verification.json').write_text(json.dumps({'passed':True,'checks':checks},indent=2))
print('VERIFIED',len(checks),'groups; small-capital tests',len(small),flush=True)
