#include <unity.h>

#include <cmath>
#include <string>

#include "detector.h"
#include "features.h"

void setUp() {}
void tearDown() {}

static CycleMetrics state(double p, double q1, double dist, double crest = 1.414,
                          double h3 = 0.0, double h5 = 0.0, double h7 = 0.0) {
  CycleMetrics m;
  m.p = p;
  m.q1 = q1;
  m.dist = dist;
  m.vrms = 240.0;
  m.irms = std::sqrt(p * p + q1 * q1 + dist * dist) / 240.0;
  m.s = 240.0 * m.irms;
  m.crest = crest;
  m.h3_h1 = h3;
  m.h5_h1 = h5;
  m.h7_h1 = h7;
  m.pf_disp = (std::hypot(p, q1) > 0) ? p / std::hypot(p, q1) : 1.0;
  m.pf_true = (m.s > 0) ? p / m.s : 1.0;
  m.valid = true;
  return m;
}

static DetectedEvent make(Edge edge, CycleMetrics before, CycleMetrics after,
                          int settle = 3, double inrush = 1.02) {
  DetectedEvent e;
  e.edge = edge;
  e.flag = EventFlag::Clean;
  e.before = before;
  e.after = after;
  e.settle_cycles = settle;
  e.inrush_ratio = inrush;
  // R1a: DetectedEvent::delta_p is the settled step, not after.p - before.p
  // in general (see detector.h). The fixtures below build before/after as a
  // single clean step, so this is the value the real detector would have
  // carried for them.
  e.delta_p = after.p - before.p;
  e.valid = true;
  return e;
}

static void test_exactly_fourteen() {
  TEST_ASSERT_EQUAL_INT(14, kFeatureCount);
}

static void test_names_match_the_contract() {
  const char* want[] = {"delta_p",      "delta_q1",     "delta_dist",
                        "delta_s",      "pf_disp",      "pf_true",
                        "delta_irms",   "delta_crest",  "h3_h1",
                        "h5_h1",        "h7_h1",        "inrush_ratio",
                        "settle_cycles", "log_delta_p"};
  for (int i = 0; i < kFeatureCount; ++i) {
    TEST_ASSERT_EQUAL_STRING(want[i], kFeatureNames[i]);
  }
}

static void test_on_and_off_share_one_feature_space() {
  // Magnitudes on all deltas is what makes this true, and it is what lets
  // one trained model cover both directions.
  const CycleMetrics quiet = state(0.0, 0.0, 0.0);
  const CycleMetrics loud = state(1800.0, 30.0, 12.0);

  const FeatureVector on = extract(make(Edge::On, quiet, loud));
  const FeatureVector off = extract(make(Edge::Off, loud, quiet));

  for (int i = 0; i < kFeatureCount; ++i) {
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-9, on[i], off[i], kFeatureNames[i]);
  }
}

static void test_every_delta_is_non_negative() {
  const FeatureVector f =
      extract(make(Edge::Off, state(1800.0, 30.0, 12.0), state(0.0, 0.0, 0.0)));
  for (const int i : {kDeltaP, kDeltaQ1, kDeltaDist, kDeltaS, kDeltaIrms,
                      kDeltaCrest}) {
    TEST_ASSERT_TRUE_MESSAGE(f[i] >= 0.0, kFeatureNames[i]);
  }
}

static void test_delta_p_is_the_difference() {
  const FeatureVector f =
      extract(make(Edge::On, state(100.0, 5.0, 2.0), state(1900.0, 35.0, 14.0)));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 1800.0, f[kDeltaP]);
}

static void test_delta_q1_is_the_difference() {
  const FeatureVector f =
      extract(make(Edge::On, state(100.0, 5.0, 2.0), state(1900.0, 35.0, 14.0)));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 30.0, f[kDeltaQ1]);
}

static void test_log_delta_p_is_log1p() {
  const FeatureVector f =
      extract(make(Edge::On, state(0.0, 0.0, 0.0), state(1800.0, 0.0, 0.0)));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, std::log1p(1800.0), f[kLogDeltaP]);
}

static void test_harmonic_ratios_come_from_the_switched_state() {
  const CycleMetrics after = state(9.0, 15.0, 8.0, 2.4, 0.48, 0.29, 0.19);
  const FeatureVector f = extract(make(Edge::On, state(0, 0, 0), after));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.48, f[kH3H1]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.29, f[kH5H1]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.19, f[kH7H1]);
}

static void test_inrush_and_settle_come_from_the_event() {
  const FeatureVector f = extract(
      make(Edge::On, state(0, 0, 0), state(40.0, 0.2, 0.1), 2, 6.5));
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 6.5, f[kInrushRatio]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 2.0, f[kSettleCycles]);
}

static void test_pf_bounds() {
  const FeatureVector f =
      extract(make(Edge::On, state(0, 0, 0), state(9.0, 15.0, 8.0)));
  TEST_ASSERT_TRUE(f[kPfDisp] >= -1.0 && f[kPfDisp] <= 1.0);
  TEST_ASSERT_TRUE(f[kPfTrue] >= -1.0 && f[kPfTrue] <= 1.0);
}

