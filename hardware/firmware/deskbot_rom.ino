// Deskbot — XIAO ESP32S3 Sense. USB CDC is the primary PC/device data link;
// a provisioned WiFi TCP link takes over whenever no USB host is talking.
#include <Arduino.h>
#include <esp_heap_caps.h>
#include <new>

#include "asr_chat_client.h"
#include "audio_capture.h"
#include "audio_frontend_esp_sr.h"
#include "audio_player.h"
#include "camera.h"
#include "cmd.h"
#include "common.h"
#include "deskbot_config.h"
#include "display.h"
#include "display_panel.h"
#include "head.h"
#include "mic_uplink_policy.h"
#include "thermal_cutoff.h"
#include "rtc_audio_downlink.h"
#include "runtime_supervisor.h"
#include "task_trace.h"
#include "usb_transport.h"
#include "wifi_link.h"

/*
 * Arduino-ESP32's prebuilt loop task defaults to 8 KiB.  Override its weak
 * accessor in sketch code; sdkconfig.defaults does not rebuild that library.
 */
SET_LOOP_TASK_STACK_SIZE(24576);

/* 实例定义在 asr_chat_client.cpp（放 PSRAM）；.ino 预处理器会被 placement
 * new 语法带偏，这里只留引用声明。 */
extern AsrChatClient& asrChatClient;

unsigned long loop_start_time = 0;

namespace {

bool s_camera_ok = false;

void on_usb_frame(const DeskbotUsbRxFrame& frame, void*) {
  asrChatClient.dispatchUsbFrame(frame);
  if (frame.channel == DESKBOT_USB_CONTROL_JSON &&
      frame.payload != nullptr && frame.payload_length > 0) {
    /* WiFi provisioning/status commands are transport-plane, not factory
     * commands; they reply inline (same pattern as heartbeat_ack) and the
     * actual reconnect runs later in wifi_link_poll(). */
    if (wifi_link_handle_control_json(frame.payload, frame.payload_length)) {
      return;
    }
    /* 舵机空闲释放开关也是传输面的小设置：内联回执，不进工厂命令邮箱。 */
    if (head_handle_control_json(frame.payload, frame.payload_length)) {
      return;
    }
    if (mic_uplink_handle_control_json(frame.payload, frame.payload_length)) {
      return;
    }
    if (thermal_cutoff_handle_control_json(frame.payload, frame.payload_length)) {
      return;
    }
    /*
     * Keep the local factory JSON surface on CONTROL_JSON. Normal PB JSON
     * uses PB_WIRE|JSON and is handled only by AsrChatClient.  handle_cmd()
     * only parses and queues into the command mailbox here: commands run
     * from loop() after usb_transport_poll() returns, never inside frame
     * dispatch while the parser is parked mid-frame.  (The gesture/motion
     * command layer was removed; the surviving factory commands are
     * head_pos / task / reboot.)
     */
    String command(reinterpret_cast<const char*>(frame.payload),
                   frame.payload_length);
    handle_cmd(command);
  }
}

void on_usb_link(bool ready, uint32_t session_epoch, void*) {
  if (!ready) {
    camera_reset_session_state();
    /* Commands queued by the dead session must not execute in a later one. */
    cmd_mailbox_clear("usb link down");
  }
  asrChatClient.onUsbLinkState(ready, session_epoch);
  if (ready) {
    log_info("[USB] service attached epoch=%u device=%s",
             static_cast<unsigned>(session_epoch), get_device_id());
  } else {
    log_warn("[USB] service detached; outputs stopped");
  }
}

}  // namespace

