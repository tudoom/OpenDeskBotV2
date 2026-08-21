#include "audio_capture.h"

#include "audio_frontend_esp_sr.h"
#include "audio_player.h"
#include "deskbot_uplink_state.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "freertos/queue.h"

#include <atomic>
#include <driver/i2s_pdm.h>
#include <string.h>

namespace {

QueueHandle_t     s_mic_q           = nullptr;
TaskHandle_t      s_mic_task        = nullptr;
SemaphoreHandle_t s_consumer_mutex = nullptr;
i2s_chan_handle_t s_mic_rx          = nullptr;
bool              s_mic_rx_enabled  = false;

std::atomic<uint32_t> s_last_capture_attempt_ms{0};
std::atomic<uint32_t> s_last_capture_frame_ms{0};
/* The consumer runs continuously while listening, so this is only a short
 * jitter buffer. Keeping 600 x 640-byte frames here consumed about 384 KiB
 * of internal RAM and could make xQueueCreate() fail at boot. */
constexpr UBaseType_t kMicCaptureQueueDepth = 16;
constexpr uint32_t kMicDmaDescriptorCount = 8;
constexpr size_t kPdmSlotCount = 2;
constexpr size_t kPdmReadSamples =
    kMicCaptureFrameSamples * kPdmSlotCount;
constexpr uint32_t kMicStartupDiscardFrames = 12;  // 240 ms
constexpr uint32_t kMicSignalRecoveryFrames = 5;   // 100 ms
constexpr uint32_t kMicRawFreezeFrames = 50;       // 1 s @ 20 ms/frame
constexpr uint32_t kMicNoSignalRestartFrames = 100;  // 2 s
constexpr uint32_t kMicSlotSwitchFrames = 8;
constexpr uint32_t kMicNoSignalQuickRestarts = 2;
constexpr uint32_t kMicNoSignalRetryBackoffMs = 30000;
constexpr uint32_t kMicNoSignalLogIntervalMs = 10000;

int16_t s_previous_raw_frame[kMicCaptureFrameSamples]{};
bool s_previous_raw_valid = false;
uint32_t s_identical_raw_frames = 0;
uint32_t s_no_signal_frames = 0;
uint32_t s_valid_signal_frames = 0;
uint32_t s_startup_discard_frames = 0;
int s_selected_slot = -1;
int s_pending_slot = -1;
uint32_t s_pending_slot_frames = 0;
std::atomic<bool> s_raw_signal_healthy{false};
portMUX_TYPE s_playback_stats_mux = portMUX_INITIALIZER_UNLOCKED;
MicPlaybackCaptureStats s_playback_stats{};

int16_t   s_partial[kMicCaptureFrameSamples];
size_t    s_partial_len = 0;

struct PdmSlotStats {
  int16_t minimum = 0;
  int16_t maximum = 0;
  int32_t mean = 0;
  uint32_t mean_abs_delta = 0;
  uint32_t peak_to_peak = 0;
  uint32_t transitions = 0;
  uint32_t score = 0;
  bool valid = false;
};

PdmSlotStats mic_slot_stats(const int16_t* interleaved, size_t slot) {
  PdmSlotStats stats{};
  if (!interleaved || slot >= kPdmSlotCount) {
    return stats;
  }
  int16_t minimum = INT16_MAX;
  int16_t maximum = INT16_MIN;
  int64_t sum = 0;
  uint64_t delta_sum = 0;
  uint32_t transitions = 0;
  int16_t previous = interleaved[slot];
  for (size_t i = 0; i < kMicCaptureFrameSamples; ++i) {
    const int16_t sample = interleaved[i * kPdmSlotCount + slot];
    if (sample < minimum) {
      minimum = sample;
    }
    if (sample > maximum) {
      maximum = sample;
    }
    sum += sample;
    if (i > 0) {
      const int32_t delta =
          static_cast<int32_t>(sample) - static_cast<int32_t>(previous);
      delta_sum += static_cast<uint32_t>(delta < 0 ? -delta : delta);
      if (sample != previous) {
        ++transitions;
      }
    }
    previous = sample;
  }
  stats.minimum = minimum;
  stats.maximum = maximum;
  stats.mean =
      static_cast<int32_t>(sum / static_cast<int64_t>(kMicCaptureFrameSamples));
  stats.mean_abs_delta = static_cast<uint32_t>(
      delta_sum / static_cast<uint64_t>(kMicCaptureFrameSamples - 1));
  stats.peak_to_peak =
      static_cast<uint32_t>(static_cast<int32_t>(maximum) -
                            static_cast<int32_t>(minimum));
  stats.transitions = transitions;

  /*
   * An inactive PDM edge on the XIAO Sense can be reported as a large,
   * perfectly flat negative PCM value instead of zero.  A live digital
   * microphone always retains a little quantisation noise, even in a quiet
   * room, so require both in-frame range and repeated sample transitions.
   */
  const bool stuck_near_rail =
      (stats.mean > 30000 || stats.mean < -30000) &&
      stats.peak_to_peak < 512;
  stats.valid =
      !stuck_near_rail && stats.peak_to_peak >= 8 &&
      stats.transitions >= 8;
  if (stats.valid) {
    const uint64_t score =
        static_cast<uint64_t>(stats.peak_to_peak) * 4U +
        static_cast<uint64_t>(stats.mean_abs_delta) * 2U +
        static_cast<uint64_t>(stats.transitions);
    stats.score =
        score > UINT32_MAX ? UINT32_MAX : static_cast<uint32_t>(score);
  }
  return stats;
}

void mic_reset_signal_state() {
  s_previous_raw_valid = false;
  s_identical_raw_frames = 0;
  s_no_signal_frames = 0;
  s_valid_signal_frames = 0;
  s_startup_discard_frames = 0;
  s_selected_slot = -1;
  s_pending_slot = -1;
  s_pending_slot_frames = 0;
  s_raw_signal_healthy.store(false, std::memory_order_release);
}

void mic_purge_pipeline() {
  if (s_mic_q) {
    xQueueReset(s_mic_q);
  }
  audio_frontend_flush();
}

void mic_release_driver() {
  if (!s_mic_rx) {
    return;
  }
  if (s_mic_rx_enabled) {
    (void)i2s_channel_disable(s_mic_rx);
    s_mic_rx_enabled = false;
  }
  (void)i2s_del_channel(s_mic_rx);
  s_mic_rx = nullptr;
}

void mic_release_unstarted_resources() {
  /* Only call this before the capture task has been created. */
  mic_release_driver();
  if (s_consumer_mutex) {
    vSemaphoreDelete(s_consumer_mutex);
    s_consumer_mutex = nullptr;
  }
  if (s_mic_q) {
    vQueueDelete(s_mic_q);
    s_mic_q = nullptr;
  }
  s_partial_len = 0;
}

bool mic_setup_driver() {
  i2s_chan_config_t channel =
      I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
  channel.dma_desc_num = kMicDmaDescriptorCount;
  channel.dma_frame_num = kMicCaptureFrameSamples;

  esp_err_t err = i2s_new_channel(&channel, nullptr, &s_mic_rx);
  if (err != ESP_OK) {
    log_error("[MIC] PDM new-channel failed err=%d", (int)err);
    s_mic_rx = nullptr;
    return false;
  }

  i2s_pdm_rx_config_t config = {
      .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
      .slot_cfg = I2S_PDM_RX_SLOT_PCM_FMT_DEFAULT_CONFIG(
          I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO),
      .gpio_cfg =
          {
              .clk = PDM_MIC_CLK,
              .dins =
                  {
                      PDM_MIC_DATA,
                      GPIO_NUM_NC,
                      GPIO_NUM_NC,
                      GPIO_NUM_NC,
                  },
              .invert_flags =
                  {
                      .clk_inv = false,
                  },
          },
  };
  /*
   * Capture both PDM clock edges.  XIAO Sense board revisions and ESP-IDF's
   * legacy/new driver mapping do not agree on which logical edge is "left".
   * The capture task validates both slots and locks to the live microphone.
   */
  config.slot_cfg.slot_mask = I2S_PDM_SLOT_BOTH;
  config.clk_cfg.dn_sample_mode = I2S_PDM_DSR_8S;

  err = i2s_channel_init_pdm_rx_mode(s_mic_rx, &config);
  if (err != ESP_OK) {
    log_error("[MIC] PDM mode init failed err=%d", (int)err);
    mic_release_driver();
    return false;
  }
  err = i2s_channel_enable(s_mic_rx);
  if (err != ESP_OK) {
    log_error("[MIC] PDM channel enable failed err=%d", (int)err);
    mic_release_driver();
    return false;
  }
  s_mic_rx_enabled = true;
  return true;
}

bool mic_restart_driver(const char* reason) {
  log_error("[MIC] restarting PDM capture reason=%s",
            reason ? reason : "unknown");
  s_raw_signal_healthy.store(false, std::memory_order_release);
  mic_purge_pipeline();
  mic_release_driver();
  /*
   * Let the microphone clock and the PDM decimator fully settle.  A 20 ms
   * recycle produced a full-scale impulse which was previously uploaded as
   * speech and could keep server VAD open.
   */
  vTaskDelay(pdMS_TO_TICKS(100));
  const bool ready = mic_setup_driver();
  mic_reset_signal_state();
  if (!ready) {
    log_error("[MIC] PDM restart failed; will retry");
  }
  return ready;
}

void mic_enqueue_drop_oldest(const MicCaptureFrame& f) {
  if (!s_mic_q) {
    return;
  }
  while (xQueueSend(s_mic_q, &f, 0) != pdTRUE) {
    MicCaptureFrame discarded{};
    if (xQueueReceive(s_mic_q, &discarded, 0) != pdTRUE) {
      break;
    }
  }
}

void mic_capture_task(void* /*arg*/) {
  MicCaptureFrame frame{};
  int16_t interleaved[kPdmReadSamples]{};
  uint32_t last_stall_log_ms = 0;
  uint32_t last_signal_log_ms = 0;
  uint32_t no_signal_restart_count = 0;
  uint32_t next_no_signal_retry_ms = 0;
  for (;;) {
    if (!s_mic_rx || !s_mic_rx_enabled) {
      if (!mic_restart_driver("driver_unavailable")) {
        vTaskDelay(pdMS_TO_TICKS(1000));
        continue;
      }
    }
    s_last_capture_attempt_ms.store(millis(), std::memory_order_release);
    size_t bytes_read = 0;
    const esp_err_t read_err =
        i2s_channel_read(s_mic_rx,
                         interleaved,
                         sizeof(interleaved),
                         &bytes_read,
                         250);
    /* 容错：极少数情况下对齐不足，读满下一轮补 */
    if (read_err != ESP_OK || bytes_read != sizeof(interleaved)) {
      const uint32_t now = millis();
      if (now - last_stall_log_ms >= 2000U) {
        log_warn("[MIC] I2S RX stalled err=%d bytes=%u expected=%u",
                 (int)read_err, (unsigned)bytes_read,
                 (unsigned)sizeof(interleaved));
        last_stall_log_ms = now;
      }
      vTaskDelay(1);
      continue;
    }
    s_last_capture_frame_ms.store(millis(), std::memory_order_release);

    const PdmSlotStats slots[kPdmSlotCount] = {
        mic_slot_stats(interleaved, 0),
        mic_slot_stats(interleaved, 1),
    };
    int best_slot = -1;
    if (slots[0].valid || slots[1].valid) {
      best_slot =
          !slots[1].valid ||
                  (slots[0].valid && slots[0].score >= slots[1].score)
              ? 0
              : 1;
    }

    /*
     * Keep a valid selected slot unless the alternative is consistently
     * stronger. This avoids channel flapping if the peripheral mirrors a
     * small amount of noise into the inactive slot.
     */
    if (s_selected_slot < 0 ||
        !slots[static_cast<size_t>(s_selected_slot)].valid) {
      s_selected_slot = best_slot;
      s_pending_slot = -1;
      s_pending_slot_frames = 0;
    } else if (best_slot >= 0 && best_slot != s_selected_slot &&
               static_cast<uint64_t>(
                   slots[static_cast<size_t>(best_slot)].score) >
                   static_cast<uint64_t>(
                       slots[static_cast<size_t>(s_selected_slot)].score) *
                       2U) {
      if (s_pending_slot == best_slot) {
        ++s_pending_slot_frames;
      } else {
        s_pending_slot = best_slot;
        s_pending_slot_frames = 1;
      }
      if (s_pending_slot_frames >= kMicSlotSwitchFrames) {
        log_warn("[MIC] switching active PDM slot %d -> %d",
                 s_selected_slot, best_slot);
        s_selected_slot = best_slot;
        s_pending_slot = -1;
        s_pending_slot_frames = 0;
        s_previous_raw_valid = false;
      }
    } else {
      s_pending_slot = -1;
      s_pending_slot_frames = 0;
    }

    const bool selected_valid =
        s_selected_slot >= 0 &&
        slots[static_cast<size_t>(s_selected_slot)].valid;
    if (!selected_valid) {
      s_raw_signal_healthy.store(false, std::memory_order_release);
      s_valid_signal_frames = 0;
      s_previous_raw_valid = false;
      s_identical_raw_frames = 0;
      if (s_no_signal_frames < UINT32_MAX) {
        ++s_no_signal_frames;
      }
      const uint32_t now = millis();
      if (last_signal_log_ms == 0 ||
          static_cast<uint32_t>(now - last_signal_log_ms) >=
              kMicNoSignalLogIntervalMs) {
        log_error(
            "[MIC] no live PDM slot "
            "slot0(min/mean/max/range/delta/trans)=%d/%d/%d/%u/%u/%u "
            "slot1=%d/%d/%d/%u/%u/%u frames=%u restarts=%u degraded=%s",
            static_cast<int>(slots[0].minimum),
            static_cast<int>(slots[0].mean),
            static_cast<int>(slots[0].maximum),
            static_cast<unsigned>(slots[0].peak_to_peak),
            static_cast<unsigned>(slots[0].mean_abs_delta),
            static_cast<unsigned>(slots[0].transitions),
            static_cast<int>(slots[1].minimum),
            static_cast<int>(slots[1].mean),
            static_cast<int>(slots[1].maximum),
            static_cast<unsigned>(slots[1].peak_to_peak),
            static_cast<unsigned>(slots[1].mean_abs_delta),
            static_cast<unsigned>(slots[1].transitions),
            static_cast<unsigned>(s_no_signal_frames),
            static_cast<unsigned>(no_signal_restart_count),
            no_signal_restart_count >= kMicNoSignalQuickRestarts
                ? "true"
                : "false");
        last_signal_log_ms = now;
      }
      if (s_no_signal_frames >= kMicNoSignalRestartFrames) {
        if (no_signal_restart_count < kMicNoSignalQuickRestarts) {
          ++no_signal_restart_count;
          if (no_signal_restart_count == kMicNoSignalQuickRestarts) {
            next_no_signal_retry_ms = now + kMicNoSignalRetryBackoffMs;
          }
          (void)mic_restart_driver("no_live_pdm_slot");
        } else if (next_no_signal_retry_ms == 0) {
          next_no_signal_retry_ms = now + kMicNoSignalRetryBackoffMs;
          log_error(
              "[MIC] PDM signal unavailable after %u restarts; "
              "entering degraded capture with %lums retry backoff",
              static_cast<unsigned>(no_signal_restart_count),
              static_cast<unsigned long>(kMicNoSignalRetryBackoffMs));
        } else if (static_cast<int32_t>(now - next_no_signal_retry_ms) >= 0) {
          next_no_signal_retry_ms = now + kMicNoSignalRetryBackoffMs;
          (void)mic_restart_driver("no_live_pdm_slot_backoff");
        }
      }
      continue;
    }
    s_no_signal_frames = 0;

    for (size_t i = 0; i < kMicCaptureFrameSamples; ++i) {
      frame.pcm[i] =
          interleaved[i * kPdmSlotCount +
                      static_cast<size_t>(s_selected_slot)];
    }

    if (s_startup_discard_frames < kMicStartupDiscardFrames) {
      ++s_startup_discard_frames;
      s_raw_signal_healthy.store(false, std::memory_order_release);
      continue;
    }

    const bool identical =
        s_previous_raw_valid &&
        memcmp(s_previous_raw_frame, frame.pcm, sizeof(frame.pcm)) == 0;
    memcpy(s_previous_raw_frame, frame.pcm, sizeof(frame.pcm));
    s_previous_raw_valid = true;
    if (identical) {
      if (s_identical_raw_frames < UINT32_MAX) {
        ++s_identical_raw_frames;
      }
    } else {
      s_identical_raw_frames = 0;
    }
    if (s_identical_raw_frames >= kMicRawFreezeFrames) {
      (void)mic_restart_driver("identical_raw_pcm");
      continue;
    }
    if (s_valid_signal_frames < kMicSignalRecoveryFrames) {
      ++s_valid_signal_frames;
      if (s_valid_signal_frames < kMicSignalRecoveryFrames) {
        continue;
      }
      s_raw_signal_healthy.store(true, std::memory_order_release);
      no_signal_restart_count = 0;
      next_no_signal_retry_ms = 0;
      log_warn("[MIC] live PDM signal locked slot=%d after stable recovery",
               s_selected_slot);
    }
    if (deskbot_uplink_speaker_audible()) {
      int64_t sum = 0;
      for (const int16_t sample : frame.pcm) {
        sum += sample;
      }
      const int32_t mean = static_cast<int32_t>(
          sum / static_cast<int64_t>(kMicCaptureFrameSamples));
      uint64_t local_absolute_sum = 0;
      uint64_t local_square_sum = 0;
      uint32_t local_peak = 0;
      for (const int16_t sample : frame.pcm) {
        const int32_t centered = static_cast<int32_t>(sample) - mean;
        const uint32_t magnitude = static_cast<uint32_t>(
            centered < 0 ? -static_cast<int64_t>(centered) : centered);
        local_absolute_sum += magnitude;
        local_square_sum +=
            static_cast<uint64_t>(static_cast<int64_t>(centered) * centered);
        if (magnitude > local_peak) {
          local_peak = magnitude;
        }
      }
      portENTER_CRITICAL(&s_playback_stats_mux);
      s_playback_stats.samples += kMicCaptureFrameSamples;
      s_playback_stats.absolute_sum += local_absolute_sum;
      s_playback_stats.square_sum += local_square_sum;
      if (local_peak > s_playback_stats.peak) {
        s_playback_stats.peak = local_peak;
      }
      portEXIT_CRITICAL(&s_playback_stats_mux);
    }
    const uint32_t now = millis();
    if (last_signal_log_ms == 0 ||
        static_cast<uint32_t>(now - last_signal_log_ms) >= 5000U) {
      int16_t minimum = INT16_MAX;
      int16_t maximum = INT16_MIN;
      uint64_t abs_sum = 0;
      for (const int16_t sample : frame.pcm) {
        if (sample < minimum) {
          minimum = sample;
        }
        if (sample > maximum) {
          maximum = sample;
        }
        abs_sum += static_cast<uint32_t>(
            sample == INT16_MIN ? 32768 : abs(sample));
      }
      log_info(
          "[MIC] raw signal slot=%d min=%d mean_abs=%u max=%d "
          "identical=%u slot0_score=%u slot1_score=%u",
          s_selected_slot,
          static_cast<int>(minimum),
          static_cast<unsigned>(abs_sum / kMicCaptureFrameSamples),
          static_cast<int>(maximum),
          static_cast<unsigned>(s_identical_raw_frames),
          static_cast<unsigned>(slots[0].score),
          static_cast<unsigned>(slots[1].score));
      last_signal_log_ms = now;
    }
    /*
     * Local capture and ESP-SR must never follow the uplink gate. During
     * speaker playback AEC needs both microphone and playback-reference
     * timelines, and VAD must remain able to detect an interrupting speaker.
     * Only the network/USB sender decides whether processed PCM is uploaded.
     */
    // Retain raw PCM continuously so an unhealthy AFE cannot mute the device.
    mic_enqueue_drop_oldest(frame);
    if (audio_frontend_ready()) {
      (void)audio_frontend_submit_mic(
          frame.pcm, kMicCaptureFrameSamples);
    }
  }
}

}  // namespace

