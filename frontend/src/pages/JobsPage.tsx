import { Box, CircularProgress, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";

export function JobsPage() {
  const { data, isLoading } = useQuery({
    queryKey: ["jobs-status"],
    queryFn: () => apiGet<Record<string, unknown>>("/jobs")
  });

  if (isLoading) {
    return <CircularProgress />;
  }

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Jobs</Typography>
      <Typography variant="body2" sx={{ mb: 2 }}>Status da fila: {String(data?.meta ? (data.meta as Record<string, unknown>).running : "-")}</Typography>
      <DataTablePage title="Jobs" endpoint="/jobs" columns={["id", "name", "job_type", "status", "progress_pct", "worker", "cpu_pct", "ram_pct", "eta_seconds"]} />
    </Box>
  );
}
