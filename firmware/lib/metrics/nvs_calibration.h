#pragma once

#include <cstdint>

struct CalibrationSet {
  double v_cal;
  double i_cal_low;
  double i_cal_high;
  /// Instrument phase error, radians, measured against a resistive load.
  /// Subtracted from the measured phi1 before Q1, pf_disp and P's
  /// fundamental term are computed from it (cycle.cpp).
  double phase_correction_rad;
  /// Unix seconds when this set was stored, so a stale calibration is
  /// visible rather than silently trusted.
  uint32_t stored_at;
};

CalibrationSet default_calibration();

/// Loads the stored calibration into `out`.
///
/// Returns TRUE only when a stored set was actually found and read.
///
/// Returns FALSE when nothing is stored (or the NVS namespace could not be
/// opened) -- and in that case `out` IS STILL FILLED, with
/// default_calibration(). So the return value answers "is this a stored
/// trim or the compile-time default?", not "is `out` usable?": `out` is
/// always usable. A caller that ignores the return value gets working
/// defaults rather than uninitialised numbers, but it also loses the one
/// signal that distinguishes a device that has been calibrated from one that
/// has not -- which is exactly the question the bench needs answered out
/// loud (see the `Calibration:` lines in src/main.cpp's setup()).
bool load_calibration(CalibrationSet& out);

/// Stores `set`, returning true only if every field was actually written.
///
/// False means the namespace would not open, or at least one write failed --
/// a full or failing NVS partition. Nothing partial is reported as success:
/// stored_at exists so that a stale calibration is visible rather than
/// silently trusted, and a write that silently did not happen is the same
/// hazard wearing a different hat.
bool save_calibration(const CalibrationSet& set);