void task_setup_mic_capture() {
  if (s_mic_q && s_mic_task && s_consumer_mutex && s_mic_rx &&
      s_mic_rx_enabled) {
    return;
  }

  /* A partial setup is never usable and must not leave mic_consumer_read()
   * waiting on a queue that has no producer. */
  if (s_mic_q || s_consumer_mutex || s_mic_task || s_mic_rx ||
      s_mic_rx_enabled) {
    log_error("[MIC] inconsistent capture state; capture disabled");
    return;
  }

  s_mic_q =
      xQueueCreate(kMicCaptureQueueDepth, sizeof(MicCaptureFrame));
  if (!s_mic_q) {
    log_error("[MIC] capture queue allocation failed depth=%u bytes=%u; "
              "capture disabled",
              (unsigned)kMicCaptureQueueDepth,
              (unsigned)(kMicCaptureQueueDepth * sizeof(MicCaptureFrame)));
    return;
  }

  s_consumer_mutex = xSemaphoreCreateMutex();
  if (!s_consumer_mutex) {
    log_error("[MIC] consumer mutex allocation failed; capture disabled");
    mic_release_unstarted_resources();
    return;
  }

  if (!mic_setup_driver()) {
    log_error("[MIC] PDM driver setup failed; capture disabled");
    mic_release_unstarted_resources();
    return;
  }

  /* 优先级 6：高于 display(2)/motor(3)，尽快把 I2S DMA 搬进 RAM */
  TaskHandle_t created_task = nullptr;
  const BaseType_t rc = xTaskCreatePinnedToCore(
      mic_capture_task,
      "mic_cap",
      8192,
      nullptr,
      6,
      &created_task,
      APP_CPU_NUM);
  if (rc != pdPASS || !created_task) {
    log_error("[MIC] capture task create failed rc=%d; capture disabled",
              (int)rc);
    mic_release_unstarted_resources();
    return;
  }

  s_mic_task = created_task;
  log_info("[MIC] PDM new-channel capture started %uHz %u samp/frame "
           "CLK=%d DATA=%d slots=stereo_auto dsr=8s "
           "queue_depth=%u queue_bytes=%u",
           (unsigned)SAMPLE_RATE, (unsigned)kMicCaptureFrameSamples,
           (int)PDM_MIC_CLK, (int)PDM_MIC_DATA,
           (unsigned)kMicCaptureQueueDepth,
           (unsigned)(kMicCaptureQueueDepth * sizeof(MicCaptureFrame)));
}

