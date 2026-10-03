import numpy as np
import pytest

from smartwatt_analysis.fingerprints import FEATURE_COLUMNS
from smartwatt_analysis.harness import (
    CannotCrossValidate,
    _nearest_rank_percentile,
    classify_or_reject,
    confusion,
    cross_validate,
    derive_threshold,
    live_accuracy,
    nde,
    permutation_importance_,
)

RNG = np.random.default_rng(20260813)


def separable(classes=5, per_class=20, jitter=0.01):
    """Perfectly separable: each class a tight cluster far from the others.

    Deviation from the brief's literal generator, noted in the task report:
    the brief jittered all 14 columns, including the 13 that carry no
    between-class signal. Under z-score normalisation (which the harness
    must use, to mirror the firmware - see the module docstring) EVERY
    column is rescaled to unit variance regardless of whether it is
    informative, so 13 columns of pure N(0, jitter) noise get inflated to
    the same scale as the one informative column and swamp it: the maximum
    z-scored between-class gap achievable in a single dimension (2.0, at
    exactly two classes) is always smaller than the combined noise floor of
    13 independent unit-variance columns (sqrt(2*13) ~= 5.1), for ANY choice
    of jitter, spacing or class count - the ratio is scale-invariant. So the
    literal generator can never reach perfect accuracy once z-scored; it was
    the CLAUDE.md-flagged case of a plan being "unreliable on numbers,
    physics". Fixed by jittering only the informative column; the other 13
    stay exactly constant (zero variance), which z-scoring correctly leaves
    at scale 1.0 and contributes nothing to any distance.

    R23: the jitter is BOUNDED (uniform in +-jitter), not Gaussian. The
    harness now scores the device, rejection included, and a Gaussian
    cluster has a tail: about 3% of its draws land beyond 1.5x the 95th
    percentile of their class's centroid distance and are (rightly)
    rejected. "Perfectly separable" has to mean inside the envelope as well
    as apart, or the anchor would not be 1.0 for a reason unrelated to the
    harness being correct. A bounded cluster's p95 x 1.5 sits well beyond
    its furthest member, so nothing is rejected.
    """
    X, y = [], []
    for c in range(classes):
        centre = np.zeros(len(FEATURE_COLUMNS))
        centre[0] = c * 1000.0
        for _ in range(per_class):
            row = centre.copy()
            row[0] += RNG.uniform(-jitter, jitter)
            X.append(row)
            y.append(f"class_{c}")
    return np.array(X), np.array(y, dtype=object)


def duplicated(per_class=20):
    """Two classes drawn from the SAME distribution. Chance is 50%.

    Deviation from the brief's literal generator, noted in the task report:
    the brief built each 'b' as the SAME point as its paired 'a' plus 1e-9
    noise, rather than an independent draw. With weights="distance" that
    near-duplicate twin sits at ~1e-9 and gets a ~1e9x vote weight whenever
    it lands in the training fold opposite its pair, which happens for most
    of a 5-fold split and deterministically flips the prediction to the
    WRONG label - empirically ~0.1 accuracy, well below chance, not because
    the harness leaks labels but because the fixture manufactures adversarial
    near-duplicates. That contradicts the docstring's own claim ("chance is
    50%") for two genuinely independent draws from the same distribution, so
    it is fixed here to draw 'a' and 'b' independently.
    """
    X, y = [], []
    for _ in range(per_class):
        X.append(RNG.normal(0, 1, len(FEATURE_COLUMNS)))
        y.append("a")
        X.append(RNG.normal(0, 1, len(FEATURE_COLUMNS)))
        y.append("b")
    return np.array(X), np.array(y, dtype=object)


# -- validating the harness itself ------------------------------------------

def test_separable_data_yields_perfect_accuracy():
    """If the harness cannot get 100% on perfectly separable data, nothing
    it says about real data means anything."""
    result = cross_validate(*separable())
    assert result.accuracy == pytest.approx(1.0)


