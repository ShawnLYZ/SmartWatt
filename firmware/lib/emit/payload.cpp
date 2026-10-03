#include "payload.h"

#include <cstdarg>
#include <cstdio>
#include <cstring>

// The event schema pins `neighbours` at exactly three entries (minItems and
// maxItems both 3), so the loop that renders them must run kK times AND kK
// must be three. knn.cpp already nails kK to the extent of
// Classification::neighbours; this is the other end of the same tie, on the
// wire side, where a raised kK would silently start failing validation on the
// server instead of failing to compile here.
static_assert(Knn::kK == 3,
              "the event schema requires exactly three neighbours");

namespace {

/// Capacity of the per-field scratch buffer the quoting helper writes into.
///
/// The longest value that survives quoting is kScratchCapacity - 3: two quotes
/// and a terminating NUL. Anything longer is reported, never truncated -- see
/// quoted_or_null.
constexpr int kScratchCapacity = 64;

/// Distance reported for a neighbour slot the classifier never filled.
///
/// The schema requires exactly three neighbours, so a table with fewer than
/// three rows still has to emit three. Rendering an unfilled slot with its
/// default distance of 0.0 made it read as a PERFECT match -- the strongest
/// evidence there is -- in the very array US16 offers as the explanation of
/// the classification. Padding must sort last under any consumer's
/// nearest-first comparison and must not read as a match at a glance.
///
/// `label` and `training_id` of "none" are what actually identify a slot as
/// padding; this value's only job is to not contradict them. Real distances
/// are z-scores over at most fourteen dimensions and live in single digits,
/// so 1e9 is unmistakable. Padding also only ever occupies the TRAILING
/// slots, and the filled ones are already in ascending order, so position
/// orders them correctly whatever a consumer does with the number.
constexpr double kUnfilledDistance = 1e9;

/// The contract's `source` enum, rendered from a literal rather than spliced.
///
/// telemetry.schema.json and event.schema.json both restrict `source` to
/// device|simulator|replay, and the field was previously written with a bare
/// %s over EmitContext::source -- trusting the caller's pointer to be one of
/// the three AND to contain no character that needs escaping, neither of which
/// anything checked. Returning the matching LITERAL makes both true by
/// construction. An unrecognised source has no valid rendering at all, so it
/// fails the payload rather than shipping a frame the server will drop.
constexpr const char* kSources[] = {"device", "simulator", "replay"};

const char* known_source(const char* source) {
  if (source == nullptr) return nullptr;
  for (const char* known : kSources) {
    if (std::strcmp(source, known) == 0) return known;
  }
  return nullptr;
}

/// Appends to `out`, tracking the cursor. Returns false on overflow.
///
/// Overflow is sticky and is REPORTED, never truncated away: every emit_*
/// returns -1 once it is set. A truncated payload is not a smaller payload,
/// it is invalid JSON, and a caller that cannot tell the two apart ships
/// half a telemetry frame and calls it success.
struct Writer {
  char* out;
  int capacity;
  int used = 0;
  bool overflowed = false;

  // EXPLICIT CONSTRUCTOR, DELIBERATELY -- do not "simplify" this back into an
  // aggregate and a brace-init at the call site.
  //
  // platformio.ini declares -std=c++17 project-wide, but the Arduino
  // framework's build script (platformio-build-esp32s3.py) appends
  // -std=gnu++11 AFTER that flag on the esp32-s3 command line, and the later
  // flag wins -- so the device build is effectively gnu++11 regardless of
  // what this file's own project declares. Under C++11, a default member
  // initialiser (`used = 0`, `overflowed = false` above) disqualifies a class
  // from being an aggregate, so `Writer w{out, capacity}` has no aggregate
  // initialisation to fall back to and needs a real constructor to find.
  // C++14 dropped that rule, which is why this compiled under native's
  // -std=c++17 for every S1-S5 review and only broke the moment S6 pulled
  // this file into the device build. This constructor makes construction
  // identical -- and, since it only assigns the two members brace-init would
  // have, behaviourally identical -- under both standards.
  Writer(char* out_, int capacity_) : out(out_), capacity(capacity_) {}

  bool append(const char* format, ...) __attribute__((format(printf, 2, 3)));

  /// Marks the payload unemittable for a reason that is not a short buffer.
  ///
  /// It shares the overflow flag on purpose: to a caller both are the same
  /// fact -- this payload could not be written whole, so nothing was written.
  /// Every reason has to reach the return value through one channel, or a
  /// caller checking one of them ships a frame that failed the other.
  void fail() { overflowed = true; }

