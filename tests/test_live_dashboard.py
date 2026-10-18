from __future__ import annotations

import json
import socket
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

import webapi.live_monitor as monitor
from webapi.app import create_app
from webapi.security import create_access_token


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail('Real network forbidden in dashboard tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)
    monkeypatch.setattr(socket, 'create_connection', blocked)


@pytest.fixture
def fixture_reader(tmp_path, monkeypatch):
    (tmp_path / '.env').write_text('MAX_OPEN_POSITIONS=3\n', encoding='utf-8')
    (tmp_path / 'optimization/results').mkdir(parents=True)
    states = [{'trade_id': 3081, 'status': 'DUST', 'quantity': .00098726,
               'symbol': 'BNB/USDT', 'timeframe': '4h', 'strategy': 'Sma200RegimeGated', 'exit_pending': False}]
    (tmp_path / 'optimization/results/live_positions.json').write_text(json.dumps(states))
    reader = monitor.LiveDashboardReader(tmp_path)
    positions = [dict(id=3081, status='DUST', quantity=.00098726, entry_price=787.152,
                      symbol='BNB/USDT', timeframe='4h', strategy_name='Sma200RegimeGated', pnl=-.457207),
                 dict(id=3080, status='OPEN', quantity=.0038, entry_price=1868.32,
                      symbol='ETH/USDT', timeframe='15m', strategy_name='ClassicDonchianBreakout', pnl=None)]
    db = dict(at='2026-10-17T23:00:00+00:00', positions=positions, history=positions[:1],
              orders=[dict(id=170, trade_id=3081, fee_usdt=.00437953)], realized_pnl=-.457207, closed_candle=None)
    monkeypatch.setattr(reader, '_read_db', lambda *a, **k: db)
    monkeypatch.setattr(reader, '_read_account', lambda: ({'USDT': {'free': '28.25577283', 'locked': '0'},
                                                        'BNB': {'free': '.00129057', 'locked': '0'}}, '2026-10-17T23:00:00+00:00'))
    monkeypatch.setattr(monitor, 'process_snapshot', lambda root: {'worker': [], 'daemon': []})
    return reader, db


def test_realized_dust_and_legacy_are_not_active_capital(fixture_reader):
    reader, db = fixture_reader
    result = reader.snapshot()
    assert result['mode'] == 'LIVE' and result['read_only']
    assert result['open_positions'] == []
    assert [r['id'] for r in result['dust_positions']] == [3081]
    assert [r['id'] for r in result['preserved_positions']] == [3080]
    assert result['capital']['committed_open'] == 0
    assert result['capital']['tracked_cost_capital'] == 28.25577283
    assert result['capital']['dust_cost'] == pytest.approx(.777124)
    assert result['capital']['realized_pnl'] == -.457207
    assert result['capital']['unrealized_pnl'] is None
    assert 'DATABASE_URL' not in json.dumps(result)


def test_database_unavailable_is_not_zero(fixture_reader, monkeypatch):
    reader, _ = fixture_reader
    monkeypatch.setattr(reader, '_read_db', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('secret-dsn')))
    result = reader.snapshot()
    assert result['open_positions'] is None
    assert result['capital']['committed_open'] is None
    assert result['capital']['tracked_cost_capital'] is None
    assert 'secret-dsn' not in json.dumps(result)


def test_exchange_unavailable_is_not_historical_cash(fixture_reader, monkeypatch):
    reader, _ = fixture_reader
    monkeypatch.setattr(reader, '_read_account', lambda: (None, None))
    result = reader.snapshot()
    assert result['capital']['usdt_free'] is None
    assert result['capital']['tracked_cost_capital'] is None
    assert result['sources']['binance']['status'] == 'unavailable'


def test_auth_required_and_no_operational_endpoints(monkeypatch):
    app = create_app(read_only=True)
    monkeypatch.setattr('webapi.live_routes.reader.snapshot', lambda: {'mode':'LIVE','read_only':True})
    with TestClient(app) as client:
        assert client.get('/api/v1/dashboard').status_code == 401
        headers = {'Authorization': 'Bearer ' + create_access_token('admin','administrator')}
        assert client.get('/api/v1/dashboard', headers=headers).json()['mode'] == 'LIVE'
        assert 'database_url' not in client.get('/health').text
        for path in ['/api/v1/orders', '/api/v1/config', '/api/v1/executions/start', '/api/v1/emergency-close']:
            assert client.post(path, json={}, headers=headers).status_code in (404,405)
        assert client.get('/api/v1/jobs', headers=headers).status_code == 404
        assert client.get('/health', headers={'host':'evil.example'}).status_code == 400
    methods = [(r.path, r.methods) for r in app.routes if hasattr(r,'methods')]
    assert all(not ({'POST','PUT','DELETE','PATCH'} & m) or path in {'/api/v1/auth/login','/api/v1/platform/start'} for path,m in methods)


