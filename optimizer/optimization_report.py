"""Optimization reporting utilities."""
from __future__ import annotations

from pathlib import Path
import csv
import json
from typing import Iterable

from optimizer.optimization_result import OptimizationResult
from utils.logger import get_logger

logger = get_logger(__name__)


class OptimizationReport:
    """Persist optimization output to CSV, JSON and SQLite-friendly JSONL."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._output_dir = output_dir or Path(__file__).parent.parent / "optimization" / "results"
        self._output_dir.mkdir(parents=True, exist_ok=True)

    @property
    def output_dir(self) -> Path:
        return self._output_dir

    def save_csv(self, results: list[OptimizationResult]) -> Path:
        path = self._output_dir / "optimization_results.csv"
        if not results:
            path.write_text("", encoding="utf-8")
            return path

        fieldnames = ["rank", "combinations_tested", "runtime_seconds", "error"]
        parameter_keys = sorted(results[0].parameters.keys())
        metric_keys = sorted(results[0].metrics.keys())
        fieldnames.extend(f"param_{key}" for key in parameter_keys)
        fieldnames.extend(f"metric_{key}" for key in metric_keys)

        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for result in results:
                row = {
                    "rank": result.rank,
                    "combinations_tested": result.combinations_tested,
                    "runtime_seconds": round(result.runtime_seconds, 4),
                    "error": result.error or "",
                }
                row.update({f"param_{key}": value for key, value in result.parameters.items()})
                row.update({f"metric_{key}": value for key, value in result.metrics.items()})
                writer.writerow(row)

        logger.info("Optimization CSV saved: %s", path)
        return path

    def save_json(self, results: list[OptimizationResult]) -> Path:
        path = self._output_dir / "optimization_results.json"
        payload = [result.to_dict() for result in results]
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        logger.info("Optimization JSON saved: %s", path)
        return path

    def save_text_report(self, summary: str) -> Path:
        path = self._output_dir / "optimization_report.txt"
        path.write_text(summary, encoding="utf-8")
        logger.info("Optimization text report saved: %s", path)
        return path
