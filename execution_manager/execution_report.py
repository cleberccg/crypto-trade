from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from execution_manager.execution_models import ExecutionJob, ExecutionState


class ExecutionReportWriter:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.json_file = self.output_dir / "execution_report.json"
        self.txt_file = self.output_dir / "execution_report.txt"
        self.html_file = self.output_dir / "execution_report.html"

    def write(self, state: ExecutionState, jobs: list[ExecutionJob]) -> None:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "state": state.to_dict(),
            "jobs": [job.to_dict() for job in jobs],
        }
        self.json_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "Execution Manager Report",
            "=" * 60,
            f"Execution ID: {state.execution_id}",
            f"Status: {state.status}",
            f"Progress: {state.progress_pct:.2f}%",
            f"Processed: {state.processed_total}/{state.target_total}",
            f"CPU: {state.cpu:.2f}%",
            f"RAM: {state.ram:.2f}%",
        ]
        self.txt_file.write_text("\n".join(lines), encoding="utf-8")

        html = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>Execution Report</title></head>
<body>
<h1>Execution Manager Report</h1>
<p><strong>Execution ID:</strong> {state.execution_id}</p>
<p><strong>Status:</strong> {state.status}</p>
<p><strong>Progress:</strong> {state.progress_pct:.2f}% ({state.processed_total}/{state.target_total})</p>
<p><strong>CPU:</strong> {state.cpu:.2f}%</p>
<p><strong>RAM:</strong> {state.ram:.2f}%</p>
</body></html>"""
        self.html_file.write_text(html, encoding="utf-8")
