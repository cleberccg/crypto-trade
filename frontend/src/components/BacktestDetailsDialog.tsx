import { Box, Dialog, DialogContent, DialogTitle, Grid2 as Grid, Typography } from "@mui/material";
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { SimpleTable } from "./SimpleTable";

interface BacktestDetailsDialogProps {
  open: boolean;
  onClose: () => void;
  payload: Record<string, unknown> | null;
}

export function BacktestDetailsDialog({ open, onClose, payload }: BacktestDetailsDialogProps) {
  const backtest = (payload?.backtest as Record<string, unknown> | undefined) ?? {};
  const equityCurve = (payload?.equity_curve as Array<Record<string, unknown>> | undefined) ?? [];
  const trades = (payload?.trades as Array<Record<string, unknown>> | undefined) ?? [];

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="lg">
      <DialogTitle>Detalhes de Backtest</DialogTitle>
      <DialogContent>
        <Grid container spacing={2}>
          <Grid size={{ xs: 12, lg: 4 }}>
            <Typography component="pre" sx={{ whiteSpace: "pre-wrap", margin: 0 }}>
              {JSON.stringify(backtest, null, 2)}
            </Typography>
          </Grid>
          <Grid size={{ xs: 12, lg: 8 }}>
            <Box sx={{ height: 280, mb: 2 }}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart data={equityCurve}>
                  <XAxis dataKey="step" stroke="#95a6b3" />
                  <YAxis stroke="#95a6b3" />
                  <Tooltip />
                  <Area type="monotone" dataKey="equity" stroke="#00d1a0" fill="rgba(0,209,160,0.25)" />
                </AreaChart>
              </ResponsiveContainer>
            </Box>
            <SimpleTable
              columns={[
                "id",
                "side",
                "entry_time",
                "exit_time",
                "entry_price",
                "exit_price",
                "pnl",
                "risk_reward",
                "duration_minutes",
                "exit_reason",
                "score"
              ]}
              rows={trades}
            />
          </Grid>
        </Grid>
      </DialogContent>
    </Dialog>
  );
}
