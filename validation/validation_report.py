"""Persist validation reports as CSV, JSON and text."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from validation.validation_result import ValidationEntry


class ValidationReport:
    """Outputs validation artifacts into optimization/results."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._output_dir = output_dir or Path(__file__).parent.parent / "optimization" / "results"
        self._output_dir.mkdir(parents=True, exist_ok=True)

    def save_csv(self, entries: list[ValidationEntry]) -> Path:
        path = self._output_dir / "validation_report.csv"
        fieldnames = [
            "rank",
            "passed",
            "overfitting_risk",
            "discard_reasons",
            "train_total_trades",
            "train_profit_factor",
            "train_win_rate",
            "train_expectancy",
            "train_sharpe_ratio",
            "train_max_drawdown_pct",
            "validation_total_trades",
            "validation_profit_factor",
            "validation_win_rate",
            "validation_expectancy",
            "validation_sharpe_ratio",
            "validation_max_drawdown_pct",
            "parameters_json",
        ]
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for entry in entries:
                writer.writerow(
                    {
                        "rank": entry.rank,
                        "passed": entry.passed,
                        "overfitting_risk": entry.overfitting_risk,
                        "discard_reasons": " | ".join(entry.discard_reasons),
                        "train_total_trades": entry.train_metrics.get("total_trades", 0),
                        "train_profit_factor": entry.train_metrics.get("profit_factor", 0.0),
                        "train_win_rate": entry.train_metrics.get("win_rate", 0.0),
                        "train_expectancy": entry.train_metrics.get("expectancy", 0.0),
                        "train_sharpe_ratio": entry.train_metrics.get("sharpe_ratio", 0.0),
                        "train_max_drawdown_pct": entry.train_metrics.get("max_drawdown_pct", 0.0),
                        "validation_total_trades": entry.validation_metrics.get("total_trades", 0),
                        "validation_profit_factor": entry.validation_metrics.get("profit_factor", 0.0),
                        "validation_win_rate": entry.validation_metrics.get("win_rate", 0.0),
                        "validation_expectancy": entry.validation_metrics.get("expectancy", 0.0),
                        "validation_sharpe_ratio": entry.validation_metrics.get("sharpe_ratio", 0.0),
                        "validation_max_drawdown_pct": entry.validation_metrics.get("max_drawdown_pct", 0.0),
                        "parameters_json": json.dumps(entry.parameters, ensure_ascii=False),
                    }
                )
        return path

    def save_json(self, entries: list[ValidationEntry]) -> Path:
        path = self._output_dir / "validation_report.json"
        path.write_text(
            json.dumps([entry.to_dict() for entry in entries], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path

    def save_text(self, report_text: str) -> Path:
        path = self._output_dir / "validation_report.txt"
        path.write_text(report_text, encoding="utf-8")
        return path
