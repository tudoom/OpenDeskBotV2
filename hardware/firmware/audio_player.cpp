#include "audio_player.h"
#include "deskbot_uplink_state.h"

/* I2S1 TX is owned exclusively by the audio_play worker. Public APIs only enqueue work. */

#include <atomic>
#include <driver/i2s_std.h>
#include <math.h>
#include <string.h>

#include "audio_capture.h"
#include "audio_frontend_esp_sr.h"
#include "esp_heap_caps.h"
#include "pb_timeline.h"
#include "freertos/idf_additions.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

namespace {

i2s_chan_handle_t s_speaker_tx = nullptr;
bool s_speaker_tx_enabled = false;
bool s_speaker_amp_enabled = false;
uint32_t s_speaker_sample_rate = SAMPLE_RATE;
constexpr uint8_t kSpeakerPhysicalChannels = 2u;
const int16_t kDmaSilence[256] = {};
bool speaker_setup_driver();
void speaker_recover_default(const char* phase, esp_err_t cause);

void speaker_amp_disable() {
  if (SPEAKER_AMP_CTRL >= 0) {
    digitalWrite(SPEAKER_AMP_CTRL, LOW);
  }
  s_speaker_amp_enabled = false;
}

bool speaker_amp_prepare_playback() {
  if (SPEAKER_AMP_CTRL < 0) {
    return true;
  }
  if (s_speaker_amp_enabled && digitalRead(SPEAKER_AMP_CTRL) == HIGH) {
    return true;
  }
  digitalWrite(SPEAKER_AMP_CTRL, HIGH);
  /* The I2S channel is already enabled with silence at this point. Give the
   * external amplifier time to leave shutdown before the first audible PCM. */
  vTaskDelay(pdMS_TO_TICKS(DESKBOT_SPEAKER_AMP_WAKE_MS));
  const int level = digitalRead(SPEAKER_AMP_CTRL);
  s_speaker_amp_enabled = level == HIGH;
  if (!s_speaker_amp_enabled) {
    log_error("[AUDIO_AMP] GPIO%d failed to assert high", SPEAKER_AMP_CTRL);
    return false;
  }
  log_warn("[AUDIO_AMP] enabled GPIO%d level=%d settle_ms=%u",
           SPEAKER_AMP_CTRL, level,
           static_cast<unsigned>(DESKBOT_SPEAKER_AMP_WAKE_MS));
  return true;
}

i2s_std_slot_config_t speaker_slot_config() {
  i2s_std_slot_config_t slot =
      I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
          I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
  /* The V2 NS4168 channel selection is fixed by the PCB.  Drive identical
   * PCM in both physical slots so the amplifier receives audio regardless of
   * which WS phase it selects.  This also matches the V2 hardware probe and
   * the previously verified audible firmware wire format. */
  slot.slot_mask = I2S_STD_SLOT_BOTH;
  return slot;
}

esp_err_t speaker_preload_silence() {
  if (!s_speaker_tx || s_speaker_tx_enabled) {
    return ESP_ERR_INVALID_STATE;
  }

  /*
   * i2s_channel_disable() resets descriptor cursors but intentionally leaves
   * the DMA bytes untouched.  Fill every descriptor while the channel is in
   * READY state so a cancellation or format change cannot replay stale PCM.
   * Query the driver after every clock reconfiguration.  Any short preload
   * leaves the channel disabled: replaying stale speech is worse than failing
   * closed.
   */
  i2s_chan_info_t channel_info{};
  esp_err_t err = i2s_channel_get_info(s_speaker_tx, &channel_info);
  if (err != ESP_OK) {
    return err;
  }
  const size_t total_dma_bytes = channel_info.total_dma_buf_size;
  if (total_dma_bytes == 0) {
    return ESP_ERR_INVALID_SIZE;
  }

  size_t total_loaded = 0;
  while (total_loaded < total_dma_bytes) {
    const size_t remaining = total_dma_bytes - total_loaded;
    const size_t bytes_to_load =
        remaining < sizeof(kDmaSilence) ? remaining : sizeof(kDmaSilence);
    size_t loaded = 0;
    err = i2s_channel_preload_data(
        s_speaker_tx, kDmaSilence, bytes_to_load, &loaded);
    if (err != ESP_OK) {
      return err;
    }
    if (loaded != bytes_to_load) {
      return ESP_FAIL;
    }
    total_loaded += loaded;
  }
  return ESP_OK;
}

void speaker_release_driver() {
  speaker_amp_disable();
  if (!s_speaker_tx) {
    return;
  }
  if (s_speaker_tx_enabled) {
    (void)i2s_channel_disable(s_speaker_tx);
    s_speaker_tx_enabled = false;
  }
  (void)i2s_del_channel(s_speaker_tx);
  s_speaker_tx = nullptr;
}

esp_err_t speaker_reconfigure(uint32_t sample_rate, uint8_t channels) {
  if (sample_rate == 0 || (channels != 1 && channels != 2)) {
    return ESP_ERR_INVALID_ARG;
  }
  if (!s_speaker_tx || !s_speaker_tx_enabled) {
    speaker_release_driver();
    if (!speaker_setup_driver()) {
      return ESP_ERR_INVALID_STATE;
    }
  }

  esp_err_t err = i2s_channel_disable(s_speaker_tx);
  if (err != ESP_OK) {
    return err;
  }
  s_speaker_tx_enabled = false;

  const i2s_std_clk_config_t clock =
      I2S_STD_CLK_DEFAULT_CONFIG(sample_rate);
  err = i2s_channel_reconfig_std_clock(s_speaker_tx, &clock);
  if (err == ESP_OK) {
    const i2s_std_slot_config_t slot = speaker_slot_config();
    err = i2s_channel_reconfig_std_slot(s_speaker_tx, &slot);
  }

  const esp_err_t silence_err = speaker_preload_silence();
  if (silence_err != ESP_OK) {
    log_error("[AUDIO] speaker DMA silence preload failed err=%d",
              (int)silence_err);
    speaker_recover_default("reconfigure-preload", silence_err);
    return err == ESP_OK ? silence_err : err;
  }

  const esp_err_t enable_err = i2s_channel_enable(s_speaker_tx);
  if (enable_err == ESP_OK) {
    s_speaker_tx_enabled = true;
    s_speaker_sample_rate = sample_rate;
  } else {
    speaker_recover_default("reconfigure-enable", enable_err);
  }
  return err == ESP_OK ? enable_err : err;
}

bool speaker_setup_driver() {
  i2s_chan_config_t channel =
      I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_1, I2S_ROLE_MASTER);
  channel.dma_desc_num = DMA_BUF_COUNT;
  channel.dma_frame_num = DMA_BUF_LEN;
  /* Never repeat a stale DMA descriptor when the producer pauses. */
  channel.auto_clear_after_cb = true;

  esp_err_t err = i2s_new_channel(&channel, &s_speaker_tx, nullptr);
  if (err != ESP_OK) {
    log_error("[AUDIO] speaker new-channel failed err=%d", (int)err);
    s_speaker_tx = nullptr;
    return false;
  }

