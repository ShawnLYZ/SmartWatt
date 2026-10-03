#include <unity.h>

#include <cstdio>
#include <cstring>
#include <initializer_list>

#include "cal_command.h"
#include "calibration.h"
#include "nvs_calibration.h"

void setUp() {}
void tearDown() {}

namespace {

/// A set no successful parse can produce, so "left untouched" is visible.
CalibrationSet sentinel() {
  return CalibrationSet{-1.0, -2.0, -3.0, -4.0, 4242};
}

void assert_untouched(const CalibrationSet& out) {
  TEST_ASSERT_EQUAL_DOUBLE(-1.0, out.v_cal);
  TEST_ASSERT_EQUAL_DOUBLE(-2.0, out.i_cal_low);
  TEST_ASSERT_EQUAL_DOUBLE(-3.0, out.i_cal_high);
  TEST_ASSERT_EQUAL_DOUBLE(-4.0, out.phase_correction_rad);
  TEST_ASSERT_EQUAL_UINT32(4242, out.stored_at);
}

/// Parses `args` into a sentinel and asserts it was refused, untouched, with
/// a reason naming `field` -- the refusal is printed on the bench, so it has
/// to point at the value that is actually wrong.
void assert_refused(const char* args, const char* field) {
  CalibrationSet out = sentinel();
  const char* refusal = parse_calibration_args(args, out);
  TEST_ASSERT_NOT_NULL_MESSAGE(refusal, args);
  TEST_ASSERT_NOT_NULL_MESSAGE(std::strstr(refusal, field), refusal);
  assert_untouched(out);
}

/// Four values, the first three scaled from calibration.h's derived
/// constants, formatted to round-trip exactly.
void format_args(char* buffer, std::size_t size, double v_scale,
                 double low_scale, double high_scale, double phase) {
  std::snprintf(buffer, size, "%.17g %.17g %.17g %.17g",
                calibration::kVCal * v_scale,
                calibration::kICalLow * low_scale,
                calibration::kICalHigh * high_scale, phase);
}

}  // namespace

static void test_four_values_are_parsed_in_order() {
  CalibrationSet out = sentinel();
  const char* refusal =
      parse_calibration_args(" 0.2835  0.0040512\t0.016201 0.0493 ", out);
  TEST_ASSERT_NULL_MESSAGE(refusal, refusal);
  TEST_ASSERT_EQUAL_DOUBLE(0.2835, out.v_cal);
  TEST_ASSERT_EQUAL_DOUBLE(0.0040512, out.i_cal_low);
  TEST_ASSERT_EQUAL_DOUBLE(0.016201, out.i_cal_high);
  TEST_ASSERT_EQUAL_DOUBLE(0.0493, out.phase_correction_rad);
  // When it was stored is the caller's to stamp; the parser has no clock.
  TEST_ASSERT_EQUAL_UINT32(0, out.stored_at);
}

/// Typing the compile-time defaults back in is the documented way to return
/// to them, so they must always be accepted.
static void test_the_derived_constants_themselves_are_accepted() {
  char args[128];
  format_args(args, sizeof(args), 1.0, 1.0, 1.0, 0.0);
  CalibrationSet out = sentinel();
  TEST_ASSERT_NULL_MESSAGE(parse_calibration_args(args, out), args);
  TEST_ASSERT_EQUAL_DOUBLE(calibration::kVCal, out.v_cal);
  TEST_ASSERT_EQUAL_DOUBLE(calibration::kICalLow, out.i_cal_low);
  TEST_ASSERT_EQUAL_DOUBLE(calibration::kICalHigh, out.i_cal_high);
  TEST_ASSERT_EQUAL_DOUBLE(0.0, out.phase_correction_rad);
}

static void test_too_few_values_are_refused() {
  assert_refused("", "needs four");
  assert_refused("   ", "needs four");
  assert_refused("0.2835 0.0040512 0.016201", "needs four");

  CalibrationSet out = sentinel();
  TEST_ASSERT_NOT_NULL(parse_calibration_args(nullptr, out));
  assert_untouched(out);
}

static void test_a_fifth_value_is_refused() {
  assert_refused("0.2835 0.0040512 0.016201 0.0493 7", "exactly four");
}

