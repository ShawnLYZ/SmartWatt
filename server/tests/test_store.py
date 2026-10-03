import logging
import sqlite3
import threading

import pytest

from smartwatt_server.store import SCHEMA_VERSION, Store

from .support import example


def test_creates_every_table(store):
    names = {
        row[0]
        for row in store.connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {
        "telemetry_1hz", "telemetry_1min", "ledger_1min",
        "events", "appliances", "actions", "ingest_stats", "meta",
    } <= names


def test_wal_is_enabled(store):
    mode = store.connection.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_write_telemetry_round_trips(store):
    payload = example("telemetry-single-load")
    store.write_telemetry(payload)
    row = store.latest_telemetry()
    assert row["ts"] == payload["ts"]
    assert row["p"] == payload["electrical"]["p"]
    assert row["residual_w"] == payload["attribution"]["residual_w"]


def test_negative_residual_survives_storage(store):
    """Non-negotiable #2: never clamped, anywhere in the pipeline."""
    payload = example("telemetry-negative-residual")
    assert payload["attribution"]["residual_w"] < 0
    store.write_telemetry(payload)
    assert store.latest_telemetry()["residual_w"] < 0


def test_null_health_stored_as_null(store):
    store.write_telemetry(example("telemetry-simulator-null-health"))
    row = store.latest_telemetry()
    assert row["isr_overruns"] is None


def test_write_event_preserves_label_and_attributed_to(store):
    """US17: the state-consistency filter must leave evidence it fired."""
    payload = example("event-off-reassigned")
    store.write_event(payload)
    row = store.events(limit=1)[0]
    assert row["label"] == "desk_fan"
    assert row["attributed_to"] == "incandescent_lamp"


def test_event_features_round_trip_as_json(store):
    payload = example("event-on-confident")
    store.write_event(payload)
    row = store.events(limit=1)[0]
    assert row["features"] == payload["features"]
    assert row["neighbours"] == payload["neighbours"]


def test_duplicate_timestamp_replaces(store):
    payload = example("telemetry-single-load")
    store.write_telemetry(payload)
    payload = {**payload, "electrical": {**payload["electrical"], "p": 1.0}}
    store.write_telemetry(payload)
    assert store.latest_telemetry()["p"] == 1.0


def test_schema_version_recorded(store):
    row = store.connection.execute(
        "SELECT value FROM meta WHERE key='schema_version'"
    ).fetchone()
    assert int(row[0]) == SCHEMA_VERSION


def test_schema_version_mismatch_recreates(tmp_path, caplog):
    path = tmp_path / "old.db"
    first = Store()
    first.open(path)
    first.write_telemetry(example("telemetry-single-load"))
    first.connection.execute(
        "UPDATE meta SET value = ? WHERE key='schema_version'",
        (str(SCHEMA_VERSION + 1),),
    )
    first.connection.commit()
    first.close()

    second = Store()
    with caplog.at_level(logging.WARNING, logger="smartwatt_server.store"):
        second.open(path)
    try:
        assert second.latest_telemetry() is None
        # The warning is the ONLY signal a user gets that their stored
        # history was just discarded, so it is asserted rather than merely
        # allowed to happen.
        warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert any(
            "schema version" in message and "discarding stored history" in message
            for message in warnings
        ), warnings
    finally:
        second.close()


def test_ledger_write_and_read(store):
    # write_ledger takes a MINUTE INDEX; ledger() takes epoch seconds and
    # converts internally.
    store.write_ledger(1_754_035_200 // 60, "kettle", 14.13)
    store.write_ledger(1_754_035_200 // 60, "__residual__", 0.17)
    rows = store.ledger(1_754_035_100, 1_754_035_300)
    ids = {row["appliance_id"] for row in rows}
    assert ids == {"kettle", "__residual__"}


def test_ledger_accumulates_within_a_minute(store):
    """One row per 1 Hz sample lands in the same minute: energy adds up and
    the stored mean stays the energy's own mean, not the last sample."""
    minute = 1_754_035_200 // 60
    for _ in range(4):
        store.write_ledger(minute, "kettle", 0.5)
    row = store.ledger(1_754_035_100, 1_754_035_300)[0]
    assert row["wh"] == pytest.approx(2.0)
    assert row["w_mean"] == pytest.approx(120.0)
    stored = store.connection.execute(
        "SELECT w_mean FROM ledger_1min WHERE appliance_id='kettle'"
    ).fetchone()
    assert stored["w_mean"] == pytest.approx(120.0)


def test_ledger_accepts_unknown_ids(store):
    store.write_ledger(1_754_035_200 // 60, "unknown_1", 5.08)
    assert store.ledger(0, 9_999_999_999)[0]["appliance_id"] == "unknown_1"


def test_ledger_accepts_negative_residual_energy(store):
    store.write_ledger(1_754_035_200 // 60, "__residual__", -0.05)
    row = store.ledger(0, 9_999_999_999)[0]
    assert row["wh"] < 0
    # Never clamped: a negative residual is a negative mean too.
    assert row["w_mean"] < 0


def test_stats_accumulate(store):
    store.bump_stats(1_754_035_200 // 60, accepted=1)
    store.bump_stats(1_754_035_200 // 60, accepted=1, rejected_schema=1)
    stats = store.stats()
    assert stats["accepted"] == 2
    assert stats["rejected_schema"] == 1
    assert stats["seq_gaps"] == 0


def test_latest_telemetry_on_empty_store_is_none(store):
    assert store.latest_telemetry() is None


def test_close_waits_for_an_in_flight_write(store):
    """close() is the one connection method that must take the lock.

    api.py's lifespan stops the MQTT thread and then closes the store.
    paho only *queues* a DISCONNECT, so the ingest thread can still be
    inside write_telemetry when close() lands -- and closing a
    sqlite3.Connection from one thread while another is executing on it
    segfaults CPython (exit 139), which is what Ctrl-C on the demo laptop
    with telemetry flowing used to trigger. Holding the lock here stands
    in for that in-flight write; close() must block until it is released.

    The unlocked READ methods are deliberately left alone: sqlite3's
    threadsafety is 3 on this build and concurrent reads were hammered
    without error.
    """
    closed = threading.Event()

    with store._lock:
        worker = threading.Thread(
            target=lambda: (store.close(), closed.set())
        )
        worker.start()
        assert not closed.wait(0.3), "close() ran while a writer held the lock"

    assert closed.wait(5.0), "close() never completed after the lock was freed"
    worker.join(timeout=5.0)
    assert store.connection is None


def test_upsert_appliance_writes_and_updates(store):
    """upsert_appliance writes a row with the lock held, and updates on
    conflict so seeding is idempotent."""
    store.upsert_appliance(
        appliance_id="test_device",
        display_name="Test Device",
        protected=True,
        heating=False,
        plug_device="plug_test",
    )
    row = store.connection.execute(
        "SELECT * FROM appliances WHERE id=?", ("test_device",)
    ).fetchone()
    assert row["display_name"] == "Test Device"
    assert bool(row["protected"]) is True
    assert bool(row["heating"]) is False
    assert row["plug_device"] == "plug_test"

    # Upsert with different values
    store.upsert_appliance(
        appliance_id="test_device",
        display_name="Updated Test Device",
        protected=False,
        heating=True,
        plug_device=None,
    )
    row = store.connection.execute(
        "SELECT * FROM appliances WHERE id=?", ("test_device",)
    ).fetchone()
    assert row["display_name"] == "Updated Test Device"
    assert bool(row["protected"]) is False
    assert bool(row["heating"]) is True
    assert row["plug_device"] is None


# ============================================================================
# Task 3 review finding: a Hypothesis-generated test catalogue could put two
# DIFFERENT appliances on the SAME plug, which produced false-positive
# property failures against correct code because the gate's own tracking
# (Gate._awaiting, keyed by plug_device -- see gate.py) cannot distinguish
# them either. The test file's generator was fixed to never produce that
# shape, but nothing in the system actually forbade it -- these four tests
# are what makes that exclusion true of the database, not just the test.
#
# The constraint is a unique INDEX (idx_appliances_plug), not a column-level
# UNIQUE, and NOT a schema version bump: the last of the four below is what
# proves the difference matters, by putting the index over a database that
# already has rows in it.
# ============================================================================

def test_upsert_appliance_rejects_two_appliances_on_the_same_plug(store):
    """One physical plug, one appliance. A second appliance trying to claim
    a plug another appliance already holds hits the unique index and
    raises -- deliberately uncaught, per the reasoning in upsert_appliance's
    docstring: no other writer in this class catches or translates a
    sqlite3 error, and this is not a condition worth a quieter failure
    mode for."""
    store.upsert_appliance(
        appliance_id="device_a", display_name="Device A",
        protected=False, heating=False, plug_device="plug_shared",
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.upsert_appliance(
            appliance_id="device_b", display_name="Device B",
            protected=False, heating=False, plug_device="plug_shared",
        )

    # The failed statement must not have left a partial row behind -- the
    # collision is refused outright, not half-applied.
    assert store.connection.execute(
        "SELECT 1 FROM appliances WHERE id=?", ("device_b",)
    ).fetchone() is None
    # And the first appliance's own row is untouched by the failed attempt.
    row = store.connection.execute(
        "SELECT plug_device FROM appliances WHERE id=?", ("device_a",)
    ).fetchone()
    assert row["plug_device"] == "plug_shared"


def test_upsert_appliance_allows_multiple_plugless_appliances(store):
    """SQLite treats NULL as never equal to another NULL under a unique
    index (confirmed directly against this SQLite build before relying
    on it here -- see the fix report), so any number of appliances with no
    plug at all -- desk_fan today, and whatever else joins it later -- must
    coexist freely. A UNIQUE column that accidentally limited the table to
    one NULL row would be at least as broken as having no constraint at
    all."""
    store.upsert_appliance(
        appliance_id="unplugged_a", display_name="Unplugged A",
        protected=False, heating=False, plug_device=None,
    )
    store.upsert_appliance(
        appliance_id="unplugged_b", display_name="Unplugged B",
        protected=False, heating=False, plug_device=None,
    )
    store.upsert_appliance(
        appliance_id="unplugged_c", display_name="Unplugged C",
        protected=False, heating=False, plug_device=None,
    )
    rows = store.connection.execute(
        "SELECT id FROM appliances WHERE plug_device IS NULL ORDER BY id"
    ).fetchall()
    assert [row["id"] for row in rows] == ["unplugged_a", "unplugged_b", "unplugged_c"]


def test_upsert_appliance_re_upserting_its_own_plug_is_not_a_self_conflict(store):
    """Registry.seed_defaults() upserts the same five devices - the same
    five plugs - every time it runs (idempotent by design, see
    test_seeding_is_idempotent in test_registry.py). The unique index
    must not turn that into a spurious conflict: re-upserting appliance A
    with the exact plug_device row A already holds is a no-op update, not
    two rows competing for one plug."""
    store.upsert_appliance(
        appliance_id="device_a", display_name="Device A",
        protected=False, heating=False, plug_device="plug_a",
    )
    store.upsert_appliance(
        appliance_id="device_a", display_name="Device A renamed",
        protected=True, heating=False, plug_device="plug_a",
    )
    row = store.connection.execute(
        "SELECT display_name, protected, plug_device FROM appliances WHERE id=?",
        ("device_a",),
    ).fetchone()
    assert row["display_name"] == "Device A renamed"
    assert bool(row["protected"]) is True
    assert row["plug_device"] == "plug_a"


def test_an_existing_unconstrained_database_gains_the_constraint_and_keeps_its_rows(
    tmp_path,
):
    """The whole reason plug-uniqueness is an INDEX and not a column-level
    UNIQUE behind a SCHEMA_VERSION bump.

    Builds a database with the PRE-CONSTRAINT `appliances` table -- exactly
    the shape a deployment that ran before this branch has on disk -- fills
    it with the two tables that actually matter (`actions`, the evidence
    trail this sub-project exists to produce, and `ledger_1min`, which S6's
    tariff-cliff work computes over), then opens it through Store and
    checks BOTH halves:

      * every stored row is still there. A column-level UNIQUE could only
        reach this table through a version bump, and _ensure_schema answers
        a bump with _recreate(), which DROPs all eight tables.
      * the constraint is nevertheless now live on that same database, not
        just on freshly-created ones. CREATE TABLE IF NOT EXISTS is a no-op
        here (the table already exists), so a UNIQUE written into the
        column list would be silently absent and this half would fail.

    Neither half is redundant: deleting idx_appliances_plug from _DDL
    leaves the row counts passing and the duplicate accepted; restoring the
    version bump leaves the duplicate rejected and the row counts at zero.
    """
    path = tmp_path / "v1.db"
    seed = sqlite3.connect(str(path))
    seed.executescript(
        """
        CREATE TABLE appliances (
            id TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            protected INTEGER NOT NULL DEFAULT 0,
            heating INTEGER NOT NULL DEFAULT 0,
            plug_device TEXT
        );
        CREATE TABLE ledger_1min (
            ts_min INTEGER NOT NULL, appliance_id TEXT NOT NULL,
            w_mean REAL NOT NULL, wh_delta REAL NOT NULL,
            PRIMARY KEY (ts_min, appliance_id)
        );
        CREATE TABLE actions (
            ts REAL NOT NULL, actor TEXT NOT NULL, device TEXT NOT NULL,
            command TEXT NOT NULL, rule TEXT, outcome TEXT NOT NULL, reason TEXT
        );
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        INSERT INTO meta VALUES ('schema_version', '1');
        """
    )
    seed.executemany(
        "INSERT INTO ledger_1min VALUES (?,?,?,?)",
        [(minute, "incandescent_lamp", 40.0, 0.667) for minute in range(500)],
    )
    seed.execute(
        "INSERT INTO actions VALUES (?,?,?,?,?,?,?)",
        (1000.0, "user", "laptop_charger", "OFF", None, "REFUSED_PROTECTED",
         "laptop_charger is protected and can never be cut"),
    )
    seed.execute("INSERT INTO appliances VALUES ('device_a','A',0,0,'plug_a')")
    seed.commit()
    seed.close()

    upgraded = Store()
    upgraded.open(path)
    try:
        counts = {
            table: upgraded.connection.execute(
                f"SELECT count(*) FROM {table}"  # noqa: S608 - literal table names
            ).fetchone()[0]
            for table in ("ledger_1min", "actions", "appliances")
        }
        assert counts == {"ledger_1min": 500, "actions": 1, "appliances": 1}

        with pytest.raises(sqlite3.IntegrityError):
            upgraded.upsert_appliance(
                appliance_id="device_b", display_name="B",
                protected=False, heating=False, plug_device="plug_a",
            )
        # And plugless appliances are still unconstrained on this same
        # upgraded database, not only on a fresh one.
        upgraded.upsert_appliance(
            appliance_id="unplugged_a", display_name="U1",
            protected=False, heating=False, plug_device=None,
        )
        upgraded.upsert_appliance(
            appliance_id="unplugged_b", display_name="U2",
            protected=False, heating=False, plug_device=None,
        )
        assert upgraded.connection.execute(
            "SELECT count(*) FROM appliances WHERE plug_device IS NULL"
        ).fetchone()[0] == 2
    finally:
        upgraded.close()


def test_write_action_writes_a_row_and_returns_its_rowid(store):
    rowid = store.write_action(1000.0, "rule", "plug_lamp", "OFF", "left_on", "SENT", None)
    row = store.connection.execute(
        "SELECT * FROM actions WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert row["ts"] == 1000.0
    assert row["actor"] == "rule"
    assert row["device"] == "plug_lamp"
    assert row["command"] == "OFF"
    assert row["rule"] == "left_on"
    assert row["outcome"] == "SENT"
    assert row["reason"] is None


def test_set_action_outcome_moves_the_matching_row(store):
    rowid = store.write_action(1000.0, "rule", "plug_lamp", "OFF", "left_on", "SENT", None)
    store.set_action_outcome(rowid, "SENT", "CONFIRMED")
    row = store.connection.execute(
        "SELECT outcome FROM actions WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert row["outcome"] == "CONFIRMED"


def test_set_action_outcome_leaves_a_row_not_in_from_outcome(store):
    """The `from_outcome` match is what stops a stale or duplicate call
    from clobbering a row a different transition already settled -- the
    whole reason the parameter exists, per its docstring."""
    rowid = store.write_action(1000.0, "rule", "plug_lamp", "OFF", "left_on", "TIMEOUT", None)
    store.set_action_outcome(rowid, "SENT", "CONFIRMED")
    row = store.connection.execute(
        "SELECT outcome FROM actions WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert row["outcome"] == "TIMEOUT"


def test_set_action_outcome_targets_the_exact_row_not_just_the_latest(store):
    """Finding 4 (task review): two SENT rows can exist for the same
    device -- a second command supersedes the first before it resolves.
    Targeting by rowid, rather than "the most recent SENT row for this
    device", means the OLDER row can still be settled correctly even
    though a newer SENT row for the same device now exists. The old
    device+MAX(rowid) selector could never express this: it always
    resolved to the newer row regardless of which one the caller meant."""
    row1 = store.write_action(1000.0, "rule", "plug_lamp", "OFF", "left_on", "SENT", None)
    row2 = store.write_action(1001.0, "user", "plug_lamp", "ON", None, "SENT", None)

    store.set_action_outcome(row1, "SENT", "TIMEOUT")

    outcomes = {
        row["rowid"]: row["outcome"]
        for row in store.connection.execute("SELECT rowid, outcome FROM actions")
    }
    assert outcomes[row1] == "TIMEOUT"
    assert outcomes[row2] == "SENT"


def test_set_action_outcome_can_record_a_reason(store):
    """Finding 2 (task review): a transition that needs to explain itself
    -- a publish that never left the process, settled straight to TIMEOUT
    -- writes that explanation into the column that exists for it."""
    rowid = store.write_action(1000.0, "rule", "plug_lamp", "OFF", "left_on", "SENT", None)
    store.set_action_outcome(rowid, "SENT", "TIMEOUT", reason="publisher reported failure")
    row = store.connection.execute(
        "SELECT outcome, reason FROM actions WHERE rowid = ?", (rowid,)
    ).fetchone()
    assert row["outcome"] == "TIMEOUT"
    assert row["reason"] == "publisher reported failure"


def _appliance(appliance_id, plug, protected=False):
    return {
        "appliance_id": appliance_id, "display_name": appliance_id.title(),
        "protected": protected, "heating": False, "plug_device": plug,
    }


def _appliance_rows(store):
    return [
        (row["id"], row["plug_device"], bool(row["protected"]))
        for row in store.connection.execute(
            "SELECT id, plug_device, protected FROM appliances ORDER BY id"
        )
    ]


def test_replace_appliances_leaves_exactly_the_new_rows(store):
    store.upsert_appliance(
        appliance_id="old", display_name="Old", protected=False, heating=False,
        plug_device="plug_a",
    )
    store.replace_appliances([_appliance("fan", "plug_a"), _appliance("tv", None, True)])
    assert _appliance_rows(store) == [("fan", "plug_a", False), ("tv", None, True)]


def test_replace_appliances_is_all_or_nothing(store):
    """Two new rows on one plug fail the unique index on the SECOND insert,
    after the DELETE has already run. The table must come back exactly as it
    was: an empty registry would leave nothing for the gate to refuse
    protected loads with."""
    store.replace_appliances([_appliance("fan", "plug_a"), _appliance("tv", "plug_b", True)])
    before = _appliance_rows(store)

    with pytest.raises(sqlite3.IntegrityError):
        store.replace_appliances([_appliance("x", "plug_c"), _appliance("y", "plug_c")])

    assert _appliance_rows(store) == before
    # And the connection is usable afterwards, not stuck in a transaction.
    store.replace_appliances([_appliance("z", None)])
    assert _appliance_rows(store) == [("z", None, False)]
