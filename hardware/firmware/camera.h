#pragma once

#include <stdint.h>

#ifndef DESKBOT_CAMERA_DIAGNOSTICS
#define DESKBOT_CAMERA_DIAGNOSTICS 0
#endif

enum class CameraBootProbeStage : uint8_t {
  kBeforeServo = 0,
  kAfterServo = 1,
};

struct CameraHealthSnapshot {
  bool ready = false;
  uint32_t last_frame_ms = 0;
  uint32_t last_upload_ms = 0;
  uint32_t capture_failures = 0;
  uint32_t transport_failures = 0;
  uint32_t init_failures = 0;
  uint32_t recovery_attempts = 0;
  uint32_t recovery_successes = 0;
  uint32_t pending_snapshots = 0;
  uint32_t interval_ms = 0;
};

/** Initialise the OV2640-compatible camera. */
bool setup_camera();

/**
 * Capture one diagnostic frame and retain the result for replay after the USB
 * session is active. esp_camera_fb_get() has a fixed ~4 s driver timeout.
 */
bool camera_boot_probe(CameraBootProbeStage stage);

/** Start the camera supervisor and on-demand USB CDC JPEG uploader. */
void task_setup_camera();

/** Set the PC-lease preview cadence; fps==0 stops continuous streaming. */
void camera_set_fps(uint32_t fps);

/** Queue one fresh JPEG upload without enabling continuous camera streaming. */
void camera_request_snapshot();

/** Clear session-scoped stream/snapshot requests at every USB link boundary. */
void camera_reset_session_state();

/** Lock-free camera telemetry suitable for hello/heartbeat reporting. */
CameraHealthSnapshot camera_health_snapshot();
