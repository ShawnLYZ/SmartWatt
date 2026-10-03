#pragma once

#include "cycle.h"

enum class Edge { On, Off };

enum class EventFlag {
  Clean,
  /// Settled delta is under the measured detection floor.
  BelowFloor,
  /// Did not stabilise within the timeout. Energy goes to the residual.
  NoSettle,
  /// A second edge arrived inside the settle window. Both are discarded.
  OverlappingEdges,
};

struct DetectorConfig {
  /// Minimum |dP| between consecutive cycles to open a candidate edge.
  double threshold_w = 8.0;
  /// Measured detection floor. S6 measures this; S5a only carries it.
  double floor_w = 6.0;
  /// Settled means inside this fractional band of the running level.
  double settle_band = 0.05;
  /// ...for this many CONSECUTIVE cycles. Not a first crossing.
  int settle_run = 3;
  /// Give up after this many cycles and flag NoSettle.
  int settle_timeout = 40;
};

struct DetectedEvent {
  Edge edge = Edge::On;
  EventFlag flag = EventFlag::Clean;
  CycleMetrics before;
  CycleMetrics after;
  int settle_cycles = 0;
  double inrush_ratio = 1.0;
  /// The settled step `edge` and the BelowFloor test were judged against:
  /// the level the run stabilised on, minus `before.p`. Signed -- the
  /// contract's `features.delta_p` is its magnitude, with the sign carried
  /// by `edge`.
  ///
  /// This is NOT in general `after.p - before.p`: `after` is whichever cycle
  /// the run happened to end on, anywhere inside the settle band, so on a
  /// noisy stream the two can differ by the whole step and even disagree in
  /// sign. Carried on the event so S5b reports the number the decision was
  /// made from rather than a different one it recomputed.
  double delta_p = 0.0;
  bool valid = false;
};

/// Detects switching edges in a per-cycle power stream.
///
/// A candidate opens when |dP| exceeds the threshold. It becomes a settled
/// event only after power stays inside the band for a SUSTAINED RUN of
/// cycles - not on first crossing, which would fire on inrush transients.
class EventDetector {
 public:
  explicit EventDetector(DetectorConfig config = {});

  void reset();

  /// Feeds one cycle. Returns true when an event is emitted.
  bool push(const CycleMetrics& cycle, DetectedEvent& out);

  double baseline() const { return baseline_; }

 private:
  bool settle_step(const CycleMetrics& cycle, DetectedEvent& out);

  DetectorConfig config_;

  bool have_baseline_ = false;
  double baseline_ = 0.0;
  CycleMetrics baseline_cycle_;

  bool in_transient_ = false;
  int transient_cycles_ = 0;
  int run_length_ = 0;
  double candidate_level_ = 0.0;
  double peak_during_transient_ = 0.0;
  int edges_in_window_ = 0;
  CycleMetrics before_;
};
