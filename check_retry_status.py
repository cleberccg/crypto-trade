#!/usr/bin/env python3
import os

log_file = 'logs/collect_aggtrades_bulk_retry.log'
if os.path.exists(log_file):
    lines = open(log_file).readlines()
    print(f"Total lines in log: {len(lines)}")
    print("\n=== Last 30 lines ===")
    for line in lines[-30:]:
        print(line.rstrip())
    
    log_text = open(log_file).read()
    if 'COLLECTION_COMPLETED' in log_text:
        print("\n✓ COLLECTION COMPLETED SUCCESSFULLY")
    else:
        print("\n⏳ COLLECTION STILL RUNNING")
else:
    print(f"Log file not found: {log_file}")
