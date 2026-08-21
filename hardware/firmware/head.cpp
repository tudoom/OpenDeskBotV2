#include "head.h"

#include <atomic>
#include <cstring>
#include <new>
#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_heap_caps.h>
#include <soc/gpio_periph.h>
#include <soc/io_mux_reg.h>

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "pb_timeline.h"

int X_CENTER = 90;
int Y_CENTER = 90;

/** motor_task 维护的逻辑角；未 attach 时 head_read_* 返回此值。 */
static std::atomic<int> s_logical_x{90};
static std::atomic<int> s_logical_y{90};

/** 全生命周期只 attach 一次；head_servo_boot_attach 在 camera 之后执行。 */
static bool s_servos_attached = false;

/** Keep every startup and runtime PWM path on the same calibrated range. */
static constexpr int kServoPulseMinUs = 1000;
static constexpr int kServoPulseMaxUs = 2000;
static constexpr uint32_t kServoPwmHz = 50;
static constexpr ledc_mode_t kServoLedcMode = LEDC_LOW_SPEED_MODE;
static constexpr ledc_timer_t kServoLedcTimer = LEDC_TIMER_3;
static constexpr ledc_channel_t kServoLedcXChannel = LEDC_CHANNEL_6;
static constexpr ledc_channel_t kServoLedcYChannel = LEDC_CHANNEL_7;
static constexpr ledc_timer_bit_t kServoLedcResolution = LEDC_TIMER_14_BIT;
static constexpr uint32_t kServoLedcPeriodTicks = 1u << 14;

static std::atomic<int> s_servo_x_pulse_us{0};
static std::atomic<int> s_servo_y_pulse_us{0};
static std::atomic<uint32_t> s_servo_write_failures{0};

static void head_sync_logical_pos(int x, int y);
static int head_deg_to_pulse_us(int deg);

static uint32_t head_servo_pulse_to_duty(int pulse_us) {
  pulse_us = constrain(pulse_us, kServoPulseMinUs, kServoPulseMaxUs);
  return (static_cast<uint32_t>(pulse_us) * kServoLedcPeriodTicks + 10000u) /
         20000u;
}

static bool head_servo_configure_channel(int pin, ledc_channel_t channel,
                                         int pulse_us, const char* label) {
  ledc_channel_config_t cfg = {};
  cfg.gpio_num = pin;
  cfg.speed_mode = kServoLedcMode;
  cfg.channel = channel;
  cfg.intr_type = LEDC_INTR_DISABLE;
  cfg.timer_sel = kServoLedcTimer;
  cfg.duty = head_servo_pulse_to_duty(pulse_us);
  cfg.hpoint = 0;
  cfg.sleep_mode = LEDC_SLEEP_MODE_NO_ALIVE_NO_PD;
  cfg.flags.output_invert = 0;
  const esp_err_t err = ledc_channel_config(&cfg);
  if (err != ESP_OK) {
    log_error("[SERVO] LEDC channel %s pin=%d ch=%d failed err=%s", label,
              pin, static_cast<int>(channel), esp_err_to_name(err));
    return false;
  }
  log_info("[SERVO] LEDC channel %s pin=%d ch=%d pulse=%dus duty=%u", label,
           pin, static_cast<int>(channel), pulse_us,
           static_cast<unsigned>(cfg.duty));
  return true;
}

static bool head_servo_attach_pins(int x_deg, int y_deg) {
  if (s_servos_attached) return true;
  ledc_timer_config_t timer = {};
  timer.speed_mode = kServoLedcMode;
  timer.duty_resolution = kServoLedcResolution;
  timer.timer_num = kServoLedcTimer;
  timer.freq_hz = kServoPwmHz;
  timer.clk_cfg = LEDC_AUTO_CLK;
  timer.deconfigure = false;
  const esp_err_t timer_err = ledc_timer_config(&timer);
  if (timer_err != ESP_OK) {
    log_error("[SERVO] LEDC timer failed timer=%d err=%s",
              static_cast<int>(kServoLedcTimer), esp_err_to_name(timer_err));
    return false;
  }
  const int x_pulse = head_deg_to_pulse_us(x_deg);
  const int y_pulse = head_deg_to_pulse_us(y_deg);
  if (!head_servo_configure_channel(Y_PIN, kServoLedcYChannel, y_pulse,
                                    "Y")) {
    return false;
  }
  if (!head_servo_configure_channel(X_PIN, kServoLedcXChannel, x_pulse,
                                    "X")) {
    (void)ledc_stop(kServoLedcMode, kServoLedcYChannel, 0);
    pinMode(Y_PIN, INPUT_PULLDOWN);
    return false;
  }
  s_servo_x_pulse_us.store(x_pulse, std::memory_order_release);
  s_servo_y_pulse_us.store(y_pulse, std::memory_order_release);
  s_servos_attached = true;
  log_info("[SERVO] attach ok backend=ledc timer=%d hz=%u pins=(%d,%d)",
           static_cast<int>(kServoLedcTimer),
           static_cast<unsigned>(ledc_get_freq(kServoLedcMode,
                                               kServoLedcTimer)),
           X_PIN, Y_PIN);
  return true;
}

static bool head_servo_ensure_attached(int x_deg, int y_deg) {
  x_deg = constrain(x_deg, X_MIN_LIMIT, X_MAX_LIMIT);
  y_deg = constrain(y_deg, Y_MIN_LIMIT, Y_MAX_LIMIT);
  if (!head_servo_attach_pins(x_deg, y_deg)) return false;
  head_sync_logical_pos(x_deg, y_deg);
  return true;
}