  /// Bytes written, or -1 on overflow -- and on overflow the buffer is left
  /// EMPTY rather than half-written. A caller that trusts strlen() over the
  /// return value then gets nothing instead of a frame missing its closing
  /// brace, which is the one failure a JSON parser downstream cannot
  /// distinguish from a transport truncation.
  int finish() {
    if (!overflowed) return used;
    if (capacity > 0) out[0] = '\0';
    return -1;
  }
};

bool Writer::append(const char* format, ...) {
  if (overflowed) return false;
  // Also the guard for capacity <= 0 and for any negative capacity: without
  // it, `capacity - used` casts to a huge size_t and vsnprintf writes past
  // the end of the caller's buffer.
  if (capacity <= used) {
    overflowed = true;
    return false;
  }
  va_list args;
  va_start(args, format);
  const int written =
      std::vsnprintf(out + used, static_cast<std::size_t>(capacity - used),
                     format, args);
  va_end(args);
  // `>=`, not `>`. At used + written == capacity vsnprintf had to drop the
  // final character to fit the terminating NUL, so the payload would be
  // returned as a success while missing its closing brace.
  if (written < 0 || used + written >= capacity) {
    overflowed = true;
    return false;
  }
  used += written;
  return true;
}

/// Renders `value` as a JSON string, or the bare literal `null`.
///
/// TRUNCATION IS ROUTED INTO THE WRITER'S OVERFLOW PATH, never dropped. The
/// snprintf return value used to be ignored, so a value longer than the
/// scratch buffer produced an unterminated string -- `"fingerprint_id":
/// "aaaa...aaa, "electrical": {` -- and the emitter returned a byte count as
/// if it had succeeded. That is exactly what Writer's own contract above
/// forbids: a truncated payload is not a smaller payload, it is invalid JSON.
///
/// Unreachable from in-tree callers today, every one of which is 24 bytes or
/// under. EmitContext::fingerprint_id is a bare `const char*` with no length
/// contract at payload.h, though, and S6 is the next caller.
const char* quoted_or_null(Writer& w, char* scratch, int size,
                           const char* value) {
  const int written =
      (value == nullptr || value[0] == '\0')
          ? std::snprintf(scratch, static_cast<std::size_t>(size), "null")
          : std::snprintf(scratch, static_cast<std::size_t>(size), "\"%s\"",
                          value);
  if (written < 0 || written >= size) {
    w.fail();
    // Nothing half-quoted may reach append(). It will refuse on the flag
    // above in any case; this is so it cannot matter if that ever changes.
    if (size > 0) scratch[0] = '\0';
  }
  return scratch;
}

}  // namespace

int emit_telemetry(char* out, int capacity, double ts,
                   const CycleMetrics& metrics, const Tracker& tracker,
                   const EmitContext& context) {
  Writer w{out, capacity};
  char scratch[kScratchCapacity];

  const char* const source = known_source(context.source);
  if (source == nullptr) {
    w.fail();
    return w.finish();
  }

  w.append("{\"schema\": \"smartwatt.telemetry.v1\", ");
  w.append("\"ts\": %.3f, ", ts);
  w.append("\"source\": \"%s\", ", source);
  w.append("\"seq\": %d, ", context.seq);
  w.append("\"fingerprint_id\": %s, ",
           quoted_or_null(w, scratch, sizeof(scratch), context.fingerprint_id));

  w.append("\"electrical\": {");
  w.append("\"vrms\": %.4f, \"irms\": %.6f, ", metrics.vrms, metrics.irms);
  w.append("\"p\": %.4f, \"q1\": %.4f, \"dist\": %.4f, \"s\": %.4f, ",
           metrics.p, metrics.q1, metrics.dist, metrics.s);
  w.append("\"pf_true\": %.6f, \"pf_disp\": %.6f, ", metrics.pf_true,
           metrics.pf_disp);
  w.append("\"freq\": %.4f, \"range\": \"%s\"}, ", metrics.freq,
           metrics.range == Range::High ? "high" : "low");

  w.append("\"attribution\": {\"active\": [");
  for (int n = 0; n < tracker.active_count(); ++n) {
    const ActiveEntry& entry = tracker.active()[n];
    w.append("%s{\"id\": \"%s\", \"w\": %.4f, \"since\": %.3f}",
             n > 0 ? ", " : "", entry.id, entry.watts, entry.since);
  }
  // Never clamped. A negative residual reaches the wire intact.
  w.append("], \"residual_w\": %.4f, \"floor_w\": %.4f}, ", tracker.residual(),
           context.floor_w);

  w.append("\"energy\": {\"wh_session\": %.4f, \"wh_today\": %.4f}, ",
           context.wh_session, context.wh_today);

  if (context.health != nullptr) {
    w.append(
        "\"health\": {\"isr_overruns\": %d, \"worst_isr_us\": %d, "
        "\"cycles_dropped\": %d}}",
        context.health->isr_overruns, context.health->worst_isr_us,
        context.health->cycles_dropped);
  } else {
    w.append("\"health\": null}");
  }

  return w.finish();
}

