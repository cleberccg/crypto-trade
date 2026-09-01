#!/usr/bin/env python3
"""
Quick post-reconciliation audit verification.
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MANIFEST_PATH = BASE_DIR / "data" / "aggtrades" / "manifest.json"

manifest = json.loads(MANIFEST_PATH.read_text())

print("="*80)
print("POST-RECONCILIATION QUICK AUDIT")
print("="*80)

partitions = manifest.get("partitions", {})
known_gaps = manifest.get("known_gaps", [])
metadata = manifest.get("metadata", {})

validated_yes = sum(1 for e in partitions.values() if e.get("VALIDATED") == "YES")
not_available = sum(1 for e in partitions.values() if e.get("VALIDATED") == "NOT_AVAILABLE")
invalid = sum(1 for e in partitions.values() if e.get("VALIDATED") == "NO")

print(f"\nManifestat entries:")
print(f"  Total partitions: {len(partitions)}")
print(f"  VALIDATED=YES: {validated_yes}")
print(f"  VALIDATED=NOT_AVAILABLE: {not_available}")
print(f"  VALIDATED=NO: {invalid}")
print(f"  KNOWN_GAPS: {len(known_gaps)}")
print(f"  Total (partitions + gaps): {len(partitions) + len(known_gaps)}")

print(f"\nKnown gaps:")
for gap in known_gaps:
    print(f"  - {gap['symbol']}|{gap['period']}: {gap['reason']}")
    print(f"    usable={gap['usable']}")
    print(f"    bounded by: {gap['bounded_by']['previous_valid']} -> {gap['bounded_by']['next_valid']}")

print(f"\nMetadata:")
print(f"  DATASET_MANIFEST_HASH: {metadata.get('DATASET_MANIFEST_HASH')[:16]}...")
print(f"  RECONCILIATION_DATE: {metadata.get('RECONCILIATION_DATE')}")
print(f"  RECONCILIATION_STATUS: {metadata.get('RECONCILIATION_STATUS')}")

# Files on disk
from pathlib import Path
OUT_ROOT = BASE_DIR / "data" / "aggtrades"
btc_files = len([f for f in (OUT_ROOT / "BTCUSDT").rglob("*.parquet") if "_tmp" not in str(f)])
eth_files = len([f for f in (OUT_ROOT / "ETHUSDT").rglob("*.parquet") if "_tmp" not in str(f)])
total_files = btc_files + eth_files

print(f"\nFiles on disk:")
print(f"  BTCUSDT: {btc_files}")
print(f"  ETHUSDT: {eth_files}")
print(f"  Total: {total_files}")

# Comparison
print(f"\nRECONCILIATION RESULT:")
print(f"  Expected partitions: 109 (after removing BTCUSDT|2026-02)")
print(f"  Manifest partitions: {len(partitions)}")
print(f"  Match: {'YES' if len(partitions) == 109 else 'NO'}")

print(f"\n  Expected validated: 107")
print(f"  Actual validated: {validated_yes}")
print(f"  Match: {'YES' if validated_yes == 107 else 'NO'}")

print(f"\n  Expected files: 107 (53 BTC, 54 ETH)")
print(f"  Actual files: {total_files}")
print(f"  Match: {'YES' if total_files == 107 else 'NO'}")

# Check manifest vs files
print(f"\nManifestat vs Files check:")
manifest_no_file = 0
for symbol in ["BTCUSDT", "ETHUSDT"]:
    for key, entry in partitions.items():
        if key.startswith(f"{symbol}|"):
            period = key.split("|")[1]
            parts = period.split("-")
            if len(parts) == 3:  # daily: YYYY-MM-DD
                year, month, day = parts
                pq_file = OUT_ROOT / symbol / year / month / f"{symbol}_{year}_{month}_{day}.parquet"
            else:  # monthly: YYYY-MM
                year, month = parts
                pq_file = OUT_ROOT / symbol / year / month / f"{symbol}_{year}_{month}.parquet"
            
            if entry.get("VALIDATED") == "YES" and not pq_file.exists():
                print(f"  ERROR: {key} claims VALIDATED=YES but file missing: {pq_file}")
                manifest_no_file += 1

if manifest_no_file == 0:
    print(f"  OK: All VALIDATED=YES entries have files")
else:
    print(f"  ERROR: {manifest_no_file} mismatches found")

# Final status
print(f"\n" + "="*80)
if (len(partitions) == 109 and 
    validated_yes == 107 and 
    total_files == 107 and 
    manifest_no_file == 0 and
    len(known_gaps) == 1):
    print("DATASET_VALID: YES")
    print("Status: READY FOR FEATURE_CACHE")
    exit(0)
else:
    print("DATASET_VALID: NO")
    print("Status: RECONCILIATION INCOMPLETE")
    exit(1)
