"""Static contracts for fail-closed firmware audio playback.

The ESP32 build remains the compile gate.  These focused checks keep the
worker-start, bounded-I2S, single-writer, and waiter-notification invariants visible
in the regular pytest suite without requiring hardware.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
AUDIO_CPP = ROOT / "hardware" / "firmware" / "audio_player.cpp"
AUDIO_H = ROOT / "hardware" / "firmware" / "audio_player.h"
DESKBOT_CONFIG_H = ROOT / "hardware" / "firmware" / "deskbot_config.h"


def _source() -> str:
    return AUDIO_CPP.read_text(encoding="utf-8")


def _between(source: str, start: str, end: str) -> str:
    start_at = source.index(start)
    return source[start_at : source.index(end, start_at)]


def test_worker_creation_and_enqueue_failures_are_fail_fast_and_owning():
    source = _source()
    ensure = _between(source, "bool ensure_audio_play_task()", "}  // namespace")
    generic_enqueue = _between(
        source,
        "static bool enqueue_audio_play_job(AudioPlayJob& j)",
        "static bool enqueue_audio_play_job_strict",
    )
    strict_enqueue = _between(
        source,
        "static bool enqueue_audio_play_job_strict",
        "static void drain_audio_play_queue",
    )

    assert "TaskHandle_t created_task = nullptr" in ensure
    assert "xQueueCreateWithCaps(" in ensure
    assert "xTaskCreatePinnedToCoreWithCaps(" in ensure
    assert "MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT" in ensure
    assert "rc != pdPASS || created_task == nullptr" in ensure
    assert ensure.index("return false;") < ensure.rindex("return true;")
    assert "if (!ensure_audio_play_task())" in generic_enqueue
    assert "if (!ensure_audio_play_task())" in strict_enqueue

    pb_push = _between(
        source, "bool audio_pb_stream_push_owned", "bool audio_pb_stream_end"
    )
    assert "if (!enqueue_audio_play_job_strict(j))" in pb_push
    assert "free_audio_play_job_pcm(j)" in pb_push

    generic_push = _between(
        source, "bool audio_stream_pcm16_push_owned", "void audio_stream_pcm16_stop"
    )
    assert "if (!enqueue_audio_play_job(j))" in generic_push
    assert "free_audio_play_job_pcm(j)" in generic_push

    for removed in (
        "post_play_job_and_wait",
        "s_audio_play_done_sem",
        "s_audio_play_mutex",
        "s_pipeline_mutex",
    ):
        assert removed not in source

def test_every_i2s_write_is_bounded_complete_or_failed_closed():
    source = _source()
    checked = _between(
        source, "static bool write_i2s_checked", "static bool pcm_chunk_audible"
    )

    assert "kI2sWriteTimeoutMs)" in checked
    assert "pdMS_TO_TICKS" not in checked
    assert "err == ESP_OK && bytes_written == bytes" in checked
    assert "return false;" in checked
    assert "portMAX_DELAY" not in checked
    assert source.count("i2s_channel_write(") == 1
    assert source.count("write_i2s_checked(") == 3
    for phase in ('"pcm"', '"stream-tail"'):
        assert phase in source
    assert '"wav-tail"' not in source

    worker = source[source.index("void audio_play_task_main") :]
    chunk = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16Chunk:",
        "case AudioPlayJob::Kind::kStreamPcm16End:",
    )
    assert "abort_active_stream_after_i2s_failure()" in chunk
    assert "AudioPbTerminalState::kFailed" in chunk

    stream_abort = _between(
        source,
        "static void abort_active_stream_after_i2s_failure",
        "void audio_play_task_main",
    )
    assert "stop_play()" in stream_abort
    assert "s_stream_pcm_active = false" in stream_abort
    assert "deskbot_uplink_set_speaker_active(false)" in stream_abort
    assert "xSemaphore" not in stream_abort

    stream_end = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16End:",
        "case AudioPlayJob::Kind::kEmergencyFlush:",
    )
    assert "AudioPbTerminalState::kFailed" in stream_end
    assert "AudioPbTerminalState::kCancelled" in stream_end

def test_dma_reset_preloads_silence_and_stereo_descriptors_stay_in_bounds():
    source = _source()
    header = AUDIO_H.read_text(encoding="utf-8")
    preload = _between(
        source, "esp_err_t speaker_preload_silence", "void speaker_release_driver"
    )
    setup = _between(source, "bool speaker_setup_driver", "}  // namespace")
    reconfigure = _between(
        source, "esp_err_t speaker_reconfigure", "bool speaker_setup_driver"
    )
    reset = _between(source, "static void stop_play()", "size_t calculate_mean")

    assert "#define DMA_BUF_COUNT 4" in header
    assert "#define DMA_BUF_LEN 320" in header
    assert "i2s_channel_get_info(" in preload
    assert "channel_info.total_dma_buf_size" in preload
    assert "i2s_channel_preload_data(" in preload
    assert "loaded != bytes_to_load" in preload
    assert setup.index("speaker_preload_silence()") < setup.index(
        "i2s_channel_enable(s_speaker_tx)"
    )
    assert reconfigure.index("i2s_channel_disable(s_speaker_tx)") < (
        reconfigure.index("speaker_preload_silence()")
    )
    assert reconfigure.index("speaker_preload_silence()") < reconfigure.index(
        "i2s_channel_enable(s_speaker_tx)"
    )
    assert reset.index("i2s_channel_disable(s_speaker_tx)") < reset.index(
        "speaker_preload_silence()"
    )
    assert reset.index("speaker_preload_silence()") < reset.index(
        "i2s_channel_enable(s_speaker_tx)"
    )
    assert 'speaker_recover_default("dma-reset", err)' in reset


def test_audio_scaling_is_saturating_and_stream_format_is_authoritative():
    source = _source()
    play = _between(source, "static bool play(", "static void stop_play()")
    worker = source[source.index("void audio_play_task_main") :]
    begin = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16Begin:",
        "case AudioPlayJob::Kind::kStreamPcm16Chunk:",
    )
    chunk = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16Chunk:",
        "case AudioPlayJob::Kind::kStreamPcm16End:",
    )
    end = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16End:",
        "case AudioPlayJob::Kind::kEmergencyFlush:",
    )

    assert "scaled >= 32767.0f" in play
    assert "scaled <= -32768.0f" in play
    assert "lroundf(scaled)" in play
    assert "s_stream_pcm_rate = job.pcm_rate" in begin
    assert "s_stream_pcm_channels = job.pcm_channels" in begin
    assert "std::atomic<uint32_t> s_stream_pcm_rate" in source
    assert "std::atomic<uint8_t> s_stream_pcm_channels" in source
    assert "job.pcm_rate != s_stream_pcm_rate" in chunk
    assert "job.pcm_channels != s_stream_pcm_channels" in chunk
    assert "job.pcm_samples % s_stream_pcm_channels != 0" in chunk
    assert "stream_write_tail_and_restore_i2s(job.cancel_epoch)" in end
    generic_push = _between(
        source,
        "bool audio_stream_pcm16_push_owned",
        "void audio_stream_pcm16_stop",
    )
    assert "j.pcm_rate = SAMPLE_RATE" in generic_push
    assert "j.pcm_channels = 1" in generic_push
    replace = _between(
        source,
        "bool audio_stream_pcm16_replace",
        "void audio_stream_pcm16_stop",
    )
    assert "s_stream_pcm_rate.store(SAMPLE_RATE" in replace
    assert "s_stream_pcm_channels.store(1" in replace


def test_v2_ns4168_uses_known_good_mono_left_wire_format():
    source = _source()
    header = AUDIO_H.read_text(encoding="utf-8")
    config = DESKBOT_CONFIG_H.read_text(encoding="utf-8")
    slot = _between(
        source, "i2s_std_slot_config_t speaker_slot_config()", "esp_err_t speaker_preload_silence"
    )
    checked = _between(
        source, "static bool write_i2s_checked", "static bool pcm_chunk_audible"
    )
    play = _between(source, "static bool play(", "static void stop_play()")
    tail = _between(
        source,
        "static bool stream_write_tail_and_restore_i2s",
        "static void abort_active_stream_after_i2s_failure",
    )
    assert "identical PCM in both physical" in header
    assert "constexpr uint8_t kSpeakerPhysicalChannels = 2u" in source
    assert "I2S_SLOT_MODE_STEREO" in slot
    assert "I2S_STD_SLOT_BOTH" in slot
    assert "I2S_SLOT_MODE_MONO" not in slot
    assert "I2S_STD_SLOT_LEFT" not in slot
    assert "speaker_slot_config(channels)" not in source

    assert "const size_t output_frames = n / logical_channels" in play
    assert "logical_channels == 2" in play
    assert "mono = (mono + data[off + frame * logical_channels + 1u]) / 2" in play
    assert "scratch[frame * kSpeakerPhysicalChannels] = sample" in play
    assert "scratch[frame * kSpeakerPhysicalChannels + 1u] = sample" in play
    assert "job.pcm_samples - skip_samples, job.pcm_channels" in source
    assert "scratch, output_samples * sizeof(int16_t)" in play

    assert "kSpeakerPhysicalChannels);" in checked
    assert "static_cast<size_t>(DMA_BUF_COUNT)" in tail
    assert "static_cast<size_t>(DMA_BUF_LEN)" in tail
    assert "channels == 2" not in tail

    assert "#define DESKBOT_SPEAKER_AMP_CTRL GPIO_NUM_45" in config
    assert "MAX98357" not in source
    assert "MAX98357" not in header
    assert "DESKBOT_ROM_MAX98357" not in config


def test_gpio45_amplifier_is_woken_before_pcm_and_disabled_when_idle():
    source = _source()
    config = DESKBOT_CONFIG_H.read_text(encoding="utf-8")
    setup = _between(
        source,
        "bool speaker_setup_driver() {",
        "void speaker_recover_default",
    )
    prepare = _between(
        source, "bool speaker_amp_prepare_playback", "i2s_std_slot_config_t"
    )
    worker = source[source.index("void audio_play_task_main") :]
    begin = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16Begin:",
        "case AudioPlayJob::Kind::kStreamPcm16Chunk:",
    )
    end = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16End:",
        "case AudioPlayJob::Kind::kEmergencyFlush:",
    )

    assert "#define DESKBOT_SPEAKER_AMP_CTRL GPIO_NUM_45" in config
    assert "#define DESKBOT_SPEAKER_AMP_WAKE_MS 20u" in config
    assert "speaker_amp_disable();" in setup
    assert "digitalWrite(SPEAKER_AMP_CTRL, HIGH)" in prepare
    assert "vTaskDelay(pdMS_TO_TICKS(DESKBOT_SPEAKER_AMP_WAKE_MS))" in prepare
    assert begin.index("speaker_amp_prepare_playback()") < begin.index(
        "speaker_reconfigure(job.pcm_rate, job.pcm_channels)"
    ) < begin.index("s_stream_pcm_active = true")
    assert "uxQueueMessagesWaiting(s_audio_play_q) == 0u" in end
    assert "speaker_amp_disable();" in end


def test_speaker_stream_emits_one_aggregate_i2s_signal_report():
    source = _source()

    assert "struct AudioOutputStats" in source
    assert "audio_output_stats_add(output_stats, scratch, output_samples)" in source
    assert "mean_abs=%u rms=%u peak=%d non_silent_x1000=%u" in source
    assert "AudioPbTerminalState::kCompleted" in source


def test_removed_wav_and_sync_paths_stay_removed():
    source = _source()
    header = AUDIO_H.read_text(encoding="utf-8")

    for removed in (
        "audio_play_wav",
        "audio_play_wav_impl",
        "AudioPlayJob::Kind::kWav",
        "post_play_job_and_wait",
        "audio_stream_pcm16_begin",
        "audio_stream_pcm16_end",
    ):
        assert removed not in source
        assert removed not in header

def test_pb_end_and_begin_are_queue_only_and_have_no_global_waiter():
    source = _source()
    begin = _between(
        source, "bool audio_pb_stream_begin", "bool audio_pb_stream_push_owned"
    )
    end = _between(
        source, "bool audio_pb_stream_end", "bool audio_take_pb_terminal_event"
    )
    worker = source[source.index("void audio_play_task_main") :]
    worker_end = _between(
        worker,
        "case AudioPlayJob::Kind::kStreamPcm16End:",
        "case AudioPlayJob::Kind::kEmergencyFlush:",
    )

    assert "return enqueue_audio_play_job_strict(job)" in begin
    assert "return enqueue_audio_play_job_strict(j)" in end
    assert "xSemaphoreTake" not in begin
    assert "xSemaphoreTake" not in end
    assert "xSemaphoreGive" not in worker_end
    assert "ok_out" not in source

def test_pb_terminal_queue_loss_is_observable_with_epoch_identity():
    source = _source()
    header = AUDIO_H.read_text(encoding="utf-8")
    note_drop = _between(
        source, "static void audio_note_pb_terminal_drop", "static void audio_emit_pb_terminal"
    )
    emit = _between(
        source, "static void audio_emit_pb_terminal", "static bool audio_wait_for_pb_start"
    )

    assert note_drop.index("memory_order_release") < note_drop.index(
        "memory_order_relaxed"
    )
    assert emit.count("audio_note_pb_terminal_drop(job.pb_epoch)") == 2
    assert "audio_pb_terminal_drop_count()" in header
    assert "audio_pb_terminal_last_dropped_epoch()" in header
    assert "memory_order_acquire" in _between(
        source,
        "uint32_t audio_pb_terminal_drop_count()",
        "bool audio_stream_pcm16_push_owned",
    )
