import type { SocketStatus } from "../hooks/useLiveSocket";

export function ConnectionState({
  status,
  stale,
}: {
  status: SocketStatus;
  stale: boolean;
}) {
  const state = status !== "open" ? "down" : stale ? "stale" : "ok";

  return (
    <div
      data-testid="connection"
      data-state={state}
      className="text-xs"
      role="status"
    >
      {state === "down" && (
        <span className="text-warn">Reconnecting to the server…</span>
      )}
      {state === "stale" && (
        <span className="text-warn">No data for 5 s — figures are stale</span>
      )}
      {state === "ok" && <span className="text-ink-muted">Connected</span>}
    </div>
  );
}