static bool head_servo_write_axis(int deg, ledc_channel_t channel,
                                  std::atomic<int>& pulse_store,
                                  const char* label) {
  if (!s_servos_attached) return false;
  /*
   * LEDC latches a newly requested duty on the next PWM period.  Validate the
   * previous request here, one motor tick later, instead of immediately after
   * ledc_update_duty() while the hardware may still expose the old duty.
   */
  const int previous_pulse_us =
      pulse_store.load(std::memory_order_acquire);
  const uint32_t expected_previous_duty =
      head_servo_pulse_to_duty(previous_pulse_us);
  const uint32_t observed_previous_duty =
      ledc_get_duty(kServoLedcMode, channel);
  const uint32_t observed_pwm_hz =
      ledc_get_freq(kServoLedcMode, kServoLedcTimer);
  /*
   * 精确相等在部分板子上会被时序抖动误杀：motor tick 恰与 PWM 周期同为
   * 20ms，读取偶尔落在新 duty 尚未闩存的边沿，观测值与期望差 1 个运动
   * 步进（实测 9-14 tick ≈ 1-1.5°），写入被丢弃导致动作丢拍。容差取
   * 一个最大步进（3° ≈ 27 tick）再留裕量；频率仍精确校验，通道死/频率
   * 错等真故障的偏差远超此带宽。
   */
  constexpr uint32_t kServoDutyVerifyTolerance = 30u;
  const uint32_t duty_delta =
      observed_previous_duty > expected_previous_duty
          ? observed_previous_duty - expected_previous_duty
          : expected_previous_duty - observed_previous_duty;
  if (previous_pulse_us <= 0 || observed_pwm_hz != kServoPwmHz ||
      duty_delta > kServoDutyVerifyTolerance) {
    s_servo_write_failures.fetch_add(1, std::memory_order_relaxed);
    log_error("[SERVO] prior PWM verify %s failed pulse=%d "
              "duty=%u/%u hz=%u/%u",
              label, previous_pulse_us,
              static_cast<unsigned>(observed_previous_duty),
              static_cast<unsigned>(expected_previous_duty),
              static_cast<unsigned>(observed_pwm_hz),
              static_cast<unsigned>(kServoPwmHz));
    return false;
  }
  const int pulse_us = head_deg_to_pulse_us(deg);
  const uint32_t duty = head_servo_pulse_to_duty(pulse_us);
  /*
   * All runtime PWM writes are serialized by motor_task, so the ordinary
   * LEDC set/update pair is the correct API here.  The nominally thread-safe
   * ledc_set_duty_and_update() depends on ledc_fade_func_install(); without
   * that unrelated ISR service ESP-IDF returns ESP_FAIL on every movement.
   */
  const esp_err_t set_err =
      ledc_set_duty(kServoLedcMode, channel, duty);
  const esp_err_t update_err =
      set_err == ESP_OK
          ? ledc_update_duty(kServoLedcMode, channel)
          : ESP_FAIL;
  if (set_err != ESP_OK || update_err != ESP_OK) {
    s_servo_write_failures.fetch_add(1, std::memory_order_relaxed);
    log_error("[SERVO] PWM write %s failed deg=%d pulse=%d duty=%u "
              "set_err=%s update_err=%s",
              label, deg, pulse_us, static_cast<unsigned>(duty),
              esp_err_to_name(set_err), esp_err_to_name(update_err));
    return false;
  }
  pulse_store.store(pulse_us, std::memory_order_release);
  return true;
}

static bool head_servo_write_x(int deg) {
  const int clamped = constrain(deg, X_MIN_LIMIT, X_MAX_LIMIT);
  if (!head_servo_write_axis(clamped, kServoLedcXChannel,
                             s_servo_x_pulse_us, "X")) {
    return false;
  }
  s_logical_x.store(clamped, std::memory_order_release);
  return true;
}

static bool head_servo_write_y(int deg) {
  const int clamped = constrain(deg, Y_MIN_LIMIT, Y_MAX_LIMIT);
  const int pwm_deg = head_y_logic_to_pwm(clamped);
  if (!head_servo_write_axis(pwm_deg, kServoLedcYChannel,
                             s_servo_y_pulse_us, "Y")) {
    return false;
  }
  s_logical_y.store(clamped, std::memory_order_release);
  return true;
}


/* ---------------------------------------------------------------
 * Motor task：把舵机斜坡推进搬到独立 FreeRTOS 任务里。
 *
 * 手势命令层（factory/action head_*）已删除：本任务只消费 PB servo[] 批次
 * 与开机回中这两类异步命令，没有任何同步等待包装；逻辑角由 motor_task 在
 * 命令完成后维护。
 * ------------------------------------------------------------- */

static QueueHandle_t s_pb_motor_terminal_q = nullptr;

static void head_sync_logical_pos(int x, int y) {
  s_logical_x.store(constrain(x, X_MIN_LIMIT, X_MAX_LIMIT),
                    std::memory_order_release);
  s_logical_y.store(constrain(y, Y_MIN_LIMIT, Y_MAX_LIMIT),
                    std::memory_order_release);
}

int head_read_x() { return s_logical_x.load(std::memory_order_acquire); }

int head_read_y_logic() {
  return s_logical_y.load(std::memory_order_acquire);
}

HeadServoHealthSnapshot head_servo_health_snapshot() {
  HeadServoHealthSnapshot snapshot{};
  snapshot.ready = s_servos_attached;
  snapshot.x_pin = X_PIN;
  snapshot.y_pin = Y_PIN;
  snapshot.pwm_hz =
      snapshot.ready
          ? ledc_get_freq(kServoLedcMode, kServoLedcTimer)
          : 0;
  snapshot.x_pulse_us =
      s_servo_x_pulse_us.load(std::memory_order_acquire);
  snapshot.y_pulse_us =
      s_servo_y_pulse_us.load(std::memory_order_acquire);
  snapshot.write_failures =
      s_servo_write_failures.load(std::memory_order_relaxed);
  return snapshot;
}

