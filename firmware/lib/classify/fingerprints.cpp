#include "fingerprints.h"

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <new>
#include <sstream>
#include <string>
#include <vector>

namespace {

/// At or below this, a feature's standard deviation counts as zero.
///
/// NOT a divisor. See the note in normalised_distance: a degenerate column is
/// scaled by 1.0, and this constant only decides which columns are degenerate.
constexpr double kDegenerateStddev = 1e-9;

/// The tracker's generated-id shape, which a trained label may not take.
constexpr char kReservedPrefix[] = "unknown_";
constexpr int kReservedPrefixLength = sizeof(kReservedPrefix) - 1;

}  // namespace

void FingerprintTable::clear() {
  // Value-initialises this very object again: every default member
  // initializer re-runs in place, with no temporary anywhere. A field added
  // later is reset too, without anyone remembering to list it here. Legal
  // because the type has no const or reference members, so every existing
  // reference (Knn's included) now names the fresh table.
  this->~FingerprintTable();
  new (this) FingerprintTable();
}

double normalised_distance(const FeatureVector& a, const FeatureVector& b,
                           const FingerprintTable& table) {
  double sum = 0.0;
  for (int i = 0; i < kFeatureCount; ++i) {
    if (!table.active[i]) continue;
    // A ZERO-VARIANCE FEATURE SCALES BY 1.0, NOT BY AN EPSILON.
    //
    // Dividing by a 1e-9 floor did the exact opposite of guarding the metric.
    // settle_cycles is constant across a whole table whenever the settle run
    // ends at its minimum -- detector.cpp sets it to transient_cycles_ and
    // settle_run defaults to 3 -- so a real, correctly-trained table has a
    // constant column in it. Under the floor, an event differing by ONE cycle
    // scored a distance of exactly 1e9 and was rejected, while every training
    // row scored 0 in that column and the derived threshold stayed small. The
    // guard rejected every real event and the derivation could not compensate,
    // because it measured the same zero.
    //
    // 1.0 is the standard treatment (sklearn's StandardScaler does it): a
    // column that carries no information contributes its difference in RAW
    // units rather than exploding it. The spec says only "z-score normalised
    // using training-set mean and standard deviation", which is undefined at
    // zero variance, so this is a choice the spec leaves open rather than a
    // departure from it.
    const double scale =
        (table.stddev[i] > kDegenerateStddev) ? table.stddev[i] : 1.0;
    const double d = (a[i] - b[i]) / scale;
    sum += d * d;
  }
  return std::sqrt(sum);
}

bool is_emittable_label(const char* label) {
  // First arm of the contract's id pattern, `[a-z][a-z0-9_]*`. An EMPTY label
  // fails on the first test, which is deliberate: it reaches the wire as
  // `{"id": ""}` -- schema-invalid -- while quoted_or_null maps it to
  // `attributed_to: null`, so the event denies an attribution the tracker
  // actually made.
  if (label == nullptr) return false;
  if (label[0] < 'a' || label[0] > 'z') return false;
  for (const char* c = label; *c != '\0'; ++c) {
    const bool allowed = (*c >= 'a' && *c <= 'z') || (*c >= '0' && *c <= '9') ||
                         *c == '_';
    if (!allowed) return false;
  }

  // The pattern's SECOND arm, `unknown_[0-9]+`, is refused even though the
  // schema admits it. That shape is reserved for ids Tracker generates for
  // loads it could not name, and tracker.cpp uses the shape itself to decide
  // which active entries may be retired by delta-pairing on an OFF edge (see
  // is_unknown_id there). A trained `unknown_5` is indistinguishable from a
  // generated one by construction, so an unrelated rejected OFF of about the
  // right size silently retires it.
  //
  // `unknown_device` is a NAME, not a generated id, and stays admissible --
  // which is exactly the distinction is_unknown_id already draws.
  if (std::strncmp(label, kReservedPrefix, kReservedPrefixLength) == 0) {
    const char* suffix = label + kReservedPrefixLength;
    bool generated_shape = *suffix != '\0';
    for (const char* c = suffix; *c != '\0'; ++c) {
      if (*c < '0' || *c > '9') generated_shape = false;
    }
    if (generated_shape) return false;
  }
  return true;
}

bool MemoryFingerprintSource::load(FingerprintTable& out) {
  out = table_;
  return true;
}

