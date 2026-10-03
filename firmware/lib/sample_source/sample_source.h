#pragma once

#include <cstdint>

/// One simultaneous acquisition of all three channels, in raw ADC counts.
struct SampleSet {
  uint16_t v;
  uint16_t i_low;
  uint16_t i_high;
};

/// SEAM 2.
///
/// Seam 1 (the MQTT contract) cannot reach the signal chain, because by the
/// time a payload exists the DSP has already run. This is the one narrow
/// interface that lets the whole chain run on a laptop.
///
/// Two implementations: FileSampleSource (S5a) and AdcSampleSource (S6).
/// Only the second is hardware-bound. This seam exists solely because seam 1
/// cannot cover the chain, and is not duplicated anywhere else.
class ISampleSource {
 public:
  virtual ~ISampleSource() = default;

  /// Fills `out` with the next sample set. Returns false at end of stream.
  virtual bool next(SampleSet& out) = 0;
};
