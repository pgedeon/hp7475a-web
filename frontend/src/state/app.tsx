/** Global app state: toast stack, WS status socket, device status poll,
 *  paper table. Provided by <AppProvider>; consumed via useApp(). */

import {
  createContext, useCallback, useContext, useEffect, useMemo, useRef, useState,
  type ReactNode,
} from "react";
import { api } from "../api/client";
import { useStatusSocket, type StatusSocket } from "../api/ws";
import type { DeviceStatus, PaperInfo, WsMessage } from "../api/types";

export interface Toast {
  id: number;
  kind: "error" | "info" | "ok";
  text: string;
}

interface AppCtx {
  ws: StatusSocket;
  device: DeviceStatus | null;
  deviceError: string | null;
  refreshDevice: () => Promise<void>;
  papers: Record<string, PaperInfo>;
  papersError: string | null;
  retryPapers: () => void;
  toasts: Toast[];
  toast: (kind: Toast["kind"], text: string) => void;
  dismissToast: (id: number) => void;
}

const Ctx = createContext<AppCtx | null>(null);

export function AppProvider({
  children,
  onWsMessage,
}: {
  children: ReactNode;
  onWsMessage?: (m: WsMessage) => void;
}) {
  const wsHandler = useRef(onWsMessage);
  wsHandler.current = onWsMessage;
  const ws = useStatusSocket((m) => wsHandler.current?.(m));

  const [toasts, setToasts] = useState<Toast[]>([]);
  const toastId = useRef(0);
  const toast = useCallback((kind: Toast["kind"], text: string) => {
    const id = ++toastId.current;
    setToasts((t) => [...t, { id, kind, text }]);
    setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), 7000);
  }, []);
  const dismissToast = useCallback((id: number) => {
    setToasts((t) => t.filter((x) => x.id !== id));
  }, []);

  const [device, setDevice] = useState<DeviceStatus | null>(null);
  const [deviceError, setDeviceError] = useState<string | null>(null);
  const refreshDevice = useCallback(async () => {
    try {
      setDevice(await api.deviceStatus());
      setDeviceError(null);
    } catch (e) {
      setDeviceError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => {
    void refreshDevice();
    const t = setInterval(() => void refreshDevice(), 4000);
    return () => clearInterval(t);
  }, [refreshDevice, ws.last]);

  const [papers, setPapers] = useState<Record<string, PaperInfo>>({});
  const [papersError, setPapersError] = useState<string | null>(null);
  const loadPapers = useCallback(async () => {
    try {
      setPapers(await api.getPapers());
      setPapersError(null);
    } catch (e) {
      setPapersError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => { void loadPapers(); }, [loadPapers]);

  const value = useMemo<AppCtx>(() => ({
    ws, device, deviceError, refreshDevice, papers, papersError,
    retryPapers: () => void loadPapers(), toasts, toast, dismissToast,
  }), [ws, device, deviceError, refreshDevice, papers, papersError, loadPapers, toasts, toast, dismissToast]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useApp(): AppCtx {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApp outside AppProvider");
  return ctx;
}
