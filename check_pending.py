#!/usr/bin/env python
import json
from pathlib import Path

state_file = Path('optimization/results/phase13_factory_state.json')
if state_file.exists():
    state = json.loads(state_file.read_text())
    backlog = state.get('backlog', [])
    
    pending = [x for x in backlog if x.get('state') in ['IMPLEMENTATION_PENDING', 'IMPLEMENTATION_INCOMPLETE']]
    incomplete = [x for x in backlog if x.get('state') == 'IMPLEMENTATION_INCOMPLETE']
    
    print(f'Total backlog: {len(backlog)}')
    print(f'IMPLEMENTATION_PENDING: {len([x for x in backlog if x.get("state") == "IMPLEMENTATION_PENDING"])}')
    print(f'IMPLEMENTATION_INCOMPLETE: {len(incomplete)}')
    print(f'Total pending + incomplete: {len(pending)}')
    
    if pending:
        print(f'\nFirst 10 pending strategies:')
        for item in pending[:10]:
            print(f'  - {item.get("candidate_name")} ({item.get("state")})')
else:
    print('State file not found')
