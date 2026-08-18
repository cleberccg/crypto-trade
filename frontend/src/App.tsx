import { Suspense, lazy, useEffect, useState } from "react";
import { CircularProgress, Stack } from "@mui/material";
import { Navigate, Route, Routes } from "react-router-dom";

import { AUTH_REQUIRED_EVENT, isAuthenticated } from "./api/client";

import { AppLayout } from "./layout/AppLayout";
import { LoginPage } from "./pages/LoginPage";

const DashboardPage = lazy(() => import("./pages/DashboardPage").then((m) => ({ default: m.DashboardPage })));
const ExecutionsPage = lazy(() => import("./pages/ExecutionsPage").then((m) => ({ default: m.ExecutionsPage })));
const OptimizationsPage = lazy(() => import("./pages/OptimizationsPage").then((m) => ({ default: m.OptimizationsPage })));
const BacktestsPage = lazy(() => import("./pages/BacktestsPage").then((m) => ({ default: m.BacktestsPage })));
const TradesPage = lazy(() => import("./pages/TradesPage").then((m) => ({ default: m.TradesPage })));
const SignalsPage = lazy(() => import("./pages/SignalsPage").then((m) => ({ default: m.SignalsPage })));
const IndicatorsPage = lazy(() => import("./pages/IndicatorsPage").then((m) => ({ default: m.IndicatorsPage })));
const AnalyticsPage = lazy(() => import("./pages/AnalyticsPage").then((m) => ({ default: m.AnalyticsPage })));
const DatabasePage = lazy(() => import("./pages/DatabasePage").then((m) => ({ default: m.DatabasePage })));
const LogsPage = lazy(() => import("./pages/LogsPage").then((m) => ({ default: m.LogsPage })));
const ValidationPage = lazy(() => import("./pages/ValidationPage").then((m) => ({ default: m.ValidationPage })));
const SettingsPage = lazy(() => import("./pages/SettingsPage").then((m) => ({ default: m.SettingsPage })));
const MonitorPage = lazy(() => import("./pages/MonitorPage").then((m) => ({ default: m.MonitorPage })));
const ObservabilityPage = lazy(() => import("./pages/ObservabilityPage").then((m) => ({ default: m.ObservabilityPage })));
const JobsPage = lazy(() => import("./pages/JobsPage").then((m) => ({ default: m.JobsPage })));
const ExecutionTimelinePage = lazy(() => import("./pages/ExecutionTimelinePage").then((m) => ({ default: m.ExecutionTimelinePage })));
const NotificationCenterPage = lazy(() => import("./pages/NotificationCenterPage").then((m) => ({ default: m.NotificationCenterPage })));
const SchedulerPage = lazy(() => import("./pages/SchedulerPage").then((m) => ({ default: m.SchedulerPage })));
const ResearchPage = lazy(() => import("./pages/ResearchPage").then((m) => ({ default: m.ResearchPage })));
const ResearchComparisonsPage = lazy(() => import("./pages/ResearchComparisonsPage").then((m) => ({ default: m.ResearchComparisonsPage })));
const ResearchRankingsPage = lazy(() => import("./pages/ResearchRankingsPage").then((m) => ({ default: m.ResearchRankingsPage })));
const ResearchInsightsPage = lazy(() => import("./pages/ResearchInsightsPage").then((m) => ({ default: m.ResearchInsightsPage })));
const ResearchHeatmapsPage = lazy(() => import("./pages/ResearchHeatmapsPage").then((m) => ({ default: m.ResearchHeatmapsPage })));
const ResearchReportsPage = lazy(() => import("./pages/ResearchReportsPage").then((m) => ({ default: m.ResearchReportsPage })));
const ScannerPage = lazy(() => import("./pages/ScannerPage").then((m) => ({ default: m.ScannerPage })));
const DashboardStatusPage = lazy(() => import("./pages/DashboardStatusPage").then((m) => ({ default: m.DashboardStatusPage })));
const NextPhaseReadinessPage = lazy(() => import("./pages/NextPhaseReadinessPage").then((m) => ({ default: m.NextPhaseReadinessPage })));
const ExecutionManagerPage = lazy(() => import("./pages/ExecutionManagerPage").then((m) => ({ default: m.ExecutionManagerPage })));
const ExecutionManagerWatchdogPage = lazy(() => import("./pages/ExecutionManagerWatchdogPage").then((m) => ({ default: m.ExecutionManagerWatchdogPage })));
const ExecutionManagerIncidentsPage = lazy(() => import("./pages/ExecutionManagerIncidentsPage").then((m) => ({ default: m.ExecutionManagerIncidentsPage })));
const ExecutionManagerHeartbeatPage = lazy(() => import("./pages/ExecutionManagerHeartbeatPage").then((m) => ({ default: m.ExecutionManagerHeartbeatPage })));
const ExecutionReplayPage = lazy(() => import("./pages/ExecutionReplayPage").then((m) => ({ default: m.ExecutionReplayPage })));
const ExecutionPerformancePage = lazy(() => import("./pages/ExecutionPerformancePage").then((m) => ({ default: m.ExecutionPerformancePage })));
const ExecutionComparisonPage = lazy(() => import("./pages/ExecutionComparisonPage").then((m) => ({ default: m.ExecutionComparisonPage })));

