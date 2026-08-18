# Scheduler

The Scheduler stores planned tasks for the platform.

## Purpose
- Register future jobs.
- Keep scheduling disabled until the platform is ready to execute automatically.

## Current State
- Read-only mock scheduler available under `/api/v1/scheduler`.
- Scheduler page available in the dashboard.
- Realtime-safe mock stream available under `/api/v1/ws/scheduler` (authenticated token).

## Future Activation
- Enable automatic execution only after the overnight optimization window finishes and the job execution policy is approved.
- Use `/api/v1/next-phase/readiness` and `/api/v1/next-phase/activation-plan`
	as the pre-activation checklist before enabling real scheduling.
