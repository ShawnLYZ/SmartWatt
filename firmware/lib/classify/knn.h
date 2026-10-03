#pragma once

#include "fingerprints.h"

struct Neighbour {
  char label[24] = {};
  char training_id[32] = {};
  double distance = 0.0;
};

struct Classification {
  char label[24] = {};
  double confidence = 0.0;
  bool rejected = true;
  /// How many leading entries of `neighbours` classify() actually filled.
  ///
  /// A table with fewer than kK rows cannot fill them all, and the trailing
  /// slots then hold a default-constructed Neighbour -- empty strings and a
  /// distance of 0.0, which is the value of a PERFECT match. Without this
  /// count each consumer has to infer emptiness from a field, and they did
  /// not agree: the tracker skipped empty labels while the emitter renamed
  /// them "unknown" and shipped their zero distance as the top-ranked
  /// evidence for a classification. The count is the primary test; the
  /// empty-label checks that remain are belt and braces.
  int neighbour_count = 0;
  Neighbour neighbours[3];
};

/// k-nearest neighbours, k = 3, inverse-distance weighted, over z-score
/// normalised features.
///
/// Chosen over a neural network for four reasons that hold at this data
/// volume: roughly 100 samples in 14 dimensions cannot support a network
/// without overfitting; nearest neighbours are directly explainable, so any
/// answer traces to the training events that produced it; retraining means
/// appending rows to a file with no reflash; and a distance threshold gives
/// principled rejection, which a softmax does not.
class Knn {
 public:
  static constexpr int kK = 3;

  explicit Knn(const FingerprintTable& table) : table_(table) {}

  /// NOTE: no edge parameter. The classifier cannot see edge direction.
  Classification classify(const FeatureVector& features) const;

 private:
  const FingerprintTable& table_;
};
