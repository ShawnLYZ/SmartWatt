#include <unity.h>

#include <cmath>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

#include "../fixtures/expected.h"
#include "cycle.h"
#include "file_sample_source.h"
#include "nvs_calibration.h"

void setUp() {}
void tearDown() {}

static std::string fixture(const std::string& name) {
  return std::string(FIXTURE_DIR) + "/" + name + ".csv";
}

/// A `const char*` parameter, not `const std::string&`: binding the
/// returned reference through a `std::string` temporary built from a
/// string-literal argument trips `-Wdangling-reference` on GCC 13+, even
/// though the reference returned is always into the static EXPECTED[]
/// table below, never into the argument.
static const ExpectedFixture& expected(const char* name) {
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    if (std::strcmp(name, EXPECTED[i].name) == 0) return EXPECTED[i];
  }
  TEST_FAIL_MESSAGE("fixture not found in expected.h");
  return EXPECTED[0];
}

/// Runs a whole fixture and returns every complete cycle, in order.
static std::vector<CycleMetrics> all_cycles(const std::string& name) {
  FileSampleSource source(fixture(name).c_str());
  TEST_ASSERT_TRUE_MESSAGE(source.ok(), "fixture missing");
  CycleProcessor processor;
  CycleMetrics metrics{};
  SampleSet set{};
  std::vector<CycleMetrics> cycles;
  while (source.next(set)) {
    if (processor.push(set, metrics)) cycles.push_back(metrics);
  }
  TEST_ASSERT_FALSE_MESSAGE(cycles.empty(), "no complete cycle");
  return cycles;
}

/// Runs a whole fixture and returns the LAST complete cycle, which is the
/// one furthest from any start-up transient.
static CycleMetrics last_cycle(const std::string& name) {
  const CycleMetrics latest = all_cycles(name).back();
  TEST_ASSERT_TRUE_MESSAGE(latest.valid, "no complete cycle");
  return latest;
}

/// Same as last_cycle(), but through an explicit set_calibration() call
/// instead of the CycleProcessor default -- for exercising the trimmed
/// constants and the phase correction directly.
static CycleMetrics last_cycle_with_calibration(const std::string& name,
                                                const CalibrationSet& cal) {
  FileSampleSource source(fixture(name).c_str());
  TEST_ASSERT_TRUE_MESSAGE(source.ok(), "fixture missing");
  CycleProcessor processor;
  processor.set_calibration(cal);
  CycleMetrics metrics{};
  CycleMetrics latest{};
  bool any = false;
  SampleSet set{};
  while (source.next(set)) {
    if (processor.push(set, metrics)) {
      latest = metrics;
      any = true;
    }
  }
  TEST_ASSERT_TRUE_MESSAGE(any, "no complete cycle");
  TEST_ASSERT_TRUE_MESSAGE(latest.valid, "no complete cycle");
  return latest;
}

static void test_window_is_exactly_one_period() {
  const CycleMetrics m = last_cycle("pure-resistive");
  TEST_ASSERT_INT_WITHIN(1, 80, m.samples);
}

static void test_vrms_matches_the_sidecar() {
  const CycleMetrics m = last_cycle("pure-resistive");
  TEST_ASSERT_DOUBLE_WITHIN(1.0, expected("pure-resistive").v_rms, m.vrms);
}

static void test_real_power_matches_the_sidecar() {
  const CycleMetrics m = last_cycle("pure-resistive");
  const double want = expected("pure-resistive").p;
  TEST_ASSERT_DOUBLE_WITHIN(std::fabs(want) * 0.01 + 1.0, want, m.p);
}

