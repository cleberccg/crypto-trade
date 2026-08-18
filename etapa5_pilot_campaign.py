#!/usr/bin/env python3
"""
ETAPA 5: Pilot Campaign Execution - ReversaoNextGenV1 Strategy

Runs a focused optimization campaign to test ReversaoNextGenV1 strategy
across a small grid of parameters on BTC/USDT 5m timeframe.

Phase 5.5 - Implementation Phase
User: implementação de ReversaoNextGenV1 na plataforma
"""

import argparse
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from config.settings import settings
from core.events import EventBus, EventType
from execution_manager.runner import HistoryListener, LogListener, MetricsListener
from notifications.notification_service import get_notification_service
from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer


def main():
    """Execute pilot optimization campaign for ReversaoNextGenV1."""
    
    print("=" * 70)
    print("ETAPA 5: PILOT CAMPAIGN - ReversaoNextGenV1")
    print("=" * 70)
    print()
    
    # Configuration
    symbol = "BTC/USDT"
    timeframe = "5m"
    start_date = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end_date = datetime(2024, 12, 31, tzinfo=timezone.utc)
    
    capital = 10_000
    workers = 16  # Use available workers
    max_combinations = 500  # Small grid: 3×3×1×1×3×3×3×2×2×2 = 648 total, sample 500
    top_n = 10
    
    print(f"Symbol:           {symbol}/{timeframe}")
    print(f"Start:            {start_date}")
    print(f"End:              {end_date}")
    print(f"Capital:          {capital:,.0f} USDT")
    print(f"Workers:          {workers}")
    print(f"Max combinations: {max_combinations}")
    print(f"Top results:      {top_n}")
    print()
    
    # Setup event handling
    history_listener = HistoryListener(checkpoint_interval=settings.optimizer.checkpoint_interval)
    metrics_listener = MetricsListener()
    
    notification_service = get_notification_service()
    notification_service.start()
    
    event_bus = EventBus(
        listeners=[history_listener, LogListener(), metrics_listener],
        async_dispatch=False,
    )
    
    # Create optimizer
    optimizer = StrategyOptimizer(
        event_bus=event_bus,
        checkpoint_interval=settings.optimizer.checkpoint_interval,
    )
    
    # Generate execution ID
    execution_id = f"pilot_reversao_v1_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    
    print(f"Execution ID:     {execution_id}")
    print()
    print("Starting optimization campaign...")
    print("-" * 70)
    
    try:
        # Run optimization
        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=symbol,
                timeframe=timeframe,
                start=start_date,
                end=end_date,
                capital=capital,
                top_n=top_n,
                workers=workers,
                max_combinations=max_combinations,
                diagnostic=False,
                execution_id=execution_id,
                resume_from=0,
                checkpoint_interval=settings.optimizer.checkpoint_interval,
                strategy_name="ReversaoNextGenV1",
                strategy_version="v1",
                git_commit=os.getenv("GIT_COMMIT"),
                host=platform.node(),
                cpu=platform.processor() or None,
                python_version=platform.python_version(),
            )
        )
        
        # Print results
        print()
        print("=" * 70)
        print("OPTIMIZATION RESULTS")
        print("=" * 70)
        print(f"Combinations tested:   {summary.combinations_tested}")
        print(f"Combinations discarded: {summary.combinations_discarded}")
        print(f"Duration:              {summary.duration_seconds:.2f}s")
        print()
        
        if summary.best_profit_factor:
            print(f"Best Profit Factor:    {summary.best_profit_factor.metrics.get('profit_factor', 'N/A')}")
            print(f"  Parameters: {summary.best_profit_factor.parameters}")
            print()
        
        if summary.best_net_profit:
            print(f"Best Net Profit:       {summary.best_net_profit.metrics.get('net_profit', 'N/A'):.2f}")
            print(f"  Parameters: {summary.best_net_profit.parameters}")
            print()
        
        if summary.best_sharpe:
            print(f"Best Sharpe Ratio:     {summary.best_sharpe.metrics.get('sharpe_ratio', 'N/A'):.2f}")
            print(f"  Parameters: {summary.best_sharpe.parameters}")
            print()
        
        if summary.lowest_drawdown:
            print(f"Lowest Max Drawdown:   {summary.lowest_drawdown.metrics.get('max_drawdown_pct', 'N/A'):.2f}%")
            print(f"  Parameters: {summary.lowest_drawdown.parameters}")
            print()
        
        if summary.top_results:
            print(f"Top {min(len(summary.top_results), top_n)} Results:")
            for i, result in enumerate(summary.top_results[:top_n], 1):
                pf = result.metrics.get('profit_factor', 0)
                np = result.metrics.get('net_profit', 0)
                dd = result.metrics.get('max_drawdown_pct', 0)
                sr = result.metrics.get('sharpe_ratio', 0)
                print(f"  {i}. PF={pf:.2f} NP={np:,.0f} DD={dd:.1f}% SR={sr:.2f}")
                print(f"     {result.parameters}")
        
        print()
        print("Output files:")
        for output_file in summary.output_files:
            print(f"  {output_file}")
        
        print()
        print("=" * 70)
        print("ETAPA 5 COMPLETED SUCCESSFULLY")
        print("=" * 70)
        print()
        print("Next: ETAPA 6 - Pipeline Scientific Validation")
        print("  - Run validation suite on top parameter sets")
        print("  - Execute strategy research labs")
        print("  - Generate comprehensive metrics report")
        print()
        
        return 0
    
    except ValueError as exc:
        print(f"\nERROR: Optimization failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"\nERROR: Unexpected error: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1
    
    finally:
        notification_service.stop()


if __name__ == "__main__":
    sys.exit(main())
