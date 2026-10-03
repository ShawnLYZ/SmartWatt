"""Cross-validation, confusion, importance, threshold and NDE.

Everything here mirrors the firmware's classifier exactly - k = 3,
inverse-distance weighted, over z-score normalised features - so a figure
measured here is a figure the device will reproduce.

The normalisation and threshold derivation mirror
firmware/lib/classify/fingerprints.cpp's normalised_distance,
compute_normalisation and derive_threshold bit for bit:

- Scale is the POPULATION standard deviation (ddof=0), matching
  compute_normalisation, which divides the sum of squared deviations by n,
  not n - 1.
- A column whose stddev is at or below kDegenerateStddev (1e-9) scales by
  1.0, never by the stddev itself and never only when it is exactly zero -
  see the note in fingerprints.cpp on why a 1e-9 floor would silently
  reject every real event on a table with a constant column.
- The 95th percentile of within-class centroid distance is NEAREST-RANK
  (sort ascending, take index ceil(0.95 * n) - 1, clamped to n - 1), not
  np.percentile's linear interpolation. The two conventions disagree on
  ordinary data, so using np.percentile here would silently derive a
  threshold the device does not reproduce.
- When that percentile is degenerate (zero, e.g. one row per class), the
  threshold falls back to half the smallest normalised distance between two
  class centroids; a single-class table has neither quantity and gets 0.0.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from sklearn.model_selection import StratifiedKFold
from sklearn.neighbors import KNeighborsClassifier

from .fingerprints import FEATURE_COLUMNS

#: firmware kDegenerateStddev. At or below this, a feature's standard
#: deviation counts as zero and scales by 1.0 instead - see the note in
#: fingerprints.cpp's normalised_distance.
DEGENERATE_STDDEV = 1e-9

#: Heading of the confusion matrix's extra column: the device declined.
REJECTED = "rejected"


class CannotCrossValidate(ValueError):
    """No class has the two rows cross-validation needs to hold one out."""


@dataclass(slots=True)
class CvResult:
    """Cross-validated accuracy, two ways, reported separately.

    ``accuracy`` is the DEVICE's figure (R23): a held-out event counts as
    correct only when it is NOT rejected - its distance to the winning
    class centroid is within the threshold derived from that fold's own
    training rows, exactly as knn.cpp decides - AND its label is right.
    ``plain_accuracy`` is plain k-NN top-1 with no rejection, a labelled
    reference figure only. ``confusion`` is the device's: one column per
    class, then a final REJECTED column. ``unscored`` lists classes with a
    single row: they sit in every training fold (the device would hold
    them) but cannot be held out, so no figure here covers them.
    """

    accuracy: float
    per_fold: list[float]
    labels: list[str]
    confusion: np.ndarray
    plain_accuracy: float = 0.0
    plain_per_fold: list[float] = field(default_factory=list)
    unscored: list[str] = field(default_factory=list)


@dataclass(slots=True)
class LiveResult:
    """Live held-out accuracy, with rejection and plain, as in CvResult."""

    accuracy: float
    plain_accuracy: float
    n: int


class _Device:
    """The firmware classifier (knn.cpp), fitted to one training table.

    Scaled by _scale_of - population stddev, a stddev <= 1e-9 scales by 1.0,
    exactly compute_normalisation and normalised_distance, NOT
    StandardScaler, whose own degenerate floor is ~1e-15 - then k = 3
    inverse-distance weighted, then rejection on the distance from the event
    to the WINNING CLASS CENTROID against the table's threshold. Distances
    are translation-invariant, so dividing by the scale without subtracting
    the mean gives the device's distances exactly.
    """

    def __init__(
        self, X: np.ndarray, y: np.ndarray, k: int = 3,
        threshold: float | None = None,
    ) -> None:
        self.scale = _scale_of(X)
        Z = X / self.scale
        self.knn = KNeighborsClassifier(
            n_neighbors=min(k, len(y)), weights="distance"
        ).fit(Z, y)
        self.centroids = {
            label: Z[y == label].mean(axis=0) for label in np.unique(y)
        }
        self.threshold = derive_threshold(X, y) if threshold is None else threshold

    def predict_plain(self, X: np.ndarray) -> np.ndarray:
        return self.knn.predict(X / self.scale)

    def predict(self, X: np.ndarray) -> np.ndarray:
        """The device's answer per row: the label, or None when rejected."""
        plain = self.predict_plain(X)
        Z = X / self.scale
        out = np.empty(len(plain), dtype=object)
        for i, label in enumerate(plain):
            distance = float(np.linalg.norm(Z[i] - self.centroids[label]))
            out[i] = label if distance <= self.threshold else None
        return out