bool FileFingerprintSource::load(FingerprintTable& out) {
  std::ifstream stream(path_);
  if (!stream.is_open()) return false;

  std::string line;
  if (!std::getline(stream, line)) return false;  // header

  out.count = 0;
  for (int i = 0; i < kFeatureCount; ++i) out.active[i] = true;

  while (std::getline(stream, line)) {
    if (line.empty()) continue;

    // MORE DATA LINES THAN THE TABLE HOLDS IS A FAILURE, NOT A SMALLER TABLE.
    //
    // Loading 256 rows of a 300-row file and returning true drops rows 256 to
    // 299 with no diagnostic anywhere -- and those are the rows most recently
    // APPENDED, i.e. exactly the appliance somebody has just trained and is
    // about to test. The wizard would report a trained table and the device
    // would classify from one nobody wrote. Same doctrine as the emitter's: a
    // truncated table is not a smaller table, it is a different one.
    //
    // Checked before the reference below is taken, so it is also what keeps
    // that reference in bounds. A file whose surplus lines would all have been
    // dropped as malformed is refused too -- there is no way to know that
    // without parsing them into a row that does not exist, and refusing is the
    // safe direction.
    if (out.count >= FingerprintTable::kMaxRows) return false;

    std::istringstream row(line);
    std::string field;
    FingerprintRow& target = out.rows[out.count];

    std::getline(row, field, ',');
    std::snprintf(target.training_id, sizeof(target.training_id), "%s",
                  field.c_str());

    // THE LABEL IS VALIDATED HERE, ONCE, FOR EVERY FIELD IT LATER REACHES.
    //
    // A label is copied verbatim onto Classification::label, then onto
    // ActiveEntry::id by Tracker::add, then spliced into telemetry's
    // `attribution.active[].id` -- a field the contract restricts to
    // `^([a-z][a-z0-9_]*|unknown_[0-9]+)$`. The server validates the WHOLE
    // frame before reading any of it, so one non-conforming label does not
    // cost one event: it drops every 1 Hz telemetry frame for as long as that
    // appliance is on, taking every other appliance's watts and the residual
    // out of the energy ledger with it. The event stream survives, because
    // `label` there is unpatterned -- so the device looks healthy while the
    // ledger gains a hole.
    //
    // Skipped exactly like a malformed numeric field: one bad row costs one
    // row. The length test is part of the same guarantee -- a 30-character
    // label silently truncated to 23 is a DIFFERENT id from the one in the
    // file, which is the same class of silent substitution.
    std::getline(row, field, ',');
    if (!is_emittable_label(field.c_str()) ||
        field.size() >= sizeof(target.label)) {
      continue;
    }
    std::snprintf(target.label, sizeof(target.label), "%s", field.c_str());
    std::getline(row, field, ',');  // edge - read and discarded on purpose

    bool complete = true;
    for (int i = 0; i < kFeatureCount; ++i) {
      if (!std::getline(row, field, ',')) {
        complete = false;
        break;
      }
      // Treat malformed data the same as a missing or short row -- the
      // contract file_sample_source.cpp already holds, and US58's whole
      // premise is that this file gets hand-corrected, so one typo must cost
      // one row rather than the whole table.
      //
      // Reached without exceptions, which is where the mechanism differs
      // from the house precedent's try/catch around std::stoi. std::stod
      // would throw here, and on the S6 target, where the Arduino/ESP-IDF
      // default is -fno-exceptions, it aborts instead -- a typo in a
      // hand-edited file would become a reboot loop, and a try/catch would
      // not even compile there to prevent it. std::strtod reports failure
      // in band and behaves identically in both builds.
      const char* start = field.c_str();
      char* end = nullptr;
      const double value = std::strtod(start, &end);
      if (end == start || !std::isfinite(value)) {
        complete = false;
        break;
      }
      target.features[i] = value;
    }
    if (complete) ++out.count;

    // Anything past the fourteenth feature is left in the stream unread:
    // the seven capture-condition columns the S8 spec puts at the end of
    // fingerprints.csv are training metadata, not signal.
  }

  compute_normalisation(out);
  out.rejection_threshold = derive_threshold(out);
  compute_fingerprint_id(out);

  // A non-positive threshold rejects every event there is, and fails
  // fingerprints.schema.json's `exclusiveMinimum: 0` besides. derive_threshold
  // falls back to the between-class separation when the within-class spread
  // carries no information, so the only tables that land here are ones with a
  // single class and no spread at all -- which cannot classify anything. Say
  // so, rather than load a table that silently refuses everything.
  return out.count > 0 && out.rejection_threshold > 0.0;
}

void compute_normalisation(FingerprintTable& table) {
  if (table.count == 0) return;
  const double n = static_cast<double>(table.count);

  for (int i = 0; i < kFeatureCount; ++i) {
    double sum = 0.0;
    for (int r = 0; r < table.count; ++r) sum += table.rows[r].features[i];
    table.mean[i] = sum / n;

    double variance = 0.0;
    for (int r = 0; r < table.count; ++r) {
      const double d = table.rows[r].features[i] - table.mean[i];
      variance += d * d;
    }
    table.stddev[i] = std::sqrt(variance / n);
  }
}

