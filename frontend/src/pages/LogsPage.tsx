import { Box, Button, Stack } from "@mui/material";

import { getApiBaseUrl, getAuthHeaders } from "../api/client";
import { DataTablePage } from "../components/DataTablePage";

export function LogsPage() {
  const download = async () => {
    const response = await fetch(`${getApiBaseUrl()}/logs/download`, {
      headers: getAuthHeaders()
    });
    const text = await response.text();
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "dashboard_logs.txt";
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <Box>
      <Stack direction="row" justifyContent="flex-end" sx={{ mb: 2 }}>
        <Button variant="outlined" onClick={download}>
          Download de Logs
        </Button>
      </Stack>
      <DataTablePage
        title="Logs"
        endpoint="/logs"
        columns={["file", "line"]}
        filters={[
          { key: "level", label: "Nivel" },
          { key: "q", label: "Busca" },
          { key: "limit", label: "Limite" }
        ]}
      />
    </Box>
  );
}