def confusion(y_true: np.ndarray, y_pred: np.ndarray, labels: list[str]) -> np.ndarray:
    index = {label: i for i, label in enumerate(labels)}
    matrix = np.zeros((len(labels), len(labels)), dtype=int)
    for truth, prediction in zip(y_true, y_pred):
        matrix[index[truth], index[prediction]] += 1
    return matrix


def _device_confusion(
    y_true: np.ndarray, y_pred: np.ndarray, labels: list[str]
) -> np.ndarray:
    """Rows: truth over ``labels``. Columns: ``labels``, then REJECTED. A
    prediction of an unscored single-row class is in neither, so a row may
    sum to less than its class size."""
    column = {label: i for i, label in enumerate(labels)}
    matrix = np.zeros((len(labels), len(labels) + 1), dtype=int)
    for truth, prediction in zip(y_true, y_pred):
        if prediction is None:
            matrix[column[truth], len(labels)] += 1
        elif prediction in column:
            matrix[column[truth], column[prediction]] += 1
    return matrix


def _correct(y_true: np.ndarray, y_pred: np.ndarray) -> np.ndarray:
    """Correct means answered (not rejected) AND right."""
    return np.array(
        [p is not None and p == t for t, p in zip(y_true, y_pred)], dtype=bool
    )


def cross_validate(X: np.ndarray, y: np.ndarray, k: int = 3, folds: int = 5) -> CvResult:
    counts = {label: int(np.sum(y == label)) for label in sorted(set(y))}
    labels = [label for label, n in counts.items() if n >= 2]
    unscored = [label for label, n in counts.items() if n < 2]
    if not labels:
        raise CannotCrossValidate(
            "every class has a single row; cross-validation needs two rows "
            "of a class to hold one out"
        )

    scored_index = np.flatnonzero(np.isin(y, labels))
    always_train = np.flatnonzero(~np.isin(y, labels))
    folds = max(2, min(folds, min(counts[label] for label in labels)))

    splitter = StratifiedKFold(n_splits=folds, shuffle=True, random_state=0)
    predictions = np.empty(len(y), dtype=object)
    plain_predictions = np.empty(len(y), dtype=object)
    per_fold: list[float] = []
    plain_per_fold: list[float] = []

    for train_part, test_part in splitter.split(X[scored_index], y[scored_index]):
        train_index = np.concatenate([scored_index[train_part], always_train])
        test_index = scored_index[test_part]
        # R23: the threshold is derived per fold from THAT fold's training
        # rows only, as the device derives it from the table it holds.
        threshold = derive_threshold(X[train_index], y[train_index])
        device = _Device(X[train_index], y[train_index], k, threshold)

        fold_prediction = device.predict(X[test_index])
        fold_plain = device.predict_plain(X[test_index])
        predictions[test_index] = fold_prediction
        plain_predictions[test_index] = fold_plain
        per_fold.append(float(np.mean(_correct(y[test_index], fold_prediction))))
        plain_per_fold.append(float(np.mean(fold_plain == y[test_index])))

    truth = y[scored_index]
    return CvResult(
        accuracy=float(np.mean(_correct(truth, predictions[scored_index]))),
        per_fold=per_fold,
        labels=labels,
        confusion=_device_confusion(truth, predictions[scored_index], labels),
        plain_accuracy=float(np.mean(plain_predictions[scored_index] == truth)),
        plain_per_fold=plain_per_fold,
        unscored=unscored,
    )


def live_accuracy(
    X_train: np.ndarray, y_train: np.ndarray, X_live: np.ndarray, y_live: np.ndarray
) -> LiveResult:
    """A held-out session scored against the FULL training table and that
    table's derived threshold - what the device would hold - by the same
    classifier cross_validate uses. No live rows is the caller's to report:
    there is no accuracy of zero events."""
    if len(y_live) == 0:
        raise ValueError("no live rows to score")
    device = _Device(X_train, y_train)
    return LiveResult(
        accuracy=float(np.mean(_correct(y_live, device.predict(X_live)))),
        plain_accuracy=float(np.mean(device.predict_plain(X_live) == y_live)),
        n=int(len(y_live)),
    )