def test_duplicated_data_yields_chance():
    """The other anchor: two identical distributions must come out at
    chance. A harness that scores well here is leaking labels."""
    result = cross_validate(*duplicated())
    assert 0.35 < result.accuracy < 0.65


def test_normalisation_decides_the_outcome():
    """Nothing else in this file actually exercises z-score normalisation:
    separable()'s non-informative columns are all exactly constant (scale
    1.0 either way), so a cross_validate that used a bare, unscaled
    KNeighborsClassifier would still pass every other test here.

    This fixture is built so the raw feature scales point the WRONG way:
    column 0 carries the real signal (class index, tightly clustered,
    std ~0.01) but column 1 is unrelated 3-orders-of-magnitude-louder noise
    (std ~1000) with no informative content at all. Unscaled, column 1's
    raw magnitude swamps column 0 in the Euclidean distance and
    classification collapses to near chance; z-scored, column 1 is put back
    on equal footing with column 0 and classification is exact. A dedicated,
    locally seeded generator is used rather than the shared module RNG, so
    this fixture's values don't depend on what ran before it in this file.
    """
    rng = np.random.default_rng(1)
    classes, per_class = 3, 20
    X, y = [], []
    for c in range(classes):
        for _ in range(per_class):
            row = np.zeros(len(FEATURE_COLUMNS))
            row[0] = c + rng.normal(0, 0.01)
            row[1] = rng.normal(0, 1000)
            X.append(row)
            y.append(f"class_{c}")
    X, y = np.array(X), np.array(y, dtype=object)

    result = cross_validate(X, y)
    assert result.accuracy == pytest.approx(1.0)


# -- R23: the figure is the DEVICE's behaviour, rejection included -----------

def _outlier_fixture():
    """Two classes of identical rows (no jitter, so every distance is exact)
    plus two rows of class 'a' displaced along column 1, away from 'b'.

    Plain k-NN names each outlier 'a' -- its nearest rows are a's -- so the
    plain top-1 is 1.0. But the device then measures the outlier's distance
    to the 'a' CENTROID against the threshold derived from the fold's
    training rows, and the outlier sits ~4.6 normalised units out against a
    threshold well under 1: rejected, so it is not a correct answer. Both
    outliers are scored exactly once across the folds: 38 / 40.
    """
    X, y = [], []
    for c, label in enumerate(("a", "b")):
        for n in range(20):
            row = np.zeros(len(FEATURE_COLUMNS))
            row[0] = 1000.0 * c
            if label == "a" and n in (0, 1):
                row[1] = 50.0
            X.append(row)
            y.append(label)
    return np.array(X), np.array(y, dtype=object)


def test_rejection_changes_the_figure():
    result = cross_validate(*_outlier_fixture())
    assert result.plain_accuracy == pytest.approx(1.0)
    assert result.accuracy == pytest.approx(38 / 40)


def test_a_rejected_event_is_counted_in_the_rejected_column():
    result = cross_validate(*_outlier_fixture())
    assert result.labels == ["a", "b"]
    # rows: truth a, b; columns: predicted a, b, then rejected.
    assert result.confusion.tolist() == [[18, 0, 2], [0, 20, 0]]


def test_the_threshold_is_derived_per_fold_from_training_rows_only(monkeypatch):
    """cross_validate must derive each fold's threshold from THAT fold's
    training rows -- never from the whole table, which would let a test
    row's own spread widen the threshold that judges it."""
    import smartwatt_analysis.harness as harness

    X, y = _outlier_fixture()
    seen = []
    real = harness.derive_threshold

    def spy(X_train, y_train):
        seen.append(len(y_train))
        return real(X_train, y_train)

    monkeypatch.setattr(harness, "derive_threshold", spy)
    harness.cross_validate(X, y)
    assert seen == [32] * 5


def test_live_accuracy_scores_rejection_too():
    X, y = _outlier_fixture()
    train = np.ones(len(y), dtype=bool)
    train[[0, 1]] = False  # the two outliers are the held-out session
    live = live_accuracy(X[train], y[train], X[~train], y[~train])
    assert live.plain_accuracy == pytest.approx(1.0)
    assert live.accuracy == pytest.approx(0.0)
    assert live.n == 2


