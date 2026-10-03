import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from smartwatt_server.api import create_app
from smartwatt_server.ingest import TOPIC_EVENT, TOPIC_TELEMETRY
from smartwatt_server.localtime import KUCHING, day_bounds, to_kuching, week_bounds

from .support import example


@pytest.fixture
def client(store):
    app = create_app(store=store, start_ingest=False)
    with TestClient(app) as test_client:
        yield test_client


def _feed(client, topic, payload):
    client.app.state.ingestor.handle(topic, json.dumps(payload).encode())


def test_live_returns_the_latest_snapshot(client):
    _feed(client, TOPIC_TELEMETRY, example("telemetry-single-load"))
    body = client.get("/api/live").json()
    assert body["electrical"]["p"] == pytest.approx(902.5)
    assert body["attribution"]["floor_w"] == pytest.approx(8.5)
    assert body["source"] == "device"


def test_live_on_empty_store_is_null(client):
    assert client.get("/api/live").json() is None


def test_series_includes_the_residual_as_a_series(client):
    """US8: the Unidentified band is a first-class series.

    Payloads are seeded relative to time.time(), not a fixed literal --
    the endpoint windows from "now", so a stale timestamp would read back
    an empty store. 1 s spacing keeps every gap under the integrator's
    10 s cap, so each payload after the first actually writes a ledger
    row; the assertion depends on __residual__ and kettle rows existing.
    """
    now = time.time()
    for n in range(5):
        payload = example("telemetry-single-load")
        payload = {**payload, "ts": now - 10 + n, "seq": n}
        _feed(client, TOPIC_TELEMETRY, payload)
    body = client.get("/api/series?window=15m").json()
    assert "__residual__" in body["series"]
    assert "kettle" in body["series"]


def test_series_never_interpolates_a_gap(client):
    now = time.time()
    for seq, ts in ((0, now - 120), (9, now - 60)):
        payload = example("telemetry-single-load")
        _feed(client, TOPIC_TELEMETRY, {**payload, "ts": ts, "seq": seq})
    body = client.get("/api/series?window=15m").json()
    assert len(body["t"]) == 2


def test_series_alignment_lengths_match(client):
    """S4 stacks each series[appliance_id] against t_min positionally, so
    a length mismatch would make the chart unplottable."""
    now = time.time()
    for n in range(5):
        payload = example("telemetry-single-load")
        payload = {**payload, "ts": now - 10 + n, "seq": n}
        _feed(client, TOPIC_TELEMETRY, payload)
    body = client.get("/api/series?window=15m").json()
    assert body["t_min"]
    for series_values in body["series"].values():
        assert len(series_values) == len(body["t_min"])


def test_events_expose_label_and_attributed_to(client):
    _feed(client, TOPIC_EVENT, example("event-off-reassigned"))
    body = client.get("/api/events?limit=10").json()
    assert body[0]["label"] == "desk_fan"
    assert body[0]["attributed_to"] == "incandescent_lamp"
    assert len(body[0]["neighbours"]) == 3


def test_health_reports_counters(client):
    _feed(client, TOPIC_TELEMETRY, example("telemetry-single-load"))
    client.app.state.ingestor.handle(TOPIC_TELEMETRY, b"{bad")
    body = client.get("/api/health").json()
    assert body["accepted"] == 1
    assert body["rejected_schema"] == 1
    assert body["broker_connected"] is False


def test_health_reports_sampler_counters_only_for_device(client):
    _feed(client, TOPIC_TELEMETRY, example("telemetry-simulator-null-health"))
    body = client.get("/api/health").json()
    assert body["source"] == "simulator"
    assert body["sampler"] is None


def test_month_money_is_string(client):
    body = client.get("/api/month").json()
    assert isinstance(body["bill"]["total"], str)


def test_month_carries_provenance(client):
    body = client.get("/api/month").json()
    provenance = body["carbon"]["local"]["provenance"]
    assert provenance["source"] and provenance["url"] and provenance["vintage"]


def test_ledger_windows(client):
    for window in ("today", "week", "month"):
        assert client.get(f"/api/ledger?window={window}").status_code == 200


