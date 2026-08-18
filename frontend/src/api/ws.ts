/** WebSocket hook for /api/ws/status with auto-reconnect + event log.
 *  URL: VITE_WS_URL override wins (workaround if the Vite ws proxy
 *  misbehaves), else ws(s)://<location.host>/api/ws/status via proxy. */

import { useEffect, useRef, useState } from "react";
import type { WsMessage } from "./types";

export type WsState = "connecting" | "open" | "closed";

const MAX_LOG = 50;

function wsUrl(): string {
  const override: string | undefined = import.meta.env.VITE_WS_URL;
  if (override) return override;
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}/api/ws/status`;
}

export interface StatusSocket {
  state: WsState;
  /** Most recent message (for one-shot consumers). */
  last: WsMessage | null;
  /** Ring buffer of recent raw events (diagnostics log). */
  log: WsMessage[];
  /** Milliseconds since epoch of the last reconnect attempt (0 = never). */
  lastAttempt: number;
}

/**
 * Single connection per hook instance; exponential-ish backoff reconnect
 * (1s → 2s → 5s cap) so a backend restart recovers automatically.
 */
export function useStatusSocket(onMessage?: (m: WsMessage) => void): StatusSocket {
  const handler = useRef(onMessage);
  handler.current = onMessage;

  const [state, setState] = useState<WsState>("connecting");
  const [last, setLast] = useState<WsMessage | null>(null);
  const [log, setLog] = useState<WsMessage[]>([]);
  const [lastAttempt, setLastAttempt] = useState(0);

  useEffect(() => {
    let closed = false;
    let attempt = 0;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      if (closed) return;
      setState("connecting");
      setLastAttempt(Date.now());
      try {
        ws = new WebSocket(wsUrl());
      } catch {
        schedule();
        return;
      }
      ws.onopen = () => { attempt = 0; setState("open"); };
      ws.onmessage = (ev: MessageEvent<string>) => {
        try {
          const msg = JSON.parse(ev.data) as WsMessage;
          setLast(msg);
          setLog((prev) => [...prev.slice(-(MAX_LOG - 1)), msg]);
          handler.current?.(msg);
        } catch { /* malformed frame — ignore */ }
      };
      ws.onclose = () => {
        ws = null;
        if (!closed) { setState("closed"); schedule(); }
      };
      ws.onerror = () => ws?.close();
    };

    const schedule = () => {
      attempt += 1;
      const delay = Math.min(5000, 1000 * 2 ** Math.min(attempt - 1, 3));
      timer = setTimeout(connect, delay);
    };

    connect();
    return () => {
      closed = true;
      if (timer) clearTimeout(timer);
      ws?.close();
    };
  }, []);

  return { state, last, log, lastAttempt };
}
