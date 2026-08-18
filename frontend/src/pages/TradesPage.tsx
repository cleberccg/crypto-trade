import { DataTablePage } from "../components/DataTablePage";

export function TradesPage() {
  return (
    <DataTablePage
      title="Operacoes"
      endpoint="/trades"
      columns={[
        "id",
        "execution_id",
        "strategy",
        "symbol",
        "timeframe",
        "side",
        "entry_time",
        "exit_time",
        "stop_loss",
        "take_profit",
        "risk_reward",
        "pnl",
        "duration_minutes",
        "exit_reason",
        "score"
      ]}
      filters={[
        { key: "symbol", label: "Ativo" },
        { key: "strategy", label: "Estrategia" },
        { key: "min_pnl", label: "PnL Min" },
        { key: "max_pnl", label: "PnL Max" }
      ]}
    />
  );
}
