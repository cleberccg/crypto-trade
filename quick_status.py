#!/usr/bin/env python3
import json
import os

# Verificação rápida do estado
print('=== PIPELINE STATUS ===')
print()

# Verifica os logs
log_file = 'logs/research_pipeline_main.log'
if os.path.exists(log_file):
    lines = open(log_file).readlines()
    print(f'Log lines: {len(lines)}')
    # Encontra as linhas das etapas do pipeline
    stage_lines = [l for l in lines if 'PIPELINE_STAGE:' in l]
    if stage_lines:
        print(f'Latest stage: {stage_lines[-1].strip()}')
    
    # Exibe as últimas 5 linhas
    print('\nLast 5 log lines:')
    for line in lines[-5:]:
        if line.strip():
            print(f'  {line.strip()[:100]}')

# Verifica o estado
state_file = 'research_pipeline_state.json'
if os.path.exists(state_file):
    state = json.load(open(state_file))
    print(f'\nState: {state.get("PIPELINE_STAGE")}, Status: {state.get("STATUS")}')

# Verifica o disco
import shutil
stat = shutil.disk_usage('D:/')
print(f'Free disk: {stat.free / (1024**3):.1f} GB')

# Verifica se há processos
print('\n=== Process Check ===')
import subprocess
result = subprocess.run(['tasklist', '/FI', 'ImageName eq python.exe'], capture_output=True, text=True)
python_procs = [l for l in result.stdout.split('\n') if 'python' in l.lower()]
print(f'Python processes: {len(python_procs)}')
