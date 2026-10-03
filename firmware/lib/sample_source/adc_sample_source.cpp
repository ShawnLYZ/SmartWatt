#include "adc_sample_source.h"

void encode_mcp3208_command(uint8_t channel, uint8_t* tx3) {
  // 0000 0110 | D2  -> start bit and single-ended mode
  tx3[0] = static_cast<uint8_t>(0x06 | ((channel & 0x04) >> 2));
  // D1 D0 followed by don't-cares
  tx3[1] = static_cast<uint8_t>((channel & 0x03) << 6);
  tx3[2] = 0x00;
}

uint16_t decode_mcp3208_result(uint8_t rx1, uint8_t rx2) {
  return static_cast<uint16_t>(((rx1 & 0x0F) << 8) | rx2);
}

#if SMARTWATT_NATIVE

// Host stub so the header compiles under `native`. Never exercised there.
AdcSampleSource::AdcSampleSource(AdcPins pins, uint32_t spi_hz)
    : pins_(pins), spi_hz_(spi_hz) {}

void AdcSampleSource::begin() {}

uint16_t AdcSampleSource::read_channel(uint8_t) { return 2048; }

bool AdcSampleSource::next(SampleSet& out) {
  out.v = out.i_low = out.i_high = 2048;
  return true;
}

#else

#include <Arduino.h>
#include <SPI.h>

namespace {
SPIClass g_spi(HSPI);
}

AdcSampleSource::AdcSampleSource(AdcPins pins, uint32_t spi_hz)
    : pins_(pins), spi_hz_(spi_hz) {}

void AdcSampleSource::begin() {
  pinMode(pins_.cs, OUTPUT);
  digitalWrite(pins_.cs, HIGH);
  g_spi.begin(pins_.sck, pins_.miso, pins_.mosi, pins_.cs);
}

uint16_t AdcSampleSource::read_channel(uint8_t channel) {
  uint8_t frame[3];
  encode_mcp3208_command(channel, frame);

  // 1 MHz is conservative for 3.3 V operation. Three conversions of 24
  // clocks each is 72 us against a 250 us budget - ample margin for the ISR.
  g_spi.beginTransaction(SPISettings(spi_hz_, MSBFIRST, SPI_MODE0));
  digitalWrite(pins_.cs, LOW);
  g_spi.transfer(frame, 3);
  digitalWrite(pins_.cs, HIGH);
  g_spi.endTransaction();

  return decode_mcp3208_result(frame[1], frame[2]);
}

bool AdcSampleSource::next(SampleSet& out) {
  out.v = read_channel(kChannelV);
  out.i_low = read_channel(kChannelILow);
  out.i_high = read_channel(kChannelIHigh);
  return true;
}

#endif