  i2s_std_config_t config = {
      .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
      .slot_cfg = speaker_slot_config(),
      .gpio_cfg =
          {
              .mclk = GPIO_NUM_NC,
              .bclk = SPEAKER_I2S_BCLK,
              .ws = SPEAKER_I2S_WS,
              .dout = SPEAKER_I2S_DOUT,
              .din = GPIO_NUM_NC,
              .invert_flags =
                  {
                      .mclk_inv = false,
                      .bclk_inv = false,
                      .ws_inv = false,
                  },
          },
  };
  err = i2s_channel_init_std_mode(s_speaker_tx, &config);
  if (err != ESP_OK) {
    log_error("[AUDIO] speaker standard-mode init failed err=%d", (int)err);
    speaker_release_driver();
    return false;
  }
  err = speaker_preload_silence();
  if (err != ESP_OK) {
    log_error("[AUDIO] speaker initial DMA silence preload failed err=%d",
              (int)err);
    speaker_release_driver();
    return false;
  }
  err = i2s_channel_enable(s_speaker_tx);
  if (err != ESP_OK) {
    log_error("[AUDIO] speaker channel enable failed err=%d", (int)err);
    speaker_release_driver();
    return false;
  }
  s_speaker_tx_enabled = true;
  s_speaker_sample_rate = SAMPLE_RATE;
  /* I2S clocks may remain ready while idle, but the physical amplifier must
   * stay shut down until an actual stream begins. */
  speaker_amp_disable();
  return true;
}

void speaker_recover_default(const char* phase, esp_err_t cause) {
  log_error("[AUDIO] speaker recovery phase=%s cause=%d",
            phase ? phase : "unknown", (int)cause);
  speaker_release_driver();
  if (speaker_setup_driver()) {
    log_warn("[AUDIO] speaker recovered with default 16kHz stereo-both wire format");
  } else {
    log_error("[AUDIO] speaker recovery failed; output remains disabled");
  }
}

}  // namespace

void setup_audio() {
  if (SPEAKER_AMP_CTRL >= 0) {
    pinMode(SPEAKER_AMP_CTRL, OUTPUT);
    speaker_amp_disable();
  }

  if (!speaker_setup_driver()) {
    log_error("[AUDIO] setup incomplete speaker unavailable");
    return;
  }
  log_warn("[AUDIO] NS4168 speaker ready format=philips/stereo-both "
           "BCLK=%d WS=%d DOUT=%d amp_ctrl=%d play_vol=%.2f",
           (int)SPEAKER_I2S_BCLK, (int)SPEAKER_I2S_WS,
           (int)SPEAKER_I2S_DOUT, (int)SPEAKER_AMP_CTRL,
           (double)DESKBOT_AUDIO_PLAY_VOLUME);
}

bool record(int16_t* data, size_t length, TickType_t wait_ticks) {
  return mic_consumer_read(data, length, wait_ticks);
}

namespace {

int16_t s_hpf_prev_in = 0;
float s_hpf_prev_out = 0.0f;

}  // namespace

void enhanceVoice_reset(void) {
  s_hpf_prev_in = 0;
  s_hpf_prev_out = 0.0f;
}

void enhanceVoice(int16_t* data, size_t length) {
  if (audio_frontend_ready()) {
    /* ESP-SR already supplies DC removal, NS and AGC. Applying the legacy
     * 5x gain again would clip the enhanced signal and hurt RTC ASR. */
    return;
  }
  /* 一阶高通（RC）：去掉 PDM 大 DC 偏置；fc≈80Hz @16kHz → alpha≈0.969 */
  constexpr float kAlpha = 0.969f;
  constexpr int kGain = 5;

  if (data == nullptr || length == 0) {
    return;
  }

  for (size_t i = 0; i < length; ++i) {
    const int16_t x = data[i];
    const float y =
        kAlpha * (s_hpf_prev_out + static_cast<float>(x) - static_cast<float>(s_hpf_prev_in));
    s_hpf_prev_in = x;
    s_hpf_prev_out = y;
    data[i] = static_cast<int16_t>(constrain(static_cast<int>(lroundf(y * static_cast<float>(kGain))),
                                             -32768, 32767));
  }
}

/* audio_yield_hook 默认空实现，asr_chat_client.cpp 提供强定义（taskYIELD）。
   weak 符号：Step 3 后实际在 audio_play_task 上下文里周期性调用，
   hook 仍可 pump ws + 推舵机——主 loop 可同时跑不再被整段 WAV 卡住。 */
extern "C" __attribute__((weak)) void audio_yield_hook() {}

size_t calculate_mean(const int16_t* data, size_t length);

static bool s_i2s_play_in_progress = false;
static bool s_audible_play_in_progress = false;
static std::atomic<uint32_t> s_audio_cancel_epoch{0};
static void stop_play();

constexpr uint32_t kI2sWriteTimeoutMs = 250u;

struct AudioOutputStats {
  uint64_t samples = 0;
  uint64_t absolute_sum = 0;
  uint64_t square_sum = 0;
  uint64_t non_silent_samples = 0;
  uint64_t bytes = 0;
  uint32_t writes = 0;
  int32_t peak = 0;
};

static void audio_output_stats_add(AudioOutputStats* stats,
                                   const int16_t* pcm, size_t samples) {
  if (!stats || !pcm || samples == 0) {
    return;
  }
  for (size_t i = 0; i < samples; ++i) {
    const int32_t value = pcm[i];
    const int32_t magnitude =
        value == INT16_MIN ? 32768 : abs(value);
    stats->absolute_sum += static_cast<uint32_t>(magnitude);
    stats->square_sum +=
        static_cast<uint64_t>(static_cast<int64_t>(value) * value);
    if (magnitude >= static_cast<int32_t>(DESKBOT_SPEAKER_AUDIBLE_MEAN_ABS)) {
      stats->non_silent_samples++;
    }
    if (magnitude > stats->peak) {
      stats->peak = magnitude;
    }
  }
  stats->samples += samples;
  stats->bytes += samples * sizeof(int16_t);
  stats->writes++;
}

static void log_audio_output_stats(const char* state,
                                   const AudioOutputStats& stats) {
  const uint32_t mean_abs =
      stats.samples == 0 ? 0 : static_cast<uint32_t>(
          stats.absolute_sum / stats.samples);
  const uint32_t rms =
      stats.samples == 0 ? 0 : static_cast<uint32_t>(lround(
          sqrt(static_cast<double>(stats.square_sum) /
               static_cast<double>(stats.samples))));
  const uint32_t non_silent_x1000 =
      stats.samples == 0 ? 0 : static_cast<uint32_t>(
          stats.non_silent_samples * 1000u / stats.samples);
  log_warn(
      "[AUDIO_OUT] stream stats state=%s samples=%llu writes=%u bytes=%llu "
      "mean_abs=%u rms=%u peak=%d non_silent_x1000=%u",
      state ? state : "unknown",
      static_cast<unsigned long long>(stats.samples),
      static_cast<unsigned>(stats.writes),
      static_cast<unsigned long long>(stats.bytes),
      static_cast<unsigned>(mean_abs), static_cast<unsigned>(rms),
      static_cast<int>(stats.peak), static_cast<unsigned>(non_silent_x1000));
}

static void log_audio_loopback_stats(const char* state) {
  const MicPlaybackCaptureStats loopback =
      mic_capture_playback_stats_take();
  const uint32_t mean_abs = loopback.samples == 0
      ? 0
      : static_cast<uint32_t>(loopback.absolute_sum / loopback.samples);
  const uint32_t rms = loopback.samples == 0
      ? 0
      : static_cast<uint32_t>(lround(sqrt(
            static_cast<double>(loopback.square_sum) /
            static_cast<double>(loopback.samples))));
  log_warn(
      "[AUDIO_LOOPBACK] state=%s mic_samples=%llu ac_mean_abs=%u "
      "ac_rms=%u ac_peak=%u",
      state ? state : "unknown",
      static_cast<unsigned long long>(loopback.samples),
      static_cast<unsigned>(mean_abs), static_cast<unsigned>(rms),
      static_cast<unsigned>(loopback.peak));
}