def test_live_accuracy_on_a_clean_session_is_perfect():
    X, y = separable(classes=3, per_class=20)
    live = live_accuracy(X[::2], y[::2], X[1::2], y[1::2])
    assert live.accuracy == pytest.approx(1.0)
    assert live.plain_accuracy == pytest.approx(1.0)


def test_scale_uses_the_firmware_degenerate_rule_not_standardscalers():
    """StandardScaler divides a column with std ~5e-10 by that stddev (its
    own floor is ~1e-15); the firmware scales anything at or below 1e-9 by
    1.0. Column 1 here is pure noise at the 5e-10 scale: z-scored by its
    own stddev it becomes unit-variance noise as loud as column 0's class
    separation and costs answers; scaled by 1.0, as the device does, it
    contributes nothing and the classes separate cleanly."""
    rng = np.random.default_rng(3)
    X, y = [], []
    for c, label in enumerate(("a", "b")):
        for _ in range(20):
            row = np.zeros(len(FEATURE_COLUMNS))
            row[0] = 10.0 * c + rng.normal(0, 1.0)
            row[1] = rng.normal(0, 5e-10)
            X.append(row)
            y.append(label)
    X, y = np.array(X), np.array(y, dtype=object)
    assert 0 < X[:, 1].std() <= 1e-9
    assert cross_validate(X, y).plain_accuracy == pytest.approx(1.0)


# -- R27d: a class with one row cannot be cross-validated, and says so -------

def test_a_single_row_class_is_trained_on_but_not_scored():
    X, y = separable(classes=3, per_class=10)
    X = np.vstack([X, np.full(len(FEATURE_COLUMNS), 7.0)])
    y = np.append(y, "lonely")
    result = cross_validate(X, y)
    assert result.unscored == ["lonely"]
    assert "lonely" not in result.labels
    assert result.confusion.sum() == 30


def test_no_class_with_two_rows_cannot_be_cross_validated():
    X = np.zeros((2, len(FEATURE_COLUMNS)))
    X[1, 0] = 1.0
    with pytest.raises(CannotCrossValidate):
        cross_validate(X, np.array(["a", "b"], dtype=object))


def test_five_folds_by_default():
    result = cross_validate(*separable())
    assert len(result.per_fold) == 5


def test_confusion_matrix_is_square_and_sums_to_n():
    """One row per class; one column per class plus a final `rejected`
    column, because the device can decline to answer."""
    X, y = separable(classes=3, per_class=10)
    result = cross_validate(X, y)
    assert result.confusion.shape == (3, 4)
    assert result.confusion.sum() == 30


def test_confusion_is_diagonal_for_separable_data():
    X, y = separable(classes=3, per_class=10)
    result = cross_validate(X, y)
    assert np.trace(result.confusion[:, :3]) == result.confusion.sum()


def test_confusion_of_a_known_pair():
    labels = ["a", "b"]
    matrix = confusion(np.array(["a", "a", "b"]), np.array(["a", "b", "b"]), labels)
    assert matrix.tolist() == [[1, 1], [0, 1]]


# -- permutation importance -------------------------------------------------

def test_an_uninformative_feature_ranks_last():
    """Deviation from the brief, noted in the task report: the brief's
    absolute threshold (importance <= 0.01) is tighter than the sampling
    noise a single 5-repeat permutation test produces on 60 samples (one
    misclassification alone moves accuracy by 1/60 ~= 0.017), so it fails
    intermittently on a genuinely uninformative feature for reasons that
    have nothing to do with the mechanism under test. Asserting the
    uninformative feature's importance well below the informative one's
    tests the same property - it ranks near the bottom, clearly separated
    from the signal - without chasing an absolute magic number."""
    X, y = separable(classes=4, per_class=15)
    # Feature 0 carries all the signal; feature 5 is pure noise.
    X[:, 5] = RNG.normal(0, 1, len(X))
    importance = permutation_importance_(X, y, repeats=5)
    informative = importance[FEATURE_COLUMNS[0]]
    uninformative = importance[FEATURE_COLUMNS[5]]
    assert uninformative < 0.1 * informative


