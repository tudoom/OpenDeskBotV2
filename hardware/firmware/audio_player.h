#ifndef AUDIO_PLAYER_H
#define AUDIO_PLAYER_H

#include <Arduino.h>
#include "common.h"
#include "deskbot_config.h"
#include "pb_completed_store.h"

#define PDM_MIC_CLK DESKBOT_PDM_MIC_CLK
#define PDM_MIC_DATA DESKBOT_PDM_MIC_DATA

#define SPEAKER_I2S_WS DESKBOT_SPEAKER_I2S_WS
#define SPEAKER_I2S_BCLK DESKBOT_SPEAKER_I2S_BCLK
#define SPEAKER_I2S_DOUT DESKBOT_SPEAKER_I2S_DOUT
#define SPEAKER_AMP_CTRL DESKBOT_SPEAKER_AMP_CTRL

// parameters
#define SAMPLE_RATE 16000
/* Four 20 ms descriptors keep speaker startup/cancel latency near 80 ms at
 * 16 kHz while retaining enough room for the playback worker to absorb jitter. */
#define DMA_BUF_COUNT 4
#define DMA_BUF_LEN 320
#define SOUND_THRESHOLD 180

/* The V2 NS4168 receives standard I2S with identical PCM in both physical
 * slots. This avoids depending on its PCB-selected WS phase and matches the
 * previously audible V2 hardware-probe format. Logical stereo input is
 * downmixed before being duplicated onto the wire. */

void setup_audio();
/* 从麦克风采集队列取 mono int16 PCM（见 audio_capture：独立任务通过
 * ESP-IDF new-channel PDM RX 驱动恒时读取 20ms 一帧）。 */
bool record(
    int16_t* data,
    size_t length = DMA_BUF_LEN,
    TickType_t wait_ticks = pdMS_TO_TICKS(100));
void enhanceVoice(int16_t *data, size_t length = DMA_BUF_LEN);
void enhanceVoice_reset(void);
size_t calculate_mean(const int16_t *data, size_t length);

/*
 * 播放（I2S1 TX）约定：
 * - 所有扬声器输出必须经下方 API：内部入队到 FreeRTOS 队列，由独立 audio_play 任务串行执行
 *   i2s_channel_reconfig_std_clock / i2s_channel_write；业务代码不要对
 *   I2S_NUM_1 再写样点。
 * - audio_stream_pcm16_*：小块连续异步入队；队列拥塞时失败或淘汰旧的非 PB PCM。
 *   所有播放状态只由 audio_play worker 串行修改，调用线程不等待 I2S。
 * - 流式队列深度见 AUDIO_PLAY_QUEUE_DEPTH；单块大小由调用方决定（如 20ms 一帧）。
 * - I2S DMA 缓冲见 DMA_BUF_COUNT / DMA_BUF_LEN。
 */
#ifndef AUDIO_PLAY_QUEUE_DEPTH
#define AUDIO_PLAY_QUEUE_DEPTH 96
#endif

/** PB 专用播放终态。epoch 是 AsrChatClient 分配的逻辑序列代次，
 *  与 audio_player 内部用于打断 I2S 的 cancel epoch 相互独立。 */
enum class AudioPbTerminalState : uint8_t {
  kCompleted = 0,
  kFailed = 1,
  kCancelled = 2,
};

struct AudioPbTerminalEvent {
  AudioPbTerminalState state = AudioPbTerminalState::kFailed;
  uint32_t epoch = 0;
  uint32_t idx = 0;
  char req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
};

/* 启动 I2S 播放任务（setup 阶段在 setup_audio + task_setup_mic_capture 之后调用一次）。 */
void task_setup_audio_play();

/* V2 bring-up self-test: enqueue a short, clearly audible reference tone
 * through the same worker/I2S path used by RTC playback. Other boards are a
 * no-op. Called before the USB service attaches, so it cannot interrupt a
 * conversation. */
void audio_play_v2_startup_self_test(uint32_t duration_ms = 2000u);

/* 流式 PCM16：调用方持续 push，小块会按顺序播放。
 * - 默认固定 16k/mono（与系统 SAMPLE_RATE 一致），因此不需要 begin/end。
 * - caps：0 → ::free；非 0 → heap_caps_free（如 MALLOC_CAP_SPIRAM）。
 * - push_* 接管缓冲区所有权；队列不可用时释放并返回 false。 */