static bool write_i2s_checked(const void* data, size_t bytes,
                              const char* phase) {
  if (!data || bytes == 0 || !s_speaker_tx || !s_speaker_tx_enabled) {
    log_error("[AUDIO] I2S write invalid phase=%s bytes=%u ready=%d",
              phase ? phase : "unknown", static_cast<unsigned>(bytes),
              static_cast<int>(s_speaker_tx && s_speaker_tx_enabled));
    return false;
  }
  if (SPEAKER_AMP_CTRL >= 0 &&
      (!s_speaker_amp_enabled || digitalRead(SPEAKER_AMP_CTRL) != HIGH)) {
    log_error("[AUDIO] refusing I2S write while amplifier GPIO%d is low phase=%s",
              SPEAKER_AMP_CTRL, phase ? phase : "unknown");
    return false;
  }
  /*
   * Publish the reference before the blocking driver write.  Publishing only
   * after the driver write returns made the reference late by one DMA wait,
   * so ESP-SR frequently saw the loudspeaker echo before its source signal.
   * A failed/partial write invalidates the whole queued reference timeline
   * below; feeding PCM that never reached the speaker is worse than silence.
   */
  audio_frontend_submit_playback_reference(
      static_cast<const int16_t*>(data),
      bytes / sizeof(int16_t),
      s_speaker_sample_rate,
      kSpeakerPhysicalChannels);
  size_t bytes_written = 0;
  const esp_err_t err = i2s_channel_write(
      s_speaker_tx, data, bytes, &bytes_written, kI2sWriteTimeoutMs);
  if (err == ESP_OK && bytes_written == bytes) {
    return true;
  }
  audio_frontend_abort_playback_reference();
  log_error("[AUDIO] I2S write failed phase=%s err=%d wrote=%u/%u",
            phase, (int)err, (unsigned)bytes_written, (unsigned)bytes);
  return false;
}

static bool pcm_chunk_audible(const int16_t* data, size_t length, float volume_ratio) {
  if (data == nullptr || length == 0) {
    return false;
  }
  const size_t mean = calculate_mean(data, length);
  const size_t effective =
      static_cast<size_t>(static_cast<float>(mean) * ((volume_ratio < 0.0f) ? 0.0f : volume_ratio));
  return effective >= (size_t)DESKBOT_SPEAKER_AUDIBLE_MEAN_ABS;
}

static bool play(const int16_t* data, size_t length, uint8_t logical_channels,
                 float volume_ratio, uint32_t cancel_epoch,
                 bool yield_after_chunk = false,
                 AudioOutputStats* output_stats = nullptr) {
  if (!data || length == 0 ||
      (logical_channels != 1 && logical_channels != 2) ||
      length % logical_channels != 0) {
    return false;
  }
  const float output_volume = volume_ratio >= 0.0f ? volume_ratio : 0.0f;
  const size_t input_mean_abs = calculate_mean(data, length);
  int32_t input_peak = 0;
  for (size_t i = 0; i < length; ++i) {
    const int32_t sample_abs = data[i] == INT16_MIN
                                   ? 32768
                                   : abs(static_cast<int32_t>(data[i]));
    if (sample_abs > input_peak) input_peak = sample_abs;
  }
  const bool audible = pcm_chunk_audible(data, length, output_volume);
  s_i2s_play_in_progress = true;
  if (audible) {
    s_audible_play_in_progress = true;
    /* 首段可听 PCM 再挡麦；stream begin 时不置位，避免 pb JSON 先到、TTS 未播就停录音。 */
    deskbot_uplink_set_speaker_active(true);
  }
  log_info("[AUDIO_OUT] begin samples=%u ch=%u mean_abs=%u peak=%d vol=%.2f",
           static_cast<unsigned>(length), static_cast<unsigned>(logical_channels),
           static_cast<unsigned>(input_mean_abs), static_cast<int>(input_peak),
           static_cast<double>(output_volume));
  /* Use the same 20 ms cadence as microphone capture/ESP-SR. This prevents
   * the AEC reference producer from feeding 16 ms bursts into a 20 ms
   * consumer, which previously caused periodic reference underflow. */
  constexpr size_t kOutputBlockFrames = DMA_BUF_LEN;
  int16_t scratch[kOutputBlockFrames * kSpeakerPhysicalChannels];
  const auto scale_sample = [output_volume](int16_t sample) -> int16_t {
    const float scaled = static_cast<float>(sample) * output_volume;
    if (scaled >= 32767.0f) {
      return 32767;
    }
    if (scaled <= -32768.0f) {
      return -32768;
    }
    return static_cast<int16_t>(lroundf(scaled));
  };
  size_t off = 0;
  while (off < length) {
    if (cancel_epoch != s_audio_cancel_epoch.load(std::memory_order_acquire)) {
      stop_play();
      s_i2s_play_in_progress = false;
      if (audible) {
        s_audible_play_in_progress = false;
        deskbot_uplink_set_speaker_active(false);
      }
      return false;
    }
    size_t n = length - off;
    const size_t max_input_samples =
        kOutputBlockFrames * logical_channels;
    if (n > max_input_samples) {
      n = max_input_samples;
    }
    const size_t output_frames = n / logical_channels;
    for (size_t frame = 0; frame < output_frames; ++frame) {
      int32_t mono = data[off + frame * logical_channels];
      if (logical_channels == 2) {
        mono = (mono + data[off + frame * logical_channels + 1u]) / 2;
      }
      const int16_t sample = scale_sample(static_cast<int16_t>(mono));
      scratch[frame * kSpeakerPhysicalChannels] = sample;
      scratch[frame * kSpeakerPhysicalChannels + 1u] = sample;
    }
    const size_t output_samples = output_frames * kSpeakerPhysicalChannels;
    if (!write_i2s_checked(
            scratch, output_samples * sizeof(int16_t), "pcm")) {
      stop_play();
      s_i2s_play_in_progress = false;
      if (audible) {
        s_audible_play_in_progress = false;
        deskbot_uplink_set_speaker_active(false);
      }
      return false;
    }
    audio_output_stats_add(output_stats, scratch, output_samples);
    off += n;
  }
  s_i2s_play_in_progress = false;
  if (audible) {
    s_audible_play_in_progress = false;
  }
  if (yield_after_chunk) {
    audio_yield_hook();
  }
  log_info("[AUDIO_OUT] complete samples=%u i2s_bytes=%u",
           static_cast<unsigned>(length),
           static_cast<unsigned>((length / logical_channels) *
                                 kSpeakerPhysicalChannels * sizeof(int16_t)));
  return true;
}

static void stop_play() {
  if (!s_speaker_tx || !s_speaker_tx_enabled) {
    return;
  }
  esp_err_t err = i2s_channel_disable(s_speaker_tx);
  if (err == ESP_OK) {
    s_speaker_tx_enabled = false;
    err = speaker_preload_silence();
    if (err == ESP_OK) {
      err = i2s_channel_enable(s_speaker_tx);
    }
    if (err == ESP_OK) {
      s_speaker_tx_enabled = true;
    }
  }
  if (err != ESP_OK) {
    log_error("[AUDIO] speaker DMA silence reset failed err=%d", (int)err);
    speaker_recover_default("dma-reset", err);
  }
}

size_t calculate_mean(const int16_t* data, size_t length) {
  /* 历史实现写的是 i += 1000，对 20ms / 320 采样帧而言 count 退化为 1，等价于只看 data[0]，
   * 噪声大、漏判多。改为整帧绝对值平均（mean-abs），与 doc/zh/硬件与传感器.md 中"50 → 1000+"
   * 的描述一致；CPU 开销在 16kHz 下可忽略。 */
  if (data == nullptr || length == 0) {
    return 0;
  }
  uint64_t sum = 0;
  for (size_t i = 0; i < length; ++i) {
    sum += static_cast<uint32_t>(abs(data[i]));
  }
  return static_cast<size_t>(sum / length);
}

