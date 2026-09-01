#!/usr/bin/env python3
import os
import json

# Check for BTCUSDT|2026-02 parquet file
path = r'data\aggtrades\BTCUSDT\2026\02'
if os.path.exists(path):
    files = os.listdir(path)
    print(f"Files in {path}: {files}")
    for f in files:
        fpath = os.path.join(path, f)
        size_mb = os.path.getsize(fpath) / (1024**2)
        print(f"  {f}: {size_mb:.2f} MB")
else:
    print(f"Directory does not exist: {path}")

# Check manifest for BTCUSDT|2026-02
print("\n=== Checking manifest ===")
manifest = json.load(open('data/aggtrades/manifest.json'))
keys_with_2026_02 = [k for k in manifest['partitions'].keys() if '2026-02' in k]
print(f"Keys with '2026-02': {keys_with_2026_02}")

if 'BTCUSDT|2026-02' in manifest['partitions']:
    entry = manifest['partitions']['BTCUSDT|2026-02']
    print(f"\nBTCUSDT|2026-02 VALIDATED: {entry['VALIDATED']}")
else:
    print("\n✗ BTCUSDT|2026-02 NOT IN MANIFEST")

# Check retry log for errors
print("\n=== Checking retry log for BTCUSDT|2026-02 ===")
retry_log = open('logs/collect_aggtrades_bulk_retry.log').read()
if 'BTCUSDT' in retry_log and '2026-02' in retry_log:
    lines = retry_log.split('\n')
    for i, line in enumerate(lines):
        if '2026-02' in line:
            print(f"Line {i}: {line}")
else:
    print("BTCUSDT|2026-02 not found in retry log")
