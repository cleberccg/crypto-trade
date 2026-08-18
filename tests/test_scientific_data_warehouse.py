from __future__ import annotations

import json

import pandas as pd

import paper_trading.paper_trader as trader_module
from database.bootstrap import bootstrap_database
from database.connection import DatabaseConnection
from database.history_models import IndicatorHistorySnapshot, ScientificTradeSnapshot, SignalSnapshot, TradeHistory
from paper_trading.paper_broker import PaperBroker
from paper_trading.paper_trader import PaperTrader
from scientific_data_warehouse import build_entry_snapshot
from strategies.classic_catalog_strategies import ClassicDonchianBreakoutStrategy


def _make_breakout_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy().iloc[:80].copy()
    prior_high = float(work.iloc[:-1]["high"].tail(20).max())
    work.iloc[-1, work.columns.get_loc("close")] = prior_high * 1.02
    work.iloc[-1, work.columns.get_loc("high")] = prior_high * 1.03
    work.iloc[-1, work.columns.get_loc("open")] = prior_high * 0.99
    work.iloc[-1, work.columns.get_loc("low")] = prior_high * 0.985
    return work


def test_build_entry_snapshot_is_observational_only(ohlcv_df: pd.DataFrame) -> None:
    strategy = ClassicDonchianBreakoutStrategy()
    strategy.initialize()
    frame = _make_breakout_frame(ohlcv_df)
    enriched = strategy.calculate(frame)
    signal_before = strategy.entry_signal(enriched)
    signal_before_copy = {
        "signal": signal_before.signal.value,
        "price": signal_before.price,
        "score": signal_before.score,
        "stop_loss": signal_before.stop_loss,
        "take_profit": signal_before.take_profit,
        "metadata": dict(signal_before.metadata),
    }

    snapshot = build_entry_snapshot(
        frame=enriched,
        symbol="BTC/USDT",
        timeframe="1h",
        execution_id="exec-1",
        strategy_name="ClassicDonchianBreakout",
        strategy_version="v1.0",
        strategy_key="ClassicDonchianBreakout@v1.0",
        campaign_id="spc-test",
        signal=signal_before,
        accepted=True,
        rejection_reason=None,
        risk_reward=2.0,
    )

    signal_after = strategy.entry_signal(enriched)
    assert signal_before.signal.value == signal_after.signal.value
    assert signal_before.price == signal_after.price
    assert signal_before.stop_loss == signal_after.stop_loss
    assert signal_before.take_profit == signal_after.take_profit
    assert signal_before_copy["metadata"] == dict(signal_before.metadata)
    assert snapshot.warehouse_row.entry_reason is not None
    assert json.loads(snapshot.warehouse_row.indicator_context_json)["adx"] is not None


def test_paper_trader_persists_scientific_snapshots(tmp_path, ohlcv_df: pd.DataFrame, monkeypatch) -> None:
    db_path = tmp_path / "scientific_test.sqlite"
    db_url = f"sqlite:///{db_path.as_posix()}"
    bootstrap_database(db_url)
    db = DatabaseConnection(db_url)
    monkeypatch.setattr(trader_module, "get_session", db.session)

    strategy = ClassicDonchianBreakoutStrategy()
    strategy.initialize()
    trader = PaperTrader(
        strategy=strategy,
        broker=PaperBroker(initial_capital=10_000.0),
        execution_id="exec-sdw-1",
        timeframe="1h",
        strategy_version="v1.0",
        campaign_id="spc-test-sdw",
    )
    frame = _make_breakout_frame(ohlcv_df)
    enriched = strategy.calculate(frame)
    signal = strategy.entry_signal(enriched)

    snapshot_id = trader._save_entry_signal(
        symbol="BTC/USDT",
        timestamp=enriched.index[-1].to_pydatetime(),
        signal=signal,
        frame=enriched,
        accepted=True,
        rejection_reason=None,
        rr=2.0,
    )
    trader._highest_price_since_entry = signal.price * 1.03
    trader._lowest_price_since_entry = signal.price * 0.99
    trader._persist_trade_history(
        symbol="BTC/USDT",
        entry_price=signal.price,
        exit_price=signal.price * 1.01,
        stop_loss=signal.stop_loss,
        take_profit=signal.take_profit,
        risk_reward=2.0,
        quantity=1.0,
        pnl=1.0,
        pnl_pct=0.01,
        entry_time=enriched.index[-1].to_pydatetime(),
        exit_time=(enriched.index[-1] + pd.Timedelta(hours=1)).to_pydatetime(),
        duration_minutes=60.0,
        exit_reason="take_profit",
        score=1.0,
        scientific_snapshot_id=snapshot_id,
    )

    with db.session() as session:
        assert session.query(SignalSnapshot).count() == 1
        assert session.query(IndicatorHistorySnapshot).count() == 1
        assert session.query(TradeHistory).count() == 1
        scientific_row = session.query(ScientificTradeSnapshot).one()
        assert scientific_row.signal_snapshot_id is not None
        assert scientific_row.trade_history_id is not None
        assert scientific_row.exit_snapshot_json is not None
        exit_payload = json.loads(scientific_row.exit_snapshot_json)
        assert exit_payload["exit_reason"] == "take_profit"

    db.dispose()