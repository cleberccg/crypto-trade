from __future__ import annotations

from datetime import datetime, timezone

from activation.readiness_models import ActivationStep, ReadinessCheck


def readiness_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    items = [
        ReadinessCheck(
            id="rd-001",
            component="optimizer_results_ingestion",
            status="prepared",
            details="Pipeline scaffolded with dry-run mode.",
            activation_required=True,
            checked_at=now,
        ),
        ReadinessCheck(
            id="rd-002",
            component="research_rankings_refresh",
            status="prepared",
            details="Research endpoints/pages ready with mock contracts.",
            activation_required=True,
            checked_at=now,
        ),
        ReadinessCheck(
            id="rd-003",
            component="scanner_refresh",
            status="prepared",
            details="Scanner contract ready and isolated from live execution.",
            activation_required=True,
            checked_at=now,
        ),
    ]
    payload = [
        {
            "id": item.id,
            "component": item.component,
            "status": item.status,
            "details": item.details,
            "activation_required": item.activation_required,
            "checked_at": item.checked_at.isoformat(),
        }
        for item in items
    ]
    return {"meta": {"page": 1, "page_size": len(payload), "total": len(payload)}, "items": payload}


def activation_plan_snapshot() -> dict:
    now = datetime.now(timezone.utc)
    steps = [
        ActivationStep(
            id="ac-001",
            name="Ingest optimizer results",
            description="Enable real repository readers for finished optimization output.",
            enabled=False,
            dry_run_only=True,
            updated_at=now,
        ),
        ActivationStep(
            id="ac-002",
            name="Refresh research rankings and insights",
            description="Compute rankings, comparisons, and reports from ingested results.",
            enabled=False,
            dry_run_only=True,
            updated_at=now,
        ),
        ActivationStep(
            id="ac-003",
            name="Refresh scanner and dashboard metrics",
            description="Replace mocks with real-time computed values.",
            enabled=False,
            dry_run_only=True,
            updated_at=now,
        ),
    ]
    payload = [
        {
            "id": step.id,
            "name": step.name,
            "description": step.description,
            "enabled": step.enabled,
            "dry_run_only": step.dry_run_only,
            "updated_at": step.updated_at.isoformat(),
        }
        for step in steps
    ]
    return {"meta": {"page": 1, "page_size": len(payload), "total": len(payload)}, "items": payload}
