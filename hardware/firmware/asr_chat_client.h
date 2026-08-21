#ifndef ASR_CHAT_CLIENT_H
#define ASR_CHAT_CLIENT_H

#include <Arduino.h>
#include <ArduinoJson.h>
#include <atomic>
#include "audio_player.h"
#include "common.h"
#include "deskbot_config.h"
#include "pb_completed_store.h"
#include "usb_transport.h"

class AsrChatClient {
public:
  AsrChatClient();

  bool connect();
  bool isReady() const;
  bool runVoiceRound(uint16_t max_record_seconds = 10);

  /** TTS/pb 半双工窗口（含 anim-only pb）；mic 抑制与旧 camera 逻辑共用。 */
  /** 仅 Opus 上行或 TTS PCM 下行时暂停 camera；anim-only pb 仍可上传 JPEG。 */
  bool isCameraUplinkPaused() const;
  /** 扬声器 I2S 正在输出或播放队列有待播 PCM（不含 stream begin 空窗）。 */
  bool isSpeaking() const;
  /** TTS/I2S DMA 尾音抑制窗口内（无 AEC 时防回声误触发 VAD）。 */
  bool isMicTailSuppressed() const;
  /** VAD 已触发、本轮语音上行窗口内（含已发/待发 Opus）。 */
  bool isVadGateOpen() const;
  /** 录音环或 Opus 上行活跃：相机降低帧率，让音频优先使用 USB。 */
  bool isVoiceUplinkBusy() const;

  /** 主循环泵 USB/PB（相机上行见独立 camera 任务）。 */
  void serviceLoop();

  /** Reset local state before usb_transport_begin(). */
  void enableUsbTransport();
  /** Link callback: makes all PB/audio state session-epoch aware. */
  void onUsbLinkState(bool ready, uint32_t session_epoch);
  /** Route one validated USB frame into the existing PB/Opus state machines. */
  void dispatchUsbFrame(const DeskbotUsbRxFrame& frame);

  /** Track attention state without generating automatic servo motion. */
  void updateAttentionDisplay();

private:
  static constexpr size_t kFrameSamples20ms = 320;  // 16kHz * 0.02s
  /** 上行 Opus batch：3×20ms=60ms 一 binary，binary 为 uint16_be+opus 重复。
   * 取舍：5 帧(100ms)攒批每包省一次 USB 帧开销，但给 ASR 首字延迟固定 +100ms；
   * 3 帧(60ms)把攒批延迟降到 60ms，USB 帧频率 10/s→~17/s，CDC 带宽仍富余。
   * PC 侧按 length-prefix 自适应解析帧数（_normalize_opus_uplink_batch /
   * decode_opus_uplink），无“每批 5 帧/100ms”硬假设，可安全调整。 */
  static constexpr size_t kUplinkBatchFrames = 3;
  static constexpr size_t kUplinkBatchMaxBin = kUplinkBatchFrames * (2 + 256);
  static constexpr size_t kUsbAudioDownBatchMaxFrames = 5;
  /* Host fragments oversized PB JSON so no physical USB frame can overflow
   * HWCDC's bounded receive queue.  Reassembly is capped independently from
   * binary media to keep malformed peers from exhausting PSRAM. */
  static constexpr size_t kUsbPbJsonMaxBytes = 64u * 1024u;

  uint32_t usb_session_epoch_ = 0;
  uint8_t* usb_pb_json_buf_ = nullptr;
  size_t usb_pb_json_len_ = 0;
  size_t usb_pb_json_capacity_ = 0;
  uint32_t usb_pb_json_epoch_ = 0;
  bool usb_pb_json_active_ = false;
  bool ready_ = false;
  /** 上电后首次收到 server ready 时置 true，重连不再重置，用于触发 boot_connect 上报。 */
  bool boot_connect_sent_ = false;
  /* 本轮下行收尾标志：pbSignalTtsRoundComplete() 触发，等价于「pb 序列已完整结束」。 */
  bool reply_done_ = false;
  /* tts_active_：pb_start 置 true、pb 序列收尾 / pbSignalTtsRoundComplete 置 false。
   * 半双工抑制、摄像头暂停、attention display 共用此窗口。 */
  bool tts_active_ = false;
  /** 连续发送失败次数；达阈值则使当前 USB session 失效。 */
  uint8_t transport_send_fail_streak_ = 0;
  static constexpr uint8_t kTransportSendFailResetThreshold = 3;
  uint32_t round_id_ = 0;
  /* USB session 在本轮内失效时置位，用于立即结束录音。 */
  bool link_abort_round_ = false;
  /* 本轮已开始向服务端发送 Opus：与 camera_frame 互斥。 */
  std::atomic<bool> voice_uplink_active_{false};
  std::atomic<unsigned long> last_voice_uplink_activity_ms_{0};
  /** VAD 触发后的上行窗口：至本轮 flush/skip/abort 结束。 */
  bool vad_gate_open_ = false;
  /** runVoiceRound 录音环内标记。 */
  bool in_voice_record_loop_ = false;
  uint8_t uplink_batch_bin_[kUplinkBatchMaxBin];
  size_t uplink_batch_bin_len_ = 0;
  uint8_t uplink_batch_count_ = 0;
  bool capture_was_allowed_ = false;
  uint32_t uplink_codec_generation_ = UINT32_MAX;
  bool boot_connect_pending_ = false;

