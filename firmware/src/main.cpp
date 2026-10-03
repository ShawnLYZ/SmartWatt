/// SmartWatt device entry point.
///
/// Everything this file calls has already been verified under `native`
/// against analytic fixtures. This file wires those verified pieces to real
/// hardware -- the signal chain, the classifier, the tracker, MQTT
/// publishing, and the bench's serial commands (CAPTURE for raw samples,
/// CAL and CAL? for the calibration in NVS) -- and nothing more.

#include <Arduino.h>
#include <LittleFS.h>

#include <sys/time.h>

#include "adc_sample_source.h"
#include "cal_command.h"
#include "console.h"
#include "cycle.h"
#include "detector.h"
#include "features.h"
#include "fingerprints.h"
#include "knn.h"
#include "net.h"
#include "nvs_calibration.h"
#include "payload.h"
#include "sampler.h"
#include "tracker.h"

namespace {

// ---- Signal chain (Tasks 3-5) ---------------------------------------------

AdcSampleSource g_adc({/*cs=*/10, /*sck=*/12, /*miso=*/13, /*mosi=*/11});
Sampler g_sampler;
CycleProcessor g_cycles;

/// The calibration g_cycles is running. Kept here because CycleProcessor
/// has no getter, and `CAL?` has to answer what is IN USE -- which after a
/// refused or failed CAL is not whatever was last typed.
CalibrationSet g_calibration = default_calibration();
bool g_calibration_from_nvs = false;

/// Windows whose VOLTAGE channel hit a rail, since boot. cycle.cpp reports
/// these rather than rejecting them (CycleMetrics::v_clipped), so this
/// count on the status line is where a divider sized for a different
/// adapter shows up. Never reset, like the sampler's counters: it has to
/// read 0, not be made to.
uint32_t g_v_clipped_windows = 0;

// ---- Event pipeline: detector, classifier, tracker -------------------------

// Shipped default (floor_w = 6.0). detector.h: "Measured detection floor.
// S6 measures this" -- that measurement is task-7-brief.md's Step 2, not
// this task's, so this stays the shipped default rather than an invented
// figure. Named once so the detector and every emitted
// EmitContext::floor_w (see device_context() below) read the SAME number,
// rather than two 6.0 literals that only happen to agree today.
//
// Empty braces, not a positional value list: DetectorConfig carries default
// member initialisers for every field, which (see payload.cpp's Writer
// constructor comment) disqualifies it from being a C++11 aggregate. Empty-
// brace value-initialisation only needs a default constructor -- which the
// compiler still provides implicitly -- so it is safe under the device
// build's effective gnu++11 regardless. A brace-init supplying explicit
// field values here would hit the exact trap Writer did.
const DetectorConfig kDetectorConfig{};
EventDetector g_detector(kDetectorConfig);

// Loaded in setup() from LittleFS; stays default-constructed (count == 0)
// if no table is present or it fails to load -- see the note there. Knn
// holds a REFERENCE to this, so it must outlive g_knn, which declaration
// order (this line precedes g_knn) guarantees for static-storage-duration
// objects in one translation unit.
FingerprintTable g_fingerprints;
bool g_have_fingerprints = false;
Knn g_knn(g_fingerprints);

Tracker g_tracker;

// ---- Networking -------------------------------------------------------------

Net g_net;

constexpr const char* kTelemetryTopic = "smartwatt/telemetry";
constexpr const char* kEventTopic = "smartwatt/event";

// ============================================================================
// YOUR WI-FI SETTINGS -- EDIT THE TWO LINES MARKED "CHANGE ME" BEFORE UPLOADING
// ============================================================================
//
// SmartWatt joins the Wi-Fi hotspot that your Windows laptop creates
// (Settings > Network & internet > Mobile hotspot). Type that hotspot's
// network name and password below, keeping the quote marks around them.
// README.md, "Part 4", walks through this.
//
// The four addresses are Windows Mobile Hotspot's own defaults: the laptop
// is always 192.168.137.1 on its hotspot, and it is the gateway, the MQTT
// broker and the time (SNTP) server all at once, because Mosquitto and the
// Windows time service both run on it. The device takes the fixed address
// .50 so the broker never has to discover it. Leave them alone unless you
// use a different router.
//
// The ESP32-S3 has no 5 GHz radio, so the hotspot's band must be 2.4 GHz.
//
// Never commit your real password to a public repository.
NetConfig g_net_config{
    "YOUR_HOTSPOT_NAME",      // CHANGE ME: the hotspot's network name
    "YOUR_HOTSPOT_PASSWORD",  // CHANGE ME: the hotspot's network password
    {192, 168, 137, 50},      // ip: this device
    {192, 168, 137, 1},       // gateway: the laptop
    {255, 255, 255, 0},       // mask
    {192, 168, 137, 1},       // broker: the laptop, running Mosquitto
    1883,                     // broker_port
};

// ---- Raw capture (Step 4) ---------------------------------------------------

// Matches capture_to_fixture.py's own --samples default, so the documented
// invocation (`--port COM5 --samples 4000 --out ...`) gets exactly as many
// rows as this dumps. A caller that overrides --samples upward would block
// on the tool's own read timeout once the device stops sending -- a known
// limit of a fixed on-device count, accepted because the tool never tells
// the device how many rows it wants (it just sends a bare "CAPTURE\n").
constexpr int kCaptureSamples = 4000;
SampleSet g_capture_buffer[kCaptureSamples];

// ---- Payload scratch, timing and sequence state -----------------------------

// Matches net.cpp's g_mqtt.setBufferSize(2048) exactly: sizing the scratch
// buffer emit_telemetry()/emit_event() write into any larger would let a
// payload "succeed" here only to be refused by PubSubClient::publish() a
// line later. The largest fixture payload in test_emit.cpp is well under a
// kilobyte even with several active entries, so this is headroom, not a
// tight fit.
constexpr int kPayloadCapacity = 2048;
char g_payload[kPayloadCapacity];

constexpr uint32_t kTelemetryPeriodMs = 1000;  // 1 Hz, per the contract.
uint32_t g_telemetry_at = 0;

// ---- Helpers ------------------------------------------------------------

/// FLOAT EPOCH SECONDS UTC, as every `ts`/`now` on the wire is documented to
/// be (payload.h, tracker.h).
///
/// The clock is set by SNTP, started from the MQTT broker's own IP once the
/// station associates (net.cpp's Net::loop()). That server is the laptop
/// hosting the hotspot -- the same machine Mosquitto runs on -- so it needs
/// no internet uplink, and the argument is an IP literal, so US72's "nothing
/// resolves a hostname" still holds exactly.
///
/// UNTIL THAT LANDS THIS RETURNS SECONDS SINCE BOOT: an ESP32 counts from 0
/// at reset. That is a small number, not a plausible-looking wrong date --
/// but it is still not publishable, and nothing publishes it. See
/// publishing_permitted() below for what it would do to the server if it
/// ever reached the wire.
///
/// gettimeofday() over time() for the fractional part: event timestamps are
/// documented to sub-second precision (e.g. the contract's 1754035188.412)
/// and time() only has whole seconds.
double now_seconds() {
  timeval tv;
  gettimeofday(&tv, nullptr);
  return static_cast<double>(tv.tv_sec) + static_cast<double>(tv.tv_usec) / 1e6;
}

/// How often the "clock not set" and "no broker" lines may repeat. Telemetry
/// ticks at 1 Hz, so an ungated warning would be one line a second forever;
/// five seconds keeps the state impossible to miss without burying the
/// metrics line it sits beside.
constexpr uint32_t kWarnPeriodMs = 5000;

/// Whether this device is allowed to stamp and publish a frame yet.
///
/// FALSE UNTIL SNTP HAS SET THE CLOCK, and loudly so. The choice here is
/// between publishing an uptime-stamped frame and withholding it; withholding
/// wins, because the server cannot tell the two apart and every consequence
/// of guessing wrong is silent:
///
///   - /api/series filters telemetry_since(now - minutes*60): an uptime `ts`
///     is ~1.7e9 seconds in the past, so the series is EMPTY.
///   - /api/ledger and /api/month use calendar bounds: also EMPTY, which
///     takes cost and carbon -- the project's headline outputs -- with them.
///   - server/smartwatt_server/api.py's _retention_loop computes
///     `cutoff = time.time() - hz_retention_s` and calls purge_hz(cutoff):
///     EVERY device row is older than any such cutoff, so the first pass
///     DELETES THE LOT.
///   - contract/schemas/telemetry.schema.json types `ts` as a bare number
///     with no minimum, so nothing anywhere rejects it, and /api/latest keeps
///     working -- which makes a casual check look healthy while the data is
///     being thrown away.
///
/// A frame withheld is one visibly missing thing with one serial line naming
/// the cause. A frame published with a wrong-but-well-formed timestamp is
/// data that looks real, is silently discarded downstream, and takes the
/// dashboard's whole history with it. The project's discipline -- nothing is
/// reported as measured that was not measured -- points the same way: a
/// timestamp is a measurement of when, and this device does not know when
/// until SNTP tells it.
///
/// The DSP is entirely unaffected: sampling, cycle metrics, detection,
/// classification and tracking all keep running with no network and no clock
/// at all, and maybe_report()'s once-a-second serial line keeps printing. It
/// is only the publish that waits.
bool publishing_permitted(double ts) {
  static bool s_warned = false;
  static uint32_t s_warned_at = 0;

  if (clock_is_set(ts)) {
    if (s_warned) {
      s_warned = false;
      Serial.printf(
          "Clock: SET (ts=%.3f) -- publishing telemetry and events\n", ts);
    }
    return true;
  }

  const uint32_t now_ms = millis();
  if (!s_warned || now_ms - s_warned_at >= kWarnPeriodMs) {
    s_warned = true;
    s_warned_at = now_ms;
    Serial.printf(
        "WITHHOLDING PUBLISH: clock NOT set. ts would be %.3f, which is "
        "uptime, not UTC. Waiting for SNTP from the broker host "
        "%u.%u.%u.%u. Sampling and the DSP are running normally.\n",
        ts, static_cast<unsigned int>(g_net_config.broker[0]),
        static_cast<unsigned int>(g_net_config.broker[1]),
        static_cast<unsigned int>(g_net_config.broker[2]),
        static_cast<unsigned int>(g_net_config.broker[3]));
  }
  return false;
}

/// Publishes g_payload and says so when it does not go out.
///
/// The return value used to be discarded, which made a refused publish
/// completely silent. Two causes, reported differently because they are not
/// equally interesting: with no broker connection this is the expected state
/// at power-on before Mosquitto is up, so it is rate-limited rather than
/// repeated every second forever; with a live connection a refusal means
/// PubSubClient rejected the payload itself (its 2048-byte buffer, matched
/// exactly by kPayloadCapacity above), which is a defect and is printed every
/// single time. The worst-case telemetry frame is ~1730 bytes against 2022
/// usable, so that second line should never appear -- if it ever does, the
/// payload has grown past what the transport can carry.
void publish_or_report(const char* topic, int bytes) {
  if (g_net.publish(topic, g_payload)) return;

  if (g_net.connected()) {
    Serial.printf(
        "MQTT PUBLISH REFUSED on %s with a live connection: %d bytes "
        "rejected by PubSubClient (buffer 2048)\n",
        topic, bytes);
    return;
  }

  static bool s_warned = false;
  static uint32_t s_warned_at = 0;
  const uint32_t now_ms = millis();
  if (!s_warned || now_ms - s_warned_at >= kWarnPeriodMs) {
    s_warned = true;
    s_warned_at = now_ms;
    Serial.printf("MQTT publish skipped on %s (%d bytes): no broker\n", topic,
                  bytes);
  }
}

/// One counter shared across BOTH topics, matching the reference publisher
/// (sim/smartwatt_sim/payload.py's SimState.seq is one counter fed to both
/// build_telemetry and build_event) -- not one counter per topic. The
/// architecture spec's error-handling table reads a gap in this number as
/// a dropped message, which only holds if silence on one topic still
/// advances the number the other topic's next message carries.
int next_seq() {
  static int seq = 0;
  return seq++;
}

/// Assembles the wire HealthCounters from the sampler's three accessors.
///
/// The narrowing this does -- uint32_t from Sampler to int in
/// HealthCounters -- happens here and only here, deliberately: Sampler has
/// no health() method (payload.h's HealthCounters is an emit/ type, and a
/// Sampler that returned one would make lib/sampler depend on lib/emit and
/// drag the classifier and tracker into the sampler build).
///
/// The headroom, stated correctly: at 4 kSPS a counter incremented on EVERY
/// sample reaches INT_MAX after 2^31 / 4000 = 536871 s, about 6.2 DAYS of
/// continuous uptime. (An earlier version of this comment claimed "over a
/// year", which was wrong by a factor of about 59 -- the kind of number this
/// project does not get to be casual about.) Even that figure is the extreme
/// case: it needs cycles_dropped to tick on every single sample, i.e. a ring
/// that never drains for 6.2 days. isr_overruns cannot approach it (it counts
/// only late ticks) and worst_isr_us is a microsecond duration bounded by the
/// callback body. Past INT_MAX the cast is implementation-defined -- GCC wraps
/// to a negative number, which is ugly in a telemetry frame but is not
/// corruption -- and the two counters that can move at all are now cleared at
/// the points that legitimately poison them (handle_capture(), and the first
/// broker connection in service_network()).
HealthCounters sampler_health() {
  HealthCounters health;
  health.isr_overruns = static_cast<int>(g_sampler.isr_overruns());
  health.worst_isr_us = static_cast<int>(g_sampler.worst_isr_us());
  health.cycles_dropped = static_cast<int>(g_sampler.cycles_dropped());
  return health;
}

/// The EmitContext fields common to every device-sourced frame.
///
/// TAKES A POINTER, not a reference, on purpose: EmitContext stores the
/// ADDRESS of these counters, so they must outlive the emit_*() call the
/// context is used for. With a reference parameter, `device_context(
/// sampler_health())` compiled happily and stored the address of a temporary
/// that died at the end of that full-expression -- a dangling read waiting
/// for someone to write the obvious-looking call. Every caller today keeps a
/// named local, so nothing dangles now; requiring an explicit `&` at the call
/// site means nothing can, because the address of a prvalue cannot be taken.
///
/// wh_session/wh_today are left at EmitContext's own defaults (0.0): no
/// energy accumulator exists anywhere under firmware/lib for this file to
/// wire up (unlike the pieces task-6-brief.md's Step 2 names -- Tracker,
/// Knn, EventDetector -- which are), and the brief's enumeration of what
/// Step 2 wires does not mention one. Building a new integrator here would
/// be exactly the "new logic" Step 2 says this file is not. Flagged in
/// task-6-report.md: the server-side ledger (server/smartwatt_server/
/// ledger.py, store.py) treats these fields as authoritative, so they read
/// zero until a later task adds the accumulator.
EmitContext device_context(const HealthCounters* health) {
  EmitContext ctx;
  ctx.source = "device";
  ctx.seq = next_seq();
  ctx.fingerprint_id =
      g_have_fingerprints ? g_fingerprints.fingerprint_id : nullptr;
  ctx.floor_w = kDetectorConfig.floor_w;
  ctx.health = health;
  return ctx;
}

void publish_telemetry(double ts, const CycleMetrics& metrics) {
  // Checked before anything is built: an unset clock means this frame is not
  // publishable at all, and next_seq() must not burn a sequence number on a
  // frame that was never sent (the architecture spec reads a gap in that
  // number as a dropped message).
  if (!publishing_permitted(ts)) return;

  const HealthCounters health = sampler_health();
  const EmitContext ctx = device_context(&health);
  const int written =
      emit_telemetry(g_payload, kPayloadCapacity, ts, metrics, g_tracker, ctx);
  // -1 means nothing was written (payload.h) -- never trust strlen() over
  // this, and never hand PubSubClient a stale or partial g_payload.
  if (written > 0) publish_or_report(kTelemetryTopic, written);
}

void publish_event(double ts, const DetectedEvent& event,
                   const Classification& classification,
                   const Attribution& attribution,
                   const FeatureVector& features) {
  if (!publishing_permitted(ts)) return;

  const HealthCounters health = sampler_health();
  const EmitContext ctx = device_context(&health);
  const int written = emit_event(g_payload, kPayloadCapacity, ts, event,
                                 classification, attribution, features, ctx);
  if (written > 0) publish_or_report(kEventTopic, written);
}

/// The once-a-second bench status line (Tasks 4-5) plus the 1 Hz telemetry
/// publish, gated together: both are "the once-a-second tick" and a second
/// independent millis() gate for the same period would just be two clocks
/// that happen to agree, not two different things.
void maybe_report(double now, const CycleMetrics& metrics) {
  if (millis() - g_telemetry_at >= kTelemetryPeriodMs) {
    g_telemetry_at = millis();
    // firmware/tools/serial_stats.py parses this line, and
    // firmware/tests_py/test_bench_tools.py renders THIS format string
    // through that parser -- change one and the test says so.
    Serial.printf(
        "Vrms=%7.2f  Irms=%7.4f  P=%9.2f  Q1=%8.2f  D=%7.2f  "
        "PF=%5.3f  f=%6.3f  range=%s  overruns=%lu  worst=%luus  vclip=%lu\n",
        metrics.vrms, metrics.irms, metrics.p, metrics.q1, metrics.dist,
        metrics.pf_true, metrics.freq,
        metrics.range == Range::High ? "high" : "low",
        (unsigned long)g_sampler.isr_overruns(),
        (unsigned long)g_sampler.worst_isr_us(),
        (unsigned long)g_v_clipped_windows);
    publish_telemetry(now, metrics);
  }
}

/// Runs one completed cycle through the event pipeline and the 1 Hz gate.
///
/// observe_total() MUST run every cycle (tracker.h) -- even one with no
/// settled edge -- so the residual stays live between events rather than
/// only updating when something switches. On a cycle that DOES settle an
/// edge, it must run AFTER apply(): apply() is what changes the active
/// set, and observe_total() is the only thing that recomputes the residual
/// from it, so calling them in the other order would let this cycle's
/// telemetry (from maybe_report(), right after) carry a residual computed
/// against the PREVIOUS active set -- tracker.h documents the exact
/// failure this produced once: an active kettle beside a residual reading
/// 0.0000 where the true figure was -700 W.
void handle_cycle(double now, const CycleMetrics& metrics) {
  if (metrics.v_clipped) ++g_v_clipped_windows;

  DetectedEvent event{};
  if (g_detector.push(metrics, event)) {
    const FeatureVector features = extract(event);
    const Classification classification = g_knn.classify(features);
    const Attribution attribution = g_tracker.apply(event, classification, now);
    g_tracker.observe_total(metrics.p, now);
    publish_event(now, event, classification, attribution, features);
  } else {
    g_tracker.observe_total(metrics.p, now);
  }
  maybe_report(now, metrics);
}

/// Returns the just-completed Serial command line (NUL terminated), or
/// nullptr if none has arrived yet -- across as many loop() calls as the
/// bytes take. The returned pointer is only valid until the next call.
///
/// The line editing is CommandLine's (lib/console, tested natively). What
/// is left here is saying so when a line was too long: it is discarded
/// whole, and a silently dropped command would look like one that ran.
const char* poll_serial_command() {
  static CommandLine s_line;
  while (Serial.available() > 0) {
    switch (s_line.feed(static_cast<char>(Serial.read()))) {
      case CommandLine::Result::Complete:
        return s_line.line();
      case CommandLine::Result::Overflowed:
        Serial.printf(
            "Command longer than %d characters -- ignored, nothing done\n",
            CommandLine::kCapacity - 1);
        break;
      case CommandLine::Result::Pending:
        break;
    }
  }
  return nullptr;
}

/// Dumps kCaptureSamples raw `v,i_low,i_high` lines: what
/// capture_to_fixture.py turns into an S1-format fixture CSV for the
/// capture-and-replay equivalence test (task-6-brief.md Step 5).
///
/// ACQUIRES INTO A BUFFER FIRST, PRINTS AFTER -- deliberately not a
/// pop-and-print loop reading the live ring. At 4 kSPS the sampler
/// produces far faster than 115200 baud can carry (ruling C5): 4000
/// samples/s of ~15-byte lines needs roughly 480 kbps of payload alone,
/// well over what 115200 baud (~11.5 kB/s) can drain. A print-as-you-pop
/// loop would starve behind that bottleneck, the ring would overflow
/// almost immediately, and the "capture" would actually be a heavily
/// decimated, gappy sample of whatever the ring happened to hold at each
/// print -- not the contiguous 4 kHz stream CycleProcessor's zero-crossing
/// windowing assumes, which would silently invalidate the equivalence
/// test this exists for. Draining the ring into g_capture_buffer first
/// takes about one second (4000 samples at the sampler's true 250 us
/// period) and is genuinely contiguous; the ~6 seconds 115200 baud then
/// needs to print it out is just transfer time for an already-correct
/// buffer, which is fine (ruling C5's own arithmetic).
void handle_capture() {
  for (int i = 0; i < kCaptureSamples; ++i) {
    while (!g_sampler.pop(g_capture_buffer[i])) {
      yield();  // let WiFi/lwIP and the idle task run while waiting.
    }
  }
  for (int i = 0; i < kCaptureSamples; ++i) {
    const SampleSet& s = g_capture_buffer[i];
    Serial.printf("%u,%u,%u\n", static_cast<unsigned int>(s.v),
                  static_cast<unsigned int>(s.i_low),
                  static_cast<unsigned int>(s.i_high));
  }

  // The print loop above never drains the ring for the ~6 seconds it runs, so
  // the sampler has just incremented cycles_dropped roughly 24000 times --
  // known, deliberate, and nothing to do with the health of the system. Left
  // alone it would ride in telemetry.health (and out to /api/health) for the
  // rest of this boot, since reset_health() is otherwise only called from
  // Sampler::begin(). Cleared here so the counter means "samples lost because
  // the DSP fell behind", which is the only reading of it that is useful.
  g_sampler.reset_health();
  Serial.println(
      "CAPTURE complete -- sampler health counters cleared (the capture's own "
      "~24000 drops are an artefact of the print, not a fault)");
}

/// Prints a calibration set on one line, after `heading`.
///
/// %.9g rather than fixed decimals: nine significant digits reproduce
/// anything typed into CAL exactly, so a read-back shows the number that was
/// stored instead of a rounded one that looks like a different trim. The
/// equivalence test (calibration-log.md, Task 6) copies its constants off
/// the boot line, whose "Calibration: loaded from NVS" heading it searches
/// for.
void print_calibration(const char* heading, const CalibrationSet& cal) {
  Serial.printf(
      "%s, stored_at=%lu  v_cal=%.9g  i_cal_low=%.9g  i_cal_high=%.9g  "
      "phase_correction_rad=%.9g\n",
      heading, (unsigned long)cal.stored_at, cal.v_cal, cal.i_cal_low,
      cal.i_cal_high, cal.phase_correction_rad);
}

void print_calibration_in_use() {
  print_calibration(g_calibration_from_nvs
                        ? "Calibration: in use, from NVS"
                        : "Calibration: in use, compile-time defaults "
                          "(nothing stored in NVS)",
                    g_calibration);
}

bool same_calibration(const CalibrationSet& a, const CalibrationSet& b) {
  return a.v_cal == b.v_cal && a.i_cal_low == b.i_cal_low &&
         a.i_cal_high == b.i_cal_high &&
         a.phase_correction_rad == b.phase_correction_rad &&
         a.stored_at == b.stored_at;
}

/// `CAL <v_cal> <i_cal_low> <i_cal_high> <phase_rad>`: stores a trimmed
/// calibration in NVS and switches the running signal chain to it.
///
/// What goes live is what NVS READS BACK, and only when it matches what was
/// written. The running chain, the next boot and the line printed here are
/// then the same set by construction. A write that did not land is refused
/// outright rather than applied from RAM, where it would look stored until
/// the next power cycle quietly reverted it.
///
/// The processor and the detector restart after the switch. A trim rescales
/// every later P reading, and to the detector a 2% trim on a running kettle
/// is a 36 W step: a switching edge that never happened.
void handle_cal(const char* args) {
  CalibrationSet requested = g_calibration;
  const char* refusal = parse_calibration_args(args, requested);
  if (refusal != nullptr) {
    Serial.printf("CAL REFUSED: %s. Nothing stored.\n", refusal);
    print_calibration_in_use();
    return;
  }

  // WHEN this was stored, which the device only knows once SNTP has set its
  // clock. Before that it is 0 -- unknown, and said so below -- never
  // uptime passed off as a date.
  const double now = now_seconds();
  requested.stored_at = clock_is_set(now) ? static_cast<uint32_t>(now) : 0;

  CalibrationSet stored{};
  if (!save_calibration(requested) || !load_calibration(stored) ||
      !same_calibration(requested, stored)) {
    Serial.println(
        "CAL FAILED: the NVS write or its read-back did not match, so nothing "
        "was applied. NVS may now hold a partial write -- the next boot's "
        "Calibration line shows what it holds.");
    print_calibration_in_use();
    return;
  }

  g_calibration = stored;
  g_calibration_from_nvs = true;
  g_cycles.set_calibration(stored);
  g_cycles.reset();
  g_detector.reset();
  print_calibration("CAL: stored in NVS and applied", stored);
  if (stored.stored_at == 0) {
    Serial.println(
        "  stored_at=0 because the clock is not set: WHEN this was stored is "
        "unknown. Send the same CAL again once the clock is set to stamp it.");
  }
}

/// Runs one bench command. Which command a line is -- exact words, see
/// parse_command() -- is decided in lib/console, where it is tested.
void handle_command(const char* line) {
  const Command command = parse_command(line);
  switch (command.kind) {
    case CommandKind::Capture:
      handle_capture();
      break;
    case CommandKind::CalQuery:
      print_calibration_in_use();
      break;
    case CommandKind::Cal:
      handle_cal(command.args);
      break;
    case CommandKind::Unknown:
      Serial.printf(
          "Unknown command '%s'. Commands: CAPTURE | CAL? | "
          "CAL <v_cal> <i_cal_low> <i_cal_high> <phase_rad>\n",
          line);
      break;
  }
}

/// Services the network, and clears the sampler's health counters once, on
/// the FIRST successful broker connection after boot.
///
/// Why the counters need clearing at all: before Mosquitto is up, every
/// PubSubClient::connect() attempt blocks on the socket (bounded to 2 s by
/// net.cpp's setSocketTimeout, but still 2 s), during which the 512-entry ring
/// overflows and thousands of samples are dropped. Those drops are an artefact
/// of waiting for a broker, and once counted they never go away.
///
/// Why ONLY the first connection, and why that cannot mask a real problem: a
/// reset on every reconnect would erase evidence. The Task 7 soak runs an hour
/// with the publishing path live and reads isr_overruns and worst_isr_us at
/// the end; if the broker blipped at minute 30 and this cleared the counters,
/// the reader would see half an hour of history and believe it was a full one
/// -- the exact shape of failure this project refuses. So a later reconnect
/// clears nothing and prints the counters instead: the drops it would have
/// erased are real losses of real samples, and belong in the number. The one
/// window that is genuinely not steady-state health is the pre-broker window
/// at power-on, and that is the only one cleared.
void service_network() {
  g_net.loop();

  static bool s_was_connected = false;
  static bool s_cleared_once = false;
  const bool connected_now = g_net.connected();

  if (connected_now && !s_was_connected) {
    if (!s_cleared_once) {
      s_cleared_once = true;
      Serial.printf(
          "Broker connected: clearing power-on health counters "
          "(isr_overruns=%lu worst=%luus cycles_dropped=%lu) -- they cover "
          "the wait for the broker, not steady-state health\n",
          (unsigned long)g_sampler.isr_overruns(),
          (unsigned long)g_sampler.worst_isr_us(),
          (unsigned long)g_sampler.cycles_dropped());
      g_sampler.reset_health();
    } else {
      Serial.printf(
          "Broker reconnected: health counters left INTACT "
          "(isr_overruns=%lu worst=%luus cycles_dropped=%lu) -- clearing "
          "them here would erase soak evidence\n",
          (unsigned long)g_sampler.isr_overruns(),
          (unsigned long)g_sampler.worst_isr_us(),
          (unsigned long)g_sampler.cycles_dropped());
    }
  }
  s_was_connected = connected_now;
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("SmartWatt bring-up");

  // Load whatever calibration NVS holds, or fall back to the compile-time
  // defaults, and say out loud which one happened. "Did it load my trim or
  // silently use defaults?" is exactly the question the bench needs
  // answered, not inferred from a metrics line that looks identical either
  // way.
  CalibrationSet cal{};
  if (load_calibration(cal)) {
    g_calibration_from_nvs = true;
    print_calibration("Calibration: loaded from NVS", cal);
  } else {
    // load_calibration() already writes default_calibration() into `cal`
    // on this path -- see nvs_calibration.cpp -- but that is a side effect
    // of its implementation, not something this file should lean on
    // silently. The fallback is spelled out here so the intent survives
    // even if that implementation detail changes.
    cal = default_calibration();
    print_calibration(
        "Calibration: none stored in NVS -- using compile-time defaults", cal);
  }
  g_calibration = cal;
  g_cycles.set_calibration(cal);

  // Fingerprint table (classifier). LittleFS mounts the "spiffs"-labelled
  // data partition this board's default partition table already carries
  // (the label is a historical holdover from before LittleFS existed; the
  // filesystem the Arduino core puts on it is LittleFS) at the default
  // base path "/littlefs". true = format-on-mount-failure, so a
  // factory-fresh or corrupt partition still mounts empty rather than
  // refusing to boot.
  //
  // No fixture ships with this task: training a real table is S8's job
  // (fingerprints.h: "Derived in S8"), so on a fresh device this file will
  // not exist yet, and that is the expected common case, not a fault.
  if (!LittleFS.begin(true)) {
    Serial.println("LittleFS: mount failed -- classifier will have no table");
  } else {
    static const char* const kFingerprintPath = "/littlefs/fingerprints.csv";
    FileFingerprintSource source(kFingerprintPath);
    g_have_fingerprints = source.load(g_fingerprints);
    if (g_have_fingerprints) {
      Serial.printf("Fingerprints: loaded %d rows from %s, id=%s\n",
                    g_fingerprints.count, kFingerprintPath,
                    g_fingerprints.fingerprint_id);
    } else {
      // load() returns false both when the file is simply absent and when
      // it loaded something unusable (fingerprints.cpp: a non-positive
      // rejection_threshold) -- and in the second case it may have left
      // g_fingerprints partly populated. Reset to a pristine, empty table
      // either way, so Knn::classify() takes its documented, always-safe
      // table_.count == 0 path (reject everything) rather than running off
      // a table load() itself decided was unusable. The device still
      // publishes telemetry -- with fingerprint_id null, handled in
      // device_context() -- rather than refusing to start. clear(), never
      // `= FingerprintTable{}`: that temporary is ~43 KB on an 8 KB stack.
      g_fingerprints.clear();
      Serial.println(
          "Fingerprints: no usable table on LittleFS -- events will "
          "classify as unknown_N only");
    }
  }

  g_adc.begin();
  g_sampler.begin(&g_adc);
  if (g_sampler.timer_start_failed()) {
    // A sampler that silently never starts looks exactly like a wiring
    // fault on the bench, so this is reported rather than left to be
    // inferred from an all-zero metrics line.
    Serial.println(
        "FATAL: sampler timer failed to start (esp_timer_create or "
        "esp_timer_start_periodic returned an error)");
  }

  g_net.begin(g_net_config);

  // Said at boot, not only when the first frame is withheld, so the state is
  // on the log from the first screenful rather than inferred from an absence.
  // No waiting here on purpose: setup() must not block on the clock -- the
  // sampler has to start and the DSP has to run whether or not a network or a
  // time source ever appears.
  Serial.println(
      "Clock: UNSET at boot (an ESP32 counts from 0 at reset). SNTP starts "
      "from the broker's own IP once the station associates -- an IP literal, "
      "never a hostname (US72). Telemetry and events are WITHHELD until the "
      "clock is set; sampling and the DSP run regardless.");
}

void loop() {
  // Serviced every pass regardless of what else this call does, so MQTT
  // keep-alive and the SNTP start do not wait behind a quiet sampler ring.
  service_network();

  const char* command = poll_serial_command();
  if (command != nullptr) {
    handle_command(command);
    return;  // re-enter loop() fresh; whatever just ran already took its turn
  }

  SampleSet set{};
  CycleMetrics metrics{};
  while (g_sampler.pop(set)) {
    if (g_cycles.push(set, metrics)) {
      handle_cycle(now_seconds(), metrics);
    }
  }
}