bool audio_stream_pcm16_push_owned(int16_t* samples, size_t num_samples, uint32_t caps_for_heap_caps_free,
                                   float volume_ratio = 1.0f);
/*
 * Atomically cancel any prior untracked stream and queue a fresh Begin.
 * Subsequent push_owned() calls are ordered behind that Begin.
 */
bool audio_stream_pcm16_replace(float volume_ratio = 1.0f);
/* 强制排队一次流式 End：含 pb begin 路径（仅 s_stream_pcm_active）与极简 push 会话。 */
void audio_stream_pcm16_stop();

/* USB session 失效 / write 失败时调用：在播放任务内排空队列、停流式、清 I2S DMA，
 * 避免已入队的短 PCM 反复播。非播放任务上会阻塞到冲刷完成；若在 audio_play 任务内调用则仅入队、不等待。 */
void audio_play_emergency_flush();

/* 打断播放：drain 队列里所有未执行 chunk（释放堆 + 唤醒 sync caller 防永久阻塞），
 * 再排队一个 reset job 到队尾。audio_play 任务完成当前正在执行的 chunk 后立即收到
 * reset → 停流式 + disable/enable 清 DMA + 释放 pipeline 互斥，让下一段
 * begin 立即生效。
 * 与 emergency_flush 的区别：
 *  - emergency_flush 走 SendToFront 抢占式，意图"立刻在 task 内排空一切"，用于链路失效兜底；
 *  - audio_play_reset 走 SendToBack，让当前 chunk 放完再 reset，用于"打断旧 pb 序列、迎接
 *    新 pb_start"——保持当前样点完整播完避免突然咔哒声。 */
void audio_play_reset();

/** audio_play 任务输入队列中待处理消息数（含未播 chunk）。 */
unsigned audio_play_input_queue_depth();

/** 流式 PCM（pb/TTS）是否仍占用 I2S 播放管线（含尾音 flush 写入）。
 * 用于 ASR 半双工：pbSignalTtsRoundComplete 之后队列里可能仍有 PCM，tts_active_ 已为 false 时仍需参考本标志。 */
bool audio_play_stream_pcm_active();

/** 扬声器是否在播可听 PCM（见 DESKBOT_SPEAKER_AUDIBLE_MEAN_ABS）。 */
bool audio_play_speaker_busy();

/** 调试：play() 内 i2s_write 是否进行中（含静音 chunk / tail flush 以外的路径）。 */
bool audio_play_i2s_in_progress();

/** PB 严格流式接口：
 * - push/end 的输入队列满时立即返回 false，绝不丢弃已经排队的 PCM；
 * - end 只表示成功入队，真正写完 I2S DMA 尾部后由 audio_take_pb_terminal_event()
 *   返回 completed；执行失败/被 reset 则返回 failed/cancelled；
 * - push_owned 无论成功与否都接管 samples 所有权。 */
bool audio_pb_stream_begin(uint32_t epoch, const char* req, uint32_t idx,
                           uint32_t sample_rate, uint8_t channels,
                           float volume_ratio = 1.0f);
bool audio_pb_stream_push_owned(uint32_t epoch, const char* req, uint32_t idx,
                                int16_t* samples, size_t num_samples,
                                uint32_t caps_for_heap_caps_free,
                                uint32_t start_at_ms,
                                uint32_t sample_rate, uint8_t channels,
                                float volume_ratio = 1.0f);
bool audio_pb_stream_end(uint32_t epoch, const char* req, uint32_t idx,
                         uint8_t channels);
bool audio_take_pb_terminal_event(AudioPbTerminalEvent* out);
/** 单调累计无法投递的 PB audio terminal；last epoch 用于上层精确
 *  fail-close 单个 pending sequence。 */
uint32_t audio_pb_terminal_drop_count();
uint32_t audio_pb_terminal_last_dropped_epoch();

/* 音频任务在 i2s_write 节拍中调用；上层提供强定义（如 asr_chat_client）可按间隔泵 ws、
 * 驱动舵机微调。钩子跑在音频任务上下文，不要做长耗时工作以免卡顿。默认弱符号空实现。
 *
 * audio_yield_hook 运行于 audio_play_task 上下文，只允许执行短小、非阻塞操作。 */
extern "C" void audio_yield_hook();

/** 当前调用者是否运行于 audio_play_task 内。
 *  用于 loop() 等跨任务函数防止从播放任务上下文调用同步 audio API 而死锁。 */
bool audio_play_is_on_play_task();

#endif
