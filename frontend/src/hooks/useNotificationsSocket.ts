import { useEffect, useState } from "react";

import { wsUrlWithAuth } from "../api/client";

export interface NotificationsTick {
  event: string;
  timestamp: string;
  snapshot: { items: Array<Record<string, unknown>> };
}

export function useNotificationsSocket(enabled: boolean) {
  const [data, setData] = useState<NotificationsTick | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const socket = new WebSocket(wsUrlWithAuth("/ws/notifications"));
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data) as NotificationsTick);
      } catch {
        // Ignore malformed payloads.
      }
    };

    return () => socket.close();
  }, [enabled]);

  return { data, connected };
}
