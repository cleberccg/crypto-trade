import { useQuery } from "@tanstack/react-query";
import { Alert, Stack, Typography } from "@mui/material";

import { apiGet } from "../api/client";
import { PageCard } from "../components/PageCard";

export function ExecutionManagerWatchdogPage() {
  const query = useQuery({
    queryKey: ["execution-watchdog"],
    queryFn: () => apiGet<{ severity: string; stalled_seconds: number }>("/execution/watchdog"),
    refetchInterval: 3000
  });

  return (
    <Stack spacing={2}>
      <Typography variant="h4">Execution Manager Watchdog</Typography>
      <PageCard title="Watchdog">
        {query.isError ? <Alert severity="error">Falha ao carregar watchdog</Alert> : null}
        <Typography>Severity: {query.data?.severity ?? "-"}</Typography>
        <Typography>Stalled seconds: {query.data?.stalled_seconds ?? "-"}</Typography>
      </PageCard>
    </Stack>
  );
}

export default ExecutionManagerWatchdogPage;
