import { useQuery } from "@tanstack/react-query";
import { Alert, Box, Button, Chip, Grid2 as Grid, Stack, Typography } from "@mui/material";

import { apiGet, apiPost } from "../api/client";
import { MetricGrid } from "../components/MetricGrid";
import { PageCard } from "../components/PageCard";
import { SimpleTable } from "../components/SimpleTable";

async function getExecution() {
  return apiGet<{ execution: Record<string, unknown>; queue: Record<string, unknown> }>("/execution");
}

async function getJobs() {
  return apiGet<{ items: Record<string, unknown>[] }>("/execution/jobs");
}

export function ExecutionManagerPage() {
  const executionQuery = useQuery({ queryKey: ["execution-manager"], queryFn: getExecution, refetchInterval: 3000 });
  const jobsQuery = useQuery({ queryKey: ["execution-manager-jobs"], queryFn: getJobs, refetchInterval: 3000 });

  const execution = executionQuery.data?.execution ?? {};
  const queue = executionQuery.data?.queue ?? {};

  const handleAction = async (path: string) => {
    await apiPost(path, {});
    await executionQuery.refetch();
    await jobsQuery.refetch();
  };

  return (
    <Stack spacing={2}>
      <Box>
        <Typography variant="h4">Execution Manager</Typography>
        <Typography color="text.secondary">Resumo operacional em tempo real</Typography>
      </Box>

      {executionQuery.isError ? <Alert severity="error">Falha ao carregar estado da execucao</Alert> : null}

      <PageCard title="Status">
        <MetricGrid
          items={[
            { label: "Execution ID", value: String(execution.execution_id ?? "-") },
            { label: "Status", value: String(execution.status ?? "Idle") },
            { label: "Progress", value: `${Number(execution.progress_pct ?? 0).toFixed(2)}%` },
            { label: "Processed", value: `${execution.processed_total ?? 0}/${execution.target_total ?? 0}` },
            { label: "CPU", value: `${Number(execution.cpu ?? 0).toFixed(2)}%` },
            { label: "RAM", value: `${Number(execution.ram ?? 0).toFixed(2)}%` }
          ]}
        />
        <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
          <Button variant="outlined" onClick={() => void handleAction("/execution/pause")}>Pause</Button>
          <Button variant="outlined" onClick={() => void handleAction("/execution/resume")}>Resume</Button>
          <Button variant="outlined" color="warning" onClick={() => void handleAction("/execution/retry")}>Retry</Button>
          <Button variant="contained" color="error" onClick={() => void handleAction("/execution/cancel")}>Cancel</Button>
        </Stack>
      </PageCard>

      <Grid container spacing={2}>
        <Grid size={{ xs: 12, md: 6 }}>
          <PageCard title="Fila">
            <Stack direction="row" spacing={1}>
              <Chip label={`Waiting: ${queue.waiting ?? 0}`} />
              <Chip label={`Running: ${queue.running ?? 0}`} color="primary" />
              <Chip label={`Failed: ${queue.failed ?? 0}`} color="error" />
              <Chip label={`Total: ${queue.total ?? 0}`} variant="outlined" />
            </Stack>
          </PageCard>
        </Grid>
        <Grid size={{ xs: 12, md: 6 }}>
          <PageCard title="Pipeline">
            <Typography>Barra de progresso</Typography>
            <Box sx={{ mt: 1, p: 1.5, borderRadius: 2, bgcolor: "rgba(255,255,255,0.06)", fontFamily: "monospace" }}>
              {"████████████████░░░░"} {Number(execution.progress_pct ?? 0).toFixed(0)}%
            </Box>
            <Typography sx={{ mt: 1 }} color="text.secondary">
              ETA: {execution.eta_seconds ? `${Math.round(Number(execution.eta_seconds) / 60)}m` : "-"}
            </Typography>
          </PageCard>
        </Grid>
      </Grid>

      <PageCard title="Jobs">
        <SimpleTable
          columns={[
            "name",
            "status",
            "processed",
            "total",
            "error"
          ]}
          rows={jobsQuery.data?.items ?? []}
        />
      </PageCard>
    </Stack>
  );
}

export default ExecutionManagerPage;
