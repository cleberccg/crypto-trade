#!/usr/bin/env python3
"""
DATASET MANIFEST RECONCILIATION - OPERACIONAL
Executa reconciliacao conforme diagnostico de reconcile_dataset.py:
1. Remove BTCUSDT|2026-02 do manifest (dados invalidos)
2. Cria secao KNOWN_GAPS com documentacao formal
3. Valida que 2026-08-24 esta como NOT_AVAILABLE (correto)
4. Preserva 23 arquivos diarios de agosto como canonicos
5. Calcula novo DATASET_MANIFEST_HASH
6. Registra timestamp de reconciliacao
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
OUT_ROOT = BASE_DIR / "data" / "aggtrades"
MANIFEST_PATH = OUT_ROOT / "manifest.json"

def reconcile_manifest() -> tuple[dict[str, Any], str]:
    """Reconciliar manifest conforme plano diagnosticado."""
    
    print("="*80)
    print("DATASET MANIFEST RECONCILIATION")
    print("="*80)
    
    manifest = json.loads(MANIFEST_PATH.read_text())
    
    # Status inicial
    print(f"\nSTATUS INICIAL:")
    print(f"  Total partitions in manifest: {len(manifest['partitions'])}")
    validated_before = sum(1 for e in manifest['partitions'].values() if e.get('VALIDATED') == 'YES')
    print(f"  VALIDATED=YES: {validated_before}")
    
    # TAREFA: Remove BTCUSDT|2026-02
    print(f"\nTAREFA 1: REMOVE BTCUSDT|2026-02")
    btc_feb_key = "BTCUSDT|2026-02"
    if btc_feb_key in manifest['partitions']:
        btc_feb_entry = manifest['partitions'][btc_feb_key]
        print(f"  Removing: {btc_feb_key}")
        print(f"    VALIDATED: {btc_feb_entry.get('VALIDATED')}")
        print(f"    ISSUES: {btc_feb_entry.get('ISSUES')}")
        print(f"    RECORDS: {btc_feb_entry.get('RECORDS')}")
        del manifest['partitions'][btc_feb_key]
        print(f"  REMOVED")
    else:
        print(f"  NOT FOUND IN MANIFEST (unexpected!)")
    
    # TAREFA: Create KNOWN_GAPS section
    print(f"\nTAREFA 2: CREATE KNOWN_GAPS SECTION")
    if "known_gaps" not in manifest:
        manifest["known_gaps"] = []
        print(f"  Created known_gaps section")
    
    # Add BTCUSDT|2026-02 as known gap
    known_gap_entry = {
        "symbol": "BTCUSDT",
        "period": "2026-02",
        "reason": "OFFICIAL_SOURCE_DATA_CORRUPTION",
        "usable": False,
        "details": {
            "issues": [
                "duplicate_agg_trade_ids=3000",
                "agg_trade_id_not_sorted",
                "timestamp_not_sorted"
            ],
            "records_attempted": 52474665,
            "documentation_date": datetime.now(timezone.utc).isoformat(),
        },
        "bounded_by": {
            "previous_valid": "2026-01",
            "next_valid": "2026-03"
        }
    }
    manifest["known_gaps"] = [known_gap_entry]
    print(f"  Added known_gap: BTCUSDT|2026-02")
    print(f"    Reason: OFFICIAL_SOURCE_DATA_CORRUPTION")
    print(f"    Issues: duplicate IDs, unsorted agg_trade_id, unsorted timestamp")
    print(f"    Bounded by: 2026-01 (valid) -> GAP -> 2026-03 (valid)")
    
    # TAREFA: Verify 2026-08-24 is NOT_AVAILABLE (should not remove - it's correct)
    print(f"\nTAREFA 3: VERIFY AUG 24 ENTRIES (NOT_AVAILABLE is CORRECT)")
    for symbol in ["BTCUSDT", "ETHUSDT"]:
        key = f"{symbol}|2026-08-24"
        if key in manifest['partitions']:
            entry = manifest['partitions'][key]
            status = entry.get('VALIDATED')
            print(f"  {key}: VALIDATED={status}")
            if status == "NOT_AVAILABLE":
                print(f"    CORRECT - day not yet published by Binance")
            else:
                print(f"    WARNING: expected NOT_AVAILABLE, got {status}")
    
    # TAREFA: August 2026 is canonical as individual daily files (already correct in manifest)
    print(f"\nTAREFA 4: AUGUST 2026 CANONICAL MODEL = DAILY FILES")
    btc_aug_daily = sum(1 for k in manifest['partitions'].keys() if k.startswith('BTCUSDT|2026-08-'))
    eth_aug_daily = sum(1 for k in manifest['partitions'].keys() if k.startswith('ETHUSDT|2026-08-'))
    print(f"  BTCUSDT August daily entries: {btc_aug_daily}")
    print(f"  ETHUSDT August daily entries: {eth_aug_daily}")
    print(f"  Canonical choice: keep individual daily files (not consolidated monthly)")
    
    # TAREFA: Recalculate DATASET_MANIFEST_HASH
    print(f"\nTAREFA 5: RECALCULATE DATASET_MANIFEST_HASH")
    
    # Collect all VALIDATED=YES partitions for hash
    validated_partitions = []
    for key in sorted(manifest['partitions'].keys()):
        entry = manifest['partitions'][key]
        if entry.get('VALIDATED') == 'YES':
            validated_partitions.append({
                'key': key,
                'hash': entry.get('HASH', ''),
                'records': entry.get('RECORDS', 0),
            })
    
    print(f"  Total validated partitions: {len(validated_partitions)}")
    
    # Hash concatenation: SHA256(sorted validated hashes)
    hash_input = ""
    for part in validated_partitions:
        hash_input += f"{part['key']}:{part['hash']}:"
    
    dataset_hash = hashlib.sha256(hash_input.encode('utf-8')).hexdigest()
    print(f"  NEW DATASET_MANIFEST_HASH: {dataset_hash}")
    
    # Store in manifest
    manifest['metadata'] = manifest.get('metadata', {})
    manifest['metadata']['DATASET_MANIFEST_HASH'] = dataset_hash
    manifest['metadata']['RECONCILIATION_DATE'] = datetime.now(timezone.utc).isoformat()
    manifest['metadata']['RECONCILIATION_STATUS'] = 'COMPLETE'
    manifest['metadata']['TOTAL_PARTITIONS_BEFORE'] = 110
    manifest['metadata']['TOTAL_PARTITIONS_AFTER'] = len(manifest['partitions'])
    manifest['metadata']['VALIDATED_PARTITIONS'] = len(validated_partitions)
    manifest['metadata']['KNOWN_GAPS'] = len(manifest.get('known_gaps', []))
    
    print(f"\nRECONCILIATION STATUS:")
    print(f"  Total partitions after: {manifest['metadata']['TOTAL_PARTITIONS_AFTER']}")
    print(f"  VALIDATED=YES after: {len(validated_partitions)}")
    print(f"  KNOWN_GAPS: {len(manifest.get('known_gaps', []))}")
    print(f"  Timestamp: {manifest['metadata']['RECONCILIATION_DATE']}")
    
    return manifest, dataset_hash


def main() -> int:
    """Execute reconciliation."""
    
    try:
        manifest, new_hash = reconcile_manifest()
        
        # Verify before saving
        print(f"\n{'='*80}")
        print("VERIFICATION BEFORE SAVING")
        print(f"{'='*80}")
        
        total_partitions = len(manifest['partitions'])
        total_known_gaps = len(manifest.get('known_gaps', []))
        validated = sum(1 for e in manifest['partitions'].values() if e.get('VALIDATED') == 'YES')
        not_available = sum(1 for e in manifest['partitions'].values() if e.get('VALIDATED') == 'NOT_AVAILABLE')
        invalid = sum(1 for e in manifest['partitions'].values() if e.get('VALIDATED') == 'NO')
        
        print(f"\nPartitions breakdown:")
        print(f"  VALIDATED=YES: {validated}")
        print(f"  VALIDATED=NOT_AVAILABLE: {not_available}")
        print(f"  VALIDATED=NO: {invalid}")
        print(f"  Total partitions: {total_partitions}")
        print(f"  Known gaps: {total_known_gaps}")
        
        if total_partitions + total_known_gaps != 110:
            print(f"\nWARNING: Total != 110")
            return 1
        
        if validated != 107:
            print(f"\nWARNING: VALIDATED != 107, got {validated}")
            return 1
        
        # Backup old manifest
        backup_path = MANIFEST_PATH.with_suffix('.backup')
        if MANIFEST_PATH.exists():
            import shutil
            shutil.copy(MANIFEST_PATH, backup_path)
            print(f"\nBACKUP: {backup_path}")
        
        # Save
        MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, default=str), encoding='utf-8')
        print(f"\nSAVED: {MANIFEST_PATH}")
        print(f"NEW DATASET_MANIFEST_HASH: {new_hash}")
        
        print(f"\nRECONCILIATION COMPLETE")
        print(f"Next: Run full audit to verify DATASET_VALID=YES")
        
        return 0
        
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
