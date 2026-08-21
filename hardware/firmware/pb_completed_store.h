#ifndef DESKBOT_PB_COMPLETED_STORE_H
#define DESKBOT_PB_COMPLETED_STORE_H

#include <stddef.h>
#include <stdint.h>

/** Matches the server-side idempotency-key limit. */
constexpr size_t DESKBOT_PB_REQ_MAX_CHARS = 64;
constexpr size_t DESKBOT_PB_REQ_BUFFER_SIZE =
    DESKBOT_PB_REQ_MAX_CHARS + 1;
constexpr size_t DESKBOT_PB_COMPLETED_STORE_DEPTH = 24;

enum class DeskbotPbStoredOutcome : uint8_t {
  kPlayed = 1,
  kFailed = 2,
  kCancelled = 3,
};

struct DeskbotPbCompletedRecord {
  char req[DESKBOT_PB_REQ_BUFFER_SIZE]{};
  uint32_t idx = 0;
  DeskbotPbStoredOutcome outcome = DeskbotPbStoredOutcome::kFailed;
  bool display_crc_valid = false;
  uint32_t display_crc32 = 0;
};

/**
 * Load the bounded NVS ring from oldest to newest.  Missing storage is a
 * successful empty load; corrupt or incompatible individual slots are
 * ignored.
 */
bool deskbot_pb_completed_load(DeskbotPbCompletedRecord* out,
                               size_t out_capacity, size_t* out_count);

/**
 * Append one terminal request to the NVS ring.  Exactly one slot plus the
 * next-slot cursor is committed, limiting write amplification.
 */
bool deskbot_pb_completed_store_record(
    const DeskbotPbCompletedRecord& record);

#endif
