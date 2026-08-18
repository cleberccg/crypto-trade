"""
Crypto Trading Bot - Entry Point.

Design decision: main.py is intentionally thin.  It wires together the
application components and delegates all logic to the appropriate modules.
No business logic lives here.

Usage examples
--------------
Run a backtest::

    python main.py backtest --symbol BTC/USDT --timeframe 1h

Run paper trading on historical data::

    python main.py paper --symbol BTC/USDT --timeframe 1h

Download historical data::

    python main.py download --symbol BTC/USDT --timeframe 1h --start 2023-01-01

Expand the market data base::

    python main.py market-data-expansion --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine.url import make_url

from config.settings import settings
from core.events import EventBus
from core.events.listeners import HistoryListener, LogListener, MetricsListener
from database.bootstrap import bootstrap_database
from database.connection import DatabaseConnection
from database.connection import get_session
from database.history_service import HistoryPersistenceService
from database.models import Candle
from logging_service.logger_manager import initialize_logging_service, shutdown_logging_service
from strategies.registry import list_registered_strategies
from utils.logger import get_logger
from utils.validators import validate_symbol, validate_timeframe

logger = get_logger("main")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Crypto Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # --- download ---
    dl = subparsers.add_parser("download", help="Download historical OHLCV data.")
    dl.add_argument("--symbol", default=settings.trading.default_symbol)
    dl.add_argument("--timeframe", default=settings.trading.default_timeframe)
    dl.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    dl.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: now)")

    # --- market-data-expansion ---
    mde = subparsers.add_parser(
        "market-data-expansion",
        help="Audit and expand the consolidated candle base, then generate inventory and quality reports.",
    )
    mde.add_argument(
        "--mode",
        choices=["incremental", "backfill", "full"],
        default="full",
        help="Run incremental update only, retroactive backfill only, or full cycle (default: full)",
    )
    mde.add_argument("--history-days", type=int, default=365, help="Historical window reference used by coverage metrics")
    mde.add_argument("--dry-run", action="store_true", help="Audit and report only; do not download candles")
    mde.add_argument(
        "--auto-pipeline",
        action="store_true",
        help="If gate passes, execute automatically: expansion -> quantitative lab -> H1 audit -> report.",
    )
    mde.add_argument(
        "--continuous",
        action="store_true",
        help="Run in continuous platform mode (bounded by --continuous-max-cycles).",
    )
    mde.add_argument(
        "--continuous-max-cycles",
        type=int,
        default=1,
        help="Maximum cycles in continuous mode before returning control (default: 1)",
    )

    # --- market-data-daemon ---
    mdd = subparsers.add_parser(
        "market-data-daemon",
        help="Run continuous market data ingestion daemon that keeps candles updated for paper-live.",
    )
    mdd.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    mdd.add_argument("--timeframes", default="5m,15m,1h")
    mdd.add_argument("--polling-interval-seconds", type=float, default=30.0)
    mdd.add_argument("--context-delay-seconds", type=float, default=0.2)
    mdd.add_argument("--batch-size", type=int, default=1000)
    mdd.add_argument("--retry-count", type=int, default=5)
    mdd.add_argument("--retry-delay-seconds", type=float, default=2.0)
    mdd.add_argument("--bootstrap-days", type=int, default=7)
    mdd.add_argument("--recent-gap-bars", type=int, default=2000)
    mdd.add_argument("--report-every-cycles", type=int, default=1)
    mdd.add_argument("--max-cycles", type=int, default=0, help="0 means run continuously until interrupted")
    mdd.add_argument("--output-prefix", default="market_data_daemon")

    # --- robustness-validation ---
    rv = subparsers.add_parser(
        "robustness-validation",
        help="Run scientific robustness validation with train/validation/test isolation and permanent artifacts.",
    )
    rv.add_argument("--phase6-csv", default="optimization/results/fase6_discovery2_clusters.csv")
    rv.add_argument("--candidate-csv", default="optimization/results/fase7_scientific_candidates.csv")
    rv.add_argument(
        "--events-glob",
        default="optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv",
        help="Glob for event CSV files used in scientific robustness validation",
    )
    rv.add_argument("--train-ratio", type=float, default=0.60)
    rv.add_argument("--validation-ratio", type=float, default=0.20)
    rv.add_argument("--min-support", type=int, default=25)
    rv.add_argument("--max-rule-coverage", type=float, default=0.95)
    rv.add_argument("--min-discrimination-gap", type=float, default=0.04)
    rv.add_argument("--min-scientific-score", type=float, default=75.0)
    rv.add_argument("--min-generalization-score", type=float, default=0.60)
    rv.add_argument("--min-robustness-score", type=float, default=0.55)
    rv.add_argument("--min-files", type=int, default=75)
    rv.add_argument("--min-events", type=int, default=17_000_000)
    rv.add_argument("--min-assets", type=int, default=10)
    rv.add_argument("--min-timeframes", type=int, default=4)
    rv.add_argument("--min-context-events", type=int, default=100)
    rv.add_argument("--min-coverage-days", type=int, default=1000)
    rv.add_argument("--min-contexts", type=int, default=2)
    rv.add_argument("--output-prefix", default="scientific_robustness_validation")
    rv.add_argument("--no-db", action="store_true", help="Do not persist scientific robustness run in database")

    # --- trade-outcome-learning ---
    tol = subparsers.add_parser(
        "trade-outcome-learning",
        help="Run supervised trade outcome discovery with explainability, robustness and Trade Outcome Score.",
    )
    tol.add_argument(
        "--events-glob",
        default="optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv",
        help="Glob for event CSV files used as supervised learning dataset",
    )
    tol.add_argument(
        "--targets",
        default="winner,return_above,positive_expectancy,risk_adjusted",
        help="Comma-separated target names: winner,loser,return_above,return_below,positive_expectancy,risk_adjusted",
    )
    tol.add_argument("--return-above-threshold", type=float, default=0.01)
    tol.add_argument("--return-below-threshold", type=float, default=-0.01)
    tol.add_argument("--risk-adjusted-threshold", type=float, default=0.8)
    tol.add_argument("--train-ratio", type=float, default=0.60)
    tol.add_argument("--validation-ratio", type=float, default=0.20)
    tol.add_argument("--min-support", type=int, default=50)
    tol.add_argument("--max-rule-coverage", type=float, default=0.70)
    tol.add_argument("--min-precision-gain", type=float, default=0.03)
    tol.add_argument("--min-generalization-score", type=float, default=0.55)
    tol.add_argument("--min-robustness-score", type=float, default=0.50)
    tol.add_argument("--max-overfit-gap", type=float, default=0.12)
    tol.add_argument("--trade-outcome-score-threshold", type=float, default=70.0)
    tol.add_argument("--top-k-candidates", type=int, default=25)
    tol.add_argument("--output-prefix", default="trade_outcome_learning")
    tol.add_argument("--no-db", action="store_true", help="Do not persist trade outcome learning run in database")

    # --- phase9-controlled-implementation ---
    p9 = subparsers.add_parser(
        "phase9-controlled-implementation",
        help="Run FASE 9 controlled implementation audit for the approved trade outcome candidate.",
    )
    p9.add_argument(
        "--events-glob",
        default="optimization/results/quantitative_discovery_chunks/fase52_full_ultra_20260629/events/events_*.csv",
        help="Glob for event CSV files used for fidelity and event-level backtest comparison",
    )
    p9.add_argument(
        "--trade-outcome-csv",
        default="",
        help="Optional explicit trade_outcome_learning CSV (default: latest artifact)",
    )
    p9.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    p9.add_argument("--target-name", default="return_above")
    p9.add_argument("--approved-rule", default="distance_to_ema_pct<=0.162026")
    p9.add_argument("--distance-threshold", type=float, default=0.162026)
    p9.add_argument("--fidelity-min-f1", type=float, default=0.95)
    p9.add_argument("--optimizer-max-combinations", type=int, default=60)
    p9.add_argument("--optimizer-workers", type=int, default=4)
    p9.add_argument("--optimizer-capital", type=float, default=10_000.0)
    p9.add_argument("--output-prefix", default="trade_outcome_controlled_implementation")
    p9.add_argument("--skip-optimizer-validation", action="store_true")
    p9.add_argument("--skip-research-labs", action="store_true")
    p9.add_argument("--no-db", action="store_true", help="Do not persist phase 9 run in database")

    # --- execution-framework-optimization ---
    efo = subparsers.add_parser(
        "execution-framework-optimization",
        help="Run FASE 9.0 permanent execution framework optimization, equivalence benchmark, and phase 9 rerun.",
    )
    efo.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    efo.add_argument("--benchmark-symbol", default="BTC/USDT")
    efo.add_argument("--benchmark-timeframe", default="5m")
    efo.add_argument("--benchmark-bars", type=int, default=20000)
    efo.add_argument("--initial-capital", type=float, default=10000.0)
    efo.add_argument("--output-prefix", default="execution_framework_optimization")
    efo.add_argument("--skip-phase9-rerun", action="store_true")
    efo.add_argument("--no-db", action="store_true", help="Do not persist framework optimization run in database")

    # --- Backtest ---
    bt = subparsers.add_parser("backtest", help="Run a strategy backtest.")
    bt.add_argument("--symbol", default=settings.trading.default_symbol)
    bt.add_argument("--timeframe", default=settings.trading.default_timeframe)
    bt.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    bt.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: now)")
    bt.add_argument("--capital", type=float, default=10_000.0)
    bt.add_argument(
        "--diagnostic",
        action="store_true",
        help="Enable detailed diagnostic report generation.",
    )

    # --- paper ---
    paper = subparsers.add_parser("paper", help="Run paper trading on historical data.")
    paper.add_argument("--symbol", default=settings.trading.default_symbol)
    paper.add_argument("--timeframe", default=settings.trading.default_timeframe)
    paper.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    paper.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: now)")
    paper.add_argument("--capital", type=float, default=10_000.0)
    paper.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    paper.add_argument("--strategy-version", default="v1.0")
    paper.add_argument(
        "--no-daily-report",
        action="store_true",
        help="Skip automatic generation of paper trading daily report",
    )

    # --- paper-daily-report ---
    pdr = subparsers.add_parser(
        "paper-daily-report",
        help="Generate a daily operational report from persisted paper trading data.",
    )
    pdr.add_argument("--date", required=True, help="Report date YYYY-MM-DD (UTC)")
    pdr.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    pdr.add_argument("--strategy-version", default=None)
    pdr.add_argument("--output-prefix", default="paper_trading_daily_report")

    # --- paper-live ---
    plive = subparsers.add_parser(
        "paper-live",
        help="Run continuous paper trading operation with resume, versioning, and automatic reports.",
    )
    plive.add_argument("--symbol", default=settings.trading.default_symbol)
    plive.add_argument("--timeframe", default=settings.trading.default_timeframe)
    plive.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    plive.add_argument("--strategy-version", default="v1.0")
    plive.add_argument("--campaign-id", default=None)
    plive.add_argument("--capital", type=float, default=10_000.0)
    plive.add_argument("--poll-seconds", type=float, default=15.0)
    plive.add_argument("--bootstrap-bars", type=int, default=1500)
    plive.add_argument("--bootstrap-replay-bars", type=int, default=350)
    plive.add_argument("--max-cycles", type=int, default=0, help="0 means run continuously until interrupted")
    plive.add_argument("--no-resume", action="store_true")
    plive.add_argument(
        "--min-trades-before-change",
        type=int,
        default=None,
        help="Minimum trades required before switching strategy version. Default: 100 (or 0 for spc-* campaign-id).",
    )
    plive.add_argument("--output-prefix", default="paper_live")

    # --- live ---
    live = subparsers.add_parser(
        "live",
        help="Run official LIVE Binance Spot operation with risk-managed sizing.",
    )
    live.add_argument("--strategy-name", required=True)
    live.add_argument("--strategy-version", required=True)
    live.add_argument("--symbol", required=False)
    live.add_argument(
        "--symbols",
        default="",
        help="Comma-separated symbols for multi-asset live mode, e.g. BTC/USDT,ETH/USDT",
    )
    live.add_argument("--timeframe", required=True)
    live.add_argument("--poll-seconds", type=float, default=15.0)
    live.add_argument("--bootstrap-bars", type=int, default=1500)
    live.add_argument("--bootstrap-replay-bars", type=int, default=350)
    live.add_argument("--max-cycles", type=int, default=0, help="0 means run continuously until interrupted")
    live.add_argument("--output-prefix", default="live")
    live.add_argument("--no-resume", action="store_true")

    # --- paper-live-supervisor ---
    pls = subparsers.add_parser(
        "paper-live-supervisor",
        help="Run resilient supervisor for multi-context PaperLive campaign with auto-restart.",
    )
    pls.add_argument("--strategy-name", default="ClassicDonchianBreakout")
    pls.add_argument("--strategy-version", default="v1.0")
    pls.add_argument("--campaign-id", required=True)
    pls.add_argument("--contexts", default="", help="Optional comma-separated contexts: SYMBOL:TIMEFRAME")
    pls.add_argument("--no-contexts-from-latest-report", action="store_true")
    pls.add_argument("--capital", type=float, default=10_000.0)
    pls.add_argument("--poll-seconds", type=float, default=15.0)
    pls.add_argument("--bootstrap-bars", type=int, default=1500)
    pls.add_argument("--bootstrap-replay-bars", type=int, default=350)
    pls.add_argument("--min-trades-before-change", type=int, default=0)
    pls.add_argument("--supervisor-poll-seconds", type=float, default=10.0)
    pls.add_argument("--stuck-timeout-seconds", type=float, default=600.0)
    pls.add_argument("--startup-grace-seconds", type=float, default=120.0)
    pls.add_argument("--restart-delay-seconds", type=float, default=2.0)
    pls.add_argument("--max-consecutive-restarts", type=int, default=5)
    pls.add_argument("--max-supervision-cycles", type=int, default=0, help="0 means run continuously")
    pls.add_argument("--output-prefix", default="paper_live")

    # --- paper-operational-report ---
    por = subparsers.add_parser(
        "paper-operational-report",
        help="Generate operation/hourly/daily/weekly/monthly reports for a strategy version.",
    )
    por.add_argument("--date", required=True, help="Reference date YYYY-MM-DD (UTC)")
    por.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    por.add_argument("--strategy-version", default="v1.0")
    por.add_argument("--output-prefix", default="paper_operational")

    # --- paper-specialized-validation ---
    psv = subparsers.add_parser(
        "paper-specialized-validation",
        help="Run specialized paper validation restricted to approved operational contexts.",
    )
    psv.add_argument("--strategy-name", default="ClassicDonchianBreakout")
    psv.add_argument("--strategy-version", default="v1.0")
    psv.add_argument("--contexts", default="", help="Comma-separated contexts: SYMBOL:TIMEFRAME")
    psv.add_argument("--edge-matrix-csv", default=None)
    psv.add_argument("--no-contexts-from-matrix", action="store_true")
    psv.add_argument("--context-min-trades", type=int, default=5)
    psv.add_argument("--context-min-profit-factor", type=float, default=1.0)
    psv.add_argument("--context-min-expectancy", type=float, default=0.0)
    psv.add_argument("--run-live", action="store_true")
    psv.add_argument("--max-global-cycles", type=int, default=0, help="0 means continuous")
    psv.add_argument("--poll-seconds", type=float, default=15.0)
    psv.add_argument("--bootstrap-bars", type=int, default=1500)
    psv.add_argument("--bootstrap-replay-bars", type=int, default=350)
    psv.add_argument("--capital", type=float, default=10_000.0)
    psv.add_argument("--min-trades-before-change", type=int, default=100)
    psv.add_argument("--min-validation-days", type=int, default=30)
    psv.add_argument("--min-validation-trades", type=int, default=100)
    psv.add_argument("--min-profit-factor", type=float, default=1.15)
    psv.add_argument("--min-expectancy", type=float, default=0.0)
    psv.add_argument("--min-sharpe", type=float, default=0.0)
    psv.add_argument("--max-drawdown", type=float, default=0.20)
    psv.add_argument("--max-pf-degradation-pct", type=float, default=0.35)
    psv.add_argument("--max-sharpe-degradation-pct", type=float, default=0.40)
    psv.add_argument("--max-expectancy-degradation-pct", type=float, default=0.40)
    psv.add_argument("--max-win-rate-degradation-pct", type=float, default=0.35)
    psv.add_argument("--max-drawdown-worsening-pct", type=float, default=0.60)
    psv.add_argument("--backtest-profit-factor", type=float, default=2.627682)
    psv.add_argument("--backtest-sharpe", type=float, default=0.126418)
    psv.add_argument("--backtest-expectancy", type=float, default=2.276779)
    psv.add_argument("--backtest-drawdown", type=float, default=0.0079)
    psv.add_argument("--backtest-win-rate", type=float, default=None)
    psv.add_argument("--rolling-profit-factor", type=float, default=2.627682)
    psv.add_argument("--rolling-sharpe", type=float, default=0.126418)
    psv.add_argument("--rolling-expectancy", type=float, default=2.276779)
    psv.add_argument("--rolling-drawdown", type=float, default=0.0079)
    psv.add_argument("--rolling-win-rate", type=float, default=None)
    psv.add_argument("--output-prefix", default="paper_specialized_validation")

    # --- edge-drift-monitor ---
    edm = subparsers.add_parser(
        "edge-drift-monitor",
        help="Monitor edge drift for ClassicDonchianBreakout in approved specialized paper contexts.",
    )
    edm.add_argument("--strategy-name", default="ClassicDonchianBreakout")
    edm.add_argument("--strategy-version", default="v1.0")
    edm.add_argument("--campaign-id", default=None)
    edm.add_argument("--specialized-report-file", default=None)
    edm.add_argument("--contexts", default="", help="Optional comma-separated contexts: SYMBOL:TIMEFRAME")
    edm.add_argument("--no-contexts-from-latest-report", action="store_true")
    edm.add_argument("--lookback-days", type=int, default=7)
    edm.add_argument("--history-window", type=int, default=30)
    edm.add_argument("--min-validation-days", type=int, default=30)
    edm.add_argument("--min-validation-trades", type=int, default=100)
    edm.add_argument("--initial-capital", type=float, default=10_000.0)
    edm.add_argument("--attention-health-score", type=float, default=70.0)
    edm.add_argument("--critical-health-score", type=float, default=50.0)
    edm.add_argument("--attention-metric-degradation-pct", type=float, default=0.15)
    edm.add_argument("--critical-metric-degradation-pct", type=float, default=0.30)
    edm.add_argument("--attention-drawdown-worsening-pct", type=float, default=0.10)
    edm.add_argument("--critical-drawdown-worsening-pct", type=float, default=0.25)
    edm.add_argument("--attention-stability-score", type=float, default=70.0)
    edm.add_argument("--critical-stability-score", type=float, default=50.0)
    edm.add_argument("--output-prefix", default="edge_drift_monitor")

    # --- paper-specialized-campaign ---
    psc = subparsers.add_parser(
        "paper-specialized-campaign",
        help="Run the official P1/P2 specialized campaign with Edge Drift monitoring and kill switch.",
    )
    psc.add_argument("--strategy-name", default="ClassicDonchianBreakout")
    psc.add_argument("--strategy-version", default="v1.0")
    psc.add_argument("--campaign-id", default=None)
    psc.add_argument("--specialized-report-file", default=None)
    psc.add_argument("--contexts", default="", help="Optional comma-separated contexts: SYMBOL:TIMEFRAME")
    psc.add_argument("--no-contexts-from-latest-report", action="store_true")
    psc.add_argument("--monitor-lookback-days", type=int, default=21)
    psc.add_argument("--monitor-history-window", type=int, default=30)
    psc.add_argument("--phase1-min-days", type=int, default=7)
    psc.add_argument("--phase1-min-trades", type=int, default=50)
    psc.add_argument("--phase2-min-days", type=int, default=21)
    psc.add_argument("--phase2-min-trades", type=int, default=200)
    psc.add_argument("--min-profit-factor", type=float, default=1.15)
    psc.add_argument("--min-expectancy", type=float, default=0.0)
    psc.add_argument("--min-sharpe", type=float, default=0.0)
    psc.add_argument("--max-drawdown", type=float, default=0.20)
    psc.add_argument("--max-consecutive-critical-alerts", type=int, default=2)
    psc.add_argument("--max-consecutive-non-normal-alerts", type=int, default=3)
    psc.add_argument("--initial-capital", type=float, default=10_000.0)
    psc.add_argument("--poll-seconds", type=float, default=15.0)
    psc.add_argument("--bootstrap-bars", type=int, default=1500)
    psc.add_argument("--bootstrap-replay-bars", type=int, default=350)
    psc.add_argument("--max-cycles-per-context", type=int, default=1)
    psc.add_argument("--min-trades-before-change", type=int, default=0)
    psc.add_argument("--legacy-live-execution", action="store_true")
    psc.add_argument("--ingest-execution-ids", default="", help="Optional CSV list of execution_ids to force-attach into campaign scope")
    psc.add_argument("--output-prefix", default="paper_specialized_campaign")

    # --- paper-campaign-coverage ---
    pcc = subparsers.add_parser(
        "paper-campaign-coverage",
        help="Monitor operational coverage for approved contexts in a specialized paper campaign.",
    )
    pcc.add_argument("--campaign-id", required=True)
    pcc.add_argument("--strategy-name", default="ClassicDonchianBreakout")
    pcc.add_argument("--strategy-version", default="v1.0")
    pcc.add_argument("--stale-minutes", type=int, default=180)
    pcc.add_argument("--min-coverage-percent", type=float, default=90.0)
    pcc.add_argument("--critical-coverage-percent", type=float, default=75.0)
    pcc.add_argument("--output-prefix", default="paper_specialized_campaign_coverage")

    # --- strategy-diagnostics ---
    sdiag = subparsers.add_parser(
        "strategy-diagnostics",
        help="Diagnose why a strategy did or did not operate across the paper/live pipeline.",
    )
    sdiag.add_argument("--strategy-name", default=None)
    sdiag.add_argument("--strategy-version", default=None)
    sdiag.add_argument("--symbol", default=None)
    sdiag.add_argument("--timeframe", default=None)
    sdiag.add_argument("--execution-id", default=None)
    sdiag.add_argument("--window-hours", type=int, default=24)
    sdiag.add_argument("--window-days", type=int, default=7)
    sdiag.add_argument("--output-prefix", default="strategy_diagnostics")

    # --- edge-discovery-lab ---
    edl = subparsers.add_parser(
        "edge-discovery-lab",
        help="FASE 17: evaluate PAPER_CANDIDATE edge across assets, timeframes, and market regimes.",
    )
    edl.add_argument("--report-file", default=None)
    edl.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    edl.add_argument("--timeframes", default="5m,15m,1h")
    edl.add_argument("--window-days", type=int, default=120)
    edl.add_argument("--capital", type=float, default=10_000.0)
    edl.add_argument("--max-bars", type=int, default=3500)
    edl.add_argument("--min-trades-per-context", type=int, default=5)
    edl.add_argument("--include-all-candidates", action="store_true")
    edl.add_argument("--limit-candidates", type=int, default=0)
    edl.add_argument("--output-prefix", default="edge_discovery_lab")

    # --- edge-extraction-lab ---
    eel = subparsers.add_parser(
        "edge-extraction-lab",
        help="Edge Extraction Lab: reverse engineer winning/losing trades and derive statistically justified filters.",
    )
    eel.add_argument("--report-file", default=None)
    eel.add_argument(
        "--prioritized-strategies",
        default="Ichimoku Kumo Breakout,ClassicDonchianBreakout,ClassicATRBreakout",
        help="Comma-separated prioritized strategies for edge extraction",
    )
    eel.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    eel.add_argument("--timeframes", default="5m,15m,1h")
    eel.add_argument("--window-days", type=int, default=180)
    eel.add_argument("--capital", type=float, default=10_000.0)
    eel.add_argument("--max-bars", type=int, default=4500)
    eel.add_argument("--min-trades-per-filter", type=int, default=20)
    eel.add_argument("--top-filters", type=int, default=6)
    eel.add_argument("--max-candidate-filters", type=int, default=30)
    eel.add_argument("--output-prefix", default="edge_extraction_lab")

    # --- edge-external-validation-lab ---
    eev = subparsers.add_parser(
        "edge-external-validation-lab",
        help="EDGE-02: validate external open-source strategies under the same scientific pipeline and compare with internal strategies.",
    )
    eev.add_argument("--edge01-report-file", default=None, help="Optional EDGE-01 JSON report file; defaults to latest edge_extraction_lab_*.json")
    eev.add_argument("--knowledge-base-file", default=None, help="Optional crypto strategy research JSON file")
    eev.add_argument("--edge-discovery-file", default=None, help="Optional edge discovery JSON file")
    eev.add_argument("--min-external-candidates", type=int, default=5)
    eev.add_argument("--max-external-candidates", type=int, default=10)
    eev.add_argument("--disable-web-research", action="store_true")
    eev.add_argument("--strict-web-filters", action="store_true")
    eev.add_argument("--min-repo-stars", type=int, default=25)
    eev.add_argument("--max-inactive-days", type=int, default=180)
    eev.add_argument("--allow-forks", action="store_true")
    eev.add_argument("--allow-missing-readme", action="store_true")
    eev.add_argument("--output-prefix", default="edge_external_validation_lab")

    # --- edge-operational-pipeline ---
    eop = subparsers.add_parser(
        "edge-operational-pipeline",
        help="Run mandatory sequence EDGE-01 -> EDGE-02 with automatic transition gate and final executive report.",
    )
    eop.add_argument(
        "--prioritized-strategies",
        default="Ichimoku Kumo Breakout,ClassicDonchianBreakout,ClassicATRBreakout",
        help="Comma-separated prioritized strategies for EDGE-01",
    )
    eop.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    eop.add_argument("--timeframes", default="5m,15m,1h")
    eop.add_argument("--window-days", type=int, default=180)
    eop.add_argument("--capital", type=float, default=10_000.0)
    eop.add_argument("--max-bars", type=int, default=4500)
    eop.add_argument("--min-trades-per-filter", type=int, default=20)
    eop.add_argument("--top-filters", type=int, default=6)
    eop.add_argument("--max-candidate-filters", type=int, default=30)
    eop.add_argument("--min-external-candidates", type=int, default=5)
    eop.add_argument("--max-external-candidates", type=int, default=10)
    eop.add_argument("--disable-web-research", action="store_true")
    eop.add_argument("--strict-web-filters", action="store_true")
    eop.add_argument("--min-repo-stars", type=int, default=25)
    eop.add_argument("--max-inactive-days", type=int, default=180)
    eop.add_argument("--allow-forks", action="store_true")
    eop.add_argument("--allow-missing-readme", action="store_true")
    eop.add_argument("--strategy-version", default="v1.0")
    eop.add_argument("--default-platform-strategy", default="ClassicDonchianBreakout")
    eop.add_argument("--output-prefix", default="edge_operational_pipeline")

    # --- market-regime-router ---
    mrr = subparsers.add_parser(
        "market-regime-router",
        help="FASE 18: detect market regime and route strategies dynamically.",
    )
    mrr.add_argument("--report-file", default=None)
    mrr.add_argument("--symbols", default="BTC/USDT,ETH/USDT,SOL/USDT,BNB/USDT")
    mrr.add_argument("--timeframes", default="5m,15m,1h")
    mrr.add_argument("--window-days", type=int, default=120)
    mrr.add_argument("--capital", type=float, default=10_000.0)
    mrr.add_argument("--max-bars", type=int, default=3500)
    mrr.add_argument("--min-trades-per-regime", type=int, default=5)
    mrr.add_argument("--baseline-strategy", default=None)
    mrr.add_argument("--include-all-candidates", action="store_true")
    mrr.add_argument("--limit-candidates", type=int, default=0)
    mrr.add_argument("--output-prefix", default="phase18_market_regime_router")

    # --- strategy-version-compare ---
    svc = subparsers.add_parser(
        "strategy-version-compare",
        help="Compare the active strategy version against the previous version using recent paper trading results.",
    )
    svc.add_argument("--strategy-name", default="TradeOutcomeNextGenV1")
    svc.add_argument("--current-version", default="v1.0")
    svc.add_argument("--base-version", default=None)
    svc.add_argument("--window-days", type=int, default=30)

    # --- optimize ---
    opt = subparsers.add_parser("optimize", help="Optimize strategy parameters with backtests.")
    opt.add_argument("--symbol", default=settings.trading.default_symbol)
    opt.add_argument("--timeframe", default=settings.trading.default_timeframe)
    opt.add_argument("--start", required=False, help="Start date YYYY-MM-DD")
    opt.add_argument("--end", default=None, help="End date YYYY-MM-DD (default: now)")
    opt.add_argument("--capital", type=float, default=10_000.0)
    opt.add_argument("--top", type=int, default=10)
    opt.add_argument("--workers", type=int, default=settings.optimizer.workers)
    opt.add_argument("--max-combinations", type=int, default=settings.optimizer.max_combinations)
    opt.add_argument("--diagnostic", action="store_true")
    opt.add_argument("--train-start", default=None, help="Training period start YYYY-MM-DD")
    opt.add_argument("--train-end", default=None, help="Training period end YYYY-MM-DD")
    opt.add_argument("--val-start", default=None, help="Validation period start YYYY-MM-DD")
    opt.add_argument("--val-end", default=None, help="Validation period end YYYY-MM-DD")
    opt.add_argument("--resume-execution-id", default=None, help="Resume optimization from an existing execution id")

    # --- validate ---
    val = subparsers.add_parser("validate", help="Run only validation for an existing optimization execution.")
    val.add_argument("--execution-id", required=True, help="Existing optimization execution id")
    val.add_argument("--symbol", default=None, help="Optional symbol override")
    val.add_argument("--timeframe", default=None, help="Optional timeframe override")
    val.add_argument("--capital", type=float, default=10_000.0)
    val.add_argument("--top", type=int, default=10)
    val.add_argument("--train-start", default=None, help="Training period start YYYY-MM-DD")
    val.add_argument("--train-end", default=None, help="Training period end YYYY-MM-DD")
    val.add_argument("--val-start", default=None, help="Validation period start YYYY-MM-DD")
    val.add_argument("--val-end", default=None, help="Validation period end YYYY-MM-DD")

    # --- api ---
    api = subparsers.add_parser("api", help="Run the FastAPI dashboard backend.")
    api.add_argument("--host", default="0.0.0.0")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--reload", action="store_true")

    # --- execution-manager ---
    em = subparsers.add_parser("execution-manager", help="Run the professional long-running execution manager.")
    em.add_argument("--pipeline", default=None, help="Optional pipeline definition file to execute.")
    em.add_argument("--execution-id", default=None, help="Resume/continue an existing execution id.")
    subparsers.add_parser("execution-manager-rc1", help="Run RC1 real validation suite (500/1000/failures).")

    # --- strategy-research-lab ---
    lab = subparsers.add_parser(
        "strategy-research-lab",
        help="Run quantitative strategy autopsy and hypothesis pipeline before designing new strategies.",
    )
    lab.add_argument(
        "--strategies",
        default="TrendV1,TrendV2",
        help="Comma-separated strategies to analyze (default: TrendV1,TrendV2)",
    )
    lab.add_argument("--symbol", default=None, help="Optional symbol filter, e.g. BTC/USDT")
    lab.add_argument("--timeframe", default=None, help="Optional timeframe filter, e.g. 5m")
    lab.add_argument("--start", default=None, help="Optional start date YYYY-MM-DD")
    lab.add_argument("--end", default=None, help="Optional end date YYYY-MM-DD")
    lab.add_argument("--horizon-bars", type=int, default=12, help="Bars horizon for continuation/reversal heuristics")
    lab.add_argument(
        "--max-candidates-per-strategy",
        type=int,
        default=8,
        help="How many recent optimization candidates to replay per strategy when trade_history is empty",
    )

    # --- strategy-discovery ---
    discovery = subparsers.add_parser(
        "strategy-discovery",
        help="Rank strategy families, persist the catalog, and identify the next family to implement.",
    )
    discovery.add_argument("--pilot-symbol", default="BTC/USDT", help="Standard pilot symbol for the selected family")
    discovery.add_argument("--pilot-timeframe", default="5m", help="Standard pilot timeframe for the selected family")
    discovery.add_argument("--pilot-combinations", type=int, default=500, help="Standard pilot combination count")
    discovery.add_argument("--pilot-workers", type=int, default=16, help="Standard pilot worker count")

    # --- trade-lifecycle-audit ---
    tla = subparsers.add_parser(
        "trade-lifecycle-audit",
        help="FASE 9.3: Audit the complete trade lifecycle to identify position management bottlenecks.",
    )
    tla.add_argument("--strategy-name", default=None, help="Filter by strategy name")
    tla.add_argument("--strategy-version", default=None, help="Filter by strategy version")
    tla.add_argument("--symbol", default=None, help="Filter by symbol, e.g. BTC/USDT")
    tla.add_argument("--timeframe", default=None, help="Filter by timeframe, e.g. 5m")
    tla.add_argument("--execution-id", default=None, help="Filter by paper trading execution id")
    tla.add_argument("--window-days", type=int, default=30, help="Look-back window in days (default: 30)")
    tla.add_argument("--output-prefix", default="trade_lifecycle_audit")
    tla.add_argument("--no-db", action="store_true", help="Do not persist results to database")

    # --- phase9-4-controlled-improvement ---
    p94 = subparsers.add_parser(
        "phase9-4-controlled-improvement",
        help="FASE 9.4: single controlled V1.0 vs V1.1 comparison changing only exit management.",
    )
    p94.add_argument("--symbol", default=None)
    p94.add_argument("--timeframe", default=None)
    p94.add_argument("--start", default=None, help="Optional start date YYYY-MM-DD")
    p94.add_argument("--end", default=None, help="Optional end date YYYY-MM-DD")
    p94.add_argument("--capital", type=float, default=10_000.0)
    p94.add_argument("--window-days", type=int, default=30)
    p94.add_argument("--output-prefix", default="phase94_controlled_improvement")
    p94.add_argument("--skip-paper-campaign", action="store_true")
    p94.add_argument("--paper-cycles", type=int, default=1)

    # --- strategy-catalog-cycle (FASE 10) ---
    sc = subparsers.add_parser(
        "strategy-catalog-cycle",
        help="FASE 10: run permanent scientific strategy catalog cycle and ranking.",
    )
    sc.add_argument("--symbol", default=settings.trading.default_symbol)
    sc.add_argument("--timeframe", default=settings.trading.default_timeframe)
    sc.add_argument("--window-days", type=int, default=120)
    sc.add_argument("--capital", type=float, default=10_000.0)
    sc.add_argument("--max-catalog-strategies", type=int, default=10)
    sc.add_argument("--optimizer-max-combinations", type=int, default=20)
    sc.add_argument("--optimizer-workers", type=int, default=1)
    sc.add_argument("--top-k-for-paper", type=int, default=3)
    sc.add_argument("--output-prefix", default="strategy_catalog_cycle")

    # --- strategy-catalog-audit (FASE 10.2) ---
    sca = subparsers.add_parser(
        "strategy-catalog-audit",
        help="FASE 10.2: run scientific audit for strategy catalog evaluation pipeline.",
    )
    sca.add_argument("--symbol", default=settings.trading.default_symbol)
    sca.add_argument("--timeframe", default=settings.trading.default_timeframe)
    sca.add_argument("--window-days", type=int, default=90)
    sca.add_argument("--capital", type=float, default=10_000.0)
    sca.add_argument("--benchmark-symbols", default="BTC/USDT,ETH/USDT")
    sca.add_argument("--benchmark-timeframes", default="5m,15m,1h")
    sca.add_argument("--optimizer-max-combinations", type=int, default=5)
    sca.add_argument("--optimizer-workers", type=int, default=1)
    sca.add_argument("--max-bars", type=int, default=3000)
    sca.add_argument("--output-prefix", default="strategy_catalog_audit")

    # --- crypto-strategy-research (FASE 11) ---
    csr = subparsers.add_parser(
        "crypto-strategy-research",
        help="FASE 11: curate permanent crypto strategy knowledge base and priority ranking.",
    )
    csr.add_argument("--output-prefix", default="crypto_strategy_research")

    # --- supertrend-controlled-implementation (FASE 12) ---
    stc = subparsers.add_parser(
        "supertrend-controlled-implementation",
        help="FASE 12: controlled implementation and staged pipeline for SuperTrendV1.",
    )
    stc.add_argument("--symbol", default=settings.trading.default_symbol)
    stc.add_argument("--timeframe", default=settings.trading.default_timeframe)
    stc.add_argument("--start", default=None, help="Optional start date YYYY-MM-DD")
    stc.add_argument("--end", default=None, help="Optional end date YYYY-MM-DD")
    stc.add_argument("--window-days", type=int, default=90)
    stc.add_argument("--capital", type=float, default=10_000.0)
    stc.add_argument("--max-bars", type=int, default=3000)
    stc.add_argument("--optimizer-max-combinations", type=int, default=15)
    stc.add_argument("--optimizer-workers", type=int, default=1)
    stc.add_argument("--skip-paper-campaign", action="store_true")
    stc.add_argument("--paper-cycles", type=int, default=1)
    stc.add_argument("--output-prefix", default="phase12_supertrend_controlled")

    # --- continuous-strategy-factory (FASE 13) ---
    p13 = subparsers.add_parser(
        "continuous-strategy-factory",
        help="FASE 13: continuous experimental strategy factory with persistent backlog and staged pipeline.",
    )
    p13.add_argument("--symbol", default=settings.trading.default_symbol)
    p13.add_argument("--timeframe", default=settings.trading.default_timeframe)
    p13.add_argument("--window-days", type=int, default=120)
    p13.add_argument("--capital", type=float, default=10_000.0)
    p13.add_argument("--batch-size", type=int, default=20)
    p13.add_argument("--target-approved", type=int, default=3)
    p13.add_argument("--max-bars", type=int, default=3500)
    p13.add_argument("--optimizer-max-combinations", type=int, default=15)
    p13.add_argument("--optimizer-workers", type=int, default=1)
    p13.add_argument("--max-strategy-runtime-seconds", type=int, default=900)
    p13.add_argument("--max-cpu-per-worker-pct", type=float, default=100.0)
    p13.add_argument("--campaign-max-seconds", type=int, default=0)
    p13.add_argument("--probe-max-combinations", type=int, default=8)
    p13.add_argument("--probe-top-n", type=int, default=3)
    p13.add_argument("--paper-candidate-min-trades", type=int, default=100)
    p13.add_argument("--paper-candidate-min-profit-factor", type=float, default=1.10)
    p13.add_argument("--paper-candidate-min-expectancy", type=float, default=0.0)
    p13.add_argument("--paper-candidate-allow-overfitting", action="store_true")
    p13.add_argument("--paper-experiment-max-cycles", type=int, default=1)
    p13.add_argument("--paper-experiment-poll-seconds", type=float, default=2.0)
    p13.add_argument("--paper-experiment-bootstrap-bars", type=int, default=1500)
    p13.add_argument("--paper-experiment-bootstrap-replay-bars", type=int, default=350)
    p13.add_argument("--paper-experiment-review-window-days", type=int, default=14)
    p13.add_argument("--output-prefix", default="phase13_continuous_strategy_factory")

    # --- market-intelligence (FASE 14) ---
    p14 = subparsers.add_parser(
        "market-intelligence",
        help="FASE 14: experimental market intelligence to build prioritized backlog for Phase 13.",
    )
    p14.add_argument("--top-n", type=int, default=20)
    p14.add_argument("--output-prefix", default="phase14_market_intelligence")

    # --- overnight-campaign ---
    poc = subparsers.add_parser(
        "overnight-campaign",
        help="Intelligent overnight campaign: prioritize IMPLEMENTATION_PENDING, target 3 PAPER_CANDIDATE, stop at 09:00.",
    )
    poc.add_argument("--symbol", default=settings.trading.default_symbol)
    poc.add_argument("--timeframe", default=settings.trading.default_timeframe)
    poc.add_argument("--window-days", type=int, default=120)
    poc.add_argument("--capital", type=float, default=10_000.0)
    poc.add_argument("--batch-size", type=int, default=50)
    poc.add_argument("--target-approved", type=int, default=1)
    poc.add_argument("--target-paper-candidates", type=int, default=3)
    poc.add_argument("--campaign-end-hour", type=int, default=9)
    poc.add_argument("--max-bars", type=int, default=3500)
    poc.add_argument("--optimizer-max-combinations", type=int, default=15)
    poc.add_argument("--optimizer-workers", type=int, default=4)
    poc.add_argument("--max-strategy-runtime-seconds", type=int, default=900)
    poc.add_argument("--max-cpu-per-worker-pct", type=float, default=100.0)
    poc.add_argument("--campaign-max-seconds", type=int, default=0)
    poc.add_argument("--phase14-top-n", type=int, default=30)
    poc.add_argument("--checkpoint-interval-seconds", type=int, default=600)
    poc.add_argument("--disable-auto-research", action="store_true")
    poc.add_argument("--probe-max-combinations", type=int, default=8)
    poc.add_argument("--probe-top-n", type=int, default=3)
    poc.add_argument("--paper-candidate-min-trades", type=int, default=100)
    poc.add_argument("--paper-candidate-min-profit-factor", type=float, default=1.10)
    poc.add_argument("--paper-candidate-min-expectancy", type=float, default=0.0)
    poc.add_argument("--paper-candidate-allow-overfitting", action="store_true")
    poc.add_argument("--paper-experiment-max-cycles", type=int, default=1)
    poc.add_argument("--paper-experiment-poll-seconds", type=float, default=2.0)
    poc.add_argument("--paper-experiment-bootstrap-bars", type=int, default=1500)
    poc.add_argument("--paper-experiment-bootstrap-replay-bars", type=int, default=350)
    poc.add_argument("--paper-experiment-review-window-days", type=int, default=14)
    poc.add_argument("--precheck-min-candles", type=int, default=1000)
    poc.add_argument("--output-prefix", default="overnight_campaign")

    # --- phase13-1-audit-strengthening (FASE 13.1) ---
    p131 = subparsers.add_parser(
        "phase13-1-audit-strengthening",
        help="FASE 13.1: audit/reclassification hardening with faithful implementation and context matrix.",
    )
    p131.add_argument("--symbol", default=settings.trading.default_symbol)
    p131.add_argument("--timeframe", default=settings.trading.default_timeframe)
    p131.add_argument("--window-days", type=int, default=120)
    p131.add_argument("--capital", type=float, default=10_000.0)
    p131.add_argument("--max-bars", type=int, default=3500)
    p131.add_argument("--optimizer-max-combinations", type=int, default=15)
    p131.add_argument("--optimizer-workers", type=int, default=1)
    p131.add_argument("--max-pending-to-process", type=int, default=12)
    p131.add_argument("--mode", choices=["audit", "continuous_coverage"], default="audit")
    p131.add_argument("--max-workers", type=int, default=2)
    p131.add_argument("--max-strategy-runtime-seconds", type=int, default=900)
    p131.add_argument("--max-optimizer-combinations-per-strategy", type=int, default=15)
    p131.add_argument("--max-cpu-per-worker-pct", type=float, default=100.0)
    p131.add_argument("--output-prefix", default="phase13_1_audit_strengthening")

    # --- trade-management-research-lab ---
    tml = subparsers.add_parser(
        "trade-management-research-lab",
        help="Replay reconstructed entries and compare position management scenarios A-G.",
    )
    tml.add_argument(
        "--operations-csv",
        default=None,
        help="Optional explicit operations CSV (default: latest strategy_research_operations_*.csv)",
    )
    tml.add_argument("--symbol", default=None, help="Optional symbol filter, e.g. BTC/USDT")
    tml.add_argument("--timeframe", default=None, help="Optional timeframe filter, e.g. 5m")
    tml.add_argument("--max-bars", type=int, default=96, help="Maximum bars to replay after each entry")
    tml.add_argument("--atr-period", type=int, default=14, help="ATR period used by trailing scenarios")
    tml.add_argument("--atr-mult", type=float, default=2.0, help="ATR multiplier used by trailing scenarios")
    tml.add_argument("--time-stop-bars", type=int, default=24, help="Bars until forced exit in time-stop scenario")
    tml.add_argument("--momentum-fast", type=int, default=8, help="Fast EMA for momentum-loss exit")
    tml.add_argument("--momentum-slow", type=int, default=21, help="Slow EMA for momentum-loss exit")
    tml.add_argument(
        "--mfe-pullback-ratio",
        type=float,
        default=0.35,
        help="Pullback ratio from MFE peak for scenario E trailing",
    )
    tml.add_argument(
        "--bootstrap-iterations",
        type=int,
        default=500,
        help="Bootstrap iterations for significance test versus baseline scenario A",
    )

    return parser.parse_args()


def _parse_date(date_str: str | None) -> datetime | None:
    if date_str is None:
        return None
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _canonical_market(symbol: str, timeframe: str) -> tuple[str, str]:
    return validate_symbol(symbol), validate_timeframe(timeframe)


def cmd_download(args: argparse.Namespace) -> None:
    """Download and persist historical OHLCV data."""
    from exchange.binance_market_data_client import BinanceMarketDataClient
    from exchange.data_downloader import DataDownloader

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    start = _parse_date(args.start)
    end = _parse_date(args.end)

    client = BinanceMarketDataClient()
    client.connect()

    downloader = DataDownloader(client)
    df = downloader.download_historical(symbol, timeframe, start, end)
    logger.info("Downloaded %d candles for %s/%s.", len(df), symbol, timeframe)

    client.disconnect()


def cmd_backtest(args: argparse.Namespace) -> None:
    """Execute a full backtest and print results."""
    from datetime import timezone

    from backtesting.engine import BacktestConfig, BacktestEngine
    from backtesting.reporter import BacktestReporter
    from database.connection import get_session
    from database.repositories import CandleRepository
    from strategies.factory import create_strategy
    import pandas as pd

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    start = _parse_date(args.start)
    end = _parse_date(args.end) or datetime.now(tz=timezone.utc)

    # Carrega dados do banco
    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(symbol, timeframe, start, end)

    if not candles:
        logger.error(
            "No data in database for %s/%s. Run 'download' first.",
            symbol,
            timeframe,
        )
        sys.exit(1)

    df = pd.DataFrame(
        [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )

    strategy = create_strategy(settings.trading.strategy)
    strategy.initialize()

    enriched_df = strategy.calculate(df)

    engine = BacktestEngine(
        strategy,
        config=BacktestConfig(initial_capital=args.capital),
    )
    result = engine.run(df, symbol=symbol, timeframe=timeframe)

    reporter = BacktestReporter()
    reporter.print_summary(result)
    reporter.save_equity_chart(result)
    reporter.save_trade_log(result)

    execution_id = HistoryPersistenceService.new_execution_id()
    with get_session() as session:
        history = HistoryPersistenceService(session)
        history.save_backtest_result(
            execution_id=execution_id,
            result=result,
            timeframe=timeframe,
            start_date=start,
            end_date=end,
        )
        history.save_trades_from_backtest(
            execution_id=execution_id,
            strategy=result.strategy_name,
            symbol=result.symbol,
            timeframe=timeframe,
            trades=result.trades,
        )

    if args.diagnostic:
        from backtesting.diagnostics import BacktestDiagnosticReporter

        diagnostic_reporter = BacktestDiagnosticReporter()
        diagnostic_reporter.generate_and_save(
            strategy=strategy,
            raw_df=df,
            enriched_df=enriched_df,
            result=result,
            timeframe=timeframe,
        )


def cmd_paper(args: argparse.Namespace) -> None:
    """Run paper trading simulation on historical data."""
    from datetime import timezone
    from sqlalchemy import func

    from database.connection import get_session
    from database.models import Trade
    from database.repositories import CandleRepository
    from paper_trading.daily_report import PaperDailyReportConfig, PaperDailyReportService
    from paper_trading.paper_broker import PaperBroker
    from paper_trading.paper_trader import PaperTrader
    from strategies.factory import create_strategy
    import pandas as pd

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    start = _parse_date(args.start)
    end = _parse_date(args.end) or datetime.now(tz=timezone.utc)

    with get_session() as session:
        repo = CandleRepository(session)
        candles = repo.get_range(symbol, timeframe, start, end)

    if not candles:
        logger.error(
            "No data for %s/%s. Run 'download' first.", symbol, timeframe
        )
        sys.exit(1)

    df = pd.DataFrame(
        [
            {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
            for c in candles
        ],
        index=pd.DatetimeIndex([c.open_time for c in candles], tz="UTC"),
    )

    strategy = create_strategy(args.strategy_name)
    strategy.initialize()

    execution_id = HistoryPersistenceService.new_execution_id()
    broker = PaperBroker(initial_capital=args.capital)
    trader = PaperTrader(
        strategy=strategy,
        broker=broker,
        execution_id=execution_id,
        timeframe=timeframe,
        strategy_version=str(args.strategy_version),
    )
    trader_summary = trader.run(df, symbol=symbol, timeframe=timeframe)

    # Relatorio final da carteira
    last_price = float(df["close"].iloc[-1])
    base_asset = symbol.split("/")[0]
    portfolio_value = broker.get_portfolio_value({base_asset: last_price})
    pnl = portfolio_value - args.capital
    logger.info(
        "Paper trading complete - final=%.2f initial=%.2f PnL=%.2f (%.2f%%)",
        portfolio_value,
        args.capital,
        pnl,
        pnl / args.capital * 100,
    )

    with get_session() as session:
        history = HistoryPersistenceService(session)
        history.register_strategy_version(
            strategy_name=strategy.name,
            version=str(args.strategy_version),
            git_commit=os.getenv("GIT_COMMIT"),
            description="Paper trading campaign",
        )
        total_trades = int(trader_summary.get("closed_trades", 0))
        win_rate = 0.0
        expectancy = 0.0
        if total_trades > 0:
            expectancy = float(trader_summary.get("net_profit", 0.0)) / float(total_trades)
            wins = (
                session.query(func.count(Trade.id))
                .filter(
                    Trade.is_paper.is_(True),
                    Trade.status == "CLOSED",
                    Trade.strategy_name == strategy.name,
                    Trade.exit_time >= start,
                    Trade.exit_time <= end,
                    Trade.pnl > 0,
                )
                .scalar()
                or 0
            )
            win_rate = float(wins) / float(total_trades)

        history.create_backtest_run(
            execution_id=execution_id,
            strategy=strategy.name,
            symbol=symbol,
            timeframe=timeframe,
            start_date=start,
            end_date=end,
            initial_capital=args.capital,
            status="completed",
            final_capital=portfolio_value,
            total_trades=total_trades,
            win_rate=win_rate,
            profit_factor=None,
            sharpe=None,
            expectancy=expectancy,
            drawdown=None,
        )

    if not bool(args.no_daily_report):
        report_date = end.astimezone(timezone.utc).date()
        with get_session() as session:
            report_service = PaperDailyReportService(session=session, base_dir=Path(__file__).parent)
            report_result = report_service.run(
                PaperDailyReportConfig(
                    report_date=report_date,
                    strategy_name=strategy.name,
                    strategy_version=str(args.strategy_version),
                )
            )

        print("Daily report summary:")
        print(json.dumps(report_result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
        print("Daily report outputs:")
        for key, value in report_result.get("outputs", {}).items():
            print(f"- {key}: {value}")

    print(f"Paper execution_id: {execution_id}")


def cmd_paper_daily_report(args: argparse.Namespace) -> None:
    from datetime import timezone

    from paper_trading.daily_report import PaperDailyReportConfig, PaperDailyReportService

    report_date = _parse_date(str(args.date)).astimezone(timezone.utc).date()
    with get_session() as session:
        service = PaperDailyReportService(session=session, base_dir=Path(__file__).parent)
        result = service.run(
            PaperDailyReportConfig(
                report_date=report_date,
                strategy_name=str(args.strategy_name),
                strategy_version=str(args.strategy_version) if args.strategy_version else None,
                output_prefix=str(args.output_prefix),
            )
        )

    print("\n======================================")
    print("PAPER TRADING DAILY REPORT")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_paper_live(args: argparse.Namespace) -> None:
    from paper_trading.paper_live_service import PaperLiveConfig, PaperLiveService

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    effective_min_trades_before_change: int
    if args.min_trades_before_change is None:
        campaign_id_value = str(args.campaign_id or "").strip()
        effective_min_trades_before_change = 0 if campaign_id_value.startswith("spc-") else 100
    else:
        effective_min_trades_before_change = max(0, int(args.min_trades_before_change))

    service = PaperLiveService(base_dir=Path(__file__).parent)
    result = service.run(
        PaperLiveConfig(
            symbol=symbol,
            timeframe=timeframe,
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            campaign_id=str(args.campaign_id) if args.campaign_id else None,
            initial_capital=max(100.0, float(args.capital)),
            poll_seconds=max(1.0, float(args.poll_seconds)),
            bootstrap_bars=max(200, int(args.bootstrap_bars)),
            bootstrap_replay_bars=max(60, int(args.bootstrap_replay_bars)),
            max_cycles=max(0, int(args.max_cycles)),
            resume=not bool(args.no_resume),
            min_trades_before_change=effective_min_trades_before_change,
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("PAPER LIVE CONTINUOUS OPERATION")
    print("======================================")
    print("Summary:")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_live(args: argparse.Namespace) -> None:
    from execution.live_trading_service import LiveTradingConfig, LiveTradingService

    timeframe = validate_timeframe(str(args.timeframe))
    symbols_raw = str(getattr(args, "symbols", "") or "").strip()
    parsed_symbols: list[str] = []
    if symbols_raw:
        for raw in symbols_raw.split(","):
            token = raw.strip()
            if not token:
                continue
            canonical_symbol, canonical_tf = _canonical_market(token, timeframe)
            parsed_symbols.append(canonical_symbol)
            timeframe = canonical_tf

    single_symbol = str(getattr(args, "symbol", "") or "").strip()
    if single_symbol:
        canonical_symbol, canonical_tf = _canonical_market(single_symbol, timeframe)
        timeframe = canonical_tf
        if canonical_symbol not in parsed_symbols:
            parsed_symbols.insert(0, canonical_symbol)

    if not parsed_symbols:
        raise ValueError("Informe --symbol ou --symbols para o comando live.")

    primary_symbol = parsed_symbols[0]
    service = LiveTradingService(base_dir=Path(__file__).parent)
    result = service.run(
        LiveTradingConfig(
            symbol=primary_symbol,
            symbols=tuple(parsed_symbols),
            timeframe=timeframe,
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            poll_seconds=max(1.0, float(args.poll_seconds)),
            bootstrap_bars=max(100, int(args.bootstrap_bars)),
            bootstrap_replay_bars=max(50, int(args.bootstrap_replay_bars)),
            max_cycles=max(0, int(args.max_cycles)),
            resume=not bool(args.no_resume),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("LIVE BINANCE SPOT")
    print("======================================")
    print("Summary:")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def cmd_paper_live_supervisor(args: argparse.Namespace) -> None:
    from paper_trading.edge_drift_monitor import EdgeDriftContext
    from paper_trading.paper_live_supervisor import PaperLiveSupervisorConfig, PaperLiveSupervisorService

    parsed_contexts = _parse_specialized_contexts(str(args.contexts))
    contexts = tuple(EdgeDriftContext(symbol=symbol, timeframe=timeframe) for symbol, timeframe in parsed_contexts)

    service = PaperLiveSupervisorService(base_dir=Path(__file__).parent)
    result = service.run(
        PaperLiveSupervisorConfig(
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            campaign_id=str(args.campaign_id),
            contexts=contexts,
            contexts_from_latest_report=not bool(args.no_contexts_from_latest_report),
            initial_capital=max(100.0, float(args.capital)),
            poll_seconds=max(1.0, float(args.poll_seconds)),
            bootstrap_bars=max(200, int(args.bootstrap_bars)),
            bootstrap_replay_bars=max(60, int(args.bootstrap_replay_bars)),
            min_trades_before_change=max(0, int(args.min_trades_before_change)),
            output_prefix=str(args.output_prefix),
            supervisor_poll_seconds=max(0.1, float(args.supervisor_poll_seconds)),
            stuck_timeout_seconds=max(1.0, float(args.stuck_timeout_seconds)),
            startup_grace_seconds=max(0.0, float(args.startup_grace_seconds)),
            restart_delay_seconds=max(0.0, float(args.restart_delay_seconds)),
            max_consecutive_restarts=max(1, int(args.max_consecutive_restarts)),
            max_supervision_cycles=max(0, int(args.max_supervision_cycles)),
        )
    )

    print("\n======================================")
    print("PAPER LIVE SUPERVISOR")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_paper_operational_report(args: argparse.Namespace) -> None:
    from paper_trading.paper_live_service import PaperLiveService

    report_date = _parse_date(str(args.date)).astimezone(timezone.utc).date()
    service = PaperLiveService(base_dir=Path(__file__).parent)
    outputs = service.generate_reports(
        report_date=report_date,
        strategy_name=str(args.strategy_name),
        strategy_version=str(args.strategy_version),
        output_prefix=str(args.output_prefix),
    )

    print("\n======================================")
    print("PAPER OPERATIONAL REPORTS")
    print("======================================")
    for key, value in outputs.items():
        print(f"- {key}: {value}")


def cmd_strategy_version_compare(args: argparse.Namespace) -> None:
    from paper_trading.paper_live_service import PaperLiveService

    service = PaperLiveService(base_dir=Path(__file__).parent)
    result = service.compare_versions(
        strategy_name=str(args.strategy_name),
        current_version=str(args.current_version),
        base_version=str(args.base_version) if args.base_version else None,
        window_days=max(1, int(args.window_days)),
    )

    print("\n======================================")
    print("STRATEGY VERSION COMPARISON")
    print("======================================")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


def _parse_specialized_contexts(raw: str) -> tuple[tuple[str, str], ...]:
    contexts: list[tuple[str, str]] = []
    for item in str(raw or "").split(","):
        token = item.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(f"Invalid context '{token}'. Expected SYMBOL:TIMEFRAME")
        symbol, timeframe = token.split(":", 1)
        symbol = symbol.strip()
        timeframe = timeframe.strip()
        if not symbol or not timeframe:
            raise ValueError(f"Invalid context '{token}'. Expected SYMBOL:TIMEFRAME")
        contexts.append((symbol, timeframe))
    return tuple(contexts)


def _parse_edge_drift_contexts(raw: str) -> tuple[tuple[str, str], ...]:
    return _parse_specialized_contexts(raw)


def _parse_specialized_campaign_contexts(raw: str) -> tuple[tuple[str, str], ...]:
    return _parse_specialized_contexts(raw)


def cmd_paper_specialized_validation(args: argparse.Namespace) -> None:
    from paper_trading.specialized_validation import (
        SpecializedBaseline,
        SpecializedContext,
        SpecializedPaperValidationConfig,
        SpecializedPaperValidationService,
        ValidationCriteria,
    )

    parsed_contexts = _parse_specialized_contexts(str(args.contexts))
    contexts = tuple(SpecializedContext(symbol=symbol, timeframe=timeframe) for symbol, timeframe in parsed_contexts)

    service = SpecializedPaperValidationService(base_dir=Path(__file__).parent)
    result = service.run(
        SpecializedPaperValidationConfig(
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            run_live=bool(args.run_live),
            max_global_cycles=max(0, int(args.max_global_cycles)),
            poll_seconds=max(1.0, float(args.poll_seconds)),
            bootstrap_bars=max(200, int(args.bootstrap_bars)),
            bootstrap_replay_bars=max(60, int(args.bootstrap_replay_bars)),
            initial_capital=max(100.0, float(args.capital)),
            min_trades_before_change=max(0, int(args.min_trades_before_change)),
            contexts=contexts,
            contexts_from_matrix=not bool(args.no_contexts_from_matrix),
            context_min_trades=max(1, int(args.context_min_trades)),
            context_min_profit_factor=float(args.context_min_profit_factor),
            context_min_expectancy=float(args.context_min_expectancy),
            edge_matrix_csv=str(args.edge_matrix_csv) if args.edge_matrix_csv else None,
            backtest_baseline=SpecializedBaseline(
                profit_factor=float(args.backtest_profit_factor) if args.backtest_profit_factor is not None else None,
                sharpe=float(args.backtest_sharpe) if args.backtest_sharpe is not None else None,
                expectancy=float(args.backtest_expectancy) if args.backtest_expectancy is not None else None,
                drawdown=float(args.backtest_drawdown) if args.backtest_drawdown is not None else None,
                win_rate=float(args.backtest_win_rate) if args.backtest_win_rate is not None else None,
            ),
            rolling_oos_baseline=SpecializedBaseline(
                profit_factor=float(args.rolling_profit_factor) if args.rolling_profit_factor is not None else None,
                sharpe=float(args.rolling_sharpe) if args.rolling_sharpe is not None else None,
                expectancy=float(args.rolling_expectancy) if args.rolling_expectancy is not None else None,
                drawdown=float(args.rolling_drawdown) if args.rolling_drawdown is not None else None,
                win_rate=float(args.rolling_win_rate) if args.rolling_win_rate is not None else None,
            ),
            criteria=ValidationCriteria(
                min_days=max(1, int(args.min_validation_days)),
                min_trades=max(1, int(args.min_validation_trades)),
                min_profit_factor=float(args.min_profit_factor),
                min_expectancy=float(args.min_expectancy),
                min_sharpe=float(args.min_sharpe),
                max_drawdown=max(0.0, float(args.max_drawdown)),
                max_pf_degradation_pct=max(0.0, float(args.max_pf_degradation_pct)),
                max_sharpe_degradation_pct=max(0.0, float(args.max_sharpe_degradation_pct)),
                max_expectancy_degradation_pct=max(0.0, float(args.max_expectancy_degradation_pct)),
                max_win_rate_degradation_pct=max(0.0, float(args.max_win_rate_degradation_pct)),
                max_drawdown_worsening_pct=max(0.0, float(args.max_drawdown_worsening_pct)),
            ),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("PAPER SPECIALIZED VALIDATION")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_edge_drift_monitor(args: argparse.Namespace) -> None:
    from paper_trading.edge_drift_monitor import (
        EdgeDriftContext,
        EdgeDriftMonitorConfig,
        EdgeDriftMonitorService,
        EdgeDriftThresholds,
    )

    parsed_contexts = _parse_edge_drift_contexts(str(args.contexts))
    contexts = tuple(EdgeDriftContext(symbol=symbol, timeframe=timeframe) for symbol, timeframe in parsed_contexts)

    service = EdgeDriftMonitorService(base_dir=Path(__file__).parent)
    result = service.run(
        EdgeDriftMonitorConfig(
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            campaign_id=str(args.campaign_id) if args.campaign_id else None,
            specialized_report_file=str(args.specialized_report_file) if args.specialized_report_file else None,
            contexts=contexts,
            contexts_from_latest_report=not bool(args.no_contexts_from_latest_report),
            lookback_days=max(1, int(args.lookback_days)),
            history_window=max(3, int(args.history_window)),
            min_validation_days=max(1, int(args.min_validation_days)),
            min_validation_trades=max(1, int(args.min_validation_trades)),
            initial_capital=max(100.0, float(args.initial_capital)),
            thresholds=EdgeDriftThresholds(
                attention_health_score=float(args.attention_health_score),
                critical_health_score=float(args.critical_health_score),
                attention_metric_degradation_pct=float(args.attention_metric_degradation_pct),
                critical_metric_degradation_pct=float(args.critical_metric_degradation_pct),
                attention_drawdown_worsening_pct=float(args.attention_drawdown_worsening_pct),
                critical_drawdown_worsening_pct=float(args.critical_drawdown_worsening_pct),
                attention_stability_score=float(args.attention_stability_score),
                critical_stability_score=float(args.critical_stability_score),
            ),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("EDGE DRIFT MONITOR")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_paper_specialized_campaign(args: argparse.Namespace) -> None:
    from paper_trading.edge_drift_monitor import EdgeDriftContext
    from paper_trading.specialized_campaign import SpecializedCampaignConfig, SpecializedPaperCampaignService

    parsed_contexts = _parse_specialized_campaign_contexts(str(args.contexts))
    contexts = tuple(EdgeDriftContext(symbol=symbol, timeframe=timeframe) for symbol, timeframe in parsed_contexts)

    service = SpecializedPaperCampaignService(base_dir=Path(__file__).parent)
    result = service.run(
        SpecializedCampaignConfig(
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            campaign_id=str(args.campaign_id) if args.campaign_id else None,
            specialized_report_file=str(args.specialized_report_file) if args.specialized_report_file else None,
            contexts=contexts,
            contexts_from_latest_report=not bool(args.no_contexts_from_latest_report),
            monitor_lookback_days=max(1, int(args.monitor_lookback_days)),
            monitor_history_window=max(3, int(args.monitor_history_window)),
            phase1_min_days=max(1, int(args.phase1_min_days)),
            phase1_min_trades=max(1, int(args.phase1_min_trades)),
            phase2_min_days=max(1, int(args.phase2_min_days)),
            phase2_min_trades=max(1, int(args.phase2_min_trades)),
            min_profit_factor=float(args.min_profit_factor),
            min_expectancy=float(args.min_expectancy),
            min_sharpe=float(args.min_sharpe),
            max_drawdown=max(0.0, float(args.max_drawdown)),
            max_consecutive_critical_alerts=max(1, int(args.max_consecutive_critical_alerts)),
            max_consecutive_non_normal_alerts=max(1, int(args.max_consecutive_non_normal_alerts)),
            initial_capital=max(100.0, float(args.initial_capital)),
            poll_seconds=max(1.0, float(args.poll_seconds)),
            bootstrap_bars=max(200, int(args.bootstrap_bars)),
            bootstrap_replay_bars=max(60, int(args.bootstrap_replay_bars)),
            max_cycles_per_context=max(0, int(args.max_cycles_per_context)),
            min_trades_before_change=max(0, int(args.min_trades_before_change)),
            legacy_live_execution=bool(args.legacy_live_execution),
            ingest_execution_ids=tuple(item.strip() for item in str(args.ingest_execution_ids or "").split(",") if item.strip()),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("PAPER SPECIALIZED CAMPAIGN")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_paper_campaign_coverage(args: argparse.Namespace) -> None:
    from paper_trading.paper_campaign_coverage_monitor import PaperCampaignCoverageConfig, PaperCampaignCoverageService

    service = PaperCampaignCoverageService(base_dir=Path(__file__).parent)
    result = service.run(
        PaperCampaignCoverageConfig(
            campaign_id=str(args.campaign_id),
            strategy_name=str(args.strategy_name),
            strategy_version=str(args.strategy_version),
            stale_minutes=max(1, int(args.stale_minutes)),
            min_coverage_percent=max(0.0, min(100.0, float(args.min_coverage_percent))),
            critical_coverage_percent=max(0.0, min(100.0, float(args.critical_coverage_percent))),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("PAPER CAMPAIGN COVERAGE")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_strategy_diagnostics(args: argparse.Namespace) -> None:
    from strategy_diagnostics import StrategyDiagnosticsConfig, StrategyDiagnosticsService

    service = StrategyDiagnosticsService(base_dir=Path(__file__).parent)
    result = service.run(
        StrategyDiagnosticsConfig(
            strategy_name=str(args.strategy_name) if args.strategy_name else None,
            strategy_version=str(args.strategy_version) if args.strategy_version else None,
            symbol=str(args.symbol) if args.symbol else None,
            timeframe=str(args.timeframe) if args.timeframe else None,
            execution_id=str(args.execution_id) if args.execution_id else None,
            window_hours=max(1, int(args.window_hours)),
            window_days=max(1, int(args.window_days)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("STRATEGY DIAGNOSTICS")
    print("======================================")
    print(json.dumps(result.get("report", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


_COMMANDS = {
    "download": cmd_download,
    "backtest": cmd_backtest,
    "paper": cmd_paper,
    "market-data-daemon": None,
    "optimize": None,
    "validate": None,
    "api": None,
    "execution-manager": None,
    "execution-manager-rc1": None,
    "strategy-research-lab": None,
    "trade-management-research-lab": None,
    "trade-lifecycle-audit": None,
    "phase9-4-controlled-improvement": None,
    "strategy-catalog-cycle": None,
    "strategy-catalog-audit": None,
    "crypto-strategy-research": None,
    "supertrend-controlled-implementation": None,
    "continuous-strategy-factory": None,
    "market-intelligence": None,
    "phase13-1-audit-strengthening": None,
    "robustness-validation": None,
    "trade-outcome-learning": None,
    "phase9-controlled-implementation": None,
    "execution-framework-optimization": None,
    "paper-daily-report": None,
    "paper-live": None,
    "live": None,
    "paper-live-supervisor": None,
    "paper-operational-report": None,
    "paper-specialized-validation": None,
    "edge-drift-monitor": None,
    "paper-specialized-campaign": None,
    "strategy-diagnostics": None,
    "strategy-version-compare": None,
}


def cmd_api(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("webapi.app:app", host=args.host, port=args.port, reload=args.reload)


def _run_validation_for_execution(
    execution_id: str,
    symbol: str,
    timeframe: str,
    strategy_name: str,
    capital: float,
    top_n: int,
    train_start: datetime | None,
    train_end: datetime | None,
    val_start: datetime | None,
    val_end: datetime | None,
) -> None:
    from database.history_models import OptimizationResultRecord
    from optimizer.optimization_result import OptimizationResult
    from validation.validator import OptimizationValidator, ValidationCriteria, default_validation_window

    candidate_limit = max(50, max(1, top_n))

    with get_session() as session:
        rows = (
            session.query(OptimizationResultRecord)
            .filter(OptimizationResultRecord.execution_id == execution_id)
            .order_by(OptimizationResultRecord.profit_factor.desc())
            .limit(candidate_limit)
            .all()
        )

    if not rows:
        raise SystemExit(
            f"No persisted optimization results found for execution_id={execution_id}. "
            "Validation cannot run without persisted optimization results."
        )

    optimized_results: list[OptimizationResult] = []
    for index, row in enumerate(rows, start=1):
        parameters = json.loads(row.parameters_json) if row.parameters_json else {}
        metrics = {
            "total_trades": row.trades or 0,
            "win_rate": row.win_rate or 0.0,
            "profit_factor": row.profit_factor or 0.0,
            "net_profit": row.net_profit or 0.0,
            "return_pct": row.return_percent or 0.0,
            "max_drawdown_pct": row.drawdown or 0.0,
            "sharpe_ratio": row.sharpe or 0.0,
            "expectancy": row.expectancy or 0.0,
        }
        optimized_results.append(
            OptimizationResult(
                rank=index,
                parameters=parameters,
                metrics=metrics,
                combinations_tested=len(rows),
                runtime_seconds=0.0,
                error=None,
            )
        )

    criteria = ValidationCriteria(
        min_trades=settings.validation.min_trades,
        min_profit_factor=settings.validation.min_profit_factor,
        max_drawdown_pct=settings.validation.max_drawdown_pct,
        min_win_rate_pct=settings.validation.min_win_rate_pct,
        min_expectancy=settings.validation.min_expectancy,
        min_sharpe=settings.validation.min_sharpe,
    )
    validator = OptimizationValidator(criteria, strategy_name=strategy_name)

    default_window = default_validation_window(None, None, symbol=symbol, timeframe=timeframe)
    final_train_start = train_start or default_window.train_start
    final_train_end = train_end or default_window.train_end
    final_val_start = val_start or default_window.validation_start
    final_val_end = val_end or default_window.validation_end

    validation_summary = validator.validate(
        optimization_results=optimized_results,
        symbol=symbol,
        timeframe=timeframe,
        capital=capital,
        train_start=final_train_start,
        train_end=final_train_end,
        validation_start=final_val_start,
        validation_end=final_val_end,
        top_n=max(1, top_n),
    )

    print("\n======================================")
    print("STATISTICAL VALIDATION REPORT")
    print("======================================")
    print(f"Execution ID: {execution_id}")
    print(f"Symbol/Timeframe: {symbol}/{timeframe}")
    print(f"Train: {final_train_start.date()} to {final_train_end.date()}")
    print(f"Validation: {final_val_start.date()} to {final_val_end.date()}")
    print(f"Total avaliadas: {validation_summary.total_candidates}")
    print(f"Descartadas: {validation_summary.discarded}")
    print(f"Aprovadas: {validation_summary.passed}")
    if validation_summary.best_validated is not None:
        print("Melhor configuracao validada:")
        print(validation_summary.best_validated)
    print("Arquivos de validacao gerados:")
    for output_file in validation_summary.output_files:
        print(f"- {output_file}")

    with get_session() as session:
        history = HistoryPersistenceService(session)
        history.save_validation_run(
            execution_id=HistoryPersistenceService.new_execution_id(),
            optimizer_run=execution_id,
            total_tested=validation_summary.total_candidates,
            approved=validation_summary.passed,
            rejected=validation_summary.discarded,
            min_profit_factor=settings.validation.min_profit_factor,
            min_trades=settings.validation.min_trades,
            max_drawdown=settings.validation.max_drawdown_pct,
            validation_status="completed",
        )


def _run_post_validation_pipeline(
    execution_id: str,
    symbol: str,
    timeframe: str,
    strategy_name: str,
) -> None:
    from database.history_models import OptimizationResultRecord, OptimizationRun, ValidationRun
    from research.services.strategy_research_lab import ResearchLabConfig, StrategyResearchLab
    from research.services.trade_management_research_lab import TradeManagementLabConfig, TradeManagementResearchLab
    from sqlalchemy import desc, func

    print("\n======================================")
    print("POST-VALIDATION PIPELINE")
    print("======================================")

    strategy_lab_result = None
    trade_lab_result = None
    strategy_lab_error = None
    trade_lab_error = None

    try:
        print("[1/4] Strategy Research Lab...")
        with get_session() as session:
            lab = StrategyResearchLab(session=session, base_dir=Path(__file__).parent)
            strategy_lab_result = lab.run(
                ResearchLabConfig(
                    strategies=[strategy_name],
                    symbol=symbol,
                    timeframe=timeframe,
                    start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    end=datetime(2024, 12, 31, tzinfo=timezone.utc),
                    horizon_bars=12,
                    max_candidates_per_strategy=8,
                )
            )
        print("  Strategy Research Lab concluido.")
    except Exception as exc:  # pragma: no cover
        strategy_lab_error = str(exc)
        print(f"  Strategy Research Lab falhou: {exc}")

    try:
        print("[2/4] Trade Management Research Lab...")
        operations_csv = None
        if strategy_lab_result:
            operations_csv = strategy_lab_result.get("outputs", {}).get("operations_csv")
        with get_session() as session:
            lab = TradeManagementResearchLab(session=session, base_dir=Path(__file__).parent)
            trade_lab_result = lab.run(
                TradeManagementLabConfig(
                    operations_csv=operations_csv,
                    symbol=symbol,
                    timeframe=timeframe,
                    max_bars=96,
                    atr_period=14,
                    atr_mult=2.0,
                    time_stop_bars=24,
                    momentum_fast=8,
                    momentum_slow=21,
                    mfe_pullback_ratio=0.35,
                    bootstrap_iterations=500,
                )
            )
        print("  Trade Management Research Lab concluido.")
    except Exception as exc:  # pragma: no cover
        trade_lab_error = str(exc)
        print(f"  Trade Management Research Lab falhou: {exc}")

    print("[3/4] Ranking final...")
    with get_session() as session:
        top10 = (
            session.query(OptimizationResultRecord)
            .filter(OptimizationResultRecord.execution_id == execution_id)
            .order_by(desc(OptimizationResultRecord.profit_factor), desc(OptimizationResultRecord.sharpe))
            .limit(10)
            .all()
        )
        validation = (
            session.query(ValidationRun)
            .filter(ValidationRun.optimizer_run == execution_id)
            .order_by(desc(ValidationRun.created_at))
            .first()
        )

        compare_strategies = ["TrendV1", "TrendV2", "MeanReversionV1", "BreakoutV1"]
        compare_rows = []
        for st in compare_strategies:
            run = (
                session.query(OptimizationRun)
                .filter(OptimizationRun.strategy == st)
                .order_by(desc(OptimizationRun.started_at))
                .first()
            )
            if not run:
                compare_rows.append((st, None, None))
                continue
            best_pf = session.query(func.max(OptimizationResultRecord.profit_factor)).filter(
                OptimizationResultRecord.execution_id == run.execution_id
            ).scalar()
            best_sharpe = session.query(func.max(OptimizationResultRecord.sharpe)).filter(
                OptimizationResultRecord.execution_id == run.execution_id
            ).scalar()
            compare_rows.append((st, best_pf, best_sharpe))

    print("\nTOP 10 FINAL")
    for i, row in enumerate(top10, start=1):
        print(
            f"{i:02d} | pf={row.profit_factor} sharpe={row.sharpe} expectancy={row.expectancy} "
            f"win_rate={row.win_rate} trades={row.trades} drawdown={row.drawdown}"
        )

    print("\n[4/4] Relatorio executivo")
    approved = bool(validation and validation.approved and validation.approved > 0)
    decision = "APROVADA" if approved else "REPROVADA"
    print(f"Decisao final: {decision}")
    if validation:
        print(
            f"Validation: tested={validation.total_tested} approved={validation.approved} rejected={validation.rejected}"
        )
    if strategy_lab_result:
        print(f"Strategy Lab: {json.dumps(strategy_lab_result.get('summary', {}), ensure_ascii=False)}")
    elif strategy_lab_error:
        print(f"Strategy Lab: erro={strategy_lab_error}")
    if trade_lab_result:
        print(f"Trade Mgmt Lab: {json.dumps(trade_lab_result.get('summary', {}), ensure_ascii=False)}")
    elif trade_lab_error:
        print(f"Trade Mgmt Lab: erro={trade_lab_error}")

    print("Comparacao com outras estrategias (ultimo run):")
    for st, pf, sharpe in compare_rows:
        print(f"- {st}: best_pf={pf} best_sharpe={sharpe}")


def cmd_optimize(args: argparse.Namespace) -> None:
    from optimizer.optimizer import OptimizerRunConfig, StrategyOptimizer
    from notifications.notification_service import get_notification_service
    from notifications.telegram_listener import make_telegram_listener

    history_listener = HistoryListener(checkpoint_interval=settings.optimizer.checkpoint_interval)
    metrics_listener = MetricsListener()
    notification_service = get_notification_service()
    notification_service.start()
    event_bus = EventBus(
        listeners=[history_listener, LogListener(), metrics_listener, make_telegram_listener()],
        async_dispatch=False,
    )

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    start = _parse_date(args.start) or datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = _parse_date(args.end)

    optimizer = StrategyOptimizer(
        event_bus=event_bus,
        checkpoint_interval=settings.optimizer.checkpoint_interval,
    )
    execution_id = args.resume_execution_id or HistoryPersistenceService.new_execution_id()

    resume_state = history_listener.resume_execution(execution_id)
    resume_from = resume_state.processed if resume_state and not resume_state.completed else 0
    if args.resume_execution_id and resume_state is None:
        raise SystemExit(f"Resume requested but no checkpoint found for execution_id={execution_id}")
    try:
        summary = optimizer.run(
            OptimizerRunConfig(
                symbol=symbol,
                timeframe=timeframe,
                start=start,
                end=end,
                capital=args.capital,
                top_n=args.top,
                workers=args.workers,
                max_combinations=args.max_combinations,
                diagnostic=args.diagnostic,
                execution_id=execution_id,
                resume_from=resume_from,
                checkpoint_interval=settings.optimizer.checkpoint_interval,
                strategy_name=settings.trading.strategy,
                strategy_version=os.getenv("STRATEGY_VERSION", "v1"),
                git_commit=os.getenv("GIT_COMMIT"),
                host=platform.node(),
                cpu=platform.processor() or None,
                python_version=platform.python_version(),
            )
        )
    except ValueError as exc:
        logger.error("Optimization aborted: %s", exc)
        raise SystemExit(1) from exc
    finally:
        notification_service.stop()

    print("\n======================================")
    print("OPTIMIZATION REPORT")
    print("======================================")
    print(f"Combinacoes testadas: {summary.combinations_tested}")
    print(f"Combinacoes descartadas: {summary.combinations_discarded}")
    print(f"Tempo total da otimizacao: {summary.duration_seconds:.2f}s")
    if summary.best_profit_factor:
        print("Melhor Profit Factor:")
        print(summary.best_profit_factor)
    if summary.best_net_profit:
        print("Melhor Lucro:")
        print(summary.best_net_profit)
    if summary.lowest_drawdown:
        print("Menor Drawdown:")
        print(summary.lowest_drawdown)
    if summary.best_sharpe:
        print("Melhor Sharpe:")
        print(summary.best_sharpe)
    print("======================================")
    print("TOP 10")
    for result in summary.top_results[:10]:
        print(result)
    print("======================================")
    print("Arquivos gerados:")
    for output_file in summary.output_files:
        print(f"- {output_file}")

    print("REAL-TIME METRICS")
    print(metrics_listener.state)

    # Save only an export checkpoint here; per-combination results are already persisted by listeners.
    with get_session() as session:
        history = HistoryPersistenceService(session)
        history.save_checkpoint(
            execution_id=execution_id,
            stage="optimization_export",
            processed=len(summary.top_results),
            completed=True,
            payload={"symbol": symbol, "timeframe": timeframe},
        )

    _run_validation_for_execution(
        execution_id=execution_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy_name=settings.trading.strategy,
        capital=args.capital,
        top_n=args.top,
        train_start=_parse_date(args.train_start),
        train_end=_parse_date(args.train_end),
        val_start=_parse_date(args.val_start),
        val_end=_parse_date(args.val_end),
    )
    _run_post_validation_pipeline(
        execution_id=execution_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy_name=settings.trading.strategy,
    )


def cmd_validate(args: argparse.Namespace) -> None:
    from database.history_models import OptimizationRun

    with get_session() as session:
        run = session.query(OptimizationRun).filter(OptimizationRun.execution_id == args.execution_id).one_or_none()

    if run is None:
        raise SystemExit(f"Execution id not found in optimization_runs: {args.execution_id}")

    symbol = args.symbol or run.symbol
    timeframe = args.timeframe or run.timeframe
    symbol, timeframe = _canonical_market(symbol, timeframe)

    _run_validation_for_execution(
        execution_id=args.execution_id,
        symbol=symbol,
        timeframe=timeframe,
        strategy_name=run.strategy,
        capital=args.capital,
        top_n=args.top,
        train_start=_parse_date(args.train_start),
        train_end=_parse_date(args.train_end),
        val_start=_parse_date(args.val_start),
        val_end=_parse_date(args.val_end),
    )


def cmd_execution_manager(_args: argparse.Namespace) -> None:
    from execution_manager.manager import ExecutionManager
    from research.services.pipeline_executor import execute_pipeline

    base_dir = Path(__file__).parent
    if _args.pipeline:
        execute_pipeline(base_dir=base_dir, pipeline_path=Path(_args.pipeline))
        return

    manager = ExecutionManager(base_dir, execution_id=_args.execution_id)
    rc = manager.run()
    if rc != 0:
        raise SystemExit(rc)


def cmd_execution_manager_rc1(_args: argparse.Namespace) -> None:
    from execution_manager.validation_rc1 import run_rc1_validation

    payload = run_rc1_validation(Path(__file__).parent)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("recommendation") != "APROVADO":
        raise SystemExit(1)


def cmd_strategy_research_lab(args: argparse.Namespace) -> None:
    from research.services.strategy_research_lab import ResearchLabConfig, StrategyResearchLab

    strategies = [item.strip() for item in args.strategies.split(",") if item.strip()]
    if not strategies:
        raise SystemExit("At least one strategy must be informed in --strategies")

    symbol = validate_symbol(args.symbol) if args.symbol else None
    timeframe = validate_timeframe(args.timeframe) if args.timeframe else None

    with get_session() as session:
        lab = StrategyResearchLab(session=session, base_dir=Path(__file__).parent)
        result = lab.run(
            ResearchLabConfig(
                strategies=strategies,
                symbol=symbol,
                timeframe=timeframe,
                start=_parse_date(args.start),
                end=_parse_date(args.end),
                horizon_bars=max(1, int(args.horizon_bars)),
                max_candidates_per_strategy=max(1, int(args.max_candidates_per_strategy)),
            )
        )

    print("\n======================================")
    print("STRATEGY RESEARCH LAB")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_trade_lifecycle_audit(args: argparse.Namespace) -> None:
    from trade_lifecycle_audit import TradeLifecycleAuditConfig, TradeLifecycleAuditService

    service = TradeLifecycleAuditService(base_dir=Path(__file__).parent)
    result = service.run(
        TradeLifecycleAuditConfig(
            strategy_name=str(args.strategy_name) if args.strategy_name else None,
            strategy_version=str(args.strategy_version) if args.strategy_version else None,
            symbol=str(args.symbol) if args.symbol else None,
            timeframe=str(args.timeframe) if args.timeframe else None,
            execution_id=str(args.execution_id) if args.execution_id else None,
            window_days=max(1, int(args.window_days)),
            output_prefix=str(args.output_prefix),
            persist_to_db=not bool(args.no_db),
        )
    )

    print("\n======================================")
    print("TRADE LIFECYCLE AUDIT — FASE 9.3")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        s7 = report.get("stage7_diagnosis", {})
        print("\n--- BOTTLENECK SCORES ---")
        for k, v in s7.get("bottleneck_scores", {}).items():
            print(f"  {k}: {v:.1f}")
        print(f"\nGARGALO PRINCIPAL: {s7.get('main_bottleneck', 'N/A').upper()}")
        print("\nEvidências:")
        for ev in s7.get("evidence", []):
            print(f"  - {ev}")
        print("\nRecomendações:")
        for i, rec in enumerate(s7.get("recommendation", []), start=1):
            print(f"  {i}. {rec}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_phase9_4_controlled_improvement(args: argparse.Namespace) -> None:
    from phase94_controlled_improvement import Phase94Config, Phase94ControlledImprovementService

    service = Phase94ControlledImprovementService(base_dir=Path(__file__).parent)
    result = service.run(
        Phase94Config(
            symbol=str(args.symbol) if args.symbol else None,
            timeframe=str(args.timeframe) if args.timeframe else None,
            start=_parse_date(args.start),
            end=_parse_date(args.end),
            capital=max(100.0, float(args.capital)),
            window_days=max(1, int(args.window_days)),
            output_prefix=str(args.output_prefix),
            run_paper_campaign_if_approved=not bool(args.skip_paper_campaign),
            paper_cycles=max(1, int(args.paper_cycles)),
        )
    )

    print("\n======================================")
    print("FASE 9.4 - CONTROLLED IMPROVEMENT")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        comp = report.get("comparison", {})
        print("\nDecision:")
        print(f"- A V1.1 melhorou a V1.0? {comp.get('v11_better_than_v10', 'Não')}")
        print(f"- Métricas melhoradas: {', '.join(comp.get('improved_metrics', [])) or 'Nenhuma'}")
        print(f"- Métricas pioradas: {', '.join(comp.get('worsened_metrics', [])) or 'Nenhuma'}")
        print(f"- Estratégia pronta para Paper Trading? {comp.get('ready_for_paper_trading', 'Não')}")
        print(f"- Recomendação: {comp.get('recommendation', 'Reverter para V1.0')}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_strategy_catalog_cycle(args: argparse.Namespace) -> None:
    from strategy_catalog import StrategyCatalogCycleConfig, StrategyCatalogCycleService

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    service = StrategyCatalogCycleService(base_dir=Path(__file__).parent)
    result = service.run(
        StrategyCatalogCycleConfig(
            symbol=symbol,
            timeframe=timeframe,
            window_days=max(30, int(args.window_days)),
            initial_capital=max(100.0, float(args.capital)),
            max_catalog_strategies=max(1, min(20, int(args.max_catalog_strategies))),
            optimizer_max_combinations=max(5, int(args.optimizer_max_combinations)),
            optimizer_workers=max(1, int(args.optimizer_workers)),
            top_k_for_paper=max(1, min(3, int(args.top_k_for_paper))),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 10 - STRATEGY CATALOG CYCLE")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        print("\nTop 3 para Paper Trading:")
        for idx, row in enumerate(report.get("top3_for_paper", []), start=1):
            print(f"  {idx}. {row.get('strategy')} ({row.get('category')})")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_strategy_catalog_audit(args: argparse.Namespace) -> None:
    from strategy_catalog import StrategyCatalogAuditConfig, StrategyCatalogAuditService

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    benchmark_symbols = tuple(
        _canonical_market(item.strip(), timeframe)[0]
        for item in str(args.benchmark_symbols).split(",")
        if item.strip()
    )
    benchmark_timeframes = tuple(
        validate_timeframe(item.strip())
        for item in str(args.benchmark_timeframes).split(",")
        if item.strip()
    )

    service = StrategyCatalogAuditService(base_dir=Path(__file__).parent)
    result = service.run(
        StrategyCatalogAuditConfig(
            symbol=symbol,
            timeframe=timeframe,
            window_days=max(30, int(args.window_days)),
            initial_capital=max(100.0, float(args.capital)),
            benchmark_symbols=benchmark_symbols or (symbol,),
            benchmark_timeframes=benchmark_timeframes or (timeframe,),
            optimizer_max_combinations=max(3, int(args.optimizer_max_combinations)),
            optimizer_workers=max(1, int(args.optimizer_workers)),
            max_bars=max(500, int(args.max_bars)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 10.2 - STRATEGY CATALOG AUDIT")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        print("\nDecision:")
        print(f"- Recomendacao final: {report.get('recommendation')}")
        print(f"- Justificativa: {report.get('recommendation_reason')}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_crypto_strategy_research(args: argparse.Namespace) -> None:
    from research.crypto_strategy_knowledge_base import (
        CryptoStrategyKnowledgeBaseService,
        CryptoStrategyResearchConfig,
    )

    service = CryptoStrategyKnowledgeBaseService(base_dir=Path(__file__).parent)
    result = service.run(
        CryptoStrategyResearchConfig(
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 11 - CRYPTO STRATEGY RESEARCH")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_supertrend_controlled_implementation(args: argparse.Namespace) -> None:
    from phase12_supertrend_controlled import Phase12SuperTrendConfig, Phase12SuperTrendService

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    service = Phase12SuperTrendService(base_dir=Path(__file__).parent)
    result = service.run(
        Phase12SuperTrendConfig(
            symbol=symbol,
            timeframe=timeframe,
            start=_parse_date(args.start),
            end=_parse_date(args.end),
            capital=max(100.0, float(args.capital)),
            window_days=max(30, int(args.window_days)),
            output_prefix=str(args.output_prefix),
            max_bars=max(500, int(args.max_bars)),
            optimizer_max_combinations=max(5, int(args.optimizer_max_combinations)),
            optimizer_workers=max(1, int(args.optimizer_workers)),
            run_paper_campaign_if_approved=not bool(args.skip_paper_campaign),
            paper_cycles=max(1, int(args.paper_cycles)),
        )
    )

    print("\n======================================")
    print("FASE 12 - SUPER TREND CONTROLLED IMPLEMENTATION")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        print("\nDecision:")
        print(f"- Decisao final: {report.get('decision', 'OPCAO B')}")
        print(f"- Early stop acionado: {'Sim' if report.get('early_stop', {}).get('triggered') else 'Nao'}")
        print(f"- Aprovada para Paper: {'Sim' if report.get('status') == 'approved_for_paper' else 'Nao'}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_continuous_strategy_factory(args: argparse.Namespace) -> None:
    from research.services.phase13_continuous_strategy_factory import (
        ContinuousStrategyFactoryService,
        Phase13ContinuousFactoryConfig,
    )

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    service = ContinuousStrategyFactoryService(base_dir=Path(__file__).parent)
    result = service.run(
        Phase13ContinuousFactoryConfig(
            symbol=symbol,
            timeframe=timeframe,
            window_days=max(10, int(args.window_days)),
            capital=max(100.0, float(args.capital)),
            batch_size=max(1, int(args.batch_size)),
            target_approved=max(1, int(args.target_approved)),
            max_bars=max(500, int(args.max_bars)),
            optimizer_max_combinations=max(5, int(args.optimizer_max_combinations)),
            optimizer_workers=max(1, int(args.optimizer_workers)),
            max_strategy_runtime_seconds=max(30, int(args.max_strategy_runtime_seconds)),
            max_cpu_per_worker_pct=max(1.0, min(100.0, float(args.max_cpu_per_worker_pct))),
            campaign_max_seconds=max(0, int(args.campaign_max_seconds)),
            optimizer_probe_enabled=True,
            probe_max_combinations=max(5, min(10, int(args.probe_max_combinations))),
            probe_top_n=max(1, min(3, int(args.probe_top_n))),
            reprocess_implemented_catalog=True,
            paper_candidate_min_trades=max(1, int(args.paper_candidate_min_trades)),
            paper_candidate_min_profit_factor=max(0.0, float(args.paper_candidate_min_profit_factor)),
            paper_candidate_min_expectancy=float(args.paper_candidate_min_expectancy),
            paper_candidate_allow_overfitting=bool(args.paper_candidate_allow_overfitting),
            paper_experiment_max_cycles=max(1, int(args.paper_experiment_max_cycles)),
            paper_experiment_poll_seconds=max(1.0, float(args.paper_experiment_poll_seconds)),
            paper_experiment_bootstrap_bars=max(200, int(args.paper_experiment_bootstrap_bars)),
            paper_experiment_bootstrap_replay_bars=max(60, int(args.paper_experiment_bootstrap_replay_bars)),
            paper_experiment_review_window_days=max(1, int(args.paper_experiment_review_window_days)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 13 - CONTINUOUS STRATEGY FACTORY")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        print("\nFinal report:")
        print(f"- Estrategias implementadas: {report.get('implemented_count', 0)}")
        print(f"- Estrategias reprovadas por desempenho: {report.get('rejected_performance_count', 0)}")
        print(f"- Estrategias reprovadas por infraestrutura: {report.get('rejected_infrastructure_count', 0)}")
        print(f"- Estrategias inconclusivas: {report.get('inconclusive_count', 0)}")
        print(f"- Estrategias aprovadas: {report.get('approved_count', 0)}")
        print(f"- Estrategias em Paper Trading: {report.get('in_paper_trading_count', 0)}")
        print(f"- Estrategias em PAPER_CANDIDATE: {report.get('paper_candidate_count', 0)}")
        print(f"- Tempo medio por estrategia (s): {report.get('average_time_per_strategy_seconds', 0.0)}")
        print(f"- Tempo total da campanha (s): {report.get('total_campaign_seconds', 0.0)}")
        print(f"- Stop reason: {report.get('stop_reason', 'unknown')}")
        print("- Principais motivos de reprovacao:")
        for reason, count in report.get("top_rejection_reasons", []):
            print(f"  - {reason}: {count}")
        print(f"- Ranking atualizado: {len(report.get('ranking_updated', []))} linhas")

        answers = report.get("answers", {})
        if answers:
            print("\nRespostas obrigatorias:")
            print(f"- Estrategias pesquisadas: {answers.get('strategies_researched', 0)}")
            print(f"- Estrategias implementadas nesta campanha: {answers.get('implemented_in_campaign', 0)}")
            print(f"- Estrategias efetivamente avaliadas: {answers.get('effectively_evaluated', 0)}")
            print(f"- Estrategias reprovadas por desempenho: {answers.get('rejected_performance', 0)}")
            print(f"- Estrategias reprovadas por infraestrutura: {answers.get('rejected_infrastructure', 0)}")
            print(f"- Estrategias aprovadas: {answers.get('approved', 0)}")
            print(f"- Estrategias em Paper Trading: {answers.get('in_paper_trading', 0)}")
            print(f"- Estrategias em PAPER_CANDIDATE: {answers.get('classified_paper_candidate', 0)}")
            print(f"- Estrategias ainda pendentes: {answers.get('pending', 0)}")
            print(f"- Cobertura antes (%): {answers.get('coverage_before_pct', 0.0)}")
            print(f"- Cobertura depois (%): {answers.get('coverage_after_pct', 0.0)}")

            print("\nConsolidado 13.4:")
            print(f"- Reprocessadas: {answers.get('reprocessed_strategies', 0)}")
            print(f"- Passaram no Optimizer Probe: {answers.get('passed_optimizer_probe', 0)}")
            print(f"- Continuaram eliminadas: {answers.get('continued_eliminated_after_probe', 0)}")
            print(f"- Chegaram ao Optimizer completo: {answers.get('reached_full_optimizer', 0)}")
            print(f"- Chegaram ao Validation: {answers.get('reached_validation', 0)}")
            print(f"- Chegaram ao Paper Qualification: {answers.get('reached_paper_qualification', 0)}")
            print(f"- Chegaram ao Paper Trading: {answers.get('reached_paper_trading', 0)}")
            print(f"- Classificadas como PAPER_CANDIDATE: {answers.get('classified_paper_candidate', 0)}")
            print(f"- Ganho medio PF apos Probe (%): {answers.get('avg_probe_gain_profit_factor_pct', 0.0)}")
            print(f"- Ganho medio Sharpe apos Probe (%): {answers.get('avg_probe_gain_sharpe_pct', 0.0)}")
            print(f"- Mudaram de decisao vs pipeline antigo: {answers.get('changed_decision_vs_old_pipeline', 0)}")

            print("\nValidacao cientifica:")
            print(f"- O Optimizer Probe reduziu falsos negativos? {answers.get('optimizer_probe_reduced_false_negatives', 'NAO')}")
            print(f"- O Optimizer Probe alterou criterios cientificos? {answers.get('optimizer_probe_altered_scientific_criteria', 'NAO')}")
            print(
                "- O Optimizer Probe apenas deu oportunidade minima de parametrizacao antes da decisao? "
                f"{answers.get('optimizer_probe_only_minimal_param_opportunity', 'SIM')}"
            )

        stage_counters = report.get("stage_counters", {})
        if stage_counters:
            print("\nValidacao final:")
            print(f"- O backlog foi realmente consumido? {'SIM' if int(stage_counters.get('implementation_real', 0)) > 0 else 'NAO'}")
            print(f"- Quantas estrategias passaram por implementacao real? {stage_counters.get('implementation_real', 0)}")
            print(f"- Quantas chegaram ao Backtest? {stage_counters.get('backtest_reached', 0)}")
            print(f"- Quantas chegaram ao Optimizer? {stage_counters.get('optimizer_reached', 0)}")
            print(f"- Quantas chegaram ao Validation? {stage_counters.get('validation_reached', 0)}")
            print(f"- Quantas chegaram ao Paper Qualification? {stage_counters.get('paper_qualification_reached', 0)}")
            print(f"- Existe agora alguma estrategia apta para Paper Trading? {'SIM' if report.get('in_paper_trading_count', 0) > 0 else 'NAO'}")

        answer = report.get("answer", {})
        print(f"\n{answer.get('question', 'Pergunta')}")
        print(answer.get("value", 0))

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_market_intelligence(args: argparse.Namespace) -> None:
    from research.services.phase14_market_intelligence import (
        MarketIntelligenceService,
        Phase14MarketIntelligenceConfig,
    )

    service = MarketIntelligenceService(base_dir=Path(__file__).parent)
    result = service.run(
        Phase14MarketIntelligenceConfig(
            top_n=max(5, int(args.top_n)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 14 - MARKET INTELLIGENCE")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    if report:
        print("\nFinal report:")
        print(f"- Novas estrategias pesquisadas: {report.get('total_researched', 0)}")
        print(f"- Descartadas por incompatibilidade: {report.get('total_incompatible', 0)}")
        print(f"- Descartadas por duplicidade: {report.get('total_duplicates', 0)}")
        print(f"- Eliminadas (outros motivos): {report.get('total_eliminated', 0)}")
        print(f"- Adicionadas ao backlog: {report.get('total_classified', 0)}")
        print(f"- Top 20: {len(report.get('top20', []))}")
        print(f"- Top 10: {len(report.get('top10', []))}")
        print(f"- Top 5: {len(report.get('top5', []))}")
        print("- Distribuicao por categoria:")
        for category, count in report.get("distribution_by_category", {}).items():
            print(f"  - {category}: {count}")
        print(f"- Principais indicadores encontrados: {report.get('top_indicators', [])[:5]}")
        print(f"- Principais timeframes encontrados: {report.get('top_timeframes', [])[:5]}")
        print(f"- Principais ativos encontrados: {report.get('top_assets', [])[:5]}")
        print(f"- Estrategia mais promissora: {result.get('summary', {}).get('top_strategy')}")
        incompatible_names = [x.get('name') for x in report.get('incompatible', [])]
        if incompatible_names:
            print(f"- Incompativeis (dados indisponiveis): {incompatible_names}")
        dup_names = [x.get('name') for x in report.get('duplicates', [])]
        if dup_names:
            print(f"- Duplicatas detectadas: {dup_names}")
        decision = report.get("decision", {})
        print(f"\n{decision.get('question', 'Pergunta')}")
        print(decision.get("answer", "NAO"))
        if decision.get("answer") == "NAO":
            print(f"Categorias faltantes: {decision.get('missing_categories_if_no', [])}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_overnight_campaign(args: argparse.Namespace) -> None:
    """
    Overnight campaign V2 autonomous orchestrator:
    1. Process IMPLEMENTATION_PENDING/INCOMPLETE first
    2. Refill backlog from Phase 14 automatically when queue is empty
    3. Never stop only because PAPER_CANDIDATE was found
    4. Continue until 09:00 / manual stop / resource or safety limit
    """
    from research.services.phase13_continuous_strategy_factory import (
        ContinuousStrategyFactoryService,
        Phase13ContinuousFactoryConfig,
    )

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)

    precheck = _run_overnight_campaign_precheck(
        symbol=symbol,
        timeframe=timeframe,
        min_candles=max(1, int(args.precheck_min_candles or 1000)),
    )
    _print_overnight_campaign_precheck(precheck)
    if str(precheck.get("status", "failed")).lower() != "ok":
        print("\nCampanha cancelada.")
        return

    service = ContinuousStrategyFactoryService(base_dir=Path(__file__).parent)
    
    # Overnight campaign settings
    overnight_config = Phase13ContinuousFactoryConfig(
        symbol=symbol,
        timeframe=timeframe,
        window_days=max(10, int(args.window_days or 120)),
        capital=max(100.0, float(args.capital or 10000.0)),
        batch_size=max(1, int(args.batch_size or 50)),  # Allow many strategies
        target_approved=max(1, int(args.target_approved or 1)),  # Original target
        target_paper_candidates=max(1, int(args.target_paper_candidates or 3)),  # NEW: 3 candidates instead of 1
        max_bars=max(500, int(args.max_bars or 3500)),
        optimizer_max_combinations=max(5, int(args.optimizer_max_combinations or 15)),
        optimizer_workers=max(1, int(args.optimizer_workers or 1)),
        max_strategy_runtime_seconds=max(30, int(args.max_strategy_runtime_seconds or 900)),
        max_cpu_per_worker_pct=max(1.0, min(100.0, float(args.max_cpu_per_worker_pct or 100.0))),
        campaign_max_seconds=max(0, int(args.campaign_max_seconds or 0)),
        campaign_end_hour=int(args.campaign_end_hour or 9),  # Stop at 09:00
        auto_research_when_queue_empty=not bool(args.disable_auto_research),
        phase14_top_n=max(5, int(args.phase14_top_n or 30)),
        stop_on_target_paper_candidates=False,
        checkpoint_interval_seconds=max(60, int(args.checkpoint_interval_seconds or 600)),
        optimizer_probe_enabled=True,
        probe_max_combinations=max(5, min(10, int(args.probe_max_combinations or 8))),
        probe_top_n=max(1, min(3, int(args.probe_top_n or 3))),
        reprocess_implemented_catalog=True,
        paper_candidate_min_trades=max(1, int(args.paper_candidate_min_trades or 100)),
        paper_candidate_min_profit_factor=max(0.0, float(args.paper_candidate_min_profit_factor or 1.10)),
        paper_candidate_min_expectancy=float(args.paper_candidate_min_expectancy or 0.0),
        paper_candidate_allow_overfitting=bool(args.paper_candidate_allow_overfitting or False),
        paper_experiment_max_cycles=max(1, int(args.paper_experiment_max_cycles or 1)),
        paper_experiment_poll_seconds=max(1.0, float(args.paper_experiment_poll_seconds or 2.0)),
        paper_experiment_bootstrap_bars=max(200, int(args.paper_experiment_bootstrap_bars or 1500)),
        paper_experiment_bootstrap_replay_bars=max(60, int(args.paper_experiment_bootstrap_replay_bars or 350)),
        paper_experiment_review_window_days=max(1, int(args.paper_experiment_review_window_days or 14)),
        output_prefix=str(args.output_prefix or "overnight_campaign"),
    )
    
    print("\n" + "="*60)
    print("OVERNIGHT CAMPAIGN - INTELLIGENT STRATEGY DISCOVERY")
    print("="*60)
    print(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Target time: 09:00 (local time)")
    print(f"Target PAPER_CANDIDATE count (informativo): {overnight_config.target_paper_candidates}")
    print(f"Priority: IMPLEMENTATION_PENDING first, then new research")
    print(f"Auto-research Phase 14: {'ON' if overnight_config.auto_research_when_queue_empty else 'OFF'}")
    print(f"Checkpoint interval (s): {overnight_config.checkpoint_interval_seconds}")
    print("="*60 + "\n")
    
    result = service.run(overnight_config)

    print("\n======================================")
    print("OVERNIGHT CAMPAIGN - FINAL REPORT")
    print("======================================")
    
    summary = result.get("summary", {})
    print("\nExecutive Summary:")
    print(f"- Status: {summary.get('status', 'unknown')}")
    print(f"- Stop reason: {summary.get('stop_reason', 'unknown')}")
    print(f"- Duration: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    report = result.get("report", {})
    if report:
        print("\nCampanha Noturna - Consolidado Final:")
        print(f"- Estrategias pesquisadas: {report.get('coverage_after', {}).get('strategies_researched', 0)}")
        print(f"- Estrategias implementadas: {report.get('implemented_count', 0)}")
        print(f"- Estrategias avaliadas (backtest reached): {report.get('stage_counters', {}).get('backtest_reached', 0)}")
        print(f"- Estrategias reprovadas: {report.get('rejected_count', 0)}")
        print(f"- Estrategias em PAPER_CANDIDATE: {report.get('paper_candidate_count', 0)}")
        print(f"- Estrategias em Paper Trading: {report.get('in_paper_trading_count', 0)}")
        print(f"- Paper Experimental iniciados: {report.get('paper_experimental_started_count', 0)}")
        print(f"- Estrategias pendentes: {report.get('pending_count', 0)}")
        
        print("\nEtapas do Pipeline:")
        sc = report.get("stage_counters", {})
        print(f"- Smoke Test: {sc.get('reprocessed_total', 0)}")
        print(f"- Backtest: {sc.get('backtest_reached', 0)}")
        print(f"- Optimizer Probe: {sc.get('optimizer_probe_reached', 0)}")
        print(f"- Optimizer Completo: {sc.get('optimizer_reached', 0)}")
        print(f"- Validation: {sc.get('validation_reached', 0)}")
        print(f"- Paper Qualification: {sc.get('paper_qualification_reached', 0)}")
        
        print("\nMotivos de Reprovação (Top 10):")
        for reason, count in report.get("top_rejection_reasons", [])[:10]:
            print(f"  - {reason}: {count}")

        overnight = report.get("overnight_v2", {})
        audit = overnight.get("paper_candidate_promotion_rule_audit", {}) if isinstance(overnight, dict) else {}
        print("\nAuditoria PAPER_CANDIDATE:")
        print(
            "- PAPER_CANDIDATE deve iniciar automaticamente Paper Experimental? "
            f"{overnight.get('paper_candidate_should_auto_start_paper_experimental', 'SIM')}"
        )
        print(f"- PAPER_CANDIDATE sem Paper Experimental: {audit.get('paper_candidate_without_experimental_count', 0)}")
        
        # Aprendizado contínuo
        rejection_knowledge = report.get("rejection_knowledge", {})
        if rejection_knowledge:
            print("\nAprendizado Contínuo - Famílias com Maior Taxa de Reprovação:")
            families = rejection_knowledge.get("family", {})
            for family_name, data in sorted(families.items(), key=lambda x: x[1]["count"], reverse=True)[:5]:
                print(f"  - {family_name}: {data['count']} reprovacoes")
        
        print(f"\nTempo total: {report.get('total_campaign_seconds', 0.0):.1f}s")
        print(f"Tempo médio por estratégia: {report.get('average_time_per_strategy_seconds', 0.0):.1f}s")
        
        # Top 20
        ranking = report.get("ranking_updated", [])
        if ranking:
            print("\nTop 20 Estratégias:")
            for item in ranking[:20]:
                status_str = item.get("state", "unknown")
                score = item.get("queue_score", 0.0)
                print(f"  {item['rank']:2d}. {item['candidate_name']:40s} [{status_str:25s}] score={score:.4f}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")
    
    print(f"\nEnd time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)


def _run_overnight_campaign_precheck(
    *,
    symbol: str,
    timeframe: str,
    min_candles: int,
    database_url: str | None = None,
) -> dict[str, Any]:
    db_url = database_url or settings.database.url
    parsed = make_url(db_url)
    backend = parsed.get_backend_name().strip().lower()

    report: dict[str, Any] = {
        "status": "ok",
        "db": {
            "url": db_url,
            "type": backend,
            "user": parsed.username or "N/A",
            "schema": parsed.database or "N/A",
            "connection_ok": False,
        },
        "config": {
            "exchange": settings.trading.exchange,
            "mode": settings.trading.mode,
            "symbol": symbol,
            "timeframe": timeframe,
            "min_candles": int(max(1, min_candles)),
        },
        "candles": {
            "total": 0,
            "symbol_count": 0,
            "timeframe_count": 0,
            "symbol_timeframe_count": 0,
            "first_open_time": None,
            "last_open_time": None,
        },
        "history": {
            "trade_history": 0,
            "execution_sessions": 0,
            "checkpoints": 0,
            "optimization_results": 0,
        },
        "strategy": {
            "eligible_count": 0,
        },
        "warnings": [],
        "fail_reasons": [],
    }

    config_failures: list[str] = []
    if not settings.trading.exchange:
        config_failures.append("Exchange ausente")
    if not settings.trading.mode:
        config_failures.append("Modo ausente")
    if not db_url:
        config_failures.append("Banco ausente")
    if not symbol:
        config_failures.append("Simbolo ausente")
    if not timeframe:
        config_failures.append("Timeframe ausente")

    if config_failures:
        report["status"] = "failed"
        report["fail_reasons"].extend(config_failures)
        return report

    db = DatabaseConnection(db_url)
    try:
        with db.session() as session:
            session.execute(text("SELECT 1"))
            report["db"]["connection_ok"] = True

            total = int(session.query(Candle).count())
            symbol_count = int(session.query(Candle).filter(Candle.symbol == symbol).count())
            timeframe_count = int(session.query(Candle).filter(Candle.timeframe == timeframe).count())

            scoped_query = session.query(Candle).filter(Candle.symbol == symbol, Candle.timeframe == timeframe)
            scoped_count = int(scoped_query.count())
            first_candle = scoped_query.order_by(Candle.open_time.asc()).first()
            last_candle = scoped_query.order_by(Candle.open_time.desc()).first()

            report["candles"].update(
                {
                    "total": total,
                    "symbol_count": symbol_count,
                    "timeframe_count": timeframe_count,
                    "symbol_timeframe_count": scoped_count,
                    "first_open_time": first_candle.open_time.isoformat() if first_candle else None,
                    "last_open_time": last_candle.open_time.isoformat() if last_candle else None,
                }
            )

            counts_sql = {
                "trade_history": "SELECT COUNT(*) FROM trade_history",
                "execution_sessions": "SELECT COUNT(*) FROM execution_sessions",
                "checkpoints": "SELECT COUNT(*) FROM execution_checkpoints",
                "optimization_results": "SELECT COUNT(*) FROM optimization_results_history",
            }
            for key, query in counts_sql.items():
                try:
                    count = int(session.execute(text(query)).scalar_one())
                    report["history"][key] = count
                except Exception:
                    report["history"][key] = 0
                    report["warnings"].append(f"Tabela indisponivel para contagem: {key}")
    except Exception as exc:
        report["status"] = "failed"
        report["fail_reasons"].append(f"Falha de conexao com banco: {exc}")
        return report
    finally:
        db.dispose()

    try:
        eligible = list_registered_strategies()
        report["strategy"]["eligible_count"] = len(eligible)
    except Exception as exc:
        report["strategy"]["eligible_count"] = 0
        report["warnings"].append(f"Falha ao carregar estrategias registradas: {exc}")

    if int(report["candles"]["symbol_timeframe_count"]) < int(report["config"]["min_candles"]):
        report["fail_reasons"].append(
            "Candles insuficientes"
            f" (encontrados={report['candles']['symbol_timeframe_count']}"
            f", minimo={report['config']['min_candles']})"
        )

    if int(report["strategy"]["eligible_count"]) <= 0:
        report["fail_reasons"].append("Nenhuma estrategia elegivel encontrada")

    if report["fail_reasons"]:
        report["status"] = "failed"

    for key, count in report["history"].items():
        if int(count) <= 0:
            report["warnings"].append(f"Historico vazio: {key}")

    return report


def _print_overnight_campaign_precheck(report: dict[str, Any]) -> None:
    status = str(report.get("status", "failed")).upper()
    db = report.get("db", {}) if isinstance(report.get("db"), dict) else {}
    cfg = report.get("config", {}) if isinstance(report.get("config"), dict) else {}
    candles = report.get("candles", {}) if isinstance(report.get("candles"), dict) else {}

    print("\nPRECHECK")
    print(f"Banco: {str(db.get('type', 'unknown')).upper()}")
    print(f"Candles: {candles.get('symbol_timeframe_count', 0)}")
    print(f"Simbolo: {cfg.get('symbol', 'N/A')}")
    print(f"Timeframe: {cfg.get('timeframe', 'N/A')}")

    if status == "OK":
        print("Resultado: OK")
        print("Campanha liberada.")
    else:
        print("FAILED")
        reasons = report.get("fail_reasons", []) if isinstance(report.get("fail_reasons"), list) else []
        motivo = reasons[0] if reasons else "precheck_invalid"
        print(f"Motivo: {motivo}")
        print(f"Mínimo necessário: {cfg.get('min_candles', 'N/A')}")


def cmd_phase13_1_audit_strengthening(args: argparse.Namespace) -> None:
    from research.services.phase13_1_audit_strengthening import (
        Phase131AuditConfig,
        Phase131AuditStrengtheningService,
    )

    symbol, timeframe = _canonical_market(args.symbol, args.timeframe)
    service = Phase131AuditStrengtheningService(base_dir=Path(__file__).parent)
    result = service.run(
        Phase131AuditConfig(
            symbol=symbol,
            timeframe=timeframe,
            window_days=max(10, int(args.window_days)),
            capital=max(100.0, float(args.capital)),
            max_bars=max(500, int(args.max_bars)),
            optimizer_max_combinations=max(5, int(args.optimizer_max_combinations)),
            optimizer_workers=max(1, int(args.optimizer_workers)),
            max_pending_to_process=max(1, int(args.max_pending_to_process)),
            mode=str(args.mode),
            max_workers=max(1, int(args.max_workers)),
            max_strategy_runtime_seconds=max(30, int(args.max_strategy_runtime_seconds)),
            max_optimizer_combinations_per_strategy=max(1, int(args.max_optimizer_combinations_per_strategy)),
            max_cpu_per_worker_pct=max(1.0, min(100.0, float(args.max_cpu_per_worker_pct))),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 13.1 - AUDIT STRENGTHENING")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))

    report = result.get("report", {})
    answers = report.get("answers", {})
    if answers and str(args.mode) == "audit":
        print("\nRespostas objetivas:")
        print(f"- Estrategias efetivamente avaliadas: {answers.get('effectively_evaluated', 0)}")
        print(f"- Estrategias apenas sem implementacao: {answers.get('without_implementation', 0)}")
        print(f"- Estrategias reprovadas por desempenho: {answers.get('market_rejections', 0)}")
        print(f"- Estrategias elegiveis para implementacao futura: {answers.get('eligible_for_future_implementation', 0)}")
        print(f"- Estrategias aptas para Paper Trading apos auditoria: {answers.get('paper_ready_after_audit', 0)}")

    if answers and str(args.mode) == "continuous_coverage":
        print("\nRelatorio final FASE 13.2:")
        print(f"- Estrategias pesquisadas: {answers.get('strategies_researched', 0)}")
        print(f"- Tentativas totais na fila: {answers.get('attempts_total', 0)}")
        print(f"- Estrategias implementadas nesta campanha: {answers.get('strategies_implemented_in_campaign', 0)}")
        print(f"- Estrategias avaliadas nesta campanha: {answers.get('strategies_evaluated_in_campaign', 0)}")
        print(f"- Estrategias reprovadas por desempenho: {answers.get('strategies_rejected_performance', 0)}")
        print(f"- Estrategias reprovadas por infraestrutura: {answers.get('strategies_rejected_infrastructure', 0)}")
        print(f"- Estrategias inconclusivas: {answers.get('strategies_inconclusive', 0)}")
        print(f"- Estrategias aprovadas para Paper Trading: {answers.get('strategies_approved_for_paper', 0)}")
        print(f"- Estrategias ainda pendentes: {answers.get('strategies_still_pending', 0)}")
        print(f"- Cobertura antes (%): {answers.get('coverage_before_pct', 0.0)}")
        print(f"- Cobertura apos (%): {answers.get('coverage_after_pct', 0.0)}")
        print(f"- Tempo medio por estrategia (s): {answers.get('average_time_per_strategy_seconds', 0.0)}")
        print(f"- Tempo total da campanha (s): {answers.get('total_campaign_seconds', 0.0)}")
        print(f"- Motivo de parada da fila: {answers.get('queue_stop_reason', 'backlog_exhausted')}")
        print("\nRespostas obrigatorias:")
        print(f"- Quantas novas estrategias foram implementadas? {answers.get('newly_implemented', 0)}")
        print(f"- Quantas foram efetivamente avaliadas? {answers.get('newly_effectively_evaluated', 0)}")
        print(f"- Quantas avancaram para Paper Trading? {answers.get('advanced_to_paper', 0)}")
        print(f"- Quantas continuam pendentes? {answers.get('continues_pending', 0)}")
        print(f"- Existe agora alguma estrategia apta para operacao continua? {answers.get('has_strategy_ready_for_continuous_operation', 'NAO')}")

    print("\nOutputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_trade_management_research_lab(args: argparse.Namespace) -> None:
    from research.services.trade_management_research_lab import TradeManagementLabConfig, TradeManagementResearchLab

    symbol = validate_symbol(args.symbol) if args.symbol else None
    timeframe = validate_timeframe(args.timeframe) if args.timeframe else None

    with get_session() as session:
        lab = TradeManagementResearchLab(session=session, base_dir=Path(__file__).parent)
        result = lab.run(
            TradeManagementLabConfig(
                operations_csv=args.operations_csv,
                symbol=symbol,
                timeframe=timeframe,
                max_bars=max(10, int(args.max_bars)),
                atr_period=max(3, int(args.atr_period)),
                atr_mult=max(0.1, float(args.atr_mult)),
                time_stop_bars=max(1, int(args.time_stop_bars)),
                momentum_fast=max(2, int(args.momentum_fast)),
                momentum_slow=max(3, int(args.momentum_slow)),
                mfe_pullback_ratio=min(0.95, max(0.05, float(args.mfe_pullback_ratio))),
                bootstrap_iterations=max(50, int(args.bootstrap_iterations)),
            )
        )

    print("\n======================================")
    print("TRADE MANAGEMENT RESEARCH LAB")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_strategy_discovery(args: argparse.Namespace) -> None:
    from research.services.strategy_discovery_pipeline import DiscoveryPilotPlan, DiscoveryWeights, run_strategy_discovery_pipeline

    with get_session() as session:
        result = run_strategy_discovery_pipeline(
            session=session,
            base_dir=Path(__file__).parent,
            pilot=DiscoveryPilotPlan(
                symbol=args.pilot_symbol,
                timeframe=args.pilot_timeframe,
                combinations=max(1, int(args.pilot_combinations)),
                workers=max(1, int(args.pilot_workers)),
            ),
            weights=DiscoveryWeights(),
        )

    print("\n======================================")
    print("STRATEGY DISCOVERY PIPELINE")
    print("======================================")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Recommendation Reason:")
    print(result.get("summary", {}).get("recommendation_reason", ""))

    audit = result.get("audit", {})
    table_rows = audit.get("table", [])
    print("\nAUDIT TABLE")
    if table_rows:
        headers = [
            "Family",
            "Status",
            "Discovery Score",
            "Validation",
            "Research",
            "Trade Management",
            "Classification Reason",
            "Recommendation Reason",
        ]
        print(" | ".join(headers))
        print("-" * 180)
        for row in table_rows:
            print(
                " | ".join(
                    [
                        str(row.get("Family", "")),
                        str(row.get("Status", "")),
                        str(row.get("Discovery Score", "")),
                        str(row.get("Validation", "")),
                        str(row.get("Research", "")),
                        str(row.get("Trade Management", "")),
                        str(row.get("Classification Reason", "")),
                        str(row.get("Recommendation Reason", "")),
                    ]
                )
            )
    else:
        print("No audit rows available.")

    print("\nAUDIT JSON")
    print(json.dumps(audit.get("json", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_market_data_expansion(args: argparse.Namespace) -> None:
    from research.services.market_data_expansion import MarketDataExpansionConfig, MarketDataExpansionService

    service = MarketDataExpansionService(Path(__file__).parent)
    result = service.run(
        MarketDataExpansionConfig(
            mode=str(args.mode),
            history_days=max(1, int(args.history_days)),
            dry_run=bool(args.dry_run),
            auto_pipeline=bool(args.auto_pipeline),
            continuous=bool(args.continuous),
            continuous_max_cycles=max(1, int(args.continuous_max_cycles)),
        )
    )

    if result.get("mode") == "continuous":
        print("\n======================================")
        print("HISTORICAL DATA PLATFORM (CONTINUOUS)")
        print("======================================")
        print(json.dumps(result.get("runs", []), ensure_ascii=False, indent=2, default=str))
        last_result = result.get("last_result") or {}
        print("\nLAST GATE")
        print(json.dumps(last_result.get("gate", {}), ensure_ascii=False, indent=2, default=str))
        print("\nLAST OUTPUTS")
        for key, value in (last_result.get("outputs", {}) or {}).items():
            print(f"- {key}: {value}")
        return

    print("\n======================================")
    print("MARKET DATA EXPANSION")
    print("======================================")
    print(json.dumps(result.get("metrics", {}), ensure_ascii=False, indent=2, default=str))
    print("\nGATE")
    print(json.dumps(result.get("gate", {}), ensure_ascii=False, indent=2, default=str))
    print("\nQUALITY")
    print(json.dumps(result.get("quality", {}), ensure_ascii=False, indent=2, default=str))
    print("\nGROWTH")
    growth = {
        "candles_delta": result.get("after", {}).get("overall", {}).get("total_candles", 0) - result.get("before", {}).get("overall", {}).get("total_candles", 0),
        "days_delta": result.get("after", {}).get("overall", {}).get("days_available", 0) - result.get("before", {}).get("overall", {}).get("days_available", 0),
        "missing_candles_delta": result.get("after", {}).get("overall", {}).get("missing_candles", 0) - result.get("before", {}).get("overall", {}).get("missing_candles", 0),
    }
    print(json.dumps(growth, ensure_ascii=False, indent=2, default=str))
    if result.get("previous_quantitative") is not None:
        print("\nPREVIOUS QUANTITATIVE")
        previous = result["previous_quantitative"]
        compact_previous = {
            "recommended_family": previous.get("recommendation", {}).get("family"),
            "recommended_hypothesis": previous.get("recommendation", {}).get("hypothesis_id"),
            "dataset_size": previous.get("dataset_size"),
            "cluster_count": len(previous.get("cluster_metrics", [])),
            "hypothesis_count": len(previous.get("hypotheses", [])),
        }
        print(json.dumps(compact_previous, ensure_ascii=False, indent=2, default=str))
    if result.get("pipeline") is not None:
        print("\nAUTO PIPELINE")
        pipeline = result["pipeline"]
        compact_pipeline = {
            "requested": pipeline.get("requested"),
            "executed": pipeline.get("executed"),
            "blocked": pipeline.get("blocked"),
            "reason": pipeline.get("reason"),
            "h1_decision": (pipeline.get("h1_audit") or {}).get("decision"),
        }
        print(json.dumps(compact_pipeline, ensure_ascii=False, indent=2, default=str))
    print("\nOUTPUTS")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_market_data_daemon(args: argparse.Namespace) -> None:
    from market_data.daemon import MarketDataDaemonConfig, MarketDataDaemonService

    symbols = tuple(
        validate_symbol(item.strip())
        for item in str(args.symbols).split(",")
        if item.strip()
    )
    timeframes = tuple(
        validate_timeframe(item.strip())
        for item in str(args.timeframes).split(",")
        if item.strip()
    )

    service = MarketDataDaemonService(Path(__file__).parent)
    result = service.run(
        MarketDataDaemonConfig(
            symbols=symbols,
            timeframes=timeframes,
            polling_interval_seconds=max(0.1, float(args.polling_interval_seconds)),
            context_delay_seconds=max(0.0, float(args.context_delay_seconds)),
            batch_size=max(100, int(args.batch_size)),
            retry_count=max(0, int(args.retry_count)),
            retry_delay_seconds=max(0.1, float(args.retry_delay_seconds)),
            bootstrap_days=max(1, int(args.bootstrap_days)),
            recent_gap_bars=max(10, int(args.recent_gap_bars)),
            report_every_cycles=max(1, int(args.report_every_cycles)),
            max_cycles=max(0, int(args.max_cycles)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("MARKET DATA DAEMON")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_robustness_validation(args: argparse.Namespace) -> None:
    from research.services.scientific_robustness_validation import (
        ScientificRobustnessValidationConfig,
        ScientificRobustnessValidationService,
    )

    with get_session() as session:
        service = ScientificRobustnessValidationService(session=session, base_dir=Path(__file__).parent)
        result = service.run(
            ScientificRobustnessValidationConfig(
                phase6_csv=str(args.phase6_csv),
                candidate_csv=str(args.candidate_csv),
                events_glob=str(args.events_glob),
                train_ratio=float(args.train_ratio),
                validation_ratio=float(args.validation_ratio),
                min_support=max(10, int(args.min_support)),
                max_rule_coverage=min(0.999, max(0.50, float(args.max_rule_coverage))),
                min_discrimination_gap=max(0.0, float(args.min_discrimination_gap)),
                min_scientific_score=max(0.0, min(100.0, float(args.min_scientific_score))),
                min_generalization_score=max(0.0, min(1.0, float(args.min_generalization_score))),
                min_robustness_score=max(0.0, min(1.0, float(args.min_robustness_score))),
                min_files=max(1, int(args.min_files)),
                min_events=max(1, int(args.min_events)),
                min_assets=max(1, int(args.min_assets)),
                min_timeframes=max(1, int(args.min_timeframes)),
                min_context_events=max(1, int(args.min_context_events)),
                min_coverage_days=max(1, int(args.min_coverage_days)),
                min_contexts=max(1, int(args.min_contexts)),
                output_prefix=str(args.output_prefix),
                persist_to_db=not bool(args.no_db),
            )
        )

    print("\n======================================")
    print("SCIENTIFIC ROBUSTNESS VALIDATION")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_trade_outcome_learning(args: argparse.Namespace) -> None:
    from research.labs.trade_outcome_learning_lab import (
        TradeOutcomeLearningConfig,
        TradeOutcomeLearningLab,
    )

    target_names = tuple([item.strip() for item in str(args.targets).split(",") if item.strip()])
    if not target_names:
        raise ValueError("--targets cannot be empty")

    with get_session() as session:
        service = TradeOutcomeLearningLab(session=session, base_dir=Path(__file__).parent)
        result = service.run(
            TradeOutcomeLearningConfig(
                events_glob=str(args.events_glob),
                targets=target_names,
                return_above_threshold=float(args.return_above_threshold),
                return_below_threshold=float(args.return_below_threshold),
                risk_adjusted_threshold=max(0.0, float(args.risk_adjusted_threshold)),
                train_ratio=max(0.20, min(0.80, float(args.train_ratio))),
                validation_ratio=max(0.10, min(0.40, float(args.validation_ratio))),
                min_support=max(10, int(args.min_support)),
                max_rule_coverage=max(0.20, min(0.95, float(args.max_rule_coverage))),
                min_precision_gain=max(0.0, min(0.50, float(args.min_precision_gain))),
                min_generalization_score=max(0.0, min(1.0, float(args.min_generalization_score))),
                min_robustness_score=max(0.0, min(1.0, float(args.min_robustness_score))),
                max_overfit_gap=max(0.0, min(0.80, float(args.max_overfit_gap))),
                trade_outcome_score_threshold=max(0.0, min(100.0, float(args.trade_outcome_score_threshold))),
                top_k_candidates=max(1, int(args.top_k_candidates)),
                output_prefix=str(args.output_prefix),
                persist_to_db=not bool(args.no_db),
            )
        )

    print("\n======================================")
    print("TRADE OUTCOME LEARNING")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_phase9_controlled_implementation(args: argparse.Namespace) -> None:
    from research.services.trade_outcome_controlled_implementation import (
        TradeOutcomeControlledImplementationConfig,
        TradeOutcomeControlledImplementationService,
    )

    with get_session() as session:
        service = TradeOutcomeControlledImplementationService(session=session, base_dir=Path(__file__).parent)
        result = service.run(
            TradeOutcomeControlledImplementationConfig(
                events_glob=str(args.events_glob),
                trade_outcome_csv=str(args.trade_outcome_csv),
                strategy_name=str(args.strategy_name),
                target_name=str(args.target_name),
                approved_rule=str(args.approved_rule),
                distance_threshold=float(args.distance_threshold),
                fidelity_min_f1=max(0.0, min(1.0, float(args.fidelity_min_f1))),
                optimizer_max_combinations=max(1, int(args.optimizer_max_combinations)),
                optimizer_workers=max(1, int(args.optimizer_workers)),
                optimizer_capital=max(100.0, float(args.optimizer_capital)),
                output_prefix=str(args.output_prefix),
                run_optimizer_validation=not bool(args.skip_optimizer_validation),
                run_research_labs=not bool(args.skip_research_labs),
                persist_to_db=not bool(args.no_db),
            )
        )

    print("\n======================================")
    print("FASE 9 - CONTROLLED IMPLEMENTATION")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_execution_framework_optimization(args: argparse.Namespace) -> None:
    from research.services.execution_framework_optimization import (
        ExecutionFrameworkOptimizationConfig,
        ExecutionFrameworkOptimizationService,
    )

    with get_session() as session:
        service = ExecutionFrameworkOptimizationService(session=session, base_dir=Path(__file__).parent)
        result = service.run(
            ExecutionFrameworkOptimizationConfig(
                strategy_name=str(args.strategy_name),
                benchmark_symbol=str(args.benchmark_symbol),
                benchmark_timeframe=str(args.benchmark_timeframe),
                benchmark_bars=max(500, int(args.benchmark_bars)),
                initial_capital=max(100.0, float(args.initial_capital)),
                output_prefix=str(args.output_prefix),
                rerun_phase9=not bool(args.skip_phase9_rerun),
                persist_to_db=not bool(args.no_db),
            )
        )

    print("\n======================================")
    print("FASE 9.0 - EXECUTION FRAMEWORK OPTIMIZATION")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_edge_discovery_lab(args: argparse.Namespace) -> None:
    from research.services.edge_discovery_lab import EdgeDiscoveryConfig, EdgeDiscoveryLabService

    symbols = tuple([item.strip() for item in str(args.symbols).split(",") if item.strip()])
    timeframes = tuple([item.strip() for item in str(args.timeframes).split(",") if item.strip()])
    if not symbols:
        raise ValueError("--symbols cannot be empty")
    if not timeframes:
        raise ValueError("--timeframes cannot be empty")

    service = EdgeDiscoveryLabService(Path(__file__).parent)
    result = service.run(
        EdgeDiscoveryConfig(
            report_file=str(args.report_file) if args.report_file else None,
            symbols=symbols,
            timeframes=timeframes,
            window_days=max(10, int(args.window_days)),
            capital=max(100.0, float(args.capital)),
            max_bars=max(500, int(args.max_bars)),
            min_trades_per_context=max(1, int(args.min_trades_per_context)),
            only_paper_candidates=not bool(args.include_all_candidates),
            limit_candidates=max(0, int(args.limit_candidates)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("EDGE DISCOVERY LAB")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_edge_extraction_lab(args: argparse.Namespace) -> None:
    from research.services.edge_extraction_lab import EdgeExtractionConfig, EdgeExtractionLabService

    prioritized = tuple([item.strip() for item in str(args.prioritized_strategies).split(",") if item.strip()])
    symbols = tuple([item.strip() for item in str(args.symbols).split(",") if item.strip()])
    timeframes = tuple([item.strip() for item in str(args.timeframes).split(",") if item.strip()])

    if not prioritized:
        raise ValueError("--prioritized-strategies cannot be empty")
    if not symbols:
        raise ValueError("--symbols cannot be empty")
    if not timeframes:
        raise ValueError("--timeframes cannot be empty")

    service = EdgeExtractionLabService(Path(__file__).parent)
    result = service.run(
        EdgeExtractionConfig(
            report_file=str(args.report_file) if args.report_file else None,
            prioritized_strategies=prioritized,
            symbols=symbols,
            timeframes=timeframes,
            window_days=max(10, int(args.window_days)),
            capital=max(100.0, float(args.capital)),
            max_bars=max(500, int(args.max_bars)),
            min_trades_per_filter=max(5, int(args.min_trades_per_filter)),
            top_filters=max(1, int(args.top_filters)),
            max_candidate_filters=max(5, int(args.max_candidate_filters)),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("EDGE EXTRACTION LAB")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_edge_external_validation_lab(args: argparse.Namespace) -> None:
    from research.services.edge_external_validation_lab import (
        EdgeExternalValidationLabService,
        ExternalStrategyValidationConfig,
    )

    edge01_report_file = str(args.edge01_report_file) if args.edge01_report_file else None
    if edge01_report_file is None:
        results_dir = Path(__file__).parent / "optimization" / "results"
        candidates = sorted(results_dir.glob("edge_extraction_lab_*.json"), key=lambda p: p.stat().st_mtime)
        if not candidates:
            raise RuntimeError("No EDGE-01 report found. Run edge-extraction-lab first or provide --edge01-report-file.")
        edge01_report_file = str(candidates[-1])

    service = EdgeExternalValidationLabService(Path(__file__).parent)
    result = service.run(
        ExternalStrategyValidationConfig(
            edge01_report_file=edge01_report_file,
            knowledge_base_file=str(args.knowledge_base_file) if args.knowledge_base_file else None,
            edge_discovery_file=str(args.edge_discovery_file) if args.edge_discovery_file else None,
            min_external_candidates=max(1, int(args.min_external_candidates)),
            max_external_candidates=max(1, int(args.max_external_candidates)),
            enable_web_research=not bool(args.disable_web_research),
            strict_web_filters=bool(args.strict_web_filters),
            min_repo_stars=max(0, int(args.min_repo_stars)),
            max_inactive_days=max(1, int(args.max_inactive_days)),
            reject_forks=not bool(args.allow_forks),
            require_readme=not bool(args.allow_missing_readme),
            strategy_version=str(args.strategy_version),
            default_platform_strategy=str(args.default_platform_strategy),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("EDGE-02 - EXTERNAL STRATEGY VALIDATION LAB")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_edge_operational_pipeline(args: argparse.Namespace) -> None:
    from research.services.edge_operational_pipeline import EdgeOperationalPipelineConfig, EdgeOperationalPipelineService

    prioritized = tuple([item.strip() for item in str(args.prioritized_strategies).split(",") if item.strip()])
    symbols = tuple([item.strip() for item in str(args.symbols).split(",") if item.strip()])
    timeframes = tuple([item.strip() for item in str(args.timeframes).split(",") if item.strip()])

    if not prioritized:
        raise ValueError("--prioritized-strategies cannot be empty")
    if not symbols:
        raise ValueError("--symbols cannot be empty")
    if not timeframes:
        raise ValueError("--timeframes cannot be empty")

    service = EdgeOperationalPipelineService(Path(__file__).parent)
    result = service.run(
        EdgeOperationalPipelineConfig(
            prioritized_strategies=prioritized,
            symbols=symbols,
            timeframes=timeframes,
            window_days=max(10, int(args.window_days)),
            capital=max(100.0, float(args.capital)),
            max_bars=max(500, int(args.max_bars)),
            min_trades_per_filter=max(5, int(args.min_trades_per_filter)),
            top_filters=max(1, int(args.top_filters)),
            max_candidate_filters=max(5, int(args.max_candidate_filters)),
            min_external_candidates=max(1, int(args.min_external_candidates)),
            max_external_candidates=max(1, int(args.max_external_candidates)),
            enable_web_research=not bool(args.disable_web_research),
            strict_web_filters=bool(args.strict_web_filters),
            min_repo_stars=max(0, int(args.min_repo_stars)),
            max_inactive_days=max(1, int(args.max_inactive_days)),
            reject_forks=not bool(args.allow_forks),
            require_readme=not bool(args.allow_missing_readme),
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("EDGE OPERATIONAL PIPELINE")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


def cmd_market_regime_router(args: argparse.Namespace) -> None:
    from research.services.market_regime_router_phase18 import (
        MarketRegimeRouterConfig,
        MarketRegimeRouterService,
    )

    symbols = tuple([item.strip() for item in str(args.symbols).split(",") if item.strip()])
    timeframes = tuple([item.strip() for item in str(args.timeframes).split(",") if item.strip()])
    if not symbols:
        raise ValueError("--symbols cannot be empty")
    if not timeframes:
        raise ValueError("--timeframes cannot be empty")

    service = MarketRegimeRouterService(Path(__file__).parent)
    result = service.run(
        MarketRegimeRouterConfig(
            report_file=str(args.report_file) if args.report_file else None,
            symbols=symbols,
            timeframes=timeframes,
            window_days=max(10, int(args.window_days)),
            capital=max(100.0, float(args.capital)),
            max_bars=max(500, int(args.max_bars)),
            min_trades_per_regime=max(1, int(args.min_trades_per_regime)),
            only_paper_candidates=not bool(args.include_all_candidates),
            limit_candidates=max(0, int(args.limit_candidates)),
            baseline_strategy=str(args.baseline_strategy) if args.baseline_strategy else None,
            output_prefix=str(args.output_prefix),
        )
    )

    print("\n======================================")
    print("FASE 18 - MARKET REGIME ROUTER")
    print("======================================")
    print("Summary:")
    print(json.dumps(result.get("summary", {}), ensure_ascii=False, indent=2, default=str))
    print("Outputs:")
    for key, value in result.get("outputs", {}).items():
        print(f"- {key}: {value}")


_COMMANDS["optimize"] = cmd_optimize
_COMMANDS["validate"] = cmd_validate
_COMMANDS["api"] = cmd_api
_COMMANDS["execution-manager"] = cmd_execution_manager
_COMMANDS["execution-manager-rc1"] = cmd_execution_manager_rc1
_COMMANDS["strategy-research-lab"] = cmd_strategy_research_lab
_COMMANDS["trade-management-research-lab"] = cmd_trade_management_research_lab
_COMMANDS["trade-lifecycle-audit"] = cmd_trade_lifecycle_audit
_COMMANDS["phase9-4-controlled-improvement"] = cmd_phase9_4_controlled_improvement
_COMMANDS["strategy-catalog-cycle"] = cmd_strategy_catalog_cycle
_COMMANDS["strategy-catalog-audit"] = cmd_strategy_catalog_audit
_COMMANDS["crypto-strategy-research"] = cmd_crypto_strategy_research
_COMMANDS["supertrend-controlled-implementation"] = cmd_supertrend_controlled_implementation
_COMMANDS["continuous-strategy-factory"] = cmd_continuous_strategy_factory
_COMMANDS["overnight-campaign"] = cmd_overnight_campaign
_COMMANDS["market-intelligence"] = cmd_market_intelligence
_COMMANDS["phase13-1-audit-strengthening"] = cmd_phase13_1_audit_strengthening
_COMMANDS["strategy-discovery"] = cmd_strategy_discovery
_COMMANDS["market-data-expansion"] = cmd_market_data_expansion
_COMMANDS["market-data-daemon"] = cmd_market_data_daemon
_COMMANDS["robustness-validation"] = cmd_robustness_validation
_COMMANDS["trade-outcome-learning"] = cmd_trade_outcome_learning
_COMMANDS["phase9-controlled-implementation"] = cmd_phase9_controlled_implementation
_COMMANDS["execution-framework-optimization"] = cmd_execution_framework_optimization
_COMMANDS["paper-daily-report"] = cmd_paper_daily_report
_COMMANDS["paper-live"] = cmd_paper_live
_COMMANDS["live"] = cmd_live
_COMMANDS["paper-live-supervisor"] = cmd_paper_live_supervisor
_COMMANDS["paper-operational-report"] = cmd_paper_operational_report
_COMMANDS["paper-specialized-validation"] = cmd_paper_specialized_validation
_COMMANDS["edge-drift-monitor"] = cmd_edge_drift_monitor
_COMMANDS["paper-specialized-campaign"] = cmd_paper_specialized_campaign
_COMMANDS["paper-campaign-coverage"] = cmd_paper_campaign_coverage
_COMMANDS["strategy-diagnostics"] = cmd_strategy_diagnostics
_COMMANDS["strategy-version-compare"] = cmd_strategy_version_compare
_COMMANDS["edge-discovery-lab"] = cmd_edge_discovery_lab
_COMMANDS["edge-extraction-lab"] = cmd_edge_extraction_lab
_COMMANDS["edge-external-validation-lab"] = cmd_edge_external_validation_lab
_COMMANDS["edge-operational-pipeline"] = cmd_edge_operational_pipeline
_COMMANDS["market-regime-router"] = cmd_market_regime_router


def main() -> None:
    """Application entry point."""
    initialize_logging_service(
        log_dir=settings.logging.log_dir,
        level=settings.logging.level,
        queue_maxsize=int(os.getenv("LOG_QUEUE_MAXSIZE", "20000")),
        enable_console=True,
        enable_time_rotation=os.getenv("LOG_ENABLE_TIME_ROTATION", "0") == "1",
    )
    try:
        settings.validate()
        settings.validate_database_access()
    except ValueError as exc:
        raise SystemExit(f"Configuration error:\n{exc}") from exc
    except Exception as exc:
        raise SystemExit(f"Startup validation failed: {exc}") from exc

    print(settings.startup_summary())
    logger.info(
        "Crypto Trading Bot starting - env=%s paper=%s",
        settings.environment,
        settings.paper_trading,
    )

    # Garante que o schema e as tabelas do banco configurado existam
    bootstrap_database()

    args = _parse_args()
    handler = _COMMANDS.get(args.command)
    try:
        if handler:
            handler(args)
        else:
            logger.error("Unknown command: %s", args.command)
            sys.exit(1)
    finally:
        shutdown_logging_service()


if __name__ == "__main__":
    main()
