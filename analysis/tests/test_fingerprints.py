import numpy as np
import pytest

from smartwatt_analysis.fingerprints import (
    ALL_COLUMNS,
    CONDITION_COLUMNS,
    FEATURE_COLUMNS,
    Fingerprint,
    MalformedFingerprints,
    append_fingerprint,
    read_fingerprints,
    to_matrix,
    write_fingerprints,
)


def _row(label="kettle", edge="on", n=1, **overrides) -> Fingerprint:
    features = {name: float(i + n) for i, name in enumerate(FEATURE_COLUMNS)}
    base = dict(
        training_id=f"{label}-{edge}-{n:03d}",
        label=label,
        edge=edge,
        features=features,
        ts=1754035200.0 + n,
        session_id="session-a",
        background_w=0.0,
        concurrent_ids="",
        vrms_mean=240.1,
        freq_mean=49.98,
        notes="quiet circuit",
    )
    base.update(overrides)
    return Fingerprint(**base)


def test_fourteen_feature_columns():
    assert len(FEATURE_COLUMNS) == 14


def test_feature_columns_match_the_contract():
    assert FEATURE_COLUMNS == (
        "delta_p", "delta_q1", "delta_dist", "delta_s",
        "pf_disp", "pf_true", "delta_irms", "delta_crest",
        "h3_h1", "h5_h1", "h7_h1",
        "inrush_ratio", "settle_cycles", "log_delta_p",
    )


def test_seven_condition_columns():
    """US59: without these, a CV-vs-live gap is a mystery."""
    assert len(CONDITION_COLUMNS) == 7
    assert set(CONDITION_COLUMNS) == {
        "ts", "session_id", "background_w", "concurrent_ids",
        "vrms_mean", "freq_mean", "notes",
    }


def test_twenty_four_columns_total():
    assert len(ALL_COLUMNS) == 24


def test_round_trip(tmp_path):
    path = tmp_path / "fingerprints.csv"
    rows = [_row(n=n) for n in range(1, 4)]
    write_fingerprints(path, rows)
    back = read_fingerprints(path)
    assert len(back) == 3
    for original, loaded in zip(rows, back):
        assert loaded.training_id == original.training_id
        for name in FEATURE_COLUMNS:
            assert loaded.features[name] == pytest.approx(original.features[name])


def test_file_is_plain_and_readable(tmp_path):
    """US58: inspectable, correctable and backupable without tooling."""
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [_row()])
    text = path.read_text()
    assert text.splitlines()[0] == ",".join(ALL_COLUMNS)
    assert "kettle" in text


def test_a_hand_added_row_loads(tmp_path):
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [_row()])
    values = ["hand-001", "desk_fan", "on"]
    values += [str(float(i)) for i in range(14)]
    values += ["1754035999.0", "manual", "0.0", "", "240.0", "50.0", "typed by hand"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(",".join(values) + "\n")
    rows = read_fingerprints(path)
    assert rows[-1].training_id == "hand-001"
    assert rows[-1].notes == "typed by hand"


def test_append_creates_the_header_once(tmp_path):
    path = tmp_path / "fingerprints.csv"
    append_fingerprint(path, _row(n=1))
    append_fingerprint(path, _row(n=2))
    lines = path.read_text().strip().splitlines()
    assert lines[0].startswith("training_id")
    assert len(lines) == 3


def test_every_row_carries_its_conditions(tmp_path):
    """US59: conditions are per row, not per file."""
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [
        _row(n=1, session_id="quiet", background_w=0.0, concurrent_ids=""),
        _row(n=2, session_id="busy", background_w=85.0,
             concurrent_ids="desk_fan|incandescent_lamp"),
    ])
    rows = read_fingerprints(path)
    assert rows[0].background_w == 0.0
    assert rows[1].background_w == 85.0
    assert "desk_fan" in rows[1].concurrent_ids


