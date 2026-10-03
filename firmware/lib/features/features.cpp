#include "features.h"

#include <cmath>

const char* const kFeatureNames[kFeatureCount] = {
    "delta_p",      "delta_q1",     "delta_dist",   "delta_s",
    "pf_disp",      "pf_true",      "delta_irms",   "delta_crest",
    "h3_h1",        "h5_h1",        "h7_h1",        "inrush_ratio",
    "settle_cycles", "log_delta_p",
};

FeatureVector extract(const DetectedEvent& event) {
  const CycleMetrics& before = event.before;
  const CycleMetrics& after = event.after;

  // event.delta_p is the settled step `edge` and the BelowFloor test were
  // actually judged against (see the doc comment on DetectedEvent::delta_p
  // in detector.h). It is NOT in general after.p - before.p: `after` is
  // whichever cycle the settle run happened to end on, so on a noisy stream
  // the two can differ by the whole step and even disagree in sign. Take it
  // from the event so this feature reports the number the decision was
  // actually made from, rather than a different one recomputed here.
  const double delta_p = std::fabs(event.delta_p);

  // S5a carries no settled counterpart for these five, so -- unlike
  // delta_p above -- they come straight from the before/after cycle
  // metrics. The mix of event-sourced delta_p and cycle-sourced deltas
  // below is deliberate, not an oversight.
  const double delta_q1 = std::fabs(after.q1 - before.q1);
  const double delta_dist = std::fabs(after.dist - before.dist);
  const double delta_s = std::fabs(after.s - before.s);
  const double delta_irms = std::fabs(after.irms - before.irms);
  const double delta_crest = std::fabs(after.crest - before.crest);

  const double fundamental_va = std::hypot(delta_p, delta_q1);

  // The switched load's harmonic content is whichever state actually has
  // it: on an OFF edge the "after" state is quiet, so take the busier one.
  const bool after_is_busier = std::fabs(after.p) >= std::fabs(before.p);
  const CycleMetrics& busier = after_is_busier ? after : before;

  FeatureVector f;
  f[kDeltaP] = delta_p;
  f[kDeltaQ1] = delta_q1;
  f[kDeltaDist] = delta_dist;
  f[kDeltaS] = delta_s;
  f[kPfDisp] = (fundamental_va > 0.0) ? delta_p / fundamental_va : 1.0;
  f[kPfTrue] = (delta_s > 0.0) ? delta_p / delta_s : 1.0;
  f[kDeltaIrms] = delta_irms;
  f[kDeltaCrest] = delta_crest;
  f[kH3H1] = busier.h3_h1;
  f[kH5H1] = busier.h5_h1;
  f[kH7H1] = busier.h7_h1;
  f[kInrushRatio] = event.inrush_ratio;
  f[kSettleCycles] = static_cast<double>(event.settle_cycles);
  f[kLogDeltaP] = std::log1p(delta_p);

  if (f[kPfDisp] > 1.0) f[kPfDisp] = 1.0;
  if (f[kPfTrue] > 1.0) f[kPfTrue] = 1.0;

  return f;
}
