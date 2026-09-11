#include "common.h"

#include <esp_mac.h>

namespace {

char s_device_id[32] = "deskbot_unavailable";
bool s_device_id_initialized = false;

bool read_device_mac(uint8_t mac[6]) {
  if (mac == nullptr) {
    return false;
  }
  memset(mac, 0, 6);
  if (esp_efuse_mac_get_default(mac) != ESP_OK) {
    return false;
  }
  bool any_nonzero = false;
  for (size_t i = 0; i < 6; ++i) {
    any_nonzero = any_nonzero || mac[i] != 0;
  }
  return any_nonzero && (mac[0] & 0x01u) == 0;
}

bool ensure_device_id() {
  if (s_device_id_initialized) {
    return true;
  }
  uint8_t mac[6] = {};
  if (!read_device_mac(mac)) {
    return false;
  }
  snprintf(s_device_id, sizeof(s_device_id),
           "deskbot_%02x%02x%02x%02x%02x%02x",
           mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
  s_device_id_initialized = true;
  return true;
}

}  // namespace

void setup_FFat() {
  if (!FFat.begin(true)) {
    log_error("[FFAT] begin failed (check partition deskbot_rom_8MB.csv); FS unavailable");
    return;
  }
  log_info("[FFAT] ready");
}

const char* get_device_id() {
  (void)ensure_device_id();
  return s_device_id;
}

bool device_id_available() {
  return ensure_device_id();
}
