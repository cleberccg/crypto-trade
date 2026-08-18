import { useQuery } from "@tanstack/react-query";
import { Stack, Typography } from "@mui/material";

import { apiGet } from "../api/client";
import { PageCard } from "../components/PageCard";
import { SimpleTable } from "../components/SimpleTable";

export function ExecutionManagerIncidentsPage() {
  const query = useQuery({
    queryKey: ["execution-incidents"],
    queryFn: () => apiGet<{ items: Record<string, unknown>[] }>("/execution/incidents"),
    refetchInterval: 5000
  });

  return (
    <Stack spacing={2}>
      <Typography variant="h4">Execution Manager Incidents</Typography>
      <PageCard title="Incidentes">
        <SimpleTable
          columns={[
            "id",
            "path"
          ]}
          rows={query.data?.items ?? []}
        />
      </PageCard>
    </Stack>
  );
}

export default ExecutionManagerIncidentsPage;
