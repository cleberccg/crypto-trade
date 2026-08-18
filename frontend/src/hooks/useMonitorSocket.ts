import { useEffect, useState } from "react";

import { wsUrl } from "../api/client";

export interface MonitorTick {
  event: string;
  timestamp: string;
  tail: string[];
}

export function useMonitorSocket(enabled: boolean) {
  const [data, setData] = useState<MonitorTick | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const socket = new WebSocket(wsUrl("/ws/monitor"));

    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data) as MonitorTick);
      } catch {
        // Ignore malformed messages.
      }
    };

    return () => {
      socket.close();
    };
  }, [enabled]);

  return { data, connected };
}
