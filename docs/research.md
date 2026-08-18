# Research Lab

The Research Lab consumes finished optimization results after the long run ends.

## Structure
- models
- repositories
- services
- statistics
- analytics
- comparisons
- insights
- heatmaps
- reports

## Current State
- Read-only scaffolding is in place.
- Mock data powers the first dashboard pages.
- No live optimizer data is consumed yet.
- Additional mock endpoints/pages are available for:
	- `/api/v1/research/comparisons`
	- `/api/v1/research/rankings`
	- `/api/v1/research/insights`
	- `/api/v1/research/heatmaps`
	- `/api/v1/research/reports`

## Tomorrow
- Attach the research pipeline to the produced results and refresh rankings, insights, analytics, and reports.

## Phase 2 Research Campaign

Campaign definition file:
- `pipelines/research_phase2.yaml`

Required campaign trigger:
- `python main.py execution-manager --pipeline pipelines/research_phase2.yaml`

Current execution status:
- Stage 1 gate interrupted due to cancelled execution (KeyboardInterrupt).
- Consolidated interruption report: `optimization/results/research_phase2_stage1_report.txt`

Artifacts generated from persisted optimization history:
- `optimization/results/research_dataset.db`
- `optimization/results/research_dataset.csv`
- `optimization/results/research_consolidated.csv`
- `optimization/results/research_top100.csv`
- `optimization/results/research_summary.json`
- `optimization/results/research_summary.txt`
- `optimization/results/research_summary.html`
- `optimization/results/research_summary.pdf`

Selection criteria are parameterized via environment variables:
- `RESEARCH_MIN_PROFIT_FACTOR`
- `RESEARCH_MIN_SHARPE`
- `RESEARCH_MAX_DRAWDOWN`
- `RESEARCH_MIN_TRADES`
- `RESEARCH_MAX_OVERFIT_SCORE`
