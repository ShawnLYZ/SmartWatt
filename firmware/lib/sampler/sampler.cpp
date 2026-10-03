#include "sampler.h"

uint32_t periods_late(int64_t prev_tick_us, int64_t this_tick_us,
                      uint32_t period_us, uint32_t tolerance_us) {
  if (prev_tick_us == kNoPreviousTick) return 0;

  const int64_t interval = this_tick_us - prev_tick_us;
  if (interval <= 0) return 0;  // clock did not advance; nothing to report

  const int64_t period = static_cast<int64_t>(period_us);
  const int64_t late_us = interval - period;
  if (late_us <= static_cast<int64_t>(tolerance_us)) return 0;

  // Ceiling division: any overshoot beyond tolerance is at least one
  // period late, and a bigger overshoot counts proportionally more.
  return static_cast<uint32_t>((late_us + period - 1) / period);
}

#if SMARTWATT_NATIVE

void Sampler::begin(AdcSampleSource*) {}
void Sampler::stop() {}
bool Sampler::timer_start_failed() const { return false; }
bool Sampler::pop(SampleSet&) { return false; }
uint32_t Sampler::isr_overruns() const { return 0; }
uint32_t Sampler::worst_isr_us() const { return 0; }
uint32_t Sampler::cycles_dropped() const { return 0; }
void Sampler::reset_health() {}

#else

#include <Arduino.h>
#include <esp_timer.h>

namespace {

SampleSet g_ring[Sampler::kRingSize];
volatile int g_head = 0;
volatile int g_tail = 0;
volatile uint32_t g_overruns = 0;
volatile uint32_t g_worst_us = 0;
volatile uint32_t g_dropped = 0;
volatile int64_t g_prev_tick_us = kNoPreviousTick;
volatile bool g_timer_start_failed = false;

AdcSampleSource* g_adc = nullptr;
esp_timer_handle_t g_timer = nullptr;

void IRAM_ATTR on_tick(void*) {
  const int64_t now = esp_timer_get_time();

  // Real schedule-slippage detection, not reentrancy detection: a
  // reentrancy guard cannot fire under ESP_TIMER_TASK dispatch (see the
  // class comment in sampler.h for why), so it would read zero regardless
  // of whether the callback is keeping up. This is the counter that must
  // read zero over an hour, and it is never reset to flatter a number.
  if (periods_late(g_prev_tick_us, now, Sampler::kPeriodMicros,
                    Sampler::kLateToleranceMicros) > 0) {
    ++g_overruns;
  }
  g_prev_tick_us = now;

  SampleSet set{};
  if (g_adc != nullptr) g_adc->next(set);

  const int next = (g_head + 1) % Sampler::kRingSize;
  if (next == g_tail) {
    ++g_dropped;  // main loop is behind; drop rather than block the ISR
  } else {
    g_ring[g_head] = set;
    g_head = next;
  }

  const uint32_t elapsed =
      static_cast<uint32_t>(esp_timer_get_time() - now);
  if (elapsed > g_worst_us) g_worst_us = elapsed;
}

}  // namespace

void Sampler::begin(AdcSampleSource* adc) {
  g_adc = adc;
  g_head = g_tail = 0;
  g_prev_tick_us = kNoPreviousTick;
  reset_health();

  // Assigned by field name, not by designated initializer: this project's
  // build_flags specify -std=c++17, though esp32-s3's actual effective
  // dialect is gnu++11 (see kNoPreviousTick's comment in sampler.h for why
  // -- further still from what designated initializers need). Either way,
  // GCC's -Wpedantic correctly flags them as unavailable short of C++20.
  // Field-by-field assignment sidesteps the warning under any of these
  // standards and, as a bonus, is immune to esp_timer_create_args_t's
  // member order ever changing.
  esp_timer_create_args_t args{};
  args.callback = &on_tick;
  args.arg = nullptr;
  // The brief called for ESP_TIMER_ISR here. That identifier is only
  // declared under CONFIG_ESP_TIMER_SUPPORTS_ISR_DISPATCH_METHOD, which
  // stock Arduino-ESP32 does not define, so the symbol does not exist
  // on this core -- this is not a style choice, it is a hard compile
  // error otherwise. ESP_TIMER_TASK is also the only context in which
  // on_tick() is actually legal: it performs an SPI transaction, and
  // SPI takes a mutex, which true interrupt context can never do.
  args.dispatch_method = ESP_TIMER_TASK;
  args.name = "smartwatt_sampler";
  args.skip_unhandled_events = false;

  // A sampler that silently never starts looks exactly like a wiring fault
  // on the bench, so both return codes are checked rather than discarded;
  // the failure is surfaced through timer_start_failed() instead.
  const esp_err_t create_err = esp_timer_create(&args, &g_timer);
  if (create_err != ESP_OK) {
    g_timer_start_failed = true;
    return;
  }

  const esp_err_t start_err =
      esp_timer_start_periodic(g_timer, kPeriodMicros);
  g_timer_start_failed = (start_err != ESP_OK);
}

void Sampler::stop() {
  if (g_timer != nullptr) {
    esp_timer_stop(g_timer);
    esp_timer_delete(g_timer);
    g_timer = nullptr;
  }
  // The flag describes the last begin(). After a stop() there is no timer for
  // it to describe, so leaving it set would keep reporting a failure that has
  // been torn down -- and would go on reporting it right up until the next
  // begin() overwrote it.
  g_timer_start_failed = false;
}

bool Sampler::timer_start_failed() const { return g_timer_start_failed; }

bool Sampler::pop(SampleSet& out) {
  if (g_tail == g_head) return false;
  out = g_ring[g_tail];
  g_tail = (g_tail + 1) % kRingSize;
  return true;
}

uint32_t Sampler::isr_overruns() const { return g_overruns; }
uint32_t Sampler::worst_isr_us() const { return g_worst_us; }
uint32_t Sampler::cycles_dropped() const { return g_dropped; }

void Sampler::reset_health() {
  g_overruns = 0;
  g_worst_us = 0;
  g_dropped = 0;
}

#endif
