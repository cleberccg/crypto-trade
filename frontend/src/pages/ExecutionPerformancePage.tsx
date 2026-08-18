import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Box, Stack, Typography } from "@mui/material";

import { apiGet } from "../api/client";
import { MetricGrid } from "../components/MetricGrid";
import { PageCard } from "../components/PageCard";
import { SimpleTable } from "../components/SimpleTable";

export function ExecutionPerformancePage() {
  const perfQuery = useQuery({
    queryKey: ["execution-performance-live"],
    queryFn: () => apiGet<Record<string, unknown>>("/execution/performance"),
    refetchInterval: 3000
  });

  const metricsQuery = useQuery({
    queryKey: ["execution-metrics"],
    queryFn: () => apiGet<{ items: Record<string, unknown>[] }>("/execution-metrics?limit=50"),
    refetchInterval: 5000
  });

  const latest = useMemo(() => (metricsQuery.data?.items ?? [])[0] ?? {}, [metricsQuery.data]);

  return (
    <Stack spacing={2}>
      <Box>
        <Typography variant="h4">Execution Performance</Typography>
        <Typography color="text.secondary">CPU, RAM, disco, throughput, ETA e historico</Typography>
      </Box>

      <PageCard title="Recursos em tempo real">
        <MetricGrid
          items={[
            { label: "CPU", value: `${Number(perfQuery.data?.cpu ?? 0).toFixed(2)}%` },
            { label: "RAM", value: `${Number(perfQuery.data?.ram ?? 0).toFixed(2)}%` },
            { label: "Disk", value: `${Number(perfQuery.data?.disk ?? 0).toFixed(2)}%` },
            { label: "Throughput", value: String(latest.combinations_per_second ?? "-") },
            { label: "Avg sec/combo", value: String(latest.avg_seconds_per_combination ?? "-") },
            { label: "Total sec", value: String(latest.total_seconds ?? "-") }
          ]}
        />
      </PageCard>

      <PageCard title="Historico de execucoes">
        <SimpleTable
          columns={[
            "execution_id",
            "status",
            "total_seconds",
            "combinations",
            "combinations_per_second",
            "avg_cpu",
            "max_cpu",
            "avg_ram",
            "max_ram",
            "incidents",
            "retries",
            "created_at"
          ]}
          rows={metricsQuery.data?.items ?? []}
        />
      </PageCard>
    </Stack>
  );
}

export default ExecutionPerformancePage;
