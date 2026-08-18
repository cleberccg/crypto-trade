import { DataTablePage } from "../components/DataTablePage";

export function DatabasePage() {
  return (
    <DataTablePage
      title="Banco de Dados (Somente Leitura)"
      endpoint="/database"
      columns={["table", "rows", "row"]}
      filters={[{ key: "table_name", label: "Tabela" }]}
    />
  );
}
