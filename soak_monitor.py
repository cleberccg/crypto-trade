from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SoakThresholds:
    max_context_lag_minutes: float = 10.0
    max_restart_count: int = 5
    max_error_lines_per_hour: int = 20
    max_disk_usage_pct: float = 95.0
    max_ram_usage_pct: float = 95.0
    max_cpu_usage_pct: float = 98.0


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def find_latest(results_dir: Path, pattern: str) -> Path | None:
    files = list(results_dir.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def list_state_files(results_dir: Path, campaign_id: str) -> list[Path]:
    files = sorted(results_dir.glob("paper_live_state__*.json"))
    if not campaign_id:
        return files
    kept: list[Path] = []
    for path in files:
        payload = read_json(path)
        if isinstance(payload, dict) and str(payload.get("campaign_id") or "") == campaign_id:
            kept.append(path)
    return kept


def file_growth_bytes(results_dir: Path, hours: int) -> int:
    if hours <= 0:
        return 0
    cutoff = now_utc() - timedelta(hours=hours)
    total = 0
    for path in results_dir.iterdir():
        if not path.is_file():
            continue
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        except OSError:
            continue
        if mtime >= cutoff:
            try:
                total += int(path.stat().st_size)
            except OSError:
                continue
    return total


def count_log_errors(log_path: Path, hours: int) -> int:
    if not log_path.exists():
        return 0
    # Heuristic read: keep it simple and fast for hourly cadence.
    raw = log_path.read_text(encoding="utf-8", errors="ignore")
    lines = raw.splitlines()
    if not lines:
        return 0

    cutoff = now_utc() - timedelta(hours=max(1, hours))
    count = 0
    for line in lines:
        if "ERROR" not in line and "Traceback" not in line and "CRITICAL" not in line:
            continue
        ts = None
        if " | " in line:
            head = line.split(" | ", 1)[0].strip()
            try:
                ts = datetime.strptime(head, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except Exception:
                ts = None
        if ts is None or ts >= cutoff:
            count += 1
    return count


def safe_resource_snapshot() -> dict[str, float | None]:
    try:
        import psutil  # type: ignore

        cpu = float(psutil.cpu_percent(interval=0.2))
        ram = float(psutil.virtual_memory().percent)
        disk = float(psutil.disk_usage(str(Path.cwd().anchor or "/")).percent)
        return {"cpu": cpu, "ram": ram, "disk": disk}
    except Exception:
        return {"cpu": None, "ram": None, "disk": None}


def collect_context_metrics(state_files: list[Path]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    now = now_utc()
    for path in state_files:
        payload = read_json(path)
        if not isinstance(payload, dict):
            continue
        updated = parse_dt(payload.get("updated_at"))
        lag_sec = None
        if updated is not None:
            lag_sec = max(0.0, (now - updated).total_seconds())
        rows.append(
            {
                "file": path.name,
                "execution_id": str(payload.get("execution_id") or ""),
                "symbol": str(payload.get("symbol") or ""),
                "timeframe": str(payload.get("timeframe") or ""),
                "strategy_name": str(payload.get("strategy_name") or ""),
                "strategy_version": str(payload.get("strategy_version") or ""),
                "campaign_id": str(payload.get("campaign_id") or ""),
                "cycles": int(payload.get("cycles") or 0),
                "updated_at": updated.isoformat() if updated else None,
                "lag_seconds": lag_sec,
                "last_report_at": str(payload.get("last_report_at") or ""),
            }
        )

    lags = [float(r["lag_seconds"]) for r in rows if r.get("lag_seconds") is not None]
    cycles = [int(r.get("cycles") or 0) for r in rows]
    return {
        "contexts_total": len(rows),
        "lag_seconds_min": min(lags) if lags else None,
        "lag_seconds_avg": (sum(lags) / len(lags)) if lags else None,
        "lag_seconds_max": max(lags) if lags else None,
        "cycles_min": min(cycles) if cycles else None,
        "cycles_avg": (sum(cycles) / len(cycles)) if cycles else None,
        "cycles_max": max(cycles) if cycles else None,
        "rows": rows,
    }


def collect_supervisor_metrics(results_dir: Path) -> dict[str, Any]:
    latest = find_latest(results_dir, "paper_live_supervisor_*.json")
    if latest is None:
        return {"exists": False}

    payload = read_json(latest)
    if not isinstance(payload, dict):
        return {"exists": True, "path": str(latest), "parse_error": True}

    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    return {
        "exists": True,
        "path": str(latest),
        "status": summary.get("status"),
        "contexts": summary.get("contexts"),
        "total_restarts": int(summary.get("total_restarts") or 0),
        "permanent_failures": int(summary.get("permanent_failures") or 0),
    }


def collect_report_metrics(results_dir: Path, hours: int) -> dict[str, Any]:
    cutoff = now_utc() - timedelta(hours=max(1, hours))
    kinds = {
        "hourly": "*_hourly_report_*.json",
        "operation": "*_operation_report_*.json",
        "daily": "*_daily_*.json",
        "weekly": "*_weekly_report_*.json",
        "monthly": "*_monthly_report_*.json",
    }
    out: dict[str, Any] = {}

    for key, pattern in kinds.items():
        files = list(results_dir.glob(pattern))
        recent = []
        for path in files:
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError:
                continue
            if mtime >= cutoff:
                recent.append(path)
        latest = max(files, key=lambda p: p.stat().st_mtime) if files else None
        out[key] = {
            "recent_count": len(recent),
            "latest": str(latest) if latest else None,
        }

    return out


def evaluate_alerts(report: dict[str, Any], thresholds: SoakThresholds) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    ctx = report.get("contexts", {})
    lag_max = ctx.get("lag_seconds_max")
    if isinstance(lag_max, (int, float)) and lag_max > thresholds.max_context_lag_minutes * 60.0:
        alerts.append(
            {
                "severity": "high",
                "code": "CONTEXT_STALE",
                "message": f"Context lag max {lag_max:.1f}s exceeds {thresholds.max_context_lag_minutes*60.0:.1f}s",
                "probable_cause": "processing stalled, feed outage, or worker hung",
                "corrective_action": "verify supervisor/process health; restart affected context worker if persistent",
            }
        )

    sup = report.get("supervisor", {})
    if sup.get("exists"):
        restarts = int(sup.get("total_restarts") or 0)
        if restarts > thresholds.max_restart_count:
            alerts.append(
                {
                    "severity": "high",
                    "code": "RESTART_STORM",
                    "message": f"total_restarts={restarts} exceeds threshold={thresholds.max_restart_count}",
                    "probable_cause": "runtime instability, dependency flaps, or bad context",
                    "corrective_action": "inspect latest supervisor audit and isolate failing context",
                }
            )

    resources = report.get("resources", {})
    cpu = resources.get("cpu")
    ram = resources.get("ram")
    disk = resources.get("disk")
    if isinstance(cpu, (int, float)) and cpu > thresholds.max_cpu_usage_pct:
        alerts.append(
            {
                "severity": "medium",
                "code": "CPU_HIGH",
                "message": f"cpu={cpu:.1f}% exceeds {thresholds.max_cpu_usage_pct:.1f}%",
                "probable_cause": "high compute load or runaway loop",
                "corrective_action": "inspect process CPU consumers and reduce load",
            }
        )
    if isinstance(ram, (int, float)) and ram > thresholds.max_ram_usage_pct:
        alerts.append(
            {
                "severity": "high",
                "code": "RAM_HIGH",
                "message": f"ram={ram:.1f}% exceeds {thresholds.max_ram_usage_pct:.1f}%",
                "probable_cause": "memory pressure or leak",
                "corrective_action": "inspect resident memory over time and restart safely if needed",
            }
        )
    if isinstance(disk, (int, float)) and disk > thresholds.max_disk_usage_pct:
        alerts.append(
            {
                "severity": "high",
                "code": "DISK_HIGH",
                "message": f"disk={disk:.1f}% exceeds {thresholds.max_disk_usage_pct:.1f}%",
                "probable_cause": "artifact/log growth",
                "corrective_action": "rotate/compress old artifacts and free disk",
            }
        )

    errors_per_hour = int(report.get("logs", {}).get("error_lines_last_window") or 0)
    if errors_per_hour > thresholds.max_error_lines_per_hour:
        alerts.append(
            {
                "severity": "medium",
                "code": "ERROR_RATE_HIGH",
                "message": f"error_lines_last_window={errors_per_hour} exceeds {thresholds.max_error_lines_per_hour}",
                "probable_cause": "systemic runtime errors",
                "corrective_action": "inspect app logs and open incident for recurring stack traces",
            }
        )

    return alerts


def readiness_status(alerts: list[dict[str, Any]]) -> str:
    if any(a.get("severity") == "high" for a in alerts):
        return "REPROVADO"
    if any(a.get("severity") == "medium" for a in alerts):
        return "APROVADO_COM_RESTRICOES"
    return "APROVADO"


def write_outputs(results_dir: Path, payload: dict[str, Any], prefix: str) -> dict[str, str]:
    stamp = now_utc().strftime("%Y%m%d_%H%M%S")
    json_path = results_dir / f"{prefix}_{stamp}.json"
    md_path = results_dir / f"{prefix}_{stamp}.md"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "# Soak Monitoring Hourly Report",
        "",
        f"- generated_at_utc: {payload.get('generated_at_utc')}",
        f"- status: {payload.get('status')}",
        f"- campaign_id: {payload.get('campaign_id') or 'n/a'}",
        f"- contexts_total: {payload.get('contexts', {}).get('contexts_total')}",
        f"- lag_max_seconds: {payload.get('contexts', {}).get('lag_seconds_max')}",
        f"- supervisor_restarts: {payload.get('supervisor', {}).get('total_restarts')}",
        f"- error_lines_last_window: {payload.get('logs', {}).get('error_lines_last_window')}",
        f"- artifact_growth_bytes_last_window: {payload.get('artifacts', {}).get('growth_bytes_last_window')}",
        "",
        "## Alerts",
    ]

    alerts = payload.get("alerts", [])
    if not alerts:
        lines.append("- none")
    else:
        for alert in alerts:
            lines.append(
                f"- [{alert.get('severity')}] {alert.get('code')}: {alert.get('message')} | "
                f"cause={alert.get('probable_cause')} | action={alert.get('corrective_action')}"
            )

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": str(json_path), "md": str(md_path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hourly soak monitor (read-only) for paper-live supervisor campaign.")
    parser.add_argument("--results-dir", default="optimization/results")
    parser.add_argument("--campaign-id", default="")
    parser.add_argument("--log-path", default="logs/application.log")
    parser.add_argument("--window-hours", type=int, default=1)
    parser.add_argument("--max-context-lag-min", type=float, default=10.0)
    parser.add_argument("--max-restarts", type=int, default=5)
    parser.add_argument("--max-error-lines", type=int, default=20)
    parser.add_argument("--max-disk-pct", type=float, default=95.0)
    parser.add_argument("--max-ram-pct", type=float, default=95.0)
    parser.add_argument("--max-cpu-pct", type=float, default=98.0)
    parser.add_argument("--output-prefix", default="soak_hourly_report")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    results_dir = Path(args.results_dir)
    if not results_dir.exists():
        print(f"[ERRO] results dir not found: {results_dir}")
        return 2

    thresholds = SoakThresholds(
        max_context_lag_minutes=max(0.5, float(args.max_context_lag_min)),
        max_restart_count=max(0, int(args.max_restarts)),
        max_error_lines_per_hour=max(0, int(args.max_error_lines)),
        max_disk_usage_pct=max(1.0, float(args.max_disk_pct)),
        max_ram_usage_pct=max(1.0, float(args.max_ram_pct)),
        max_cpu_usage_pct=max(1.0, float(args.max_cpu_pct)),
    )

    state_files = list_state_files(results_dir, str(args.campaign_id or "").strip())
    context_metrics = collect_context_metrics(state_files)
    supervisor_metrics = collect_supervisor_metrics(results_dir)
    report_metrics = collect_report_metrics(results_dir, max(1, int(args.window_hours)))
    resources = safe_resource_snapshot()

    logs = {
        "log_path": str(Path(args.log_path)),
        "error_lines_last_window": count_log_errors(Path(args.log_path), max(1, int(args.window_hours))),
    }

    artifacts = {
        "growth_bytes_last_window": file_growth_bytes(results_dir, max(1, int(args.window_hours))),
    }

    payload: dict[str, Any] = {
        "generated_at_utc": now_utc().isoformat(),
        "campaign_id": str(args.campaign_id or "").strip(),
        "thresholds": {
            "max_context_lag_minutes": thresholds.max_context_lag_minutes,
            "max_restart_count": thresholds.max_restart_count,
            "max_error_lines_per_hour": thresholds.max_error_lines_per_hour,
            "max_disk_usage_pct": thresholds.max_disk_usage_pct,
            "max_ram_usage_pct": thresholds.max_ram_usage_pct,
            "max_cpu_usage_pct": thresholds.max_cpu_usage_pct,
        },
        "contexts": context_metrics,
        "supervisor": supervisor_metrics,
        "reports": report_metrics,
        "resources": resources,
        "logs": logs,
        "artifacts": artifacts,
    }

    alerts = evaluate_alerts(payload, thresholds)
    payload["alerts"] = alerts
    payload["status"] = readiness_status(alerts)

    outputs = write_outputs(results_dir, payload, str(args.output_prefix))
    payload["outputs"] = outputs

    print("=" * 88)
    print("SOAK HOURLY REPORT")
    print("=" * 88)
    print(f"status                  : {payload['status']}")
    print(f"campaign_id             : {payload['campaign_id'] or '(latest/all)'}")
    print(f"contexts_total          : {context_metrics.get('contexts_total')}")
    print(f"lag_max_seconds         : {context_metrics.get('lag_seconds_max')}")
    print(f"supervisor_restarts     : {supervisor_metrics.get('total_restarts')}")
    print(f"error_lines_last_window : {logs['error_lines_last_window']}")
    print(f"artifact_growth_bytes   : {artifacts['growth_bytes_last_window']}")
    print(f"report_json             : {outputs['json']}")
    print(f"report_md               : {outputs['md']}")
    if alerts:
        print("-" * 88)
        print("ALERTS")
        for alert in alerts:
            print(f"[{alert.get('severity')}] {alert.get('code')} -> {alert.get('message')}")
    print("=" * 88)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
