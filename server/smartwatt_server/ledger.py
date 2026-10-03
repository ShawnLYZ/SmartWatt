"""Per-appliance energy integration.

Energy is integrated SERVER-SIDE from active[].w at 1 Hz. The device's own
wh_session is kept as a cross-check, and the difference between the two is
exposed on /api/health rather than silently resolved in favour of one.
"""

from __future__ import annotations

import logging

from .store import Store

log = logging.getLogger(__name__)

#: Reserved ledger id for power the system cannot attribute.
RESIDUAL_ID = "__residual__"

#: Beyond this gap, the device was away rather than drawing continuously.
#: Integrating across it would invent energy that was never measured.
_MAX_INTEGRATION_GAP_S = 10.0


class LedgerIntegrator:
    """Turns a 1 Hz telemetry stream into per-minute per-appliance energy."""

    def __init__(self, store: Store) -> None:
        self._store = store
        self.last_ts: float | None = None
        self.last_wh_session: float | None = None
        self.resets = 0

    def ingest(self, payload: dict) -> None:
        ts = payload["ts"]
        session = payload["energy"]["wh_session"]

        if self.last_wh_session is not None and session < self.last_wh_session:
            # The device restarted. Record the fact; never write a negative
            # delta that would corrupt the month's totals.
            self.resets += 1
            log.warning("device accumulator reset at ts=%s", ts)
            self.last_wh_session = session
            self.last_ts = ts
            return
        self.last_wh_session = session

        previous = self.last_ts
        self.last_ts = ts
        if previous is None:
            return

        gap = ts - previous
        if gap <= 0 or gap > _MAX_INTEGRATION_GAP_S:
            return

        hours = gap / 3600.0
        ts_min = int(ts // 60)

        for item in payload["attribution"]["active"]:
            self._store.write_ledger(ts_min, item["id"], item["w"] * hours)

        residual = payload["attribution"]["residual_w"]
        self._store.write_ledger(ts_min, RESIDUAL_ID, residual * hours)
