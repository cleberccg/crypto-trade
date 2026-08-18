import { Box, CircularProgress, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { SimpleTable } from "../components/SimpleTable";

interface ListPageFactoryProps {
  title: string;
  endpoint: string;
  columns: string[];
}

export function ListPageFactory({ title, endpoint, columns }: ListPageFactoryProps) {
  const { data, isLoading } = useQuery({
    queryKey: [endpoint],
    queryFn: () => apiGet<{ items: Array<Record<string, unknown>> }>(endpoint)
  });

  if (isLoading) {
    return <CircularProgress />;
  }

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>
        {title}
      </Typography>
      <SimpleTable columns={columns} rows={data?.items ?? []} />
    </Box>
  );
}
