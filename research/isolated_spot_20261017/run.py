"""Isolated orchestration of existing engine/indicators; never imports live executors."""
from pathlib import Path
import os, sys, json, time, hashlib, io, zipfile, urllib.request, logging, socket, types
ROOT=Path(__file__).resolve().parents[2]
OUT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT))
for key in list(os.environ):
    if key.upper() not in {'SYSTEMROOT','WINDIR','PATH','TEMP','TMP','USERPROFILE'}:
        del os.environ[key]
for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS'):
    os.environ[key]='1'
os.environ['DATABASE_URL']='sqlite:///:memory:'
os.environ['BINANCE_API_KEY']=''
os.environ['BINANCE_API_SECRET']=''
import psutil
proc=psutil.Process()
proc.nice(psutil.IDLE_PRIORITY_CLASS)
proc.cpu_affinity([proc.cpu_affinity()[-1]])
def budget():
    if proc.memory_info().rss>900*1024**2 or psutil.virtual_memory().available<2*1024**3:
        raise RuntimeError('Research resource budget exceeded')
    if not all(psutil.pid_exists(p) for p in (80848,17832)):
        raise RuntimeError('Original production PID no longer present; stop research only')
    time.sleep(.15)
def audit(event,args):
    if event=='open':
        path,mode,flags=args
        if isinstance(path,(str,bytes)):
            p=Path(os.fsdecode(path)).resolve()
            if p.name.startswith('.env'):
                raise PermissionError('Production environment access forbidden')
            if (isinstance(mode,str) and any(c in mode for c in 'wax+')) or (isinstance(flags,int) and flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC)):
                if not p.is_relative_to(OUT):
                    raise PermissionError('Write outside isolated research forbidden: '+str(p))
    if event in ('subprocess.Popen','os.system'):
        raise PermissionError('Child processes forbidden')
sys.addaudithook(audit)
import pandas as pd
import numpy as np
SYMBOLS=['BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT']
START=pd.Timestamp('2025-06-01',tz='UTC'); END=pd.Timestamp('2026-06-01',tz='UTC')
protocol={'start':str(START),'end_exclusive':str(END),'split':[.6,.2,.2], 'capital_usdt':1000,'fee_per_side':.001,'half_spread_per_side':.0001,'slippage_per_side':.0002,'stress_total_per_side':.0019,'thresholds':{'oos_trades':30,'oos_pf':1.2,'bootstrap_mean_lower_gt':0,'multiple_tests':16},'live_pids':[80848,17832], 'no_parameter_search':True}
(OUT/'protocol.json').write_text(json.dumps(protocol,indent=2))
frames={}; manifest=[]
source=ROOT/'data/binance_spot_extended_klines.csv.gz'
parts={s:[] for s in SYMBOLS}
for chunk in pd.read_csv(source,chunksize=80000,usecols=['open_time','open','high','low','close','volume','symbol','timeframe']):
    budget()
    # Filter text dates before timestamp conversion; reserved holdout never enters a backtest.
    chunk=chunk[(chunk.timeframe=='5m') & (chunk.open_time>='2025-06-01') & (chunk.open_time<'2026-06-01')]
    for symbol,g in chunk.groupby('symbol'):
        key=symbol.replace('/','')
        if key in parts:
            g=g.set_index(pd.to_datetime(g.open_time,utc=True))
            parts[key].append(g[['open','high','low','close','volume']])