def permutation_importance_(
    X: np.ndarray, y: np.ndarray, k: int = 3, repeats: int = 10, seed: int = 0
) -> dict[str, float]:
    """Which of the fourteen dimensions earn their place.

    Drop in accuracy when a column is shuffled. The rest are pruned, which
    is what makes the fourteen a starting point rather than a commitment.
    """
    rng = np.random.default_rng(seed)
    baseline = cross_validate(X, y, k=k).accuracy

    out: dict[str, float] = {}
    for column, name in enumerate(FEATURE_COLUMNS):
        drops = []
        for _ in range(repeats):
            shuffled = X.copy()
            rng.shuffle(shuffled[:, column])
            drops.append(baseline - cross_validate(shuffled, y, k=k).accuracy)
        out[name] = float(np.mean(drops))
    return out


def _scale_of(X: np.ndarray) -> np.ndarray:
    """Per-column z-score scale, mirroring compute_normalisation's stddev.

    Population standard deviation (ddof=0); a column at or below
    DEGENERATE_STDDEV scales by 1.0 rather than by its (near-zero) stddev -
    see the note in fingerprints.cpp's normalised_distance.
    """
    scale = X.std(axis=0, ddof=0)
    return np.where(scale > DEGENERATE_STDDEV, scale, 1.0)


def _normalised(X: np.ndarray) -> np.ndarray:
    """z-score normalise X by its own column scale (see _scale_of)."""
    return X / _scale_of(X)


def _nearest_rank_percentile(values: np.ndarray, percentile: float) -> float:
    """Nearest-rank percentile, matching derive_threshold's std::sort +
    ceil(p * n) - 1, clamped. NOT np.percentile, which interpolates."""
    ordered = np.sort(values)
    n = len(ordered)
    if n == 0:
        return 0.0
    rank = int(np.ceil(percentile * n)) - 1
    rank = min(max(rank, 0), n - 1)
    return float(ordered[rank])


def derive_threshold(X: np.ndarray, y: np.ndarray) -> float:
    """95th percentile (nearest-rank) of within-class centroid distance,
    times 1.5.

    DERIVED from the training set, never hardcoded from intuition, and then
    validated against a load the system was never taught. Mirrors
    firmware/lib/classify/fingerprints.cpp's derive_threshold exactly,
    including its degenerate-table fallback, so a threshold measured here is
    the threshold the device will load.
    """
    Z = _normalised(X)
    labels = np.unique(y)

    distances: list[float] = []
    centroids: dict[object, np.ndarray] = {}
    for label in labels:
        cluster = Z[y == label]
        centroid = cluster.mean(axis=0)
        centroids[label] = centroid
        distances.extend(np.linalg.norm(cluster - centroid, axis=1))

    p95 = _nearest_rank_percentile(np.array(distances), 0.95)
    if p95 > 0.0:
        return p95 * 1.5

    # Degenerate table: no within-class spread to measure (e.g. one row per
    # class). Fall back to half the smallest between-class centroid
    # separation; a single-class table has no such pair and returns 0.0.
    closest: float | None = None
    label_list = list(labels)
    for i in range(len(label_list)):
        for j in range(i + 1, len(label_list)):
            separation = float(
                np.linalg.norm(centroids[label_list[i]] - centroids[label_list[j]])
            )
            if closest is None or separation < closest:
                closest = separation
    return closest * 0.5 if closest is not None else 0.0


def classify_or_reject(
    X_train: np.ndarray, y_train: np.ndarray, x: np.ndarray, threshold: float
) -> str | None:
    """Returns the label, or None when the point falls outside the threshold."""
    answer = _Device(X_train, y_train, threshold=threshold).predict(
        x.reshape(1, -1)
    )[0]
    return None if answer is None else str(answer)


def nde(
    truth: dict[str, np.ndarray], predicted: dict[str, np.ndarray]
) -> float:
    """Normalised Disaggregation Error.

            sum_t sum_a | yhat_a(t) - y_a(t) |
    NDE  =  -------------------------------------
                    sum_t sum_a  y_a(t)

    The standard NILM metric. Using it rather than inventing one signals
    that the literature was read.
    """
    numerator = 0.0
    denominator = 0.0
    for appliance, actual in truth.items():
        estimate = predicted.get(appliance, np.zeros_like(actual))
        numerator += float(np.abs(estimate - actual).sum())
        denominator += float(np.abs(actual).sum())
    return numerator / denominator if denominator > 0 else 0.0
