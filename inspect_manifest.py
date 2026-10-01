#!/usr/bin/env python3
"""Inspeciona a estrutura atual do manifesto."""
import json
from pathlib import Path

manifest_path = Path("data/aggtrades/manifest.json")
manifest = json.loads(manifest_path.read_text())

partitions = manifest["partitions"]
total = len(partitions)

# Separa por granularidade
monthly_entries = {}
daily_entries = {}
for key, entry in partitions.items():
    period = entry["PERIOD"]
    if len(period) == 7:  # YYYY-MM
        monthly_entries[key] = entry
    elif len(period) == 10:  # YYYY-MM-DD
        daily_entries[key] = entry

print(f"MANIFEST INSPECTION")
print(f"=" * 80)
print(f"Total entries: {total}")
print(f"Monthly entries: {len(monthly_entries)}")
print(f"Daily entries: {len(daily_entries)}")
print()

# Conta por estado de validação
validated = sum(1 for e in partitions.values() if e.get("VALIDATED") == "YES")
not_available = sum(1 for e in partitions.values() if e.get("VALIDATED") == "NOT_AVAILABLE")
invalid = sum(1 for e in partitions.values() if e.get("VALIDATED") == "NO")

print(f"Validation status:")
print(f"  VALIDATED=YES: {validated}")
print(f"  VALIDATED=NOT_AVAILABLE: {not_available}")
print(f"  VALIDATED=NO: {invalid}")
print()

# Conta por símbolo
btc = sum(1 for e in partitions.values() if e.get("SYMBOL") == "BTCUSDT")
eth = sum(1 for e in partitions.values() if e.get("SYMBOL") == "ETHUSDT")
print(f"By symbol:")
print(f"  BTCUSDT: {btc}")
print(f"  ETHUSDT: {eth}")
print()

# Exibe as partições com problemas
print(f"Entries with VALIDATED != YES:")
for key in sorted(partitions.keys()):
    entry = partitions[key]
    if entry.get("VALIDATED") != "YES":
        print(f"  {key}: VALIDATED={entry.get('VALIDATED')} ISSUES={entry.get('ISSUES', [])}")
print()

# Exibe as últimas 20 entradas, ordenadas
print(f"Last 20 entries (sorted by key):")
for key in sorted(partitions.keys())[-20:]:
    entry = partitions[key]
    val = entry.get("VALIDATED")
    print(f"  {key}: {val}")
