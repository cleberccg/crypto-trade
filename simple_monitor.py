#!/usr/bin/env python3
import os
import time
import json

def check_status():
    log_file = 'logs/research_pipeline.log'
    
    if not os.path.exists(log_file) or os.path.getsize(log_file) == 0:
        return "LOG_NOT_STARTED"
    
    lines = open(log_file).readlines()
    
    # Find latest stage
    stage_lines = [l for l in lines if 'PIPELINE_STAGE:' in l]
    if not stage_lines:
        return "NO_STAGE"
    
    latest_stage = stage_lines[-1].strip()
    
    # Parse stage
    if 'VALIDATION' in latest_stage:
        if 'FAILED' in latest_stage:
            return "VALIDATION_FAILED"
        else:
            return "VALIDATING"
    elif 'CACHE' in latest_stage:
        return "BUILDING_CACHE"
    elif 'DISCOVERY' in latest_stage:
        return "RUNNING_DISCOVERY"
    elif 'COLLECTION' in latest_stage:
        if 'COMPLETED' in latest_stage:
            return "COLLECTION_DONE"
        else:
            return "COLLECTING"
    
    return "UNKNOWN"

# Run forever until major transition
start_time = time.time()
last_status = None

while time.time() - start_time < 600:  # 10 minute timeout
    status = check_status()
    
    if status != last_status:
        print(f"[{time.strftime('%H:%M:%S')}] STATUS: {status}")
        last_status = status
    
    if status in ['BUILDING_CACHE', 'RUNNING_DISCOVERY', 'VALIDATION_FAILED']:
        print(f"✓ Major transition detected: {status}")
        break
    
    time.sleep(5)