function RouteLoader() {
  return (
    <Stack alignItems="center" justifyContent="center" sx={{ minHeight: "50vh" }}>
      <CircularProgress />
    </Stack>
  );
}

export function App() {
  const [authenticated, setAuthenticated] = useState(isAuthenticated());

  useEffect(() => {
    const onAuthRequired = () => setAuthenticated(false);
    window.addEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
    return () => window.removeEventListener(AUTH_REQUIRED_EVENT, onAuthRequired);
  }, []);

  if (!authenticated) {
    return <LoginPage onAuthenticated={() => setAuthenticated(true)} />;
  }

  return (
    <Suspense fallback={<RouteLoader />}>
      <Routes>
        <Route path="/" element={<AppLayout />}>
          <Route index element={<DashboardPage />} />
          <Route path="executions" element={<ExecutionsPage />} />
          <Route path="optimizations" element={<OptimizationsPage />} />
          <Route path="backtests" element={<BacktestsPage />} />
          <Route path="trades" element={<TradesPage />} />
          <Route path="signals" element={<SignalsPage />} />
          <Route path="indicators" element={<IndicatorsPage />} />
          <Route path="analytics" element={<AnalyticsPage />} />
          <Route path="database" element={<DatabasePage />} />
          <Route path="logs" element={<LogsPage />} />
          <Route path="validation" element={<ValidationPage />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="monitor" element={<MonitorPage />} />
          <Route path="observability" element={<ObservabilityPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="timeline" element={<ExecutionTimelinePage />} />
          <Route path="notifications" element={<NotificationCenterPage />} />
          <Route path="scheduler" element={<SchedulerPage />} />
          <Route path="research" element={<ResearchPage />} />
          <Route path="research/comparisons" element={<ResearchComparisonsPage />} />
          <Route path="research/rankings" element={<ResearchRankingsPage />} />
          <Route path="research/insights" element={<ResearchInsightsPage />} />
          <Route path="research/heatmaps" element={<ResearchHeatmapsPage />} />
          <Route path="research/reports" element={<ResearchReportsPage />} />
          <Route path="scanner" element={<ScannerPage />} />
          <Route path="system-status" element={<DashboardStatusPage />} />
          <Route path="next-phase" element={<NextPhaseReadinessPage />} />
          <Route path="execution-manager" element={<ExecutionManagerPage />} />
          <Route path="execution-manager/watchdog" element={<ExecutionManagerWatchdogPage />} />
          <Route path="execution-manager/incidents" element={<ExecutionManagerIncidentsPage />} />
          <Route path="execution-manager/heartbeat" element={<ExecutionManagerHeartbeatPage />} />
          <Route path="execution-manager/replay" element={<ExecutionReplayPage />} />
          <Route path="execution-manager/performance" element={<ExecutionPerformancePage />} />
          <Route path="execution-manager/comparison" element={<ExecutionComparisonPage />} />
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  );
}
