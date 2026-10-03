#pragma once

#include "detector.h"
#include "knn.h"

struct ActiveEntry {
  char id[24] = {};
  /// The tracker's current estimate of what this load draws, in watts.
  double watts = 0.0;
  /// When it came on THIS time. Per activation, not per first sighting.
  ///
  /// FLOAT EPOCH SECONDS UTC -- whatever `now` was on the apply() that
  /// activated it. The unit is not free: payload.cpp emits this straight into
  /// telemetry's `attribution.active[].since`, which the contract describes as
  /// "float epoch seconds UTC", and it must be the same clock the frame's own
  /// `ts` comes from or the two disagree about when the load started.
  double since = 0.0;
};

/// A remembered watts estimate for one id.
///
/// Deliberately NOT part of `ActiveEntry`: the estimate is a property of the
/// appliance and outlives any single activation, whereas an `ActiveEntry`
/// exists only while the appliance is believed to be on. Keeping the estimate
/// in the active set would destroy it on every OFF, so every ON would be a
/// first observation and the blend would never happen -- which looks correct
/// in the active set and is invisible in aggregate figures.
struct KnownLoad {
  char id[24] = {};
  double watts = 0.0;
};

struct Attribution {
  /// What the classifier said. Empty when rejected.
  char label[24] = {};
  /// What the tracker did after its state-consistency filter. Empty when
  /// nothing could be attributed.
  char attributed_to[24] = {};
  bool rejected = false;
  bool ambiguous = false;
  /// Matches the contract's reason enum, or nullptr.
  const char* reason = nullptr;
};

/// Maintains the active-appliance set and the residual.
class Tracker {
 public:
  static constexpr int kMaxActive = 16;

  /// Capacity of the remembered-estimate table. Larger than kMaxActive
  /// because it holds every NAMED id seen since the last reset, not just the
  /// ones currently on.
  ///
  /// `unknown_N` ids are excluded on purpose and never occupy a slot -- see
  /// the note in Tracker::estimate(). When the table is full a new id's
  /// estimate is SET and not remembered: the alternative, evicting somebody
  /// else's accumulated estimate, silently degrades a figure that no caller
  /// can see is stale.
  static constexpr int kMaxKnown = 32;

  void reset();

  /// Applies one settled event. Returns what was recorded, including both
  /// the classifier's label and the tracker's final attribution.
  ///
  /// `now` is FLOAT EPOCH SECONDS UTC: it is stored verbatim as
  /// ActiveEntry::since and emitted into a contract field described as
  /// "float epoch seconds UTC".
  ///
  /// LEAVES THE RESIDUAL STALE. apply() changes the active set and therefore
  /// changes what the residual should be, but it has no measured total to
  /// recompute it from -- only observe_total() does. See observe_total().
  Attribution apply(const DetectedEvent& event,
                    const Classification& classification, double now);

  /// Feeds the measured total so the residual can be computed continuously.
  ///
  /// MUST BE CALLED EVERY CYCLE, AND BEFORE THAT CYCLE IS EMITTED. It is the
  /// only thing that recomputes the residual, and payload.cpp reads the active
  /// set live while reading residual() from this cached value: a frame emitted
  /// after an apply() but before the next observe_total() carries a live active
  /// set beside a residual computed from a different one. Measured: an active
  /// kettle at 1700 W with `electrical.p` 1000.0 emitted `residual_w: 0.0000`
  /// where the true residual was -700 W -- the exact figure non-negotiable #2
  /// exists to keep visible, reading zero.
  ///
  /// The sequencing belongs to whoever wires the chain (S6), because this
  /// class cannot enforce it: `p` arrives from the cycle stream, not from an
  /// event. `now` is float epoch seconds UTC, as on apply().
  void observe_total(double p, double now);

  /// Total measured power minus the sum of attributed power.
  /// MAY BE NEGATIVE. Never clamped. Non-negotiable #2.
  ///
  /// AS OF THE LAST observe_total(), not as of the last apply(). Stale between
  /// the two -- see observe_total().
  double residual() const { return residual_; }

  int active_count() const { return active_count_; }
  const ActiveEntry* active() const { return active_; }

 private:
  int find(const char* id) const;
  int find_known(const char* id) const;
  /// Folds one observation into the remembered estimate and returns it.
  double estimate(const char* id, double observed);
  int add(const char* id, double watts, double now);
  void remove(int index);
  int nearest_unknown(double delta) const;

  ActiveEntry active_[kMaxActive];
  int active_count_ = 0;

  KnownLoad known_[kMaxKnown];
  int known_count_ = 0;

  int next_unknown_ = 1;
  double residual_ = 0.0;
};
