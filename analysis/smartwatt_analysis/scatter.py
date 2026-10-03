"""The two-dimensional signature plot.

Shows separability rather than asserting it (US56). Goes on the Setup
screen and on the poster.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402


def write_scatter(X: np.ndarray, y: np.ndarray, path: Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    projected = PCA(n_components=2, random_state=0).fit_transform(
        StandardScaler().fit_transform(X)
    )

    figure, axes = plt.subplots(figsize=(7, 5), dpi=150)
    for label in sorted(set(y)):
        mask = y == label
        axes.scatter(
            projected[mask, 0], projected[mask, 1], label=str(label), s=28,
            alpha=0.85, edgecolors="none",
        )

    axes.set_xlabel("first principal component")
    axes.set_ylabel("second principal component")
    axes.set_title("Appliance signatures")
    axes.legend(frameon=False, fontsize=8)
    axes.spines["top"].set_visible(False)
    axes.spines["right"].set_visible(False)
    figure.tight_layout()
    figure.savefig(path)
    plt.close(figure)
    return path
