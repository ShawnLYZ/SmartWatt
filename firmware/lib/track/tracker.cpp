#include "tracker.h"

#include <cmath>
#include <cstdio>
#include <cstring>

namespace {

/// Weight given to a new observation once an appliance has been seen before.
constexpr double kBlend = 0.3;

/// Fractional tolerance when pairing an OFF edge with an active unknown.
constexpr double kUnknownPairTolerance = 0.25;

/// Prefix of every generated id for an unrecognised load.
constexpr char kUnknownPrefix[] = "unknown_";
constexpr int kUnknownPrefixLength = sizeof(kUnknownPrefix) - 1;

/// True only for ids this tracker GENERATED: the prefix, then at least one
/// digit, then nothing else.
///
/// Not a bare prefix test. R11 gave `unknown_` a second job -- it now decides
/// both which ids are denied a persistent estimate and which are eligible for
/// delta-pairing on an OFF -- and the contract's id pattern
/// `^([a-z][a-z0-9_]*|unknown_[0-9]+)$` happily admits a NAMED label such as
/// `unknown_device`. Matching that on the prefix alone would deny a real
/// appliance its accumulated estimate forever and let an unrelated OFF retire
/// it by delta proximity, both silently.
bool is_unknown_id(const char* id) {
  if (std::strncmp(id, kUnknownPrefix, kUnknownPrefixLength) != 0) return false;
  const char* suffix = id + kUnknownPrefixLength;
  if (*suffix == '\0') return false;  // the bare prefix is not a generated id
  for (const char* c = suffix; *c != '\0'; ++c) {
    if (*c < '0' || *c > '9') return false;
  }
  return true;
}

/// The settled step the DETECTOR judged this event from.
///
/// Not `after.p - before.p`: `after` is whichever cycle the settle run
/// happened to end on, anywhere inside the settle band, so on a noisy stream
/// the two differ by the whole step and can even disagree in sign. S5a carries
/// `delta_p` on the event precisely so this stage reports the number the
/// decision was made from, and so the feature vector and the tracker agree on
/// what the step was.
double delta_of(const DetectedEvent& event) { return std::fabs(event.delta_p); }

}  // namespace

void Tracker::reset() {
  active_count_ = 0;
  // The remembered estimates go too. A reset tracker has seen nothing, so the
  // next observation of any id is a first observation and SETS.
  known_count_ = 0;
  next_unknown_ = 1;
  residual_ = 0.0;
}

int Tracker::find(const char* id) const {
  for (int n = 0; n < active_count_; ++n) {
    if (std::strcmp(active_[n].id, id) == 0) return n;
  }
  return -1;
}

int Tracker::find_known(const char* id) const {
  for (int n = 0; n < known_count_; ++n) {
    if (std::strcmp(known_[n].id, id) == 0) return n;
  }
  return -1;
}

double Tracker::estimate(const char* id, double observed) {
  // R11: unknown_N ids are deliberately NOT remembered. Do not "tidy up"
  // this special case into the uniform path below.
  //
  // `next_unknown_` only ever increments, and an OFF is paired against an
  // ACTIVE unknown, so a given unknown_N receives exactly one ON event in its
  // entire life. A persistent estimate for it could never be read back --
  // there is never a second observation to blend with -- while the slot it
  // occupied would be permanent. Remember enough of them and the table fills
  // with entries that can never be looked up, at which point every REAL
  // appliance falls back to SET on every observation and the blend silently
  // stops. That is the failure test_tracker_first_observation_sets_not_blends
  // exists to prevent, arriving through a side door.
  if (is_unknown_id(id)) return observed;

  const int known = find_known(id);
  if (known >= 0) {
    // Seen before: blend, and blend from the ACCUMULATED value so successive
    // observations actually converge rather than just averaging the last two.
    known_[known].watts =
        (1.0 - kBlend) * known_[known].watts + kBlend * observed;
    return known_[known].watts;
  }

  if (known_count_ < kMaxKnown) {
    KnownLoad& entry = known_[known_count_++];
    std::snprintf(entry.id, sizeof(entry.id), "%s", id);
    // FIRST OBSERVATION SETS. It does not blend toward a default.
    //
    // Blending here would drag every subsequent estimate toward zero in a way
    // that is silent, permanent and invisible in aggregate figures. This is
    // the bug test_tracker_first_observation_sets_not_blends guards.
    entry.watts = observed;
    return entry.watts;
  }

  // Table full: use the observation as-is and do not remember it. See the
  // note on kMaxKnown -- this degrades to "always a first observation" for
  // the overflowing ids rather than corrupting an existing appliance's
  // accumulated estimate.
  return observed;
}

int Tracker::add(const char* id, double watts, double now) {
  const double estimated = estimate(id, watts);

  const int existing = find(id);
  if (existing >= 0) {
    // Already believed on. Refresh the estimate but keep `since`: the
    // appliance did not come on again, it never went off.
    active_[existing].watts = estimated;
    return existing;
  }

  if (active_count_ >= kMaxActive) return -1;

  ActiveEntry& entry = active_[active_count_];
  std::snprintf(entry.id, sizeof(entry.id), "%s", id);
  entry.watts = estimated;
  entry.since = now;
  return active_count_++;
}

