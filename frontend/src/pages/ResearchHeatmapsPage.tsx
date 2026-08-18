import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ResearchHeatmapsPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Research Heatmaps</Typography>
      <DataTablePage title="Heatmaps" endpoint="/research/heatmaps" columns={["symbol", "timeframe", "value"]} />
    </Box>
  );
}
