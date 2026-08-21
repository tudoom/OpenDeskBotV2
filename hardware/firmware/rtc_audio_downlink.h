#pragma once

#include <stddef.h>
#include <stdint.h>

/*
 * Standalone RTC Opus is decoded outside the USB receive callback.  The
 * callback only copies and queues a small packet/batch, so control and
 * heartbeat frames keep moving even if an Opus decode or I2S start is slow.
 */
bool rtc_audio_downlink_begin();
bool rtc_audio_downlink_ready();

/* Start or atomically replace one explicit host playback generation. */
bool rtc_audio_downlink_begin_stream(uint32_t session_epoch);

bool rtc_audio_downlink_enqueue(const uint8_t* payload, size_t length,
                                uint16_t frames, uint32_t session_epoch,
                                bool end_stream);

bool rtc_audio_downlink_enqueue_end(uint32_t session_epoch);

/* Invalidate queued/in-flight work after disconnect, PB takeover or reset. */
void rtc_audio_downlink_cancel();

bool rtc_audio_downlink_active();
bool rtc_audio_downlink_closing();
uint32_t rtc_audio_downlink_generation();
size_t rtc_audio_downlink_queue_depth();

/*
 * True only after END_STREAM is ordered behind every queued decode and the
 * decoder worker is idle.  The caller must additionally wait for the audio
 * playback queue/I2S write to drain before closing the PCM stream.
 */
bool rtc_audio_downlink_ready_to_close();
void rtc_audio_downlink_mark_closed();
