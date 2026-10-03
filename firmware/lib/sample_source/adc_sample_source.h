#pragma once

#include <cstdint>

#include "sample_source.h"

struct AdcPins {
  int cs;
  int sck;
  int miso;
  int mosi;
};

/// MCP3208 24-clock frame: five leading zeros, START=1, SGL/DIFF=1, then
/// D2 D1 D0. Pure arithmetic, so it is tested on the laptop - getting it
/// wrong on the bench looks exactly like a wiring fault.
void encode_mcp3208_command(uint8_t channel, uint8_t* tx3);

/// Twelve data bits: the low nibble of the second returned byte, then all
/// of the third.
uint16_t decode_mcp3208_result(uint8_t rx1, uint8_t rx2);

/// THE ONLY HARDWARE-BOUND MODULE IN THE FIRMWARE.
///
/// Everything else compiles and runs under `native`. That is not an
/// accident of layering: it is the property that lets the maths be debugged
/// separately from the wiring, and it is why Seam 2 exists at all.
class AdcSampleSource : public ISampleSource {
 public:
  static constexpr uint8_t kChannelV = 0;
  static constexpr uint8_t kChannelILow = 1;
  static constexpr uint8_t kChannelIHigh = 2;

  explicit AdcSampleSource(AdcPins pins, uint32_t spi_hz = 1000000);

  void begin();

  /// Acquires all three channels. Always returns true on hardware; the
  /// stream never ends.
  bool next(SampleSet& out) override;

  uint16_t read_channel(uint8_t channel);

 private:
  AdcPins pins_;
  uint32_t spi_hz_;
};
