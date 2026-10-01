from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from database.bootstrap import _migrate_live_accounting_schema
import execution.live_trading_service as live_service_module
from execution.live_trading_service import LiveTradingService


def test_live_accounting_migration_adds_columns_without_rewriting_history(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE trades (id INTEGER PRIMARY KEY, status VARCHAR(10), "
                "quantity FLOAT, entry_price FLOAT, stake_amount FLOAT, pnl FLOAT, pnl_pct FLOAT, fee FLOAT, exit_price FLOAT, "
                "exit_reason VARCHAR(50), exit_time DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(text("CREATE TABLE orders (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO trades (id, status, pnl) VALUES (1, 'CLOSED', 12.5)"))
        connection.execute(text("INSERT INTO trades (id, status, quantity, pnl) VALUES (2, 'OPEN', 0.006, 0.032)"))
        connection.execute(text("INSERT INTO trades (id, status, quantity) VALUES (3, 'OPEN', 0)"))

    _migrate_live_accounting_schema(SimpleNamespace(engine=engine))

    with engine.connect() as connection:
        trade_columns = {item["name"] for item in inspect(connection).get_columns("trades")}
        order_columns = {item["name"] for item in inspect(connection).get_columns("orders")}
        assert {"gross_pnl", "total_fees_usdt", "entry_fee_usdt", "entry_fee_allocated_usdt", "exit_fee_usdt", "fee_accounting_complete"} <= trade_columns
        assert {"fee_currency", "fee_usdt", "fee_source", "fee_conversion_price", "base_fee_quantity"} <= order_columns
        assert connection.execute(text("SELECT pnl FROM trades WHERE id=1")).scalar_one() == 12.5

    sessions = sessionmaker(bind=engine, expire_on_commit=False)

    @contextmanager
    def test_session():
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(live_service_module, "get_session", test_session)
    service = LiveTradingService(base_dir=Path("."))
    service._db_update_trade_after_buy(
        trade_id=3,
        fill_price=100.0,
        fill_qty=0.01,
        stake_amount=1.0,
        entry_fee_usdt=None,
        fee_accounting_complete=False,
    )
    service._db_record_live_exit(
        trade_id=2,
        remaining_quantity=0.0,
        exit_price=120.0,
        gross_pnl=0.16,
        pnl=0.14,
        pnl_pct=14.0,
        total_fees_usdt=0.02,
        entry_fee_allocated_usdt=0.01,
        exit_fee_usdt=0.01,
        fee_accounting_complete=True,
        exit_reason="strategy_exit",
        exit_time=datetime.now(tz=timezone.utc),
        fully_closed=True,
    )

    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT status, gross_pnl, total_fees_usdt, fee, pnl, fee_accounting_complete FROM trades WHERE id=2")
        ).one()
        assert row.status == "CLOSED"
        assert row.gross_pnl == 0.16
        assert row.total_fees_usdt == 0.02
        assert row.fee == 0.02
        assert row.pnl == 0.14
        assert row.fee_accounting_complete in (True, 1)
        unknown_fee = connection.execute(
            text("SELECT total_fees_usdt, pnl, fee_accounting_complete FROM trades WHERE id=3")
        ).one()
        assert unknown_fee.total_fees_usdt is None
        assert unknown_fee.pnl is None
        assert unknown_fee.fee_accounting_complete in (False, 0)
        assert connection.execute(text("SELECT pnl FROM trades WHERE id=1")).scalar_one() == 12.5

    engine.dispose()