  /* -----------------------------------------------------------------------
   * pb v2 下行播放序列（JSON + 紧随 binary PCM）：
   * - 维护当前 req、idx、期望下一份 logical binary、queue_level 与在途序列计数。
   * - logical binary 可由多个 PB_WIRE 非 JSON transport frame 组成；按
   *   next_bin_len 精确重组后才交给 audio/display worker。
   * - pb_start / pb_single（链首）按 level + action（replace|append|default）做入队决策（§2.1/§2.2）。
   * - append 跨 req 不清 worker 队列，分片顺序入队；高 level 或 replace 同级时 drain+reset。
   * - v1 action=opportunistic 降级为 level=0 + append。
   * ----------------------------------------------------------------------- */
  enum class PbEnqueueAction : uint8_t { kReplace = 0, kAppend = 1, kDefault = 2 };
  enum class PbQueueDecision : uint8_t { kDrop = 0, kClear = 1, kAppend = 2 };
  PbQueueDecision pbDecideChainHead(int8_t level, PbEnqueueAction action) const;
  int pbCountHigherPrioritySeqs(int8_t level) const;
  void pbSeqTrackPush(int8_t level);
  void pbSeqTrackPop();
  /** Normal replace cancels only lanes actually present in the new request. */
  void pbDrainWorkersForNewSequence(bool replace_display,
                                    bool replace_motor,
                                    bool replace_audio,
                                    bool cancel_terminals = true,
                                    bool preserve_display_baseline = false);
  void pbOnSequenceComplete();
  PbEnqueueAction pb_pending_enqueue_action_ = PbEnqueueAction::kReplace;
  int8_t pb_queue_level_ = -1;
  uint8_t pb_inflight_seq_count_ = 0;
  static constexpr size_t kPbMaxTrackedSeqLevels = 16;
  int8_t pb_seq_levels_[kPbMaxTrackedSeqLevels]{};
  size_t pb_seq_level_count_ = 0;

  enum class PbModality : uint8_t {
    kAudio = 0,
    kDisplay = 1,
    kMotor = 2,
  };
  enum class PbWorkerOutcome : uint8_t {
    kCompleted = 0,
    kFailed = 1,
    kCancelled = 2,
  };
  enum class PbSequenceOutcome : uint8_t {
    kPending = 0,
    kPlayed = 1,
    kFailed = 2,
    kCancelled = 3,
  };
  struct PbTerminalTrack {
    bool used = false;
    uint32_t epoch = 0;
    String req;
    /** Lease origin/deadline use rollover-safe millis() arithmetic. */
    uint32_t created_ms = 0;
    uint32_t lease_deadline_ms = 0;
    uint32_t last_idx = 0;
    uint32_t final_idx = 0;
    bool sealed = false;
    bool audio_expected = false;
    bool audio_completed = false;
    uint32_t display_expected = 0;
    uint32_t display_completed = 0;
    bool display_crc_valid = false;
    uint32_t display_crc_idx = 0;
    uint32_t display_crc32 = 0;
    uint32_t motor_expected = 0;
    uint32_t motor_completed = 0;
    /** Last commanded pose emitted by this track's motor actor events. */
    bool motor_pose_valid = false;
    uint32_t motor_pose_idx = 0;
    int16_t motor_commanded_x = 0;
    int16_t motor_commanded_y = 0;
    /** Only explicit durable operations consume the flash replay window. */
    bool durable = false;
    /** One monotonic wall-clock origin shared by audio/display/motor. */
    uint32_t timeline_start_ms = 0;
    uint32_t timeline_next_offset_ms = 0;
    PbSequenceOutcome outcome = PbSequenceOutcome::kPending;
  };
  static constexpr size_t kPbTerminalTrackCount = 24;
  /**
   * A new tracker may legitimately wait for a large logical BIN for up to
   * 30 s. Once work is on the shared timeline, the lease follows the
   * predicted finish plus a bounded worker grace period. No request may
   * retain a tracker beyond the hard lifetime.
   */
  static constexpr uint32_t kPbTerminalIngressLeaseMs = 35000u;
  static constexpr uint32_t kPbTerminalProgressLeaseMs = 15000u;
  static constexpr uint32_t kPbTerminalWorkerGraceMs = 10000u;
  static constexpr uint32_t kPbTerminalHardLeaseMs =
      35u * 60u * 1000u;
  PbTerminalTrack pb_terminal_tracks_[kPbTerminalTrackCount]{};
  uint32_t pb_terminal_epoch_counter_ = 0;
  uint32_t pb_current_epoch_ = 0;
  uint32_t pb_audio_terminal_drop_seen_ = 0;
  uint32_t pb_display_terminal_drop_seen_ = 0;
  uint32_t pb_motor_terminal_drop_seen_ = 0;

