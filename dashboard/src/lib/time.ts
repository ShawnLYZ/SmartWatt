/** The only module in the dashboard permitted to name a timezone. */
export const KUCHING_TZ = "Asia/Kuching";

export function formatClock(ts: number): string {
  return new Date(ts * 1000).toLocaleTimeString("en-GB", {
    timeZone: KUCHING_TZ,
    hour12: false,
  });
}

export function formatDay(ts: number): string {
  return new Date(ts * 1000).toLocaleDateString("en-GB", {
    timeZone: KUCHING_TZ,
    day: "2-digit",
    month: "short",
    year: "numeric",
  });
}
