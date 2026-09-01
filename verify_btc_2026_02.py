#!/usr/bin/env python3
import json
import sys

try:
    manifest = json.load(open('data/aggtrades/manifest.json'))
    entry = manifest['partitions'].get('BTCUSDT|2026-02', {})
    
    if entry:
        validated = entry.get('VALIDATED', 'UNKNOWN')
        records = entry.get('RECORDS', 0)
        issues = entry.get('ISSUES', [])
        
        print(f"BTCUSDT|2026-02:")
        print(f"  VALIDATED: {validated}")
        print(f"  RECORDS: {records:,}")
        
        if validated == 'YES':
            print("  ✓ VALIDATION SUCCESS - Ready for pipeline")
        else:
            print(f"  ✗ VALIDATION FAILED - Issues: {issues}")
    else:
        print("ERROR: BTCUSDT|2026-02 not found in manifest")
        print("Available keys sample:", list(manifest['partitions'].keys())[:5])
        
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    import traceback
    traceback.print_exc()
