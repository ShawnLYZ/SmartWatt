import pytest

from smartwatt_server.ledger import RESIDUAL_ID, LedgerIntegrator

from .support import example


def _telemetry(ts, active, residual, total_p):
    payload = example("telemetry-single-load")
    return {
        **payload,
        "ts": ts,
        "seq": int(ts),
        "electrical": {**payload["electrical"], "p": total_p},
        "attribution": {
            "active": active,
            "residual_w": residual,
            "floor_w": 8.5,
        },
    }


def test_first_sample_writes_nothing(store):
    integrator = LedgerIntegrator(store)
    integrator.ingest(_telemetry(
        1_754_035_200, [{"id": "kettle", "w": 3600.0, "since": 0}], 0.0, 3600.0
    ))
    assert store.ledger(0, 9_999_999_999) == []


def test_one_hour_at_3600_w_is_3600_wh(store):
    # LedgerIntegrator discards any gap over _MAX_INTEGRATION_GAP_S (10 s), so
    # the hour has to be fed at true 1 Hz: 3600 one-second steps at 3600 W
    # integrate to exactly 3600 Wh (3600 W * 1/3600 h = 1 Wh per step).
    integrator = LedgerIntegrator(store)
    active = [{"id": "kettle", "w": 3600.0, "since": 0}]
    for second in range(3601):
        integrator.ingest(_telemetry(float(second), active, 0.0, 3600.0))
    rows = {r["appliance_id"]: r["wh"] for r in store.ledger(0, 9_999_999_999)}
    assert rows["kettle"] == pytest.approx(3600.0)


def test_residual_gets_its_own_row(store):
    """US37: the ledger must add up to actual consumption."""
    integrator = LedgerIntegrator(store)
    active = [{"id": "kettle", "w": 1000.0, "since": 0}]
    for second in range(3601):
        integrator.ingest(_telemetry(float(second), active, 200.0, 1200.0))
    rows = {r["appliance_id"]: r["wh"] for r in store.ledger(0, 9_999_999_999)}
    assert rows[RESIDUAL_ID] == pytest.approx(200.0)


def test_negative_residual_energy_is_recorded(store):
    integrator = LedgerIntegrator(store)
    active = [{"id": "desk_fan", "w": 50.0, "since": 0}]
    for second in range(3601):
        integrator.ingest(_telemetry(float(second), active, -5.0, 45.0))
    rows = {r["appliance_id"]: r["wh"] for r in store.ledger(0, 9_999_999_999)}
    assert rows[RESIDUAL_ID] < 0


def test_unknown_load_energy_is_counted(store):
    """US14: refusing to name something never means losing track of it."""
    integrator = LedgerIntegrator(store)
    active = [{"id": "unknown_1", "w": 305.0, "since": 0}]
    for second in range(3601):
        integrator.ingest(_telemetry(float(second), active, 0.0, 305.0))
    rows = {r["appliance_id"]: r["wh"] for r in store.ledger(0, 9_999_999_999)}
    assert rows["unknown_1"] == pytest.approx(305.0)


def test_ledger_sums_to_total_consumption(store):
    integrator = LedgerIntegrator(store)
    active = [
        {"id": "kettle", "w": 1800.0, "since": 0},
        {"id": "desk_fan", "w": 45.0, "since": 0},
    ]
    for second in range(3601):
        integrator.ingest(_telemetry(float(second), active, 55.0, 1900.0))
    total = sum(r["wh"] for r in store.ledger(0, 9_999_999_999))
    assert total == pytest.approx(1900.0)


def test_large_time_gap_is_not_integrated(store):
    """A gap means the device was away, not that it drew power the whole time."""
    integrator = LedgerIntegrator(store)
    integrator.ingest(_telemetry(
        0.0, [{"id": "kettle", "w": 1800.0, "since": 0}], 0.0, 1800.0
    ))
    integrator.ingest(_telemetry(
        86_400.0, [{"id": "kettle", "w": 1800.0, "since": 0}], 0.0, 1800.0
    ))
    rows = store.ledger(0, 9_999_999_999)
    assert rows == [] or all(r["wh"] < 1800.0 for r in rows)


def test_partial_minute_w_mean_is_a_mean_not_the_last_sample(store):
    """w_mean is minute-mean watts, derived from the minute's energy.

    A kettle drawing 2000 W for the first ten seconds of a minute and
    nothing after is the discriminating case: the ledger correctly charges
    real energy for that minute, so the Live chart must not plot the last
    sample of the minute (0 W) as though it were the minute's mean. The
    figure is derived from wh_delta, which is already right.

    The first sample only starts the clock, so nine one-second steps at
    2000 W integrate to 5.0 Wh -- 300 W averaged over the whole minute.
    """
    integrator = LedgerIntegrator(store)
    minute_start = 1_754_035_200.0  # already minute-aligned
    for second in range(60):
        watts = 2000.0 if second < 10 else 0.0
        integrator.ingest(_telemetry(
            minute_start + second,
            [{"id": "kettle", "w": watts, "since": 0}],
            0.0,
            watts,
        ))

    minutes = [
        row
        for row in store.ledger_minutes(minute_start, minute_start + 60)
        if row["appliance_id"] == "kettle"
    ]
    assert len(minutes) == 1
    assert minutes[0]["wh_delta"] == pytest.approx(5.0)
    assert minutes[0]["w_mean"] == pytest.approx(300.0)

    # ledger() aggregates the same figure, and whatif turns it into money.
    rolled = {
        row["appliance_id"]: row for row in store.ledger(0, 9_999_999_999)
    }
    assert rolled["kettle"]["w_mean"] == pytest.approx(300.0)


def test_window_mean_watts_reconciles_with_the_window_energy(store):
    """US37 again: hours * mean watts must come back to the stored kWh.

    ledger() reports minutes and mean watts side by side; a table whose own
    two columns do not multiply out to its energy does not add up.
    """
    integrator = LedgerIntegrator(store)
    for second in range(600):
        watts = 1200.0 if (second // 60) % 2 == 0 else 100.0
        integrator.ingest(_telemetry(
            float(second), [{"id": "kettle", "w": watts, "since": 0}], 0.0, watts
        ))

    row = next(
        r for r in store.ledger(0, 9_999_999_999) if r["appliance_id"] == "kettle"
    )
    hours = row["minutes"] / 60.0
    assert row["w_mean"] * hours == pytest.approx(row["wh"])


def test_accumulator_reset_is_detected(store):
    """wh_session going down means the device restarted."""
    integrator = LedgerIntegrator(store)
    a = _telemetry(0.0, [], 0.0, 0.0)
    a["energy"] = {"wh_session": 5000.0, "wh_today": 5000.0}
    b = _telemetry(1.0, [], 0.0, 0.0)
    b["energy"] = {"wh_session": 0.5, "wh_today": 0.5}
    integrator.ingest(a)
    integrator.ingest(b)
    assert integrator.resets == 1