/// P and Q1 against the analytic sidecar on EVERY fixture, not just one
/// each. mixed-three in particular -- the only multi-load fixture, and the
/// one whose superposition property is the entire argument for computing
/// Q1 from the fundamental phasors rather than as sqrt(S^2 - P^2) -- had
/// neither asserted anywhere.
///
/// The tolerances are roughly 10x tighter than the 1% + 1.0 used above, and
/// still clear the measured worst case with headroom: 0.85% / 0.42 W on P
/// and 2.21% / 0.80 VAr on Q1, across every cycle of all eight fixtures. A
/// loose tolerance here is what let a 0.65% systematic bias sit unnoticed.
static void test_real_power_matches_the_sidecar_for_every_fixture() {
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    const ExpectedFixture& want = EXPECTED[i];
    const CycleMetrics m = last_cycle(want.name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(std::fabs(want.p) * 0.002 + 0.6, want.p,
                                      m.p, want.name);
  }
}

static void test_q1_matches_the_sidecar_for_every_fixture() {
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    const ExpectedFixture& want = EXPECTED[i];
    const CycleMetrics m = last_cycle(want.name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(std::fabs(want.q1) * 0.03 + 0.6, want.q1,
                                      m.q1, want.name);
  }
}

/// A steady load's power must not depend on where the sample clock happens
/// to land relative to the mains zero crossings. When the window is
/// normalised by its integer sample count rather than by the period it
/// spans, it does: at 49.7 Hz the window alternates between 80 and 81
/// samples and P swings 22 W on a constant 1800 W load, which is a
/// detectable switching edge. Measured spread under the correct
/// normalisation: exactly 0 on the six 50 Hz fixtures, 0.65 W on freq-low
/// and 0.47 W on freq-high.
static void test_metrics_are_stable_across_cycles() {
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    const char* name = EXPECTED[i].name;
    const std::vector<CycleMetrics> cycles = all_cycles(name);
    TEST_ASSERT_GREATER_THAN_MESSAGE(1, (int)cycles.size(), name);

    double lowest = cycles[0].p, highest = cycles[0].p, total = 0.0;
    for (const CycleMetrics& m : cycles) {
      if (m.p < lowest) lowest = m.p;
      if (m.p > highest) highest = m.p;
      total += m.p;
    }
    const double mean = total / static_cast<double>(cycles.size());
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(std::fabs(mean) * 0.002 + 0.5, 0.0,
                                      highest - lowest, name);
  }
}

static void test_resistive_load_has_near_zero_q1() {
  const CycleMetrics m = last_cycle("pure-resistive");
  TEST_ASSERT_DOUBLE_WITHIN(40.0, 0.0, m.q1);
}

static void test_inductive_load_has_positive_q1() {
  const CycleMetrics m = last_cycle("inductive");
  TEST_ASSERT_TRUE_MESSAGE(m.q1 > 0.0, "a lagging current must give Q1 > 0");
}

static void test_q1_matches_the_sidecar_for_an_inductive_load() {
  const CycleMetrics m = last_cycle("inductive");
  const double want = expected("inductive").q1;
  TEST_ASSERT_DOUBLE_WITHIN(std::fabs(want) * 0.05 + 0.5, want, m.q1);
}

static void test_switch_mode_load_has_distortion() {
  const CycleMetrics m = last_cycle("switch-mode");
  TEST_ASSERT_TRUE_MESSAGE(m.dist > 0.0, "a switch-mode load must show D > 0");
}

