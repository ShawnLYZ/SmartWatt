#include "cycle.h"

#include <cmath>
#include <cstdint>

#include "calibration.h"
#include "goertzel.h"
#include "nvs_calibration.h"

namespace {

/// Bins 1, 3, 5 and 7 are extracted. A bin is only distinct from its alias
/// when N > 2*bin, so this is the shortest window that can carry the
/// highest bin computed (7) without aliasing it.
constexpr int kHighestBinExtracted = 7;
constexpr int kMinWindowSamples = 2 * kHighestBinExtracted + 1;

/// `v_cal` is the trimmed (or default) constant from CycleProcessor::cal_,
/// not the calibration::kVCal compile-time default directly -- see the
/// call site in push().
double counts_to_volts(uint16_t raw, double v_cal) {
  return (static_cast<double>(raw) - calibration::kAdcMid) * v_cal;
}

bool is_clipped(uint16_t raw) {
  return raw <= calibration::kClipMargin ||
         raw >= calibration::kAdcMax - calibration::kClipMargin;
}

}  // namespace

void CycleProcessor::reset() {
  count_ = 0;
  have_previous_ = false;
  collecting_ = false;
  low_clipped_ = false;
  high_clipped_ = false;
  v_clipped_ = false;
  truncated_ = false;
  sample_index_ = 0.0;
  last_crossing_ = 0.0;
}

void CycleProcessor::set_calibration(const CalibrationSet& cal) {
  cal_ = cal;
}

void CycleProcessor::begin_window() {
  count_ = 0;
  low_clipped_ = false;
  high_clipped_ = false;
  v_clipped_ = false;
  truncated_ = false;
}

bool CycleProcessor::push(const SampleSet& set, CycleMetrics& out) {
  const double v = counts_to_volts(set.v, cal_.v_cal);
  const double i_low =
      (static_cast<double>(set.i_low) - calibration::kAdcMid) *
      cal_.i_cal_low;
  const double i_high =
      (static_cast<double>(set.i_high) - calibration::kAdcMid) *
      cal_.i_cal_high;

  sample_index_ += 1.0;

  bool completed = false;

  if (have_previous_ && previous_v_ <= 0.0 && v > 0.0) {
    // Positive-going zero crossing. Interpolate between the straddling
    // samples: an integer count quantises frequency to roughly +/-0.3 Hz
    // at 4 kSPS, which is far too coarse for US7.
    const double fraction = -previous_v_ / (v - previous_v_);
    const double crossing = sample_index_ - 1.0 + fraction;

    if (collecting_ && count_ > 4) {
      const double period_samples = crossing - last_crossing_;
      finish_window(out, period_samples, v, i_low, i_high);
      completed = true;
    }

    last_crossing_ = crossing;
    collecting_ = true;
    begin_window();
  }

  if (collecting_) {
    if (count_ < kMaxWindow) {
      buffer_v_[count_] = v;
      buffer_i_low_[count_] = i_low;
      buffer_i_high_[count_] = i_high;
      ++count_;
      if (is_clipped(set.i_low)) low_clipped_ = true;
      if (is_clipped(set.i_high)) high_clipped_ = true;
      if (is_clipped(set.v)) v_clipped_ = true;
    } else {
      // The buffer is already full and the window has not closed: this
      // sample is dropped, so the window no longer spans exactly one
      // period. finish_window() rejects on this.
      truncated_ = true;
    }
  }

  previous_v_ = v;
  have_previous_ = true;
  return completed;
}

