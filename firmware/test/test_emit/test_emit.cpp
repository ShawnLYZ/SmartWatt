#include <unity.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "payload.h"

void setUp() {}
void tearDown() {}

static char buffer[8192];

static CycleMetrics metrics() {
  CycleMetrics m;
  m.vrms = 239.4;
  m.irms = 3.812;
  m.p = 902.5;
  m.q1 = 118.3;
  m.dist = 41.2;
  m.s = 912.6;
  m.pf_true = 0.989;
  m.pf_disp = 0.997;
  m.freq = 49.98;
  m.range = Range::High;
  m.samples = 80;
  m.valid = true;
  return m;
}

static HealthCounters health() { return HealthCounters{0, 41, 2}; }

static EmitContext device_context(const HealthCounters* h) {
  EmitContext ctx;
  ctx.source = "device";
  ctx.seq = 12345;
  ctx.fingerprint_id = "a3f19c2e";
  ctx.floor_w = 8.5;
  ctx.wh_session = 4231.7;
  ctx.wh_today = 1204.3;
  ctx.health = h;
  return ctx;
}

static bool contains(const char* needle) {
  return std::string(buffer).find(needle) != std::string::npos;
}

/// Non-overlapping occurrences of `needle` in the emitted payload.
static int count(const char* needle) {
  const std::string body(buffer);
  const std::string key(needle);
  int found = 0;
  for (std::size_t at = body.find(key); at != std::string::npos;
       at = body.find(key, at + key.size())) {
    ++found;
  }
  return found;
}

/// Writes the emitted payload so the pytest bridge can validate it against
/// the SAME schemas the server uses.
///
/// The fopen result is ASSERTED, not ignored. The bridge's per-payload tests
/// skip when a file is absent, so a missing output directory would otherwise
/// turn a real failure into a green run that validated nothing. Fail here,
/// where the cause is still visible.
static void save(const char* name) {
  char path[512];
  std::snprintf(path, sizeof(path), "%s/../output/%s.json", FIXTURE_DIR, name);
  std::FILE* file = std::fopen(path, "w");
  TEST_ASSERT_NOT_NULL_MESSAGE(file, path);
  const int written = std::fputs(buffer, file);
  std::fclose(file);
  TEST_ASSERT_TRUE_MESSAGE(written >= 0, path);
}

static void test_telemetry_has_the_schema_tag() {
  Tracker tracker;
  const HealthCounters h = health();
  const int written = emit_telemetry(buffer, sizeof(buffer), 1754035200.0,
                                     metrics(), tracker, device_context(&h));
  TEST_ASSERT_TRUE(contains("\"smartwatt.telemetry.v1\""));
  // The return value is the payload length, not a nominal or truncated one.
  TEST_ASSERT_EQUAL_INT(static_cast<int>(std::strlen(buffer)), written);
}

/// Builds a settled ON of `watts`, already carrying its signed step (R1).
static DetectedEvent on_event(double watts) {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.settle_cycles = 3;
  e.before.p = 0.0;
  e.after.p = watts;
  e.delta_p = watts;
  e.before.valid = e.after.valid = true;
  return e;
}

static void test_telemetry_carries_every_group() {
  // R1 again, at the payload level: a device frame with an EMPTY active set
  // never exercises the schema's active[].id pattern, so it would validate
  // whatever the tracker put there. Give it one named load and one generated
  // unknown_N, which are the two branches of that pattern.
  Tracker tracker;
  Classification kettle;
  kettle.rejected = false;
  std::snprintf(kettle.label, sizeof(kettle.label), "kettle");
  tracker.apply(on_event(848.0), kettle, 1754035188.0);

  Classification unrecognised;  // rejected = true by default
  tracker.apply(on_event(41.5), unrecognised, 1754035190.0);

  // residual_ is recomputed ONLY here -- apply() has no measured total to
  // compute it from. 902.5 measured - (848.0 + 41.5) attributed = 13.0.
  tracker.observe_total(902.5, 1754035200.0);

  const HealthCounters h = health();
  emit_telemetry(buffer, sizeof(buffer), 1754035200.0, metrics(), tracker,
                 device_context(&h));
  for (const char* key : {"\"electrical\"", "\"attribution\"", "\"energy\"",
                          "\"health\"", "\"fingerprint_id\"", "\"seq\""}) {
    TEST_ASSERT_TRUE_MESSAGE(contains(key), key);
  }
  TEST_ASSERT_TRUE_MESSAGE(contains("\"id\": \"kettle\""), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"id\": \"unknown_1\""), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"residual_w\": 13.0000"), buffer);
  // The range enum is carried through, not hard-coded to one branch.
  TEST_ASSERT_TRUE_MESSAGE(contains("\"range\": \"high\""), buffer);
  save("telemetry-device");
}