/// THE test. Both routes to D must agree.
///
///   (1) D = sqrt(S^2 - P^2 - Q1^2)          from aggregate power
///   (2) D = V1 * sqrt(sum_{h>=2} I_h^2)     from the harmonic spectrum
///
/// These are algebraically equal ONLY when reactive power has genuinely
/// been separated from distortion. A chain that conflates the two satisfies
/// (1) trivially and fails (2).
static void test_dist_cross_derivation() {
  const char* names[] = {"pure-resistive", "switch-mode", "inductive",
                         "mixed-three"};
  for (const char* name : names) {
    const ExpectedFixture& want = expected(name);
    if (!want.cross_derivation_holds) continue;
    const CycleMetrics m = last_cycle(name);
    const double tolerance = std::fabs(want.dist) * 0.10 + 1.0;
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(tolerance, want.dist, m.dist, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(
        tolerance, want.dist_from_spectrum, m.dist, name);
  }
}

static void test_distorted_voltage_uses_the_general_formula() {
  // A distorted-VOLTAGE waveform, where the pure-V identity
  // D = V1*sqrt(sum I_h^2) does not hold and cross_derivation_holds is
  // false. What this checks is that S, P, Q1 and D each come out right on
  // such a waveform anyway -- worth having on its own.
  //
  // It does NOT discriminate between the general formula and the pure-V
  // shortcut. The two differ by 0.0055 W here, against a tolerance of 1.799
  // -- 329x below what this assertion can see -- and the fixture's current
  // is only +/-23 ADC counts peak, so quantisation noise already exceeds
  // the difference. Tightening the tolerance cannot recover it at this
  // amplitude.
  //
  // TODO(S1): proving the discrimination needs a distorted-V fixture at
  // kettle-scale current, where the two formulas separate by more than the
  // quantisation floor.
  const ExpectedFixture& want = expected("distorted-v");
  TEST_ASSERT_FALSE(want.cross_derivation_holds);
  const CycleMetrics m = last_cycle("distorted-v");
  TEST_ASSERT_DOUBLE_WITHIN(std::fabs(want.dist) * 0.15 + 1.0, want.dist,
                            m.dist);
}

/// The bin-3, -5 and -7 Goertzels are three quarters of the per-cycle
/// spectral work and h3_h1/h5_h1/h7_h1 are their only consumers, so these
/// two tests are the whole observation of that work.
///
/// Expected ratios come from the sidecar's harmonic_rms map. Measured worst
/// error: 0.00427, on switch-mode's h3.
static void test_harmonic_ratios_match_the_sidecar() {
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    const ExpectedFixture& want = EXPECTED[i];
    const CycleMetrics m = last_cycle(want.name);
    const double wanted[] = {want.h3_ratio, want.h5_ratio, want.h7_ratio};
    const double got[] = {m.h3_h1, m.h5_h1, m.h7_h1};
    const char* labels[] = {" h3_h1", " h5_h1", " h7_h1"};
    for (int h = 0; h < 3; ++h) {
      const std::string message = std::string(want.name) + labels[h];
      TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(wanted[h] * 0.02 + 0.005, wanted[h],
                                        got[h], message.c_str());
    }
  }
}

/// A steady load's spectrum must not depend on where the sample clock lands
/// either. The bins are placed at 2*pi*h/period, and the period is only a
/// whole number of samples at exactly 50.000 Hz; placing them at the integer
/// sample count instead puts them beside the fundamental rather than on it,
/// and the leakage alternates with the window length. On freq-low that made
/// h3_h1 swing between 0.0008 and 0.0096 -- an order of magnitude, on a
/// constant load, where the true value is 0.0051. Measured spread with the
/// bins at the true period: 0.00018 (freq-low) and 0.00019 (freq-high).
///
/// This, not the accuracy check above, is what guards the bin placement:
/// the low-distortion fixtures pass the accuracy check either way.
static void test_harmonic_ratios_are_stable_across_cycles() {
  for (int i = 0; i < EXPECTED_COUNT; ++i) {
    const char* name = EXPECTED[i].name;
    const std::vector<CycleMetrics> cycles = all_cycles(name);
    TEST_ASSERT_GREATER_THAN_MESSAGE(1, (int)cycles.size(), name);

    double lowest = cycles[0].h3_h1, highest = cycles[0].h3_h1;
    for (const CycleMetrics& m : cycles) {
      if (m.h3_h1 < lowest) lowest = m.h3_h1;
      if (m.h3_h1 > highest) highest = m.h3_h1;
    }
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(0.001, 0.0, highest - lowest, name);
  }
}

static void test_interpolated_frequency_beats_an_integer_count() {
  // An integer sample count quantises to about +/-0.3 Hz at 4 kSPS, which
  // is far too coarse for US7's purpose.
  TEST_ASSERT_DOUBLE_WITHIN(0.05, 49.7, last_cycle("freq-low").freq);
  TEST_ASSERT_DOUBLE_WITHIN(0.05, 50.3, last_cycle("freq-high").freq);
}

