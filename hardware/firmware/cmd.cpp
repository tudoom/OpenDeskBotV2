#include "cmd.h"
#include "task_trace.h"

namespace {

/* USB 帧分发路径（usb_transport_poll → on_usb_frame → handle_cmd）禁止在
 * 分发上下文执行命令：执行中重入 usb_transport_poll() 会在解析器停留在
 * payload CRC 状态时消费主机帧，向解析器结构越界写。
 * 因此 handle_cmd 只负责解析并把命令文本复制进本邮箱；loop() 在
 * usb_transport_poll() 返回后调用 cmd_mailbox_service() 顺序执行。邮箱满时
 * 丢弃新命令并告警；会话断开时由 cmd_mailbox_clear() 丢弃未执行的旧命令。
 * 生产者与消费者都在 loopTask（poll 与 loop 同任务），无跨任务并发。 */
constexpr size_t kCmdMailboxDepth = 8;
constexpr size_t kCmdMailboxEntryBytes = 96;

struct CmdMailboxEntry {
  char text[kCmdMailboxEntryBytes];
};

CmdMailboxEntry s_cmd_mailbox[kCmdMailboxDepth];
size_t s_cmd_mailbox_head = 0; /* 下一个待消费槽位 */
size_t s_cmd_mailbox_count = 0;

bool cmd_mailbox_push(const String& cmd) {
  if (cmd.length() >= kCmdMailboxEntryBytes) {
    log_warn("[CMD] mailbox entry too long (%u bytes); dropped: %.32s...",
             (unsigned)cmd.length(), cmd.c_str());
    return false;
  }
  if (s_cmd_mailbox_count >= kCmdMailboxDepth) {
    log_warn("[CMD] mailbox full; dropping factory command: %s", cmd.c_str());
    return false;
  }
  CmdMailboxEntry& slot =
      s_cmd_mailbox[(s_cmd_mailbox_head + s_cmd_mailbox_count) %
                    kCmdMailboxDepth];
  memcpy(slot.text, cmd.c_str(), cmd.length() + 1);
  s_cmd_mailbox_count++;
  return true;
}

}  // namespace

/* 手势/动作命令层（factory head_* / adjust_* / asr_chat / audio_test 与
 * actions[] 数组）已整体删除：头部运动只走 PB servo[] 通道，语音轮次由
 * loop() 自主发起。CONTROL_JSON 本地命令面只剩 factory 只读查询与维护命令。 */
void handle_cmd(String cmd) {
  if (cmd.isEmpty()) {
    return;
  }
  /* 纯文本模式：非 { 开头时，直接当 factory 命令入邮箱（便于串口调试）。 */
  if (cmd[0] != '{') {
    cmd_mailbox_push(cmd);
    return;
  }

  StaticJsonDocument<1024> doc;
  DeserializationError error = deserializeJson(doc, cmd);

  if (error) {
    log_error("JSON parse failed: %s", error.c_str());
    return;
  }

  if (doc["factory"].is<String>()) {
    String factoryCmd = doc["factory"].as<String>();
    cmd_mailbox_push(factoryCmd);
  }
}

void cmd_mailbox_service() {
  /* 先弹出再执行：执行期间嵌套的 usb_transport_poll 可能继续入邮箱，链路
   * 断开也可能触发 cmd_mailbox_clear()，因此每次循环都重新读取计数，不缓存
   * 快照。 */
  while (s_cmd_mailbox_count > 0) {
    CmdMailboxEntry entry = s_cmd_mailbox[s_cmd_mailbox_head];
    s_cmd_mailbox_head = (s_cmd_mailbox_head + 1) % kCmdMailboxDepth;
    s_cmd_mailbox_count--;
    executeFactoryCommand(String(entry.text));
  }
}

void cmd_mailbox_clear(const char* reason) {
  if (s_cmd_mailbox_count > 0) {
    log_warn("[CMD] dropping %u queued command(s) (%s)",
             (unsigned)s_cmd_mailbox_count,
             reason != nullptr ? reason : "session reset");
  }
  s_cmd_mailbox_head = 0;
  s_cmd_mailbox_count = 0;
}

/* 保留面：均为非阻塞、不触碰 motor 队列的只读查询与维护命令。
 * - head_pos：只读位置查询，service/tools/usb_device_smoke.py 依赖；
 * - task：任务栈/CPU 诊断转储；
 * - reboot/restart：产测与远程恢复用整机重启。 */
void executeFactoryCommand(String cmd) {
  if (cmd == "reboot" || cmd == "restart") {
    log_info("[Factory] Rebooting device...");
    ESP.restart();
  } else if (cmd == "head_pos") {
    head_log_position();
  } else if (cmd == "task") {
    log_task_dump();
  } else {
    log_warn("[Factory] Unknown factory command: %s", cmd.c_str());
    return;
  }
  log_info("%s", cmd.c_str());
}
