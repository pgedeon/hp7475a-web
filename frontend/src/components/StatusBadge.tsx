import type { JobStatus } from "../api/types";

/** Status → color class map shared by jobs, device, connection badges. */
const CLASSES: Record<string, string> = {
  QUEUED: "badge info", PREPARING: "badge busy", READY: "badge ok",
  SENDING: "badge busy", PLOTTING: "badge busy", COMPLETING: "badge busy",
  COMPLETED: "badge ok", PAUSED: "badge warn", CANCELLED: "badge info",
  FAILED: "badge err", DISCONNECTED: "badge err",
};

export default function StatusBadge({ status }: { status: JobStatus | string }) {
  return <span className={CLASSES[status] ?? "badge info"} data-status={status}>{status}</span>;
}