void head_log_position() {
  const int logical_x = s_logical_x.load(std::memory_order_acquire);
  const int logical_y = s_logical_y.load(std::memory_order_acquire);
  log_info("[HEAD] pos=(%d,%d) center=(%d,%d) lim X[%d,%d] Y[%d,%d] pwm=%s",
           logical_x, logical_y, X_CENTER, Y_CENTER,
           X_MIN_LIMIT, X_MAX_LIMIT, Y_MIN_LIMIT, Y_MAX_LIMIT,
           s_servos_attached ? "attached" : "deferred");
}

namespace {

constexpr TickType_t k_tick_ticks           = pdMS_TO_TICKS(SERVO_TICK_MS);
constexpr size_t     k_motor_queue_depth    = 32;

/** 与下行 JSON `servo` 同形（另含斜坡字段）。xm/ym 见 head.h HEAD_SERVO_*。 */
struct MotorCmd {
  uint8_t xm, ym;
  int     x,  y;
  int x_min = X_MIN_LIMIT;
  int x_max = X_MAX_LIMIT;
  int y_min = Y_MIN_LIMIT;
  int y_max = Y_MAX_LIMIT;
  uint32_t ms;       /**< Wall-clock duration; supports the PB 300 s limit. */
  uint32_t hold_ms;
  uint8_t  step_deg;
  uint32_t pb_ack_idx;
  char     pb_ack_req[DESKBOT_PB_REQ_BUFFER_SIZE];
  uint32_t cancel_epoch;
  bool     pb_tracked;
  uint32_t pb_epoch;
  uint32_t pb_start_at_ms;
};

QueueHandle_t     s_motor_queue       = nullptr;
TaskHandle_t      s_motor_task        = nullptr;
SemaphoreHandle_t s_motor_submit_lock = nullptr;
std::atomic<uint32_t> s_motor_cancel_epoch{0};
static std::atomic<uint32_t> s_head_pb_terminal_drop_count{0};
/*
 * Runtime creation is a boot-time transaction, not a polling side effect.
 * Once an allocation has failed there is no safe way to recover the partial
 * FreeRTOS object set while producers may already be running.  Latch the
 * failure until reboot so the USB service loop cannot retry task creation on
 * every terminal-queue poll and flood the transport/log lane.
 */
static std::atomic<bool> s_motor_runtime_failed{false};
static constexpr uint32_t kMotorTaskStackBytes = 6u * 1024u;

static void head_emit_pb_terminal(const MotorCmd& cmd,
                                  HeadPbTerminalState state,
                                  int commanded_x, int commanded_y) {
  if (!cmd.pb_tracked) {
    return;
  }
  if (!s_pb_motor_terminal_q) {
    s_head_pb_terminal_drop_count.fetch_add(
        1, std::memory_order_relaxed);
    log_error("[SERVO] PB terminal queue unavailable epoch=%u req=%s idx=%u",
              (unsigned)cmd.pb_epoch, cmd.pb_ack_req,
              (unsigned)cmd.pb_ack_idx);
    return;
  }
  HeadPbTerminalEvent out{};
  out.state = state;
  out.epoch = cmd.pb_epoch;
  out.idx = cmd.pb_ack_idx;
  out.commanded_x = static_cast<int16_t>(commanded_x);
  out.commanded_y = static_cast<int16_t>(commanded_y);
  out.pose_valid = true;
  strncpy(out.req, cmd.pb_ack_req, sizeof(out.req) - 1);
  if (xQueueSend(s_pb_motor_terminal_q, &out, 0) != pdTRUE) {
    s_head_pb_terminal_drop_count.fetch_add(
        1, std::memory_order_relaxed);
    log_error("[SERVO] PB terminal queue full epoch=%u req=%s idx=%u state=%u",
              (unsigned)out.epoch, out.req, (unsigned)out.idx,
              (unsigned)out.state);
  }
}

/** 根据 xm/ym 模式将命令值转换为目标角度。 */
static int resolve_target(uint8_t mode, int cur, int val, int lo, int hi) {
  if (mode == HEAD_SERVO_ABS) return constrain(val, lo, hi);
  if (mode == HEAD_SERVO_REL) {
    const int64_t wide_target =
        static_cast<int64_t>(cur) + static_cast<int64_t>(val);
    if (wide_target < static_cast<int64_t>(lo)) return lo;
    if (wide_target > static_cast<int64_t>(hi)) return hi;
    return static_cast<int>(wide_target);
  }
  return cur; /* HEAD_SERVO_HOLD 或非法值 */
}

/** 单步收敛：向 target 方向走最多 step，保证不越过目标（避免震荡）。 */
static int step_toward(int cur, int target, int step) {
  const int d = target - cur;
  return cur + (d > step ? step : d < -step ? -step : d);
}

/**
 * Interpolate one servo axis without mixing signed deltas with unsigned wall
 * time.  ESP32 uses a 32-bit ``long``; multiplying a negative delta by a
 * uint32_t progress value would otherwise promote the delta to unsigned and
 * turn a descending move into a very large positive target.
 */
static int interpolate_axis_clamped(int start, int target,
                                    uint32_t progress, uint32_t total,
                                    int hard_lo, int hard_hi) {
  /*
   * Keep ``start`` equal to the last commanded PWM position.  A command may
   * intentionally narrow its soft range while the head is outside that new
   * range; clamping the local start would fabricate a pose without writing
   * PWM and could immediately report a false completion.  The target was
   * already resolved inside the command's soft range.  Only the value written
   * on this step is bounded to the immutable hardware envelope.
   */
  target = constrain(target, hard_lo, hard_hi);
  if (total == 0 || progress >= total) {
    return target;
  }
  const int64_t delta =
      static_cast<int64_t>(target) - static_cast<int64_t>(start);
  const int64_t offset =
      delta * static_cast<int64_t>(progress) / static_cast<int64_t>(total);
  const int64_t ideal = static_cast<int64_t>(start) + offset;
  const int path_lo = start < target ? start : target;
  const int path_hi = start > target ? start : target;
  if (ideal < static_cast<int64_t>(path_lo)) return path_lo;
  if (ideal > static_cast<int64_t>(path_hi)) return path_hi;
  if (ideal < static_cast<int64_t>(hard_lo)) return hard_lo;
  if (ideal > static_cast<int64_t>(hard_hi)) return hard_hi;
  return static_cast<int>(ideal);
}

static bool motor_cmd_cancelled(const MotorCmd& cmd) {
  return cmd.cancel_epoch !=
         s_motor_cancel_epoch.load(std::memory_order_acquire);
}

static bool motor_wait_for_pb_start(const MotorCmd& cmd) {
  if (!cmd.pb_tracked || cmd.pb_start_at_ms == 0) {
    return true;
  }
  while (!motor_cmd_cancelled(cmd)) {
    const uint32_t remaining =
        deskbot_pb_time_remaining(millis(), cmd.pb_start_at_ms);
    if (remaining == 0) {
      return true;
    }
    const uint32_t slice_ms = remaining > 5u ? 5u : remaining;
    vTaskDelay(pdMS_TO_TICKS(slice_ms));
  }
  return false;
}

/* ---- motor_task ---- */

void motor_task(void* /*arg*/) {
  MotorCmd cmd{};
  for (;;) {
    if (xQueueReceive(s_motor_queue, &cmd, portMAX_DELAY) != pdTRUE) continue;
    if (motor_cmd_cancelled(cmd)) {
      head_emit_pb_terminal(cmd, HeadPbTerminalState::kCancelled,
                            head_read_x(), head_read_y_logic());
      continue;
    }

    int x = s_logical_x.load(std::memory_order_acquire);
    int y = s_logical_y.load(std::memory_order_acquire);
    if (!head_servo_ensure_attached(x, y)) {
      head_emit_pb_terminal(cmd, HeadPbTerminalState::kFailed, x, y);
      continue;
    }
    const int x_target =
        resolve_target(cmd.xm, x, cmd.x, cmd.x_min, cmd.x_max);
    const int y_target =
        resolve_target(cmd.ym, y, cmd.y, cmd.y_min, cmd.y_max);
    if (!motor_wait_for_pb_start(cmd)) {
      head_sync_logical_pos(x, y);
      head_emit_pb_terminal(cmd, HeadPbTerminalState::kCancelled, x, y);
      continue;
    }

    bool execution_failed = false;
    if (cmd.ms > 0 && cmd.pb_tracked && cmd.pb_start_at_ms != 0) {
      /*
       * PB motion follows the shared wall clock, but physical safety wins
       * over a stale/too-short deadline.  No PWM update may exceed 3 degrees
       * per 20 ms.  A late worker therefore catches up gradually and extends
       * completion instead of snapping to the target at the deadline.
       */
      constexpr int kPbMaxStepDegPerTick = 3;
      const int x_start = x;
      const int y_start = y;
      const uint32_t segment_end_at_ms = cmd.pb_start_at_ms + cmd.ms;
      const uint32_t x_travel = static_cast<uint32_t>(
          x_target >= x_start ? x_target - x_start : x_start - x_target);
      const uint32_t y_travel = static_cast<uint32_t>(
          y_target >= y_start ? y_target - y_start : y_start - y_target);
      const uint32_t travel = x_travel > y_travel ? x_travel : y_travel;
      const uint32_t physical_min_ms =
          ((travel + kPbMaxStepDegPerTick - 1u) /
           kPbMaxStepDegPerTick) *
          SERVO_TICK_MS;
      const uint32_t execution_budget_ms =
          cmd.ms > physical_min_ms ? cmd.ms : physical_min_ms;
      constexpr uint32_t kPbMotorExecutionGraceMs = 500u;
      const uint32_t execution_started_ms = millis();
      const uint32_t execution_watchdog_deadline_ms =
          execution_started_ms + execution_budget_ms +
          kPbMotorExecutionGraceMs;
      TickType_t last_wake = xTaskGetTickCount();

      while (!motor_cmd_cancelled(cmd)) {
        const uint32_t now_ms = millis();
        const bool deadline_reached =
            deskbot_pb_time_reached(now_ms, segment_end_at_ms);
        if (deadline_reached && x == x_target && y == y_target) {
          break;
        }
        if (deskbot_pb_time_reached(now_ms,
                                    execution_watchdog_deadline_ms)) {
          execution_failed = true;
          log_error("[SERVO] PB execution watchdog epoch=%u req=%s idx=%u "
                    "pos=(%d,%d) target=(%d,%d) budget=%u elapsed=%u",
                    (unsigned)cmd.pb_epoch, cmd.pb_ack_req,
                    (unsigned)cmd.pb_ack_idx, x, y, x_target, y_target,
                    (unsigned)execution_budget_ms,
                    (unsigned)(now_ms - execution_started_ms));
          break;
        }
        const uint32_t elapsed_ms = now_ms - cmd.pb_start_at_ms;
        const uint32_t clamped_ms =
            elapsed_ms < cmd.ms ? elapsed_ms : cmd.ms;
        const int ideal_x = interpolate_axis_clamped(
            x_start, x_target, clamped_ms, cmd.ms,
            X_MIN_LIMIT, X_MAX_LIMIT);
        const int ideal_y = interpolate_axis_clamped(
            y_start, y_target, clamped_ms, cmd.ms,
            Y_MIN_LIMIT, Y_MAX_LIMIT);
        const int new_x = constrain(
            step_toward(x, ideal_x, kPbMaxStepDegPerTick),
            X_MIN_LIMIT, X_MAX_LIMIT);
        const int new_y = constrain(
            step_toward(y, ideal_y, kPbMaxStepDegPerTick),
            Y_MIN_LIMIT, Y_MAX_LIMIT);
        if (new_x != x) {
          if (!head_servo_write_x(new_x)) {
            execution_failed = true;
            break;
          }
          x = new_x;
        }
        if (new_y != y) {
          if (!head_servo_write_y(new_y)) {
            execution_failed = true;
            break;
          }
          y = new_y;
        }
        vTaskDelayUntil(&last_wake, k_tick_ticks);
      }

    } else if (cmd.ms > 0) {
      /* ms 模式：Bresenham 整数线性插值，保证 ms 时间内均匀到达目标。 */
      const uint32_t total_ticks =
          (cmd.ms > SERVO_TICK_MS) ? cmd.ms / SERVO_TICK_MS : 1u;
      const int x_start = x, y_start = y;

      TickType_t last_wake = xTaskGetTickCount();
      for (uint32_t i = 1; i <= total_ticks; i++) {
        if (motor_cmd_cancelled(cmd)) break;
        const int ideal_x = interpolate_axis_clamped(
            x_start, x_target, i, total_ticks,
            X_MIN_LIMIT, X_MAX_LIMIT);
        const int ideal_y = interpolate_axis_clamped(
            y_start, y_target, i, total_ticks,
            Y_MIN_LIMIT, Y_MAX_LIMIT);
        const int new_x = constrain(ideal_x, X_MIN_LIMIT, X_MAX_LIMIT);
        const int new_y = constrain(ideal_y, Y_MIN_LIMIT, Y_MAX_LIMIT);
        if (new_x != x) {
          if (!head_servo_write_x(new_x)) {
            execution_failed = true;
            break;
          }
          x = new_x;
        }
        if (new_y != y) {
          if (!head_servo_write_y(new_y)) {
            execution_failed = true;
            break;
          }
          y = new_y;
        }
        vTaskDelayUntil(&last_wake, k_tick_ticks);
      }
      const uint32_t used_ms = (uint32_t)total_ticks * SERVO_TICK_MS;
      uint32_t remain_ms = used_ms < cmd.ms ? cmd.ms - used_ms : 0;
      while (remain_ms > 0 && !motor_cmd_cancelled(cmd)) {
        const uint32_t tick_ms = remain_ms > SERVO_TICK_MS ? SERVO_TICK_MS : remain_ms;
        vTaskDelay(pdMS_TO_TICKS(tick_ms));
        remain_ms -= tick_ms;
      }

    } else {
      /* 普通模式：以 step_deg 步进直到到达目标。 */
      const int step = cmd.step_deg ? cmd.step_deg : 1;
      TickType_t last_wake = xTaskGetTickCount();
      while (x != x_target || y != y_target) {
        if (motor_cmd_cancelled(cmd)) break;
        if (x != x_target) {
          const int next_x = step_toward(x, x_target, step);
          if (!head_servo_write_x(next_x)) {
            execution_failed = true;
            break;
          }
          x = next_x;
        }
        if (y != y_target) {
          const int next_y = step_toward(y, y_target, step);
          if (!head_servo_write_y(next_y)) {
            execution_failed = true;
            break;
          }
          y = next_y;
        }
        vTaskDelayUntil(&last_wake, k_tick_ticks);
      }
      uint32_t remain_ms = cmd.hold_ms;
      while (remain_ms > 0 && !motor_cmd_cancelled(cmd)) {
        const uint32_t tick_ms = remain_ms > SERVO_TICK_MS ? SERVO_TICK_MS : remain_ms;
        vTaskDelay(pdMS_TO_TICKS(tick_ms));
        remain_ms -= tick_ms;
      }
    }

    head_sync_logical_pos(x, y);
    const bool completed = !motor_cmd_cancelled(cmd);
    head_emit_pb_terminal(
        cmd, !completed ? HeadPbTerminalState::kCancelled
                        : execution_failed ? HeadPbTerminalState::kFailed
                                           : HeadPbTerminalState::kCompleted,
        x, y);
  }
}

/** 丢弃队列中尚未被 motor_task 取走的命令，并对 PB 命令补发取消终态。 */
void drain_motor_queue_nonblocking() {
  if (!s_motor_queue) return;
  MotorCmd dropped{};
  while (xQueueReceive(s_motor_queue, &dropped, 0) == pdTRUE) {
    head_emit_pb_terminal(dropped, HeadPbTerminalState::kCancelled,
                          head_read_x(), head_read_y_logic());
  }
}

bool ensure_motor_task() {
  if (s_motor_runtime_failed.load(std::memory_order_acquire)) {
    return false;
  }
  if (!s_pb_motor_terminal_q)
    s_pb_motor_terminal_q =
        xQueueCreate(64, sizeof(HeadPbTerminalEvent));
  /* Fast path: every resource required by producers and the actor exists. */
  if (s_pb_motor_terminal_q && s_motor_queue && s_motor_task &&
      s_motor_submit_lock) {
    return true;
  }
  if (!s_motor_queue)
    s_motor_queue = xQueueCreate(k_motor_queue_depth, sizeof(MotorCmd));
  if (!s_motor_submit_lock) s_motor_submit_lock = xSemaphoreCreateMutex();
  if (!s_pb_motor_terminal_q || !s_motor_queue || !s_motor_submit_lock) {
    s_motor_runtime_failed.store(true, std::memory_order_release);
    log_error(
        "[SERVO] runtime resource allocation failed; motor disabled "
        "internal_free=%u internal_largest=%u",
        static_cast<unsigned>(
            heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
        static_cast<unsigned>(heap_caps_get_largest_free_block(
            MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
    return false;
  }
  if (!s_motor_task) {
    /* core 1 (APP_CPU)，优先级 3：高于 act/anim，低于音频（5），确保 I2S 不被抢占。
     * 栈 6KB：PWM/日志所需余量；FFat 和 JSON 均不在本任务执行。 */
    TaskHandle_t created_task = nullptr;
    const BaseType_t rc = xTaskCreatePinnedToCore(
        motor_task, "motor", kMotorTaskStackBytes, nullptr, 3, &created_task,
        APP_CPU_NUM);
    if (rc != pdPASS || !created_task) {
      s_motor_task = nullptr;
      s_motor_runtime_failed.store(true, std::memory_order_release);
      log_error(
          "[SERVO] motor task creation failed rc=%d; motor disabled "
          "stack=%u internal_free=%u internal_largest=%u",
          (int)rc, static_cast<unsigned>(kMotorTaskStackBytes),
          static_cast<unsigned>(
              heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
          static_cast<unsigned>(heap_caps_get_largest_free_block(
              MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)));
      return false;
    }
    s_motor_task = created_task;
  }
  return true;
}

/** Strict producer transaction: capacity is checked while every producer and
 * reset path is excluded. The consumer may only increase free capacity, so
 * all sends are guaranteed after the preflight check. */
static bool enqueue_motor_batch_strict(const MotorCmd* commands,
                                       size_t command_count) {
  if (commands == nullptr || command_count == 0) {
    return false;
  }
  if (!ensure_motor_task()) {
    return false;
  }
  constexpr TickType_t kSubmitLockWait = pdMS_TO_TICKS(100);
  if (xSemaphoreTake(s_motor_submit_lock, kSubmitLockWait) != pdTRUE) {
    log_error("[SERVO] producer lock timeout; batch rejected count=%u",
              (unsigned)command_count);
    return false;
  }
  if (command_count > uxQueueSpacesAvailable(s_motor_queue)) {
    xSemaphoreGive(s_motor_submit_lock);
    log_warn("[SERVO] queue capacity rejected batch count=%u free=%u",
             (unsigned)command_count,
             (unsigned)uxQueueSpacesAvailable(s_motor_queue));
    return false;
  }

  const uint32_t submit_epoch =
      s_motor_cancel_epoch.load(std::memory_order_acquire);
  for (size_t i = 0; i < command_count; ++i) {
    MotorCmd command = commands[i];
    command.cancel_epoch = submit_epoch;
    if (xQueueSend(s_motor_queue, &command, 0) != pdTRUE) {
      /* This should be unreachable while producers share this lock. Fail
       * closed if the RTOS invariant is ever violated: cancel and drain the
       * partially visible batch instead of executing a prefix. */
      s_motor_cancel_epoch.fetch_add(1, std::memory_order_acq_rel);
      drain_motor_queue_nonblocking();
      xSemaphoreGive(s_motor_submit_lock);
      log_error("[SERVO] atomic batch commit failed at %u/%u; queue cancelled",
                (unsigned)i, (unsigned)command_count);
      return false;
    }
  }
  xSemaphoreGive(s_motor_submit_lock);
  return true;
}

/** Submit one motor command asynchronously (fire-and-forget enqueue).
 * 手势命令层删除后唯一调用方是开机回中。原同步 wait 包装（done_sem/
 * caller_lock/sync_disabled 闩）随之删除：不再阻塞调用任务，也不会在超时
 * 路径调用 head_clear_motor_pending() 令全局 cancel epoch 跨 lane 误伤
 * PB servo 队列。 */
void submit_motor(uint8_t xm, int x, uint8_t ym, int y,
                  uint32_t hold_ms, uint8_t step_deg, uint32_t ms) {
  if (!ensure_motor_task()) {
    log_error("[SERVO] motor submission rejected: runtime unavailable");
    return;
  }
  MotorCmd cmd{};
  cmd.xm = xm;
  cmd.x = x;
  cmd.ym = ym;
  cmd.y = y;
  cmd.ms = ms;
  cmd.hold_ms = hold_ms;
  cmd.step_deg = step_deg;
  if (!enqueue_motor_batch_strict(&cmd, 1)) {
    log_error("[SERVO] motor submission queue rejected");
  }
}
}  // namespace

/* ================================================================
 * 公开接口实现
 * ================================================================ */

/* ---- 初始化 ---- */

void head_servo_boot_attach() {
  if (!head_servo_attach_pins(head_read_x(), head_read_y_logic())) {
    log_error("[SERVO] boot: attach failed, motor_task starts without servo");
    (void)ensure_motor_task();
    return;
  }
  if (!ensure_motor_task()) {
    log_error("[SERVO] boot: runtime unavailable; keeping centered PWM only");
    return;
  }
  /* 双轴 attach 完成后统一回中：异步入队由 motor_task 缓动（boot 不等待）；
   * 逻辑角已在中位时直接同步逻辑角。 */
  const int lx = s_logical_x.load(std::memory_order_acquire);
  const int ly = s_logical_y.load(std::memory_order_acquire);
  submit_motor(HEAD_SERVO_ABS, X_CENTER, HEAD_SERVO_ABS, Y_CENTER, 0, 0, 0);
  if (lx == X_CENTER && ly == Y_CENTER) {
    head_sync_logical_pos(X_CENTER, Y_CENTER);
  }
  log_info("[SERVO] boot center (%d,%d)", X_CENTER, Y_CENTER);
}

/**
 * 角度 → 脉宽（µs）。约定 0°=1000 / 90°=1500 / 180°=2000，与常见模拟舵机一致。
 */
static int head_deg_to_pulse_us(int deg) {
  deg = constrain(deg, 0, 180);
  return kServoPulseMinUs +
         (deg * (kServoPulseMaxUs - kServoPulseMinUs)) / 180;
}

/**
 * GPIO 位bang 单轴中位脉冲串（不经过 Servo.attach）。
 *
 * 帧周期拉长到 period_ms（默认 60）只能降低指令刷新率，不能限制舵机转速：
 * 内部闭环在收到中位脉宽后仍会全速追位。两轴串行，降低同时堵转力矩。
 * 永久 PWM 仍由后续 head_servo_boot_attach 负责。
 */
static void head_gpio_soft_center_axis(int pin, int pulse_us, uint16_t period_ms, int pulses,
                                       const char* label) {
  const gpio_num_t gpio = (gpio_num_t)pin;
  PIN_FUNC_SELECT(GPIO_PIN_MUX_REG[pin], PIN_FUNC_GPIO);
  gpio_config_t cfg = {};
  cfg.pin_bit_mask  = 1ULL << pin;
  cfg.mode          = GPIO_MODE_OUTPUT;
  cfg.pull_up_en    = GPIO_PULLUP_DISABLE;
  cfg.pull_down_en  = GPIO_PULLDOWN_ENABLE;
  cfg.intr_type     = GPIO_INTR_DISABLE;
  gpio_config(&cfg);
  gpio_set_level(gpio, 0);

  /* 低电平段：帧周期 − 脉宽；period_ms 按「上升沿间隔」理解。 */
  const uint32_t low_us =
      (period_ms * 1000u > (uint32_t)pulse_us) ? (period_ms * 1000u - (uint32_t)pulse_us) : 0;

  log_info("[HEAD] gpio-center %s pin=%d pulse=%dus period=%ums n=%d", label, pin, pulse_us,
           (unsigned)period_ms, pulses);

  for (int i = 0; i < pulses; i++) {
    gpio_set_level(gpio, 1);
    delayMicroseconds(pulse_us);
    gpio_set_level(gpio, 0);
    if (low_us >= 1000) {
      delay(low_us / 1000);
      delayMicroseconds(low_us % 1000);
    } else if (low_us > 0) {
      delayMicroseconds(low_us);
    }
  }
  gpio_set_level(gpio, 0);
}

void setup_head() {
  /* 60ms/帧 ≈16.7Hz；每轴约 12 帧 ≈720ms，给机械到位留余量。 */
  static constexpr uint16_t kCenterPeriodMs = 60;
  static constexpr int      kCenterPulses   = 12;

  const int x_us = head_deg_to_pulse_us(constrain(X_CENTER, X_MIN_LIMIT, X_MAX_LIMIT));
  const int y_us = head_deg_to_pulse_us(constrain(Y_CENTER, Y_MIN_LIMIT, Y_MAX_LIMIT));

  log_info("[HEAD] gpio-center begin center=(%d,%d) us=(%d,%d) period=%ums pulses=%d/axis",
           X_CENTER, Y_CENTER, x_us, y_us, (unsigned)kCenterPeriodMs, kCenterPulses);

  head_gpio_soft_center_axis(Y_PIN, y_us, kCenterPeriodMs, kCenterPulses, "Y");
  head_gpio_soft_center_axis(X_PIN, x_us, kCenterPeriodMs, kCenterPulses, "X");

  s_logical_x.store(X_CENTER, std::memory_order_release);
  s_logical_y.store(Y_CENTER, std::memory_order_release);
  /*
   * Reserve the actor and its stack before ESP-SR/camera consume fragmented
   * internal RAM.  The actor blocks on its queue and does not touch PWM until
   * head_servo_boot_attach() has run after camera initialisation.
   */
  if (!ensure_motor_task()) {
    log_error("[SERVO] early runtime reservation failed; PWM stays centered");
  }
  log_info("[HEAD] gpio-center done (await camera then permanent attach)");
}

/* ---- 任务管理 ---- */

void head_clear_motor_pending() {
  if (!ensure_motor_task()) {
    s_motor_cancel_epoch.fetch_add(1, std::memory_order_acq_rel);
    drain_motor_queue_nonblocking();
    return;
  }
  constexpr TickType_t kResetLockWait = pdMS_TO_TICKS(100);
  if (xSemaphoreTake(s_motor_submit_lock, kResetLockWait) != pdTRUE) {
    /* Cancellation still wins even if a broken producer holds the lock. */
    s_motor_cancel_epoch.fetch_add(1, std::memory_order_acq_rel);
    drain_motor_queue_nonblocking();
    log_error("[SERVO] reset producer lock timeout; forced cancellation");
    return;
  }
  s_motor_cancel_epoch.fetch_add(1, std::memory_order_acq_rel);
  drain_motor_queue_nonblocking();
  xSemaphoreGive(s_motor_submit_lock);
}

bool head_servo_cmd_pb_batch_async(const HeadPbServoCmd* commands,
                                   size_t command_count, uint32_t epoch,
                                   const char* req, uint32_t idx) {
  constexpr uint32_t kPbMaxDurationMs = 300000u;
  if (commands == nullptr || command_count == 0 ||
      command_count > k_motor_queue_depth || epoch == 0 || req == nullptr ||
      req[0] == '\0') {
    return false;
  }
  /* Validate the complete batch before allocating or exposing any command to
   * the actor.  Do not put 32 MotorCmd values on Arduino's loopTask stack:
   * this call is nested below the PB preflight/staging frames and the former
   * array consumed more than half of the default 8 KiB stack by itself. */
  uint32_t total_duration_ms = 0;
  for (size_t i = 0; i < command_count; ++i) {
    const HeadPbServoCmd& input = commands[i];
    const bool x_value_valid =
        (input.xm == HEAD_SERVO_ABS && input.x >= X_MIN_LIMIT &&
         input.x <= X_MAX_LIMIT) ||
        (input.xm == HEAD_SERVO_REL && input.x >= -180 &&
         input.x <= 180) ||
        (input.xm == HEAD_SERVO_HOLD && input.x == 0);
    const bool y_value_valid =
        (input.ym == HEAD_SERVO_ABS && input.y >= Y_MIN_LIMIT &&
         input.y <= Y_MAX_LIMIT) ||
        (input.ym == HEAD_SERVO_REL && input.y >= -40 && input.y <= 40) ||
        (input.ym == HEAD_SERVO_HOLD && input.y == 0);
    const int64_t abs_x = input.x < 0
                              ? -static_cast<int64_t>(input.x)
                              : static_cast<int64_t>(input.x);
    const int64_t abs_y = input.y < 0
                              ? -static_cast<int64_t>(input.y)
                              : static_cast<int64_t>(input.y);
    const bool x_soft_valid =
        (input.xm == HEAD_SERVO_ABS && input.x >= input.x_min &&
         input.x <= input.x_max) ||
        (input.xm == HEAD_SERVO_REL &&
         abs_x <= static_cast<int64_t>(input.x_max) -
                      static_cast<int64_t>(input.x_min)) ||
        input.xm == HEAD_SERVO_HOLD;
    const bool y_soft_valid =
        (input.ym == HEAD_SERVO_ABS && input.y >= input.y_min &&
         input.y <= input.y_max) ||
        (input.ym == HEAD_SERVO_REL &&
         abs_y <= static_cast<int64_t>(input.y_max) -
                      static_cast<int64_t>(input.y_min)) ||
        input.ym == HEAD_SERVO_HOLD;
    if (input.xm > HEAD_SERVO_HOLD || input.ym > HEAD_SERVO_HOLD ||
        !x_value_valid || !y_value_valid || !x_soft_valid || !y_soft_valid ||
        input.x_min < X_MIN_LIMIT || input.x_max > X_MAX_LIMIT ||
        input.y_min < Y_MIN_LIMIT || input.y_max > Y_MAX_LIMIT ||
        input.x_min > input.x_max || input.y_min > input.y_max ||
        input.ms < 50u || input.ms > kPbMaxDurationMs ||
        input.start_at_ms == 0) {
      log_warn("[SERVO] invalid PB batch segment epoch=%u req=%s idx=%u seg=%u",
               (unsigned)epoch, req, (unsigned)idx, (unsigned)i);
      return false;
    }
    if (input.ms > kPbMaxDurationMs - total_duration_ms) {
      log_warn("[SERVO] PB batch duration exceeds limit epoch=%u req=%s idx=%u",
               (unsigned)epoch, req, (unsigned)idx);
      return false;
    }
    total_duration_ms += input.ms;
  }

  const size_t staged_bytes = command_count * sizeof(MotorCmd);
  MotorCmd* staged = static_cast<MotorCmd*>(heap_caps_malloc(
      staged_bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (staged == nullptr) {
    staged = static_cast<MotorCmd*>(
        heap_caps_malloc(staged_bytes, MALLOC_CAP_8BIT));
  }
  if (staged == nullptr) {
    /* Allocation occurs before enqueue_motor_batch_strict(), therefore heap
     * pressure rejects the whole PB batch with no executable prefix. */
    log_error("[SERVO] PB batch staging allocation failed count=%u bytes=%u",
              (unsigned)command_count, (unsigned)staged_bytes);
    return false;
  }

  for (size_t i = 0; i < command_count; ++i) {
    const HeadPbServoCmd& input = commands[i];
    new (&staged[i]) MotorCmd{};
    MotorCmd& command = staged[i];
    command.xm = input.xm;
    command.ym = input.ym;
    command.x = input.x;
    command.y = input.y;
    command.x_min = input.x_min;
    command.x_max = input.x_max;
    command.y_min = input.y_min;
    command.y_max = input.y_max;
    command.ms = input.ms;
    command.step_deg = 3;
    command.pb_tracked = true;
    command.pb_epoch = epoch;
    command.pb_ack_idx = idx;
    command.pb_start_at_ms = input.start_at_ms;
    strncpy(command.pb_ack_req, req, sizeof(command.pb_ack_req) - 1);
    command.pb_ack_req[sizeof(command.pb_ack_req) - 1] = '\0';
  }
  const bool queued = enqueue_motor_batch_strict(staged, command_count);
  for (size_t i = 0; i < command_count; ++i) {
    staged[i].~MotorCmd();
  }
  heap_caps_free(staged);
  return queued;
}

bool head_take_pb_terminal_event(HeadPbTerminalEvent* out) {
  if (out == nullptr) {
    return false;
  }
  /* Hot-path observation must never allocate resources or create a task. */
  return s_pb_motor_terminal_q != nullptr &&
         xQueueReceive(s_pb_motor_terminal_q, out, 0) == pdTRUE;
}

uint32_t head_pb_terminal_drop_count() {
  return s_head_pb_terminal_drop_count.load(std::memory_order_acquire);
}

unsigned head_motor_input_queue_depth() {
  /* Telemetry is passive; boot owns runtime initialisation. */
  return s_motor_queue ? (unsigned)uxQueueMessagesWaiting(s_motor_queue) : 0u;
}
