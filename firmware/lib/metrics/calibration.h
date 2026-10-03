#pragma once

#include <cstdint>

/// Derived from datasheet ratios in the S6 spec, and re-derived during
/// specification:
///
///   ADC step  3.3 V / 4096                      = 0.80566 mV/count
///   i_low     0.80566 mV * 5 A/V                = 0.0040283 A/count
///   i_high    0.80566 mV * 20 A/V               = 0.016113  A/count
///   v         0.80566 mV / (12/112) * (240/9)   = 0.2006    V/count
///
/// S6 trims these against reference instruments and stores the trimmed
/// values in NVS. These are the starting point, not the final answer.
namespace calibration {

constexpr double kVCal = 0.2006;
constexpr double kICalLow = 0.0040283;
constexpr double kICalHigh = 0.016113;

constexpr uint16_t kAdcMid = 2048;
constexpr uint16_t kAdcMax = 4095;

constexpr double kSampleRateHz = 4000.0;

/// Counts from the rail at which a channel is treated as clipped.
constexpr uint16_t kClipMargin = 8;

}  // namespace calibration
