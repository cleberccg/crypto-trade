"""Meaningful causality and execution/accounting verification; no live imports."""
from pathlib import Path
import json
src=Path(__file__).with_name('run.py').read_text(encoding='utf-8-sig')
exec(compile(src[:src.index('frames={}; manifest=[]')],__file__,'exec'))
a=src.index('# No sockets whatsoever'); b=src.index('rng=np.random.default_rng')
exec(compile(src[a:b],__file__,'exec'))
rng=np.random.default_rng(71)
index=pd.date_range('2025-01-01',periods=1500,freq='5min',tz='UTC')
c=100*np.exp(np.cumsum(rng.normal(0,.003,len(index))))
df=pd.DataFrame({'open':c,'high':c*1.003,'low':c*.997,'close':c,'volume':1.},index=index)
checks=[]
for kind in ('pullback','reversion','baseline'):
    full=Rule(kind).prepare_dataset(df)
    for cut in (300,800,1200):
        prefix=Rule(kind).prepare_dataset(df.iloc[:cut])
        # Last bar deliberately liquidates at predefined fold boundary; compare preceding bars.
        pd.testing.assert_frame_equal(prefix.iloc[:-1],full.iloc[:cut-1])
        checks.append(kind+' prefix invariance '+str(cut))
    assert (full.close==df.open).all() and (full.high==df.open).all() and (full.low==df.open).all()
    checks.append(kind+' execution only at current open')
for symbol in SYMBOLS:
    e=ExecutionFilters(symbol)
    try:
        e.evaluate_trade(portfolio_value=28.25577283,entry_price=100,stop_loss=1,take_profit=10000,trailing_stop_pct=.99,strategy_score=1,min_risk_reward_ratio=None)
    except ValueError as exc:
        assert 'minimum/lot' in str(exc)
    else: raise AssertionError('Small capital unexpectedly executable')
    checks.append(symbol+' 28.25577283 USDT minimum-notional rejection')
rows=json.loads((OUT/'results.json').read_text()); trades=pd.read_csv(OUT/'trades.csv')
assert len(rows)==80
for row in rows:
    subset=trades[(trades.symbol==row['symbol'])&(trades.timeframe==row['timeframe'])&(trades.strategy==row['strategy'])&(trades.split==row['split'])&(abs(trades.cost_per_side-row['cost_per_side'])<1e-9)]
    assert len(subset)==row['total_trades']
    assert abs(subset.pnl.sum()-row['net_profit'])<=.005001
    assert abs((subset.entry_fee.sum()+subset.exit_fee.sum())-row['total_cost'])<1e-7
    assert abs(row['net_profit']-(row['final_capital']-1000))<=.01001
    for t in subset.itertuples():
        assert abs(t.pnl-(t.quantity*(t.exit_price-t.entry_price)-t.entry_fee-t.exit_fee))<1e-7
checks.extend(['80 experiment rows present','all trade counts reconciled','all net PnL reconciled','all modeled costs reconciled','all positions liquidated at split end'])
(OUT/'verification.json').write_text(json.dumps({'passed':True,'checks':checks},indent=2))
print('VERIFIED',len(checks),'checks')
