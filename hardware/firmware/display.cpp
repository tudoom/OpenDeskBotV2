#include "display.h"
#include "audio_player.h"
#include <atomic>
#include "display_text.h"

#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

#include <ArduinoJson.h>
#include <JPEGDEC.h>
#include <cstdint>
#include <cmath>
#include <cstring>
#include "esp_heap_caps.h"
#include "pb_timeline.h"

DeskbotDisplay g_display(DESKBOT_DISPLAY_CS, DESKBOT_DISPLAY_DC, -1);

/* ── PSRAM 帧缓冲 ──
 * PB 矢量动画先在 PSRAM 里绘制（速度快，0 SPI 流量），完成后整帧 DMA 推送。
 * 效果：消除逐像素撕裂，动画平滑；若 ps_malloc 失败则回退直写模式。
 */
class PsramCanvas16 : public GFXcanvas16 {
public:
  PsramCanvas16(uint16_t w, uint16_t h) : GFXcanvas16(w, h, /*alloc=*/false) {
    buffer = (uint16_t*)ps_malloc((uint32_t)w * h * 2u);
    if (buffer) memset(buffer, 0, (uint32_t)w * h * 2u);
  }
  ~PsramCanvas16() { if (buffer) { free(buffer); buffer = nullptr; } }
};

static PsramCanvas16* s_canvas   = nullptr;
static uint16_t* s_last_panel_frame = nullptr;
static bool s_last_panel_frame_valid = false;
static Adafruit_GFX*  s_draw_gfx = &g_display;   /* 渲染目标：canvas 或直写面板 */
static std::atomic<uint32_t> s_render_cancel_epoch{0};
static std::atomic<bool> s_reset_interp_on_next_pb{false};
static uint32_t s_active_render_epoch = 0;  // display task only

/* 待机屏状态（产品需求：未连接 PC 服务时显示默认表情 + 「请连接PC服务」）。
 * display_standby_set() 可从任意任务调用，只翻转这两个原子标记；真正的绘制
 * 只发生在 display worker 的空闲 tick 里，保持渲染单一所有者原则。 */
static std::atomic<bool> s_standby_active{false};
static std::atomic<bool> s_standby_dirty{false};

static bool display_render_cancelled() {
  return s_active_render_epoch != s_render_cancel_epoch.load(std::memory_order_acquire);
}

static bool display_wait_for_pb_start(uint32_t start_at_ms) {
  if (start_at_ms == 0) {
    return true;
  }
  while (true) {
    if (display_render_cancelled()) {
      return false;
    }
    const uint32_t remaining =
        deskbot_pb_time_remaining(millis(), start_at_ms);
    if (remaining == 0) {
      return true;
    }
    const uint32_t slice_ms = remaining > 5u ? 5u : remaining;
    vTaskDelay(pdMS_TO_TICKS(slice_ms));
  }
}

/** canvas 整帧单次 SPI 推送（横屏 canvas 在 x=CANVAS_X0 对齐）。 */
static inline void pb_canvas_push() {
  if (!s_canvas || !s_canvas->getBuffer()) {
    return;
  }
  uint16_t* current = s_canvas->getBuffer();
  constexpr int16_t width = DESKBOT_PB_COORD_W;
  constexpr int16_t height = DESKBOT_PB_COORD_H;
  constexpr size_t pixels = static_cast<size_t>(width) * height;

  if (!s_last_panel_frame || !s_last_panel_frame_valid) {
    g_display.writeRGB565WindowStrided(
        DESKBOT_DISPLAY_CANVAS_X0, 0, width, height, current, width);
    if (s_last_panel_frame) {
      memcpy(s_last_panel_frame, current, pixels * sizeof(uint16_t));
      s_last_panel_frame_valid = true;
    }
    return;
  }

  int16_t min_x = width;
  int16_t min_y = height;
  int16_t max_x = -1;
  int16_t max_y = -1;
  for (int16_t y = 0; y < height; ++y) {
    const size_t row = static_cast<size_t>(y) * width;
    for (int16_t x = 0; x < width; ++x) {
      const size_t index = row + static_cast<size_t>(x);
      if (current[index] == s_last_panel_frame[index]) {
        continue;
      }
      if (x < min_x) min_x = x;
      if (x > max_x) max_x = x;
      if (y < min_y) min_y = y;
      if (y > max_y) max_y = y;
    }
  }
  if (max_x < min_x || max_y < min_y) {
    return;
  }

  const int16_t dirty_w = max_x - min_x + 1;
  const int16_t dirty_h = max_y - min_y + 1;
  const size_t dirty_pixels = static_cast<size_t>(dirty_w) * dirty_h;
  if (dirty_pixels * 4U >= pixels * 3U) {
    min_x = 0;
    min_y = 0;
    max_x = width - 1;
    max_y = height - 1;
  }

  const int16_t send_w = max_x - min_x + 1;
  const int16_t send_h = max_y - min_y + 1;
  g_display.writeRGB565WindowStrided(
      DESKBOT_DISPLAY_CANVAS_X0 + min_x, min_y, send_w, send_h,
      current + static_cast<size_t>(min_y) * width + min_x, width);
  for (int16_t y = min_y; y <= max_y; ++y) {
    const size_t offset = static_cast<size_t>(y) * width + min_x;
    memcpy(s_last_panel_frame + offset, current + offset,
           static_cast<size_t>(send_w) * sizeof(uint16_t));
  }
}

/* canvas 文字叠写：首次调用先刷黑背景，后续调用直接在上面叠加 */
static bool s_canvas_text_bg = false;

static void display_canvas_text_reset() {
  s_canvas_text_bg = false;
}

void display_boot_screen_reset() { display_canvas_text_reset(); }

static constexpr uint8_t kDisplayBootTextSize = DESKBOT_DISPLAY_BOOT_TEXT_SIZE;

void display_boot_header_lines(char* line1, size_t line1_len, char* line2, size_t line2_len) {
  snprintf(line1, line1_len, "%s v%s", PRODUCT_NAME, VERSION);
  snprintf(line2, line2_len, "device_id: %s", get_device_id());
}

static void display_boot_draw_lines(const char* line1, const char* line2, const char* line3,
                                 const char* line4 = nullptr) {
  display_boot_screen_reset();
  const int16_t x0 = DESKBOT_DISPLAY_BOOT_SX;
  const int16_t y0 = DESKBOT_DISPLAY_BOOT_SY0;
  const int16_t dy = display_text_line_height(kDisplayBootTextSize);

  Adafruit_GFX* target = (s_canvas && s_canvas->getBuffer()) ? static_cast<Adafruit_GFX*>(s_canvas)
                                                             : static_cast<Adafruit_GFX*>(&g_display);
  if (s_canvas && s_canvas->getBuffer()) {
    s_canvas->fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
    s_canvas_text_bg = true;
  } else {
    g_display.fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
  }

  int16_t row = 0;
  auto draw_row = [&](const char* text) {
    if (!text || text[0] == '\0') {
      return;
    }
    display_text_draw(target, x0, y0 + row * dy, text, kDisplayBootTextSize, DESKBOT_DISPLAY_COLOR_WHITE);
    row++;
  };
  draw_row(line1);
  draw_row(line2);
  draw_row(line3);
  draw_row(line4);

  if (s_canvas && s_canvas->getBuffer()) {
    pb_canvas_push();
  }
  display_flush_timed();
}

void display_boot_show(const char* status_line3, const char* status_line4) {
  char line1[48];
  char line2[40];
  display_boot_header_lines(line1, sizeof(line1), line2, sizeof(line2));
  display_boot_draw_lines(line1, line2, status_line3, status_line4);
}

static void display_canvas_ensure_black() {
  if (s_canvas && s_canvas->getBuffer() && !s_canvas_text_bg) {
    s_canvas->fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
    s_canvas_text_bg = true;
  }
}

void display_clear_timed() { g_display.fillScreen(DESKBOT_DISPLAY_COLOR_BLACK); }

void display_flush_timed() { /* TFT 直写显存，无需 display() */ }

static int16_t s_display_text_sx = 8;
static int16_t s_display_text_sy = 8;
static constexpr int16_t kDisplayTextServerLineDy = 14;