#: 02:00 local on Wednesday 19 August 2026. Two hours into a local day and
#: two days into a local week, so a rolling 24 h reaches back into Tuesday
#: and a rolling 7 days back into the previous week -- which is exactly
#: what these two tests need to be able to tell apart.
_WEDNESDAY_0200 = datetime(2026, 8, 19, 2, 0, tzinfo=KUCHING).timestamp()


def _freeze_api_clock(monkeypatch, now_ts: float) -> None:
    """Freeze only api's view of the clock.

    Rebinding the name in api's namespace rather than patching time.time
    globally: the store takes explicit timestamps, and nothing else in the
    process should have its clock moved to 2026.
    """
    monkeypatch.setattr(
        "smartwatt_server.api.time", SimpleNamespace(time=lambda: now_ts)
    )


def test_today_is_a_local_calendar_day_not_a_rolling_24_hours(
    client, store, monkeypatch
):
    """US28's reasoning applied to the day: local calendar, not "24 h ago".

    At 09:00 a rolling window carried fifteen hours of yesterday, which
    could more than double the headline today-cost and contradicted the
    device's own wh_today in the same payload set.
    """
    day_start, _ = day_bounds(_WEDNESDAY_0200)
    assert to_kuching(day_start).hour == 0  # LOCAL midnight, not UTC's

    store.write_ledger(int((day_start - 3600) // 60), "before_midnight", 100.0)
    store.write_ledger(int((day_start + 3600) // 60), "after_midnight", 100.0)
    _freeze_api_clock(monkeypatch, _WEDNESDAY_0200)

    rows = client.get("/api/ledger?window=today").json()["rows"]
    assert {row["appliance_id"] for row in rows} == {"after_midnight"}


def test_week_is_a_local_calendar_week_starting_monday(client, store, monkeypatch):
    week_start, _ = week_bounds(_WEDNESDAY_0200)
    local = to_kuching(week_start)
    assert (local.weekday(), local.hour) == (0, 0)  # Monday, local midnight

    store.write_ledger(int((week_start - 3600) // 60), "last_week", 100.0)
    store.write_ledger(int((week_start + 3600) // 60), "this_week", 100.0)
    _freeze_api_clock(monkeypatch, _WEDNESDAY_0200)

    rows = client.get("/api/ledger?window=week").json()["rows"]
    assert {row["appliance_id"] for row in rows} == {"this_week"}


def test_negative_residual_survives_the_api_unclamped(client):
    """Non-negotiable #2 at the layer where a defensive max(0, ...) gets
    added by someone tidying up a chart. Storage and ingest are covered
    elsewhere; this is the API's own guard.
    """
    now = time.time()
    payload = example("telemetry-negative-residual")
    assert payload["attribution"]["residual_w"] < 0
    for n in range(5):
        _feed(client, TOPIC_TELEMETRY, {**payload, "ts": now - 10 + n, "seq": n})

    assert client.get("/api/live").json()["attribution"]["residual_w"] < 0

    series = client.get("/api/series?window=15m").json()["series"]
    assert series["__residual__"]
    assert all(value < 0 for value in series["__residual__"])


def test_ledger_rejects_an_unknown_window(client):
    assert client.get("/api/ledger?window=fortnight").status_code == 422


def test_appliances_endpoint(client):
    assert client.get("/api/appliances").status_code == 200


def test_whatif_unknown_appliance_is_404(client):
    response = client.post("/api/whatif", json={"appliance_id": "nope", "hours": 4})
    assert response.status_code == 404


def test_unknown_api_path_is_404_not_index(client):
    assert client.get("/api/nonexistent").status_code == 404


def test_unknown_non_api_path_serves_index(client):
    response = client.get("/appliances")
    assert response.status_code == 200
    assert "html" in response.headers["content-type"]


def test_no_cors_headers_configured(client):
    """One origin. A CORS header would mean a development server exists,
    and US73 says there is none at demonstration time."""
    response = client.get("/api/health")
    assert "access-control-allow-origin" not in {
        k.lower() for k in response.headers
    }


def test_websocket_delivers_a_published_payload(client):
    with client.websocket_connect("/api/ws") as ws:
        _feed(client, TOPIC_TELEMETRY, example("telemetry-single-load"))
        message = ws.receive_json()
        assert message["kind"] == "telemetry"
        assert message["payload"]["electrical"]["p"] == pytest.approx(902.5)


def test_slow_websocket_client_is_disconnected(client):
    """Spec: bounded queue, THEN disconnect. Both halves.

    A browser that silently stops draining but stays connected renders a
    gapped live chart with no way to know, and S4's reconnect-and-refetch
    (US10) is triggered by a disconnect that never arrives.

    The flood runs on the app's own event loop through the test session's
    portal, so it completes before the socket handler is scheduled: the
    queue genuinely overflows rather than being drained message by message
    as a race between two threads would allow.
    """
    with client.websocket_connect("/api/ws") as ws:
        bus = client.app.state.bus

        async def flood():
            for n in range(200):  # comfortably past the 64-deep queue
                bus.publish_threadsafe("telemetry", {"seq": n})

        ws.portal.call(flood)
        message = ws.receive()

    assert message["type"] == "websocket.close"
    assert message["code"] == 1011

    # ...and ingest was never back-pressured by any of it.
    assert client.app.state.ingestor.handle(
        TOPIC_TELEMETRY, json.dumps(example("telemetry-single-load")).encode()
    )


def test_startup_blocks_on_empty_provenance(store, monkeypatch):
    """The server refuses to boot rather than serve an unsourceable figure."""
    from smartwatt_tariff import selftest

    def boom(*args, **kwargs):
        raise selftest.ConfigError("empty provenance: test")

    monkeypatch.setattr("smartwatt_server.api.run_selftest", boom)
    with pytest.raises(selftest.ConfigError):
        with TestClient(create_app(store=store, start_ingest=False)):
            pass


# -- the owned-store branch (Important 6) ------------------------------------


class _ShutdownOrderProbe:
    """Stands exactly where MqttIngest stands during lifespan shutdown,
    and records whether the store was still open when stop() was called.

    Ruling E's ordering -- ingest stopped BEFORE the connection closes --
    is what keeps api.py from closing SQLite out from under a write still
    running on paho's thread, which segfaults CPython rather than raising.
    This does not reproduce that hazard: standing in for a real paho
    thread mid-write needs a real broker and a real thread. What it does
    discriminate is the ORDER OF THE TWO STATEMENTS, which is the thing
    a future edit could silently swap: anything running inside stop() can
    see whether close() has already happened, and swapping those two
    lines in api.py makes `connection_at_stop` None and fails this test.
    """

    def __init__(self, store):
        self._store = store
        self.stopped = False
        self.connection_at_stop = object()  # never None, so "not set" is visible

    def stop(self):
        self.stopped = True
        self.connection_at_stop = self._store.connection


def test_an_app_given_no_store_opens_and_closes_its_own(tmp_path):
    """`owned_store = store is None` in create_app -- and every other test
    on this branch passes `store=store`, so `the_store.open(...)` and
    `the_store.close()` had ZERO execution coverage, shutdown ordering
    included.

    Three separate things are checked, and each fails on its own mutation:
      * open ran, and the routes read the store it opened (deleting the
        open() call makes startup raise instead);
      * close ran (deleting it leaves `connection` non-None);
      * close ran AFTER ingest was stopped (swapping the two lines makes
        the probe see a closed connection).
    """
    import dataclasses

    from smartwatt_server.config import settings as load_settings

    db_path = tmp_path / "owned.db"
    config = dataclasses.replace(load_settings(), db_path=db_path)

    app = create_app(settings=config, start_ingest=False)  # store=None
    with TestClient(app) as client:
        owned = client.app.state.store
        assert owned.connection is not None, "the owned store was never opened"
        assert db_path.is_file(), "the owned store opened somewhere else"

        # Not just "a connection exists": the routes must be reading THIS
        # database, seeded through this app's own startup.
        ids = {row["id"] for row in client.get("/api/appliances").json()}
        assert {"incandescent_lamp", "laptop_charger"} <= ids

        client.app.state.mqtt = _ShutdownOrderProbe(owned)

    probe = app.state.mqtt
    assert probe.stopped, "lifespan never stopped ingest"
    assert probe.connection_at_stop is not None, (
        "the store was closed BEFORE ingest was stopped -- Ruling E's ordering"
    )
    assert owned.connection is None, "the owned store was never closed"
