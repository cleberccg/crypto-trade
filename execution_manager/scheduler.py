from __future__ import annotations

import os

from execution_manager.execution_models import ExecutionJob


class ExecutionScheduler:
    def build_default_jobs(self, execution_id: str) -> list[ExecutionJob]:
        combos = int(os.getenv("EXECUTION_MANAGER_COMBINATIONS", "10000"))
        jobs = [
            ExecutionJob(name="Download BTC", stage="download_btc", total=1, execution_id=execution_id),
            ExecutionJob(name="Download ETH", stage="download_eth", total=1, execution_id=execution_id),
            ExecutionJob(name="Download SOL", stage="download_sol", total=1, execution_id=execution_id),
            ExecutionJob(name="Smoke Test", stage="smoke", total=min(500, combos), execution_id=execution_id),
            ExecutionJob(name="Optimizer BTC 5m", stage="optimizer_btc_5m", total=combos, execution_id=execution_id),
            ExecutionJob(name="Optimizer BTC 15m", stage="optimizer_btc_15m", total=combos, execution_id=execution_id),
            ExecutionJob(name="Optimizer ETH 5m", stage="optimizer_eth_5m", total=combos, execution_id=execution_id),
            ExecutionJob(name="Optimizer ETH 15m", stage="optimizer_eth_15m", total=combos, execution_id=execution_id),
            ExecutionJob(name="Optimizer SOL 5m", stage="optimizer_sol_5m", total=combos, execution_id=execution_id),
            ExecutionJob(name="Optimizer SOL 15m", stage="optimizer_sol_15m", total=combos, execution_id=execution_id),
            ExecutionJob(name="Validation", stage="validation", total=1, execution_id=execution_id),
            ExecutionJob(name="Research", stage="research", total=1, execution_id=execution_id),
            ExecutionJob(name="Analytics", stage="analytics", total=1, execution_id=execution_id),
            ExecutionJob(name="Backup", stage="backup", total=1, execution_id=execution_id),
        ]
        return jobs
