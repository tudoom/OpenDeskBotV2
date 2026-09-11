#ifndef DESKBOT_MIC_UPLINK_POLICY_H
#define DESKBOT_MIC_UPLINK_POLICY_H

#include <stddef.h>
#include <stdint.h>

/*
 * 麦克风上行策略（2026-09-09 发热排查）：
 *  - continuous（默认，2026-09-10 起）：会话在线就把每 20 ms 一帧全部 Opus 编码上行，
 *    断句全交给 PC，不会漏掉轻声/远场的开头。
 *  - vad：设备端能量 VAD 检到人声才开始编码上行，带 300 ms 预滚，人声停止后再送 2 s
 *    静音让 PC 端的 Silero 正常断句；其余时间不编码，芯片负载显著下降（省电档）。
 * 模式落 NVS，可用 CONTROL_JSON {"type":"mic_uplink_mode","mode":"vad|continuous"} 切换。
 *
 * 静音（2026-09-10，参数设置页开关）：打开后麦克风帧读出即丢——不增强、不编码、不上行，
 * PC 收不到任何音频；与上行模式互不影响，关闭静音后仍按原模式工作。落 NVS，默认关闭。
 * CONTROL_JSON {"type":"mic_mute","muted":true|false} / {"type":"mic_mute_req"}
 *   → {"type":"mic_mute_ack","muted":...,"mode":"..."}。
 */
enum class MicUplinkMode : uint8_t { kContinuous = 0, kVad = 1 };
constexpr MicUplinkMode MIC_UPLINK_DEFAULT_MODE = MicUplinkMode::kContinuous;

constexpr uint32_t MIC_VAD_HANGOVER_MS = 2000;   /* 人声结束后继续上行的时长 */
constexpr uint8_t MIC_VAD_PREROLL_FRAMES = 15;   /* 300 ms 预滚（20 ms/帧） */
constexpr uint32_t MIC_VAD_ABS_MIN = 150;        /* 帧平均绝对值的绝对门限 */
constexpr float MIC_VAD_NOISE_RATIO = 3.0f;      /* 相对噪声底的倍数门限 */

void mic_uplink_load_prefs();
MicUplinkMode mic_uplink_mode();
const char* mic_uplink_mode_name();
bool mic_uplink_set_mode(MicUplinkMode mode);
bool mic_uplink_muted();
bool mic_uplink_set_muted(bool muted);
/* 统计：总帧数 / 实际上行帧数 / 门打开次数（供 ack 与 hello 展示）。 */
void mic_uplink_note_frame(bool sent);
void mic_uplink_note_gate_open();
uint32_t mic_uplink_frames_total();
uint32_t mic_uplink_frames_sent();
uint32_t mic_uplink_gate_opens();
/* {"type":"mic_uplink_mode","mode":"vad"} / {"type":"mic_uplink_mode_req"}
 * → {"type":"mic_uplink_mode_ack",...}；{"type":"mic_mute",...} / {"type":"mic_mute_req"}
 * → {"type":"mic_mute_ack",...}；返回 true 表示已处理。 */
bool mic_uplink_handle_control_json(const uint8_t* payload, size_t length);

#endif  // DESKBOT_MIC_UPLINK_POLICY_H
