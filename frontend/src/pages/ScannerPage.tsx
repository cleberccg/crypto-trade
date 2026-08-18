import { Box, Typography } from "@mui/material";

import { DataTablePage } from "../components/DataTablePage";

export function ScannerPage() {
  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>Market Scanner</Typography>
      <DataTablePage title="Scanner" endpoint="/scanner" columns={["symbol", "liquidity_score", "volatility_score", "volume_score", "spread_score", "trend_score", "momentum_score", "opportunity_score"]} />
    </Box>
  );
}
