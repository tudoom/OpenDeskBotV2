#include <Adafruit_GFX.h>
#include <Adafruit_ST7789.h>
#include <Arduino.h>
#include <ESP_I2S.h>
#include <SPI.h>
#include <esp_camera.h>

namespace pin {
constexpr int mic_clk = 1, mic_data = 2;
constexpr int lcd_sck = 4, lcd_mosi = 5, lcd_cs = 6, lcd_dc = 7;
constexpr int servo_x = 15, servo_y = 16;
constexpr int cam_sda = 9, cam_scl = 10, cam_xclk = 14, cam_pclk = 48;
constexpr int cam_vsync = 11, cam_href = 12;
constexpr int cam_y2 = 39, cam_y3 = 18, cam_y4 = 8, cam_y5 = 17;
constexpr int cam_y6 = 38, cam_y7 = 47, cam_y8 = 21, cam_y9 = 13;
constexpr int amp_ctrl = 45, amp_dout = 40, amp_bclk = 41, amp_lrck = 42;
}

class V2Display : public Adafruit_ST7789 {
 public:
  V2Display() : Adafruit_ST7789(pin::lcd_cs, pin::lcd_dc, -1) {}
  void setV2Landscape() {
    Adafruit_ST7789::setRotation(3);
    _xstart = 0;
    _ystart = 0;
  }
};

V2Display lcd;
I2SClass mic;
I2SClass speaker;
int status_line = 0;

void report(const char* name, bool ok, const String& detail) {
  Serial.printf("[V2_PROBE] %-8s %s %s\n", name, ok ? "PASS" : "FAIL", detail.c_str());
  lcd.setTextColor(ok ? ST77XX_GREEN : ST77XX_RED, ST77XX_BLACK);
  lcd.setCursor(8, 44 + status_line * 22);
  lcd.printf("%-7s %s", name, ok ? "PASS" : "FAIL");
  ++status_line;
}

void initDisplay() {
  SPI.begin(pin::lcd_sck, -1, pin::lcd_mosi, pin::lcd_cs);
  lcd.init(240, 284, SPI_MODE3);
  lcd.setSPISpeed(10000000);
  lcd.setV2Landscape();
  lcd.invertDisplay(true);
  lcd.fillScreen(ST77XX_BLACK);
  lcd.setTextSize(2);
  lcd.setTextColor(ST77XX_WHITE);
  lcd.setCursor(8, 10);
  lcd.print("Deskbot V2 self-test");
}

bool testMic() {
  mic.setPinsPdmRx(pin::mic_clk, pin::mic_data);
  if (!mic.begin(I2S_MODE_PDM_RX, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO)) {
    report("MIC", false, "I2S begin failed");
    return false;
  }
  int16_t samples[1024];
  size_t bytes = mic.readBytes(reinterpret_cast<char*>(samples), sizeof(samples));
  int16_t lo = 32767, hi = -32768;
  int64_t sum = 0;
  const size_t count = bytes / sizeof(int16_t);
  for (size_t i = 0; i < count; ++i) {
    lo = min(lo, samples[i]);
    hi = max(hi, samples[i]);
    sum += samples[i];
  }
  const int range = static_cast<int>(hi) - static_cast<int>(lo);
  const bool ok = count > 100 && range > 8;
  report("MIC", ok, String("n=") + count + " range=" + range + " mean=" +
                        (count ? static_cast<long>(sum / count) : 0));
  mic.end();
  return ok;
}

bool testCamera() {
  camera_config_t c = {};
  c.pin_pwdn = -1;
  c.pin_reset = -1;
  c.pin_xclk = pin::cam_xclk;
  c.pin_sccb_sda = pin::cam_sda;
  c.pin_sccb_scl = pin::cam_scl;
  c.pin_d7 = pin::cam_y9;
  c.pin_d6 = pin::cam_y8;
  c.pin_d5 = pin::cam_y7;
  c.pin_d4 = pin::cam_y6;
  c.pin_d3 = pin::cam_y5;
  c.pin_d2 = pin::cam_y4;
  c.pin_d1 = pin::cam_y3;
  c.pin_d0 = pin::cam_y2;
  c.pin_vsync = pin::cam_vsync;
  c.pin_href = pin::cam_href;
  c.pin_pclk = pin::cam_pclk;
  c.xclk_freq_hz = 10000000;
  c.ledc_timer = LEDC_TIMER_0;
  c.ledc_channel = LEDC_CHANNEL_0;
  c.pixel_format = PIXFORMAT_JPEG;
  c.frame_size = FRAMESIZE_QVGA;
  c.jpeg_quality = 15;
  c.fb_count = 1;
  c.fb_location = CAMERA_FB_IN_PSRAM;
  c.grab_mode = CAMERA_GRAB_WHEN_EMPTY;
  esp_err_t err = esp_camera_init(&c);
  if (err != ESP_OK) {
    report("CAMERA", false, String("init=0x") + String(err, HEX));
    return false;
  }
  camera_fb_t* fb = esp_camera_fb_get();
  const bool ok = fb && fb->len > 100;
  report("CAMERA", ok, fb ? String(fb->width) + "x" + fb->height + " jpeg=" + fb->len : "no frame");
  if (fb) esp_camera_fb_return(fb);
  esp_camera_deinit();
  return ok;
}

bool testSpeaker() {
  digitalWrite(pin::amp_ctrl, LOW);
  speaker.setPins(pin::amp_bclk, pin::amp_lrck, pin::amp_dout);
  if (!speaker.begin(I2S_MODE_STD, 16000, I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO)) {
    report("SPEAKER", false, "I2S begin failed");
    return false;
  }
  int16_t frame[320] = {};
  speaker.write(reinterpret_cast<uint8_t*>(frame), sizeof(frame));
  digitalWrite(pin::amp_ctrl, HIGH);
  for (int block = 0; block < 16; ++block) {
    for (int i = 0; i < 160; ++i) {
      const int16_t v = ((i + block * 160) / 20) % 2 ? 1200 : -1200;
      frame[i * 2] = v;
      frame[i * 2 + 1] = v;
    }
    speaker.write(reinterpret_cast<uint8_t*>(frame), sizeof(frame));
  }
  memset(frame, 0, sizeof(frame));
  speaker.write(reinterpret_cast<uint8_t*>(frame), sizeof(frame));
  digitalWrite(pin::amp_ctrl, LOW);
  speaker.end();
  report("SPEAKER", true, "short quiet tone sent");
  return true;
}

void setup() {
  pinMode(pin::amp_ctrl, OUTPUT);
  digitalWrite(pin::amp_ctrl, LOW);
  pinMode(pin::servo_x, INPUT);
  pinMode(pin::servo_y, INPUT);
  Serial.begin(115200);
  delay(1200);
  initDisplay();
  report("MEMORY", ESP.getFlashChipSize() == 16U * 1024U * 1024U && ESP.getPsramSize() >= 8U * 1024U * 1024U,
         String("flash=") + ESP.getFlashChipSize() + " psram=" + ESP.getPsramSize());
  report("LCD", true, "ST7789 mode3 offset0");
  testMic();
  testCamera();
  testSpeaker();
  lcd.setTextColor(ST77XX_WHITE, ST77XX_BLACK);
  lcd.setCursor(8, 210);
  lcd.print("Servo outputs kept OFF");
  lcd.setCursor(8, 234);
  lcd.print("Check serial for detail");
  Serial.println("[V2_PROBE] DONE servo outputs deliberately left high-Z");
}

void loop() {
  digitalWrite(pin::amp_ctrl, LOW);
  delay(1000);
}
