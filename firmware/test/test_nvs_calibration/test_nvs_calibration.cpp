#include <unity.h>

#include "calibration.h"
#include "nvs_calibration.h"

void setUp() {}
void tearDown() {}

/// The native stub (nvs_calibration.cpp, #if SMARTWATT_NATIVE) backs
/// load_calibration()/save_calibration() with a single pair of namespace-
/// scope variables for the lifetime of this test binary -- there is no
/// per-test reset, mirroring the fact that the real NVS-backed
/// implementation is like this too. So test_load_before_any_save... below
/// MUST run before any test that calls save_calibration(), which is why it
/// is RUN_TEST'd first in main(): once something has saved, "nothing has
/// been saved yet" is no longer true for the rest of this process.

static void test_default_calibration_matches_compile_time_constants() {
  // This is what keeps a CycleProcessor nobody calls set_calibration() on
  // behaving exactly as it did before Task 5 -- see cycle.h's `cal_`
  // default member initializer, which calls this same function.
  const CalibrationSet d = default_calibration();
  TEST_ASSERT_EQUAL_DOUBLE(calibration::kVCal, d.v_cal);
  TEST_ASSERT_EQUAL_DOUBLE(calibration::kICalLow, d.i_cal_low);
  TEST_ASSERT_EQUAL_DOUBLE(calibration::kICalHigh, d.i_cal_high);
  TEST_ASSERT_EQUAL_DOUBLE(0.0, d.phase_correction_rad);
  TEST_ASSERT_EQUAL_UINT32(0, d.stored_at);
}

/// MUST run before any test that calls save_calibration() -- see the file
/// comment above.
static void test_load_before_any_save_reports_absent_but_fills_defaults() {
  CalibrationSet out = CalibrationSet{-1.0, -1.0, -1.0, -1.0, 4242};

  const bool present = load_calibration(out);

  TEST_ASSERT_FALSE_MESSAGE(present, "nothing has been saved yet");
  const CalibrationSet want = default_calibration();
  TEST_ASSERT_EQUAL_DOUBLE(want.v_cal, out.v_cal);
  TEST_ASSERT_EQUAL_DOUBLE(want.i_cal_low, out.i_cal_low);
  TEST_ASSERT_EQUAL_DOUBLE(want.i_cal_high, out.i_cal_high);
  TEST_ASSERT_EQUAL_DOUBLE(want.phase_correction_rad, out.phase_correction_rad);
  TEST_ASSERT_EQUAL_UINT32(want.stored_at, out.stored_at);
}

static void test_save_then_load_round_trips_every_field() {
  const CalibrationSet saved =
      CalibrationSet{0.2010, 0.0040500, 0.016200, 0.0175, 1755000000};

  TEST_ASSERT_TRUE_MESSAGE(save_calibration(saved),
                            "the native stub always reports success");

  CalibrationSet loaded = CalibrationSet{-1.0, -1.0, -1.0, -1.0, 0};
  const bool present = load_calibration(loaded);

  TEST_ASSERT_TRUE_MESSAGE(present, "a calibration was just saved");
  TEST_ASSERT_EQUAL_DOUBLE(saved.v_cal, loaded.v_cal);
  TEST_ASSERT_EQUAL_DOUBLE(saved.i_cal_low, loaded.i_cal_low);
  TEST_ASSERT_EQUAL_DOUBLE(saved.i_cal_high, loaded.i_cal_high);
  TEST_ASSERT_EQUAL_DOUBLE(saved.phase_correction_rad,
                           loaded.phase_correction_rad);
  TEST_ASSERT_EQUAL_UINT32(saved.stored_at, loaded.stored_at);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_default_calibration_matches_compile_time_constants);
  RUN_TEST(test_load_before_any_save_reports_absent_but_fills_defaults);
  RUN_TEST(test_save_then_load_round_trips_every_field);
  return UNITY_END();
}
