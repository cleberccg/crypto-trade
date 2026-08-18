# Frontend Architecture - Dashboard Web

## Overview
The dashboard frontend is implemented with React + TypeScript + Vite and consumes the backend only through REST and WebSocket endpoints. No direct database access is performed by the frontend.

## Stack
- React 18
- TypeScript
- Vite
- Material UI
- TanStack Query
- React Router
- Recharts
- React Hook Form (ready to use in forms)

## Folder Structure
- frontend/src/api: REST client and token handling
- frontend/src/hooks: custom hooks (WebSocket monitor)
- frontend/src/layout: side navigation and responsive shell
- frontend/src/components: reusable cards/tables/metrics
- frontend/src/pages: screen-level pages mapped to routes

## Navigation
The app includes 11 route targets matching the task:
- /
- /executions
- /optimizations
- /backtests
- /trades
- /signals
- /analytics
- /database
- /logs
- /settings
- /monitor

## Data Flow
1. User authenticates via /api/v1/auth/login.
2. JWT token is stored in memory and attached to REST requests.
3. Page components fetch data through TanStack Query.
4. Realtime monitor uses WebSocket /api/v1/ws/monitor.

## API Contract (Current)
- Paginated list endpoints return meta + items.
- Dashboard/analytics/monitor endpoints return snapshot payloads.
- Settings endpoint allows runtime override payloads in API process scope.

## Responsiveness
- Desktop: persistent left drawer + content panel.
- Mobile/tablet: MUI breakpoints are enabled in grid and page spacing.

## Next Frontend Iterations
- Add typed DTO layer for all endpoint contracts.
- Add advanced filters and server-side sorting controls.
- Add richer charts per screen (drawdown, equity curve, heatmaps).
- Persist auth token in secure storage strategy.
- Add route-level role guards (admin/read-only/operator).
