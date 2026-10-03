#pragma once

#include "features.h"

struct FingerprintRow {
  char training_id[32] = {};
  char label[24] = {};
  FeatureVector features;
};

struct FingerprintTable {
  static constexpr int kMaxRows = 256;

  FingerprintRow rows[kMaxRows];
  int count = 0;

  /// z-score parameters, computed over the table.
  FeatureVector mean;
  FeatureVector stddev;

  /// Derived in S8 as the 95th percentile of within-class centroid distance
  /// multiplied by 1.5, then validated against an untaught load. NEVER
  /// hardcoded from intuition.
  double rejection_threshold = 0.0;

  /// Which dimensions earned their place by measured permutation importance.
  bool active[kFeatureCount] = {};

  /// Short hash of this table, echoed in telemetry so the wizard can tell
  /// which table the device actually holds.
  char fingerprint_id[9] = {};

  /// Resets this table, in place, to exactly what a freshly constructed one
  /// holds.
  ///
  /// NEVER `table = FingerprintTable{}` ON THE DEVICE. That builds a ~43 KB
  /// temporary in the caller's stack frame, more than five times the Arduino
  /// loop task's 8 KB stack: setup() did exactly that and overflowed at boot,
  /// before printing its first line. The host tests have megabytes of stack
  /// and never noticed. Resetting in place also keeps Knn's reference to the
  /// table valid.
  void clear();
};

/// Mirrors Seam 2's shape so S8 adds a transport rather than a redesign.
class IFingerprintSource {
 public:
  virtual ~IFingerprintSource() = default;
  virtual bool load(FingerprintTable& out) = 0;
};

class MemoryFingerprintSource : public IFingerprintSource {
 public:
  explicit MemoryFingerprintSource(const FingerprintTable& table)
      : table_(table) {}
  bool load(FingerprintTable& out) override;

 private:
  FingerprintTable table_;
};

/// Reads the plain, hand-editable CSV described in the S8 spec.
class FileFingerprintSource : public IFingerprintSource {
 public:
  explicit FileFingerprintSource(const char* path) : path_(path) {}
  bool load(FingerprintTable& out) override;

 private:
  const char* path_;
};

/// Euclidean distance over the z-score normalised ACTIVE dimensions only.
///
/// Declared here rather than kept private to fingerprints.cpp because
/// derive_threshold() and Knn::classify() must measure with the same ruler:
/// the threshold is the 95th percentile of within-class centroid distance
/// under THIS metric, and classify() compares an event's centroid distance
/// against it. Two copies of the metric that drifted apart would leave the
/// rejection test comparing one measurement against a threshold derived from
/// a different one, which is silently meaningless rather than loudly broken.
double normalised_distance(const FeatureVector& a, const FeatureVector& b,
                           const FingerprintTable& table);

/// True for a label a fingerprint source may keep.
///
/// A trained label is copied verbatim onto Classification::label, then onto
/// ActiveEntry::id, then into telemetry's `attribution.active[].id` -- which
/// the contract restricts to `^([a-z][a-z0-9_]*|unknown_[0-9]+)$`. Declared
/// here rather than kept private to fingerprints.cpp for the same reason
/// normalised_distance is: S8's transport must admit exactly the labels this
/// loader admits, and two copies of the rule that drifted apart would let a
/// label through one door that the other refuses.
///
/// The `unknown_[0-9]+` arm is deliberately NOT admitted even though the
/// schema allows it -- see the note on the definition.
bool is_emittable_label(const char* label);

/// Computes z-score mean and standard deviation over the table's rows.
void compute_normalisation(FingerprintTable& table);

/// 95th percentile of within-class centroid distance, times 1.5.
///
/// Falls back to half the smallest between-class centroid distance when the
/// within-class spread is degenerate -- one row per class measures no spread
/// at all -- and returns 0.0 only when the table has a single class and no
/// spread either, which cannot classify anything. See the note on the
/// definition.
double derive_threshold(const FingerprintTable& table);

/// Short content hash, so a stale table on the device is visible rather
/// than presenting as "trained but classifying wrong".
void compute_fingerprint_id(FingerprintTable& table);
