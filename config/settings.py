"""
Application settings loaded exclusively from environment variables.

Design decision: a single Settings class centralizes all operational
configuration and validation. The rest of the project consumes the exported
`settings` instance instead of reading .env directly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine.url import make_url

from utils.validators import validate_timeframe

# A raiz do projeto fica um nivel acima deste arquivo
BASE_DIR: Path = Path(__file__).parent.parent

# Carrega o .env da raiz do projeto (ignorado silenciosamente se nao existir)
load_dotenv(BASE_DIR / ".env")


def _as_bool(value: str | None, default: bool = False) -> bool:
    """Convert common truthy/falsey string values to bool."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: str | None, default: float) -> float:
    """Safe float parsing with default fallback."""
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _percent_to_fraction(raw: str | None, default: float) -> float:
    """Accept both 0.02 and 2 styles; return fraction."""
    value = _as_float(raw, default)
    if value > 1:
        return value / 100.0
    return value


def _normalize_symbol(symbol: str) -> str:
    """Normalize BTCUSDT to BTC/USDT and keep BTC/USDT unchanged."""
    symbol = symbol.strip().upper()
    if "/" in symbol:
        return symbol
    for quote in ("USDT", "USDC", "BUSD", "BTC", "ETH", "BNB", "BRL", "EUR", "TRY"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            base = symbol[: -len(quote)]
            return f"{base}/{quote}"
    return symbol


@dataclass(frozen=True)
class BinanceConfig:
    """Binance API connection settings."""

    api_key: str
    api_secret: str
    testnet: bool
    base_url: str


@dataclass(frozen=True)
class DatabaseConfig:
    """Database connection settings."""

    type: str
    url: str
    echo: bool


@dataclass(frozen=True)
class TradingConfig:
    """Trading parameters used by runtime modules."""

    exchange: str
    mode: str
    default_symbol: str
    default_timeframe: str
    strategy: str
    max_open_trades: int
    max_open_positions: int
    min_free_usdt_reserve: float
    min_notional_buffer_pct: float
    min_operational_stake_usdt: float
    max_operational_stake_usdt: float
    min_trades_per_symbol: int
    asset_ranking_refresh_cycles: int
    stake_currency: str
    stake_amount_pct: float


@dataclass(frozen=True)
class OptimizerConfig:
    """Strategy optimizer runtime settings."""

    enabled: bool
    workers: int
    max_combinations: int
    checkpoint_interval: int


@dataclass(frozen=True)
class ValidationConfig:
    """Post-optimization statistical validation thresholds."""

    min_trades: int
    min_profit_factor: float
    max_drawdown_pct: float
    min_win_rate_pct: float
    min_expectancy: float
    min_sharpe: float


@dataclass(frozen=True)
class RiskConfig:
    """Risk management parameters."""

    risk_percent: float
    max_daily_loss_percent: float
    default_stop_loss_pct: float
    default_take_profit_pct: float
    default_trailing_stop_pct: float
    trailing_stop_enabled: bool
    max_risk_per_trade_pct: float
    risk_reward_ratio: float
    atr_stop_multiplier: float


@dataclass(frozen=True)
class LoggingConfig:
    """Logging configuration."""

    level: str
    log_dir: Path
    log_to_file: bool


@dataclass(frozen=True)
class BacktestConfig:
    """Backtesting runtime defaults."""

    initial_capital: float
    commission: float
    slippage: float


@dataclass(frozen=True)
class N8nConfig:
    """n8n integration settings."""

    webhook_url: str
    api_key: str


@dataclass(frozen=True)
class TelegramConfig:
    """Telegram notification and command settings."""

    enabled: bool
    bot_token: str
    chat_id: str
    admin_chat_id: str
    send_progress: bool
    progress_interval_minutes: int
    send_errors: bool
    send_success: bool
    send_reports: bool


class Settings:
    """Centralized settings object for the whole application."""

    def __init__(self) -> None:
        self.app_name = os.getenv("APP_NAME", "CryptoBot")
        self.app_env = os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "development"))
        self.app_debug = _as_bool(os.getenv("APP_DEBUG"), default=False)
        self.small_account_mode = _as_bool(os.getenv("SMALL_ACCOUNT_MODE"), default=False)

        default_db_url = f"sqlite:///{BASE_DIR / 'data' / 'crypto_bot.db'}"
        database_url = os.getenv("DATABASE_URL") or default_db_url
        database_backend = make_url(database_url).get_backend_name().strip().lower()
        self.database = DatabaseConfig(
            type=database_backend,
            url=database_url,
            echo=_as_bool(os.getenv("DATABASE_ECHO"), default=False),
        )

        raw_symbol = os.getenv("SYMBOL", os.getenv("DEFAULT_SYMBOL", "BTCUSDT"))
        symbol = _normalize_symbol(raw_symbol)
        timeframe = os.getenv("TIMEFRAME", os.getenv("DEFAULT_TIMEFRAME", "5m"))

        self.trading = TradingConfig(
            exchange=os.getenv("EXCHANGE", "binance").strip().lower(),
            mode=os.getenv("MODE", "paper").strip().lower(),
            default_symbol=symbol,
            default_timeframe=timeframe,
            strategy=os.getenv("STRATEGY", "TradeOutcomeNextGenV1").strip(),
            max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "1")),
            max_open_positions=int(os.getenv("MAX_OPEN_POSITIONS", "3")),
            min_free_usdt_reserve=_as_float(os.getenv("MIN_FREE_USDT_RESERVE"), 4.0),
            min_notional_buffer_pct=_as_float(os.getenv("MIN_NOTIONAL_BUFFER_PCT"), 0.40),
            min_operational_stake_usdt=_as_float(os.getenv("MIN_OPERATIONAL_STAKE_USDT"), 7.0),
            max_operational_stake_usdt=_as_float(os.getenv("MAX_OPERATIONAL_STAKE_USDT"), 10.0),
            min_trades_per_symbol=max(1, int(os.getenv("MIN_TRADES_PER_SYMBOL", "30"))),
            asset_ranking_refresh_cycles=max(1, int(os.getenv("ASSET_RANKING_REFRESH_CYCLES", "1"))),
            stake_currency=os.getenv("STAKE_CURRENCY", "USDT").strip().upper(),
            stake_amount_pct=_percent_to_fraction(os.getenv("STAKE_AMOUNT_PCT"), 0.02),
        )

        self.optimizer = OptimizerConfig(
            enabled=_as_bool(os.getenv("OPTIMIZER_ENABLED"), default=False),
            workers=int(os.getenv("OPTIMIZER_WORKERS", "4")),
            max_combinations=int(os.getenv("OPTIMIZER_MAX_COMBINATIONS", "5000")),
            checkpoint_interval=int(os.getenv("CHECKPOINT_INTERVAL", "50")),
        )

        self.validation = ValidationConfig(
            min_trades=int(os.getenv("MIN_TRADES", "100")),
            min_profit_factor=_as_float(os.getenv("MIN_PROFIT_FACTOR"), 1.40),
            max_drawdown_pct=_as_float(os.getenv("MAX_DRAWDOWN"), 15.0),
            min_win_rate_pct=_as_float(os.getenv("MIN_WIN_RATE"), 35.0),
            min_expectancy=_as_float(os.getenv("MIN_EXPECTANCY"), 0.0),
            min_sharpe=_as_float(os.getenv("MIN_SHARPE"), 1.0),
        )

        risk_percent = _percent_to_fraction(
            os.getenv("RISK_PERCENT", os.getenv("MAX_RISK_PER_TRADE_PCT")), 0.01
        )
        stop_loss = _percent_to_fraction(
            os.getenv("STOP_LOSS_PERCENT", os.getenv("DEFAULT_STOP_LOSS_PCT")), 0.02
        )
        take_profit = _percent_to_fraction(
            os.getenv("TAKE_PROFIT_PERCENT", os.getenv("DEFAULT_TAKE_PROFIT_PCT")), 0.04
        )

        self.risk = RiskConfig(
            risk_percent=risk_percent,
            max_daily_loss_percent=_percent_to_fraction(os.getenv("MAX_DAILY_LOSS"), 0.03),
            default_stop_loss_pct=stop_loss,
            default_take_profit_pct=take_profit,
            default_trailing_stop_pct=_percent_to_fraction(
                os.getenv("DEFAULT_TRAILING_STOP_PCT"), 0.015
            ),
            trailing_stop_enabled=_as_bool(os.getenv("TRAILING_STOP"), default=False),
            max_risk_per_trade_pct=_percent_to_fraction(
                os.getenv("MAX_RISK_PER_TRADE_PCT", os.getenv("RISK_PERCENT")), 0.01
            ),
            risk_reward_ratio=_as_float(os.getenv("RISK_REWARD_RATIO"), 2.0),
            atr_stop_multiplier=_as_float(os.getenv("ATR_STOP_MULTIPLIER"), 2.0),
        )

        self.logging = LoggingConfig(
            level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
            log_dir=BASE_DIR / "logs",
            log_to_file=_as_bool(os.getenv("LOG_TO_FILE"), default=True),
        )

        self.backtest = BacktestConfig(
            initial_capital=_as_float(os.getenv("INITIAL_CAPITAL"), 10_000.0),
            commission=_as_float(os.getenv("COMMISSION"), 0.001),
            slippage=_as_float(os.getenv("SLIPPAGE"), 0.0005),
        )

        self.binance = BinanceConfig(
            api_key=os.getenv("BINANCE_API_KEY", "").strip(),
            api_secret=os.getenv("BINANCE_API_SECRET", "").strip(),
            testnet=_as_bool(os.getenv("BINANCE_TESTNET"), default=True),
            base_url=os.getenv("BINANCE_BASE_URL", "https://testnet.binance.vision").strip(),
        )

        self.n8n = N8nConfig(
            webhook_url=os.getenv("N8N_WEBHOOK_URL", "").strip(),
            api_key=os.getenv("N8N_API_KEY", "").strip(),
        )

        self.telegram = TelegramConfig(
            enabled=_as_bool(os.getenv("TELEGRAM_ENABLED"), default=False),
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            admin_chat_id=os.getenv("TELEGRAM_ADMIN_CHAT_ID", "").strip(),
            send_progress=_as_bool(os.getenv("TELEGRAM_SEND_PROGRESS"), default=True),
            progress_interval_minutes=max(1, int(os.getenv("TELEGRAM_PROGRESS_INTERVAL_MINUTES", "30"))),
            send_errors=_as_bool(os.getenv("TELEGRAM_SEND_ERRORS"), default=True),
            send_success=_as_bool(os.getenv("TELEGRAM_SEND_SUCCESS"), default=True),
            send_reports=_as_bool(os.getenv("TELEGRAM_SEND_REPORTS"), default=True),
        )

    # ------------------------------------------------------------------
    # Propriedades de compatibilidade usadas pelos modulos existentes
    # ------------------------------------------------------------------
    @property
    def environment(self) -> str:
        return self.app_env

    @property
    def paper_trading(self) -> bool:
        return self.trading.mode == "paper"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def is_paper_trading(self) -> bool:
        return self.paper_trading

    def validate(self) -> None:
        """Validate mandatory startup configuration and fail fast."""
        errors: list[str] = []

        if not self.binance.api_key:
            errors.append("Missing required configuration: BINANCE_API_KEY")
        if not self.binance.api_secret:
            errors.append("Missing required configuration: BINANCE_API_SECRET")

        if self.trading.mode not in {"paper", "live"}:
            errors.append("Invalid MODE. Expected 'paper' or 'live'.")

        if "/" not in self.trading.default_symbol:
            errors.append(
                f"Invalid SYMBOL '{self.trading.default_symbol}'. "
                "Expected format like BTCUSDT or BTC/USDT."
            )

        try:
            validate_timeframe(self.trading.default_timeframe)
        except ValueError as exc:
            errors.append(str(exc))

        if not self.database.url:
            errors.append("Missing required configuration: DATABASE_URL")

        if errors:
            raise ValueError("Configuration validation failed:\n- " + "\n- ".join(errors))

    def validate_database_access(self) -> None:
        """Validate that the configured database is reachable."""
        engine = create_engine(self.database.url, future=True)
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            engine.dispose()

    def startup_summary(self) -> str:
        """Return a startup banner with key runtime settings."""
        return "\n".join(
            [
                "========================================",
                "Crypto Trading Bot",
                f"Ambiente: {self.app_env.capitalize()}",
                f"Exchange: {self.trading.exchange.capitalize()}",
                f"Modo: {self.trading.mode.upper()}",
                f"Par: {self.trading.default_symbol.replace('/', '')}",
                f"Timeframe: {self.trading.default_timeframe}",
                f"Estrategia: {self.trading.strategy}",
                f"Banco: {self.database.type.upper()}",
                "========================================",
            ]
        )


# Instancia unica de configuracoes da aplicacao
settings = Settings()
