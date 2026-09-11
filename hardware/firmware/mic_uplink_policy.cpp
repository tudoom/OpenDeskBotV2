#include "mic_uplink_policy.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <Preferences.h>

#include <atomic>
#include <string.h>

#include "logger.h"
#include "usb_transport.h"

namespace {
constexpr const char* kPrefsNs = "micup";
constexpr const char* kPrefsKey = "mode";
constexpr const char* kPrefsMuteKey = "mute";
std::atomic<uint8_t> s_mode{static_cast<uint8_t>(MIC_UPLINK_DEFAULT_MODE)};
std::atomic<bool> s_muted{false};
std::atomic<uint32_t> s_frames_total{0};
std::atomic<uint32_t> s_frames_sent{0};
std::atomic<uint32_t> s_gate_opens{0};
}  // namespace

void mic_uplink_load_prefs() {
  Preferences prefs;
  if (!prefs.begin(kPrefsNs, /*readOnly=*/true)) {
    return;
  }
  if (prefs.isKey(kPrefsKey)) {
    const uint8_t raw = prefs.getUChar(kPrefsKey, static_cast<uint8_t>(MIC_UPLINK_DEFAULT_MODE));
    s_mode.store(raw == 0 ? 0 : 1, std::memory_order_relaxed);
  }
  if (prefs.isKey(kPrefsMuteKey)) {
    s_muted.store(prefs.getUChar(kPrefsMuteKey, 0) != 0, std::memory_order_relaxed);
  }
  prefs.end();
  log_warn("[MIC_UPLINK] mode=%s muted=%d", mic_uplink_mode_name(), mic_uplink_muted() ? 1 : 0);
}

MicUplinkMode mic_uplink_mode() {
  return static_cast<MicUplinkMode>(s_mode.load(std::memory_order_relaxed));
}

const char* mic_uplink_mode_name() {
  return mic_uplink_mode() == MicUplinkMode::kVad ? "vad" : "continuous";
}

bool mic_uplink_set_mode(MicUplinkMode mode) {
  s_mode.store(static_cast<uint8_t>(mode), std::memory_order_relaxed);
  Preferences prefs;
  if (prefs.begin(kPrefsNs, /*readOnly=*/false)) {
    prefs.putUChar(kPrefsKey, static_cast<uint8_t>(mode));
    prefs.end();
  }
  log_warn("[MIC_UPLINK] mode set to %s", mic_uplink_mode_name());
  return true;
}

bool mic_uplink_muted() { return s_muted.load(std::memory_order_relaxed); }

bool mic_uplink_set_muted(bool muted) {
  s_muted.store(muted, std::memory_order_relaxed);
  Preferences prefs;
  if (prefs.begin(kPrefsNs, /*readOnly=*/false)) {
    prefs.putUChar(kPrefsMuteKey, muted ? 1 : 0);
    prefs.end();
  }
  log_warn("[MIC_UPLINK] muted=%d", muted ? 1 : 0);
  return true;
}

void mic_uplink_note_frame(bool sent) {
  s_frames_total.fetch_add(1, std::memory_order_relaxed);
  if (sent) {
    s_frames_sent.fetch_add(1, std::memory_order_relaxed);
  }
}

void mic_uplink_note_gate_open() { s_gate_opens.fetch_add(1, std::memory_order_relaxed); }
uint32_t mic_uplink_frames_total() { return s_frames_total.load(std::memory_order_relaxed); }
uint32_t mic_uplink_frames_sent() { return s_frames_sent.load(std::memory_order_relaxed); }
uint32_t mic_uplink_gate_opens() { return s_gate_opens.load(std::memory_order_relaxed); }

bool mic_uplink_handle_control_json(const uint8_t* payload, size_t length) {
  if (payload == nullptr || length == 0 || length > 512) {
    return false;
  }
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, payload, length)) {
    return false;
  }
  const char* type = doc["type"] | "";
  if (strcmp(type, "mic_mute") == 0 || strcmp(type, "mic_mute_req") == 0) {
    if (strcmp(type, "mic_mute") == 0 && doc.containsKey("muted")) {
      mic_uplink_set_muted(doc["muted"].as<bool>());
    }
    char mute_json[96];
    snprintf(mute_json, sizeof(mute_json), "{\"type\":\"mic_mute_ack\",\"muted\":%s,\"mode\":\"%s\"}",
             mic_uplink_muted() ? "true" : "false", mic_uplink_mode_name());
    (void)usb_transport_send_control_json(mute_json);
    return true;
  }
  if (strcmp(type, "mic_uplink_mode") == 0) {
    const char* mode = doc["mode"] | "";
    if (strcmp(mode, "continuous") == 0) {
      mic_uplink_set_mode(MicUplinkMode::kContinuous);
    } else if (strcmp(mode, "vad") == 0) {
      mic_uplink_set_mode(MicUplinkMode::kVad);
    }
  } else if (strcmp(type, "mic_uplink_mode_req") != 0) {
    return false;
  }
  const uint32_t total = mic_uplink_frames_total();
  const uint32_t sent = mic_uplink_frames_sent();
  char json[224];
  snprintf(json, sizeof(json),
           "{\"type\":\"mic_uplink_mode_ack\",\"mode\":\"%s\",\"muted\":%s,\"frames_total\":%u,"
           "\"frames_sent\":%u,\"sent_pct\":%u,\"gate_opens\":%u}",
           mic_uplink_mode_name(), mic_uplink_muted() ? "true" : "false",
           static_cast<unsigned>(total), static_cast<unsigned>(sent),
           static_cast<unsigned>(total ? (100ULL * sent / total) : 0),
           static_cast<unsigned>(mic_uplink_gate_opens()));
  (void)usb_transport_send_control_json(json);
  return true;
}
