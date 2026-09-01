#!/usr/bin/env python3
import json
import os
import subprocess
import time

def get_latest_log_lines(filename, n=20):
    if os.path.exists(filename):
        try:
            lines = open(filename).readlines()
            return [l.strip() for l in lines[-n:] if l.strip()]
        except:
            return []
    return []

print("=" * 80)
print(f"PIPELINE STATUS - {time.strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 80)

# Main orchestrator log
print("\n[ORCHESTRATOR LOG]")
lines = get_latest_log_lines('logs/research_pipeline_main.log', 10)
for line in lines[-5:]:
    print(f"  {line[:100]}")

# Check state file
print("\n[PIPELINE STATE]")
state_file = 'research_pipeline_state.json'
if os.path.exists(state_file):
    try:
        state = json.load(open(state_file))
        for key, val in state.items():
            print(f"  {key}: {val}")
    except Exception as e:
        print(f"  Error reading state: {e}")
else:
    print(f"  {state_file} not found yet")

# Check feature cache
print("\n[FEATURE CACHE]")
cache_dir = 'data/cache_minute_bars_v2'
if os.path.exists(cache_dir):
    subdirs = os.listdir(cache_dir)
    print(f"  Cache directories: {len(subdirs)}")
    for d in subdirs[:3]:
        path = os.path.join(cache_dir, d)
        files = os.listdir(path) if os.path.isdir(path) else []
        print(f"    - {d}: {len(files)} files")
else:
    print(f"  Cache directory not yet created")

# Check discovery registry
print("\n[DISCOVERY REGISTRY]")
registry_file = 'autonomous_discovery_v2_registry.json'
if os.path.exists(registry_file):
    try:
        registry = json.load(open(registry_file))
        status = registry.get('STATUS', 'UNKNOWN')
        hypotheses = registry.get('hypotheses', [])
        print(f"  Overall Status: {status}")
        print(f"  Total Hypotheses: {len(hypotheses)}")
        
        # Count by status
        status_counts = {}
        for h in hypotheses:
            s = h.get('status', 'UNKNOWN')
            status_counts[s] = status_counts.get(s, 0) + 1
        
        for s, count in sorted(status_counts.items()):
            print(f"    - {s}: {count}")
        
        # Show candidates
        candidates = [h for h in hypotheses if h.get('status') == 'CANDIDATE']
        if candidates:
            print(f"\n  CANDIDATES FOUND: {len(candidates)}")
            for c in candidates:
                print(f"    - {c.get('hypothesis_name', 'unknown')}: {c.get('family', 'unknown')}")
    except Exception as e:
        print(f"  Error reading registry: {e}")
else:
    print(f"  {registry_file} not yet created")

# Check disk space
print("\n[DISK SPACE]")
import shutil
stat = shutil.disk_usage('D:/')
free_gb = stat.free / (1024**3)
total_gb = stat.total / (1024**3)
used_gb = stat.used / (1024**3)
print(f"  Free: {free_gb:.1f} GB")
print(f"  Used: {used_gb:.1f} GB")
print(f"  Total: {total_gb:.1f} GB")

# Check processes
print("\n[PROCESSES]")
result = subprocess.run(['tasklist', '/FI', 'ImageName eq python.exe'], 
                       capture_output=True, text=True)
lines = result.stdout.split('\n')
proc_lines = [l for l in lines if 'python' in l.lower() and 'PID' not in l]
print(f"  Python processes: {len(proc_lines)}")

# Check locks
print("\n[LOCKS]")
locks = ['data/aggtrades_bulk.lock', 'research_pipeline.lock']
for lock in locks:
    if os.path.exists(lock):
        size = os.path.getsize(lock)
        print(f"  ✓ {lock} ({size} bytes)")
    else:
        print(f"  ✗ {lock} (not active)")

print("\n" + "=" * 80)