def test_the_informative_feature_ranks_first():
    X, y = separable(classes=4, per_class=15)
    importance = permutation_importance_(X, y, repeats=5)
    best = max(importance, key=lambda name: importance[name])
    assert best == FEATURE_COLUMNS[0]


def test_importance_covers_every_feature():
    X, y = separable(classes=3, per_class=10)
    assert set(permutation_importance_(X, y, repeats=3)) == set(FEATURE_COLUMNS)


# -- the rejection threshold -------------------------------------------------
#
# R2: the harness must reproduce the DEVICE's threshold exactly, mirroring
# firmware/lib/classify/fingerprints.cpp's normalised_distance,
# compute_normalisation and derive_threshold:
#   - population std (ddof=0); a column whose std <= 1e-9 (kDegenerateStddev)
#     scales by 1.0, not only when std == 0.
#   - p95 is NEAREST-RANK (sort, index ceil(0.95*n)-1, clamped), not
#     np.percentile's interpolation.
#   - threshold = p95 * 1.5 when p95 > 0; otherwise half the smallest
#     between-class centroid separation (0.0 if there is only one class).

def _firmware_normalise(X: np.ndarray) -> np.ndarray:
    """Independent re-derivation of the firmware's z-score scale rule."""
    scale = X.std(axis=0, ddof=0)
    scale = np.where(scale > 1e-9, scale, 1.0)
    return X / scale


def _nearest_rank_p95(distances: list[float]) -> float:
    distances = sorted(distances)
    n = len(distances)
    rank = int(np.ceil(0.95 * n)) - 1
    rank = min(rank, n - 1)
    return distances[rank]


def test_threshold_is_the_nearest_rank_p95_of_within_class_centroid_distance_times_1_5():
    """Asserted against the derivation, EXACTLY. Not approximately, and
    never against a recorded output - a regression must be distinguishable
    from a change in convention.

    The expected value is computed independently here by the firmware's
    nearest-rank convention, not np.percentile."""
    X, y = separable(classes=3, per_class=20)

    Z = _firmware_normalise(X)

    distances = []
    for label in np.unique(y):
        centroid = Z[y == label].mean(axis=0)
        distances.extend(np.linalg.norm(Z[y == label] - centroid, axis=1))
    expected = _nearest_rank_p95(distances) * 1.5

    assert derive_threshold(X, y) == pytest.approx(expected, rel=1e-9)


def test_nearest_rank_p95_differs_from_numpy_percentile():
    """Pins the convention as deliberate rather than accidental, against the
    HARNESS's own _nearest_rank_percentile - not a same-file reimplementation
    of it (a prior version of this test compared the test's own
    _nearest_rank_p95 helper to np.percentile, which would keep passing even
    if the harness itself silently switched to np.percentile). On [1..10],
    nearest-rank gives 10.0 (index ceil(0.95*10)-1 == 9, the last element);
    np.percentile's linear interpolation gives 9.55. They disagree, so a
    regression to np.percentile inside the harness is caught here."""
    distances = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0])
    nearest_rank = _nearest_rank_percentile(distances, 0.95)
    assert nearest_rank == pytest.approx(10.0)
    assert nearest_rank != pytest.approx(float(np.percentile(distances, 95)))


