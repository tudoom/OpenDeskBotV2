#include "camera.h"

#include "asr_chat_client.h"
#include "audio_player.h"
#include "deskbot_config.h"
#include "deskbot_uplink_state.h"
#include "logger.h"
#include "usb_transport.h"

#include <Arduino.h>
#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <driver/gpio.h>
#include <driver/ledc.h>
#include <esp_camera.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>
#include <img_converters.h>

/* Camera pins selected by the board profile. */
#define PWDN_GPIO_NUM -1
#define RESET_GPIO_NUM -1
#if DESKBOT_BOARD_V2
#define XCLK_GPIO_NUM 14
#define SIOD_GPIO_NUM 9
#define SIOC_GPIO_NUM 10
#define Y9_GPIO_NUM 13
#define Y8_GPIO_NUM 21
#define Y7_GPIO_NUM 47
#define Y6_GPIO_NUM 38
#define Y5_GPIO_NUM 17
#define Y4_GPIO_NUM 8
#define Y3_GPIO_NUM 18
#define Y2_GPIO_NUM 39
#define VSYNC_GPIO_NUM 11
#define HREF_GPIO_NUM 12
#define PCLK_GPIO_NUM 48
#else
#define XCLK_GPIO_NUM 10
#define SIOD_GPIO_NUM 40
#define SIOC_GPIO_NUM 39
#define Y9_GPIO_NUM 48
#define Y8_GPIO_NUM 11
#define Y7_GPIO_NUM 12
#define Y6_GPIO_NUM 14
#define Y5_GPIO_NUM 16
#define Y4_GPIO_NUM 18
#define Y3_GPIO_NUM 17
#define Y2_GPIO_NUM 15
#define VSYNC_GPIO_NUM 38
#define HREF_GPIO_NUM 47
#define PCLK_GPIO_NUM 13
#endif

#ifndef DESKBOT_CAMERA_FB_COUNT
#define DESKBOT_CAMERA_FB_COUNT 1
#endif
#ifndef DESKBOT_CAMERA_GRAB_LATEST
#define DESKBOT_CAMERA_GRAB_LATEST 0
#endif
#ifndef DESKBOT_CAMERA_FORCE_DRAM
#define DESKBOT_CAMERA_FORCE_DRAM 0
#endif
#ifndef DESKBOT_CAMERA_PSRAM_DMA
#define DESKBOT_CAMERA_PSRAM_DMA 0
#endif
#ifndef DESKBOT_CAMERA_RGB565_JPEG
#define DESKBOT_CAMERA_RGB565_JPEG 0
#endif
#ifndef DESKBOT_CAMERA_XCLK_HZ
#define DESKBOT_CAMERA_XCLK_HZ 10000000
#endif
#ifndef DESKBOT_CAMERA_INIT_FRAME_SIZE
#define DESKBOT_CAMERA_INIT_FRAME_SIZE FRAMESIZE_QVGA
#endif
#ifndef DESKBOT_CAMERA_RUNTIME_FRAME_SIZE
#define DESKBOT_CAMERA_RUNTIME_FRAME_SIZE FRAMESIZE_QVGA
#endif
#ifndef DESKBOT_CAMERA_SOFTWARE_JPEG_QUALITY
#define DESKBOT_CAMERA_SOFTWARE_JPEG_QUALITY 80
#endif
/* -1 keeps the sensor driver's native orientation.  A board profile may set
 * either value to 0/1 when the physical module mounting requires correction. */
#ifndef DESKBOT_CAMERA_HMIRROR
#define DESKBOT_CAMERA_HMIRROR -1
#endif
#ifndef DESKBOT_CAMERA_VFLIP
#define DESKBOT_CAMERA_VFLIP -1
#endif
static_assert(DESKBOT_CAMERA_FB_COUNT == 1 ||
                  DESKBOT_CAMERA_FB_COUNT == 2,
              "Deskbot camera supports one or two framebuffers");
static_assert(DESKBOT_CAMERA_GRAB_LATEST == 0 ||
                  DESKBOT_CAMERA_GRAB_LATEST == 1,
              "Deskbot camera grab mode must be 0 or 1");
static_assert(DESKBOT_CAMERA_FORCE_DRAM == 0 ||
                  DESKBOT_CAMERA_FORCE_DRAM == 1,
              "Deskbot camera framebuffer location must be 0 or 1");
static_assert(DESKBOT_CAMERA_PSRAM_DMA == 0 ||
                  DESKBOT_CAMERA_PSRAM_DMA == 1,
              "Deskbot camera PSRAM DMA mode must be 0 or 1");
static_assert(DESKBOT_CAMERA_RGB565_JPEG == 0 ||
                  DESKBOT_CAMERA_RGB565_JPEG == 1,
              "Deskbot camera pixel mode must be 0 or 1");
static_assert(DESKBOT_CAMERA_HMIRROR >= -1 &&
                  DESKBOT_CAMERA_HMIRROR <= 1,
              "Deskbot camera hmirror must be -1, 0 or 1");
static_assert(DESKBOT_CAMERA_VFLIP >= -1 &&
                  DESKBOT_CAMERA_VFLIP <= 1,
              "Deskbot camera vflip must be -1, 0 or 1");
static_assert(!(DESKBOT_CAMERA_FORCE_DRAM &&
                DESKBOT_CAMERA_PSRAM_DMA),
              "Deskbot camera cannot force DRAM and PSRAM DMA together");

