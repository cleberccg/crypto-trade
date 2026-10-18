"""Authenticated process startup only: fixed commands, no manual orders or process termination."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic

from dotenv import dotenv_values

from audit_binance_spot_readonly import _load_project_env, _read_mysql_snapshot, _read_state, reconcile
from exchange.binance_spot_readonly_audit import BinanceSpotReadOnlyAuditClient
from webapi.live_monitor import ROOT, process_snapshot, read_log_activity

UTC = timezone.utc
KNOWN_EXTERNAL_BNB = 0.00030331  # Documented ownership, never inferred from a wallet balance.


class PlatformStartError(RuntimeError):
    pass


class WindowedAuditClient:
    """Reuse the fixed GET-only client with <=24h windows for a multi-day reboot gap."""
    def __init__(self, client): self.client = client
    def fetch_account(self): return self.client.fetch_account()
    def fetch_bnb_usdt_open_orders(self): return self.client.fetch_bnb_usdt_open_orders()
    def _history(self, method, start, end):
        if end < start or end - start > 90 * 86_400_000:
            raise PlatformStartError('Período de auditoria inválido ou superior a 90 dias; revisão manual necessária.')
        rows, seen = [], set()
        cursor = start
        key = 'id' if method == 'fetch_bnb_usdt_trades' else 'orderId'
        while cursor <= end:
            stop = min(cursor + 86_400_000 - 1, end)
            for row in getattr(self.client, method)(cursor, stop):
                identifier = row.get(key)
                if identifier is None:
                    raise PlatformStartError('Histórico Binance incompleto; identificador ausente.')
                if identifier not in seen:
                    rows.append(row)
                    seen.add(identifier)
            cursor = stop + 1
        return rows
    def fetch_bnb_usdt_orders(self, start, end): return self._history('fetch_bnb_usdt_orders', start, end)
    def fetch_bnb_usdt_trades(self, start, end): return self._history('fetch_bnb_usdt_trades', start, end)


class PlatformController:
    def __init__(self, root=ROOT):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.starting_until = {}
        self.receipt = self.root / 'optimization/results/platform_last_audit.json'

    def status(self):
        processes = process_snapshot(self.root)
        activity = read_log_activity(self.root/'logs/application.log', processes, datetime.now(UTC))
        return {'processes': processes, 'starting': {key: monotonic() < value for key,value in self.starting_until.items()},
                'daemon_activity_at': activity['daemon_at'],
                'worker_command': 'C:\\Python313\\python.exe main.py live --strategy-name Sma200RegimeGated --strategy-version v1 --symbol BNB/USDT --timeframe 4h --poll-seconds 15 --bootstrap-bars 1500 --bootstrap-replay-bars 350 --enable-real-orders'}

    def _audit(self):
        config = _load_project_env(self.root/'.env')
        public = dotenv_values(self.root/'.env', interpolate=False)
        expected = {'STRATEGY':'Sma200RegimeGated', 'SYMBOL':'BNB/USDT', 'TIMEFRAME':'4h'}
        if any(public.get(k) != v for k,v in expected.items()):
            raise PlatformStartError('Configuração difere do contexto LIVE aprovado; início bloqueado.')
        state_path = self.root/'optimization/results/live_positions.json'
        states = _read_state(state_path)
        if any(s.get('exit_pending') for s in states):
            raise PlatformStartError('Há intenção de saída pendente no estado; início bloqueado.')
        if self.receipt.exists():
            try:
                since = datetime.fromisoformat(json.loads(self.receipt.read_text(encoding='utf-8'))['audited_to_utc']) - timedelta(minutes=5)
                if since.tzinfo is None: raise ValueError()
                since=since.astimezone(UTC)
            except Exception:
                raise PlatformStartError('Registro da última auditoria inválido; nenhuma correção automática aplicada.') from None
        else:
            opened = [datetime.fromisoformat(s['opened_at']).replace(tzinfo=UTC) for s in states if s.get('symbol') == 'BNB/USDT' and s.get('strategy') == 'Sma200RegimeGated']
            if not opened:
                raise PlatformStartError('Sem referência persistida para o início da auditoria; revisão manual necessária.')
            since = min(opened) - timedelta(minutes=5)
        if not 0 <= (datetime.now(UTC) - since).total_seconds() <= 90 * 86400:
            raise PlatformStartError('Referência temporal de auditoria inválida ou anterior a 90 dias.')
        mysql = _read_mysql_snapshot(config['DATABASE_URL'], since)
        client = BinanceSpotReadOnlyAuditClient(config['BINANCE_API_KEY'], config['BINANCE_API_SECRET'])
        try:
            report = reconcile(WindowedAuditClient(client), database_url=config['DATABASE_URL'], state_path=state_path,
                               since=since, known_external_bnb=KNOWN_EXTERNAL_BNB, mysql_snapshot=mysql)
        finally:
            client.close()
        if report['decision'] != 'LIBERADO PARA REIN\u00cdCIO' or report['blockers']:
            raise PlatformStartError('Auditoria BLOQUEADA: ' + '; '.join(report['blockers']))
        self.receipt.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.receipt.with_suffix('.tmp')
        temporary.write_text(json.dumps({'audited_to_utc':report['audited_to_utc']}),encoding='utf-8')
        temporary.replace(self.receipt)
        return {'decision':'LIBERADO', 'at': report['audited_to_utc']}

    def _launch(self, component):
        # Remove .env overrides from the child only. The original bot loads its own existing .env.
        env = os.environ.copy()
        protected = {'PATH','SYSTEMROOT','COMSPEC','TEMP','TMP','USERPROFILE','HOME'}
        for key in dotenv_values(self.root/'.env', interpolate=False):
            if key.upper() not in protected:
                env.pop(key, None)
        process = subprocess.Popen(['powershell.exe','-NoProfile','-NoExit','-ExecutionPolicy','Bypass',
                                    '-File',str(self.root/'scripts/run_platform_component.ps1'),'-Component',component],
                                   cwd=self.root, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        return process.pid

    def start(self, component, confirm_real_orders=False):
        if component not in {'worker','daemon'}:
            raise PlatformStartError('Componente não permitido.')
        if component == 'worker' and confirm_real_orders is not True:
            raise PlatformStartError('Confirme explicitamente o início LIVE com ordens reais.')
        with self.lock:
            processes = process_snapshot(self.root)
            if len(processes[component]) > 1:
                raise PlatformStartError('Há processos duplicados; nenhum serviço foi iniciado.')
            if processes[component]:
                return {'status':'already_running','component':component,'pid':processes[component][0]['pid']}
            if monotonic() < self.starting_until.get(component,0):
                return {'status':'starting','component':component}
            audit = None
            if component == 'worker':
                if len(processes['daemon']) != 1:
                    raise PlatformStartError('Inicie primeiro o market-data daemon e aguarde sua atividade.')
                activity=read_log_activity(self.root/'logs/application.log',processes,datetime.now(UTC))
                at=activity['daemon_at']
                if not at or (datetime.now(UTC)-datetime.fromisoformat(at)).total_seconds()>90:
                    raise PlatformStartError('Daemon sem atividade recente; aguarde sincronização antes do LIVE.')
                try:
                    audit=self._audit()
                except PlatformStartError:
                    raise
                except Exception:
                    raise PlatformStartError('Auditoria indisponível; início LIVE bloqueado. Nenhum detalhe sensível foi exibido.') from None
                if process_snapshot(self.root)['worker']:
                    raise PlatformStartError('Um worker foi iniciado durante a auditoria; não foi criado outro.')
            shell_pid=self._launch(component)
            self.starting_until[component]=monotonic()+60
            return {'status':'starting','component':component,'shell_pid':shell_pid,'audit':audit}


controller=PlatformController()
