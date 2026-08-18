import { Box, Chip, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";
import { SimpleTable } from "../components/SimpleTable";
import { useNotificationsSocket } from "../hooks/useNotificationsSocket";

export function NotificationCenterPage() {
  const { data: tick, connected } = useNotificationsSocket(true);
  const { data } = useQuery({
    queryKey: ["notifications-live-fallback"],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>("/notifications"),
    refetchInterval: 5000,
  });

  const rows = tick?.snapshot.items ?? data?.items ?? [];

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2.5 }}>
        <Typography variant="h4">Notification Center</Typography>
        <Chip label={connected ? "Realtime ON" : "Realtime OFF"} color={connected ? "success" : "warning"} size="small" />
      </Stack>
      <SimpleTable columns={["id", "channel", "title", "message", "status", "created_at"]} rows={rows} />
      <Box sx={{ mt: 2 }}>
        <DataTablePage title="Notifications (REST)" endpoint="/notifications" columns={["id", "channel", "title", "message", "status", "created_at"]} />
      </Box>
    </Box>
  );
}