static void test_low_range_is_emitted_as_low() {
  Tracker tracker;
  CycleMetrics m = metrics();
  m.range = Range::Low;
  const HealthCounters h = health();
  emit_telemetry(buffer, sizeof(buffer), 1754035200.0, m, tracker,
                 device_context(&h));
  TEST_ASSERT_TRUE_MESSAGE(contains("\"range\": \"low\""), buffer);
}

static void test_simulated_source_emits_null_health() {
  Tracker tracker;
  EmitContext ctx = device_context(nullptr);
  ctx.source = "simulator";
  ctx.fingerprint_id = nullptr;
  emit_telemetry(buffer, sizeof(buffer), 1754035200.0, metrics(), tracker, ctx);
  TEST_ASSERT_TRUE(contains("\"health\": null"));
  TEST_ASSERT_TRUE(contains("\"fingerprint_id\": null"));
  save("telemetry-simulated");
}

static void test_active_entries_are_emitted() {
  Tracker tracker;
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.before.p = 0.0;
  e.after.p = 848.0;
  // R1. Both the tracker and extract() read the SETTLED step from the event,
  // never from after.p - before.p. Leaving delta_p unset attributes 0 W and
  // makes every number below vacuous.
  e.delta_p = 848.0;
  e.before.valid = e.after.valid = true;
  Classification c;
  c.rejected = false;
  std::snprintf(c.label, sizeof(c.label), "kettle");
  tracker.apply(e, c, 1754035188.0);
  tracker.observe_total(902.5, 1754035200.0);

  const HealthCounters h = health();
  emit_telemetry(buffer, sizeof(buffer), 1754035200.0, metrics(), tracker,
                 device_context(&h));
  TEST_ASSERT_TRUE(contains("\"kettle\""));
  TEST_ASSERT_TRUE(contains("\"since\""));
  // The attributed watts are the event's settled step; the residual is
  // 902.5 measured minus 848.0 attributed.
  TEST_ASSERT_TRUE_MESSAGE(contains("\"w\": 848.0000"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"residual_w\": 54.5000"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"since\": 1754035188.000"), buffer);
}

static void test_negative_residual_is_emitted_unclamped() {
  Tracker tracker;
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.before.p = 0.0;
  e.after.p = 46.2;
  e.delta_p = 46.2;  // R1
  e.before.valid = e.after.valid = true;
  Classification c;
  c.rejected = false;
  std::snprintf(c.label, sizeof(c.label), "desk_fan");
  tracker.apply(e, c, 100.0);
  tracker.observe_total(44.9, 110.0);

  CycleMetrics m = metrics();
  m.p = 44.9;
  const HealthCounters h = health();
  emit_telemetry(buffer, sizeof(buffer), 1754035260.0, m, tracker,
                 device_context(&h));
  TEST_ASSERT_TRUE_MESSAGE(contains("\"residual_w\": -"),
                           "a negative residual must survive to the wire");
  // 44.9 measured minus 46.2 attributed. The value, not merely the sign.
  TEST_ASSERT_TRUE_MESSAGE(contains("\"residual_w\": -1.3000"), buffer);
  save("telemetry-negative-residual");
}

