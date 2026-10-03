#pragma once

#include <cmath>

/// One DFT bin as a complex value.
///
/// Convention: this is the CONJUGATE of the usual forward DFT — the kernel is
/// exp(+j*2*pi*k*n/N), not exp(-j*...). For x[n] = A*sin(2*pi*k*n/N + phi) the
/// forward transform gives arg = phi - pi/2; conjugating it gives
///
///     |X| = A*N/2      arg(X) = pi/2 - phi
///
/// which is the convention the whole chain is written against. Consequently,
/// for a current lagging the voltage by phi1, phi1 = arg(I1) - arg(V1): the
/// two pi/2 terms cancel in the difference and the lag is what is left.
/// Verified against the analytic fixtures — an inductive load yields Q1 > 0,
/// matching the simulator's sidecar.
struct Phasor {
  double real = 0.0;
  double imag = 0.0;

  double magnitude() const { return std::sqrt(real * real + imag * imag); }
  double phase() const { return std::atan2(imag, real); }
};

/// Single-bin Goertzel.
///
/// Chosen over an FFT because only four bins are ever needed (1, 3, 5, 7),
/// state is two doubles, and there is no buffer to hold a whole window in.
///
/// `window_samples` is a double, and MAY BE FRACTIONAL. The caller's window
/// spans one mains period, which is only a whole number of samples when the
/// line frequency divides the sample rate exactly: at 4 kSPS, 50.000 Hz gives
/// 80 samples but 49.7 Hz gives 80.483. Placing the bins at 2*pi*h/n for the
/// integer sample count n instead would put them beside the fundamental
/// rather than on it, and the resulting leakage alternates cycle to cycle as
/// n flips between 80 and 81. So the bin frequency and the normalisation both
/// take the true period. Whenever the period IS a whole number the arithmetic
/// is identical to the integer form.
class Goertzel {
 public:
  Goertzel(int bin, double window_samples);

  void reset();
  void push(double x);
  Phasor result() const;

 private:
  double cosine_ = 0.0;
  double sine_ = 0.0;
  double coefficient_ = 0.0;
  double s1_ = 0.0;
  double s2_ = 0.0;
};

/// Peak amplitude of the component in this bin.
inline double amplitude_from(const Phasor& p, double window_samples) {
  return 2.0 * p.magnitude() / window_samples;
}

/// RMS of the component in this bin.
inline double rms_from(const Phasor& p, double window_samples) {
  return amplitude_from(p, window_samples) / std::sqrt(2.0);
}
