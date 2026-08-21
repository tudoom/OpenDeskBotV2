#include "usb_transport.h"

#include "audio_capture.h"
#include "audio_frontend_esp_sr.h"
#include "camera.h"
#include "common.h"
#include "deskbot_uplink_state.h"
#include "head.h"
#include "rtc_audio_downlink.h"
#include "runtime_supervisor.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <HWCDC.h>
#include <esp_heap_caps.h>
#include <esp_log.h>
#include <esp_system.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <soc/soc_caps.h>
#include <atomic>
#include <string.h>

#if !defined(CONFIG_IDF_TARGET_ESP32S3) || !CONFIG_IDF_TARGET_ESP32S3
#error "Deskbot USB firmware requires ESP32-S3"
#endif
#if !defined(SOC_USB_SERIAL_JTAG_SUPPORTED) || !SOC_USB_SERIAL_JTAG_SUPPORTED
#error "Deskbot USB firmware requires the USB Serial/JTAG peripheral"
#endif
#if !defined(ARDUINO_USB_MODE) || ARDUINO_USB_MODE != 1
#error "Deskbot requires ARDUINO_USB_MODE=1 (HWCDC)"
#endif
#if !defined(ARDUINO_USB_CDC_ON_BOOT) || ARDUINO_USB_CDC_ON_BOOT != 1
#error "Deskbot requires ARDUINO_USB_CDC_ON_BOOT=1"
#endif
#if !defined(HWCDC_SERIAL_IS_DEFINED)
#error "HWCDCSerial must be provided by the selected Arduino board"
#endif