static void test_event_carries_all_fourteen_features() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.settle_cycles = 3;
  e.inrush_ratio = 1.02;
  // R1. A fixture whose cycles carry only `p` emits a payload that is
  // structurally right and numerically empty: eleven of the fourteen
  // features would be zero and the bridge would prove nothing about them.
  //
  // A QUIET `before` IS NOT ENOUGH EITHER. With before all zeros,
  // delta_s == after.s and delta_p / delta_s == after.p / after.s, so the
  // emitted pf_true equals after.pf_true to within the six decimals on the
  // wire -- and no assertion downstream could tell extract() COMPUTING the
  // ratio from extract() COPYING the cycle field. Same for pf_disp, and a
  // zero before.h3_h1 hides which state the busier-side selection picked.
  //
  // So `before` is a real standing load and `after` is that load plus the
  // kettle. Both cycles are internally consistent -- s = hypot(p, q1, dist),
  // irms = s / vrms, pf_true = p / s, pf_disp = p / hypot(p, q1) -- and the
  // settled step is 860.2 - 12.0 = 848.2 W. The features and the cycle
  // fields they might be confused with now differ by:
  //   pf_true  0.999891 vs 0.998855   (1.0e-3)
  //   pf_disp  0.999311 vs 0.999150   (1.6e-4)
  //   delta_s  848.292071 vs 861.185868
  //   h3_h1    0.021 (after, busier) vs 0.140 (before)
  // all far above the 1e-6 the bridge compares at.
  e.before.vrms = 239.4;
  e.before.freq = 49.98;
  e.before.range = Range::High;
  e.before.p = 12.0;
  e.before.q1 = 4.0;
  e.before.dist = 2.5;
  e.before.s = 12.8937969582;
  e.before.irms = 0.0538588010;
  e.before.crest = 1.90;
  e.before.pf_true = 0.9306800812;
  e.before.pf_disp = 0.9486832981;
  e.before.h3_h1 = 0.14;
  e.before.h5_h1 = 0.08;
  e.before.h7_h1 = 0.05;
  e.after.vrms = 239.4;
  e.after.freq = 49.98;
  e.after.range = Range::High;
  e.after.p = 860.2;
  e.after.q1 = 35.5;
  e.after.dist = 20.9;
  e.after.s = 861.1858684396;
  e.after.irms = 3.5972676209;
  e.after.crest = 1.423;
  e.after.pf_true = 0.9988552199;
  e.after.pf_disp = 0.9991495016;
  e.after.h3_h1 = 0.021;
  e.after.h5_h1 = 0.009;
  e.after.h7_h1 = 0.004;
  e.delta_p = 848.2;
  e.before.valid = e.after.valid = true;

  Classification c;
  c.rejected = false;
  c.confidence = 0.94;
  std::snprintf(c.label, sizeof(c.label), "kettle");
  for (int n = 0; n < 3; ++n) {
    std::snprintf(c.neighbours[n].label, sizeof(c.neighbours[n].label),
                  "kettle");
    std::snprintf(c.neighbours[n].training_id,
                  sizeof(c.neighbours[n].training_id), "kettle-on-%03d", n + 1);
    c.neighbours[n].distance = 0.31 + 0.13 * n;
  }
  // What Knn::classify publishes beside a full neighbour list. Without it the
  // emitter renders all three slots as padding.
  c.neighbour_count = 3;

  Attribution a;
  std::snprintf(a.label, sizeof(a.label), "kettle");
  std::snprintf(a.attributed_to, sizeof(a.attributed_to), "kettle");

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1754035188.412, e, c, a, extract(e),
             device_context(&h));

  for (int i = 0; i < kFeatureCount; ++i) {
    char key[64];
    std::snprintf(key, sizeof(key), "\"%s\"", kFeatureNames[i]);
    TEST_ASSERT_TRUE_MESSAGE(contains(key), kFeatureNames[i]);
  }
  TEST_ASSERT_TRUE(contains("\"neighbours\""));
  // Real numbers reached the wire, not struct defaults. log1p(848.2).
  TEST_ASSERT_TRUE_MESSAGE(contains("\"delta_p\": 848.200000"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"delta_q1\": 31.500000"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"delta_irms\": 3.543409"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"h3_h1\": 0.021000"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"log_delta_p\": 6.744295"), buffer);
  // The busier state's harmonics, i.e. the ON side -- not before.h3_h1.
  TEST_ASSERT_FALSE_MESSAGE(contains("\"h3_h1\": 0.140000"), buffer);
  // Cardinality, which no `contains` check can see. The bridge's schema
  // catches a dropped slot too, but the bridge runs off gitignored
  // artefacts and depends on run order; this runs on every firmware change.
  TEST_ASSERT_EQUAL_INT(3, count("\"training_id\""));
  TEST_ASSERT_EQUAL_INT(3, count("\"distance\""));
  save("event-on-confident");
}

/// The contract types settle_cycles as an integer, but JSON Schema 2020-12
/// counts 3.0 as an integer -- a float with a zero fraction satisfies
/// "type": "integer". So schema validation alone cannot tell the special
/// case in the emit loop from its absence. This can.
static void test_settle_cycles_is_emitted_as_a_json_integer() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.settle_cycles = 3;
  e.after.p = 305.0;
  e.delta_p = 305.0;  // R1
  e.before.valid = e.after.valid = true;

  Classification c;
  c.rejected = false;
  Attribution a;
  std::snprintf(a.label, sizeof(a.label), "kettle");
  std::snprintf(a.attributed_to, sizeof(a.attributed_to), "kettle");

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));
  TEST_ASSERT_TRUE_MESSAGE(contains("\"settle_cycles\": 3,"), buffer);
  TEST_ASSERT_FALSE_MESSAGE(contains("\"settle_cycles\": 3."), buffer);
}

