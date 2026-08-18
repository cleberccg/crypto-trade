from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

import research.services.edge_operational_pipeline as eop_module
import validation.validator as validator_module
from backtesting.engine import BacktestConfig, BacktestEngine
from execution.hypothesis_runtime import (
    HypothesisApprovedContext,
    HypothesisGateConfig,
    wrap_strategy_with_hypothesis,
)
from execution.live_trading_service import LiveTradingConfig, LiveTradingService
from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService
from research.services.edge_operational_pipeline import (
    ApprovedContext,
    EdgeOperationalPipelineConfig,
    EdgeOperationalPipelineService,
    OperationalHypothesisContract,
)
from strategies.base_strategy import BaseStrategy, SignalType, StrategySignal


class DummyGateStrategy(BaseStrategy):
    def __init__(self, entry_step: int = 5, tp_pct: float = 0.006):
        self.entry_step = int(entry_step)
        self.tp_pct = float(tp_pct)

    @property
    def name(self) -> str:
        return "DummyGateStrategy"

    def initialize(self) -> None:
        return None

    def calculate(self, df: pd.DataFrame) -> pd.DataFrame:
        return df

    def entry_signal(self, df: pd.DataFrame) -> StrategySignal:
        price = float(df.iloc[-1]["close"])
        ts = df.index[-1].to_pydatetime()
        if len(df) % max(2, self.entry_step) == 0:
            return StrategySignal(
                signal=SignalType.BUY,
                price=price,
                timestamp=ts,
                score=0.7,
                stop_loss=price * 0.99,
                take_profit=price * (1.0 + self.tp_pct),
            )
        return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=ts)

    def exit_signal(self, df: pd.DataFrame, entry_price: float) -> StrategySignal:
        price = float(df.iloc[-1]["close"])
        ts = df.index[-1].to_pydatetime()
        if price >= entry_price * (1.0 + self.tp_pct):
            return StrategySignal(signal=SignalType.SELL, price=price, timestamp=ts, metadata={"exit_reason": "tp"})
        return StrategySignal(signal=SignalType.HOLD, price=price, timestamp=ts)

    def score(self, df: pd.DataFrame) -> float:
        return 0.7


def _build_df(bars: int = 140, gate_value: float = 1.0, trend: str = "bullish", vol: str = "high_volatility") -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    idx = [start + timedelta(minutes=5 * i) for i in range(bars)]
    close = [100.0 + 0.08 * i + (0.2 if i % 9 == 0 else 0.0) for i in range(bars)]
    rows = []
    for c in close:
        rows.append(
            {
                "open": c - 0.1,
                "high": c + 0.2,
                "low": c - 0.2,
                "close": c,
                "volume": 1000.0,
                "gate_flag": gate_value,
                "trend_bucket": trend,
                "vol_regime": vol,
            }
        )
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx, tz="UTC"))


def _run_backtest(entry_step: int, df: pd.DataFrame, gate: HypothesisGateConfig) -> int:
    strategy = DummyGateStrategy(entry_step=entry_step)
    wrapped = wrap_strategy_with_hypothesis(strategy, gate, symbol="BTC/USDT", timeframe="5m")
    wrapped.initialize()
    wrapped.prepare_dataset(df.copy(), symbol="BTC/USDT", timeframe="5m")
    result = BacktestEngine(wrapped, config=BacktestConfig(initial_capital=10_000.0)).run(df, symbol="BTC/USDT", timeframe="5m")
    return int(result.metrics.total_trades)


def test_hypothesis_gate_filter_and_regime_pass_fail() -> None:
    base = DummyGateStrategy(entry_step=2)
    gate = HypothesisGateConfig(
        approved_filters=("gate_flag >= 1",),
        approved_contexts=(
            HypothesisApprovedContext(
                symbol="BTC/USDT",
                timeframe="5m",
                trend_bucket="bullish",
                vol_regime="high_volatility",
            ),
        ),
        regime="bullish|high_volatility",
    )

    wrapped = wrap_strategy_with_hypothesis(base, gate, symbol="BTC/USDT", timeframe="5m")
    wrapped.initialize()

    df_ok = _build_df(bars=60, gate_value=1.0, trend="bullish", vol="high_volatility")
    sig_ok = wrapped.entry_signal(df_ok)
    assert sig_ok.signal == SignalType.BUY

    df_filter_fail = _build_df(bars=60, gate_value=0.0, trend="bullish", vol="high_volatility")
    sig_filter_fail = wrapped.entry_signal(df_filter_fail)
    assert sig_filter_fail.signal == SignalType.HOLD

    df_regime_fail = _build_df(bars=60, gate_value=1.0, trend="bearish", vol="low_volatility")
    sig_regime_fail = wrapped.entry_signal(df_regime_fail)
    assert sig_regime_fail.signal == SignalType.HOLD


