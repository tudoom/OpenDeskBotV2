#pragma once

#include <Arduino.h>
#include <stdint.h>

/*
 * PB workers share 32-bit millis() deadlines.  Signed subtraction keeps the
 * comparisons valid across millis() wrap as long as one sequence is shorter
 * than 2^31 ms (the wire limit is many orders of magnitude smaller).
 */
static inline bool deskbot_pb_time_reached(uint32_t now_ms,
                                           uint32_t target_ms) {
  return static_cast<int32_t>(now_ms - target_ms) >= 0;
}

static inline uint32_t deskbot_pb_time_remaining(uint32_t now_ms,
                                                 uint32_t target_ms) {
  return deskbot_pb_time_reached(now_ms, target_ms)
             ? 0u
             : static_cast<uint32_t>(target_ms - now_ms);
}

static inline uint32_t deskbot_pb_time_late(uint32_t now_ms,
                                            uint32_t target_ms) {
  return deskbot_pb_time_reached(now_ms, target_ms)
             ? static_cast<uint32_t>(now_ms - target_ms)
             : 0u;
}
