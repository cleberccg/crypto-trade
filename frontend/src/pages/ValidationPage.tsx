import { DataTablePage } from "../components/DataTablePage";

export function ValidationPage() {
  return (
    <DataTablePage
      title="Validacoes"
      endpoint="/validation"
      columns={[
        "execution_id",
        "optimizer_run",
        "total_tested",
        "approved",
        "rejected",
        "min_profit_factor",
        "max_drawdown",
        "validation_status",
        "created_at"
      ]}
    />
  );
}
