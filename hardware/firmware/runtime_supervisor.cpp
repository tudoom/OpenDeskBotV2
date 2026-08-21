#include "runtime_supervisor.h"

#include "audio_capture.h"
#include "audio_frontend_esp_sr.h"
#include "logger.h"

#include <atomic>
#include <esp_attr.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <string.h>

namespace {

#ifndef DESKBOT_RUNTIME_USB_STALL_MS
#define DESKBOT_RUNTIME_USB_STALL_MS 15000U
#endif
#ifndef DESKBOT_RUNTIME_MIC_TASK_STALL_MS
#define DESKBOT_RUNTIME_MIC_TASK_STALL_MS 4000U
#endif
#ifndef DESKBOT_RUNTIME_MIC_FRAME_STALL_MS
#define DESKBOT_RUNTIME_MIC_FRAME_STALL_MS 6000U
#endif
#ifndef DESKBOT_RUNTIME_AFE_STALL_MS
#define DESKBOT_RUNTIME_AFE_STALL_MS 5000U
#endif
#ifndef DESKBOT_RUNTIME_HEALTHY_RESET_MS
#define DESKBOT_RUNTIME_HEALTHY_RESET_MS 120000U
#endif

constexpr uint32_t kRetentionMagic = 0x44525331U;  // "DRS1"
constexpr uint32_t kPeripheralRestartLimit = 3U;

struct RetainedRuntimeState {
  uint32_t magic;
  uint32_t boot_count;
  uint32_t recovery_count;
  uint32_t last_uptime_ms;
  char last_reason[32];
};

RTC_DATA_ATTR RetainedRuntimeState s_retained{};

std::atomic<uint32_t> s_last_usb_poll_ms{0};
TaskHandle_t s_supervisor_task = nullptr;
uint32_t s_supervisor_started_ms = 0;
bool s_healthy_window_recorded = false;
uint32_t s_last_degraded_log_ms = 0;
esp_reset_reason_t s_hardware_reset_reason = ESP_RST_UNKNOWN;

const char* hardware_reset_reason_name(esp_reset_reason_t reason) {
  switch (reason) {
    case ESP_RST_POWERON:
      return "poweron";
    case ESP_RST_EXT:
      return "external";
    case ESP_RST_SW:
      return "software";
    case ESP_RST_PANIC:
      return "panic";
    case ESP_RST_INT_WDT:
      return "interrupt_wdt";
    case ESP_RST_TASK_WDT:
      return "task_wdt";
    case ESP_RST_WDT:
      return "watchdog";
    case ESP_RST_DEEPSLEEP:
      return "deepsleep";
    case ESP_RST_BROWNOUT:
      return "brownout";
    case ESP_RST_SDIO:
      return "sdio";
    case ESP_RST_USB:
      return "usb";
    case ESP_RST_JTAG:
      return "jtag";
    case ESP_RST_EFUSE:
      return "efuse";
    case ESP_RST_PWR_GLITCH:
      return "power_glitch";
    case ESP_RST_CPU_LOCKUP:
      return "cpu_lockup";
    case ESP_RST_UNKNOWN:
    default:
      return "unknown";
  }
}

bool hardware_reset_was_panic(esp_reset_reason_t reason) {
  return reason == ESP_RST_PANIC || reason == ESP_RST_INT_WDT ||
         reason == ESP_RST_TASK_WDT || reason == ESP_RST_WDT ||
         reason == ESP_RST_CPU_LOCKUP;
}

uint32_t age_ms(uint32_t now, uint32_t then) {
  return then == 0 ? UINT32_MAX : static_cast<uint32_t>(now - then);
}

void retain_restart_reason(const char* reason, uint32_t uptime_ms) {
  s_retained.magic = kRetentionMagic;
  s_retained.recovery_count++;
  s_retained.last_uptime_ms = uptime_ms;
  strncpy(
      s_retained.last_reason,
      reason ? reason : "unknown",
      sizeof(s_retained.last_reason) - 1);
  s_retained.last_reason[sizeof(s_retained.last_reason) - 1] = '\0';
}

[[noreturn]] void restart_for_stall(const char* reason, uint32_t age) {
  const uint32_t now = millis();
  retain_restart_reason(reason, now);
  log_error(
      "[RUNTIME] restarting reason=%s age=%lums recovery_count=%u",
      reason,
      static_cast<unsigned long>(age),
      static_cast<unsigned>(s_retained.recovery_count));
  vTaskDelay(pdMS_TO_TICKS(80));
  esp_restart();
  for (;;) {
    vTaskDelay(portMAX_DELAY);
  }
}
bool peripheral_restart_allowed() {
  return s_retained.recovery_count < kPeripheralRestartLimit;
}

void log_peripheral_degraded(const char* reason, uint32_t age) {
  const uint32_t now = millis();
  if (s_last_degraded_log_ms != 0 &&
      static_cast<uint32_t>(now - s_last_degraded_log_ms) < 10000U) {
    return;
  }
  s_last_degraded_log_ms = now;
  log_error(
      "[RUNTIME] peripheral degraded reason=%s age=%lums recovery_count=%u",
      reason,
      static_cast<unsigned long>(age),
      static_cast<unsigned>(s_retained.recovery_count));
}

void runtime_supervisor_task(void*) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(1000));
    const uint32_t now = millis();
    const uint32_t uptime =
        static_cast<uint32_t>(now - s_supervisor_started_ms);

    if (!s_healthy_window_recorded &&
        uptime >= DESKBOT_RUNTIME_HEALTHY_RESET_MS) {
      s_healthy_window_recorded = true;
      s_retained.recovery_count = 0;
      log_info("[RUNTIME] healthy window reached; recovery streak cleared");
    }

    const uint32_t usb_age =
        age_ms(now, s_last_usb_poll_ms.load(std::memory_order_acquire));
    if (usb_age > DESKBOT_RUNTIME_USB_STALL_MS) {
      restart_for_stall("usb_poll_stall", usb_age);
    }

    const uint32_t mic_task_age =
        age_ms(now, mic_capture_last_attempt_ms());
    if (mic_task_age > DESKBOT_RUNTIME_MIC_TASK_STALL_MS) {
      if (peripheral_restart_allowed()) {
        restart_for_stall("mic_task_stall", mic_task_age);
      }
      log_peripheral_degraded("mic_task_stall", mic_task_age);
      continue;
    }

    const uint32_t mic_frame_age =
        age_ms(now, mic_capture_last_frame_ms());
    if (mic_frame_age > DESKBOT_RUNTIME_MIC_FRAME_STALL_MS) {
      if (peripheral_restart_allowed()) {
        restart_for_stall("mic_frame_stall", mic_frame_age);
      }
      log_peripheral_degraded("mic_frame_stall", mic_frame_age);
      continue;
    }

    if (audio_frontend_ready()) {
      const uint32_t afe_age =
          age_ms(now, audio_frontend_last_progress_ms());
      if (afe_age > DESKBOT_RUNTIME_AFE_STALL_MS) {
        // Raw microphone capture is independently retained.  ESP-SR is an
        // enhancement layer, so its worker stall must not reboot the display,
        // USB transport, RTC stream and servos along with it.
        audio_frontend_force_raw_fallback("esp_sr_task_stall");
        log_peripheral_degraded("esp_sr_task_stall", afe_age);
      }
    }
  }
}

}  // namespace

