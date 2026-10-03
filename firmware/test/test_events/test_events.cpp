#include <unity.h>

#include <vector>

#include "detector.h"

void setUp() {}
void tearDown() {}

static CycleMetrics cycle(double p, double q1 = 0.0, double dist = 0.0) {
  CycleMetrics m;
  m.p = p;
  m.q1 = q1;
  m.dist = dist;
  m.s = p;
  m.vrms = 240.0;
  m.irms = p / 240.0;
  m.freq = 50.0;
  m.samples = 80;
  m.valid = true;
  return m;
}

/// Feeds a power profile and collects every event the detector emits.
static std::vector<DetectedEvent> run(const std::vector<double>& profile,
                                      DetectorConfig config = {}) {
  EventDetector detector(config);
  std::vector<DetectedEvent> events;
  DetectedEvent event{};
  for (const double p : profile) {
    if (detector.push(cycle(p), event)) events.push_back(event);
  }
  return events;
}

static std::vector<double> hold(double value, int cycles) {
  return std::vector<double>(static_cast<size_t>(cycles), value);
}

static std::vector<double> concat(std::vector<std::vector<double>> parts) {
  std::vector<double> out;
  for (auto& part : parts) out.insert(out.end(), part.begin(), part.end());
  return out;
}

static void test_a_clean_on_edge_produces_exactly_one_event() {
  const auto events = run(concat({hold(0.0, 20), hold(1800.0, 20)}));
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_TRUE(events[0].edge == Edge::On);
  TEST_ASSERT_TRUE(events[0].flag == EventFlag::Clean);
}

static void test_a_clean_off_edge_produces_exactly_one_event() {
  const auto events =
      run(concat({hold(1800.0, 20), hold(0.0, 20)}));
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_TRUE(events[0].edge == Edge::Off);
}

static void test_on_then_off_produces_two_events_with_correct_signs() {
  const auto events =
      run(concat({hold(0.0, 20), hold(45.0, 20), hold(0.0, 20)}));
  TEST_ASSERT_EQUAL_INT(2, (int)events.size());
  TEST_ASSERT_TRUE(events[0].edge == Edge::On);
  TEST_ASSERT_TRUE(events[1].edge == Edge::Off);
}

static void test_steady_state_produces_no_events() {
  TEST_ASSERT_EQUAL_INT(0, (int)run(hold(500.0, 200)).size());
}

static void test_settle_requires_a_sustained_run_not_a_first_crossing() {
  // Touches the eventual level, leaves it, and only later holds it. The
  // wander is wider than the +/-5% band but smaller than threshold_w, so it
  // stays ONE edge that refuses to settle rather than becoming a second one.
  DetectorConfig config;
  config.settle_run = 5;
  std::vector<double> profile = hold(0.0, 20);
  profile.push_back(100.0);  // the eventual level, touched once
  profile.push_back(106.0);
  profile.push_back(100.0);
  profile.push_back(106.0);
  for (int n = 0; n < 20; ++n) profile.push_back(100.0);

  const auto events = run(profile, config);
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_TRUE(events[0].flag == EventFlag::Clean);
  // Four unsettled cycles, then a run of five. A first-crossing
  // implementation emits at 5; only a sustained run reaches 9.
  TEST_ASSERT_EQUAL_INT(9, events[0].settle_cycles);
}

static void test_failure_to_settle_within_timeout_is_flagged() {
  // Never stabilises: a small self-cycling load looks like this. The wander
  // sits outside the +/-5% band but under threshold_w, so it stays one edge
  // and the timeout is what ends it.
  DetectorConfig config;
  config.settle_timeout = 10;
  config.threshold_w = 20.0;
  std::vector<double> profile = hold(0.0, 20);
  for (int n = 0; n < 40; ++n) profile.push_back(n % 2 ? 110.0 : 100.0);

  const auto events = run(profile, config);
  TEST_ASSERT_GREATER_OR_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_TRUE(events[0].flag == EventFlag::NoSettle);
}

static void test_two_edges_in_one_settle_window_are_ambiguous() {
  DetectorConfig config;
  config.settle_run = 6;
  std::vector<double> profile = hold(0.0, 20);
  profile.push_back(45.0);   // first edge
  profile.push_back(45.0);
  profile.push_back(90.0);   // second edge, inside the settle window
  for (int n = 0; n < 20; ++n) profile.push_back(90.0);

  const auto events = run(profile, config);
  TEST_ASSERT_GREATER_OR_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_TRUE(events[0].flag == EventFlag::OverlappingEdges);
}

