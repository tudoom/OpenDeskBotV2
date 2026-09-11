#pragma once

#include <stddef.h>
#include <driver/gpio.h>

/*
 * USB-only Deskbot firmware configuration.
 *
 * Every unit receives the same firmware image.  Device identity comes from
 * the factory eFuse MAC. This file contains hardware and local media tuning
 * only; no deployment-specific or per-device value belongs in the image.
 */

/* ESP32-S3 USB Serial/JTAG uses fixed native USB pins. */
static constexpr int DESKBOT_USB_DM_GPIO = 19;
static constexpr int DESKBOT_USB_DP_GPIO = 20;
static constexpr bool deskbot_pin_conflicts_with_usb(int pin) {
  return pin == DESKBOT_USB_DM_GPIO || pin == DESKBOT_USB_DP_GPIO;
}

#ifndef DESKBOT_BOARD_V2
#define DESKBOT_BOARD_V2 0
#endif

/* ST7789P 240x284. */
#if DESKBOT_BOARD_V2
#define DESKBOT_DISPLAY_MOSI 5
#define DESKBOT_DISPLAY_SCK 4
#define DESKBOT_DISPLAY_CS 6
#define DESKBOT_DISPLAY_DC 7
#else
#define DESKBOT_DISPLAY_MOSI 9
#define DESKBOT_DISPLAY_SCK 7
#define DESKBOT_DISPLAY_CS 2
#define DESKBOT_DISPLAY_DC 3
#endif

#define DESKBOT_DISPLAY_WIDTH 240
#ifndef DESKBOT_DISPLAY_HEIGHT
#define DESKBOT_DISPLAY_HEIGHT 284
#endif
#ifndef DESKBOT_DISPLAY_ROW_OFFSET
#define DESKBOT_DISPLAY_ROW_OFFSET (DESKBOT_BOARD_V2 ? 0 : 36)
#endif
#ifndef DESKBOT_DISPLAY_COL_OFFSET
#define DESKBOT_DISPLAY_COL_OFFSET 0
#endif
#ifndef DESKBOT_DISPLAY_TOP_SAFE_PX
#define DESKBOT_DISPLAY_TOP_SAFE_PX 4
#endif

#define DESKBOT_PB_COORD_W DESKBOT_DISPLAY_HEIGHT
#define DESKBOT_PB_COORD_H 240
#ifndef DESKBOT_DISPLAY_CANVAS_X0
#define DESKBOT_DISPLAY_CANVAS_X0 \
  ((DESKBOT_DISPLAY_HEIGHT - DESKBOT_PB_COORD_W) / 2)
#endif
#ifndef DESKBOT_DISPLAY_ROT3_XSTART_ADJ
#define DESKBOT_DISPLAY_ROT3_XSTART_ADJ (-18)
#endif

#define DESKBOT_DRAW_W DESKBOT_PB_COORD_W
#define DESKBOT_DRAW_H DESKBOT_PB_COORD_H

/* Servo PWM. */
#ifndef DESKBOT_ROM_X_PIN
/* V2 production mechanics: horizontal servo is wired to SY / GPIO16. */
#define DESKBOT_ROM_X_PIN (DESKBOT_BOARD_V2 ? 16 : 8)
#endif
#ifndef DESKBOT_ROM_Y_PIN
/* V2 production mechanics: vertical servo is wired to SX / GPIO15. */
#define DESKBOT_ROM_Y_PIN (DESKBOT_BOARD_V2 ? 15 : 4)
#endif

#ifndef DESKBOT_AUDIO_PLAY_VOLUME
#define DESKBOT_AUDIO_PLAY_VOLUME 0.85f
#endif

#if DESKBOT_BOARD_V2
#define DESKBOT_SPEAKER_I2S_DOUT GPIO_NUM_40
#define DESKBOT_SPEAKER_I2S_BCLK GPIO_NUM_41
#define DESKBOT_SPEAKER_I2S_WS GPIO_NUM_42
/* Production V2 wiring supplements the released schematic: GPIO45 drives the
 * audio-amplifier enable input. HIGH wakes the amplifier; LOW shuts it down. */
#define DESKBOT_SPEAKER_AMP_CTRL GPIO_NUM_45
#ifndef DESKBOT_SPEAKER_AMP_WAKE_MS
/* Let the external gate/amplifier leave shutdown before the first PCM. */
#define DESKBOT_SPEAKER_AMP_WAKE_MS 20u
#endif
#define DESKBOT_PDM_MIC_CLK GPIO_NUM_1
#define DESKBOT_PDM_MIC_DATA GPIO_NUM_2
#else
#define DESKBOT_SPEAKER_I2S_DOUT GPIO_NUM_1
#define DESKBOT_SPEAKER_I2S_BCLK GPIO_NUM_6
#define DESKBOT_SPEAKER_I2S_WS GPIO_NUM_5
#define DESKBOT_SPEAKER_AMP_CTRL GPIO_NUM_45
#define DESKBOT_PDM_MIC_CLK GPIO_NUM_42
#define DESKBOT_PDM_MIC_DATA GPIO_NUM_41
#endif

