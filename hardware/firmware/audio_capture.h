#ifndef AUDIO_CAPTURE_H
#define AUDIO_CAPTURE_H

#include <Arduino.h>
#include "freertos/FreeRTOS.h"

/* Step 2：麦克风独立采集任务。
 *
 * 背景：Arduino loop / runVoiceRound 在主任务里阻塞时（网络、舵机、gaze、部分 delay），
 * 若此时才调用 i2s_read，DMA 缓冲区会积压甚至溢出，收音时间轴与实际 wall clock 偏离。
 *
 * 模型：
 * - 独占 I2S_NUM_0 RX：仅此任务调用 i2s_channel_read，恒定读 320 样本（20ms @ 16k 单声道）一帧，
 *   推入 FreeRTOS 队列；队列满时丢最旧的帧再放新帧（始终保持“最新音频”）。
 * - 所有业务侧取样统一走 mic_consumer_read()（由 audio_player.cpp::record 转发），内部用 mutex
 *   + stash 拼装任意长度的 PCM，保证同时只有一个消费者在拆帧（wake / Doubao ASR / asr_chat
 *   若在极端情况下并行，后来者阻塞等待）。
 *
 * flush：上行开始前倒掉队列里 Idle 积压的旧帧（runVoiceRound 起点调用）。*/

static constexpr size_t kMicCaptureFrameSamples = 320;

struct MicCaptureFrame {
  int16_t pcm[kMicCaptureFrameSamples];
};

/** 配置 I2S0 PDM RX 并启动 mic_cap 任务。 */
void task_setup_mic_capture();
void mic_capture_flush_queue();

/*
 * Read continuous mono PCM16. Returns true only when every requested sample
 * was delivered before `first_frame_ticks`; a timeout zero-fills the remainder.
 * Callers must use a finite wait so a failed microphone/AFE cannot wedge the
 * USB-owning Arduino loop forever.
 */
bool mic_consumer_read(
    int16_t* dst,
    size_t length,
    TickType_t first_frame_ticks);

/** Monotonic progress counters used by the independent runtime supervisor. */
uint32_t mic_capture_last_attempt_ms();
uint32_t mic_capture_last_frame_ms();

/** True only after the PDM slot has produced stable, non-frozen PCM. */
bool mic_capture_signal_healthy();

struct MicPlaybackCaptureStats {
  uint64_t samples = 0;
  uint64_t absolute_sum = 0;
  uint64_t square_sum = 0;
  uint32_t peak = 0;
};

/** Raw-microphone acoustic energy captured while the speaker gate is active.
 * Values are DC-centred per 20 ms frame, so a silent PDM offset does not look
 * like loudspeaker output. These diagnostics never alter the capture path. */
void mic_capture_playback_stats_reset();
MicPlaybackCaptureStats mic_capture_playback_stats_take();

#endif