static void test_a_settled_delta_below_the_floor_is_flagged() {
  DetectorConfig config;
  config.threshold_w = 2.0;
  config.floor_w = 6.0;
  const auto events =
      run(concat({hold(0.0, 20), hold(4.2, 20)}), config);
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_TRUE(events[0].flag == EventFlag::BelowFloor);
}

static void test_noise_under_the_threshold_produces_nothing() {
  std::vector<double> profile;
  for (int n = 0; n < 200; ++n) profile.push_back(500.0 + (n % 3) * 1.5);
  TEST_ASSERT_EQUAL_INT(0, (int)run(profile).size());
}

static void test_before_and_after_are_carried_on_the_event() {
  const auto events = run(concat({hold(100.0, 20), hold(1900.0, 20)}));
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 100.0, events[0].before.p);
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 1900.0, events[0].after.p);
}

static void test_delta_p_is_the_step_the_flags_were_judged_from() {
  // The contract requires delta_p on every event, and it must be the number
  // the edge sign and the floor test were actually decided from -- not
  // after.p - before.p, which is a different quantity that can disagree
  // with the emitted edge on a noisy stream.
  const auto events = run(concat({hold(0.0, 20), hold(1800.0, 20)}));
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 1800.0, events[0].delta_p);
  TEST_ASSERT_TRUE_MESSAGE(events[0].delta_p > 0.0 &&
                               events[0].edge == Edge::On,
                           "a positive delta_p must carry an On edge");
}

static void test_inrush_ratio_is_peak_over_settled() {
  std::vector<double> profile = hold(0.0, 20);
  profile.push_back(260.0);  // inrush peak
  for (int n = 0; n < 20; ++n) profile.push_back(40.0);

  const auto events = run(profile);
  TEST_ASSERT_EQUAL_INT(1, (int)events.size());
  // An inrush peak is one edge, not two: the peak sample is never "held"
  // for a cycle, so it must not trip the overlapping-edge gate.
  TEST_ASSERT_TRUE(events[0].flag == EventFlag::Clean);
  TEST_ASSERT_DOUBLE_WITHIN(0.5, 6.5, events[0].inrush_ratio);
}

static void test_baseline_tracks_the_settled_level() {
  DetectorConfig config;
  EventDetector detector(config);
  DetectedEvent event{};
  for (const double p : concat({hold(0.0, 20), hold(450.0, 30)})) {
    detector.push(cycle(p), event);
  }
  TEST_ASSERT_DOUBLE_WITHIN(5.0, 450.0, detector.baseline());
}

static void test_reset_clears_state() {
  DetectorConfig config;
  EventDetector detector(config);
  DetectedEvent event{};
  for (const double p : hold(1800.0, 20)) detector.push(cycle(p), event);
  detector.reset();
  // Deliberately a different level from the pre-reset hold: if reset() left
  // the stale 1800 W baseline in place, this drop to 0 W would open a
  // transient and eventually emit an event, exposing a no-op reset.
  int events = 0;
  for (const double p : hold(0.0, 20)) {
    if (detector.push(cycle(p), event)) ++events;
  }
  TEST_ASSERT_EQUAL_INT(0, events);
}

static void test_thresholds_are_configurable() {
  // US50: a demonstration can use a short window while the shipped default
  // stays realistic.
  DetectorConfig strict;
  strict.threshold_w = 500.0;
  TEST_ASSERT_EQUAL_INT(
      0, (int)run(concat({hold(0.0, 20), hold(45.0, 20)}), strict).size());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_a_clean_on_edge_produces_exactly_one_event);
  RUN_TEST(test_a_clean_off_edge_produces_exactly_one_event);
  RUN_TEST(test_on_then_off_produces_two_events_with_correct_signs);
  RUN_TEST(test_steady_state_produces_no_events);
  RUN_TEST(test_settle_requires_a_sustained_run_not_a_first_crossing);
  RUN_TEST(test_failure_to_settle_within_timeout_is_flagged);
  RUN_TEST(test_two_edges_in_one_settle_window_are_ambiguous);
  RUN_TEST(test_a_settled_delta_below_the_floor_is_flagged);
  RUN_TEST(test_noise_under_the_threshold_produces_nothing);
  RUN_TEST(test_before_and_after_are_carried_on_the_event);
  RUN_TEST(test_delta_p_is_the_step_the_flags_were_judged_from);
  RUN_TEST(test_inrush_ratio_is_peak_over_settled);
  RUN_TEST(test_baseline_tracks_the_settled_level);
  RUN_TEST(test_reset_clears_state);
  RUN_TEST(test_thresholds_are_configurable);
  return UNITY_END();
}
