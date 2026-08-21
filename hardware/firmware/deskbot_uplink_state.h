#ifndef DESKBOT_UPLINK_STATE_H
#define DESKBOT_UPLINK_STATE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/** 扬声器正在输出可听 PCM（stream 或 chunk 播放）。audio_play 路径写入。 */
void deskbot_uplink_set_speaker_active(bool active);

/** 播音结束时刻 + DESKBOT_TAIL_SUPPRESS_MS；active=false 时设置。 */
bool deskbot_uplink_speaker_audible(void);
bool deskbot_uplink_in_tail_suppress(void);
/** 尾音抑制剩余 ms；不在抑制期返回 0。 */
unsigned long deskbot_uplink_tail_ms_remaining(void);

/** 当前 USB session 的 ready 与 generation 状态。 */
void deskbot_uplink_set_transport_ready(bool ready);
bool deskbot_uplink_transport_ready(void);
bool deskbot_uplink_transport_allowed(void);
uint32_t deskbot_uplink_transport_generation(void);
void deskbot_uplink_bump_transport_generation(void);

/** PC/用户要求的 mic 总开关。mute 会真实阻止上行。 */
void deskbot_uplink_set_mic_enabled(bool enabled);
bool deskbot_uplink_mic_enabled(void);

/** RTC mode: stable keeps speaker echo-gating; interruptible keeps uplink open. */
void deskbot_uplink_set_rtc_interruptible(bool enabled);
bool deskbot_uplink_rtc_interruptible(void);

/** mic 入队 + record 总开关：用户允许 && USB OK && !speaking && !尾音抑制。 */
bool deskbot_uplink_capture_allowed(void);

#ifdef __cplusplus
}
#endif

#endif