void mic_capture_flush_queue() {
  if (!s_mic_q || !s_consumer_mutex) {
    return;
  }
  if (xSemaphoreTake(s_consumer_mutex, pdMS_TO_TICKS(100)) != pdTRUE) {
    log_warn("[MIC] flush skipped: consumer busy");
    return;
  }
  s_partial_len = 0;
  MicCaptureFrame tmp{};
  while (xQueueReceive(s_mic_q, &tmp, 0) == pdTRUE) {
  }
  audio_frontend_flush();
  xSemaphoreGive(s_consumer_mutex);
}

bool mic_consumer_read(
    int16_t* dst,
    size_t length,
    TickType_t first_frame_ticks) {
  if (!dst || length == 0) {
    return false;
  }
  if (!s_mic_q || !s_mic_task || !s_consumer_mutex || !s_mic_rx ||
      !s_mic_rx_enabled) {
    /* Fail closed: the capture task is the sole I2S0 reader. A direct-read
     * fallback here would race it after a partial setup and could deadlock
     * I2S. Initialising the caller's buffer also avoids leaking stack data. */
    memset(dst, 0, length * sizeof(int16_t));
    const TickType_t fallback_ticks = pdMS_TO_TICKS(
        (length * 1000U + SAMPLE_RATE - 1U) / SAMPLE_RATE);
    vTaskDelay(fallback_ticks > 0 ? fallback_ticks : 1);
    return false;
  }

  if (!mic_capture_signal_healthy()) {
    /*
     * Preserve real-time audio pacing while the physical microphone is
     * degraded.  Returning immediately makes the voice-round loop spin tens
     * of thousands of times per second, starving USB/camera work and flooding
     * logs.  No invalid PCM is released: the caller still receives zeros and
     * a false result after one nominal audio-frame duration.
     */
    memset(dst, 0, length * sizeof(int16_t));
    const TickType_t frame_ticks = pdMS_TO_TICKS(
        (length * 1000U + SAMPLE_RATE - 1U) / SAMPLE_RATE);
    vTaskDelay(frame_ticks > 0 ? frame_ticks : 1);
    return false;
  }

  if (first_frame_ticks == portMAX_DELAY) {
    log_error("[MIC] refusing unbounded consumer wait");
    first_frame_ticks = pdMS_TO_TICKS(100);
  }
  if (xSemaphoreTake(s_consumer_mutex, first_frame_ticks) != pdTRUE) {
    memset(dst, 0, length * sizeof(int16_t));
    return false;
  }

  size_t out = 0;
  TickType_t wait_ticks = first_frame_ticks;

  while (out < length) {
    if (s_partial_len > 0) {
      size_t take = s_partial_len;
      if (take > length - out) {
        take = length - out;
      }
      memcpy(dst + out, s_partial, take * sizeof(int16_t));
      out += take;
      s_partial_len -= take;
      if (s_partial_len > 0) {
        memmove(s_partial, s_partial + take, s_partial_len * sizeof(int16_t));
      }
      wait_ticks = 0;
      continue;
    }

    MicCaptureFrame frame{};
    const bool got_frame =
        mic_capture_signal_healthy() &&
                audio_frontend_output_healthy()
            ? audio_frontend_take_output_frame(frame.pcm, wait_ticks)
            : mic_capture_signal_healthy() &&
                      xQueueReceive(s_mic_q, &frame, wait_ticks) == pdTRUE;
    if (!got_frame) {
      break;
    }
    wait_ticks = 0;

    size_t frame_remain = kMicCaptureFrameSamples;
    int16_t* fp = frame.pcm;
    while (frame_remain > 0 && out < length) {
      size_t take = frame_remain;
      if (take > length - out) {
        take = length - out;
      }
      memcpy(dst + out, fp, take * sizeof(int16_t));
      out += take;
      fp += take;
      frame_remain -= take;
    }
    if (frame_remain > 0) {
      memcpy(s_partial, fp, frame_remain * sizeof(int16_t));
      s_partial_len = frame_remain;
    }
  }

  if (out < length) {
    memset(dst + out, 0, (length - out) * sizeof(int16_t));
  }
  xSemaphoreGive(s_consumer_mutex);
  return out == length;
}

uint32_t mic_capture_last_attempt_ms() {
  return s_last_capture_attempt_ms.load(std::memory_order_acquire);
}

uint32_t mic_capture_last_frame_ms() {
  return s_last_capture_frame_ms.load(std::memory_order_acquire);
}

void mic_capture_playback_stats_reset() {
  portENTER_CRITICAL(&s_playback_stats_mux);
  s_playback_stats = {};
  portEXIT_CRITICAL(&s_playback_stats_mux);
}

MicPlaybackCaptureStats mic_capture_playback_stats_take() {
  MicPlaybackCaptureStats result{};
  portENTER_CRITICAL(&s_playback_stats_mux);
  result = s_playback_stats;
  s_playback_stats = {};
  portEXIT_CRITICAL(&s_playback_stats_mux);
  return result;
}

bool mic_capture_signal_healthy() {
  return s_raw_signal_healthy.load(std::memory_order_acquire);
}