static void test_event_edge_sits_outside_features() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.before.valid = e.after.valid = true;
  e.after.p = 100.0;
  e.delta_p = 100.0;  // R1
  Classification c;
  c.rejected = false;
  std::snprintf(c.label, sizeof(c.label), "kettle");
  Attribution a;
  std::snprintf(a.label, sizeof(a.label), "kettle");
  std::snprintf(a.attributed_to, sizeof(a.attributed_to), "kettle");

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));

  const std::string body(buffer);
  const std::size_t features_at = body.find("\"features\"");
  const std::size_t edge_at = body.find("\"edge\"");
  TEST_ASSERT_TRUE(edge_at != std::string::npos);
  TEST_ASSERT_TRUE(features_at != std::string::npos);
  TEST_ASSERT_TRUE(edge_at < features_at);
}

static void test_rejected_event_emits_null_label_and_a_reason() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.before.valid = e.after.valid = true;
  e.after.p = 305.0;
  e.delta_p = 305.0;  // R1

  Classification c;
  c.rejected = true;
  c.confidence = 0.0;

  Attribution a;
  a.rejected = true;
  a.reason = "distance_threshold";
  std::snprintf(a.attributed_to, sizeof(a.attributed_to), "unknown_1");

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));
  TEST_ASSERT_TRUE(contains("\"label\": null"));
  TEST_ASSERT_TRUE(contains("\"rejected\": true"));
  TEST_ASSERT_TRUE(contains("\"distance_threshold\""));
  TEST_ASSERT_TRUE(contains("\"unknown_1\""));
  save("event-rejected-unknown");
}

static void test_ambiguous_event_emits_null_attribution() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::OverlappingEdges;
  e.valid = true;
  e.before.valid = e.after.valid = true;
  e.after.p = 89.4;
  e.delta_p = 89.4;  // R1

  Classification c;
  c.rejected = false;
  Attribution a;
  a.ambiguous = true;
  a.reason = "overlapping_edges";

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));
  TEST_ASSERT_TRUE(contains("\"ambiguous\": true"));
  TEST_ASSERT_TRUE(contains("\"attributed_to\": null"));
  save("event-ambiguous-overlap");
}

static void test_reassigned_event_keeps_both_names() {
  DetectedEvent e;
  e.edge = Edge::Off;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.before.valid = e.after.valid = true;
  e.before.p = 40.0;
  // R1, signed: an OFF carries a negative step. extract() takes its
  // magnitude and `edge` carries the sign, which is what the contract says.
  e.delta_p = -40.0;

  Classification c;
  c.rejected = false;
  c.confidence = 0.61;
  std::snprintf(c.label, sizeof(c.label), "desk_fan");
  for (int n = 0; n < 3; ++n) {
    std::snprintf(c.neighbours[n].label, sizeof(c.neighbours[n].label),
                  "incandescent_lamp");
    std::snprintf(c.neighbours[n].training_id,
                  sizeof(c.neighbours[n].training_id), "lamp-off-%03d", n + 1);
    c.neighbours[n].distance = 0.7 + 0.1 * n;
  }
  c.neighbour_count = 3;

  Attribution a;
  std::snprintf(a.label, sizeof(a.label), "desk_fan");
  std::snprintf(a.attributed_to, sizeof(a.attributed_to),
                "incandescent_lamp");

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));
  TEST_ASSERT_TRUE(contains("\"desk_fan\""));
  TEST_ASSERT_TRUE(contains("\"incandescent_lamp\""));
  TEST_ASSERT_TRUE(contains("\"edge\": \"off\""));
  // The magnitude of the signed step, with the sign left to `edge`.
  TEST_ASSERT_TRUE_MESSAGE(contains("\"delta_p\": 40.000000"), buffer);
  save("event-off-reassigned");
}