void setup_display() {
  g_display.setupPanel();
  if (g_display.width() <= 0 || g_display.height() <= 0) {
    log_error("[DISPLAY] setup_display panel size invalid w=%d h=%d", (int)g_display.width(),
              (int)g_display.height());
  }

  s_canvas = new PsramCanvas16(DESKBOT_DRAW_W, DESKBOT_DRAW_H);
  if (s_canvas && s_canvas->getBuffer()) {
    s_draw_gfx = s_canvas;
    log_info("[DISPLAY] PSRAM canvas %dx%d ok (%.0f KB)",
             DESKBOT_DRAW_W, DESKBOT_DRAW_H,
             (float)(DESKBOT_DRAW_W * DESKBOT_DRAW_H * 2) / 1024.f);
    s_last_panel_frame = static_cast<uint16_t*>(heap_caps_malloc(
        static_cast<size_t>(DESKBOT_DRAW_W) * DESKBOT_DRAW_H *
            sizeof(uint16_t),
        MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (s_last_panel_frame) {
      s_last_panel_frame_valid = false;
      log_info("[DISPLAY] dirty-frame shadow allocated in PSRAM");
    } else {
      log_warn("[DISPLAY] dirty-frame shadow unavailable; using full frames");
    }
  } else {
    log_error("[DISPLAY] PSRAM canvas alloc failed, fallback direct-write (anim may tear)");
    delete s_canvas;
    s_canvas = nullptr;
    s_draw_gfx = &g_display;
  }

  g_display.fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
  display_canvas_text_reset();
  log_info("[DISPLAY] ready ST7789 %dx%d off=%d,%d SPI mosi=%d sck=%d cs=%d dc=%d",
           (int)g_display.width(), (int)g_display.height(), DESKBOT_DISPLAY_COL_OFFSET,
           DESKBOT_DISPLAY_ROW_OFFSET, DESKBOT_DISPLAY_MOSI, DESKBOT_DISPLAY_SCK, DESKBOT_DISPLAY_CS,
           DESKBOT_DISPLAY_DC);
  g_display.setTextSize(kDisplayBootTextSize);
  g_display.setTextColor(DESKBOT_DISPLAY_COLOR_WHITE, DESKBOT_DISPLAY_COLOR_BLACK);
  display_boot_show("初始化中...", nullptr);
}

/* display_print/println：boot 阶段 banner；ROTATION=3 时用服务端 280×240 坐标 */
void display_text_layout_reset(int16_t sx, int16_t sy) {
  s_display_text_sx = sx;
  s_display_text_sy = sy;
}

void display_println_server(int16_t sx, int16_t sy, String text, int delay_time) {
  /* 禁止直写 SPI 逐字：会引起顶栏渐变花屏。
   * 改在 canvas 上画完再整帧推送（单次 setAddrWindow + 大块写）。 */
  if (s_canvas && s_canvas->getBuffer()) {
    display_canvas_ensure_black();
    display_text_draw(s_canvas, sx, sy, text.c_str(), 2, DESKBOT_DISPLAY_COLOR_WHITE);
    pb_canvas_push();
    display_flush_timed();
    vTaskDelay(pdMS_TO_TICKS(delay_time));
    return;
  }

  /* 无 canvas 时降级直写 */
  g_display.setCursor(sx, sy);
  g_display.println(text);
  display_flush_timed();
  vTaskDelay(pdMS_TO_TICKS(delay_time));
}

void display_print(String text, int delay_time) {
  g_display.setCursor(s_display_text_sx, s_display_text_sy);
  g_display.print(text);
  display_flush_timed();
  vTaskDelay(pdMS_TO_TICKS(delay_time));
}

void display_println(String text, int delay_time) {
  display_println_server(s_display_text_sx, s_display_text_sy, text, delay_time);
  s_display_text_sy += kDisplayTextServerLineDy;
}

namespace {

/* pb 矢量帧：同层同下标且 shape 一致时在 anim[k].ms 内按 t 插值；否则画本帧。 */
static constexpr uint32_t kDisplayVoiceFramePeriodMs = 100;  // 10 FPS
static constexpr uint32_t kDisplayIdleFramePeriodMs = 67;    // about 15 FPS
static constexpr uint8_t  kPbMaxPrimsPerLayer   = 16;
/** text 图元：服务端预换行后下发；单行 UTF-8 按字节截断（约 42 个汉字）。 */
static constexpr size_t kPbMaxTextChars = 128;
static constexpr uint8_t kDisplayMaxPbAssets = 8;
static constexpr uint16_t kPbDefaultPrimColor = 65535u;

static uint32_t pb_display_frame_period_ms() {
  return audio_play_stream_pcm_active() ? kDisplayVoiceFramePeriodMs
                                        : kDisplayIdleFramePeriodMs;
}

/* pb 图元：与服务端 anim[] 实际下发的 shape 对齐。 */
enum class PbShape : uint8_t {
  None = 0,
  Rect,
  RectOutline,
  Circle,
  CircleOutline,
  Line,
  Pixel,
  HLine,
  VLine,
  Ellipse,
  EllipseFill,
  Triangle,
  TriangleFill,
  RoundRect,
  RoundRectOutline,
  RotatedRectOutline,
  RotatedRectFill,
  Text,
  Image,
};

struct StoredPrim {
  PbShape shape;
  uint16_t color; /* RGB565；图元字段 c/color，缺省为白 */
  int16_t x;
  int16_t y;
  int16_t w;
  int16_t h;
  int16_t r;
  int16_t x1;
  int16_t y1;
  int16_t x2;
  int16_t y2;
  int16_t rot_cx;
  int16_t rot_cy;
  int16_t angle_deg;
  uint8_t stroke_width;
  uint8_t text_size; /* 仅 Text：1–3，与 setTextSize 一致 */
  char    text[kPbMaxTextChars + 1];
  uint8_t asset_index; /* 仅 Image：assets[] 下标 */
};

struct StoredLayer {
  uint8_t     count;
  StoredPrim  prims[kPbMaxPrimsPerLayer];
};

static StoredLayer s_prev_bg{};
static StoredLayer s_prev_nose{};
static StoredLayer s_prev_mouth{};
static StoredLayer s_prev_eye_l{};
static StoredLayer s_prev_eye_r{};
static StoredLayer s_prev_extra{};
static bool        s_have_prev = false;
static uint16_t    s_prev_frame_bg = DESKBOT_DISPLAY_COLOR_BLACK;

/** pb 矢量解析用的「当前帧」图层；放静态区避免 display_render 任务栈过大。仅渲染任务串行访问。 */
static StoredLayer s_pb_curr_bg{};
static StoredLayer s_pb_curr_nose{};
static StoredLayer s_pb_curr_mouth{};
static StoredLayer s_pb_curr_eye_l{};
static StoredLayer s_pb_curr_eye_r{};
static StoredLayer s_pb_curr_extra{};

/*
 * The RTC mouth overlay needs an immutable copy of the current face while
 * audio is playing.  Six StoredLayer globals used to reserve about 15 KiB of
 * scarce internal DRAM before setup() even ran, leaving too little contiguous
 * DMA memory for esp32-camera.  Keep the exact same snapshot semantics, but
 * allocate the snapshot lazily from PSRAM after the camera has initialised.
 * Only the display task accesses this storage.
 */
struct VoiceBaseStorage {
  StoredLayer bg{};
  StoredLayer nose{};
  StoredLayer mouth{};
  StoredLayer eye_l{};
  StoredLayer eye_r{};
  StoredLayer extra{};
  uint16_t frame_bg = DESKBOT_DISPLAY_COLOR_BLACK;
};
static VoiceBaseStorage* s_voice_base = nullptr;
static std::atomic<uint8_t> s_voice_mouth_level{0};
static std::atomic<bool> s_voice_mouth_active{false};
static std::atomic<bool> s_voice_mouth_allowed{false};
static bool s_voice_base_valid = false;
static uint8_t s_voice_last_drawn_level = UINT8_MAX;
static uint32_t s_voice_last_refresh_ms = 0;

struct DisplayPbAssetBlob {
  uint8_t* data = nullptr;
  size_t   len  = 0;
};
static DisplayPbAssetBlob s_render_assets[kDisplayMaxPbAssets]{};
static uint8_t         s_render_asset_count = 0;

struct JpegBlitCtx {
  uint16_t*     canvas_buf;
  int16_t       canvas_w;
  int16_t       canvas_h;
  int16_t       dx;
  int16_t       dy;
  int16_t       dw;
  int16_t       dh;
  int           iw;
  int           ih;
};
/** JPEG 解码器放静态区：JPEGDEC 解码 284×240 时栈帧较大，避免 display_render 栈溢出。 */
static JPEGDEC     s_jpeg_dec;
static JpegBlitCtx s_jpeg_blit_ctx{};

static void pb_vector_interp_reset() {
  s_have_prev = false;
  s_prev_frame_bg = DESKBOT_DISPLAY_COLOR_BLACK;
  memset(&s_prev_bg, 0, sizeof(s_prev_bg));
  memset(&s_prev_nose, 0, sizeof(s_prev_nose));
  memset(&s_prev_mouth, 0, sizeof(s_prev_mouth));
  memset(&s_prev_eye_l, 0, sizeof(s_prev_eye_l));
  memset(&s_prev_eye_r, 0, sizeof(s_prev_eye_r));
  memset(&s_prev_extra, 0, sizeof(s_prev_extra));
}

static void display_free_render_assets() {
  for (uint8_t i = 0; i < s_render_asset_count; i++) {
    if (s_render_assets[i].data) {
      heap_caps_free(s_render_assets[i].data);
      s_render_assets[i].data = nullptr;
    }
    s_render_assets[i].len = 0;
  }
  s_render_asset_count = 0;
}

static bool pb_image_asset_available(uint8_t asset_index) {
  if (asset_index >= s_render_asset_count || !s_canvas ||
      !s_canvas->getBuffer() || s_draw_gfx != s_canvas) {
    return false;
  }
  const DisplayPbAssetBlob& blob = s_render_assets[asset_index];
  return blob.data && blob.len >= 4 && blob.data[0] == 0xFFu &&
         blob.data[1] == 0xD8u;
}

static void layer_drop_images(StoredLayer* layer) {
  uint8_t kept = 0;
  for (uint8_t i = 0; i < layer->count; ++i) {
    if (layer->prims[i].shape == PbShape::Image) {
      continue;
    }
    if (kept != i) {
      layer->prims[kept] = layer->prims[i];
    }
    ++kept;
  }
  layer->count = kept;
}

static void pb_drop_inherited_images() {
  layer_drop_images(&s_prev_bg);
  layer_drop_images(&s_prev_nose);
  layer_drop_images(&s_prev_mouth);
  layer_drop_images(&s_prev_eye_l);
  layer_drop_images(&s_prev_eye_r);
  layer_drop_images(&s_prev_extra);
}

static int lerp_i16(int16_t a, int16_t b, float t) {
  return (int)lroundf((1.f - t) * (float)a + t * (float)b);
}

static void layer_clear(StoredLayer* L) {
  L->count = 0;
}

/** 从 JSON 读取 RGB565（低 16 位）；缺省 default_rgb565。 */
static uint16_t pb_json_rgb565_field(JsonObjectConst obj, const char* key, uint16_t default_rgb565) {
  if (obj[key].isNull()) {
    return default_rgb565;
  }
  if (obj[key].is<uint32_t>()) {
    return (uint16_t)(obj[key].as<uint32_t>() & 0xFFFFu);
  }
  if (obj[key].is<int>()) {
    return (uint16_t)((uint32_t)obj[key].as<int>() & 0xFFFFu);
  }
  if (obj[key].is<double>()) {
    return (uint16_t)((uint32_t)(int)obj[key].as<double>() & 0xFFFFu);
  }
  return default_rgb565;
}

static uint16_t pb_json_prim_color(JsonObjectConst it) {
  if (!it["c"].isNull()) {
    return pb_json_rgb565_field(it, "c", kPbDefaultPrimColor);
  }
  if (!it["color"].isNull()) {
    return pb_json_rgb565_field(it, "color", kPbDefaultPrimColor);
  }
  return kPbDefaultPrimColor;
}

static int16_t pb_json_angle(JsonObjectConst it) {
  if (!it["rotation"].isNull()) {
    return (int16_t)lroundf(it["rotation"].as<float>());
  }
  if (!it["angle"].isNull()) {
    return (int16_t)lroundf(it["angle"].as<float>());
  }
  return 0;
}

static void pb_json_rotation(JsonObjectConst it, StoredPrim* p,
                             int16_t default_cx, int16_t default_cy) {
  p->angle_deg = pb_json_angle(it);
  p->rot_cx = !it["rot_cx"].isNull()
                  ? (int16_t)lroundf(it["rot_cx"].as<float>())
                  : !it["cx"].isNull()
                        ? (int16_t)lroundf(it["cx"].as<float>())
                        : default_cx;
  p->rot_cy = !it["rot_cy"].isNull()
                  ? (int16_t)lroundf(it["rot_cy"].as<float>())
                  : !it["cy"].isNull()
                        ? (int16_t)lroundf(it["cy"].as<float>())
                        : default_cy;
}

static bool json_fill_layer(JsonArrayConst arr, StoredLayer* out) {
  layer_clear(out);
  if (arr.isNull()) {
    return true;
  }
  if (arr.size() > kPbMaxPrimsPerLayer) {
    log_error("[DISPLAY] layer has %u primitives; device limit is %u",
              (unsigned)arr.size(), (unsigned)kPbMaxPrimsPerLayer);
    return false;
  }
  for (JsonObjectConst it : arr) {
    StoredPrim& p = out->prims[out->count];
    memset(&p, 0, sizeof(p));
    p.color = pb_json_prim_color(it);
    int stroke_width = !it["stroke_width"].isNull()
                           ? it["stroke_width"].as<int>()
                           : (it["sw"] | 1);
    p.stroke_width = (uint8_t)constrain(stroke_width, 1, 12);
    const char* shape = it["shape"] | "";
    if (strcmp(shape, "rect") == 0 || strcmp(shape, "rect_fill") == 0 ||
        strcmp(shape, "fill_rect") == 0 || strcmp(shape, "fillRect") == 0) {
      p.shape = PbShape::Rect;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      if (p.w < 1 || p.h < 1) continue;
      pb_json_rotation(it, &p, (int16_t)(p.x + p.w / 2),
                       (int16_t)(p.y + p.h / 2));
    } else if (strcmp(shape, "rect_outline") == 0 || strcmp(shape, "draw_rect") == 0 ||
               strcmp(shape, "drawRect") == 0) {
      p.shape = PbShape::RectOutline;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      if (p.w < 1 || p.h < 1) continue;
      pb_json_rotation(it, &p, (int16_t)(p.x + p.w / 2),
                       (int16_t)(p.y + p.h / 2));
    } else if (strcmp(shape, "circle") == 0 || strcmp(shape, "circle_fill") == 0 ||
               strcmp(shape, "fill_circle") == 0 || strcmp(shape, "fillCircle") == 0) {
      p.shape = PbShape::Circle;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.r = (int16_t)(it["r"] | 0);
      if (p.r < 1) continue;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "circle_outline") == 0 || strcmp(shape, "draw_circle") == 0 ||
               strcmp(shape, "drawCircle") == 0) {
      p.shape = PbShape::CircleOutline;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.r = (int16_t)(it["r"] | 0);
      if (p.r < 1) continue;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "line") == 0 || strcmp(shape, "drawLine") == 0) {
      p.shape = PbShape::Line;
      p.x1 = (int16_t)(it["x1"] | 0);
      p.y1 = (int16_t)(it["y1"] | 0);
      p.x2 = (int16_t)(it["x2"] | 0);
      p.y2 = (int16_t)(it["y2"] | 0);
      pb_json_rotation(it, &p, (int16_t)((p.x1 + p.x2) / 2),
                       (int16_t)((p.y1 + p.y2) / 2));
    } else if (strcmp(shape, "pixel") == 0 || strcmp(shape, "point") == 0 ||
               strcmp(shape, "drawPixel") == 0) {
      p.shape = PbShape::Pixel;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "hline") == 0 || strcmp(shape, "h_line") == 0 ||
               strcmp(shape, "drawFastHLine") == 0) {
      p.shape = PbShape::HLine;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      if (p.w < 1) continue;
      pb_json_rotation(it, &p, (int16_t)(p.x + (p.w - 1) / 2), p.y);
    } else if (strcmp(shape, "vline") == 0 || strcmp(shape, "v_line") == 0 ||
               strcmp(shape, "drawFastVLine") == 0) {
      p.shape = PbShape::VLine;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      if (p.h < 1) continue;
      pb_json_rotation(it, &p, p.x, (int16_t)(p.y + (p.h - 1) / 2));
    } else if (strcmp(shape, "ellipse") == 0 || strcmp(shape, "ellipse_outline") == 0 ||
               strcmp(shape, "draw_ellipse") == 0 || strcmp(shape, "drawEllipse") == 0) {
      p.shape = PbShape::Ellipse;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["rw"] | it["w"] | 0);
      p.h = (int16_t)(it["rh"] | it["h"] | 0);
      if (p.w < 1 || p.h < 1) continue;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "ellipse_fill") == 0 || strcmp(shape, "fill_ellipse") == 0 ||
               strcmp(shape, "fillEllipse") == 0) {
      p.shape = PbShape::EllipseFill;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["rw"] | it["w"] | 0);
      p.h = (int16_t)(it["rh"] | it["h"] | 0);
      if (p.w < 1 || p.h < 1) continue;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "triangle") == 0 || strcmp(shape, "triangle_outline") == 0 ||
               strcmp(shape, "draw_triangle") == 0 || strcmp(shape, "drawTriangle") == 0) {
      p.shape = PbShape::Triangle;
      p.x = (int16_t)(it["x0"] | it["x"] | 0);
      p.y = (int16_t)(it["y0"] | it["y"] | 0);
      p.x1 = (int16_t)(it["x1"] | 0);
      p.y1 = (int16_t)(it["y1"] | 0);
      p.x2 = (int16_t)(it["x2"] | 0);
      p.y2 = (int16_t)(it["y2"] | 0);
      pb_json_rotation(it, &p,
                       (int16_t)((p.x + p.x1 + p.x2) / 3),
                       (int16_t)((p.y + p.y1 + p.y2) / 3));
    } else if (strcmp(shape, "triangle_fill") == 0 || strcmp(shape, "fill_triangle") == 0 ||
               strcmp(shape, "fillTriangle") == 0) {
      p.shape = PbShape::TriangleFill;
      p.x = (int16_t)(it["x0"] | it["x"] | 0);
      p.y = (int16_t)(it["y0"] | it["y"] | 0);
      p.x1 = (int16_t)(it["x1"] | 0);
      p.y1 = (int16_t)(it["y1"] | 0);
      p.x2 = (int16_t)(it["x2"] | 0);
      p.y2 = (int16_t)(it["y2"] | 0);
      pb_json_rotation(it, &p,
                       (int16_t)((p.x + p.x1 + p.x2) / 3),
                       (int16_t)((p.y + p.y1 + p.y2) / 3));
    } else if (strcmp(shape, "round_rect") == 0 || strcmp(shape, "round_rect_fill") == 0 ||
               strcmp(shape, "fill_round_rect") == 0 || strcmp(shape, "fillRoundRect") == 0) {
      p.shape = PbShape::RoundRect;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      p.r = (int16_t)(it["radius"] | it["r"] | 0);
      if (p.w < 1 || p.h < 1 || p.r < 0) continue;
      pb_json_rotation(it, &p, (int16_t)(p.x + p.w / 2),
                       (int16_t)(p.y + p.h / 2));
    } else if (strcmp(shape, "round_rect_outline") == 0 || strcmp(shape, "draw_round_rect") == 0 ||
               strcmp(shape, "drawRoundRect") == 0) {
      p.shape = PbShape::RoundRectOutline;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      p.r = (int16_t)(it["radius"] | it["r"] | 0);
      if (p.w < 1 || p.h < 1 || p.r < 0) continue;
      pb_json_rotation(it, &p, (int16_t)(p.x + p.w / 2),
                       (int16_t)(p.y + p.h / 2));
    } else if (strcmp(shape, "rotated_rect_outline") == 0 ||
               strcmp(shape, "draw_rotated_rect") == 0) {
      p.shape = PbShape::RotatedRectOutline;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      if (p.w < 1 || p.h < 1) continue;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "rotated_rect_fill") == 0 ||
               strcmp(shape, "fill_rotated_rect") == 0) {
      p.shape = PbShape::RotatedRectFill;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      if (p.w < 1 || p.h < 1) continue;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "text") == 0 || strcmp(shape, "print") == 0 || strcmp(shape, "label") == 0) {
      p.shape = PbShape::Text;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      const char* tstr = nullptr;
      if (!it["text"].isNull()) {
        tstr = it["text"].as<const char*>();
      } else if (!it["s"].isNull()) {
        tstr = it["s"].as<const char*>();
      } else if (!it["str"].isNull()) {
        tstr = it["str"].as<const char*>();
      }
      if (!tstr || !tstr[0]) {
        continue;
      }
      strncpy(p.text, tstr, kPbMaxTextChars);
      p.text[kPbMaxTextChars] = '\0';
      int tsz = 1;
      if (!it["size"].isNull()) {
        tsz = it["size"].as<int>();
      } else if (!it["text_size"].isNull()) {
        tsz = it["text_size"].as<int>();
      }
      if (tsz < 1) {
        tsz = 1;
      }
      if (tsz > 3) {
        tsz = 3;
      }
      p.text_size = (uint8_t)tsz;
      pb_json_rotation(it, &p, p.x, p.y);
    } else if (strcmp(shape, "image") == 0) {
      p.shape = PbShape::Image;
      p.x = (int16_t)(it["x"] | 0);
      p.y = (int16_t)(it["y"] | 0);
      p.w = (int16_t)(it["w"] | 0);
      p.h = (int16_t)(it["h"] | 0);
      p.asset_index = (uint8_t)constrain((int)(it["asset"] | 0), 0, 255);
      if (p.w < 1 || p.h < 1) {
        continue;
      }
      if (!pb_image_asset_available(p.asset_index)) {
        log_warn("[DISPLAY] image asset=%u invalid or unavailable",
                 (unsigned)p.asset_index);
        continue;
      }
    } else {
      continue;
    }
    out->count++;
  }
  return true;
}

