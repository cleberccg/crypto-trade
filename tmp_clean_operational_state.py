from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

DB_URL = "mysql+pymysql://root:@127.0.0.1:3306/crypto_bot"
RESULTS_DIR = Path("D:/xampp/htdocs/crypto/optimization/results")


def main() -> None:
    engine = create_engine(DB_URL)
    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(hours=2)

    with engine.begin() as conn:
        running_sessions_before = conn.execute(
            text("SELECT COUNT(*) FROM execution_sessions WHERE LOWER(status) IN ('running','in_progress')")
        ).scalar_one()
        running_opt_before = conn.execute(
            text("SELECT COUNT(*) FROM optimization_runs WHERE LOWER(status) IN ('running','in_progress')")
        ).scalar_one()

        updated_sessions = conn.execute(
            text(
                """
                UPDATE execution_sessions
                SET status='interrupted',
                    finished_at=COALESCE(finished_at, UTC_TIMESTAMP())
                WHERE LOWER(status) IN ('running','in_progress')
                  AND started_at < :cutoff
                """
            ),
            {"cutoff": stale_cutoff},
        ).rowcount

        updated_opt = conn.execute(
            text(
                """
                UPDATE optimization_runs
                SET status='interrupted',
                    finished_at=COALESCE(finished_at, UTC_TIMESTAMP())
                WHERE LOWER(status) IN ('running','in_progress')
                  AND started_at < :cutoff
                """
            ),
            {"cutoff": stale_cutoff},
        ).rowcount

        deleted_orphan_checkpoints = conn.execute(
            text(
                """
                DELETE c
                FROM execution_checkpoints c
                LEFT JOIN execution_sessions s ON s.execution_id = c.execution_id
                WHERE c.completed = 0
                  AND c.created_at < :cutoff
                  AND s.execution_id IS NULL
                """
            ),
            {"cutoff": stale_cutoff},
        ).rowcount

        deleted_stale_incomplete_checkpoints = conn.execute(
            text(
                """
                DELETE FROM execution_checkpoints
                WHERE completed = 0
                  AND created_at < :cutoff
                """
            ),
            {"cutoff": stale_cutoff},
        ).rowcount

        running_sessions_after = conn.execute(
            text("SELECT COUNT(*) FROM execution_sessions WHERE LOWER(status) IN ('running','in_progress')")
        ).scalar_one()
        running_opt_after = conn.execute(
            text("SELECT COUNT(*) FROM optimization_runs WHERE LOWER(status) IN ('running','in_progress')")
        ).scalar_one()

    transient_files = [
        RESULTS_DIR / "phase13_factory_state.json",
        RESULTS_DIR / "paper_live_state.json",
        RESULTS_DIR / "execution_state.json",
        RESULTS_DIR / "execution_heartbeat.json",
    ]
    transient_files.extend(sorted(RESULTS_DIR.glob("paper_live_state__*.json")))

    removed = 0
    for path in transient_files:
        if path.exists():
            path.unlink()
            removed += 1

    print(f"running_sessions_before={running_sessions_before}")
    print(f"running_optimization_runs_before={running_opt_before}")
    print(f"updated_sessions={updated_sessions}")
    print(f"updated_optimization_runs={updated_opt}")
    print(f"deleted_orphan_checkpoints={deleted_orphan_checkpoints}")
    print(f"deleted_stale_incomplete_checkpoints={deleted_stale_incomplete_checkpoints}")
    print(f"running_sessions_after={running_sessions_after}")
    print(f"running_optimization_runs_after={running_opt_after}")
    print(f"transient_files_removed={removed}")


if __name__ == "__main__":
    main()
