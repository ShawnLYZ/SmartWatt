// The pure arithmetic behind the device's clock gate: whether a timestamp is
// a real UTC wall-clock reading or the seconds-since-boot an ESP32 counts
// until SNTP sets its clock.
//
// This is the only part of lib/net that native testing can reach --
// everything else is Wi-Fi and PubSubClient, hardware-bound and stubbed
// behind `#if SMARTWATT_NATIVE`. clock_is_set() carries the timestamp
// decision out of that stub, the same way periods_late() carries the timing
// decision out of Sampler and encode_mcp3208_command() carries the SPI
// framing out of AdcSampleSource: getting it wrong on the bench does not look
// like a bug. It looks like a dashboard that is simply empty, because every
// /api/series and /api/ledger window filters on wall-clock bounds, and like
// device rows that vanished, because the server's retention pass computes
// `time.time() - retention` and every uptime-stamped row is older than any
// such cutoff. The contract types `ts` as a bare number with no minimum, so
// nothing downstream would ever reject the bad value and say so.

#include <unity.h>

#include "net.h"

void setUp() {}
void tearDown() {}

static void test_seconds_since_boot_is_not_a_set_clock() {
  // What gettimeofday() returns on an ESP32 that has never been told the
  // time: it counts from 0 at reset. Zero, the first second, and an hour of
  // uptime must all read as unset.
  TEST_ASSERT_FALSE(clock_is_set(0.0));
  TEST_ASSERT_FALSE(clock_is_set(1.0));
  TEST_ASSERT_FALSE(clock_is_set(3600.0));
}

static void test_a_long_uptime_is_still_not_a_set_clock() {
  // 49.7 days of uptime -- longer than any bench soak this project runs, and
  // still five orders of magnitude short of a real epoch second.
  TEST_ASSERT_FALSE(clock_is_set(4294967.295));
}

static void test_a_real_epoch_second_is_a_set_clock() {
  // The contract's own example timestamp (contract/schemas, 1754035188.412 --
  // 2025-08-01). A frame carrying this is publishable.
  TEST_ASSERT_TRUE(clock_is_set(1754035188.412));
}

static void test_the_threshold_boundary_is_pinned() {
  // 1.7e9 is 2023-11-14: after this project began and far beyond any
  // plausible uptime, so the boundary itself counts as set, and one second
  // below it does not. Pins the comparison as >=, not >, and pins the
  // constant against a silent edit.
  TEST_ASSERT_EQUAL_DOUBLE(1.7e9, kMinSyncedUnixSeconds);
  TEST_ASSERT_TRUE(clock_is_set(kMinSyncedUnixSeconds));
  TEST_ASSERT_FALSE(clock_is_set(kMinSyncedUnixSeconds - 1.0));
}

static void test_a_negative_timestamp_is_not_a_set_clock() {
  // gettimeofday() should never produce one, but a pre-epoch date is not a
  // clock this device may stamp a frame with either.
  TEST_ASSERT_FALSE(clock_is_set(-1.0));
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_seconds_since_boot_is_not_a_set_clock);
  RUN_TEST(test_a_long_uptime_is_still_not_a_set_clock);
  RUN_TEST(test_a_real_epoch_second_is_a_set_clock);
  RUN_TEST(test_the_threshold_boundary_is_pinned);
  RUN_TEST(test_a_negative_timestamp_is_not_a_set_clock);
  return UNITY_END();
}
