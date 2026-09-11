#include "pb_completed_store.h"

#include <nvs.h>
#include <stdio.h>
#include <string.h>

#include "logger.h"

namespace {

constexpr char kNvsNamespace[] = "deskbot_pb";
constexpr char kNvsCursorKey[] = "next";
constexpr uint32_t kRecordMagic = 0x50424352u;  // "PBCR"
constexpr uint16_t kRecordVersion = 2;
constexpr uint8_t kRecordFlagDisplayCrc = 0x01u;
constexpr uint8_t kKnownRecordFlags = kRecordFlagDisplayCrc;

struct PersistedRecord {
  uint32_t magic = kRecordMagic;
  uint16_t version = kRecordVersion;
  uint16_t record_size = 0;
  uint32_t idx = 0;
  uint8_t outcome = 0;
  uint8_t req_len = 0;
  uint8_t flags = 0;
  uint8_t reserved = 0;
  uint32_t display_crc32 = 0;
  char req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
};

void slot_key(uint8_t slot, char* out, size_t out_size) {
  snprintf(out, out_size, "r%02u", static_cast<unsigned>(slot));
}

bool outcome_valid(uint8_t value) {
  return value ==
             static_cast<uint8_t>(DeskbotPbStoredOutcome::kPlayed) ||
         value ==
             static_cast<uint8_t>(DeskbotPbStoredOutcome::kFailed) ||
         value ==
             static_cast<uint8_t>(DeskbotPbStoredOutcome::kCancelled);
}

bool persisted_record_valid(const PersistedRecord& record) {
  if (record.magic != kRecordMagic ||
      record.version != kRecordVersion ||
      record.record_size != sizeof(PersistedRecord) ||
      !outcome_valid(record.outcome) || record.req_len == 0 ||
      record.req_len > DESKBOT_PB_REQ_MAX_CHARS ||
      (record.flags & ~kKnownRecordFlags) != 0 ||
      record.req[record.req_len] != '\0') {
    return false;
  }
  return strnlen(record.req, DESKBOT_PB_REQ_BUFFER_SIZE) ==
         record.req_len;
}

bool public_record_valid(const DeskbotPbCompletedRecord& record) {
  const size_t req_len =
      strnlen(record.req, DESKBOT_PB_REQ_BUFFER_SIZE);
  return req_len > 0 && req_len <= DESKBOT_PB_REQ_MAX_CHARS &&
         outcome_valid(static_cast<uint8_t>(record.outcome));
}

}  // namespace

bool deskbot_pb_completed_load(DeskbotPbCompletedRecord* out,
                               size_t out_capacity, size_t* out_count) {
  if (out_count == nullptr || (out == nullptr && out_capacity != 0)) {
    return false;
  }
  *out_count = 0;

  nvs_handle_t handle = 0;
  const esp_err_t open_err =
      nvs_open(kNvsNamespace, NVS_READONLY, &handle);
  if (open_err == ESP_ERR_NVS_NOT_FOUND) {
    return true;
  }
  if (open_err != ESP_OK) {
    log_error("[PB] completed cache NVS open failed err=%d",
              static_cast<int>(open_err));
    return false;
  }

  uint8_t cursor = 0;
  const esp_err_t cursor_err =
      nvs_get_u8(handle, kNvsCursorKey, &cursor);
  if (cursor_err != ESP_OK && cursor_err != ESP_ERR_NVS_NOT_FOUND) {
    log_warn("[PB] completed cache cursor invalid err=%d",
             static_cast<int>(cursor_err));
    cursor = 0;
  }
  if (cursor >= DESKBOT_PB_COMPLETED_STORE_DEPTH) {
    log_warn("[PB] completed cache cursor out of range value=%u",
             static_cast<unsigned>(cursor));
    cursor = 0;
  }

  for (size_t offset = 0;
       offset < DESKBOT_PB_COMPLETED_STORE_DEPTH &&
       *out_count < out_capacity;
       ++offset) {
    const uint8_t slot = static_cast<uint8_t>(
        (cursor + offset) % DESKBOT_PB_COMPLETED_STORE_DEPTH);
    char key[8];
    slot_key(slot, key, sizeof(key));
    PersistedRecord persisted{};
    size_t persisted_size = sizeof(persisted);
    const esp_err_t read_err =
        nvs_get_blob(handle, key, &persisted, &persisted_size);
    if (read_err == ESP_ERR_NVS_NOT_FOUND) {
      continue;
    }
    if (read_err != ESP_OK || persisted_size != sizeof(persisted) ||
        !persisted_record_valid(persisted)) {
      log_warn("[PB] completed cache ignored invalid slot=%u err=%d size=%u",
               static_cast<unsigned>(slot), static_cast<int>(read_err),
               static_cast<unsigned>(persisted_size));
      continue;
    }

    DeskbotPbCompletedRecord& loaded = out[*out_count];
    loaded = DeskbotPbCompletedRecord{};
    memcpy(loaded.req, persisted.req,
           static_cast<size_t>(persisted.req_len) + 1u);
    loaded.idx = persisted.idx;
    loaded.outcome =
        static_cast<DeskbotPbStoredOutcome>(persisted.outcome);
    loaded.display_crc_valid =
        loaded.outcome == DeskbotPbStoredOutcome::kPlayed &&
        (persisted.flags & kRecordFlagDisplayCrc) != 0;
    loaded.display_crc32 =
        loaded.display_crc_valid ? persisted.display_crc32 : 0u;
    (*out_count)++;
  }
  nvs_close(handle);
  log_info("[PB] completed cache restored count=%u",
           static_cast<unsigned>(*out_count));
  return true;
}

