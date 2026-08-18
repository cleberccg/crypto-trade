import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";
import { ExecutionDetailsDialog } from "../components/ExecutionDetailsDialog";

export function ExecutionsPage() {
  const [selectedExecutionId, setSelectedExecutionId] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ["execution-detail", selectedExecutionId],
    queryFn: () => apiGet<Record<string, unknown>>(`/executions/${selectedExecutionId}`),
    enabled: Boolean(selectedExecutionId)
  });

  return (
    <>
      <DataTablePage
        title="Execucoes"
        endpoint="/executions"
        columns={["execution_id", "status", "started_at", "finished_at", "workers", "cpu", "host"]}
        filters={[
          { key: "status", label: "Status" },
          { key: "execution_type", label: "Tipo" }
        ]}
        onOpenDetails={(row) => setSelectedExecutionId(String(row.execution_id ?? ""))}
      />
      <ExecutionDetailsDialog
        open={Boolean(selectedExecutionId)}
        onClose={() => setSelectedExecutionId(null)}
        payload={data ?? null}
      />
    </>
  );
}
