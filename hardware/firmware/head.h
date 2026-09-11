#ifndef Head_h
#define Head_h

#include <stddef.h>
#include "common.h"
#include "deskbot_config.h"
#include "pb_completed_store.h"

// Servo（见 deskbot_config.h）
#define X_PIN DESKBOT_ROM_X_PIN
#define Y_PIN DESKBOT_ROM_Y_PIN

/** 舵机物理极限（°）；所有运动均 constrain 于此。
 *
 * Y_MIN_LIMIT 从 70 收到 78（2026-09-08）：逻辑 70 是「低头到底」，实机上
 * 头部已经顶在外壳上，舵机堵转电流一直在高位；顶着不动 8–20 秒就把 Mac 的
 * USB 口拉过流断电（当天三次真机复现，复位原因全是上电复位）。留 8° 余量
 * 后舵机能真正到位，只剩很小的保持电流。PC 侧 SERVO_HARDWARE_ENVELOPE
 * 与此锁步。 */
#define X_MIN_LIMIT 20
#define X_MAX_LIMIT 160
#define Y_MIN_LIMIT 78
#define Y_MAX_LIMIT 110
/** 舵机 PWM 更新周期（ms）= 50Hz，motor_task 每拍间隔。 */
constexpr uint16_t SERVO_TICK_MS = 20;

/** 逻辑中位（固定 90/90）。原 factory adjust_*_center 偏移接口已随手势命令层删除。 */
extern int X_CENTER;
extern int Y_CENTER;

struct HeadServoHealthSnapshot {
  bool ready = false;
  int x_pin = X_PIN;
  int y_pin = Y_PIN;
  uint32_t pwm_hz = 0;
  int x_pulse_us = 0;
  int y_pulse_us = 0;
  uint32_t write_failures = 0;
};

/** Driver-visible PWM state; this is not a physical position sensor. */
HeadServoHealthSnapshot head_servo_health_snapshot();

/**
 * V2 上下舵机的机械方向与逻辑坐标相反：逻辑小角度向上、大角度向下。
 * 以 90° 为中心镜像后，逻辑 70..110 对应 PWM 110..70。
 */
inline int head_y_logic_to_pwm(int y_logic) { return 180 - y_logic; }
inline int head_y_pwm_to_logic(int y_pwm) { return 180 - y_pwm; }

/** 读 X 轴 PWM 目标角（逻辑角）；无物理反馈，不等于机械真实位置。 */
int head_read_x();
/** 读 Y 轴 PWM 目标角（逻辑角）；同上。 */
int head_read_y_logic();
/** 串口打印 PWM 目标角、中位、限位与 attach 状态（非机械实测）。 */
void head_log_position();

/** 与下行 JSON `servo.xm` / `servo.ym` 一致；motor 队列内 `MotorCmd` 使用同一编码。 */
constexpr uint8_t HEAD_SERVO_ABS = 0;
constexpr uint8_t HEAD_SERVO_REL = 1;
constexpr uint8_t HEAD_SERVO_HOLD = 2;

enum class HeadPbTerminalState : uint8_t {
  kCompleted = 0,
  kFailed = 1,
  kCancelled = 2,
};

struct HeadPbTerminalEvent {
  HeadPbTerminalState state = HeadPbTerminalState::kFailed;
  uint32_t epoch = 0;
  uint32_t idx = 0;
  int16_t commanded_x = 0;
  int16_t commanded_y = 0;
  bool pose_valid = false;
  char req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
};

/** One validated PB servo segment. Relative values are resolved by the
 * motor actor, in execution order, rather than by the USB parser. */
struct HeadPbServoCmd {
  uint8_t xm = HEAD_SERVO_HOLD;
  uint8_t ym = HEAD_SERVO_HOLD;
  int x = 0;
  int y = 0;
  int x_min = X_MIN_LIMIT;
  int x_max = X_MAX_LIMIT;
  int y_min = Y_MIN_LIMIT;
  int y_max = Y_MAX_LIMIT;
  uint32_t ms = 0;
  uint32_t start_at_ms = 0;
};