static bool stored_layers_have_renderable(const StoredLayer& bg,
                                          const StoredLayer& nose,
                                          const StoredLayer& mouth,
                                          const StoredLayer& eye_l,
                                          const StoredLayer& eye_r,
                                          const StoredLayer& extra) {
  const StoredLayer* layers[] = {&bg, &nose, &mouth, &eye_l, &eye_r, &extra};
  for (const StoredLayer* layer : layers) {
    for (uint8_t i = 0; i < layer->count; ++i) {
      const StoredPrim& prim = layer->prims[i];
      if (prim.shape != PbShape::None &&
          (prim.shape != PbShape::Image ||
           pb_image_asset_available(prim.asset_index))) {
        return true;
      }
    }
  }
  return false;
}

static bool pb_prev_has_renderable() {
  return s_have_prev && stored_layers_have_renderable(
                            s_prev_bg, s_prev_nose, s_prev_mouth,
                            s_prev_eye_l, s_prev_eye_r, s_prev_extra);
}

static bool stored_from_elements_v(JsonVariantConst elements_v,
                                   StoredLayer* bg, StoredLayer* nose,
                                   StoredLayer* mouth, StoredLayer* eye_l,
                                   StoredLayer* eye_r, StoredLayer* extra) {
  /* Build a complete composited frame. Missing layer keys inherit the last
   * committed frame; an explicit JSON [] is still an array and clears only
   * that layer through json_fill_layer(). */
  if (s_have_prev) {
    memcpy(bg, &s_prev_bg, sizeof(*bg));
    memcpy(nose, &s_prev_nose, sizeof(*nose));
    memcpy(mouth, &s_prev_mouth, sizeof(*mouth));
    memcpy(eye_l, &s_prev_eye_l, sizeof(*eye_l));
    memcpy(eye_r, &s_prev_eye_r, sizeof(*eye_r));
    memcpy(extra, &s_prev_extra, sizeof(*extra));
  } else {
    layer_clear(bg);
    layer_clear(nose);
    layer_clear(mouth);
    layer_clear(eye_l);
    layer_clear(eye_r);
    layer_clear(extra);
  }
  if (!elements_v.is<JsonObjectConst>()) {
    return false;
  }
  JsonObjectConst elements = elements_v.as<JsonObjectConst>();
  if (elements["bg"].is<JsonArrayConst>()) {
    if (!json_fill_layer(elements["bg"].as<JsonArrayConst>(), bg)) return false;
  }
  if (elements["nose"].is<JsonArrayConst>()) {
    if (!json_fill_layer(elements["nose"].as<JsonArrayConst>(), nose)) return false;
  }
  if (elements["mouth"].is<JsonArrayConst>()) {
    if (!json_fill_layer(elements["mouth"].as<JsonArrayConst>(), mouth)) return false;
  }
  if (elements["eye_l"].is<JsonArrayConst>()) {
    if (!json_fill_layer(elements["eye_l"].as<JsonArrayConst>(), eye_l)) return false;
  }
  if (elements["eye_r"].is<JsonArrayConst>()) {
    if (!json_fill_layer(elements["eye_r"].as<JsonArrayConst>(), eye_r)) return false;
  }
  if (elements["extra"].is<JsonArrayConst>()) {
    if (!json_fill_layer(elements["extra"].as<JsonArrayConst>(), extra)) return false;
  }
  return true;
}

static int pb_jpeg_draw_cb(JPEGDRAW* pDraw) {
  JpegBlitCtx* ctx = static_cast<JpegBlitCtx*>(pDraw->pUser);
  if (!ctx || !ctx->canvas_buf || !pDraw->pPixels || ctx->iw <= 0 || ctx->ih <= 0 || ctx->dw <= 0 ||
      ctx->dh <= 0 || ctx->canvas_w <= 0 || ctx->canvas_h <= 0) {
    return 0;
  }
  const uint16_t* pixels = pDraw->pPixels;
  const int src_row_w = (pDraw->iWidthUsed > 0) ? pDraw->iWidthUsed : pDraw->iWidth;
  for (int row_i = 0; row_i < pDraw->iHeight; row_i++) {
    const int src_y = pDraw->y + row_i;
    const int dst_y = ctx->dy + (src_y * ctx->dh) / ctx->ih;
    if (dst_y < 0 || dst_y >= ctx->canvas_h) {
      continue;
    }
    uint16_t* dst_row = ctx->canvas_buf + (int32_t)dst_y * (int32_t)ctx->canvas_w;
    for (int col_i = 0; col_i < src_row_w; col_i++) {
      const int src_x = pDraw->x + col_i;
      const int dst_x = ctx->dx + (src_x * ctx->dw) / ctx->iw;
      if (dst_x < 0 || dst_x >= ctx->canvas_w) {
        continue;
      }
      dst_row[dst_x] = pixels[row_i * pDraw->iWidth + col_i];
    }
  }
  return 1;
}

