#!/usr/bin/env python3
import os
import subprocess
import json
import shutil
from pathlib import Path

print("=== PIPELINE DIAGNOSTIC ===\n")

# Check disk
stat = shutil.disk_usage('D:/')
free_gb = stat.free / (1024**3)
print(f"Disk space: {free_gb:.1f} GB free")

if free_gb < 5:
    print("⚠️  CRITICAL: Low disk space (<5GB)")

# Check if orchestrator process running
try:
    result = subprocess.run(['tasklist', '/FO', 'CSV'], capture_output=True, text=True, timeout=5)
    lines = result.stdout.split('\n')
    python_processes = [l for l in lines if 'python.exe' in l.lower()]
    print(f"Python processes: {len(python_processes)}")
    
    # PID 40536 was the orchestrator
    if any('40536' in l for l in python_processes):
        print("  ✓ Orchestrator (PID 40536) still running")
    else:
        print("  ✗ Orchestrator (PID 40536) NOT FOUND")
except Exception as e:
    print(f"Error checking processes: {e}")

# Check log file
print("\nLog file status:")
log_file = 'logs/research_pipeline.log'
if os.path.exists(log_file):
    size_kb = os.path.getsize(log_file) / 1024
    lines = len(open(log_file).readlines())
    print(f"  Size: {size_kb:.1f} KB")
    print(f"  Lines: {lines}")
    
    # Get last few lines
    last_lines = open(log_file).readlines()[-5:]
    print(f"  Last line: {last_lines[-1].strip()[:80] if last_lines else 'N/A'}")

# Check if any cache/validation started
cache_dir = Path('data/cache_minute_bars_v2')
print(f"\nCache directory exists: {cache_dir.exists()}")

discovery_file = Path('autonomous_discovery_v2_registry.json')
print(f"Discovery registry exists: {discovery_file.exists()}")

state_file = Path('research_pipeline_state.json')
print(f"State file exists: {state_file.exists()}")

# Check manifest status
manifest_file = Path('data/aggtrades/manifest.json')
if manifest_file.exists():
    manifest = json.load(open(manifest_file))
    validated_count = sum(1 for p in manifest['partitions'].values() if p.get('VALIDATED') == 'YES')
    total_count = len(manifest['partitions'])
    print(f"\nManifest: {validated_count}/{total_count} partitions validated")

print("\n=== RECOMMENDATION ===")
if free_gb < 5:
    print("Disk space critically low - stop pipeline and free space")
else:
    print("Orchestrator may be stuck on partition processing.")
    print("Consider: kill process, check error logs, restart")
