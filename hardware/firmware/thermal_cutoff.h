#ifndef DESKBOT_THERMAL_CUTOFF_H
#define DESKBOT_THERMAL_CUTOFF_H

#include <stddef.h>
#include <stdint.h>

/*
 * 温度断电保护（2026-09-09）：片内温度连续 30 s ≥ 阈值（默认 70℃）就把设备"断电"——
 * 设备没有能切自己供电的开关，最接近的是深度睡眠：CPU/WiFi/相机时钟/舵机脉冲/功放全停，
 * 只剩稳压器静态电流；kSleepSec 后自动重新开机。触发前把原因发给 PC 并写黑匣子。
 * 阈值落 NVS，0 = 关闭；CONTROL_JSON {"type":"thermal_cutoff","c":70} / {"type":"thermal_cutoff_req"}。
 */
constexpr uint32_t THERMAL_CUTOFF_DEFAULT_C = 70;
constexpr uint32_t THERMAL_CUTOFF_SUSTAIN_MS = 30000;
constexpr uint32_t THERMAL_CUTOFF_SLEEP_SEC = 600;
constexpr uint32_t THERMAL_CUTOFF_SAMPLE_MS = 5000;

void thermal_cutoff_load_prefs();
uint32_t thermal_cutoff_c();
bool thermal_cutoff_set(uint32_t celsius);
/* loop() 每拍调用；到条件即进入深度睡眠（不返回）。 */
void thermal_cutoff_tick();
uint32_t thermal_cutoff_trips();
bool thermal_cutoff_handle_control_json(const uint8_t* payload, size_t length);

#endif  // DESKBOT_THERMAL_CUTOFF_H
