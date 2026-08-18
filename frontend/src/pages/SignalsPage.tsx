import { DataTablePage } from "../components/DataTablePage";

export function SignalsPage() {
  return (
    <DataTablePage
      title="Sinais"
      endpoint="/signals"
      columns={[
        "id",
        "execution_id",
        "strategy",
        "symbol",
        "timeframe",
        "timestamp",
        "signal",
        "score",
        "accepted",
        "rejection_reason",
        "market_regime"
      ]}
      filters={[
        { key: "symbol", label: "Ativo" },
        { key: "strategy", label: "Estrategia" },
        { key: "signal_type", label: "BUY/SELL" },
        { key: "accepted", label: "Aceito true/false" }
      ]}
    />
  );
}
