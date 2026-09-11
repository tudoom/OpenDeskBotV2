# 固件内部 RAM 预算表（ESP32-S3，新批次 OV3660 机器实测）

任何新固件功能进入前先过这张表；数字来自设备黑匣子的 `mem@*` 事件
（`free` = 内部 8bit 堆空闲，`largest` = 最大连续块）。

## 已知基线（2026-09-02，固件 0.0.27）

| 阶段 | free | largest | 说明 |
| --- | ---: | ---: | --- |
| 开机后、USB 会话、射频关 | ~137 KB | ~59 KB | `radio off (usb host)` 之后 |
| WiFi 绑定 + 语音会话满载 | ~72 KB | ~31.7 KB | `mem@ack` / `mem@wifi` 采样 |
| WiFi TCP 刚建立 | ~72 KB | ~31.7 KB | `mem@tcp` |

`esp_wifi_init` 需要几十 KB 连续块（static_rx 16 × ~1.6 KB 预占 + 动态收缓冲按需），
所以 **配了 WiFi 的机器开机即放弃板载 ESP-SR AFE**（deskbot_rom.ino）。

## 固定占用（源码常量）

| 任务 | 栈 | 文件 |
| --- | ---: | --- |
| display_render | 32 KB（PSRAM 栈） | display.cpp |
| audio_capture (mic_cap) | 8 KB（PSRAM 栈，0.0.47 起） | audio_capture.cpp |
| AFE feed / fetch | 8 KB × 2（PSRAM 栈，0.0.47 起） | audio_frontend_esp_sr.cpp（配网机器不启动） |
| audio_play | 8 KB（PSRAM 栈） | audio_player.cpp |
| opus_enc / opus_dec / rtc_opus_dec | 24 / 32 / 32 KB（PSRAM 栈，按需创建） | opus_uplink.cpp / opus_downlink.cpp / rtc_audio_downlink.cpp |
| camera_usb | 4 KB | camera.cpp |
| runtime_supervisor | 4 KB | runtime_supervisor.cpp |
| idle_headroom | 3 KB | task_trace.cpp |
| loopTask | 24 KB | deskbot_rom.ino（SET_LOOP_TASK_STACK_SIZE） |

"PSRAM 栈"经 `deskbot_task_create_pinned()`（deskbot_task.h）创建：栈在 PSRAM、
TCB 在内部 RAM，分配失败回落内部堆（前提 `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY`
预编译 SDK 已开启，需求登记在 `firmware/sdkconfig.defaults`，纯 arduino 构建不接线）。**只允许 ≥ 8 KB 且从不写 flash/NVS 的任务**
使用（flash 写入期间 cache 关闭，写入者的栈必须在内部 RAM）；loopTask 写 NVS，
motor/camera_usb/runtime_guard/idle_headroom 栈小，都留在内部 RAM。

WiFi 驱动：`static_rx_buf_num=16`、`dynamic_rx_buf_num=40`、`static_tx_buf_num=8`
（wifi_link.cpp `lean_wifi_init`）。相机驱动重建门槛：最大连续块 ≥ 40 KB
（运行期）/ ≥ 24 KB（开机），见 camera.cpp。

## 规则

1. 新增静态缓冲 ≥ 1 KB 必须写进本表并说明放 PSRAM 还是内部 RAM。
2. 收包主循环（loopTask）上不得阻塞式分配。
3. 每次固件发版前对照一次 `mem@wifi` 采样，largest 低于 28 KB 视为回归。
