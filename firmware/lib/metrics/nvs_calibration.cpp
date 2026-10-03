#include "nvs_calibration.h"

#include "calibration.h"

CalibrationSet default_calibration() {
  return CalibrationSet{calibration::kVCal, calibration::kICalLow,
                        calibration::kICalHigh, 0.0, 0};
}

#if SMARTWATT_NATIVE

namespace {
CalibrationSet g_stored = default_calibration();
bool g_present = false;
}  // namespace

bool load_calibration(CalibrationSet& out) {
  out = g_stored;
  return g_present;
}

bool save_calibration(const CalibrationSet& set) {
  g_stored = set;
  g_present = true;
  return true;
}

#else

#include <Preferences.h>

namespace {
constexpr const char* kNamespace = "smartwatt";
Preferences g_prefs;
}  // namespace

bool load_calibration(CalibrationSet& out) {
  out = default_calibration();
  if (!g_prefs.begin(kNamespace, /*readOnly=*/true)) return false;
  const bool present = g_prefs.isKey("v_cal");
  if (present) {
    out.v_cal = g_prefs.getDouble("v_cal", out.v_cal);
    out.i_cal_low = g_prefs.getDouble("i_low", out.i_cal_low);
    out.i_cal_high = g_prefs.getDouble("i_high", out.i_cal_high);
    out.phase_correction_rad = g_prefs.getDouble("phase", 0.0);
    out.stored_at = g_prefs.getUInt("at", 0);
  }
  g_prefs.end();
  return present;
}

bool save_calibration(const CalibrationSet& set) {
  if (!g_prefs.begin(kNamespace, /*readOnly=*/false)) return false;

  // Every return value is checked. Preferences::putDouble/putUInt return the
  // number of bytes stored and 0 on failure (a full partition, a failed
  // nvs_commit), so discarding them -- as this function used to -- reports a
  // successful save for a write that never landed. That matters more here
  // than almost anywhere: stored_at exists precisely so a stale calibration
  // is VISIBLE rather than silently trusted, and a silently-failed write
  // leaves the previous trim in place wearing a fresh timestamp's confidence.
  //
  // Deliberately not short-circuited: every field is attempted even after one
  // fails, so a partial write is not made worse by also being partial in a
  // different, order-dependent way, and end() still runs. The single false
  // covers the lot -- the caller's recourse is the same whichever field it was.
  bool ok = true;
  ok = (g_prefs.putDouble("v_cal", set.v_cal) != 0) && ok;
  ok = (g_prefs.putDouble("i_low", set.i_cal_low) != 0) && ok;
  ok = (g_prefs.putDouble("i_high", set.i_cal_high) != 0) && ok;
  ok = (g_prefs.putDouble("phase", set.phase_correction_rad) != 0) && ok;
  ok = (g_prefs.putUInt("at", set.stored_at) != 0) && ok;

  g_prefs.end();
  return ok;
}

#endif