static void test_nominal_frequency() {
  TEST_ASSERT_DOUBLE_WITHIN(0.05, 50.0, last_cycle("pure-resistive").freq);
}

static void test_pf_true_never_exceeds_pf_disp_for_a_distorting_load() {
  const CycleMetrics m = last_cycle("switch-mode");
  TEST_ASSERT_TRUE(m.pf_true <= m.pf_disp + 1e-9);
}

static void test_range_low_for_a_small_load() {
  TEST_ASSERT_TRUE(last_cycle("switch-mode").range == Range::Low);
}

static void test_range_high_when_low_clips() {
  // A 1800 W kettle saturates i_low. That is why i_high exists.
  TEST_ASSERT_TRUE(last_cycle("pure-resistive").range == Range::High);
}

static void test_range_is_consistent_across_cycles() {
  // What is observable through this API is that a steady load does not flip
  // range from one cycle to the next -- which is what this checks.
  //
  // Stability WITHIN a window is structural rather than testable: the
  // channel is chosen once, in finish_window(), after the window has
  // closed, so there is no point during accumulation at which it could
  // change. That is what keeps a mid-window switch from corrupting the
  // Goertzel accumulation it sits in.
  FileSampleSource source(fixture("pure-resistive").c_str());
  CycleProcessor processor;
  CycleMetrics metrics{};
  SampleSet set{};
  std::vector<Range> ranges;
  while (source.next(set)) {
    if (processor.push(set, metrics)) ranges.push_back(metrics.range);
  }
  TEST_ASSERT_GREATER_THAN(1, (int)ranges.size());
  for (const Range r : ranges) TEST_ASSERT_TRUE(r == ranges[0]);
}

static void test_idle_fixture_is_all_zero_current() {
  const CycleMetrics m = last_cycle("idle");
  TEST_ASSERT_DOUBLE_WITHIN(0.01, 0.0, m.irms);
  TEST_ASSERT_DOUBLE_WITHIN(1.0, 0.0, m.p);
  // With no fundamental current, phi1 has no angle to measure: pf_disp
  // must be guarded to 1.0, not left to report atan2(0,0)'s incidental
  // -arg(V1) as if it were a real power factor.
  TEST_ASSERT_DOUBLE_WITHIN(1e-9, 1.0, m.pf_disp);
}

static void test_frequency_sweep_does_not_desynchronise_the_window() {
  // Windows stay locked to positive-going voltage zero crossings across a
  // frequency change.
  for (const char* name : {"freq-low", "freq-high"}) {
    FileSampleSource source(fixture(name).c_str());
    CycleProcessor processor;
    CycleMetrics metrics{};
    SampleSet set{};
    int cycles = 0;
    while (source.next(set)) {
      if (processor.push(set, metrics)) {
        ++cycles;
        TEST_ASSERT_INT_WITHIN_MESSAGE(2, 80, metrics.samples, name);
      }
    }
    TEST_ASSERT_GREATER_THAN_MESSAGE(4, cycles, name);
  }
}

static void test_short_window_is_rejected_as_invalid() {
  // Bins 1, 3, 5 and 7 are extracted, and a bin is only distinct from its
  // alias when N > 2*bin: a window under 15 samples cannot carry that
  // spectrum. Feed CycleProcessor a "voltage" that crosses zero every 5
  // samples -- far too fast for real mains, but push() only looks at
  // crossings, so this is enough to produce a short window on its own,
  // with no fixture required.
  CycleProcessor processor;
  CycleMetrics metrics{};
  bool saw_completed = false;
  for (int n = 0; n < 20 && !saw_completed; ++n) {
    SampleSet set{};
    set.v = ((n % 10) < 5) ? 1000 : 3000;
    set.i_low = 2048;
    set.i_high = 2048;
    if (processor.push(set, metrics)) saw_completed = true;
  }
  TEST_ASSERT_TRUE_MESSAGE(saw_completed, "expected a window to complete");
  TEST_ASSERT_FALSE_MESSAGE(metrics.valid,
                             "a too-short window must be rejected");
}

