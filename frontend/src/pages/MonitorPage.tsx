import { Box, Chip, Paper, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";

import { apiGet } from "../api/client";
import { useMonitorSocket } from "../hooks/useMonitorSocket";

export function MonitorPage() {
  const { connected, data } = useMonitorSocket(true);
  const { data: snapshot } = useQuery({
    queryKey: ["monitor-snapshot"],
    queryFn: () => apiGet<Record<string, unknown>>("/monitor"),
    refetchInterval: 3000
  });

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2.5 }}>
        <Typography variant="h4">Monitor em Tempo Real</Typography>
        <Chip label={connected ? "Conectado" : "Desconectado"} color={connected ? "success" : "warning"} />
      </Stack>

      <Paper sx={{ p: 2.5 }}>
        <Typography variant="subtitle2" sx={{ mb: 1 }}>
          Preco: {String(snapshot?.price ?? "-")} | CPU: {String(snapshot?.cpu ?? "-")}% | RAM: {String(snapshot?.ram ?? "-")}% | Posicoes abertas: {String(snapshot?.open_positions ?? "-")}
        </Typography>
        <Typography variant="subtitle1" sx={{ mb: 1 }}>
          Stream de Logs
        </Typography>
        <Box component="pre" sx={{ margin: 0, whiteSpace: "pre-wrap", fontSize: 13, opacity: 0.95 }}>
          {(data?.tail ?? []).join("\n") || "Aguardando mensagens do WebSocket..."}
        </Box>
      </Paper>
    </Box>
  );
}
