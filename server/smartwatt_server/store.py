"""SQLite storage.

One schema, no migration framework: a SCHEMA_VERSION mismatch recreates
every table and discards everything stored. That is a blunt instrument
rather than a licence -- what it discards includes `actions`, the safety
gate's evidence trail, and `ledger_1min`, which the tariff work computes
over. So a schema change that can reach an existing database WITHOUT a
version bump is made that way; the unique index on `appliances`
(idx_appliances_plug) is the worked example, and SCHEMA_VERSION's own
comment says why the alternative was rejected.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Deliberately still 1. Plug-uniqueness (see idx_appliances_plug in _DDL
# below) was briefly carried by a bump to 2, because CREATE TABLE IF NOT
# EXISTS never alters an existing table and a version bump was the only
# thing that would make an existing database pick the constraint up. That
# is true of a column-level UNIQUE and false of an index: CREATE UNIQUE
# INDEX IF NOT EXISTS builds over whatever rows are already there, so no
# bump is needed -- and the bump was never free. _ensure_schema answers a
# mismatch with _recreate(), which DROPs all eight tables, discarding the
# `actions` log this sub-project exists to produce and the `ledger_1min`
# history S6's tariff work computes over, behind one log.warning.
#
# Stated rather than glossed: a database stamped 2 by this branch's own
# earlier code IS recreated once, the first time it is opened after this.
# That is the cost of undoing the bump, it falls only on databases created
# by unmerged code during development, and it is paid once -- where
# keeping the bump would have charged it to every real deployment.
SCHEMA_VERSION = 1

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS telemetry_1hz (
    ts REAL PRIMARY KEY,
    source TEXT NOT NULL,
    seq INTEGER NOT NULL,
    fingerprint_id TEXT,
    vrms REAL, irms REAL, p REAL, q1 REAL, dist REAL, s REAL,
    pf_true REAL, pf_disp REAL, freq REAL, range TEXT,
    residual_w REAL NOT NULL,
    floor_w REAL NOT NULL,
    wh_session REAL, wh_today REAL,
    isr_overruns INTEGER, worst_isr_us INTEGER, cycles_dropped INTEGER
);

CREATE TABLE IF NOT EXISTS telemetry_1min (
    ts_min INTEGER PRIMARY KEY,
    p_mean REAL, p_max REAL, vrms_mean REAL, freq_mean REAL,
    residual_mean REAL,
    -- stored alongside the mean deliberately: averaging a residual that
    -- swings negative would hide the excursions.
    residual_min REAL,
    wh_delta REAL, samples INTEGER
);

CREATE TABLE IF NOT EXISTS ledger_1min (
    ts_min INTEGER NOT NULL,
    appliance_id TEXT NOT NULL,
    -- mean watts across the minute, which is wh_delta * 60 and nothing
    -- else. Stored because the schema names it, never written
    -- independently of the energy, and derived again on read so an
    -- existing database heals rather than needing its history discarded.
    w_mean REAL NOT NULL,
    wh_delta REAL NOT NULL,
    PRIMARY KEY (ts_min, appliance_id)
);

CREATE TABLE IF NOT EXISTS events (
    ts REAL PRIMARY KEY,
    source TEXT NOT NULL,
    seq INTEGER NOT NULL,
    edge TEXT NOT NULL,
    label TEXT,
    confidence REAL NOT NULL,
    rejected INTEGER NOT NULL,
    ambiguous INTEGER NOT NULL,
    reason TEXT,
    attributed_to TEXT,
    features TEXT NOT NULL,
    neighbours TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS appliances (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    protected INTEGER NOT NULL DEFAULT 0,
    heating INTEGER NOT NULL DEFAULT 0,
    -- Uniqueness of plug_device is enforced by idx_appliances_plug at the
    -- bottom of this script, NOT by a UNIQUE here. Identical constraint,
    -- but a column-level UNIQUE only exists in a table CREATE TABLE
    -- actually runs, and IF NOT EXISTS skips that for a database that
    -- already has the table -- so the constraint would need a
    -- SCHEMA_VERSION bump to reach an existing database, and a bump costs
    -- every stored row (see SCHEMA_VERSION above).
    plug_device TEXT
);

CREATE TABLE IF NOT EXISTS actions (
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    device TEXT NOT NULL,
    command TEXT NOT NULL,
    rule TEXT,
    outcome TEXT NOT NULL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS ingest_stats (
    ts_min INTEGER PRIMARY KEY,
    accepted INTEGER NOT NULL DEFAULT 0,
    rejected_schema INTEGER NOT NULL DEFAULT 0,
    seq_gaps INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_ledger_ts ON ledger_1min(ts_min);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

-- One physical plug controls one appliance. Without this, two appliance
-- rows could share a plug_device, and the gate's own _awaiting tracking
-- (keyed by plug_device, not appliance id -- see gate.py) would have no
-- way to tell which appliance a confirmation or timeout belonged to.
--
-- An INDEX rather than a column-level UNIQUE so it reaches databases that
-- already exist: CREATE UNIQUE INDEX IF NOT EXISTS builds over the rows
-- already in the table, where CREATE TABLE IF NOT EXISTS is a no-op
-- against an existing table and would leave it unconstrained until a
-- SCHEMA_VERSION bump discarded every row in the database. Verified
-- directly against a v1-shaped database on this SQLite build (3.49.1),
-- not assumed: 500 ledger rows, the actions log and the appliance table
-- all survived, and the duplicate that follows is refused with exactly
-- the same "UNIQUE constraint failed: appliances.plug_device" a column
-- constraint raises.
--
-- SQLite treats NULL as never equal to another NULL under a unique index,
-- so any number of plugless appliances (desk_fan today) still coexist --
-- also confirmed directly, and covered by
-- test_upsert_appliance_allows_multiple_plugless_appliances.
--
-- One behaviour a column-level UNIQUE would not have had: if an existing
-- database ALREADY holds two appliances on one plug, building this index
-- raises IntegrityError out of open() instead of starting up. That is the
-- honest failure -- the alternative is a running system whose gate cannot
-- tell those two appliances apart -- and it is not reachable from
-- seed_defaults(), whose five plugs are distinct.
CREATE UNIQUE INDEX IF NOT EXISTS idx_appliances_plug
    ON appliances(plug_device);
"""


