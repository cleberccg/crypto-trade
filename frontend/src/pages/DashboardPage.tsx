import { Box, Chip, CircularProgress, Grid2 as Grid, Paper, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { apiGet } from "../api/client";
import { MetricGrid } from "../components/MetricGrid";

export function DashboardPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["dashboard"],
    queryFn: () => apiGet<Record<string, unknown>>("/dashboard")
  });

  const { data: status } = useQuery({
    queryKey: ["dashboard-status"],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>("/dashboard/status")
  });

  if (isLoading) {
    return <CircularProgress />;
  }

  const items = [
    { label: "Status", value: data?.system_status as string },
    { label: "Modo", value: data?.mode as string },
    { label: "Estrategia", value: data?.active_strategy as string },
    { label: "Symbol", value: data?.symbol as string },
    { label: "Timeframe", value: data?.timeframe as string },
    { label: "Capital Inicial", value: data?.capital_initial as number },
    { label: "Capital Atual", value: data?.capital_current as number },
    { label: "Lucro Diario", value: data?.daily_profit as number },
    { label: "Lucro Semanal", value: data?.weekly_profit as number },
    { label: "Lucro Mensal", value: data?.monthly_profit as number },
    { label: "Trades", value: data?.trade_count as number },
    { label: "Sinais", value: data?.signal_count as number },
    { label: "Backtests", value: data?.backtest_count as number },
    { label: "Otimizacoes", value: data?.optimization_count as number },
    { label: "PF Melhor", value: data?.best_profit_factor as number },
    { label: "Sharpe Medio", value: data?.avg_sharpe as number },
    { label: "Expectancia", value: data?.avg_expectancy as number },
    { label: "Drawdown Max", value: data?.max_drawdown as number },
    { label: "CPU", value: data?.cpu as number },
    { label: "RAM", value: data?.ram as number },
    { label: "Tempo Execucao(s)", value: data?.runtime_seconds as number },
    { label: "Ultima Atualizacao", value: data?.updated_at as string }
  ];

  const trendData = [
    { name: "Trade", value: Number(data?.trade_count ?? 0) },
    { name: "Signal", value: Number(data?.signal_count ?? 0) },
    { name: "Backtest", value: Number(data?.backtest_count ?? 0) },
    { name: "Opt", value: Number(data?.optimization_results_count ?? 0) }
  ];

  const platformStatus = status?.items?.[0] ?? {};
  const healthItems = [
    { label: "System Health", value: String(platformStatus.system_health ?? "-") },
    { label: "Realtime", value: String(platformStatus.realtime_status ?? "-") },
    { label: "Workers", value: String(platformStatus.workers ?? "-") },
    { label: "Queue", value: String(platformStatus.execution_queue ?? "-") },
    { label: "CPU", value: String(platformStatus.cpu ?? "-") },
    { label: "RAM", value: String(platformStatus.ram ?? "-") },
    { label: "Disk", value: String(platformStatus.disk ?? "-") },
    { label: "Database", value: String(platformStatus.database ?? "-") },
    { label: "Binance", value: String(platformStatus.binance ?? "-") },
    { label: "API", value: String(platformStatus.api ?? "-") },
    { label: "WebSocket", value: String(platformStatus.websocket ?? "-") },
    { label: "Optimizer", value: String(platformStatus.optimizer ?? "-") },
    { label: "Validation", value: String(platformStatus.validation ?? "-") },
    { label: "Research", value: String(platformStatus.research ?? "-") },
    { label: "Scanner", value: String(platformStatus.scanner ?? "-") }
  ];

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>
        Dashboard Principal
      </Typography>

      <Paper sx={{ p: 2, mb: 2 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5, flexWrap: "wrap" }}>
          <Typography variant="h6">System Health</Typography>
          <Chip label={String(platformStatus.system_health ?? "-")} color={String(platformStatus.system_health ?? "") === "healthy" ? "success" : "warning"} size="small" />
          <Chip label={String(platformStatus.realtime_status ?? "-")} color="info" size="small" />
          <Chip label={`Updated: ${String(platformStatus.updated_at ?? "-")}`} size="small" variant="outlined" />
        </Stack>
        <Grid container spacing={1.5}>
          {healthItems.map((item) => (
            <Grid size={{ xs: 12, sm: 6, md: 4, lg: 3 }} key={item.label}>
              <Paper sx={{ p: 1.5, border: "1px solid rgba(255,255,255,0.08)" }}>
                <Typography variant="caption" color="text.secondary">
                  {item.label}
                </Typography>
                <Typography variant="body1">{item.value}</Typography>
              </Paper>
            </Grid>
          ))}
        </Grid>
      </Paper>

      <MetricGrid items={items} />
      <Grid container spacing={2} sx={{ mt: 1 }}>
        <Grid size={{ xs: 12, lg: 6 }}>
          <Paper sx={{ p: 2, height: 280 }}>
            <Typography variant="subtitle1" sx={{ mb: 1 }}>
              Atividade Geral
            </Typography>
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={trendData}>
                <XAxis dataKey="name" stroke="#9aa9b2" />
                <YAxis stroke="#9aa9b2" />
                <Tooltip />
                <Area type="monotone" dataKey="value" stroke="#00d1a0" fill="rgba(0,209,160,0.28)" />
              </AreaChart>
            </ResponsiveContainer>
          </Paper>
        </Grid>
      </Grid>
    </Box>
  );
}
