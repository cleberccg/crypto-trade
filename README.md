# Crypto Trading Bot

Professional modular cryptocurrency trading bot in Python 3.12+.

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12+ |
| Exchange | ccxt + python-binance |
| Indicators | Custom (pandas/numpy) + ta |
| Database | SQLite (dev) / PostgreSQL (prod) |
| ORM | SQLAlchemy 2.0 |
| Testing | pytest + pytest-cov |
| Automation | n8n |

---

## Quick Start

### 1. Create virtual environment and install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate        # Linux/macOS

pip install -r requirements.txt
```

### 2. Configure environment

```bash
copy .env.example .env           # Windows
cp .env.example .env             # Linux/macOS
# Edit .env with your Binance testnet API keys
```

### 3. Run tests

```bash
pytest
```

### 4. Download historical data

```bash
python main.py download --symbol BTC/USDT --timeframe 1h --start 2023-01-01
```

### 5. Run backtest

```bash
python main.py backtest --symbol BTC/USDT --timeframe 1h --start 2023-01-01 --capital 10000
```

### 6. Run paper trading

```bash
python main.py paper --symbol BTC/USDT --timeframe 1h --start 2023-01-01 --capital 10000
```

### 7. Run Execution Manager RC1 real validation

```bash
python main.py execution-manager-rc1
```

Generated artifacts are written under `optimization/results/`:

- `validation_execution_500.txt`
- `failure_recovery_report.txt`
- `execution_validation_report.txt`
- `execution_validation_report.json`
- `execution_validation_report.html`
- `execution_validation_report.pdf`
- `release_candidate_rc1.json`

---

## Project Structure

```
crypto/
├── config/             # Settings loaded from .env
├── database/           # SQLAlchemy models, connection, repositories
├── exchange/           # Exchange adapters (Binance via ccxt)
├── indicators/         # EMA, RSI, ATR, MACD, Bollinger Bands
├── strategies/         # BaseStrategy ABC + TrendV1 implementation
├── risk/               # Position sizing, risk validation, trailing stop
├── backtesting/        # Event-driven backtest engine + metrics + reporter
├── paper_trading/      # PaperBroker + PaperTrader (no real orders)
├── execution/          # Live order executor (guarded by PAPER_TRADING flag)
├── utils/              # Logger factory, helpers, validators
├── tests/              # pytest unit + integration tests
├── docs/               # task_details.txt + checklist.md
├── logs/               # Rotating log files (auto-created)
├── data/               # SQLite database (auto-created)
└── main.py             # CLI entry point (thin orchestration only)
```

---

## Adding a New Strategy

1. Create `strategies/my_strategy.py` extending `BaseStrategy`.
2. Implement `initialize()`, `calculate()`, `entry_signal()`, `exit_signal()`, `score()`.
3. Add unit tests in `tests/test_strategies.py`.
4. Run backtest to validate before paper trading.
5. Update `docs/checklist.md` and `docs/task_details.txt`.

---

## Safety Guarantees

- **No real orders without explicit opt-in**: `PAPER_TRADING=false` in `.env`.
- **No secrets in code**: all credentials via `.env`.
- **Risk per trade capped**: configured via `MAX_RISK_PER_TRADE_PCT`.
- **Never 100% of balance**: `STAKE_AMOUNT_PCT` enforces maximum position size.
- **Risk/Reward filter**: trades rejected if below `RISK_REWARD_RATIO`.

---

## Documentation

- [docs/checklist.md](docs/checklist.md) — Project progress checklist
- [docs/task_details.txt](docs/task_details.txt) — Technical diary
