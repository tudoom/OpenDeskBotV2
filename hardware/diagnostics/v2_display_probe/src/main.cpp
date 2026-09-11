#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Arduino.h>
#include <SPI.h>

namespace {

constexpr int kLcdSck = 4;
constexpr int kLcdMosi = 5;
constexpr int kLcdCs = 6;
constexpr int kLcdDc = 7;
constexpr int kLcdReset = -1;  // V2 routes RESX to a hardware pull-up option.
constexpr int kAmpCtrl = 45;   // Active high; keep muted in display-only probe.

constexpr uint16_t kPanelWidth = 240;
constexpr uint16_t kPanelHeight = 284;
constexpr uint32_t kProbeSpiHz = 10000000UL;

class DeskbotV2Display : public Adafruit_ST7789 {
 public:
  DeskbotV2Display(int8_t cs, int8_t dc, int8_t rst)
      : Adafruit_ST7789(cs, dc, rst) {}

  void setV2Landscape() {
    Adafruit_ST7789::setRotation(3);
    /*
     * Adafruit centers a generic 240x284 glass inside ST7789's 240x320 RAM,
     * producing an 18-pixel landscape X offset.  The T183B7-C12-04 module
     * maps its visible area from RAM origin zero, so that generic offset
     * leaves the left edge untouched and visibly white.
     */
    _xstart = 0;
    _ystart = 0;
  }
};

DeskbotV2Display display(kLcdCs, kLcdDc, kLcdReset);

void draw_probe_screen() {
  display.fillScreen(ST77XX_BLACK);

  const int16_t width = display.width();
  const int16_t height = display.height();
  const int16_t stripe_width = width / 6;
  const uint16_t colors[] = {
      ST77XX_RED,
      ST77XX_YELLOW,
      ST77XX_GREEN,
      ST77XX_CYAN,
      ST77XX_BLUE,
      ST77XX_MAGENTA,
  };
  for (size_t i = 0; i < sizeof(colors) / sizeof(colors[0]); ++i) {
    const int16_t x = static_cast<int16_t>(i) * stripe_width;
    const int16_t next =
        i + 1 == sizeof(colors) / sizeof(colors[0])
            ? width
            : static_cast<int16_t>(i + 1) * stripe_width;
    display.fillRect(x, 0, next - x, 36, colors[i]);
  }

  // Corner fiducials expose clipping/offsets without creating a deliberate
  // full-height white line that can be mistaken for a panel defect.
  constexpr int16_t kMarker = 12;
  display.drawFastHLine(0, 0, kMarker, ST77XX_WHITE);
  display.drawFastVLine(0, 0, kMarker, ST77XX_WHITE);
  display.drawFastHLine(width - kMarker, 0, kMarker, ST77XX_WHITE);
  display.drawFastVLine(width - 1, 0, kMarker, ST77XX_WHITE);
  display.drawFastHLine(0, height - 1, kMarker, ST77XX_WHITE);
  display.drawFastVLine(0, height - kMarker, kMarker, ST77XX_WHITE);
  display.drawFastHLine(width - kMarker, height - 1, kMarker, ST77XX_WHITE);
  display.drawFastVLine(width - 1, height - kMarker, kMarker, ST77XX_WHITE);
  display.setTextWrap(false);
  display.setTextColor(ST77XX_WHITE, ST77XX_BLACK);
  display.setTextSize(3);
  display.setCursor(22, 72);
  display.print("Deskbot V2");
  display.setTextSize(2);
  display.setCursor(22, 116);
  display.print("ST7789 240x284");
  display.setCursor(22, 146);
  display.print("SPI 4/5/6/7");
  display.setTextSize(1);
  display.setCursor(22, 184);
  display.print("If this is readable, LCD wiring is OK.");

  for (int16_t x = 20; x < width - 20; x += 12) {
    display.drawFastVLine(x, height - 30, 12, ST77XX_GREEN);
  }
}

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println();
  Serial.println("[V2_DISPLAY] boot");
  pinMode(kAmpCtrl, OUTPUT);
  digitalWrite(kAmpCtrl, LOW);
  Serial.printf("[V2_DISPLAY] amp_ctrl=%d level=LOW (disabled)\n", kAmpCtrl);
  Serial.printf(
      "[V2_DISPLAY] pins sck=%d mosi=%d cs=%d dc=%d rst=hardware\n",
      kLcdSck,
      kLcdMosi,
      kLcdCs,
      kLcdDc);
  Serial.printf(
      "[V2_DISPLAY] flash=%u psram=%u\n",
      static_cast<unsigned>(ESP.getFlashChipSize()),
      static_cast<unsigned>(ESP.getPsramSize()));

  pinMode(kLcdCs, OUTPUT);
  digitalWrite(kLcdCs, HIGH);
  pinMode(kLcdDc, OUTPUT);
  digitalWrite(kLcdDc, HIGH);

  SPI.begin(kLcdSck, -1, kLcdMosi, kLcdCs);
  display.init(kPanelWidth, kPanelHeight, SPI_MODE3);
  display.setSPISpeed(kProbeSpiHz);
  display.setV2Landscape();
  display.invertDisplay(true);

  Serial.printf(
      "[V2_DISPLAY] initialized logical=%dx%d spi=%u mode=3 invert=1\n",
      display.width(),
      display.height(),
      static_cast<unsigned>(kProbeSpiHz));

  display.fillScreen(ST77XX_RED);
  delay(250);
  display.fillScreen(ST77XX_GREEN);
  delay(250);
  display.fillScreen(ST77XX_BLUE);
  delay(250);
  draw_probe_screen();
  Serial.println("[V2_DISPLAY] test pattern ready");
}

void loop() {
  static uint32_t last_log_ms = 0;
  static bool marker = false;
  const uint32_t now = millis();
  if (now - last_log_ms >= 2000U) {
    last_log_ms = now;
    marker = !marker;
    display.fillCircle(
        display.width() - 14,
        display.height() - 14,
        5,
        marker ? ST77XX_GREEN : ST77XX_RED);
    Serial.printf("[V2_DISPLAY] alive uptime_ms=%lu\n",
                  static_cast<unsigned long>(now));
  }
  delay(10);
}
