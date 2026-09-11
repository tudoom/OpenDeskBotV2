#include "head.h"

#include <ArduinoJson.h>
#include <Preferences.h>

#include <atomic>
#include <cmath>
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
#include "usb_transport.h"

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
/*
 * Y（俯仰）轴单独用 500–2500µs 全行程映射：1000–2000µs 在常见 500–2500µs
 * 舵机上只有约 ±45° 物理行程，导致逻辑 70–110 的点头幅度只有 ~±11°，
 * 整机观感「幅度太小」。X（偏航）保持 1000–2000µs——现有预设的横向
 * 幅度已按该映射调校，改了会让所有左右动作翻倍越界。
 */
static constexpr int kServoYPulseMinUs = 500;
static constexpr int kServoYPulseMaxUs = 2500;
/** duty 换算的物理夹取边界：取两轴映射的并集。 */
static constexpr int kServoPulseClampMinUs = kServoYPulseMinUs;
static constexpr int kServoPulseClampMaxUs = kServoYPulseMaxUs;
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

/*
 * 空闲释放（2026-09-08 USB 过流排查）：舵机只要有脉冲就一直输出保持力矩，
 * 顶到机械限位时是持续的堵转电流（单轴 0.6–0.8 A），叠加两轴满速运动的
 * 瞬态电流就会把 Mac 的 USB 口拉过流断电。到位后把 LEDC 占空比清零，舵机
 * 失去脉冲即松弛，保持/堵转电流归零；下一次运动前先按最后一次脉宽恢复
 * 输出，等一个 PWM 周期让舵机回到原位再开始斜坡。
 */
#ifndef DESKBOT_SERVO_IDLE_RELAX_MS
#define DESKBOT_SERVO_IDLE_RELAX_MS 150u
#endif
static std::atomic<uint32_t> s_servo_idle_relax_ms{DESKBOT_SERVO_IDLE_RELAX_MS};
static std::atomic<bool> s_servo_relaxed{false};
static std::atomic<uint32_t> s_servo_relax_count{0};
static uint32_t s_servo_last_write_ms = 0;   /* motor_task only */
static uint32_t s_servo_energized_at_ms = 0; /* motor_task only */
static constexpr const char* kServoPrefsNamespace = "servo";
static constexpr const char* kServoPrefsRelaxKey = "relax_ms";

static void head_sync_logical_pos(int x, int y);
static int head_deg_to_pulse_us(int deg);
static int head_y_deg_to_pulse_us(int deg);

