/** Bytes progress bar (transmitted/total). Percent labeled as estimate when
 *  total is unknown (backend streams may report bytes_total=0 pre-prepare). */
export default function Progress({
  value, total, compact = false,
}: { value: number; total: number; compact?: boolean }) {
  const pct = total > 0 ? Math.min(100, (value / total) * 100) : 0;
  const kb = (n: number) => `${(n / 1024).toFixed(1)} KB`;
  return (
    <div className={compact ? "progress compact" : "progress"} role="progressbar"
      aria-valuenow={value} aria-valuemax={total} aria-label="bytes sent">
      <div className="progress-bar" style={{ width: `${pct}%` }} />
      <span className="progress-text">
        {total > 0 ? `${pct.toFixed(1)}% · ` : ""}{kb(value)}{total > 0 ? ` / ${kb(total)}` : ""}
      </span>
    </div>
  );
}
