#ifndef DISPLAY_H
#define DISPLAY_H

#include "common.h"
#include "deskbot_config.h"
#include "display_panel.h"
#include "pb_completed_store.h"

#define SCREEN_WIDTH  DESKBOT_DISPLAY_WIDTH
#define SCREEN_HEIGHT DESKBOT_DISPLAY_HEIGHT

extern DeskbotDisplay g_display;

void setup_display();

void display_clear_timed();
void display_flush_timed();

void display_print(String text, int delay_time = 100);
void display_println(String text, int delay_time = 100);
/** 在服务端 280×240 坐标 (sx,sy) 处输出一行（随 ROTATION 映射到物理屏） */
void display_println_server(int16_t sx, int16_t sy, String text, int delay_time = 100);
void display_text_layout_reset(int16_t sx = 8, int16_t sy = 8);

#define DESKBOT_DISPLAY_BOOT_SX 8
#define DESKBOT_DISPLAY_BOOT_SY0 8
#define DESKBOT_DISPLAY_BOOT_TEXT_SIZE 1
#define DESKBOT_DISPLAY_BOOT_LINE_DY 14

/** 清空 boot 叠字 canvas 背景标记，下次 display_println_server 会整屏刷黑后重绘 */
void display_boot_screen_reset();

/** 写入第 1/2 行：应用名+版本、设备 ID。 */
void display_boot_header_lines(char* line1, size_t line1_len, char* line2, size_t line2_len);

/** 开机屏：第 1/2 行固定为版本、设备 ID；第 3/4 行可选状态（nullptr 省略）。 */
void display_boot_show(const char* status_line3 = nullptr, const char* status_line4 = nullptr);
enum DisplayScene : uint8_t {
  DISPLAY_SCENE_PB_VECTOR_JSON = 0,
};

enum class DisplayPbTerminalState : uint8_t {
  kCompleted = 0,
  kFailed = 1,
  kCancelled = 2,
};

struct DisplayPbTerminalEvent {
  DisplayPbTerminalState state = DisplayPbTerminalState::kFailed;
  uint32_t epoch = 0;
  uint32_t idx = 0;
  bool display_crc_valid = false;
  uint32_t display_crc32 = 0;
  char req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
};

void task_setup_display();

/** 待机屏（内建默认脸 + 「请连接PC服务」）：未连接 PC（等待 hello）与断开后
 *  由 asr_chat/boot 置 true，hello 成功后置 false。绘制本身只发生在 display
 *  worker 空闲 tick（单一所有者），不进入表情基线、不产生 pb_ack/CRC。 */
void display_standby_set(bool active);

/** PB 严格提交：成功后由渲染 worker 回报 terminal；队列满/参数非法时返回
 * false，且仍会释放传入 json 与 assets 的所有权，不会 drop-oldest。 */
bool display_pb_submit_vector_json_owned(uint32_t epoch, const char* req, uint32_t idx,
                                         uint32_t start_at_ms,
                                         char* json, size_t json_len,
                                         uint8_t* const* asset_bufs,
                                         const size_t* asset_lens,
                                         uint8_t asset_count,
                                         bool mouth_only = false);
bool display_take_pb_terminal_event(DisplayPbTerminalEvent* out);
/** Monotonic count of tracked terminal events that could not be queued. */
uint32_t display_pb_terminal_drop_count();

/**
 * Cancel queued/current PB display work while keeping the last panel pixels
 * visible until the replacement starts. ``preserve_baseline`` is true only
 * for mouth-only playback or transport/error cancellation. A complete face
 * replacement resets inherited layers before decoding its first frame.
 */
void display_render_replace(bool preserve_baseline = false);

/** Low-rate local RTC mouth overlay; keeps the current PB face and changes
 * only its mouth while decoded speech audio is playing. */
void display_voice_mouth_set_allowed(bool allowed);
void display_voice_mouth_set_level(uint8_t level);
void display_voice_mouth_stop();

unsigned display_render_input_queue_depth();

#endif