def test_backtest_baseline_vs_hypothesis_and_filter_changes() -> None:
    df = _build_df(bars=140, gate_value=1.0, trend="bullish", vol="high_volatility")

    gate_baseline = HypothesisGateConfig()
    baseline_trades = _run_backtest(entry_step=7, df=df, gate=gate_baseline)

    gate_hypothesis_ok = HypothesisGateConfig(
        approved_filters=("gate_flag >= 1",),
        approved_contexts=(HypothesisApprovedContext(symbol="BTC/USDT", timeframe="5m"),),
        regime="bullish|high_volatility",
    )
    hypothesis_trades = _run_backtest(entry_step=5, df=df, gate=gate_hypothesis_ok)

    gate_hypothesis_rejected = HypothesisGateConfig(
        approved_filters=("gate_flag >= 2",),
        approved_contexts=(HypothesisApprovedContext(symbol="BTC/USDT", timeframe="5m"),),
        regime="bullish|high_volatility",
    )
    rejected_trades = _run_backtest(entry_step=5, df=df, gate=gate_hypothesis_rejected)

    assert baseline_trades != hypothesis_trades
    assert rejected_trades == 0
    assert hypothesis_trades > 0


def test_walk_forward_uses_hypothesis_gate(monkeypatch, tmp_path: Path) -> None:
    df = _build_df(bars=180, gate_value=1.0, trend="bullish", vol="high_volatility")

    orig_eop_factory = eop_module.create_strategy
    orig_validator_factory = validator_module.create_strategy
    orig_get_range = validator_module.OptimizationValidator._get_available_date_range
    orig_load_df = validator_module.OptimizationValidator._load_dataframe
    orig_validate = validator_module.OptimizationValidator.validate

    try:
        monkeypatch.setattr(eop_module, "create_strategy", lambda _name, **kwargs: DummyGateStrategy(**kwargs))
        monkeypatch.setattr(validator_module, "create_strategy", lambda _name, **kwargs: DummyGateStrategy(**kwargs))
        monkeypatch.setattr(
            validator_module.OptimizationValidator,
            "_get_available_date_range",
            staticmethod(lambda symbol, timeframe: (df.index[0].to_pydatetime(), df.index[-1].to_pydatetime())),
        )
        monkeypatch.setattr(
            validator_module.OptimizationValidator,
            "_load_dataframe",
            lambda self, symbol, timeframe, start, end: df.loc[(df.index >= start) & (df.index <= end)].copy(),
        )

        class _Best:
            def __init__(self, passed: bool):
                self.rank = 1
                self.parameters = {}
                self.train_metrics = {"total_trades": 1 if passed else 0}
                self.validation_metrics = {"total_trades": 1 if passed else 0}
                self.passed = passed
                self.discard_reasons = []
                self.overfitting_risk = False

        class _Summary:
            def __init__(self, passed: bool):
                self.total_candidates = 1
                self.discarded = 0 if passed else 1
                self.passed = 1 if passed else 0
                self.best_validated = _Best(passed) if passed else None
                self.validated_top = [self.best_validated] if self.best_validated is not None else []
                self.output_files = []

        def fake_validate(self, *args, **kwargs):
            built = self._strategy_factory({}, "BTC/USDT", "5m")  # type: ignore[attr-defined]
            allow_sig = built.entry_signal(df.iloc[:60])
            return _Summary(allow_sig.signal == SignalType.BUY)

        monkeypatch.setattr(validator_module.OptimizationValidator, "validate", fake_validate)

        service = EdgeOperationalPipelineService(tmp_path)
        cfg = EdgeOperationalPipelineConfig(
            symbols=("BTC/USDT",),
            timeframes=("5m",),
            walk_forward_min_trades=1,
            walk_forward_min_profit_factor=0.0,
            walk_forward_max_drawdown_pct=100.0,
            walk_forward_min_win_rate_pct=0.0,
            walk_forward_min_expectancy=-1_000_000.0,
            walk_forward_min_sharpe=-1_000_000.0,
        )

        contract_ok = OperationalHypothesisContract(
            strategy_name="DummyGateStrategy",
            strategy_version="v1.0",
            symbols=("BTC/USDT",),
            timeframes=("5m",),
            regime="bullish|high_volatility",
            approved_contexts=(ApprovedContext(symbol="BTC/USDT", timeframe="5m", trend_bucket="bullish", vol_regime="high_volatility"),),
            approved_filters=("gate_flag >= 1",),
            approved_parameters={"entry_step": 5, "tp_pct": 0.006},
            promotion_criteria={},
            hypothesis_status="ROUTED",
            status_history=("ROUTED",),
        )

        contract_blocked = OperationalHypothesisContract(
            strategy_name="DummyGateStrategy",
            strategy_version="v1.0",
            symbols=("BTC/USDT",),
            timeframes=("5m",),
            regime="bearish|low_volatility",
            approved_contexts=(ApprovedContext(symbol="BTC/USDT", timeframe="5m", trend_bucket="bearish", vol_regime="low_volatility"),),
            approved_filters=("gate_flag >= 2",),
            approved_parameters={"entry_step": 5, "tp_pct": 0.006},
            promotion_criteria={},
            hypothesis_status="ROUTED",
            status_history=("ROUTED",),
        )

        wf_ok = service._run_walk_forward(cfg, contract_ok)
        wf_blocked = service._run_walk_forward(cfg, contract_blocked)

        assert wf_ok.get("passed") is True
        assert wf_blocked.get("passed") is False
    finally:
        eop_module.create_strategy = orig_eop_factory
        validator_module.create_strategy = orig_validator_factory
        validator_module.OptimizationValidator._get_available_date_range = orig_get_range
        validator_module.OptimizationValidator._load_dataframe = orig_load_df
        validator_module.OptimizationValidator.validate = orig_validate


