import { useQuery } from "@tanstack/react-query";
import { Stack, Typography } from "@mui/material";

import { apiGet } from "../api/client";
import { MetricGrid } from "../components/MetricGrid";
import { PageCard } from "../components/PageCard";

export function ExecutionManagerHeartbeatPage() {
  const query = useQuery({
    queryKey: ["execution-heartbeat"],
    queryFn: () => apiGet<Record<string, unknown>>("/execution/heartbeat"),
    refetchInterval: 3000
  });

  const hb = query.data ?? {};
  return (
    <Stack spacing={2}>
      <Typography variant="h4">Execution Manager Heartbeat</Typography>
      <PageCard title="Heartbeat">
        <MetricGrid
          items={[
            { label: "Timestamp", value: String(hb.timestamp ?? "-") },
            { label: "Execution ID", value: String(hb.execution_id ?? "-") },
            { label: "CPU", value: `${Number(hb.cpu ?? 0).toFixed(2)}%` },
            { label: "RAM", value: `${Number(hb.ram ?? 0).toFixed(2)}%` },
            { label: "Last checkpoint", value: String(hb.last_checkpoint_at ?? "-") },
            { label: "Last DB write", value: String(hb.last_db_write_at ?? "-") }
          ]}
        />
      </PageCard>
    </Stack>
  );
}

export default ExecutionManagerHeartbeatPage;
