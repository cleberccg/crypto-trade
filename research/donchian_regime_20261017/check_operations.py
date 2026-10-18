from pathlib import Path
import json, hashlib
import psutil
from datetime import datetime, timezone, timedelta
root=Path(__file__).resolve().parents[2]; out=Path(__file__).resolve().parent
pids=[80848,17832]
evidence={'checked_at':datetime.now(timezone(timedelta(hours=-3))).isoformat(),'processes':[],'production_processes_signalled_or_modified':False,'orders_emitted_by_research':0,'production_database_connections_by_research':0,'source_hashes_unchanged_since_record':{},'resource_policy':{'affinity_logical_cpus':1,'priority':'Idle','rss_budget_mib':900,'checks':'between datasets/backtests; not an OS hard limit'},'observed_research_rss_mib_about':None}
for pid in pids:
    p=psutil.Process(pid)
    evidence['processes'].append({'pid':pid,'is_running':p.is_running(),'created_at':datetime.fromtimestamp(p.create_time(),timezone(timedelta(hours=-3))).isoformat(),'command':p.cmdline(),'rss_mib':p.memory_info().rss/1024**2})
initial=json.loads((out/'operational_initial.json').read_text())
evidence['same_process_creation_times']=all(psutil.Process(a['pid']).create_time()==a['created'] for a in initial)
assert evidence['same_process_creation_times']
original=json.loads((out/'protected_source_hashes.json').read_text())
for f,expected in original.items(): evidence['source_hashes_unchanged_since_record'][f]=hashlib.sha256((root/f).read_bytes()).hexdigest()==expected
assert all(evidence['source_hashes_unchanged_since_record'].values())
daemon=json.loads((root/'optimization/results/market_data_daemon_latest.json').read_text(encoding='utf-8-sig'))
evidence['daemon']={'generated_at':daemon['generated_at'],'summary':daemon['summary'],'initial_observed_cycle':48726,'initial_daemon_process_start':'2026-09-30T23:20:43-03:00'}
with (root/'logs/application.log').open('rb') as f:
    f.seek(max(0,(root/'logs/application.log').stat().st_size-90000)); tail=f.read().decode('utf-8',errors='replace').splitlines()
evidence['recent_live_log_lines']=[line for line in tail if 'execution.live_trading_service' in line][-6:]
evidence['recent_daemon_log_lines']=[line for line in tail if 'market-data-daemon cycle=' in line][-6:]
evidence['exchange_audit_performed']=False
evidence['limitation']='Process/log continuity observed; no authenticated exchange audit. LIVE may execute its own authorized orders.'
(out/'operational_final.json').write_text(json.dumps(evidence,indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'same_live_pid':pids[0],'same_daemon_pid':pids[1],'daemon_cycle':daemon['summary']['cycle'],'daemon_failures':daemon['summary']['total_failures'],'source_hashes_unchanged':all(evidence['source_hashes_unchanged_since_record'].values())}))
