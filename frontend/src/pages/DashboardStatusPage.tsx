import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function DashboardStatusPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>System Status</Typography>
      <DataTablePage title="System Status" endpoint="/dashboard/status" columns={["system_health", "realtime_status", "workers", "execution_queue", "cpu", "ram", "disk", "database", "binance", "api", "websocket", "optimizer", "validation", "research", "scanner", "updated_at"]} />
    </Box>
  );
}
