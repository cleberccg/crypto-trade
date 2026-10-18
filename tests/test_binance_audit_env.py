from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import audit_binance_spot_readonly as audit


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        pytest.fail("Network forbidden in audit tests")
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def env_file(tmp_path, **overrides):
    values = dict(MODE="live", BINANCE_TESTNET="false",
                  DATABASE_URL="mysql+pymysql://writer:fake-password@localhost/crypto_bot",
                  BINANCE_API_KEY="fixture-key", BINANCE_API_SECRET="fixture-secret")
    values.update(overrides)
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()), encoding="utf-8")
    return path


def test_explicit_env_ignores_terminal_and_does_not_mutate_it(monkeypatch, tmp_path):
    for key, value in dict(MODE="paper", BINANCE_TESTNET="true", DATABASE_URL="sqlite://",
                           BINANCE_API_KEY="terminal-key", BINANCE_API_SECRET="terminal-secret").items():
        monkeypatch.setenv(key, value)
    before = dict(os.environ)
    config = audit._load_project_env(env_file(tmp_path))
    assert config["MODE"] == "live"
    assert config["BINANCE_TESTNET"] == "false"
    assert config["DATABASE_URL"].startswith("mysql+pymysql://writer:")
    assert config["BINANCE_API_KEY"] == "fixture-key"
    assert dict(os.environ) == before


@pytest.mark.parametrize("overrides", [
    {"MODE": "paper"}, {"BINANCE_TESTNET": "true"},
    {"DATABASE_URL": "sqlite://"}, {"DATABASE_URL": "mysql://localhost/wrong"},
    {"BINANCE_API_SECRET": ""}, {"BINANCE_API_SECRET": "${TERMINAL_SECRET}"},
    {"EXCHANGE": "other"},
])
def test_invalid_env_stops_before_database_and_binance(monkeypatch, tmp_path, capsys, overrides):
    path = env_file(tmp_path, **overrides)
    monkeypatch.setattr(audit, "_read_mysql_snapshot", lambda *a: pytest.fail("database contacted"))
    monkeypatch.setattr(audit, "BinanceSpotReadOnlyAuditClient", lambda *a: pytest.fail("client created"))
    result = audit.main(["--env-file", str(path), "--since", "2026-10-17T00:00:00Z",
                         "--known-external-bnb", "0.00030331"])
    output = capsys.readouterr().out
    assert result == 2
    assert json.loads(output)["decision"] == "BLOQUEADO"
    assert "fixture-secret" not in output and "fake-password" not in output


def test_cli_passes_only_file_credentials_and_warns_about_permissions(monkeypatch, tmp_path, capsys):
    path = env_file(tmp_path)
    state = tmp_path / "state.json"
    state.write_text("[]")
    calls = []
    class Client:
        def __init__(self, key, secret):
            assert (key, secret) == ("fixture-key", "fixture-secret")
            calls.append("client")
        def close(self):
            calls.append("closed")
    def snapshot(url, since):
        assert url.startswith("mysql+pymysql://writer:")
        calls.append("mysql")
        return {"fixture": True}
    def reconcile(client, **kwargs):
        assert calls == ["client", "mysql"]
        assert kwargs["mysql_snapshot"] == {"fixture": True}
        return {"decision": "LIBERADO PARA REIN\u00cdCIO"}
    monkeypatch.setattr(audit, "BinanceSpotReadOnlyAuditClient", Client)
    monkeypatch.setattr(audit, "_read_mysql_snapshot", snapshot)
    monkeypatch.setattr(audit, "reconcile", reconcile)
    result = audit.main(["--env-file", str(path), "--state", str(state),
                         "--since", "2026-10-17T00:00:00Z", "--known-external-bnb", "0.00030331"])
    captured = capsys.readouterr()
    assert result == 0 and calls[-1] == "closed"
    assert "de credencial" in captured.err
    assert json.loads(captured.out)["configuration_source"] == "explicit_env_file"
    for secret in ("fixture-key", "fixture-secret", "fake-password"):
        assert secret not in captured.out + captured.err


def test_sql_guard_rejects_writes():
    for sql in ("INSERT INTO trades VALUES (1)", "UPDATE trades SET pnl=0", "DELETE FROM orders", "CALL procedure()"):
        with pytest.raises(RuntimeError, match="SQL .* permitido"):
            audit._guard_readonly_sql(None, None, sql, None, None, False)
    for sql in ("SELECT * FROM trades", "SET SESSION TRANSACTION READ ONLY"):
        audit._guard_readonly_sql(None, None, sql, None, None, False)


def test_standalone_audit_import_does_not_load_trading_or_settings():
    root = Path(audit.__file__).resolve().parent
    code = "import socket; socket.socket.connect=lambda *a: (_ for _ in ()).throw(AssertionError('network')); import audit_binance_spot_readonly; import sys; assert 'config.settings' not in sys.modules; assert 'exchange.binance_client' not in sys.modules; assert 'execution.live_trading_service' not in sys.modules"
    result = subprocess.run([sys.executable, "-B", "-c", code], cwd=root,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def test_mysql_snapshot_uses_readonly_transaction_and_only_selects(monkeypatch):
    statements = []
    class Result:
        def __init__(self, sql): self.sql = sql
        def scalar_one(self):
            from datetime import datetime
            return datetime(2026, 10, 17) if "UTC_TIMESTAMP" in self.sql else 0
        def mappings(self): return self
        def one_or_none(self): return None
        def all(self): return []
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def exec_driver_sql(self, sql): statements.append(sql)
        def commit(self): statements.append("COMMIT_SETUP")
        def begin(self):
            statements.append("BEGIN_READONLY")
            return self
        def execute(self, sql, *args):
            statement = str(sql)
            assert statement.startswith("SELECT ")
            assert "BEGIN_READONLY" in statements
            statements.append(statement)
            return Result(statement)
    class Engine:
        def connect(self): return Connection()
        def dispose(self): statements.append("DISPOSE")
    def engine(url, **kwargs):
        assert kwargs["echo"] is False and kwargs["hide_parameters"] is True
        return Engine()
    monkeypatch.setattr(audit, "create_engine", engine)
    monkeypatch.setattr(audit.event, "listen", lambda engine, name, callback:
                        None if callback is audit._guard_readonly_sql else pytest.fail("missing SQL guard"))
    from datetime import datetime, timezone
    snapshot = audit._read_mysql_snapshot("mysql+pymysql://writer@localhost/crypto_bot",
                                          datetime(2026, 10, 17, tzinfo=timezone.utc))
    assert statements[:4] == ["SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ",
                              "SET SESSION TRANSACTION READ ONLY", "COMMIT_SETUP", "BEGIN_READONLY"]
    assert snapshot["nonterminal_local_orders"] == 0
    assert statements[-1] == "DISPOSE"
