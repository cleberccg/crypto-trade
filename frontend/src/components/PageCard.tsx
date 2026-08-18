import { Paper, Typography } from "@mui/material";
import type { ReactNode } from "react";

interface PageCardProps {
  title: string;
  children: ReactNode;
}

export function PageCard({ title, children }: PageCardProps) {
  return (
    <Paper className="fade-in" sx={{ p: 2.5 }}>
      <Typography variant="h6" sx={{ mb: 2 }}>
        {title}
      </Typography>
      {children}
    </Paper>
  );
}
