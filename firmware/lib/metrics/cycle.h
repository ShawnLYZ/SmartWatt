#pragma once

#include "nvs_calibration.h"
#include "sample_source.h"

enum class Range { Low, High };

struct CycleMetrics {
  double vrms = 0.0;
  double irms = 0.0;
  double p = 0.0;
  double q1 = 0.0;
  double dist = 0.0;
  double s = 0.0;
  double pf_true = 1.0;
  double pf_disp = 1.0;
  double freq = 0.0;
  double crest = 0.0;
  double h3_h1 = 0.0;
  double h5_h1 = 0.0;
  double h7_h1 = 0.0;
  Range range = Range::Low;
  int samples = 0;
  bool valid = false;
  /// The VOLTAGE channel came within calibration::kClipMargin of a rail
  /// somewhere in this window. Reported, never used to reject the window --
  /// see finish_window().
  bool v_clipped = false;
};

/// Accumulates one mains period at a time, locked to positive-going voltage
/// zero crossings.
///
/// Locking to zero crossings is what makes each window exactly one period,
/// and therefore what makes the Goertzel bin index for harmonic h simply h.
/// No windowing function and no bin interpolation are needed.
class CycleProcessor {
 public:
  static constexpr int kMaxWindow = 256;

  CycleProcessor() = default;

  void reset();

  /// Replaces the trimmed constants used for the volts/amps conversions and
  /// the phase correction applied to phi1 -- which Q1, pf_disp and P's
  /// fundamental term are all computed from -- in place of the compile-time
  /// defaults. A CycleProcessor nobody calls this on keeps using
  /// default_calibration() -- see `cal_` below -- so today's behaviour is
  /// the untouched default, not a special case.
  void set_calibration(const CalibrationSet& cal);

  /// Feeds one sample set. Returns true on the sample that completes a
  /// window, in which case `out` holds that cycle's metrics.
  bool push(const SampleSet& set, CycleMetrics& out);

 private:
  void begin_window();
  /// `period_samples` is the interpolated crossing-to-crossing distance and
  /// is generally fractional; `next_*` is the sample that triggered the
  /// closing crossing, which is the first sample of the NEXT window and the
  /// neighbour of the fractional remainder. finish_window() needs it to
  /// weight that remainder.
  void finish_window(CycleMetrics& out, double period_samples, double next_v,
                     double next_i_low, double next_i_high);

  /// Trimmed calibration constants and phase correction, read by push() and
  /// finish_window() in place of the compile-time defaults in
  /// calibration.h. Defaults to default_calibration(), which carries the
  /// SAME constants and a zero phase correction, so this is a genuine
  /// default, not merely a placeholder waiting for set_calibration().
  CalibrationSet cal_ = default_calibration();

  double buffer_v_[kMaxWindow] = {};
  double buffer_i_low_[kMaxWindow] = {};
  double buffer_i_high_[kMaxWindow] = {};
  int count_ = 0;

  double previous_v_ = 0.0;
  bool have_previous_ = false;
  bool collecting_ = false;

  /// Interpolated sample index of the last positive-going zero crossing.
  double last_crossing_ = 0.0;
  double sample_index_ = 0.0;

  bool low_clipped_ = false;
  bool high_clipped_ = false;
  bool v_clipped_ = false;
  /// Set when the write guard in push() refuses a sample because the
  /// buffer is already at kMaxWindow: the window ran longer than the
  /// buffer can hold, so the accumulation no longer covers exactly one
  /// period and finish_window() must reject it.
  bool truncated_ = false;
};