  struct PbTerminalAck {
    String req;
    uint32_t idx = 0;
    PbSequenceOutcome outcome = PbSequenceOutcome::kPending;
    int16_t commanded_x = 0;
    int16_t commanded_y = 0;
    bool display_crc_valid = false;
    uint32_t display_crc32 = 0;
  };
  static constexpr uint8_t kPbTerminalAckQueueDepth = 32;
  PbTerminalAck pb_terminal_ack_queue_[kPbTerminalAckQueueDepth]{};
  uint8_t pb_terminal_ack_head_ = 0;
  uint8_t pb_terminal_ack_tail_ = 0;

  struct PbCompletedReq {
    bool used = false;
    bool persisted = false;
    String req;
    uint32_t idx = 0;
    PbSequenceOutcome outcome = PbSequenceOutcome::kPending;
    bool display_crc_valid = false;
    uint32_t display_crc32 = 0;
  };
  static constexpr size_t kPbCompletedReqCacheDepth =
      DESKBOT_PB_COMPLETED_STORE_DEPTH;
  /** Durable RAM mirror; its cursor tracks the bounded NVS ring. */
  PbCompletedReq pb_completed_req_cache_[kPbCompletedReqCacheDepth]{};
  uint8_t pb_completed_req_cache_cursor_ = 0;
  /**
   * Ordinary chat has an independent RAM-only ring so high-frequency turns
   * cannot evict durable reminder/control receipts from the NVS mirror.
   */
  PbCompletedReq pb_volatile_completed_req_cache_[
      kPbCompletedReqCacheDepth]{};
  uint8_t pb_volatile_completed_req_cache_cursor_ = 0;
  bool pb_completed_req_cache_loaded_ = false;

  PbTerminalTrack* pbFindTerminalTrack(uint32_t epoch);
  bool pbEnsureCompletedReqCacheLoaded();
  const PbCompletedReq* pbFindCompletedReq(const String& req);
  bool pbRememberCompletedReq(const char* req, uint32_t idx,
                              PbSequenceOutcome outcome,
                              bool persist,
                              bool display_crc_valid = false,
                              uint32_t display_crc32 = 0);
  bool pbPersistCompletedReq(const char* req);
  bool pbBeginTerminalTrack(const String& req, uint32_t idx,
                            bool durable);
  void pbRefreshTerminalLease(PbTerminalTrack& track,
                              uint32_t expected_finish_ms = 0);
  void pbNoteModalitySubmitted(PbModality modality);
  void pbSealCurrentTerminalTrack(uint32_t final_idx);
  void pbEvaluateTerminalTrack(PbTerminalTrack& track);
  void pbHandleWorkerTerminal(uint32_t epoch, const char* req, uint32_t idx,
                              PbModality modality, PbWorkerOutcome outcome,
                              bool pose_valid = false, int pose_x = 0,
                              int pose_y = 0,
                              bool display_crc_valid = false,
                              uint32_t display_crc32 = 0);
  void pbFailTracksWaitingForModality(PbModality modality,
                                      uint32_t dropped_count);
  void pbDrainWorkerTerminals();
  void pbExpireTerminalTracks();
  void pbFailCurrentTerminal(uint32_t idx, const char* why);
  void pbCancelTerminalTracks(const char* why, const char* req = nullptr);
  void pbCancelTerminalTracksForLanes(bool display, bool motor, bool audio,
                                      const char* why);
  bool pbHasPendingTerminalTracks() const;
  bool pbHasPendingAudioTerminalTracks() const;
  void pbQueueTerminalAck(const char* req, uint32_t idx,
                          PbSequenceOutcome outcome,
                           bool persist = false, bool pose_valid = false,
                           int pose_x = 0, int pose_y = 0,
                           bool display_crc_valid = false,
                           uint32_t display_crc32 = 0);
  bool pbPopTerminalAck(PbTerminalAck* out);
  uint32_t pbArmCurrentChunkTimeline(uint32_t idx, uint32_t chunk_ms);