namespace {

static constexpr uint8_t kMagicBytes[4] = {'D', 'B', 'O', 'T'};
/* Four KiB is above the observed RX high-water mark while returning another
 * contiguous 4 KiB to camera/audio DMA.  Protocol payloads are streamed into
 * their own bounded assembler, so this does not cap PB or control messages. */
static constexpr size_t kCdcRxBufferBytes = 4096u;
static constexpr size_t kCdcTxBufferBytes = 4096u;
static constexpr uint32_t kCdcDriverTxTimeoutMs = 20u;
static constexpr uint32_t kHeartbeatIntervalMs = 2000u;
// Do not race the PC's three-heartbeat liveness window.  The device remains
// receptive long enough for Windows CDC jitter and lets the host reopen first.
static constexpr uint32_t kHeartbeatTimeoutMs = 15000u;
static constexpr uint32_t kHeartbeatRetryMs = 100u;
static constexpr uint32_t kHeartbeatFailureLogIntervalMs = 5000u;
static constexpr uint32_t kParserIdleTimeoutMs = 2500u;
static constexpr uint32_t kParserAbsoluteTimeoutMs = 15000u;
static constexpr uint32_t kHelloReplayWindowMs = 3000u;
static constexpr uint32_t kCameraTxMutexWaitMs = 25u;
static constexpr uint32_t kSessionBoundaryTxWaitMs = 250u;
/* Best-effort goodbye on end_session: a short mutex bound keeps the teardown
 * path from stalling behind an in-flight media frame. */
static constexpr uint32_t kSessionEndNoticeTxWaitMs = 50u;
static constexpr size_t kControlMaxPayload = 16u * 1024u;
static constexpr size_t kLogMaxPayload = 16u * 1024u;
static constexpr size_t kAudioMaxPayload = 64u * 1024u;
static constexpr size_t kInlineRxPayloadBytes = 512u;
static constexpr size_t kWriteChunkBytes = 1024u;
static constexpr uint32_t kWriteBaseTimeoutMs = 300u;
static constexpr uint32_t kWritePerKiBTimeoutMs = 100u;

enum class RxState : uint8_t {
  kSeekMagic = 0,
  kReadHeader = 1,
  kReadPayload = 2,
  kReadPayloadCrc = 3,
};

struct Parser {
  RxState state = RxState::kSeekMagic;
  uint8_t magic_match = 0;
  uint32_t started_ms = 0;
  uint32_t last_progress_ms = 0;
  uint8_t header[DESKBOT_USB_HEADER_SIZE]{};
  size_t header_pos = 0;
  uint8_t* payload = nullptr;
  uint8_t inline_payload[kInlineRxPayloadBytes]{};
  size_t payload_length = 0;
  size_t payload_pos = 0;
  uint8_t payload_crc[4]{};
  size_t payload_crc_pos = 0;
};

Parser s_parser;
DeskbotUsbFrameHandler s_frame_handler = nullptr;
DeskbotUsbLinkHandler s_link_handler = nullptr;
void* s_callback_context = nullptr;
SemaphoreHandle_t s_tx_mutex = nullptr;
std::atomic<bool> s_cdc_begun{false};
std::atomic<bool> s_cdc_preferred_buffers{false};
std::atomic<bool> s_begun{false};
std::atomic<bool> s_active{false};
std::atomic<bool> s_framed_mode{false};
std::atomic<uint32_t> s_session_epoch{0};
uint32_t s_next_tx_sequence = 1;
std::atomic<uint32_t> s_last_rx_ms{0};
std::atomic<uint32_t> s_last_heartbeat_tx_ms{0};
std::atomic<uint32_t> s_last_heartbeat_attempt_ms{0};
std::atomic<uint32_t> s_last_heartbeat_failure_log_ms{0};
uint32_t s_client_nonce = 0;
uint32_t s_last_hello_ms = 0;
bool s_client_nonce_valid = false;
std::atomic<bool> s_parser_reset_pending{false};
std::atomic<bool> s_cdc_tx_cleanup_pending{false};
/* Writer tasks may not run the link callback (it frees parser-task-owned
 * buffers); they flag the failure here and usb_transport_poll() delivers
 * notify_link(false) on loopTask.  See send_with_epoch. */
std::atomic<bool> s_link_down_pending{false};
/* Tripwire against nested polling: the DBOT parser is single-owner state and
 * a re-entered poll consumes host bytes with a mid-frame parser. */
std::atomic<bool> s_poll_in_progress{false};
std::atomic<bool> s_poll_reentry_warned{false};
std::atomic<uint32_t> s_partial_tx_failures{0};
std::atomic<uint32_t> s_payload_crc_errors{0};
std::atomic<uint32_t> s_cdc_rx_buffer_bytes{0};
std::atomic<uint32_t> s_cdc_rx_high_water{0};
std::atomic<uint32_t> s_usb_poll_max_gap_ms{0};
uint32_t s_last_usb_poll_ms = 0;

void update_max(std::atomic<uint32_t>& target, uint32_t value) {
  uint32_t current = target.load(std::memory_order_relaxed);
  while (value > current &&
         !target.compare_exchange_weak(current, value,
                                       std::memory_order_relaxed,
                                       std::memory_order_relaxed)) {
  }
}

uint32_t read_u32_le(const uint8_t* p) {
  return static_cast<uint32_t>(p[0]) |
         (static_cast<uint32_t>(p[1]) << 8) |
         (static_cast<uint32_t>(p[2]) << 16) |
         (static_cast<uint32_t>(p[3]) << 24);
}

void write_u32_le(uint8_t* p, uint32_t value) {
  p[0] = static_cast<uint8_t>(value & 0xffu);
  p[1] = static_cast<uint8_t>((value >> 8) & 0xffu);
  p[2] = static_cast<uint8_t>((value >> 16) & 0xffu);
  p[3] = static_cast<uint8_t>((value >> 24) & 0xffu);
}

size_t channel_payload_limit(uint8_t channel) {
  switch (channel) {
    case DESKBOT_USB_CONTROL_JSON:
      return kControlMaxPayload;
    case DESKBOT_USB_LOG:
      return kLogMaxPayload;
    case DESKBOT_USB_AUDIO_UP_OPUS:
    case DESKBOT_USB_AUDIO_DOWN_OPUS:
      return kAudioMaxPayload;
    case DESKBOT_USB_PB_WIRE:
    case DESKBOT_USB_CAMERA_JPEG:
      return DESKBOT_USB_MAX_PAYLOAD;
    default:
      return 0;
  }
}

void free_parser_payload() {
  if (s_parser.payload != nullptr &&
      s_parser.payload != s_parser.inline_payload) {
    heap_caps_free(s_parser.payload);
  }
  // Clear the parser's view even when the previous payload used inline
  // storage.  Zero-length frames (notably CANCEL_STREAM) deliberately expose
  // a null payload pointer to the application layer.
  s_parser.payload = nullptr;
  s_parser.payload_length = 0;
  s_parser.payload_pos = 0;
}

void reset_parser() {
  free_parser_payload();
  s_parser.state = RxState::kSeekMagic;
  s_parser.magic_match = 0;
  s_parser.started_ms = 0;
  s_parser.last_progress_ms = 0;
  s_parser.header_pos = 0;
  s_parser.payload_crc_pos = 0;
}

bool flush_stale_cdc_tx() {
  if (s_tx_mutex == nullptr) {
    HWCDCSerial.flush();
    return true;
  }
  if (xSemaphoreTake(s_tx_mutex, pdMS_TO_TICKS(kSessionBoundaryTxWaitMs)) !=
      pdTRUE) {
    return false;
  }
  HWCDCSerial.flush();
  xSemaphoreGive(s_tx_mutex);
  return true;
}


void seek_magic_byte(uint8_t value) {
  if (value == kMagicBytes[s_parser.magic_match]) {
    s_parser.magic_match++;
    if (s_parser.magic_match == sizeof(kMagicBytes)) {
      memcpy(s_parser.header, kMagicBytes, sizeof(kMagicBytes));
      s_parser.header_pos = sizeof(kMagicBytes);
      s_parser.magic_match = 0;
      s_parser.started_ms = millis();
      s_parser.last_progress_ms = s_parser.started_ms;
      s_parser.state = RxState::kReadHeader;
    }
    return;
  }
  /*
   * "DBOT" has no non-empty proper prefix which is also a suffix.  A failed
   * match can therefore only restart when this byte itself is 'D'.
   */
  s_parser.magic_match = (value == kMagicBytes[0]) ? 1u : 0u;
}

void resynchronise_header() {
  uint8_t snapshot[DESKBOT_USB_HEADER_SIZE];
  const size_t length = s_parser.header_pos;
  memcpy(snapshot, s_parser.header, length);
  reset_parser();
  /*
   * Re-feed every byte except the first magic byte.  This preserves a valid
   * DBOT marker which started inside a corrupt candidate header.
   */
  for (size_t i = 1; i < length; ++i) {
    if (s_parser.state == RxState::kSeekMagic) {
      seek_magic_byte(snapshot[i]);
    } else if (s_parser.state == RxState::kReadHeader &&
               s_parser.header_pos < DESKBOT_USB_HEADER_SIZE) {
      s_parser.header[s_parser.header_pos++] = snapshot[i];
    }
  }
}

bool header_is_valid() {
  if (read_u32_le(s_parser.header) != DESKBOT_USB_MAGIC ||
      s_parser.header[4] != DESKBOT_USB_PROTOCOL_VERSION ||
      s_parser.header[5] != DESKBOT_USB_HEADER_SIZE) {
    return false;
  }
  const uint8_t channel = s_parser.header[6];
  const uint32_t payload_length = read_u32_le(s_parser.header + 16);
  const size_t limit = channel_payload_limit(channel);
  if (limit == 0 || payload_length > limit ||
      payload_length > DESKBOT_USB_MAX_PAYLOAD) {
    return false;
  }
  return read_u32_le(s_parser.header + 20) ==
         usb_transport_crc32(s_parser.header, 20);
}

bool write_all(const uint8_t* data, size_t length, uint32_t deadline_ms,
               uint32_t epoch, bool require_active,
               size_t* bytes_written) {
  if (bytes_written != nullptr) {
    *bytes_written = 0;
  }
  size_t offset = 0;
  while (offset < length) {
    if (require_active &&
        (!s_active.load(std::memory_order_acquire) ||
         s_session_epoch.load(std::memory_order_relaxed) != epoch)) {
      return false;
    }
    if (static_cast<int32_t>(millis() - deadline_ms) >= 0) {
      return false;
    }
    size_t chunk = length - offset;
    if (chunk > kWriteChunkBytes) {
      chunk = kWriteChunkBytes;
    }
    const size_t written = HWCDCSerial.write(data + offset, chunk);
    if (written == 0) {
      vTaskDelay(pdMS_TO_TICKS(1));
      continue;
    }
    offset += written;
    if (bytes_written != nullptr) {
      *bytes_written = offset;
    }
    taskYIELD();
  }
  return true;
}

void notify_link(bool ready);

bool send_with_epoch(DeskbotUsbChannel channel, const uint8_t* payload,
                     size_t payload_length, uint8_t flags, uint32_t epoch,
                     bool require_active,
                     uint32_t mutex_wait_ms_override = 0) {
  if (!s_begun || (require_active && !s_active) ||
      (payload_length > 0 && payload == nullptr) ||
      payload_length > channel_payload_limit(channel)) {
    return false;
  }
  TickType_t mutex_wait = pdMS_TO_TICKS(1000);
  if (mutex_wait_ms_override > 0) {
    mutex_wait = pdMS_TO_TICKS(mutex_wait_ms_override);
  } else if (channel == DESKBOT_USB_CAMERA_JPEG) {
    /*
     * Camera remains best-effort, but a zero-wait lock can starve forever
     * while the microphone emits near-continuous Opus batches.  A bounded
     * 25 ms window lets the current priority write finish without allowing
     * camera traffic to queue indefinitely ahead of later voice/PB frames.
     */
    mutex_wait = pdMS_TO_TICKS(kCameraTxMutexWaitMs);
  } else if (channel == DESKBOT_USB_LOG) {
    mutex_wait = pdMS_TO_TICKS(10);
  }
  if (s_tx_mutex == nullptr ||
      xSemaphoreTake(s_tx_mutex, mutex_wait) != pdTRUE) {
    return false;
  }
  // A sender can wait behind a session boundary. Revalidate after locking so
  // an old camera/audio frame cannot leak into the newly negotiated epoch.
  if (require_active &&
      (!s_active.load(std::memory_order_acquire) ||
       s_session_epoch.load(std::memory_order_relaxed) != epoch)) {
    xSemaphoreGive(s_tx_mutex);
    return false;
  }

  uint8_t header[DESKBOT_USB_HEADER_SIZE]{};
  memcpy(header, kMagicBytes, sizeof(kMagicBytes));
  header[4] = DESKBOT_USB_PROTOCOL_VERSION;
  header[5] = DESKBOT_USB_HEADER_SIZE;
  header[6] = static_cast<uint8_t>(channel);
  header[7] = flags;
  write_u32_le(header + 8, s_next_tx_sequence++);
  write_u32_le(header + 12, epoch);
  write_u32_le(header + 16, static_cast<uint32_t>(payload_length));
  write_u32_le(header + 20, usb_transport_crc32(header, 20));

  uint8_t trailer[4];
  write_u32_le(trailer, usb_transport_crc32(payload, payload_length));
  const uint32_t kib = static_cast<uint32_t>((payload_length + 1023u) / 1024u);
  const uint32_t deadline =
      millis() + kWriteBaseTimeoutMs + kib * kWritePerKiBTimeoutMs;
  size_t frame_bytes_written = 0;
  size_t part_written = 0;
  bool ok = write_all(header, sizeof(header), deadline, epoch, require_active,
                      &part_written);
  frame_bytes_written += part_written;
  if (ok) {
    part_written = 0;
    ok = write_all(payload, payload_length, deadline, epoch, require_active,
                   &part_written);
    frame_bytes_written += part_written;
  }
  if (ok) {
    part_written = 0;
    ok = write_all(trailer, sizeof(trailer), deadline, epoch, require_active,
                   &part_written);
    frame_bytes_written += part_written;
  }

  /*
   * Once any byte of a frame reaches CDC, a timeout leaves an undecodable
   * prefix in the byte stream.  Fail the matching session closed while the TX
   * mutex still prevents a new epoch from starting.  A zero-byte timeout
   * remains ordinary backpressure and does not tear down an otherwise healthy
   * session.
   *
   * Never invoke the link callback here: send_with_epoch runs on arbitrary
   * writer tasks (camera, any logging task), while the callback releases PB
   * JSON assembly buffers and mutates Strings owned by the parser task.  Like
   * s_parser_reset_pending, only flag the failure; usb_transport_poll()
   * delivers notify_link(false) on loopTask.  The exchange on s_active stays
   * on the writer side so new sends fail fast immediately.  Both flags are
   * stored before the TX mutex is released, so a fresh hello — which flushes
   * stale TX under this mutex — observes and consumes them before it
   * establishes a new epoch.
   */
  bool partial_invalidated = false;
  if (!ok && frame_bytes_written > 0 &&
      s_active.load(std::memory_order_acquire) &&
      s_session_epoch.load(std::memory_order_relaxed) == epoch) {
    partial_invalidated =
        s_active.exchange(false, std::memory_order_acq_rel);
    if (partial_invalidated) {
      s_parser_reset_pending.store(true, std::memory_order_release);
      s_link_down_pending.store(true, std::memory_order_release);
      s_partial_tx_failures.fetch_add(1, std::memory_order_relaxed);
    }
  }
  xSemaphoreGive(s_tx_mutex);
  return ok;
}

void notify_link(bool ready) {
  if (ready) {
    deskbot_uplink_bump_transport_generation();
    deskbot_uplink_set_transport_ready(true);
  } else {
    deskbot_uplink_bump_transport_generation();
  }
  if (s_link_handler != nullptr) {
    s_link_handler(ready, s_session_epoch, s_callback_context);
  }
}

uint32_t make_session_epoch() {
  uint32_t epoch = esp_random();
  if (epoch == 0) {
    epoch = 1;
  }
  if (epoch == s_session_epoch) {
    epoch++;
    if (epoch == 0) {
      epoch = 1;
    }
  }
  return epoch;
}

bool control_type_is(const JsonDocument& doc, const char* expected) {
  const char* type = doc["type"] | "";
  return strcmp(type, expected) == 0;
}

void send_hello_error(const char* code) {
  char json[160];
  snprintf(json, sizeof(json),
           "{\"type\":\"hello_error\",\"protocol\":%u,\"code\":\"%s\"}",
           static_cast<unsigned>(DESKBOT_USB_PROTOCOL_VERSION),
           code != nullptr ? code : "invalid_hello");
  (void)send_with_epoch(DESKBOT_USB_CONTROL_JSON,
                        reinterpret_cast<const uint8_t*>(json), strlen(json),
                        DESKBOT_USB_FLAG_ERROR, 0, false);
}

bool session_end_reason_link_dead(const char* reason) {
  /*
   * frame_ack_send_failed: a CONTROL_JSON write just failed, so a goodbye
   * write can only fail the same way.  heartbeat_timeout: the host has been
   * silent past the liveness window and nobody drains CDC TX; queueing a
   * goodbye there would wait on a dead link (and PC-side already detects the
   * loss via its own heartbeat window).  Every other trigger — RX integrity
   * failures and application-level protocol resets — normally fires while the
   * physical link is still writable.
   */
  return reason != nullptr &&
         (strcmp(reason, "frame_ack_send_failed") == 0 ||
          strcmp(reason, "heartbeat_timeout") == 0);
}

void send_session_end_notice(const char* reason) {
  /* Best-effort, bounded, single attempt.  Failure is ignored: the PC still
   * falls back to its heartbeat timeout exactly as before this notice. */
  char safe_reason[96];
  const char* source = reason != nullptr ? reason : "unspecified";
  size_t out = 0;
  for (; source[out] != '\0' && out + 1 < sizeof(safe_reason); ++out) {
    const char c = source[out];
    /* Keep the JSON string literal well-formed for arbitrary callers. */
    safe_reason[out] =
        (c == '"' || c == '\\' || static_cast<uint8_t>(c) < 0x20u) ? '_' : c;
  }
  safe_reason[out] = '\0';
  char json[192];
  const uint32_t epoch = s_session_epoch.load(std::memory_order_relaxed);
  const int n = snprintf(
      json, sizeof(json),
      "{\"type\":\"session_end\",\"session_epoch\":%u,\"reason\":\"%s\"}",
      static_cast<unsigned>(epoch), safe_reason);
  if (n <= 0 || static_cast<size_t>(n) >= sizeof(json)) {
    return;
  }
  (void)send_with_epoch(DESKBOT_USB_CONTROL_JSON,
                        reinterpret_cast<const uint8_t*>(json),
                        static_cast<size_t>(n), DESKBOT_USB_FLAG_NONE, epoch,
                        true, kSessionEndNoticeTxWaitMs);
}

bool send_hello_ack(uint32_t rx_sequence, uint32_t client_nonce) {
  char json[1536];
  const CameraHealthSnapshot camera_health = camera_health_snapshot();
  const HeadServoHealthSnapshot servo_health = head_servo_health_snapshot();
  const char* audio_frontend_caps =
      audio_frontend_ready()
          ? ",\"audio_aec\",\"audio_ns\",\"audio_vad_esp_sr\""
          : "";
  const char* rtc_audio_caps =
      rtc_audio_downlink_ready()
          ? ",\"audio_down_opus\",\"rtc_audio_gateway\""
          : "";
  const char* full_duplex_cap =
      rtc_audio_downlink_ready() && audio_frontend_ready()
          ? ",\"rtc_full_duplex\""
          : "";
  const int n = snprintf(
      json, sizeof(json),
      "{\"type\":\"hello_ack\",\"protocol\":%u,\"device_id\":\"%s\","
      "\"product\":\"%s\",\"firmware\":\"%s\",\"session_epoch\":%u,"
      "\"ack_client_nonce\":%u,\"heartbeat_ms\":%u,\"timeout_ms\":%u,"
      "\"max_payload\":%u,\"ack_sequence\":%u,"
       "\"reset_reason\":\"%s\",\"recovery_count\":%u,"
       "\"boot_count\":%u,\"uptime_ms\":%u,"
       "\"hardware_reset_reason\":\"%s\",\"hardware_reset_code\":%u,"
       "\"last_panic\":%s,\"last_restart_uptime_ms\":%u,"
       "\"mic_signal_healthy\":%s,"
       "\"camera_ready\":%s,\"camera_last_frame_ms\":%u,"
       "\"camera_capture_failures\":%u,\"camera_recovery_count\":%u,"
       "\"usb_partial_tx_failures\":%u,"
       "\"usb_payload_crc_errors\":%u,\"usb_rx_buffer_bytes\":%u,"
       "\"usb_rx_high_water\":%u,\"usb_poll_max_gap_ms\":%u,"
       "\"servo_ready\":%s,\"servo_backend\":\"ledc\","
       "\"servo_x_pin\":%d,\"servo_y_pin\":%d,\"servo_pwm_hz\":%u,"
       "\"servo_x_pulse_us\":%d,\"servo_y_pulse_us\":%d,"
       "\"servo_write_failures\":%u,"

      "\"capabilities\":[\"control_json\",\"pb_wire\","
      "\"audio_up_opus\",\"camera_jpeg\",\"log\",\"frame_ack\","
      "\"pb_json_fragments\"%s%s%s]}",
      static_cast<unsigned>(DESKBOT_USB_PROTOCOL_VERSION), get_device_id(),
      PRODUCT_NAME, VERSION, static_cast<unsigned>(s_session_epoch),
      static_cast<unsigned>(client_nonce),
      static_cast<unsigned>(kHeartbeatIntervalMs),
      static_cast<unsigned>(kHeartbeatTimeoutMs),
      static_cast<unsigned>(DESKBOT_USB_MAX_PAYLOAD),
      static_cast<unsigned>(rx_sequence),
       runtime_supervisor_last_reason(),
       static_cast<unsigned>(runtime_supervisor_recovery_count()),
       static_cast<unsigned>(runtime_supervisor_boot_count()),
       static_cast<unsigned>(millis()),
       runtime_supervisor_hardware_reset_reason(),
       static_cast<unsigned>(runtime_supervisor_hardware_reset_code()),
       runtime_supervisor_last_reset_was_panic() ? "true" : "false",
       static_cast<unsigned>(
           runtime_supervisor_last_restart_uptime_ms()),
       mic_capture_signal_healthy() ? "true" : "false",
       camera_health.ready ? "true" : "false",
       static_cast<unsigned>(camera_health.last_frame_ms),
       static_cast<unsigned>(camera_health.capture_failures),
       static_cast<unsigned>(camera_health.recovery_successes),
       static_cast<unsigned>(
           s_partial_tx_failures.load(std::memory_order_relaxed)),
       static_cast<unsigned>(
           s_payload_crc_errors.load(std::memory_order_relaxed)),
       static_cast<unsigned>(
           s_cdc_rx_buffer_bytes.load(std::memory_order_relaxed)),
       static_cast<unsigned>(
           s_cdc_rx_high_water.load(std::memory_order_relaxed)),
       static_cast<unsigned>(
           s_usb_poll_max_gap_ms.load(std::memory_order_relaxed)),
       servo_health.ready ? "true" : "false",
       servo_health.x_pin,
       servo_health.y_pin,
       static_cast<unsigned>(servo_health.pwm_hz),
       servo_health.x_pulse_us,
       servo_health.y_pulse_us,
       static_cast<unsigned>(servo_health.write_failures),

      rtc_audio_caps,
      audio_frontend_caps,
      full_duplex_cap);
  return n > 0 && static_cast<size_t>(n) < sizeof(json) &&
         send_with_epoch(DESKBOT_USB_CONTROL_JSON,
                         reinterpret_cast<const uint8_t*>(json),
                         static_cast<size_t>(n), DESKBOT_USB_FLAG_ACK,
                         s_session_epoch, false);
}

bool send_frame_ack(uint32_t rx_sequence, uint8_t rx_channel) {
  char json[160];
  const int n = snprintf(
      json, sizeof(json),
      "{\"type\":\"frame_ack\",\"session_epoch\":%u,"
      "\"ack_sequence\":%u,\"ack_channel\":%u}",
      static_cast<unsigned>(s_session_epoch),
      static_cast<unsigned>(rx_sequence),
      static_cast<unsigned>(rx_channel));
  return n > 0 && static_cast<size_t>(n) < sizeof(json) &&
         send_with_epoch(DESKBOT_USB_CONTROL_JSON,
                         reinterpret_cast<const uint8_t*>(json),
                         static_cast<size_t>(n), DESKBOT_USB_FLAG_ACK,
                         s_session_epoch, true);
}

void accept_hello(uint32_t rx_sequence, uint32_t client_nonce) {
  const uint32_t now = millis();
  const bool same_nonce =
      s_client_nonce_valid && client_nonce == s_client_nonce;
  const bool legacy_replay =
      client_nonce == 0 && same_nonce && s_last_hello_ms != 0 &&
      static_cast<uint32_t>(now - s_last_hello_ms) <= kHelloReplayWindowMs;
  /*
   * A replayed nonce may only short-circuit the handshake while the session it
   * refers to is still fully alive on both layers: transport active and no
   * deferred link-down waiting for delivery.  Reviving s_active here after a
   * partial hello_ack failure created a zombie session — heartbeats kept
   * flowing while the application layer had already torn its state down.
   */
  if (((client_nonce != 0 && same_nonce && s_session_epoch != 0) ||
       legacy_replay) &&
      s_active.load(std::memory_order_acquire) &&
      !s_link_down_pending.load(std::memory_order_acquire)) {
    s_last_hello_ms = now;
    s_last_rx_ms.store(now, std::memory_order_relaxed);
    s_last_heartbeat_tx_ms.store(now, std::memory_order_relaxed);
    s_last_heartbeat_attempt_ms.store(now, std::memory_order_relaxed);
    s_last_heartbeat_failure_log_ms.store(0, std::memory_order_relaxed);
    s_framed_mode = true;
    if (send_hello_ack(rx_sequence, client_nonce)) {
      return;
    }
    /*
     * hello_ack did not reach the host completely.  A partial write already
     * invalidated s_active and flagged the deferred link-down; a zero-byte
     * timeout leaves the session unacknowledged.  Either way, fall through to
     * a full re-handshake so host and application rejoin one fresh epoch
     * together instead of resurrecting a half-dead session.
     */
  }

  const bool was_active =
      s_active.exchange(false, std::memory_order_acq_rel);
  if (was_active) {
    notify_link(false);
  }
  // Stop old epoch writers before clearing HWCDC. Without this boundary, a
  // closed/reopened COM port can leave a large stale frame ahead of hello_ack.
  if (!flush_stale_cdc_tx()) {
    s_cdc_tx_cleanup_pending.store(true, std::memory_order_release);
    return;
  }
  s_cdc_tx_cleanup_pending.store(false, std::memory_order_release);
  /*
   * The TX mutex boundary above orders every writer's deferred link-down
   * store before this point.  Consume it now, on loopTask, so a flag raised
   * for the dying epoch cannot leak into the new session and kill it on the
   * next poll.  When the writer already won the s_active exchange, this is
   * also the only link-down notification the application receives.
   */
  if (s_link_down_pending.exchange(false, std::memory_order_acq_rel)) {
    notify_link(false);
  }
  s_session_epoch = make_session_epoch();
  s_client_nonce = client_nonce;
  s_client_nonce_valid = true;
  s_last_hello_ms = now;
  s_next_tx_sequence = 1;
  s_last_rx_ms.store(now, std::memory_order_relaxed);
  s_last_heartbeat_tx_ms.store(now, std::memory_order_relaxed);
  s_last_heartbeat_attempt_ms.store(now, std::memory_order_relaxed);
  s_last_heartbeat_failure_log_ms.store(0, std::memory_order_relaxed);
  s_framed_mode = true;
  s_active = true;

  if (!send_hello_ack(rx_sequence, client_nonce)) {
    s_active = false;
    return;
  }
  notify_link(true);
}

bool process_transport_control(const DeskbotUsbRxFrame& frame) {
  if (frame.payload == nullptr || frame.payload_length == 0 ||
      frame.payload_length > kControlMaxPayload) {
    return false;
  }
  JsonDocument doc;
  const DeserializationError error =
      deserializeJson(doc, frame.payload, frame.payload_length);
  if (error) {
    return false;
  }

  if (control_type_is(doc, "hello")) {
    const int protocol = doc["protocol"] | 0;
    if (protocol != DESKBOT_USB_PROTOCOL_VERSION) {
      send_hello_error("unsupported_protocol");
      return true;
    }
    if (frame.session_epoch != 0 &&
        (!s_active || frame.session_epoch != s_session_epoch)) {
      send_hello_error("stale_session");
      return true;
    }
    const uint32_t client_nonce =
        doc["client_nonce"].is<uint32_t>()
            ? doc["client_nonce"].as<uint32_t>()
            : 0u;
    accept_hello(frame.sequence, client_nonce);
    return true;
  }

  if (!s_active || frame.session_epoch != s_session_epoch) {
    return true;
  }
  if (control_type_is(doc, "heartbeat") ||
      control_type_is(doc, "ping")) {
    char json[512];
    const CameraHealthSnapshot camera_health = camera_health_snapshot();
    const int n = snprintf(
        json, sizeof(json),
        "{\"type\":\"heartbeat_ack\",\"session_epoch\":%u,"
        "\"ack_sequence\":%u,\"uptime_ms\":%u,"
        "\"mic_signal_healthy\":%s,\"camera_ready\":%s,"
        "\"camera_last_frame_ms\":%u,\"camera_capture_failures\":%u,"
        "\"camera_recovery_count\":%u,\"usb_partial_tx_failures\":%u,"
        "\"usb_payload_crc_errors\":%u,\"usb_rx_high_water\":%u,"
        "\"usb_poll_max_gap_ms\":%u}",
        static_cast<unsigned>(s_session_epoch),
        static_cast<unsigned>(frame.sequence),
        static_cast<unsigned>(millis()),
        mic_capture_signal_healthy() ? "true" : "false",
        camera_health.ready ? "true" : "false",
        static_cast<unsigned>(camera_health.last_frame_ms),
        static_cast<unsigned>(camera_health.capture_failures),
        static_cast<unsigned>(camera_health.recovery_successes),
        static_cast<unsigned>(
            s_partial_tx_failures.load(std::memory_order_relaxed)),
        static_cast<unsigned>(
            s_payload_crc_errors.load(std::memory_order_relaxed)),
        static_cast<unsigned>(
            s_cdc_rx_high_water.load(std::memory_order_relaxed)),
        static_cast<unsigned>(
            s_usb_poll_max_gap_ms.load(std::memory_order_relaxed)));
    if (n > 0 && static_cast<size_t>(n) < sizeof(json)) {
      (void)usb_transport_send(
          DESKBOT_USB_CONTROL_JSON,
          reinterpret_cast<const uint8_t*>(json), static_cast<size_t>(n),
          DESKBOT_USB_FLAG_ACK);
    }
    return true;
  }
  if (control_type_is(doc, "heartbeat_ack") ||
      control_type_is(doc, "pong")) {
    /*
     * These are transport acknowledgements, not application commands.  The
     * validated frame still refreshes s_last_rx_ms in dispatch_complete_frame,
     * but must not reach AsrChatClient as an unknown CONTROL_JSON message.
     */
    return true;
  }
  return false;
}

void dispatch_complete_frame() {
  const uint8_t channel_raw = s_parser.header[6];
  const uint8_t flags = s_parser.header[7];
  const uint32_t sequence = read_u32_le(s_parser.header + 8);
  const uint32_t epoch = read_u32_le(s_parser.header + 12);
  const uint32_t expected_crc = read_u32_le(s_parser.payload_crc);
  const uint32_t actual_crc =
      usb_transport_crc32(s_parser.payload, s_parser.payload_length);
  if (expected_crc != actual_crc) {
    s_payload_crc_errors.fetch_add(1, std::memory_order_relaxed);
    log_warn(
        "[USB] payload CRC mismatch channel=%u sequence=%u flags=0x%02x "
        "length=%u expected=0x%08lx actual=0x%08lx",
        static_cast<unsigned>(channel_raw), static_cast<unsigned>(sequence),
        static_cast<unsigned>(flags),
        static_cast<unsigned>(s_parser.payload_length),
        static_cast<unsigned long>(expected_crc),
        static_cast<unsigned long>(actual_crc));
    reset_parser();
    if (s_active) {
      /*
       * A dropped PB fragment would otherwise leave its logical accumulator
       * alive until a later timeout.  CRC failure invalidates the negotiated
       * byte stream immediately so the application can release all partial
       * PB/audio state through its link-down callback.
       */
      usb_transport_end_session("payload_crc_mismatch");
    }
    return;
  }

  DeskbotUsbRxFrame frame{};
  frame.channel = static_cast<DeskbotUsbChannel>(channel_raw);
  frame.flags = flags;
  frame.sequence = sequence;
  frame.session_epoch = epoch;
  frame.payload = s_parser.payload;
  frame.payload_length = s_parser.payload_length;

  bool consumed = false;
  if (frame.channel == DESKBOT_USB_CONTROL_JSON) {
    consumed = process_transport_control(frame);
  }
  if (s_active && frame.session_epoch == s_session_epoch) {
    s_last_rx_ms = millis();
    /*
     * PB binaries can be larger than HWCDC's bounded receive queue.  ACK only
     * after the complete frame passed CRC and epoch validation, but before the
     * application emits logs or PB progress.  The host permits only one
     * ACK-required frame in flight, so at most one following fragment can wait
     * in HWCDC while this frame's application handler runs.
     */
    if ((frame.flags & DESKBOT_USB_FLAG_ACK_REQUIRED) != 0 &&
        !send_frame_ack(frame.sequence, channel_raw)) {
      usb_transport_end_session("frame_ack_send_failed");
      reset_parser();
      return;
    }
    if (!consumed && s_frame_handler != nullptr) {
      s_frame_handler(frame, s_callback_context);
    }
  }
  reset_parser();
}

void consume_rx_byte(uint8_t value) {
  if (s_parser.state != RxState::kSeekMagic) {
    s_parser.last_progress_ms = millis();
  }
  switch (s_parser.state) {
    case RxState::kSeekMagic:
      seek_magic_byte(value);
      return;
    case RxState::kReadHeader:
      s_parser.header[s_parser.header_pos++] = value;
      if (s_parser.header_pos < DESKBOT_USB_HEADER_SIZE) {
        return;
      }
      if (!header_is_valid()) {
        resynchronise_header();
        if (s_active) {
          usb_transport_end_session("invalid_frame_header");
        }
        return;
      }
      s_parser.payload_length = read_u32_le(s_parser.header + 16);
      s_parser.payload_pos = 0;
      s_parser.payload_crc_pos = 0;
      if (s_parser.payload_length == 0) {
        s_parser.state = RxState::kReadPayloadCrc;
        return;
      }
      if (s_parser.payload_length <= sizeof(s_parser.inline_payload)) {
        s_parser.payload = s_parser.inline_payload;
      } else {
        s_parser.payload = static_cast<uint8_t*>(heap_caps_malloc(
            s_parser.payload_length, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (s_parser.payload == nullptr) {
          s_parser.payload = static_cast<uint8_t*>(
              heap_caps_malloc(s_parser.payload_length, MALLOC_CAP_8BIT));
        }
      }
      if (s_parser.payload == nullptr) {
        reset_parser();
        if (s_active) {
          usb_transport_end_session("rx_payload_allocation_failed");
        }
        return;
      }
      s_parser.state = RxState::kReadPayload;
      return;
    case RxState::kReadPayload:
      s_parser.payload[s_parser.payload_pos++] = value;
      if (s_parser.payload_pos == s_parser.payload_length) {
        s_parser.state = RxState::kReadPayloadCrc;
      }
      return;
    case RxState::kReadPayloadCrc:
      s_parser.payload_crc[s_parser.payload_crc_pos++] = value;
      if (s_parser.payload_crc_pos == sizeof(s_parser.payload_crc)) {
        dispatch_complete_frame();
      }
      return;
  }
}

void heartbeat_tick() {
  if (!s_active) {
    return;
  }
  const uint32_t now = millis();
  if (static_cast<uint32_t>(now - s_last_rx_ms) > kHeartbeatTimeoutMs) {
    usb_transport_end_session("heartbeat_timeout");
    return;
  }
  const uint32_t last_success =
      s_last_heartbeat_tx_ms.load(std::memory_order_relaxed);
  const uint32_t last_attempt =
      s_last_heartbeat_attempt_ms.load(std::memory_order_relaxed);
  if (static_cast<uint32_t>(now - last_success) >= kHeartbeatIntervalMs &&
      static_cast<uint32_t>(now - last_attempt) >= kHeartbeatRetryMs) {
    s_last_heartbeat_attempt_ms.store(now, std::memory_order_relaxed);
    char json[512];
    const CameraHealthSnapshot camera_health = camera_health_snapshot();
    const int n = snprintf(
        json, sizeof(json),
        "{\"type\":\"heartbeat\",\"session_epoch\":%u,\"uptime_ms\":%u,"
        "\"mic_signal_healthy\":%s,\"camera_ready\":%s,"
        "\"camera_last_frame_ms\":%u,\"camera_capture_failures\":%u,"
        "\"camera_recovery_count\":%u,\"usb_partial_tx_failures\":%u,"
        "\"usb_payload_crc_errors\":%u,\"usb_rx_high_water\":%u,"
        "\"usb_poll_max_gap_ms\":%u}",
        static_cast<unsigned>(s_session_epoch), static_cast<unsigned>(now),
        mic_capture_signal_healthy() ? "true" : "false",
        camera_health.ready ? "true" : "false",
        static_cast<unsigned>(camera_health.last_frame_ms),
        static_cast<unsigned>(camera_health.capture_failures),
        static_cast<unsigned>(camera_health.recovery_successes),
        static_cast<unsigned>(
            s_partial_tx_failures.load(std::memory_order_relaxed)),
        static_cast<unsigned>(
            s_payload_crc_errors.load(std::memory_order_relaxed)),
        static_cast<unsigned>(
            s_cdc_rx_high_water.load(std::memory_order_relaxed)),
        static_cast<unsigned>(
            s_usb_poll_max_gap_ms.load(std::memory_order_relaxed)));
    const bool sent =
        n > 0 && static_cast<size_t>(n) < sizeof(json) &&
        usb_transport_send(
            DESKBOT_USB_CONTROL_JSON,
            reinterpret_cast<const uint8_t*>(json), static_cast<size_t>(n));
    if (sent) {
      /* Only a complete CDC frame is evidence of a transmitted heartbeat. */
      s_last_heartbeat_tx_ms.store(now, std::memory_order_relaxed);
      return;
    }
    if (!s_active.load(std::memory_order_acquire)) {
      /* A partial frame already failed the session closed.  Do not enqueue a
       * diagnostic LOG frame behind its corrupt prefix. */
      return;
    }

    const uint32_t last_log =
        s_last_heartbeat_failure_log_ms.load(std::memory_order_relaxed);
    if (last_log == 0 ||
        static_cast<uint32_t>(now - last_log) >=
            kHeartbeatFailureLogIntervalMs) {
      s_last_heartbeat_failure_log_ms.store(now, std::memory_order_relaxed);
      log_warn("[USB] heartbeat send failed; retrying (last_ok_age=%lums)",
               static_cast<unsigned long>(now - last_success));
    }
  }
}

void parser_idle_tick() {
  if (s_parser.state == RxState::kSeekMagic ||
      s_parser.last_progress_ms == 0) {
    return;
  }
  const uint32_t now = millis();
  const bool idle_expired =
      static_cast<uint32_t>(now - s_parser.last_progress_ms) >
      kParserIdleTimeoutMs;
  const bool lifetime_expired =
      s_parser.started_ms != 0 &&
      static_cast<uint32_t>(now - s_parser.started_ms) >
          kParserAbsoluteTimeoutMs;
  if (!idle_expired && !lifetime_expired) {
    return;
  }
  /*
   * A valid header followed by a truncated payload must not make the parser
   * consume a later hello as payload forever.  Native USB transfers complete
   * far below this bound; an idle partial frame is therefore abandoned and
   * the next DBOT marker can establish a fresh session.
   */
  reset_parser();
  if (s_active) {
    usb_transport_end_session(
        lifetime_expired
            ? "partial_frame_lifetime_timeout"
            : "partial_frame_idle_timeout");
  }
}

}  // namespace

bool usb_transport_cdc_begin(void) {
  if (s_cdc_begun.load(std::memory_order_acquire)) {
    return s_cdc_preferred_buffers.load(std::memory_order_relaxed);
  }

  /*
   * Arduino's HWCDC defaults to only 256 bytes in each direction.  Allocate
   * before begin(): begin() preserves a pre-created queue but otherwise
   * installs the small default.  This protects an early PC hello while the
   * display, FFat, audio and camera are still initialising.
   */
  const size_t rx_size = HWCDCSerial.setRxBufferSize(kCdcRxBufferBytes);
  const size_t tx_size = HWCDCSerial.setTxBufferSize(kCdcTxBufferBytes);
  HWCDCSerial.setTxTimeoutMs(kCdcDriverTxTimeoutMs);
  HWCDCSerial.begin(115200);

  /*
   * The prebuilt IDF image exposes USB Serial/JTAG as a secondary console.
   * Disable framework ESP_LOG output at runtime so it cannot inject raw text
   * into the DBOT stream after hello.  Deskbot's own logger remains active and
   * switches to the framed LOG channel.
   */
  esp_log_level_set("*", ESP_LOG_NONE);

  const bool preferred =
      rx_size == kCdcRxBufferBytes && tx_size == kCdcTxBufferBytes;
  s_cdc_rx_buffer_bytes.store(
      static_cast<uint32_t>(rx_size == 0 ? 256u : rx_size),
                              std::memory_order_relaxed);
  s_cdc_preferred_buffers.store(preferred, std::memory_order_relaxed);
  s_cdc_begun.store(true, std::memory_order_release);
  return preferred;
}

uint32_t usb_transport_crc32(const uint8_t* data, size_t length) {
  uint32_t crc = 0xffffffffu;
  if (data != nullptr) {
    for (size_t i = 0; i < length; ++i) {
      crc ^= static_cast<uint32_t>(data[i]);
      for (uint8_t bit = 0; bit < 8; ++bit) {
        const uint32_t mask =
            static_cast<uint32_t>(-static_cast<int32_t>(crc & 1u));
        crc = (crc >> 1) ^ (0xedb88320u & mask);
      }
    }
  }
  return crc ^ 0xffffffffu;
}

bool usb_transport_begin(DeskbotUsbFrameHandler frame_handler,
                         DeskbotUsbLinkHandler link_handler, void* context) {
  if (!s_cdc_begun.load(std::memory_order_acquire)) {
    return false;
  }
  if (s_tx_mutex == nullptr) {
    s_tx_mutex = xSemaphoreCreateMutex();
  }
  if (s_tx_mutex == nullptr) {
    return false;
  }
  reset_parser();
  s_frame_handler = frame_handler;
  s_link_handler = link_handler;
  s_callback_context = context;
  s_active = false;
  s_framed_mode = false;
  s_session_epoch = 0;
  s_next_tx_sequence = 1;
  s_last_rx_ms = millis();
  s_last_heartbeat_tx_ms = 0;
  s_last_heartbeat_attempt_ms = 0;
  s_last_heartbeat_failure_log_ms = 0;
  s_client_nonce = 0;
  s_last_hello_ms = 0;
  s_client_nonce_valid = false;
  s_parser_reset_pending.store(false, std::memory_order_relaxed);
  s_cdc_tx_cleanup_pending.store(false, std::memory_order_relaxed);
  s_link_down_pending.store(false, std::memory_order_relaxed);
  s_payload_crc_errors.store(0, std::memory_order_relaxed);
  s_cdc_rx_high_water.store(0, std::memory_order_relaxed);
  s_usb_poll_max_gap_ms.store(0, std::memory_order_relaxed);
  s_last_usb_poll_ms = millis();
  s_begun = true;
  deskbot_uplink_bump_transport_generation();
  return true;
}

void usb_transport_poll(void) {
  if (!s_begun) {
    return;
  }
  /*
   * Reject nested polling: the DBOT parser is single-owner state, and a poll
   * re-entered from frame dispatch (historically a factory command executing
   * inside the handler chain) consumes host bytes while the outer parser is
   * parked mid-frame.  Command execution is deferred to loop() now; this
   * guard is a loud tripwire so any future violation fails fast instead of
   * corrupting the parser.  Placed before the supervisor mark on purpose — a
   * rejected nested call must not feed the usb_poll watchdog.
   */
  if (s_poll_in_progress.exchange(true, std::memory_order_acquire)) {
    if (!s_poll_reentry_warned.exchange(true, std::memory_order_acq_rel)) {
      log_error("[USB] usb_transport_poll re-entered; nested call rejected");
    }
    return;
  }
  runtime_supervisor_mark_usb_poll();
  const uint32_t poll_now = millis();
  if (s_last_usb_poll_ms != 0) {
    update_max(s_usb_poll_max_gap_ms,
               static_cast<uint32_t>(poll_now - s_last_usb_poll_ms));
  }
  s_last_usb_poll_ms = poll_now;
  if (s_parser_reset_pending.exchange(false, std::memory_order_acq_rel)) {
    reset_parser();
  }
  /*
   * Deliver link-down callbacks flagged by writer tasks (partial CDC frame
   * failures in send_with_epoch) here on loopTask.  The callback frees PB
   * JSON assembly state and mutates Arduino Strings, which is only safe on
   * the parser task.  A fresh hello consumes the flag itself before starting
   * a new epoch, so a stale flag can never kill a new session.
   */
  if (s_link_down_pending.exchange(false, std::memory_order_acq_rel)) {
    notify_link(false);
  }
  if (s_cdc_tx_cleanup_pending.load(std::memory_order_acquire) &&
      flush_stale_cdc_tx()) {
    s_cdc_tx_cleanup_pending.store(false, std::memory_order_release);
  }
  /*
   * Bound one poll so camera/audio workers cannot starve.  At native USB CDC
   * this is enough to drain normal control and Opus bursts; large PB frames
   * are completed over consecutive polls.
   */
  int available = HWCDCSerial.available();
  if (available > 0) {
    update_max(s_cdc_rx_high_water, static_cast<uint32_t>(available));
  }
  const uint32_t started = micros();
  size_t drained = 0;
  while (available > 0 && drained < 16u * 1024u &&
         static_cast<uint32_t>(micros() - started) < 3000u) {
    const int value = HWCDCSerial.read();
    if (value < 0) {
      break;
    }
    consume_rx_byte(static_cast<uint8_t>(value));
    drained++;
    available = HWCDCSerial.available();
    if (available > 0) {
      update_max(s_cdc_rx_high_water, static_cast<uint32_t>(available));
    }
  }
  /* Never expire an incremental frame while bytes are already queued.  Under
   * display/audio load loopTask can be delayed beyond the idle threshold;
   * checking before drain used to discard a valid continuation. */
  if (available <= 0) {
    parser_idle_tick();
  }
  heartbeat_tick();
  s_poll_in_progress.store(false, std::memory_order_release);
}

bool usb_transport_is_active(void) {
  return s_active;
}

bool usb_transport_framed_mode(void) {
  return s_framed_mode;
}

uint32_t usb_transport_session_epoch(void) {
  return s_session_epoch;
}

uint32_t usb_transport_last_rx_ms(void) {
  return s_last_rx_ms;
}

bool usb_transport_send(DeskbotUsbChannel channel, const uint8_t* payload,
                        size_t payload_length, uint8_t flags) {
  return send_with_epoch(channel, payload, payload_length, flags,
                         s_session_epoch, true);
}

bool usb_transport_send_for_epoch(DeskbotUsbChannel channel,
                                  const uint8_t* payload,
                                  size_t payload_length,
                                  uint32_t expected_epoch, uint8_t flags) {
  return send_with_epoch(channel, payload, payload_length, flags,
                         expected_epoch, true);
}

bool usb_transport_send_control_json(const char* json, uint8_t flags) {
  if (json == nullptr) {
    return false;
  }
  return usb_transport_send(
      DESKBOT_USB_CONTROL_JSON, reinterpret_cast<const uint8_t*>(json),
      strlen(json), flags);
}

bool usb_transport_send_log(const char* text, size_t length) {
  if (text == nullptr || length == 0 || !s_framed_mode || !s_active) {
    return false;
  }
  /*
   * Once a session times out there is no reader to drain CDC.  Drop logs until
   * the next hello instead of making every log call wait on USB backpressure.
   */
  return send_with_epoch(DESKBOT_USB_LOG,
                         reinterpret_cast<const uint8_t*>(text), length,
                         DESKBOT_USB_FLAG_NONE, s_session_epoch, true);
}

void usb_transport_end_session(const char* reason) {
  if (s_active.load(std::memory_order_acquire)) {
    log_warn(
        "[USB] ending session reason=%s epoch=%u last_rx_age=%lums "
        "parser_state=%u parser_bytes=%u/%u",
        reason ? reason : "unspecified",
        static_cast<unsigned>(
            s_session_epoch.load(std::memory_order_relaxed)),
        static_cast<unsigned long>(
            millis() - s_last_rx_ms.load(std::memory_order_relaxed)),
        static_cast<unsigned>(s_parser.state),
        static_cast<unsigned>(s_parser.payload_pos),
        static_cast<unsigned>(s_parser.payload_length));
    /*
     * Tell the host now, while the epoch is still valid, so an idle PC does
     * not need its 15 s heartbeat window to discover the teardown.  Skipped
     * on triggers where the link is already known dead for TX; bounded and
     * never retried otherwise.  Ordered before the s_active exchange because
     * send_with_epoch(require_active=true) refuses to write afterwards.
     */
    if (!session_end_reason_link_dead(reason)) {
      send_session_end_notice(reason);
    }
  }
  const bool was_active =
      s_active.exchange(false, std::memory_order_acq_rel);
  if (was_active) {
    notify_link(false);
  }
  // Parser ownership belongs to usb_transport_poll(); defer its reset when a
  // media task detects link failure. Do not perform a blocking CDC TX flush
  // while the host is absent: Arduino HWCDC may then stop receiving a
  // later hello after that timeout path. The next fresh hello cancels old epoch
  // writers and clears stale TX immediately before sending hello_ack.
  s_parser_reset_pending.store(true, std::memory_order_release);
}
