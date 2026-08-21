"""Static PB-v2 terminal and replay-safety invariants.

The ESP32 build is a separate CI gate.  These focused checks keep the
cross-worker completion contract and wire-framing rules visible in the normal
service pytest job as well.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FW = ROOT / "hardware" / "firmware"


def _read(name: str) -> str:
    return (FW / name).read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    return source[start_at : source.index(end, start_at)]


def test_pb_worker_queues_are_strict_and_do_not_evict_existing_pb_work():
    audio = _between(
        _read("audio_player.cpp"),
        "static bool enqueue_audio_play_job_strict",
        "static void drain_audio_play_queue",
    )
    assert "xQueueSend(s_audio_play_q, &j, 0)" in audio
    assert "xQueueReceive" not in audio
    assert "return false;" in audio

    display = _between(
        _read("display.cpp"),
        "bool display_pb_submit_vector_json_owned",
        "bool display_take_pb_terminal_event",
    )
    assert "xQueueSend(s_queue, &req, 0)" in display
    assert "xQueueReceive" not in display
    assert "return false;" in display

    motor = _between(
        _read("head.cpp"),
        "bool head_servo_cmd_pb_batch_async",
        "bool head_take_pb_terminal_event",
    )
    assert "enqueue_motor_batch_strict(staged, command_count)" in motor
    assert "xQueueReceive" not in motor
    assert "return false;" in motor


def test_any_modality_enqueue_failure_makes_the_sequence_terminal_failed():
    asr = _read("asr_chat_client.cpp")

    fail_current = _between(
        asr,
        "void AsrChatClient::pbFailCurrentTerminal",
        "void AsrChatClient::pbCancelTerminalTracks",
    )
    assert "track->outcome = PbSequenceOutcome::kFailed" in fail_current
    assert "pbQueueTerminalAck(" in fail_current
    assert "PbSequenceOutcome::kFailed" in fail_current
    assert "lease_deadline_ms" not in fail_current

    display_submit = _between(
        asr,
        "bool AsrChatClient::pbSubmitAnimIfAny",
        "bool AsrChatClient::pbPreflightChunk",
    )
    assert 'pbFailCurrentTerminal(chunk_idx, "display enqueue failed")' in display_submit

    motor_submit = _between(
        asr,
        "bool AsrChatClient::pbApplyServoArrayIfAny",
        "void AsrChatClient::pbUpdateAudioBufDecayWall",
    )
    assert 'pbFailCurrentTerminal(chunk_idx, "motor batch enqueue failed")' in motor_submit

    audio_submit = _between(
        asr,
        "bool AsrChatClient::pbSubmitPendingAudio",
        "void AsrChatClient::pbBuildPendingBinQueue",
    )
    assert 'pbFailCurrentTerminal(chunk_idx, "audio stream begin failed")' in audio_submit
    assert 'pbFailCurrentTerminal(chunk_idx, "pcm push failed")' in audio_submit

    binary = asr[asr.index("pb_last_bin_ms_ = millis()") :]
    assert "pbFinishChunkBins(" in binary
    protocol_error = _between(
        asr,
        "void AsrChatClient::pbProtocolError",
        "bool AsrChatClient::pbSubmitAnimIfAny",
    )
    assert "pbFailCurrentTerminal(" in protocol_error
    assert 'pbFailCurrentTerminal(pending_idx_snap, "audio end enqueue failed")' in asr


def test_display_overlay_capabilities_are_strict_chain_head_contracts():
    asr = _read("asr_chat_client.cpp")
    header = _read("asr_chat_client.h")
    preflight = _between(
        asr,
        "bool AsrChatClient::pbPreflightChunk",
        "void AsrChatClient::pbCommitServoPreflight",
    )
    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )

    assert 'reject("voice_mouth must be bool")' in preflight
    assert 'reject("mouth_only must be bool")' in preflight
    assert 'reject("voice_mouth/mouth_only are chain-head fields")' in preflight
    assert 'reject("voice_mouth and mouth_only are mutually exclusive")' in preflight
    assert "bool voice_mouth = false" in header
    assert "bool mouth_only = false" in header
    assert "display_voice_mouth_set_allowed(preflight.voice_mouth)" in parse
    assert "pb_sequence_mouth_only_ = preflight.mouth_only" in parse


def test_played_waits_for_seal_and_every_submitted_modality():
    asr = _read("asr_chat_client.cpp")
    barrier = _between(
        asr,
        "void AsrChatClient::pbEvaluateTerminalTrack",
        "void AsrChatClient::pbSealCurrentTerminalTrack",
    )
    assert "!track.sealed" in barrier
    assert "!track.audio_expected || track.audio_completed" in barrier
    assert "track.display_completed == track.display_expected" in barrier
    assert "track.motor_completed == track.motor_expected" in barrier
    assert barrier.index("if (!audio_done || !display_done || !motor_done)") < barrier.index(
        "PbSequenceOutcome::kPlayed"
    )


def test_r0_audio_free_anim_servo_uses_workers_and_reaches_played():
    """JSON-only gesture PB is a first-class R0 payload, not an audio special case."""
    asr = _read("asr_chat_client.cpp")
    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    assert "(pb_pending_anim_buf_ != nullptr && pb_pending_anim_len_ > 0)" in parse
    assert "(pb_pending_servo_seg_count_ > 0)" in parse
    assert "const bool has_payload =" in parse

    no_bin = parse[
        parse.index("} else {\n    const bool sequence_end")
        : parse.index("/* 有 audio.next_bin_len 时须等 BIN 入队后再推进 idx")
    ]
    assert "pbArmCurrentChunkTimeline(idx, chunk_ms)" in no_bin
    assert "pbDispatchChunkPreamble(idx, start_at_ms)" in no_bin
    assert "pbMaybeAck(idx)" in no_bin
    assert "pbSealCurrentTerminalTrack(idx)" in no_bin

    barrier = _between(
        asr,
        "void AsrChatClient::pbEvaluateTerminalTrack",
        "void AsrChatClient::pbSealCurrentTerminalTrack",
    )
    assert "!track.audio_expected || track.audio_completed" in barrier
    assert "track.display_completed == track.display_expected" in barrier
    assert "track.motor_completed == track.motor_expected" in barrier


def test_reset_cancels_current_work_in_all_three_workers():
    audio = _read("audio_player.cpp")
    display = _read("display.cpp")
    motor = _read("head.cpp")
    asr = _read("asr_chat_client.cpp")

    assert "s_audio_cancel_epoch.fetch_add" in audio
    assert "s_render_cancel_epoch.fetch_add" in display
    assert "s_motor_cancel_epoch.fetch_add" in motor

    abort = _between(
        asr,
        "void AsrChatClient::abortRuntimeState",
        "void AsrChatClient::resetTransportSession",
    )
    assert "audio_play_emergency_flush()" in abort
    assert "display_render_replace(/*preserve_baseline=*/true)" in abort
    assert "display_render_reset()" not in abort
    assert "head_clear_motor_pending()" in abort


def test_completed_req_is_checked_before_a_new_terminal_track_is_started():
    asr = _read("asr_chat_client.cpp")
    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    assert parse.index("pbFindCompletedReq(req)") < parse.index(
        "pbBeginTerminalTrack(req, idx, durable_replay)"
    )
    assert "pbStageCompletedReplay(doc, *completed)" in parse

    begin = _between(
        asr,
        "bool AsrChatClient::pbBeginTerminalTrack",
        "void AsrChatClient::pbNoteModalitySubmitted",
    )
    assert "pbRememberCompletedReq" not in begin


def test_completed_req_cache_is_loaded_persisted_and_bounded():
    asr = _read("asr_chat_client.cpp")
    header = _read("pb_completed_store.h")
    store = _read("pb_completed_store.cpp")

    restore = _between(
        asr,
        "bool AsrChatClient::pbEnsureCompletedReqCacheLoaded",
        "const AsrChatClient::PbCompletedReq* AsrChatClient::pbFindCompletedReq",
    )
    assert "deskbot_pb_completed_load(" in restore
    assert "completed.persisted = true" in restore

    find = _between(
        asr,
        "const AsrChatClient::PbCompletedReq* AsrChatClient::pbFindCompletedReq",
        "bool AsrChatClient::pbPersistCompletedReq",
    )
    assert find.index("pbEnsureCompletedReqCacheLoaded") < find.index(
        "for (const auto& completed"
    )

    persist = _between(
        asr,
        "bool AsrChatClient::pbPersistCompletedReq",
        "bool AsrChatClient::pbRememberCompletedReq",
    )
    assert persist.index("if (found->persisted)") < persist.index(
        "deskbot_pb_completed_store_record(record)"
    )
    assert "found->persisted = true" in persist

    remember = _between(
        asr,
        "bool AsrChatClient::pbRememberCompletedReq",
        "bool AsrChatClient::pbBeginTerminalTrack",
    )
    assert "The first terminal phase wins" in remember
    assert "if (persist)" in remember
    assert "if (!pbPersistCompletedReq(req))" in remember
    assert "completed.outcome = PbSequenceOutcome::kFailed" in remember

    assert "DESKBOT_PB_COMPLETED_STORE_DEPTH = 24" in header
    assert "offset < DESKBOT_PB_COMPLETED_STORE_DEPTH" in store
    assert "(cursor + 1u) % DESKBOT_PB_COMPLETED_STORE_DEPTH" in store
    assert "nvs_set_blob(" in store
    assert "nvs_set_u8(" in store
    assert store.count("nvs_commit(") == 1
    assert "nvs_erase" not in store
    assert "kRecordVersion = 2" in store
    assert "kRecordFlagDisplayCrc" in store
    assert "record.display_crc_valid" in persist
    assert "record.display_crc32" in persist


def test_only_explicit_durable_sequences_consume_nvs_replay_window():
    asr = _read("asr_chat_client.cpp")
    header = _read("asr_chat_client.h")

    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    assert 'doc["durable"].is<bool>()' in parse
    assert 'doc["durable"].as<bool>()' in parse
    assert "pbBeginTerminalTrack(req, idx, durable_replay)" in parse
    assert "durable_replay && !completed->persisted" in parse

    begin = _between(
        asr,
        "bool AsrChatClient::pbBeginTerminalTrack",
        "uint32_t AsrChatClient::pbArmCurrentChunkTimeline",
    )
    assert "track.durable = durable" in begin
    assert "PbSequenceOutcome::kFailed,\n                     durable" in begin

    remember = _between(
        asr,
        "bool AsrChatClient::pbRememberCompletedReq",
        "bool AsrChatClient::pbBeginTerminalTrack",
    )
    assert "if (persist && !completed.persisted)" in remember
    assert "if (persist)" in remember
    assert "Only explicit durable operations consume" in header
    assert "pb_volatile_completed_req_cache_" in header
    assert "high-frequency turns" in header
    assert "promote_from_volatile" in asr


def test_completed_replay_only_replays_ack_and_never_enters_workers():
    asr = _read("asr_chat_client.cpp")
    replay = _between(
        asr,
        "bool AsrChatClient::pbStageCompletedReplay",
        "void AsrChatClient::pbAdvanceCompletedReplayBin",
    )
    assert "pbQueueTerminalAck(" in replay
    assert "audio_pb_stream_" not in replay
    assert "display_pb_submit_" not in replay
    assert "head_servo_cmd_" not in replay
    assert "pbBeginTerminalTrack" not in replay
    assert "completed.display_crc_valid" in replay
    assert "completed.display_crc32" in replay


def test_completed_replay_validates_and_discards_binary_before_normal_pb_path():
    asr = _read("asr_chat_client.cpp")
    binary = asr[asr.index("pb_last_bin_ms_ = millis()") :]
    replay_at = binary.index("if (pbCompletedReplayExpectsBin())")
    normal_at = binary.index("if (pb_active_ && pb_expect_bin_)")
    assert replay_at < normal_at
    replay = binary[replay_at:normal_at]
    assert "length > expected - pb_completed_replay_bin_received_" in replay
    assert "pb_completed_replay_bin_received_ += length" in replay
    assert (
        'resetTransportSession("completed PB replay BIN fragment overflow")'
        in replay
    )
    assert "pbAdvanceCompletedReplayBin()" in replay
    assert "audio_pb_stream_push_owned" not in replay
    assert "WStype_FRAGMENT" not in asr
    assert "individually complete, CRC-validated frames" in asr


def test_normal_pb_binary_fragments_are_bounded_and_reassembled_once():
    asr = _read("asr_chat_client.cpp")
    header = _read("asr_chat_client.h")
    binary = asr[asr.index("if (pb_active_ && pb_expect_bin_)") :]
    reset = _between(
        asr,
        "void AsrChatClient::pbReset",
        "void AsrChatClient::pbProtocolError",
    )

    assert "pb_expect_bin_buf_" in header
    assert "pb_expect_bin_received_" in header
    assert "pb_expect_bin_free_caps_" in header
    assert "length > pb_expect_bin_len_ - pb_expect_bin_received_" in binary
    assert "pb_expect_bin_received_ += length" in binary
    assert "pb_expect_bin_received_ < pb_expect_bin_len_" in binary
    assert "logical_payload = pb_expect_bin_buf_" in binary
    assert "pcm_owned = reinterpret_cast<int16_t*>(logical_payload)" in binary
    assert "pbAdvanceBinQueue()" in binary
    assert "pbFreeExpectedBin()" in reset


def test_completed_replay_blocks_deferred_json_until_all_declared_bins_arrive():
    asr = _read("asr_chat_client.cpp")
    flush = _between(
        asr,
        "void AsrChatClient::flushDeferredPbJson",
        "void AsrChatClient::pbDiscardDeferredJsonQueue",
    )
    replay_guard = flush.index("if (pbCompletedReplayExpectsBin())")
    assert replay_guard < flush.index("pb_defer_head_ =")
    assert "break;" in flush[replay_guard : flush.index("if (pb_expect_bin_)")]

    timeout = _between(
        asr,
        "void AsrChatClient::pbTickExpectBinTimeout",
        "void AsrChatClient::loopLite",
    )
    assert "pb_completed_replay_bin_since_ms_" in timeout
    assert 'resetTransportSession("completed PB replay BIN timeout")' in timeout


def test_pb_reset_preserves_completed_req_idempotency_cache():
    asr = _read("asr_chat_client.cpp")
    reset = _between(
        asr,
        "void AsrChatClient::pbReset",
        "void AsrChatClient::pbProtocolError",
    )
    assert "pb_completed_req_cache_" not in reset
    assert "pb_completed_req_cache_cursor_" not in reset
    assert "pbClearCompletedReplay" in reset

    remember = _between(
        asr,
        "bool AsrChatClient::pbRememberCompletedReq",
        "bool AsrChatClient::pbBeginTerminalTrack",
    )
    assert "The first terminal phase wins" in remember
    assert "return true;" in remember


def test_all_modalities_share_absolute_chunk_timeline_without_drift():
    asr = _read("asr_chat_client.cpp")
    audio = _read("audio_player.cpp")
    display = _read("display.cpp")
    motor = _read("head.cpp")

    duration = _between(
        asr,
        "static uint32_t pb_effective_chunk_ms",
        "AsrChatClient::PbTerminalTrack* AsrChatClient::pbFindTerminalTrack",
    )
    assert "declared_ms > kMaxChunkMs ? kMaxChunkMs : declared_ms" in duration
    assert "if (declared_ms > 0)" in duration
    assert "if (pb_read_audio_next_bin_len(doc) > 0)" in duration
    assert duration.index("if (declared_ms > 0)") < duration.index(
        "if (pb_read_audio_next_bin_len(doc) > 0)"
    )
    assert duration.index("if (pb_read_audio_next_bin_len(doc) > 0)") < duration.index(
        "uint32_t anim_ms = 0"
    )
    assert "if (anim_ms > effective_ms)" in duration
    assert "if (servo_ms > effective_ms)" in duration
    assert "requested > kMaxChunkMs ? kMaxChunkMs : requested" in duration

    arm = _between(
        asr,
        "uint32_t AsrChatClient::pbArmCurrentChunkTimeline",
        "void AsrChatClient::pbNoteModalitySubmitted",
    )
    assert "track->timeline_start_ms" in arm
    assert "track->timeline_next_offset_ms" in arm
    assert "start_at_ms" in arm

    finish = _between(
        asr,
        "bool AsrChatClient::pbFinishChunkBins",
        "bool AsrChatClient::pbDispatchChunkPreamble",
    )
    assert "pb_pending_pcm_samples_" in finish
    assert "pbArmCurrentChunkTimeline(" in finish
    assert "pbSubmitPendingAudio(pending_idx_snap, start_at_ms)" in finish
    assert "pbDispatchChunkPreamble(pending_idx_snap, start_at_ms)" in finish

    assert "deskbot_pb_time_late(millis(), job.pb_start_at_ms)" in audio
    assert "skip_samples" in audio
    assert "segment_start_at_ms =" in display
    assert "chunk_start_at_ms + segment_offset_ms" in display
    assert "command.start_at_ms = start_at_ms + segment_offset_ms" in asr
    assert "deskbot_pb_time_reached(now_ms, segment_end_at_ms)" in motor


def test_pending_terminal_trackers_have_timeline_aware_bounded_leases():
    asr = _read("asr_chat_client.cpp")
    header = _read("asr_chat_client.h")

    assert "created_ms" in header
    assert "lease_deadline_ms" in header
    assert "kPbTerminalIngressLeaseMs = 35000u" in header
    assert "kPbTerminalProgressLeaseMs = 15000u" in header
    assert "kPbTerminalWorkerGraceMs = 10000u" in header
    assert "kPbTerminalHardLeaseMs" in header

    begin = _between(
        asr,
        "bool AsrChatClient::pbBeginTerminalTrack",
        "void AsrChatClient::pbRefreshTerminalLease",
    )
    assert "track.created_ms = millis()" in begin
    assert "track.created_ms + kPbTerminalIngressLeaseMs" in begin

    refresh = _between(
        asr,
        "void AsrChatClient::pbRefreshTerminalLease",
        "uint32_t AsrChatClient::pbArmCurrentChunkTimeline",
    )
    assert "elapsed_ms >= kPbTerminalHardLeaseMs" in refresh
    assert "deskbot_pb_time_remaining(now, expected_finish_ms)" in refresh
    assert "kPbTerminalWorkerGraceMs" in refresh
    assert "remaining_ms > hard_remaining_ms" in refresh

    arm = _between(
        asr,
        "uint32_t AsrChatClient::pbArmCurrentChunkTimeline",
        "void AsrChatClient::pbNoteModalitySubmitted",
    )
    assert "pbRefreshTerminalLease(" in arm
    assert "track->timeline_start_ms + track->timeline_next_offset_ms" in arm

    expire = _between(
        asr,
        "void AsrChatClient::pbExpireTerminalTracks",
        "void AsrChatClient::pbFailCurrentTerminal",
    )
    assert "deskbot_pb_time_reached(now, track.lease_deadline_ms)" in expire
    assert "PbSequenceOutcome::kFailed" in expire
    assert "pbQueueTerminalAck(" in expire
    assert "track.used = false" in expire

    loop_lite = _between(
        asr,
        "void AsrChatClient::loopLite",
        "void AsrChatClient::serviceLoop",
    )
    assert loop_lite.index("pbDrainWorkerTerminals()") < loop_lite.index(
        "pbExpireTerminalTracks()"
    )
    assert loop_lite.index("pbExpireTerminalTracks()") < loop_lite.index(
        "flushPendingPbAck()"
    )


def test_terminal_event_queue_loss_fails_closed_in_main_loop():
    asr = _read("asr_chat_client.cpp")
    workers = (
        ("audio_player.cpp", "audio_pb_terminal_drop_count"),
        ("display.cpp", "display_pb_terminal_drop_count"),
        ("head.cpp", "head_pb_terminal_drop_count"),
    )
    for filename, counter_fn in workers:
        source = _read(filename)
        assert "PB terminal queue unavailable" in source
        assert "PB terminal queue full" in source
        assert "fetch_add(" in source
        assert f"uint32_t {counter_fn}()" in source

    drain = _between(
        asr,
        "void AsrChatClient::pbDrainWorkerTerminals",
        "void AsrChatClient::pbExpireTerminalTracks",
    )
    for counter_fn in (
        "audio_pb_terminal_drop_count()",
        "display_pb_terminal_drop_count()",
        "head_pb_terminal_drop_count()",
    ):
        assert counter_fn in drain
    assert drain.count("pbFailTracksWaitingForModality(") == 3

    fail_closed = _between(
        asr,
        "void AsrChatClient::pbFailTracksWaitingForModality",
        "void AsrChatClient::pbDrainWorkerTerminals",
    )
    assert "track.audio_expected && !track.audio_completed" in fail_closed
    assert "track.display_completed < track.display_expected" in fail_closed
    assert "track.motor_completed < track.motor_expected" in fail_closed
    assert "PbSequenceOutcome::kFailed" in fail_closed
    assert "pbQueueTerminalAck(" in fail_closed
    assert "track.used = false" in fail_closed


def test_servo_replace_and_classifier_have_single_unambiguous_semantics():
    asr = _read("asr_chat_client.cpp")
    classifier = _between(
        asr,
        "static bool pb_doc_is_servo_only_gesture",
        "extern AsrChatClient asrChatClient",
    )
    assert "!pb_doc_has_nonempty_servo(doc)" in classifier
    assert "pb_doc_has_nonempty_anim(doc)" in classifier
    assert "pb_parse_mic_hint(doc) != PbMicHint::kNone" in classifier
    assert 'doc["camera_once"]' in classifier

    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    replace = parse[parse.index("if (qd == PbQueueDecision::kClear") :]
    replace = replace[: replace.index("pb_inflight_seq_count_ = 0")]
    assert "const bool has_display = pb_doc_has_nonempty_anim(doc)" in replace
    assert "const bool has_motor = pb_doc_has_nonempty_servo(doc)" in replace
    assert "pbDrainWorkersForNewSequence(has_display, has_motor, replace_audio" in replace
    assert "display_fallback" not in replace
    assert "keep motor queue" not in replace


def test_servo_array_is_prevalidated_and_motor_batch_commit_is_atomic():
    asr = _read("asr_chat_client.cpp")
    head = _read("head.cpp")
    stage = _between(
        asr,
        "bool AsrChatClient::pbPreflightChunk",
        "static uint32_t pb_servo_worst_axis_travel",
    )
    assert "servo.size() > kPbMaxServoSegsPerChunk" in stage
    assert "pb_json_integer_in_range" in stage
    assert "kMaxModalityDurationMs = 300000u" in stage
    assert 'segment["ms"], 50,' in stage
    assert "ms > 0 ? static_cast<uint32_t>(ms) : 20u" not in stage
    assert "kMaxModalityDurationMs - servo_total_ms" in stage
    assert "servo[] truncated" not in asr
    assert "(uint16_t)constrain" not in stage

    apply = _between(
        asr,
        "bool AsrChatClient::pbApplyServoArrayIfAny",
        "void AsrChatClient::pbUpdateAudioBufDecayWall",
    )
    assert "HeadPbServoCmd commands[kPbMaxServoSegsPerChunk]" in apply
    assert "head_servo_cmd_pb_batch_async(commands, n" in apply
    assert apply.index("head_servo_cmd_pb_batch_async") < apply.index(
        "pbNoteModalitySubmitted(PbModality::kMotor)"
    )

    commit = _between(
        head,
        "static bool enqueue_motor_batch_strict",
        "/** Submit one motor command asynchronously",
    )
    assert "xSemaphoreTake(s_motor_submit_lock" in commit
    assert "command_count > uxQueueSpacesAvailable(s_motor_queue)" in commit
    assert commit.index("uxQueueSpacesAvailable") < commit.index(
        "xQueueSend(s_motor_queue, &command, 0)"
    )
    assert "drop_oldest" not in head


def test_max_servo_batch_is_heap_staged_without_stack_or_partial_execution():
    head = _read("head.cpp")
    batch = _between(
        head,
        "bool head_servo_cmd_pb_batch_async",
        "bool head_take_pb_terminal_event",
    )
    assert "MotorCmd staged[k_motor_queue_depth]" not in batch
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in batch
    assert "heap_caps_malloc(staged_bytes, MALLOC_CAP_8BIT)" in batch
    assert "PB batch staging allocation failed" in batch
    assert batch.index("total_duration_ms += input.ms") < batch.index(
        "heap_caps_malloc("
    )
    allocation_failure = batch.index("if (staged == nullptr)", batch.index("if (staged == nullptr)") + 1)
    enqueue = batch.index("enqueue_motor_batch_strict(staged, command_count)")
    assert batch.index("return false;", allocation_failure) < enqueue
    assert batch.index("heap_caps_free(staged)") > enqueue


def test_relative_servo_is_resolved_by_actor_and_duration_stays_uint32():
    header = _read("head.h")
    asr_header = _read("asr_chat_client.h")
    asr = _read("asr_chat_client.cpp")
    head = _read("head.cpp")
    apply = _between(
        asr,
        "bool AsrChatClient::pbApplyServoArrayIfAny",
        "void AsrChatClient::pbUpdateAudioBufDecayWall",
    )
    actor = _between(head, "void motor_task", "void drain_motor_queue_nonblocking")

    assert "command.xm = static_cast<uint8_t>(seg.xm)" in apply
    assert "command.x = seg.x" in apply
    assert "planned_x" not in apply
    assert "resolve_target(cmd.xm, x, cmd.x" in actor
    assert "uint32_t ms = 0;" in header
    assert "uint32_t ms = 0;" in asr_header
    assert "uint32_t ms;" in head
    assert "65535" not in apply


def test_pb_motor_rate_limit_extends_deadline_without_final_snap():
    head = _read("head.cpp")
    actor = _between(head, "void motor_task", "void drain_motor_queue_nonblocking")
    pb_motion = actor[
        actor.index("if (cmd.ms > 0 && cmd.pb_tracked") : actor.index(
            "} else if (cmd.ms > 0)"
        )
    ]
    assert "kPbMaxStepDegPerTick = 3" in pb_motion
    assert "step_toward(x, ideal_x, kPbMaxStepDegPerTick)" in pb_motion
    assert "step_toward(y, ideal_y, kPbMaxStepDegPerTick)" in pb_motion
    assert "deadline_reached && x == x_target && y == y_target" in pb_motion
    assert "x = x_target" not in pb_motion
    assert "y = y_target" not in pb_motion


def test_motor_terminal_pose_is_identity_bound_and_ack_is_commanded():
    head_header = _read("head.h")
    head = _read("head.cpp")
    asr_header = _read("asr_chat_client.h")
    asr = _read("asr_chat_client.cpp")
    assert "commanded_x" in head_header and "commanded_y" in head_header
    assert "out.commanded_x = static_cast<int16_t>(commanded_x)" in head
    assert "static std::atomic<int> s_logical_x" in head
    assert "motor_pose_idx" in asr_header
    assert "terminal.commanded_x" in asr
    assert r'\"pose_source\":\"commanded\"' in asr
    assert "pb_ack_servo_report" not in asr
    assert "head_take_pb_motor_ack_done" not in head + asr + head_header
    assert "head_drain_pb_motor_ack_queue" not in head + asr + head_header


def test_servo_pwm_range_and_runtime_initialization_fail_closed():
    head = _read("head.cpp")
    assert "kServoPulseMinUs = 1000" in head
    assert "kServoPulseMaxUs = 2000" in head
    assert "ledc_timer_config(&timer)" in head
    assert head.count("head_servo_configure_channel(") == 3
    assert "ledc_set_duty(" in head
    assert "ledc_update_duty(" in head
    assert "ledc_set_duty_and_update(kServoLedcMode" not in head
    ensure = _between(
        head, "bool ensure_motor_task()", "/** Strict producer transaction"
    )
    assert "runtime resource allocation failed; motor disabled" in ensure
    assert "rc != pdPASS || !created_task" in ensure
    assert "s_motor_runtime_failed.load" in ensure
    assert "s_motor_runtime_failed.store(true" in ensure
    assert "kMotorTaskStackBytes" in ensure
    assert "internal_largest" in ensure
    submit = _between(
        head,
        "static bool enqueue_motor_batch_strict",
        "/** Submit one motor command asynchronously",
    )
    assert "if (!ensure_motor_task())" in submit
    assert "return false;" in submit


def test_servo_runtime_is_reserved_early_and_telemetry_never_retries_creation():
    head = _read("head.cpp")
    setup = _between(head, "void setup_head()", "void head_clear_motor_pending(")
    assert "if (!ensure_motor_task())" in setup
    assert setup.index("if (!ensure_motor_task())") < setup.index(
        "await camera then permanent attach"
    )
    assert "kMotorTaskStackBytes = 6u * 1024u" in head

    terminal_poll = _between(
        head, "bool head_take_pb_terminal_event", "uint32_t head_pb_terminal_drop_count"
    )
    queue_depth = _between(
        head, "unsigned head_motor_input_queue_depth", "\n}"
    )
    assert "ensure_motor_task" not in terminal_poll
    assert "ensure_motor_task" not in queue_depth
    assert "xQueueReceive" in terminal_poll
    assert "uxQueueMessagesWaiting" in queue_depth


def test_chain_head_preflight_precedes_every_destructive_replace_mutation():
    asr = _read("asr_chat_client.cpp")
    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    preflight_call = parse.index("pbPreflightChunk(doc")
    assert preflight_call < parse.index("pbDecideChainHead(")
    assert preflight_call < parse.index("pbDrainWorkersForNewSequence(")
    assert preflight_call < parse.index("pbBeginTerminalTrack(")
    reject = parse[
        parse.index("if (!pbPreflightChunk(doc") : parse.index(
            "const uint32_t idx = preflight.idx"
        )
    ]
    assert "PbSequenceOutcome::kFailed" in reject
    assert "pbDrainWorkersForNewSequence" not in reject
    assert "head_clear_motor_pending" not in reject

    pure = _between(
        asr,
        "bool AsrChatClient::pbPreflightChunk",
        "void AsrChatClient::pbCommitServoPreflight",
    )
    assert "pb_pending_servo_segs_" not in pure
    assert "pb_pending_servo_seg_count_" not in pure
    assert "audio_play_" not in pure
    assert "display_render_reset" not in pure
    assert "head_clear_motor_pending" not in pure


def test_servo_soft_limits_and_extreme_values_are_fail_closed_end_to_end():
    asr = _read("asr_chat_client.cpp")
    head = _read("head.cpp")
    head_header = _read("head.h")
    asr_header = _read("asr_chat_client.h")
    preflight = _between(
        asr,
        "bool AsrChatClient::pbPreflightChunk",
        "void AsrChatClient::pbCommitServoPreflight",
    )
    for field in ("x_min", "x_max", "y_min", "y_max"):
        assert field in preflight
        assert field in head_header
        assert field in asr_header
    assert "bound_count != 0u && bound_count != 4u" in preflight
    assert 'reject("invalid servo soft limits")' in preflight
    assert 'reject("servo target exceeds soft limits")' in preflight
    assert "x >= -180 && x <= 180" in preflight
    assert "y >= -40 && y <= 40" in preflight
    assert "xm == HEAD_SERVO_HOLD && x == 0" in preflight
    assert "ym == HEAD_SERVO_HOLD && y == 0" in preflight

    apply = _between(
        asr,
        "bool AsrChatClient::pbApplyServoArrayIfAny",
        "void AsrChatClient::pbUpdateAudioBufDecayWall",
    )
    assert "command.x_min = seg.x_min" in apply
    assert "command.y_max = seg.y_max" in apply
    batch = _between(
        head,
        "bool head_servo_cmd_pb_batch_async",
        "bool head_take_pb_terminal_event",
    )
    assert "!x_soft_valid || !y_soft_valid" in batch
    assert "command.x_min = input.x_min" in batch
    actor = _between(head, "void motor_task", "void drain_motor_queue_nonblocking")
    assert "cmd.x_min, cmd.x_max" in actor
    assert "cmd.y_min, cmd.y_max" in actor
    resolver = _between(head, "static int resolve_target", "static int step_toward")
    assert "const int64_t wide_target" in resolver
    assert "static_cast<int64_t>(cur) + static_cast<int64_t>(val)" in resolver


def test_servo_interpolation_is_signed_unified_and_hardware_bounded():
    head = _read("head.cpp")
    helper = _between(
        head,
        "static int interpolate_axis_clamped",
        "static bool motor_cmd_cancelled",
    )
    assert "const int64_t delta" in helper
    assert (
        "static_cast<int64_t>(target) - static_cast<int64_t>(start)" in helper
    )
    assert "delta * static_cast<int64_t>(progress)" in helper
    assert "/ static_cast<int64_t>(total)" in helper
    assert "ideal < static_cast<int64_t>(hard_lo)" in helper
    assert "ideal > static_cast<int64_t>(hard_hi)" in helper
    assert "const int path_lo = start < target ? start : target" in helper
    assert "const int path_hi = start > target ? start : target" in helper
    assert "ideal < static_cast<int64_t>(path_lo)" in helper
    assert "ideal > static_cast<int64_t>(path_hi)" in helper

    actor = _between(head, "void motor_task", "void drain_motor_queue_nonblocking")
    assert actor.count("interpolate_axis_clamped(") == 4
    assert "(long)dx_total" not in actor
    assert "(long)dy_total" not in actor
    assert "x = constrain(x, cmd.x_min, cmd.x_max)" not in actor
    assert "y = constrain(y, cmd.y_min, cmd.y_max)" not in actor
    assert "step_toward(x, ideal_x, kPbMaxStepDegPerTick)" in actor
    assert "X_MIN_LIMIT, X_MAX_LIMIT" in actor
    assert "Y_MIN_LIMIT, Y_MAX_LIMIT" in actor
    assert "const uint32_t execution_started_ms = millis()" in actor
    assert "const uint32_t execution_budget_ms" in actor
    assert "cmd.ms > physical_min_ms ? cmd.ms : physical_min_ms" in actor
    assert "kPbMotorExecutionGraceMs" in actor
    assert "execution_watchdog_deadline_ms" in actor
    assert "execution_failed = true" in actor
    assert "HeadPbTerminalState::kFailed" in actor
    assert (
        actor.index("!completed ? HeadPbTerminalState::kCancelled")
        < actor.index(": execution_failed ? HeadPbTerminalState::kFailed")
    )


def test_servo_new_soft_range_does_not_fabricate_the_starting_pose():
    head = _read("head.cpp")
    helper = _between(
        head,
        "static int interpolate_axis_clamped",
        "static bool motor_cmd_cancelled",
    )
    actor = _between(head, "void motor_task", "void drain_motor_queue_nonblocking")

    assert "Keep ``start`` equal to the last commanded PWM position" in helper
    assert "start = constrain(start" not in helper
    assert "target = constrain(target, hard_lo, hard_hi)" in helper
    assert actor.index("const int x_target") < actor.index("motor_wait_for_pb_start")
    assert "x = constrain(x, cmd.x_min, cmd.x_max)" not in actor
    assert "y = constrain(y, cmd.y_min, cmd.y_max)" not in actor
    assert "x_start, x_target" in actor
    assert "y_start, y_target" in actor


def test_voice_round_rollover_preserves_valid_pending_terminal_lanes():
    asr = _read("asr_chat_client.cpp")
    voice_round = _between(
        asr,
        "bool AsrChatClient::runVoiceRound",
        "void AsrChatClient::onWirePayload",
    )
    cleanup_start = voice_round.index(
        "if (!pb_active_ && !pbAnyDownlinkBinExpected())"
    )
    cleanup_end = voice_round.index("struct VoiceUplinkGuard", cleanup_start)
    cleanup = voice_round[cleanup_start:cleanup_end]
    assert "if (pbHasPendingTerminalTracks())" in cleanup
    assert "preserve pending PB terminal lanes" in cleanup
    assert "pbDrainWorkersForNewSequence" not in cleanup
    assert cleanup.index("pbReset(/*stop_audio=*/true)") > cleanup.index("} else {")


def test_audio_chunk_clock_does_not_expand_to_cross_chunk_servo_lane():
    asr = _read("asr_chat_client.cpp")
    duration = _between(
        asr,
        "static uint32_t pb_effective_chunk_ms",
        "AsrChatClient::PbTerminalTrack* AsrChatClient::pbFindTerminalTrack",
    )
    declared_guard = duration.index("if (declared_ms > 0)")
    audio_guard = duration.index("if (pb_read_audio_next_bin_len(doc) > 0)")
    assert declared_guard < audio_guard
    assert audio_guard < duration.index("uint32_t anim_ms = 0")
    assert "return effective_ms;" in duration[declared_guard : audio_guard]
    assert "return 0u;" in duration[audio_guard : audio_guard + 180]
    finish = _between(
        asr,
        "bool AsrChatClient::pbFinishChunkBins",
        "bool AsrChatClient::pbDispatchChunkPreamble",
    )
    assert "if (audio_ms > pb_pending_chunk_ms_)" in finish
    assert "pb_pending_chunk_ms_ = audio_ms" in finish


def test_declared_audio_free_row_span_is_not_extended_by_long_motor_or_anim():
    asr = _read("asr_chat_client.cpp")
    duration = _between(
        asr,
        "static uint32_t pb_effective_chunk_ms",
        "AsrChatClient::PbTerminalTrack* AsrChatClient::pbFindTerminalTrack",
    )
    declared_guard = duration.index("if (declared_ms > 0)")
    fallback = duration.index("uint32_t anim_ms = 0")
    assert declared_guard < fallback
    declared_block = duration[declared_guard:fallback]
    assert "return effective_ms;" in declared_block
    assert "servo_ms" not in declared_block
    assert "anim_ms" not in declared_block


def test_pb_motor_api_enforces_minimum_and_total_duration_independently():
    head = _read("head.cpp")
    batch = _between(
        head,
        "bool head_servo_cmd_pb_batch_async",
        "bool head_take_pb_terminal_event",
    )
    assert "input.ms < 50u" in batch
    assert "uint32_t total_duration_ms = 0" in batch
    assert "input.ms > kPbMaxDurationMs - total_duration_ms" in batch
    assert "total_duration_ms += input.ms" in batch


def test_terminal_lease_never_shrinks_and_counters_are_uint32():
    asr = _read("asr_chat_client.cpp")
    header = _read("asr_chat_client.h")
    refresh = _between(
        asr,
        "void AsrChatClient::pbRefreshTerminalLease",
        "uint32_t AsrChatClient::pbArmCurrentChunkTimeline",
    )
    assert "current_remaining_ms" in refresh
    assert "current_remaining_ms > remaining_ms" in refresh
    assert refresh.index("current_remaining_ms > remaining_ms") < refresh.index(
        "track.lease_deadline_ms = now + remaining_ms"
    )
    for field in (
        "display_expected",
        "display_completed",
        "motor_expected",
        "motor_completed",
    ):
        assert f"uint32_t {field}" in header
    note = _between(
        asr,
        "void AsrChatClient::pbNoteModalitySubmitted",
        "void AsrChatClient::pbQueueTerminalAck",
    )
    assert note.count("UINT32_MAX") == 2


def test_cross_request_append_is_failed_while_prior_lanes_are_pending():
    asr = _read("asr_chat_client.cpp")
    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    guard = parse[
        parse.index("const bool unsafe_cross_request_append") : parse.index(
            "const bool force_restart_same_req"
        )
    ]
    assert "qd == PbQueueDecision::kAppend" in guard
    assert "pbHasPendingTerminalTracks()" in guard
    assert "unsafe_cross_request_append ? PbSequenceOutcome::kFailed" in guard
    assert "cross-request append rejected" in guard
    assert 'if (type == "pb_start")' in guard
    assert "pb_suppress_tail_req_ = req" in guard
    assert guard.index("pb_suppress_tail_req_ = req") < guard.index(
        "pbQueueTerminalAck("
    )
    # Continuation chunks do not enter chain-head arbitration.
    assert "if (is_chain_head)" in parse

    suppress = parse[
        parse.index("if (!pb_suppress_tail_req_.isEmpty())") : parse.index(
            "const PbMicHint mic_hint_early"
        )
    ]
    assert "req != pb_suppress_tail_req_" in suppress
    assert 'type == "pb_start" || type == "pb_single"' in suppress
    assert "ignore stale pb after abort" in suppress
    assert "pbProtocolError" not in suppress


def test_rejected_multiframe_chain_has_independent_bounded_bin_discard():
    header = _read("asr_chat_client.h")
    asr = _read("asr_chat_client.cpp")
    assert "pb_rejected_discard_bin_lens_[kPbMaxBinsPerChunk]" in header
    assert "pbRejectedDiscardExpectsBin()" in header
    any_bin = _between(
        header,
        "bool pbAnyDownlinkBinExpected() const",
        "bool pbStageCompletedReplay",
    )
    assert "pbRejectedDiscardExpectsBin()" in any_bin

    stage = _between(
        asr,
        "bool AsrChatClient::pbStageRejectedDiscard",
        "void AsrChatClient::pbAdvanceRejectedDiscardBin",
    )
    assert "DESKBOT_USB_MAX_PAYLOAD" in stage
    assert "kPbMaxBinsPerChunk" in stage
    assert "kPbMaxAssetsPerChunk" in stage
    assert "pb_rejected_discard_unframed_ = true" in stage
    assert "pbProtocolError" not in stage
    assert "head_clear_motor_pending" not in stage

    parse = _between(
        asr,
        "bool AsrChatClient::pbParseAndStage",
        "void AsrChatClient::abortRuntimeState",
    )
    assert parse.index("if (pb_rejected_discard_active_)") < parse.index(
        "const PbCompletedReq* completed"
    )
    rejected_tail = parse[
        parse.index("if (pb_rejected_discard_active_)") : parse.index(
            "const PbCompletedReq* completed"
        )
    ]
    assert "pbStageRejectedDiscard(doc, req)" in rejected_tail
    assert "ignored rejected-chain tail" in rejected_tail
    assert "return true;" in rejected_tail
    assert "pbProtocolError" not in rejected_tail

    binary = asr[asr.index("if (pb_rejected_discard_active_)", asr.index("pb_last_bin_ms_")) :]
    binary = binary[: binary.index("if (pbCompletedReplayExpectsBin())")]
    assert "pbRejectedDiscardExpectsBin()" in binary
    assert "pb_rejected_discard_bin_received_" in binary
    assert "pbAdvanceRejectedDiscardBin()" in binary
    assert "pbProtocolError" not in binary
    assert "audio_pb_stream" not in binary
    assert "head_clear_motor_pending" not in binary


def test_durable_store_failure_cannot_claim_played():
    asr = _read("asr_chat_client.cpp")
    remember = _between(
        asr,
        "bool AsrChatClient::pbRememberCompletedReq",
        "bool AsrChatClient::pbBeginTerminalTrack",
    )
    assert remember.count("if (!pbPersistCompletedReq(req))") >= 3
    assert remember.count("completed.outcome = PbSequenceOutcome::kFailed") >= 3
    queue = _between(
        asr,
        "void AsrChatClient::pbQueueTerminalAck",
        "bool AsrChatClient::pbPopTerminalAck",
    )
    assert "&& persist" in queue
    assert '"reporting failed instead of played"' in queue
    assert "outcome = PbSequenceOutcome::kFailed" in queue


def test_protocol_errors_cancel_every_worker_even_without_audio():
    asr = _read("asr_chat_client.cpp")
    protocol_error = _between(
        asr,
        "void AsrChatClient::pbProtocolError",
        "bool AsrChatClient::pbSubmitAnimIfAny",
    )
    assert "audio_play_emergency_flush();" in protocol_error
    assert "display_render_replace(/*preserve_baseline=*/true);" in protocol_error
    assert "display_render_reset();" not in protocol_error
    assert "head_clear_motor_pending();" in protocol_error
    assert "audio_play_input_queue_depth()" not in protocol_error


def test_pb_cancel_and_defer_failure_preserve_the_selected_face():
    asr = _read("asr_chat_client.cpp")
    wire = asr[asr.index("void AsrChatClient::onWirePayload") :]
    cancel = wire[
        wire.index('if (t == "pb_cancel")') :
        wire.index("if (pb_doc_is_servo_only_gesture", wire.index('if (t == "pb_cancel")'))
    ]
    defer_failure = wire[
        wire.index("if (!pbDeferEnqueue(payload, length))") :
        wire.index("} else if (!pbAnyDownlinkBinExpected())")
    ]

    for path in (cancel, defer_failure):
        assert "display_fallback" not in path
        assert "/*preserve_display_baseline=*/true" in path


def test_platformio_ignores_only_owner_qualified_unused_transitive_libraries():
    ini = (ROOT / "hardware" / "platformio.ini").read_text(encoding="utf-8")
    assert "arduino-libraries/SD" in ini
    assert "bitbank2/bb_spi_lcd" in ini
    assert "\n\tSD\n" not in ini
    assert "arduino-libraries/SD@" not in ini
    assert "bitbank2/bb_spi_lcd@" not in ini
    assert "\n\tolikraus/U8g2@" not in ini
    assert "U8g2_for_Adafruit_GFX.git#82d2b3eea866e7d40266672b41e5c8306ee97403" in ini
