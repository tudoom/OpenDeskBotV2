#include "thermal_cutoff.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>

#include <esp_sleep.h>
#include <string.h>

#include "logger.h"
#include "usb_transport.h"
#include "wifi_link.h"

namespace {
constexpr const char* kPrefsNs = "thermal";
constexpr const char* kPrefsKey = "cutoff_c";
constexpr int kAmpCtrlGpio = 45;  /* SPEAKER_AMP_CTRL：睡前拉低 */
uint32_t s_cutoff_c = THERMAL_CUTOFF_DEFAULT_C;
uint32_t s_last_sample_ms = 0;
uint32_t s_over_since_ms = 0;
RTC_NOINIT_ATTR uint32_t s_trips;
RTC_NOINIT_ATTR uint32_t s_trips_magic;
constexpr uint32_t kTripsMagic = 0x7E3A11C0u;
}  // namespace

void thermal_cutoff_load_prefs() {
  if (s_trips_magic != kTripsMagic) {
    s_trips = 0;
    s_trips_magic = kTripsMagic;
  }
  Preferences prefs;
  if (prefs.begin(kPrefsNs, /*readOnly=*/true)) {
    if (prefs.isKey(kPrefsKey)) {
      s_cutoff_c = prefs.getUInt(kPrefsKey, THERMAL_CUTOFF_DEFAULT_C);
    }
    prefs.end();
  }
  if (esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER) {
    log_warn("[THERMAL] woke from thermal cutoff sleep (trips=%u) temp=%dC",
             static_cast<unsigned>(s_trips), usb_transport_chip_temp_c());
    wifi_link_forensic("thermal wake temp=%dC", usb_transport_chip_temp_c());
  }
  log_warn("[THERMAL] cutoff=%uC sustain=%us sleep=%us", static_cast<unsigned>(s_cutoff_c),
           static_cast<unsigned>(THERMAL_CUTOFF_SUSTAIN_MS / 1000),
           static_cast<unsigned>(THERMAL_CUTOFF_SLEEP_SEC));
}

uint32_t thermal_cutoff_c() { return s_cutoff_c; }
uint32_t thermal_cutoff_trips() { return s_trips_magic == kTripsMagic ? s_trips : 0; }

bool thermal_cutoff_set(uint32_t celsius) {
  if (celsius > 120u) {
    celsius = 120u;
  }
  s_cutoff_c = celsius;
  s_over_since_ms = 0;
  Preferences prefs;
  if (prefs.begin(kPrefsNs, /*readOnly=*/false)) {
    prefs.putUInt(kPrefsKey, celsius);
    prefs.end();
  }
  log_warn("[THERMAL] cutoff set to %uC%s", static_cast<unsigned>(celsius),
           celsius == 0 ? " (disabled)" : "");
  return true;
}

static void thermal_cutoff_shutdown(int temp_c) {
  s_trips++;
  char json[192];
  snprintf(json, sizeof(json),
           "{\"type\":\"thermal_shutdown\",\"temp_c\":%d,\"cutoff_c\":%u,\"sleep_s\":%u,"
           "\"trips\":%u}",
           temp_c, static_cast<unsigned>(s_cutoff_c), static_cast<unsigned>(THERMAL_CUTOFF_SLEEP_SEC),
           static_cast<unsigned>(s_trips));
  (void)usb_transport_send_control_json(json);
  log_error("[THERMAL] %dC >= %uC for %us: powering down (deep sleep %us)", temp_c,
            static_cast<unsigned>(s_cutoff_c), static_cast<unsigned>(THERMAL_CUTOFF_SUSTAIN_MS / 1000),
            static_cast<unsigned>(THERMAL_CUTOFF_SLEEP_SEC));
  wifi_link_forensic("thermal cutoff %dC sleep", temp_c);
  /* 给日志/黑匣子一点时间落地，再把功放关掉进入深睡（LEDC/相机时钟/WiFi 随之停止）。 */
  delay(300);
  pinMode(kAmpCtrlGpio, OUTPUT);
  digitalWrite(kAmpCtrlGpio, LOW);
  esp_sleep_enable_timer_wakeup(static_cast<uint64_t>(THERMAL_CUTOFF_SLEEP_SEC) * 1000000ULL);
  esp_deep_sleep_start();
}

void thermal_cutoff_tick() {
  if (s_cutoff_c == 0) {
    return;
  }
  const uint32_t now = millis();
  if (s_last_sample_ms != 0 && static_cast<uint32_t>(now - s_last_sample_ms) < THERMAL_CUTOFF_SAMPLE_MS) {
    return;
  }
  s_last_sample_ms = now;
  const int temp_c = usb_transport_chip_temp_c();
  if (temp_c <= 0) {
    return;
  }
  if (static_cast<uint32_t>(temp_c) < s_cutoff_c) {
    s_over_since_ms = 0;
    return;
  }
  if (s_over_since_ms == 0) {
    s_over_since_ms = now == 0 ? 1 : now;
    log_warn("[THERMAL] %dC >= cutoff %uC; powering down in %us unless it cools", temp_c,
             static_cast<unsigned>(s_cutoff_c), static_cast<unsigned>(THERMAL_CUTOFF_SUSTAIN_MS / 1000));
    return;
  }
  if (static_cast<uint32_t>(now - s_over_since_ms) >= THERMAL_CUTOFF_SUSTAIN_MS) {
    thermal_cutoff_shutdown(temp_c);
  }
}

bool thermal_cutoff_handle_control_json(const uint8_t* payload, size_t length) {
  if (payload == nullptr || length == 0 || length > 512) {
    return false;
  }
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, payload, length)) {
    return false;
  }
  const char* type = doc["type"] | "";
  if (strcmp(type, "thermal_cutoff") == 0) {
    thermal_cutoff_set(doc["c"] | THERMAL_CUTOFF_DEFAULT_C);
  } else if (strcmp(type, "thermal_cutoff_req") != 0) {
    return false;
  }
  char json[160];
  snprintf(json, sizeof(json),
           "{\"type\":\"thermal_cutoff_ack\",\"cutoff_c\":%u,\"temp_c\":%d,\"trips\":%u,"
           "\"sustain_s\":%u,\"sleep_s\":%u}",
           static_cast<unsigned>(s_cutoff_c), usb_transport_chip_temp_c(),
           static_cast<unsigned>(thermal_cutoff_trips()),
           static_cast<unsigned>(THERMAL_CUTOFF_SUSTAIN_MS / 1000),
           static_cast<unsigned>(THERMAL_CUTOFF_SLEEP_SEC));
  (void)usb_transport_send_control_json(json);
  return true;
}
