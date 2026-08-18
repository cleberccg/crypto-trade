from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jobs.job_manager import JobManager
from notifications.notification_models import NotificationRecord
from notifications.notification_repository import NotificationRepository
from notifications.notification_service import NotificationService
from research.research_models import ResearchInsight
from research.research_repository import ResearchRepository
from research.research_service import ResearchService
from scanner.scanner_models import ScannerAsset
from scanner.scanner_repository import ScannerRepository
from scanner.scanner_service import ScannerService
from scheduler.scheduler_models import SchedulerTask
from scheduler.scheduler_repository import SchedulerRepository
from scheduler.scheduler_service import SchedulerService
from timeline.timeline_events import seed_timeline_events
from timeline.timeline_repository import TimelineRepository
from timeline.timeline_service import TimelineService


job_manager = JobManager()

_timeline_service = TimelineService(TimelineRepository(seed_timeline_events()))
_notification_service = NotificationService(
    NotificationRepository(
        [
            NotificationRecord(id="n-001", channel="dashboard", title="Optimizer ativo", message="Optimizer com checkpoint em andamento."),
            NotificationRecord(id="n-002", channel="research", title="Research pendente", message="Aguardando resultados finais para leitura automatica."),
        ]
    )
)
_scheduler_service = SchedulerService(
    SchedulerRepository(
        [
            SchedulerTask(id="sch-001", name="Daily Download", schedule="02:00", enabled=False),
            SchedulerTask(id="sch-002", name="Validation After Optimizer", schedule="after-optimizer", enabled=False),
        ]
    )
)
_research_service = ResearchService(
    ResearchRepository(
        [
            ResearchInsight(id="r-001", title="Sharpe acima da media", summary="Conjunto TrendV1 com consistencia acima do baseline.", category="ranking"),
            ResearchInsight(id="r-002", title="Drawdown controlado", summary="Curvas ainda dentro da faixa esperada.", category="risk"),
        ]
    )
)
_scanner_service = ScannerService(
    ScannerRepository(
        [
            ScannerAsset(symbol="BTC/USDT", liquidity_score=97.0, volatility_score=72.5, volume_score=93.1, spread_score=88.4, trend_score=91.2, momentum_score=84.7, opportunity_score=92.3),
            ScannerAsset(symbol="ETH/USDT", liquidity_score=94.2, volatility_score=68.4, volume_score=86.3, spread_score=82.5, trend_score=89.1, momentum_score=80.4, opportunity_score=88.1),
        ]
    )
)


def jobs_snapshot() -> dict:
    return job_manager.service.snapshot()


def job_detail(job_id: str) -> dict | None:
    return job_manager.service.get_job(job_id)


def jobs_running() -> dict:
    return job_manager.service.running()


def jobs_history() -> dict:
    return job_manager.service.history()


def execution_timeline_snapshot() -> dict:
    payload = _timeline_service.snapshot()
    items = payload["items"]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def notifications_snapshot() -> dict:
    payload = _notification_service.snapshot()
    items = payload["items"]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def scheduler_snapshot() -> dict:
    payload = _scheduler_service.snapshot()
    items = payload["items"]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def research_snapshot() -> dict:
    payload = _research_service.snapshot()
    items = payload["items"]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def scanner_snapshot() -> dict:
    payload = _scanner_service.snapshot()
    items = payload["items"]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def research_comparisons_snapshot() -> dict:
    items = [
        {"id": "cmp-001", "left_strategy": "TrendV1", "right_strategy": "TrendV1-Alt", "winner": "TrendV1", "profit_factor_diff": 0.24},
        {"id": "cmp-002", "left_strategy": "TrendV1", "right_strategy": "TrendV1-Vol", "winner": "TrendV1-Vol", "profit_factor_diff": 0.07},
    ]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def research_rankings_snapshot() -> dict:
    items = [
        {"rank": 1, "strategy": "TrendV1", "symbol": "BTC/USDT", "timeframe": "5m", "profit_factor": 2.43, "sharpe": 1.71},
        {"rank": 2, "strategy": "TrendV1-Vol", "symbol": "ETH/USDT", "timeframe": "15m", "profit_factor": 2.21, "sharpe": 1.56},
    ]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def research_insights_snapshot() -> dict:
    items = [
        {"id": "ins-001", "category": "consistency", "title": "PF consistente em BTC", "summary": "Resultados com baixa dispersao no top quartile."},
        {"id": "ins-002", "category": "risk", "title": "Drawdown sob controle", "summary": "Nenhum outlier acima do limite configurado nos mocks."},
    ]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def research_heatmaps_snapshot() -> dict:
    items = [
        {"symbol": "BTC/USDT", "timeframe": "5m", "value": 0.82},
        {"symbol": "BTC/USDT", "timeframe": "15m", "value": 0.73},
        {"symbol": "ETH/USDT", "timeframe": "5m", "value": 0.77},
    ]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def research_reports_snapshot() -> dict:
    items = [
        {"id": "rep-001", "name": "daily_research_mock", "status": "ready", "generated_at": datetime.now(timezone.utc).isoformat()},
        {"id": "rep-002", "name": "rankings_summary_mock", "status": "ready", "generated_at": datetime.now(timezone.utc).isoformat()},
    ]
    return {"meta": {"page": 1, "page_size": len(items), "total": len(items)}, "items": items}


def mock_dashboard_status() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "system_health": "degraded" if jobs_snapshot()["meta"]["running"] else "healthy",
        "realtime_status": "online",
        "workers": 4,
        "execution_queue": 12,
        "cpu": 76.4,
        "ram": 68.9,
        "disk": 52.2,
        "database": "ok",
        "binance": "ok",
        "api": "ok",
        "websocket": "ok",
        "optimizer": "running",
        "validation": "queued",
        "research": "waiting",
        "scanner": "mocked",
        "updated_at": now.isoformat(),
    }