/// Runs one synthetic window with the given constant raw current readings
/// on each channel and returns the metrics from the first cycle it
/// completes. No fixture: `push()` takes sample sets directly.
static CycleMetrics run_synthetic_window(uint16_t i_low_raw,
                                          uint16_t i_high_raw) {
  CycleProcessor processor;
  CycleMetrics metrics{};
  bool saw_completed = false;
  for (int n = 0; n < 90 && !saw_completed; ++n) {
    SampleSet set{};
    set.v = ((n % 40) < 20) ? 1000 : 3000;
    set.i_low = i_low_raw;
    set.i_high = i_high_raw;
    if (processor.push(set, metrics)) saw_completed = true;
  }
  TEST_ASSERT_TRUE_MESSAGE(saw_completed, "expected a window to complete");
  return metrics;
}

static void test_both_channels_clipped_is_rejected_but_one_channel_is_not() {
  // Above roughly 23 A rms both shunts saturate together: every metric is
  // understated with no unclipped channel left to fall back on, so that
  // window must be rejected. A comparable window where only i_low clips is
  // the ordinary high-range case and must stay valid.
  const CycleMetrics both_clipped = run_synthetic_window(4095, 4095);
  TEST_ASSERT_FALSE_MESSAGE(both_clipped.valid,
                             "both channels clipped must be rejected");

  const CycleMetrics only_low_clipped = run_synthetic_window(4095, 3000);
  TEST_ASSERT_TRUE_MESSAGE(only_low_clipped.valid,
                            "only i_low clipped must stay valid");
  TEST_ASSERT_TRUE(only_low_clipped.range == Range::High);
}

/// Task 5: set_calibration() must reach the volts conversion AND BOTH
/// current-range conversions, not just one of the three -- the easy bug
/// here is wiring one constant correctly and leaving the wrong field (or
/// the old compile-time default) in one of the others.
///
/// v = (raw - mid) * v_cal is exactly linear in v_cal for a fixed sample
/// sequence, and which range is selected is decided from the RAW counts
/// (is_clipped() in cycle.cpp), never the calibrated value -- so doubling
/// a constant must double the corresponding RMS output, on whichever range
/// is already active, without changing which range that is.
static void test_set_calibration_scales_volts_and_both_current_ranges() {
  // switch-mode stays on the low range (test_range_low_for_a_small_load);
  // pure-resistive clips i_low onto the high range
  // (test_range_high_when_low_clips). Together they exercise all three
  // trimmed constants.
  const CycleMetrics low_baseline =
      last_cycle_with_calibration("switch-mode", default_calibration());
  TEST_ASSERT_TRUE(low_baseline.range == Range::Low);

  CalibrationSet doubled_v = default_calibration();
  doubled_v.v_cal *= 2.0;
  const CycleMetrics scaled_v =
      last_cycle_with_calibration("switch-mode", doubled_v);
  TEST_ASSERT_DOUBLE_WITHIN(low_baseline.vrms * 0.001 + 0.01,
                            low_baseline.vrms * 2.0, scaled_v.vrms);

  CalibrationSet doubled_i_low = default_calibration();
  doubled_i_low.i_cal_low *= 2.0;
  const CycleMetrics scaled_i_low =
      last_cycle_with_calibration("switch-mode", doubled_i_low);
  TEST_ASSERT_DOUBLE_WITHIN(low_baseline.irms * 0.001 + 0.001,
                            low_baseline.irms * 2.0, scaled_i_low.irms);

  const CycleMetrics high_baseline =
      last_cycle_with_calibration("pure-resistive", default_calibration());
  TEST_ASSERT_TRUE(high_baseline.range == Range::High);

  CalibrationSet doubled_i_high = default_calibration();
  doubled_i_high.i_cal_high *= 2.0;
  const CycleMetrics scaled_i_high =
      last_cycle_with_calibration("pure-resistive", doubled_i_high);
  TEST_ASSERT_DOUBLE_WITHIN(high_baseline.irms * 0.001 + 0.001,
                            high_baseline.irms * 2.0, scaled_i_high.irms);
}

