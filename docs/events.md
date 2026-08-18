# Events

This document describes the event-driven integration layer used by the platform.

## Principles
- The optimizer publishes events.
- Listeners consume events independently.
- No listener should require direct calls between dashboard, research, scanner, or notifications.
- Persistence, dashboard, notifications, and analytics are all observers.

## Event Categories
- Optimizer lifecycle
- Backtest lifecycle
- Execution checkpoints
- Job scheduling
- Timeline events
- Notification routing
- Research refresh
- Scanner refresh

## Current Safe Implementation
- The core optimizer event bus already exists in `core/events/`.
- The new platform modules are prepared with read-only mock services.
- Tomorrow, real listeners can be attached without changing the optimizer loop.