def test_a_near_zero_but_nonzero_stddev_column_still_scales_by_one():
    """Firmware's kDegenerateStddev is a THRESHOLD (<= 1e-9), not a test for
    exact zero: compute_normalisation's stddev is a floating-point sqrt of a
    variance sum and essentially never lands on exactly 0.0 for real data
    with a constant-in-theory column, so a rule that only special-cased
    std == 0 would silently divide by a near-zero, near-degenerate stddev
    and blow the distance up - see the note in normalised_distance about
    exactly this failure mode. A column with a genuine but tiny (1e-10)
    stddev must still scale by 1.0, same as an exactly-constant one."""
    classes = 3
    X = np.zeros((classes, len(FEATURE_COLUMNS)))
    X[:, 0] = [0.0, 10.0, 20.0]
    # Column 1 is "constant" in intent but has floating-point-scale jitter
    # well under the 1e-9 degenerate threshold - not exactly zero.
    X[:, 1] = np.array([0.0, 1e-10, -1e-10])
    y = np.array(["a", "b", "c"], dtype=object)

    scale = X.std(axis=0, ddof=0)
    assert 0.0 < scale[1] <= 1e-9  # the case this test exists to cover

    threshold = derive_threshold(X, y)

    # Column 1, correctly treated as degenerate (scale 1.0), contributes at
    # most ~1e-10 to any centroid distance - negligible next to column 0's
    # separation of 10. If it were instead divided by its own ~1e-10
    # stddev, its raw ~1e-10 differences would each become ~O(1), and for
    # a three-point, one-row-per-class table (p95 == 0, so this exercises
    # the same degenerate-fallback path as the dedicated fallback test)
    # that corrupted metric would show up directly in the derived
    # threshold, which is what this test pins against.
    # Half the closest normalised gap in column 0 alone (10 / std([0,10,20])).
    expected_ignoring_column_1 = 0.5 * (10.0 / X[:, 0].std(ddof=0))
    assert threshold == pytest.approx(expected_ignoring_column_1, rel=1e-6)


def test_threshold_degenerate_fallback_is_half_the_class_separation():
    """One row per class: every within-class distance is exactly zero, so
    p95 is zero and the derivation must fall back to half the normalised
    separation between the two class centroids."""
    X = np.zeros((2, len(FEATURE_COLUMNS)))
    X[1, 0] = 10.0
    y = np.array(["a", "b"], dtype=object)

    Z = _firmware_normalise(X)
    expected = float(np.linalg.norm(Z[0] - Z[1])) * 0.5

    assert derive_threshold(X, y) == pytest.approx(expected, rel=1e-9)


def test_threshold_is_positive():
    assert derive_threshold(*separable()) > 0


def test_threshold_moves_with_the_data():
    tight = derive_threshold(*separable(jitter=0.01))
    loose = derive_threshold(*separable(jitter=1.0))
    assert loose > tight


def test_an_untaught_load_is_rejected():
    """The threshold is DERIVED, then validated against a load the system
    was never taught."""
    X, y = separable(classes=3, per_class=20)
    threshold = derive_threshold(X, y)
    alien = np.full(len(FEATURE_COLUMNS), 99999.0)
    assert classify_or_reject(X, y, alien, threshold) is None


def test_a_taught_load_is_accepted():
    X, y = separable(classes=3, per_class=20)
    threshold = derive_threshold(X, y)
    assert classify_or_reject(X, y, X[0], threshold) == y[0]


# -- NDE --------------------------------------------------------------------

def test_nde_of_a_perfect_prediction_is_zero():
    truth = {"kettle": np.array([1800.0, 1800.0, 0.0])}
    assert nde(truth, dict(truth)) == pytest.approx(0.0)


def test_nde_matches_the_published_definition():
    truth = {"kettle": np.array([100.0, 100.0]), "fan": np.array([50.0, 50.0])}
    predicted = {"kettle": np.array([90.0, 110.0]), "fan": np.array([60.0, 40.0])}
    # sum|error| = 10 + 10 + 10 + 10 = 40; sum truth = 300
    assert nde(truth, predicted) == pytest.approx(40.0 / 300.0)


def test_nde_of_all_zero_prediction_is_one():
    truth = {"kettle": np.array([100.0, 100.0])}
    predicted = {"kettle": np.array([0.0, 0.0])}
    assert nde(truth, predicted) == pytest.approx(1.0)


def test_nde_handles_a_missing_appliance():
    truth = {"kettle": np.array([100.0]), "fan": np.array([50.0])}
    assert nde(truth, {"kettle": np.array([100.0])}) == pytest.approx(50.0 / 150.0)