static uint32_t head_servo_pulse_to_duty(int pulse_us) {
  pulse_us = constrain(pulse_us, kServoPulseClampMinUs, kServoPulseClampMaxUs);
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
  /* y_deg 是逻辑角：与运行期写入路径一致，先取反再走 Y 轴全行程映射。 */
  const int y_pulse = head_y_deg_to_pulse_us(head_y_logic_to_pwm(y_deg));
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

static bool head_servo_write_axis(int deg, int pulse_us_target,
                                  ledc_channel_t channel,
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
  /* 刚从空闲释放恢复：占空比是本 tick 才写回的，硬件下个周期才闩存，
   * 此刻读到的仍可能是 0；这一拍不做「上一次写入」校验。 */
  const bool just_energized =
      s_servo_energized_at_ms != 0 &&
      static_cast<uint32_t>(millis() - s_servo_energized_at_ms) <
          2u * SERVO_TICK_MS;
  const uint32_t observed_previous_duty =
      just_energized ? expected_previous_duty
                     : ledc_get_duty(kServoLedcMode, channel);
  const uint32_t observed_pwm_hz =
      ledc_get_freq(kServoLedcMode, kServoLedcTimer);
  /*
   * 精确相等在部分板子上会被时序抖动误杀：motor tick 恰与 PWM 周期同为
   * 20ms，读取偶尔落在新 duty 尚未闩存的边沿，观测值与期望差 1 个运动
   * 步进（实测 9-14 tick ≈ 1-1.5°），写入被丢弃导致动作丢拍。容差取
   * Y 轴全行程映射（11.1µs/°）下一个最大步进（3° ≈ 27 tick）的两倍；
   * 频率仍精确校验，通道死/频率错等真故障的偏差远超此带宽。
   */
  constexpr uint32_t kServoDutyVerifyTolerance = 60u;
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
  const int pulse_us = pulse_us_target;
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
  s_servo_last_write_ms = millis();
  return true;
}

/* 停止输出脉冲（占空比 0）：舵机松弛，保持/堵转电流归零。仅 motor_task。 */
static void head_servo_relax() {
  if (!s_servos_attached || s_servo_relaxed.load(std::memory_order_acquire)) {
    return;
  }
  (void)ledc_set_duty(kServoLedcMode, kServoLedcXChannel, 0);
  (void)ledc_update_duty(kServoLedcMode, kServoLedcXChannel);
  (void)ledc_set_duty(kServoLedcMode, kServoLedcYChannel, 0);
  (void)ledc_update_duty(kServoLedcMode, kServoLedcYChannel);
  s_servo_relaxed.store(true, std::memory_order_release);
  if (s_servo_relax_count.fetch_add(1, std::memory_order_relaxed) == 0) {
    log_info("[SERVO] idle relax armed: pulses stop %ums after the last move",
             static_cast<unsigned>(
                 s_servo_idle_relax_ms.load(std::memory_order_relaxed)));
  }
}

/* 恢复最后一次脉宽输出；调用方在开始斜坡前等一个 PWM 周期。仅 motor_task。 */
static void head_servo_energize() {
  if (!s_servos_attached || !s_servo_relaxed.load(std::memory_order_acquire)) {
    return;
  }
  const int x_pulse = s_servo_x_pulse_us.load(std::memory_order_acquire);
  const int y_pulse = s_servo_y_pulse_us.load(std::memory_order_acquire);
  (void)ledc_set_duty(kServoLedcMode, kServoLedcXChannel,
                      head_servo_pulse_to_duty(x_pulse));
  (void)ledc_update_duty(kServoLedcMode, kServoLedcXChannel);
  (void)ledc_set_duty(kServoLedcMode, kServoLedcYChannel,
                      head_servo_pulse_to_duty(y_pulse));
  (void)ledc_update_duty(kServoLedcMode, kServoLedcYChannel);
  s_servo_relaxed.store(false, std::memory_order_release);
  s_servo_energized_at_ms = millis();
  if (s_servo_energized_at_ms == 0) {
    s_servo_energized_at_ms = 1;
  }
  s_servo_last_write_ms = s_servo_energized_at_ms;
}

/* 每个运动 tick 调用：这一拍没有写 PWM 且已经静止超过 idle_relax_ms → 释放。 */
static void head_servo_idle_tick() {
  const uint32_t relax_ms =
      s_servo_idle_relax_ms.load(std::memory_order_relaxed);
  if (relax_ms == 0 || s_servo_relaxed.load(std::memory_order_acquire)) {
    return;
  }
  if (static_cast<uint32_t>(millis() - s_servo_last_write_ms) >= relax_ms) {
    head_servo_relax();
  }
}

static bool head_servo_write_x(int deg) {
  const int clamped = constrain(deg, X_MIN_LIMIT, X_MAX_LIMIT);
  if (!head_servo_write_axis(clamped, head_deg_to_pulse_us(clamped),
                             kServoLedcXChannel,
                             s_servo_x_pulse_us, "X")) {
    return false;
  }
  s_logical_x.store(clamped, std::memory_order_release);
  return true;
}

static bool head_servo_write_y(int deg) {
  const int clamped = constrain(deg, Y_MIN_LIMIT, Y_MAX_LIMIT);
  const int pwm_deg = head_y_logic_to_pwm(clamped);
  if (!head_servo_write_axis(pwm_deg, head_y_deg_to_pulse_us(pwm_deg),
                             kServoLedcYChannel,
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

uint32_t head_servo_idle_relax_ms() {
  return s_servo_idle_relax_ms.load(std::memory_order_relaxed);
}

bool head_servo_relaxed() {
  return s_servo_relaxed.load(std::memory_order_acquire);
}

static void head_servo_load_relax_prefs() {
  Preferences prefs;
  if (!prefs.begin(kServoPrefsNamespace, /*readOnly=*/true)) {
    return;
  }
  if (prefs.isKey(kServoPrefsRelaxKey)) {
    s_servo_idle_relax_ms.store(prefs.getUInt(kServoPrefsRelaxKey,
                                              DESKBOT_SERVO_IDLE_RELAX_MS),
                                std::memory_order_relaxed);
  }
  prefs.end();
}

void head_servo_set_idle_relax_ms(uint32_t ms) {
  if (ms > 60000u) {
    ms = 60000u;
  }
  s_servo_idle_relax_ms.store(ms, std::memory_order_relaxed);
  Preferences prefs;
  if (prefs.begin(kServoPrefsNamespace, /*readOnly=*/false)) {
    prefs.putUInt(kServoPrefsRelaxKey, ms);
    prefs.end();
  }
  log_warn("[SERVO] idle relax %s (ms=%u)", ms == 0 ? "disabled" : "enabled",
           static_cast<unsigned>(ms));
  /* 关闭释放时让 motor_task 立刻恢复力矩：塞一个 0ms 的保持命令即可，
   * 命令开头的 energize 会把脉冲写回。 */
  if (ms == 0 && s_servo_relaxed.load(std::memory_order_acquire)) {
    HeadPbServoCmd hold{};
    hold.xm = HEAD_SERVO_HOLD;
    hold.ym = HEAD_SERVO_HOLD;
    hold.ms = 0;
    (void)head_servo_cmd_pb_batch_async(&hold, 1, 0, "relax-off", 0);
  }
}

bool head_handle_control_json(const uint8_t* payload, size_t length) {
  if (payload == nullptr || length == 0 || length > 512) {
    return false;
  }
  StaticJsonDocument<256> doc;
  if (deserializeJson(doc, payload, length)) {
    return false;
  }
  const char* type = doc["type"] | "";
  if (strcmp(type, "servo_relax") == 0) {
    head_servo_set_idle_relax_ms(doc["ms"] | DESKBOT_SERVO_IDLE_RELAX_MS);
  } else if (strcmp(type, "servo_relax_req") != 0) {
    return false;
  }
  char json[128];
  snprintf(json, sizeof(json),
           "{\"type\":\"servo_relax_ack\",\"ms\":%u,\"relaxed\":%s,"
           "\"relax_count\":%u}",
           static_cast<unsigned>(head_servo_idle_relax_ms()),
           head_servo_relaxed() ? "true" : "false",
           static_cast<unsigned>(
               s_servo_relax_count.load(std::memory_order_relaxed)));
  (void)usb_transport_send_control_json(json);
  return true;
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

/* ---- 运动学：梯形速度曲线 / 换向停顿 / 热预算 ------------------------- */

}  // namespace

uint32_t head_servo_travel_min_ticks(uint32_t travel_deg, int deg_per_tick,
                                     int accel_ticks) {
  if (travel_deg == 0) {
    return 0;
  }
  const uint32_t v = static_cast<uint32_t>(deg_per_tick < 1 ? 1 : deg_per_tick);
  const uint32_t ta = static_cast<uint32_t>(accel_ticks < 0 ? 0 : accel_ticks);
  if (travel_deg >= v * ta) {
    /* 梯形：巡航 D/V + 加减速各 Ta（加减速合计正好多出 Ta 拍）。 */
    return (travel_deg + v - 1u) / v + ta;
  }
  /* 三角：2*sqrt(D*Ta/V)，取最小整数 n 满足 v*n*n >= 4*D*Ta。 */
  const uint32_t num = 4u * travel_deg * ta;
  uint32_t n = 0;
  while (v * n * n < num) {
    ++n;
  }
  return n;
}

uint32_t head_servo_travel_min_ms(uint32_t travel_deg) {
  return head_servo_travel_min_ticks(travel_deg,
                                     SERVO_USB_BOTH_AXES_STEP_DEG_PER_TICK,
                                     SERVO_ACCEL_TICKS) *
         SERVO_TICK_MS;
}

namespace {

constexpr float kServoHeatPerDeg = 1.0f;
constexpr float kServoHeatPerReversal = 5.0f;
constexpr float kServoCoolPerSec = 25.0f;
constexpr float kServoHeatCooldownEnter = 600.0f;
constexpr float kServoHeatCooldownExit = 300.0f;

struct AxisState {
  float heat = 0.0f;
  bool cooling = false;
  int last_dir = 0;
  uint32_t last_move_end_ms = 0;
};
AxisState s_axis[2];  /* 0 = X, 1 = Y；仅 motor_task 访问 */
uint32_t s_heat_last_cool_ms = 0;
std::atomic<int> s_heat_x_pub{0};
std::atomic<int> s_heat_y_pub{0};
std::atomic<uint32_t> s_cooldown_count{0};

void heat_cool_tick() {
  const uint32_t now = millis();
  if (s_heat_last_cool_ms == 0) {
    s_heat_last_cool_ms = now;
    return;
  }
  const uint32_t dt = now - s_heat_last_cool_ms;
  if (dt < 100u) {
    return;
  }
  s_heat_last_cool_ms = now;
  const float cool = kServoCoolPerSec * static_cast<float>(dt) / 1000.0f;
  for (auto& ax : s_axis) {
    ax.heat = ax.heat > cool ? ax.heat - cool : 0.0f;
    if (ax.cooling && ax.heat <= kServoHeatCooldownExit) {
      ax.cooling = false;
    }
  }
  s_heat_x_pub.store(static_cast<int>(s_axis[0].heat), std::memory_order_relaxed);
  s_heat_y_pub.store(static_cast<int>(s_axis[1].heat), std::memory_order_relaxed);
}

void heat_add(int axis, float amount, const char* label) {
  AxisState& ax = s_axis[axis];
  ax.heat += amount;
  if (!ax.cooling && ax.heat >= kServoHeatCooldownEnter) {
    ax.cooling = true;
    s_cooldown_count.fetch_add(1, std::memory_order_relaxed);
    log_warn("[SERVO] %s axis overheating budget=%d -> cooldown (moves skipped until %d)",
             label, static_cast<int>(ax.heat),
             static_cast<int>(kServoHeatCooldownExit));
  }
}

/* 梯形速度曲线：加速时间固定 Ta 拍；给的时间比物理最短长时降低巡航速度
 * 而不是提前到达后干等（观感更柔、峰值电流更低）。 */
struct AxisProfile {
  float d = 0.0f;
  int dir = 0;
  float a = 0.0f;
  float v = 0.0f;
  float t_acc = 0.0f;
  float d_acc = 0.0f;
  float t_cruise_end = 0.0f;
  uint32_t total_ticks = 0;
};

void profile_init(AxisProfile& p, int start, int target, int v_max,
                  uint32_t alloc_ticks) {
  const int delta = target - start;
  p.dir = delta > 0 ? 1 : (delta < 0 ? -1 : 0);
  p.d = static_cast<float>(delta < 0 ? -delta : delta);
  if (p.d <= 0.0f) {
    p.total_ticks = 0;
    return;
  }
  const float ta = static_cast<float>(SERVO_ACCEL_TICKS);
  float v = static_cast<float>(v_max);
  if (alloc_ticks > static_cast<uint32_t>(SERVO_ACCEL_TICKS)) {
    const float v_fit = p.d / static_cast<float>(alloc_ticks - SERVO_ACCEL_TICKS);
    if (v_fit < v) {
      v = v_fit;
    }
  }
  if (v < 0.05f) {
    v = 0.05f;
  }
  p.a = v / ta;
  if (p.d >= v * ta) {
    p.v = v;
    p.t_acc = ta;
    p.d_acc = 0.5f * v * ta;
    p.t_cruise_end = p.t_acc + (p.d - 2.0f * p.d_acc) / v;
  } else {
    p.t_acc = sqrtf(p.d / p.a);
    p.v = p.a * p.t_acc;
    p.d_acc = 0.5f * p.d;
    p.t_cruise_end = p.t_acc;
  }
  p.total_ticks = static_cast<uint32_t>(ceilf(p.t_cruise_end + p.t_acc));
}

float profile_travel(const AxisProfile& p, float n) {
  if (p.d <= 0.0f || n <= 0.0f) {
    return 0.0f;
  }
  if (n < p.t_acc) {
    return 0.5f * p.a * n * n;
  }
  if (n < p.t_cruise_end) {
    return p.d_acc + p.v * (n - p.t_acc);
  }
  const float t_end = p.t_cruise_end + p.t_acc;
  if (n >= t_end) {
    return p.d;
  }
  const float r = t_end - n;
  return p.d - 0.5f * p.a * r * r;
}

int profile_position(const AxisProfile& p, int start, float n) {
  const float travelled = profile_travel(p, n);
  const float pos = static_cast<float>(start) + static_cast<float>(p.dir) * travelled;
  return static_cast<int>(lroundf(pos));
}

/* 目标解析加两道保护：软限位边缘留 3°、与当前角差 ≤2° 视为到位。 */
int resolve_target_safe(uint8_t mode, int cur, int val, int lo, int hi) {
  int lo2 = lo + SERVO_EDGE_MARGIN_DEG;
  int hi2 = hi - SERVO_EDGE_MARGIN_DEG;
  if (lo2 > hi2) {
    lo2 = hi2 = (lo + hi) / 2;
  }
  int target = resolve_target(mode, cur, val, lo2, hi2);
  /* 当前角本身可能在边缘外（旧配置留下的）：这种情况允许回到安全区内。 */
  const int diff = target - cur;
  if (diff >= -SERVO_DEADBAND_DEG && diff <= SERVO_DEADBAND_DEG) {
    return cur;
  }
  return target;
}

}  // namespace

void head_servo_heat(int* x_heat, int* y_heat) {
  if (x_heat) *x_heat = s_heat_x_pub.load(std::memory_order_relaxed);
  if (y_heat) *y_heat = s_heat_y_pub.load(std::memory_order_relaxed);
}

uint32_t head_servo_cooldown_count() {
  return s_cooldown_count.load(std::memory_order_relaxed);
}

namespace {

/* ---- motor_task ---- */

void motor_task(void* /*arg*/) {
  MotorCmd cmd{};
  for (;;) {
    /* 空闲释放：静止 idle_relax_ms 内没有新命令就停脉冲；已释放则一直等。 */
    const uint32_t relax_ms =
        s_servo_idle_relax_ms.load(std::memory_order_relaxed);
    const TickType_t wait_ticks =
        (relax_ms == 0 || s_servo_relaxed.load(std::memory_order_acquire))
            ? pdMS_TO_TICKS(1000)
            : pdMS_TO_TICKS(relax_ms);
    if (xQueueReceive(s_motor_queue, &cmd, wait_ticks) != pdTRUE) {
      head_servo_idle_tick();
      heat_cool_tick();
      continue;
    }
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
    heat_cool_tick();
    int x_target = resolve_target_safe(cmd.xm, x, cmd.x, cmd.x_min, cmd.x_max);
    int y_target = resolve_target_safe(cmd.ym, y, cmd.y, cmd.y_min, cmd.y_max);
    /* 冷却中的轴不动（指令按时完成，只是不出力）。 */
    if (s_axis[0].cooling && x_target != x) {
      x_target = x;
    }
    if (s_axis[1].cooling && y_target != y) {
      y_target = y;
    }
    if (!motor_wait_for_pb_start(cmd)) {
      head_sync_logical_pos(x, y);
      head_emit_pb_terminal(cmd, HeadPbTerminalState::kCancelled, x, y);
      continue;
    }
    const bool moving = (x != x_target) || (y != y_target);
    /* 需要动才恢复脉冲（纯保持段不唤醒）；恢复后等一个 PWM 周期让舵机
     * 回到最后位置，再开始斜坡。关闭释放（ms=0）时无论动不动都恢复力矩。 */
    if (s_servo_relaxed.load(std::memory_order_acquire) &&
        (moving || s_servo_idle_relax_ms.load(std::memory_order_relaxed) == 0)) {
      head_servo_energize();
      vTaskDelay(k_tick_ticks);
    }

    bool execution_failed = false;
    if (moving) {
      const uint32_t x_travel = static_cast<uint32_t>(
          x_target >= x ? x_target - x : x - x_target);
      const uint32_t y_travel = static_cast<uint32_t>(
          y_target >= y ? y_target - y : y - y_target);
      /* 换向停顿：上一段刚停下就反向，先停一下再走。 */
      const int x_dir = x_target > x ? 1 : (x_target < x ? -1 : 0);
      const int y_dir = y_target > y ? 1 : (y_target < y ? -1 : 0);
      uint32_t pause_ms = 0;
      const uint32_t now0 = millis();
      const int dirs[2] = {x_dir, y_dir};
      for (int i = 0; i < 2; ++i) {
        AxisState& ax = s_axis[i];
        if (dirs[i] != 0 && ax.last_dir != 0 && dirs[i] != ax.last_dir) {
          const uint32_t since = now0 - ax.last_move_end_ms;
          if (since < SERVO_REVERSAL_PAUSE_MS) {
            const uint32_t need = SERVO_REVERSAL_PAUSE_MS - since;
            if (need > pause_ms) pause_ms = need;
          }
          heat_add(i, kServoHeatPerReversal, i == 0 ? "X" : "Y");
        }
      }
      if (pause_ms > 0) {
        vTaskDelay(pdMS_TO_TICKS(pause_ms));
      }
      /* 速度上限：USB 主机供电且两轴同时大幅运动时每轴减半。 */
      int v_max = SERVO_MAX_STEP_DEG_PER_TICK;
      if (x_travel > static_cast<uint32_t>(SERVO_BOTH_AXES_BIG_TRAVEL_DEG) &&
          y_travel > static_cast<uint32_t>(SERVO_BOTH_AXES_BIG_TRAVEL_DEG) &&
          usb_transport_is_active() && !usb_transport_active_link_is_wifi()) {
        v_max = SERVO_USB_BOTH_AXES_STEP_DEG_PER_TICK;
      }
      const uint32_t min_ticks_x =
          head_servo_travel_min_ticks(x_travel, v_max, SERVO_ACCEL_TICKS);
      const uint32_t min_ticks_y =
          head_servo_travel_min_ticks(y_travel, v_max, SERVO_ACCEL_TICKS);
      const uint32_t min_ticks = min_ticks_x > min_ticks_y ? min_ticks_x : min_ticks_y;
      /* 分配时长：PB 段按共享时间线的截止时刻，ms 模式按 ms；都不短于物理最短。 */
      const uint32_t start_ms = millis();
      uint32_t alloc_ticks = min_ticks;
      const bool pb_timeline = cmd.pb_tracked && cmd.pb_start_at_ms != 0 && cmd.ms > 0;
      uint32_t deadline_ms = 0;
      if (pb_timeline) {
        deadline_ms = cmd.pb_start_at_ms + cmd.ms;
        const uint32_t remain = deskbot_pb_time_remaining(start_ms, deadline_ms);
        const uint32_t remain_ticks = remain / SERVO_TICK_MS;
        if (remain_ticks > alloc_ticks) alloc_ticks = remain_ticks;
      } else if (cmd.ms > 0) {
        const uint32_t ms_ticks = cmd.ms / SERVO_TICK_MS;
        if (ms_ticks > alloc_ticks) alloc_ticks = ms_ticks;
      }
      /* 曲线的起点是本段起始角，整段固定；每拍只用它算理想位置。
       * （0.0.50 首版拿"当前角"当起点逐拍累加，位置一路跑飞到硬限位。） */
      const int x_start = x;
      const int y_start = y;
      AxisProfile px{}, py{};
      profile_init(px, x_start, x_target, v_max, alloc_ticks);
      profile_init(py, y_start, y_target, v_max, alloc_ticks);
      const uint32_t profile_ticks =
          px.total_ticks > py.total_ticks ? px.total_ticks : py.total_ticks;
      constexpr uint32_t kPbMotorExecutionGraceMs = 500u;
      const uint32_t watchdog_deadline_ms =
          start_ms + (alloc_ticks > profile_ticks ? alloc_ticks : profile_ticks) *
                         SERVO_TICK_MS +
          kPbMotorExecutionGraceMs;
      TickType_t last_wake = xTaskGetTickCount();
      while (!motor_cmd_cancelled(cmd)) {
        const uint32_t now_ms = millis();
        const float n = static_cast<float>(now_ms - start_ms) /
                        static_cast<float>(SERVO_TICK_MS);
        const bool arrived = (x == x_target) && (y == y_target);
        if (arrived) {
          /* 到位：PB 段等到截止时刻，ms 段等满时长（其间静止即释放）。 */
          const bool time_up = pb_timeline
                                   ? deskbot_pb_time_reached(now_ms, deadline_ms)
                                   : (n >= static_cast<float>(alloc_ticks));
          if (time_up) break;
          head_servo_idle_tick();
          heat_cool_tick();
          vTaskDelayUntil(&last_wake, k_tick_ticks);
          continue;
        }
        if (deskbot_pb_time_reached(now_ms, watchdog_deadline_ms)) {
          execution_failed = true;
          log_error("[SERVO] execution watchdog req=%s idx=%u pos=(%d,%d) "
                    "target=(%d,%d) alloc=%u ticks elapsed=%u ms",
                    cmd.pb_ack_req, (unsigned)cmd.pb_ack_idx, x, y, x_target,
                    y_target, (unsigned)alloc_ticks,
                    (unsigned)(now_ms - start_ms));
          break;
        }
        const int ideal_x = profile_position(px, x_start, n);
        const int ideal_y = profile_position(py, y_start, n);
        /* 迟到补追也不超过速度上限（物理安全优先于时间线）。 */
        const int new_x = constrain(step_toward(x, ideal_x, v_max), X_MIN_LIMIT, X_MAX_LIMIT);
        const int new_y = constrain(step_toward(y, ideal_y, v_max), Y_MIN_LIMIT, Y_MAX_LIMIT);
        if (new_x != x) {
          if (!head_servo_write_x(new_x)) { execution_failed = true; break; }
          heat_add(0, kServoHeatPerDeg * static_cast<float>(new_x > x ? new_x - x : x - new_x), "X");
          s_axis[0].last_dir = x_dir;
          x = new_x;
          if (x == x_target) s_axis[0].last_move_end_ms = millis();
        }
        if (new_y != y) {
          if (!head_servo_write_y(new_y)) { execution_failed = true; break; }
          heat_add(1, kServoHeatPerDeg * static_cast<float>(new_y > y ? new_y - y : y - new_y), "Y");
          s_axis[1].last_dir = y_dir;
          y = new_y;
          if (y == y_target) s_axis[1].last_move_end_ms = millis();
        }
        heat_cool_tick();
        vTaskDelayUntil(&last_wake, k_tick_ticks);
      }
    } else if (cmd.ms > 0) {
      /* 纯保持段：不出力，只等时间（PB 段等截止时刻）。 */
      const bool pb_timeline = cmd.pb_tracked && cmd.pb_start_at_ms != 0;
      const uint32_t deadline_ms = pb_timeline ? cmd.pb_start_at_ms + cmd.ms : millis() + cmd.ms;
      while (!motor_cmd_cancelled(cmd) && !deskbot_pb_time_reached(millis(), deadline_ms)) {
        head_servo_idle_tick();
        heat_cool_tick();
        vTaskDelay(k_tick_ticks);
      }
    } else if (cmd.hold_ms > 0) {
      uint32_t remain_ms = cmd.hold_ms;
      while (remain_ms > 0 && !motor_cmd_cancelled(cmd)) {
        const uint32_t tick_ms = remain_ms > SERVO_TICK_MS ? SERVO_TICK_MS : remain_ms;
        vTaskDelay(pdMS_TO_TICKS(tick_ms));
        remain_ms -= tick_ms;
        head_servo_idle_tick();
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
 * Y 轴角度 → 脉宽（µs）：0°=500 / 90°=1500 / 180°=2500 全行程映射。
 * 中位仍是 1500µs，与 X 轴及 gpio-center 的中位脉冲一致；相同逻辑角
 * 的物理俯仰幅度约为旧映射的 2 倍。
 */
static int head_y_deg_to_pulse_us(int deg) {
  deg = constrain(deg, 0, 180);
  return kServoYPulseMinUs +
         (deg * (kServoYPulseMaxUs - kServoYPulseMinUs)) / 180;
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
  head_servo_load_relax_prefs();
  /* 60ms/帧 ≈16.7Hz；每轴约 12 帧 ≈720ms，给机械到位留余量。 */
  static constexpr uint16_t kCenterPeriodMs = 60;
  static constexpr int      kCenterPulses   = 12;

  const int x_us = head_deg_to_pulse_us(constrain(X_CENTER, X_MIN_LIMIT, X_MAX_LIMIT));
  const int y_us = head_y_deg_to_pulse_us(
      head_y_logic_to_pwm(constrain(Y_CENTER, Y_MIN_LIMIT, Y_MAX_LIMIT)));

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