void Tracker::remove(int index) {
  if (index < 0 || index >= active_count_) return;
  for (int n = index; n < active_count_ - 1; ++n) active_[n] = active_[n + 1];
  --active_count_;
}

int Tracker::nearest_unknown(double delta) const {
  int best = -1;
  // Seeded with the tolerance, so this is a bounded search and not a bare
  // argmin: an OFF that resembles no active unknown pairs with none of them.
  double best_error = kUnknownPairTolerance;
  for (int n = 0; n < active_count_; ++n) {
    if (!is_unknown_id(active_[n].id)) continue;
    if (active_[n].watts <= 0.0) continue;
    const double error = std::fabs(active_[n].watts - delta) / active_[n].watts;
    if (error < best_error) {
      best_error = error;
      best = n;
    }
  }
  return best;
}

Attribution Tracker::apply(const DetectedEvent& event,
                           const Classification& classification, double now) {
  Attribution out;
  std::snprintf(out.label, sizeof(out.label), "%s", classification.label);
  out.rejected = classification.rejected;

  // Ambiguous events change the active set not at all. Their energy goes to
  // the residual, which is what makes US18 honest.
  if (event.flag == EventFlag::OverlappingEdges) {
    out.ambiguous = true;
    out.reason = "overlapping_edges";
    return out;
  }
  if (event.flag == EventFlag::NoSettle) {
    out.ambiguous = true;
    out.reason = "no_settle";
    return out;
  }
  if (event.flag == EventFlag::BelowFloor) {
    out.rejected = true;
    out.reason = "below_floor";
    out.label[0] = '\0';
    return out;
  }

  const double delta = delta_of(event);

  if (classification.rejected) {
    out.reason = "distance_threshold";
    out.label[0] = '\0';

    if (event.edge == Edge::On) {
      // A cleanly settled but unrecognised load. It gets a generated id and
      // its energy is still counted: refusing to name something never means
      // losing track of it. The id matches the contract's pattern
      // `unknown_[0-9]+`, which Task 4 emits into schema-validated JSON.
      char id[24];
      std::snprintf(id, sizeof(id), "unknown_%d", next_unknown_);
      if (add(id, delta, now) >= 0) {
        // The number is burned only when the load is actually tracked, so
        // unknown numbering records loads the tracker holds rather than
        // attempts it made. A failed add leaves the id unused, so nothing
        // can collide with it later.
        ++next_unknown_;
        std::snprintf(out.attributed_to, sizeof(out.attributed_to), "%s", id);
      }
      // R12: if the active set was full the load is NOT tracked. Its watts
      // are in no active entry, so the residual absorbs them -- and the
      // payload must not claim an attribution that did not happen, or the
      // under-attribution is papered over exactly where #2 says it must
      // stay visible.
    } else {
      const int index = nearest_unknown(delta);
      if (index >= 0) {
        std::snprintf(out.attributed_to, sizeof(out.attributed_to), "%s",
                      active_[index].id);
        remove(index);
      }
    }
    return out;
  }

  if (event.edge == Edge::On) {
    // Same rule as the rejected-ON path above and as an OFF with no active
    // candidate: claim nothing the tracker could not do (R12).
    if (add(classification.label, delta, now) >= 0) {
      std::snprintf(out.attributed_to, sizeof(out.attributed_to), "%s",
                    classification.label);
    }
    return out;
  }

  // OFF: the STATE-CONSISTENCY FILTER. An OFF event can only be attributed
  // to an appliance currently believed to be on (US17).
  int index = find(classification.label);
  if (index < 0) {
    // Reassign to the highest-ranked candidate in the neighbour list that
    // IS on -- the list is already in rank order, so the first hit wins.
    // `label` keeps the classifier's answer so the filter leaves evidence
    // it fired.
    // Bounded by the count the classifier published AND by the array extent,
    // so a caller that hand-builds a Classification with a wrong count reads
    // no further than the slots that exist.
    for (int n = 0; n < classification.neighbour_count && n < Knn::kK; ++n) {
      const Neighbour& neighbour = classification.neighbours[n];
      // Belt and braces behind the count. Active ids are never empty, so an
      // empty label could not match anyway -- but say so here rather than
      // lean on that invariant from a distance.
      if (neighbour.label[0] == '\0') continue;
      const int candidate = find(neighbour.label);
      if (candidate >= 0) {
        index = candidate;
        break;
      }
    }
  }

  if (index >= 0) {
    std::snprintf(out.attributed_to, sizeof(out.attributed_to), "%s",
                  active_[index].id);
    remove(index);
  }
  // If nothing is on that could have produced it, it is counted unknown:
  // attributed_to stays empty and the energy falls to the residual.
  return out;
}

void Tracker::observe_total(double p, double /*now*/) {
  double attributed = 0.0;
  for (int n = 0; n < active_count_; ++n) attributed += active_[n].watts;
  // Never clamped. A negative residual means the tracker is over-attributing
  // and a silent zero would hide it. Non-negotiable #2.
  residual_ = p - attributed;
}
