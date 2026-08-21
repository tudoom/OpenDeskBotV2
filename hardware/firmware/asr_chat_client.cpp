#include "asr_chat_client.h"

#include <ArduinoJson.h>
#include <math.h>
#include <stdlib.h>
#include <string.h>
#include "camera.h"
#include "display.h"
#include "audio_capture.h"
#include "audio_frontend_esp_sr.h"
#include "esp_heap_caps.h"
#include "head.h"
#include "task_trace.h"
#include "opus_uplink.h"
#include "opus_downlink.h"
#include "pb_timeline.h"
#include "rtc_audio_downlink.h"
#include "deskbot_uplink_state.h"
#include "runtime_supervisor.h"

static int pb_json_number_to_int(JsonVariantConst v, int defv) {
  if (v.isNull()) {
    return defv;
  }
  return (int)lround(v.as<double>());
}

static bool pb_json_integer_in_range(JsonVariantConst value, int64_t minimum,
                                     int64_t maximum, int64_t* parsed) {
  if (parsed == nullptr || value.isNull() || value.is<bool>() ||
      (!value.is<int>() && !value.is<unsigned int>() &&
       !value.is<long>() && !value.is<unsigned long>() &&
       !value.is<double>())) {
    return false;
  }
  const double raw = value.as<double>();
  if (!isfinite(raw) || floor(raw) != raw ||
      raw < static_cast<double>(minimum) ||
      raw > static_cast<double>(maximum)) {
    return false;
  }
  *parsed = static_cast<int64_t>(raw);
  return true;
}

/** PB 下行：audio.next_bin_len > 0 表示下一条 PB_WIRE 为定长 binary。 */
static size_t pb_read_audio_next_bin_len(const JsonDocument& doc) {
  if (!doc["audio"].is<JsonObjectConst>()) {
    return 0;
  }
  JsonVariantConst v = doc["audio"]["next_bin_len"];
  if (v.isNull()) {
    return 0;
  }
  const int n = pb_json_number_to_int(v, 0);
  return n > 0 ? (size_t)n : 0;
}

static uint16_t pb_read_audio_opus_frames(const JsonDocument& doc) {
  if (!doc["audio"].is<JsonObjectConst>()) {
    return 0;
  }
  JsonVariantConst v = doc["audio"]["frames"];
  if (v.isNull()) {
    return 0;
  }
  const int n = pb_json_number_to_int(v, 0);
  return n > 0 ? (uint16_t)n : 0;
}

static size_t pb_read_asset_next_bin_len(JsonObjectConst asset) {
  if (asset.isNull()) {
    return 0;
  }
  JsonVariantConst v = asset["next_bin_len"];
  if (v.isNull()) {
    return 0;
  }
  const int n = pb_json_number_to_int(v, 0);
  return n > 0 ? (size_t)n : 0;
}

enum class PbMicHint : uint8_t { kNone = 0, kOpen = 1, kMute = 2 };

static PbMicHint pb_parse_mic_hint(const JsonDocument& doc) {
  if (!doc["mic"].is<String>()) {
    return PbMicHint::kNone;
  }
  String m = doc["mic"].as<String>();
  m.toLowerCase();
  if (m == "open") {
    return PbMicHint::kOpen;
  }
  if (m == "mute") {
    return PbMicHint::kMute;
  }
  return PbMicHint::kNone;
}

/** 打印服务端 pb JSON 摘要（不含 anim/servo 等大字段）。 */
static void log_pb_rx_summary(const JsonDocument& doc) {
  const String type = doc["type"].is<String>() ? doc["type"].as<String>() : String("-");
  const uint32_t idx = doc["idx"].is<uint32_t>() ? doc["idx"].as<uint32_t>() : 0;
  const PbMicHint mic = pb_parse_mic_hint(doc);
  const char* mic_s =
      mic == PbMicHint::kOpen ? "open" : mic == PbMicHint::kMute ? "mute" : "-";
  int vol = -1;
  if (doc["volume"].is<int>()) {
    vol = doc["volume"].as<int>();
  }
  log_warn("[PB_RX] type=%s idx=%u mic=%s volume=%d", type.c_str(), (unsigned)idx, mic_s, vol);
}

static bool pb_doc_has_asset_bins(const JsonDocument& doc) {
  if (!doc["assets"].is<JsonArrayConst>()) {
    return false;
  }
  for (JsonObjectConst asset : doc["assets"].as<JsonArrayConst>()) {
    if (pb_read_asset_next_bin_len(asset) > 0) {
      return true;
    }
  }
  return false;
}

static bool pb_doc_has_nonempty_anim(const JsonDocument& doc) {
  return doc["anim"].is<JsonArrayConst>() && doc["anim"].as<JsonArrayConst>().size() > 0;
}

static bool pb_doc_has_nonempty_servo(const JsonDocument& doc) {
  return doc["servo"].is<JsonArrayConst>() && doc["servo"].as<JsonArrayConst>().size() > 0;
}

static bool pb_doc_has_audio_payload(const JsonDocument& doc) {
  return pb_read_audio_next_bin_len(doc) > 0;
}

static bool pb_doc_is_servo_only_gesture(const JsonDocument& doc) {
  const String type = doc["type"].is<String>() ? doc["type"].as<String>() : String("");
  if (type != "pb_single" && type != "pb_start") {
    return false;
  }
  if (!pb_doc_has_nonempty_servo(doc) || pb_doc_has_nonempty_anim(doc) ||
      pb_read_audio_next_bin_len(doc) > 0 || pb_doc_has_asset_bins(doc) ||
      pb_parse_mic_hint(doc) != PbMicHint::kNone ||
      !doc["rtc_mode"].isNull() || !doc["cam_fps"].isNull() ||
      (doc["camera_once"].is<bool>() && doc["camera_once"].as<bool>())) {
    return false;
  }
  return true;
}

extern AsrChatClient asrChatClient;

bool asr_chat_voice_uplink_busy(void) {
  return asrChatClient.isVoiceUplinkBusy();
}

namespace {
/* 无 AEC：尾音抑制见 deskbot_uplink_state / DESKBOT_TAIL_SUPPRESS_MS。 */

/* mic 提示直接落到全局 uplink 状态（deskbot_uplink_state）；原先寄居
 * display.cpp 的 set_active 一行转发已删除。 */
void micUplinkSetActive(bool active) {
  deskbot_uplink_set_mic_enabled(active);
  log_info("[MIC] requested=%s effective=%s", active ? "open" : "mute",
           deskbot_uplink_capture_allowed() ? "open" : "blocked");
}

const char* micCaptureBlockReason(bool transport_can_send) {
  if (!deskbot_uplink_transport_allowed()) {
    return "transport_blocked";
  }
  if (!transport_can_send) {
    return "transport_not_ready";
  }
  if (deskbot_uplink_speaker_audible()) {
    return "isSpeaking";
  }
  if (deskbot_uplink_in_tail_suppress()) {
    return "tail_suppress";
  }
  return nullptr;
}

void logMicCaptureBlockReason(const char* phase, uint32_t round_id, const char* reason) {
  if (reason == nullptr) {
    return;
  }
  if (strcmp(reason, "tail_suppress") == 0) {
    log_warn("[MIC_UPLINK] 暂停录音 %s round=%u reason=%s remain=%lums "
             "isSpeaking=%d transport_ok=%d",
             phase, (unsigned)round_id, reason,
             (unsigned long)deskbot_uplink_tail_ms_remaining(),
             (int)deskbot_uplink_speaker_audible(),
             (int)deskbot_uplink_transport_allowed());
  } else {
    log_warn("[MIC_UPLINK] 暂停录音 %s round=%u reason=%s "
             "isSpeaking=%d transport_ok=%d",
             phase, (unsigned)round_id, reason, (int)deskbot_uplink_speaker_audible(),
             (int)deskbot_uplink_transport_allowed());
  }
}

extern "C" void audio_yield_hook() {
  /* Never re-enter loop() from the audio worker. Yield briefly so USB/PB
   * processing on the application task can continue. */
  taskYIELD();
}

struct FrameSampleStats {
  int16_t signed_min;
  int16_t signed_max;
  size_t abs_avg;
  uint64_t abs_sum;
};

FrameSampleStats frame_sample_stats(const int16_t* pcm, size_t samples) {
  FrameSampleStats out{INT16_MAX, INT16_MIN, 0, 0};
  if (pcm == nullptr || samples == 0) {
    return {0, 0, 0, 0};
  }
  for (size_t i = 0; i < samples; ++i) {
    const int16_t s = pcm[i];
    if (s < out.signed_min) {
      out.signed_min = s;
    }
    if (s > out.signed_max) {
      out.signed_max = s;
    }
    out.abs_sum += static_cast<uint64_t>(abs(s));
  }
  out.abs_avg = static_cast<size_t>(out.abs_sum / samples);
  if (out.signed_min == INT16_MAX) {
    out.signed_min = 0;
  }
  if (out.signed_max == INT16_MIN) {
    out.signed_max = 0;
  }
  return out;
}

}  // namespace

int AsrChatClient::pbCountHigherPrioritySeqs(int8_t level) const {
  int n = 0;
  for (size_t i = 0; i < pb_seq_level_count_; i++) {
    if (pb_seq_levels_[i] > level) {
      n++;
    }
  }
  return n;
}

AsrChatClient::PbQueueDecision AsrChatClient::pbDecideChainHead(int8_t level, PbEnqueueAction action) const {
  if (pb_inflight_seq_count_ == 0) {
    if (action == PbEnqueueAction::kReplace) {
      return PbQueueDecision::kClear;
    }
    return PbQueueDecision::kAppend;
  }
  const int8_t ql = pb_queue_level_;
  if (ql < 0 || level > ql) {
    return PbQueueDecision::kClear;
  }
  if (action == PbEnqueueAction::kAppend) {
    return PbQueueDecision::kAppend;
  }
  if (level == ql && action == PbEnqueueAction::kReplace) {
    return PbQueueDecision::kClear;
  }
  if (level <= ql && action == PbEnqueueAction::kDefault) {
    const int n_high = pbCountHigherPrioritySeqs(level);
    if (n_high <= 1) {
      return PbQueueDecision::kAppend;
    }
    return PbQueueDecision::kDrop;
  }
  if (level < ql && action == PbEnqueueAction::kReplace) {
    log_warn("[PB] drop chain head: level=%d < queue_level=%d action=replace", (int)level, (int)ql);
    return PbQueueDecision::kDrop;
  }
  log_warn("[PB] drop chain head: level=%d queue_level=%d action=%d", (int)level, (int)ql, (int)action);
  return PbQueueDecision::kDrop;
}

void AsrChatClient::pbSeqTrackPush(int8_t level) {
  if (pb_seq_level_count_ < kPbMaxTrackedSeqLevels) {
    pb_seq_levels_[pb_seq_level_count_++] = level;
  }
}

void AsrChatClient::pbSeqTrackPop() {
  if (pb_seq_level_count_ == 0) {
    return;
  }
  pb_seq_level_count_--;
  for (size_t i = 0; i < pb_seq_level_count_; i++) {
    pb_seq_levels_[i] = pb_seq_levels_[i + 1];
  }
}

static bool pb_parse_rtc_mode(const JsonDocument& doc, bool* interruptible) {
  if (!interruptible || !doc["rtc_mode"].is<String>()) {
    return false;
  }
  String mode = doc["rtc_mode"].as<String>();
  mode.toLowerCase();
  if (mode == "stable") {
    *interruptible = false;
    return true;
  }
  if (mode == "interruptible" || mode == "esp32_aec") {
    *interruptible = true;
    return true;
  }
  return false;
}

static uint32_t pb_effective_chunk_ms(const JsonDocument& doc,
                                      uint32_t declared_ms) {
  constexpr uint32_t kMaxChunkMs = 300000u;
  uint32_t effective_ms =
      declared_ms > kMaxChunkMs ? kMaxChunkMs : declared_ms;
  /* A declared row span owns the shared chunk-to-chunk clock. Servo/display
   * are independent lanes and may intentionally continue across later rows.
   * Audio with no declaration remains at zero until its BIN is decoded. */
  if (declared_ms > 0) {
    return effective_ms;
  }
  if (pb_read_audio_next_bin_len(doc) > 0) {
    return 0u;
  }
  uint32_t anim_ms = 0;
  if (doc["anim"].is<JsonArrayConst>()) {
    for (JsonObjectConst seg : doc["anim"].as<JsonArrayConst>()) {
      const int raw = pb_json_number_to_int(seg["ms"], 0);
      const uint32_t requested =
          raw > 0 ? static_cast<uint32_t>(raw) : 1u;
      const uint32_t ms =
          requested > kMaxChunkMs ? kMaxChunkMs : requested;
      anim_ms = anim_ms > kMaxChunkMs - ms ? kMaxChunkMs : anim_ms + ms;
    }
  }
  uint32_t servo_ms = 0;
  if (doc["servo"].is<JsonArrayConst>()) {
    for (JsonObjectConst seg : doc["servo"].as<JsonArrayConst>()) {
      const int raw = pb_json_number_to_int(seg["ms"], 0);
      const uint32_t requested =
          raw > 0 ? static_cast<uint32_t>(raw) : 50u;
      const uint32_t ms =
          requested > kMaxChunkMs ? kMaxChunkMs : requested;
      servo_ms =
          servo_ms > kMaxChunkMs - ms ? kMaxChunkMs : servo_ms + ms;
    }
  }
  if (anim_ms > effective_ms) {
    effective_ms = anim_ms;
  }
  if (servo_ms > effective_ms) {
    effective_ms = servo_ms;
  }
  /*
   * Leave an audio-only chunk at zero until its BIN has been decoded.  Using
   * a synthetic 1 ms duration here makes every later chunk share an almost
   * identical deadline and forces the audio worker to discard most samples.
   * pbFinishChunkBins() derives the real duration from decoded PCM, which is
   * valid for both s16le and variable-size Opus payloads.
   */
  return effective_ms;
}

AsrChatClient::PbTerminalTrack* AsrChatClient::pbFindTerminalTrack(
    uint32_t epoch) {
  if (epoch == 0) {
    return nullptr;
  }
  for (auto& track : pb_terminal_tracks_) {
    if (track.used && track.epoch == epoch) {
      return &track;
    }
  }
  return nullptr;
}

bool AsrChatClient::pbEnsureCompletedReqCacheLoaded() {
  if (pb_completed_req_cache_loaded_) {
    return true;
  }

  /*
   * AsrChatClient is a global object, so its constructor runs before the
   * Arduino core has initialised NVS.  Load lazily on the first PB request
   * instead.  This is a one-shot attempt: even if NVS is unavailable, the
   * in-memory idempotency window must remain usable for this boot.
   */
  pb_completed_req_cache_loaded_ = true;
  DeskbotPbCompletedRecord stored[kPbCompletedReqCacheDepth]{};
  size_t stored_count = 0;
  if (!deskbot_pb_completed_load(stored, kPbCompletedReqCacheDepth,
                                 &stored_count)) {
    log_error("[PB] completed cache restore failed; using RAM window");
    return false;
  }

  for (size_t i = 0; i < stored_count; ++i) {
    const DeskbotPbCompletedRecord& record = stored[i];
    PbSequenceOutcome outcome = PbSequenceOutcome::kPending;
    switch (record.outcome) {
      case DeskbotPbStoredOutcome::kPlayed:
        outcome = PbSequenceOutcome::kPlayed;
        break;
      case DeskbotPbStoredOutcome::kFailed:
        outcome = PbSequenceOutcome::kFailed;
        break;
      case DeskbotPbStoredOutcome::kCancelled:
        outcome = PbSequenceOutcome::kCancelled;
        break;
    }
    if (outcome == PbSequenceOutcome::kPending || record.req[0] == '\0') {
      continue;
    }

    bool duplicate = false;
    for (const auto& completed : pb_completed_req_cache_) {
      if (completed.used && completed.req == record.req) {
        duplicate = true;
        break;
      }
    }
    if (duplicate) {
      continue;
    }

    PbCompletedReq& completed =
        pb_completed_req_cache_[pb_completed_req_cache_cursor_];
    completed = PbCompletedReq{};
    completed.used = true;
    completed.persisted = true;
    completed.req = record.req;
    completed.idx = record.idx;
    completed.outcome = outcome;
    completed.display_crc_valid =
        outcome == PbSequenceOutcome::kPlayed &&
        record.display_crc_valid;
    completed.display_crc32 =
        completed.display_crc_valid ? record.display_crc32 : 0u;
    pb_completed_req_cache_cursor_ = static_cast<uint8_t>(
        (pb_completed_req_cache_cursor_ + 1) %
        kPbCompletedReqCacheDepth);
  }
  log_info("[PB] completed idempotency window ready restored=%u",
           (unsigned)stored_count);
  return true;
}

const AsrChatClient::PbCompletedReq* AsrChatClient::pbFindCompletedReq(
    const String& req) {
  (void)pbEnsureCompletedReqCacheLoaded();
  if (req.isEmpty()) {
    return nullptr;
  }
  for (const auto& completed : pb_completed_req_cache_) {
    if (completed.used && completed.req == req) {
      return &completed;
    }
  }
  for (const auto& completed : pb_volatile_completed_req_cache_) {
    if (completed.used && completed.req == req) {
      return &completed;
    }
  }
  return nullptr;
}

bool AsrChatClient::pbPersistCompletedReq(const char* req) {
  (void)pbEnsureCompletedReqCacheLoaded();
  if (req == nullptr || req[0] == '\0') {
    return false;
  }

  PbCompletedReq* found = nullptr;
  for (auto& completed : pb_completed_req_cache_) {
    if (completed.used && completed.req == req) {
      found = &completed;
      break;
    }
  }
  bool promote_from_volatile = false;
  if (found == nullptr) {
    for (auto& completed : pb_volatile_completed_req_cache_) {
      if (completed.used && completed.req == req) {
        found = &completed;
        promote_from_volatile = true;
        break;
      }
    }
  }
  if (found == nullptr || found->outcome == PbSequenceOutcome::kPending) {
    return false;
  }
  if (found->persisted) {
    return true;
  }
  if (found->req.length() > DESKBOT_PB_REQ_MAX_CHARS) {
    log_error("[PB] terminal req too long for durable cache len=%u",
              (unsigned)found->req.length());
    return false;
  }

  DeskbotPbStoredOutcome stored_outcome =
      DeskbotPbStoredOutcome::kFailed;
  switch (found->outcome) {
    case PbSequenceOutcome::kPlayed:
      stored_outcome = DeskbotPbStoredOutcome::kPlayed;
      break;
    case PbSequenceOutcome::kFailed:
      stored_outcome = DeskbotPbStoredOutcome::kFailed;
      break;
    case PbSequenceOutcome::kCancelled:
      stored_outcome = DeskbotPbStoredOutcome::kCancelled;
      break;
    case PbSequenceOutcome::kPending:
      return false;
  }

  DeskbotPbCompletedRecord record{};
  strncpy(record.req, found->req.c_str(), sizeof(record.req) - 1);
  record.req[sizeof(record.req) - 1] = '\0';
  record.idx = found->idx;
  record.outcome = stored_outcome;
  record.display_crc_valid =
      found->outcome == PbSequenceOutcome::kPlayed &&
      found->display_crc_valid;
  record.display_crc32 =
      record.display_crc_valid ? found->display_crc32 : 0u;
  if (!deskbot_pb_completed_store_record(record)) {
    log_error("[PB] terminal outcome not persisted req=%s idx=%u",
              found->req.c_str(), (unsigned)found->idx);
    return false;
  }
  if (promote_from_volatile) {
    PbCompletedReq& durable =
        pb_completed_req_cache_[pb_completed_req_cache_cursor_];
    durable = *found;
    durable.persisted = true;
    if (!durable.used || durable.req != req) {
      log_error("[PB] durable RAM cache promotion failed req=%s",
                req);
      found->persisted = true;
      return true;
    }
    *found = PbCompletedReq{};
    pb_completed_req_cache_cursor_ = static_cast<uint8_t>(
        (pb_completed_req_cache_cursor_ + 1) %
        kPbCompletedReqCacheDepth);
  } else {
    found->persisted = true;
  }
  return true;
}

bool AsrChatClient::pbRememberCompletedReq(const char* req, uint32_t idx,
                                           PbSequenceOutcome outcome,
                                           bool persist,
                                           bool display_crc_valid,
                                           uint32_t display_crc32) {
  (void)pbEnsureCompletedReqCacheLoaded();
  if (req == nullptr || req[0] == '\0' ||
      outcome == PbSequenceOutcome::kPending) {
    return false;
  }
  for (auto& completed : pb_completed_req_cache_) {
    if (completed.used && completed.req == req) {
      /* The first terminal phase wins.  A late worker event/reset must never
       * downgrade played or replace failed/cancelled with played. */
      if (persist && !completed.persisted) {
        if (!pbPersistCompletedReq(req)) {
          completed.outcome = PbSequenceOutcome::kFailed;
          completed.persisted = false;
          return false;
        }
      }
      return true;
    }
  }
  for (auto& completed : pb_volatile_completed_req_cache_) {
    if (completed.used && completed.req == req) {
      if (persist && !completed.persisted) {
        if (!pbPersistCompletedReq(req)) {
          completed.outcome = PbSequenceOutcome::kFailed;
          completed.persisted = false;
          return false;
        }
      }
      return true;
    }
  }
  PbCompletedReq& completed =
      pb_volatile_completed_req_cache_[
          pb_volatile_completed_req_cache_cursor_];
  completed = PbCompletedReq{};
  completed.used = true;
  completed.req = req;
  completed.idx = idx;
  completed.outcome = outcome;
  completed.display_crc_valid =
      outcome == PbSequenceOutcome::kPlayed && display_crc_valid;
  completed.display_crc32 =
      completed.display_crc_valid ? display_crc32 : 0u;
  pb_volatile_completed_req_cache_cursor_ = static_cast<uint8_t>(
      (pb_volatile_completed_req_cache_cursor_ + 1) %
      kPbCompletedReqCacheDepth);
  if (persist) {
    if (!pbPersistCompletedReq(req)) {
      completed.outcome = PbSequenceOutcome::kFailed;
      completed.persisted = false;
      return false;
    }
  }
  return true;
}