static void draw_jpeg_asset(const StoredPrim& p) {
  if (p.asset_index >= s_render_asset_count) {
    log_warn("[DISPLAY] image asset=%u missing (have %u)", (unsigned)p.asset_index,
             (unsigned)s_render_asset_count);
    return;
  }
  const DisplayPbAssetBlob& blob = s_render_assets[p.asset_index];
  if (!blob.data || blob.len < 4) {
    return;
  }
  if (!s_canvas || !s_canvas->getBuffer() || s_draw_gfx != s_canvas) {
    log_warn("[DISPLAY] JPEG skip: need PSRAM canvas target");
    return;
  }
  if (s_jpeg_dec.openRAM(blob.data, (int)blob.len, pb_jpeg_draw_cb) != 1) {
    log_warn("[DISPLAY] JPEG openRAM failed asset=%u len=%u", (unsigned)p.asset_index, (unsigned)blob.len);
    return;
  }
  s_jpeg_blit_ctx.canvas_buf = s_canvas->getBuffer();
  s_jpeg_blit_ctx.canvas_w = (int16_t)DESKBOT_DRAW_W;
  s_jpeg_blit_ctx.canvas_h = (int16_t)DESKBOT_DRAW_H;
  s_jpeg_blit_ctx.dx = p.x;
  s_jpeg_blit_ctx.dy = p.y;
  s_jpeg_blit_ctx.dw = p.w;
  s_jpeg_blit_ctx.dh = p.h;
  s_jpeg_blit_ctx.iw = s_jpeg_dec.getWidth();
  s_jpeg_blit_ctx.ih = s_jpeg_dec.getHeight();
  if (s_jpeg_blit_ctx.iw <= 0 || s_jpeg_blit_ctx.ih <= 0) {
    s_jpeg_dec.close();
    return;
  }
  /* 与 GFXcanvas16 / drawRGBBitmap 一致：ESP32 上为小端 RGB565；BIG_ENDIAN 会花屏。 */
  s_jpeg_dec.setPixelType(RGB565_LITTLE_ENDIAN);
  s_jpeg_dec.setUserPointer(&s_jpeg_blit_ctx);
  s_jpeg_dec.decode(0, 0, 0);
  s_jpeg_dec.close();
}

struct PbPoint {
  int16_t x;
  int16_t y;
};

static PbPoint pb_rotate_point(float x, float y, int16_t cx, int16_t cy,
                               int16_t angle_deg) {
  if (angle_deg == 0) {
    return {(int16_t)lroundf(x), (int16_t)lroundf(y)};
  }
  const float radians = (float)angle_deg * 0.01745329251994329577f;
  const float cs = cosf(radians);
  const float sn = sinf(radians);
  const float dx = x - (float)cx;
  const float dy = y - (float)cy;
  return {(int16_t)lroundf((float)cx + dx * cs - dy * sn),
          (int16_t)lroundf((float)cy + dx * sn + dy * cs)};
}

static uint8_t pb_stroke_width(const StoredPrim& p) {
  return p.stroke_width > 0 ? p.stroke_width : 1;
}

static void pb_draw_thick_line(PbPoint a, PbPoint b, uint8_t width,
                               uint16_t color) {
  if (width <= 1 || (a.x == b.x && a.y == b.y)) {
    (*s_draw_gfx).drawLine(a.x, a.y, b.x, b.y, color);
    return;
  }
  const float dx = (float)b.x - (float)a.x;
  const float dy = (float)b.y - (float)a.y;
  const float len = sqrtf(dx * dx + dy * dy);
  if (len < 0.5f) {
    (*s_draw_gfx).fillCircle(a.x, a.y, width / 2, color);
    return;
  }
  for (uint8_t i = 0; i < width; ++i) {
    const float offset = (float)i - ((float)width - 1.0f) * 0.5f;
    const int ox = (int)lroundf(-dy * offset / len);
    const int oy = (int)lroundf(dx * offset / len);
    (*s_draw_gfx).drawLine(a.x + ox, a.y + oy, b.x + ox, b.y + oy,
                           color);
  }
}

static void pb_fill_quad(const PbPoint (&v)[4], uint16_t color) {
  (*s_draw_gfx).fillTriangle(v[0].x, v[0].y, v[1].x, v[1].y, v[2].x,
                             v[2].y, color);
  (*s_draw_gfx).fillTriangle(v[0].x, v[0].y, v[2].x, v[2].y, v[3].x,
                             v[3].y, color);
}

static void pb_rotated_quad(float x0, float y0, float x1, float y1,
                            int16_t pivot_x, int16_t pivot_y,
                            int16_t angle_deg, PbPoint (&out)[4]) {
  out[0] = pb_rotate_point(x0, y0, pivot_x, pivot_y, angle_deg);
  out[1] = pb_rotate_point(x1, y0, pivot_x, pivot_y, angle_deg);
  out[2] = pb_rotate_point(x1, y1, pivot_x, pivot_y, angle_deg);
  out[3] = pb_rotate_point(x0, y1, pivot_x, pivot_y, angle_deg);
}

static void pb_draw_quad_outline(const PbPoint (&v)[4], uint8_t width,
                                 uint16_t color) {
  for (uint8_t i = 0; i < 4; ++i) {
    pb_draw_thick_line(v[i], v[(i + 1) % 4], width, color);
  }
}

static void pb_draw_rotated_ellipse(const StoredPrim& p, bool fill) {
  static constexpr uint8_t kSegments = 32;
  PbPoint center = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                   p.angle_deg);
  PbPoint points[kSegments];
  for (uint8_t i = 0; i < kSegments; ++i) {
    const float theta = 6.2831853071795864769f * (float)i /
                        (float)kSegments;
    points[i] = pb_rotate_point((float)p.x + (float)p.w * cosf(theta),
                                (float)p.y + (float)p.h * sinf(theta),
                                p.rot_cx, p.rot_cy, p.angle_deg);
  }
  if (fill) {
    for (uint8_t i = 0; i < kSegments; ++i) {
      const PbPoint& a = points[i];
      const PbPoint& b = points[(i + 1) % kSegments];
      (*s_draw_gfx).fillTriangle(center.x, center.y, a.x, a.y, b.x, b.y,
                                 p.color);
    }
  } else {
    for (uint8_t i = 0; i < kSegments; ++i) {
      pb_draw_thick_line(points[i], points[(i + 1) % kSegments],
                         pb_stroke_width(p), p.color);
    }
  }
}

static int16_t pb_round_radius(const StoredPrim& p) {
  const int16_t limit = (int16_t)((p.w < p.h ? p.w : p.h) / 2);
  return p.r < limit ? p.r : limit;
}

