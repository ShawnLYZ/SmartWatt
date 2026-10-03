import json

import numpy as np
import pytest

from smartwatt_analysis.cli import main
from smartwatt_analysis.fingerprints import FEATURE_COLUMNS, Fingerprint, write_fingerprints
from smartwatt_analysis.harness import cross_validate, derive_threshold
from smartwatt_analysis.report import TARGETS, Measured, build_report
from smartwatt_analysis.scatter import write_scatter

RNG = np.random.default_rng(7)


def _fingerprint_row(label, n) -> Fingerprint:
    features = {name: float(i + n) for i, name in enumerate(FEATURE_COLUMNS)}
    return Fingerprint(
        training_id=f"{label}-on-{n:03d}",
        label=label,
        edge="on",
        features=features,
        ts=1754035200.0 + n,
        session_id="session-a",
        background_w=0.0,
        concurrent_ids="",
        vrms_mean=240.1,
        freq_mean=49.98,
        notes="",
    )


def dataset(classes=5, per_class=20):
    X, y = [], []
    for c in range(classes):
        centre = np.zeros(14)
        centre[0] = c * 1000.0
        for _ in range(per_class):
            X.append(centre + RNG.normal(0, 0.5, 14))
            y.append(f"class_{c}")
    return np.array(X), np.array(y, dtype=object)


@pytest.fixture
def report():
    X, y = dataset()
    cv = cross_validate(X, y)
    return build_report(
        cv=cv,
        live_accuracy=0.88,
        importance={f"f{i}": 0.1 for i in range(14)},
        threshold=derive_threshold(X, y),
        measured=[
            Measured("power_accuracy", 0.014),
            Measured("detection_floor", 7.5),
            Measured("event_recall", 0.96),
            Measured("event_precision", 1.2),
            Measured("rejection", 0.93),
            Measured("nde", 0.11),
            Measured("latency", 2.1),
            Measured("overruns", 0.0),
        ],
        conditions="Live session ran at 20:00 with a 120 W background load.",
    )


def test_nine_targets():
    assert len(TARGETS) == 9


def test_targets_match_the_published_set():
    keys = {t.key for t in TARGETS}
    assert keys == {
        "power_accuracy", "detection_floor", "event_recall", "event_precision",
        "classification", "rejection", "nde", "latency", "overruns",
    }


def test_report_names_every_target(report):
    """US76: missing a target is documented rather than quietly dropped."""
    for target in TARGETS:
        assert target.name in report, target.name


def test_every_target_carries_a_verdict(report):
    assert report.count("MET") + report.count("NOT MET") >= 10


def test_classification_is_reported_twice(report):
    """Ten rows for nine targets: classification has one stated target and
    is reported cross-validated AND live, because the two must never be
    averaged into one figure."""
    assert "cross-validated" in report.lower()
    assert "live held-out" in report.lower()


def test_cv_and_live_are_never_averaged(report):
    assert "0.88" in report or "88" in report
    assert "average" not in report.lower().split("gap")[0]


def test_the_mean_of_cv_and_live_is_never_printed():
    """A stronger check than 'the word average is absent': the actual
    arithmetic mean of cv.accuracy and live_accuracy, formatted the way the
    report formats its own figures, must not appear anywhere - not even as
    an unlabelled coincidence."""
    X, y = dataset()
    cv = cross_validate(X, y)
    live_accuracy = 0.6123456  # chosen so the mean can't coincide with
    # another printed figure (threshold, per-fold accuracy, etc).
    report = build_report(
        cv=cv,
        live_accuracy=live_accuracy,
        importance={},
        threshold=derive_threshold(X, y),
        measured=[],
        conditions="",
    )
    mean = (cv.accuracy + live_accuracy) / 2
    assert f"{mean:.3f}" not in report
    assert f"{mean:g}" not in report


def test_the_gap_gets_its_own_section(report):
    assert "gap" in report.lower()
    assert "background load" in report


def test_a_missed_target_says_so():
    X, y = dataset()
    report = build_report(
        cv=cross_validate(X, y),
        live_accuracy=0.61,
        importance={},
        threshold=1.0,
        measured=[Measured("detection_floor", 18.0)],
        conditions="",
    )
    assert "NOT MET" in report


def test_targets_are_not_lowered():
    """The stated column is a constant, not something the report computes."""
    stated = {t.key: t.stated for t in TARGETS}
    assert stated["classification"] == 0.92
    assert stated["detection_floor"] == 10.0
    assert stated["overruns"] == 0.0


def _row_cells(report: str, row_label: str) -> list[str]:
    for line in report.splitlines():
        if line.strip().startswith("|") and row_label in line:
            return [cell.strip() for cell in line.strip().strip("|").split("|")]
    raise AssertionError(f"no row found for {row_label!r}")


def test_an_unmeasured_target_is_reported_as_unmeasured():
    report = build_report(
        cv=cross_validate(*dataset()),
        live_accuracy=None,
        importance={},
        threshold=1.0,
        measured=[],
        conditions="",
    )
    assert "not measured" in report.lower()

    floor_cells = _row_cells(report, "Detection floor")
    assert floor_cells[-1] == "not measured"

    live_cells = _row_cells(report, "live held-out")
    assert live_cells[-1] == "not measured"


