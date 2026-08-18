import { Box, Chip, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";
import { SimpleTable } from "../components/SimpleTable";
import { useTimelineSocket } from "../hooks/useTimelineSocket";

export function ExecutionTimelinePage() {
  const { data: tick, connected } = useTimelineSocket(true);
  const { data } = useQuery({
    queryKey: ["timeline-live-fallback"],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>("/timeline"),
    refetchInterval: 5000,
  });

  const rows = tick?.snapshot.items ?? data?.items ?? [];

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2.5 }}>
        <Typography variant="h4">Execution Timeline</Typography>
        <Chip label={connected ? "Realtime ON" : "Realtime OFF"} color={connected ? "success" : "warning"} size="small" />
      </Stack>
      <SimpleTable columns={["id", "event_type", "title", "details", "created_at"]} rows={rows} />
      <Box sx={{ mt: 2 }}>
        <DataTablePage title="Execution Timeline (REST)" endpoint="/timeline" columns={["id", "event_type", "title", "details", "created_at"]} />
      </Box>
    </Box>
  );
}