/// A below-floor event ships no confidence, because it ships no label.
///
/// The two rejection sources did not agree. Knn::classify zeroes confidence
/// AND label on its own rejection; the tracker's below-floor path holds an
/// Attribution, not a Classification, so it can only clear the label -- and
/// the emitter reads `label` from the attribution while reading `confidence`
/// from the classification. The pair reached the wire as
/// `{"label": null, "confidence": 0.9400, "rejected": true,
///   "reason": "below_floor"}`: 94% certainty in a name that is not there.
/// It is schema-valid, so the pytest bridge could not catch it either -- which
/// is why this payload is now registered there with its values pinned.
static void test_a_below_floor_event_ships_no_confidence() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::BelowFloor;
  e.valid = true;
  e.settle_cycles = 3;
  e.after.p = 3.0;
  e.delta_p = 3.0;  // R1
  e.before.valid = e.after.valid = true;

  // The classifier ran and was CONFIDENT. It never sees the detection floor,
  // so its answer is not wrong -- it is simply not the tracker's answer.
  Classification c;
  c.rejected = false;
  c.confidence = 0.94;
  std::snprintf(c.label, sizeof(c.label), "kettle");
  for (int n = 0; n < 3; ++n) {
    std::snprintf(c.neighbours[n].label, sizeof(c.neighbours[n].label),
                  "kettle");
    std::snprintf(c.neighbours[n].training_id,
                  sizeof(c.neighbours[n].training_id), "kettle-on-%03d", n + 1);
    c.neighbours[n].distance = 0.31 + 0.13 * n;
  }
  c.neighbour_count = 3;

  Tracker tracker;
  const Attribution a = tracker.apply(e, c, 100.0);
  TEST_ASSERT_TRUE(a.rejected);
  TEST_ASSERT_EQUAL_STRING("", a.label);
  TEST_ASSERT_EQUAL_INT(0, tracker.active_count());

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));
  TEST_ASSERT_TRUE_MESSAGE(contains("\"label\": null"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"reason\": \"below_floor\""), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"rejected\": true"), buffer);
  TEST_ASSERT_TRUE_MESSAGE(contains("\"confidence\": 0.0000"), buffer);
  // The classifier's number, nowhere on the wire. The three neighbour
  // distances are 0.31/0.44/0.57, so this needle is unambiguous.
  TEST_ASSERT_FALSE_MESSAGE(contains("0.9400"), buffer);
  save("event-below-floor");
}

/// An unfilled neighbour slot is padding, and must not read as a match.
///
/// The schema requires exactly three neighbours, so a two-row table still
/// emits three. The third used to render as
/// `{"label": "unknown", "distance": 0.000000, "training_id": "none"}` -- a
/// zero-distance perfect match to a load called "unknown", at rank 3 of the
/// array US16 presents as the explanation of the classification.
static void test_unfilled_neighbour_slots_are_padding_not_perfect_matches() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.settle_cycles = 3;
  e.after.p = 1800.0;
  e.delta_p = 1800.0;  // R1
  e.before.valid = e.after.valid = true;

  // Two neighbours found, one slot never filled -- what a two-row table gives.
  Classification c;
  c.rejected = false;
  c.confidence = 0.88;
  std::snprintf(c.label, sizeof(c.label), "kettle");
  std::snprintf(c.neighbours[0].label, sizeof(c.neighbours[0].label), "kettle");
  std::snprintf(c.neighbours[0].training_id,
                sizeof(c.neighbours[0].training_id), "kettle-001");
  c.neighbours[0].distance = 0.0;  // sitting exactly on the training row
  std::snprintf(c.neighbours[1].label, sizeof(c.neighbours[1].label),
                "desk_fan");
  std::snprintf(c.neighbours[1].training_id,
                sizeof(c.neighbours[1].training_id), "desk_fan-001");
  c.neighbours[1].distance = 2.0;
  c.neighbour_count = 2;
  // Slot 2 is left over -- a non-empty label and a real-looking distance, of
  // the kind a reused or hand-built Classification carries. THE COUNT is what
  // says it is not part of this classification; inspecting the label would
  // ship it as rank 3 of the explanation.
  std::snprintf(c.neighbours[2].label, sizeof(c.neighbours[2].label),
                "space_heater");
  std::snprintf(c.neighbours[2].training_id,
                sizeof(c.neighbours[2].training_id), "heater-099");
  c.neighbours[2].distance = 3.5;

  Attribution a;
  std::snprintf(a.label, sizeof(a.label), "kettle");
  std::snprintf(a.attributed_to, sizeof(a.attributed_to), "kettle");

  const HealthCounters h = health();
  emit_event(buffer, sizeof(buffer), 1.0, e, c, a, extract(e),
             device_context(&h));

  // Still exactly three, because the schema requires exactly three.
  TEST_ASSERT_EQUAL_INT(3, count("\"training_id\""));
  TEST_ASSERT_EQUAL_INT(3, count("\"distance\""));
  // The two real ones, unchanged -- including a genuine 0.0, which is what
  // stops this being "any zero distance is padding".
  TEST_ASSERT_TRUE_MESSAGE(
      contains("{\"label\": \"kettle\", \"distance\": 0.000000, "
               "\"training_id\": \"kettle-001\"}"),
      buffer);
  // And the third: named "none" at both ends, carrying a distance that sorts
  // after every real neighbour instead of ahead of all of them.
  TEST_ASSERT_TRUE_MESSAGE(
      contains("{\"label\": \"none\", \"distance\": 1000000000.000000, "
               "\"training_id\": \"none\"}"),
      buffer);
  TEST_ASSERT_FALSE_MESSAGE(contains("\"label\": \"unknown\""), buffer);
  // The leftover slot, nowhere on the wire -- neither its label nor its
  // distance. This is what the count buys over inspecting the label.
  TEST_ASSERT_FALSE_MESSAGE(contains("space_heater"), buffer);
  TEST_ASSERT_FALSE_MESSAGE(contains("heater-099"), buffer);
  TEST_ASSERT_FALSE_MESSAGE(contains("3.500000"), buffer);
}

