import { Box, Chip, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";
import { SimpleTable } from "../components/SimpleTable";
import { useSchedulerSocket } from "../hooks/useSchedulerSocket";

export function SchedulerPage() {
  const { data: tick, connected } = useSchedulerSocket(true);
  const { data } = useQuery({
    queryKey: ["scheduler-live-fallback"],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>("/scheduler"),
    refetchInterval: 5000,
  });

  const rows = tick?.snapshot.items ?? data?.items ?? [];

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2.5 }}>
        <Typography variant="h4">Scheduler</Typography>
        <Chip label={connected ? "Realtime ON" : "Realtime OFF"} color={connected ? "success" : "warning"} size="small" />
      </Stack>
      <SimpleTable columns={["id", "name", "schedule", "enabled"]} rows={rows} />
      <Box sx={{ mt: 2 }}>
        <DataTablePage title="Scheduler (REST)" endpoint="/scheduler" columns={["id", "name", "schedule", "enabled"]} />
      </Box>
    </Box>
  );
}
