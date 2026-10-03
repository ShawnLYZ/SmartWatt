"""Seam 1 end to end: simulator payloads in, database state and API out.

No hardware, no firmware. This is the seam that carries the majority of
coverage in the whole project.
"""

import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from smartwatt_server.api import create_app
from smartwatt_server.ingest import TOPIC_EVENT, TOPIC_TELEMETRY
from smartwatt_server.localtime import KUCHING, day_bounds

TOPIC = {"telemetry": TOPIC_TELEMETRY, "event": TOPIC_EVENT}

#: smartwatt_sim.scenarios.run defaults start_ts to a fixed literal that is
#: already over a year in the past. /api/ledger and /api/month both window
#: to the CURRENT local day, week or billing month, so a run seeded at that
#: default lands entirely outside every one of them and the windowed
#: assertions below would read back an empty result instead of the run's
#: own data. demo, the longest scenario, runs 125 s; five minutes of
#: lead-in keeps every scenario's whole timeline inside those windows
#: regardless of when the suite happens to run. One function so the call
#: sites below share it rather than drifting apart.
_LEAD_IN_S = 300.0


def _recent_start_ts(now: float | None = None) -> float:
    """Recent, but never earlier than local midnight.

    /api/ledger?window=today is a LOCAL CALENDAR day, so an unclamped
    five-minute lead-in puts the run in yesterday whenever the suite runs
    within five minutes after midnight in Asia/Kuching -- and, for the same
    reason, in last month on the 1st, which was a known flake here.
    Clamping to the day start covers the week and month windows too, since
    today is contained in both.

    Within the first ~2 minutes of a local day there is not enough room
    left for a whole scenario before "now", so the tail of the longest run
    can land after the endpoints' now+60 bound. The assertions below only
    need the run's opening minutes, which are always inside the window.
    """
    now = time.time() if now is None else now
    day_start, _ = day_bounds(now)
    return max(now - _LEAD_IN_S, day_start)


def test_lead_in_never_precedes_the_local_day_start():
    """The clamp above, at the two instants that matter.

    Real time is unusable for this: the difference only shows up in the
    five minutes after local midnight.
    """
    just_after_midnight = datetime(2026, 8, 19, 0, 1, tzinfo=KUCHING).timestamp()
    day_start, _ = day_bounds(just_after_midnight)
    assert _recent_start_ts(just_after_midnight) == day_start

    midday = datetime(2026, 8, 19, 12, 0, tzinfo=KUCHING).timestamp()
    assert _recent_start_ts(midday) == midday - _LEAD_IN_S


@pytest.fixture
def client(store):
    app = create_app(store=store, start_ingest=False)
    with TestClient(app) as test_client:
        yield test_client


def _drive(client, scenario: str) -> int:
    from smartwatt_sim.scenarios import run

    count = 0
    for kind, payload in run(scenario, start_ts=_recent_start_ts()):
        accepted = client.app.state.ingestor.handle(
            TOPIC[kind], json.dumps(payload).encode()
        )
        assert accepted, f"{kind} payload rejected: {payload}"
        count += 1
    return count


@pytest.mark.parametrize(
    "scenario",
    ["baseline", "single", "overlap", "simultaneous",
     "unknown", "below-floor", "cliff", "demo"],
)
def test_every_scenario_ingests_without_rejection(client, scenario):
    """The simulator and the device are interchangeable publishers."""
    assert _drive(client, scenario) > 0
    assert client.app.state.ingestor.rejected == 0


@pytest.mark.parametrize("window", ["today", "week", "month"])
def test_demo_run_populates_the_ledger(client, window):
    """Every window, not just the month.

    The spec says "__residual__ and unknown_N rows included" for
    /api/ledger without qualification, and US37 wants the ledger to add up
    in whichever window the household is looking at.
    """
    _drive(client, "demo")
    rows = client.get(f"/api/ledger?window={window}").json()["rows"]
    ids = {row["appliance_id"] for row in rows}
    assert "__residual__" in ids
    assert ids & {"kettle", "desk_fan", "incandescent_lamp"}


def test_unknown_scenario_energy_reaches_the_ledger(client):
    """US14 end to end."""
    _drive(client, "unknown")
    ids = {
        row["appliance_id"]
        for row in client.get("/api/ledger?window=month").json()["rows"]
    }
    assert any(i.startswith("unknown_") for i in ids)


def test_ambiguous_events_are_stored_with_their_reason(client):
    _drive(client, "simultaneous")
    events = client.get("/api/events?limit=50").json()
    ambiguous = [e for e in events if e["ambiguous"]]
    assert ambiguous
    assert all(e["reason"] == "overlapping_edges" for e in ambiguous)


def test_below_floor_events_are_stored(client):
    _drive(client, "below-floor")
    reasons = {e["reason"] for e in client.get("/api/events?limit=50").json()}
    assert "below_floor" in reasons


def test_month_endpoint_works_after_a_real_run(client):
    _drive(client, "demo")
    body = client.get("/api/month").json()
    assert isinstance(body["bill"]["total"], str)
    assert body["carbon"]["local"]["provenance"]["url"]
    assert body["mtd_kwh"] > 0


def test_restart_preserves_stored_state(tmp_path):
    """A restart must serve the same ledger it had before, from the same
    on-disk file rather than from anything held in memory.

    The second app gets a FRESH Store opened on the same path, which is
    what "restart" means and is the only coverage anywhere of Store.open
    against an existing, populated, version-MATCHING database -- the other
    reopen test deliberately mismatches the version. Reusing the first
    still-open Store object, as this test once did, restarted nothing.

    Recency matters here as much as it does in _drive: both reads are
    windowed to "this month", so a run seeded at the simulator's stale
    default would leave `before` and `after` both equal to [] and this
    would pass whether or not a restart actually preserves anything. The
    non-empty check keeps that failure mode from coming back unnoticed.
    """
    from smartwatt_sim.scenarios import run

    from smartwatt_server.store import Store

    path = tmp_path / "restart.db"

    first = Store()
    first.open(path)
    try:
        with TestClient(create_app(store=first, start_ingest=False)) as client:
            for kind, payload in run("single", start_ts=_recent_start_ts()):
                client.app.state.ingestor.handle(
                    TOPIC[kind], json.dumps(payload).encode()
                )
            before = client.get("/api/ledger?window=month").json()["rows"]
    finally:
        first.close()

    second = Store()
    second.open(path)
    try:
        with TestClient(create_app(store=second, start_ingest=False)) as client:
            after = client.get("/api/ledger?window=month").json()["rows"]
    finally:
        second.close()

    assert before
    assert before == after


def test_a_single_corrupted_payload_does_not_stop_the_run(client):
    from smartwatt_sim.scenarios import run

    stream = list(run("demo", start_ts=_recent_start_ts()))
    stream[10][1]["schema"] = "smartwatt.telemetry.v9"
    accepted = 0
    for kind, payload in stream:
        if client.app.state.ingestor.handle(
            TOPIC[kind], json.dumps(payload).encode()
        ):
            accepted += 1
    assert client.app.state.ingestor.rejected == 1
    assert accepted == len(stream) - 1
