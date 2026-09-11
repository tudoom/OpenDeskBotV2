# 小歪固件（Deskbot v2）

本目录是 Deskbot v2 自定义 ESP32-S3 主板的固件源码。当前版本见 `VERSION`
（单一版本来源，构建时由 `scripts/gen_version.py` 生成 `version_gen.h`）。仓库
仍保留 Seeed Studio XIAO ESP32S3 Sense 的兼容构建环境，但它不是产品默认目标。

所有机器人烧录完全相同的固件镜像。设备码由芯片出厂 eFuse base MAC 生成，
镜像中不写部署地址、PC 端凭证或任何每台设备的配置；同一镜像可连接任意一台
运行桌面客户端的电脑，业务数据始终保存在那台电脑本地。

## 日常升级：用桌面客户端一键烧录

桌面客户端随包附带与之匹配的应用固件（`service/src/deskbot_server/firmware_payload/deskbot_v2.bin`，
由 `service/scripts/sync_firmware_payload.py` 从构建产物同步，版本号取自本目录
的 `VERSION`）。控制台「固件」页会对比设备当前版本与随包版本，点一下即可经
USB 烧录，不需要安装任何开发工具。注意它只重写 0x10000 起的应用分区，前提是
机器已经在运行本固件；新板子、空片或 bootloader / 分区表损坏的机器必须按下面
「手动烧录」做一次全量烧录（GitHub Release 提供打包好的四个 bin）。

## 连接模型

机器人有两条链路，USB 始终优先：

- **USB**：通过 ESP32-S3 的 USB Serial/JTAG CDC 串口直连电脑，即插即用；
- **Wi-Fi（可选）**：在控制台「设备连接」页一键配网（`wifi_config`）后，机器人
  连入你的局域网，通过 Bonjour 服务 `_deskbot-core._tcp` 或 UDP 广播找到运行
  桌面客户端的那台电脑，再由机器人主动发起一条 TCP 连接回连核心服务。只有
  USB 上没有主机时才使用 Wi-Fi；插上有主机的 USB 线会切回 USB。

固件不访问互联网。所有云端访问（大模型、语音识别与合成、资源下载、时间同步）
都由电脑负责。

两条链路承载同一套 DBOT v1 协议：24 字节定长帧头、帧头与 payload CRC、递增
序号、随机 session epoch、hello / 心跳生命周期，并隔离以下通道：

- 控制 JSON；
- PB JSON 与 PB 二进制媒体（表情、动作、语音的时间线）；
- 麦克风 Opus 上行（按 3 × 20 ms = 60 ms 攒批为一帧）；
- 扬声器 Opus 下行；
- 相机 JPEG 上行；
- 设备日志帧。

电脑完成 hello 前，固件不会发送媒体或日志。心跳超时、epoch 不一致、坏帧或
连续关键写失败都会使当前 session 失效，并清理未完成的音频、PB 和录音。固件
自行拆除 session 且链路仍可写时，会先发一条 `session_end`，让电脑立即重连而
不必等心跳超时。

## 控制 JSON 命令面

舵机动作只经 PB `servo[]` 时间线执行，旧的手势命令层已删除。控制 JSON 只保留
查询、设置与维护类命令：

| 命令 | 作用 |
|---|---|
| `wifi_config` / `wifi_status_req` | 配网与查询 Wi-Fi 状态 |
| `mic_mute` / `mic_mute_req` | 静音开关（存在设备 NVS，重装电脑端不会重置） |
| `mic_uplink_mode` / `mic_uplink_mode_req` | 麦克风上行模式：`continuous`（默认，会话内全部上行）或 `vad`（设备端能量门控，只上行有声片段） |
| `thermal_cutoff` / `thermal_cutoff_req` | 温度断电阈值（默认 70 ℃，持续 30 s 触发，深睡 10 分钟后自恢复） |
| `servo_relax` / `servo_relax_req` | 动作停下 N 毫秒后释放舵机力矩 |
| `head_pos`、`task`、`reboot` / `restart`、`factory` | 位置查询、任务与 CPU 诊断转储、重启 |

hello 与心跳会上报固件版本、片内温度、麦克风上行模式与静音状态，供电脑端
展示与告警。

## 设备端保护

- **舵机**：梯形速度曲线、换向停顿、边缘余量、热预算冷却；到位后可释放力矩；
  俯仰下限 78°，避免顶到机械限位导致 USB 过流掉线。
