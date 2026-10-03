#include "knn.h"

#include <algorithm>
#include <cstdio>
#include <cstring>

// knn.h declares `Neighbour neighbours[3]` as a fixed-extent array while kK
// is a separate constant. Raising kK without widening that array would walk
// off the end of it in the fill loop below with no diagnostic at all, so nail
// the two together here where a mismatch becomes a compile error.
static_assert(Knn::kK == static_cast<int>(sizeof(Classification::neighbours) /
                                          sizeof(Neighbour)),
              "Knn::kK must match the extent of Classification::neighbours");

namespace {

constexpr double kTinyDistance = 1e-12;

FeatureVector centroid_of(const FingerprintTable& table, const char* label) {
  FeatureVector sum;
  int count = 0;
  for (int r = 0; r < table.count; ++r) {
    if (std::strcmp(table.rows[r].label, label) != 0) continue;
    for (int i = 0; i < kFeatureCount; ++i) sum[i] += table.rows[r].features[i];
    ++count;
  }
  if (count > 0) {
    for (int i = 0; i < kFeatureCount; ++i) {
      sum[i] /= static_cast<double>(count);
    }
  }
  return sum;
}

}  // namespace

Classification Knn::classify(const FeatureVector& features) const {
  // FIXED-CAPACITY THROUGHOUT, DELIBERATELY. classify() is the event path,
  // where this plan's global constraint forbids dynamic allocation, and it
  // is the constraint that has to survive the move onto the ESP32-S3 in S6.
  // So: no std::vector, no std::string, no allocation of any kind below --
  // and no kMaxRows-sized scratch array either, since 256 rows of scores is
  // 4 KB of stack for a k of 3.
  //
  // derive_threshold() and the fingerprint sources next door DO use STL
  // containers, and that is not an inconsistency: they run once, when a
  // table is loaded, and never per event.
  Classification result;
  result.rejected = true;
  result.confidence = 0.0;

  if (table_.count == 0) return result;

  // Top-k selection by insertion into a k-length array, one pass over the
  // table. O(n*k) for k = 3, which beats sorting all n, and it leaves the
  // winners already in ascending distance order. Ties resolve to the
  // earlier row -- both comparisons below are strict -- so the same table
  // and the same event always give the same neighbours.
  struct Scored {
    int index = 0;
    double distance = 0.0;
  };
  Scored best[kK];
  int taken = 0;

  for (int r = 0; r < table_.count; ++r) {
    const double d =
        normalised_distance(features, table_.rows[r].features, table_);
    if (taken == kK && !(d < best[kK - 1].distance)) continue;

    int slot = (taken < kK) ? taken : kK - 1;
    while (slot > 0 && best[slot - 1].distance > d) {
      best[slot] = best[slot - 1];
      --slot;
    }
    best[slot].index = r;
    best[slot].distance = d;
    if (taken < kK) ++taken;
  }

  for (int n = 0; n < taken; ++n) {
    const FingerprintRow& row = table_.rows[best[n].index];
    std::snprintf(result.neighbours[n].label, sizeof(result.neighbours[n].label),
                  "%s", row.label);
    std::snprintf(result.neighbours[n].training_id,
                  sizeof(result.neighbours[n].training_id), "%s",
                  row.training_id);
    result.neighbours[n].distance = best[n].distance;
  }
  // Published, so no consumer has to guess which trailing slots are real. A
  // short table leaves the rest default-constructed, and a default Neighbour's
  // distance of 0.0 is indistinguishable from a perfect match.
  result.neighbour_count = taken;

  // Inverse-distance weighted vote. At most kK neighbours vote, so at most
  // kK distinct labels can appear -- which is what makes a fixed kK-wide
  // tally the whole story rather than a capped approximation of one.
  char labels[kK][sizeof(Neighbour::label)] = {};
  double weights[kK] = {};
  int label_count = 0;
  double total_weight = 0.0;

  for (int n = 0; n < taken; ++n) {
    const double weight =
        1.0 / std::max(result.neighbours[n].distance, kTinyDistance);
    total_weight += weight;

    int slot = -1;
    for (int l = 0; l < label_count; ++l) {
      if (std::strcmp(labels[l], result.neighbours[n].label) == 0) {
        slot = l;
        break;
      }
    }
    if (slot < 0) {
      slot = label_count++;
      std::snprintf(labels[slot], sizeof(labels[slot]), "%s",
                    result.neighbours[n].label);
    }
    weights[slot] += weight;
  }

  int winner = 0;
  for (int l = 1; l < label_count; ++l) {
    if (weights[l] > weights[winner]) winner = l;
  }
  std::snprintf(result.label, sizeof(result.label), "%s", labels[winner]);
  result.confidence =
      (total_weight > 0.0) ? weights[winner] / total_weight : 0.0;

  // Rejection: distance from the event to the WINNING CLASS CENTROID against
  // the derived threshold. Comparing against the centroid rather than the
  // nearest neighbour is what makes the threshold's derivation - the 95th
  // percentile of within-class centroid distance - the right yardstick.
  const FeatureVector centroid = centroid_of(table_, result.label);
  const double centroid_distance =
      normalised_distance(features, centroid, table_);
  result.rejected = centroid_distance > table_.rejection_threshold;

  if (result.rejected) {
    result.confidence = 0.0;
    result.label[0] = '\0';
  }

  return result;
}
