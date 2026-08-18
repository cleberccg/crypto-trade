import { Box, CircularProgress, Grid2 as Grid, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import {
  Bar,
  BarChart,
  Cell,
  Pie,
  PieChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis
} from "recharts";

import { apiGet } from "../api/client";

export function AnalyticsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["analytics"],
    queryFn: () => apiGet<Record<string, unknown>>("/analytics")
  });

  if (isLoading) {
    return <CircularProgress />;
  }

  const symbolData = (data?.profit_factor_by_symbol as Array<{ symbol: string; value: number }>) ?? [];
  const radarLikeData = [
    { name: "Win Rate", value: Number(data?.win_rate_avg ?? 0) },
    { name: "Drawdown", value: Number(data?.drawdown_avg ?? 0) },
    { name: "Sharpe", value: Number(data?.sharpe_avg ?? 0) },
    { name: "Expectancy", value: Number(data?.expectancy_avg ?? 0) }
  ];

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>
        Analytics
      </Typography>
      <Grid container spacing={2}>
        <Grid size={{ xs: 12, lg: 6 }}>
          <Typography variant="subtitle1" sx={{ mb: 1 }}>
            Profit Factor por Ativo
          </Typography>
          <Box sx={{ height: 320, background: "rgba(255,255,255,0.02)", borderRadius: 2, p: 1.5 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={symbolData}>
                <XAxis dataKey="symbol" stroke="#9aa9b2" />
                <YAxis stroke="#9aa9b2" />
                <Tooltip />
                <Bar dataKey="value" fill="#00d1a0" radius={[6, 6, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </Box>
        </Grid>
        <Grid size={{ xs: 12, lg: 6 }}>
          <Typography variant="subtitle1" sx={{ mb: 1 }}>
            Indicadores Medios
          </Typography>
          <Box sx={{ height: 320, background: "rgba(255,255,255,0.02)", borderRadius: 2, p: 1.5 }}>
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie data={radarLikeData} dataKey="value" nameKey="name" outerRadius={95}>
                  {radarLikeData.map((entry, index) => (
                    <Cell key={entry.name} fill={["#00d1a0", "#ff5f72", "#50b6ff", "#ffc857"][index % 4]} />
                  ))}
                </Pie>
                <Tooltip />
              </PieChart>
            </ResponsiveContainer>
          </Box>
        </Grid>
      </Grid>
    </Box>
  );
}