  bool pb_active_ = false;
  String pb_req_;
  /* 断线 / pbReset 后服务端仍可能送达同一 req 的 pb_chunk+BIN；若已 pbReset 则 next_idx
   * 归零，会误触发 idx not monotonic。记录被中止的 req：丢弃尾帧，直至同 req 的 pb_start|pb_single
   * idx==0（合法新流）或收到不同 req。 */
  String pb_suppress_tail_req_;
  uint32_t pb_next_idx_ = 0;
  bool pb_expect_bin_ = false;
  size_t pb_expect_bin_len_ = 0;
  /** 进入 expect_bin 或收到最近一个 fragment 的时刻（idle timeout）。 */
  unsigned long pb_expect_bin_since_ms_ = 0;
  /** 最近一次 PB_WIRE binary fragment 的长度与接收时刻。 */
  size_t pb_last_bin_len_ = 0;
  unsigned long pb_last_bin_ms_ = 0;
  /** 当前 logical binary 的跨 frame 重组缓冲。 */
  uint8_t* pb_expect_bin_buf_ = nullptr;
  size_t pb_expect_bin_received_ = 0;
  uint32_t pb_expect_bin_free_caps_ = 0;
  void pbFreeExpectedBin();
  uint32_t pb_sr_ = 0;
  uint8_t pb_ch_ = 0;
  String pb_fmt_;
  uint32_t pb_pending_idx_ = 0;
  uint32_t pb_pending_chunk_ms_ = 0;
  bool pb_pending_mouth_only_ = false;
  bool pb_sequence_mouth_only_ = false;
  char* pb_pending_anim_buf_ = nullptr;
  size_t pb_pending_anim_len_ = 0;
  void pbFreePendingAnim();
  struct PbServoSeg {
    int xm = 2;
    int ym = 2;
    int x = 0;
    int y = 0;
    int x_min = 10;
    int x_max = 170;
    int y_min = 70;
    int y_max = 110;
    uint32_t ms = 0;
  };
  static constexpr size_t kPbMaxServoSegsPerChunk = 32;
  struct PbChunkPreflight {
    uint32_t idx = 0;
    bool durable = false;
    bool is_chain_head = false;
    bool voice_mouth = false;
    bool mouth_only = false;
    PbEnqueueAction action = PbEnqueueAction::kReplace;
    int8_t level = 1;
    uint32_t chunk_ms = 0;
    size_t servo_count = 0;
    PbServoSeg servo_segs[kPbMaxServoSegsPerChunk]{};
  };
  PbServoSeg pb_pending_servo_segs_[kPbMaxServoSegsPerChunk]{};
  size_t pb_pending_servo_seg_count_ = 0;
  /** pb JSON 中 volume 字段（0–100）换算的播放音量比例；省略时保持上次值（初始为编译期默认）。 */
  float pb_volume_ratio_ = DESKBOT_AUDIO_PLAY_VOLUME;
  bool pb_audio_stream_started_ = false;
  int16_t* pb_pending_pcm_owned_ = nullptr;
  size_t pb_pending_pcm_samples_ = 0;
  uint32_t pb_pending_pcm_free_caps_ = 0;
  void pbFreePendingPcm();
  bool pbSubmitPendingAudio(uint32_t chunk_idx, uint32_t start_at_ms);
  unsigned long pb_last_buf_decay_ms_ = 0;
  int32_t pb_audio_buf_ms_est_ = 0;
  uint32_t pb_last_ack_idx_ = 0;
  /** 本轮 req 已成功入队的 PCM BIN 包数 / 累计字节（供与服务端对照是否收全）。 */
  uint8_t pb_bins_rx_count_ = 0;
  size_t pb_pcm_bytes_rx_total_ = 0;
  bool pb_end_waiting_bin_ = false;
  uint32_t pb_end_idx_ = 0;