/// Task 5, Step 3: the substantive part of the calibration task. Without a
/// phase correction, Q1 = V1*I1*sin(phi1) is maximally wrong exactly where
/// PF ~= 1 -- the fan-versus-incandescent boundary this project exists to
/// discriminate -- and the failure has no visible symptom.
///
/// This does not just recompute finish_window()'s own arithmetic from the
/// same phi1: on a fixture with a purely sinusoidal voltage (inductive's
/// sidecar carries distorted_v: false), current harmonics contribute no
/// real power against a sinusoidal voltage (orthogonality), so out.p IS
/// the fundamental active power V1*I1*cos(phi1), not merely close to it --
/// and with no correction stored, out.p is exactly the raw time-domain
/// integral, independent of the Goertzel phasors phi1/Q1 come from (the
/// phase-corrected term cycle.cpp adds is exactly zero there). That makes
///
///   Q1_corrected = Q1_baseline*cos(correction) - P_baseline*sin(correction)
///
/// an exact trig identity relating TWO SEPARATE runs of the processor (one
/// with zero correction, one with a known nonzero correction), not a
/// restatement of what finish_window() computes internally. A sign error
/// (adding the correction instead of subtracting it) would miss this
/// prediction by roughly 2*P*sin(correction) -- about 3.3 VAr here, more
/// than 10x the tolerance below.
static void test_phase_correction_moves_q1_by_the_exact_trig_relation() {
  const CycleMetrics baseline =
      last_cycle_with_calibration("inductive", default_calibration());

  CalibrationSet corrected_cal = default_calibration();
  const double correction = 0.0349;  // ~2 degrees: SCT-013's high end
  corrected_cal.phase_correction_rad = correction;
  const CycleMetrics corrected =
      last_cycle_with_calibration("inductive", corrected_cal);

  // cycle.cpp: phi1_corrected = phi1_measured - phase_correction_rad, so
  //   Q1_corrected = V1*I1*sin(phi1_measured - correction)
  //                = Q1_baseline*cos(correction) - P_baseline*sin(correction)
  const double want =
      baseline.q1 * std::cos(correction) - baseline.p * std::sin(correction);
  TEST_ASSERT_DOUBLE_WITHIN(std::fabs(want) * 0.01 + 0.1, want, corrected.q1);

  // Sign sanity, independent of the exact magnitude above: inductive's
  // phi1 is a moderate lag (pf_disp ~= 0.81, so phi1 ~= 35 degrees), well
  // short of 90 degrees, so subtracting a further-positive correction must
  // move Q1 down, not up.
  TEST_ASSERT_TRUE_MESSAGE(
      corrected.q1 < baseline.q1,
      "a positive phase_correction_rad must lower Q1 for a lagging load");
}

/// The same correction must reach P, not only Q1. The time-domain integral
/// carries the instrument's phase error in its fundamental term,
/// V1*I1*cos(phi1), and a correction that stopped at Q1 left P reading 6%
/// low on a resistive load and 23% low on the fan once the voltage channel's
/// 19.9-degree RC lag was counted (cycle.cpp).
///
/// phi1 and V1*I1 are recovered from the BASELINE run's own outputs --
/// cos(phi1) is its pf_disp, and V1*I1 = Q1 / sin(phi1) -- which predicts
/// the corrected run's P exactly:
///
///   P_corrected = P_baseline + V1*I1*(cos(phi1 - c) - cos(phi1))
///
/// On this fixture that step is +1.14 W. Deleting the correction misses it
/// by all of that, and reversing its sign by twice that: both are seven
/// orders of magnitude outside a tolerance that only has to absorb rounding.
static void test_phase_correction_moves_p_by_the_exact_trig_relation() {
  const CycleMetrics baseline =
      last_cycle_with_calibration("inductive", default_calibration());

  CalibrationSet corrected_cal = default_calibration();
  const double correction = 0.0349;
  corrected_cal.phase_correction_rad = correction;
  const CycleMetrics corrected =
      last_cycle_with_calibration("inductive", corrected_cal);

  // A lagging load's phi1 lies in (0, 90) degrees, so sin(phi1) is the
  // positive root.
  const double cos_phi = baseline.pf_disp;
  const double sin_phi = std::sqrt(1.0 - cos_phi * cos_phi);
  const double v1_i1 = baseline.q1 / sin_phi;
  const double cos_corrected =
      cos_phi * std::cos(correction) + sin_phi * std::sin(correction);
  const double want = baseline.p + v1_i1 * (cos_corrected - cos_phi);
  TEST_ASSERT_DOUBLE_WITHIN(std::fabs(want) * 1e-9 + 1e-9, want, corrected.p);

  // Subtracting a positive correction moves a lagging phi1 toward zero, so
  // the fundamental's cos(phi1) -- and with it P -- must rise.
  TEST_ASSERT_TRUE_MESSAGE(
      corrected.p > baseline.p,
      "a positive phase_correction_rad must raise P for a lagging load");
}

