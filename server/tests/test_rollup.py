import logging

import pytest

from .support import example


def _telemetry(
    ts: float, p: float, residual: float = 5.0, wh_today: float | None = None
) -> dict:
    payload = example("telemetry-single-load")
    # Only override wh_today when a test cares about it; otherwise every row
    # carries the fixture's constant, so MAX-MIN is 0 and wh_delta is inert.
    energy = (
        payload["energy"]
        if wh_today is None
        else {**payload["energy"], "wh_today": wh_today}
    )
    payload = {
        **payload,
        "ts": ts,
        "seq": int(ts),
        "electrical": {**payload["electrical"], "p": p},
        "attribution": {**payload["attribution"], "residual_w": residual},
        "energy": energy,
    }
    return payload


def test_rollup_makes_one_row_per_minute(store):
    for n in range(180):
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0))
    written = store.rollup(before_ts=1_754_035_200 + 200)
    assert written == 3


def test_rollup_computes_mean_and_max(store):
    for n in range(60):
        store.write_telemetry(
            _telemetry(1_754_035_200 + n, float(n), wh_today=1000.0 + n * 2.0)
        )
    store.rollup(before_ts=1_754_035_400)
    row = store.connection.execute("SELECT * FROM telemetry_1min").fetchone()
    assert row["p_mean"] == pytest.approx(29.5)
    assert row["p_max"] == pytest.approx(59.0)
    assert row["samples"] == 60
    # wh_today climbs 2.0/s across the minute (n=0..59): MAX-MIN == 59 * 2.0.
    assert row["wh_delta"] == pytest.approx(118.0)


def test_rollup_guards_against_an_accumulator_reset(store, caplog):
    """A wh_today reset inside a minute must not write a day's energy.

    ledger.py already treats a wh_session decrease as a device restart and
    refuses to write a negative delta; wh_today is a field whose very name
    implies a daily reset and the rollup applied no equivalent reasoning.
    It matters more here: telemetry_1min is retained INDEFINITELY, and
    purge_hz then deletes the 1 Hz rows a recomputation would need, so the
    corruption is permanent.

    Nothing can catch this by observation -- the simulator's wh_today is
    monotonic for ever, so no soak of any length produces it.
    """
    for n in range(60):
        # Climbs to 1029, then the device restarts 30 s into the minute.
        wh_today = 1000.0 + n if n < 30 else float(n - 30)
        store.write_telemetry(
            _telemetry(1_754_035_200 + n, 100.0, wh_today=wh_today)
        )
    with caplog.at_level(logging.WARNING, logger="smartwatt_server.store"):
        store.rollup(before_ts=1_754_035_400)

    row = store.connection.execute("SELECT * FROM telemetry_1min").fetchone()
    assert row["wh_delta"] == 0.0
    assert row["samples"] == 60  # everything else about the minute survives
    assert any(
        "reset" in record.getMessage() for record in caplog.records
    ), [record.getMessage() for record in caplog.records]


def test_rollup_reset_guard_leaves_a_normal_minute_alone(store, caplog):
    """A reset ACROSS a minute boundary is not a reset within either minute."""
    for n in range(120):
        wh_today = 1000.0 + n if n < 60 else float(n - 60)
        store.write_telemetry(
            _telemetry(1_754_035_200 + n, 100.0, wh_today=wh_today)
        )
    with caplog.at_level(logging.WARNING, logger="smartwatt_server.store"):
        store.rollup(before_ts=1_754_035_320)

    deltas = [
        row["wh_delta"]
        for row in store.connection.execute(
            "SELECT wh_delta FROM telemetry_1min ORDER BY ts_min"
        )
    ]
    assert deltas == [pytest.approx(59.0), pytest.approx(59.0)]
    assert not [r for r in caplog.records if "reset" in r.getMessage()]


def test_rollup_keeps_residual_min_not_just_mean(store):
    """Averaging a residual that swings negative would hide the excursions."""
    for n in range(60):
        residual = -20.0 if n == 30 else 5.0
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0, residual))
    store.rollup(before_ts=1_754_035_400)
    row = store.connection.execute("SELECT * FROM telemetry_1min").fetchone()
    assert row["residual_min"] == pytest.approx(-20.0)
    assert row["residual_mean"] > 0


def test_rollup_is_idempotent(store):
    for n in range(60):
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0))
    store.rollup(before_ts=1_754_035_400)
    before = store.connection.execute(
        "SELECT * FROM telemetry_1min"
    ).fetchall()
    store.rollup(before_ts=1_754_035_400)
    after = store.connection.execute("SELECT * FROM telemetry_1min").fetchall()
    assert [dict(r) for r in before] == [dict(r) for r in after]


def test_rollup_leaves_recent_rows_alone(store):
    for n in range(120):
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0))
    store.rollup(before_ts=1_754_035_260)
    assert store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_1min"
    ).fetchone()[0] == 1


def test_purge_deletes_only_rolled_rows(store):
    for n in range(120):
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0))
    store.rollup(before_ts=1_754_035_260)
    deleted = store.purge_hz(before_ts=1_754_035_260)
    assert deleted == 60
    assert store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_1hz"
    ).fetchone()[0] == 60
    assert store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_1min"
    ).fetchone()[0] == 1


def test_one_minute_rows_are_kept_indefinitely(store):
    for n in range(60):
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0))
    store.rollup(before_ts=1_754_035_400)
    store.purge_hz(before_ts=9_999_999_999)
    assert store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_1min"
    ).fetchone()[0] == 1


def test_mid_minute_cutoff_defers_the_boundary_minute(store):
    """A cutoff that lands inside a minute must not roll or purge that
    minute at all, let alone partially.

    Task 6's retention loop calls rollup/purge_hz with
    ``time.time() - retention_s``, which is minute-aligned only by chance.
    A boundary minute rolled from a partial sample set, then purged of
    exactly those samples, would be silently re-rolled from only the
    survivors on the next cycle -- overwriting the row with a permanently
    incomplete one. The fix is to floor the cutoff to a whole minute so the
    boundary minute is skipped entirely until it is complete.
    """
    for n in range(60):
        store.write_telemetry(_telemetry(1_754_035_200 + n, 100.0))
    mid_minute = 1_754_035_200 + 30  # 30 s into the only minute present

    written = store.rollup(before_ts=mid_minute)
    assert written == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_1min"
    ).fetchone()[0] == 0

    deleted = store.purge_hz(before_ts=mid_minute)
    assert deleted == 0
    assert store.connection.execute(
        "SELECT COUNT(*) FROM telemetry_1hz"
    ).fetchone()[0] == 60

    # Later, once the minute is actually complete, it rolls whole -- not
    # the partial remainder a naive fix (floor rollup but not purge_hz,
    # or vice versa) would produce.
    written = store.rollup(before_ts=1_754_035_260)
    assert written == 1
    row = store.connection.execute("SELECT * FROM telemetry_1min").fetchone()
    assert row["samples"] == 60
