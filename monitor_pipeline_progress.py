#!/usr/bin/env python3
import os
import json
import time
import sys

def monitor_pipeline():
    """Monitor pipeline progress"""
    
    log_file = 'logs/research_pipeline_main.log'
    state_file = 'research_pipeline_state.json'
    
    while True:
        print("\n" + "="*70)
        print(f"Time: {time.strftime('%H:%M:%S')}")
        
        # Check orchestrator process
        try:
            os.system('tasklist /FI "ImageName eq python.exe" /FO CSV | findstr python > nul')
            print("✓ Orchestrator process running")
        except:
            pass
        
        # Read latest log
        if os.path.exists(log_file):
            try:
                lines = open(log_file).readlines()
                if lines:
                    last_10 = [l.strip() for l in lines[-10:] if l.strip()]
                    print(f"\nLatest {len(last_10)} lines:")
                    for line in last_10:
                        # Extract key info
                        if 'PIPELINE_STAGE:' in line:
                            print(f"  {line}")
                        elif 'COMPLETED' in line:
                            print(f"  ✓ {line}")
                        elif 'FAILED' in line:
                            print(f"  ✗ {line}")
                        elif 'STATUS:' in line and 'PROGRESS' in line:
                            # Extract progress
                            parts = line.split('TOTAL_PROGRESS=')
                            if len(parts) > 1:
                                prog = parts[1].split('%')[0] + '%'
                                print(f"  Progress: {prog}")
            except Exception as e:
                print(f"Error reading log: {e}")
        
        # Check state file
        if os.path.exists(state_file):
            try:
                state = json.load(open(state_file))
                print(f"\nPipeline State:")
                print(f"  Stage: {state.get('PIPELINE_STAGE', 'Unknown')}")
                print(f"  Status: {state.get('STATUS', 'Unknown')}")
            except:
                pass
        
        # Check discovery registry
        registry_file = 'autonomous_discovery_v2_registry.json'
        if os.path.exists(registry_file):
            try:
                registry = json.load(open(registry_file))
                total = len(registry.get('hypotheses', []))
                candidates = sum(1 for h in registry.get('hypotheses', []) if h.get('status') == 'CANDIDATE')
                print(f"\nDiscovery Status:")
                print(f"  Total hypotheses: {total}")
                print(f"  Candidates found: {candidates}")
            except:
                pass
        
        print("\nWaiting 30s... (Ctrl+C to stop)")
        time.sleep(30)

if __name__ == '__main__':
    try:
        monitor_pipeline()
    except KeyboardInterrupt:
        print("\n\nMonitor stopped by user")
        sys.exit(0)
