#pragma once

#include "deskbot_config.h"

#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <SPI.h>

#ifndef DESKBOT_DISPLAY_SPI_HZ
/* ST7789 SPI 时钟。40 → 80MHz 让整屏推送耗时减半（284×240×2B 理论
 * 27ms → 13.6ms，同款 v2 板实测可用）。80MHz 是 ESP32-S3 GPIO
 * 矩阵路由下 SPI 主机的上限，**必须在 v2 板实机验证**：若出现花屏、
 * 横纹或偶发错位，在 platformio.ini 对应 env 的 build_flags 里加
 *   -DDESKBOT_DISPLAY_SPI_HZ=40000000
 * 即可回到 0.0.46 及以前的 40MHz（本宏只是默认值，不改代码）。
 * 实际生效值随 `[DISPLAY] ST7789P init … %luHz` 启动日志打出。 */
#define DESKBOT_DISPLAY_SPI_HZ 80000000UL
#endif

#define DESKBOT_DISPLAY_COLOR_BLACK ST77XX_BLACK
#define DESKBOT_DISPLAY_COLOR_WHITE ST77XX_WHITE
#define DESKBOT_DISPLAY_COLOR_YELLOW ST77XX_YELLOW
#define DESKBOT_DISPLAY_COLOR_RED ST77XX_RED
#define DESKBOT_DISPLAY_COLOR_GREEN ST77XX_GREEN
#define DESKBOT_DISPLAY_COLOR_BLUE ST77XX_BLUE

/** ST7789P 240×284 */
class DeskbotDisplay : public Adafruit_ST7789 {
public:
  DeskbotDisplay(int8_t cs, int8_t dc, int8_t rst) : Adafruit_ST7789(cs, dc, rst) {}

  void setupPanel();
  void applyOffsets(int8_t col, int8_t row) { setColRowStart(col, row); }
  void syncPanelSize();
  void setRotation(uint8_t r) override;
  void writeRGB565WindowStrided(int16_t x, int16_t y, int16_t w, int16_t h,
                                uint16_t* pixels, uint16_t stride);
};

void display_log_wiring_required();
void display_backlight_on();
