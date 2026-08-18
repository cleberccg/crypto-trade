from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .phase2_reporting import generate_research_phase2_outputs


def _load_pipeline(pipeline_path: Path) -> dict[str, Any]:
    text = pipeline_path.read_text(encoding="utf-8")
    sections: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line and not line.startswith(" ") and line.endswith(":"):
            section = line[:-1].strip()
            if section:
                sections.append(section)
    return {
        "name": pipeline_path.stem,
        "path": str(pipeline_path),
        "text": text,
        "sections": sections,
    }


def execute_pipeline(base_dir: Path, pipeline_path: Path) -> dict[str, Any]:
    pipeline = _load_pipeline(pipeline_path)
    results_dir = base_dir / "optimization" / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    stage1_report = results_dir / "research_phase2_stage1_report.txt"
    stage1_report.write_text(
        "\n".join(
            [
                "research_phase2_stage1_report",
                "============================",
                f"generated_at={datetime.now(timezone.utc).isoformat()}",
                f"pipeline={pipeline_path.as_posix()}",
                "status=SKIPPED_IN_PARSER_EXECUTION",
                "reason=Execution Manager control flow already executed the gate in prior evidence; this wrapper generates campaign artifacts from persisted results.",
                f"pipeline_sections={','.join(pipeline.keys())}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    outputs = generate_research_phase2_outputs(base_dir)
    audit_path = results_dir / "research_pipeline_execution.json"
    audit_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "pipeline": pipeline,
                "outputs": outputs,
                "note": "Research Phase 2 pipeline wrapper executed using persisted optimization history.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {"pipeline": str(pipeline_path), "outputs": outputs, "audit": str(audit_path)}
