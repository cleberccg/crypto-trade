from __future__ import annotations

from datetime import datetime, timedelta, timezone

from timeline.timeline_models import TimelineEvent


def seed_timeline_events() -> list[TimelineEvent]:
    now = datetime.now(timezone.utc)
    return [
        TimelineEvent(id="evt-001", event_type="download_started", title="Download iniciado", details="Download histórico iniciado", created_at=now - timedelta(hours=8)),
        TimelineEvent(id="evt-002", event_type="optimizer_started", title="Optimizer iniciado", details="Execucao paralela iniciada", created_at=now - timedelta(hours=7, minutes=40)),
        TimelineEvent(id="evt-003", event_type="checkpoint_saved", title="Checkpoint salvo", details="Ponto persistido com sucesso", created_at=now - timedelta(hours=7, minutes=10)),
    ]