namespace {

/* Queue messages own any PCM buffer and are processed serially by audio_play. */
enum class AudioHeapFree : uint8_t {
  kMalloc = 0,
  kHeapCaps = 1,
};

struct AudioPlayJob {
  enum class Kind : uint8_t {
    kStreamPcm16Begin = 0,
    kStreamPcm16Chunk = 1,
    kStreamPcm16End = 2,
    kEmergencyFlush = 3,
    kReset = 4,
  };

  Kind kind = Kind::kStreamPcm16Begin;
  AudioHeapFree free_mode = AudioHeapFree::kMalloc;

  int16_t* pcm_heap = nullptr;
  size_t pcm_samples = 0;
  uint32_t pcm_rate = SAMPLE_RATE;
  uint8_t pcm_channels = 1;

  float volume = 1.0f;
  uint32_t cancel_epoch = 0;
  bool pb_tracked = false;
  bool preserve_client_session = false;
  uint32_t pb_epoch = 0;
  uint32_t pb_idx = 0;
  uint32_t pb_start_at_ms = 0;
  char pb_req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
};

QueueHandle_t     s_audio_play_q           = nullptr;
QueueHandle_t     s_audio_pb_terminal_q    = nullptr;
TaskHandle_t      s_audio_play_task        = nullptr;
std::atomic<uint32_t> s_audio_pb_terminal_drop_count{0};
std::atomic<uint32_t> s_audio_pb_terminal_last_dropped_epoch{0};

/* These flags are observed by both the USB/main task and audio_play. */
static std::atomic<bool> s_stream_pcm_active{false};
static std::atomic<float> s_stream_vol{1.0f};
static std::atomic<bool> s_stream_client_session{false};
static std::atomic<uint32_t> s_stream_pcm_rate{SAMPLE_RATE};
static std::atomic<uint8_t> s_stream_pcm_channels{1};

/* Drop queued work and release all owned buffers. */
static bool ensure_audio_play_task();

static void audio_fill_pb_meta(AudioPlayJob& job, uint32_t epoch, const char* req,
                               uint32_t idx) {
  job.pb_tracked = epoch != 0 && req != nullptr && req[0] != '\0';
  job.pb_epoch = epoch;
  job.pb_idx = idx;
  if (job.pb_tracked) {
    strncpy(job.pb_req, req, sizeof(job.pb_req) - 1);
    job.pb_req[sizeof(job.pb_req) - 1] = '\0';
  }
}

static void audio_note_pb_terminal_drop(uint32_t epoch) {
  s_audio_pb_terminal_last_dropped_epoch.store(
      epoch, std::memory_order_release);
  s_audio_pb_terminal_drop_count.fetch_add(1, std::memory_order_relaxed);
}

static void audio_emit_pb_terminal(const AudioPlayJob& job,
                                   AudioPbTerminalState state) {
  if (!job.pb_tracked) {
    return;
  }
  if (!s_audio_pb_terminal_q) {
    audio_note_pb_terminal_drop(job.pb_epoch);
    log_error("[AUDIO] PB terminal queue unavailable epoch=%u req=%s idx=%u state=%u",
              (unsigned)job.pb_epoch, job.pb_req, (unsigned)job.pb_idx,
              (unsigned)state);
    return;
  }
  AudioPbTerminalEvent out{};
  out.state = state;
  out.epoch = job.pb_epoch;
  out.idx = job.pb_idx;
  strncpy(out.req, job.pb_req, sizeof(out.req) - 1);
  if (xQueueSend(s_audio_pb_terminal_q, &out, 0) != pdTRUE) {
    audio_note_pb_terminal_drop(job.pb_epoch);
    log_error("[AUDIO] PB terminal queue full epoch=%u req=%s idx=%u state=%u",
              (unsigned)out.epoch, out.req, (unsigned)out.idx,
              (unsigned)out.state);
  }
}

static bool audio_wait_for_pb_start(const AudioPlayJob& job) {
  if (!job.pb_tracked || job.pb_start_at_ms == 0) {
    return true;
  }
  while (true) {
    if (job.cancel_epoch !=
        s_audio_cancel_epoch.load(std::memory_order_acquire)) {
      return false;
    }
    const uint32_t remaining =
        deskbot_pb_time_remaining(millis(), job.pb_start_at_ms);
    if (remaining == 0) {
      return true;
    }
    const uint32_t slice_ms = remaining > 5u ? 5u : remaining;
    vTaskDelay(pdMS_TO_TICKS(slice_ms));
  }
}

static void free_audio_play_job_pcm(AudioPlayJob& d) {
  if (d.kind != AudioPlayJob::Kind::kStreamPcm16Chunk || d.pcm_heap == nullptr) {
    return;
  }
  if (d.free_mode == AudioHeapFree::kMalloc) {
    ::free(d.pcm_heap);
  } else if (d.free_mode == AudioHeapFree::kHeapCaps) {
    heap_caps_free(d.pcm_heap);
  }
  d.pcm_heap = nullptr;
}

/* 播放队列满时丢弃最旧的一块 PCM chunk，避免在 WS 收包回调里 portMAX_DELAY 卡死、收不到后续 pb_chunk。 */
static bool enqueue_audio_play_job(AudioPlayJob& j) {
  if (!ensure_audio_play_task()) {
    return false;
  }
  j.cancel_epoch = s_audio_cancel_epoch.load(std::memory_order_acquire);
  static bool s_logged_drop = false;
  for (unsigned attempt = 0; attempt < 32; ++attempt) {
    if (xQueueSend(s_audio_play_q, &j, 0) == pdTRUE) {
      s_logged_drop = false;
      return true;
    }
    AudioPlayJob oldest{};
    if (xQueueReceive(s_audio_play_q, &oldest, 0) != pdTRUE) {
      audio_yield_hook();
      vTaskDelay(1);
      continue;
    }
    if (oldest.kind == AudioPlayJob::Kind::kStreamPcm16Chunk &&
        !oldest.pb_tracked) {
      free_audio_play_job_pcm(oldest);
      if (!s_logged_drop) {
        s_logged_drop = true;
        log_warn("[AUDIO] play queue full: dropping oldest pcm chunk");
      }
      continue;
    }
    if (oldest.pb_tracked) {
      log_warn("[AUDIO] generic enqueue cannot evict tracked PB epoch=%u req=%s idx=%u",
               (unsigned)oldest.pb_epoch, oldest.pb_req,
               (unsigned)oldest.pb_idx);
    }
    (void)xQueueSendToFront(s_audio_play_q, &oldest, 0);
    audio_yield_hook();
    vTaskDelay(1);
  }
  return false;
}

/** PB 路径不允许通过淘汰旧 PCM 来“制造”入队成功。 */
static bool enqueue_audio_play_job_strict(AudioPlayJob& j) {
  if (!ensure_audio_play_task()) {
    return false;
  }
  j.cancel_epoch = s_audio_cancel_epoch.load(std::memory_order_acquire);
  if (xQueueSend(s_audio_play_q, &j, 0) == pdTRUE) {
    return true;
  }
  log_warn("[AUDIO] PB play queue full epoch=%u req=%s idx=%u kind=%u",
           (unsigned)j.pb_epoch, j.pb_req, (unsigned)j.pb_idx,
           (unsigned)j.kind);
  return false;
}

static void drain_audio_play_queue() {
  AudioPlayJob dropped{};
  while (xQueueReceive(s_audio_play_q, &dropped, 0) == pdTRUE) {
    switch (dropped.kind) {
      case AudioPlayJob::Kind::kStreamPcm16Begin:
        audio_emit_pb_terminal(dropped, AudioPbTerminalState::kCancelled);
        break;
      case AudioPlayJob::Kind::kStreamPcm16Chunk:
        free_audio_play_job_pcm(dropped);
        break;
      case AudioPlayJob::Kind::kStreamPcm16End:
        audio_emit_pb_terminal(dropped, AudioPbTerminalState::kCancelled);
        break;
      case AudioPlayJob::Kind::kEmergencyFlush:
      case AudioPlayJob::Kind::kReset:
        break;
    }
  }
}
static bool stream_write_tail_and_restore_i2s(uint32_t cancel_epoch) {
  const size_t tail_samples =
      static_cast<size_t>(DMA_BUF_COUNT) * static_cast<size_t>(DMA_BUF_LEN) *
      kSpeakerPhysicalChannels;
  static int16_t s_zero_block[256];
  memset(s_zero_block, 0, sizeof(s_zero_block));
  size_t written = 0;
  bool completed = true;
  while (written < tail_samples) {
    if (cancel_epoch != s_audio_cancel_epoch.load(std::memory_order_acquire)) {
      completed = false;
      break;
    }
    size_t n = tail_samples - written;
    if (n > 256u) {
      n = 256u;
    }
    if (!write_i2s_checked(s_zero_block, n * sizeof(int16_t),
                           "stream-tail")) {
      completed = false;
      break;
    }
    written += n;
  }
  const esp_err_t restore_clock_err =
      speaker_reconfigure(SAMPLE_RATE, 1);
  if (restore_clock_err != ESP_OK) {
    log_error("[AUDIO] stream clock restore failed err=%d",
              (int)restore_clock_err);
    completed = false;
  }
  stop_play();
  return completed;
}

static void abort_active_stream_after_i2s_failure() {
  if (!s_stream_pcm_active) {
    return;
  }
  const esp_err_t restore_clock_err =
      speaker_reconfigure(SAMPLE_RATE, 1);
  if (restore_clock_err != ESP_OK) {
    log_error("[AUDIO] failed-stream clock restore failed err=%d",
              (int)restore_clock_err);
  }
  stop_play();
  s_stream_pcm_active = false;
  s_stream_client_session = false;
  s_stream_pcm_rate = SAMPLE_RATE;
  s_stream_pcm_channels = 1;
  deskbot_uplink_set_speaker_active(false);
  speaker_amp_disable();
}

void audio_play_task_main(void* /*arg*/) {
  AudioPlayJob job{};
  bool stream_first_pcm_logged = false;
  AudioOutputStats stream_stats{};
  for (;;) {
    if (xQueueReceive(s_audio_play_q, &job, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    const bool stale =
        job.cancel_epoch != s_audio_cancel_epoch.load(std::memory_order_acquire);
    if (stale && job.kind != AudioPlayJob::Kind::kEmergencyFlush &&
        job.kind != AudioPlayJob::Kind::kReset) {
      if (job.pb_tracked &&
          (job.kind == AudioPlayJob::Kind::kStreamPcm16Begin ||
           job.kind == AudioPlayJob::Kind::kStreamPcm16End)) {
        audio_emit_pb_terminal(job, AudioPbTerminalState::kCancelled);
      }
      free_audio_play_job_pcm(job);
      continue;
    }

    switch (job.kind) {
      case AudioPlayJob::Kind::kStreamPcm16Begin: {
        bool begin_ok = false;
        if (s_stream_pcm_active) {
          log_warn("[AUDIO] stream begin while active");
        } else if (job.pcm_channels != 1 && job.pcm_channels != 2) {
          log_error("[AUDIO] invalid stream channels=%u",
                    (unsigned)job.pcm_channels);
        } else if (job.pcm_rate == 0) {
          log_error("[AUDIO] invalid stream rate=0");
        } else if (!speaker_amp_prepare_playback()) {
            log_error("[AUDIO] stream begin amplifier enable failed");
            stop_play();
          } else {
            /* Keep NS4168 awake while BCLK/LRCLK are stopped and restarted.
             * The production board's GPIO45 gate is not on the released
             * schematic; the previously audible firmware always held CTRL
             * high across this clock transition.  Enabling only after the
             * transition can leave the amplifier's serial receiver unlocked. */
            const esp_err_t clock_err =
                speaker_reconfigure(job.pcm_rate, job.pcm_channels);
            if (clock_err != ESP_OK) {
              log_error("[AUDIO] stream begin clock setup failed err=%d",
                        (int)clock_err);
              stop_play();
              speaker_amp_disable();
            } else {
              s_stream_pcm_active = true;
              s_stream_vol = job.volume;
              s_stream_pcm_rate = job.pcm_rate;
              s_stream_pcm_channels = job.pcm_channels;
              begin_ok = true;
              stream_first_pcm_logged = false;
              stream_stats = {};
              mic_capture_playback_stats_reset();
              log_warn(
                  "[AUDIO_OUT] stream ready rate=%u ch=%u vol=%.2f "
                  "wire=philips/stereo-both",
                  (unsigned)job.pcm_rate, (unsigned)job.pcm_channels,
                  (double)job.volume);
            }
          }
        if (!begin_ok) {
          s_stream_client_session = false;
          speaker_amp_disable();
          if (job.pb_tracked) {
            audio_emit_pb_terminal(job, AudioPbTerminalState::kFailed);
          }
        }
        break;
      }
      case AudioPlayJob::Kind::kStreamPcm16Chunk: {
        if (!s_stream_pcm_active) {
          log_warn("[AUDIO] stream chunk dropped (no begin)");
          free_audio_play_job_pcm(job);
          audio_emit_pb_terminal(job, AudioPbTerminalState::kFailed);
        } else if (job.pcm_rate != s_stream_pcm_rate ||
                   job.pcm_channels != s_stream_pcm_channels ||
                   job.pcm_samples % s_stream_pcm_channels != 0) {
          log_error(
              "[AUDIO] stream chunk format mismatch got=%uHz/%uch/%u samples "
              "expected=%uHz/%uch",
              (unsigned)job.pcm_rate, (unsigned)job.pcm_channels,
              (unsigned)job.pcm_samples, (unsigned)s_stream_pcm_rate,
              (unsigned)s_stream_pcm_channels);
          free_audio_play_job_pcm(job);
          abort_active_stream_after_i2s_failure();
          audio_emit_pb_terminal(job, AudioPbTerminalState::kFailed);
        } else if (job.pcm_heap != nullptr && job.pcm_samples > 0) {
          bool chunk_ok = audio_wait_for_pb_start(job);
          size_t skip_samples = 0;
          if (chunk_ok && job.pb_tracked && job.pb_start_at_ms != 0 &&
              job.pcm_rate > 0 && job.pcm_channels > 0) {
            constexpr uint32_t kCatchupFloorMs = 8u;
            const uint32_t late_ms =
                deskbot_pb_time_late(millis(), job.pb_start_at_ms);
            if (late_ms > kCatchupFloorMs) {
              const uint64_t skip64 =
                  static_cast<uint64_t>(job.pcm_rate) *
                  static_cast<uint64_t>(job.pcm_channels) * late_ms / 1000u;
              skip_samples =
                  skip64 > job.pcm_samples
                      ? job.pcm_samples
                      : static_cast<size_t>(skip64);
              skip_samples -= skip_samples % job.pcm_channels;
              log_warn("[AUDIO] PB timeline catch-up epoch=%u req=%s idx=%u "
                       "late=%ums skip=%u/%u",
                       (unsigned)job.pb_epoch, job.pb_req,
                       (unsigned)job.pb_idx, (unsigned)late_ms,
                       (unsigned)skip_samples,
                       (unsigned)job.pcm_samples);
            }
          }
          if (chunk_ok && skip_samples < job.pcm_samples) {
            chunk_ok = play(job.pcm_heap + skip_samples,
                            job.pcm_samples - skip_samples, job.pcm_channels,
                            s_stream_vol,
                            job.cancel_epoch,
                            /*yield_after_chunk=*/true,
                            &stream_stats);
            if (chunk_ok && !stream_first_pcm_logged) {
              stream_first_pcm_logged = true;
              log_warn(
                  "[AUDIO_OUT] first PCM reached I2S samples=%u mean_abs=%u",
                  (unsigned)(job.pcm_samples - skip_samples),
                  (unsigned)calculate_mean(job.pcm_heap + skip_samples,
                                           job.pcm_samples - skip_samples));
            }
          }
          free_audio_play_job_pcm(job);
          if (!chunk_ok) {
            const bool cancelled =
                job.cancel_epoch !=
                s_audio_cancel_epoch.load(std::memory_order_acquire);
            if (!cancelled) {
              abort_active_stream_after_i2s_failure();
            }
            if (stream_stats.samples > 0) {
              log_audio_output_stats(
                  cancelled ? "cancelled" : "failed", stream_stats);
              stream_stats = {};
            }
            log_audio_loopback_stats(cancelled ? "cancelled" : "failed");
            if (job.pb_tracked) {
              audio_emit_pb_terminal(
                  job, cancelled ? AudioPbTerminalState::kCancelled
                                 : AudioPbTerminalState::kFailed);
            }
          }
        } else {
          free_audio_play_job_pcm(job);
          audio_emit_pb_terminal(job, AudioPbTerminalState::kFailed);
        }
        break;
      }
      case AudioPlayJob::Kind::kStreamPcm16End: {
        AudioPbTerminalState pb_state = AudioPbTerminalState::kFailed;
        const bool format_ok =
            s_stream_pcm_active &&
            job.pcm_channels == s_stream_pcm_channels;
        if (s_stream_pcm_active && !format_ok) {
          log_error(
              "[AUDIO] stream end format mismatch got=%uch expected=%uch",
              (unsigned)job.pcm_channels,
              (unsigned)s_stream_pcm_channels);
        }
        if (s_stream_pcm_active) {
          const bool tail_ok =
              stream_write_tail_and_restore_i2s(job.cancel_epoch);
          s_stream_pcm_active = false;
          s_stream_pcm_rate = SAMPLE_RATE;
          s_stream_pcm_channels = 1;
          deskbot_uplink_set_speaker_active(false);
          log_warn("[AUDIO_OUT] stream ended tail_ok=%d format_ok=%d",
                   (int)tail_ok, (int)format_ok);
          const bool cancelled =
              job.cancel_epoch !=
              s_audio_cancel_epoch.load(std::memory_order_acquire);
          pb_state =
              tail_ok && format_ok ? AudioPbTerminalState::kCompleted
                      : cancelled ? AudioPbTerminalState::kCancelled
                                  : AudioPbTerminalState::kFailed;
          log_audio_output_stats(
              pb_state == AudioPbTerminalState::kCompleted
                  ? "completed"
                  : pb_state == AudioPbTerminalState::kCancelled
                        ? "cancelled"
                        : "failed",
              stream_stats);
          stream_stats = {};
          log_audio_loopback_stats(
              pb_state == AudioPbTerminalState::kCompleted
                  ? "completed"
                  : pb_state == AudioPbTerminalState::kCancelled
                        ? "cancelled"
                        : "failed");
          if (uxQueueMessagesWaiting(s_audio_play_q) == 0u) {
            speaker_amp_disable();
            log_info("[AUDIO_AMP] disabled GPIO%d after idle stream end",
                     SPEAKER_AMP_CTRL);
          } else {
            log_info("[AUDIO_AMP] kept enabled; queued_audio_jobs=%u",
                     static_cast<unsigned>(
                         uxQueueMessagesWaiting(s_audio_play_q)));
          }
        }
        s_stream_client_session = false;
        audio_emit_pb_terminal(job, pb_state);
        break;
      }
      case AudioPlayJob::Kind::kEmergencyFlush: {
        /* The public cancellation path increments the epoch and drains the
         * queue before this marker is inserted. Do not drain again here: a
         * replacement Begin/Chunk queued behind this marker belongs to the
         * new epoch and must survive. Genuinely stale jobs are rejected by the
         * epoch check at the top of this loop. */
        if (s_stream_pcm_active) {
          (void)speaker_reconfigure(SAMPLE_RATE, 1);
          stop_play();
          s_stream_pcm_active = false;
          s_stream_pcm_rate = SAMPLE_RATE;
          s_stream_pcm_channels = 1;
          deskbot_uplink_set_speaker_active(false);
          log_audio_output_stats("flushed", stream_stats);
          stream_stats = {};
          log_audio_loopback_stats("flushed");
        }
        if (!job.preserve_client_session) {
          s_stream_client_session = false;
        }
        speaker_amp_disable();
        log_warn("[AUDIO] emergency flush: queue drained, stream stopped");
        break;
      }
      case AudioPlayJob::Kind::kReset: {
        if (s_stream_pcm_active) {
          (void)speaker_reconfigure(SAMPLE_RATE, 1);
          stop_play();
          s_stream_pcm_active = false;
          s_stream_pcm_rate = SAMPLE_RATE;
          s_stream_pcm_channels = 1;
          deskbot_uplink_set_speaker_active(false);
          log_audio_output_stats("reset", stream_stats);
          stream_stats = {};
          log_audio_loopback_stats("reset");
        }
        s_stream_client_session = false;
        speaker_amp_disable();
        log_info("[AUDIO] reset: stream stopped, ready for next pb sequence");
        break;
      }
    }
  }
}
bool ensure_audio_play_task() {
  if (s_audio_play_q && s_audio_pb_terminal_q && s_audio_play_task) {
    return true;
  }
  if (!s_audio_play_q) {
    if (psramFound()) {
      s_audio_play_q = xQueueCreateWithCaps(
          AUDIO_PLAY_QUEUE_DEPTH, sizeof(AudioPlayJob),
          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (!s_audio_play_q) {
      s_audio_play_q =
          xQueueCreate(AUDIO_PLAY_QUEUE_DEPTH, sizeof(AudioPlayJob));
    }
  }
  if (!s_audio_pb_terminal_q) {
    if (psramFound()) {
      s_audio_pb_terminal_q = xQueueCreateWithCaps(
          64, sizeof(AudioPbTerminalEvent),
          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    }
    if (!s_audio_pb_terminal_q) {
      s_audio_pb_terminal_q =
          xQueueCreate(64, sizeof(AudioPbTerminalEvent));
    }
  }
  if (!s_audio_play_q || !s_audio_pb_terminal_q) {
    static uint32_t last_allocation_error_ms = 0;
    const uint32_t now_ms = millis();
    if (last_allocation_error_ms == 0 ||
        now_ms - last_allocation_error_ms >= 1000u) {
      last_allocation_error_ms = now_ms;
      log_error(
          "[AUDIO] play runtime allocation failed q=%d terminal_q=%d "
          "free_int=%u free_psram=%u",
          s_audio_play_q != nullptr, s_audio_pb_terminal_q != nullptr,
          static_cast<unsigned>(heap_caps_get_free_size(
              MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
          static_cast<unsigned>(heap_caps_get_free_size(
              MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
    }
    return false;
  }
  if (!s_audio_play_task) {
    TaskHandle_t created_task = nullptr;
    BaseType_t rc = xTaskCreatePinnedToCoreWithCaps(
        audio_play_task_main, "audio_play", 8 * 1024, nullptr, 7,
        &created_task, APP_CPU_NUM,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (rc != pdPASS || created_task == nullptr) {
      created_task = nullptr;
      rc = xTaskCreatePinnedToCore(
          audio_play_task_main, "audio_play", 8 * 1024, nullptr, 7,
          &created_task, APP_CPU_NUM);
    }
    if (rc != pdPASS || created_task == nullptr) {
      log_error(
          "[AUDIO] audio_play_task rc=%d free_int=%u free_psram=%u",
          (int)rc,
          static_cast<unsigned>(heap_caps_get_free_size(
              MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
          static_cast<unsigned>(heap_caps_get_free_size(
              MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
      return false;
    }
    s_audio_play_task = created_task;
    log_info(
        "[AUDIO] play task started prio=7 depth=%d stack=8192B "
        "memory=psram-preferred free_int=%u free_psram=%u",
        (int)AUDIO_PLAY_QUEUE_DEPTH,
        static_cast<unsigned>(heap_caps_get_free_size(
            MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
        static_cast<unsigned>(heap_caps_get_free_size(
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
  }
  return true;
}

}  // namespace

/* 极简流式：固定 16k/mono。第一次 push 时自动 begin（占用 pipeline），stop 时 end（释放 pipeline）。 */
static bool ensure_stream_started(float volume_ratio) {
  if (s_stream_client_session) {
    s_stream_vol = volume_ratio;
    return true;
  }
  if (!ensure_audio_play_task()) {
    return false;
  }
  AudioPlayJob job{};
  job.kind = AudioPlayJob::Kind::kStreamPcm16Begin;
  job.pcm_rate = SAMPLE_RATE;
  job.pcm_channels = 1;
  job.volume = volume_ratio;
  if (!enqueue_audio_play_job(job)) {
    return false;
  }
  s_stream_client_session = true;
  s_stream_vol = volume_ratio;
  return true;
}
bool audio_pb_stream_begin(uint32_t epoch, const char* req, uint32_t idx,
                           uint32_t sample_rate, uint8_t channels,
                           float volume_ratio) {
  if (epoch == 0 || req == nullptr || req[0] == '\0' ||
      sample_rate == 0 || (channels != 1 && channels != 2)) {
    return false;
  }
  AudioPlayJob job{};
  job.kind = AudioPlayJob::Kind::kStreamPcm16Begin;
  job.pcm_rate = sample_rate;
  job.pcm_channels = channels;
  job.volume = volume_ratio;
  audio_fill_pb_meta(job, epoch, req, idx);
  return enqueue_audio_play_job_strict(job);
}
bool audio_pb_stream_push_owned(uint32_t epoch, const char* req, uint32_t idx,
                                int16_t* samples, size_t num_samples,
                                uint32_t caps_for_heap_caps_free,
                                uint32_t start_at_ms,
                                uint32_t sample_rate, uint8_t channels,
                                float volume_ratio) {
  AudioPlayJob j{};
  j.kind = AudioPlayJob::Kind::kStreamPcm16Chunk;
  j.pcm_samples = num_samples;
  j.free_mode = (caps_for_heap_caps_free == 0)
                    ? AudioHeapFree::kMalloc
                    : AudioHeapFree::kHeapCaps;
  j.pcm_heap = samples;
  j.pcm_rate = sample_rate;
  j.pcm_channels = channels;
  j.volume = volume_ratio;
  j.pb_start_at_ms = start_at_ms;
  audio_fill_pb_meta(j, epoch, req, idx);

  if (!j.pb_tracked || samples == nullptr || num_samples == 0 ||
      start_at_ms == 0 || sample_rate == 0 ||
      (channels != 1 && channels != 2)) {
    free_audio_play_job_pcm(j);
    return false;
  }
  if (!enqueue_audio_play_job_strict(j)) {
    free_audio_play_job_pcm(j);
    return false;
  }
  return true;
}

bool audio_pb_stream_end(uint32_t epoch, const char* req, uint32_t idx,
                         uint8_t channels) {
  if (epoch == 0 || req == nullptr || req[0] == '\0' ||
      (channels != 1 && channels != 2)) {
    return false;
  }
  AudioPlayJob j{};
  j.kind = AudioPlayJob::Kind::kStreamPcm16End;
  j.pcm_channels = channels;
  audio_fill_pb_meta(j, epoch, req, idx);
  return enqueue_audio_play_job_strict(j);
}

bool audio_take_pb_terminal_event(AudioPbTerminalEvent* out) {
  if (out == nullptr) {
    return false;
  }
  if (!ensure_audio_play_task()) {
    return false;
  }
  return s_audio_pb_terminal_q != nullptr &&
         xQueueReceive(s_audio_pb_terminal_q, out, 0) == pdTRUE;
}

uint32_t audio_pb_terminal_drop_count() {
  return s_audio_pb_terminal_drop_count.load(std::memory_order_acquire);
}

uint32_t audio_pb_terminal_last_dropped_epoch() {
  return s_audio_pb_terminal_last_dropped_epoch.load(
      std::memory_order_acquire);
}

bool audio_stream_pcm16_push_owned(int16_t* samples, size_t num_samples, uint32_t caps_for_heap_caps_free,
                                   float volume_ratio) {
  const auto free_owned_samples = [caps_for_heap_caps_free](int16_t* owned) {
    if (owned == nullptr) {
      return;
    }
    if (caps_for_heap_caps_free == 0) {
      ::free(owned);
    } else {
      heap_caps_free(owned);
    }
  };
  if (!samples || num_samples == 0) {
    free_owned_samples(samples);
    return false;
  }
  /* A queued begin marks s_stream_client_session before the worker flips active. */
  if (s_stream_pcm_active) {
    s_stream_vol = volume_ratio;
  } else if (!ensure_stream_started(volume_ratio)) {
    free_owned_samples(samples);
    return false;
  }
  AudioPlayJob j{};
  j.kind = AudioPlayJob::Kind::kStreamPcm16Chunk;
  j.pcm_samples = num_samples;
  /* This API is the standalone RTC path and is fixed at 16 kHz mono.  During
   * stream replacement the worker may still expose the previous PB format;
   * copying those atomics here would enqueue a mismatched first RTC chunk. */
  j.pcm_rate = SAMPLE_RATE;
  j.pcm_channels = 1;
  j.free_mode = (caps_for_heap_caps_free == 0) ? AudioHeapFree::kMalloc : AudioHeapFree::kHeapCaps;
  j.pcm_heap = samples;
  if (!enqueue_audio_play_job(j)) {
    free_audio_play_job_pcm(j);
    return false;
  }
  return true;
}

bool audio_stream_pcm16_replace(float volume_ratio) {
  if (!ensure_audio_play_task()) {
    return false;
  }
  /* Replacement invalidates PCM that may already have been submitted as the
   * AEC far-end reference but has not reached the loudspeaker.  Start the new
   * stream with a fresh reference timeline instead of feeding that stale tail
   * to ESP-SR. */
  audio_frontend_abort_playback_reference();
  const uint32_t replace_epoch =
      s_audio_cancel_epoch.fetch_add(1, std::memory_order_acq_rel) + 1;
  s_stream_client_session = false;
  drain_audio_play_queue();

  AudioPlayJob flush{};
  flush.kind = AudioPlayJob::Kind::kEmergencyFlush;
  flush.cancel_epoch = replace_epoch;
  /* This flush separates an old stream from a replacement stream. The
   * producer has already committed the new session below; unlike an explicit
   * cancellation it must not clear that producer-side state when processed
   * asynchronously after audio_stream_pcm16_replace() returns. */
  flush.preserve_client_session = true;
  if (xQueueSendToFront(s_audio_play_q, &flush, 0) != pdTRUE) {
    log_error("[AUDIO] stream replace flush queue unavailable epoch=%u",
              static_cast<unsigned>(replace_epoch));
    return false;
  }

  AudioPlayJob begin{};
  begin.kind = AudioPlayJob::Kind::kStreamPcm16Begin;
  begin.pcm_rate = SAMPLE_RATE;
  begin.pcm_channels = 1;
  begin.volume = volume_ratio;
  begin.cancel_epoch = replace_epoch;
  if (xQueueSend(s_audio_play_q, &begin, 0) != pdTRUE) {
    log_error("[AUDIO] stream replace begin queue unavailable epoch=%u",
              static_cast<unsigned>(replace_epoch));
    return false;
  }
  /* Publish the replacement format before the decoder can enqueue its first
   * PCM job.  The queued Begin remains the worker-side authority. */
  s_stream_pcm_rate.store(SAMPLE_RATE, std::memory_order_release);
  s_stream_pcm_channels.store(1, std::memory_order_release);
  /*
   * Mark the client session now, before audio_play processes Begin.  This
   * prevents the decoder task from inserting a duplicate Begin or a Chunk in
   * front of the replacement boundary.
   */
  s_stream_client_session = true;
  s_stream_vol = volume_ratio;
  return true;
}

void audio_stream_pcm16_stop() {
  /* END is asynchronous.  Atomically close the producer-side session before
   * enqueueing it so loopLite cannot queue duplicate END markers while the
   * audio task is still writing the DMA silence tail. */
  if (!s_stream_client_session.exchange(false, std::memory_order_acq_rel)) {
    return;
  }
  if (!ensure_audio_play_task()) {
    return;
  }
  AudioPlayJob j{};
  j.kind = AudioPlayJob::Kind::kStreamPcm16End;
  j.pcm_channels =
      s_stream_pcm_channels.load(std::memory_order_acquire);
  j.cancel_epoch = s_audio_cancel_epoch.load(std::memory_order_acquire);
  if (xQueueSend(s_audio_play_q, &j, 0) != pdTRUE) {
    log_warn("[AUDIO] async stream stop queue full; forcing cancellation");
    audio_play_emergency_flush();
  }
}

void audio_play_emergency_flush() {
  /* Emergency cancellation clears speaker DMA/PCM.  Its far-end AEC timeline
   * must be invalidated at the same instant; otherwise ESP-SR consumes audio
   * that was never emitted and can create a false VAD/ASR tail. */
  audio_frontend_abort_playback_reference();
  if (!ensure_audio_play_task()) {
    return;
  }
  const uint32_t flush_epoch =
      s_audio_cancel_epoch.fetch_add(1, std::memory_order_acq_rel) + 1;
  /*
   * Cancellation is immediately visible to the currently executing job.
   * Queue cleanup and worker shutdown are asynchronous: disconnect callbacks
   * run on the USB parser owner and must never wait for I2S or another task.
   */
  s_stream_client_session = false;
  drain_audio_play_queue();
  AudioPlayJob j{};
  j.kind = AudioPlayJob::Kind::kEmergencyFlush;
  j.cancel_epoch = flush_epoch;
  if (xQueueSendToFront(s_audio_play_q, &j, 0) != pdTRUE) {
    log_error("[AUDIO] emergency flush queue unavailable epoch=%u",
              static_cast<unsigned>(flush_epoch));
  }
}

unsigned audio_play_input_queue_depth() {
  if (!ensure_audio_play_task()) {
    return 0u;
  }
  return (unsigned)uxQueueMessagesWaiting(s_audio_play_q);
}

bool audio_play_stream_pcm_active() {
  if (!ensure_audio_play_task()) {
    return false;
  }
  return s_stream_pcm_active.load(std::memory_order_acquire);
}

/** 扬声器是否正在输出可听 PCM（chunk mean-abs×volume ≥ DESKBOT_SPEAKER_AUDIBLE_MEAN_ABS）。
 * 不含：stream begin、队列里仅有 Begin/End job、静音 tail flush、stream_open 空窗。 */
bool audio_play_speaker_busy() {
  return s_audible_play_in_progress;
}

bool audio_play_i2s_in_progress() {
  return s_i2s_play_in_progress;
}

bool audio_play_is_on_play_task() {
  return s_audio_play_task != nullptr &&
         xTaskGetCurrentTaskHandle() == s_audio_play_task;
}

void audio_play_reset() {
  if (!ensure_audio_play_task()) {
    return;
  }
  const uint32_t reset_epoch =
      s_audio_cancel_epoch.fetch_add(1, std::memory_order_acq_rel) + 1;
  s_stream_client_session = false;
  drain_audio_play_queue();
  AudioPlayJob j{};
  j.kind = AudioPlayJob::Kind::kReset;
  j.cancel_epoch = reset_epoch;
  if (xQueueSendToFront(s_audio_play_q, &j, 0) != pdTRUE) {
    log_error("[AUDIO] reset queue unavailable epoch=%u",
              static_cast<unsigned>(reset_epoch));
  }
}

void task_setup_audio_play() {
  ensure_audio_play_task();
}

void audio_play_v2_startup_self_test(uint32_t duration_ms) {
#if DESKBOT_BOARD_V2
  if (!ensure_audio_play_task()) {
    log_error("[AUDIO_SELFTEST] play task unavailable");
    return;
  }

  const uint32_t bounded_duration_ms =
      duration_ms < 250u ? 250u : duration_ms > 15000u ? 15000u : duration_ms;
  const size_t kToneSamples =
      static_cast<size_t>(SAMPLE_RATE) * bounded_duration_ms / 1000u;
  constexpr int kToneHz = 500;
  constexpr int16_t kAmplitude = 12000;
  int16_t* pcm = static_cast<int16_t*>(heap_caps_malloc(
      kToneSamples * sizeof(int16_t),
      MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  uint32_t free_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
  if (pcm == nullptr) {
    pcm = static_cast<int16_t*>(malloc(kToneSamples * sizeof(int16_t)));
    free_caps = 0;
  }
  if (pcm == nullptr) {
    log_error("[AUDIO_SELFTEST] tone allocation failed samples=%u",
              static_cast<unsigned>(kToneSamples));
    return;
  }

  for (size_t i = 0; i < kToneSamples; ++i) {
    const size_t phase =
        (i * static_cast<size_t>(kToneHz) * 2u) / SAMPLE_RATE;
    pcm[i] = (phase & 1u) ? kAmplitude : -kAmplitude;
  }

  log_warn("[AUDIO_SELFTEST] begin 500Hz/%ums amp=%d "
           "format=philips/stereo-both BCLK=%d WS=%d DOUT=%d amp_ctrl=%d",
           static_cast<unsigned>(bounded_duration_ms),
           static_cast<int>(kAmplitude), static_cast<int>(SPEAKER_I2S_BCLK),
           static_cast<int>(SPEAKER_I2S_WS),
           static_cast<int>(SPEAKER_I2S_DOUT),
           static_cast<int>(SPEAKER_AMP_CTRL));
  if (!audio_stream_pcm16_replace(1.0f)) {
    if (free_caps == 0) {
      free(pcm);
    } else {
      heap_caps_free(pcm);
    }
    log_error("[AUDIO_SELFTEST] replace failed");
    return;
  }
  if (!audio_stream_pcm16_push_owned(
          pcm, kToneSamples, free_caps, 1.0f)) {
    /* push_owned always owns and frees pcm on failure. */
    log_error("[AUDIO_SELFTEST] tone enqueue failed");
    audio_stream_pcm16_stop();
    return;
  }
  audio_stream_pcm16_stop();
  log_warn("[AUDIO_SELFTEST] queued");
#else
  (void)duration_ms;
#endif
}
