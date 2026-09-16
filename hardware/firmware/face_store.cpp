#include "face_store.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <FFat.h>
#include <esp_heap_caps.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>

#include "display.h"
#include "logger.h"
#include "usb_transport.h"

namespace {

constexpr const char* kDir = "/face";
constexpr const char* kMetaPath = "/face/meta.json";
constexpr const char* kAnimPath = "/face/anim.json";
constexpr size_t kMaxTotalBytes = 256u * 1024u; /* FFat 共 1 MiB，套图最多占 1/4 */
constexpr uint8_t kMaxAssets = 8;
constexpr size_t kTagLen = 24;

char s_persisted_tag[kTagLen] = {0};
std::atomic<bool> s_persist_busy{false};

void asset_path(char* out, size_t n, uint8_t index) {
  snprintf(out, n, "/face/a%u.jpg", static_cast<unsigned>(index));
}

bool write_file(const char* path, const uint8_t* data, size_t len) {
  File f = FFat.open(path, FILE_WRITE);
  if (!f) {
    return false;
  }
  const size_t n = f.write(data, len);
  f.close();
  return n == len;
}

void* psram_alloc(size_t n) {
  void* p = heap_caps_malloc(n, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
  return p ? p : heap_caps_malloc(n, MALLOC_CAP_8BIT);
}

uint8_t* read_file(const char* path, size_t expect_len) {
  File f = FFat.open(path, FILE_READ);
  if (!f) {
    return nullptr;
  }
  const size_t len = f.size();
  if (len == 0 || len != expect_len) {
    f.close();
    return nullptr;
  }
  uint8_t* buf = static_cast<uint8_t*>(psram_alloc(len + 1));
  if (!buf) {
    f.close();
    return nullptr;
  }
  const size_t n = f.read(buf, len);
  f.close();
  if (n != len) {
    heap_caps_free(buf);
    return nullptr;
  }
  buf[len] = 0;
  return buf;
}

void remove_files() {
  FFat.remove(kMetaPath);
  FFat.remove(kAnimPath);
  for (uint8_t i = 0; i < kMaxAssets; i++) {
    char path[24];
    asset_path(path, sizeof(path), i);
    FFat.remove(path);
  }
  s_persisted_tag[0] = 0;
}

struct SaveCtx {
  bool ok = false;
  size_t bytes = 0;
  const char* error = "";
  char tag[kTagLen] = {0};
};

void save_visitor(const char* json, size_t json_len, uint8_t* const* bufs, const size_t* lens,
                  uint8_t count, const char* tag, void* vctx) {
  SaveCtx* ctx = static_cast<SaveCtx*>(vctx);
  size_t total = json_len;
  for (uint8_t i = 0; i < count; i++) {
    total += lens[i];
  }
  if (count > kMaxAssets || total > kMaxTotalBytes) {
    ctx->error = "too_large";
    return;
  }
  remove_files();
  if (!FFat.exists(kDir) && !FFat.mkdir(kDir)) {
    ctx->error = "mkdir_failed";
    return;
  }
  if (!write_file(kAnimPath, reinterpret_cast<const uint8_t*>(json), json_len)) {
    ctx->error = "write_anim_failed";
    return;
  }
  for (uint8_t i = 0; i < count; i++) {
    char path[24];
    asset_path(path, sizeof(path), i);
    if (!write_file(path, bufs[i], lens[i])) {
      ctx->error = "write_asset_failed";
      return;
    }
  }
  char meta[320];
  int n = snprintf(meta, sizeof(meta), "{\"tag\":\"%s\",\"json_len\":%u,\"assets\":[",
                   tag ? tag : "", static_cast<unsigned>(json_len));
  for (uint8_t i = 0; i < count && n > 0 && static_cast<size_t>(n) < sizeof(meta) - 16; i++) {
    n += snprintf(meta + n, sizeof(meta) - n, "%s%u", i ? "," : "", static_cast<unsigned>(lens[i]));
  }
  if (n <= 0 || static_cast<size_t>(n) >= sizeof(meta) - 3) {
    ctx->error = "meta_too_long";
    return;
  }
  n += snprintf(meta + n, sizeof(meta) - n, "]}");
  if (!write_file(kMetaPath, reinterpret_cast<const uint8_t*>(meta), static_cast<size_t>(n))) {
    ctx->error = "write_meta_failed";
    return;
  }
  strncpy(ctx->tag, tag ? tag : "", sizeof(ctx->tag) - 1);
  strncpy(s_persisted_tag, ctx->tag, sizeof(s_persisted_tag) - 1);
  ctx->bytes = total + static_cast<size_t>(n);
  ctx->ok = true;
}

/* FFat 写最多 256 KB 要 1～2 s：放在独立低优先级任务里，不阻塞 loopTask（USB 收发/心跳），
 * 写完再回执。持锁期间 display worker 的待机回放拿不到锁会等下一个空闲 tick 重试。 */
void persist_task(void*) {
  SaveCtx ctx;
  const bool has = display_standby_face_visit(save_visitor, &ctx);
  if (!has) {
    ctx.error = "no_standby_face";
  }
  char json[160];
  if (ctx.ok) {
    snprintf(json, sizeof(json), "{\"type\":\"face_persist_ack\",\"ok\":true,\"tag\":\"%s\",\"bytes\":%u}",
             ctx.tag, static_cast<unsigned>(ctx.bytes));
    log_info("[FACE] standby face persisted tag=%s bytes=%u", ctx.tag, (unsigned)ctx.bytes);
  } else {
    snprintf(json, sizeof(json), "{\"type\":\"face_persist_ack\",\"ok\":false,\"error\":\"%s\"}", ctx.error);
    log_warn("[FACE] standby face persist failed: %s", ctx.error);
  }
  (void)usb_transport_send_control_json(json);
  s_persist_busy.store(false, std::memory_order_release);
  vTaskDelete(nullptr);
}

}  // namespace

const char* face_store_persisted_tag() { return s_persisted_tag; }

bool face_store_load() {
  if (!FFat.exists(kMetaPath)) {
    return false;
  }
  File mf = FFat.open(kMetaPath, FILE_READ);
  if (!mf) {
    return false;
  }
  StaticJsonDocument<512> meta;
  const DeserializationError err = deserializeJson(meta, mf);
  mf.close();
  if (err) {
    log_warn("[FACE] meta.json invalid (%s); ignoring persisted face", err.c_str());
    return false;
  }
  const size_t json_len = meta["json_len"] | 0u;
  const char* tag = meta["tag"] | "";
  JsonArrayConst lens = meta["assets"].as<JsonArrayConst>();
  const uint8_t count = static_cast<uint8_t>(lens.size());
  if (json_len == 0 || count == 0 || count > kMaxAssets) {
    log_warn("[FACE] meta.json out of range json_len=%u assets=%u", (unsigned)json_len, (unsigned)count);
    return false;
  }
  uint8_t* json = read_file(kAnimPath, json_len);
  uint8_t* bufs[kMaxAssets] = {nullptr};
  size_t sizes[kMaxAssets] = {0};
  bool ok = json != nullptr;
  for (uint8_t i = 0; ok && i < count; i++) {
    sizes[i] = lens[i] | 0u;
    char path[24];
    asset_path(path, sizeof(path), i);
    bufs[i] = sizes[i] ? read_file(path, sizes[i]) : nullptr;
    ok = bufs[i] != nullptr;
  }
  if (ok) {
    ok = display_standby_face_store(reinterpret_cast<const char*>(json), json_len, bufs, sizes, count, tag);
  }
  if (json) {
    heap_caps_free(json);
  }
  for (uint8_t i = 0; i < count; i++) {
    if (bufs[i]) {
      heap_caps_free(bufs[i]);
    }
  }
  if (ok) {
    strncpy(s_persisted_tag, tag, sizeof(s_persisted_tag) - 1);
    log_info("[FACE] persisted standby face restored tag=%s assets=%u", tag, (unsigned)count);
  } else {
    log_warn("[FACE] persisted standby face unreadable; falling back to builtin face");
  }
  return ok;
}

bool face_store_handle_control_json(const uint8_t* payload, size_t length) {
  if (payload == nullptr || length == 0 || length > 256) {
    return false;
  }
  StaticJsonDocument<192> doc;
  if (deserializeJson(doc, payload, length)) {
    return false;
  }
  const char* type = doc["type"] | "";
  char json[160];
  if (strcmp(type, "face_persist") == 0) {
    const char* tag = display_standby_face_tag();
    if (tag[0] == 0) {
      (void)usb_transport_send_control_json(
          "{\"type\":\"face_persist_ack\",\"ok\":false,\"error\":\"no_standby_face\"}");
      return true;
    }
    if (s_persisted_tag[0] != 0 && strcmp(s_persisted_tag, tag) == 0) {
      /* 同一张脸已经在 FFat 里（标签是内容哈希）：不重写 flash，直接回执。 */
      snprintf(json, sizeof(json), "{\"type\":\"face_persist_ack\",\"ok\":true,\"tag\":\"%s\",\"already\":true}", tag);
      (void)usb_transport_send_control_json(json);
      return true;
    }
    if (s_persist_busy.exchange(true, std::memory_order_acq_rel)) {
      (void)usb_transport_send_control_json(
          "{\"type\":\"face_persist_ack\",\"ok\":false,\"error\":\"busy\"}");
      return true;
    }
    if (xTaskCreate(persist_task, "face_persist", 8192, nullptr, 1, nullptr) != pdPASS) {
      s_persist_busy.store(false, std::memory_order_release);
      (void)usb_transport_send_control_json(
          "{\"type\":\"face_persist_ack\",\"ok\":false,\"error\":\"task_failed\"}");
    }
    return true;
  }
  if (strcmp(type, "face_clear") == 0) {
    remove_files();
    display_standby_face_clear();
    (void)usb_transport_send_control_json("{\"type\":\"face_clear_ack\",\"ok\":true}");
    return true;
  }
  if (strcmp(type, "face_status_req") == 0) {
    const char* tag = display_standby_face_tag();
    const bool persisted = s_persisted_tag[0] != 0 && strcmp(s_persisted_tag, tag) == 0;
    snprintf(json, sizeof(json), "{\"type\":\"face_status_ack\",\"tag\":\"%s\",\"persisted\":%s}",
             tag, persisted ? "true" : "false");
    (void)usb_transport_send_control_json(json);
    return true;
  }
  return false;
}