- **温度**：片内温度持续超过阈值 30 s 即深睡 10 分钟，阈值可在控制台「参数设置」调整。
- **相机**：初始化在独立任务中进行并有 8 s 上限，掉电后相机卡死不再拖住启动；
  启动里程碑写入 NVS 黑匣子，配合启动看门狗定位卡死点。新批次机器使用 OV3660
  传感器，固件自动适配。
- **内存**：大块任务栈放 PSRAM，内部 RAM 预算见 [`docs/MEMORY_BUDGET.md`](docs/MEMORY_BUDGET.md)。

## 编译

安装 PlatformIO 后执行：

```bash
cd hardware
pio run -e deskbot_v2
```

`deskbot_v2` 环境固定 `ARDUINO_USB_CDC_ON_BOOT=1`，并以 `ARDUINO_USB_MODE=1`
使用 ESP32-S3 的 USB Serial/JTAG CDC。需要 XIAO 兼容构建时显式指定
`-e seeed_xiao_esp32s3`。

ESP-SR WebRTC AGC 的固定压缩增益可用构建宏
`-DDESKBOT_AFE_AGC_COMPRESSION_GAIN_DB=<n>`（默认 15 dB）标定，用于覆盖自适应
级尚未收敛的冷启动窗口，保证连接后的首几句话电平足以转写。

改动固件后，先更新 `VERSION`，再在仓库根目录运行
`python service/scripts/sync_firmware_payload.py`，把构建产物同步进桌面客户端的
随包固件，保证用户一键烧录到的就是这一版。

## USB 枚举与诊断

运行态预期枚举为 Espressif `303A:1001`，即 ESP32-S3 硬件 USB Serial/JTAG 端点；
`2886:0056` 属于另一套 TinyUSB device 模式，本固件不使用。

硬复位或烧录后的复位会使 COM / tty 短暂消失，等待 Windows、macOS 或 Linux
重新枚举属于正常现象。排查时区分两种状态：

- 完全没有 COM / tty：优先检查枚举、线材与供电（只充电的线没有数据脚）、驱动、
  重启循环或无效镜像；
- COM / tty 已存在但控制台仍显示未连接：这是 DBOT hello / session 问题，先看
  电脑端日志，不要立即擦除或重复烧录。

## 手动烧录与看日志

烧录前请先退出桌面客户端，释放串口。

不想编译时，直接用 GitHub Release 里的固件包（`OpenDeskBotV2-firmware-<版本>.zip`，
含 bootloader / partitions / boot_app0 / firmware 四个 bin）配合 esptool 全量烧录：

```bash
esptool.py --chip esp32s3 --port <串口> erase_flash
esptool.py --chip esp32s3 --port <串口> write_flash 0x0 bootloader.bin 0x8000 partitions.bin 0xe000 boot_app0.bin 0x10000 firmware.bin
```

自己编译后，Linux 或 macOS：

```bash
cd hardware
./flash_rom.sh build
./flash_rom.sh upload /dev/ttyACM0
./flash_rom.sh log /dev/ttyACM0
```

Windows 使用对应的 PlatformIO upload 命令。设备若曾运行更早的固件，第一次安装
本版本前应整片擦除，避免旧 NVS 数据继续保留。

烧录完成后保持 USB 连接并打开桌面客户端。未连接电脑时屏幕显示待机屏：内建
默认脸加「请连接PC服务」文案；hello 成功后待机屏清除，屏幕交给电脑端的表情
系统，session 结束时恢复。待机屏只描画像素，不写入 PB 表情状态、插值基线和
显示 CRC。

## 分区

8 MiB 分区只保留 NVS、单个 factory app、coredump 和 FFat，不设备用应用槽或
更新元数据；固件更新一律经 USB 整包烧录。

## 诊断工程

`diagnostics/v2_display_probe` 与 `diagnostics/v2_hardware_probe` 是两个独立的
PlatformIO 小工程，用于新板子点亮屏幕和逐项检查外设，不属于产品固件。

## 参考

- PB payload 约定：[`service/docs/esp32_pb_protocol.md`](../service/docs/esp32_pb_protocol.md)
- 电脑端串口与 Wi-Fi 链路实现：`service/src/deskbot_server/infrastructure/serial/`、`service/src/deskbot_server/infrastructure/net/`
- 固件内存预算：[`docs/MEMORY_BUDGET.md`](docs/MEMORY_BUDGET.md)
