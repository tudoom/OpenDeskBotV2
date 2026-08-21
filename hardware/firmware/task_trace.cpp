#include "task_trace.h"

#include "logger.h"

#include <esp_freertos_hooks.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <stdio.h>

#ifndef DESKBOT_CPU_STATS_INTERVAL_MS
#define DESKBOT_CPU_STATS_INTERVAL_MS 5000
#endif

namespace {

struct TaskTraceState {
  bool active = false;
  char task[24];
  char phase[24];
  char detail[64];
  unsigned long task_start_ms = 0;
  unsigned long phase_start_ms = 0;
  unsigned long last_heartbeat_ms = 0;
  unsigned long last_stall_log_ms = 0;
};

TaskTraceState s;

void copy_field(char* dst, size_t cap, const char* src) {
  if (src == nullptr || src[0] == '\0') {
    dst[0] = '\0';
    return;
  }
  snprintf(dst, cap, "%s", src);
}

unsigned long elapsed_ms(unsigned long start_ms) { return millis() - start_ms; }

void log_alive_if_due() {
  const unsigned long now = millis();
  if (now - s.last_heartbeat_ms < DESKBOT_TASK_HEARTBEAT_MS) {
    return;
  }
  s.last_heartbeat_ms = now;
  log_info("[TASK] ALIVE task=%s phase=%s task_ms=%lu phase_ms=%lu%s%s",
           s.task, s.phase, elapsed_ms(s.task_start_ms),
           elapsed_ms(s.phase_start_ms), s.detail[0] ? " detail=" : "",
           s.detail[0] ? s.detail : "");
}

/*
 * Record whether each Idle task ran during a tick and the longest interval in
 * which it did not. This replaces the former two 1 kHz task-sampling hooks,
 * which took a shared cross-core spinlock and walked a task table every tick.
 * Each core is the sole writer of its own slot; the monitor's approximate
 * five-second exchange may lose one boundary sample but cannot burden audio.
 */
volatile uint32_t s_idle_last_tick[2] = {0, 0};
volatile uint32_t s_idle_seen_ticks[2] = {0, 0};
volatile uint32_t s_idle_max_gap_ticks[2] = {0, 0};

bool IRAM_ATTR record_idle_tick(unsigned core) {
  const uint32_t now = static_cast<uint32_t>(xTaskGetTickCount());
  const uint32_t previous = s_idle_last_tick[core];
  if (now != previous) {
    const uint32_t gap = now - previous;
    s_idle_last_tick[core] = now;
    s_idle_seen_ticks[core] = s_idle_seen_ticks[core] + 1U;
    if (gap > s_idle_max_gap_ticks[core]) {
      s_idle_max_gap_ticks[core] = gap;
    }
  }
  return true;
}

bool IRAM_ATTR idle_hook_cpu0() { return record_idle_tick(0); }
bool IRAM_ATTR idle_hook_cpu1() { return record_idle_tick(1); }

void idle_headroom_task(void*) {
  for (;;) {
    vTaskDelay(pdMS_TO_TICKS(DESKBOT_CPU_STATS_INTERVAL_MS));
    const uint32_t now = static_cast<uint32_t>(xTaskGetTickCount());
    const uint32_t interval_ticks =
        pdMS_TO_TICKS(DESKBOT_CPU_STATS_INTERVAL_MS);
    for (unsigned core = 0; core < 2; ++core) {
      const uint32_t seen = s_idle_seen_ticks[core];
      s_idle_seen_ticks[core] = 0;
      uint32_t max_gap = s_idle_max_gap_ticks[core];
      s_idle_max_gap_ticks[core] = 0;
      const uint32_t ongoing_gap = now - s_idle_last_tick[core];
      if (ongoing_gap > max_gap) {
        max_gap = ongoing_gap;
      }
      const uint32_t idle_tick_pct =
          interval_ticks > 0
              ? (seen > interval_ticks ? 100U
                                       : seen * 100U / interval_ticks)
              : 0U;
      log_info("[IDLE] core=%u observed_ticks=%u%% max_gap_ms=%u", core,
               static_cast<unsigned>(idle_tick_pct),
               static_cast<unsigned>(max_gap * portTICK_PERIOD_MS));
    }
  }
}

}  // namespace

