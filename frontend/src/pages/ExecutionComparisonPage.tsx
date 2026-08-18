import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button, Grid2 as Grid, Stack, TextField, Typography } from "@mui/material";

import { apiGet } from "../api/client";
import { PageCard } from "../components/PageCard";
import { SimpleTable } from "../components/SimpleTable";

export function ExecutionComparisonPage() {
  const [aInput, setAInput] = useState("");
  const [bInput, setBInput] = useState("");
  const [execA, setExecA] = useState("");
  const [execB, setExecB] = useState("");

  const queryA = useQuery({
    queryKey: ["exec-comparison-a", execA],
    queryFn: () => apiGet<Record<string, unknown>>(`/execution/${execA}/metrics`),
    enabled: execA.length > 0
  });

  const queryB = useQuery({
    queryKey: ["exec-comparison-b", execB],
    queryFn: () => apiGet<Record<string, unknown>>(`/execution/${execB}/metrics`),
    enabled: execB.length > 0
  });

  const rows = useMemo(() => {
    const a = queryA.data ?? {};
    const b = queryB.data ?? {};
    const keys = [
      "total_seconds",
      "avg_cpu",
      "max_cpu",
      "avg_ram",
      "max_ram",
      "combinations",
      "combinations_per_second",
      "checkpoints",
      "heartbeats",
      "incidents",
      "retries",
      "total_jobs",
      "failed_jobs"
    ];

    return keys.map((key) => ({
      metric: key,
      execution_a: String(a[key] ?? "-"),
      execution_b: String(b[key] ?? "-")
    }));
  }, [queryA.data, queryB.data]);

  return (
    <Stack spacing={2}>
      <Typography variant="h4">Execution Comparison</Typography>

      <PageCard title="Selecionar execucoes">
        <Grid container spacing={1}>
          <Grid size={{ xs: 12, md: 5 }}>
            <TextField fullWidth label="Execution A" value={aInput} onChange={(e) => setAInput(e.target.value)} />
          </Grid>
          <Grid size={{ xs: 12, md: 5 }}>
            <TextField fullWidth label="Execution B" value={bInput} onChange={(e) => setBInput(e.target.value)} />
          </Grid>
          <Grid size={{ xs: 12, md: 2 }}>
            <Button fullWidth variant="contained" onClick={() => { setExecA(aInput.trim()); setExecB(bInput.trim()); }}>
              Comparar
            </Button>
          </Grid>
        </Grid>
      </PageCard>

      <PageCard title="Comparativo">
        <SimpleTable columns={["metric", "execution_a", "execution_b"]} rows={rows} />
      </PageCard>
    </Stack>
  );
}

export default ExecutionComparisonPage;
