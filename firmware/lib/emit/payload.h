#pragma once

#include "features.h"
#include "knn.h"
#include "tracker.h"

struct HealthCounters {
  int isr_overruns = 0;
  int worst_isr_us = 0;
  int cycles_dropped = 0;
};

struct EmitContext {
  /// One of "device", "simulator", "replay" -- the contract's enum. Anything
  /// else has no valid rendering and the emitters return -1 rather than ship
  /// a frame the server will drop.
  const char* source = "device";
  int seq = 0;
  /// Null off-target, or when no fingerprint table is loaded.
  ///
  /// No length contract, deliberately -- the emitter reports a value it cannot
  /// quote whole instead of truncating it. Anything over 61 characters is
  /// refused rather than half-written.
  const char* fingerprint_id = nullptr;
  double floor_w = 6.0;
  double wh_session = 0.0;
  double wh_today = 0.0;
  /// Null unless source == "device". Sampler counters are meaningless
  /// off-target and the payload says so structurally.
  ///
  /// This pointer IS the mechanism that satisfies the telemetry schema's
  /// allOf/if-then-else: `health` must be an object when source is "device"
  /// and null otherwise. Nothing else in the emitter enforces the pairing,
  /// so a caller that passes counters with source "simulator" produces a
  /// payload the server rejects -- which is the intended failure mode.
  const HealthCounters* health = nullptr;
};

/// Returns bytes written, or -1 if the payload could not be written WHOLE.
///
/// -1 covers every reason: the buffer was too small, `context.source` was not
/// one of the three the contract allows, or a string field did not fit the
/// emitter's quoting scratch. On -1 the buffer is left empty, so a caller that
/// trusts strlen() over the return value gets nothing rather than a fragment.
int emit_telemetry(char* out, int capacity, double ts,
                   const CycleMetrics& metrics, const Tracker& tracker,
                   const EmitContext& context);

/// Returns bytes written, or -1 if the payload could not be written whole.
/// Same reasons and the same empty-buffer guarantee as emit_telemetry.
int emit_event(char* out, int capacity, double ts, const DetectedEvent& event,
               const Classification& classification,
               const Attribution& attribution, const FeatureVector& features,
               const EmitContext& context);
