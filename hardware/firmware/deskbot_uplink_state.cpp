#include "deskbot_uplink_state.h"

#include "audio_frontend_esp_sr.h"
#include "deskbot_config.h"

#include <Arduino.h>
#include <atomic>

namespace {

std::atomic<bool> s_speaker_audible{false};
std::atomic<unsigned long> s_speaker_silent_until_ms{0};
std::atomic<bool> s_transport_ready{false};
std::atomic<bool> s_transport_uplink_allowed{false};
std::atomic<uint32_t> s_transport_generation{0};
std::atomic<bool> s_mic_enabled{true};
std::atomic<bool> s_rtc_interruptible{false};

}  // namespace

void deskbot_uplink_set_speaker_active(bool active) {
  const bool previous =
      s_speaker_audible.exchange(active, std::memory_order_acq_rel);
  if (previous != active) {
    audio_frontend_set_playback_active(active);
  }
  if (!active) {
    const unsigned long until = millis() + (unsigned long)DESKBOT_TAIL_SUPPRESS_MS;
    unsigned long old = s_speaker_silent_until_ms.load(std::memory_order_relaxed);
    while ((long)(until - old) > 0 &&
           !s_speaker_silent_until_ms.compare_exchange_weak(
               old, until, std::memory_order_release, std::memory_order_relaxed)) {
    }
  }
}

bool deskbot_uplink_speaker_audible(void) {
  return s_speaker_audible.load(std::memory_order_acquire);
}

bool deskbot_uplink_in_tail_suppress(void) {
  if (s_speaker_audible.load(std::memory_order_acquire)) {
    return true;
  }
  return (long)(s_speaker_silent_until_ms.load(std::memory_order_acquire) - millis()) > 0;
}

unsigned long deskbot_uplink_tail_ms_remaining(void) {
  if (s_speaker_audible.load(std::memory_order_acquire)) {
    return (unsigned long)DESKBOT_TAIL_SUPPRESS_MS;
  }
  const unsigned long now = millis();
  const unsigned long until = s_speaker_silent_until_ms.load(std::memory_order_acquire);
  if ((long)(until - now) <= 0) {
    return 0;
  }
  return until - now;
}

void deskbot_uplink_set_transport_ready(bool ready) {
  s_transport_ready.store(ready, std::memory_order_release);
  s_transport_uplink_allowed.store(ready, std::memory_order_release);
}

bool deskbot_uplink_transport_ready(void) {
  return s_transport_ready.load(std::memory_order_acquire);
}

bool deskbot_uplink_transport_allowed(void) {
  return s_transport_uplink_allowed.load(std::memory_order_acquire);
}

uint32_t deskbot_uplink_transport_generation(void) {
  return s_transport_generation.load(std::memory_order_acquire);
}

void deskbot_uplink_bump_transport_generation(void) {
  s_transport_generation.fetch_add(1, std::memory_order_acq_rel);
  s_transport_ready.store(false, std::memory_order_release);
  s_transport_uplink_allowed.store(false, std::memory_order_release);
}

void deskbot_uplink_set_mic_enabled(bool enabled) {
  s_mic_enabled.store(enabled, std::memory_order_release);
}

bool deskbot_uplink_mic_enabled(void) {
  return s_mic_enabled.load(std::memory_order_acquire);
}

void deskbot_uplink_set_rtc_interruptible(bool enabled) {
  s_rtc_interruptible.store(enabled, std::memory_order_release);
}

bool deskbot_uplink_rtc_interruptible(void) {
  return s_rtc_interruptible.load(std::memory_order_acquire);
}

bool deskbot_uplink_capture_allowed(void) {
  if (!s_mic_enabled.load(std::memory_order_acquire)) {
    return false;
  }
  if (!s_transport_uplink_allowed.load(std::memory_order_acquire)) {
    return false;
  }
  if (!s_rtc_interruptible.load(std::memory_order_acquire) &&
      s_speaker_audible.load(std::memory_order_acquire)) {
    return false;
  }
  if (!s_rtc_interruptible.load(std::memory_order_acquire) &&
      (long)(s_speaker_silent_until_ms.load(std::memory_order_acquire) - millis()) > 0) {
    return false;
  }
  return true;
}
