from __future__ import annotations
import json
from datetime import datetime,timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
import webapi.platform_control as platform
from webapi.app import create_app
from webapi.security import create_access_token


@pytest.fixture
def controller(tmp_path,monkeypatch):
    ctrl=platform.PlatformController(tmp_path)
    states={'worker':[],'daemon':[{'pid':17832,'started_at':datetime.now(timezone.utc).isoformat()}]}
    monkeypatch.setattr(platform,'process_snapshot',lambda root:states)
    monkeypatch.setattr(platform,'read_log_activity',lambda *a:{'daemon_at':datetime.now(timezone.utc).isoformat()})
    return ctrl,states


def test_existing_worker_is_never_duplicated(controller,monkeypatch):
    ctrl,states=controller
    states['worker']=[{'pid':80848}]
    monkeypatch.setattr(ctrl,'_launch',lambda *a:pytest.fail('duplicate launch'))
    monkeypatch.setattr(ctrl,'_audit',lambda:pytest.fail('unnecessary audit'))
    assert ctrl.start('worker',True)['status']=='already_running'


def test_live_requires_confirmation_and_daemon(controller,monkeypatch):
    ctrl,states=controller
    monkeypatch.setattr(ctrl,'_launch',lambda *a:pytest.fail('unsafe launch'))
    with pytest.raises(platform.PlatformStartError,match='Confirme'):
        ctrl.start('worker',False)
    states['daemon']=[]
    with pytest.raises(platform.PlatformStartError,match='daemon'):
        ctrl.start('worker',True)


def test_audit_failure_never_launches_live(controller,monkeypatch):
    ctrl,_=controller
    def fail():raise platform.PlatformStartError('Auditoria BLOQUEADA: fixture divergence')
    monkeypatch.setattr(ctrl,'_audit',fail)
    monkeypatch.setattr(ctrl,'_launch',lambda *a:pytest.fail('must not launch after blocked audit'))
    with pytest.raises(platform.PlatformStartError,match='BLOQUEADA'):
        ctrl.start('worker',True)


def test_success_audits_before_launch_and_blocks_repeated_click(controller,monkeypatch):
    ctrl,_=controller
    calls=[]
    def audit():calls.append('audit');return {'decision':'LIBERADO'}
    def launch(component):calls.append(component);return 999
    monkeypatch.setattr(ctrl,'_audit',audit)
    monkeypatch.setattr(ctrl,'_launch',launch)
    assert ctrl.start('worker',True)['shell_pid']==999
    assert calls==['audit','worker']
    assert ctrl.start('worker',True)['status']=='starting'
    assert calls==['audit','worker']


def test_daemon_can_start_without_starting_trades(controller,monkeypatch):
    ctrl,states=controller
    states['daemon']=[]
    calls=[]
    monkeypatch.setattr(ctrl,'_audit',lambda:pytest.fail('daemon must not access private audit'))
    monkeypatch.setattr(ctrl,'_launch',lambda part:calls.append(part) or 123)
    assert ctrl.start('daemon')['status']=='starting'
    assert calls==['daemon']


def test_duplicate_or_stale_daemon_blocks_live(controller,monkeypatch):
    ctrl,states=controller
    states['daemon']=[{'pid':1},{'pid':2}]
    with pytest.raises(platform.PlatformStartError):ctrl.start('worker',True)
    states['daemon']=[{'pid':1}]
    monkeypatch.setattr(platform,'read_log_activity',lambda *a:{'daemon_at':None})
    with pytest.raises(platform.PlatformStartError,match='atividade'):ctrl.start('worker',True)


def test_start_endpoint_is_protected_by_role_origin_and_component(monkeypatch):
    app=create_app(read_only=True)
    calls=[]
    monkeypatch.setattr(platform.controller,'start',lambda *args:calls.append(args) or {'status':'starting'})
    with TestClient(app) as client:
        viewer={'Authorization':'Bearer '+create_access_token('viewer','read-only')}
        operator={'Authorization':'Bearer '+create_access_token('admin','administrator')}
        body={'component':'worker','confirm_real_orders':True}
        assert client.post('/api/v1/platform/start',json=body).status_code==401
        assert client.post('/api/v1/platform/start',json=body,headers=viewer).status_code==401
        assert client.post('/api/v1/platform/start',json=body,headers={**operator,'Origin':'https://evil.example'}).status_code==403
        assert client.post('/api/v1/platform/start',json={'component':'shell'},headers=operator).status_code==422
        assert calls==[]
        assert client.post('/api/v1/platform/start',json=body,headers=operator).status_code==200
        assert calls==[('worker',True)]


