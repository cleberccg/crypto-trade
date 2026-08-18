import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ResearchComparisonsPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Research Comparisons</Typography>
      <DataTablePage title="Comparisons" endpoint="/research/comparisons" columns={["id", "left_strategy", "right_strategy", "winner", "profit_factor_diff"]} />
    </Box>
  );
}
