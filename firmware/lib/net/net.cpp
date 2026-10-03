#include "net.h"

/// Deliberately ABOVE the SMARTWATT_NATIVE split, like periods_late() in
/// sampler.cpp: the laptop tests and the device run the same code, not two
/// copies that happen to agree.
bool clock_is_set(double unix_seconds) {
  return unix_seconds >= kMinSyncedUnixSeconds;
}

#if SMARTWATT_NATIVE

void Net::begin(const NetConfig&) {}
bool Net::connected() { return false; }
void Net::loop() {}
bool Net::publish(const char*, const char*) { return false; }

#else

#include <Arduino.h>
#include <PubSubClient.h>
#include <WiFi.h>

#include <cstdio>

namespace {
WiFiClient g_wifi;
PubSubClient g_mqtt(g_wifi);
NetConfig g_config{};
uint32_t g_next_attempt = 0;

/// The broker's IP rendered once, at begin(), for configTime(): "255.255.255.255"
/// is 15 characters plus the NUL.
char g_time_server[16] = {0};
bool g_time_source_started = false;
}  // namespace

void Net::begin(const NetConfig& config) {
  g_config = config;

  WiFi.mode(WIFI_STA);
  WiFi.config(IPAddress(config.ip), IPAddress(config.gateway),
              IPAddress(config.mask));
  // Stated explicitly rather than inherited from the framework default,
  // because loop() below depends on it: this is the thing that retries a
  // lost association, now that loop() no longer (wrongly) does it itself.
  WiFi.setAutoReconnect(true);
  WiFi.begin(config.ssid, config.password);

  // TIME SOURCE, US72. Formatted from NetConfig::broker, so the SNTP server
  // is the same machine as the MQTT broker -- the laptop hosting the hotspot,
  // which is where Mosquitto runs -- and no internet uplink is needed: SNTP
  // is a LAN protocol here, and the venue condition (hotspot, every other
  // adapter off) does not forbid it.
  //
  // This is an IP LITERAL and must stay one. lwIP's SNTP takes a dotted quad
  // inline with no DNS query, so "nothing in this system resolves a hostname"
  // (net.h's top comment) still holds exactly. DO NOT "improve" this into
  // pool.ntp.org, a .local name, or anything else that needs resolving: an
  // mDNS or DNS failure at a venue is precisely what US72 exists to make
  // impossible, and it would take the clock -- and with it every timestamp,
  // the whole ledger and the retention pass -- down with it.
  std::snprintf(g_time_server, sizeof(g_time_server), "%u.%u.%u.%u",
                static_cast<unsigned int>(config.broker[0]),
                static_cast<unsigned int>(config.broker[1]),
                static_cast<unsigned int>(config.broker[2]),
                static_cast<unsigned int>(config.broker[3]));
  g_time_source_started = false;

  // An IP literal, never a hostname.
  g_mqtt.setServer(IPAddress(config.broker), config.broker_port);
  g_mqtt.setBufferSize(2048);
  // PubSubClient::connect() blocks on the socket, and its default timeout is
  // 15 seconds. With no broker -- the normal state at power-on, before
  // Mosquitto is up -- loop() would sit inside that call while the sampler's
  // 512-entry ring overflowed in 128 ms, dropping tens of thousands of
  // samples per attempt. Two seconds still gives a healthy LAN broker far
  // more time than it needs to answer, and bounds the DSP's starvation to
  // something the ring's own drop counter reports honestly.
  g_mqtt.setSocketTimeout(2);
}

bool Net::connected() {
  return WiFi.status() == WL_CONNECTED && g_mqtt.connected();
}

void Net::loop() {
  if (WiFi.status() != WL_CONNECTED) {
    // Deliberately NOT WiFi.reconnect(). This function is called from the
    // sketch's loop() on every pass, as fast as the CPU allows, and
    // WiFi.reconnect() is esp_wifi_disconnect() followed immediately by
    // esp_wifi_connect(): calling it per pass tears association down and
    // restarts it thousands of times a second, while association needs
    // hundreds of milliseconds of uninterrupted exchange to complete. The
    // station would most likely never associate at all, and the only symptom
    // would be silence. Retrying is the framework's job (setAutoReconnect()
    // in begin(), above), and it already does it at a sane cadence.
    return;
  }

  // Start SNTP once, the first time the station is actually associated --
  // sntp_init() before there is a network to answer on just wastes its first
  // poll. configTime(0, 0, ...) means UTC with no DST offset, which is what
  // every `ts` on the wire is documented to be. See begin() for why the
  // argument is an IP literal and must remain one (US72).
  if (!g_time_source_started) {
    configTime(0, 0, g_time_server);
    g_time_source_started = true;
  }

  // Rollover-safe backoff. millis() wraps to 0 about every 49.7 days; a plain
  // `millis() > g_next_attempt` compares two wrapped values and, across the
  // wrap, retries on every pass instead of backing off -- reinstating exactly
  // the blocking-connect starvation the timeout above bounds. Subtracting
  // first and reading the difference as signed is correct on both sides of
  // the wrap.
  if (!g_mqtt.connected() &&
      static_cast<int32_t>(millis() - g_next_attempt) >= 0) {
    if (!g_mqtt.connect("smartwatt-device")) {
      g_next_attempt = millis() + 2000;
    }
  }
  g_mqtt.loop();
}

bool Net::publish(const char* topic, const char* payload) {
  return g_mqtt.connected() && g_mqtt.publish(topic, payload);
}

#endif
