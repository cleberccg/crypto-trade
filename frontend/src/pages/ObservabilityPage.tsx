import { Alert, Box, Chip, CircularProgress, Grid2 as Grid, Paper, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { apiGet } from "../api/client";
import { SimpleTable } from "../components/SimpleTable";
import { useObservabilitySocket } from "../hooks/useObservabilitySocket";

interface ObservabilitySnapshot {
  system_time: string;
  running_executions: number;
  last_checkpoint: {
    execution_id: string | null;
    stage: string | null;
    processed: number | null;
    completed: boolean | null;
    created_at: string | null;
    age_seconds: number | null;
  };
  last_optimization: {
    execution_id: string | null;
    status: string | null;
    symbol: string | null;
    timeframe: string | null;
    strategy: string | null;
    total_combinations: number | null;
    processed_combinations: number;
    remaining_combinations: number;
    started_at: string | null;
    finished_at: string | null;
  };
  host: {
    cpu_percent: number;
    ram_percent: number;
  };
  recent_sessions: Array<Record<string, unknown>>;
}

export function ObservabilityPage() {
  const { data: tick, connected } = useObservabilitySocket(true);
  const [progressHistory, setProgressHistory] = useState<Array<{ time: string; processed: number }>>([]);

  const { data, isLoading } = useQuery({
    queryKey: ["observability"],
    queryFn: () => apiGet<ObservabilitySnapshot>("/observability"),
    refetchInterval: 5000
  });

  if (isLoading) {
    return <CircularProgress />;
  }

  const snapshot = tick?.snapshot ?? data;
  const totalCombinations = Number(snapshot?.last_optimization.total_combinations ?? 0);
  const processedCombinations = Number(snapshot?.last_optimization.processed_combinations ?? 0);
  const progressPct = totalCombinations > 0 ? (processedCombinations / totalCombinations) * 100 : 0;

  useEffect(() => {
    if (!snapshot) {
      return;
    }

    const executionId = snapshot.last_optimization.execution_id ?? "";
    const processed = Number(snapshot.last_optimization.processed_combinations ?? 0);
    const timestamp = tick?.timestamp ?? snapshot.system_time;

    if (!executionId || !timestamp) {
      return;
    }

    setProgressHistory((prev) => {
      const last = prev[prev.length - 1];
      if (last && last.processed === processed) {
        return prev;
      }

      const next = [...prev, { time: timestamp, processed }];
      return next.slice(-120);
    });
  }, [snapshot, tick?.timestamp]);

  const eta = useMemo(() => {
    if (progressHistory.length < 2 || totalCombinations <= 0) {
      return "-";
    }

    const first = progressHistory[0];
    const last = progressHistory[progressHistory.length - 1];
    const elapsedSeconds = (Date.parse(last.time) - Date.parse(first.time)) / 1000;
    const processedDelta = last.processed - first.processed;

    if (elapsedSeconds <= 0 || processedDelta <= 0) {
      return "-";
    }

    const speed = processedDelta / elapsedSeconds;
    const remaining = Math.max(0, totalCombinations - processedCombinations);
    const etaSeconds = remaining / speed;
    if (!Number.isFinite(etaSeconds) || etaSeconds <= 0) {
      return "-";
    }

    const hours = Math.floor(etaSeconds / 3600);
    const minutes = Math.floor((etaSeconds % 3600) / 60);
    const seconds = Math.floor(etaSeconds % 60);
    return `${hours}h ${minutes}m ${seconds}s`;
  }, [progressHistory, totalCombinations, processedCombinations]);

  const chartData = useMemo(() => {
    return progressHistory.map((item) => ({
      time: new Date(item.time).toLocaleTimeString(),
      processed: item.processed,
    }));
  }, [progressHistory]);

  const speedPerMinute = useMemo(() => {
    if (progressHistory.length < 2) {
      return 0;
    }

    const first = progressHistory[0];
    const last = progressHistory[progressHistory.length - 1];
    const elapsedMinutes = (Date.parse(last.time) - Date.parse(first.time)) / 60000;
    const processedDelta = last.processed - first.processed;

    if (elapsedMinutes <= 0 || processedDelta <= 0) {
      return 0;
    }

    return processedDelta / elapsedMinutes;
  }, [progressHistory]);

  const stalled = useMemo(() => {
    if (!snapshot?.last_checkpoint.created_at) {
      return false;
    }

    const ageSeconds = Number(snapshot.last_checkpoint.age_seconds ?? 0);
    return ageSeconds > 600 && snapshot.running_executions > 0;
  }, [snapshot]);

  const health = useMemo(() => {
    const cpu = Number(snapshot?.host.cpu_percent ?? 0);
    const ram = Number(snapshot?.host.ram_percent ?? 0);
    const checkpointAge = Number(snapshot?.last_checkpoint.age_seconds ?? 0);
    const running = Number(snapshot?.running_executions ?? 0) > 0;

    if (!running) {
      return { level: "Normal", color: "default" as const, reason: "Sem execucao ativa no momento." };
    }

    if (checkpointAge > 1200 || speedPerMinute <= 0 || cpu >= 95 || ram >= 95) {
      return {
        level: "Critico",
        color: "error" as const,
        reason: "Execucao ativa com risco alto: checkpoint muito antigo, sem progresso ou host saturado.",
      };
    }

    if (checkpointAge > 600 || speedPerMinute < 1 || cpu >= 85 || ram >= 90) {
      return {
        level: "Atencao",
        color: "warning" as const,
        reason: "Sinais de degradacao detectados no ritmo de processamento ou uso de recursos.",
      };
    }

    return {
      level: "Normal",
      color: "success" as const,
      reason: "Execucao seguindo com progresso e recursos em faixa esperada.",
    };
  }, [snapshot, speedPerMinute]);

  const recommendedActions = useMemo(() => {
    if (health.level === "Critico") {
      return [
        "Verificar imediatamente se houve travamento de I/O ou saturacao de disco no host.",
        "Conferir se os workers continuam ativos e processando filas de combinacoes.",
        "Acompanhar checkpoints nos proximos minutos para confirmar retomada do progresso.",
      ];
    }

    if (health.level === "Atencao") {
      return [
        "Monitorar por 5 a 10 minutos se a velocidade volta para a faixa esperada.",
        "Observar crescimento de CPU e RAM para antecipar degradacao.",
        "Confirmar atualizacao regular de checkpoint antes de qualquer intervencao.",
      ];
    }

    return [
      "Manter acompanhamento periodico da evolucao e ETA.",
      "Registrar marcos de progresso para auditoria da execucao longa.",
      "Preservar o ambiente sem alteracoes disruptivas durante o processamento.",
    ];
  }, [health.level]);

  return (
    <Box>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2.5 }}>
        <Typography variant="h4">Observabilidade Operacional</Typography>
        <Chip label={connected ? "Realtime ON" : "Realtime OFF"} color={connected ? "success" : "warning"} size="small" />
      </Stack>

      <Grid container spacing={2}>
        <Grid size={{ xs: 12, md: 6, xl: 3 }}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">Execucoes em andamento</Typography>
            <Typography variant="h5">{snapshot?.running_executions ?? 0}</Typography>
          </Paper>
        </Grid>
        <Grid size={{ xs: 12, md: 6, xl: 3 }}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">CPU do host</Typography>
            <Typography variant="h5">{Number(snapshot?.host.cpu_percent ?? 0).toFixed(1)}%</Typography>
          </Paper>
        </Grid>
        <Grid size={{ xs: 12, md: 6, xl: 3 }}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">RAM do host</Typography>
            <Typography variant="h5">{Number(snapshot?.host.ram_percent ?? 0).toFixed(1)}%</Typography>
          </Paper>
        </Grid>
        <Grid size={{ xs: 12, md: 6, xl: 3 }}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">Progresso da ultima otimizacao</Typography>
            <Typography variant="h5">{progressPct.toFixed(1)}%</Typography>
          </Paper>
        </Grid>
        <Grid size={{ xs: 12, md: 6, xl: 3 }}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">ETA estimado</Typography>
            <Typography variant="h5">{eta}</Typography>
          </Paper>
        </Grid>
        <Grid size={{ xs: 12, md: 6, xl: 3 }}>
          <Paper sx={{ p: 2 }}>
            <Typography variant="caption" color="text.secondary">Velocidade media</Typography>
            <Typography variant="h5">{speedPerMinute.toFixed(2)} comb/min</Typography>
          </Paper>
        </Grid>
        <Grid size={{ xs: 12, md: 12, xl: 6 }}>
          <Paper sx={{ p: 2 }}>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.8 }}>
              <Typography variant="caption" color="text.secondary">Saude da Execucao</Typography>
              <Chip size="small" label={health.level} color={health.color} />
            </Stack>
            <Typography variant="body2">{health.reason}</Typography>
          </Paper>
        </Grid>
      </Grid>

      {stalled && (
        <Alert severity="warning" sx={{ mt: 2 }}>
          Estagnacao detectada: ultimo checkpoint sem atualizacao ha mais de 10 minutos.
        </Alert>
      )}

      <Paper sx={{ p: 2, mt: 2 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.2 }}>
          <Typography variant="h6">Acoes Recomendadas</Typography>
          <Chip size="small" label={health.level} color={health.color} />
        </Stack>
        <Stack spacing={0.8}>
          {recommendedActions.map((action) => (
            <Typography key={action} variant="body2">
              - {action}
            </Typography>
          ))}
        </Stack>
      </Paper>

      <Paper sx={{ p: 2, mt: 2 }}>
        <Typography variant="h6" sx={{ mb: 1.2 }}>Evolucao de combinacoes processadas</Typography>
        <Box sx={{ height: 260 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={chartData}>
              <XAxis dataKey="time" stroke="#9aa9b2" />
              <YAxis stroke="#9aa9b2" allowDecimals={false} />
              <Tooltip />
              <Line type="monotone" dataKey="processed" stroke="#00d1a0" dot={false} />
            </LineChart>
          </ResponsiveContainer>
        </Box>
      </Paper>

      <Paper sx={{ p: 2, mt: 2 }}>
        <Typography variant="h6" sx={{ mb: 1.2 }}>Ultimo checkpoint</Typography>
        <SimpleTable
          columns={["execution_id", "stage", "processed", "completed", "created_at", "age_seconds"]}
          rows={snapshot?.last_checkpoint ? [snapshot.last_checkpoint as unknown as Record<string, unknown>] : []}
        />
      </Paper>

      <Paper sx={{ p: 2, mt: 2 }}>
        <Typography variant="h6" sx={{ mb: 1.2 }}>Ultima otimizacao</Typography>
        <SimpleTable
          columns={[
            "execution_id",
            "status",
            "strategy",
            "symbol",
            "timeframe",
            "total_combinations",
            "processed_combinations",
            "remaining_combinations",
            "started_at",
            "finished_at"
          ]}
          rows={snapshot?.last_optimization ? [snapshot.last_optimization as unknown as Record<string, unknown>] : []}
        />
      </Paper>

      <Paper sx={{ p: 2, mt: 2 }}>
        <Typography variant="h6" sx={{ mb: 1.2 }}>Sessoes recentes</Typography>
        <SimpleTable
          columns={["execution_id", "status", "started_at", "finished_at", "duration", "workers"]}
          rows={snapshot?.recent_sessions ?? []}
        />
      </Paper>
    </Box>
  );
}
