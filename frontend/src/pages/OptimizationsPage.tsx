import { Box, MenuItem, Paper, Select, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { apiGet } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";
import { SimpleTable } from "../components/SimpleTable";

export function OptimizationsPage() {
  const [executionId, setExecutionId] = useState<string>("");

  const { data: optimizationList } = useQuery({
    queryKey: ["optimizations-selection"],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>("/optimizations?page=1&page_size=100")
  });

  const { data: ranking } = useQuery({
    queryKey: ["optimization-ranking", executionId],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>(`/optimizations/${executionId}/ranking?limit=100`),
    enabled: executionId.length > 0
  });

  const chartData = useMemo(() => {
    const items = ranking?.items ?? [];
    return items.map((item, index) => ({
      rank: index + 1,
      profit_factor: Number(item.profit_factor ?? 0),
      net_profit: Number(item.net_profit ?? 0)
    }));
  }, [ranking]);

  const options = (optimizationList?.items ?? []).map((item) => String(item.execution_id ?? ""));

  return (
    <Box>
      <DataTablePage
        title="Otimizacoes"
        endpoint="/optimizations"
        columns={[
          "execution_id",
          "strategy",
          "symbol",
          "timeframe",
          "status",
          "processed_combinations",
          "remaining_combinations"
        ]}
        filters={[
          { key: "symbol", label: "Ativo" },
          { key: "timeframe", label: "Timeframe" },
          { key: "status", label: "Status" }
        ]}
      />

      <Paper sx={{ p: 2, mt: 3 }}>
        <Stack direction={{ xs: "column", md: "row" }} spacing={2} alignItems={{ xs: "stretch", md: "center" }}>
          <Typography variant="h6">Ranking Top 100 e Evolucao</Typography>
          <Select value={executionId} onChange={(event) => setExecutionId(String(event.target.value))} displayEmpty size="small" sx={{ minWidth: 360 }}>
            <MenuItem value="">Selecione uma execucao</MenuItem>
            {options.map((id) => (
              <MenuItem value={id} key={id}>
                {id}
              </MenuItem>
            ))}
          </Select>
        </Stack>

        <Box sx={{ mt: 2, height: 280 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <XAxis dataKey="rank" stroke="#98a7b5" />
              <YAxis stroke="#98a7b5" />
              <Tooltip />
              <Line type="monotone" dataKey="profit_factor" stroke="#00d1a0" dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </Box>

        <Box sx={{ mt: 2 }}>
          <SimpleTable
            columns={["id", "profit_factor", "net_profit", "drawdown", "win_rate", "approved"]}
            rows={ranking?.items ?? []}
          />
        </Box>
      </Paper>
    </Box>
  );
}
