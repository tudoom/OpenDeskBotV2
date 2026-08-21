#ifndef DESKBOT_USB_TRANSPORT_H
#define DESKBOT_USB_TRANSPORT_H

#include <stddef.h>
#include <stdint.h>

/*
 * Deskbot USB CDC framed protocol, version 1.
 *
 * Every integer is little-endian.  A frame is:
 *
 *   24-byte header
 *     magic          u32  0x544f4244 ("DBOT" on the wire)
 *     version        u8   1
 *     header_size    u8   24
 *     channel        u8
 *     flags          u8
 *     sequence       u32
 *     session_epoch  u32
 *     payload_length u32
 *     header_crc32   u32  IEEE CRC-32 of bytes 0..19
 *   payload
 *   payload_crc32    u32  IEEE CRC-32 of payload
 *
 * The header CRC lets the receiver safely resynchronise on the magic marker
 * without trusting a corrupted payload length.  The payload CRC detects USB
 * driver/application truncation and accidental stream pollution.
 */
static constexpr uint32_t DESKBOT_USB_MAGIC = 0x544f4244u;
static constexpr uint8_t DESKBOT_USB_PROTOCOL_VERSION = 1u;
static constexpr uint8_t DESKBOT_USB_HEADER_SIZE = 24u;
static constexpr uint32_t DESKBOT_USB_MAX_PAYLOAD = 1024u * 1024u;

enum DeskbotUsbChannel : uint8_t {
  DESKBOT_USB_CONTROL_JSON = 1,
  DESKBOT_USB_PB_WIRE = 2,
  DESKBOT_USB_AUDIO_UP_OPUS = 3,
  DESKBOT_USB_AUDIO_DOWN_OPUS = 4,
  DESKBOT_USB_CAMERA_JPEG = 5,
  DESKBOT_USB_LOG = 6,
};

enum DeskbotUsbFlags : uint8_t {
  DESKBOT_USB_FLAG_NONE = 0,
  DESKBOT_USB_FLAG_ACK_REQUIRED = 1u << 0,
  DESKBOT_USB_FLAG_ACK = 1u << 1,
  DESKBOT_USB_FLAG_ERROR = 1u << 2,
  DESKBOT_USB_FLAG_END_STREAM = 1u << 3,
  /** PB_WIRE payload is an existing PB JSON envelope, not binary media. */
  DESKBOT_USB_FLAG_JSON = 1u << 4,
  /** Standalone RTC audio payload starts/replaces one playback generation. */
  DESKBOT_USB_FLAG_BEGIN_STREAM = 1u << 5,
  /** Empty AUDIO_DOWN_OPUS frame immediately cancels its active generation. */
  DESKBOT_USB_FLAG_CANCEL_STREAM = 0x40,
};

static_assert(DESKBOT_USB_HEADER_SIZE == 24u,
              "DBOT v1 header layout must remain 24 bytes");
static_assert(DESKBOT_USB_FLAG_JSON == 0x10u,
              "DBOT v1 JSON flag is part of the host protocol");
static_assert(DESKBOT_USB_FLAG_BEGIN_STREAM == 0x20u,
              "DBOT v1 BEGIN flag is part of the host protocol");
static_assert(DESKBOT_USB_FLAG_CANCEL_STREAM == 0x40u,
              "DBOT v1 CANCEL flag is part of the host protocol");

struct DeskbotUsbRxFrame {
  DeskbotUsbChannel channel = DESKBOT_USB_CONTROL_JSON;
  uint8_t flags = DESKBOT_USB_FLAG_NONE;
  uint32_t sequence = 0;
  uint32_t session_epoch = 0;
  const uint8_t* payload = nullptr;
  size_t payload_length = 0;
};

typedef void (*DeskbotUsbFrameHandler)(const DeskbotUsbRxFrame& frame,
                                       void* context);
typedef void (*DeskbotUsbLinkHandler)(bool ready, uint32_t session_epoch,
                                      void* context);

/**
 * Start the physical ESP32-S3 USB Serial/JTAG CDC endpoint.
 *
 * Call this at the very beginning of setup(), before display/audio/camera
 * initialisation.  RX/TX queues are enlarged before HWCDC begins so a PC hello
 * which arrives during the slower peripheral boot is retained.  The function
 * is idempotent; false means the preferred queues could not be allocated, but
 * HWCDC still starts with its framework fallback buffers.
 */
bool usb_transport_cdc_begin(void);

/**
 * Initialise the DBOT parser and callbacks after every command target is
 * ready. Safe to call once during setup(); physical CDC must already be up.
 */
bool usb_transport_begin(DeskbotUsbFrameHandler frame_handler,
                         DeskbotUsbLinkHandler link_handler = nullptr,
                         void* context = nullptr);

/**
 * Drain currently available CDC bytes and service heartbeat/session timeout.
 * This function never blocks waiting for input and must be called frequently
 * from the main loop (including long-running voice loops).
 */
void usb_transport_poll(void);

/** True only after a valid hello and while the host heartbeat is alive. */
bool usb_transport_is_active(void);

/**
 * True forever after the first valid hello.  Logger uses this to ensure that
 * raw text is never mixed into the framed byte stream after activation.
 */
bool usb_transport_framed_mode(void);

uint32_t usb_transport_session_epoch(void);
uint32_t usb_transport_last_rx_ms(void);

/**
 * Send one framed payload in the current session.  Returns false when there
 * is no active session, the payload violates channel limits, or CDC write
 * cannot complete before its bounded deadline.
 */
bool usb_transport_send(DeskbotUsbChannel channel, const uint8_t* payload,
                         size_t payload_length,
                         uint8_t flags = DESKBOT_USB_FLAG_NONE);

/**
 * Send only if the caller's captured session epoch is still current.
 * Media producers use this to prevent an old request crossing a reconnect.
 */
bool usb_transport_send_for_epoch(
    DeskbotUsbChannel channel, const uint8_t* payload, size_t payload_length,
    uint32_t expected_epoch, uint8_t flags = DESKBOT_USB_FLAG_NONE);
bool usb_transport_send_control_json(
    const char* json, uint8_t flags = DESKBOT_USB_FLAG_NONE);
bool usb_transport_send_log(const char* text, size_t length);

/** Invalidate the current session and notify the link callback exactly once. */
void usb_transport_end_session(const char* reason = nullptr);

/** Standard IEEE CRC-32, exposed for host/firmware protocol tests. */
uint32_t usb_transport_crc32(const uint8_t* data, size_t length);

#endif  // DESKBOT_USB_TRANSPORT_H
