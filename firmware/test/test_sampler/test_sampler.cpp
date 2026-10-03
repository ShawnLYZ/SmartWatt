// The pure arithmetic behind isr_overruns: whether a callback tick arrived
// late against the expected 250 us cadence, and by how many periods.
//
// This is the only part of the Sampler that native testing can reach --
// everything else is hardware-bound (esp_timer, SPI) and lives behind
// `#if SMARTWATT_NATIVE` as an untestable stub. periods_late() carries the
// real timing decision out of that stub, the same way encode_mcp3208_command()
// carries the SPI framing decision out of AdcSampleSource: getting it wrong
// on the bench looks exactly like a wiring fault, and here it would look
// like a clean "0 overruns" pass that proves nothing.

#include <unity.h>

#include "sampler.h"

void setUp() {}
void tearDown() {}

static void test_first_tick_never_counts() {
  // No previous entry exists yet -- there is nothing to compare against,
  // so this can never be reported as a slip, whatever the timestamp is.
  TEST_ASSERT_EQUAL_UINT32(
      0, periods_late(kNoPreviousTick, 999999, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_on_time_tick_does_not_count() {
  const int64_t prev = 1000;
  const int64_t now = prev + Sampler::kPeriodMicros;  // exactly one period
  TEST_ASSERT_EQUAL_UINT32(
      0, periods_late(prev, now, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_tick_inside_tolerance_does_not_count() {
  const int64_t prev = 1000;
  // 20 us over the period -- well inside the 50 us tolerance.
  const int64_t now = prev + Sampler::kPeriodMicros + 20;
  TEST_ASSERT_EQUAL_UINT32(
      0, periods_late(prev, now, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_tick_exactly_at_tolerance_boundary_does_not_count() {
  const int64_t prev = 1000;
  const int64_t now =
      prev + Sampler::kPeriodMicros + Sampler::kLateToleranceMicros;
  TEST_ASSERT_EQUAL_UINT32(
      0, periods_late(prev, now, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_tick_one_microsecond_past_tolerance_counts() {
  const int64_t prev = 1000;
  const int64_t now =
      prev + Sampler::kPeriodMicros + Sampler::kLateToleranceMicros + 1;
  TEST_ASSERT_EQUAL_UINT32(
      1, periods_late(prev, now, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_tick_one_period_late_counts() {
  const int64_t prev = 1000;
  // A whole extra period elapsed: this tick missed its own slot entirely.
  const int64_t now = prev + 2 * Sampler::kPeriodMicros;
  TEST_ASSERT_EQUAL_UINT32(
      1, periods_late(prev, now, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_tick_two_periods_late_counts_as_two() {
  const int64_t prev = 1000;
  const int64_t now = prev + 3 * Sampler::kPeriodMicros;
  TEST_ASSERT_EQUAL_UINT32(
      2, periods_late(prev, now, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_nonadvancing_clock_does_not_count() {
  // Defensive: a non-positive interval (a stalled or non-monotonic clock)
  // is not a schedule slip to report, whatever else it might be.
  const int64_t prev = 5000;
  TEST_ASSERT_EQUAL_UINT32(
      0, periods_late(prev, prev, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
  TEST_ASSERT_EQUAL_UINT32(
      0, periods_late(prev, prev - 10, Sampler::kPeriodMicros,
                      Sampler::kLateToleranceMicros));
}

static void test_zero_tolerance_flags_any_overshoot() {
  // With tolerance stripped to zero, even a single microsecond over the
  // period must count -- confirms the tolerance parameter, and not some
  // other hidden slack, is what absorbs jitter.
  TEST_ASSERT_EQUAL_UINT32(0, periods_late(0, 250, 250, 0));
  TEST_ASSERT_EQUAL_UINT32(1, periods_late(0, 251, 250, 0));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_first_tick_never_counts);
  RUN_TEST(test_on_time_tick_does_not_count);
  RUN_TEST(test_tick_inside_tolerance_does_not_count);
  RUN_TEST(test_tick_exactly_at_tolerance_boundary_does_not_count);
  RUN_TEST(test_tick_one_microsecond_past_tolerance_counts);
  RUN_TEST(test_tick_one_period_late_counts);
  RUN_TEST(test_tick_two_periods_late_counts_as_two);
  RUN_TEST(test_nonadvancing_clock_does_not_count);
  RUN_TEST(test_zero_tolerance_flags_any_overshoot);
  return UNITY_END();
}
