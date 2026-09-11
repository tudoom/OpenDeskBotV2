#include "rtc_audio_downlink.h"

#include "audio_player.h"
#include "deskbot_config.h"
#include "display.h"
#include "logger.h"
#include "opus.h"
#include "usb_transport.h"

#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/idf_additions.h>
#include <freertos/queue.h>
#include <freertos/task.h>

#include <atomic>
#include <string.h>

namespace {

constexpr UBaseType_t kQueueDepth = 24;
constexpr uint16_t kMaxBatchFrames = 5;
constexpr int kSampleRate = SAMPLE_RATE;
constexpr int kMaxOpusFrameSamples = kSampleRate * 60 / 1000;
constexpr uint32_t kDecodeTaskStackBytes = 32u * 1024u;

struct RtcAudioJob {
  uint8_t* payload = nullptr;
  size_t length = 0;
  uint16_t frames = 0;
  uint32_t session_epoch = 0;
  uint32_t generation = 0;
  bool end_stream = false;
};

QueueHandle_t s_queue = nullptr;
TaskHandle_t s_task = nullptr;
OpusDecoder* s_decoder = nullptr;
uint32_t s_decoder_generation = 0;

std::atomic<uint32_t> s_generation{1};
std::atomic<bool> s_active{false};
std::atomic<bool> s_closing{false};
std::atomic<bool> s_end_ready{false};
std::atomic<bool> s_decode_busy{false};
std::atomic<uint32_t> s_drop_count{0};
std::atomic<bool> s_first_pcm_logged{false};

void free_job(RtcAudioJob& job) {
  if (job.payload != nullptr) {
    heap_caps_free(job.payload);
    job.payload = nullptr;
  }
}

bool job_is_current(const RtcAudioJob& job) {
  return job.generation == s_generation.load(std::memory_order_acquire) &&
         usb_transport_is_active() &&
         job.session_epoch != 0 &&
         job.session_epoch == usb_transport_session_epoch();
}

bool ensure_decoder(uint32_t generation) {
  if (s_decoder != nullptr && s_decoder_generation == generation) {
    return true;
  }
  if (s_decoder != nullptr) {
    opus_decoder_destroy(s_decoder);
    s_decoder = nullptr;
  }
  int error = OPUS_OK;
  s_decoder = opus_decoder_create(kSampleRate, 1, &error);
  if (error != OPUS_OK || s_decoder == nullptr) {
    log_error("[RTC_AUDIO] Opus decoder create failed err=%d", error);
    s_decoder_generation = 0;
    return false;
  }
  s_decoder_generation = generation;
  return true;
}

bool decode_job(const RtcAudioJob& job, int16_t** pcm_out,
                size_t* samples_out, uint32_t* free_caps_out) {
  if (pcm_out == nullptr || samples_out == nullptr ||
      free_caps_out == nullptr || job.payload == nullptr ||
      job.length == 0 || job.frames == 0 ||
      job.frames > kMaxBatchFrames ||
      !ensure_decoder(job.generation)) {
    return false;
  }

  const size_t capacity =
      static_cast<size_t>(job.frames) * kMaxOpusFrameSamples;
  uint32_t free_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
  int16_t* pcm = static_cast<int16_t*>(heap_caps_malloc(
      capacity * sizeof(int16_t), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (pcm == nullptr) {
    free_caps = MALLOC_CAP_DEFAULT;
    pcm = static_cast<int16_t*>(
        heap_caps_malloc(capacity * sizeof(int16_t), MALLOC_CAP_DEFAULT));
  }
  if (pcm == nullptr) {
    log_error("[RTC_AUDIO] PCM allocation failed samples=%u",
              static_cast<unsigned>(capacity));
    return false;
  }

  size_t written = 0;
  if (job.frames == 1) {
    const int decoded = opus_decode(
        s_decoder, job.payload, static_cast<opus_int32>(job.length), pcm,
        kMaxOpusFrameSamples, 0);
    if (decoded < 0) {
      log_warn("[RTC_AUDIO] Opus decode failed err=%s",
               opus_strerror(decoded));
      heap_caps_free(pcm);
      return false;
    }
    written = static_cast<size_t>(decoded);
  } else {
    size_t offset = 0;
    for (uint16_t index = 0; index < job.frames; ++index) {
      if (offset + 2u > job.length) {
        heap_caps_free(pcm);
        return false;
      }
      const uint16_t packet_length =
          static_cast<uint16_t>((job.payload[offset] << 8) |
                                job.payload[offset + 1]);
      offset += 2u;
      if (packet_length == 0 || offset + packet_length > job.length) {
        heap_caps_free(pcm);
        return false;
      }
      const int decoded = opus_decode(
          s_decoder, job.payload + offset,
          static_cast<opus_int32>(packet_length), pcm + written,
          kMaxOpusFrameSamples, 0);
      offset += packet_length;
      if (decoded < 0) {
        log_warn("[RTC_AUDIO] Opus batch decode failed frame=%u err=%s",
                 static_cast<unsigned>(index), opus_strerror(decoded));
        heap_caps_free(pcm);
        return false;
      }
      written += static_cast<size_t>(decoded);
    }
    if (offset != job.length) {
      log_warn("[RTC_AUDIO] Opus batch trailing bytes=%u",
               static_cast<unsigned>(job.length - offset));
    }
  }

  if (written == 0) {
    heap_caps_free(pcm);
    return false;
  }
  *pcm_out = pcm;
  *samples_out = written;
  *free_caps_out = free_caps;
  return true;
}

uint8_t pcm_mouth_level(const int16_t* pcm, size_t samples) {
  if (pcm == nullptr || samples == 0) {
    return 0;
  }
  uint64_t sum_abs = 0;
  for (size_t index = 0; index < samples; index += 4) {
    const int32_t sample = pcm[index];
    sum_abs += static_cast<uint32_t>(sample < 0 ? -sample : sample);
  }
  const uint32_t mean_abs =
      static_cast<uint32_t>(sum_abs / ((samples + 3u) / 4u));
  if (mean_abs < 180u) return 0;
  if (mean_abs < 500u) return 1;
  if (mean_abs < 1100u) return 2;
  if (mean_abs < 2300u) return 3;
  return 4;
}

void rtc_audio_task(void*) {
  RtcAudioJob job{};
  for (;;) {
    if (xQueueReceive(s_queue, &job, portMAX_DELAY) != pdTRUE) {
      continue;
    }
    if (!job_is_current(job)) {
      free_job(job);
      continue;
    }
    if (job.end_stream) {
      s_end_ready.store(true, std::memory_order_release);
      continue;
    }

    s_decode_busy.store(true, std::memory_order_release);
    int16_t* pcm = nullptr;
    size_t samples = 0;
    uint32_t free_caps = 0;
    const bool decoded =
        decode_job(job, &pcm, &samples, &free_caps);
    free_job(job);

    if (!decoded) {
      s_decode_busy.store(false, std::memory_order_release);
      continue;
    }
    if (!job_is_current(job)) {
      heap_caps_free(pcm);
      s_decode_busy.store(false, std::memory_order_release);
      continue;
    }
    display_voice_mouth_set_level(pcm_mouth_level(pcm, samples));
    if (!audio_stream_pcm16_push_owned(
            pcm, samples, free_caps, DESKBOT_AUDIO_PLAY_VOLUME)) {
      /* audio_stream_pcm16_push_owned() owns and frees pcm on failure. */
      log_warn("[RTC_AUDIO] playback queue rejected samples=%u",
               static_cast<unsigned>(samples));
    } else if (!s_first_pcm_logged.exchange(true, std::memory_order_acq_rel)) {
      log_warn("[RTC_AUDIO] first Opus batch decoded generation=%u samples=%u",
               static_cast<unsigned>(job.generation),
               static_cast<unsigned>(samples));
    }
    /*
     * END observes decode_busy from loopLite().  Clear it only after PCM has
     * entered audio_play; clearing before push allowed End to close the stream
     * and made the final chunk arrive at a nonexistent Begin.
     */
    s_decode_busy.store(false, std::memory_order_release);
  }
}

bool enqueue_job(RtcAudioJob& job, bool must_deliver) {
  if (xQueueSend(s_queue, &job, 0) == pdTRUE) {
    return true;
  }
  if (!must_deliver) {
    return false;
  }

  /*
   * END_STREAM must be ordered after all work still retained by the bounded
   * queue.  Under pathological overload, discard one oldest audio packet to
   * guarantee the terminal marker can enter without blocking the USB callback.
   */
  RtcAudioJob dropped{};
  if (xQueueReceive(s_queue, &dropped, 0) == pdTRUE) {
    free_job(dropped);
    s_drop_count.fetch_add(1, std::memory_order_relaxed);
  }
  return xQueueSend(s_queue, &job, 0) == pdTRUE;
}

void drain_queue() {
  if (s_queue == nullptr) {
    return;
  }
  RtcAudioJob job{};
  while (xQueueReceive(s_queue, &job, 0) == pdTRUE) {
    free_job(job);
  }
}

}  // namespace

bool rtc_audio_downlink_begin() {
  if (s_queue != nullptr && s_task != nullptr) {
    return true;
  }
  if (s_queue == nullptr) {
    s_queue = xQueueCreate(kQueueDepth, sizeof(RtcAudioJob));
  }
  if (s_queue == nullptr) {
    log_error("[RTC_AUDIO] queue allocation failed");
    return false;
  }
  if (s_task == nullptr) {
    TaskHandle_t created = nullptr;
    const BaseType_t result = xTaskCreatePinnedToCoreWithCaps(
        rtc_audio_task, "rtc_opus_dec",
        kDecodeTaskStackBytes / sizeof(StackType_t), nullptr, 4, &created,
        APP_CPU_NUM,
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (result != pdPASS || created == nullptr) {
      log_error("[RTC_AUDIO] decoder task create failed rc=%d",
                static_cast<int>(result));
      return false;
    }
    s_task = created;
    log_info("[RTC_AUDIO] async decoder ready core=%d priority=4 depth=%u stack=%uB",
             APP_CPU_NUM,
             static_cast<unsigned>(kQueueDepth),
             static_cast<unsigned>(kDecodeTaskStackBytes));
  }
  return true;
}

bool rtc_audio_downlink_ready() {
  return s_queue != nullptr && s_task != nullptr;
}

bool rtc_audio_downlink_begin_stream(uint32_t session_epoch) {
  if (session_epoch == 0 || !usb_transport_is_active() ||
      session_epoch != usb_transport_session_epoch() ||
      !rtc_audio_downlink_begin()) {
    return false;
  }
  uint32_t generation =
      s_generation.fetch_add(1, std::memory_order_acq_rel) + 1u;
  if (generation == 0) {
    generation = 1;
    s_generation.store(generation, std::memory_order_release);
  }
  s_active.store(true, std::memory_order_release);
  s_closing.store(false, std::memory_order_release);
  s_end_ready.store(false, std::memory_order_release);
  s_first_pcm_logged.store(false, std::memory_order_release);
  drain_queue();
  if (!audio_stream_pcm16_replace(DESKBOT_AUDIO_PLAY_VOLUME)) {
    s_active.store(false, std::memory_order_release);
    log_error("[RTC_AUDIO] generation=%u playback replace failed",
              static_cast<unsigned>(generation));
    return false;
  }
  log_warn("[RTC_AUDIO] BEGIN generation=%u epoch=%u decode_q=%u play_q=%u",
           static_cast<unsigned>(generation),
           static_cast<unsigned>(session_epoch),
           static_cast<unsigned>(uxQueueMessagesWaiting(s_queue)),
           audio_play_input_queue_depth());
  return true;
}

bool rtc_audio_downlink_enqueue(const uint8_t* payload, size_t length,
                                uint16_t frames, uint32_t session_epoch,
                                bool end_stream) {
  if (payload == nullptr || length == 0 || frames == 0 ||
      frames > kMaxBatchFrames || session_epoch == 0 ||
      !usb_transport_is_active() ||
      session_epoch != usb_transport_session_epoch() ||
      !s_active.load(std::memory_order_acquire) ||
      s_closing.load(std::memory_order_acquire) ||
      !rtc_audio_downlink_begin()) {
    return false;
  }

  uint8_t* copy = static_cast<uint8_t*>(
      heap_caps_malloc(length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (copy == nullptr) {
    copy =
        static_cast<uint8_t*>(heap_caps_malloc(length, MALLOC_CAP_DEFAULT));
  }
  if (copy == nullptr) {
    log_error("[RTC_AUDIO] packet allocation failed len=%u",
              static_cast<unsigned>(length));
    return false;
  }
  memcpy(copy, payload, length);

  RtcAudioJob job{};
  job.payload = copy;
  job.length = length;
  job.frames = frames;
  job.session_epoch = session_epoch;
  job.generation = s_generation.load(std::memory_order_acquire);
  if (!enqueue_job(job, false)) {
    free_job(job);
    const uint32_t drops =
        s_drop_count.fetch_add(1, std::memory_order_relaxed) + 1u;
    if (drops == 1u || drops % 25u == 0u) {
      log_warn("[RTC_AUDIO] decode queue full drops=%u",
               static_cast<unsigned>(drops));
    }
    if (end_stream) {
      (void)rtc_audio_downlink_enqueue_end(session_epoch);
    }
    return false;
  }
  s_active.store(true, std::memory_order_release);
  if (!end_stream) {
    return true;
  }
  return rtc_audio_downlink_enqueue_end(session_epoch);
}

bool rtc_audio_downlink_enqueue_end(uint32_t session_epoch) {
  if (session_epoch == 0 || !usb_transport_is_active() ||
      session_epoch != usb_transport_session_epoch() ||
      !s_active.load(std::memory_order_acquire) ||
      !rtc_audio_downlink_begin()) {
    return false;
  }
  bool expected = false;
  if (!s_closing.compare_exchange_strong(
          expected, true, std::memory_order_acq_rel,
          std::memory_order_acquire)) {
    return true;
  }
  s_active.store(true, std::memory_order_release);

  RtcAudioJob end{};
  end.end_stream = true;
  end.session_epoch = session_epoch;
  end.generation = s_generation.load(std::memory_order_acquire);
  if (enqueue_job(end, true)) {
    log_warn("[RTC_AUDIO] END generation=%u decode_q=%u play_q=%u",
             static_cast<unsigned>(end.generation),
             static_cast<unsigned>(uxQueueMessagesWaiting(s_queue)),
             audio_play_input_queue_depth());
    return true;
  }

  /*
   * A second concurrent drain is extremely unlikely, but closing remains
   * fail-safe: ready_to_close() still waits for the queue and decoder to idle.
   */
  s_end_ready.store(true, std::memory_order_release);
  log_warn("[RTC_AUDIO] END_STREAM queue fallback armed");
  return true;
}

void rtc_audio_downlink_cancel() {
  uint32_t next =
      s_generation.fetch_add(1, std::memory_order_acq_rel) + 1u;
  if (next == 0) {
    s_generation.store(1, std::memory_order_release);
  }
  s_active.store(false, std::memory_order_release);
  s_closing.store(false, std::memory_order_release);
  s_end_ready.store(false, std::memory_order_release);
  drain_queue();
  display_voice_mouth_stop();
}

bool rtc_audio_downlink_active() {
  return s_active.load(std::memory_order_acquire);
}

bool rtc_audio_downlink_closing() {
  return s_closing.load(std::memory_order_acquire);
}

uint32_t rtc_audio_downlink_generation() {
  return s_generation.load(std::memory_order_acquire);
}

size_t rtc_audio_downlink_queue_depth() {
  return s_queue ? static_cast<size_t>(uxQueueMessagesWaiting(s_queue)) : 0U;
}

bool rtc_audio_downlink_ready_to_close() {
  return s_active.load(std::memory_order_acquire) &&
         s_closing.load(std::memory_order_acquire) &&
         s_end_ready.load(std::memory_order_acquire) &&
         !s_decode_busy.load(std::memory_order_acquire) &&
         s_queue != nullptr && uxQueueMessagesWaiting(s_queue) == 0;
}

void rtc_audio_downlink_mark_closed() {
  display_voice_mouth_stop();
  rtc_audio_downlink_cancel();
}