int emit_event(char* out, int capacity, double ts, const DetectedEvent& event,
               const Classification& classification,
               const Attribution& attribution, const FeatureVector& features,
               const EmitContext& context) {
  Writer w{out, capacity};
  char scratch[kScratchCapacity];

  const char* const source = known_source(context.source);
  if (source == nullptr) {
    w.fail();
    return w.finish();
  }

  w.append("{\"schema\": \"smartwatt.event.v1\", ");
  w.append("\"ts\": %.3f, ", ts);
  w.append("\"source\": \"%s\", ", source);
  w.append("\"seq\": %d, ", context.seq);

  // `edge` is emitted BEFORE `features` and outside it. The classifier
  // never saw it and the payload makes that visible.
  w.append("\"edge\": \"%s\", ", event.edge == Edge::On ? "on" : "off");

  // A NULL LABEL MAY NOT SHIP A CONFIDENCE.
  //
  // `label` is read from the attribution and `confidence` from the
  // classification, and the two rejection sources do not agree about what
  // that means. Knn::classify zeroes both on ITS rejection. The tracker's
  // below-floor path holds an Attribution, not a Classification, so it can
  // only clear the label -- and the pair reached the wire as
  // `{"label": null, "confidence": 0.9400, "rejected": true,
  //   "reason": "below_floor"}`: 94% certainty in a name that is not there,
  // and schema-valid, so nothing downstream can catch it. The label is the
  // field that decides.
  const bool named = attribution.label[0] != '\0';
  w.append("\"label\": %s, ",
           quoted_or_null(w, scratch, sizeof(scratch), attribution.label));
  w.append("\"confidence\": %.4f, ", named ? classification.confidence : 0.0);
  w.append("\"rejected\": %s, ", attribution.rejected ? "true" : "false");
  w.append("\"ambiguous\": %s, ", attribution.ambiguous ? "true" : "false");
  w.append("\"reason\": %s, ",
           quoted_or_null(w, scratch, sizeof(scratch), attribution.reason));
  w.append("\"attributed_to\": %s, ",
           quoted_or_null(w, scratch, sizeof(scratch),
                          attribution.attributed_to));

  w.append("\"features\": {");
  for (int i = 0; i < kFeatureCount; ++i) {
    if (i == kSettleCycles) {
      // The contract types this one as an integer. Emitting it through the
      // %.6f path below would produce "3.000000", which JSON Schema 2020-12
      // still accepts as an integer -- so the schema cannot catch the loss.
      // Keep the special case.
      w.append("%s\"%s\": %d", i > 0 ? ", " : "", kFeatureNames[i],
               static_cast<int>(features[i]));
    } else {
      w.append("%s\"%s\": %.6f", i > 0 ? ", " : "", kFeatureNames[i],
               features[i]);
    }
  }
  w.append("}, ");

  w.append("\"neighbours\": [");
  for (int n = 0; n < Knn::kK; ++n) {
    // Always kK entries, because the schema requires exactly three -- so a
    // fingerprint table with fewer than three rows still emits three, and the
    // trailing ones must be unmistakably PADDING rather than matches.
    //
    // The count is what decides, not an empty label. Inspecting the label was
    // how the tracker and the emitter came to disagree about the same slots:
    // tracker.cpp skipped empty ones while this loop renamed them "unknown"
    // and shipped their default 0.0 distance, so the emitted list was not the
    // list the state-consistency filter had considered, and rank 3 of the
    // classification's own explanation was a zero-distance perfect match to a
    // load called "unknown".
    const bool filled = n < classification.neighbour_count;
    const Neighbour& neighbour = classification.neighbours[n];
    w.append("%s{\"label\": \"%s\", \"distance\": %.6f, \"training_id\": \"%s\"}",
             n > 0 ? ", " : "", filled ? neighbour.label : "none",
             filled ? neighbour.distance : kUnfilledDistance,
             filled ? neighbour.training_id : "none");
  }
  w.append("]}");

  return w.finish();
}