#define DESKBOT_ASSERT_CAMERA_USB_SAFE(pin_name)                           \
  static_assert(!deskbot_pin_conflicts_with_usb(static_cast<int>(pin_name)), \
                #pin_name " conflicts with fixed ESP32-S3 USB D-/D+")
DESKBOT_ASSERT_CAMERA_USB_SAFE(XCLK_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(SIOD_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(SIOC_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y9_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y8_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y7_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y6_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y5_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y4_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y3_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(Y2_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(VSYNC_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(HREF_GPIO_NUM);
DESKBOT_ASSERT_CAMERA_USB_SAFE(PCLK_GPIO_NUM);
#undef DESKBOT_ASSERT_CAMERA_USB_SAFE

namespace {

std::atomic<bool> s_camera_ok{false};
std::atomic<uint32_t> s_interval_ms{DESKBOT_CAMERA_UPLINK_INTERVAL_MS};
std::atomic<uint32_t> s_pending_snapshots{0};
std::atomic<uint32_t> s_last_frame_ms{0};
std::atomic<uint32_t> s_last_upload_ms{0};
std::atomic<uint32_t> s_capture_failures{0};
std::atomic<uint32_t> s_transport_failures{0};
std::atomic<uint32_t> s_init_failures{0};
std::atomic<uint32_t> s_recovery_attempts{0};
std::atomic<uint32_t> s_recovery_successes{0};
std::atomic<int32_t> s_last_init_error{ESP_OK};
TaskHandle_t s_camera_task = nullptr;

/*
 * Match the XIAO ESP32S3 Sense / OV2640 reference camera clock profile.
 *
 * camera_config_t retains the legacy LEDC selector fields, but the ESP32-S3
 * esp32-camera driver generates XCLK with LCD_CAM. Do not query the LEDC
 * driver to validate XCLK: no LEDC object exists on this target, and the
 * resulting ESP_ERR_INVALID_STATE value can be mistaken for a frequency.
 * A structurally valid JPEG is the portable end-to-end health signal.
 */
constexpr ledc_channel_t kCameraXclkChannel = LEDC_CHANNEL_0;
constexpr ledc_timer_t kCameraXclkTimer = LEDC_TIMER_0;
constexpr uint32_t kCameraXclkHz = DESKBOT_CAMERA_XCLK_HZ;
constexpr pixformat_t kCameraPixelFormat =
    DESKBOT_CAMERA_RGB565_JPEG ? PIXFORMAT_RGB565 : PIXFORMAT_JPEG;
constexpr framesize_t kCameraInitFrameSize =
    DESKBOT_CAMERA_INIT_FRAME_SIZE;
constexpr framesize_t kCameraRuntimeFrameSize =
    DESKBOT_CAMERA_RUNTIME_FRAME_SIZE;

constexpr uint32_t kCameraFailureRecoveryThreshold = 3u;
constexpr uint32_t kCameraRecoveryMinIntervalMs = 10000u;
constexpr uint32_t kCameraRecoveryBackoffBaseMs = 250u;
constexpr uint32_t kCameraRecoveryBackoffMaxMs = 2000u;
constexpr uint32_t kCameraInitialRetryBaseMs = 1000u;
constexpr uint32_t kCameraInitialRetryMaxMs = 30000u;
constexpr uint32_t kCameraNoMemoryRetryMs = 300000u;
constexpr uint32_t kCameraWarmupFrames = 2u;
/*
 * Camera work shares PRO_CPU with the ESP-SR feed task (priority 5).  Priority
 * 2 cannot pre-empt audio, but prevents the on-demand capture supervisor from
 * being starved by the Arduino loop/idle work at priority 1 while speech is
 * continuously uploaded.
 */
constexpr UBaseType_t kCameraTaskPriority = 2u;
/*
 * CAMERA_GRAB_WHEN_EMPTY with a single framebuffer needs a short producer
 * window after an old queued frame is returned.  Calling fb_get() again in
 * the same timeslice can leave the LCD_CAM driver waiting until its ~4 s
 * timeout.  That exactly matches (and races) the Core vision-tool timeout.
 * Split one-shot capture into two supervisor iterations and let the sensor
 * refill the sole buffer before requesting the fresh frame.
 */
constexpr uint32_t kCameraSnapshotRefillDelayMs = 100u;
#ifndef DESKBOT_CAMERA_MAX_FPS
#define DESKBOT_CAMERA_MAX_FPS 30
#endif
static_assert(DESKBOT_CAMERA_MAX_FPS > 0 && DESKBOT_CAMERA_MAX_FPS <= 1000,
              "Deskbot camera FPS limit must keep a non-zero interval");

void log_camera_heap(const char* stage) {
  const uint32_t internal_caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT;
  const uint32_t dma_caps = MALLOC_CAP_INTERNAL | MALLOC_CAP_DMA;
  const uint32_t psram_caps = MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
  log_warn(
      "[CAMERA_HEAP] stage=%s int_free=%u int_largest=%u "
      "dma_free=%u dma_largest=%u psram_free=%u psram_largest=%u",
      stage == nullptr ? "unknown" : stage,
      static_cast<unsigned>(heap_caps_get_free_size(internal_caps)),
      static_cast<unsigned>(heap_caps_get_largest_free_block(internal_caps)),
      static_cast<unsigned>(heap_caps_get_free_size(dma_caps)),
      static_cast<unsigned>(heap_caps_get_largest_free_block(dma_caps)),
      static_cast<unsigned>(heap_caps_get_free_size(psram_caps)),
      static_cast<unsigned>(heap_caps_get_largest_free_block(psram_caps)));
}

void set_camera_driver_log_level(esp_log_level_t level) {
  esp_log_level_set("camera", level);
  esp_log_level_set("cam_hal", level);
  esp_log_level_set("sccb", level);
}

esp_err_t initialise_camera_driver(camera_config_t* config) {
  if (config == nullptr) {
    return ESP_ERR_INVALID_ARG;
  }

  /* Framework ESP_LOG output is raw USB data, so it is safe only before the
   * framed DBOT session starts.  Temporarily expose the first boot failure's
   * exact camera/cam_hal/SCCB error, then restore the framing-safe policy. */
  const bool raw_driver_logs_safe = !usb_transport_framed_mode();
  if (raw_driver_logs_safe) {
    set_camera_driver_log_level(ESP_LOG_ERROR);
  }
  const esp_err_t err = esp_camera_init(config);
  if (raw_driver_logs_safe) {
    set_camera_driver_log_level(ESP_LOG_NONE);
  }
  return err;
}

enum class CameraProbeStatus : uint8_t {
  kNotRun = 0,
  kFrame = 1,
  kNoFrame = 2,
  kInvalidFrame = 3,
};

struct CameraProbeRecord {
  CameraProbeStatus status = CameraProbeStatus::kNotRun;
  uint32_t elapsed_ms = 0;
  uint32_t samples = 0;
  uint32_t vsync_edges = 0;
  uint32_t href_edges = 0;
  uint32_t pclk_edges = 0;
  size_t frame_len = 0;
  uint16_t width = 0;
  uint16_t height = 0;
  int format = -1;
};

CameraProbeRecord s_boot_probe[2];
bool s_boot_probe_reported = false;
bool s_runtime_probe_finished = false;

size_t camera_probe_index(CameraBootProbeStage stage) {
  return stage == CameraBootProbeStage::kAfterServo ? 1u : 0u;
}

const char* camera_probe_stage_name(CameraBootProbeStage stage) {
  return stage == CameraBootProbeStage::kAfterServo ? "after_servo"
                                                     : "before_servo";
}

const char* camera_probe_status_name(CameraProbeStatus status) {
  switch (status) {
    case CameraProbeStatus::kNotRun:
      return "not_run";
    case CameraProbeStatus::kFrame:
      return "valid_frame";
    case CameraProbeStatus::kNoFrame:
      return "no_frame";
    case CameraProbeStatus::kInvalidFrame:
      return "invalid_frame";
  }
  return "unknown";
}

bool camera_jpeg_valid(const camera_fb_t* fb) {
  return fb != nullptr && fb->format == PIXFORMAT_JPEG && fb->buf != nullptr &&
         fb->len >= 4 && fb->buf[0] == 0xff && fb->buf[1] == 0xd8 &&
         fb->buf[fb->len - 2] == 0xff && fb->buf[fb->len - 1] == 0xd9;
}

bool camera_source_frame_valid(const camera_fb_t* fb) {
  if (fb == nullptr || fb->buf == nullptr || fb->width == 0 ||
      fb->height == 0) {
    return false;
  }
  if (!DESKBOT_CAMERA_RGB565_JPEG) {
    return camera_jpeg_valid(fb);
  }
  const size_t expected =
      static_cast<size_t>(fb->width) * static_cast<size_t>(fb->height) * 2u;
  return fb->format == PIXFORMAT_RGB565 && fb->len == expected;
}

bool discard_one_camera_frame(const char* reason) {
  camera_fb_t* fb = esp_camera_fb_get();
  if (fb == nullptr) {
    s_capture_failures.fetch_add(1, std::memory_order_relaxed);
    log_warn("[CAMERA] discard failed reason=%s no_frame",
             reason ? reason : "unspecified");
    return false;
  }
  const bool valid = camera_source_frame_valid(fb);
  if (valid) {
    s_last_frame_ms.store(millis(), std::memory_order_release);
  } else {
    s_capture_failures.fetch_add(1, std::memory_order_relaxed);
    log_warn(
        "[CAMERA] discard failed reason=%s invalid len=%u size=%ux%u fmt=%d",
        reason ? reason : "unspecified", static_cast<unsigned>(fb->len),
        static_cast<unsigned>(fb->width), static_cast<unsigned>(fb->height),
        static_cast<int>(fb->format));
  }
  esp_camera_fb_return(fb);
  return valid;
}

bool discard_camera_warmup_frames() {
  for (uint32_t i = 0; i < kCameraWarmupFrames; ++i) {
    if (!discard_one_camera_frame("warmup")) {
      return false;
    }
  }
  return true;
}

bool apply_sensor_int_setting(sensor_t* sensor, const char* name,
                              int (*setter)(sensor_t*, int), int value,
                              bool required) {
  const int rc = setter == nullptr ? -1 : setter(sensor, value);
  if (rc == 0) {
    return true;
  }
  if (required) {
    log_error("[CAMERA] required sensor setting failed name=%s value=%d rc=%d",
              name, value, rc);
  } else {
    log_warn("[CAMERA] optional sensor setting failed name=%s value=%d rc=%d",
             name, value, rc);
  }
  return !required;
}

void sample_camera_sync_activity(CameraProbeRecord* record) {
  if (record == nullptr) {
    return;
  }
  int previous_vsync = gpio_get_level(static_cast<gpio_num_t>(VSYNC_GPIO_NUM));
  int previous_href = gpio_get_level(static_cast<gpio_num_t>(HREF_GPIO_NUM));
  int previous_pclk = gpio_get_level(static_cast<gpio_num_t>(PCLK_GPIO_NUM));
  const uint32_t started_ms = millis();
  while (millis() - started_ms < 120u) {
    const int vsync =
        gpio_get_level(static_cast<gpio_num_t>(VSYNC_GPIO_NUM));
    const int href = gpio_get_level(static_cast<gpio_num_t>(HREF_GPIO_NUM));
    const int pclk = gpio_get_level(static_cast<gpio_num_t>(PCLK_GPIO_NUM));
    if (vsync != previous_vsync) {
      ++record->vsync_edges;
      previous_vsync = vsync;
    }
    if (href != previous_href) {
      ++record->href_edges;
      previous_href = href;
    }
    if (pclk != previous_pclk) {
      ++record->pclk_edges;
      previous_pclk = pclk;
    }
    ++record->samples;
    if ((record->samples & 0x3ffu) == 0u) {
      taskYIELD();
    }
  }
}

void log_camera_probe(CameraBootProbeStage stage,
                      const CameraProbeRecord& record,
                      const char* prefix) {
  log_warn("[CAMERA_DIAG] %s stage=%s status=%s fb_ms=%u len=%u "
           "size=%ux%u fmt=%d configured_xclk=%u "
           "edges(v/h/p)=%u/%u/%u samples=%u",
           prefix, camera_probe_stage_name(stage),
           camera_probe_status_name(record.status),
           static_cast<unsigned>(record.elapsed_ms),
           static_cast<unsigned>(record.frame_len),
           static_cast<unsigned>(record.width),
           static_cast<unsigned>(record.height), record.format,
           static_cast<unsigned>(kCameraXclkHz),
           static_cast<unsigned>(record.vsync_edges),
           static_cast<unsigned>(record.href_edges),
           static_cast<unsigned>(record.pclk_edges),
           static_cast<unsigned>(record.samples));
}

void report_boot_camera_probes_once() {
  if (s_boot_probe_reported) {
    return;
  }
  s_boot_probe_reported = true;
  log_camera_probe(CameraBootProbeStage::kBeforeServo, s_boot_probe[0],
                   "replay");
  log_camera_probe(CameraBootProbeStage::kAfterServo, s_boot_probe[1],
                   "replay");

  const CameraProbeStatus before = s_boot_probe[0].status;
  const CameraProbeStatus after = s_boot_probe[1].status;
  const char* classification = "failure_before_servo";
  if (before == CameraProbeStatus::kFrame) {
    classification =
        after == CameraProbeStatus::kFrame ? "capture_ok_before_and_after_servo"
                                           : "failure_after_servo";
  }
  log_warn("[CAMERA_DIAG] classification=%s", classification);
}

enum class CameraUploadResult : uint8_t {
  kSent = 0,
  kNoFrame = 1,
  kInvalidFrame = 2,
  kTransportBusy = 3,
};

const char* camera_upload_result_name(CameraUploadResult result) {
  switch (result) {
    case CameraUploadResult::kSent:
      return "sent";
    case CameraUploadResult::kNoFrame:
      return "no_frame";
    case CameraUploadResult::kInvalidFrame:
      return "invalid_frame";
    case CameraUploadResult::kTransportBusy:
      return "transport_busy";
  }
  return "unknown";
}

bool capture_paused() {
  /*
   * Stable/half-duplex mode keeps the legacy protection.  With the official
   * ESP-SR full-duplex pipeline active, an on-demand snapshot may proceed
   * during playback; USB audio still wins the camera's bounded 25 ms
   * transport lock wait (kCameraTxMutexWaitMs).
   */
  return !deskbot_uplink_rtc_interruptible() &&
         (deskbot_uplink_speaker_audible() || audio_play_speaker_busy());
}

uint32_t upload_interval_ms() {
  uint32_t ms = s_interval_ms.load(std::memory_order_acquire);
  if (ms == 0) {
    return 0;
  }
  if (asr_chat_voice_uplink_busy()) {
    const uint32_t listen_ms =
        DESKBOT_CAMERA_UPLINK_INTERVAL_DURING_LISTEN_MS;
    if (listen_ms > ms) {
      ms = listen_ms;
    }
  }
  return ms;
}

CameraUploadResult try_upload_one_frame(uint32_t expected_epoch) {
#if DESKBOT_CAMERA_DIAGNOSTICS
  const bool runtime_probe = !s_runtime_probe_finished;
  const uint32_t started_ms = millis();
  if (runtime_probe) {
    log_warn("[CAMERA_DIAG] runtime fb_get begin configured_xclk=%u",
             static_cast<unsigned>(kCameraXclkHz));
  }
#endif
  camera_fb_t* fb = esp_camera_fb_get();
#if DESKBOT_CAMERA_DIAGNOSTICS
  const uint32_t elapsed_ms = millis() - started_ms;
  if (runtime_probe) {
    s_runtime_probe_finished = true;
    log_warn("[CAMERA_DIAG] runtime fb_get end status=%s elapsed=%ums "
             "len=%u fmt=%d",
             fb == nullptr
                 ? "no_frame"
                 : (camera_source_frame_valid(fb) ? "valid_frame"
                                                  : "invalid_frame"),
             static_cast<unsigned>(elapsed_ms),
             static_cast<unsigned>(fb == nullptr ? 0u : fb->len),
             fb == nullptr ? -1 : static_cast<int>(fb->format));
  }
#endif
  if (fb == nullptr) {
    s_capture_failures.fetch_add(1, std::memory_order_relaxed);
    return CameraUploadResult::kNoFrame;
  }
  if (!camera_source_frame_valid(fb)) {
    s_capture_failures.fetch_add(1, std::memory_order_relaxed);
    esp_camera_fb_return(fb);
    return CameraUploadResult::kInvalidFrame;
  }
  s_last_frame_ms.store(millis(), std::memory_order_release);
  const uint8_t* jpeg_payload = fb->buf;
  size_t jpeg_len = fb->len;
  uint8_t* converted_jpeg = nullptr;
  if (DESKBOT_CAMERA_RGB565_JPEG) {
    const uint32_t encode_started_ms = millis();
    if (!frame2jpg(fb, DESKBOT_CAMERA_SOFTWARE_JPEG_QUALITY,
                   &converted_jpeg, &jpeg_len) ||
        converted_jpeg == nullptr) {
      esp_camera_fb_return(fb);
      log_warn("[CAMERA] RGB565 software JPEG encode failed");
      s_capture_failures.fetch_add(1, std::memory_order_relaxed);
      return CameraUploadResult::kInvalidFrame;
    }
    jpeg_payload = converted_jpeg;
    static bool conversion_profile_reported = false;
    if (!conversion_profile_reported) {
      conversion_profile_reported = true;
      log_warn("[CAMERA_DIAG] RGB565 software JPEG len=%u encode_ms=%u",
               static_cast<unsigned>(jpeg_len),
               static_cast<unsigned>(millis() - encode_started_ms));
    }
  }
  const bool jpeg_valid =
      jpeg_payload != nullptr && jpeg_len >= 4 &&
      jpeg_len <= DESKBOT_USB_MAX_PAYLOAD && jpeg_payload[0] == 0xff &&
      jpeg_payload[1] == 0xd8 && jpeg_payload[jpeg_len - 2] == 0xff &&
      jpeg_payload[jpeg_len - 1] == 0xd9;
  if (!jpeg_valid) {
    s_capture_failures.fetch_add(1, std::memory_order_relaxed);
    free(converted_jpeg);
    esp_camera_fb_return(fb);
    return CameraUploadResult::kInvalidFrame;
  }
  /*
   * CAMERA_JPEG takes the shared TX mutex with a bounded wait.  Voice and PB
   * remain higher-priority tasks, while the bounded wait prevents a requested
   * snapshot from being starved by near-continuous Opus packets.
   */
  const bool sent = usb_transport_send_for_epoch(
      DESKBOT_USB_CAMERA_JPEG, jpeg_payload, jpeg_len, expected_epoch);
  free(converted_jpeg);
  esp_camera_fb_return(fb);
  if (sent) {
    s_last_upload_ms.store(millis(), std::memory_order_release);
  } else {
    s_transport_failures.fetch_add(1, std::memory_order_relaxed);
  }
  return sent ? CameraUploadResult::kSent
              : CameraUploadResult::kTransportBusy;
}

void camera_uplink_task(void*) {
  unsigned long last_upload_ms = 0;
  unsigned long last_retry_log_ms = 0;
  unsigned long last_recovery_attempt_ms = 0;
  unsigned long last_recovery_rate_limit_log_ms = 0;
  unsigned long next_setup_attempt_ms =
      s_camera_ok.load(std::memory_order_acquire)
          ? 0
          : millis() + kCameraInitialRetryBaseMs;
  uint32_t consecutive_camera_failures = 0;
  uint32_t recovery_attempts_without_valid_frame = 0;
  uint32_t setup_retry_failures = 0;
  bool snapshot_warmed = false;
  for (;;) {
    if (!s_camera_ok.load(std::memory_order_acquire)) {
      snapshot_warmed = false;
      const unsigned long now = millis();
      if (next_setup_attempt_ms != 0 &&
          static_cast<int32_t>(now - next_setup_attempt_ms) < 0) {
        vTaskDelay(pdMS_TO_TICKS(100));
        continue;
      }

      const uint32_t recovery_attempt =
          s_recovery_attempts.fetch_add(1, std::memory_order_acq_rel) + 1;
      log_warn("[CAMERA] supervisor setup attempt=%u",
               static_cast<unsigned>(recovery_attempt));
      if (setup_camera()) {
        s_recovery_successes.fetch_add(1, std::memory_order_relaxed);
        consecutive_camera_failures = 0;
        recovery_attempts_without_valid_frame = 0;
        setup_retry_failures = 0;
        next_setup_attempt_ms = 0;
        log_warn("[CAMERA] supervisor recovery complete attempt=%u",
                 static_cast<unsigned>(recovery_attempt));
      } else {
        const int32_t setup_error =
            s_last_init_error.load(std::memory_order_acquire);
        const bool no_memory = setup_error == ESP_ERR_NO_MEM;
        const uint32_t shift = min(setup_retry_failures, uint32_t{5});
        const uint32_t retry_ms =
            no_memory
                ? kCameraNoMemoryRetryMs
                : min(kCameraInitialRetryBaseMs * (1u << shift),
                      kCameraInitialRetryMaxMs);
        ++setup_retry_failures;
        next_setup_attempt_ms = millis() + retry_ms;
        log_error(
            "[CAMERA] supervisor setup failed attempt=%u error=0x%x "
            "retry_in=%ums%s",
            static_cast<unsigned>(recovery_attempt),
            static_cast<unsigned>(setup_error),
            static_cast<unsigned>(retry_ms),
            no_memory ? " (NO_MEM rate-limited)" : "");
      }
      continue;
    }

    if (!usb_transport_is_active()) {
      (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(25));
      continue;
    }
#if DESKBOT_CAMERA_DIAGNOSTICS
    report_boot_camera_probes_once();
#endif
    if (capture_paused()) {
      vTaskDelay(pdMS_TO_TICKS(25));
      continue;
    }
    const bool snapshot_requested =
        s_pending_snapshots.load(std::memory_order_acquire) > 0;
    if (!snapshot_requested) {
      snapshot_warmed = false;
    }
    const uint32_t interval = upload_interval_ms();
    if (!snapshot_requested && interval == 0) {
      (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(25));
      continue;
    }
    const unsigned long now = millis();
    if (!snapshot_requested && last_upload_ms != 0 &&
        (now - last_upload_ms) < interval) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }
    const uint32_t capture_epoch = usb_transport_session_epoch();
    CameraUploadResult upload_result = CameraUploadResult::kNoFrame;
    if (snapshot_requested && !snapshot_warmed &&
        !DESKBOT_CAMERA_GRAB_LATEST) {
      /*
       * Do not discard and recapture in one call.  If the fresh capture times
       * out, the old implementation repeated the discard forever and could
       * never satisfy camera_once.  Once warm, every retry below attempts to
       * upload a frame until it succeeds or normal driver recovery runs.
       * CAMERA_GRAB_LATEST continuously replaces queued frames, so its first
       * available framebuffer is already current and must not be discarded.
       */
      if (discard_one_camera_frame("one_shot_stale")) {
        snapshot_warmed = true;
        consecutive_camera_failures = 0;
        recovery_attempts_without_valid_frame = 0;
        vTaskDelay(pdMS_TO_TICKS(kCameraSnapshotRefillDelayMs));
        continue;
      }
    } else {
      upload_result = try_upload_one_frame(capture_epoch);
    }
    const bool camera_capture_failed =
        upload_result == CameraUploadResult::kNoFrame ||
        upload_result == CameraUploadResult::kInvalidFrame;
    if (camera_capture_failed) {
      ++consecutive_camera_failures;
    } else {
      /*
       * A transport-busy result still proves that the camera produced a
       * structurally valid JPEG. Only actual capture failures count toward
       * driver recovery.
       */
      consecutive_camera_failures = 0;
      recovery_attempts_without_valid_frame = 0;
    }

    if (consecutive_camera_failures >=
        kCameraFailureRecoveryThreshold) {
      const unsigned long recovery_now = millis();
      const unsigned long since_last_recovery =
          recovery_now - last_recovery_attempt_ms;
      if (last_recovery_attempt_ms != 0 &&
          since_last_recovery < kCameraRecoveryMinIntervalMs) {
        if (last_recovery_rate_limit_log_ms == 0 ||
            recovery_now - last_recovery_rate_limit_log_ms >= 5000u) {
          last_recovery_rate_limit_log_ms = recovery_now;
          log_warn(
              "[CAMERA] recovery rate-limited failures=%u retry_in=%ums",
              static_cast<unsigned>(consecutive_camera_failures),
              static_cast<unsigned>(
                  kCameraRecoveryMinIntervalMs - since_last_recovery));
        }
        vTaskDelay(pdMS_TO_TICKS(250));
        continue;
      }

      last_recovery_attempt_ms = recovery_now;
      const uint32_t backoff_multiplier =
          recovery_attempts_without_valid_frame >= 3u
              ? 8u
              : (1u << recovery_attempts_without_valid_frame);
      const uint32_t backoff_ms =
          min(kCameraRecoveryBackoffBaseMs * backoff_multiplier,
              kCameraRecoveryBackoffMaxMs);
      ++recovery_attempts_without_valid_frame;

      s_camera_ok.store(false, std::memory_order_release);
      snapshot_warmed = false;
      log_warn(
          "[CAMERA] resetting failed driver failures=%u backoff=%ums",
          static_cast<unsigned>(consecutive_camera_failures),
          static_cast<unsigned>(backoff_ms));
      const esp_err_t deinit_err = esp_camera_deinit();
      if (deinit_err != ESP_OK) {
        log_warn("[CAMERA] deinit during recovery failed 0x%x",
                 deinit_err);
      }
      setup_retry_failures = recovery_attempts_without_valid_frame - 1u;
      next_setup_attempt_ms = millis() + backoff_ms;
      continue;
    }

    if (upload_result != CameraUploadResult::kSent) {
      /*
       * Camera waits at most kCameraTxMutexWaitMs (25 ms) for the TX lock so
       * microphone/PB effectively always win.  Do not
       * treat lock contention as a successful cadence slot: with continuous
       * Opus traffic the two periodic tasks can otherwise stay phase-locked
       * and suppress JPEG uplink forever.  A short retry de-phases the
       * best-effort camera without delaying the priority writer.
       */
      const unsigned long retry_now = millis();
      if (last_retry_log_ms == 0 ||
          retry_now - last_retry_log_ms >= 5000u) {
        last_retry_log_ms = retry_now;
        log_warn("[CAMERA] upload retry reason=%s",
                 camera_upload_result_name(upload_result));
      }
      vTaskDelay(pdMS_TO_TICKS(25));
      continue;
    }
    last_upload_ms = millis();
    if (snapshot_requested) {
      uint32_t pending =
          s_pending_snapshots.load(std::memory_order_acquire);
      while (pending > 0 &&
             !s_pending_snapshots.compare_exchange_weak(
                 pending, pending - 1, std::memory_order_acq_rel,
                 std::memory_order_acquire)) {
      }
      snapshot_warmed = false;
    }
    taskYIELD();
  }
}

}  // namespace

bool setup_camera() {
  if (s_camera_ok.load(std::memory_order_acquire)) {
    return true;
  }
  camera_config_t config{};
  config.ledc_channel = kCameraXclkChannel;
  config.ledc_timer = kCameraXclkTimer;
  config.pin_d0 = Y2_GPIO_NUM;
  config.pin_d1 = Y3_GPIO_NUM;
  config.pin_d2 = Y4_GPIO_NUM;
  config.pin_d3 = Y5_GPIO_NUM;
  config.pin_d4 = Y6_GPIO_NUM;
  config.pin_d5 = Y7_GPIO_NUM;
  config.pin_d6 = Y8_GPIO_NUM;
  config.pin_d7 = Y9_GPIO_NUM;
  config.pin_xclk = XCLK_GPIO_NUM;
  config.pin_pclk = PCLK_GPIO_NUM;
  config.pin_vsync = VSYNC_GPIO_NUM;
  config.pin_href = HREF_GPIO_NUM;
  config.pin_sccb_sda = SIOD_GPIO_NUM;
  config.pin_sccb_scl = SIOC_GPIO_NUM;
  config.pin_pwdn = PWDN_GPIO_NUM;
  config.pin_reset = RESET_GPIO_NUM;
  config.xclk_freq_hz = kCameraXclkHz;
  config.pixel_format = kCameraPixelFormat;

  const bool psram_available = psramFound();
  /*
   * Production and diagnostic profiles share one implementation. Build-time
   * flags select the allocation/capture mode so an A/B firmware cannot silently
   * claim one profile in logs while running another.
   */
  config.frame_size = kCameraInitFrameSize;
  config.grab_mode = DESKBOT_CAMERA_GRAB_LATEST
                         ? CAMERA_GRAB_LATEST
                         : CAMERA_GRAB_WHEN_EMPTY;
  config.fb_location =
      DESKBOT_CAMERA_FORCE_DRAM
          ? CAMERA_FB_IN_DRAM
          : (psram_available ? CAMERA_FB_IN_PSRAM : CAMERA_FB_IN_DRAM);
  config.jpeg_quality = 18;
  config.fb_count = DESKBOT_CAMERA_FB_COUNT;

  log_info("[CAMERA] config profile=%s pixel=%s framesize=%d "
           "runtime_framesize=%d jpeg_quality=%d fb_count=%u fb=%s "
           "grab=%s psram=%d psram_dma_requested=%d size=%u free=%u",
           DESKBOT_CAMERA_DIAGNOSTICS ? "diagnostic" : "production",
           DESKBOT_CAMERA_RGB565_JPEG ? "rgb565+software_jpeg"
                                      : "sensor_jpeg",
           static_cast<int>(config.frame_size),
           static_cast<int>(kCameraRuntimeFrameSize),
           config.jpeg_quality,
           static_cast<unsigned>(config.fb_count),
           config.fb_location == CAMERA_FB_IN_DRAM ? "dram" : "psram",
           config.grab_mode == CAMERA_GRAB_WHEN_EMPTY ? "when_empty"
                                                      : "latest",
           psram_available ? 1 : 0,
           DESKBOT_CAMERA_PSRAM_DMA ? 1 : 0,
           static_cast<unsigned>(ESP.getPsramSize()),
           static_cast<unsigned>(ESP.getFreePsram()));
  log_info("[CAMERA] pins xclk=%d legacy_clock_selector=%d/%d "
           "sda=%d scl=%d pclk=%d "
           "vsync=%d href=%d data=%d,%d,%d,%d,%d,%d,%d,%d",
           XCLK_GPIO_NUM,
           static_cast<int>(kCameraXclkChannel),
           static_cast<int>(kCameraXclkTimer), SIOD_GPIO_NUM, SIOC_GPIO_NUM,
           PCLK_GPIO_NUM, VSYNC_GPIO_NUM, HREF_GPIO_NUM, Y2_GPIO_NUM,
           Y3_GPIO_NUM, Y4_GPIO_NUM, Y5_GPIO_NUM, Y6_GPIO_NUM, Y7_GPIO_NUM,
           Y8_GPIO_NUM, Y9_GPIO_NUM);

  const bool psram_dma_requested =
      DESKBOT_CAMERA_PSRAM_DMA && psram_available &&
      config.fb_location == CAMERA_FB_IN_PSRAM;
  log_camera_heap("before_init");
  esp_err_t err = initialise_camera_driver(&config);
  if (err == ESP_OK && psram_dma_requested &&
      !esp_camera_get_psram_mode()) {
    /* esp_camera_set_psram_mode() is explicitly a runtime reconfiguration
     * API.  The driver returns ESP_ERR_INVALID_STATE when it is called before
     * esp_camera_init(), so initialise the conservative legacy path first and
     * only then move the capture DMA buffers to PSRAM. */
    const bool raw_driver_logs_safe = !usb_transport_framed_mode();
    if (raw_driver_logs_safe) {
      set_camera_driver_log_level(ESP_LOG_ERROR);
    }
    const esp_err_t dma_err = esp_camera_set_psram_mode(true);
    if (raw_driver_logs_safe) {
      set_camera_driver_log_level(ESP_LOG_NONE);
    }
    if (dma_err != ESP_OK) {
      /* Runtime reconfiguration may leave the driver deinitialised.  Rebuild
       * the known-compatible legacy mode so a PSRAM-DMA capability mismatch
       * never disables the camera completely. */
      log_warn(
          "[CAMERA] PSRAM DMA reconfigure failed 0x%x; "
          "reinitialising legacy DMA mode",
          static_cast<unsigned>(dma_err));
      (void)esp_camera_deinit();
      err = initialise_camera_driver(&config);
      if (err == ESP_OK) {
        log_warn("[CAMERA] legacy DMA compatibility fallback active");
      }
    }
  }
  if (err != ESP_OK) {
    s_last_init_error.store(err, std::memory_order_release);
    log_camera_heap("init_failed");
    log_error("[CAMERA] setup failed 0x%x", static_cast<unsigned>(err));
    s_init_failures.fetch_add(1, std::memory_order_relaxed);
    s_camera_ok.store(false, std::memory_order_release);
    return false;
  }

  sensor_t* sensor = esp_camera_sensor_get();
  if (sensor == nullptr) {
    log_error("[CAMERA] sensor unavailable after init");
    esp_camera_deinit();
    s_last_init_error.store(ESP_FAIL, std::memory_order_release);
    s_init_failures.fetch_add(1, std::memory_order_relaxed);
    s_camera_ok.store(false, std::memory_order_release);
    return false;
  }
  if (sensor->id.PID == OV3660_PID) {
    (void)apply_sensor_int_setting(
        sensor, "vflip", sensor->set_vflip, 1, false);
    (void)apply_sensor_int_setting(
        sensor, "brightness", sensor->set_brightness, 1, false);
    (void)apply_sensor_int_setting(
        sensor, "saturation", sensor->set_saturation, -2, false);
  }
  bool required_settings_ok = true;
#if DESKBOT_CAMERA_HMIRROR >= 0
  required_settings_ok =
      apply_sensor_int_setting(sensor, "hmirror", sensor->set_hmirror,
                               DESKBOT_CAMERA_HMIRROR, true) &&
      required_settings_ok;
#endif
#if DESKBOT_CAMERA_VFLIP >= 0
  required_settings_ok =
      apply_sensor_int_setting(sensor, "vflip", sensor->set_vflip,
                               DESKBOT_CAMERA_VFLIP, true) &&
      required_settings_ok;
#endif
  /*
   * Do not rely on sensor power-on register state.  Warm resets previously
   * left AWB/color gain in an undefined mode, producing the blue/green cast
   * seen in the preview and reducing image consistency.
   */
  (void)apply_sensor_int_setting(
      sensor, "whitebal", sensor->set_whitebal, 1, false);
  (void)apply_sensor_int_setting(
      sensor, "awb_gain", sensor->set_awb_gain, 1, false);
  (void)apply_sensor_int_setting(
      sensor, "wb_mode", sensor->set_wb_mode, 0, false);
  (void)apply_sensor_int_setting(
      sensor, "exposure_ctrl", sensor->set_exposure_ctrl, 1, false);
  (void)apply_sensor_int_setting(
      sensor, "aec2", sensor->set_aec2, 1, false);
  (void)apply_sensor_int_setting(
      sensor, "gain_ctrl", sensor->set_gain_ctrl, 1, false);
  (void)apply_sensor_int_setting(
      sensor, "raw_gma", sensor->set_raw_gma, 1, false);
  (void)apply_sensor_int_setting(
      sensor, "lenc", sensor->set_lenc, 1, false);
  const int framesize_rc =
      sensor->set_framesize == nullptr
          ? -1
          : sensor->set_framesize(sensor, kCameraRuntimeFrameSize);
  if (framesize_rc != 0) {
    log_error(
        "[CAMERA] required sensor setting failed name=framesize value=%d rc=%d",
        static_cast<int>(kCameraRuntimeFrameSize), framesize_rc);
    required_settings_ok = false;
  }
  if (!DESKBOT_CAMERA_RGB565_JPEG) {
    required_settings_ok =
        apply_sensor_int_setting(
            sensor, "quality", sensor->set_quality, 18, true) &&
        required_settings_ok;
  }
  if (!required_settings_ok || !discard_camera_warmup_frames()) {
    log_error("[CAMERA] setup validation failed; deinitialising driver");
    esp_camera_deinit();
    s_last_init_error.store(ESP_FAIL, std::memory_order_release);
    s_init_failures.fetch_add(1, std::memory_order_relaxed);
    s_camera_ok.store(false, std::memory_order_release);
    return false;
  }
  camera_sensor_info_t* sensor_info =
      esp_camera_sensor_get_info(&sensor->id);

  s_camera_ok.store(true, std::memory_order_release);
  s_last_init_error.store(ESP_OK, std::memory_order_release);
  log_camera_heap("ready");
  log_info("[CAMERA] ready sensor=%s pid=0x%x ver=0x%x mid=%02x:%02x "
           "framesize=%d quality=%u sensor_xclk=%d configured_xclk=%u "
           "psram_dma=%d hmirror=%u vflip=%u",
           sensor_info == nullptr ? "unknown" : sensor_info->name,
           static_cast<unsigned>(sensor->id.PID),
           static_cast<unsigned>(sensor->id.VER),
           static_cast<unsigned>(sensor->id.MIDH),
           static_cast<unsigned>(sensor->id.MIDL),
           static_cast<int>(sensor->status.framesize),
           static_cast<unsigned>(sensor->status.quality),
           sensor->xclk_freq_hz, static_cast<unsigned>(kCameraXclkHz),
           esp_camera_get_psram_mode() ? 1 : 0,
           static_cast<unsigned>(sensor->status.hmirror),
           static_cast<unsigned>(sensor->status.vflip));
  return true;
}

bool camera_boot_probe(CameraBootProbeStage stage) {
  CameraProbeRecord& record = s_boot_probe[camera_probe_index(stage)];
  record = CameraProbeRecord{};
  if (!s_camera_ok.load(std::memory_order_acquire)) {
    log_warn("[CAMERA_DIAG] probe skipped stage=%s camera_not_ready",
             camera_probe_stage_name(stage));
    return false;
  }

  sample_camera_sync_activity(&record);
  log_warn("[CAMERA_DIAG] probe begin stage=%s configured_xclk=%u "
           "edges(v/h/p)=%u/%u/%u",
           camera_probe_stage_name(stage),
           static_cast<unsigned>(kCameraXclkHz),
           static_cast<unsigned>(record.vsync_edges),
           static_cast<unsigned>(record.href_edges),
           static_cast<unsigned>(record.pclk_edges));

  const uint32_t started_ms = millis();
  camera_fb_t* fb = esp_camera_fb_get();
  record.elapsed_ms = millis() - started_ms;
  if (fb == nullptr) {
    record.status = CameraProbeStatus::kNoFrame;
    s_capture_failures.fetch_add(1, std::memory_order_relaxed);
  } else {
    record.frame_len = fb->len;
    record.width = static_cast<uint16_t>(fb->width);
    record.height = static_cast<uint16_t>(fb->height);
    record.format = static_cast<int>(fb->format);
    record.status = camera_source_frame_valid(fb)
                        ? CameraProbeStatus::kFrame
                        : CameraProbeStatus::kInvalidFrame;
    if (record.status == CameraProbeStatus::kFrame) {
      s_last_frame_ms.store(millis(), std::memory_order_release);
    } else {
      s_capture_failures.fetch_add(1, std::memory_order_relaxed);
    }
    esp_camera_fb_return(fb);
  }
  log_camera_probe(stage, record, "live");
  return record.status == CameraProbeStatus::kFrame;
}

/*
 * 设置浏览器预览的上传节奏。唯一生产者是 PC 端的 CameraCadenceController
 * （预览租约与有界 capture boost 的合并值）；LLM 可写通道已在服务端源头
 * 删除，本入口不构成租约体系外的旁路。fps==0 表示停止预览。
 */
void camera_set_fps(uint32_t fps) {
  if (fps == 0) {
    s_interval_ms.store(0, std::memory_order_release);
    log_info("[CAMERA] preview upload stopped");
    return;
  }
  const uint32_t requested_fps = fps;
  if (fps > DESKBOT_CAMERA_MAX_FPS) {
    fps = DESKBOT_CAMERA_MAX_FPS;
    log_warn("[CAMERA] fps clamped requested=%u max=%u",
             static_cast<unsigned>(requested_fps),
             static_cast<unsigned>(DESKBOT_CAMERA_MAX_FPS));
  }
  const uint32_t interval_ms = 1000u / fps;
  s_interval_ms.store(interval_ms, std::memory_order_release);
  log_info("[CAMERA] fps=%u interval=%ums", static_cast<unsigned>(fps),
           static_cast<unsigned>(interval_ms));
}

void camera_request_snapshot() {
  uint32_t pending = s_pending_snapshots.load(std::memory_order_relaxed);
  while (pending < 4u &&
         !s_pending_snapshots.compare_exchange_weak(
             pending, pending + 1u, std::memory_order_release,
             std::memory_order_relaxed)) {
  }
  log_info("[CAMERA] one-shot requested pending=%u",
           static_cast<unsigned>(
               s_pending_snapshots.load(std::memory_order_acquire)));
  if (s_camera_task != nullptr) {
    xTaskNotifyGive(s_camera_task);
  }
}

void camera_reset_session_state() {
  s_interval_ms.store(
      DESKBOT_CAMERA_UPLINK_INTERVAL_MS, std::memory_order_release);
  s_pending_snapshots.store(0, std::memory_order_release);
  log_info("[CAMERA] session controls reset interval=%ums",
           static_cast<unsigned>(DESKBOT_CAMERA_UPLINK_INTERVAL_MS));
}

CameraHealthSnapshot camera_health_snapshot() {
  CameraHealthSnapshot snapshot{};
  snapshot.ready = s_camera_ok.load(std::memory_order_acquire);
  snapshot.last_frame_ms =
      s_last_frame_ms.load(std::memory_order_acquire);
  snapshot.last_upload_ms =
      s_last_upload_ms.load(std::memory_order_acquire);
  snapshot.capture_failures =
      s_capture_failures.load(std::memory_order_acquire);
  snapshot.transport_failures =
      s_transport_failures.load(std::memory_order_acquire);
  snapshot.init_failures =
      s_init_failures.load(std::memory_order_acquire);
  snapshot.recovery_attempts =
      s_recovery_attempts.load(std::memory_order_acquire);
  snapshot.recovery_successes =
      s_recovery_successes.load(std::memory_order_acquire);
  snapshot.pending_snapshots =
      s_pending_snapshots.load(std::memory_order_acquire);
  snapshot.interval_ms = s_interval_ms.load(std::memory_order_acquire);
  return snapshot;
}

void task_setup_camera() {
  if (s_camera_task != nullptr) {
    return;
  }
  TaskHandle_t created_task = nullptr;
  const BaseType_t created = xTaskCreatePinnedToCore(
      camera_uplink_task, "camera_usb", 4096, nullptr,
      kCameraTaskPriority, &created_task, PRO_CPU_NUM);
  if (created != pdPASS || created_task == nullptr) {
    log_error("[CAMERA] supervisor task create failed");
    return;
  }
  s_camera_task = created_task;
  log_info("[CAMERA] supervisor started ready=%d interval=%ums mode=%s",
           s_camera_ok.load(std::memory_order_acquire) ? 1 : 0,
           static_cast<unsigned>(
               s_interval_ms.load(std::memory_order_acquire)),
           s_interval_ms.load(std::memory_order_acquire) == 0
               ? "on-demand"
               : "continuous");
}
