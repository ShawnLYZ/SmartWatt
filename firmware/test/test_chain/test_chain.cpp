#include <unity.h>

#include <string>
#include <vector>

#include "cycle.h"
#include "detector.h"
#include "file_sample_source.h"

void setUp() {}
void tearDown() {}

/// The two halves of S5a composed, which is the only way either half's
/// output is observed as the other half's input.
///
/// test_cycle runs fixtures through CycleProcessor and stops there;
/// test_events starts from a synthetic helper that manufactures exact,
/// noiseless CycleMetrics. Neither ever hands a real fixture-derived cycle
/// to the detector, and that gap is what let a 22 W per-cycle ripple on a
/// steady load -- large enough to emit a phantom switching event with
/// shipped defaults -- through five reviews.

static std::string fixture(const std::string& name) {
  return std::string(FIXTURE_DIR) + "/" + name + ".csv";
}

static const char* const ALL_FIXTURES[] = {
    "distorted-v", "freq-high",      "freq-low",   "idle",
    "inductive",   "pure-resistive", "mixed-three", "switch-mode"};

/// Every complete cycle a fixture produces, in order.
static std::vector<CycleMetrics> cycles_of(const char* name) {
  FileSampleSource source(fixture(name).c_str());
  TEST_ASSERT_TRUE_MESSAGE(source.ok(), name);
  CycleProcessor processor;
  CycleMetrics metrics{};
  SampleSet set{};
  std::vector<CycleMetrics> out;
  while (source.next(set)) {
    if (processor.push(set, metrics)) out.push_back(metrics);
  }
  TEST_ASSERT_GREATER_THAN_MESSAGE(1, (int)out.size(), name);
  return out;
}

static void test_steady_load_produces_no_events() {
  // THE regression guard. Every one of these fixtures is a single steady
  // load with nothing switching, so the correct event count is zero --
  // whatever the line frequency happens to be. freq-low and freq-high are
  // the ones that matter: their period is not a whole number of samples, so
  // an implementation that normalises the window by the sample count
  // instead of the period reports a 22 W square wave on a constant 1800 W
  // load. That clears threshold_w (8 W), settles inside the +/-5% band in
  // three cycles, and clears floor_w (6 W), so it emits as a clean Off
  // edge. Nothing else in the suite can see it. pure-resistive is the
  // nominal-frequency control, where the two divisors coincide.
  for (const char* name : {"freq-low", "freq-high", "pure-resistive"}) {
    EventDetector detector;  // SHIPPED defaults, deliberately.
    DetectedEvent event{};
    int events = 0;
    for (const CycleMetrics& cycle : cycles_of(name)) {
      if (detector.push(cycle, event)) ++events;
    }
    TEST_ASSERT_EQUAL_INT_MESSAGE(0, events, name);
  }
}

static void test_every_fixture_cycle_is_valid() {
  // The `valid` gate rejects truncated, doubly-clipped and too-short
  // windows. None of the fixtures is any of those, so every complete cycle
  // of every one of them must pass -- which pins the gate against becoming
  // over-broad and silently swallowing the whole stream.
  for (const char* name : ALL_FIXTURES) {
    for (const CycleMetrics& cycle : cycles_of(name)) {
      TEST_ASSERT_TRUE_MESSAGE(cycle.valid, name);
    }
  }
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_steady_load_produces_no_events);
  RUN_TEST(test_every_fixture_cycle_is_valid);
  return UNITY_END();
}
