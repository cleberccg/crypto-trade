import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ResearchPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Research Lab</Typography>
      <DataTablePage title="Research" endpoint="/research" columns={["id", "title", "summary", "category"]} />
    </Box>
  );
}
