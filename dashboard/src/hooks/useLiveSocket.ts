import { useEffect, useRef, useState } from "react";
import type { SmartWattEvent, Telemetry } from "../types/contract";

export type SocketStatus = "connecting" | "open" | "reconnecting";

/**
 * Telemetry older than this is visibly staled rather than displayed as
 * current. A dashboard that keeps rendering the last known value after the
 * device dies is exactly the concealment non-negotiable #2 forbids.
 */
const STALE_AFTER_MS = 5000;

const RECONNECT_BASE_MS = 500;
const RECONNECT_MAX_MS = 8000;

export interface LiveState {
  telemetry: Telemetry | null;
  lastEvent: SmartWattEvent | null;
  status: SocketStatus;
  stale: boolean;
}

/**
 * ONE hook owns connection state, and every screen reads it. That is what
 * stops the connection indicator disagreeing with itself across screens.
 */
export function useLiveSocket(): LiveState {
  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [lastEvent, setLastEvent] = useState<SmartWattEvent | null>(null);
  const [status, setStatus] = useState<SocketStatus>("connecting");
  const [stale, setStale] = useState(false);

  const lastArrival = useRef<number>(Date.now());
  const attempt = useRef(0);

  useEffect(() => {
    // Local to THIS effect instance, deliberately not a useRef. A ref is
    // shared across effect instances, so under React 19 StrictMode's
    // mount/unmount/remount the sequence was: effect1 connects; cleanup1
    // sets closed=true and closes socket1; effect2 sets closed=false;
    // socket1's asynchronous onclose then fires, reads `false`, and
    // flashes "Reconnecting..." while scheduling a retry that NO cleanup
    // owns -- an orphaned socket outliving the component that made it.
    // A plain `let` is captured per instance, so socket1's onclose can
    // only ever see its own disposal flag.
    let disposed = false;
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      socket = new WebSocket(`${scheme}://${window.location.host}/api/ws`);

      socket.onopen = () => {
        attempt.current = 0;
        setStatus("open");
      };

      socket.onmessage = (frame) => {
        try {
          const parsed = JSON.parse(frame.data as string) as unknown;
          // Valid JSON of the wrong shape -- null, an array, a bare
          // number, an object with no `kind` -- must take the failure
          // path below rather than half-executing: reading `.kind` off
          // `null` throws, and the other cases would dispatch nothing
          // anyway. Everything that depends on `parsed` stays inside
          // this try, including the shape check itself.
          if (
            parsed === null ||
            typeof parsed !== "object" ||
            typeof (parsed as { kind?: unknown }).kind !== "string"
          ) {
            throw new TypeError("frame is not a {kind, payload} object");
          }
          const message = parsed as { kind: string; payload: unknown };
          // A frame that parsed AND carries a string `kind` is a frame
          // that genuinely arrived: only now does the staleness clock
          // advance. An unrecognised `kind` still reaches here and still
          // refreshes it -- the connection is alive even when this build
          // doesn't know what to do with the message.
          lastArrival.current = Date.now();
          setStale(false);
          if (message.kind === "telemetry") {
            setTelemetry(message.payload as Telemetry);
          } else if (message.kind === "event") {
            setLastEvent(message.payload as SmartWattEvent);
          }
        } catch (err) {
          // Deliberately does NOT touch lastArrival or stale: a frame the
          // client cannot read as {kind, payload} is a broken stream, and
          // the figures must go stale rather than sit fresh behind a
          // "Connected" indicator (exactly the concealment non-negotiable
          // #2 forbids).
          console.warn(
            "useLiveSocket: could not parse WS frame, ignoring",
            String(frame.data).slice(0, 400),
            err,
          );
        }
      };

      socket.onclose = () => {
        if (disposed) return;
        setStatus("reconnecting");
        const delay = Math.min(
          RECONNECT_BASE_MS * 2 ** attempt.current,
          RECONNECT_MAX_MS,
        );
        attempt.current += 1;
        retry = setTimeout(connect, delay);
      };
    };

    connect();

    const ticker = setInterval(() => {
      setStale(Date.now() - lastArrival.current > STALE_AFTER_MS);
    }, 1000);

    return () => {
      disposed = true;
      clearInterval(ticker);
      if (retry) clearTimeout(retry);
      socket?.close();
    };
  }, []);

  return { telemetry, lastEvent, status, stale };
}