def test_paper_live_runtime_strategy_applies_parameters_and_gate(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_create_strategy(_name: str, **kwargs):
        captured["kwargs"] = kwargs
        return DummyGateStrategy(**kwargs)

    monkeypatch.setattr("paper_trading.paper_live_service.create_strategy", fake_create_strategy)

    service = PaperLiveService(tmp_path)
    cfg = PaperLiveConfig(
        symbol="BTC/USDT",
        timeframe="5m",
        strategy_name="DummyGateStrategy",
        strategy_version="v1.0",
        hypothesis_config={
            "approved_parameters": {"entry_step": 3, "tp_pct": 0.004},
            "approved_filters": ["gate_flag >= 1"],
            "regime": "bullish|high_volatility",
            "approved_contexts": [
                {
                    "symbol": "BTC/USDT",
                    "timeframe": "5m",
                    "trend_bucket": "bullish",
                    "vol_regime": "high_volatility",
                }
            ],
        },
    )

    runtime_strategy = service._build_runtime_strategy(cfg)
    assert captured["kwargs"] == {"entry_step": 3, "tp_pct": 0.004}

    df_ok = _build_df(bars=60, gate_value=1.0, trend="bullish", vol="high_volatility")
    assert runtime_strategy.entry_signal(df_ok).signal == SignalType.BUY

    df_rejected = _build_df(bars=60, gate_value=0.0, trend="bullish", vol="high_volatility")
    assert runtime_strategy.entry_signal(df_rejected).signal == SignalType.HOLD


def test_same_hypothesis_payload_produces_same_gate_behavior_in_paper_and_live(
    monkeypatch,
    tmp_path: Path,
) -> None:
    hypothesis_payload = {
        "approved_parameters": {"entry_step": 3, "tp_pct": 0.004},
        "approved_filters": ["gate_flag >= 1"],
        "regime": "bullish|high_volatility",
        "approved_contexts": [
            {
                "symbol": "BTC/USDT",
                "timeframe": "5m",
                "trend_bucket": "bullish",
                "vol_regime": "high_volatility",
            }
        ],
    }

    monkeypatch.setattr(
        "paper_trading.paper_live_service.create_strategy",
        lambda _name, **kwargs: DummyGateStrategy(**kwargs),
    )

    paper_service = PaperLiveService(tmp_path)
    paper_cfg = PaperLiveConfig(
        symbol="BTC/USDT",
        timeframe="5m",
        strategy_name="DummyGateStrategy",
        strategy_version="v1.0",
        hypothesis_config=hypothesis_payload,
    )
    paper_strategy = paper_service._build_runtime_strategy(paper_cfg, hypothesis_payload=hypothesis_payload)

    live_service = LiveTradingService(
        base_dir=tmp_path,
        exchange_factory=lambda: object(),
        strategy_factory=lambda _name, **kwargs: DummyGateStrategy(**kwargs),
        sleep_fn=lambda _: None,
        db_ops={"load_all_open_trade_states": lambda: {}},
    )
    live_cfg = LiveTradingConfig(
        symbol="BTC/USDT",
        timeframe="5m",
        strategy_name="DummyGateStrategy",
        strategy_version="v1.0",
        hypothesis_config=hypothesis_payload,
    )
    live_strategy = live_service._build_runtime_strategy(live_cfg, symbol="BTC/USDT", timeframe="5m")

    df_ok = _build_df(bars=60, gate_value=1.0, trend="bullish", vol="high_volatility")
    df_rejected = _build_df(bars=60, gate_value=0.0, trend="bullish", vol="high_volatility")

    assert paper_strategy.entry_signal(df_ok).signal == SignalType.BUY
    assert live_strategy.entry_signal(df_ok).signal == SignalType.BUY
    assert paper_strategy.entry_signal(df_rejected).signal == SignalType.HOLD
    assert live_strategy.entry_signal(df_rejected).signal == SignalType.HOLD