void CycleProcessor::finish_window(CycleMetrics& out, double period_samples,
                                   double next_v, double next_i_low,
                                   double next_i_high) {
  const int n = count_;

  // Range is selected HERE, once, by inspecting the completed window.
  // Never mid-cycle: a range change inside a window would corrupt the
  // Goertzel accumulation it sits in.
  const Range range = low_clipped_ ? Range::High : Range::Low;
  const double* current =
      (range == Range::High) ? buffer_i_high_ : buffer_i_low_;

  // The bins sit at the TRUE period, not at the integer sample count: bin h
  // is harmonic h only when the window length used to place it is the length
  // the window actually spans. See the note in goertzel.h.
  Goertzel v1(1, period_samples), i1(1, period_samples), i3(3, period_samples),
      i5(5, period_samples), i7(7, period_samples);

  // The buffer holds n WHOLE samples but the window spans period_samples,
  // which is fractional. Normalising by n instead makes every aggregate
  // frequency-dependent: at 49.7 Hz the same steady load alternates between
  // 80- and 81-sample windows, P swings 22 W, and the detector reads that
  // swing as a switching edge. Add the signed remainder from the sample on
  // the side the window is short or long on, then divide by the true period.
  // w is 0 whenever the period is an integer, so this is exactly a no-op at
  // a nominal 50 Hz.
  const double w = period_samples - static_cast<double>(n);
  const double raw_extra_v = (w >= 0.0) ? next_v : buffer_v_[n - 1];
  const double raw_extra_i =
      (w >= 0.0) ? ((range == Range::High) ? next_i_high : next_i_low)
                 : current[n - 1];

  // THE BIAS IS MEASURED, NOT ASSUMED.
  //
  // push() converts against calibration::kAdcMid, but nothing holds the bias
  // rail there. On the prototype board it sat about 280 counts high
  // (1.88 V, not 1.65 V) on all three channels and wandered by tens of counts
  // over 100 ms. A clamp cannot pass DC and mains carries none, so any DC in
  // a window is the front end's, not the load's. Left in, the two offsets
  // multiplied: v's ~56 V by i_low's ~1.1 A made P read 50-140 W with nothing
  // switched on, and Irms, S and D carried the same error. So each channel's
  // mean over the window, weighted like the sums below and so taken over the
  // true period, is removed before anything is computed from it. On a
  // fixture centred on kAdcMid the means are ~0, and nothing moves.
  double sum_v = w * raw_extra_v;
  double sum_i = w * raw_extra_i;
  for (int k = 0; k < n; ++k) {
    sum_v += buffer_v_[k];
    sum_i += current[k];
  }
  const double mean_v = sum_v / period_samples;
  const double mean_i = sum_i / period_samples;

  double sum_vv = 0.0;
  double sum_ii = 0.0;
  double sum_vi = 0.0;
  double peak_i = 0.0;

  for (int k = 0; k < n; ++k) {
    const double v = buffer_v_[k] - mean_v;
    const double i = current[k] - mean_i;

    sum_vv += v * v;
    sum_ii += i * i;
    sum_vi += v * i;
    if (std::fabs(i) > peak_i) peak_i = std::fabs(i);

    v1.push(v);
    i1.push(i);
    i3.push(i);
    i5.push(i);
    i7.push(i);
  }

  // The fractional remainder, from the sample chosen above.
  const double extra_v = raw_extra_v - mean_v;
  const double extra_i = raw_extra_i - mean_i;
  sum_vv += w * extra_v * extra_v;
  sum_ii += w * extra_i * extra_i;
  sum_vi += w * extra_v * extra_i;

  // The sample count and the window length are different numbers now, and
  // both are wanted: `samples` is how many real samples were captured.
  out.samples = n;
  out.range = range;
  out.freq = (period_samples > 0.0)
                 ? calibration::kSampleRateHz / period_samples
                 : 0.0;

  out.vrms = std::sqrt(sum_vv / period_samples);
  out.irms = std::sqrt(sum_ii / period_samples);
  out.s = out.vrms * out.irms;

  // Total real power, time domain. Includes any harmonic active power. Its
  // fundamental term is phase-corrected below, once phi1 is known.
  out.p = sum_vi / period_samples;

  const Phasor pv = v1.result();
  const Phasor pi = i1.result();
  const double v1_rms = rms_from(pv, period_samples);
  const double i1_rms = rms_from(pi, period_samples);

  // Sign convention fixed in goertzel.h: a current lagging the voltage by
  // phi1 gives phi1 = arg(I1) - arg(V1), so an inductive load yields Q1 > 0.
  //
  // cal_.phase_correction_rad is the clamp's OWN phase error (SCT-013 carries
  // 0.5-2 deg of it), measured by reading phi1 on a load whose true phi1 is
  // ~0: whatever the instrument reports there IS the error, so subtracting
  // it removes the clamp's bias rather than adding to it. Q1 = V1*I1*sin(phi1)
  // is maximally sensitive to exactly this error at PF ~= 1, which is the
  // fan-versus-incandescent boundary this project exists to discriminate --
  // see nvs_calibration.h and docs/hardware/calibration-log.md.
  const double phi1_raw = pi.phase() - pv.phase();
  const double phi1 = phi1_raw - cal_.phase_correction_rad;

  // THE CORRECTION REACHES P, NOT ONLY Q1.
  //
  // out.p is the time-domain integral, so the instrument's phase error sits
  // in its fundamental term, V1*I1*cos(phi1_raw), and correcting phi1 for Q1
  // alone left it there. The error is not only the clamps' 0.5-2 degrees:
  // the voltage channel's anti-alias RC sees the divider's Thevenin
  // resistance in series with its 820 ohms, which lags the voltage 19.9
  // degrees with the 100k/12k divider (2.8 with 10k/820 --
  // docs/hardware/bringup-runbook.md). Uncorrected, that read P 6% low on a
  // resistive load and 23% low on the fan, and D = sqrt(S^2 - P^2 - Q1^2)
  // then reported a third of the kettle's S as distortion. Swapping the raw
  // fundamental term for the corrected one fixes P -- and D and pf_true,
  // which are computed from it below -- while the harmonic active power
  // stays as measured.
  //
  // EXACTLY a no-op with no correction stored: phi1_raw - 0.0 is phi1_raw in
  // IEEE arithmetic, so the cosine difference is exactly zero, and every
  // fixture test sees the time-domain P bit for bit.
  out.p += v1_rms * i1_rms * (std::cos(phi1) - std::cos(phi1_raw));

  // TRUE FUNDAMENTAL REACTIVE POWER.
  //
  // The superseded draft computed Q = sqrt(S^2 - P^2), which is non-active
  // power and conflates this with harmonic distortion. Two loads in parallel
  // superpose linearly in P and Q1 but NOT in sqrt(S^2 - P^2), so the
  // tracker's delta arithmetic silently breaks whenever two switch-mode
  // loads overlap. Running Goertzel on the VOLTAGE waveform as well is what
  // buys the separation.
  out.q1 = v1_rms * i1_rms * std::sin(phi1);

  // Distortion RESIDUE, not a strict IEEE 1459 quantity: it absorbs current
  // distortion power, voltage distortion power and harmonic active power
  // together. Calling it a residue rather than distortion power is the
  // honest description.
  const double residue = out.s * out.s - out.p * out.p - out.q1 * out.q1;
  out.dist = (residue > 0.0) ? std::sqrt(residue) : 0.0;

  // Guarded exactly like pf_true below: with no fundamental current (or
  // voltage) there is no angle to take between them, and atan2(0,0) would
  // otherwise let phi1 collapse to -arg(V1) and report the sub-sample
  // window offset as if it were a power factor.
  out.pf_disp = (i1_rms > 0.0 && v1_rms > 0.0) ? std::cos(phi1) : 1.0;
  out.pf_true = (out.s > 0.0) ? out.p / out.s : 1.0;
  out.crest = (out.irms > 0.0) ? peak_i / out.irms : 0.0;

  // i1_rms IS the fundamental current RMS, already computed above for Q1.
  const double h1 = i1_rms;
  out.h3_h1 = (h1 > 0.0) ? rms_from(i3.result(), period_samples) / h1 : 0.0;
  out.h5_h1 = (h1 > 0.0) ? rms_from(i5.result(), period_samples) / h1 : 0.0;
  out.h7_h1 = (h1 > 0.0) ? rms_from(i7.result(), period_samples) / h1 : 0.0;

  // Reject windows the metrics above cannot be trusted for, without
  // discarding the numbers themselves:
  //   (1) truncated at kMaxWindow -- bin h stopped being harmonic h;
  //   (2) both current channels clipped -- every metric understated, with
  //       no unclipped channel left to fall back on;
  //   (3) shorter than kMinWindowSamples -- too short to resolve the
  //       highest bin extracted without aliasing.
  out.valid = !truncated_ && !(low_clipped_ && high_clipped_) &&
              n >= kMinWindowSamples;

  // A clipped VOLTAGE window is reported, not rejected. The usual cause is
  // a divider sized for a different adapter, which clips every cycle; a
  // rejecting chain would then go silent, and from the bench that looks
  // like an idle circuit. Carried out instead, so main.cpp can count it on
  // the status line where a wrong divider is visible.
  out.v_clipped = v_clipped_;
}
