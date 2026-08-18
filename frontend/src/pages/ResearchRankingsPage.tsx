import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ResearchRankingsPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Research Rankings</Typography>
      <DataTablePage title="Rankings" endpoint="/research/rankings" columns={["rank", "strategy", "symbol", "timeframe", "profit_factor", "sharpe"]} />
    </Box>
  );
}
