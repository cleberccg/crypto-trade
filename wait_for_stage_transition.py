#!/usr/bin/env python3
import time
import os

print("Waiting for collection completion (monitoring for 3 minutes)...\n")

start_time = time.time()
timeout = 180  # 3 minutes

while time.time() - start_time < timeout:
    if os.path.exists('logs/research_pipeline.log'):
        lines = open('logs/research_pipeline.log').readlines()
        
        # Find stage lines
        stage_lines = [l for l in lines if 'PIPELINE_STAGE' in l]
        if stage_lines:
            current_stage = stage_lines[-1].strip()
            
            # Check for transition beyond COLLECTION
            if 'VALIDATION' in current_stage or 'CACHE' in current_stage or 'DISCOVERY' in current_stage:
                print(f"\n✓ STAGE TRANSITION DETECTED!")
                print(f"  Current: {current_stage[:100]}")
                break
            
            # Show progress if in COLLECTION
            if 'COLLECTION' in current_stage:
                # Extract progress
                if 'REMAINING' in current_stage:
                    print(f"  {current_stage[:80]}")
    
    time.sleep(30)
    elapsed = int(time.time() - start_time)
    print(f"Elapsed: {elapsed}s, checking in 30s...")

# Final status
print("\n=== FINAL STATUS ===")
if os.path.exists('logs/research_pipeline.log'):
    lines = open('logs/research_pipeline.log').readlines()
    stage_lines = [l for l in lines if 'PIPELINE_STAGE' in l]
    if stage_lines:
        print(f"Current stage: {stage_lines[-1].strip()[:100]}")
    print(f"Total log lines: {len(lines)}")
