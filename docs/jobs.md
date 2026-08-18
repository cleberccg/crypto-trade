# Jobs

The Jobs module centralizes platform process orchestration.

## Responsibilities
- Track job status.
- Report progress, workers, CPU, RAM, ETA, and logs.
- Group platform processes such as Download, Optimizer, Validation, Backup, Research, Paper Trading, Live Trading, and Dashboard.

## Current State
- Read-only mock repository and service implemented.
- API endpoints available under `/api/v1/jobs`.
- Dashboard page available as `Jobs`.

## Activation Point
- When long optimizer processing completes, the job repository can be replaced with a persistent implementation without changing the UI contract.
