from contextlib import contextmanager

import pandas as pd
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

import repair_market_data as repair


def candles():
    index = pd.date_range('2020-01-01', periods=3, freq='4h', tz='UTC')
    return pd.DataFrame([[10., 12., 9., 11., 100.]] * 3, index=index, columns=repair.COLUMNS)


def test_repair_only_updates_divergent_existing_rows(monkeypatch):
    reference = candles()
    stored = reference.iloc[:2].copy()
    stored.iloc[0, 3] = 8.
    opened = pd.Timestamp.now(tz='UTC').floor('4h')
    stored.loc[opened] = [10., 12., 9., 11., 100.]
    engine = create_engine('sqlite://')
    with engine.begin() as connection:
        connection.execute(text('CREATE TABLE candles (symbol TEXT, timeframe TEXT, open_time DATETIME, open REAL, high REAL, low REAL, close REAL, volume REAL, UNIQUE(symbol,timeframe,open_time))'))
        for ts, row in stored.iterrows():
            connection.execute(text('INSERT INTO candles VALUES (:s,:t,:ot,:o,:h,:l,:c,:v)'), dict(s='BNB/USDT',t='4h',ot=repair._naive(ts),o=row.open,h=row.high,l=row.low,c=row.close,v=row.volume))

    @contextmanager
    def session():
        with Session(engine) as current:
            with current.begin():
                yield current

    monkeypatch.setattr(repair, 'get_session', session)
    result = repair.compare(stored, reference, '4h')
    assert len(result['divergent']) == 1
    assert len(result['missing_closed']) == 1
    assert len(result['open_in_db']) == 1
    repair.apply_repair('BNB/USDT', '4h', result, reference)
    after = repair.load_db('BNB/USDT','4h',reference.index[0],None)
    assert len(after) == len(stored)
    assert after.loc[reference.index[0], 'close'] == 11.
    pd.testing.assert_series_equal(after.loc[reference.index[1]], stored.loc[reference.index[1]])
    assert opened in after.index
    assert reference.index[2] not in after.index
    assert repair.compare(after, reference, '4h')['divergent'].empty


def test_open_reference_is_never_repaired():
    frame = candles()
    frame.index = pd.date_range(pd.Timestamp.now(tz='UTC').floor('4h'), periods=3, freq='4h')
    altered = frame.copy()
    altered['close'] = 1.
    assert repair.compare(altered, frame, '4h')['divergent'].empty


def test_duplicate_timestamp_refuses_repair():
    frame = candles()
    with pytest.raises(ValueError, match='Duplicate'):
        repair.compare(pd.concat([frame, frame.iloc[:1]]), frame, '4h')


def test_invalid_reference_refuses_repair():
    reference = candles()
    reference.iloc[0, 0] = float('nan')
    with pytest.raises(ValueError, match='Invalid Binance'):
        repair.compare(candles(), reference, '4h')


def test_default_mode_never_writes(monkeypatch, capsys):
    frame = candles()
    altered = frame.copy()
    altered.iloc[0, 3] = 1.
    monkeypatch.setattr('sys.argv', ['repair_market_data.py','--symbol','BNB/USDT','--timeframe','4h','--start','2020-01-01'])
    monkeypatch.setattr(repair, 'fetch_binance_closed', lambda *args: frame)
    monkeypatch.setattr(repair, 'load_db', lambda *args: altered)
    monkeypatch.setattr(repair, 'apply_repair', lambda *args: pytest.fail('dry-run attempted a write'))
    assert repair.main() == 0
    assert 'MODE=CHECK' in capsys.readouterr().out
