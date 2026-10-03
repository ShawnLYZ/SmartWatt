#include <unity.h>

#include <cmath>

#include "goertzel.h"

void setUp() {}
void tearDown() {}

static constexpr int N = 80;  // 4 kSPS at 50 Hz
static constexpr double PI = 3.14159265358979323846;

/// Feeds A*sin(2*pi*bin*n/N + phase) through a Goertzel at `bin`.
static Phasor run(int bin, double amplitude, double phase, int harmonic = 0) {
  Goertzel g(bin, N);
  for (int n = 0; n < N; ++n) {
    const double theta = 2.0 * PI * n / N;
    double x = amplitude * std::sin(bin * theta + phase);
    if (harmonic > 0) x += 0.5 * std::sin(harmonic * theta);
    g.push(x);
  }
  return g.result();
}

static void test_magnitude_is_amplitude_times_half_n() {
  const Phasor p = run(1, 2.0, 0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 2.0 * N / 2.0, p.magnitude());
}

static void test_amplitude_helper_recovers_the_input() {
  const Phasor p = run(1, 3.5, 0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 3.5, amplitude_from(p, N));
}

static void test_rms_helper() {
  const Phasor p = run(1, std::sqrt(2.0), 0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 1.0, rms_from(p, N));
}

static void test_phase_convention() {
  // arg(X) = pi/2 - phi for x[n] = A*sin(theta + phi).
  const Phasor p = run(1, 1.0, 0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, PI / 2.0, p.phase());
}

static void test_phase_difference_recovers_the_lag() {
  // THE property the whole Q1 calculation rests on.
  const double lag = 0.62;
  const Phasor v = run(1, 1.0, 0.0);
  const Phasor i = run(1, 1.0, -lag);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, lag, i.phase() - v.phase());
}

static void test_bin_h_finds_harmonic_h_with_no_interpolation() {
  // Because the window is exactly one period, the bin index for harmonic h
  // is simply h. No windowing function, no interpolation.
  Goertzel third(3, N);
  for (int n = 0; n < N; ++n) {
    const double theta = 2.0 * PI * n / N;
    third.push(1.0 * std::sin(theta) + 0.25 * std::sin(3.0 * theta));
  }
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.25, amplitude_from(third.result(), N));
}

static void test_fundamental_is_blind_to_a_harmonic() {
  const Phasor p = run(1, 1.0, 0.0, /*harmonic=*/3);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 1.0, amplitude_from(p, N));
}

static void test_dc_does_not_leak_into_bin_one() {
  Goertzel g(1, N);
  for (int n = 0; n < N; ++n) g.push(5.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 0.0, g.result().magnitude());
}

static void test_zero_input_is_zero() {
  Goertzel g(1, N);
  for (int n = 0; n < N; ++n) g.push(0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, g.result().magnitude());
}

static void test_reset_clears_state() {
  Goertzel g(1, N);
  for (int n = 0; n < N; ++n) g.push(1.0);
  g.reset();
  for (int n = 0; n < N; ++n) g.push(0.0);
  TEST_ASSERT_DOUBLE_WITHIN(1e-12, 0.0, g.result().magnitude());
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_magnitude_is_amplitude_times_half_n);
  RUN_TEST(test_amplitude_helper_recovers_the_input);
  RUN_TEST(test_rms_helper);
  RUN_TEST(test_phase_convention);
  RUN_TEST(test_phase_difference_recovers_the_lag);
  RUN_TEST(test_bin_h_finds_harmonic_h_with_no_interpolation);
  RUN_TEST(test_fundamental_is_blind_to_a_harmonic);
  RUN_TEST(test_dc_does_not_leak_into_bin_one);
  RUN_TEST(test_zero_input_is_zero);
  RUN_TEST(test_reset_clears_state);
  return UNITY_END();
}
