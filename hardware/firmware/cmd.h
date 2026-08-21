#ifndef CMD_H
#define CMD_H

#include <ArduinoJson.h>
#include "common.h"
#include "head.h"

/* 解析 CONTROL_JSON 本地命令并入邮箱；可在 USB 帧分发上下文调用，绝不执行。
 * 手势/动作命令层（actions[] 与 factory 运动命令）已删除；仅剩 factory
 * 只读查询与维护命令（head_pos / task / reboot）。 */
void handle_cmd(String cmd);
void executeFactoryCommand(String cmd = "");
/* 消费 handle_cmd 入邮箱的命令。只允许 loop()（loopTask 顶层）在
 * usb_transport_poll() 返回后调用；命令在这里执行才不会重入解析器。 */
void cmd_mailbox_service();
/* 会话断开时丢弃尚未执行的命令，避免旧会话命令落到新会话执行。 */
void cmd_mailbox_clear(const char* reason = nullptr);

#endif