def test_log_timestamps_are_brasilia_and_raw_errors_never_exposed(tmp_path):
    file = tmp_path / 'application.log'
    file.write_text('2026-10-17 20:45:35 | INFO     | execution.live_trading_service | MainProcess | [LIVE] BNB/USDT -> HOLD_NO_ACTIVE_POSITION\n'
                    '2026-10-17 20:45:36 | ERROR    | execution.live_trading_service | MainProcess | secret signed-url\n', encoding='utf-8')
    result = monitor.read_log_activity(file, {'worker':[{'started_at':'2026-10-17T23:45:00+00:00'}], 'daemon':[]}, datetime(2026,10,17,23,46,tzinfo=timezone.utc))
    assert result['worker_at'] == '2026-10-17T23:45:36+00:00'
    assert result['decision'] == 'HOLD_NO_ACTIVE_POSITION'
    assert 'secret' not in json.dumps(result)


def test_sql_queries_filter_live_and_history_period(monkeypatch):
    engine = create_engine('sqlite://')
    with engine.begin() as c:
        c.connection.driver_connection.create_function('UTC_TIMESTAMP',0,lambda:'2026-10-17 23:00:00')
        c.execute(text('CREATE TABLE trades (id INTEGER,symbol TEXT,strategy_name TEXT,timeframe TEXT,status TEXT,is_paper INTEGER,quantity FLOAT,entry_price FLOAT,stake_amount FLOAT,pnl FLOAT,total_fees_usdt FLOAT,fee_accounting_complete INTEGER,entry_time TEXT,exit_time TEXT,exit_price FLOAT,exit_reason TEXT)'))
        c.execute(text("INSERT INTO trades VALUES (3081,'BNB/USDT','Sma200RegimeGated','4h','DUST',0,.00098726,787.152,7.08,-.457207,.00969281,1,'2026-10-04',NULL,NULL,NULL),(9999,'BNB/USDT','Sma200RegimeGated','4h','CLOSED',1,1,100,100,999,1,1,'2026-10-04','2026-10-17',100,NULL)"))
        c.execute(text('CREATE TABLE orders (id INTEGER,trade_id INTEGER,exchange_order_id TEXT,symbol TEXT,side TEXT,status TEXT,quantity FLOAT,filled_quantity FLOAT,price FLOAT,fee FLOAT,fee_currency TEXT,fee_usdt FLOAT,fee_source TEXT,fee_conversion_price FLOAT,timestamp TEXT)'))
        c.execute(text('CREATE TABLE candles (symbol TEXT,timeframe TEXT,open_time TEXT)'))
    calls=[]
    class Connection:
        def __enter__(self):
            self.conn=engine.connect();return self
        def __exit__(self,*args):self.conn.close()
        def exec_driver_sql(self,sql):
            assert sql == 'SET SESSION TRANSACTION READ ONLY';calls.append(sql)
        def commit(self):self.conn.commit()
        def begin(self):return self.conn.begin()
        def execute(self,sql,*args):
            assert str(sql).startswith('SELECT ')
            calls.append(str(sql))
            return self.conn.execute(sql,*args)
    class Database:
        def connect(self):return Connection()
    reader=monitor.LiveDashboardReader()
    monkeypatch.setattr(reader,'_database',lambda:Database())
    data=reader._read_db('Sma200RegimeGated','BNB/USDT','4h')
    assert data['realized_pnl'] == -.457207
    assert [r['id'] for r in data['history']] == [3081]
    assert calls[0] == 'SET SESSION TRANSACTION READ ONLY'
    assert all('is_paper=0' in s for s in calls if 'FROM trades' in s)
    engine.dispose()


def test_process_scan_excludes_shell_parent(tmp_path, monkeypatch):
    class Process:
        def __init__(self, pid, name):
            self.info={'pid':pid,'name':name,'cmdline':['python.exe','main.py','market-data-daemon'],'create_time':1}
        def cwd(self):return str(tmp_path)
    monkeypatch.setattr(monitor.psutil,'process_iter',lambda fields:[Process(2264,'powershell.exe'),Process(17832,'python.exe')])
    result=monitor.process_snapshot(tmp_path)
    assert [p['pid'] for p in result['daemon']] == [17832]


def test_worker_polling_is_fresh_activity_without_frequent_summary(tmp_path):
    file=tmp_path/'app.log'
    file.write_text('2026-10-17 20:48:00 | INFO | exchange.binance_client | MainProcess | fetch_ohlcv - symbol=BNB/USDT\n')
    result=monitor.read_log_activity(file,{'worker':[{'started_at':'2026-10-17T23:45:00+00:00'}],'daemon':[]},datetime(2026,10,17,23,48,tzinfo=timezone.utc))
    assert result['worker_at'] == '2026-10-17T23:48:00+00:00'
    assert result['state_at'] is None


