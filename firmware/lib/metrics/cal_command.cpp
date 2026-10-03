#include "cal_command.h"

#include <cctype>
#include <cstdlib>

#include "calibration.h"

namespace {

constexpr int kValueCount = 4;
constexpr int kTrimCount = 3;

const char* const kUsage =
    "CAL needs four numbers: CAL <v_cal> <i_cal_low> <i_cal_high> "
    "<phase_rad>";

const char* const kTooMany = "CAL takes exactly four numbers and got more";

const char* const kNotANumber[kValueCount] = {
    "v_cal is not a number",
    "i_cal_low is not a number",
    "i_cal_high is not a number",
    "phase_rad is not a number",
};

const char* const kTrimOutOfRange[kTrimCount] = {
    "v_cal is more than 4x from the constant calibration.h derives -- a trim "
    "corrects a derived value, so this is a typo or different hardware",
    "i_cal_low is more than 4x from the constant calibration.h derives -- a "
    "trim corrects a derived value, so this is a typo or different hardware",
    "i_cal_high is more than 4x from the constant calibration.h derives -- a "
    "trim corrects a derived value, so this is a typo or different hardware",
};

const char* const kPhaseOutOfRange =
    "phase_rad is beyond +/-0.785 rad (45 deg) -- it is in RADIANS, and an "
    "error that large means reversed polarity: turn a clamp round or swap "
    "the adapter's leads instead";

bool is_space(char c) {
  return std::isspace(static_cast<unsigned char>(c)) != 0;
}

const char* skip_spaces(const char* p) {
  while (is_space(*p)) ++p;
  return p;
}

/// Reads one number at `cursor` and advances past it.
///
/// The number must END at whitespace or at the end of the line. strtod on
/// its own would read "0.28x" as 0.28 and stop, and the refusal would then
/// name the NEXT field as the one that is not a number -- pointing the bench
/// at the wrong typo.
bool read_number(const char*& cursor, double& value) {
  char* end = nullptr;
  value = std::strtod(cursor, &end);
  if (end == cursor) return false;
  if (*end != '\0' && !is_space(*end)) return false;
  cursor = end;
  return true;
}

/// Written as "inside the range", so that a NaN -- which compares false
/// against everything -- lands outside it. That, and an infinity failing the
/// same bounds, is the whole of the non-finite check.
bool within(double value, double lowest, double highest) {
  return value >= lowest && value <= highest;
}

}  // namespace

const char* parse_calibration_args(const char* args, CalibrationSet& out) {
  const char* cursor = skip_spaces(args != nullptr ? args : "");

  double values[kValueCount] = {};
  for (int i = 0; i < kValueCount; ++i) {
    if (*cursor == '\0') return kUsage;
    if (!read_number(cursor, values[i])) return kNotANumber[i];
    cursor = skip_spaces(cursor);
  }
  if (*cursor != '\0') return kTooMany;

  const double derived[kTrimCount] = {
      calibration::kVCal, calibration::kICalLow, calibration::kICalHigh};
  for (int i = 0; i < kTrimCount; ++i) {
    if (!within(values[i], derived[i] / kMaxTrimFactor,
                derived[i] * kMaxTrimFactor)) {
      return kTrimOutOfRange[i];
    }
  }
  if (!within(values[3], -kMaxPhaseCorrectionRad, kMaxPhaseCorrectionRad)) {
    return kPhaseOutOfRange;
  }

  out.v_cal = values[0];
  out.i_cal_low = values[1];
  out.i_cal_high = values[2];
  out.phase_correction_rad = values[3];
  out.stored_at = 0;
  return nullptr;
}
