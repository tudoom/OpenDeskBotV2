#ifndef Common_h
#define Common_h

#include <Arduino.h>
#include <Wire.h>
#include <FFat.h>
#include "logger.h"
#include "led.h"
#include "display.h"

/* 版本唯一来源：hardware/VERSION（构建时由 scripts/gen_version.py 生成
 * version_gen.h）。没有生成头（例如 IDE 直接打开）时给一个可辨认的占位。 */
#if __has_include("version_gen.h")
#include "version_gen.h"
#define VERSION DESKBOT_VERSION
#else
#define VERSION "0.0.0-dev"
#endif
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
