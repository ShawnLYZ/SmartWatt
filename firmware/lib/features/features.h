#pragma once

#include "detector.h"

constexpr int kFeatureCount = 14;

enum FeatureIndex {
  kDeltaP = 0,
  kDeltaQ1,
  kDeltaDist,
  kDeltaS,
  kPfDisp,
  kPfTrue,
  kDeltaIrms,
  kDeltaCrest,
  kH3H1,
  kH5H1,
  kH7H1,
  kInrushRatio,
  kSettleCycles,
  kLogDeltaP,
};

extern const char* const kFeatureNames[kFeatureCount];

struct FeatureVector {
  double v[kFeatureCount] = {};

  double& operator[](int i) { return v[i]; }
  double operator[](int i) const { return v[i]; }
};

/// Extracts the fourteen-dimensional signature from a settled edge.
///
/// NOTE THE ABSENCE OF AN `edge` PARAMETER. Edge direction is carried
/// separately and consumed by the tracker, never by the classifier. That
/// absence is a structural guarantee, not a convention: the classifier
/// cannot see edge direction because there is no way to hand it one.
///
/// Where that guarantee actually lives: FeatureVector carries no edge
/// dimension, so nothing downstream of extract() -- the classifier
/// included -- can ever see edge direction; there is no field to read it
/// from. That part the compiler enforces. extract()'s own parameter is a
/// DetectedEvent, and DetectedEvent::edge IS reachable from inside this
/// function's body, so extract() itself being edge-blind is this code
/// holding a discipline, not the type system blocking a violation --
/// test_on_and_off_share_one_feature_space is what holds it to that.
FeatureVector extract(const DetectedEvent& event);
