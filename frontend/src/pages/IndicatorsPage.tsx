import { DataTablePage } from "../components/DataTablePage";

export function IndicatorsPage() {
  return (
    <DataTablePage
      title="Indicadores"
      endpoint="/indicators"
      columns={[
        "id",
        "signal_id",
        "ema_fast",
        "ema_slow",
        "ema_trend",
        "rsi",
        "atr",
        "volume",
        "volume_average",
        "close",
        "high",
        "low",
        "created_at"
      ]}
      filters={[
        { key: "min_rsi", label: "RSI Min" },
        { key: "max_rsi", label: "RSI Max" }
      ]}
    />
  );
}
