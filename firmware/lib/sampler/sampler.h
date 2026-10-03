#pragma once

#include "adc_sample_source.h"
#include "sample_source.h"

/// Sentinel for periods_late()'s prev_tick_us: "no previous tick exists
/// yet." esp_timer_get_time() never returns a negative value, so -1 is
/// unambiguous and needs no separate has-previous flag.
///
/// `static`, not C++17's `inline`: despite this project's -std=c++17, the
/// espressif32/Arduino platform's own build script appends -std=gnu++11
/// after this project's build_flags on the esp32-s3 command line, and the
/// later flag wins, so esp32-s3 compiles under gnu++11 in practice. `static`
/// at namespace scope (internal linkage, one copy per translation unit) is
/// the pre-C++17-safe way to write a header-only constant, and is correct
/// here regardless: this constant is only ever used by value, never by
/// address, so per-TU duplication is not observable.
static constexpr int64_t kNoPreviousTick = -1;

/// How many whole sample periods late a callback tick arrived, given the
/// timestamp of the previous tick, the timestamp of this tick, the expected
/// period, and a jitter tolerance -- all in microseconds, as returned by
/// esp_timer_get_time().
///
/// Returns 0 when the tick is on time: the interval since the previous tick
/// is no more than `period_us + tolerance_us`. This also covers the very
/// first tick ever seen (prev_tick_us == kNoPreviousTick) -- there is
/// nothing to compare it against, so it cannot be a slip.
///
/// Returns >=1 otherwise: the interval overshoots the expected period by
/// more than the tolerance, which is real schedule slippage rather than
/// jitter. The value is how many period-widths that overshoot spans,
/// rounded up.
///
/// Pure arithmetic, so it is tested on the laptop -- getting it wrong on
/// the bench looks exactly like the fault it exists to catch, or worse,
/// like the confident absence of one.
uint32_t periods_late(int64_t prev_tick_us, int64_t this_tick_us,
                      uint32_t period_us, uint32_t tolerance_us);

/// Timer-driven acquisition into a ring buffer.
///
/// The callback performs the SPI reads (about 72 us) and pushes; the main
/// loop runs the DSP. It runs under esp_timer's ESP_TIMER_TASK dispatch --
/// one dedicated FreeRTOS task, not true interrupt context (see sampler.cpp
/// for why) -- so two invocations of the callback can never overlap; there
/// is no reentrancy for a guard to catch. Instead, timing margin is PROVEN
/// (US70) by timestamping every entry and comparing the gap against the
/// previous entry to the expected 250 us cadence (periods_late(), above).
/// A zero isr_overruns count from the hour-long soak means "no callback
/// ever arrived late by more than the tolerance" -- not "no callback ever
/// re-entered itself," which this dispatch method makes impossible by
/// construction, regardless of scheduling health.
class Sampler {
 public:
  static constexpr int kRingSize = 512;
  static constexpr uint32_t kPeriodMicros = 250;  // 4 kSPS

  // Tolerance for periods_late(), in microseconds: 20% of the 250 us
  // period. ESP_TIMER_TASK dispatch hands off from a hardware-timer ISR to
  // a dedicated, high-priority FreeRTOS task through a queue; that hand-off
  // carries a small, genuine scheduling latency (typically low single-digit
  // microseconds, occasionally more under contention from other
  // interrupts) that is not evidence anything is wrong. 50 us sits
  // comfortably above that ordinary jitter -- so it will not fire on noise
  // -- while staying well under one full period, so a real slip (the
  // dispatch task actually being starved) still trips the counter rather
  // than being absorbed. It also leaves most of the ~178 us of slack the
  // 72 us SPI read has within a 250 us period unclaimed by tolerance alone.
  static constexpr uint32_t kLateToleranceMicros = 50;

  void begin(AdcSampleSource* adc);
  void stop();

  /// True if esp_timer_create() or esp_timer_start_periodic() failed during
  /// the last begin(). A sampler that silently never starts looks exactly
  /// like a wiring fault on the bench, so this must be checked, not assumed.
  bool timer_start_failed() const;

  /// Pops one sample set. False when the ring is empty.
  bool pop(SampleSet& out);

  uint32_t isr_overruns() const;
  uint32_t worst_isr_us() const;
  uint32_t cycles_dropped() const;
  void reset_health();
};
