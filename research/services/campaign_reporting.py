from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_text_if_exists(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="ignore")


def _read_json_if_exists(path: Path) -> Any | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def write_campaign_reports(base_dir: Path, snapshot: dict[str, Any]) -> dict[str, str]:
    results_dir = base_dir / "optimization" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    execution_report = _read_json_if_exists(results_dir / "execution_report.json") or {}
    execution_metrics = _read_json_if_exists(results_dir / "execution_metrics.json") or {}
    research_summary = _read_json_if_exists(results_dir / "research_summary.json") or {}
    ranking_csv = _read_text_if_exists(results_dir / "strategy_ranking.csv") or ""
    executive_md = _read_text_if_exists(results_dir / "executive_strategy_report.md") or ""

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "snapshot": snapshot,
        "execution_report": execution_report,
        "execution_metrics": execution_metrics,
        "research_summary": research_summary,
        "artifacts": {
            "strategy_ranking_csv": str(results_dir / "strategy_ranking.csv") if ranking_csv else None,
            "executive_strategy_report_md": str(results_dir / "executive_strategy_report.md") if executive_md else None,
        },
    }

    json_path = results_dir / "research_campaign_report.json"
    txt_path = results_dir / "research_campaign_report.txt"
    html_path = results_dir / "research_campaign_report.html"

    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    lines = [
        "Research Campaign Report",
        "=" * 60,
        f"Generated at: {payload['generated_at']}",
        f"Execution ID: {snapshot.get('execution_id')}",
        f"Status: {snapshot.get('status')}",
        f"Current asset: {snapshot.get('current_asset') or snapshot.get('asset') or ''}",
        f"Current timeframe: {snapshot.get('current_timeframe') or snapshot.get('timeframe') or ''}",
        f"Progress: {snapshot.get('progress_pct', 0.0)}%",
        f"Processed: {snapshot.get('processed_total', 0)}/{snapshot.get('target_total', 0)}",
        f"ETA seconds: {snapshot.get('eta_seconds')}",
        f"Workers: {snapshot.get('workers')}",
        f"CPU: {snapshot.get('cpu')}",
        f"RAM: {snapshot.get('ram')}",
        "",
        "Execution report:",
        json.dumps(execution_report, ensure_ascii=False, indent=2, default=str),
        "",
        "Research summary:",
        json.dumps(research_summary, ensure_ascii=False, indent=2, default=str),
    ]
    txt_path.write_text("\n".join(lines), encoding="utf-8")

    html = [
        "<!doctype html>",
        "<html><head><meta charset='utf-8'><title>Research Campaign Report</title></head><body>",
        "<h1>Research Campaign Report</h1>",
        f"<p><strong>Generated at:</strong> {payload['generated_at']}</p>",
        f"<p><strong>Execution ID:</strong> {snapshot.get('execution_id')}</p>",
        f"<p><strong>Status:</strong> {snapshot.get('status')}</p>",
        f"<p><strong>Current asset:</strong> {snapshot.get('current_asset') or snapshot.get('asset') or ''}</p>",
        f"<p><strong>Current timeframe:</strong> {snapshot.get('current_timeframe') or snapshot.get('timeframe') or ''}</p>",
        f"<p><strong>Progress:</strong> {snapshot.get('progress_pct', 0.0)}%</p>",
        f"<p><strong>Processed:</strong> {snapshot.get('processed_total', 0)}/{snapshot.get('target_total', 0)}</p>",
        f"<p><strong>ETA seconds:</strong> {snapshot.get('eta_seconds')}</p>",
        f"<p><strong>Workers:</strong> {snapshot.get('workers')}</p>",
        f"<p><strong>CPU:</strong> {snapshot.get('cpu')}</p>",
        f"<p><strong>RAM:</strong> {snapshot.get('ram')}</p>",
        "<h2>Execution Report</h2>",
        f"<pre>{json.dumps(execution_report, ensure_ascii=False, indent=2, default=str)}</pre>",
        "<h2>Research Summary</h2>",
        f"<pre>{json.dumps(research_summary, ensure_ascii=False, indent=2, default=str)}</pre>",
        "</body></html>",
    ]
    html_path.write_text("\n".join(html), encoding="utf-8")

    return {
        "research_campaign_report_json": str(json_path),
        "research_campaign_report_txt": str(txt_path),
        "research_campaign_report_html": str(html_path),
        "strategy_ranking_csv": str(results_dir / "strategy_ranking.csv") if ranking_csv else "",
        "executive_strategy_report_md": str(results_dir / "executive_strategy_report.md") if executive_md else "",
    }