void log_task_begin(const char* task, const char* detail) {
  if (s.active) {
    log_warn("[TASK] begin(%s) while task=%s phase=%s still active; "
             "ending previous task",
             task ? task : "?", s.task, s.phase);
    log_task_end("superseded");
  }
  copy_field(s.task, sizeof(s.task), task ? task : "?");
  copy_field(s.phase, sizeof(s.phase), "init");
  copy_field(s.detail, sizeof(s.detail), detail);
  const unsigned long now = millis();
  s.task_start_ms = now;
  s.phase_start_ms = now;
  s.last_heartbeat_ms = now;
  s.last_stall_log_ms = 0;
  s.active = true;
  log_info("[TASK] START task=%s%s%s", s.task,
           s.detail[0] ? " detail=" : "", s.detail[0] ? s.detail : "");
}

void log_task_end(const char* result) {
  if (!s.active) {
    return;
  }
  log_info("[TASK] END task=%s phase=%s task_ms=%lu%s%s", s.task, s.phase,
           elapsed_ms(s.task_start_ms), result ? " result=" : "",
           result ? result : "");
  s.active = false;
  s.task[0] = '\0';
  s.phase[0] = '\0';
  s.detail[0] = '\0';
}

void log_task_phase(const char* phase, const char* detail) {
  if (!s.active) {
    log_task_begin("unknown", "phase-without-begin");
  }
  copy_field(s.phase, sizeof(s.phase), phase ? phase : "?");
  if (detail != nullptr) {
    copy_field(s.detail, sizeof(s.detail), detail);
  }
  const unsigned long now = millis();
  s.phase_start_ms = now;
  s.last_heartbeat_ms = now;
  s.last_stall_log_ms = 0;
  log_info("[TASK] PHASE task=%s phase=%s%s%s", s.task, s.phase,
           s.detail[0] ? " detail=" : "", s.detail[0] ? s.detail : "");
}

void log_task_pump(const char* detail) {
  if (!s.active) {
    return;
  }
  if (detail != nullptr) {
    copy_field(s.detail, sizeof(s.detail), detail);
  }
  log_alive_if_due();
}

void log_task_tick() {
  if (!s.active) {
    return;
  }
  const unsigned long phase_ms = elapsed_ms(s.phase_start_ms);
  if (phase_ms < DESKBOT_TASK_STALL_MS) {
    return;
  }
  const unsigned long now = millis();
  if (s.last_stall_log_ms != 0 &&
      now - s.last_stall_log_ms < DESKBOT_TASK_STALL_MS) {
    return;
  }
  s.last_stall_log_ms = now;
  log_warn("[TASK] STALL task=%s phase=%s task_ms=%lu phase_ms=%lu%s%s",
           s.task, s.phase, elapsed_ms(s.task_start_ms), phase_ms,
           s.detail[0] ? " detail=" : "", s.detail[0] ? s.detail : "");
}

void log_task_dump() {
  if (!s.active) {
    log_info("[TASK] idle (no active task)");
    return;
  }
  log_info("[TASK] NOW task=%s phase=%s task_ms=%lu phase_ms=%lu%s%s",
           s.task, s.phase, elapsed_ms(s.task_start_ms),
           elapsed_ms(s.phase_start_ms), s.detail[0] ? " detail=" : "",
           s.detail[0] ? s.detail : "");
}

LogTaskScope::LogTaskScope(const char* task, const char* detail) {
  log_task_begin(task, detail);
}

LogTaskScope::~LogTaskScope() { log_task_end(nullptr); }

void task_setup_cpu_runtime_stats() {
  const uint32_t now = static_cast<uint32_t>(xTaskGetTickCount());
  s_idle_last_tick[0] = now;
  s_idle_last_tick[1] = now;
  const esp_err_t e0 =
      esp_register_freertos_idle_hook_for_cpu(idle_hook_cpu0, 0);
  const esp_err_t e1 =
      esp_register_freertos_idle_hook_for_cpu(idle_hook_cpu1, 1);
  if (e0 != ESP_OK || e1 != ESP_OK) {
    log_error("[IDLE] hook register failed e0=%d e1=%d", static_cast<int>(e0),
              static_cast<int>(e1));
    return;
  }

  const BaseType_t ok = xTaskCreatePinnedToCore(
      idle_headroom_task, "idle_headroom", 3 * 1024, nullptr, 1, nullptr,
      APP_CPU_NUM);
  if (ok != pdPASS) {
    log_error("[IDLE] failed to start headroom monitor");
    return;
  }
  log_info("[IDLE] lock-free headroom monitor started interval=%ums",
           static_cast<unsigned>(DESKBOT_CPU_STATS_INTERVAL_MS));
}
