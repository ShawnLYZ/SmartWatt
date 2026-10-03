#pragma once

#include "nvs_calibration.h"

/// How far a CAL constant may sit from the one calibration.h derives from
/// datasheet ratios, as a factor either way.
///
/// A trim corrects a derived constant by a few percent; it does not replace
/// it. Four covers every sanctioned hardware variant -- the -010/-030 clamp
/// substitutions (2x and 1.5x), a 10k/820 or 200k/12k voltage divider (1.4x,
/// 1.9x) -- while refusing the typo that matters most, a slipped decimal
/// point, which is 10x.
///
/// `static constexpr` at namespace scope, not C++17's `inline`, for the same
/// reason as net.h's kMinSyncedUnixSeconds: the esp32-s3 build is gnu++11.
static constexpr double kMaxTrimFactor = 4.0;

/// The largest phase correction CAL accepts: pi/4 rad, 45 degrees.
///
/// The worst genuine instrument error in view is the 100k/12k divider's
/// 19.9 degrees plus a few degrees of transformer and clamp error. Past 45
/// degrees the number is either DEGREES typed where radians belong, or the
/// signature of reversed polarity -- which is fixed by turning a clamp round
/// or swapping the adapter's leads, never by calibrating it away.
static constexpr double kMaxPhaseCorrectionRad = 0.78539816339744831;

/// Parses the arguments of the bench's `CAL` serial command -- everything
/// after the word CAL:
///
///   <v_cal> <i_cal_low> <i_cal_high> <phase_correction_rad>
///
/// four numbers separated by whitespace.
///
/// Returns nullptr on success, having written the four constants into `out`
/// and zeroed `stored_at`, which only the caller can stamp (it owns the
/// clock). Otherwise returns a one-line reason fit for the serial line and
/// LEAVES `out` UNTOUCHED: a refused CAL must not half-apply.
///
/// Pure arithmetic, so it runs under `native`; main.cpp does the NVS write.
const char* parse_calibration_args(const char* args, CalibrationSet& out);