def test_report_includes_the_confusion_matrix(report):
    assert "confusion" in report.lower()


def test_report_includes_the_derived_threshold(report):
    assert "threshold" in report.lower()


def test_rejection_threshold_section_names_no_specific_loads(report):
    section = report.split("## Rejection threshold")[1].split("##")[0]
    assert "phone charger" not in section.lower()
    assert "clothes iron" not in section.lower()


def test_rejection_threshold_reports_validation_when_measured():
    X, y = dataset()
    report = build_report(
        cv=cross_validate(X, y),
        live_accuracy=None,
        importance={},
        threshold=derive_threshold(X, y),
        measured=[Measured("rejection", 0.93)],
        conditions="",
    )
    section = report.split("## Rejection threshold")[1].split("##")[0]
    assert "0.93" in section or "93" in section
    assert "validated" in section.lower()
    assert "has not been measured" not in section.lower()


def test_rejection_threshold_says_not_yet_validated_when_unmeasured():
    X, y = dataset()
    report = build_report(
        cv=cross_validate(X, y),
        live_accuracy=None,
        importance={},
        threshold=derive_threshold(X, y),
        measured=[],
        conditions="",
    )
    section = report.split("## Rejection threshold")[1].split("##")[0]
    assert "has not been measured" in section.lower()


def test_scatter_writes_a_png(tmp_path):
    X, y = dataset(classes=3, per_class=10)
    path = write_scatter(X, y, tmp_path / "scatter.png")
    assert path.exists()
    assert path.stat().st_size > 0


def test_cli_writes_a_report_and_scatter(tmp_path):
    rows = [
        _fingerprint_row(label, n)
        for label in ("kettle", "fan")
        for n in range(4)
    ]
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)

    live_rows = [
        _fingerprint_row(label, n)
        for label in ("kettle", "fan")
        for n in range(4, 6)
    ]
    live_path = tmp_path / "live.csv"
    write_fingerprints(live_path, live_rows)

    exit_code = main([
        "--fingerprints", str(fingerprints_path),
        "--live", str(live_path),
        "--out", str(tmp_path),
    ])

    assert exit_code == 0
    reports = list(tmp_path.glob("*-report.md"))
    assert len(reports) == 1
    assert reports[0].stat().st_size > 0
    assert (tmp_path / "scatter.png").exists()


def test_cli_measured_json_drives_the_validated_rejection_branch(tmp_path):
    """--measured '{"rejection": ..., "detection_floor": ...}' must reach the
    report's validated-rejection prose and a MET verdict for detection floor -
    end to end through the CLI, not just through build_report directly."""
    rows = [
        _fingerprint_row(label, n)
        for label in ("kettle", "fan")
        for n in range(4)
    ]
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)

    measured_path = tmp_path / "measured.json"
    measured_path.write_text(
        json.dumps({"rejection": 0.93, "detection_floor": 7.5}), encoding="utf-8"
    )

    exit_code = main([
        "--fingerprints", str(fingerprints_path),
        "--measured", str(measured_path),
        "--out", str(tmp_path),
    ])

    assert exit_code == 0
    reports = list(tmp_path.glob("*-report.md"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")

    section = text.split("## Rejection threshold")[1].split("##")[0]
    assert "validated" in section.lower()
    assert "0.93" in section or "93" in section
    assert "has not been measured" not in section.lower()

    floor_cells = _row_cells(text, "Detection floor")
    assert floor_cells[-1] == "MET"


def test_cli_reports_a_missing_measured_file_cleanly(tmp_path, capsys):
    rows = [
        _fingerprint_row(label, n)
        for label in ("kettle", "fan")
        for n in range(4)
    ]
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)

    exit_code = main([
        "--fingerprints", str(fingerprints_path),
        "--measured", str(tmp_path / "does-not-exist.json"),
        "--out", str(tmp_path),
    ])

    assert exit_code != 0
    assert not list(tmp_path.glob("*-report.md"))
    captured = capsys.readouterr()
    assert "error" in captured.out.lower()


def test_cli_reports_a_malformed_measured_file_cleanly(tmp_path, capsys):
    rows = [
        _fingerprint_row(label, n)
        for label in ("kettle", "fan")
        for n in range(4)
    ]
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)

    measured_path = tmp_path / "measured.json"
    measured_path.write_text("{not valid json", encoding="utf-8")

    exit_code = main([
        "--fingerprints", str(fingerprints_path),
        "--measured", str(measured_path),
        "--out", str(tmp_path),
    ])

    assert exit_code != 0
    assert not list(tmp_path.glob("*-report.md"))
    captured = capsys.readouterr()
    assert "error" in captured.out.lower()


# -- R23: verdicts use the device's figure, rejection included --------------