void setup() {
  const bool preferred_cdc_buffers = usb_transport_cdc_begin();
  /* ≥4 KiB 的普通 malloc 自动落 PSRAM（默认阈值 16 KiB）：把稀缺的内部
   * DRAM 留给 DMA 缓冲、任务栈和 WiFi 驱动。显式 heap_caps 调用不受影响。 */
  heap_caps_malloc_extmem_enable(2048);
  /* WiFi 驱动初始化需要约 44 KiB 内部堆，必须赶在音频/相机等大户分配之前；
   * 已配网的设备在这里入网，未配网的零成本跳过。 */
  wifi_link_setup();
  /*
   * Enumeration proceeds asynchronously while the remaining peripherals
   * start.  This short delay helps Windows observe the endpoint without
   * penalising every boot; early host bytes are retained by the 4 KiB RX
   * queue.  Never block startup waiting for a host.
   */
  delay(250);
  log_set_level(LOG_LEVEL_INFO);
  log_info("Initializing Deskbot USB-only firmware...");
  if (!preferred_cdc_buffers) {
    log_warn("[USB] preferred 4 KiB CDC queues unavailable; using fallback");
  }

  if (!device_id_available()) {
    log_error("[BOOT] hardware device identity unavailable; startup halted");
    for (;;) {
      delay(1000);
    }
  }
  log_info("[BOOT] device_id=%s", get_device_id());

  /* Reserve LCD_CAM/GDMA resources while internal RAM is still contiguous.
   * Framebuffers live in PSRAM, but the driver still needs internal control,
   * descriptor, ISR and queue allocations.  Initialising after ESP-SR and the
   * microphone workers made those allocations depend on heap fragmentation. */
  s_camera_ok = setup_camera();

  setup_display();
  setup_FFat();
  setup_led();
  setup_head();
  mic_uplink_load_prefs();
  thermal_cutoff_load_prefs();

  setup_audio();
  /* WiFi 数据面（动态收发缓冲、lwIP 段）与 ESP-SR AFE 在内部 SRAM 里放不下
   * 同一台机器：满载后堆只剩 ~2KB，收包分配静默失败，无线会话必死。配过
   * 网的设备开机让出 AFE（走既有 raw-mic 上行回退，PC 侧照常处理），未配
   * 网则维持原有 AEC/NS/VAD 行为不变。清除配网即可恢复。 */
  if (wifi_link_boot_configured()) {
    log_warn(
        "[BOOT] WiFi provisioned: skip ESP-SR AFE, RAM reserved for radio "
        "(raw-mic uplink)");
  } else if (!audio_frontend_setup()) {
    log_warn(
        "[BOOT] ESP-SR AEC/NS/VAD unavailable; retaining stable raw-mic mode");
  }
  task_setup_mic_capture();

#if DESKBOT_CAMERA_DIAGNOSTICS
  const bool camera_first_frame_before_servo =
      s_camera_ok &&
      camera_boot_probe(CameraBootProbeStage::kBeforeServo);
  /*
   * Require a second returned-and-reacquired framebuffer before servo attach.
   * This distinguishes a one-shot DMA/queue failure from a peripheral change
   * caused by attaching the servos.
   */
  const bool camera_frame_before_servo =
      camera_first_frame_before_servo &&
      camera_boot_probe(CameraBootProbeStage::kBeforeServo);
  head_servo_boot_attach();
  /*
   * If capture already failed before servo attach, a second 4 s driver
   * timeout cannot implicate the servo. Skip it so failed hardware still
   * reaches the USB diagnostic session promptly.
   */
  if (s_camera_ok && camera_frame_before_servo) {
    (void)camera_boot_probe(CameraBootProbeStage::kAfterServo);
  }
#else
  head_servo_boot_attach();
#endif
  display_backlight_on();
  if (!s_camera_ok) {
    log_warn("[BOOT] Camera absent or failed; continuing without camera");
    display_boot_show("Camera unavailable", "Continuing startup");
  }

  task_setup_audio_play();
  audio_play_v2_startup_self_test();
  if (!rtc_audio_downlink_begin()) {
    log_warn(
        "[BOOT] RTC Opus worker unavailable; full-duplex capability disabled");
  }
  task_setup_display();
  task_setup_cpu_runtime_stats();

  asrChatClient.enableUsbTransport();
  if (!usb_transport_begin(on_usb_frame, on_usb_link, nullptr)) {
    log_error("[BOOT] USB transport init failed");
    /* USB 永远起不来：撤销待机屏，避免 display worker 把致命错误提示覆盖掉。 */
    display_standby_set(false);
    display_boot_show("USB init failed", "Please reboot device");
    return;
  }
  if (!runtime_supervisor_start()) {
    log_error("[BOOT] runtime supervisor unavailable");
  }
  /* The camera task is also its supervisor. Start it after every initial
   * setup result so transient SCCB/PSRAM/power failures recover without a
   * manual reboot. */
  task_setup_camera();
  log_info("[BOOT] firmware=%s %s %s", VERSION, __DATE__, __TIME__);
  log_info("[BOOT] device_id=%s transport=usb_cdc protocol=%u",
           get_device_id(),
           static_cast<unsigned>(DESKBOT_USB_PROTOCOL_VERSION));
  log_info("PSRAM size=%u free=%u", static_cast<unsigned>(ESP.getPsramSize()),
           static_cast<unsigned>(ESP.getFreePsram()));
  log_warn("[BOOT] Link Lite ready device=%s camera=%s",
           get_device_id(), s_camera_ok ? "present" : "absent");
  loop_start_time = millis();
}

void loop() {
  usb_transport_poll();
  wifi_link_poll();
  thermal_cutoff_tick();
  /* Deferred CONTROL_JSON factory commands execute only here, after the
   * parser has fully released its frame; see the cmd.cpp mailbox notes. */
  cmd_mailbox_service();
  asrChatClient.serviceLoop();
  log_task_tick();
  log_stack_heap_tick();
  if (!usb_transport_is_active()) {
    delay(5);
    return;
  }
  if (!asrChatClient.runVoiceRound(RECORD_TIME)) {
    log_error("[CHAT] USB voice round failed; awaiting healthy session");
    blink_led(COLOR_RED, 2);
    delay(100);
  }
}