def test_audit_history_is_windowed_and_deduplicated():
    calls=[]
    class Client:
        def fetch_bnb_usdt_orders(self,start,end):
            calls.append((start,end));return [{'orderId':1}]
    result=platform.WindowedAuditClient(Client()).fetch_bnb_usdt_orders(0,2*86400000+100)
    assert len(calls)==3
    assert all(end-start<86400000 for start,end in calls)
    assert calls[1][0]==calls[0][1]+1
    assert result==[{'orderId':1}]


def test_audit_history_fails_closed_for_oversized_or_incomplete_range():
    with pytest.raises(platform.PlatformStartError):
        platform.WindowedAuditClient(object()).fetch_bnb_usdt_orders(0,91*86400000)
    class Client:
        def fetch_bnb_usdt_trades(self,*a):return [{}]
    with pytest.raises(platform.PlatformStartError):
        platform.WindowedAuditClient(Client()).fetch_bnb_usdt_trades(0,100)


def test_launcher_contains_original_commands_and_no_automatic_live_in_bat():
    root=Path(platform.__file__).resolve().parents[1]
    runner=(root/'scripts/run_platform_component.ps1').read_text(encoding='utf-8-sig')
    assert '--strategy-name Sma200RegimeGated --strategy-version v1' in runner
    assert '--enable-real-orders' in runner
    assert '--recent-gap-bars 2000' in runner
    assert 'WaitOne(0)' in runner and 'ReleaseMutex' in runner
    opener=(root/'scripts/open_live_dashboard.ps1').read_text(encoding='utf-8-sig')
    assert 'start_live_dashboard.ps1' in opener
    assert 'Start-Process' in opener and 'http://127.0.0.1:5173/' in opener
    assert 'run_platform_component' not in opener
    assert 'open_live_dashboard.ps1' in (root/'iniciar_dashboard.bat').read_text()


def test_worker_audit_validates_pending_intents_and_saves_only_metadata(tmp_path,monkeypatch):
    ctrl=platform.PlatformController(tmp_path)
    ctrl.receipt.parent.mkdir(parents=True)
    state=ctrl.receipt.parent/'live_positions.json'
    rows=[{'trade_id':3081,'symbol':'BNB/USDT','strategy':'Sma200RegimeGated','opened_at':(datetime.now(timezone.utc)-__import__('datetime').timedelta(days=2)).isoformat(),'exit_pending':False}]
    state.write_text(json.dumps(rows))
    before=state.read_bytes()
    monkeypatch.setattr(platform,'_load_project_env',lambda p:{'DATABASE_URL':'fixture','BINANCE_API_KEY':'key','BINANCE_API_SECRET':'secret'})
    monkeypatch.setattr(platform,'dotenv_values',lambda *a,**k:{'STRATEGY':'Sma200RegimeGated','SYMBOL':'BNB/USDT','TIMEFRAME':'4h'})
    monkeypatch.setattr(platform,'_read_mysql_snapshot',lambda *a:{})
    class Client:
        def __init__(self,*a):pass
        def close(self):pass
    monkeypatch.setattr(platform,'BinanceSpotReadOnlyAuditClient',Client)
    def reconcile(*args,**kwargs):
        assert kwargs['known_external_bnb']==.00030331
        return {'decision':'LIBERADO PARA REIN\u00cdCIO','blockers':[],'audited_to_utc':datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(platform,'reconcile',reconcile)
    assert ctrl._audit()['decision']=='LIBERADO'
    assert state.read_bytes()==before
    assert set(json.loads(ctrl.receipt.read_text()))=={'audited_to_utc'}
    rows[0]['exit_pending']=True
    state.write_text(json.dumps(rows))
    with pytest.raises(platform.PlatformStartError,match='pendente'):ctrl._audit()


def test_child_environment_does_not_inherit_project_overrides(tmp_path,monkeypatch):
    ctrl=platform.PlatformController(tmp_path)
    monkeypatch.setenv('MODE','paper')
    monkeypatch.setenv('BINANCE_TESTNET','true')
    monkeypatch.setenv('DATABASE_URL','sqlite://')
    monkeypatch.setattr(platform,'dotenv_values',lambda *a,**k:{'MODE':'live','BINANCE_TESTNET':'false','DATABASE_URL':'mysql://fixture'})
    calls=[]
    def popen(args,**kwargs):
        calls.append((args,kwargs));return type('Process',(),{'pid':123})()
    monkeypatch.setattr(platform.subprocess,'Popen',popen)
    assert ctrl._launch('daemon')==123
    args,kwargs=calls[0]
    assert args[-1]=='daemon'
    assert all(key not in kwargs['env'] for key in ['MODE','BINANCE_TESTNET','DATABASE_URL'])
    assert kwargs['creationflags']==platform.subprocess.CREATE_NEW_CONSOLE
