#ifndef Head_h
#define Head_h

#include <stddef.h>
#include "common.h"
#include "deskbot_config.h"
#include "pb_completed_store.h"

// Servo（见 deskbot_config.h）
#define X_PIN DESKBOT_ROM_X_PIN
#define Y_PIN DESKBOT_ROM_Y_PIN

/** 舵机物理极限（°）；所有运动均 constrain 于此。 */
#define X_MIN_LIMIT 10
#define X_MAX_LIMIT 170
#define Y_MIN_LIMIT 70
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

#endif
