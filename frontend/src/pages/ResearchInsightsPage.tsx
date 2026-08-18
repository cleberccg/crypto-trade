import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ResearchInsightsPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Research Insights</Typography>
      <DataTablePage title="Insights" endpoint="/research/insights" columns={["id", "category", "title", "summary"]} />
    </Box>
  );
}
