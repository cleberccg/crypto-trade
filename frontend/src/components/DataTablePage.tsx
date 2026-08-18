import {
  Alert,
  Box,
  Button,
  CircularProgress,
  Grid2 as Grid,
  Paper,
  Stack,
  TextField,
  Typography
} from "@mui/material";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import type { PaginatedResponse } from "../api/types";
import { SimpleTable } from "./SimpleTable";

interface DataTablePageProps {
  title: string;
  endpoint: string;
  columns: string[];
  defaultPageSize?: number;
  filters?: Array<{ key: string; label: string }>;
  onOpenDetails?: (row: Record<string, unknown>) => void;
}

export function DataTablePage({
  title,
  endpoint,
  columns,
  defaultPageSize = 30,
  filters = [],
  onOpenDetails
}: DataTablePageProps) {
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(defaultPageSize);
  const [filterValues, setFilterValues] = useState<Record<string, string>>({});

  const queryString = useMemo(() => {
    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("page_size", String(pageSize));
    Object.entries(filterValues).forEach(([key, value]) => {
      if (value.trim().length > 0) {
        params.set(key, value);
      }
    });
    return params.toString();
  }, [page, pageSize, filterValues]);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: [endpoint, queryString],
    queryFn: () => apiGet<PaginatedResponse<Record<string, unknown>>>(`${endpoint}?${queryString}`)
  });

  const total = data?.meta.total ?? 0;
  const totalPages = Math.max(1, Math.ceil(total / pageSize));

  return (
    <Box>
      <Stack
        direction={{ xs: "column", md: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", md: "center" }}
        sx={{ mb: 2.5 }}
        spacing={1.5}
      >
        <Typography variant="h4">{title}</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          {isFetching ? <CircularProgress size={18} /> : null}
          <Button variant="outlined" onClick={() => refetch()}>
            Atualizar
          </Button>
        </Stack>
      </Stack>

      <Paper sx={{ p: 2, mb: 2 }}>
        <Grid container spacing={1.5}>
          {filters.map((filter) => (
            <Grid size={{ xs: 12, sm: 6, md: 3 }} key={filter.key}>
              <TextField
                fullWidth
                size="small"
                label={filter.label}
                value={filterValues[filter.key] ?? ""}
                onChange={(event) => {
                  setPage(1);
                  setFilterValues((prev) => ({ ...prev, [filter.key]: event.target.value }));
                }}
              />
            </Grid>
          ))}
        </Grid>
      </Paper>

      {isLoading ? <CircularProgress /> : null}
      {isError ? (
        <Alert severity="error">{(error as Error)?.message ?? "Erro ao carregar dados"}</Alert>
      ) : null}
      {!isLoading && !isError ? (
        <SimpleTable columns={columns} rows={data?.items ?? []} onRowClick={onOpenDetails} />
      ) : null}

      <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
        <Button variant="outlined" disabled={page <= 1} onClick={() => setPage((value) => Math.max(1, value - 1))}>
          Anterior
        </Button>
        <Typography sx={{ alignSelf: "center" }}>
          Pagina {page} de {totalPages} ({total} registros)
        </Typography>
        <Button
          variant="outlined"
          disabled={page >= totalPages}
          onClick={() => setPage((value) => Math.min(totalPages, value + 1))}
        >
          Proxima
        </Button>
        <TextField
          size="small"
          label="Page size"
          value={String(pageSize)}
          onChange={(event) => {
            const next = Number(event.target.value || "30");
            setPage(1);
            setPageSize(Math.max(1, Math.min(200, next)));
          }}
          sx={{ width: 120 }}
        />
      </Stack>
    </Box>
  );
}