/// A `source` outside the contract's enum fails the payload.
///
/// The field was spliced in with a bare %s, so the caller's pointer was
/// trusted to be one of device|simulator|replay AND to need no escaping --
/// neither of which anything checked. A frame carrying an unknown source is
/// dropped by the server anyway; failing here says so where the cause is
/// still visible, and nothing half-written is left for a caller to ship.
static void test_an_unknown_source_is_refused() {
  Tracker tracker;
  const HealthCounters h = health();
  EmitContext ctx = device_context(&h);

  for (const char* good : {"device", "simulator", "replay"}) {
    ctx.source = good;
    // health must pair with source per the schema's if/then/else.
    ctx.health = (std::strcmp(good, "device") == 0) ? &h : nullptr;
    TEST_ASSERT_TRUE_MESSAGE(
        emit_telemetry(buffer, sizeof(buffer), 1.0, metrics(), tracker, ctx) > 0,
        good);
  }

  ctx.health = &h;
  // A plausible typo, a case error, and a string that would splice raw JSON
  // straight into the frame.
  const char* const bad_sources[] = {"", "Device", "gateway",
                                     "device\", \"seq\": 99, \"x\": \"",
                                     nullptr};
  for (const char* bad : bad_sources) {
    ctx.source = bad;
    TEST_ASSERT_EQUAL_INT(-1, emit_telemetry(buffer, sizeof(buffer), 1.0,
                                             metrics(), tracker, ctx));
    TEST_ASSERT_EQUAL_CHAR('\0', buffer[0]);
  }

  // The event emitter holds the same rule.
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.delta_p = 100.0;
  e.before.valid = e.after.valid = true;
  Classification c;
  Attribution a;
  ctx.source = "gateway";
  TEST_ASSERT_EQUAL_INT(-1, emit_event(buffer, sizeof(buffer), 1.0, e, c, a,
                                       extract(e), ctx));
  TEST_ASSERT_EQUAL_CHAR('\0', buffer[0]);
}

