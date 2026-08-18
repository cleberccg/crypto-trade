import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ResearchReportsPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Research Reports</Typography>
      <DataTablePage title="Reports" endpoint="/research/reports" columns={["id", "name", "status", "generated_at"]} />
    </Box>
  );
}
