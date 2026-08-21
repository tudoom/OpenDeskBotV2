#ifndef Common_h
#define Common_h

#include <Arduino.h>
#include <Wire.h>
#include <FFat.h>
#include "logger.h"
#include "led.h"
#include "display.h"

#define VERSION "0.0.15"
#define PRODUCT_NAME "Deskbot"

#ifndef RECORD_TIME
#define RECORD_TIME DESKBOT_UPLINK_MAX_SEC
#endif

/* asr_chat 主循环最长运行时间（毫秒）。0 = 上电后一直对话，不自动 leave。原固件为 600000（10 分钟）。 */
#ifndef CHAT_LOOP_MAX_MS
#define CHAT_LOOP_MAX_MS 0
#endif

void setup_FFat();
/** Stable hardware ID, formatted as deskbot_<factory eFuse MAC>. */
const char* get_device_id();
/** True only after a valid, non-zero unicast eFuse MAC was read. */
bool device_id_available();

#endif