// Functions
/**
 * 相机 init 之前调用：GPIO 位bang 中位脉宽预归中（不 attach）。
 * 须在 setup_camera 之前；永久 attach 仍由 head_servo_boot_attach 完成。
 */
void setup_head();
/** 摄像头 init 之后调用：双轴永久 attach → 回中 (X_CENTER/Y_CENTER)，启动 motor_task。 */
void head_servo_boot_attach();
/** PB 严格提交：队列满立即返回 false，绝不淘汰已排队的动作。worker 在 ramp
 * 完成、attach/执行失败或 reset 取消后回报带 epoch/req/idx 的终态。 */
bool head_servo_cmd_pb_batch_async(const HeadPbServoCmd* commands,
                                   size_t command_count, uint32_t epoch,
                                   const char* req, uint32_t idx);
bool head_take_pb_terminal_event(HeadPbTerminalEvent* out);
/** Monotonic count of tracked terminal events that could not be queued. */
uint32_t head_pb_terminal_drop_count();
/** 非阻塞排空 motor 的 FreeRTOS 输入队列（尚未被 motor_task 取走的 cmd），
 *  并递增全局取消 epoch。当前已在执行的 ramp 在下个 tick 观察到取消。
 *  仅供会话级 PB 取消/中止路径使用（手势命令层已删除）。 */
void head_clear_motor_pending();

unsigned head_motor_input_queue_depth();

/* 运动学与保护常量（与 PC 端 servo_protocol.py 锁步，见
 * tests/test_servo_contract_lockstep.py）：
 *  - 每拍最大步进 2°（100°/s），加/减速各 5 拍（100 ms）的梯形速度曲线；
 *  - USB 主机供电且两轴同时大幅（>30°）运动时每轴降到 1°/拍，峰值电流减半；
 *  - 目标在软限位边缘 3° 内只到边缘减 3°，永不推到止点；
 *  - 目标与当前角差 ≤2° 视为到位，不唤醒不起动；
 *  - 换向前若上一段刚停下（<60 ms）先停 60 ms，消除急停反向冲击。 */
constexpr int SERVO_MAX_STEP_DEG_PER_TICK = 2;
constexpr int SERVO_ACCEL_TICKS = 5;
constexpr int SERVO_USB_BOTH_AXES_STEP_DEG_PER_TICK = 1;
constexpr int SERVO_BOTH_AXES_BIG_TRAVEL_DEG = 30;
constexpr int SERVO_EDGE_MARGIN_DEG = 3;
constexpr int SERVO_DEADBAND_DEG = 2;
constexpr uint32_t SERVO_REVERSAL_PAUSE_MS = 60;

/* 一段行程在梯形曲线下最少要多少拍：整数算法，PC 端 servo_protocol.
 * servo_travel_min_ticks 与之逐位一致。 */
uint32_t head_servo_travel_min_ticks(uint32_t travel_deg, int deg_per_tick,
                                     int accel_ticks);
/* 最坏情况（1°/拍）的最少毫秒数：asr_chat_client 的 PB 时间线预算用。 */
uint32_t head_servo_travel_min_ms(uint32_t travel_deg);

/* 热预算（没有电流采样的"堵转/过热"替身）：每度行程 +1、每次换向 +5，
 * 静止每秒 -25；超过 600 进入冷却（该轴不再执行运动）直到降回 300。 */
void head_servo_heat(int* x_heat, int* y_heat);
uint32_t head_servo_cooldown_count();

/* 空闲释放：舵机到位且 idle_relax_ms 内没有新的 PWM 写入时停止输出脉冲，
 * 保持电流/堵转电流归零；下一次运动前自动恢复。0 = 关闭（始终保持力矩）。
 * 设置会落 NVS。 */
void head_servo_set_idle_relax_ms(uint32_t ms);
uint32_t head_servo_idle_relax_ms();
bool head_servo_relaxed();
/* CONTROL_JSON: {"type":"servo_relax","ms":N} / {"type":"servo_relax_req"}
 * → 回 {"type":"servo_relax_ack",...}。返回 true 表示已处理。 */
bool head_handle_control_json(const uint8_t* payload, size_t length);

#endif
