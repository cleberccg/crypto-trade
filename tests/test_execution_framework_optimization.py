from __future__ import annotations

from pathlib import Path

from main import _parse_args
from research.services.execution_framework_optimization import (
    ExecutionFrameworkOptimizationConfig,
    ExecutionFrameworkOptimizationService,
)


def test_cli_parser_execution_framework_optimization(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "main.py",
            "execution-framework-optimization",
            "--strategy-name",
            "TradeOutcomeNextGenV1",
            "--benchmark-bars",
            "5000",
            "--skip-phase9-rerun",
        ],
    )
    args = _parse_args()
    assert args.command == "execution-framework-optimization"
    assert args.strategy_name == "TradeOutcomeNextGenV1"
    assert args.benchmark_bars == 5000
    assert args.skip_phase9_rerun is True


def test_execution_framework_service_writes_artifacts(tmp_path: Path) -> None:
    service = ExecutionFrameworkOptimizationService(session=None, base_dir=Path(__file__).resolve().parents[1])
    result = service.run(
        ExecutionFrameworkOptimizationConfig(
            strategy_name="TradeOutcomeNextGenV1",
            benchmark_symbol="BTC/USDT",
            benchmark_timeframe="5m",
            benchmark_bars=500,
            output_prefix="execution_framework_optimization_test",
            rerun_phase9=False,
            persist_to_db=False,
        )
    )

    assert "summary" in result
    assert "outputs" in result
    assert Path(result["outputs"]["json"]).exists()
    assert Path(result["outputs"]["csv"]).exists()
    assert Path(result["outputs"]["md"]).exists()
    assert isinstance(result["summary"]["equivalence_passed"], bool)