/// A string field too long for the quoting scratch is REPORTED, not truncated.
///
/// quoted_or_null ignored its snprintf return value, so a 120-character
/// fingerprint_id produced `"fingerprint_id": "aaaa...aaa, "electrical": {`
/// -- no closing quote, unparseable -- and the emitter returned 543 as if it
/// had succeeded. That is exactly what Writer's own contract forbids.
/// Unreachable from in-tree callers today, all of which are 24 bytes or under,
/// but EmitContext::fingerprint_id is a bare `const char*` with no length
/// contract and S6 is the next caller.
static void test_an_overlong_string_field_is_refused_not_truncated() {
  Tracker tracker;
  const HealthCounters h = health();

  // The scratch is 64 bytes and quoting costs three of them: two quotes and
  // the terminating NUL. So 61 characters is the last that fits.
  char id[128];
  EmitContext ctx = device_context(&h);
  ctx.fingerprint_id = id;

  // Refilled before each case, and the length ASSERTED. Reusing the buffer
  // without refilling leaves the previous NUL in place, and every case after
  // the first silently measures the first one's string.
  const auto fill = [&id](int length) {
    std::memset(id, 'a', sizeof(id));
    id[length] = '\0';
    TEST_ASSERT_EQUAL_INT(length, static_cast<int>(std::strlen(id)));
  };

  fill(61);
  const int fits =
      emit_telemetry(buffer, sizeof(buffer), 1.0, metrics(), tracker, ctx);
  TEST_ASSERT_TRUE_MESSAGE(fits > 0, "61 characters must still be emitted");
  TEST_ASSERT_EQUAL_INT(fits, static_cast<int>(std::strlen(buffer)));
  TEST_ASSERT_EQUAL_CHAR('}', buffer[fits - 1]);
  TEST_ASSERT_TRUE(contains("\"fingerprint_id\": \"aaaaaaaaaa"));

  // One more character does not fit, and the failure is reported rather than
  // written. The buffer is 8192 bytes and the payload is nowhere near it, so
  // nothing here is about capacity.
  fill(62);
  TEST_ASSERT_EQUAL_INT(-1, emit_telemetry(buffer, sizeof(buffer), 1.0,
                                           metrics(), tracker, ctx));
  TEST_ASSERT_EQUAL_CHAR('\0', buffer[0]);

  // The 120-character case the reviewer measured, which returned a byte count.
  fill(120);
  TEST_ASSERT_EQUAL_INT(-1, emit_telemetry(buffer, sizeof(buffer), 1.0,
                                           metrics(), tracker, ctx));
  TEST_ASSERT_EQUAL_CHAR('\0', buffer[0]);

  // The event emitter quotes label, reason and attributed_to through the same
  // helper. `reason` is a bare const char*, so it can be overlong too.
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.delta_p = 100.0;
  e.before.valid = e.after.valid = true;
  Classification c;
  Attribution a;
  a.reason = id;  // still 120 characters
  TEST_ASSERT_EQUAL_INT(-1, emit_event(buffer, sizeof(buffer), 1.0, e, c, a,
                                       extract(e), device_context(&h)));
  TEST_ASSERT_EQUAL_CHAR('\0', buffer[0]);
}

static void test_emission_respects_the_buffer_capacity() {
  Tracker tracker;
  char tiny[16];
  const HealthCounters h = health();
  const int written = emit_telemetry(tiny, sizeof(tiny), 1.0, metrics(),
                                     tracker, device_context(&h));
  TEST_ASSERT_TRUE_MESSAGE(written < 0, "overflow must be reported, not silent");
  // ...and the half-written bytes do not stay in the caller's buffer, so a
  // caller trusting strlen() over the return value gets nothing rather than
  // half a frame.
  TEST_ASSERT_EQUAL_CHAR('\0', tiny[0]);
}

static void test_event_emission_respects_the_buffer_capacity() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.delta_p = 100.0;
  e.before.valid = e.after.valid = true;
  Classification c;
  Attribution a;
  char tiny[24];
  const HealthCounters h = health();
  const int written = emit_event(tiny, sizeof(tiny), 1.0, e, c, a, extract(e),
                                 device_context(&h));
  TEST_ASSERT_TRUE_MESSAGE(written < 0, "overflow must be reported, not silent");
  TEST_ASSERT_EQUAL_CHAR('\0', tiny[0]);
}

/// Both overflow tests above overflow on the FIRST append -- 37 and 32 bytes
/// into 16- and 24-byte buffers -- so `used + written >= capacity` and
/// `used + written > capacity` are indistinguishable to them. The boundary
/// is where they differ: at capacity == length, vsnprintf has to drop the
/// closing brace to fit the terminating NUL, and `>` would return that
/// truncated payload as a success.
static void test_capacity_boundary_is_exact() {
  Tracker tracker;
  const HealthCounters h = health();
  const int length = emit_telemetry(buffer, sizeof(buffer), 1754035200.0,
                                    metrics(), tracker, device_context(&h));
  TEST_ASSERT_TRUE(length > 0);
  const std::string complete(buffer);

  static char sized[8192];
  // Exactly the payload length: one byte short, because of the NUL.
  TEST_ASSERT_EQUAL_INT(-1, emit_telemetry(sized, length, 1754035200.0,
                                           metrics(), tracker,
                                           device_context(&h)));
  TEST_ASSERT_EQUAL_CHAR('\0', sized[0]);

  // One more byte is exactly enough, and the payload is whole.
  TEST_ASSERT_EQUAL_INT(length, emit_telemetry(sized, length + 1,
                                               1754035200.0, metrics(),
                                               tracker, device_context(&h)));
  TEST_ASSERT_EQUAL_INT(length, static_cast<int>(std::strlen(sized)));
  TEST_ASSERT_EQUAL_CHAR('}', sized[length - 1]);
  TEST_ASSERT_EQUAL_STRING(complete.c_str(), sized);
}

