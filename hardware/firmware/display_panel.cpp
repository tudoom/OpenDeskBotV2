#include "display_panel.h"

#include "common.h"

#include <SPI.h>

void DeskbotDisplay::setRotation(uint8_t r) {
  (void)r;
  Adafruit_ST7789::setRotation(3);
  int16_t xstart = (int16_t)((320 - DESKBOT_DISPLAY_HEIGHT) / 2) +
                   (int16_t)DESKBOT_DISPLAY_ROT3_XSTART_ADJ;
  if (xstart < 0) {
    xstart = 0;
  }
  _xstart = xstart;
  _ystart = DESKBOT_DISPLAY_COL_OFFSET;
  _width  = DESKBOT_DISPLAY_HEIGHT;
  _height = DESKBOT_DISPLAY_WIDTH;
}

void DeskbotDisplay::syncPanelSize() {
  setRotation(0);
}

void DeskbotDisplay::writeRGB565WindowStrided(
    int16_t x, int16_t y, int16_t w, int16_t h, uint16_t* pixels,
    uint16_t stride) {
  if (pixels == nullptr || x < 0 || y < 0 || w <= 0 || h <= 0 ||
      stride < static_cast<uint16_t>(w) || x + w > width() ||
      y + h > height()) {
    return;
  }
  startWrite();
  setAddrWindow(static_cast<uint16_t>(x), static_cast<uint16_t>(y),
                static_cast<uint16_t>(w), static_cast<uint16_t>(h));
  for (int16_t row = 0; row < h; ++row) {
    writePixels(pixels + static_cast<size_t>(row) * stride,
                static_cast<uint32_t>(w), true, false);
  }
  endWrite();
}

void DeskbotDisplay::setupPanel() {
  SPI.begin(DESKBOT_DISPLAY_SCK, -1, DESKBOT_DISPLAY_MOSI, DESKBOT_DISPLAY_CS);
  init(DESKBOT_DISPLAY_WIDTH, DESKBOT_DISPLAY_HEIGHT, SPI_MODE3);
  applyOffsets(DESKBOT_DISPLAY_COL_OFFSET, DESKBOT_DISPLAY_ROW_OFFSET);
  syncPanelSize();
  setSPISpeed(DESKBOT_DISPLAY_SPI_HZ);
  invertDisplay(true);
  uint8_t ctrl = 0x2C, bri = 255;
  sendCommand(0x53, &ctrl, 1);
  sendCommand(0x51, &bri, 1);
  display_backlight_on();
  log_info("[DISPLAY] ST7789P init hw_rot=3 landscape %dx%d xstart=%d adj=%d %luHz invert=1",
           (int)_width, (int)_height, (int)_xstart, (int)DESKBOT_DISPLAY_ROT3_XSTART_ADJ,
           (unsigned long)DESKBOT_DISPLAY_SPI_HZ);
}

void display_log_wiring_required() {
  log_info("[DISPLAY] RST: hardware 3.3V");
  log_info("[DISPLAY] BL: hardware 3.3V");
}

void display_backlight_on() { display_log_wiring_required(); }
