import { useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { BacktestDetailsDialog } from "../components/BacktestDetailsDialog";
import { DataTablePage } from "../components/DataTablePage";

export function BacktestsPage() {
  const [selectedExecutionId, setSelectedExecutionId] = useState<string | null>(null);
  const { data } = useQuery({
    queryKey: ["backtest-detail", selectedExecutionId],
    queryFn: () => apiGet<Record<string, unknown>>(`/backtests/${selectedExecutionId}`),
    enabled: Boolean(selectedExecutionId)
  });

  return (
    <>
      <DataTablePage
        title="Backtests"
        endpoint="/backtests"
        columns={[
          "execution_id",
          "strategy",
          "symbol",
          "timeframe",
          "status",
          "initial_capital",
          "final_capital",
          "profit_factor",
          "sharpe",
          "expectancy",
          "drawdown"
        ]}
        filters={[
          { key: "strategy", label: "Estrategia" },
          { key: "symbol", label: "Ativo" }
        ]}
        onOpenDetails={(row) => setSelectedExecutionId(String(row.execution_id ?? ""))}
      />

      <BacktestDetailsDialog
        open={Boolean(selectedExecutionId)}
        onClose={() => setSelectedExecutionId(null)}
        payload={data ?? null}
      />
    </>
  );
}