static void test_a_non_number_is_refused_by_name() {
  assert_refused("abc 0.0040512 0.016201 0.0493", "v_cal");
  assert_refused("0.2835 abc 0.016201 0.0493", "i_cal_low");
  assert_refused("0.2835 0.0040512 abc 0.0493", "i_cal_high");
  assert_refused("0.2835 0.0040512 0.016201 abc", "phase_rad");
}

/// strtod alone reads "0.2835x" as 0.2835 and stops at the x. Without the
/// end-of-token check the refusal would then blame i_cal_low for a typo
/// that is in v_cal.
static void test_junk_glued_to_a_number_blames_that_number() {
  assert_refused("0.2835x 0.0040512 0.016201 0.0493", "v_cal");
  assert_refused("0.2835 0.0040512 0.016201 0.0493x", "phase_rad");
}

static void test_non_finite_values_are_refused() {
  assert_refused("inf 0.0040512 0.016201 0.0493", "v_cal");
  assert_refused("0.2835 nan 0.016201 0.0493", "i_cal_low");
  assert_refused("0.2835 0.0040512 0.016201 nan", "phase_rad");
  assert_refused("0.2835 0.0040512 0.016201 -inf", "phase_rad");
}

/// The window is exact at both ends and refuses anything past it, on every
/// one of the three trims -- the easy bug is bounds-checking one constant
/// and forgetting another.
static void test_each_trim_is_bounded_at_a_factor_of_four() {
  const char* names[] = {"v_cal", "i_cal_low", "i_cal_high"};
  char args[128];
  for (int field = 0; field < 3; ++field) {
    for (double edge : {kMaxTrimFactor, 1.0 / kMaxTrimFactor}) {
      double scale[3] = {1.0, 1.0, 1.0};
      scale[field] = edge;
      format_args(args, sizeof(args), scale[0], scale[1], scale[2], 0.0);
      CalibrationSet out = sentinel();
      TEST_ASSERT_NULL_MESSAGE(parse_calibration_args(args, out), args);

      scale[field] = edge > 1.0 ? edge * (1.0 + 1e-9) : edge * (1.0 - 1e-9);
      format_args(args, sizeof(args), scale[0], scale[1], scale[2], 0.0);
      assert_refused(args, names[field]);
    }
  }
}

/// The slipped decimal point this bound exists for, and a sign error.
static void test_a_tenfold_or_negative_trim_is_refused() {
  assert_refused("2.835 0.0040512 0.016201 0.0493", "v_cal");
  assert_refused("0.2835 0.040512 0.016201 0.0493", "i_cal_low");
  assert_refused("0.2835 0.0040512 -0.016201 0.0493", "i_cal_high");
}

static void test_phase_is_bounded_at_45_degrees() {
  char args[128];
  for (double phase : {kMaxPhaseCorrectionRad, -kMaxPhaseCorrectionRad}) {
    format_args(args, sizeof(args), 1.0, 1.0, 1.0, phase);
    CalibrationSet out = sentinel();
    TEST_ASSERT_NULL_MESSAGE(parse_calibration_args(args, out), args);
    TEST_ASSERT_EQUAL_DOUBLE(phase, out.phase_correction_rad);
  }
  assert_refused("0.2835 0.0040512 0.016201 0.8", "RADIANS");
  assert_refused("0.2835 0.0040512 0.016201 -0.8", "RADIANS");
  // 2.84 is the 10k/820 divider's lag in DEGREES, typed where radians go.
  assert_refused("0.2835 0.0040512 0.016201 2.84", "RADIANS");
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_four_values_are_parsed_in_order);
  RUN_TEST(test_the_derived_constants_themselves_are_accepted);
  RUN_TEST(test_too_few_values_are_refused);
  RUN_TEST(test_a_fifth_value_is_refused);
  RUN_TEST(test_a_non_number_is_refused_by_name);
  RUN_TEST(test_junk_glued_to_a_number_blames_that_number);
  RUN_TEST(test_non_finite_values_are_refused);
  RUN_TEST(test_each_trim_is_bounded_at_a_factor_of_four);
  RUN_TEST(test_a_tenfold_or_negative_trim_is_refused);
  RUN_TEST(test_phase_is_bounded_at_45_degrees);
  return UNITY_END();
}