/// A clipped voltage channel is reported on the window it happened in, is
/// cleared for the next one, and never gets the window rejected: rejecting
/// would turn a mis-sized divider -- which clips every cycle -- into a
/// silent chain that looks like an idle circuit from the bench.
///
/// A square-wave "voltage" is enough, as in the short-window test above:
/// push() only needs zero crossings. The first window's positive half sits
/// on the top rail; every later one stops at 3000 counts.
static void test_voltage_clipping_is_reported_per_window_and_not_rejected() {
  CycleProcessor processor;
  CycleMetrics metrics{};
  std::vector<CycleMetrics> windows;
  for (int n = 0; n < 200; ++n) {
    SampleSet set{};
    const bool positive_half = (n % 40) < 20;
    set.v = positive_half ? (n < 60 ? 4095 : 3000) : 1000;
    set.i_low = 2048;
    set.i_high = 2048;
    if (processor.push(set, metrics)) windows.push_back(metrics);
  }

  TEST_ASSERT_EQUAL_INT_MESSAGE(3, static_cast<int>(windows.size()),
                                "crossings at 40, 80, 120 and 160");
  TEST_ASSERT_TRUE_MESSAGE(windows[0].v_clipped,
                           "the window spanning samples 40-79 hits 4095");
  TEST_ASSERT_TRUE_MESSAGE(windows[0].valid, "reported, not rejected");
  TEST_ASSERT_FALSE_MESSAGE(windows[1].v_clipped,
                            "the flag must not stick past its window");
  TEST_ASSERT_FALSE(windows[2].v_clipped);

  // And the bottom rail counts as well as the top one.
  CycleProcessor bottom;
  bool completed = false;
  for (int n = 0; n < 90 && !completed; ++n) {
    SampleSet set{};
    set.v = ((n % 40) < 20) ? 3000 : 0;
    set.i_low = 2048;
    set.i_high = 2048;
    completed = bottom.push(set, metrics);
  }
  TEST_ASSERT_TRUE_MESSAGE(completed, "expected a window to complete");
  TEST_ASSERT_TRUE_MESSAGE(metrics.v_clipped, "0 counts is a rail too");
}

/// Runs a fixture with each channel's raw counts shifted by a constant -- what
/// a bias rail away from kAdcMid does to them -- and returns the last cycle.
static CycleMetrics last_cycle_with_bias_offset(const std::string& name,
                                                int dv, int di_low,
                                                int di_high) {
  FileSampleSource source(fixture(name).c_str());
  TEST_ASSERT_TRUE_MESSAGE(source.ok(), "fixture missing");
  CycleProcessor processor;
  CycleMetrics metrics{};
  CycleMetrics latest{};
  bool any = false;
  SampleSet set{};
  while (source.next(set)) {
    set.v = static_cast<uint16_t>(set.v + dv);
    set.i_low = static_cast<uint16_t>(set.i_low + di_low);
    set.i_high = static_cast<uint16_t>(set.i_high + di_high);
    if (processor.push(set, metrics)) {
      latest = metrics;
      any = true;
    }
  }
  TEST_ASSERT_TRUE_MESSAGE(any, "no complete cycle");
  TEST_ASSERT_TRUE_MESSAGE(latest.valid, "no complete cycle");
  return latest;
}