manifest.append({'source':str(source.relative_to(ROOT)),'sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'rejected_cache':'BTC 5m parquet missing 8064 candles in requested window; not used','public_archive_download':'DNS failed; complete existing extended kline CSV used for all four assets'})
for symbol in SYMBOLS:
    budget()
    frame=pd.concat(parts.pop(symbol)).sort_index()
    expected=pd.date_range(START,END,freq='5min',inclusive='left')
    quality={'symbol':symbol,'rows':len(frame),'duplicates':int(frame.index.duplicated().sum()),'missing':len(expected.difference(frame.index)),'nonpositive':int((frame[['open','high','low','close']]<=0).any(axis=1).sum()),'nan':int(frame.isna().any(axis=1).sum()),'invalid_ohlc':int(((frame.high<frame[['open','close','low']].max(axis=1))|(frame.low>frame[['open','close','high']].min(axis=1))).sum())}
    manifest.append(quality)
    if any(quality[k] for k in ('duplicates','missing','nonpositive','nan','invalid_ohlc')):
        raise RuntimeError('Invalid historical data '+str(quality))
    frames[symbol]=frame
(OUT/'data_manifest.json').write_text(json.dumps(manifest,indent=2))
# No sockets whatsoever after data preparation, including DB connections.
def blocked(*a,**k): raise PermissionError('Network disabled in backtest process')
sys.addaudithook(lambda event,args: blocked() if event in ('socket.connect','socket.bind','socket.getaddrinfo') else None)
fake=types.ModuleType('dotenv'); fake.load_dotenv=lambda *a,**k:False; sys.modules['dotenv']=fake
logging.disable(logging.CRITICAL)
log_stub=types.ModuleType('utils.logger'); log_stub.get_logger=logging.getLogger; log_stub.setup_logger=lambda name,**kw: logging.getLogger(name); sys.modules['utils.logger']=log_stub
from backtesting.engine import BacktestEngine,BacktestConfig
import backtesting.engine as engine_module
engine_module._EXEC_DIAG_PATH=OUT/'execution_diagnostic.txt'
from backtesting.metrics import compute_metrics
from strategies.base_strategy import BaseStrategy,StrategySignal,SignalType
from research.external_strategy_replication_strategies import compute_adx,Sma200RegimeGatedStrategy
from indicators.rsi import RSI
from risk.risk_manager import RiskManager
from dataclasses import replace
from decimal import Decimal,ROUND_DOWN
info=json.loads((OUT/'exchange_info.json').read_text())
filters={s['symbol']:{f['filterType']:f for f in s['filters']} for s in info['symbols']}
class ExecutionFilters:
    """Research execution adapter: unmodified risk calculation followed by exchange lot rounding."""
    def __init__(self,symbol): self.risk=RiskManager(); self.f=filters[symbol]; self.rejected=0
    def evaluate_trade(self,**kw):
        r=self.risk.evaluate_trade(**kw); lot=self.f['LOT_SIZE']; step=Decimal(lot['stepSize'])
        q=float((Decimal(str(r.quantity_after_cap))/step).to_integral_value(rounding=ROUND_DOWN)*step)
        value=q*kw['entry_price']; minimum=float(self.f.get('NOTIONAL',self.f.get('MIN_NOTIONAL',{})).get('minNotional',0))
        if q<float(lot['minQty']) or q>float(lot['maxQty']) or value<minimum:
            self.rejected+=1; raise ValueError('Exchange minimum/lot rejection')
        return replace(r,quantity=q,quantity_after_cap=q,stake_amount=value)
    def check_trailing_stop(self,*a): return self.risk.check_trailing_stop(*a)
class Rule(BaseStrategy):
    def __init__(self,kind): self.kind=kind
    @property
    def name(self): return 'Isolated_'+self.kind
    def initialize(self): pass
    def calculate(self,df): return self.prepare_dataset(df)
    def prepare_dataset(self,df,**kw):
        c=df.close; sma=c.rolling(200).mean(); ema=c.ewm(span=20,adjust=False).mean(); adx=compute_adx(df); rsi=RSI(14).calculate(df)
        if self.kind=='pullback':
            buy=(c>sma)&(sma>sma.shift(20))&(adx>=20)&(c.shift(1)<ema.shift(1))&(c>ema)&(rsi>=40)&(rsi<=65)
            sell=(c<ema)|(c<sma)|(rsi>=70)
        elif self.kind=='reversion':
            mean=c.rolling(20).mean(); sd=c.rolling(20).std()
            regime=(adx<20)&((c/sma-1).abs()<.05)
            buy=regime&(c<mean-2*sd)&(rsi<35)
            sell=(c>=mean)|(adx>=25)|(c<sma*.95)
        else:
            s=Sma200RegimeGatedStrategy(); s.initialize(); enriched=s.prepare_dataset(df)
            if enriched is None: enriched=s.calculate(df)
            buy=pd.Series([s.entry_signal(enriched.iloc[:i+1]).signal==SignalType.BUY for i in range(len(df))],index=df.index)
            sell=pd.Series([s.exit_signal(enriched.iloc[:i+1],float(c.iloc[i])).signal==SignalType.SELL for i in range(len(df))],index=df.index)
        out=df.copy(); out['buy']=buy.shift(1,fill_value=False); out['sell']=sell.shift(1,fill_value=False)
        # Point-in-time execution dataset: only current open is visible to engine stops/equity.
        for col in ('close','high','low'): out[col]=df.open
        out.iloc[-1,out.columns.get_loc('buy')]=False
        out.iloc[-1,out.columns.get_loc('sell')]=True
        return out
    def entry_signal(self,df):
        a=df.iloc[-1]; p=float(a.close)
        return StrategySignal(SignalType.BUY if bool(a.buy) and not bool(a.sell) else SignalType.HOLD,p,a.name.to_pydatetime(),score=1,stop_loss=p*.01,take_profit=p*100,trailing_stop_pct=.99)
    def exit_signal(self,df,entry_price):
        a=df.iloc[-1]; return StrategySignal(SignalType.SELL if bool(a.sell) else SignalType.HOLD,float(a.close),a.name.to_pydatetime())
    def score(self,df): return 1.
rng=np.random.default_rng(20261017)
rows=[]; trades_all=[]
for symbol,base in frames.items():
    for tf,kind in [(tf,k) for tf in ('5min','15min') for k in ('pullback','reversion')]+[('4h','baseline')]:
        budget()
        frame=base.resample(tf,label='left',closed='left').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'})
        n=len(frame); cuts=[0,int(n*.6),int(n*.8),n]
        for j,split in enumerate(('DEV','VALIDATION','OOS')):
            lo,hi=cuts[j:j+2]; warm=min(lo,220); segment=frame.iloc[lo-warm:hi].copy()
            start=max(220,warm)
            for cost in ([.0013,.0019] if split=='OOS' else [.0013]):
                budget(); execution=ExecutionFilters(symbol)
                result=BacktestEngine(Rule(kind),BacktestConfig(initial_capital=1000,fee_pct=cost,warmup_bars=start),execution).run(segment,symbol=symbol,timeframe=tf)
                m=result.metrics.to_dict(); pnl=np.array([t['pnl'] for t in result.trades]); days=(segment.index[-1]-segment.index[start]).total_seconds()/86400
                costs=sum(t['entry_fee']+t['exit_fee'] for t in result.trades)
                # Moving blocks of five consecutive trades; familywise CI across sixteen candidate combinations.
                means=[]
                if len(pnl)>=5:
                    for _ in range(2000):
                        ix=(rng.integers(0,len(pnl),size=(len(pnl)+4)//5)[:,None]+np.arange(5))%len(pnl)
                        means.append(float(pnl[ix.ravel()[:len(pnl)]].mean()))
                ci=[float(np.quantile(means,.025/16)),float(np.quantile(means,1-.025/16))] if means else [None,None]
                row={'symbol':symbol,'timeframe':tf,'strategy':kind,'split':split,'cost_per_side':cost,'start':str(segment.index[start]),'end':str(segment.index[-1]),'days':days,'trades_per_day':len(pnl)/days,**m,'expectancy_unrounded':float(pnl.mean()) if len(pnl) else 0,'total_cost':costs,'estimated_fee':costs*.001/cost,'estimated_spread':costs*.0001/cost,'estimated_slippage':costs*(cost-.0011)/cost,'lot_rejections':execution.rejected,'block_bootstrap_familywise_ci':ci}
                rows.append(row)
                for t in result.trades: trades_all.append({'symbol':symbol,'timeframe':tf,'strategy':kind,'split':split,'cost_per_side':cost,**t})
                (OUT/'results.json').write_text(json.dumps(rows,indent=2,allow_nan=False))
                print(symbol,tf,kind,split,cost,'trades',len(pnl),'PF',m['profit_factor'],'net',m['net_profit'],flush=True)
        pd.DataFrame(rows).drop(columns=['block_bootstrap_familywise_ci']).to_csv(OUT/'results.csv',index=False)
        pd.DataFrame(trades_all).to_csv(OUT/'trades.csv',index=False)
        time.sleep(.5)
print('COMPLETE',len(rows),flush=True)

