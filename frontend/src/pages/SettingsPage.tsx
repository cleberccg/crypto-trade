import { Box, Button, Paper, Stack, TextField, Typography } from "@mui/material";
import { useState } from "react";

import { getRole } from "../api/client";
import { apiPut } from "../api/client";

export function SettingsPage() {
  const [mode, setMode] = useState("paper");
  const [symbol, setSymbol] = useState("BTC/USDT");
  const [timeframe, setTimeframe] = useState("5m");
  const [workers, setWorkers] = useState("16");
  const [message, setMessage] = useState<string>("");
  const role = getRole() ?? "read-only";
  const canEdit = role === "administrator" || role === "operator";

  const onSave = async () => {
    const payload = {
      mode,
      symbol,
      timeframe,
      workers: Number(workers)
    };
    const response = await apiPut<Record<string, unknown>>("/settings", payload);
    setMessage(`Config atualizada: ${JSON.stringify(response)}`);
  };

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 2.5 }}>
        Configuracoes
      </Typography>
      <Paper sx={{ p: 2.5, maxWidth: 560 }}>
        <Stack spacing={2}>
          <TextField disabled={!canEdit} label="Modo" value={mode} onChange={(e) => setMode(e.target.value)} />
          <TextField disabled={!canEdit} label="Ativo" value={symbol} onChange={(e) => setSymbol(e.target.value)} />
          <TextField disabled={!canEdit} label="Timeframe" value={timeframe} onChange={(e) => setTimeframe(e.target.value)} />
          <TextField disabled={!canEdit} label="Workers" value={workers} onChange={(e) => setWorkers(e.target.value)} />
          <Button variant="contained" onClick={onSave} disabled={!canEdit}>
            Salvar
          </Button>
          {!canEdit ? <Typography variant="caption">Seu perfil e somente leitura para configuracoes.</Typography> : null}
          {message ? <Typography variant="body2">{message}</Typography> : null}
        </Stack>
      </Paper>
    </Box>
  );
}