bool deskbot_pb_completed_store_record(
    const DeskbotPbCompletedRecord& record) {
  if (!public_record_valid(record)) {
    log_error("[PB] completed cache rejected invalid record");
    return false;
  }

  nvs_handle_t handle = 0;
  esp_err_t err = nvs_open(kNvsNamespace, NVS_READWRITE, &handle);
  if (err != ESP_OK) {
    log_error("[PB] completed cache NVS write open failed err=%d",
              static_cast<int>(err));
    return false;
  }

  uint8_t cursor = 0;
  const esp_err_t cursor_err =
      nvs_get_u8(handle, kNvsCursorKey, &cursor);
  if (cursor_err != ESP_OK && cursor_err != ESP_ERR_NVS_NOT_FOUND) {
    log_error("[PB] completed cache cursor read failed err=%d",
              static_cast<int>(cursor_err));
    nvs_close(handle);
    return false;
  }
  if (cursor >= DESKBOT_PB_COMPLETED_STORE_DEPTH) {
    cursor = 0;
  }

  PersistedRecord persisted{};
  persisted.record_size = sizeof(PersistedRecord);
  persisted.idx = record.idx;
  persisted.outcome = static_cast<uint8_t>(record.outcome);
  if (record.outcome == DeskbotPbStoredOutcome::kPlayed &&
      record.display_crc_valid) {
    persisted.flags |= kRecordFlagDisplayCrc;
    persisted.display_crc32 = record.display_crc32;
  }
  persisted.req_len = static_cast<uint8_t>(
      strnlen(record.req, DESKBOT_PB_REQ_BUFFER_SIZE));
  memcpy(persisted.req, record.req,
         static_cast<size_t>(persisted.req_len) + 1u);

  char key[8];
  slot_key(cursor, key, sizeof(key));
  err = nvs_set_blob(handle, key, &persisted, sizeof(persisted));
  const uint8_t next_cursor = static_cast<uint8_t>(
      (cursor + 1u) % DESKBOT_PB_COMPLETED_STORE_DEPTH);
  if (err == ESP_OK) {
    err = nvs_set_u8(handle, kNvsCursorKey, next_cursor);
  }
  if (err == ESP_OK) {
    err = nvs_commit(handle);
  }

  PersistedRecord readback{};
  size_t readback_size = sizeof(readback);
  const bool verified =
      err == ESP_OK &&
      nvs_get_blob(handle, key, &readback, &readback_size) == ESP_OK &&
      readback_size == sizeof(readback) &&
      memcmp(&readback, &persisted, sizeof(persisted)) == 0;
  nvs_close(handle);
  if (!verified) {
    log_error("[PB] completed cache commit/readback failed slot=%u err=%d",
              static_cast<unsigned>(cursor), static_cast<int>(err));
    return false;
  }
  return true;
}