def test_to_matrix_shapes():
    rows = [_row(label="kettle", n=1), _row(label="desk_fan", n=2)]
    X, y = to_matrix(rows)
    assert X.shape == (2, 14)
    assert y.shape == (2,)
    assert set(y) == {"kettle", "desk_fan"}


def test_to_matrix_preserves_column_order():
    rows = [_row(n=1)]
    X, _ = to_matrix(rows)
    for i, name in enumerate(FEATURE_COLUMNS):
        assert X[0, i] == pytest.approx(rows[0].features[name])


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_fingerprints(tmp_path / "nope.csv")


def test_empty_file_returns_no_rows(tmp_path):
    path = tmp_path / "empty.csv"
    write_fingerprints(path, [])
    assert read_fingerprints(path) == []


# -- R24: an unmeasured condition is BLANK, never a fabricated zero ---------

def test_an_unmeasured_condition_round_trips_as_none(tmp_path):
    """A capture with no telemetry in its window has no background, Vrms or
    frequency. That must be written as an empty cell and read back as None:
    reading it back as 0.0 would present an unmeasured 0 W / 0 V / 0 Hz as
    a measurement (non-negotiable #2)."""
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [
        _row(n=1, background_w=None, vrms_mean=None, freq_mean=None),
    ])
    header, line = path.read_text().splitlines()
    cells = dict(zip(header.split(","), line.split(",")))
    assert cells["background_w"] == ""
    assert cells["vrms_mean"] == ""
    assert cells["freq_mean"] == ""

    back = read_fingerprints(path)[0]
    assert back.background_w is None
    assert back.vrms_mean is None
    assert back.freq_mean is None


def test_a_measured_zero_background_stays_zero(tmp_path):
    """The other half: a real 0.0 is not confused with blank."""
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [_row(n=1, background_w=0.0)])
    assert read_fingerprints(path)[0].background_w == 0.0


def test_a_hand_blanked_condition_reads_as_none(tmp_path):
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [_row()])
    values = ["hand-001", "desk_fan", "on"]
    values += [str(float(i)) for i in range(14)]
    values += ["1754035999.0", "manual", "", "", "", "", "blank conditions"]
    with path.open("a", encoding="utf-8") as handle:
        handle.write(",".join(values) + "\n")
    row = read_fingerprints(path)[-1]
    assert (row.background_w, row.vrms_mean, row.freq_mean) == (None, None, None)


# -- R27c: a malformed hand edit is a clear error, not a crash ---------------

def _hand_row(features: list[str], tail: list[str] | None = None) -> str:
    values = ["hand-001", "desk_fan", "on", *features]
    if tail is None:
        tail = ["1754035999.0", "manual", "0.0", "", "240.0", "50.0", "x"]
    return ",".join(values + tail) + "\n"


def _append_line(tmp_path, line: str):
    path = tmp_path / "fingerprints.csv"
    write_fingerprints(path, [_row()])
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line)
    return path


def test_a_short_row_is_a_clear_error(tmp_path):
    path = _append_line(tmp_path, "hand-001,desk_fan,on,1.0,2.0\n")
    with pytest.raises(MalformedFingerprints) as error:
        read_fingerprints(path)
    assert "line 3" in str(error.value)


def test_an_empty_feature_is_a_clear_error(tmp_path):
    features = [str(float(i)) for i in range(14)]
    features[4] = ""
    path = _append_line(tmp_path, _hand_row(features))
    with pytest.raises(MalformedFingerprints) as error:
        read_fingerprints(path)
    assert "pf_disp" in str(error.value)
    assert "line 3" in str(error.value)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "abc"])
def test_a_non_finite_or_non_numeric_feature_is_a_clear_error(tmp_path, bad):
    features = [str(float(i)) for i in range(14)]
    features[0] = bad
    path = _append_line(tmp_path, _hand_row(features))
    with pytest.raises(MalformedFingerprints) as error:
        read_fingerprints(path)
    assert "delta_p" in str(error.value)


def test_malformed_is_a_value_error():
    """Callers that already catch ValueError keep working."""
    assert issubclass(MalformedFingerprints, ValueError)