double derive_threshold(const FingerprintTable& table) {
  if (table.count == 0) return 0.0;

  // STL containers are fine here and nowhere near classify(): deriving the
  // threshold happens once, when a table is loaded, never per event.
  //
  // Class centroids.
  std::vector<std::string> labels;
  std::vector<FeatureVector> centroids;
  std::vector<int> counts;

  for (int r = 0; r < table.count; ++r) {
    const std::string label = table.rows[r].label;
    auto it = std::find(labels.begin(), labels.end(), label);
    if (it == labels.end()) {
      labels.push_back(label);
      centroids.push_back(FeatureVector{});
      counts.push_back(0);
      it = labels.end() - 1;
    }
    const auto index = static_cast<std::size_t>(it - labels.begin());
    for (int i = 0; i < kFeatureCount; ++i) {
      centroids[index][i] += table.rows[r].features[i];
    }
    ++counts[index];
  }

  for (std::size_t c = 0; c < centroids.size(); ++c) {
    for (int i = 0; i < kFeatureCount; ++i) {
      centroids[c][i] /= static_cast<double>(counts[c]);
    }
  }

  // Within-class centroid distances.
  std::vector<double> distances;
  distances.reserve(static_cast<std::size_t>(table.count));
  for (int r = 0; r < table.count; ++r) {
    const std::string label = table.rows[r].label;
    const auto it = std::find(labels.begin(), labels.end(), label);
    const auto index = static_cast<std::size_t>(it - labels.begin());
    distances.push_back(
        normalised_distance(table.rows[r].features, centroids[index], table));
  }

  std::sort(distances.begin(), distances.end());
  const auto rank = static_cast<std::size_t>(
      std::ceil(0.95 * static_cast<double>(distances.size())) - 1);
  const double p95 = distances[std::min(rank, distances.size() - 1)];

  // 95th percentile of within-class centroid distance, times 1.5.
  if (p95 > 0.0) return p95 * 1.5;

  // DEGENERATE TABLE: no within-class spread to measure.
  //
  // With one row per class every row IS its own class centroid, so every
  // within-class distance is exactly zero and so is the percentile. Returned
  // as-is that is a threshold which rejects everything -- including a probe
  // one watt from a training row -- silently, and it fails
  // fingerprints.schema.json's `exclusiveMinimum: 0` as well. It is also
  // precisely the US58 bringup case: hand-write two rows and see it work.
  //
  // So fall back to the one thing such a table still measures: HALF the
  // smallest distance between two class centroids. An event is then accepted
  // while it is nearer its winning class than the midpoint to the next one,
  // which is the most a table with no within-class scatter can honestly claim
  // -- and it is still DERIVED from the training data, never hardcoded.
  //
  // A single-class table has neither quantity and gets 0.0. The sources refuse
  // to load it rather than present a table that cannot classify.
  double closest = 0.0;
  bool have_pair = false;
  for (std::size_t a = 0; a + 1 < centroids.size(); ++a) {
    for (std::size_t b = a + 1; b < centroids.size(); ++b) {
      const double separation =
          normalised_distance(centroids[a], centroids[b], table);
      if (!have_pair || separation < closest) {
        closest = separation;
        have_pair = true;
      }
    }
  }
  return closest * 0.5;
}

void compute_fingerprint_id(FingerprintTable& table) {
  // FNV-1a over each row's training id, label AND feature values. Not
  // cryptographic; it only needs to change when the table changes.
  //
  // The two strings are in the hash deliberately. A relabel-only correction
  // -- "row 12 was the fan, not the lamp" -- changes no feature value at
  // all, so a features-only hash reports the corrected table and the stale
  // one as the same table: the wizard confirms a push that never landed and
  // the device keeps classifying from the old labels. That silent failure is
  // the exact one this id exists to make visible.
  //
  // Raw bytes for the features, so +0.0 and -0.0 hash apart despite
  // comparing equal. That is the safe direction to be wrong in: a spurious
  // id change costs one redundant push, a missed one costs a stale table
  // nobody can see.
  uint64_t hash = 1469598103934665603ULL;

  const auto absorb = [&hash](const unsigned char* bytes, std::size_t size) {
    for (std::size_t b = 0; b < size; ++b) {
      hash ^= bytes[b];
      hash *= 1099511628211ULL;
    }
  };
  const auto absorb_text = [&absorb](const char* text, std::size_t capacity) {
    std::size_t length = 0;
    while (length < capacity && text[length] != '\0') ++length;
    absorb(reinterpret_cast<const unsigned char*>(text), length);
    // A field separator, so ("ab", "c") and ("a", "bc") cannot collide.
    const unsigned char kSeparator = 0xff;
    absorb(&kSeparator, 1);
  };

  for (int r = 0; r < table.count; ++r) {
    absorb_text(table.rows[r].training_id, sizeof(table.rows[r].training_id));
    absorb_text(table.rows[r].label, sizeof(table.rows[r].label));
    absorb(reinterpret_cast<const unsigned char*>(&table.rows[r].features),
           sizeof(FeatureVector));
  }
  std::snprintf(table.fingerprint_id, sizeof(table.fingerprint_id), "%08x",
                static_cast<unsigned>(hash & 0xffffffffULL));
}
