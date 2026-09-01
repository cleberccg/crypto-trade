#!/usr/bin/env python3
import time
import os

print("Pipeline Status Monitor")
print("=" * 60)

# Wait for log to be written
wait_count = 0
while not os.path.exists('logs/research_pipeline.log') or os.path.getsize('logs/research_pipeline.log') == 0:
    if wait_count > 30:
        print("Timeout waiting for log to start")
        exit(1)
    print("Waiting for log... ({}/30)".format(wait_count))
    time.sleep(1)
    wait_count += 1

# Monitor for transitions
stage_line_count = 0
last_check = 0

while True:
    if os.path.exists('logs/research_pipeline.log'):
        lines = open('logs/research_pipeline.log').readlines()
        
        # Count stage lines
        new_stage_count = sum(1 for l in lines if 'PIPELINE_STAGE:' in l)
        
        if new_stage_count > stage_line_count:
            stage_line_count = new_stage_count
            # Show all stage lines
            stage_lines = [l.strip() for l in lines if 'PIPELINE_STAGE:' in l]
            for sl in stage_lines[-3:]:
                print(f"✓ {sl[:100]}")
        
        # Check for significant updates every 15 lines
        if len(lines) > last_check + 15:
            last_check = len(lines)
            last_line = lines[-1].strip() if lines else ''
            if last_line and 'STATUS:' in last_line:
                # Extract key info
                if 'TOTAL_PROGRESS' in last_line:
                    parts = last_line.split('TOTAL_PROGRESS=')
                    if len(parts) > 1:
                        prog = parts[1].split('%')[0] + '%'
                        print(f"  Progress: {prog} ({len(lines)} lines)")
            elif last_line:
                print(f"  {last_line[:100]}")
        
        # Check if cache or discovery started
        if any('CACHE' in l or 'DISCOVERY' in l for l in lines):
            print("\n✓✓✓ MAJOR MILESTONE: Cache or Discovery phase started!")
            break
        
        # Break on error
        if any('FAILED' in l for l in lines):
            print("\n✗ VALIDATION/STAGE FAILURE DETECTED")
            failed_lines = [l.strip() for l in lines if 'FAILED' in l.upper()]
            for fl in failed_lines[-3:]:
                print(f"  {fl[:100]}")
            break
    
    time.sleep(5)
