import { Grid2 as Grid, Paper, Typography } from "@mui/material";

interface MetricItem {
  label: string;
  value: string | number | null | undefined;
}

interface MetricGridProps {
  items: MetricItem[];
}

export function MetricGrid({ items }: MetricGridProps) {
  return (
    <Grid container spacing={2}>
      {items.map((item) => (
        <Grid size={{ xs: 12, sm: 6, md: 4, lg: 3 }} key={item.label}>
          <Paper sx={{ p: 2, border: "1px solid rgba(255,255,255,0.08)" }}>
            <Typography variant="caption" color="text.secondary">
              {item.label}
            </Typography>
            <Typography variant="h6">{item.value ?? "-"}</Typography>
          </Paper>
        </Grid>
      ))}
    </Grid>
  );
}
