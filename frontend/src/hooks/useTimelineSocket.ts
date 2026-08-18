import { useEffect, useState } from "react";

import { wsUrlWithAuth } from "../api/client";

export interface TimelineTick {
  event: string;
  timestamp: string;
  snapshot: { items: Array<Record<string, unknown>> };
}

export function useTimelineSocket(enabled: boolean) {
  const [data, setData] = useState<TimelineTick | null>(null);
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    if (!enabled) {
      return;
    }

    const socket = new WebSocket(wsUrlWithAuth("/ws/timeline"));
    socket.onopen = () => setConnected(true);
    socket.onclose = () => setConnected(false);
    socket.onmessage = (event) => {
      try {
        setData(JSON.parse(event.data) as TimelineTick);
      } catch {
        // Ignore malformed payloads.
      }
    };

    return () => socket.close();
  }, [enabled]);

  return { data, connected };
}