bool runtime_supervisor_start() {
  if (s_supervisor_task != nullptr) {
    return true;
  }
  s_hardware_reset_reason = esp_reset_reason();
  if (s_retained.magic != kRetentionMagic) {
    memset(&s_retained, 0, sizeof(s_retained));
    s_retained.magic = kRetentionMagic;
  }
  s_retained.boot_count++;
  const uint32_t now = millis();
  s_supervisor_started_ms = now;
  s_last_usb_poll_ms.store(now, std::memory_order_release);

  TaskHandle_t created = nullptr;
  const BaseType_t rc = xTaskCreatePinnedToCore(
      runtime_supervisor_task,
      "runtime_guard",
      4096,
      nullptr,
      7,
      &created,
      PRO_CPU_NUM);
  if (rc != pdPASS || created == nullptr) {
    log_error("[RUNTIME] supervisor task create failed rc=%d", static_cast<int>(rc));
    return false;
  }
  s_supervisor_task = created;
  log_warn(
      "[RUNTIME] supervisor ready boot=%u prior_reason=%s recovery_count=%u "
      "hardware_reset=%s(%u) last_panic=%d last_restart_uptime_ms=%lu",
      static_cast<unsigned>(s_retained.boot_count),
      runtime_supervisor_last_reason(),
      static_cast<unsigned>(s_retained.recovery_count),
      runtime_supervisor_hardware_reset_reason(),
      static_cast<unsigned>(runtime_supervisor_hardware_reset_code()),
      runtime_supervisor_last_reset_was_panic() ? 1 : 0,
      static_cast<unsigned long>(s_retained.last_uptime_ms));
  return true;
}

void runtime_supervisor_mark_usb_poll() {
  s_last_usb_poll_ms.store(millis(), std::memory_order_release);
}

const char* runtime_supervisor_last_reason() {
  return s_retained.last_reason[0] ? s_retained.last_reason : "none";
}

uint32_t runtime_supervisor_recovery_count() {
  return s_retained.recovery_count;
}

uint32_t runtime_supervisor_boot_count() {
  return s_retained.boot_count;
}

uint32_t runtime_supervisor_last_restart_uptime_ms() {
  return s_retained.last_uptime_ms;
}

const char* runtime_supervisor_hardware_reset_reason() {
  return hardware_reset_reason_name(s_hardware_reset_reason);
}

uint32_t runtime_supervisor_hardware_reset_code() {
  return static_cast<uint32_t>(s_hardware_reset_reason);
}

bool runtime_supervisor_last_reset_was_panic() {
  return hardware_reset_was_panic(s_hardware_reset_reason);
}