#define DESKBOT_ASSERT_USB_SAFE_PIN(pin_name)                              \
  static_assert(!deskbot_pin_conflicts_with_usb(static_cast<int>(pin_name)), \
                #pin_name " conflicts with fixed ESP32-S3 USB D-/D+")
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_DISPLAY_MOSI);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_DISPLAY_SCK);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_DISPLAY_CS);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_DISPLAY_DC);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_ROM_X_PIN);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_ROM_Y_PIN);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_SPEAKER_I2S_DOUT);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_SPEAKER_I2S_BCLK);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_SPEAKER_I2S_WS);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_SPEAKER_AMP_CTRL);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_PDM_MIC_CLK);
DESKBOT_ASSERT_USB_SAFE_PIN(DESKBOT_PDM_MIC_DATA);
#undef DESKBOT_ASSERT_USB_SAFE_PIN

/* Local microphone energy gate; final VAD remains in the PC service. */
#define DESKBOT_PDM_VOICE_MARGIN 320
#define DESKBOT_PDM_VOICE_HANGOVER_MARGIN 200
#define DESKBOT_PDM_VOICE_TRIGGER_RATIO_NUM 130
#define DESKBOT_PDM_VOICE_TRIGGER_RATIO_DEN 100
#define DESKBOT_PDM_VOICE_TRIGGER_FLOOR 140

static inline size_t deskbot_pdm_voice_trigger_thr(size_t ema) {
  const size_t delta = ema + static_cast<size_t>(DESKBOT_PDM_VOICE_MARGIN);
  const size_t ratio =
      (ema * static_cast<size_t>(DESKBOT_PDM_VOICE_TRIGGER_RATIO_NUM)) /
      static_cast<size_t>(DESKBOT_PDM_VOICE_TRIGGER_RATIO_DEN);
  size_t threshold = delta > ratio ? delta : ratio;
  if (threshold < static_cast<size_t>(DESKBOT_PDM_VOICE_TRIGGER_FLOOR)) {
    threshold = static_cast<size_t>(DESKBOT_PDM_VOICE_TRIGGER_FLOOR);
  }
  return threshold;
}

static inline size_t deskbot_pdm_voice_hangover_thr(size_t ema) {
  return ema + static_cast<size_t>(DESKBOT_PDM_VOICE_HANGOVER_MARGIN);
}

#define DESKBOT_PDM_EMA_QUIET_RATIO_NUM 102
#define DESKBOT_PDM_EMA_QUIET_RATIO_DEN 100
#define DESKBOT_PDM_VOICE_TRIGGER_FRAMES 3
#define DESKBOT_PDM_VOICE_THRESHOLD_MAX 24000
#define DESKBOT_PDM_PRE_VOICE_FRAMES 50
#define DESKBOT_PDM_SILENCE_END_MS 650

#define DESKBOT_SPEAKER_AUDIBLE_MEAN_ABS 16

#ifndef DESKBOT_TAIL_SUPPRESS_MS
#define DESKBOT_TAIL_SUPPRESS_MS 300
#endif

/* Expensive AEC lag correlation is a lab diagnostic, never production DSP. */
#ifndef DESKBOT_AEC_CORRELATION_DIAGNOSTICS
#define DESKBOT_AEC_CORRELATION_DIAGNOSTICS 0
#endif

/* A/B-only build switch. Production keeps the proven high-performance AFE;
 * deskbot_v2_low_cost_afe can be flashed for measured CPU/quality comparison. */
#ifndef DESKBOT_AFE_LOW_COST
#define DESKBOT_AFE_LOW_COST 0
#endif

#ifndef DESKBOT_CAMERA_UPLINK_INTERVAL_MS
/*
 * Privacy-first default: the camera stays idle until the service explicitly
 * requests a snapshot or a debug client explicitly enables a stream.
 */
#define DESKBOT_CAMERA_UPLINK_INTERVAL_MS 0
#endif

#ifndef DESKBOT_CAMERA_UPLINK_INTERVAL_DURING_LISTEN_MS
#define DESKBOT_CAMERA_UPLINK_INTERVAL_DURING_LISTEN_MS 2000
#endif

#ifndef DESKBOT_UPLINK_MAX_SEC
#define DESKBOT_UPLINK_MAX_SEC 30
#endif

#ifndef DESKBOT_PB_EXPECT_BIN_TIMEOUT_MS
#define DESKBOT_PB_EXPECT_BIN_TIMEOUT_MS 12000
#endif