static void test_event_capacity_boundary_is_exact() {
  DetectedEvent e;
  e.edge = Edge::On;
  e.flag = EventFlag::Clean;
  e.valid = true;
  e.settle_cycles = 3;
  e.after.p = 305.0;
  e.delta_p = 305.0;  // R1
  e.before.valid = e.after.valid = true;
  Classification c;
  c.rejected = false;
  Attribution a;
  std::snprintf(a.label, sizeof(a.label), "kettle");
  std::snprintf(a.attributed_to, sizeof(a.attributed_to), "kettle");
  const HealthCounters h = health();

  const int length = emit_event(buffer, sizeof(buffer), 1.0, e, c, a,
                                extract(e), device_context(&h));
  TEST_ASSERT_TRUE(length > 0);
  const std::string complete(buffer);

  static char sized[8192];
  TEST_ASSERT_EQUAL_INT(-1, emit_event(sized, length, 1.0, e, c, a, extract(e),
                                       device_context(&h)));
  TEST_ASSERT_EQUAL_CHAR('\0', sized[0]);

  TEST_ASSERT_EQUAL_INT(length, emit_event(sized, length + 1, 1.0, e, c, a,
                                           extract(e), device_context(&h)));
  TEST_ASSERT_EQUAL_INT(length, static_cast<int>(std::strlen(sized)));
  TEST_ASSERT_EQUAL_CHAR('}', sized[length - 1]);
  TEST_ASSERT_EQUAL_STRING(complete.c_str(), sized);
}

/// A negative capacity would make `capacity - used` cast to a huge size_t
/// and let vsnprintf run off the end of the caller's buffer. Zero has no
/// room even for the terminator, so nothing may be written at all.
static void test_non_positive_capacity_is_refused_without_writing() {
  Tracker tracker;
  const HealthCounters h = health();
  // Static and roomier than a whole payload on purpose. If the guard is
  // ever removed, the negative capacity casts to SIZE_MAX and vsnprintf
  // writes the entire frame here -- which lands inside this buffer and
  // trips the assertions below, rather than smashing a stack frame and
  // taking the harness down with a message about nothing.
  static char sentinel[1024];
  std::memset(sentinel, 'x', sizeof(sentinel));

  TEST_ASSERT_EQUAL_INT(-1, emit_telemetry(sentinel, 0, 1.0, metrics(),
                                           tracker, device_context(&h)));
  TEST_ASSERT_EQUAL_CHAR('x', sentinel[0]);
  TEST_ASSERT_EQUAL_INT(-1, emit_telemetry(sentinel, -1, 1.0, metrics(),
                                           tracker, device_context(&h)));
  TEST_ASSERT_EQUAL_CHAR('x', sentinel[0]);
  TEST_ASSERT_EQUAL_CHAR('x', sentinel[sizeof(sentinel) - 1]);
}

int main() {
  UNITY_BEGIN();
  RUN_TEST(test_telemetry_has_the_schema_tag);
  RUN_TEST(test_telemetry_carries_every_group);
  RUN_TEST(test_low_range_is_emitted_as_low);
  RUN_TEST(test_simulated_source_emits_null_health);
  RUN_TEST(test_active_entries_are_emitted);
  RUN_TEST(test_negative_residual_is_emitted_unclamped);
  RUN_TEST(test_event_carries_all_fourteen_features);
  RUN_TEST(test_settle_cycles_is_emitted_as_a_json_integer);
  RUN_TEST(test_event_edge_sits_outside_features);
  RUN_TEST(test_rejected_event_emits_null_label_and_a_reason);
  RUN_TEST(test_ambiguous_event_emits_null_attribution);
  RUN_TEST(test_reassigned_event_keeps_both_names);
  RUN_TEST(test_a_below_floor_event_ships_no_confidence);
  RUN_TEST(test_unfilled_neighbour_slots_are_padding_not_perfect_matches);
  RUN_TEST(test_an_unknown_source_is_refused);
  RUN_TEST(test_an_overlong_string_field_is_refused_not_truncated);
  RUN_TEST(test_emission_respects_the_buffer_capacity);
  RUN_TEST(test_event_emission_respects_the_buffer_capacity);
  RUN_TEST(test_capacity_boundary_is_exact);
  RUN_TEST(test_event_capacity_boundary_is_exact);
  RUN_TEST(test_non_positive_capacity_is_refused_without_writing);
  return UNITY_END();
}
