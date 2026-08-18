"""
Smoke test for ReversaoNextGenV1 strategy integration.

Tests:
1. Strategy imports successfully
2. Registry discovers the strategy
3. Factory can create instances
4. Strategy methods are callable
5. No regressions in existing strategies
"""

import sys
from pathlib import Path

# Add repo to path
sys.path.insert(0, str(Path(__file__).parent))

def test_reversao_import():
    """Test that ReversaoNextGenV1 imports without error."""
    try:
        from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
        print("[OK] Import successful: ReversaoNextGenV1Strategy")
        return True
    except Exception as e:
        print(f"[FAIL] Import failed: {e}")
        return False


def test_registry_discovery():
    """Test that registry discovers ReversaoNextGenV1."""
    try:
        from strategies.registry import discover_strategies, lookup_strategy
        
        discover_strategies()
        
        # Try multiple names/aliases
        for name in ["ReversaoNextGenV1", "h27", "reversao_v1", "reversaov1"]:
            entry = lookup_strategy(name)
            if entry:
                print(f"[OK] Registry discovery successful: {name} → {entry.name}")
                return True
        
        print("[FAIL] Registry did not find ReversaoNextGenV1 under any alias")
        return False
    except Exception as e:
        print(f"✗ Registry discovery failed: {e}")
        return False


def test_factory_creation():
    """Test that Factory can create ReversaoNextGenV1 instances."""
    try:
        from strategies.factory import create_strategy
        
        # Default parameters
        strategy1 = create_strategy("ReversaoNextGenV1")
        print(f"[OK] Factory creation successful (default params): {strategy1.name}")
        
        # Custom parameters
        strategy2 = create_strategy(
            "reversao_v1",
            ema_fast=15,
            ema_slow=45,
            risk_reward_ratio=2.5,
        )
        print(f"[OK] Factory creation successful (custom params): {strategy2.name}")
        
        return True
    except Exception as e:
        print(f"✗ Factory creation failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_strategy_lifecycle():
    """Test strategy initialization and basic methods."""
    try:
        from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
        import pandas as pd
        import numpy as np
        
        strategy = ReversaoNextGenV1Strategy()
        
        # Initialize
        strategy.initialize()
        print(f"✓ Strategy initialization successful: {strategy.name}")
        
        # Create dummy OHLCV data
        dates = pd.date_range("2026-01-01", periods=100, freq="1h", tz="UTC")
        df = pd.DataFrame({
            "open": np.random.uniform(100, 110, 100),
            "high": np.random.uniform(110, 115, 100),
            "low": np.random.uniform(95, 100, 100),
            "close": np.random.uniform(100, 110, 100),
            "volume": np.random.uniform(1000, 5000, 100),
        }, index=dates)
        
        # Test calculate
        df_enriched = strategy.calculate(df)
        print(f"✓ Calculate successful: added {len(df_enriched.columns) - len(df.columns)} indicator columns")
        
        # Test entry signal
        signal = strategy.entry_signal(df_enriched)
        print(f"✓ Entry signal generated: {signal.signal.value}, confidence={signal.score:.2f}")
        
        # Test exit signal (assuming entry at 105)
        exit_signal = strategy.exit_signal(df_enriched, entry_price=105.0)
        print(f"✓ Exit signal generated: {exit_signal.signal.value}")
        
        # Test score
        score = strategy.score(df_enriched)
        print(f"✓ Score calculation: {score:.2f}")
        
        return True
    except Exception as e:
        print(f"✗ Strategy lifecycle test failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_existing_strategies():
    """Ensure no regression in existing strategies."""
    try:
        from strategies.factory import create_strategy
        
        existing = ["TrendV1", "TrendV2", "MeanReversionV1", "BreakoutV1"]
        
        for name in existing:
            strategy = create_strategy(name)
            print(f"✓ Existing strategy still works: {name}")
        
        return True
    except Exception as e:
        print(f"✗ Regression test failed: {e}")
        return False


def run_all_tests():
    """Run all smoke tests."""
    print("\n" + "="*70)
    print("FASE 5.5 — SMOKE TESTS: ReversaoNextGenV1 Strategy")
    print("="*70 + "\n")
    
    tests = [
        ("Import", test_reversao_import),
        ("Registry Discovery", test_registry_discovery),
        ("Factory Creation", test_factory_creation),
        ("Strategy Lifecycle", test_strategy_lifecycle),
        ("Regression Test", test_existing_strategies),
    ]
    
    results = []
    for test_name, test_func in tests:
        print(f"\n[{test_name}]")
        passed = test_func()
        results.append((test_name, passed))
    
    # Summary
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    
    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)
    
    for test_name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status:8} {test_name}")
    
    print(f"\nTotal: {passed_count}/{total_count} tests passed")
    
    if passed_count == total_count:
        print("\n✓ All smoke tests PASSED")
        return 0
    else:
        print(f"\n✗ {total_count - passed_count} tests FAILED")
        return 1


if __name__ == "__main__":
    sys.exit(run_all_tests())