static void pb_fill_rotated_round_rect(const StoredPrim& p) {
  const int16_t r = pb_round_radius(p);
  if (r <= 0) {
    PbPoint v[4];
    pb_rotated_quad(p.x, p.y, p.x + p.w - 1, p.y + p.h - 1,
                    p.rot_cx, p.rot_cy, p.angle_deg, v);
    pb_fill_quad(v, p.color);
    return;
  }
  PbPoint horizontal[4];
  pb_rotated_quad(p.x + r, p.y, p.x + p.w - r - 1, p.y + p.h - 1,
                  p.rot_cx, p.rot_cy, p.angle_deg, horizontal);
  pb_fill_quad(horizontal, p.color);
  PbPoint vertical[4];
  pb_rotated_quad(p.x, p.y + r, p.x + p.w - 1, p.y + p.h - r - 1,
                  p.rot_cx, p.rot_cy, p.angle_deg, vertical);
  pb_fill_quad(vertical, p.color);
  const float xs[2] = {(float)(p.x + r), (float)(p.x + p.w - r - 1)};
  const float ys[2] = {(float)(p.y + r), (float)(p.y + p.h - r - 1)};
  for (uint8_t ix = 0; ix < 2; ++ix) {
    for (uint8_t iy = 0; iy < 2; ++iy) {
      const PbPoint c = pb_rotate_point(xs[ix], ys[iy], p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      (*s_draw_gfx).fillCircle(c.x, c.y, r, p.color);
    }
  }
}

static void pb_draw_rotated_round_rect_outline(const StoredPrim& p) {
  static constexpr uint8_t kArcSegments = 8;
  const int16_t r = pb_round_radius(p);
  if (r <= 0) {
    PbPoint v[4];
    pb_rotated_quad(p.x, p.y, p.x + p.w - 1, p.y + p.h - 1,
                    p.rot_cx, p.rot_cy, p.angle_deg, v);
    pb_draw_quad_outline(v, pb_stroke_width(p), p.color);
    return;
  }
  PbPoint points[kArcSegments * 4];
  const float centers_x[4] = {(float)(p.x + p.w - r - 1),
                              (float)(p.x + p.w - r - 1),
                              (float)(p.x + r), (float)(p.x + r)};
  const float centers_y[4] = {(float)(p.y + r),
                              (float)(p.y + p.h - r - 1),
                              (float)(p.y + p.h - r - 1),
                              (float)(p.y + r)};
  for (uint8_t corner = 0; corner < 4; ++corner) {
    const float start_deg = -90.0f + 90.0f * (float)corner;
    for (uint8_t i = 0; i < kArcSegments; ++i) {
      const float theta = (start_deg + 90.0f * (float)i /
                                           (float)(kArcSegments - 1)) *
                          0.01745329251994329577f;
      points[corner * kArcSegments + i] = pb_rotate_point(
          centers_x[corner] + (float)r * cosf(theta),
          centers_y[corner] + (float)r * sinf(theta), p.rot_cx, p.rot_cy,
          p.angle_deg);
    }
  }
  for (uint8_t i = 0; i < kArcSegments * 4; ++i) {
    pb_draw_thick_line(points[i], points[(i + 1) % (kArcSegments * 4)],
                       pb_stroke_width(p), p.color);
  }
}

static void draw_prim(const StoredPrim& p) {
  const uint16_t col = p.color;
  const uint8_t sw = pb_stroke_width(p);
  switch (p.shape) {
    case PbShape::Rect:
    case PbShape::RectOutline: {
      if (p.w < 1 || p.h < 1) break;
      if (p.angle_deg == 0) {
        if (p.shape == PbShape::Rect) {
          (*s_draw_gfx).fillRect(p.x, p.y, p.w, p.h, col);
        } else {
          for (uint8_t i = 0; i < sw && p.w > 2 * i && p.h > 2 * i; ++i) {
            (*s_draw_gfx).drawRect(p.x + i, p.y + i, p.w - 2 * i,
                                   p.h - 2 * i, col);
          }
        }
      } else {
        PbPoint v[4];
        pb_rotated_quad(p.x, p.y, p.x + p.w - 1, p.y + p.h - 1,
                        p.rot_cx, p.rot_cy, p.angle_deg, v);
        if (p.shape == PbShape::Rect) pb_fill_quad(v, col);
        else pb_draw_quad_outline(v, sw, col);
      }
    } break;
    case PbShape::Circle:
    case PbShape::CircleOutline: {
      if (p.r < 1) break;
      const PbPoint c = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      if (p.shape == PbShape::Circle) {
        (*s_draw_gfx).fillCircle(c.x, c.y, p.r, col);
      } else {
        for (uint8_t i = 0; i < sw && p.r >= i; ++i) {
          (*s_draw_gfx).drawCircle(c.x, c.y, p.r - i, col);
        }
      }
    } break;
    case PbShape::Line: {
      const PbPoint a = pb_rotate_point(p.x1, p.y1, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      const PbPoint b = pb_rotate_point(p.x2, p.y2, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      pb_draw_thick_line(a, b, sw, col);
    } break;
    case PbShape::Pixel: {
      const PbPoint q = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      (*s_draw_gfx).drawPixel(q.x, q.y, col);
    } break;
    case PbShape::HLine: {
      const PbPoint a = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      const PbPoint b = pb_rotate_point(p.x + p.w - 1, p.y, p.rot_cx,
                                        p.rot_cy, p.angle_deg);
      pb_draw_thick_line(a, b, sw, col);
    } break;
    case PbShape::VLine: {
      const PbPoint a = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      const PbPoint b = pb_rotate_point(p.x, p.y + p.h - 1, p.rot_cx,
                                        p.rot_cy, p.angle_deg);
      pb_draw_thick_line(a, b, sw, col);
    } break;
    case PbShape::Ellipse:
    case PbShape::EllipseFill:
      if (p.w < 1 || p.h < 1) break;
      if (p.angle_deg == 0) {
        if (p.shape == PbShape::EllipseFill) {
          (*s_draw_gfx).fillEllipse(p.x, p.y, p.w, p.h, col);
        } else {
          for (uint8_t i = 0; i < sw && p.w > i && p.h > i; ++i) {
            (*s_draw_gfx).drawEllipse(p.x, p.y, p.w - i, p.h - i, col);
          }
        }
      } else {
        pb_draw_rotated_ellipse(p, p.shape == PbShape::EllipseFill);
      }
      break;
    case PbShape::Triangle:
    case PbShape::TriangleFill: {
      const PbPoint a = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      const PbPoint b = pb_rotate_point(p.x1, p.y1, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      const PbPoint c = pb_rotate_point(p.x2, p.y2, p.rot_cx, p.rot_cy,
                                        p.angle_deg);
      if (p.shape == PbShape::TriangleFill) {
        (*s_draw_gfx).fillTriangle(a.x, a.y, b.x, b.y, c.x, c.y, col);
      } else {
        pb_draw_thick_line(a, b, sw, col);
        pb_draw_thick_line(b, c, sw, col);
        pb_draw_thick_line(c, a, sw, col);
      }
    } break;
    case PbShape::RoundRect:
    case PbShape::RoundRectOutline:
      if (p.w < 1 || p.h < 1 || p.r < 0) break;
      if (p.angle_deg == 0) {
        const int16_t r = pb_round_radius(p);
        if (p.shape == PbShape::RoundRect) {
          (*s_draw_gfx).fillRoundRect(p.x, p.y, p.w, p.h, r, col);
        } else {
          for (uint8_t i = 0; i < sw && p.w > 2 * i && p.h > 2 * i; ++i) {
            const int16_t ri = r > i ? r - i : 0;
            (*s_draw_gfx).drawRoundRect(p.x + i, p.y + i, p.w - 2 * i,
                                        p.h - 2 * i, ri, col);
          }
        }
      } else if (p.shape == PbShape::RoundRect) {
        pb_fill_rotated_round_rect(p);
      } else {
        pb_draw_rotated_round_rect_outline(p);
      }
      break;
    case PbShape::RotatedRectOutline:
    case PbShape::RotatedRectFill: {
      if (p.w < 1 || p.h < 1) break;
      const float half_w = (float)p.w * 0.5f;
      const float half_h = (float)p.h * 0.5f;
      PbPoint v[4];
      pb_rotated_quad((float)p.x - half_w, (float)p.y - half_h,
                      (float)p.x + half_w, (float)p.y + half_h,
                      p.rot_cx, p.rot_cy, p.angle_deg, v);
      if (p.shape == PbShape::RotatedRectFill) pb_fill_quad(v, col);
      else pb_draw_quad_outline(v, sw, col);
    } break;
    case PbShape::Text:
      if (p.text[0] != '\0') {
        uint8_t sz = p.text_size ? p.text_size : 1;
        if (sz > 3) sz = 3;
        const PbPoint q = pb_rotate_point(p.x, p.y, p.rot_cx, p.rot_cy,
                                          p.angle_deg);
        display_text_draw(s_draw_gfx, q.x, q.y, p.text, sz, col);
      }
      break;
    case PbShape::Image:
      draw_jpeg_asset(p);
      break;
    case PbShape::None:
    default:
      break;
  }
}

static void draw_prim_lerp(const StoredPrim* prev, const StoredPrim& curr,
                           float t) {
  if (!prev || prev->shape != curr.shape || curr.shape == PbShape::None) {
    draw_prim(curr);
    return;
  }
  if (curr.shape == PbShape::Image) {
    draw_prim(curr);
    return;
  }
  if (curr.shape == PbShape::Text &&
      (strcmp(prev->text, curr.text) != 0 ||
       prev->text_size != curr.text_size)) {
    draw_prim(curr);
    return;
  }
  StoredPrim tween = curr;
  tween.x = (int16_t)lerp_i16(prev->x, curr.x, t);
  tween.y = (int16_t)lerp_i16(prev->y, curr.y, t);
  tween.w = (int16_t)lerp_i16(prev->w, curr.w, t);
  tween.h = (int16_t)lerp_i16(prev->h, curr.h, t);
  tween.r = (int16_t)lerp_i16(prev->r, curr.r, t);
  tween.x1 = (int16_t)lerp_i16(prev->x1, curr.x1, t);
  tween.y1 = (int16_t)lerp_i16(prev->y1, curr.y1, t);
  tween.x2 = (int16_t)lerp_i16(prev->x2, curr.x2, t);
  tween.y2 = (int16_t)lerp_i16(prev->y2, curr.y2, t);
  tween.rot_cx = (int16_t)lerp_i16(prev->rot_cx, curr.rot_cx, t);
  tween.rot_cy = (int16_t)lerp_i16(prev->rot_cy, curr.rot_cy, t);
  tween.angle_deg =
      (int16_t)lerp_i16(prev->angle_deg, curr.angle_deg, t);
  tween.stroke_width = (uint8_t)constrain(
      lerp_i16(prev->stroke_width, curr.stroke_width, t), 1, 12);
  draw_prim(tween);
}

static void draw_layer_lerp(const StoredLayer* prev, const StoredLayer& curr, float t) {
  const uint8_t ncurr = curr.count;
  if (ncurr == 0) {
    /* curr is the complete composited frame. An empty layer was explicitly
     * cleared; missing layers were copied before interpolation. */
    return;
  }
  const uint8_t nprev = prev ? prev->count : 0;
  for (uint8_t i = 0; i < ncurr; i++) {
    const StoredPrim& c = curr.prims[i];
    if (i < nprev) {
      draw_prim_lerp(&prev->prims[i], c, t);
    } else {
      draw_prim(c);
    }
  }
}

/** extra 层：先画 image 再画 text/其它，避免全屏 JPEG 盖住「正在拍照」等文案。 */
static void draw_extra_layer_ordered(const StoredLayer* prev, const StoredLayer& curr, float t) {
  const uint8_t ncurr = curr.count;
  if (ncurr == 0) {
    return;
  }
  const uint8_t nprev = prev ? prev->count : 0;
  for (uint8_t pass = 0; pass < 2; pass++) {
    for (uint8_t i = 0; i < ncurr; i++) {
      const StoredPrim& c = curr.prims[i];
      const bool is_image = (c.shape == PbShape::Image);
      if ((pass == 0) != is_image) {
        continue;
      }
      if (i < nprev) {
        draw_prim_lerp(&prev->prims[i], c, t);
      } else {
        draw_prim(c);
      }
    }
  }
}

static void draw_stored_interpolated(const StoredLayer* pbg, const StoredLayer* pn, const StoredLayer* pm,
                                     const StoredLayer* pel, const StoredLayer* per, const StoredLayer* pex,
                                     const StoredLayer& cbg, const StoredLayer& cn, const StoredLayer& cm,
                                     const StoredLayer& cel, const StoredLayer& cer, const StoredLayer& cex,
                                     float t) {
  draw_layer_lerp(pbg, cbg, t);
  draw_layer_lerp(pn, cn, t);
  draw_layer_lerp(pm, cm, t);
  draw_layer_lerp(pel, cel, t);
  draw_layer_lerp(per, cer, t);
  draw_extra_layer_ordered(pex, cex, t);
}

static constexpr size_t kPbMaxAnimSegsPerChunk = 64;

static uint32_t display_crc32_update(uint32_t crc, const uint8_t* data,
                                     size_t len) {
  crc = ~crc;
  for (size_t i = 0; i < len; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit) {
      crc = (crc >> 1) ^ (0xEDB88320u &
                          static_cast<uint32_t>(-(static_cast<int32_t>(crc & 1u))));
    }
  }
  return ~crc;
}

static void display_crc32_u8(uint32_t* crc, uint8_t value) {
  *crc = display_crc32_update(*crc, &value, sizeof(value));
}

static void display_crc32_u16(uint32_t* crc, uint16_t value) {
  const uint8_t bytes[2] = {static_cast<uint8_t>(value & 0xFFu),
                            static_cast<uint8_t>((value >> 8) & 0xFFu)};
  *crc = display_crc32_update(*crc, bytes, sizeof(bytes));
}

static void display_crc32_i16(uint32_t* crc, int16_t value) {
  display_crc32_u16(crc, static_cast<uint16_t>(value));
}

static void display_crc32_u32(uint32_t* crc, uint32_t value) {
  const uint8_t bytes[4] = {
      static_cast<uint8_t>(value & 0xFFu),
      static_cast<uint8_t>((value >> 8) & 0xFFu),
      static_cast<uint8_t>((value >> 16) & 0xFFu),
      static_cast<uint8_t>((value >> 24) & 0xFFu)};
  *crc = display_crc32_update(*crc, bytes, sizeof(bytes));
}

static uint32_t display_asset_crc32(uint8_t asset_index) {
  if (asset_index >= s_render_asset_count) {
    return 0;
  }
  const DisplayPbAssetBlob& asset = s_render_assets[asset_index];
  return asset.data && asset.len > 0
             ? display_crc32_update(0, asset.data, asset.len)
             : 0;
}

static void display_crc32_layer(uint32_t* crc, const StoredLayer& layer) {
  display_crc32_u8(crc, layer.count);
  for (uint8_t i = 0; i < layer.count; ++i) {
    const StoredPrim& prim = layer.prims[i];
    display_crc32_u8(crc, static_cast<uint8_t>(prim.shape));
    display_crc32_u16(crc, prim.color);
    display_crc32_i16(crc, prim.x);
    display_crc32_i16(crc, prim.y);
    display_crc32_i16(crc, prim.w);
    display_crc32_i16(crc, prim.h);
    display_crc32_i16(crc, prim.r);
    display_crc32_i16(crc, prim.x1);
    display_crc32_i16(crc, prim.y1);
    display_crc32_i16(crc, prim.x2);
    display_crc32_i16(crc, prim.y2);
    display_crc32_i16(crc, prim.rot_cx);
    display_crc32_i16(crc, prim.rot_cy);
    display_crc32_i16(crc, prim.angle_deg);
    display_crc32_u8(crc, prim.stroke_width);
    display_crc32_u8(crc, prim.text_size);
    display_crc32_u8(crc, prim.asset_index);
    display_crc32_u32(
        crc, prim.shape == PbShape::Image
                 ? display_asset_crc32(prim.asset_index)
                 : 0u);
    const size_t raw_len = strnlen(prim.text, kPbMaxTextChars);
    const uint16_t text_len = static_cast<uint16_t>(raw_len);
    display_crc32_u16(crc, text_len);
    if (text_len > 0) {
      *crc = display_crc32_update(
          *crc, reinterpret_cast<const uint8_t*>(prim.text), text_len);
    }
  }
}

static uint32_t display_current_semantic_crc32() {
  static constexpr uint8_t kVersion[] = {'D', 'B', 'D', '1'};
  uint32_t crc = display_crc32_update(0, kVersion, sizeof(kVersion));
  display_crc32_u16(&crc, s_prev_frame_bg);
  display_crc32_layer(&crc, s_prev_bg);
  display_crc32_layer(&crc, s_prev_nose);
  display_crc32_layer(&crc, s_prev_mouth);
  display_crc32_layer(&crc, s_prev_eye_l);
  display_crc32_layer(&crc, s_prev_eye_r);
  display_crc32_layer(&crc, s_prev_extra);
  return crc;
}

static void pb_commit_prev(const StoredLayer& bg, const StoredLayer& nose, const StoredLayer& mouth,
                           const StoredLayer& eye_l, const StoredLayer& eye_r, const StoredLayer& extra) {
  memcpy(&s_prev_bg, &bg, sizeof(bg));
  memcpy(&s_prev_nose, &nose, sizeof(nose));
  memcpy(&s_prev_mouth, &mouth, sizeof(mouth));
  memcpy(&s_prev_eye_l, &eye_l, sizeof(eye_l));
  memcpy(&s_prev_eye_r, &eye_r, sizeof(eye_r));
  memcpy(&s_prev_extra, &extra, sizeof(extra));
  s_have_prev = true;
}

static void pb_commit_voice_base() {
  if (!s_have_prev) {
    s_voice_base_valid = false;
    return;
  }
  if (!s_voice_base) {
    s_voice_base = static_cast<VoiceBaseStorage*>(heap_caps_calloc(
        1, sizeof(VoiceBaseStorage), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!s_voice_base) {
      s_voice_base_valid = false;
      log_error(
          "[DISPLAY] voice-base PSRAM allocation failed bytes=%u; "
          "mouth overlay disabled",
          static_cast<unsigned>(sizeof(VoiceBaseStorage)));
      return;
    }
    log_info("[DISPLAY] voice-base snapshot allocated in PSRAM bytes=%u",
             static_cast<unsigned>(sizeof(VoiceBaseStorage)));
  }
  StoredLayer& s_voice_base_bg = s_voice_base->bg;
  StoredLayer& s_voice_base_nose = s_voice_base->nose;
  StoredLayer& s_voice_base_mouth = s_voice_base->mouth;
  StoredLayer& s_voice_base_eye_l = s_voice_base->eye_l;
  StoredLayer& s_voice_base_eye_r = s_voice_base->eye_r;
  StoredLayer& s_voice_base_extra = s_voice_base->extra;
  uint16_t& s_voice_base_frame_bg = s_voice_base->frame_bg;
  memcpy(&s_voice_base_bg, &s_prev_bg, sizeof(s_prev_bg));
  memcpy(&s_voice_base_nose, &s_prev_nose, sizeof(s_prev_nose));
  memcpy(&s_voice_base_mouth, &s_prev_mouth, sizeof(s_prev_mouth));
  memcpy(&s_voice_base_eye_l, &s_prev_eye_l, sizeof(s_prev_eye_l));
  memcpy(&s_voice_base_eye_r, &s_prev_eye_r, sizeof(s_prev_eye_r));
  memcpy(&s_voice_base_extra, &s_prev_extra, sizeof(s_prev_extra));
  s_voice_base_frame_bg = s_prev_frame_bg;
  s_voice_base_valid = s_have_prev;
}

static void pb_draw_voice_mouth(uint8_t level) {
  if (!s_voice_base_valid || !s_voice_base || !s_canvas ||
      !s_canvas->getBuffer()) {
    return;
  }
  const StoredLayer& s_voice_base_bg = s_voice_base->bg;
  const StoredLayer& s_voice_base_nose = s_voice_base->nose;
  const StoredLayer& s_voice_base_mouth = s_voice_base->mouth;
  const StoredLayer& s_voice_base_eye_l = s_voice_base->eye_l;
  const StoredLayer& s_voice_base_eye_r = s_voice_base->eye_r;
  const StoredLayer& s_voice_base_extra = s_voice_base->extra;
  const uint16_t s_voice_base_frame_bg = s_voice_base->frame_bg;
  (*s_draw_gfx).fillScreen(s_voice_base_frame_bg);
  StoredLayer empty_mouth{};
  draw_stored_interpolated(
      nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, s_voice_base_bg,
      s_voice_base_nose, empty_mouth, s_voice_base_eye_l, s_voice_base_eye_r,
      s_voice_base_extra, 1.0f);
  if (level == 0) {
    draw_layer_lerp(nullptr, s_voice_base_mouth, 1.0f);
  } else {
    StoredPrim mouth{};
    mouth.shape = level >= 3 ? PbShape::EllipseFill
                             : PbShape::RoundRectOutline;
    mouth.color = 0xFB20u;  // RGB565 #ff6700, same default as wire
    mouth.x = 142;
    mouth.y = 159;
    mouth.w = static_cast<int16_t>(20 + level * 5);
    mouth.h = static_cast<int16_t>(6 + level * 5);
    mouth.r = static_cast<int16_t>(3 + level * 2);
    mouth.rot_cx = mouth.x;
    mouth.rot_cy = mouth.y;
    mouth.stroke_width = 2;
    if (mouth.shape == PbShape::RoundRectOutline) {
      mouth.x -= mouth.w / 2;
      mouth.y -= mouth.h / 2;
      mouth.rot_cx = mouth.x + mouth.w / 2;
      mouth.rot_cy = mouth.y + mouth.h / 2;
    }
    draw_prim(mouth);
  }
  pb_canvas_push();
}

/** 内建默认脸几何：写入 s_pb_curr_* 六层暂存（仅 display 任务串行访问）。
 *  被两个调用方共用：render runtime 创建失败时的硬件兜底 idle，以及未连接
 *  PC 服务期间的「请连接PC服务」待机屏。 */
static void pb_build_builtin_face_layers() {
  layer_clear(&s_pb_curr_bg);
  layer_clear(&s_pb_curr_nose);
  layer_clear(&s_pb_curr_mouth);
  layer_clear(&s_pb_curr_eye_l);
  layer_clear(&s_pb_curr_eye_r);
  layer_clear(&s_pb_curr_extra);

  StoredPrim eye{};
  eye.shape = PbShape::EllipseFill;
  eye.color = DESKBOT_DISPLAY_COLOR_WHITE;
  eye.w = 18;
  eye.h = 18;
  eye.stroke_width = 1;
  eye.x = 90;
  eye.y = 88;
  eye.rot_cx = eye.x;
  eye.rot_cy = eye.y;
  s_pb_curr_eye_l.prims[s_pb_curr_eye_l.count++] = eye;
  eye.x = 194;
  eye.rot_cx = eye.x;
  s_pb_curr_eye_r.prims[s_pb_curr_eye_r.count++] = eye;

  StoredPrim mouth{};
  mouth.shape = PbShape::RoundRectOutline;
  mouth.color = DESKBOT_DISPLAY_COLOR_WHITE;
  mouth.x = 117;
  mouth.y = 151;
  mouth.w = 50;
  mouth.h = 16;
  mouth.r = 8;
  mouth.rot_cx = mouth.x + mouth.w / 2;
  mouth.rot_cy = mouth.y + mouth.h / 2;
  mouth.stroke_width = 2;
  s_pb_curr_mouth.prims[s_pb_curr_mouth.count++] = mouth;
}

static void pb_show_builtin_idle(const char* reason) {
  pb_build_builtin_face_layers();
  (*s_draw_gfx).fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
  draw_stored_interpolated(
      nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, s_pb_curr_bg,
      s_pb_curr_nose, s_pb_curr_mouth, s_pb_curr_eye_l, s_pb_curr_eye_r,
      s_pb_curr_extra, 1.0f);
  pb_canvas_push();
  pb_commit_prev(s_pb_curr_bg, s_pb_curr_nose, s_pb_curr_mouth,
                 s_pb_curr_eye_l, s_pb_curr_eye_r, s_pb_curr_extra);
  log_info("[DISPLAY] builtin idle (%s)", reason ? reason : "fallback");
}

/* 待机屏：内建默认脸 + 底部文案「请连接PC服务」。
 * 产品需求（显式推翻旧「正常启动不绘制内建脸」约定）：未连接（等待 hello）
 * 与连接断开后（会话结束）都显示本屏；hello 成功后由 asr_chat 调用
 * display_standby_set(false) 清除，屏幕交还 PC 端表情系统。
 * 与 PB 机制的边界：待机屏只描画像素——不写入 s_prev_* 表情基线、不调用
 * pb_commit_prev、不产生 pb_ack，也不参与语义 CRC；PB 终态机制完全不感知它。
 * 字形来源：display_text 内置文泉驿 12px GB2312 点阵（U8g2
 * u8g2_font_wqy12_t_gb2312b，见 display_text.cpp），「请连接服务」五个汉字与
 * "PC" 均已覆盖，无需另嵌位图；若未来文案超出 GB2312 子集，需在
 * DESKBOT_DISPLAY_CJK_FONT 处扩展字库。 */
static constexpr char kStandbyHintText[] = "请连接PC服务";
/* wqy12：汉字宽 12px、ASCII 约 7px → 文案宽约 5*12+2*7=74px；
 * 画布 DESKBOT_DRAW_W(=284) 内水平居中，纵向放在嘴型下方。 */
static constexpr int16_t kStandbyHintX =
    static_cast<int16_t>((DESKBOT_DRAW_W - 74) / 2);
static constexpr int16_t kStandbyHintY = 208;

static void display_draw_standby_screen() {
  pb_build_builtin_face_layers();
  (*s_draw_gfx).fillScreen(DESKBOT_DISPLAY_COLOR_BLACK);
  draw_stored_interpolated(
      nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, s_pb_curr_bg,
      s_pb_curr_nose, s_pb_curr_mouth, s_pb_curr_eye_l, s_pb_curr_eye_r,
      s_pb_curr_extra, 1.0f);
  display_text_draw(s_draw_gfx, kStandbyHintX, kStandbyHintY, kStandbyHintText,
                    1, DESKBOT_DISPLAY_COLOR_WHITE);
  pb_canvas_push();
  log_info("[DISPLAY] standby screen drawn (builtin face + connect hint)");
}

static bool pb_play_layers_interpolated(const StoredLayer* pbg, const StoredLayer* pn, const StoredLayer* pm,
                                         const StoredLayer* pl, const StoredLayer* pr, const StoredLayer* px,
                                         const StoredLayer& cbg, const StoredLayer& cn, const StoredLayer& cm,
                                         const StoredLayer& cel, const StoredLayer& cer, const StoredLayer& cex,
                                         uint32_t segment_start_at_ms,
                                         uint32_t segment_ms,
                                         uint16_t bg_rgb565) {
  if (display_render_cancelled()) {
    return false;
  }
  if (!display_wait_for_pb_start(segment_start_at_ms)) {
    return false;
  }
  if (segment_ms == 0) {
    (*s_draw_gfx).fillScreen(bg_rgb565);
    draw_stored_interpolated(pbg, pn, pm, pl, pr, px, cbg, cn, cm, cel, cer, cex, 1.f);
    pb_canvas_push();
    return true;
  }

  uint32_t budget = segment_ms;
  if (budget > 300000u) {
    budget = 300000u;
  }

  const uint32_t segment_end_at_ms = segment_start_at_ms + budget;
  TickType_t frame_anchor_tick = xTaskGetTickCount();
  while (true) {
    if (display_render_cancelled()) {
      return false;
    }
    const uint32_t now_ms = millis();
    if (deskbot_pb_time_reached(now_ms, segment_end_at_ms)) {
      break;
    }
    const uint32_t elapsed = now_ms - segment_start_at_ms;
    float t = (float)elapsed / (float)budget;
    if (t > 1.f) {
      t = 1.f;
    }
    (*s_draw_gfx).fillScreen(bg_rgb565);
    draw_stored_interpolated(pbg, pn, pm, pl, pr, px, cbg, cn, cm, cel, cer, cex, t);
    pb_canvas_push();

    const uint32_t after_draw = millis();
    uint32_t remain =
        deskbot_pb_time_remaining(after_draw, segment_end_at_ms);
    if (remain == 0) {
      break;
    }
    const uint32_t frame_period_ms = pb_display_frame_period_ms();
    const TickType_t period_ticks =
        pdMS_TO_TICKS(frame_period_ms) > 0 ? pdMS_TO_TICKS(frame_period_ms) : 1;
    const TickType_t now_tick = xTaskGetTickCount();
    const TickType_t next_tick = frame_anchor_tick + period_ticks;
    const TickType_t wait_ticks =
        static_cast<int32_t>(next_tick - now_tick) > 0
            ? next_tick - now_tick
            : 0;
    const uint32_t wait_ms =
        static_cast<uint32_t>(wait_ticks) * portTICK_PERIOD_MS;
    if (wait_ticks == 0) {
      /* Rendering missed its cadence. Drop intermediate frames instead of
       * spinning to catch up, then continue from the current tick. */
      frame_anchor_tick = now_tick;
      taskYIELD();
    } else if (remain <= wait_ms) {
      vTaskDelay(pdMS_TO_TICKS(remain));
      break;
    } else {
      vTaskDelayUntil(&frame_anchor_tick, period_ticks);
    }
  }

  if (display_render_cancelled()) {
    return false;
  }
  /* Commit the segment endpoint even when this worker arrived late. */
  (*s_draw_gfx).fillScreen(bg_rgb565);
  draw_stored_interpolated(pbg, pn, pm, pl, pr, px, cbg, cn, cm, cel, cer,
                           cex, 1.f);
  pb_canvas_push();
  return true;
}

static bool pb_render_anim_array_timed(JsonArrayConst anim_arr,
                                       uint32_t chunk_start_at_ms,
                                       bool mouth_only) {
  if (anim_arr.isNull() || anim_arr.size() == 0) {
    return false;
  }
  if (chunk_start_at_ms == 0) {
    chunk_start_at_ms = millis();
  }

  /* A speech PB temporarily owns only the mouth layer. Keep the exact mouth
   * that belonged to the currently displayed Web/RTC/Agent face and restore
   * it before reporting the display chunk complete. Without this snapshot the
   * final phoneme became the new face baseline, so the device no longer
   * matched the expression selected in the browser after TTS stopped. */
  StoredLayer mouth_only_baseline{};
  uint16_t mouth_only_baseline_bg = s_prev_frame_bg;
  const bool restore_mouth = mouth_only && s_have_prev;
  if (restore_mouth) {
    memcpy(&mouth_only_baseline, &s_prev_mouth,
           sizeof(mouth_only_baseline));
  }

  const auto restore_mouth_only_baseline = [&]() {
    if (!restore_mouth || display_render_cancelled() || !s_have_prev) {
      return;
    }
    memcpy(&s_prev_mouth, &mouth_only_baseline,
           sizeof(s_prev_mouth));
    s_prev_frame_bg = mouth_only_baseline_bg;
    (*s_draw_gfx).fillScreen(mouth_only_baseline_bg);
    draw_stored_interpolated(
        nullptr, nullptr, nullptr, nullptr, nullptr, nullptr, s_prev_bg,
        s_prev_nose, s_prev_mouth, s_prev_eye_l, s_prev_eye_r, s_prev_extra,
        1.0f);
    pb_canvas_push();
  };

  size_t seg_idx = 0;
  uint32_t segment_offset_ms = 0;
  for (JsonObjectConst seg : anim_arr) {
    if (display_render_cancelled()) {
      return false;
    }
    if (seg_idx >= kPbMaxAnimSegsPerChunk) {
      log_warn("[DISPLAY] anim[] truncated at %u", (unsigned)kPbMaxAnimSegsPerChunk);
      restore_mouth_only_baseline();
      return false;
    }
    uint32_t seg_ms = seg["ms"].is<uint32_t>() ? seg["ms"].as<uint32_t>() : 0u;
    if (seg_ms < 1) {
      seg_ms = 1;
    }

    StoredLayer& cbg = s_pb_curr_bg;
    StoredLayer& cn = s_pb_curr_nose;
    StoredLayer& cm = s_pb_curr_mouth;
    StoredLayer& cel = s_pb_curr_eye_l;
    StoredLayer& cer = s_pb_curr_eye_r;
    StoredLayer& cex = s_pb_curr_extra;
    JsonVariantConst elements_v = seg["elements"];
    if (mouth_only && !s_have_prev) {
      log_warn("[DISPLAY] mouth-only frame has no complete face baseline");
      return false;
    }
    if (mouth_only &&
        (!elements_v.is<JsonObjectConst>() ||
         !elements_v.as<JsonObjectConst>()["mouth"].is<JsonArrayConst>())) {
      log_warn("[DISPLAY] mouth-only frame must contain a mouth array");
      restore_mouth_only_baseline();
      return false;
    }
    if (!stored_from_elements_v(elements_v, &cbg, &cn, &cm, &cel, &cer,
                                &cex)) {
      log_warn("[DISPLAY] invalid or over-limit anim segment");
      restore_mouth_only_baseline();
      return false;
    }

    const bool composite_has_renderable = stored_layers_have_renderable(
        cbg, cn, cm, cel, cer, cex);
    if (!composite_has_renderable) {
      log_warn("[DISPLAY] anim segment resolves to an empty frame");
      restore_mouth_only_baseline();
      return false;
    }

    const uint16_t seg_bg = pb_json_rgb565_field(seg, "bg", DESKBOT_DISPLAY_COLOR_BLACK);

    const StoredLayer* pbg = s_have_prev ? &s_prev_bg : nullptr;
    const StoredLayer* pn = s_have_prev ? &s_prev_nose : nullptr;
    const StoredLayer* pm = s_have_prev ? &s_prev_mouth : nullptr;
    const StoredLayer* pl = s_have_prev ? &s_prev_eye_l : nullptr;
    const StoredLayer* pr = s_have_prev ? &s_prev_eye_r : nullptr;
    const StoredLayer* px = s_have_prev ? &s_prev_extra : nullptr;

    const uint32_t segment_start_at_ms =
        chunk_start_at_ms + segment_offset_ms;
    if (!pb_play_layers_interpolated(
            pbg, pn, pm, pl, pr, px, cbg, cn, cm, cel, cer, cex,
            segment_start_at_ms, seg_ms, seg_bg)) {
      restore_mouth_only_baseline();
      return false;
    }
    pb_commit_prev(cbg, cn, cm, cel, cer, cex);
    s_prev_frame_bg = seg_bg;
    segment_offset_ms += seg_ms;
    seg_idx++;
  }
  restore_mouth_only_baseline();
  return seg_idx > 0 && !display_render_cancelled();
}

/* pb_ver 2：anim[] 为 [{ elements, ms, bg?, phoneme? }, …]；bg/c 为 RGB565（缺省 bg 黑、c 白）。 */
static bool pb_render_vector_json(const char* json, size_t json_len,
                                  uint32_t start_at_ms, bool mouth_only) {
  if (!json || json_len == 0) {
    return false;
  }
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, json, json_len);
  if (err) {
    log_warn("[DISPLAY] pb vector json parse failed: %s", err.c_str());
    return false;
  }

  if (doc.is<JsonArrayConst>()) {
    return pb_render_anim_array_timed(doc.as<JsonArrayConst>(), start_at_ms,
                                      mouth_only);
  }

  log_warn("[DISPLAY] pb anim payload must be anim[] array (pb_ver 2)");
  return false;
}

struct DisplayRequest {
  DisplayScene scene;
  int32_t arg;
  char* json_payload; /* 仅 DISPLAY_SCENE_PB_VECTOR_JSON 使用；malloc 分配，任务内 free。 */
  size_t json_len;
  uint8_t asset_count = 0;
  DisplayPbAssetBlob assets[kDisplayMaxPbAssets]{};
  uint32_t cancel_epoch;
  bool pb_tracked = false;
  uint32_t pb_epoch = 0;
  uint32_t pb_idx = 0;
  uint32_t pb_start_at_ms = 0;
  bool mouth_only = false;
  char pb_req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
};

static void display_free_request_assets(DisplayRequest& req) {
  for (uint8_t i = 0; i < req.asset_count; i++) {
    if (req.assets[i].data) {
      heap_caps_free(req.assets[i].data);
      req.assets[i].data = nullptr;
    }
    req.assets[i].len = 0;
  }
  req.asset_count = 0;
}

QueueHandle_t     s_queue       = nullptr;
QueueHandle_t     s_pb_terminal_queue = nullptr;
TaskHandle_t      s_task        = nullptr;
static bool s_queue_uses_psram = false;
static bool s_pb_terminal_queue_uses_psram = false;
static bool s_task_stack_uses_psram = false;
static std::atomic<uint32_t> s_display_pb_terminal_drop_count{0};

static QueueHandle_t display_create_queue_prefer_psram(
    UBaseType_t depth, UBaseType_t item_size, bool* uses_psram) {
  if (uses_psram) {
    *uses_psram = false;
  }
  if (psramFound()) {
    QueueHandle_t queue = xQueueCreateWithCaps(
        depth, item_size, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (queue) {
      if (uses_psram) {
        *uses_psram = true;
      }
      return queue;
    }
  }
  return xQueueCreate(depth, item_size);
}

static void display_log_runtime_allocation_failure(const char* stage) {
  static uint32_t last_error_ms = 0;
  const uint32_t now_ms = millis();
  if (last_error_ms != 0 && now_ms - last_error_ms < 1000u) {
    return;
  }
  last_error_ms = now_ms;
  log_error(
      "[DISPLAY] render runtime allocation failed stage=%s q=%d "
      "terminal_q=%d task=%d free_int=%u free_psram=%u",
      stage ? stage : "unknown", s_queue != nullptr,
      s_pb_terminal_queue != nullptr, s_task != nullptr,
      static_cast<unsigned>(heap_caps_get_free_size(
          MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
      static_cast<unsigned>(heap_caps_get_free_size(
          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
}

static void display_emit_pb_terminal(const DisplayRequest& req,
                                     DisplayPbTerminalState state,
                                     bool display_crc_valid = false,
                                     uint32_t display_crc32 = 0) {
  if (!req.pb_tracked) {
    return;
  }
  if (!s_pb_terminal_queue) {
    s_display_pb_terminal_drop_count.fetch_add(
        1, std::memory_order_relaxed);
    log_error("[DISPLAY] PB terminal queue unavailable epoch=%u req=%s idx=%u",
              (unsigned)req.pb_epoch, req.pb_req, (unsigned)req.pb_idx);
    return;
  }
  DisplayPbTerminalEvent out{};
  out.state = state;
  out.epoch = req.pb_epoch;
  out.idx = req.pb_idx;
  if (state == DisplayPbTerminalState::kCompleted && display_crc_valid) {
    out.display_crc_valid = true;
    out.display_crc32 = display_crc32;
  }
  strncpy(out.req, req.pb_req, sizeof(out.req) - 1);
  if (xQueueSend(s_pb_terminal_queue, &out, 0) != pdTRUE) {
    s_display_pb_terminal_drop_count.fetch_add(
        1, std::memory_order_relaxed);
    log_error("[DISPLAY] PB terminal queue full epoch=%u req=%s idx=%u state=%u",
              (unsigned)out.epoch, out.req, (unsigned)out.idx,
              (unsigned)out.state);
  }
}

void display_render_task(void* /*arg*/) {
  DisplayRequest req{};
  for (;;) {
    if (xQueueReceive(s_queue, &req, pdMS_TO_TICKS(50)) != pdTRUE) {
      if (s_standby_active.load(std::memory_order_acquire)) {
        /* 待机屏按 dirty 一次性绘制；hello 清除待机态后不再重绘，
         * 后续像素所有权立即回到 PC 下发的 PB 表情。 */
        if (s_standby_dirty.exchange(false, std::memory_order_acq_rel)) {
          display_draw_standby_screen();
        }
        continue;
      }
      const bool voice_active =
          s_voice_mouth_active.load(std::memory_order_acquire);
      const uint8_t level =
          voice_active ? s_voice_mouth_level.load(std::memory_order_acquire) : 0;
      const uint32_t now_ms = millis();
      if (s_voice_base_valid &&
          (level != s_voice_last_drawn_level ||
           (voice_active && now_ms - s_voice_last_refresh_ms >= 120u))) {
        pb_draw_voice_mouth(level);
        s_voice_last_drawn_level = level;
        s_voice_last_refresh_ms = now_ms;
      }
      continue;
    }
    s_active_render_epoch = req.cancel_epoch;
    if (req.cancel_epoch != s_render_cancel_epoch.load(std::memory_order_acquire)) {
      display_emit_pb_terminal(req, DisplayPbTerminalState::kCancelled);
      if (req.scene == DISPLAY_SCENE_PB_VECTOR_JSON) {
        if (req.json_payload) ::free(req.json_payload);
        display_free_request_assets(req);
      }
      continue;
    }
    if (req.scene == DISPLAY_SCENE_PB_VECTOR_JSON) {
      display_free_render_assets();
      if (s_reset_interp_on_next_pb.exchange(false, std::memory_order_acq_rel)) {
        /* A replacement expression starts from its own first frame. This
         * matches the browser preview and prevents a tween from a stale RTC
         * face while still leaving the old panel pixels visible until now. */
        pb_vector_interp_reset();
      }
      /* Assets are request-owned. Never let an omitted layer in a later PB
       * chunk inherit an image index from the previous request's freed table. */
      pb_drop_inherited_images();
      s_render_asset_count = req.asset_count;
      for (uint8_t i = 0; i < req.asset_count && i < kDisplayMaxPbAssets; i++) {
        s_render_assets[i] = req.assets[i];
        req.assets[i].data = nullptr;
        req.assets[i].len = 0;
      }
      req.asset_count = 0;
      const bool render_ok =
          pb_render_vector_json(req.json_payload, req.json_len,
                                req.pb_start_at_ms, req.mouth_only);
      const bool cancelled =
          req.cancel_epoch !=
          s_render_cancel_epoch.load(std::memory_order_acquire);
      if (!cancelled && (!render_ok || !pb_prev_has_renderable())) {
        /* Runtime playback failure is fail-closed: keep the last pixels rather
         * than injecting the firmware fallback face.  Built-in idle belongs
         * only to boot/runtime-allocation fallback; the PC owns every normal
         * expression transition. */
        log_warn("[DISPLAY] PB frame rejected; retained previous panel frame "
                 "render_ok=%d baseline=%d",
                 (int)render_ok, (int)pb_prev_has_renderable());
      }
      const bool display_crc_valid =
          !cancelled && render_ok && pb_prev_has_renderable();
      const uint32_t display_crc32 =
          display_crc_valid ? display_current_semantic_crc32() : 0u;
      if (display_crc_valid) {
        log_info("[DISPLAY] committed semantic crc32=%08lx req=%s idx=%u",
                 (unsigned long)display_crc32, req.pb_req,
                 (unsigned)req.pb_idx);
      }
      display_free_render_assets();
      if (req.json_payload) {
        ::free(req.json_payload);
      }
      display_emit_pb_terminal(
          req, cancelled ? DisplayPbTerminalState::kCancelled
                         : render_ok ? DisplayPbTerminalState::kCompleted
                                     : DisplayPbTerminalState::kFailed,
          display_crc_valid, display_crc32);
      if (!cancelled && render_ok && pb_prev_has_renderable()) {
        pb_commit_voice_base();
        s_voice_last_drawn_level = UINT8_MAX;
      }
    }
  }
}

bool ensure_render_task() {
  if (s_queue && s_pb_terminal_queue && s_task) {
    return true;
  }
  if (!s_queue) {
    /* 队列容量 32：与音频/舵机队列对齐，可缓冲 ~32 个 chunk_ms（~3.2~6.4s）的口型动画。
     * PB submission is strict and never blocks the USB caller. */
    s_queue = display_create_queue_prefer_psram(
        32, sizeof(DisplayRequest), &s_queue_uses_psram);
  }
  if (!s_pb_terminal_queue) {
    s_pb_terminal_queue = display_create_queue_prefer_psram(
        64, sizeof(DisplayPbTerminalEvent),
        &s_pb_terminal_queue_uses_psram);
  }
  if (!s_queue || !s_pb_terminal_queue) {
    display_log_runtime_allocation_failure("queues");
    return false;
  }
  if (!s_task) {
    /* U8g2 drawUTF8(gb2312) + JPEGDEC 解码全屏图栈较深；10KB 会触发 canary（见拍照 overlay）。 */
    TaskHandle_t created_task = nullptr;
    BaseType_t rc = pdFAIL;
    if (psramFound()) {
      rc = xTaskCreatePinnedToCoreWithCaps(
          display_render_task, "display_render", 32 * 1024, nullptr, 2,
          &created_task, APP_CPU_NUM,
          MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
      s_task_stack_uses_psram =
          rc == pdPASS && created_task != nullptr;
    }
    if (rc != pdPASS || created_task == nullptr) {
      created_task = nullptr;
      s_task_stack_uses_psram = false;
      rc = xTaskCreatePinnedToCore(
          display_render_task, "display_render", 32 * 1024, nullptr, 2,
          &created_task, APP_CPU_NUM);
    }
    if (rc != pdPASS || created_task == nullptr) {
      display_log_runtime_allocation_failure("task");
      return false;
    }
    s_task = created_task;
    log_info(
        "[DISPLAY] render runtime ready q=%s terminal_q=%s stack=%s "
        "depth=32 terminal_depth=64 stack_bytes=32768 free_int=%u "
        "free_psram=%u",
        s_queue_uses_psram ? "psram" : "internal",
        s_pb_terminal_queue_uses_psram ? "psram" : "internal",
        s_task_stack_uses_psram ? "psram" : "internal",
        static_cast<unsigned>(heap_caps_get_free_size(
            MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT)),
        static_cast<unsigned>(heap_caps_get_free_size(
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)));
  }
  return true;
}

}  // namespace

void task_setup_display() {
  if (!ensure_render_task()) {
    /* Keep the panel useful even when the asynchronous runtime cannot start.
     * PB submissions will fail closed instead of being falsely ACKed. */
    pb_show_builtin_idle("render worker unavailable");
    return;
  }
  /* 产品需求（推翻旧「正常启动只留 boot 文本、不绘制内建脸」的约定）：
   * 未连接 PC 服务期间由 render worker 显示待机屏（内建默认脸 +
   * 「请连接PC服务」）。hello 成功后 asr_chat 清除待机态，PC 端表情系统
   * 接管每一次换脸；待机屏不进入 s_prev_* 基线、不产生 pb_ack。 */
  display_standby_set(true);
  log_info("[DISPLAY] standby screen active; waiting for PC service hello");
}

void display_standby_set(bool active) {
  const bool previous =
      s_standby_active.exchange(active, std::memory_order_acq_rel);
  s_standby_dirty.store(active, std::memory_order_release);
  if (previous != active) {
    log_info("[DISPLAY] standby screen %s",
             active ? "requested (no PC service)"
                    : "cleared (PC session owns display)");
  }
}

static void display_fill_request_assets(DisplayRequest& req, uint8_t* const* asset_bufs,
                                     const size_t* asset_lens, uint8_t asset_count) {
  req.asset_count = 0;
  if (!asset_bufs || !asset_lens || asset_count == 0) {
    return;
  }
  for (uint8_t i = 0; i < asset_count && i < kDisplayMaxPbAssets; i++) {
    if (!asset_bufs[i] || asset_lens[i] == 0) {
      continue;
    }
    req.assets[req.asset_count].data = asset_bufs[i];
    req.assets[req.asset_count].len = asset_lens[i];
    req.asset_count++;
  }
}

bool display_pb_submit_vector_json_owned(uint32_t epoch, const char* req_id,
                                         uint32_t idx,
                                         uint32_t start_at_ms, char* json,
                                         size_t json_len,
                                         uint8_t* const* asset_bufs,
                                         const size_t* asset_lens,
                                         uint8_t asset_count,
                                         bool mouth_only) {
  DisplayRequest req{};
  req.scene = DISPLAY_SCENE_PB_VECTOR_JSON;
  req.json_payload = json;
  req.json_len = json_len;
  req.mouth_only = mouth_only;
  display_fill_request_assets(req, asset_bufs, asset_lens, asset_count);

  if (json == nullptr || json_len == 0 || epoch == 0 ||
      start_at_ms == 0 ||
      req_id == nullptr || req_id[0] == '\0') {
    if (req.json_payload) {
      ::free(req.json_payload);
      req.json_payload = nullptr;
    }
    display_free_request_assets(req);
    return false;
  }

  if (!ensure_render_task()) {
    log_error("[DISPLAY] PB rejected: render runtime unavailable epoch=%u req=%s idx=%u",
              (unsigned)epoch, req_id, (unsigned)idx);
    ::free(req.json_payload);
    req.json_payload = nullptr;
    display_free_request_assets(req);
    return false;
  }

  json[json_len] = '\0';
  req.cancel_epoch =
      s_render_cancel_epoch.load(std::memory_order_acquire);
  req.pb_tracked = true;
  req.pb_epoch = epoch;
  req.pb_idx = idx;
  req.pb_start_at_ms = start_at_ms;
  strncpy(req.pb_req, req_id, sizeof(req.pb_req) - 1);
  req.pb_req[sizeof(req.pb_req) - 1] = '\0';

  if (xQueueSend(s_queue, &req, 0) == pdTRUE) {
    return true;
  }
  log_warn("[DISPLAY] PB render queue full epoch=%u req=%s idx=%u",
           (unsigned)epoch, req.pb_req, (unsigned)idx);
  if (req.json_payload) {
    ::free(req.json_payload);
    req.json_payload = nullptr;
  }
  display_free_request_assets(req);
  return false;
}

bool display_take_pb_terminal_event(DisplayPbTerminalEvent* out) {
  if (out == nullptr) {
    return false;
  }
  if (!ensure_render_task()) {
    return false;
  }
  return s_pb_terminal_queue != nullptr &&
         xQueueReceive(s_pb_terminal_queue, out, 0) == pdTRUE;
}

uint32_t display_pb_terminal_drop_count() {
  return s_display_pb_terminal_drop_count.load(std::memory_order_acquire);
}

void display_render_replace(bool preserve_baseline) {
  if (!ensure_render_task()) {
    log_error("[DISPLAY] replace rejected: render runtime unavailable");
    return;
  }
  const uint32_t replace_epoch =
      s_render_cancel_epoch.fetch_add(1, std::memory_order_acq_rel) + 1;
  display_voice_mouth_stop();
  s_voice_mouth_allowed.store(false, std::memory_order_release);
  if (!s_queue) {
    log_error("[DISPLAY] replace requested without render queue");
    return;
  }
  DisplayRequest dropped{};
  while (xQueueReceive(s_queue, &dropped, 0) == pdTRUE) {
    display_emit_pb_terminal(dropped, DisplayPbTerminalState::kCancelled);
    if (dropped.scene == DISPLAY_SCENE_PB_VECTOR_JSON) {
      if (dropped.json_payload) {
        ::free(dropped.json_payload);
      }
      display_free_request_assets(dropped);
    }
  }
  s_reset_interp_on_next_pb.store(!preserve_baseline,
                                  std::memory_order_release);
  /* The new epoch makes the active interpolation leave on its next cadence.
   * Keep both the panel pixels and interpolation baseline until the incoming
   * expression's first frame is accepted; no built-in idle is inserted. */
  log_info("[DISPLAY] seamless replace epoch=%u preserve_baseline=%d",
           static_cast<unsigned>(replace_epoch), (int)preserve_baseline);
}

void display_voice_mouth_set_level(uint8_t level) {
  if (!s_voice_mouth_allowed.load(std::memory_order_acquire)) {
    return;
  }
  if (level > 4) {
    level = 4;
  }
  s_voice_mouth_level.store(level, std::memory_order_release);
  s_voice_mouth_active.store(true, std::memory_order_release);
}

void display_voice_mouth_set_allowed(bool allowed) {
  s_voice_mouth_allowed.store(allowed, std::memory_order_release);
  if (!allowed) {
    display_voice_mouth_stop();
  }
}

void display_voice_mouth_stop() {
  s_voice_mouth_level.store(0, std::memory_order_release);
  s_voice_mouth_active.store(false, std::memory_order_release);
}

unsigned display_render_input_queue_depth(void) {
  if (!ensure_render_task()) {
    return 0u;
  }
  return s_queue ? (unsigned)uxQueueMessagesWaiting(s_queue) : 0u;
}