def test_binance_cache_uses_isolated_reads_and_discards_failed_balance(monkeypatch):
    reader=monitor.LiveDashboardReader()
    now=[0.0]
    calls=[]
    monkeypatch.setattr(monitor,'monotonic',lambda:now[0])
    monkeypatch.setattr(monitor,'_load_project_env',lambda path:{'BINANCE_API_KEY':'fixture-key','BINANCE_API_SECRET':'fixture-secret'})
    class Client:
        def __init__(self,*args,**kwargs):pass
        def fetch_account(self):
            calls.append('account')
            if now[0] > 30:raise RuntimeError('secret signed URL')
            return {'balances':[{'asset':'USDT','free':'28.25','locked':'0'},{'asset':'BNB','free':'.001','locked':'0'}]}
        def fetch_bnb_usdt_open_orders(self):calls.append('openOrders');return []
        def close(self):calls.append('close')
    monkeypatch.setattr(monitor,'BinanceSpotReadOnlyAuditClient',Client)
    first=reader._read_account()
    assert first[0]['USDT']['free']=='28.25'
    assert reader._read_account()==first
    assert calls==['account','openOrders','close']
    now[0]=31
    assert reader._read_account()==(None,None)
    assert reader._open_orders is None


def test_brl_quote_public_cache_and_no_stale_rate(monkeypatch):
    import webapi.brl_quote as fx
    clock=[0.0]
    calls=[]
    monkeypatch.setattr(fx,'_cached',None)
    monkeypatch.setattr(fx,'_until',0)
    monkeypatch.setattr(fx,'monotonic',lambda:clock[0])
    class Response:
        status_code=200
        url='https://api.binance.com/api/v3/ticker/price?symbol=USDTBRL'
        def json(self):return {'symbol':'USDTBRL','price':'5.0793'}
    class Session:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,url,**kwargs):
            assert url=='https://api.binance.com/api/v3/ticker/price'
            assert kwargs['params']=={'symbol':'USDTBRL'}
            assert kwargs['allow_redirects'] is False
            assert 'headers' not in kwargs
            assert self.trust_env is False
            calls.append(url)
            if clock[0]>60:raise RuntimeError('private details must not leak')
            return Response()
    monkeypatch.setattr(fx.requests,'Session',Session)
    assert fx.get_brl_quote()['rate']==5.0793
    assert fx.get_brl_quote()['rate']==5.0793
    assert len(calls)==1
    clock[0]=61
    failed=fx.get_brl_quote()
    assert failed['rate'] is None and failed['at'] is None
    assert 'private' not in json.dumps(failed)


@pytest.mark.parametrize('price',['0','-1','NaN','Infinity','bad'])
def test_brl_quote_rejects_invalid_prices(monkeypatch,price):
    import webapi.brl_quote as fx
    monkeypatch.setattr(fx,'_cached',None)
    class Response:
        status_code=200
        url='https://api.binance.com/api/v3/ticker/price'
        def json(self):return {'symbol':'USDTBRL','price':price}
    class Session:
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**k):return Response()
    monkeypatch.setattr(fx.requests,'Session',Session)
    assert fx.get_brl_quote()['rate'] is None


def test_brl_quote_requires_authentication():
    app=create_app(read_only=True)
    with TestClient(app) as client:
        assert client.get('/api/v1/live/brl-quote').status_code==401


@pytest.mark.parametrize('side,quantity,price,fee,expected',[
    ('BUY', .007, 787.15, .00413254, 5.51418254),
    ('SELL', .008, 731.14, .00437953, 5.84474047),
])
def test_transaction_costs_use_only_fills_and_preserve_input(side,quantity,price,fee,expected):
    from webapi.transaction_costs import transaction_costs
    original={'symbol':'BNB/USDT','side':side,'filled_quantity':quantity,'quantity':99,
              'price':price,'fee_usdt':fee,'fee_currency':'BNB','fee':.00000599}
    before=dict(original)
    result=transaction_costs(original)
    assert original==before
    assert result['executed_value_usdt']==pytest.approx(quantity*price)
    key='buy_cost_equivalent_usdt' if side=='BUY' else 'sell_proceeds_equivalent_usdt'
    assert result[key]==pytest.approx(expected)
    assert result['conversion_charge_usdt'] is None


def test_transaction_costs_do_not_invent_missing_fees_or_requested_fill():
    from webapi.transaction_costs import transaction_costs
    result=transaction_costs({'symbol':'BNB/USDT','side':'BUY','filled_quantity':.01,
                              'price':700,'fee_usdt':None})
    assert result['executed_value_usdt']==7
    assert result['buy_cost_equivalent_usdt'] is None
    result=transaction_costs({'symbol':'BNB/USDT','side':'BUY','filled_quantity':0,
                              'quantity':10,'price':700,'fee_usdt':0})
    assert result['executed_value_usdt']==0
    assert result['buy_cost_equivalent_usdt']==0


def test_transaction_costs_do_not_treat_other_quote_as_usdt():
    from webapi.transaction_costs import transaction_costs
    result=transaction_costs({'symbol':'BNB/BRL','side':'BUY','filled_quantity':1,
                              'price':700,'fee_usdt':1})
    assert result['executed_value_usdt'] is None
    assert result['buy_cost_equivalent_usdt'] is None