bool AsrChatClient::pbBeginTerminalTrack(const String& req, uint32_t idx,
                                         bool durable) {
  for (auto& track : pb_terminal_tracks_) {
    if (track.used) {
      continue;
    }
    track = PbTerminalTrack{};
    track.used = true;
    pb_terminal_epoch_counter_++;
    if (pb_terminal_epoch_counter_ == 0) {
      pb_terminal_epoch_counter_++;
    }
    track.epoch = pb_terminal_epoch_counter_;
    track.req = req;
    track.created_ms = millis();
    track.lease_deadline_ms =
        track.created_ms + kPbTerminalIngressLeaseMs;
    track.last_idx = idx;
    track.final_idx = idx;
    track.durable = durable;
    pb_current_epoch_ = track.epoch;
    return true;
  }
  log_error("[PB] terminal tracker full req=%s idx=%u",
            req.c_str(), (unsigned)idx);
  pbQueueTerminalAck(req.c_str(), idx, PbSequenceOutcome::kFailed,
                     durable);
  pb_current_epoch_ = 0;
  return false;
}

void AsrChatClient::pbRefreshTerminalLease(
    PbTerminalTrack& track, uint32_t expected_finish_ms) {
  if (!track.used || track.outcome != PbSequenceOutcome::kPending) {
    return;
  }
  const uint32_t now = millis();
  const uint32_t elapsed_ms = now - track.created_ms;
  if (elapsed_ms >= kPbTerminalHardLeaseMs) {
    track.lease_deadline_ms = now;
    return;
  }

  uint32_t remaining_ms = kPbTerminalProgressLeaseMs;
  if (expected_finish_ms != 0) {
    const uint32_t until_finish_ms =
        deskbot_pb_time_remaining(now, expected_finish_ms);
    const uint32_t finish_and_grace_ms =
        until_finish_ms > UINT32_MAX - kPbTerminalWorkerGraceMs
            ? UINT32_MAX
            : until_finish_ms + kPbTerminalWorkerGraceMs;
    if (finish_and_grace_ms > remaining_ms) {
      remaining_ms = finish_and_grace_ms;
    }
  }

  const uint32_t hard_remaining_ms =
      kPbTerminalHardLeaseMs - elapsed_ms;
  if (remaining_ms > hard_remaining_ms) {
    remaining_ms = hard_remaining_ms;
  }
  /* A later nominal timeline/seal refresh must not shorten a lease already
   * extended for the motor lane's physical rate limit. */
  const uint32_t current_remaining_ms =
      deskbot_pb_time_remaining(now, track.lease_deadline_ms);
  if (current_remaining_ms > remaining_ms) {
    remaining_ms = current_remaining_ms > hard_remaining_ms
                       ? hard_remaining_ms
                       : current_remaining_ms;
  }
  track.lease_deadline_ms = now + remaining_ms;
}

uint32_t AsrChatClient::pbArmCurrentChunkTimeline(uint32_t idx,
                                                  uint32_t chunk_ms) {
  constexpr uint32_t kStartLeadMs = 80u;
  constexpr uint32_t kMaxTimelineOffsetMs = 30u * 60u * 1000u;
  PbTerminalTrack* track = pbFindTerminalTrack(pb_current_epoch_);
  if (track == nullptr || track->outcome != PbSequenceOutcome::kPending) {
    return 0;
  }
  if (track->timeline_start_ms == 0) {
    track->timeline_start_ms = millis() + kStartLeadMs;
    if (track->timeline_start_ms == 0) {
      track->timeline_start_ms = 1;
    }
  }
  const uint32_t span_ms = chunk_ms > 0 ? chunk_ms : 1u;
  const uint32_t room =
      kMaxTimelineOffsetMs - track->timeline_next_offset_ms;
  if (span_ms > room) {
    log_error("[PB] timeline exceeds sequence limit epoch=%u req=%s idx=%u "
              "offset=%u span=%u max=%u",
              (unsigned)track->epoch, track->req.c_str(), (unsigned)idx,
              (unsigned)track->timeline_next_offset_ms, (unsigned)span_ms,
              (unsigned)kMaxTimelineOffsetMs);
    return 0;
  }
  const uint32_t start_at_ms =
      track->timeline_start_ms + track->timeline_next_offset_ms;
  track->timeline_next_offset_ms += span_ms;
  pbRefreshTerminalLease(
      *track, track->timeline_start_ms + track->timeline_next_offset_ms);
  log_info("[PB] timeline epoch=%u req=%s idx=%u start_at=%u span=%u "
           "next_offset=%u now=%u",
           (unsigned)track->epoch, track->req.c_str(), (unsigned)idx,
           (unsigned)start_at_ms, (unsigned)span_ms,
           (unsigned)track->timeline_next_offset_ms, (unsigned)millis());
  return start_at_ms;
}

void AsrChatClient::pbNoteModalitySubmitted(PbModality modality) {
  PbTerminalTrack* track = pbFindTerminalTrack(pb_current_epoch_);
  if (track == nullptr || track->outcome != PbSequenceOutcome::kPending) {
    return;
  }
  switch (modality) {
    case PbModality::kAudio:
      track->audio_expected = true;
      break;
    case PbModality::kDisplay:
      if (track->display_expected < UINT32_MAX) {
        track->display_expected++;
      }
      break;
    case PbModality::kMotor:
      if (track->motor_expected < UINT32_MAX) {
        track->motor_expected++;
      }
      break;
  }
}

void AsrChatClient::pbQueueTerminalAck(const char* req, uint32_t idx,
                                       PbSequenceOutcome outcome,
                                       bool persist, bool pose_valid,
                                       int pose_x, int pose_y,
                                       bool display_crc_valid,
                                       uint32_t display_crc32) {
  if (req == nullptr || req[0] == '\0' ||
      outcome == PbSequenceOutcome::kPending) {
    return;
  }
  if (!pbRememberCompletedReq(req, idx, outcome, persist,
                              display_crc_valid, display_crc32) && persist) {
    log_error("[PB] durable terminal persistence failed req=%s idx=%u; "
              "reporting failed instead of played",
              req, (unsigned)idx);
    outcome = PbSequenceOutcome::kFailed;
  }
  const PbCompletedReq* remembered = pbFindCompletedReq(String(req));
  if (remembered != nullptr &&
      remembered->outcome != PbSequenceOutcome::kPending) {
    outcome = remembered->outcome;
    display_crc_valid =
        outcome == PbSequenceOutcome::kPlayed &&
        remembered->display_crc_valid;
    display_crc32 =
        display_crc_valid ? remembered->display_crc32 : 0u;
  } else if (outcome != PbSequenceOutcome::kPlayed) {
    display_crc_valid = false;
    display_crc32 = 0;
  }
  for (uint8_t pos = pb_terminal_ack_head_;
       pos != pb_terminal_ack_tail_;
       pos = static_cast<uint8_t>((pos + 1) %
                                  kPbTerminalAckQueueDepth)) {
    if (pb_terminal_ack_queue_[pos].req == req) {
      /* A sequence has exactly one terminal phase.  In particular a late
       * completion may never replace failed/cancelled with played. */
      return;
    }
  }
  const uint8_t next_tail = static_cast<uint8_t>(
      (pb_terminal_ack_tail_ + 1) % kPbTerminalAckQueueDepth);
  if (next_tail == pb_terminal_ack_head_) {
    log_error("[PB] terminal ACK queue full req=%s idx=%u outcome=%u",
              req, (unsigned)idx, (unsigned)outcome);
    return;
  }
  PbTerminalAck& ack = pb_terminal_ack_queue_[pb_terminal_ack_tail_];
  ack.req = req;
  ack.idx = idx;
  ack.outcome = outcome;
  ack.commanded_x = static_cast<int16_t>(constrain(
      pose_valid ? pose_x : head_read_x(), X_MIN_LIMIT, X_MAX_LIMIT));
  ack.commanded_y = static_cast<int16_t>(constrain(
      pose_valid ? pose_y : head_read_y_logic(), Y_MIN_LIMIT, Y_MAX_LIMIT));
  ack.display_crc_valid = display_crc_valid;
  ack.display_crc32 = display_crc32;
  pb_terminal_ack_tail_ = next_tail;
  pb_ack_bypass_throttle_ = true;
}

bool AsrChatClient::pbPopTerminalAck(PbTerminalAck* out) {
  if (out == nullptr ||
      pb_terminal_ack_head_ == pb_terminal_ack_tail_) {
    return false;
  }
  *out = pb_terminal_ack_queue_[pb_terminal_ack_head_];
  pb_terminal_ack_queue_[pb_terminal_ack_head_].req.remove(0);
  pb_terminal_ack_head_ = static_cast<uint8_t>(
      (pb_terminal_ack_head_ + 1) % kPbTerminalAckQueueDepth);
  return true;
}

void AsrChatClient::pbEvaluateTerminalTrack(PbTerminalTrack& track) {
  if (!track.used || track.outcome != PbSequenceOutcome::kPending ||
      !track.sealed) {
    return;
  }
  const bool audio_done =
      !track.audio_expected || track.audio_completed;
  const bool display_done =
      track.display_completed == track.display_expected;
  const bool motor_done =
      track.motor_completed == track.motor_expected;
  if (!audio_done || !display_done || !motor_done) {
    return;
  }
  track.outcome = PbSequenceOutcome::kPlayed;
  log_info("[PB] all modalities terminal-success epoch=%u req=%s idx=%u "
           "audio=%u display=%u/%u motor=%u/%u",
           (unsigned)track.epoch, track.req.c_str(),
           (unsigned)track.final_idx, (unsigned)track.audio_expected,
           (unsigned)track.display_completed,
           (unsigned)track.display_expected,
           (unsigned)track.motor_completed,
           (unsigned)track.motor_expected);
  pbQueueTerminalAck(track.req.c_str(), track.final_idx,
                       PbSequenceOutcome::kPlayed, track.durable,
                       track.motor_pose_valid, track.motor_commanded_x,
                       track.motor_commanded_y, track.display_crc_valid,
                       track.display_crc32);
  if (pb_current_epoch_ == track.epoch) {
    pb_current_epoch_ = 0;
  }
  track.used = false;
  track.req.remove(0);
}

void AsrChatClient::pbSealCurrentTerminalTrack(uint32_t final_idx) {
  PbTerminalTrack* track = pbFindTerminalTrack(pb_current_epoch_);
  if (track == nullptr) {
    return;
  }
  track->sealed = true;
  track->final_idx = final_idx;
  if (final_idx > track->last_idx) {
    track->last_idx = final_idx;
  }
  const uint32_t expected_finish_ms =
      track->timeline_start_ms == 0
          ? 0
          : track->timeline_start_ms + track->timeline_next_offset_ms;
  pbRefreshTerminalLease(*track, expected_finish_ms);
  if (track->outcome != PbSequenceOutcome::kPending) {
    track->used = false;
    track->req.remove(0);
    pb_current_epoch_ = 0;
    return;
  }
  pbEvaluateTerminalTrack(*track);
}

void AsrChatClient::pbHandleWorkerTerminal(
    uint32_t epoch, const char* req, uint32_t idx, PbModality modality,
    PbWorkerOutcome outcome, bool pose_valid, int pose_x, int pose_y,
    bool display_crc_valid, uint32_t display_crc32) {
  PbTerminalTrack* track = pbFindTerminalTrack(epoch);
  if (track == nullptr) {
    return;
  }
  if (track->outcome != PbSequenceOutcome::kPending) {
    return;
  }
  if (req != nullptr && req[0] != '\0' && track->req != req) {
    log_error("[PB] terminal identity mismatch epoch=%u track_req=%s event_req=%s",
              (unsigned)epoch, track->req.c_str(), req);
    outcome = PbWorkerOutcome::kFailed;
  }
  if (idx > track->last_idx) {
    track->last_idx = idx;
  }
  if (outcome != PbWorkerOutcome::kCompleted) {
    track->outcome =
        outcome == PbWorkerOutcome::kCancelled
            ? PbSequenceOutcome::kCancelled
            : PbSequenceOutcome::kFailed;
    const uint32_t terminal_idx =
        track->sealed ? track->final_idx : track->last_idx;
    log_warn("[PB] modality terminal-%s epoch=%u req=%s idx=%u modality=%u",
             track->outcome == PbSequenceOutcome::kCancelled
                 ? "cancelled"
                 : "failed",
             (unsigned)track->epoch, track->req.c_str(),
             (unsigned)terminal_idx, (unsigned)modality);
    const bool terminal_pose_valid = pose_valid || track->motor_pose_valid;
    const int terminal_pose_x =
        pose_valid ? pose_x : track->motor_commanded_x;
    const int terminal_pose_y =
        pose_valid ? pose_y : track->motor_commanded_y;
    pbQueueTerminalAck(track->req.c_str(), terminal_idx, track->outcome,
                       track->durable, terminal_pose_valid, terminal_pose_x,
                       terminal_pose_y);
    if (pb_current_epoch_ == track->epoch) {
      pb_current_epoch_ = 0;
    }
    track->used = false;
    track->req.remove(0);
    return;
  }

  switch (modality) {
    case PbModality::kAudio:
      if (!track->audio_expected) {
        outcome = PbWorkerOutcome::kFailed;
      } else {
        track->audio_completed = true;
      }
      break;
    case PbModality::kDisplay:
      if (track->display_completed >= track->display_expected) {
        outcome = PbWorkerOutcome::kFailed;
      } else {
        track->display_completed++;
        if (display_crc_valid &&
            (!track->display_crc_valid || idx >= track->display_crc_idx)) {
          track->display_crc_valid = true;
          track->display_crc_idx = idx;
          track->display_crc32 = display_crc32;
        }
      }
      break;
    case PbModality::kMotor:
      if (track->motor_completed >= track->motor_expected) {
        outcome = PbWorkerOutcome::kFailed;
      } else {
        track->motor_completed++;
        if (pose_valid &&
            (!track->motor_pose_valid || idx >= track->motor_pose_idx)) {
          track->motor_pose_valid = true;
          track->motor_pose_idx = idx;
          track->motor_commanded_x = static_cast<int16_t>(constrain(
              pose_x, X_MIN_LIMIT, X_MAX_LIMIT));
          track->motor_commanded_y = static_cast<int16_t>(constrain(
              pose_y, Y_MIN_LIMIT, Y_MAX_LIMIT));
        }
      }
      break;
  }
  if (outcome == PbWorkerOutcome::kFailed) {
    pbHandleWorkerTerminal(epoch, req, idx, modality,
                           PbWorkerOutcome::kFailed, pose_valid, pose_x,
                           pose_y);
    return;
  }
  const uint32_t expected_finish_ms =
      track->timeline_start_ms == 0
          ? 0
          : track->timeline_start_ms + track->timeline_next_offset_ms;
  pbRefreshTerminalLease(*track, expected_finish_ms);
  pbEvaluateTerminalTrack(*track);
}

void AsrChatClient::pbFailTracksWaitingForModality(
    PbModality modality, uint32_t dropped_count) {
  for (auto& track : pb_terminal_tracks_) {
    if (!track.used || track.outcome != PbSequenceOutcome::kPending) {
      continue;
    }
    bool waiting = false;
    switch (modality) {
      case PbModality::kAudio:
        waiting = track.audio_expected && !track.audio_completed;
        break;
      case PbModality::kDisplay:
        waiting = track.display_completed < track.display_expected;
        break;
      case PbModality::kMotor:
        waiting = track.motor_completed < track.motor_expected;
        break;
    }
    if (!waiting) {
      continue;
    }
    const uint32_t terminal_idx =
        track.sealed ? track.final_idx : track.last_idx;
    log_error("[PB] terminal event loss; fail closed epoch=%u req=%s idx=%u "
              "modality=%u dropped=%u",
              (unsigned)track.epoch, track.req.c_str(),
              (unsigned)terminal_idx, (unsigned)modality,
              (unsigned)dropped_count);
    track.outcome = PbSequenceOutcome::kFailed;
    pbQueueTerminalAck(track.req.c_str(), terminal_idx,
                       PbSequenceOutcome::kFailed, track.durable);
    if (pb_current_epoch_ == track.epoch) {
      pb_current_epoch_ = 0;
    }
    track.used = false;
    track.req.remove(0);
  }
}

void AsrChatClient::pbDrainWorkerTerminals() {
  AudioPbTerminalEvent audio{};
  while (audio_take_pb_terminal_event(&audio)) {
    pbHandleWorkerTerminal(
        audio.epoch, audio.req, audio.idx, PbModality::kAudio,
        audio.state == AudioPbTerminalState::kCompleted
            ? PbWorkerOutcome::kCompleted
            : audio.state == AudioPbTerminalState::kCancelled
                  ? PbWorkerOutcome::kCancelled
                  : PbWorkerOutcome::kFailed);
  }
  DisplayPbTerminalEvent display{};
  while (display_take_pb_terminal_event(&display)) {
    pbHandleWorkerTerminal(
        display.epoch, display.req, display.idx, PbModality::kDisplay,
        display.state == DisplayPbTerminalState::kCompleted
            ? PbWorkerOutcome::kCompleted
            : display.state == DisplayPbTerminalState::kCancelled
                  ? PbWorkerOutcome::kCancelled
                  : PbWorkerOutcome::kFailed,
        /*pose_valid=*/false, /*pose_x=*/0, /*pose_y=*/0,
        display.display_crc_valid, display.display_crc32);
  }
  HeadPbTerminalEvent motor{};
  while (head_take_pb_terminal_event(&motor)) {
    pbHandleWorkerTerminal(
        motor.epoch, motor.req, motor.idx, PbModality::kMotor,
        motor.state == HeadPbTerminalState::kCompleted
            ? PbWorkerOutcome::kCompleted
            : motor.state == HeadPbTerminalState::kCancelled
                  ? PbWorkerOutcome::kCancelled
                  : PbWorkerOutcome::kFailed,
        motor.pose_valid, motor.commanded_x, motor.commanded_y);
  }

  const uint32_t audio_drop_count = audio_pb_terminal_drop_count();
  if (audio_drop_count != pb_audio_terminal_drop_seen_) {
    const uint32_t dropped =
        audio_drop_count - pb_audio_terminal_drop_seen_;
    pb_audio_terminal_drop_seen_ = audio_drop_count;
    pbFailTracksWaitingForModality(PbModality::kAudio, dropped);
  }
  const uint32_t display_drop_count = display_pb_terminal_drop_count();
  if (display_drop_count != pb_display_terminal_drop_seen_) {
    const uint32_t dropped =
        display_drop_count - pb_display_terminal_drop_seen_;
    pb_display_terminal_drop_seen_ = display_drop_count;
    pbFailTracksWaitingForModality(PbModality::kDisplay, dropped);
  }
  const uint32_t motor_drop_count = head_pb_terminal_drop_count();
  if (motor_drop_count != pb_motor_terminal_drop_seen_) {
    const uint32_t dropped =
        motor_drop_count - pb_motor_terminal_drop_seen_;
    pb_motor_terminal_drop_seen_ = motor_drop_count;
    pbFailTracksWaitingForModality(PbModality::kMotor, dropped);
  }
}

void AsrChatClient::pbExpireTerminalTracks() {
  const uint32_t now = millis();
  for (auto& track : pb_terminal_tracks_) {
    if (!track.used || track.outcome != PbSequenceOutcome::kPending ||
        !deskbot_pb_time_reached(now, track.lease_deadline_ms)) {
      continue;
    }
    const uint32_t terminal_idx =
        track.sealed ? track.final_idx : track.last_idx;
    log_error("[PB] terminal lease expired epoch=%u req=%s idx=%u "
              "age_ms=%u timeline_ms=%u",
              (unsigned)track.epoch, track.req.c_str(),
              (unsigned)terminal_idx,
              (unsigned)(now - track.created_ms),
              (unsigned)track.timeline_next_offset_ms);
    track.outcome = PbSequenceOutcome::kFailed;
    pbQueueTerminalAck(track.req.c_str(), terminal_idx,
                       PbSequenceOutcome::kFailed, track.durable);
    if (pb_current_epoch_ == track.epoch) {
      pb_current_epoch_ = 0;
    }
    track.used = false;
    track.req.remove(0);
  }
}

void AsrChatClient::pbFailCurrentTerminal(uint32_t idx, const char* why) {
  PbTerminalTrack* track = pbFindTerminalTrack(pb_current_epoch_);
  if (track == nullptr) {
    return;
  }
  if (track->outcome != PbSequenceOutcome::kPending) {
    return;
  }
  if (idx > track->last_idx) {
    track->last_idx = idx;
  }
  track->outcome = PbSequenceOutcome::kFailed;
  log_warn("[PB] sequence failed epoch=%u req=%s idx=%u why=%s",
           (unsigned)track->epoch, track->req.c_str(),
           (unsigned)track->last_idx, why ? why : "?");
  pbQueueTerminalAck(track->req.c_str(), track->last_idx,
                     PbSequenceOutcome::kFailed, track->durable);
}

void AsrChatClient::pbCancelTerminalTracks(const char* why,
                                           const char* req) {
  for (auto& track : pb_terminal_tracks_) {
    if (!track.used || track.outcome != PbSequenceOutcome::kPending) {
      continue;
    }
    if (req != nullptr && req[0] != '\0' && track.req != req) {
      continue;
    }
    const uint32_t terminal_idx =
        track.sealed ? track.final_idx : track.last_idx;
    log_warn("[PB] sequence cancelled epoch=%u req=%s idx=%u why=%s",
             (unsigned)track.epoch, track.req.c_str(),
             (unsigned)terminal_idx, why ? why : "?");
    track.outcome = PbSequenceOutcome::kCancelled;
    pbQueueTerminalAck(track.req.c_str(), terminal_idx,
                       PbSequenceOutcome::kCancelled, track.durable);
    if (pb_current_epoch_ == track.epoch) {
      pb_current_epoch_ = 0;
    }
    track.used = false;
    track.req.remove(0);
  }
}

