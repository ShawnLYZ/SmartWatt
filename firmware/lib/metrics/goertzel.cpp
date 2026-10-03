#include "goertzel.h"

Goertzel::Goertzel(int bin, double window_samples) {
  const double omega =
      2.0 * 3.14159265358979323846 * static_cast<double>(bin) /
      window_samples;
  cosine_ = std::cos(omega);
  sine_ = std::sin(omega);
  coefficient_ = 2.0 * cosine_;
}

void Goertzel::reset() {
  s1_ = 0.0;
  s2_ = 0.0;
}

void Goertzel::push(double x) {
  const double s = x + coefficient_ * s1_ - s2_;
  s2_ = s1_;
  s1_ = s;
}

Phasor Goertzel::result() const {
  Phasor p;
  // The standard Goertzel reconstruction leaves the result rotated by one
  // bin, because s1_ and s2_ carry the last two samples rather than the
  // first. Undoing that rotation here is what makes arg(X) exactly
  // pi/2 - phi, which is the convention the whole chain is written against.
  //
  // Negative imaginary part: this returns the conjugate of the forward DFT
  // (kernel exp(+j...), not exp(-j...); see goertzel.h). The sign is fixed by
  // test_phase_difference_recovers_the_lag and everything downstream depends on it.
  p.real = s1_ * cosine_ - s2_;
  p.imag = -s1_ * sine_;
  return p;
}
