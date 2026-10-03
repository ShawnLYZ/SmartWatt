#include "detector.h"

#include <cmath>

EventDetector::EventDetector(DetectorConfig config) : config_(config) {}

void EventDetector::reset() {
  have_baseline_ = false;
  baseline_ = 0.0;
  in_transient_ = false;
  transient_cycles_ = 0;
  run_length_ = 0;
  candidate_level_ = 0.0;
  peak_during_transient_ = 0.0;
  edges_in_window_ = 0;
}

bool EventDetector::push(const CycleMetrics& cycle, DetectedEvent& out) {
  if (!cycle.valid) return false;

  if (!have_baseline_) {
    // First observation SETS. It does not blend toward a default.
    have_baseline_ = true;
    baseline_ = cycle.p;
    baseline_cycle_ = cycle;
    return false;
  }

  if (!in_transient_) {
    if (std::fabs(cycle.p - baseline_) > config_.threshold_w) {
      in_transient_ = true;
      transient_cycles_ = 0;
      run_length_ = 0;
      edges_in_window_ = 1;
      before_ = baseline_cycle_;
      candidate_level_ = cycle.p;
      peak_during_transient_ = std::fabs(cycle.p - baseline_);
      return false;
    }
    // Slow drift tracking while quiet.
    baseline_ = 0.98 * baseline_ + 0.02 * cycle.p;
    baseline_cycle_ = cycle;
    return false;
  }

  return settle_step(cycle, out);
}

bool EventDetector::settle_step(const CycleMetrics& cycle,
                                DetectedEvent& out) {
  ++transient_cycles_;

  const double excursion = std::fabs(cycle.p - baseline_);
  if (excursion > peak_during_transient_) peak_during_transient_ = excursion;

  const double band = std::fabs(candidate_level_) * config_.settle_band;
  const double tolerance = (band > 1.0) ? band : 1.0;

  if (std::fabs(cycle.p - candidate_level_) <= tolerance) {
    ++run_length_;
  } else {
    // A step counts as a SECOND edge only if the level it left had been held
    // for at least one cycle. A single-sample inrush peak is not another
    // appliance switching on, and treating it as one would route almost every
    // real turn-on to OverlappingEdges and discard its energy to the residual.
    if (run_length_ > 0 &&
        std::fabs(cycle.p - candidate_level_) > config_.threshold_w) {
      ++edges_in_window_;
    }
    run_length_ = 0;
    candidate_level_ = cycle.p;
  }

  const bool settled = run_length_ >= config_.settle_run;
  const bool timed_out = transient_cycles_ >= config_.settle_timeout;
  if (!settled && !timed_out) return false;

  const double delta = candidate_level_ - before_.p;

  out.before = before_;
  out.after = cycle;
  out.edge = (delta >= 0.0) ? Edge::On : Edge::Off;
  // Emit the delta the edge and the floor test were decided from. Deriving
  // it downstream as after.p - before.p is a different number: `after` is
  // whichever cycle the run happened to end on, not the settled level.
  out.delta_p = delta;
  out.settle_cycles = transient_cycles_;
  out.inrush_ratio =
      (std::fabs(delta) > 0.0) ? peak_during_transient_ / std::fabs(delta) : 1.0;
  out.valid = true;

  if (edges_in_window_ > 1) {
    out.flag = EventFlag::OverlappingEdges;
  } else if (!settled) {
    out.flag = EventFlag::NoSettle;
  } else if (std::fabs(delta) < config_.floor_w) {
    out.flag = EventFlag::BelowFloor;
  } else {
    out.flag = EventFlag::Clean;
  }

  baseline_ = cycle.p;
  baseline_cycle_ = cycle;
  in_transient_ = false;
  transient_cycles_ = 0;
  run_length_ = 0;
  edges_in_window_ = 0;
  return true;
}