static void test_fan_and_lamp_separate_on_q1_and_inrush() {
  // The project's central discrimination claim, in feature space: two loads
  // at the same wattage, separated by fundamental reactive power and inrush
  // ratio alone.
  const FeatureVector fan = extract(
      make(Edge::On, state(0, 0, 0), state(45.0, 33.0, 2.0), 7, 2.4));
  const FeatureVector lamp = extract(
      make(Edge::On, state(0, 0, 0), state(40.0, 0.16, 0.05), 2, 6.5));

  TEST_ASSERT_TRUE(std::fabs(fan[kDeltaP] - lamp[kDeltaP]) < 10.0);
  TEST_ASSERT_TRUE(fan[kDeltaQ1] > lamp[kDeltaQ1] * 5.0);
  TEST_ASSERT_TRUE(lamp[kInrushRatio] > fan[kInrushRatio]);
}

static void test_zero_delta_does_not_divide_by_zero() {
  const CycleMetrics same = state(500.0, 20.0, 5.0);
  const FeatureVector f = extract(make(Edge::On, same, same));
  for (int i = 0; i < kFeatureCount; ++i) {
    TEST_ASSERT_FALSE_MESSAGE(std::isnan(f[i]), kFeatureNames[i]);
    TEST_ASSERT_FALSE_MESSAGE(std::isinf(f[i]), kFeatureNames[i]);
  }
}

// R1b regression test: DetectedEvent::delta_p is the settled step the
// detector actually judged `edge` and BelowFloor against, and on a noisy
// stream it can differ from after.p - before.p in both magnitude and sign
// (see the doc comment on DetectedEvent::delta_p in detector.h). Built by
// hand, not through make(), so the disagreement is explicit: an Off edge
// whose settled step was a NEGATIVE 1800 W even though the before/after
// cycle snapshots the run happened to end on show a positive 200 W move.
// f[kDeltaP] must come from the event's delta_p, not from recomputing
// after.p - before.p, which would silently give 200.0 here instead of the
// correct 1800.0.
//
// before.q1 != after.q1 (5.0 vs 9.0) on purpose. With delta_q1 == 0, R1's
// pf_disp = delta_p / hypot(delta_p, delta_q1) reduces to delta_p /
// delta_p == 1.0 no matter which delta_p (event-sourced or a reverted
// after.p - before.p) went in, so an all-zero-delta_q1 fixture could never
// catch a reverted, cycle-recomputed denominator. With delta_q1 == 4.0 the
// two candidates give different denominators -- hypot(1800, 4) for the
// correct event-sourced delta_p, hypot(200, 4) for the reverted one --
// so pf_disp comes out ~0.9999975 vs ~0.9998000: distinguishable well
// beyond the 1e-9 tolerance asserted below.
static void test_delta_p_comes_from_the_event_not_the_cycles() {
  const CycleMetrics before = state(100.0, 5.0, 2.0);
  const CycleMetrics after = state(300.0, 9.0, 2.0);

  DetectedEvent e;
  e.edge = Edge::Off;
  e.flag = EventFlag::Clean;
  e.before = before;
  e.after = after;
  e.settle_cycles = 3;
  e.inrush_ratio = 1.02;
  e.delta_p = -1800.0;
  e.valid = true;

  const FeatureVector f = extract(e);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 1800.0, f[kDeltaP]);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, std::log1p(1800.0), f[kLogDeltaP]);

  // pf_disp's denominator, hypot(delta_p, delta_q1), must be built from
  // the event-sourced delta_p (1800.0) asserted above, not a
  // cycle-recomputed |after.p - before.p| (200.0).
  const double delta_q1 = 4.0;  // |after.q1 - before.q1| = |9.0 - 5.0|
  const double expected_pf_disp = 1800.0 / std::hypot(1800.0, delta_q1);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, expected_pf_disp, f[kPfDisp]);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_exactly_fourteen);
  RUN_TEST(test_names_match_the_contract);
  RUN_TEST(test_on_and_off_share_one_feature_space);
  RUN_TEST(test_every_delta_is_non_negative);
  RUN_TEST(test_delta_p_is_the_difference);
  RUN_TEST(test_delta_q1_is_the_difference);
  RUN_TEST(test_log_delta_p_is_log1p);
  RUN_TEST(test_harmonic_ratios_come_from_the_switched_state);
  RUN_TEST(test_inrush_and_settle_come_from_the_event);
  RUN_TEST(test_pf_bounds);
  RUN_TEST(test_fan_and_lamp_separate_on_q1_and_inrush);
  RUN_TEST(test_zero_delta_does_not_divide_by_zero);
  RUN_TEST(test_delta_p_comes_from_the_event_not_the_cycles);
  return UNITY_END();
}
