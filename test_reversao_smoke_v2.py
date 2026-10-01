"""
Teste rápido da estratégia ReversaoNextGenV1 — Fase 5.5 ETAPA 4.

Testa:
1. Se a estratégia pode ser importada com sucesso
2. Se o registro descobre a estratégia
3. Se a fábrica pode criar instâncias
4. Se os métodos da estratégia podem ser chamados
5. Se não há regressões nas estratégias existentes
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

def test_import():
    """Verifica se ReversaoNextGenV1 é importada sem erros."""
    try:
        from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
        print("[PASS] test_import: ReversaoNextGenV1Strategy imported")
        return True
    except Exception as e:
        print(f"[FAIL] test_import: {e}")
        return False


def test_registry():
    """Verifica se o registro descobre ReversaoNextGenV1."""
    try:
        from strategies.registry import discover_strategies, get_registration
        
        discover_strategies()
        
        # Try aliases
        for name in ["ReversaoNextGenV1", "h27", "reversao_v1"]:
            try:
                entry = get_registration(name)
                print(f"[PASS] test_registry: Found {name} as {entry.name}")
                return True
            except:
                continue
        
        print("[FAIL] test_registry: Strategy not found under any alias")
        return False
    except Exception as e:
        print(f"[FAIL] test_registry: {e}")
        return False


def test_factory():
    """Verifica se Factory consegue criar instâncias de ReversaoNextGenV1."""
    try:
        from strategies.factory import create_strategy
        
        strategy1 = create_strategy("ReversaoNextGenV1")
        print(f"[PASS] test_factory: Created {strategy1.name} with defaults")
        
        strategy2 = create_strategy("h27", ema_fast=15, risk_reward_ratio=2.5)
        print(f"[PASS] test_factory: Created {strategy2.name} with custom params")
        
        return True
    except Exception as e:
        print(f"[FAIL] test_factory: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_lifecycle():
    """Verifica a inicialização da estratégia e seus métodos."""
    try:
        from strategies.reversao_nextgen_v1 import ReversaoNextGenV1Strategy
        import pandas as pd
        import numpy as np
        
        strategy = ReversaoNextGenV1Strategy()
        strategy.initialize()
        print(f"[PASS] test_lifecycle: Strategy initialized")
        
        # Dados fictícios
        dates = pd.date_range("2026-01-01", periods=100, freq="1h", tz="UTC")
        df = pd.DataFrame({
            "open": np.random.uniform(100, 110, 100),
            "high": np.random.uniform(110, 115, 100),
            "low": np.random.uniform(95, 100, 100),
            "close": np.random.uniform(100, 110, 100),
            "volume": np.random.uniform(1000, 5000, 100),
        }, index=dates)
        
        df_calc = strategy.calculate(df)
        print(f"[PASS] test_lifecycle: Calculate added {len(df_calc.columns) - len(df.columns)} indicators")
        
        signal = strategy.entry_signal(df_calc)
        print(f"[PASS] test_lifecycle: Entry signal generated (score={signal.score:.2f})")
        
        return True
    except Exception as e:
        print(f"[FAIL] test_lifecycle: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_no_regression():
    """Garante que as estratégias existentes continuem funcionando."""
    try:
        from strategies.factory import create_strategy
        
        for name in ["TrendV1", "TrendV2", "MeanReversionV1", "BreakoutV1"]:
            strategy = create_strategy(name)
        
        print("[PASS] test_no_regression: All existing strategies still work")
        return True
    except Exception as e:
        print(f"[FAIL] test_no_regression: {e}")
        return False


if __name__ == "__main__":
    print("\nFASE 5.5 - ETAPA 4: Validacao Tecnica")
    print("=" * 70)
    
    tests = [
        ("Import", test_import),
        ("Registry", test_registry),
        ("Factory", test_factory),
        ("Lifecycle", test_lifecycle),
        ("No Regression", test_no_regression),
    ]
    
    results = []
    for name, test_func in tests:
        try:
            result = test_func()
            results.append((name, result))
        except Exception as e:
            print(f"[ERROR] {name}: {e}")
            results.append((name, False))
    
    print("\n" + "=" * 70)
    print("SUMMARY")
    print("=" * 70)
    
    passed = sum(1 for _, r in results if r)
    total = len(results)
    
    for name, result in results:
        status = "PASS" if result else "FAIL"
        print(f"  [{status}] {name}")
    
    print(f"\nTotal: {passed}/{total} passed")
    
    if passed == total:
        print("\nAll smoke tests PASSED")
        sys.exit(0)
    else:
        print(f"\n{total - passed} tests FAILED")
        sys.exit(1)