void AsrChatClient::pbCancelTerminalTracksForLanes(
    bool display, bool motor, bool audio, const char* why) {
  for (auto& track : pb_terminal_tracks_) {
    if (!track.used || track.outcome != PbSequenceOutcome::kPending) {
      continue;
    }
    const bool display_waiting =
        display && track.display_completed < track.display_expected;
    const bool motor_waiting =
        motor && track.motor_completed < track.motor_expected;
    const bool audio_waiting =
        audio && track.audio_expected && !track.audio_completed;
    if (!display_waiting && !motor_waiting && !audio_waiting) {
      continue;
    }
    const uint32_t terminal_idx =
        track.sealed ? track.final_idx : track.last_idx;
    log_warn(
        "[PB] sequence lane-cancelled epoch=%u req=%s idx=%u "
        "display=%d motor=%d audio=%d why=%s",
        (unsigned)track.epoch, track.req.c_str(), (unsigned)terminal_idx,
        (int)display_waiting, (int)motor_waiting, (int)audio_waiting,
        why ? why : "?");
    track.outcome = PbSequenceOutcome::kCancelled;
    pbQueueTerminalAck(track.req.c_str(), terminal_idx,
                       PbSequenceOutcome::kCancelled, track.durable);
    if (pb_current_epoch_ == track.epoch) {
      pb_current_epoch_ = 0;
    }
    track.used = false;
    track.req.remove(0);
  }
}

bool AsrChatClient::pbHasPendingTerminalTracks() const {
  for (const auto& track : pb_terminal_tracks_) {
    if (track.used && track.outcome == PbSequenceOutcome::kPending) {
      return true;
    }
  }
  return false;
}

bool AsrChatClient::pbHasPendingAudioTerminalTracks() const {
  for (const auto& track : pb_terminal_tracks_) {
    if (track.used && track.outcome == PbSequenceOutcome::kPending &&
        track.audio_expected && !track.audio_completed) {
      return true;
    }
  }
  return false;
}

void AsrChatClient::pbDrainWorkersForNewSequence(bool replace_display,
                                                 bool replace_motor,
                                                 bool replace_audio,
                                                 bool cancel_terminals,
                                                 bool preserve_display_baseline) {
  if (cancel_terminals) {
    pbCancelTerminalTracksForLanes(
        replace_display, replace_motor, replace_audio,
        "replaced by new PB sequence");
  }
  if (!replace_motor) {
    /* 手势 pb_single：只停音频/显示，不动 motor 队列。 */
    if (replace_audio) {
      audio_play_reset();
    }
    if (replace_display) {
      display_render_replace(preserve_display_baseline);
    }
  } else {
    if (replace_audio) {
      audio_play_reset();
    }
    if (replace_display) {
      display_render_replace(preserve_display_baseline);
    }
    head_clear_motor_pending();
  }
  pbReset(/*stop_audio=*/false, /*preserve_pending_terminals=*/true);
  if (replace_audio) {
    pb_audio_stream_started_ = false;
  }
}

void AsrChatClient::pbOnSequenceComplete() {
  if (pb_inflight_seq_count_ > 0) {
    pb_inflight_seq_count_--;
  }
  pbSeqTrackPop();
  if (pb_inflight_seq_count_ == 0) {
    pb_queue_level_ = -1;
    pb_active_ = false;
    pbSignalTtsRoundComplete();
  }
}

AsrChatClient::AsrChatClient() {}

bool AsrChatClient::isReady() const {
  return ready_ && usb_transport_is_active() &&
         deskbot_uplink_transport_ready();
}

void AsrChatClient::enableUsbTransport() {
  resetUsbPbJsonAssembly();
  usb_session_epoch_ = 0;
  rtc_audio_downlink_cancel();
  ready_ = false;
  transport_send_fail_streak_ = 0;
}

void AsrChatClient::onUsbLinkState(bool ready, uint32_t session_epoch) {
  if (!ready) {
    resetUsbPbJsonAssembly();
    rtc_audio_downlink_cancel();
    if (ready_ || usb_session_epoch_ != 0) {
      abortRuntimeState("USB session ended");
    }
    usb_session_epoch_ = 0;
    ready_ = false;
    transport_send_fail_streak_ = 0;
    return;
  }

  if (usb_session_epoch_ != 0 && usb_session_epoch_ != session_epoch) {
    resetUsbPbJsonAssembly();
    abortRuntimeState("USB session epoch rotated");
  }
  usb_session_epoch_ = session_epoch;
  ready_ = true;
  transport_send_fail_streak_ = 0;
  link_abort_round_ = false;
  /* hello 完成：清掉「请连接PC服务」待机屏，屏幕交还 PC 端表情系统。 */
  display_standby_set(false);
  rtc_audio_downlink_cancel();
  /* Legacy PB Opus is initialized lazily only when a PB bundle actually
   * contains audio.  Keeping its deep SILK stack resident for every RTC-only
   * session needlessly consumed memory and scheduler time. */
  if (!boot_connect_sent_) {
    boot_connect_pending_ = true;
  }
  log_info("[ASR_CHAT] USB transport ready epoch=%u",
           static_cast<unsigned>(session_epoch));
}

void AsrChatClient::resetUsbPbJsonAssembly() {
  if (usb_pb_json_buf_ != nullptr) {
    heap_caps_free(usb_pb_json_buf_);
    usb_pb_json_buf_ = nullptr;
  }
  usb_pb_json_len_ = 0;
  usb_pb_json_capacity_ = 0;
  usb_pb_json_epoch_ = 0;
  usb_pb_json_active_ = false;
}

