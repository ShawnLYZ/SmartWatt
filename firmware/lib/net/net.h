#pragma once

#include <cstdint>

/// STATIC ADDRESSING ONLY. Nothing in this system resolves a hostname, so
/// an mDNS failure at a venue cannot break it (US72).
struct NetConfig {
  const char* ssid;
  const char* password;
  uint8_t ip[4];
  uint8_t gateway[4];
  uint8_t mask[4];
  uint8_t broker[4];
  uint16_t broker_port;
};

/// The lowest value this system will accept as a real UTC wall-clock
/// reading: 1.7e9 seconds is 2023-11-14, long before this project began and
/// astronomically beyond any plausible seconds-since-boot figure. An ESP32
/// that has not been told the time counts from 0 at reset, so anything below
/// this is uptime wearing a timestamp's clothes.
///
/// `static constexpr` at namespace scope, not C++17's `inline`: this header
/// is reached by the esp32-s3 build, which compiles as gnu++11 in practice
/// (platformio.ini's note; sampler.h's kNoPreviousTick carries the same
/// reasoning). Only ever used by value, never by address, so per-translation-
/// unit duplication is not observable.
static constexpr double kMinSyncedUnixSeconds = 1.7e9;

/// True when `unix_seconds` is a real UTC wall-clock reading rather than the
/// seconds-since-boot the device counts until SNTP sets its clock.
///
/// Pure arithmetic, carved deliberately out of the device-only code and
/// tested on the laptop -- the same reason periods_late() and
/// encode_mcp3208_command() are. Getting this wrong on the bench does not
/// look like a bug: it looks like a dashboard that is simply empty (every
/// /api/series and /api/ledger window filters on wall-clock bounds) and like
/// device rows the server's retention pass quietly deleted, because every
/// uptime-stamped row is older than any cutoff it computes.
bool clock_is_set(double unix_seconds);

class Net {
 public:
  void begin(const NetConfig& config);
  bool connected();
  void loop();
  bool publish(const char* topic, const char* payload);
};