  enum class PbBinKind : uint8_t { kPcm = 0, kAsset = 1 };
  static constexpr uint8_t kPbMaxAssetsPerChunk = 8;
  static constexpr uint8_t kPbMaxBinsPerChunk = 1 + kPbMaxAssetsPerChunk;
  uint8_t pb_pending_bin_count_ = 0;
  uint8_t pb_pending_bin_cursor_ = 0;
  PbBinKind pb_pending_bin_kinds_[kPbMaxBinsPerChunk]{};
  size_t pb_pending_bin_lens_[kPbMaxBinsPerChunk]{};
  uint16_t pb_pending_bin_frames_[kPbMaxBinsPerChunk]{};
  PbBinKind pb_expect_bin_kind_ = PbBinKind::kPcm;
  uint16_t pb_expect_opus_frames_ = 0;
  uint8_t* pb_asset_bufs_[kPbMaxAssetsPerChunk]{};
  size_t pb_asset_lens_[kPbMaxAssetsPerChunk]{};
  uint8_t pb_asset_count_ = 0;

  /* Durable bounded idempotency window for service/device restarts after
   * playback.  A cached terminal req is ACKed again while its JSON/BIN
   * frames are consumed without submitting audio, display, or motor work. */
  bool pb_completed_replay_active_ = false;
  String pb_completed_replay_req_;
  PbSequenceOutcome pb_completed_replay_outcome_ =
      PbSequenceOutcome::kPending;
  uint32_t pb_completed_replay_idx_ = 0;
  bool pb_completed_replay_end_after_bins_ = false;
  size_t pb_completed_replay_bin_lens_[kPbMaxBinsPerChunk]{};
  uint8_t pb_completed_replay_bin_count_ = 0;
  uint8_t pb_completed_replay_bin_cursor_ = 0;
  size_t pb_completed_replay_bin_received_ = 0;
  unsigned long pb_completed_replay_bin_since_ms_ = 0;

  /** Independent bounded framing sink for a rejected multi-frame chain.
   * It consumes that chain's declared BIN fragments without touching the
   * still-running PB request or any worker epoch. */
  bool pb_rejected_discard_active_ = false;
  bool pb_rejected_discard_unframed_ = false;
  bool pb_rejected_discard_end_after_bins_ = false;
  String pb_rejected_discard_req_;
  size_t pb_rejected_discard_bin_lens_[kPbMaxBinsPerChunk]{};
  uint8_t pb_rejected_discard_bin_count_ = 0;
  uint8_t pb_rejected_discard_bin_cursor_ = 0;
  size_t pb_rejected_discard_bin_received_ = 0;
  unsigned long pb_rejected_discard_bin_since_ms_ = 0;

  bool pbCompletedReplayExpectsBin() const {
    return pb_completed_replay_active_ &&
           pb_completed_replay_bin_cursor_ <
               pb_completed_replay_bin_count_;
  }
  bool pbRejectedDiscardExpectsBin() const {
    return pb_rejected_discard_active_ &&
           pb_rejected_discard_bin_cursor_ <
               pb_rejected_discard_bin_count_;
  }
  bool pbAnyDownlinkBinExpected() const {
    return pb_expect_bin_ || pbCompletedReplayExpectsBin() ||
           pbRejectedDiscardExpectsBin();
  }
  bool pbStageCompletedReplay(const JsonDocument& doc,
                              const PbCompletedReq& completed);
  void pbAdvanceCompletedReplayBin();
  void pbClearCompletedReplay(const char* why);
  bool pbStageRejectedDiscard(const JsonDocument& doc, const String& req);
  void pbAdvanceRejectedDiscardBin();
  void pbClearRejectedDiscard(const char* why);

  void pbFreePendingAssets();
  void pbBuildPendingBinQueue(const JsonDocument& doc);
  void pbAdvanceBinQueue();
  bool pbFinishChunkBins(uint32_t pending_idx_snap, bool closing_pb_end_bin,
                         uint8_t ch_for_stream_end);
  /* PB 最后一包不在 USB RX 回调内同步收尾，避免阻塞心跳处理。 */

  /* pb_ack 只入队，统一在 loop() 发送。 */
  bool pb_ack_out_pending_ = false;
  String pb_ack_out_req_;
  uint32_t pb_ack_out_idx_ = 0;
  int32_t pb_ack_out_buf_ms_ = 0;
  String pb_ack_out_phase_ = "accepted";
  int16_t pb_ack_out_commanded_x_ = 0;
  int16_t pb_ack_out_commanded_y_ = 0;
  bool pb_ack_out_display_crc_valid_ = false;
  uint32_t pb_ack_out_display_crc32_ = 0;
  /** 纯音频 pb_ack 节流（ms）；舵机完成 ack 走 pb_ack_bypass_throttle_ 立即发。 */
  unsigned long pb_last_pb_ack_sent_wall_ms_ = 0;
  bool pb_ack_bypass_throttle_ = false;

