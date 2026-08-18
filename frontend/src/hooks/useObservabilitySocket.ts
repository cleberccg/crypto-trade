import { useEffect, useState } from "react";

import { wsUrlWithAuth } from "../api/client";

export interface ObservabilityTick {
  event: string;
  timestamp: string;
  snapshot: {
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
  };
}

export function useObservabilitySocket(enabled: boolean) {
  const [data, setData] = useState<ObservabilityTick | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const socket = new WebSocket(wsUrlWithAuth("/ws/observability"));

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data) as ObservabilityTick);
      } catch {
        // Ignore malformed payloads.
      }
    };

    return () => {
      socket.close();
    };
  }, [enabled]);

  return { data, connected };
}
