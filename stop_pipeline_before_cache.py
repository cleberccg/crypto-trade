#!/usr/bin/env python3
"""
STOP PIPELINE BEFORE FEATURE_CACHE
Conforme solicitado do usuario.

Atualiza research_pipeline_state.json para STOP status.
"""
import json
from pathlib import Path
from datetime import datetime, timezone

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "research_pipeline_state.json"

print("="*80)
print("STOPPING PIPELINE BEFORE FEATURE_CACHE")
print("="*80)

if not STATE_FILE.exists():
    print(f"\nWARNING: {STATE_FILE} does not exist")
    print("Creating new state file...")
    state = {
        "stage": "VALIDATION",
        "status": "STOPPED_BEFORE_FEATURE_CACHE",
        "reason": "Reconciliation complete - manual authorization required for feature cache",
        "dataset_valid": True,
        "dataset_manifest_hash": "a851c3fa38a7f8cad3553f1e45dad38ac204bb821c5eafeab0cda221cb4d4357",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
else:
    state = json.loads(STATE_FILE.read_text())
    state["stage"] = "VALIDATION"
    state["status"] = "STOPPED_BEFORE_FEATURE_CACHE"
    state["reason"] = "Reconciliation complete - manual authorization required"
    state["dataset_valid"] = True
    state["dataset_manifest_hash"] = "a851c3fa38a7f8cad3553f1e45dad38ac204bb821c5eafeab0cda221cb4d4357"
    state["timestamp"] = datetime.now(timezone.utc).isoformat()

STATE_FILE.write_text(json.dumps(state, indent=2, default=str), encoding='utf-8')

print(f"\nPipeline state updated:")
print(f"  Stage: {state['stage']}")
print(f"  Status: {state['status']}")
print(f"  Dataset valid: {state['dataset_valid']}")
print(f"  New manifest hash: {state['dataset_manifest_hash'][:48]}...")
print(f"  Timestamp: {state['timestamp']}")

print(f"\nPipeline STOPPED before FEATURE_CACHE")
print(f"Reason: Reconciliation complete, awaiting authorization")
print(f"\nTo continue:")
print(f"  1. Review RECONCILIATION_COMPLETE.md")
print(f"  2. Verify all checks passed")
print(f"  3. Run: python run_full_research_pipeline.py")

print("\n" + "="*80 + "\n")