  /* Logical attention state only; it never owns the servos. */
  enum DisplayState : uint8_t {
    DISPLAY_UNINIT = 0,
    DISPLAY_WAKEUP = 1,
    DISPLAY_SLEEP = 2,
  };
  DisplayState display_state_ = DISPLAY_UNINIT;
  /* Debounce logical wake/idle state changes without moving the head. */
  unsigned long last_should_wake_ms_ = 0;
  static constexpr unsigned long kIdleEnterDelayMs = 2000;

  enum class WirePayloadKind : uint8_t {
    kJson = 0,
    kBinary = 1,
  };
  void resetUsbPbJsonAssembly();
  bool appendUsbPbJsonFragment(const uint8_t* payload, size_t length);
  void onWirePayload(WirePayloadKind kind, uint8_t* payload, size_t length);
  void engageVoiceUplink();
  void resetUplinkBatch();
  bool queueAudioOpusFrame(const int16_t* pcm, size_t samples);
  bool flushAudioOpusBatch();
  void drainAudioFrontendVadEvents();
  void discardPendingUplinkMedia();
  bool sendJson(const char* msg, bool critical = true);
  bool sendJson(const String& msg, bool critical = true);
  /** 发送失败或 session 污染：使当前 USB session 失效并等待新 hello。 */
  void resetTransportSession(const char* why);
  /** 幂等清理当前 transport 的 PB、音频和录音状态。 */
  void abortRuntimeState(const char* why);
  void maybeSendBootConnect();
  /** 可发送：hello 已完成、epoch 一致且 transport 仍允许上行。 */
  bool transportCanSend();
  void noteTransportSendOk();
  void noteTransportSendFail(const char* what);
  /** loop() 内：断线或僵尸连接时按 backoff 自动 connect()。 */

  /* 无 AEC：仅喇叭播音/播放队列/I2S DMA 尾音窗口内不上行真实 mic（录音环仍读麦排空队列）。 */
  bool shouldSuppressMicUplink();

  /** 播音结束后 I2S 尾音抑制见 deskbot_uplink_state。 */

  void pbReset(bool stop_audio, bool preserve_pending_terminals = false);
  void pbProtocolError(const char* why);
  bool pbPreflightChunk(const JsonDocument& doc, bool completed_replay,
                        PbChunkPreflight* out, const char** why) const;
  void pbCommitServoPreflight(const PbChunkPreflight& preflight);
  bool pbApplyServoArrayIfAny(uint32_t chunk_idx, uint32_t start_at_ms,
                              bool* submitted_any = nullptr);
  bool pbSubmitAnimIfAny(uint32_t chunk_idx, uint32_t start_at_ms);
  void pbUpdateAudioBufDecayWall();
  void pbMaybeAck(uint32_t idx);
  void flushPendingPbAck();
  /* pb 序列播完（或仅动画无流）时置本轮下行完成，解除 runVoiceRound 等待。 */
  void pbSignalTtsRoundComplete();
  bool pbParseAndStage(const JsonDocument& doc);
  /** 录音环和长操作使用的轻量 USB/PB pump。 */
  void loopLite();
  void pbTickExpectBinTimeout();
  static constexpr size_t kPbDeferMaxBytes = 65536;
  static constexpr uint8_t kPbDeferQueueDepth = 16;
  uint8_t* pb_defer_bufs_[kPbDeferQueueDepth]{};
  size_t pb_defer_lens_[kPbDeferQueueDepth]{};
  uint8_t pb_defer_head_ = 0;
  uint8_t pb_defer_tail_ = 0;
  bool pbDeferEnqueue(const uint8_t* payload, size_t length);
  uint8_t pbDeferQueueDepth() const;
  void flushDeferredPbJson(bool pump_after = true);
  void pbDiscardDeferredJsonQueue();
  /* audio.next_bin_len：pb_ack 仅在 loop() 发送；舵机 async 不再阻塞 ack（与音频解耦）。 */
  bool pbDispatchChunkPreamble(uint32_t chunk_idx, uint32_t start_at_ms);
};

bool asr_chat_voice_uplink_busy(void);

#endif