/// The prototype board's bias rail sat ~280 counts above kAdcMid.
/// A clamp passes no DC and mains carries none, so an offset bias must leave
/// every metric where it was. Each channel gets a different offset, so a
/// mean taken from the wrong channel fails too. Both fixtures are exactly
/// periodic at 80 samples, so the shifted run's windows -- which start at a
/// different phase, because the crossing is still found against kAdcMid --
/// still span one exact period, and only rounding separates the two runs.
///
/// With kAdcMid assumed instead of the measured mean, inductive's P gains
/// (282 * 0.2006 V) * (250 * 0.0040283 A) = 57 W on its true 47 W, its Irms
/// goes from 0.24 A to 1.04 A, and its Vrms from 240 V to 247 V: each is
/// thousands of times these tolerances.
static void test_bias_offset_does_not_move_the_metrics() {
  for (const char* name : {"inductive", "switch-mode"}) {
    const CycleMetrics centred = last_cycle(name);
    const CycleMetrics offset =
        last_cycle_with_bias_offset(name, 282, 250, 300);
    TEST_ASSERT_TRUE_MESSAGE(offset.range == centred.range, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-6 * centred.vrms + 1e-9,
                                      centred.vrms, offset.vrms, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-6 * centred.irms + 1e-9,
                                      centred.irms, offset.irms, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-6 * std::fabs(centred.p) + 1e-9,
                                      centred.p, offset.p, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-6 * std::fabs(centred.q1) + 1e-9,
                                      centred.q1, offset.q1, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-6 * centred.dist + 1e-6,
                                      centred.dist, offset.dist, name);
    TEST_ASSERT_DOUBLE_WITHIN_MESSAGE(1e-6 * centred.h3_h1 + 1e-9,
                                      centred.h3_h1, offset.h3_h1, name);
  }
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_window_is_exactly_one_period);
  RUN_TEST(test_vrms_matches_the_sidecar);
  RUN_TEST(test_real_power_matches_the_sidecar);
  RUN_TEST(test_real_power_matches_the_sidecar_for_every_fixture);
  RUN_TEST(test_q1_matches_the_sidecar_for_every_fixture);
  RUN_TEST(test_metrics_are_stable_across_cycles);
  RUN_TEST(test_resistive_load_has_near_zero_q1);
  RUN_TEST(test_inductive_load_has_positive_q1);
  RUN_TEST(test_q1_matches_the_sidecar_for_an_inductive_load);
  RUN_TEST(test_switch_mode_load_has_distortion);
  RUN_TEST(test_dist_cross_derivation);
  RUN_TEST(test_distorted_voltage_uses_the_general_formula);
  RUN_TEST(test_harmonic_ratios_match_the_sidecar);
  RUN_TEST(test_harmonic_ratios_are_stable_across_cycles);
  RUN_TEST(test_interpolated_frequency_beats_an_integer_count);
  RUN_TEST(test_nominal_frequency);
  RUN_TEST(test_pf_true_never_exceeds_pf_disp_for_a_distorting_load);
  RUN_TEST(test_range_low_for_a_small_load);
  RUN_TEST(test_range_high_when_low_clips);
  RUN_TEST(test_range_is_consistent_across_cycles);
  RUN_TEST(test_idle_fixture_is_all_zero_current);
  RUN_TEST(test_frequency_sweep_does_not_desynchronise_the_window);
  RUN_TEST(test_short_window_is_rejected_as_invalid);
  RUN_TEST(test_both_channels_clipped_is_rejected_but_one_channel_is_not);
  RUN_TEST(test_set_calibration_scales_volts_and_both_current_ranges);
  RUN_TEST(test_phase_correction_moves_q1_by_the_exact_trig_relation);
  RUN_TEST(test_phase_correction_moves_p_by_the_exact_trig_relation);
  RUN_TEST(test_voltage_clipping_is_reported_per_window_and_not_rejected);
  RUN_TEST(test_bias_offset_does_not_move_the_metrics);
  return UNITY_END();
}