def _cv(accuracy, plain, labels=("a", "b"), unscored=()):
    from smartwatt_analysis.harness import CvResult

    n = len(labels)
    return CvResult(
        accuracy=accuracy,
        per_fold=[accuracy],
        labels=list(labels),
        confusion=np.zeros((n, n + 1), dtype=int),
        plain_accuracy=plain,
        plain_per_fold=[plain],
        unscored=list(unscored),
    )


def test_the_classification_verdict_uses_the_with_rejection_figure():
    """Plain k-NN at 0.97 would MEET 0.92; the device, rejecting, is at
    0.5. The verdict is the device's."""
    report = build_report(
        cv=_cv(0.5, 0.97), live_accuracy=0.4, importance={}, threshold=1.0,
        measured=[], conditions="", live_plain_accuracy=0.99,
    )
    cv_cells = _row_cells(report, "cross-validated")
    assert cv_cells[2] == "0.5"
    assert cv_cells[-1] == "NOT MET"
    live_cells = _row_cells(report, "live held-out")
    assert live_cells[2] == "0.4"
    assert live_cells[-1] == "NOT MET"


def test_the_plain_figures_are_shown_labelled_and_unverdicted():
    report = build_report(
        cv=_cv(0.5, 0.9731), live_accuracy=0.4, importance={}, threshold=1.0,
        measured=[], conditions="", live_plain_accuracy=0.9912,
    )
    plain = [line for line in report.splitlines() if "0.973" in line]
    assert plain
    for line in plain:
        assert "without rejection" in line.lower() or (
            "without the device's rejection" in line.lower()
        )
        assert not line.lstrip().startswith("|")  # never a verdict row
    assert any("0.991" in line for line in plain)


def test_no_mean_of_any_two_classification_figures_is_printed():
    figures = {"cv": 0.5123, "plain_cv": 0.9731, "live": 0.4417, "plain_live": 0.8819}
    report = build_report(
        cv=_cv(figures["cv"], figures["plain_cv"]),
        live_accuracy=figures["live"], importance={}, threshold=1.0,
        measured=[], conditions="", live_plain_accuracy=figures["plain_live"],
    )
    values = list(figures.values())
    for i in range(len(values)):
        for j in range(i + 1, len(values)):
            mean = (values[i] + values[j]) / 2
            assert f"{mean:.3f}" not in report
            assert f"{mean:g}" not in report


def test_the_confusion_matrix_has_a_rejected_column():
    report = build_report(
        cv=cross_validate(*dataset()), live_accuracy=None, importance={},
        threshold=1.0, measured=[], conditions="",
    )
    header = report.split("## Confusion matrix")[1].strip().splitlines()[0]
    assert header.rstrip(" |").endswith("rejected")


# -- R27d: limitations are reported, not crashed on --------------------------

def test_an_unscored_class_is_reported_as_a_limitation():
    report = build_report(
        cv=_cv(1.0, 1.0, unscored=("lonely",)), live_accuracy=None,
        importance={}, threshold=1.0, measured=[], conditions="",
    )
    section = report.split("## Limitations")[1].split("##")[0]
    assert "lonely" in section


def test_cli_survives_a_single_row_class(tmp_path):
    rows = [_fingerprint_row(label, n) for label in ("kettle", "fan") for n in range(4)]
    rows.append(_fingerprint_row("lonely", 9))
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)

    exit_code = main(["--fingerprints", str(fingerprints_path), "--out", str(tmp_path)])

    assert exit_code == 0
    text = next(tmp_path.glob("*-report.md")).read_text(encoding="utf-8")
    assert "lonely" in text.split("## Limitations")[1]


def test_cli_survives_an_empty_live_file(tmp_path):
    rows = [_fingerprint_row(label, n) for label in ("kettle", "fan") for n in range(4)]
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)
    live_path = tmp_path / "live.csv"
    write_fingerprints(live_path, [])

    exit_code = main([
        "--fingerprints", str(fingerprints_path), "--live", str(live_path),
        "--out", str(tmp_path),
    ])

    assert exit_code == 0
    text = next(tmp_path.glob("*-report.md")).read_text(encoding="utf-8")
    assert _row_cells(text, "live held-out")[-1] == "not measured"
    assert "no rows" in text.split("## Limitations")[1]


def test_cli_reports_every_class_single_row_cleanly(tmp_path, capsys):
    rows = [_fingerprint_row("kettle", 1), _fingerprint_row("fan", 2)]
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, rows)

    exit_code = main(["--fingerprints", str(fingerprints_path), "--out", str(tmp_path)])

    assert exit_code != 0
    assert "single row" in capsys.readouterr().out


def test_cli_reports_a_malformed_fingerprints_file_cleanly(tmp_path, capsys):
    fingerprints_path = tmp_path / "fingerprints.csv"
    write_fingerprints(fingerprints_path, [_fingerprint_row("kettle", 1)])
    with fingerprints_path.open("a", encoding="utf-8") as handle:
        handle.write("hand-001,fan,on,1.0\n")

    exit_code = main(["--fingerprints", str(fingerprints_path), "--out", str(tmp_path)])

    assert exit_code != 0
    assert "line 3" in capsys.readouterr().out