def _floor_to_minute(ts: float) -> int:
    """Start-of-minute boundary at or before ``ts``, as an epoch-second int.

    ``rollup`` and ``purge_hz`` both floor their ``before_ts`` through this
    before using it. The retention loop's cutoff (``time.time() -
    retention_s``) is minute-aligned only by chance, and a raw mid-minute
    cutoff would roll a minute from a partial sample set, then purge exactly
    those samples -- the next cycle's re-roll would then silently overwrite
    that row from only the survivors, discarding the rest for good.
    """
    return int(ts // 60) * 60


class Store:
    """Single-connection SQLite store.

    The WRITERS -- and ``close`` -- take the lock; the readers deliberately
    do not. ``sqlite3.threadsafety`` is 3 on this build, so the module
    serialises concurrent use of one connection itself, and a hammer test
    of a writer against readers produced no errors. ``close`` is the
    exception that matters: closing the connection while another thread is
    executing on it segfaults CPython rather than raising, so it waits for
    the in-flight write the same way a writer would.

    1 Hz telemetry plus a handful of ledger rows is trivial volume; a lock
    is simpler than an async driver and easier to reason about.
    """

    def __init__(self) -> None:
        self.connection: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # -- lifecycle ---------------------------------------------------------

    def open(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self._ensure_schema(path)

    def close(self) -> None:
        # Under the lock: paho's thread can still be inside write_telemetry
        # when the lifespan shuts down (disconnect() only queues a
        # DISCONNECT), and closing the connection out from under a running
        # statement segfaults the interpreter instead of raising.
        with self._lock:
            if self.connection is not None:
                self.connection.close()
                self.connection = None

    def _ensure_schema(self, path: Path) -> None:
        with self._lock:
            self.connection.executescript(_DDL)
            row = self.connection.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                    (str(SCHEMA_VERSION),),
                )
            elif int(row["value"]) != SCHEMA_VERSION:
                log.warning(
                    "schema version %s != %s; recreating all tables and "
                    "discarding stored history",
                    row["value"], SCHEMA_VERSION,
                )
                self._recreate()
            self.connection.commit()

    def _recreate(self) -> None:
        for table in (
            "telemetry_1hz", "telemetry_1min", "ledger_1min", "events",
            "appliances", "actions", "ingest_stats", "meta",
        ):
            self.connection.execute(f"DROP TABLE IF EXISTS {table}")
        self.connection.executescript(_DDL)
        self.connection.execute(
            "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    # -- writes ------------------------------------------------------------

    def write_telemetry(self, payload: dict) -> None:
        e, a, n = payload["electrical"], payload["attribution"], payload["energy"]
        h = payload["health"] or {}
        with self._lock:
            self.connection.execute(
                """INSERT OR REPLACE INTO telemetry_1hz VALUES
                   (?,?,?,?, ?,?,?,?,?,?, ?,?,?,?, ?,?, ?,?, ?,?,?)""",
                (
                    payload["ts"], payload["source"], payload["seq"],
                    payload.get("fingerprint_id"),
                    e["vrms"], e["irms"], e["p"], e["q1"], e["dist"], e["s"],
                    e["pf_true"], e["pf_disp"], e["freq"], e["range"],
                    a["residual_w"], a["floor_w"],
                    n["wh_session"], n["wh_today"],
                    h.get("isr_overruns"), h.get("worst_isr_us"),
                    h.get("cycles_dropped"),
                ),
            )
            self.connection.commit()

    def write_event(self, payload: dict) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    payload["ts"], payload["source"], payload["seq"],
                    payload["edge"], payload["label"], payload["confidence"],
                    int(payload["rejected"]), int(payload["ambiguous"]),
                    payload["reason"], payload["attributed_to"],
                    json.dumps(payload["features"]),
                    json.dumps(payload["neighbours"]),
                ),
            )
            self.connection.commit()

    def write_ledger(self, ts_min: int, appliance_id: str, wh_delta: float) -> None:
        """Add ``wh_delta`` Wh to one appliance's row for one minute.

        There is no ``w_mean`` argument: mean watts over a minute is
        ``wh_delta * 60`` and nothing else, so it is derived rather than
        passed in. Accepting it separately is what let the two disagree --
        the integrator writes one row per 1 Hz sample, and the old
        ``w_mean = excluded.w_mean`` left whichever sample landed last in
        the minute standing in for its mean.
        """
        with self._lock:
            self.connection.execute(
                """INSERT INTO ledger_1min(ts_min, appliance_id, w_mean, wh_delta)
                   VALUES (:ts_min, :appliance_id, :wh_delta * 60.0, :wh_delta)
                   ON CONFLICT(ts_min, appliance_id) DO UPDATE SET
                     wh_delta = ledger_1min.wh_delta + excluded.wh_delta,
                     w_mean   = (ledger_1min.wh_delta + excluded.wh_delta) * 60.0""",
                {
                    "ts_min": ts_min,
                    "appliance_id": appliance_id,
                    "wh_delta": wh_delta,
                },
            )
            self.connection.commit()

    def upsert_appliance(
        self,
        appliance_id: str,
        display_name: str,
        protected: bool,
        heating: bool,
        plug_device: str | None,
    ) -> None:
        """Upsert one appliance row, idempotently.

        Takes the write lock for the same two reasons every other writer
        here does, and for no others. A single ``INSERT ... ON CONFLICT``
        is atomic whatever this lock does, and ``write_ledger`` never
        touches ``appliances`` at all, so there is no torn appliance row
        for anything to read. What the lock actually buys: it serialises
        this write against the OTHER writers sharing this one connection
        (the ingest thread's ``write_telemetry``/``write_ledger``, the
        gate's ``write_action``), and it is what lets ``close()`` wait for
        an in-flight statement instead of closing the connection under it,
        which segfaults CPython rather than raising -- see the class
        docstring.

        The ``ON CONFLICT(id)`` clause below resolves conflicts on the
        primary key only. A caller that tries to give a NEW appliance a
        ``plug_device`` some OTHER appliance already has hits the
        ``idx_appliances_plug`` unique index instead, and that is
        deliberately left to raise ``sqlite3.IntegrityError`` uncaught -
        no other writer in this class catches or translates a sqlite3
        error either, and two appliances legitimately claiming one
        physical plug is not a state worth a quieter failure mode for.
        Re-upserting an appliance with the plug_device it ALREADY holds
        (idempotent re-seeding, e.g. calling ``Registry.seed_defaults()``
        twice) is NOT a conflict - confirmed directly, not assumed -
        since the constraint only ever compares against OTHER rows.
        """
        with self._lock:
            self.connection.execute(
                """INSERT INTO appliances
                       (id, display_name, protected, heating, plug_device)
                   VALUES (:id, :display_name, :protected, :heating, :plug_device)
                   ON CONFLICT(id) DO UPDATE SET
                       display_name = excluded.display_name,
                       protected    = excluded.protected,
                       heating      = excluded.heating,
                       plug_device  = excluded.plug_device""",
                {
                    "id": appliance_id,
                    "display_name": display_name,
                    "protected": int(protected),
                    "heating": int(heating),
                    "plug_device": plug_device,
                },
            )
            self.connection.commit()

    def replace_appliances(self, appliances: list[dict]) -> None:
        """Make the appliances table hold exactly ``appliances``, atomically.

        Each dict carries ``upsert_appliance``'s keyword arguments. This is
        how a household's own list (appliances.toml, see appliances.py)
        reaches the database at startup, and upserting alone cannot do it:

          * an appliance deleted from the file would keep its row, stay on
            the Control screen, and stay switchable under whatever
            protected/heating flags it last had;
          * moving a plug from a deleted appliance to a new one, or swapping
            two appliances' plugs, would hit ``idx_appliances_plug`` part-way
            through and stop the server from starting.

        So the old rows go and the new ones arrive in ONE transaction, under
        the write lock for the reasons ``upsert_appliance`` gives. If any
        insert fails -- two rows on one plug, say -- the whole replacement is
        rolled back and the table keeps every row it had. Nothing else in the
        schema references an appliance row, so the actions log and the ledger
        keep their history of appliances that are no longer listed.
        """
        with self._lock:
            try:
                self.connection.execute("DELETE FROM appliances")
                self.connection.executemany(
                    """INSERT INTO appliances
                           (id, display_name, protected, heating, plug_device)
                       VALUES (:id, :display_name, :protected, :heating,
                               :plug_device)""",
                    [
                        {
                            "id": row["appliance_id"],
                            "display_name": row["display_name"],
                            "protected": int(row["protected"]),
                            "heating": int(row["heating"]),
                            "plug_device": row["plug_device"],
                        }
                        for row in appliances
                    ],
                )
            except BaseException:
                self.connection.rollback()
                raise
            self.connection.commit()

    def write_action(
        self,
        ts: float,
        actor: str,
        device: str,
        command: str,
        rule: str | None,
        outcome: str,
        reason: str | None,
    ) -> int:
        """Append one row to the gate's action ledger. Returns its rowid.

        ``device`` is an APPLIANCE ID. The schema's column name predates
        the registry and is kept because the spec names it, but every
        caller listed below puts an appliance id in it, so one row lines
        up against another and against /api/appliances. It is never a
        plug name; see Gate._log for why the two namespaces were
        collapsed onto this one.

        THREE callers, and no others. `Gate.send()` writes a row for every
        command it evaluates, sent or refused. `RulesEngine.cancel()`
        writes the CANCELLED row for a pending cut a user stops during
        the grace period, and `RulesEngine._abandon_stale_evidence()`
        writes the CANCELLED row for one the engine withdraws because the
        telemetry under it went stale -- neither of which reaches the gate
        at all, because in neither case is anything sent.

        Called from more than one thread by design: a manual command
        arrives on a FastAPI request thread, an automatic one from the
        rules loop, and both share this one connection with the ingest
        thread. The lock serialises those writers the same way every
        other writer here does, and it is what lets `close()` wait for
        one of these in flight instead of closing the connection under
        it, which segfaults CPython rather than raising (see the class
        docstring).

        The rowid is handed back so a caller can settle THIS row later --
        a confirmation, a timeout, a publish that never left the process
        -- via `set_action_outcome`, rather than asking for "the most
        recent row for this device", which resolves to the wrong row once
        a second command to the same device is sent before the first one
        settles.
        """
        with self._lock:
            cursor = self.connection.execute(
                "INSERT INTO actions (ts, actor, device, command, rule, outcome, reason)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, actor, device, command, rule, outcome, reason),
            )
            self.connection.commit()
            return cursor.lastrowid

    def set_action_outcome(
        self,
        rowid: int,
        from_outcome: str,
        to_outcome: str,
        reason: str | None = None,
    ) -> None:
        """Advance action row ``rowid`` from ``from_outcome`` to ``to_outcome``.

        `Gate.confirm()`, `Gate.sweep_timeouts()` and a failed publish
        inside `Gate.send()` all call this, from more than one thread (a
        `stat/` reply arrives on whichever thread the MQTT client calls
        back on; a sweep runs wherever the rules loop ticks). The lock
        serialises that against each other and against `write_action()` on
        the same connection, for the reasons the class docstring gives.

        Targets the exact row the caller already knows the id of -- the
        one `write_action()` handed back when it inserted that row --
        rather than "the most recent row for this device". That distinction
        matters because `_awaiting` is keyed by plug, not by row: a second
        command to the same plug can be sent before the first settles, and
        at that point there are two SENT rows for one device, only one of
        which any given caller is actually tracking. Resolving by rowid
        means the OLDER row can still be settled correctly instead of a
        newer, unrelated row being resolved in its place.

        The ``from_outcome`` match is still not a threading precaution --
        it is what stops a late or duplicate call from moving a row a
        different call already settled.

        ``reason`` is written alongside the outcome, unconditionally
        (even to ``None``). That is safe today because the only rows this
        method is ever asked to move are SENT ones, and a SENT row is
        always logged with ``reason=None`` in the first place -- so there
        is nothing to preserve. It exists so a transition that needs to
        explain itself, like a publish failure moving straight to
        TIMEOUT, can leave the "why" in the column the schema already has
        for it.
        """
        with self._lock:
            self.connection.execute(
                """UPDATE actions SET outcome = ?, reason = ?
                   WHERE rowid = ? AND outcome = ?""",
                (to_outcome, reason, rowid, from_outcome),
            )
            self.connection.commit()

    def bump_stats(self, ts_min: int, **counters: int) -> None:
        with self._lock:
            self.connection.execute(
                "INSERT OR IGNORE INTO ingest_stats(ts_min) VALUES (?)", (ts_min,)
            )
            for name, amount in counters.items():
                self.connection.execute(
                    f"UPDATE ingest_stats SET {name} = {name} + ? WHERE ts_min = ?",
                    (amount, ts_min),
                )
            self.connection.commit()

    # -- reads -------------------------------------------------------------

    def latest_telemetry(self) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM telemetry_1hz ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None

    def telemetry_at_or_before(self, ts: float, window_s: float) -> dict | None:
        """The newest 1 Hz row with ``ts - window_s <= row.ts <= ts``, or
        None when nothing was stored in that window."""
        row = self.connection.execute(
            "SELECT * FROM telemetry_1hz WHERE ts <= ? AND ts >= ? "
            "ORDER BY ts DESC LIMIT 1",
            (ts, ts - window_s),
        ).fetchone()
        return dict(row) if row else None

    def telemetry_since(self, ts: float) -> list[dict]:
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM telemetry_1hz WHERE ts >= ? ORDER BY ts", (ts,)
            )
        ]

    def events(self, limit: int = 50) -> list[dict]:
        out = []
        for row in self.connection.execute(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        ):
            record = dict(row)
            record["features"] = json.loads(record["features"])
            record["neighbours"] = json.loads(record["neighbours"])
            record["rejected"] = bool(record["rejected"])
            record["ambiguous"] = bool(record["ambiguous"])
            out.append(record)
        return out

    def actions(self, limit: int = 100) -> list[dict]:
        """The gate's action ledger, most recent first.

        Ordered by ts DESC, rowid DESC. ts is real wall-clock
        time.time(), and a burst of several actions -- several refusals
        from one rapid sequence of /api/control calls, or several rule
        cuts from one tick() -- can land inside the same clock tick and
        tie on ts alone (observed directly: back-to-back writes on this
        platform can share one ts value). rowid DESC breaks that tie in
        insertion order, which is what keeps "most recent first"
        well-defined for a burst instead of leaving it to whatever a
        query planner happens to do with two equal sort keys.
        """
        return [
            dict(row)
            for row in self.connection.execute(
                "SELECT * FROM actions ORDER BY ts DESC, rowid DESC LIMIT ?",
                (limit,),
            )
        ]

    def ledger(self, start_ts: float, end_ts: float) -> list[dict]:
        # w_mean is derived from the energy here as well as at write time,
        # so a database written before that derivation existed reads back
        # correctly instead of needing a schema version bump to discard it.
        # Deriving also keeps the row self-consistent: mean watts times the
        # hours in `minutes` comes back to `wh`, which is what makes the
        # ledger table add up (US37).
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT appliance_id,
                          SUM(wh_delta)      AS wh,
                          AVG(wh_delta) * 60 AS w_mean,
                          COUNT(*)           AS minutes
                   FROM ledger_1min
                   WHERE ts_min >= ? AND ts_min < ?
                   GROUP BY appliance_id""",
                (int(start_ts // 60), int(end_ts // 60)),
            )
        ]

    def ledger_minutes(self, start_ts: float, end_ts: float) -> list[dict]:
        """Un-aggregated ledger_1min rows in the window, ordered by minute.

        Where ``ledger()`` collapses to one row per appliance for a bill
        or ledger table, a stacked chart needs every (minute, appliance)
        pair kept apart so it can plot a real minute axis instead of one
        point per appliance for the whole window.
        """
        return [
            dict(row)
            for row in self.connection.execute(
                """SELECT ts_min, appliance_id,
                          wh_delta * 60 AS w_mean,
                          wh_delta
                   FROM ledger_1min
                   WHERE ts_min >= ? AND ts_min < ?
                   ORDER BY ts_min""",
                (int(start_ts // 60), int(end_ts // 60)),
            )
        ]

    def appliances(self) -> list[dict]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM appliances")]

    def stats(self) -> dict:
        row = self.connection.execute(
            """SELECT COALESCE(SUM(accepted),0)        AS accepted,
                      COALESCE(SUM(rejected_schema),0) AS rejected_schema,
                      COALESCE(SUM(seq_gaps),0)        AS seq_gaps
               FROM ingest_stats"""
        ).fetchone()
        return dict(row)

    # -- rollup and retention ----------------------------------------------

    def _accumulator_reset_minutes(self, boundary: float) -> set[int]:
        """Minutes in which ``wh_today`` went backwards between samples.

        ``wh_today`` is a daily accumulator, so it resets by design. Inside
        a minute that makes ``MAX - MIN`` read the whole day's energy as
        that minute's delta -- into a table kept indefinitely, after which
        ``purge_hz`` deletes the 1 Hz rows a recomputation would need.
        ``ledger.py`` reasons the same way about a ``wh_session`` decrease.

        A decrease ACROSS a minute boundary is not reported: each of those
        two minutes has a perfectly good delta of its own.
        """
        return {
            row["ts_min"]
            for row in self.connection.execute(
                """SELECT ts_min FROM (
                       SELECT CAST(ts / 60 AS INTEGER) AS ts_min,
                              wh_today,
                              LAG(wh_today) OVER (
                                  PARTITION BY CAST(ts / 60 AS INTEGER)
                                  ORDER BY ts
                              ) AS previous
                       FROM telemetry_1hz
                       WHERE ts < ?
                   )
                   WHERE previous IS NOT NULL AND wh_today < previous""",
                (boundary,),
            )
        }

    def rollup(self, before_ts: float) -> int:
        """Fold complete 1 Hz minutes older than ``before_ts`` into one-minute
        buckets.

        ``before_ts`` is floored to a whole minute first: a boundary minute
        that has only partially arrived is skipped this cycle rather than
        rolled from a partial sample set (see ``_floor_to_minute``).
        Idempotent: re-running over an already-rolled window produces no
        change, so a crash mid-rollup is recovered by re-running it.
        """
        with self._lock:
            boundary = _floor_to_minute(before_ts)
            cursor = self.connection.execute(
                """SELECT CAST(ts / 60 AS INTEGER) AS ts_min,
                          AVG(p) AS p_mean, MAX(p) AS p_max,
                          AVG(vrms) AS vrms_mean, AVG(freq) AS freq_mean,
                          AVG(residual_w) AS residual_mean,
                          MIN(residual_w) AS residual_min,
                          COUNT(*) AS samples,
                          (MAX(wh_today) - MIN(wh_today)) AS wh_delta
                   FROM telemetry_1hz
                   WHERE ts < ?
                   GROUP BY ts_min""",
                (boundary,),
            )
            rows = cursor.fetchall()
            resets = self._accumulator_reset_minutes(boundary)
            for row in rows:
                wh_delta = row["wh_delta"]
                if row["ts_min"] in resets:
                    # MAX-MIN across a reset reads a whole day's energy as
                    # one minute's delta. Mirror ledger.py: record the fact,
                    # write no bogus delta.
                    log.warning(
                        "wh_today accumulator reset inside minute %s; "
                        "recording 0 Wh rather than %.1f Wh",
                        row["ts_min"], wh_delta or 0.0,
                    )
                    wh_delta = 0.0
                self.connection.execute(
                    """INSERT OR REPLACE INTO telemetry_1min
                       (ts_min, p_mean, p_max, vrms_mean, freq_mean,
                        residual_mean, residual_min, wh_delta, samples)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (
                        row["ts_min"], row["p_mean"], row["p_max"],
                        row["vrms_mean"], row["freq_mean"],
                        row["residual_mean"], row["residual_min"],
                        wh_delta, row["samples"],
                    ),
                )
            self.connection.commit()
            return len(rows)

    def purge_hz(self, before_ts: float) -> int:
        """Delete 1 Hz rows already rolled. Returns rows deleted.

        Floors ``before_ts`` the same way ``rollup`` does, so a row already
        purged from ``telemetry_1hz`` is always one that ``rollup`` actually
        rolled and never one from a minute rollup deferred as incomplete.
        """
        with self._lock:
            boundary = _floor_to_minute(before_ts)
            cursor = self.connection.execute(
                "DELETE FROM telemetry_1hz WHERE ts < ?", (boundary,)
            )
            self.connection.commit()
            return cursor.rowcount
