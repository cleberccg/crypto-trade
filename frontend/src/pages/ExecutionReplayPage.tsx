import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Alert, Box, Button, Stack, TextField, Typography } from "@mui/material";

import { apiGet } from "../api/client";
import { PageCard } from "../components/PageCard";
import { SimpleTable } from "../components/SimpleTable";

export function ExecutionReplayPage() {
  const [executionIdInput, setExecutionIdInput] = useState("");
  const [executionId, setExecutionId] = useState("");

  const enabled = executionId.trim().length > 0;

  const replayQuery = useQuery({
    queryKey: ["execution-replay", executionId],
    queryFn: () => apiGet<Record<string, unknown>>(`/execution/${executionId}`),
    enabled
  });

  const timelineQuery = useQuery({
    queryKey: ["execution-replay-timeline", executionId],
    queryFn: () => apiGet<{ items: Record<string, unknown>[] }>(`/execution/${executionId}/timeline`),
    enabled
  });

  const jobsQuery = useQuery({
    queryKey: ["execution-replay-jobs", executionId],
    queryFn: () => apiGet<{ items: Record<string, unknown>[] }>(`/execution/${executionId}/jobs`),
    enabled
  });

  const artifactsQuery = useQuery({
    queryKey: ["execution-replay-artifacts", executionId],
    queryFn: () => apiGet<{ items: Record<string, unknown>[]; logs?: Record<string, unknown>[] }>(`/execution/${executionId}/artifacts`),
    enabled
  });

  const execution = useMemo(() => replayQuery.data?.state as Record<string, unknown> | undefined, [replayQuery.data]);

  return (
    <Stack spacing={2}>
      <Box>
        <Typography variant="h4">Execution Replay</Typography>
        <Typography color="text.secondary">Auditoria completa por execution_id</Typography>
      </Box>

      <PageCard title="Buscar Execucao">
        <Stack direction={{ xs: "column", md: "row" }} spacing={1}>
          <TextField
            fullWidth
            label="Execution ID"
            value={executionIdInput}
            onChange={(event) => setExecutionIdInput(event.target.value)}
          />
          <Button variant="contained" onClick={() => setExecutionId(executionIdInput.trim())}>Abrir Replay</Button>
        </Stack>
      </PageCard>

      {replayQuery.isError ? <Alert severity="error">Falha ao abrir replay</Alert> : null}

      <PageCard title="Resumo">
        <SimpleTable
          columns={["field", "value"]}
          rows={[
            { field: "execution_id", value: String(execution?.execution_id ?? executionId ?? "-") },
            { field: "status", value: String(execution?.status ?? "-") },
            { field: "started_at", value: String(execution?.started_at ?? "-") },
            { field: "finished_at", value: String(execution?.finished_at ?? "-") },
            { field: "progress_pct", value: String(execution?.progress_pct ?? "-") }
          ]}
        />
      </PageCard>

      <PageCard title="Timeline">
        <SimpleTable
          columns={["event_type", "title", "details", "created_at"]}
          rows={timelineQuery.data?.items ?? []}
        />
      </PageCard>

      <PageCard title="Jobs">
        <SimpleTable
          columns={["name", "stage", "status", "processed", "total", "started_at", "finished_at", "error"]}
          rows={jobsQuery.data?.items ?? []}
        />
      </PageCard>

      <PageCard title="Artefatos">
        <SimpleTable
          columns={["name", "path", "size"]}
          rows={artifactsQuery.data?.items ?? []}
        />
      </PageCard>
    </Stack>
  );
}

export default ExecutionReplayPage;