bool AsrChatClient::appendUsbPbJsonFragment(const uint8_t* payload,
                                            size_t length) {
  if (payload == nullptr || length == 0 ||
      length > kUsbPbJsonMaxBytes - usb_pb_json_len_) {
    return false;
  }
  const size_t required = usb_pb_json_len_ + length;
  if (required > usb_pb_json_capacity_) {
    size_t capacity = usb_pb_json_capacity_ == 0 ? 2048u
                                                 : usb_pb_json_capacity_;
    while (capacity < required && capacity < kUsbPbJsonMaxBytes) {
      capacity *= 2u;
    }
    if (capacity > kUsbPbJsonMaxBytes) {
      capacity = kUsbPbJsonMaxBytes;
    }
    uint8_t* replacement = static_cast<uint8_t*>(
        heap_caps_malloc(capacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (replacement == nullptr) {
      replacement = static_cast<uint8_t*>(
          heap_caps_malloc(capacity, MALLOC_CAP_8BIT));
    }
    if (replacement == nullptr) {
      return false;
    }
    if (usb_pb_json_buf_ != nullptr && usb_pb_json_len_ > 0) {
      memcpy(replacement, usb_pb_json_buf_, usb_pb_json_len_);
      heap_caps_free(usb_pb_json_buf_);
    }
    usb_pb_json_buf_ = replacement;
    usb_pb_json_capacity_ = capacity;
  }
  memcpy(usb_pb_json_buf_ + usb_pb_json_len_, payload, length);
  usb_pb_json_len_ = required;
  return true;
}

void AsrChatClient::dispatchUsbFrame(const DeskbotUsbRxFrame& frame) {
  if (!usb_transport_is_active() ||
      frame.session_epoch != usb_session_epoch_) {
    return;
  }
  uint8_t* const payload = const_cast<uint8_t*>(frame.payload);

  if (frame.channel == DESKBOT_USB_CONTROL_JSON) {
    JsonDocument control;
    if (payload == nullptr || frame.payload_length == 0 ||
        (frame.flags & DESKBOT_USB_FLAG_JSON) == 0 ||
        deserializeJson(control, payload, frame.payload_length)) {
      log_warn("[USB] drop malformed CONTROL_JSON len=%u flags=0x%02x",
               static_cast<unsigned>(frame.payload_length),
               static_cast<unsigned>(frame.flags));
      return;
    }
    const char* const type = control["type"] | "";
    if (strcmp(type, "pb_cancel") == 0) {
      resetUsbPbJsonAssembly();
    }
    if (strncmp(type, "pb_", 3) == 0 && strcmp(type, "pb_cancel") != 0) {
      log_warn("[USB] drop PB message on CONTROL_JSON type=%s", type);
      return;
    }
    onWirePayload(WirePayloadKind::kJson, payload, frame.payload_length);
    return;
  }
  if (frame.channel == DESKBOT_USB_PB_WIRE) {
    if ((frame.flags & DESKBOT_USB_FLAG_JSON) != 0) {
      const bool begin =
          (frame.flags & DESKBOT_USB_FLAG_BEGIN_STREAM) != 0;
      const bool end = (frame.flags & DESKBOT_USB_FLAG_END_STREAM) != 0;
      const bool fragmented = begin || end || usb_pb_json_active_;
      if (!fragmented) {
        onWirePayload(WirePayloadKind::kJson, payload,
                      frame.payload_length);
        return;
      }
      if (begin) {
        if (usb_pb_json_active_) {
          log_warn("[USB] replace incomplete PB JSON fragments len=%u",
                   static_cast<unsigned>(usb_pb_json_len_));
        }
        resetUsbPbJsonAssembly();
        usb_pb_json_active_ = true;
        usb_pb_json_epoch_ = frame.session_epoch;
      } else if (!usb_pb_json_active_ ||
                 usb_pb_json_epoch_ != frame.session_epoch) {
        log_warn("[USB] drop PB JSON fragment without matching BEGIN len=%u "
                 "flags=0x%02x",
                 static_cast<unsigned>(frame.payload_length),
                 static_cast<unsigned>(frame.flags));
        resetUsbPbJsonAssembly();
        return;
      }
      if (!appendUsbPbJsonFragment(payload, frame.payload_length)) {
        log_warn("[USB] PB JSON fragment assembly failed current=%u add=%u",
                 static_cast<unsigned>(usb_pb_json_len_),
                 static_cast<unsigned>(frame.payload_length));
        resetUsbPbJsonAssembly();
        resetTransportSession("PB JSON fragment assembly failed");
        return;
      }
      if (end) {
        uint8_t* const assembled = usb_pb_json_buf_;
        const size_t assembled_length = usb_pb_json_len_;
        usb_pb_json_active_ = false;
        log_debug("[USB] PB JSON fragments complete len=%u",
                  static_cast<unsigned>(assembled_length));
        onWirePayload(WirePayloadKind::kJson, assembled,
                      assembled_length);
        resetUsbPbJsonAssembly();
      }
    } else {
      if (usb_pb_json_active_) {
        log_warn("[USB] binary PB interleaved with JSON fragments");
        resetUsbPbJsonAssembly();
        resetTransportSession("PB JSON fragment interleaving");
        return;
      }
      onWirePayload(WirePayloadKind::kBinary, payload, frame.payload_length);
    }
    return;
  }
  if (frame.channel != DESKBOT_USB_AUDIO_DOWN_OPUS) {
    return;
  }
  if ((frame.flags & DESKBOT_USB_FLAG_CANCEL_STREAM) != 0) {
    const uint8_t incompatible =
        DESKBOT_USB_FLAG_BEGIN_STREAM | DESKBOT_USB_FLAG_END_STREAM |
        DESKBOT_USB_FLAG_JSON;
    if (frame.payload_length != 0 || frame.payload != nullptr ||
        (frame.flags & incompatible) != 0) {
      log_warn(
          "[USB] malformed CANCEL_STREAM len=%u flags=0x%02x",
          static_cast<unsigned>(frame.payload_length),
          static_cast<unsigned>(frame.flags));
      return;
    }
    const bool had_active_stream =
        rtc_audio_downlink_active() || rtc_audio_downlink_closing();
    const uint32_t cancelled_generation =
        rtc_audio_downlink_generation();
    rtc_audio_downlink_cancel();
    if (had_active_stream) {
      /* Generation invalidation stops decode; the emergency flush also
       * removes PCM already owned by audio_play and clears stale I2S DMA. */
      audio_play_emergency_flush();
    }
    log_warn(
        "[USB] CANCEL_STREAM generation=%u active=%d decode_q=%u play_q=%u",
        static_cast<unsigned>(cancelled_generation),
        static_cast<int>(had_active_stream),
        static_cast<unsigned>(rtc_audio_downlink_queue_depth()),
        audio_play_input_queue_depth());
    return;
  }
  /*
   * Standalone AUDIO_DOWN_OPUS is deliberately separate from PB_WIRE.  PB
   * PCM/assets always remain PB_WIRE binary, so an unsolicited audio frame
   * can never consume pb_expect_bin_ and corrupt the PB sequence.
   *
   * Accept either one raw Opus packet or the same u16_be-length-prefixed batch
   * used by AUDIO_UP_OPUS.  For a one-frame prefixed batch, strip its prefix
   * because the asynchronous RTC decoder expects one raw packet.
   */
  if (pb_audio_stream_started_ || pbHasPendingAudioTerminalTracks()) {
    log_warn("[USB] drop standalone audio while PB speaker audio is active");
    return;
  }
  const bool begin_stream =
      (frame.flags & DESKBOT_USB_FLAG_BEGIN_STREAM) != 0;
  if (frame.payload_length == 0) {
    if (begin_stream) {
      log_warn("[USB] standalone BEGIN_STREAM requires an Opus payload");
      return;
    }
    if ((frame.flags & DESKBOT_USB_FLAG_END_STREAM) != 0) {
      if (!rtc_audio_downlink_enqueue_end(frame.session_epoch)) {
        log_warn("[USB] standalone END_STREAM queue rejected");
      }
    }
    return;
  }
  if (frame.payload == nullptr) {
    return;
  }
  const uint8_t* opus = frame.payload;
  size_t opus_length = frame.payload_length;
  uint16_t frames = 0;
  size_t offset = 0;
  size_t parsed_frames = 0;
  bool valid_prefixed_batch = true;
  while (offset < frame.payload_length) {
    if (offset + 2u > frame.payload_length) {
      valid_prefixed_batch = false;
      break;
    }
    const uint16_t packet_length =
        static_cast<uint16_t>((frame.payload[offset] << 8) |
                              frame.payload[offset + 1]);
    if (packet_length == 0 ||
        offset + 2u + packet_length > frame.payload_length) {
      valid_prefixed_batch = false;
      break;
    }
    offset += 2u + packet_length;
    parsed_frames++;
  }
  if (valid_prefixed_batch && parsed_frames > kUsbAudioDownBatchMaxFrames) {
    log_warn("[USB] standalone Opus batch rejected frames=%u max=%u",
             static_cast<unsigned>(parsed_frames),
             static_cast<unsigned>(kUsbAudioDownBatchMaxFrames));
    if ((frame.flags & DESKBOT_USB_FLAG_END_STREAM) != 0) {
      (void)rtc_audio_downlink_enqueue_end(frame.session_epoch);
    }
    return;
  }
  if (valid_prefixed_batch && parsed_frames > 0) {
    frames = static_cast<uint16_t>(parsed_frames);
  }
  if (frames == 1u) {
    opus = frame.payload + 2;
    opus_length = frame.payload_length - 2;
  } else if (frames == 0u) {
    frames = 1;
  }

  if (begin_stream) {
    if (!rtc_audio_downlink_begin_stream(frame.session_epoch)) {
      log_warn("[USB] standalone BEGIN_STREAM rejected");
      return;
    }
  } else if (!rtc_audio_downlink_active()) {
    log_warn("[USB] drop standalone audio without BEGIN_STREAM");
    return;
  } else if (rtc_audio_downlink_closing()) {
    log_warn("[USB] drop standalone audio received after END_STREAM");
    return;
  }

  const bool end_stream =
      (frame.flags & DESKBOT_USB_FLAG_END_STREAM) != 0;
  if (!rtc_audio_downlink_enqueue(
          opus, opus_length, frames, frame.session_epoch, end_stream)) {
    log_warn("[USB] standalone Opus queue rejected len=%u frames=%u",
             static_cast<unsigned>(frame.payload_length),
             static_cast<unsigned>(frames));
    if (end_stream) {
      (void)rtc_audio_downlink_enqueue_end(frame.session_epoch);
    }
  }
  /*
   * Decode and stream start happen on rtc_opus_dec, never in this USB receive
   * callback.  END_STREAM is ordered behind the decode queue and closed by
   * loopLite() only after queued PCM/I2S output drains.
   */
}

void AsrChatClient::pbFreePendingAssets() {
  for (uint8_t i = 0; i < pb_asset_count_; i++) {
    if (pb_asset_bufs_[i]) {
      heap_caps_free(pb_asset_bufs_[i]);
      pb_asset_bufs_[i] = nullptr;
    }
    pb_asset_lens_[i] = 0;
  }
  pb_asset_count_ = 0;
}

void AsrChatClient::pbFreePendingPcm() {
  if (pb_pending_pcm_owned_ != nullptr) {
    if (pb_pending_pcm_free_caps_ == 0) {
      ::free(pb_pending_pcm_owned_);
    } else {
      heap_caps_free(pb_pending_pcm_owned_);
    }
  }
  pb_pending_pcm_owned_ = nullptr;
  pb_pending_pcm_samples_ = 0;
  pb_pending_pcm_free_caps_ = 0;
}

bool AsrChatClient::pbSubmitPendingAudio(uint32_t chunk_idx,
                                         uint32_t start_at_ms) {
  if (pb_pending_pcm_owned_ == nullptr || pb_pending_pcm_samples_ == 0) {
    return true;
  }
  if (start_at_ms == 0 || pb_current_epoch_ == 0 || pb_req_.isEmpty()) {
    pbFreePendingPcm();
    pbFailCurrentTerminal(chunk_idx, "audio timeline unavailable");
    return false;
  }
  if (!pb_audio_stream_started_) {
    if (rtc_audio_downlink_active()) {
      /*
       * PB owns the speaker only once real audio is ready for playback.
       * Camera, servo and display-only PB controls may run beside standalone
       * RTC audio and must never tear that stream down.
       */
      rtc_audio_downlink_cancel();
      audio_play_emergency_flush();
      log_info("[PB] speaker handoff RTC->PB req=%s idx=%u",
               pb_req_.c_str(), static_cast<unsigned>(chunk_idx));
    }
    if (!audio_pb_stream_begin(pb_current_epoch_, pb_req_.c_str(), chunk_idx,
                               pb_sr_, pb_ch_, pb_volume_ratio_)) {
      pbFreePendingPcm();
      pbFailCurrentTerminal(chunk_idx, "audio stream begin failed");
      return false;
    }
    pb_audio_stream_started_ = true;
    pbNoteModalitySubmitted(PbModality::kAudio);
    pb_last_buf_decay_ms_ = millis();
    pb_audio_buf_ms_est_ = 0;
  }

  int16_t* const pcm_owned = pb_pending_pcm_owned_;
  const size_t samples = pb_pending_pcm_samples_;
  const uint32_t free_caps = pb_pending_pcm_free_caps_;
  pb_pending_pcm_owned_ = nullptr;
  pb_pending_pcm_samples_ = 0;
  pb_pending_pcm_free_caps_ = 0;
  if (!audio_pb_stream_push_owned(
          pb_current_epoch_, pb_req_.c_str(), chunk_idx, pcm_owned, samples,
          free_caps, start_at_ms, pb_sr_, pb_ch_, pb_volume_ratio_)) {
    pbFailCurrentTerminal(chunk_idx, "pcm push failed");
    return false;
  }
  pb_audio_buf_ms_est_ += (int32_t)pb_pending_chunk_ms_;
  return true;
}

void AsrChatClient::pbFreeExpectedBin() {
  if (pb_expect_bin_buf_ != nullptr) {
    heap_caps_free(pb_expect_bin_buf_);
    pb_expect_bin_buf_ = nullptr;
  }
  pb_expect_bin_received_ = 0;
  pb_expect_bin_free_caps_ = 0;
}

void AsrChatClient::pbBuildPendingBinQueue(const JsonDocument& doc) {
  pbFreeExpectedBin();
  pb_pending_bin_count_ = 0;
  pb_pending_bin_cursor_ = 0;
  const size_t pcm_len = pb_read_audio_next_bin_len(doc);
  if (pcm_len > 0 && pb_pending_bin_count_ < kPbMaxBinsPerChunk) {
    pb_pending_bin_kinds_[pb_pending_bin_count_] = PbBinKind::kPcm;
    pb_pending_bin_lens_[pb_pending_bin_count_] = pcm_len;
    pb_pending_bin_frames_[pb_pending_bin_count_] = pb_read_audio_opus_frames(doc);
    pb_pending_bin_count_++;
  }
  if (doc["assets"].is<JsonArrayConst>()) {
    for (JsonObjectConst asset : doc["assets"].as<JsonArrayConst>()) {
      const size_t asset_len = pb_read_asset_next_bin_len(asset);
      if (asset_len == 0) {
        continue;
      }
      if (pb_pending_bin_count_ >= kPbMaxBinsPerChunk) {
        log_warn("[PB] assets[] bin queue truncated at %u", (unsigned)kPbMaxBinsPerChunk);
        break;
      }
      pb_pending_bin_kinds_[pb_pending_bin_count_] = PbBinKind::kAsset;
      pb_pending_bin_lens_[pb_pending_bin_count_] = asset_len;
      pb_pending_bin_count_++;
    }
  }
}

void AsrChatClient::pbAdvanceBinQueue() {
  pbFreeExpectedBin();
  pb_pending_bin_cursor_++;
  if (pb_pending_bin_cursor_ < pb_pending_bin_count_) {
    pb_expect_bin_kind_ = pb_pending_bin_kinds_[pb_pending_bin_cursor_];
    pb_expect_bin_len_ = pb_pending_bin_lens_[pb_pending_bin_cursor_];
    pb_expect_opus_frames_ = pb_pending_bin_frames_[pb_pending_bin_cursor_];
    pb_expect_bin_ = true;
    pb_expect_bin_since_ms_ = millis();
  } else {
    pb_expect_bin_ = false;
    pb_expect_bin_len_ = 0;
    pb_expect_opus_frames_ = 0;
    pb_expect_bin_since_ms_ = 0;
  }
}

void AsrChatClient::pbClearCompletedReplay(const char* why) {
  if (pb_completed_replay_active_) {
    log_info("[PB] completed replay closed req=%s why=%s",
             pb_completed_replay_req_.c_str(), why ? why : "?");
  }
  pb_completed_replay_active_ = false;
  pb_completed_replay_req_.remove(0);
  pb_completed_replay_outcome_ = PbSequenceOutcome::kPending;
  pb_completed_replay_idx_ = 0;
  pb_completed_replay_end_after_bins_ = false;
  pb_completed_replay_bin_count_ = 0;
  pb_completed_replay_bin_cursor_ = 0;
  pb_completed_replay_bin_received_ = 0;
  pb_completed_replay_bin_since_ms_ = 0;
}

void AsrChatClient::pbClearRejectedDiscard(const char* why) {
  if (pb_rejected_discard_active_) {
    log_info("[PB] rejected-chain discard closed req=%s why=%s",
             pb_rejected_discard_req_.c_str(), why ? why : "?");
  }
  pb_rejected_discard_active_ = false;
  pb_rejected_discard_unframed_ = false;
  pb_rejected_discard_end_after_bins_ = false;
  pb_rejected_discard_req_.remove(0);
  pb_rejected_discard_bin_count_ = 0;
  pb_rejected_discard_bin_cursor_ = 0;
  pb_rejected_discard_bin_received_ = 0;
  pb_rejected_discard_bin_since_ms_ = 0;
}

bool AsrChatClient::pbStageRejectedDiscard(const JsonDocument& doc,
                                            const String& req) {
  if (req.isEmpty()) return false;
  if (pb_rejected_discard_active_ &&
      pb_rejected_discard_req_ != req) {
    pbClearRejectedDiscard("switch rejected req");
  }
  pb_rejected_discard_active_ = true;
  pb_rejected_discard_unframed_ = false;
  pb_rejected_discard_req_ = req;
  pb_rejected_discard_bin_count_ = 0;
  pb_rejected_discard_bin_cursor_ = 0;
  pb_rejected_discard_bin_received_ = 0;
  pb_rejected_discard_bin_since_ms_ = 0;
  const String type =
      doc["type"].is<String>() ? doc["type"].as<String>() : String("");
  pb_rejected_discard_end_after_bins_ =
      type == "pb_end" || type == "pb_single";

  const auto add_length = [this](JsonVariantConst value) {
    if (value.isNull()) return true;
    int64_t parsed = 0;
    if (!pb_json_integer_in_range(value, 0, DESKBOT_USB_MAX_PAYLOAD,
                                  &parsed)) {
      return false;
    }
    if (parsed == 0) return true;
    if (pb_rejected_discard_bin_count_ >= kPbMaxBinsPerChunk) {
      return false;
    }
    pb_rejected_discard_bin_lens_
        [pb_rejected_discard_bin_count_++] = static_cast<size_t>(parsed);
    return true;
  };

  bool framing_valid = true;
  if (!doc["audio"].isNull()) {
    framing_valid = doc["audio"].is<JsonObjectConst>() &&
                    add_length(doc["audio"]["next_bin_len"]);
  }
  if (framing_valid && !doc["assets"].isNull()) {
    if (!doc["assets"].is<JsonArrayConst>() ||
        doc["assets"].as<JsonArrayConst>().size() >
            kPbMaxAssetsPerChunk) {
      framing_valid = false;
    } else {
      for (JsonVariantConst raw_asset :
           doc["assets"].as<JsonArrayConst>()) {
        if (!raw_asset.is<JsonObjectConst>() ||
            !add_length(raw_asset["next_bin_len"])) {
          framing_valid = false;
          break;
        }
      }
    }
  }
  if (!framing_valid) {
    /* Keep the rejected req active, but do not guess a byte count from
     * malformed metadata. Every binary frame is discarded until the next
     * JSON preamble re-establishes an exact bounded declaration. */
    pb_rejected_discard_unframed_ = true;
    pb_rejected_discard_bin_count_ = 0;
    log_warn("[PB] rejected-chain BIN metadata invalid req=%s; discard until next JSON",
             req.c_str());
    return false;
  }
  if (pb_rejected_discard_bin_count_ > 0) {
    pb_rejected_discard_bin_since_ms_ = millis();
    log_info("[PB] rejected-chain discard staged req=%s type=%s bins=%u",
             req.c_str(), type.c_str(),
             (unsigned)pb_rejected_discard_bin_count_);
  } else if (pb_rejected_discard_end_after_bins_) {
    pbClearRejectedDiscard("rejected terminal JSON consumed");
    if (pb_suppress_tail_req_ == req) pb_suppress_tail_req_.remove(0);
  }
  return true;
}

void AsrChatClient::pbAdvanceRejectedDiscardBin() {
  if (!pbRejectedDiscardExpectsBin()) return;
  pb_rejected_discard_bin_received_ = 0;
  pb_rejected_discard_bin_cursor_++;
  if (pbRejectedDiscardExpectsBin()) {
    pb_rejected_discard_bin_since_ms_ = millis();
    return;
  }
  pb_rejected_discard_bin_count_ = 0;
  pb_rejected_discard_bin_cursor_ = 0;
  pb_rejected_discard_bin_since_ms_ = 0;
  if (pb_rejected_discard_end_after_bins_) {
    const String completed_req = pb_rejected_discard_req_;
    pbClearRejectedDiscard("rejected terminal BIN consumed");
    if (pb_suppress_tail_req_ == completed_req) {
      pb_suppress_tail_req_.remove(0);
    }
  }
}

bool AsrChatClient::pbStageCompletedReplay(
    const JsonDocument& doc, const PbCompletedReq& completed) {
  const String req =
      doc["req"].is<String>() ? doc["req"].as<String>() : String("");
  if (!completed.used || req.isEmpty() || completed.req != req ||
      completed.outcome == PbSequenceOutcome::kPending) {
    return false;
  }
  if (pbCompletedReplayExpectsBin()) {
    /* A TEXT frame before the declared BIN is retained in the deferred queue;
     * reaching this guard would otherwise overwrite framing state. */
    log_error("[PB] completed replay JSON arrived before BIN req=%s",
              req.c_str());
    return false;
  }
  if (pb_completed_replay_active_ &&
      pb_completed_replay_req_ != req) {
    pbClearCompletedReplay("switch cached req");
  }
  pb_completed_replay_active_ = true;
  pb_completed_replay_req_ = req;
  pb_completed_replay_outcome_ = completed.outcome;
  pb_completed_replay_idx_ = completed.idx;
  pb_completed_replay_bin_count_ = 0;
  pb_completed_replay_bin_cursor_ = 0;
  pb_completed_replay_bin_received_ = 0;
  pb_completed_replay_bin_since_ms_ = 0;

  const size_t pcm_len = pb_read_audio_next_bin_len(doc);
  if (pcm_len > 0) {
    if (pcm_len > DESKBOT_USB_MAX_PAYLOAD) {
      log_error("[PB] completed replay PCM exceeds logical limit req=%s len=%u",
                req.c_str(), (unsigned)pcm_len);
      pbClearCompletedReplay("replay PCM too large");
      return false;
    }
    pb_completed_replay_bin_lens_[pb_completed_replay_bin_count_++] =
        pcm_len;
  }
  if (doc["assets"].is<JsonArrayConst>()) {
    for (JsonObjectConst asset : doc["assets"].as<JsonArrayConst>()) {
      const size_t asset_len = pb_read_asset_next_bin_len(asset);
      if (asset_len == 0) {
        continue;
      }
      if (pb_completed_replay_bin_count_ >= kPbMaxBinsPerChunk) {
        log_error("[PB] completed replay bin list overflow req=%s max=%u",
                  req.c_str(), (unsigned)kPbMaxBinsPerChunk);
        pbClearCompletedReplay("replay bin list overflow");
        return false;
      }
      if (asset_len > DESKBOT_USB_MAX_PAYLOAD) {
        log_error("[PB] completed replay asset exceeds logical limit req=%s len=%u",
                  req.c_str(), (unsigned)asset_len);
        pbClearCompletedReplay("replay asset too large");
        return false;
      }
      pb_completed_replay_bin_lens_
          [pb_completed_replay_bin_count_++] = asset_len;
    }
  }

  const String type =
      doc["type"].is<String>() ? doc["type"].as<String>() : String("");
  pb_completed_replay_end_after_bins_ =
      type == "pb_end" || type == "pb_single";
  if (pb_completed_replay_bin_count_ > 0) {
    pb_completed_replay_bin_since_ms_ = millis();
  }
  pbQueueTerminalAck(completed.req.c_str(), completed.idx,
                     completed.outcome, /*persist=*/false,
                     /*pose_valid=*/false, /*pose_x=*/0, /*pose_y=*/0,
                     completed.display_crc_valid,
                     completed.display_crc32);
  log_warn("[PB] suppress completed replay type=%s req=%s wire_idx=%u "
           "terminal_idx=%u outcome=%u bins=%u",
           type.c_str(), req.c_str(),
           (unsigned)(doc["idx"].is<uint32_t>()
                          ? doc["idx"].as<uint32_t>()
                          : 0),
           (unsigned)completed.idx, (unsigned)completed.outcome,
           (unsigned)pb_completed_replay_bin_count_);
  if (pb_completed_replay_bin_count_ == 0 &&
      pb_completed_replay_end_after_bins_) {
    pbClearCompletedReplay("terminal JSON consumed");
  }
  return true;
}

void AsrChatClient::pbAdvanceCompletedReplayBin() {
  if (!pbCompletedReplayExpectsBin()) {
    return;
  }
  pb_completed_replay_bin_received_ = 0;
  pb_completed_replay_bin_cursor_++;
  if (pb_completed_replay_bin_cursor_ <
      pb_completed_replay_bin_count_) {
    pb_completed_replay_bin_since_ms_ = millis();
    return;
  }
  pb_completed_replay_bin_count_ = 0;
  pb_completed_replay_bin_cursor_ = 0;
  pb_completed_replay_bin_received_ = 0;
  pb_completed_replay_bin_since_ms_ = 0;
  if (pb_completed_replay_end_after_bins_) {
    pbClearCompletedReplay("terminal BIN consumed");
  }
}

void AsrChatClient::pbFreePendingAnim() {
  if (pb_pending_anim_buf_) {
    ::free(pb_pending_anim_buf_);
    pb_pending_anim_buf_ = nullptr;
  }
  pb_pending_anim_len_ = 0;
}

void AsrChatClient::pbReset(bool stop_audio,
                            bool preserve_pending_terminals) {
  const uint8_t pb_ch_before_reset = pb_ch_;
  if (!preserve_pending_terminals) {
    pbClearCompletedReplay("PB state reset");
    pbCancelTerminalTracks("PB state reset");
  }
  for (auto& track : pb_terminal_tracks_) {
    if (track.used && track.outcome != PbSequenceOutcome::kPending) {
      track.used = false;
      track.req.remove(0);
    }
  }
  if (!preserve_pending_terminals && pb_ack_out_pending_ &&
      pb_ack_out_phase_ != "accepted" &&
      !pb_ack_out_req_.isEmpty()) {
    const PbSequenceOutcome outcome =
        pb_ack_out_phase_ == "played"
            ? PbSequenceOutcome::kPlayed
            : pb_ack_out_phase_ == "cancelled"
                  ? PbSequenceOutcome::kCancelled
                  : PbSequenceOutcome::kFailed;
    pbQueueTerminalAck(pb_ack_out_req_.c_str(), pb_ack_out_idx_, outcome,
                       /*persist=*/false, /*pose_valid=*/true,
                       pb_ack_out_commanded_x_,
                       pb_ack_out_commanded_y_,
                       pb_ack_out_display_crc_valid_,
                       pb_ack_out_display_crc32_);
  }
  pbFreeExpectedBin();
  pbFreePendingPcm();
  pbFreePendingAssets();
  pb_pending_bin_count_ = 0;
  pb_pending_bin_cursor_ = 0;
  pb_expect_opus_frames_ = 0;
  pb_active_ = false;
  pb_req_.remove(0);
  pb_next_idx_ = 0;
  pb_expect_bin_ = false;
  pb_expect_bin_len_ = 0;
  pb_expect_bin_since_ms_ = 0;
  pb_sr_ = 0;
  pb_ch_ = 0;
  pb_fmt_.remove(0);
  pb_pending_idx_ = 0;
  pb_pending_chunk_ms_ = 0;
  pb_pending_mouth_only_ = false;
  pb_sequence_mouth_only_ = false;
  pbFreePendingAnim();
  pb_pending_servo_seg_count_ = 0;
  pb_last_ack_idx_ = 0;
  pb_bins_rx_count_ = 0;
  pb_pcm_bytes_rx_total_ = 0;
  pb_end_waiting_bin_ = false;
  pb_end_idx_ = 0;
  pb_current_epoch_ = 0;

  if (!preserve_pending_terminals) {
    pb_ack_out_pending_ = false;
    pb_ack_out_req_.remove(0);
    pb_ack_out_idx_ = 0;
    pb_ack_out_buf_ms_ = 0;
    pb_ack_out_phase_ = "accepted";
    pb_ack_out_commanded_x_ = 0;
    pb_ack_out_commanded_y_ = 0;
    pb_ack_out_display_crc_valid_ = false;
    pb_ack_out_display_crc32_ = 0;
    pb_last_pb_ack_sent_wall_ms_ = 0;
    pb_ack_bypass_throttle_ = false;
  }
  pb_pending_enqueue_action_ = PbEnqueueAction::kReplace;
  pb_queue_level_ = -1;
  pb_inflight_seq_count_ = 0;
  pb_seq_level_count_ = 0;

  pb_audio_buf_ms_est_ = 0;
  pb_last_buf_decay_ms_ = millis();
  if (stop_audio) {
    const bool had_audio_stream =
        pb_audio_stream_started_ || audio_play_stream_pcm_active();
    if (had_audio_stream) {
      deskbot_uplink_set_speaker_active(false);
    }
    if (pb_audio_stream_started_) {
      const uint8_t ch_end = (pb_ch_before_reset == 0 || pb_ch_before_reset > 2) ? 1 : pb_ch_before_reset;
      audio_stream_pcm16_stop();
    }
    pb_audio_stream_started_ = false;
  }
}

void AsrChatClient::pbProtocolError(const char* why) {
  log_warn("[PB] protocol error: %s (req=%s expect_bin=%d expect_len=%u next_idx=%u)",
           why ? why : "?", pb_req_.c_str(), (int)pb_expect_bin_, (unsigned)pb_expect_bin_len_,
           (unsigned)pb_next_idx_);
  pbFailCurrentTerminal(pb_pending_idx_ > 0 ? pb_pending_idx_ : pb_next_idx_,
                        why);
  /* A malformed chunk may already have transferred one modality to its
   * worker. Protocol failure is therefore an all-worker transaction abort. */
  audio_play_emergency_flush();
  /* A protocol failure aborts the broken display transaction, but it must not
   * synthesize a firmware-owned face.  The PC expression runtime remains the
   * sole steady-state display owner and will replace the retained baseline on
   * its next valid command. */
  display_render_replace(/*preserve_baseline=*/true);
  head_clear_motor_pending();
  pbReset(/*stop_audio=*/false);
  /* 协议错位后兜底标记本轮 reply 结束，避免 runVoiceRound 等 30s 超时。 */
  pbSignalTtsRoundComplete();
}

bool AsrChatClient::pbSubmitAnimIfAny(uint32_t chunk_idx,
                                      uint32_t start_at_ms) {
  if (!pb_pending_anim_buf_ || pb_pending_anim_len_ == 0) {
    if (pb_asset_count_ > 0) {
      log_warn("[PB] assets without anim ignored req=%s idx=%u count=%u",
               pb_req_.c_str(), (unsigned)chunk_idx,
               (unsigned)pb_asset_count_);
      pbFreePendingAssets();
    }
    return true;
  }
  const bool queued = display_pb_submit_vector_json_owned(
      pb_current_epoch_, pb_req_.c_str(), chunk_idx, start_at_ms,
      pb_pending_anim_buf_, pb_pending_anim_len_, pb_asset_bufs_,
      pb_asset_lens_, pb_asset_count_, pb_pending_mouth_only_);
  pb_pending_anim_buf_ = nullptr;
  pb_pending_anim_len_ = 0;
  for (uint8_t i = 0; i < pb_asset_count_; i++) {
    pb_asset_bufs_[i] = nullptr;
    pb_asset_lens_[i] = 0;
  }
  pb_asset_count_ = 0;
  if (!queued) {
    pbFailCurrentTerminal(chunk_idx, "display enqueue failed");
    return false;
  }
  pbNoteModalitySubmitted(PbModality::kDisplay);
  return true;
}

bool AsrChatClient::pbPreflightChunk(const JsonDocument& doc,
                                     bool completed_replay,
                                     PbChunkPreflight* out,
                                     const char** why) const {
  constexpr uint32_t kMaxModalityDurationMs = 300000u;
  constexpr size_t kMaxAnimSegsPerChunk = 64u;
  if (why != nullptr) *why = "invalid PB chunk";
  if (out == nullptr) return false;
  *out = PbChunkPreflight{};
  const auto reject = [why](const char* reason) {
    if (why != nullptr) *why = reason;
    return false;
  };

  if (!doc["type"].is<String>() || !doc["req"].is<String>()) {
    return reject("type/req must be strings");
  }
  const String type = doc["type"].as<String>();
  const String req = doc["req"].as<String>();
  if (type != "pb_start" && type != "pb_chunk" && type != "pb_end" &&
      type != "pb_single") {
    return reject("unsupported PB type");
  }
  if (req.isEmpty() || req.length() > DESKBOT_PB_REQ_MAX_CHARS) {
    return reject("invalid req");
  }
  int64_t parsed = 0;
  if (!pb_json_integer_in_range(doc["idx"], 0, UINT32_MAX, &parsed)) {
    return reject("idx must be uint32");
  }
  out->idx = static_cast<uint32_t>(parsed);
  out->is_chain_head = type == "pb_start" || type == "pb_single";
  if ((out->is_chain_head && out->idx != 0u) ||
      (!out->is_chain_head && out->idx == 0u)) {
    return reject("PB type/idx mismatch");
  }
  if (!doc["pb_ver"].isNull() &&
      (!pb_json_integer_in_range(doc["pb_ver"], 2, 2, &parsed))) {
    return reject("unsupported pb_ver");
  }
  if (!doc["durable"].isNull() && !doc["durable"].is<bool>()) {
    return reject("durable must be bool");
  }
  out->durable = doc["durable"].is<bool>() && doc["durable"].as<bool>();
  if (!doc["voice_mouth"].isNull() && !doc["voice_mouth"].is<bool>()) {
    return reject("voice_mouth must be bool");
  }
  if (!doc["mouth_only"].isNull() && !doc["mouth_only"].is<bool>()) {
    return reject("mouth_only must be bool");
  }
  out->voice_mouth =
      doc["voice_mouth"].is<bool>() && doc["voice_mouth"].as<bool>();
  out->mouth_only =
      doc["mouth_only"].is<bool>() && doc["mouth_only"].as<bool>();
  if (!out->is_chain_head &&
      (!doc["voice_mouth"].isNull() || !doc["mouth_only"].isNull())) {
    return reject("voice_mouth/mouth_only are chain-head fields");
  }
  if (out->voice_mouth && out->mouth_only) {
    return reject("voice_mouth and mouth_only are mutually exclusive");
  }

  bool legacy_opportunistic = false;
  if (!doc["action"].isNull()) {
    if (!doc["action"].is<String>()) return reject("action must be string");
    String action = doc["action"].as<String>();
    action.toLowerCase();
    if (action == "append") {
      out->action = PbEnqueueAction::kAppend;
    } else if (action == "default") {
      out->action = PbEnqueueAction::kDefault;
    } else if (action == "replace") {
      out->action = PbEnqueueAction::kReplace;
    } else if (action == "opportunistic") {
      out->action = PbEnqueueAction::kAppend;
      legacy_opportunistic = true;
    } else {
      return reject("unsupported action");
    }
  }
  if (legacy_opportunistic) {
    out->level = 0;
  } else if (!doc["level"].isNull()) {
    if (!pb_json_integer_in_range(doc["level"], 0, 3, &parsed)) {
      return reject("level must be integer 0..3");
    }
    out->level = static_cast<int8_t>(parsed);
  }

  uint32_t declared_chunk_ms = 0;
  if (!doc["chunk_ms"].isNull()) {
    if (!pb_json_integer_in_range(doc["chunk_ms"], 0,
                                  kMaxModalityDurationMs, &parsed)) {
      return reject("chunk_ms out of range");
    }
    declared_chunk_ms = static_cast<uint32_t>(parsed);
  }
  if (!doc["volume"].isNull() &&
      !pb_json_integer_in_range(doc["volume"], 0, 100, &parsed)) {
    return reject("volume must be integer 0..100");
  }
  if (!doc["camera_once"].isNull() && !doc["camera_once"].is<bool>()) {
    return reject("camera_once must be bool");
  }
  if (!doc["cam_fps"].isNull() &&
      !pb_json_integer_in_range(doc["cam_fps"], 0, UINT16_MAX, &parsed)) {
    return reject("cam_fps must be non-negative integer");
  }
  if (!doc["mic"].isNull()) {
    if (!doc["mic"].is<String>()) return reject("mic must be string");
    String mic = doc["mic"].as<String>();
    mic.toLowerCase();
    if (mic != "open" && mic != "mute") return reject("unsupported mic hint");
  }
  if (!doc["rtc_mode"].isNull()) {
    if (!doc["rtc_mode"].is<String>()) {
      return reject("rtc_mode must be string");
    }
    String rtc_mode = doc["rtc_mode"].as<String>();
    rtc_mode.toLowerCase();
    if (rtc_mode != "stable" && rtc_mode != "interruptible" &&
        rtc_mode != "esp32_aec") {
      return reject("unsupported rtc_mode");
    }
  }

  uint32_t effective_sr = out->is_chain_head ? 0u : pb_sr_;
  uint8_t effective_ch = out->is_chain_head ? 0u : pb_ch_;
  String effective_fmt = out->is_chain_head ? String("") : pb_fmt_;
  if (!doc["sr"].isNull()) {
    if (!pb_json_integer_in_range(doc["sr"], 8000, 48000, &parsed)) {
      return reject("audio sample rate out of range");
    }
    effective_sr = static_cast<uint32_t>(parsed);
  }
  if (!doc["ch"].isNull()) {
    if (!pb_json_integer_in_range(doc["ch"], 1, 2, &parsed)) {
      return reject("audio channels must be 1 or 2");
    }
    effective_ch = static_cast<uint8_t>(parsed);
  }
  if (!doc["fmt"].isNull()) {
    if (!doc["fmt"].is<String>()) return reject("audio fmt must be string");
    effective_fmt = doc["fmt"].as<String>();
    effective_fmt.toLowerCase();
    if (effective_fmt != "s16le" && effective_fmt != "opus") {
      return reject("unsupported audio fmt");
    }
  }
  size_t audio_bin_len = 0;
  if (!doc["audio"].isNull()) {
    if (!doc["audio"].is<JsonObjectConst>()) {
      return reject("audio must be object");
    }
    JsonObjectConst audio = doc["audio"].as<JsonObjectConst>();
    if (!audio["next_bin_len"].isNull()) {
      if (!pb_json_integer_in_range(audio["next_bin_len"], 0,
                                    DESKBOT_USB_MAX_PAYLOAD, &parsed)) {
        return reject("audio binary length out of range");
      }
      audio_bin_len = static_cast<size_t>(parsed);
    }
    if (!audio["frames"].isNull() &&
        !pb_json_integer_in_range(audio["frames"], 1, UINT16_MAX, &parsed)) {
      return reject("audio frames out of range");
    }
  }
  if (audio_bin_len > 0) {
    if (!completed_replay &&
        (effective_sr == 0 || effective_ch == 0 || effective_fmt.isEmpty())) {
      return reject("audio binary missing sr/ch/fmt");
    }
    if (effective_fmt == "s16le" && (audio_bin_len & 1u) != 0u) {
      return reject("s16le binary length must be even");
    }
    if (effective_fmt == "opus" && effective_sr != 8000u &&
        effective_sr != 12000u && effective_sr != 16000u &&
        effective_sr != 24000u && effective_sr != 48000u) {
      return reject("unsupported Opus sample rate");
    }
  }

  if (!doc["assets"].isNull()) {
    if (!doc["assets"].is<JsonArrayConst>()) {
      return reject("assets must be array");
    }
    JsonArrayConst assets = doc["assets"].as<JsonArrayConst>();
    if (assets.size() > kPbMaxAssetsPerChunk) {
      return reject("too many assets");
    }
    for (JsonVariantConst raw_asset : assets) {
      if (!raw_asset.is<JsonObjectConst>()) {
        return reject("asset must be object");
      }
      JsonObjectConst asset = raw_asset.as<JsonObjectConst>();
      if (!asset["next_bin_len"].isNull() &&
          !pb_json_integer_in_range(asset["next_bin_len"], 0,
                                    DESKBOT_USB_MAX_PAYLOAD, &parsed)) {
        return reject("asset binary length out of range");
      }
    }
  }

  if (!doc["anim"].isNull()) {
    if (!doc["anim"].is<JsonArrayConst>()) return reject("anim must be array");
    JsonArrayConst anim = doc["anim"].as<JsonArrayConst>();
    if (anim.size() > kMaxAnimSegsPerChunk ||
        measureJson(anim) > kPbDeferMaxBytes) {
      return reject("anim exceeds device limits");
    }
    uint32_t anim_total_ms = 0;
    for (JsonVariantConst raw_segment : anim) {
      if (!raw_segment.is<JsonObjectConst>()) {
        return reject("anim segment must be object");
      }
      JsonObjectConst segment = raw_segment.as<JsonObjectConst>();
      uint32_t duration_ms = 1u;
      if (!segment["ms"].isNull()) {
        if (!pb_json_integer_in_range(segment["ms"], 0,
                                      kMaxModalityDurationMs, &parsed)) {
          return reject("anim duration out of range");
        }
        duration_ms = parsed > 0 ? static_cast<uint32_t>(parsed) : 1u;
      }
      if (duration_ms > kMaxModalityDurationMs - anim_total_ms) {
        return reject("anim total duration exceeds limit");
      }
      anim_total_ms += duration_ms;
      if (!segment["elements"].isNull() &&
          !segment["elements"].is<JsonObjectConst>()) {
        return reject("anim elements must be object");
      }
    }
  }

  if (!doc["servo"].isNull()) {
    if (!doc["servo"].is<JsonArrayConst>()) {
      return reject("servo must be array");
    }
    JsonArrayConst servo = doc["servo"].as<JsonArrayConst>();
    if (servo.size() > kPbMaxServoSegsPerChunk) {
      return reject("servo segment count exceeds limit");
    }
    uint32_t servo_total_ms = 0;
    for (JsonVariantConst raw_segment : servo) {
      if (!raw_segment.is<JsonObjectConst>()) {
        return reject("servo segment must be object");
      }
      JsonObjectConst segment = raw_segment.as<JsonObjectConst>();
      int64_t xm = 0, ym = 0, x = 0, y = 0, ms = 0;
      if (!pb_json_integer_in_range(segment["xm"], HEAD_SERVO_ABS,
                                    HEAD_SERVO_HOLD, &xm) ||
          !pb_json_integer_in_range(segment["ym"], HEAD_SERVO_ABS,
                                    HEAD_SERVO_HOLD, &ym) ||
          !pb_json_integer_in_range(segment["x"], INT32_MIN, INT32_MAX, &x) ||
          !pb_json_integer_in_range(segment["y"], INT32_MIN, INT32_MAX, &y) ||
          !pb_json_integer_in_range(segment["ms"], 50,
                                    kMaxModalityDurationMs, &ms)) {
        return reject("invalid servo segment fields");
      }
      const bool x_valid =
          (xm == HEAD_SERVO_ABS && x >= X_MIN_LIMIT && x <= X_MAX_LIMIT) ||
          (xm == HEAD_SERVO_REL && x >= -180 && x <= 180) ||
          (xm == HEAD_SERVO_HOLD && x == 0);
      const bool y_valid =
          (ym == HEAD_SERVO_ABS && y >= Y_MIN_LIMIT && y <= Y_MAX_LIMIT) ||
          (ym == HEAD_SERVO_REL && y >= -40 && y <= 40) ||
          (ym == HEAD_SERVO_HOLD && y == 0);
      if (!x_valid || !y_valid) return reject("servo coordinate out of range");

      int64_t x_min = X_MIN_LIMIT, x_max = X_MAX_LIMIT;
      int64_t y_min = Y_MIN_LIMIT, y_max = Y_MAX_LIMIT;
      const bool has_x_min = !segment["x_min"].isNull();
      const bool has_x_max = !segment["x_max"].isNull();
      const bool has_y_min = !segment["y_min"].isNull();
      const bool has_y_max = !segment["y_max"].isNull();
      const unsigned bound_count = static_cast<unsigned>(has_x_min) +
                                   static_cast<unsigned>(has_x_max) +
                                   static_cast<unsigned>(has_y_min) +
                                   static_cast<unsigned>(has_y_max);
      if ((bound_count != 0u && bound_count != 4u) ||
          (has_x_min &&
           !pb_json_integer_in_range(segment["x_min"], X_MIN_LIMIT,
                                     X_MAX_LIMIT, &x_min)) ||
          (has_x_max &&
           !pb_json_integer_in_range(segment["x_max"], X_MIN_LIMIT,
                                     X_MAX_LIMIT, &x_max)) ||
          (has_y_min &&
           !pb_json_integer_in_range(segment["y_min"], Y_MIN_LIMIT,
                                     Y_MAX_LIMIT, &y_min)) ||
          (has_y_max &&
           !pb_json_integer_in_range(segment["y_max"], Y_MIN_LIMIT,
                                     Y_MAX_LIMIT, &y_max)) ||
          x_min > x_max || y_min > y_max) {
        return reject("invalid servo soft limits");
      }
      const int64_t x_span = x_max - x_min;
      const int64_t y_span = y_max - y_min;
      const int64_t abs_x = x < 0 ? -x : x;
      const int64_t abs_y = y < 0 ? -y : y;
      if ((xm == HEAD_SERVO_ABS && (x < x_min || x > x_max)) ||
          (ym == HEAD_SERVO_ABS && (y < y_min || y > y_max)) ||
          (xm == HEAD_SERVO_REL && abs_x > x_span) ||
          (ym == HEAD_SERVO_REL && abs_y > y_span)) {
        return reject("servo target exceeds soft limits");
      }
      const uint32_t duration_ms = static_cast<uint32_t>(ms);
      if (duration_ms > kMaxModalityDurationMs - servo_total_ms) {
        return reject("servo total duration exceeds limit");
      }
      servo_total_ms += duration_ms;
      PbServoSeg& staged = out->servo_segs[out->servo_count++];
      staged.xm = static_cast<int>(xm);
      staged.ym = static_cast<int>(ym);
      staged.x = static_cast<int>(x);
      staged.y = static_cast<int>(y);
      staged.x_min = static_cast<int>(x_min);
      staged.x_max = static_cast<int>(x_max);
      staged.y_min = static_cast<int>(y_min);
      staged.y_max = static_cast<int>(y_max);
      staged.ms = duration_ms;
    }
  }
  out->chunk_ms = pb_effective_chunk_ms(doc, declared_chunk_ms);
  return true;
}

void AsrChatClient::pbCommitServoPreflight(
    const PbChunkPreflight& preflight) {
  pb_pending_servo_seg_count_ = preflight.servo_count;
  for (size_t i = 0; i < preflight.servo_count; ++i) {
    pb_pending_servo_segs_[i] = preflight.servo_segs[i];
  }
  if (preflight.servo_count > 0) {
    log_info("[PB] servo preflight committed idx=%u segs=%u",
             (unsigned)preflight.idx, (unsigned)preflight.servo_count);
  }
}

static uint32_t pb_servo_worst_axis_travel(uint8_t mode, int value, int lo,
                                           int hi) {
  if (mode == HEAD_SERVO_HOLD) {
    return 0;
  }
  if (mode == HEAD_SERVO_REL) {
    const int64_t magnitude =
        value < 0 ? -static_cast<int64_t>(value)
                  : static_cast<int64_t>(value);
    const uint32_t span = static_cast<uint32_t>(hi - lo);
    return magnitude > span ? span : static_cast<uint32_t>(magnitude);
  }
  const int target = constrain(value, lo, hi);
  const int from_lo = target - lo;
  const int from_hi = hi - target;
  return static_cast<uint32_t>(from_lo > from_hi ? from_lo : from_hi);
}

bool AsrChatClient::pbApplyServoArrayIfAny(uint32_t chunk_idx,
                                           uint32_t start_at_ms,
                                           bool* submitted_any) {
  if (submitted_any) {
    *submitted_any = false;
  }
  if (pb_pending_servo_seg_count_ == 0) {
    return true;
  }
  const size_t n = pb_pending_servo_seg_count_;
  HeadPbServoCmd commands[kPbMaxServoSegsPerChunk]{};
  uint32_t segment_offset_ms = 0;
  uint32_t conservative_elapsed_ms = 0;
  for (size_t i = 0; i < n; i++) {
    const PbServoSeg& seg = pb_pending_servo_segs_[i];
    HeadPbServoCmd& command = commands[i];
    command.xm = static_cast<uint8_t>(seg.xm);
    command.ym = static_cast<uint8_t>(seg.ym);
    command.x = seg.x;
    command.y = seg.y;
    command.x_min = seg.x_min;
    command.x_max = seg.x_max;
    command.y_min = seg.y_min;
    command.y_max = seg.y_max;
    command.ms = seg.ms;
    command.start_at_ms = start_at_ms + segment_offset_ms;
    if (command.start_at_ms == 0) {
      command.start_at_ms = 1;
    }
    segment_offset_ms += command.ms;

    const uint32_t x_travel = pb_servo_worst_axis_travel(
        command.xm, command.x, X_MIN_LIMIT, X_MAX_LIMIT);
    const uint32_t y_travel = pb_servo_worst_axis_travel(
        command.ym, command.y, Y_MIN_LIMIT, Y_MAX_LIMIT);
    const uint32_t travel = x_travel > y_travel ? x_travel : y_travel;
    const uint32_t physical_min_ms = ((travel + 2u) / 3u) * SERVO_TICK_MS;
    conservative_elapsed_ms +=
        command.ms > physical_min_ms ? command.ms : physical_min_ms;
  }
  if (!head_servo_cmd_pb_batch_async(commands, n, pb_current_epoch_,
                                      pb_req_.c_str(), chunk_idx)) {
    pbFailCurrentTerminal(chunk_idx, "motor batch enqueue failed");
    return false;
  }
  for (size_t i = 0; i < n; ++i) {
    pbNoteModalitySubmitted(PbModality::kMotor);
  }
  PbTerminalTrack* track = pbFindTerminalTrack(pb_current_epoch_);
  if (track != nullptr) {
    pbRefreshTerminalLease(*track, start_at_ms + conservative_elapsed_ms);
  }
  if (submitted_any) {
    *submitted_any = true;
  }
  return true;
}

void AsrChatClient::pbUpdateAudioBufDecayWall() {
  unsigned long now = millis();
  if (pb_last_buf_decay_ms_ == 0) {
    pb_last_buf_decay_ms_ = now;
  }
  /* 流已结束后仍按墙钟衰减，供半双工抑制覆盖播放队列排空前的估算缓冲。 */
  if (pb_audio_buf_ms_est_ > 0) {
    int32_t dec = (int32_t)(now - pb_last_buf_decay_ms_);
    if (dec > 0) {
      pb_audio_buf_ms_est_ -= dec;
      if (pb_audio_buf_ms_est_ < 0) {
        pb_audio_buf_ms_est_ = 0;
      }
    }
  }
  pb_last_buf_decay_ms_ = now;
}

void AsrChatClient::pbMaybeAck(uint32_t idx) {
  if (!pb_active_ || pb_req_.isEmpty()) {
    return;
  }

  pbUpdateAudioBufDecayWall();

  if (idx < pb_last_ack_idx_) {
    return;
  }
  if (pb_ack_out_pending_ && pb_ack_out_phase_ != "accepted") {
    return;
  }
  pb_last_ack_idx_ = idx;
  pb_ack_out_req_ = pb_req_;
  pb_ack_out_idx_ = idx;
  pb_ack_out_phase_ = "accepted";
  pb_ack_out_commanded_x_ = static_cast<int16_t>(head_read_x());
  pb_ack_out_commanded_y_ = static_cast<int16_t>(head_read_y_logic());
  pb_ack_out_display_crc_valid_ = false;
  pb_ack_out_display_crc32_ = 0;
  /* 上报播放队列深度供服务端流水线；wall-clock 估算在排空时易长期为 0。 */
  const uint32_t chunk_ms = pb_pending_chunk_ms_ > 0 ? pb_pending_chunk_ms_ : 127u;
  const unsigned qd = audio_play_input_queue_depth();
  pb_ack_out_buf_ms_ = (int32_t)(qd * chunk_ms);
  if (pb_ack_out_buf_ms_ < pb_audio_buf_ms_est_) {
    pb_ack_out_buf_ms_ = pb_audio_buf_ms_est_;
  }
  pb_ack_out_pending_ = true;
}

void AsrChatClient::flushPendingPbAck() {
  if (!pb_ack_out_pending_) {
    PbTerminalAck terminal{};
    if (!pbPopTerminalAck(&terminal)) {
      return;
    }
    pb_ack_out_pending_ = true;
    pb_ack_out_req_ = terminal.req;
    pb_ack_out_idx_ = terminal.idx;
    pb_ack_out_buf_ms_ = 0;
    pb_ack_out_commanded_x_ = terminal.commanded_x;
    pb_ack_out_commanded_y_ = terminal.commanded_y;
    pb_ack_out_display_crc_valid_ = terminal.display_crc_valid;
    pb_ack_out_display_crc32_ = terminal.display_crc32;
    switch (terminal.outcome) {
      case PbSequenceOutcome::kPlayed:
        pb_ack_out_phase_ = "played";
        break;
      case PbSequenceOutcome::kFailed:
        pb_ack_out_phase_ = "failed";
        break;
      case PbSequenceOutcome::kCancelled:
        pb_ack_out_phase_ = "cancelled";
        break;
      case PbSequenceOutcome::kPending:
        pb_ack_out_phase_ = "failed";
        break;
    }
  }
  if (!transportCanSend()) {
    return;
  }
  const unsigned long now_wall = millis();
  if (!pb_ack_bypass_throttle_ &&
      (pb_last_pb_ack_sent_wall_ms_ != 0) &&
      (now_wall - pb_last_pb_ack_sent_wall_ms_ < 80UL)) {
    return;
  }
  pb_ack_bypass_throttle_ = false;
  pb_last_pb_ack_sent_wall_ms_ = now_wall;
  char msg[336];
  const char* crc_separator = pb_ack_out_display_crc_valid_
                                  ? ",\"display_crc32\":\""
                                  : "";
  char crc_value[10] = "";
  if (pb_ack_out_display_crc_valid_) {
    snprintf(crc_value, sizeof(crc_value), "%08lx\"",
             (unsigned long)pb_ack_out_display_crc32_);
  }
  const int n = snprintf(
      msg, sizeof(msg),
      "{\"type\":\"pb_ack\",\"phase\":\"%s\",\"req\":\"%s\",\"idx\":%u,\"audio_buf_ms\":%d%s%s,"
      "\"servo\":{\"x\":%d,\"y\":%d,\"pose_source\":\"commanded\",\"x_min\":%d,\"x_max\":%d,\"y_min\":%d,\"y_max\":%d}}",
      pb_ack_out_phase_.c_str(), pb_ack_out_req_.c_str(),
      (unsigned)pb_ack_out_idx_, (int)pb_ack_out_buf_ms_, crc_separator,
      crc_value, (int)pb_ack_out_commanded_x_,
      (int)pb_ack_out_commanded_y_, X_MIN_LIMIT, X_MAX_LIMIT, Y_MIN_LIMIT,
      Y_MAX_LIMIT);
  if (n <= 0 || (size_t)n >= sizeof(msg)) {
    log_warn("[PB] pb_ack snprintf truncated");
    return;
  }
  /*
   * PB ACK is PB protocol JSON, not a generic transport control.  Keeping it
   * on PB_WIRE also lets diagnostics and smoke tests distinguish protocol
   * progress from hello/heartbeat/control traffic.
   */
  if (!usb_transport_send(
          DESKBOT_USB_PB_WIRE,
          reinterpret_cast<const uint8_t*>(msg), static_cast<size_t>(n),
          DESKBOT_USB_FLAG_JSON)) {
    pb_ack_bypass_throttle_ = true;
    return;
  }
  pb_ack_out_pending_ = false;
  pb_ack_out_req_.remove(0);
  pb_ack_out_display_crc_valid_ = false;
  pb_ack_out_display_crc32_ = 0;
}

void AsrChatClient::pbSignalTtsRoundComplete() {
  reply_done_ = true;
  tts_active_ = false;
}

bool AsrChatClient::pbFinishChunkBins(uint32_t pending_idx_snap,
                                     bool closing_pb_end_bin,
                                     uint8_t ch_for_stream_end) {
  const uint32_t sequence_epoch = pb_current_epoch_;
  const String sequence_req = pb_req_;
  if (pb_next_idx_ <= pending_idx_snap) {
    pb_next_idx_ = pending_idx_snap + 1;
  }

  if (pb_pending_pcm_owned_ != nullptr &&
      pb_pending_pcm_samples_ > 0 && pb_sr_ > 0 && pb_ch_ > 0) {
    /*
     * Derive duration from decoded PCM rather than wire bytes.  Opus payload
     * length is bitrate-dependent and cannot be used as a clock.  Taking the
     * max also prevents malformed/legacy input with an undersized chunk_ms
     * from making later chunks overlap this audio on the shared timeline.
     */
    const uint64_t denominator =
        static_cast<uint64_t>(pb_sr_) * static_cast<uint64_t>(pb_ch_);
    const uint64_t audio_ms_64 =
        (static_cast<uint64_t>(pb_pending_pcm_samples_) * 1000u +
         denominator - 1u) /
        denominator;
    const uint32_t audio_ms =
        audio_ms_64 > UINT32_MAX ? UINT32_MAX
                                : static_cast<uint32_t>(audio_ms_64);
    if (audio_ms > pb_pending_chunk_ms_) {
      pb_pending_chunk_ms_ = audio_ms;
    }
  }

  const uint32_t start_at_ms =
      pbArmCurrentChunkTimeline(pending_idx_snap, pb_pending_chunk_ms_);
  if (start_at_ms == 0) {
    pbProtocolError("PB timeline arm failed");
    return false;
  }
  /*
   * The PCM frame is retained until every binary declared by this JSON
   * (including display assets) has arrived.  Only then are all three workers
   * queued against the exact same absolute start timestamp.
   */
  if (!pbSubmitPendingAudio(pending_idx_snap, start_at_ms)) {
    pbProtocolError("audio timeline submit failed");
    return false;
  }
  if (!pbDispatchChunkPreamble(pending_idx_snap, start_at_ms)) {
    pbProtocolError("display/motor timeline submit failed");
    return false;
  }
  pbMaybeAck(pending_idx_snap);
  pb_last_ack_idx_ = pending_idx_snap;
  pb_ack_bypass_throttle_ = true;

  pb_pending_chunk_ms_ = 0;
  pb_pending_mouth_only_ = false;
  pb_pending_servo_seg_count_ = 0;

  if (closing_pb_end_bin) {
    const bool stream_alive = pb_audio_stream_started_;
    const uint8_t end_ch =
        (ch_for_stream_end == 0 || ch_for_stream_end > 2)
            ? 1
            : ch_for_stream_end;
    if (stream_alive) {
      if (!audio_pb_stream_end(sequence_epoch, sequence_req.c_str(),
                               pending_idx_snap, end_ch)) {
        pbFailCurrentTerminal(pending_idx_snap, "audio end enqueue failed");
        audio_play_emergency_flush();
      }
    }
    pb_audio_stream_started_ = false;
    pb_end_waiting_bin_ = false;
    pb_end_idx_ = 0;
    pbSealCurrentTerminalTrack(pending_idx_snap);
    log_info("[PB] complete end_bin req=%s pending_idx=%u inflight=%u (await worker terminals)",
             pb_req_.c_str(), (unsigned)pb_pending_idx_, (unsigned)pb_inflight_seq_count_);
    pbMaybeAck(pending_idx_snap);
    pb_ack_bypass_throttle_ = true;
    pbOnSequenceComplete();
  }
  /* Only advance deferred JSON after the current chunk's staged anim/assets/
   * servo have transferred to their workers.  Doing this earlier lets the
   * next JSON overwrite the current preamble before it is dispatched. */
  flushDeferredPbJson(/*pump_after=*/false);
  return true;
}

bool AsrChatClient::pbDispatchChunkPreamble(uint32_t chunk_idx,
                                            uint32_t start_at_ms) {
  /* Three asynchronous workers consume one absolute epoch/chunk timeline. */
  if (!pbSubmitAnimIfAny(chunk_idx, start_at_ms)) {
    return false;
  }
  return pbApplyServoArrayIfAny(chunk_idx, start_at_ms);
}

bool AsrChatClient::pbDeferEnqueue(const uint8_t* payload, size_t length) {
  if (payload == nullptr || length == 0 || length > kPbDeferMaxBytes) {
    log_warn("[PB] defer reject len=%u", (unsigned)length);
    return false;
  }
  const uint8_t next_tail = static_cast<uint8_t>((pb_defer_tail_ + 1) % kPbDeferQueueDepth);
  if (next_tail == pb_defer_head_) {
    log_warn("[PB] defer queue full (depth=%u), drop len=%u", (unsigned)kPbDeferQueueDepth,
             (unsigned)length);
    return false;
  }
  uint8_t* buf = pb_defer_bufs_[pb_defer_tail_];
  if (buf == nullptr) {
    buf = static_cast<uint8_t*>(
        heap_caps_malloc(kPbDeferMaxBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (buf == nullptr) {
      buf = static_cast<uint8_t*>(malloc(kPbDeferMaxBytes));
    }
    if (buf == nullptr) {
      log_error("[PB] defer alloc failed %u bytes", (unsigned)kPbDeferMaxBytes);
      return false;
    }
    pb_defer_bufs_[pb_defer_tail_] = buf;
  }
  memcpy(buf, payload, length);
  pb_defer_lens_[pb_defer_tail_] = length;
  pb_defer_tail_ = next_tail;
  return true;
}

uint8_t AsrChatClient::pbDeferQueueDepth() const {
  return static_cast<uint8_t>((pb_defer_tail_ + kPbDeferQueueDepth - pb_defer_head_) %
                              kPbDeferQueueDepth);
}

void AsrChatClient::flushDeferredPbJson(bool pump_after) {
  (void)pump_after;
  /* 迭代处理 defer 队列；expect_bin 等待 TTS 音频时仍放行纯舵机/表情 pb_single。 */
  while (pb_defer_head_ != pb_defer_tail_) {
    if (pbCompletedReplayExpectsBin()) {
      /* The next wire frame belongs to the cached req.  Never inspect a later
       * JSON until every declared replay BIN has been consumed. */
      break;
    }
    if (pb_expect_bin_) {
      /* One global PB parser owns req/idx/BIN framing.  Even a media-free
       * gesture must wait until the declared binary has been consumed;
       * parsing it here would overwrite the active request and corrupt the
       * next PB_WIRE binary association. */
      break;
    }

    const uint8_t slot = pb_defer_head_;
    const size_t len = pb_defer_lens_[slot];
    const uint8_t* const wire = pb_defer_bufs_[slot];
    pb_defer_head_ = static_cast<uint8_t>((pb_defer_head_ + 1) % kPbDeferQueueDepth);
    if (len == 0 || wire == nullptr) {
      continue;
    }

    JsonDocument doc;
    if (deserializeJson(doc, wire, len)) {
      log_warn("[PB] defer deserialize failed len=%u", (unsigned)len);
      pbProtocolError("deferred PB JSON parse failed");
      pbDiscardDeferredJsonQueue();
      return;
    }
    (void)pbParseAndStage(doc);
  }
}

void AsrChatClient::pbDiscardDeferredJsonQueue() {
  pb_defer_head_ = pb_defer_tail_;
}

bool AsrChatClient::pbParseAndStage(const JsonDocument& doc) {
  String type = doc["type"].is<String>() ? doc["type"].as<String>() : String("");
  if (type != "pb_start" && type != "pb_chunk" && type != "pb_end" && type != "pb_single") {
    return false;
  }
  String req = doc["req"].is<String>() ? doc["req"].as<String>() : String("");
  if (req.isEmpty()) {
    log_warn("[PB] preflight rejected frame without req; active req preserved");
    return true;
  }
  if (req.length() > DESKBOT_PB_REQ_MAX_CHARS) {
    log_warn("[PB] preflight rejected oversized req len=%u; active req preserved",
             (unsigned)req.length());
    if (type == "pb_start" || type == "pb_single") {
      pb_suppress_tail_req_ = req;
      (void)pbStageRejectedDiscard(doc, req);
    }
    return true;
  }

  if (pb_rejected_discard_active_) {
    if (req != pb_rejected_discard_req_) {
      pbClearRejectedDiscard("different req arrived");
    } else {
      const bool explicit_restart =
          (type == "pb_start" || type == "pb_single") &&
          doc["idx"].is<uint32_t>() && doc["idx"].as<uint32_t>() == 0u;
      if (explicit_restart) {
        pbClearRejectedDiscard("explicit rejected-req restart");
        if (pb_suppress_tail_req_ == req) pb_suppress_tail_req_.remove(0);
      } else {
        (void)pbStageRejectedDiscard(doc, req);
        log_info("[PB] ignored rejected-chain tail type=%s req=%s",
                 type.c_str(), req.c_str());
        return true;
      }
    }
  }

  const PbCompletedReq* completed = pbFindCompletedReq(req);
  PbChunkPreflight preflight{};
  const char* preflight_error = nullptr;
  if (!pbPreflightChunk(doc, completed != nullptr, &preflight,
                        &preflight_error)) {
    const uint32_t rejected_idx =
        doc["idx"].is<uint32_t>() ? doc["idx"].as<uint32_t>() : 0u;
    log_warn("[PB] preflight rejected req=%s type=%s idx=%u why=%s",
             req.c_str(), type.c_str(), (unsigned)rejected_idx,
             preflight_error ? preflight_error : "?");
    const bool corrupts_active_chain =
        (type == "pb_chunk" || type == "pb_end") && pb_active_ &&
        req == pb_req_;
    if (corrupts_active_chain) {
      pbProtocolError(preflight_error ? preflight_error
                                      : "PB chunk preflight failed");
    } else {
      if ((type == "pb_start" || type == "pb_single") &&
          (!pb_active_ || req != pb_req_)) {
        if (type == "pb_start") pb_suppress_tail_req_ = req;
        (void)pbStageRejectedDiscard(doc, req);
      }
      const bool rejected_durable =
          doc["durable"].is<bool>() && doc["durable"].as<bool>();
      pbQueueTerminalAck(req.c_str(), rejected_idx,
                         PbSequenceOutcome::kFailed, rejected_durable);
    }
    return true;
  }
  const uint32_t idx = preflight.idx;
  const bool durable_replay = preflight.durable;

  if (completed != nullptr) {
    /*
     * A same-boot retry may upgrade an already terminal RAM entry to durable.
     * Persist before replaying its ACK so a subsequent reboot remains safe.
     */
    if (durable_replay && !completed->persisted) {
      (void)pbRememberCompletedReq(req.c_str(), completed->idx,
                                  completed->outcome, /*persist=*/true);
      completed = pbFindCompletedReq(req);
      if (completed == nullptr) {
        pbQueueTerminalAck(req.c_str(), idx, PbSequenceOutcome::kFailed);
        return true;
      }
    }
    if (!pbStageCompletedReplay(doc, *completed)) {
      pbClearCompletedReplay("invalid completed replay framing");
      resetTransportSession("invalid completed PB replay framing");
    }
    return true;
  }
  if (pb_completed_replay_active_ &&
      !pbCompletedReplayExpectsBin()) {
    pbClearCompletedReplay("uncached req arrived");
  }

  if (!pb_suppress_tail_req_.isEmpty()) {
    if (req != pb_suppress_tail_req_) {
      pb_suppress_tail_req_.remove(0);
    } else if (!((type == "pb_start" || type == "pb_single") && idx == 0u)) {
      log_info("[PB] ignore stale pb after abort type=%s idx=%u req=%s", type.c_str(), (unsigned)idx,
               req.c_str());
      return true;
    } else {
      pb_suppress_tail_req_.remove(0);
    }
  }

  /* mic-only pb_single：anim[]/servo[]/audio/assets 均可空，仅设备侧开麦/禁麦提示。 */
  const PbMicHint mic_hint_early = pb_parse_mic_hint(doc);
  bool rtc_interruptible_early = false;
  const bool has_rtc_mode_early =
      pb_parse_rtc_mode(doc, &rtc_interruptible_early);
  if (type == "pb_single" && idx == 0u &&
      (mic_hint_early != PbMicHint::kNone || has_rtc_mode_early)) {
    const size_t next_bin_early = pb_read_audio_next_bin_len(doc);
    if (next_bin_early == 0 && !pb_doc_has_nonempty_anim(doc) && !pb_doc_has_nonempty_servo(doc) &&
        !pb_doc_has_asset_bins(doc)) {
      if (has_rtc_mode_early) {
        deskbot_uplink_set_rtc_interruptible(rtc_interruptible_early);
      }
      if (mic_hint_early == PbMicHint::kOpen) {
        micUplinkSetActive(true);
        tts_active_ = false;
        pb_active_ = false;
        pbSignalTtsRoundComplete();
      } else if (mic_hint_early == PbMicHint::kMute) {
        micUplinkSetActive(false);
      }
      /*
       * Reuse the normal pending ACK state so a busy USB writer retries this
       * accepted phase instead of silently dropping it.  flushPendingPbAck()
       * owns the PB_WIRE|JSON channel selection for every PB ACK.
       */
      if (!pb_ack_out_pending_) {
        pb_ack_out_req_ = req;
        pb_ack_out_idx_ = 0;
        pb_ack_out_buf_ms_ = 0;
        pb_ack_out_phase_ = "accepted";
        pb_ack_out_commanded_x_ = static_cast<int16_t>(head_read_x());
        pb_ack_out_commanded_y_ =
            static_cast<int16_t>(head_read_y_logic());
        pb_ack_out_display_crc_valid_ = false;
        pb_ack_out_display_crc32_ = 0;
        pb_ack_out_pending_ = true;
        pb_ack_bypass_throttle_ = true;
        flushPendingPbAck();
      }
      log_info("[PB] media hint pb_single req=%s mic=%s rtc_interruptible=%d",
               req.c_str(),
               mic_hint_early == PbMicHint::kOpen
                   ? "open"
                   : mic_hint_early == PbMicHint::kMute ? "mute" : "hold",
               (int)deskbot_uplink_rtc_interruptible());
      pbQueueTerminalAck(req.c_str(), 0, PbSequenceOutcome::kPlayed,
                         durable_replay);
      return true;
    }
  }

  const uint32_t chunk_ms = preflight.chunk_ms;
  const PbEnqueueAction chunk_action = preflight.action;
  const int8_t chunk_level = preflight.level;

  const bool is_chain_head = preflight.is_chain_head;
  const bool servo_only_gesture = pb_doc_is_servo_only_gesture(doc);
  if (is_chain_head) {
    const PbQueueDecision qd = pbDecideChainHead(chunk_level, chunk_action);
    const bool unsafe_cross_request_append =
        qd == PbQueueDecision::kAppend && pbHasPendingTerminalTracks();
    if (qd == PbQueueDecision::kDrop || unsafe_cross_request_append) {
      log_warn("[PB] drop chain head req=%s type=%s level=%d queue_level=%d n_high=%d",
               req.c_str(), type.c_str(), (int)chunk_level, (int)pb_queue_level_,
               pbCountHigherPrioritySeqs(chunk_level));
      if (unsafe_cross_request_append) {
        log_warn("[PB] cross-request append rejected while prior lanes are active req=%s",
                 req.c_str());
      }
      if (type == "pb_start") {
        /* The service may already have queued this rejected chain's remaining
         * JSON/BIN frames. Consume its tail without treating it as corruption
         * of the still-running request. */
        pb_suppress_tail_req_ = req;
      }
      pbQueueTerminalAck(
          req.c_str(), idx,
          unsafe_cross_request_append ? PbSequenceOutcome::kFailed
                                      : PbSequenceOutcome::kCancelled,
          durable_replay);
      (void)pbStageRejectedDiscard(doc, req);
      return true;
    }
    const bool force_restart_same_req =
        pb_active_ && (req == pb_req_) && (chunk_action == PbEnqueueAction::kReplace);
    if (qd == PbQueueDecision::kClear || force_restart_same_req) {
      const bool has_display = pb_doc_has_nonempty_anim(doc);
      const bool has_motor = pb_doc_has_nonempty_servo(doc);
      const bool has_audio = pb_doc_has_audio_payload(doc);
      const bool voice_servo_only = in_voice_record_loop_ && servo_only_gesture;
      /* Replace is lane-local. A display-only face must not erase an unrelated
       * gesture or RTC audio; a motor-only gesture must not flash a face. */
      const bool preserve_standalone_rtc =
          rtc_audio_downlink_active() && !pb_audio_stream_started_ &&
          !pbHasPendingAudioTerminalTracks();
      const bool replace_audio =
          has_audio && !preserve_standalone_rtc && !voice_servo_only;
      pbDrainWorkersForNewSequence(has_display, has_motor, replace_audio,
                                   /*cancel_terminals=*/true,
                                   /*preserve_display_baseline=*/
                                       preflight.mouth_only);
      if (servo_only_gesture) {
        log_info("[PB] replace servo gesture: old motor epoch cancelled req=%s",
                 req.c_str());
      } else {
        log_info("[PB] lane-local replace req=%s display=%d motor=%d audio=%d",
                 req.c_str(), (int)has_display, (int)has_motor,
                 (int)replace_audio);
      }
      pb_inflight_seq_count_ = 0;
      pb_seq_level_count_ = 0;
      pb_queue_level_ = chunk_level;
      const char* act_s = (chunk_action == PbEnqueueAction::kAppend)    ? "append"
                          : (chunk_action == PbEnqueueAction::kDefault) ? "default"
                                                                      : "replace";
      log_info("[PB] new sequence clear: req=%s type=%s idx=%u level=%d action=%s",
               req.c_str(), type.c_str(), (unsigned)idx, (int)chunk_level, act_s);
    } else {
      const char* act_s = (chunk_action == PbEnqueueAction::kDefault) ? "default" : "append";
      log_info("[PB] new sequence append: req=%s type=%s idx=%u level=%d queue_level=%d action=%s",
               req.c_str(), type.c_str(), (unsigned)idx, (int)chunk_level, (int)pb_queue_level_, act_s);
    }
    pb_inflight_seq_count_++;
    pbSeqTrackPush(chunk_level);
    if (!pbBeginTerminalTrack(req, idx, durable_replay)) {
      pb_suppress_tail_req_ = req;
      (void)pbStageRejectedDiscard(doc, req);
      return true;
    }
    pb_active_ = true;
    pb_req_ = req;
    pb_next_idx_ = 0;
    pb_bins_rx_count_ = 0;
    pb_pcm_bytes_rx_total_ = 0;
    const bool voice_servo_only = in_voice_record_loop_ && servo_only_gesture;
    if (!voice_servo_only) {
      tts_active_ = true;
    } else {
      log_info("[PB] voice round: servo-only pb_single, keep mic uplink req=%s", req.c_str());
    }
    /* Commit display ownership after any lane-local replace, because replace
     * intentionally revokes the previous owner. Servo/audio-only PB leaves
     * the current display owner untouched. */
    if (pb_doc_has_nonempty_anim(doc)) {
      display_voice_mouth_set_allowed(preflight.voice_mouth);
    }
  } else if (!pb_active_ || req != pb_req_) {
    pbProtocolError("pb_chunk/pb_end without active matching req");
    return true;
  }

  if (!pb_active_ || req != pb_req_) {
    pbProtocolError("req mismatch after reset");
    return true;
  }

  if (idx != pb_next_idx_) {
    if (idx > pb_next_idx_) {
      if (pb_expect_bin_) {
        log_warn("[PB] idx gap while expect BIN: expected %u got %u (JSON should stay queued)",
                 (unsigned)pb_next_idx_, (unsigned)idx);
        return true;
      }
      log_warn("[PB] idx gap: expected %u got %u, resync (do not reset audio)",
               (unsigned)pb_next_idx_, (unsigned)idx);
      pb_next_idx_ = idx;
    } else {
      log_warn("[PB] duplicate idx %u (expected %u), skip",
               (unsigned)idx, (unsigned)pb_next_idx_);
      return true;
    }
  }
  PbTerminalTrack* terminal_track =
      pbFindTerminalTrack(pb_current_epoch_);
  if (terminal_track != nullptr) {
    if (idx > terminal_track->last_idx) {
      terminal_track->last_idx = idx;
    }
  }

  /* Commit the already validated local copy only after queue arbitration and
   * sequence identity checks have accepted this frame. */
  pbCommitServoPreflight(preflight);
  if (is_chain_head) {
    pb_sequence_mouth_only_ = preflight.mouth_only;
  }
  pb_pending_mouth_only_ = pb_sequence_mouth_only_;

  if (doc["sr"].is<uint32_t>()) {
    pb_sr_ = doc["sr"].as<uint32_t>();
  }
  if (doc["ch"].is<int>()) {
    pb_ch_ = (uint8_t)doc["ch"].as<int>();
  } else if (doc["ch"].is<double>()) {
    pb_ch_ = (uint8_t)doc["ch"].as<double>();
  } else if (doc["ch"].is<uint8_t>()) {
    pb_ch_ = doc["ch"].as<uint8_t>();
  }
  if (doc["fmt"].is<String>()) {
    pb_fmt_ = doc["fmt"].as<String>();
    pb_fmt_.toLowerCase();
  }

  /* volume（0–100）：有值则更新播放音量比例，省略则沿用上次值。 */
  if (doc["volume"].is<int>()) {
    const int vol = constrain(doc["volume"].as<int>(), 0, 100);
    pb_volume_ratio_ = vol / 100.0f;
    log_info("[PB] volume=%d ratio=%.2f", vol, pb_volume_ratio_);
  }

  /*
   * camera_once requests one fresh frame without turning the camera into a
   * permanent stream.  cam_fps 设定浏览器预览的上传节奏：唯一生产者是
   * PC 端的 CameraCadenceController（LLM 可写通道已在服务端源头删除，
   * 本通道不再构成租约体系外的旁路）。
   */
  const bool camera_once = doc["camera_once"] | false;
  if (camera_once) {
    camera_request_snapshot();
  } else if (doc["cam_fps"].is<int>()) {
    const int fps = doc["cam_fps"].as<int>();
    if (fps >= 0) {
      camera_set_fps((uint32_t)fps);
    }
  }

  if (doc["pb_ver"].is<int>() && doc["pb_ver"].as<int>() != 2) {
    log_warn("[PB] pb_ver=%d (expected 2)", doc["pb_ver"].as<int>());
  }

  pbFreePendingAnim();
  if (doc["anim"].is<JsonArrayConst>()) {
    JsonArrayConst anim_arr = doc["anim"].as<JsonArrayConst>();
    if (anim_arr.size() > 0) {
      const size_t need = measureJson(anim_arr);
      if (need > 0) {
        char* buf = static_cast<char*>(heap_caps_malloc(need + 1, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (buf == nullptr) {
          buf = static_cast<char*>(malloc(need + 1));
        }
        if (buf != nullptr) {
          const size_t n = serializeJson(anim_arr, buf, need + 1);
          if (n > 0) {
            buf[n] = '\0';
            pb_pending_anim_buf_ = buf;
            pb_pending_anim_len_ = n;
            log_info("[PB] anim[] staged idx=%u segs=%u len=%u", (unsigned)idx, (unsigned)anim_arr.size(),
                     (unsigned)n);
          } else {
            ::free(buf);
            pbFailCurrentTerminal(idx, "anim serialization failed");
          }
        } else {
          pbFailCurrentTerminal(idx, "anim allocation failed");
        }
      }
    }
  } else if (!doc["anim"].isNull()) {
    log_warn("[PB] anim must be array (pb_ver 2); ignore idx=%u", (unsigned)idx);
    pbFailCurrentTerminal(idx, "anim must be array");
  }

  if (pb_pending_pcm_owned_ != nullptr) {
    pbProtocolError("previous PB PCM was not dispatched");
    return true;
  }
  pbFreePendingAssets();
  pbBuildPendingBinQueue(doc);
  for (uint8_t bin_index = 0; bin_index < pb_pending_bin_count_;
       ++bin_index) {
    if (pb_pending_bin_lens_[bin_index] > DESKBOT_USB_MAX_PAYLOAD) {
      log_error("[PB] logical BIN exceeds limit req=%s idx=%u bin=%u len=%u max=%u",
                pb_req_.c_str(), (unsigned)idx, (unsigned)bin_index,
                (unsigned)pb_pending_bin_lens_[bin_index],
                (unsigned)DESKBOT_USB_MAX_PAYLOAD);
      pbProtocolError("logical binary exceeds device limit");
      return true;
    }
  }
  const size_t next_bin_len = pb_read_audio_next_bin_len(doc);
  const bool expect_pcm_bin = (next_bin_len > 0);
  const bool expect_asset_bins = pb_doc_has_asset_bins(doc);
  const PbMicHint mic_hint = pb_parse_mic_hint(doc);
  bool rtc_interruptible = false;
  const bool has_rtc_mode = pb_parse_rtc_mode(doc, &rtc_interruptible);
  if (mic_hint != PbMicHint::kNone) {
    micUplinkSetActive(mic_hint == PbMicHint::kOpen);
  }
  if (has_rtc_mode) {
    deskbot_uplink_set_rtc_interruptible(rtc_interruptible);
  }
  /* anim[]/servo[] 允许为空；有效载荷：非空 anim/servo、audio BIN、assets BIN 或 mic 提示。 */
  const bool has_payload = (pb_pending_anim_buf_ != nullptr && pb_pending_anim_len_ > 0) ||
                           (pb_pending_servo_seg_count_ > 0) || expect_pcm_bin || expect_asset_bins ||
                           (mic_hint != PbMicHint::kNone) || has_rtc_mode;
  if (!has_payload) {
    log_warn("[PB] skip empty chunk type=%s idx=%u req=%s", type.c_str(), (unsigned)idx, req.c_str());
    pbMaybeAck(idx);
    pb_ack_bypass_throttle_ = true;
    pb_next_idx_ = idx + 1;
    if (type == "pb_end" || type == "pb_single") {
      if (pb_audio_stream_started_) {
        const uint8_t ch_end =
            (pb_ch_ == 0 || pb_ch_ > 2) ? 1 : pb_ch_;
        if (!audio_pb_stream_end(pb_current_epoch_, pb_req_.c_str(), idx,
                                 ch_end)) {
          pbFailCurrentTerminal(idx, "audio end enqueue failed");
          audio_play_emergency_flush();
        }
        pb_audio_stream_started_ = false;
      }
      pbSealCurrentTerminalTrack(idx);
      pbOnSequenceComplete();
    }
    return true;
  }

  if (pb_pending_bin_count_ > 0) {
    if (expect_pcm_bin) {
      if (pb_sr_ == 0 || pb_ch_ == 0 || pb_fmt_.isEmpty()) {
        pbProtocolError("audio.next_bin_len but missing sr/ch/fmt");
        return true;
      }
      if (pb_fmt_ != "s16le" && pb_fmt_ != "opus") {
        pbProtocolError("unsupported fmt (need s16le or opus)");
        return true;
      }
      if (pb_fmt_ == "s16le" && (next_bin_len & 1u) != 0u) {
        pbProtocolError("audio.next_bin_len must be even for s16le");
        return true;
      }
    }
    pb_expect_bin_ = true;
    pb_expect_bin_kind_ = pb_pending_bin_kinds_[0];
    pb_expect_bin_len_ = pb_pending_bin_lens_[0];
    pb_expect_opus_frames_ = pb_pending_bin_frames_[0];
    pb_expect_bin_since_ms_ = millis();
    pb_pending_idx_ = idx;
    pb_pending_chunk_ms_ = chunk_ms;
    log_info("[PB] expect BIN req=%s type=%s idx=%u bins=%u first_kind=%u first_len=%u chunk_ms=%u "
             "heap=%u",
             pb_req_.c_str(), type.c_str(), (unsigned)idx, (unsigned)pb_pending_bin_count_,
             (unsigned)pb_expect_bin_kind_, (unsigned)pb_expect_bin_len_,
             (unsigned)pb_pending_chunk_ms_, (unsigned)ESP.getFreeHeap());
    if (type == "pb_end" || type == "pb_single") {
      pb_end_waiting_bin_ = true;
      pb_end_idx_ = idx;
    } else {
      pb_end_waiting_bin_ = false;
    }
    pb_pending_enqueue_action_ = chunk_action;
  } else {
    const bool sequence_end = (type == "pb_end" || type == "pb_single");
    const uint32_t sequence_epoch = pb_current_epoch_;
    const String sequence_req = pb_req_;
    const uint32_t start_at_ms =
        pbArmCurrentChunkTimeline(idx, chunk_ms);
    if (start_at_ms == 0 ||
        !pbDispatchChunkPreamble(idx, start_at_ms)) {
      pbProtocolError("display/motor timeline submit failed");
      return true;
    }
    pbMaybeAck(idx);
    pb_ack_bypass_throttle_ = true;
    if (sequence_end) {
      /* 允许“最后一片无音频”的情况：收尾释放 pipeline。pb_single 语义为单包整轮，与 pb_end 同收尾。 */
      log_info("[PB] complete end_no_bin req=%s type=%s idx=%u inflight=%u",
               pb_req_.c_str(), type.c_str(), (unsigned)idx, (unsigned)pb_inflight_seq_count_);
      if (pb_audio_stream_started_) {
        const uint8_t ch_end = (pb_ch_ == 0 || pb_ch_ > 2) ? 1 : pb_ch_;
        if (!audio_pb_stream_end(sequence_epoch, sequence_req.c_str(), idx,
                                 ch_end)) {
          pbFailCurrentTerminal(idx, "audio end enqueue failed");
          audio_play_emergency_flush();
        }
        pb_audio_stream_started_ = false;
      }
      pb_end_waiting_bin_ = false;
      pb_end_idx_ = 0;
      pbSealCurrentTerminalTrack(idx);
      pbOnSequenceComplete();
    }
  }

  /* 有 audio.next_bin_len 时须等 BIN 入队后再推进 idx，避免覆盖 expect_len。 */
  if (!pb_expect_bin_) {
    pb_next_idx_ = idx + 1;
  }
  return true;
}

void AsrChatClient::abortRuntimeState(const char* why) {
  resetUsbPbJsonAssembly();
  deskbot_uplink_set_transport_ready(false);
  discardPendingUplinkMedia();
  pbDiscardDeferredJsonQueue();
  pbClearRejectedDiscard("runtime aborted");
  if (!pb_req_.isEmpty()) {
    pb_suppress_tail_req_ = pb_req_;
  }
  pbCancelTerminalTracks(why ? why : "transport aborted");
  rtc_audio_downlink_cancel();

  /* Cancel epochs in all workers make the currently executing chunk/segment
   * exit on a short tick; reset also drains work not started yet. */
  audio_play_emergency_flush();
  /* Disconnect is a transport boundary, not a request to change expression:
   * the replace keeps the interpolation baseline (s_prev_*) intact for CRC
   * continuity.  The panel pixels, however, switch to the standby screen
   * (builtin face + 「请连接PC服务」) until the next hello clears it. */
  display_render_replace(/*preserve_baseline=*/true);
  display_standby_set(true);
  head_clear_motor_pending();

  pb_audio_stream_started_ = false;
  pbReset(/*stop_audio=*/false);
  link_abort_round_ = true;
  reply_done_ = true;
  tts_active_ = false;
  ready_ = false;
  log_warn("[ASR_CHAT] runtime aborted (%s)", why ? why : "?");
}

void AsrChatClient::resetTransportSession(const char* why) {
  log_warn("[ASR_CHAT] reset USB session (%s)", why ? why : "?");
  if (usb_transport_is_active()) {
    /*
     * usb_transport_end_session() invalidates the epoch, bumps the transport
     * generation and invokes onUsbLinkState(false) exactly once.
     */
    usb_transport_end_session(why);
  } else {
    abortRuntimeState(why);
  }
  ready_ = false;
  transport_send_fail_streak_ = 0;
}

bool AsrChatClient::transportCanSend() {
  return ready_ && usb_transport_is_active() &&
         usb_session_epoch_ != 0 &&
         usb_session_epoch_ == usb_transport_session_epoch() &&
         deskbot_uplink_transport_allowed();
}

void AsrChatClient::noteTransportSendOk() {
  transport_send_fail_streak_ = 0;
}

void AsrChatClient::noteTransportSendFail(const char* what) {
  if (transport_send_fail_streak_ < 255) {
    ++transport_send_fail_streak_;
  }
  log_warn("[ASR_CHAT] transport send fail %u/%u (%s)",
           (unsigned)transport_send_fail_streak_,
           (unsigned)kTransportSendFailResetThreshold,
           what ? what : "?");
  if (transport_send_fail_streak_ >=
      kTransportSendFailResetThreshold) {
    resetTransportSession(what ? what : "send streak");
  }
}

bool AsrChatClient::connect() {
  usb_transport_poll();
  return transportCanSend();
}

void AsrChatClient::pbTickExpectBinTimeout() {
  if (pbRejectedDiscardExpectsBin() &&
      pb_rejected_discard_bin_since_ms_ != 0) {
    const unsigned long wait_ms =
        millis() - pb_rejected_discard_bin_since_ms_;
    if (wait_ms > (unsigned long)DESKBOT_PB_EXPECT_BIN_TIMEOUT_MS) {
      log_warn("[PB] rejected-chain BIN timeout req=%s wait=%lums; "
               "discard until next JSON",
               pb_rejected_discard_req_.c_str(), wait_ms);
      pb_rejected_discard_unframed_ = true;
      pb_rejected_discard_bin_count_ = 0;
      pb_rejected_discard_bin_cursor_ = 0;
      pb_rejected_discard_bin_received_ = 0;
      pb_rejected_discard_bin_since_ms_ = 0;
      flushDeferredPbJson(/*pump_after=*/false);
    }
    return;
  }
  if (pbCompletedReplayExpectsBin() &&
      pb_completed_replay_bin_since_ms_ != 0) {
    const size_t expected =
        pb_completed_replay_bin_lens_[pb_completed_replay_bin_cursor_];
    const unsigned long wait_ms =
        millis() - pb_completed_replay_bin_since_ms_;
    unsigned long limit_ms =
        (unsigned long)DESKBOT_PB_EXPECT_BIN_TIMEOUT_MS;
    if (expected > 200000U) {
      limit_ms = 30000UL;
    } else if (expected > 50000U) {
      limit_ms = 20000UL;
    }
    if (wait_ms > limit_ms) {
      log_error("[PB] completed replay BIN timeout wait=%lums limit=%lums "
                "expect_len=%u req=%s",
                wait_ms, limit_ms, (unsigned)expected,
                pb_completed_replay_req_.c_str());
      pbClearCompletedReplay("BIN timeout");
      resetTransportSession("completed PB replay BIN timeout");
    }
    return;
  }
  if (!pb_expect_bin_ || pb_expect_bin_since_ms_ == 0) {
    return;
  }
  const unsigned long wait_ms = millis() - pb_expect_bin_since_ms_;
  unsigned long limit_ms = (unsigned long)DESKBOT_PB_EXPECT_BIN_TIMEOUT_MS;
  if (pb_expect_bin_len_ > 200000U) {
    limit_ms = 30000UL;
  } else if (pb_expect_bin_len_ > 50000U) {
    limit_ms = 20000UL;
  }
  if (wait_ms <= limit_ms) {
    return;
  }
  log_warn("[PB] expect_bin timeout wait=%lums limit=%lums expect_len=%u req=%s",
           wait_ms, limit_ms, (unsigned)pb_expect_bin_len_, pb_req_.c_str());
  pbProtocolError("expect bin timeout");
  flushDeferredPbJson(/*pump_after=*/false);
}

void AsrChatClient::loopLite() {
  if (audio_play_is_on_play_task()) {
    return;
  }
  usb_transport_poll();
  if (rtc_audio_downlink_ready_to_close() &&
      audio_play_input_queue_depth() == 0u &&
      !audio_play_i2s_in_progress()) {
    if (audio_play_stream_pcm_active()) {
      audio_stream_pcm16_stop();
    } else {
      /* Keep the RTC generation cancellable until audio_play has processed
       * END and completed its DMA silence tail.  Closing it immediately after
       * merely enqueueing END made a barge-in during that tail look idle and
       * skip the emergency speaker flush. */
      rtc_audio_downlink_mark_closed();
    }
  }
  pbDrainWorkerTerminals();
  pbExpireTerminalTracks();
  flushPendingPbAck();
  if (!pbAnyDownlinkBinExpected()) {
    flushDeferredPbJson(/*pump_after=*/false);
  }
  flushPendingPbAck();
  pbTickExpectBinTimeout();
}

void AsrChatClient::serviceLoop() {
  if (audio_play_is_on_play_task()) {
    return;
  }
  loopLite();
  maybeSendBootConnect();
  if (!pb_active_) {
    updateAttentionDisplay();
  }
  shouldSuppressMicUplink();
  drainAudioFrontendVadEvents();
}

bool AsrChatClient::isCameraUplinkPaused() const {
  return isVoiceUplinkBusy() || deskbot_uplink_speaker_audible() || audio_play_speaker_busy();
}

bool AsrChatClient::isVoiceUplinkBusy() const {
  if (voice_uplink_active_.load(std::memory_order_acquire)) {
    return true;
  }
  const unsigned long last =
      last_voice_uplink_activity_ms_.load(std::memory_order_acquire);
  return last != 0 && (millis() - last) < 80UL;
}

bool AsrChatClient::isSpeaking() const {
  return deskbot_uplink_speaker_audible() || audio_play_speaker_busy();
}

bool AsrChatClient::isMicTailSuppressed() const {
  return deskbot_uplink_in_tail_suppress();
}

bool AsrChatClient::isVadGateOpen() const {
  return vad_gate_open_;
}

void AsrChatClient::updateAttentionDisplay() {
  unsigned long now = millis();
  /*
   * Keep only the logical attention state.  No automatic logic moves the
   * head; only explicit PB motor commands may do so.
   */
  const bool should_wake = tts_active_;

  if (should_wake) {
    last_should_wake_ms_ = now;
    if (display_state_ != DISPLAY_WAKEUP) {
      log_info("[ATTENTION] -> WAKEUP (tts=%d)", (int)tts_active_);
      display_state_ = DISPLAY_WAKEUP;
    }
    return;
  }

  bool first_time = (last_should_wake_ms_ == 0) && (display_state_ == DISPLAY_UNINIT);
  bool dwell_done = (last_should_wake_ms_ != 0) &&
                    (now - last_should_wake_ms_ >= kIdleEnterDelayMs);
  if (display_state_ != DISPLAY_SLEEP && (first_time || dwell_done)) {
    log_info("[ATTENTION] -> IDLE (motion disabled dwell=%lums first=%d)",
             last_should_wake_ms_ == 0 ? 0UL : (now - last_should_wake_ms_),
             (int)first_time);
    display_state_ = DISPLAY_SLEEP;
  }
}

bool AsrChatClient::sendJson(const char* msg, bool critical) {
  if (msg == nullptr || !transportCanSend()) {
    return false;
  }
  const bool sent = usb_transport_send_control_json(msg);
  if (!sent) {
    if (critical) {
      noteTransportSendFail("CONTROL_JSON");
    }
    return false;
  }
  if (critical) {
    noteTransportSendOk();
  }
  return true;
}

bool AsrChatClient::sendJson(const String& msg, bool critical) {
  return sendJson(msg.c_str(), critical);
}

void AsrChatClient::maybeSendBootConnect() {
  if (!boot_connect_pending_ || boot_connect_sent_ ||
      !transportCanSend()) {
    return;
  }
  if (sendJson("{\"type\":\"boot_connect\"}", /*critical=*/false)) {
    boot_connect_sent_ = true;
    boot_connect_pending_ = false;
    log_info("[ASR_CHAT] boot_connect sent exactly once for this power cycle");
  }
}

bool AsrChatClient::shouldSuppressMicUplink() {
  /*
   * A PB response becomes active as soon as its JSON declaration arrives,
   * before its binary audio reaches the player. Stop microphone traffic
   * during that download window as well as during audible playback. This
   * prevents AUDIO_UP_OPUS from delaying ACKs for large PB media and avoids
   * retaining pre-playback room audio for the next ASR turn. Servo-only
   * gestures deliberately leave tts_active_ false.
   */
  const bool stable_tts_gate =
      tts_active_ && !deskbot_uplink_rtc_interruptible();
  return stable_tts_gate || !deskbot_uplink_capture_allowed();
}

void AsrChatClient::engageVoiceUplink() {
  voice_uplink_active_.store(true, std::memory_order_release);
  last_voice_uplink_activity_ms_.store(millis(), std::memory_order_release);
}

void AsrChatClient::discardPendingUplinkMedia() {
  vad_gate_open_ = false;
  voice_uplink_active_.store(false, std::memory_order_release);
  resetUplinkBatch();
}

void AsrChatClient::resetUplinkBatch() {
  uplink_batch_bin_len_ = 0;
  uplink_batch_count_ = 0;
}

bool AsrChatClient::flushAudioOpusBatch() {
  if (uplink_batch_count_ == 0 || uplink_batch_bin_len_ == 0) {
    return true;
  }
  engageVoiceUplink();
  const bool sent =
      usb_transport_send(DESKBOT_USB_AUDIO_UP_OPUS, uplink_batch_bin_,
                         uplink_batch_bin_len_);
  voice_uplink_active_.store(false, std::memory_order_release);
  last_voice_uplink_activity_ms_.store(millis(), std::memory_order_release);
  resetUplinkBatch();
  if (!sent) {
    noteTransportSendFail("AUDIO_UP_OPUS");
  } else {
    noteTransportSendOk();
  }
  return sent;
}

void AsrChatClient::drainAudioFrontendVadEvents() {
  AudioFrontendVadEvent event{};
  while (audio_frontend_take_vad_event(&event)) {
    const bool speech = event.state == AudioFrontendVadState::kSpeech;
    vad_gate_open_ = speech;
    char json[192];
    const int n = snprintf(
        json,
        sizeof(json),
        "{\"type\":\"audio_vad\",\"state\":\"%s\","
        "\"source\":\"esp_sr\",\"sequence\":%u,\"volume_db_x100\":%d}",
        speech ? "speech_start" : "speech_end",
        static_cast<unsigned>(event.sequence),
        static_cast<int>(event.volume_db_x100));
    const bool sent =
        n > 0 && static_cast<size_t>(n) < sizeof(json) &&
        sendJson(json, /*critical=*/false);
    log_info(
        "[ESP-SR] VAD %s seq=%u volume_db_x100=%d signalled=%d",
        speech ? "speech_start" : "speech_end",
        static_cast<unsigned>(event.sequence),
        static_cast<int>(event.volume_db_x100),
        static_cast<int>(sent));
  }
}

bool AsrChatClient::queueAudioOpusFrame(const int16_t* pcm, size_t samples) {
  static uint8_t opus_buf[256];
  const size_t opus_len = opus_uplink_encode(pcm, samples, opus_buf, sizeof(opus_buf));
  if (opus_len == 0) {
    log_warn("[ASR_CHAT] Opus encode failed samples=%u", (unsigned)samples);
    return false;
  }
  if (opus_len > 65535U || uplink_batch_bin_len_ + 2U + opus_len > kUplinkBatchMaxBin) {
    log_warn("[ASR_CHAT] Opus batch overflow len=%u bin=%u",
             (unsigned)opus_len, (unsigned)uplink_batch_bin_len_);
    return false;
  }
  uplink_batch_bin_[uplink_batch_bin_len_++] = static_cast<uint8_t>((opus_len >> 8) & 0xFF);
  uplink_batch_bin_[uplink_batch_bin_len_++] = static_cast<uint8_t>(opus_len & 0xFF);
  memcpy(uplink_batch_bin_ + uplink_batch_bin_len_, opus_buf, opus_len);
  uplink_batch_bin_len_ += opus_len;
  uplink_batch_count_++;
  if (uplink_batch_count_ >= kUplinkBatchFrames) {
    return flushAudioOpusBatch();
  }
  return true;
}

bool AsrChatClient::runVoiceRound(uint16_t max_record_seconds) {
  round_id_++;
  char round_detail[24];
  snprintf(round_detail, sizeof(round_detail), "id=%u", (unsigned)round_id_);
  LogTaskScope round_scope("asr_round", round_detail);

  round_scope.phase("usb_link");
  if (!connect()) {
    log_warn("[ASR_CHAT] round=%u skip voice (USB host unavailable)",
             (unsigned)round_id_);
    return true;
  }

  if (max_record_seconds == 0 || max_record_seconds > 60) {
    max_record_seconds = (uint16_t)DESKBOT_UPLINK_MAX_SEC;
  }

  link_abort_round_ = false;
  reply_done_ = false;
  const uint32_t uplink_gen =
      deskbot_uplink_transport_generation();
  const bool continuous_rollover = (uplink_codec_generation_ == uplink_gen);
  capture_was_allowed_ =
      continuous_rollover && !shouldSuppressMicUplink();
  /* 每轮开头清掉上一轮 pb/PCM 残余；但若 USB 仍在推本轮 TTS（pb_active），此处 pbReset 会停流并清
   * expect_bin → 后续 BIN 变「unexpected」、idx 全错位（见 CHAT 重叠发起 runVoiceRound 与下行并行）。
   * 仅在没有进行中 pb 序列时才硬清。 */
  if (!pb_active_ && !pbAnyDownlinkBinExpected()) {
    if (pbHasPendingTerminalTracks()) {
      /* A capture rollover is not a PB replacement.  Audio, display and motor
       * actors may still be completing a sealed request after its JSON/BIN
       * ingress has closed.  Preserve those lanes here; the terminal lease
       * sweeper owns recovery if a worker genuinely stops reporting. */
      log_info("[ASR_CHAT] round=%u start: preserve pending PB terminal lanes",
               (unsigned)round_id_);
    } else {
      pbReset(/*stop_audio=*/true);
      pbDiscardDeferredJsonQueue();
      for (int i = 0; i < 8; ++i) {
        loopLite();
        if (!pbAnyDownlinkBinExpected()) {
          flushDeferredPbJson(/*pump_after=*/false);
        }
      }
    }
  } else {
    log_warn("[ASR_CHAT] round=%u start: skip initial pbReset (pb_active=%d expect_bin=%d req=%s next_idx=%u)",
             (unsigned)round_id_, (int)pb_active_, (int)pb_expect_bin_, pb_req_.c_str(), (unsigned)pb_next_idx_);
  }
  struct VoiceUplinkGuard {
    AsrChatClient& client;
    explicit VoiceUplinkGuard(AsrChatClient& c) : client(c) {}
    ~VoiceUplinkGuard() { client.voice_uplink_active_ = false; }
  } voice_guard(*this);

  if (!continuous_rollover) {
    if (!opus_uplink_init()) {
      log_error("[ASR_CHAT] Opus encoder init failed");
      return false;
    }
    opus_uplink_reset();
    resetUplinkBatch();
    mic_capture_flush_queue();
    enhanceVoice_reset();
    uplink_codec_generation_ = uplink_gen;
  } else {
    log_info("[ASR_CHAT] round=%u gapless capture rollover generation=%u",
             (unsigned)round_id_, (unsigned)uplink_gen);
  }
  vad_gate_open_ = false;
  voice_uplink_active_.store(false, std::memory_order_release);

  const size_t total_samples = static_cast<size_t>(max_record_seconds) * SAMPLE_RATE;
  size_t samples_recorded = 0;
  size_t samples_sent = 0;
  int16_t frame[kFrameSamples20ms];
  uint16_t capture_timeout_streak = 0;
  uint32_t last_capture_timeout_log_ms = 0;
  bool record_aborted_disconnect = false;

  const unsigned long record_deadline_ms =
      millis() + static_cast<unsigned long>(max_record_seconds) * 1000UL + 500UL;

  unsigned long sec_stats_start_ms = millis();
  int16_t sec_signed_min = INT16_MAX;
  int16_t sec_signed_max = INT16_MIN;
  uint64_t sec_abs_sum = 0;
  size_t sec_samp_count = 0;

  log_warn("[ASR_CHAT] round=%u continuous opus uplink up to %us",
           (unsigned)round_id_, (unsigned)max_record_seconds);

  if (shouldSuppressMicUplink()) {
    logMicCaptureBlockReason("(听音中)", round_id_,
                             micCaptureBlockReason(transportCanSend()));
  }

  in_voice_record_loop_ = true;
  struct VoiceRecordLoopGuard {
    AsrChatClient& client;
    explicit VoiceRecordLoopGuard(AsrChatClient& c) : client(c) {}
    ~VoiceRecordLoopGuard() { client.in_voice_record_loop_ = false; }
  } record_loop_guard(*this);

  while (samples_recorded < total_samples && millis() < record_deadline_ms) {
    usb_transport_poll();
    if (!transportCanSend() || link_abort_round_ ||
        deskbot_uplink_transport_generation() != uplink_gen) {
      record_aborted_disconnect = true;
      log_warn("[ASR_CHAT] round=%u record abort (USB link down sent=%u)",
               (unsigned)round_id_, (unsigned)samples_sent);
      break;
    }

    if (shouldSuppressMicUplink()) {
      if (capture_was_allowed_) {
        resetUplinkBatch();
        logMicCaptureBlockReason("(听音中)", round_id_,
                                 micCaptureBlockReason(
                                     transportCanSend()));
      }
      capture_was_allowed_ = false;
      loopLite();
      round_scope.pump("listen");
      /*
       * A mic-only "mute" PB keeps the voice round alive so it can reopen
       * without renegotiating USB. Yielding alone immediately reschedules the
       * only ready task on this core and burns 100% CPU; a short blocked delay
       * leaves time for camera/USB workers and power-management idle.
       */
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }

    if (!capture_was_allowed_) {
      log_warn("[MIC_UPLINK] 开始正常录音 (听音中) round=%u "
               "isSpeaking=%d transport_ok=%d",
               (unsigned)round_id_, (int)isSpeaking(),
               (int)transportCanSend());
      mic_capture_flush_queue();
      enhanceVoice_reset();
      capture_was_allowed_ = true;
    }

    if (!record(frame, kFrameSamples20ms, pdMS_TO_TICKS(100))) {
      if (capture_timeout_streak < UINT16_MAX) {
        ++capture_timeout_streak;
      }
      const uint32_t capture_timeout_now_ms = millis();
      if (capture_timeout_streak == 1 ||
          static_cast<uint32_t>(
              capture_timeout_now_ms - last_capture_timeout_log_ms) >=
              10000U) {
        log_warn("[MIC] processed frame timeout streak=%u",
                 static_cast<unsigned>(capture_timeout_streak));
        last_capture_timeout_log_ms = capture_timeout_now_ms;
      }
      drainAudioFrontendVadEvents();
      loopLite();
      round_scope.pump("mic_wait");
      continue;
    }
    capture_timeout_streak = 0;
    enhanceVoice(frame, kFrameSamples20ms);
    drainAudioFrontendVadEvents();
    samples_recorded += kFrameSamples20ms;

    const FrameSampleStats frame_stats = frame_sample_stats(frame, kFrameSamples20ms);
    if (frame_stats.signed_min < sec_signed_min) {
      sec_signed_min = frame_stats.signed_min;
    }
    if (frame_stats.signed_max > sec_signed_max) {
      sec_signed_max = frame_stats.signed_max;
    }
    sec_abs_sum += frame_stats.abs_sum;
    sec_samp_count += kFrameSamples20ms;

    const unsigned long now_sec = millis();
    if (now_sec - sec_stats_start_ms >= 10000) {
      const size_t samp_avg =
          sec_samp_count > 0 ? static_cast<size_t>(sec_abs_sum / sec_samp_count) : 0;
      log_info("[ASR_CHAT] round=%u 10s samp_min=%d samp_avg=%u samp_max=%d sent=%u",
               (unsigned)round_id_, (int)(sec_signed_min == INT16_MAX ? 0 : sec_signed_min),
               (unsigned)samp_avg, (int)(sec_signed_max == INT16_MIN ? 0 : sec_signed_max),
               (unsigned)samples_sent);
      sec_stats_start_ms = now_sec;
      sec_signed_min = INT16_MAX;
      sec_signed_max = INT16_MIN;
      sec_abs_sum = 0;
      sec_samp_count = 0;
    }

    loopLite();

    if (shouldSuppressMicUplink()) {
      round_scope.pump("listen");
      taskYIELD();
      continue;
    }

    bool audio_ok = false;
    for (int uplink_retry = 0; uplink_retry < 3; ++uplink_retry) {
      if (queueAudioOpusFrame(frame, kFrameSamples20ms)) {
        audio_ok = true;
        break;
      }
      loopLite();
      vTaskDelay(pdMS_TO_TICKS(10));
    }
    if (!audio_ok) {
      log_warn("[ASR_CHAT] send audio failed after retry (sent=%u)", (unsigned)samples_sent);
      resetUplinkBatch();
      noteTransportSendFail("AUDIO_UP_OPUS");
      if (!transportCanSend()) {
        record_aborted_disconnect = true;
        break;
      }
      round_scope.pump("uplink_fail");
      taskYIELD();
      continue;
    }
    samples_sent += kFrameSamples20ms;
    round_scope.pump("uplink");
    taskYIELD();
  }

  if (record_aborted_disconnect) {
    if (samples_sent > 0) {
      sendJson("{\"type\":\"audio_cancel\"}", /*critical=*/false);
    }
    discardPendingUplinkMedia();
    mic_capture_flush_queue();
    enhanceVoice_reset();
    log_warn("[ASR_CHAT] round=%u end (disconnect discard)", (unsigned)round_id_);
    return true;
  }

  if (samples_sent > 0) {
    if (!flushAudioOpusBatch()) {
      log_warn("[ASR_CHAT] flush audio batch failed");
    } else {
      /* This is only a local capture rollover.  Do not send protocol `flush`
       * and do not empty the mic queue: server VAD keeps the utterance
       * continuous across the old 30 s boundary. */
      log_warn("[ASR_CHAT] round=%u gapless rollover (uploaded %u samples)",
               (unsigned)round_id_, (unsigned)samples_sent);
    }
  } else {
    log_warn("[ASR_CHAT] round=%u end (no audio sent)", (unsigned)round_id_);
  }

  vad_gate_open_ = false;
  drainAudioFrontendVadEvents();
  voice_uplink_active_.store(false, std::memory_order_release);
  log_warn("[ASR_CHAT] round=%u end", (unsigned)round_id_);
  return true;
}

void AsrChatClient::onWirePayload(WirePayloadKind kind, uint8_t* payload,
                                  size_t length) {
  /*
   * DBOT v1 supplies individually complete, CRC-validated frames.  PB JSON
   * and binary media stay on PB_WIRE; one logical PB binary may span several
   * non-JSON frames and is reassembled to its declared next_bin_len.
   * CONTROL_JSON reaches this handler only for non-stream controls such as
   * pb_cancel.
   */
  if (kind == WirePayloadKind::kJson) {
    JsonDocument doc;
    if (deserializeJson(doc, payload, length)) {
      String raw((const char*)payload, length);
      log_info("[ASR_CHAT] text(raw): %s", raw.c_str());
      return;
    }
    String t = doc["type"].is<String>() ? doc["type"].as<String>() : String("");

    /* pb v2：优先处理 pb_*。 */
    if (t.startsWith("pb_")) {
      log_pb_rx_summary(doc);
      if (t == "pb_cancel") {
        String req = doc["req"].is<String>() ? doc["req"].as<String>() : String("");
        if (req.isEmpty() || (!pb_req_.isEmpty() && req == pb_req_)) {
          if (!pb_req_.isEmpty()) {
            pb_suppress_tail_req_ = pb_req_;
          }
          pbCancelTerminalTracks("server pb_cancel",
                                 req.isEmpty() ? nullptr : req.c_str());
          pbDrainWorkersForNewSequence(
              /*replace_display=*/true, /*replace_motor=*/true,
              /*replace_audio=*/true, /*cancel_terminals=*/false,
              /*preserve_display_baseline=*/true);
          pbDiscardDeferredJsonQueue();
          pbSignalTtsRoundComplete();
        }
        return;
      }
      if (pb_doc_is_servo_only_gesture(doc) &&
          !pbAnyDownlinkBinExpected()) {
        (void)pbParseAndStage(doc);
        return;
      }
      if (!pbDeferEnqueue(payload, length)) {
        log_warn("[PB] defer enqueue failed type=%s len=%u", t.c_str(), (unsigned)length);
        const String failed_req =
            doc["req"].is<String>() ? doc["req"].as<String>() : String("");
        const uint32_t failed_idx =
            doc["idx"].is<uint32_t>() ? doc["idx"].as<uint32_t>() : 0;
        if (!failed_req.isEmpty() && failed_req == pb_req_) {
          pbFailCurrentTerminal(failed_idx, "PB JSON defer enqueue failed");
        } else if (!failed_req.isEmpty()) {
          const bool durable_replay =
              doc["durable"].is<bool>() && doc["durable"].as<bool>();
          pbQueueTerminalAck(failed_req.c_str(), failed_idx,
                             PbSequenceOutcome::kFailed,
                             durable_replay);
        }
        if (!failed_req.isEmpty()) {
          pb_suppress_tail_req_ = failed_req;
        }
        pbDrainWorkersForNewSequence(
            /*replace_display=*/true, /*replace_motor=*/true,
            /*replace_audio=*/true, /*cancel_terminals=*/true,
            /*preserve_display_baseline=*/true);
        pbDiscardDeferredJsonQueue();
        pbSignalTtsRoundComplete();
      } else if (!pbAnyDownlinkBinExpected()) {
        flushDeferredPbJson(/*pump_after=*/false);
      }
      return;
    }
    log_warn("[ASR_CHAT] ignore unsupported control type=%s",
             t.isEmpty() ? "<missing>" : t.c_str());
    return;
  }
  {
    pb_last_bin_ms_ = millis();
    pb_last_bin_len_ = length;
    if (payload == nullptr && length > 0) {
      log_error("[PB] USB BIN payload is null but length=%u",
                (unsigned)length);
      return;
    }
    if (pb_rejected_discard_active_) {
      if (!pbRejectedDiscardExpectsBin()) {
        log_info("[PB] drop unframed rejected-chain BIN req=%s len=%u",
                 pb_rejected_discard_req_.c_str(), (unsigned)length);
        return;
      }
      const size_t expected =
          pb_rejected_discard_bin_lens_
              [pb_rejected_discard_bin_cursor_];
      if (length == 0 || expected == 0 ||
          pb_rejected_discard_bin_received_ > expected ||
          length > expected - pb_rejected_discard_bin_received_) {
        log_warn("[PB] rejected-chain BIN framing overflow req=%s fragment=%u "
                 "received=%u expected=%u; discard until next JSON",
                 pb_rejected_discard_req_.c_str(), (unsigned)length,
                 (unsigned)pb_rejected_discard_bin_received_,
                 (unsigned)expected);
        pb_rejected_discard_unframed_ = true;
        pb_rejected_discard_bin_count_ = 0;
        pb_rejected_discard_bin_cursor_ = 0;
        pb_rejected_discard_bin_received_ = 0;
        pb_rejected_discard_bin_since_ms_ = 0;
        return;
      }
      pb_rejected_discard_bin_received_ += length;
      pb_rejected_discard_bin_since_ms_ = millis();
      if (pb_rejected_discard_bin_received_ < expected) {
        log_info("[PB] discard rejected-chain BIN fragment req=%s %u/%u",
                 pb_rejected_discard_req_.c_str(),
                 (unsigned)pb_rejected_discard_bin_received_,
                 (unsigned)expected);
        return;
      }
      log_info("[PB] discarded rejected-chain logical BIN req=%s len=%u",
               pb_rejected_discard_req_.c_str(), (unsigned)expected);
      pbAdvanceRejectedDiscardBin();
      if (!pbRejectedDiscardExpectsBin()) {
        flushDeferredPbJson(/*pump_after=*/false);
      }
      return;
    }
    if (pbCompletedReplayExpectsBin()) {
      const size_t expected =
          pb_completed_replay_bin_lens_
              [pb_completed_replay_bin_cursor_];
      if (length == 0 || expected == 0 ||
          pb_completed_replay_bin_received_ > expected ||
          length > expected - pb_completed_replay_bin_received_) {
        log_error("[PB] completed replay BIN fragment overflow req=%s "
                  "fragment=%u received=%u expected=%u cursor=%u/%u",
                  pb_completed_replay_req_.c_str(), (unsigned)length,
                  (unsigned)pb_completed_replay_bin_received_,
                  (unsigned)expected,
                  (unsigned)pb_completed_replay_bin_cursor_,
                  (unsigned)pb_completed_replay_bin_count_);
        pbClearCompletedReplay("BIN fragment overflow");
        resetTransportSession("completed PB replay BIN fragment overflow");
        return;
      }
      pb_completed_replay_bin_received_ += length;
      pb_completed_replay_bin_since_ms_ = millis();
      if (pb_completed_replay_bin_received_ < expected) {
        log_info("[PB] drop completed replay BIN fragment req=%s "
                 "fragment=%u received=%u/%u cursor=%u/%u",
                 pb_completed_replay_req_.c_str(), (unsigned)length,
                 (unsigned)pb_completed_replay_bin_received_,
                 (unsigned)expected,
                 (unsigned)pb_completed_replay_bin_cursor_,
                 (unsigned)pb_completed_replay_bin_count_);
        return;
      }
      log_info("[PB] drop completed replay logical BIN req=%s len=%u cursor=%u/%u",
               pb_completed_replay_req_.c_str(), (unsigned)expected,
               (unsigned)(pb_completed_replay_bin_cursor_ + 1),
               (unsigned)pb_completed_replay_bin_count_);
      pbAdvanceCompletedReplayBin();
      if (!pbCompletedReplayExpectsBin()) {
        flushDeferredPbJson(/*pump_after=*/false);
      }
      return;
    }
    if (pb_active_ && pb_expect_bin_) {
      if (length == 0u) {
        log_error("[PB] BIN fragment reject: empty frame");
        pbProtocolError("binary fragment length empty");
        return;
      }
      if (pb_expect_bin_len_ == 0 ||
          pb_expect_bin_received_ > pb_expect_bin_len_ ||
          length > pb_expect_bin_len_ - pb_expect_bin_received_) {
        log_error("[PB] BIN fragment overflow: fragment=%u received=%u "
                  "expect=%u kind=%u req=%s idx=%u",
                  (unsigned)length, (unsigned)pb_expect_bin_received_,
                  (unsigned)pb_expect_bin_len_,
                  (unsigned)pb_expect_bin_kind_, pb_req_.c_str(),
                  (unsigned)pb_pending_idx_);
        pbProtocolError("binary fragment exceeds declared length");
        return;
      }

      if (pb_expect_bin_buf_ == nullptr) {
        pb_expect_bin_free_caps_ =
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT;
        pb_expect_bin_buf_ = static_cast<uint8_t*>(heap_caps_malloc(
            pb_expect_bin_len_, pb_expect_bin_free_caps_));
        if (pb_expect_bin_buf_ == nullptr) {
          pb_expect_bin_free_caps_ = MALLOC_CAP_DEFAULT;
          pb_expect_bin_buf_ = static_cast<uint8_t*>(heap_caps_malloc(
              pb_expect_bin_len_, pb_expect_bin_free_caps_));
        }
        if (pb_expect_bin_buf_ == nullptr) {
          pb_expect_bin_free_caps_ = 0;
          pbProtocolError("logical binary allocation failed");
          return;
        }
      }
      memcpy(pb_expect_bin_buf_ + pb_expect_bin_received_, payload, length);
      pb_expect_bin_received_ += length;
      pb_expect_bin_since_ms_ = millis();
      if (pb_expect_bin_received_ < pb_expect_bin_len_) {
        log_info("[PB] BIN fragment req=%s idx=%u len=%u received=%u/%u",
                 pb_req_.c_str(), (unsigned)pb_pending_idx_,
                 (unsigned)length, (unsigned)pb_expect_bin_received_,
                 (unsigned)pb_expect_bin_len_);
        return;
      }

      uint8_t* logical_payload = pb_expect_bin_buf_;
      const size_t logical_length = pb_expect_bin_received_;
      const uint32_t logical_free_caps = pb_expect_bin_free_caps_;
      pb_expect_bin_buf_ = nullptr;
      pb_expect_bin_received_ = 0;
      pb_expect_bin_free_caps_ = 0;

      const bool closing_pb_end_bin = pb_end_waiting_bin_;
      const uint8_t ch_for_stream_end = pb_ch_;
      const uint32_t pending_idx_snap = pb_pending_idx_;
      const PbBinKind bin_kind = pb_expect_bin_kind_;

      if (bin_kind == PbBinKind::kPcm) {
        if (pb_sr_ == 0 || pb_ch_ == 0 || pb_fmt_.isEmpty()) {
          heap_caps_free(logical_payload);
          pbProtocolError("binary without valid audio params");
          return;
        }
        if (pb_fmt_ != "s16le" && pb_fmt_ != "opus") {
          heap_caps_free(logical_payload);
          pbProtocolError("unsupported audio fmt");
          return;
        }
        if (pb_fmt_ == "s16le" && (logical_length & 1u) != 0u) {
          heap_caps_free(logical_payload);
          pbProtocolError("PCM binary length not even");
          return;
        }
        if (pb_pending_pcm_owned_ != nullptr) {
          heap_caps_free(logical_payload);
          pbProtocolError("multiple PCM binaries in one PB chunk");
          return;
        }
        if (pb_bins_rx_count_ < 255) {
          pb_bins_rx_count_++;
        }
        pb_pcm_bytes_rx_total_ += logical_length;
        int16_t* pcm_owned = nullptr;
        size_t samples = 0;
        uint32_t free_caps = 0;
        if (pb_fmt_ == "opus") {
          if (!opus_downlink_decode(logical_payload, logical_length,
                                    (int)pb_sr_, pb_expect_opus_frames_,
                                    &pcm_owned, &samples, &free_caps)) {
            heap_caps_free(logical_payload);
            pbProtocolError("opus decode failed");
            return;
          }
          heap_caps_free(logical_payload);
          logical_payload = nullptr;
        } else {
          pcm_owned = reinterpret_cast<int16_t*>(logical_payload);
          samples = logical_length / 2;
          free_caps = logical_free_caps;
          logical_payload = nullptr;
        }
        /*
         * Do not enqueue audio yet: assets declared after this PCM still
         * belong to the same chunk.  pbFinishChunkBins() releases audio,
         * display and motor together against one absolute start timestamp.
         */
        pb_pending_pcm_owned_ = pcm_owned;
        pb_pending_pcm_samples_ = samples;
        pb_pending_pcm_free_caps_ = free_caps;
      } else {
        if (pb_asset_count_ >= kPbMaxAssetsPerChunk) {
          heap_caps_free(logical_payload);
          pbProtocolError("too many asset binaries");
          return;
        }
        pb_asset_bufs_[pb_asset_count_] = logical_payload;
        pb_asset_lens_[pb_asset_count_] = logical_length;
        logical_payload = nullptr;
        pb_asset_count_++;
      }

      pbAdvanceBinQueue();
      if (pb_expect_bin_) {
        flushDeferredPbJson(/*pump_after=*/false);
        return;
      }

      (void)pbFinishChunkBins(pending_idx_snap, closing_pb_end_bin,
                              ch_for_stream_end);
      return;
    }

    if (!pb_suppress_tail_req_.isEmpty()) {
      log_info("[PB] BIN drop stale tail len=%u", (unsigned)length);
    } else if (pb_active_) {
      log_warn("[PB] BIN ignore: pb_active expect_bin=%d expect_len=%u actual_len=%u "
               "(expected JSON preamble before next BIN)",
               (int)pb_expect_bin_, (unsigned)pb_expect_bin_len_, (unsigned)length);
      pbProtocolError("unexpected BIN (expect JSON per pb v2)");
    } else {
      log_warn("[ASR_CHAT] drop unexpected BIN len=%u", (unsigned)length);
    }
    return;
  }
}
