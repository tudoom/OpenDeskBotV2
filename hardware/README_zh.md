# Deskbot 固件

本目录是 Deskbot v2 ESP32-S3 自定义板的纯 USB 固件。仓库仍保留 Seeed Studio
XIAO ESP32S3 Sense 兼容环境，但它不是产品默认目标。

所有机器人烧录完全相同的固件镜像。设备码由芯片出厂 eFuse base MAC
生成，镜像中不写部署地址、PC 服务凭证或每设备配置。

同一镜像可连接任意兼容的自托管 PC 服务。业务数据始终保存在所连接 PC 的
唯一 `data/local/` 工作区，不需要为某台 PC 服务重新编译固件。

## 连接模型

机器人只通过 USB CDC 串口连接本机 PC 服务，所有外部服务访问都由 PC
负责，固件不会自行建立网络连接。

DBOT v1 串口协议包含 24 字节定长帧头、帧头和 payload CRC、递增序号、
随机 session epoch、hello/心跳生命周期，并隔离以下通道：

- 控制 JSON；
- PB JSON 与 PB 二进制媒体；
- 麦克风 Opus 上行（按 3×20ms=60ms 攒批为一个 USB 帧）；
- 扬声器 Opus 下行；
- 相机 JPEG 上行；
- 设备日志帧。

PC 完成 hello 前，固件不会发送媒体或日志。心跳超时、epoch 不一致、坏帧
或连续关键写失败都会使当前 session 失效，并清理未完成的音频、PB 和录音。
固件自行拆除 session 且链路仍可写时，会主动发送 `session_end` 告知帧，
让主机立即重连而不必等心跳超时。

控制 JSON 的本地命令面只保留只读查询与维护命令：`head_pos`（位置查询）、
`task`（任务/CPU 诊断转储）、`reboot`/`restart`（整机重启）。旧的
factory/action 手势命令层已删除，舵机动作只经 PB `servo[]` 时间线执行。

## 编译

安装 PlatformIO 后执行：

```bash
cd hardware
pio run -e deskbot_v2
```

固定的 ESP32-S3 board 配置会设置
`ARDUINO_USB_CDC_ON_BOOT=1`，并以 `ARDUINO_USB_MODE=1` 使用 ESP32-S3
USB Serial/JTAG CDC。需要兼容 XIAO 时必须显式指定
`-e seeed_xiao_esp32s3`。

ESP-SR WebRTC AGC 的固定压缩增益可用构建宏
`-DDESKBOT_AFE_AGC_COMPRESSION_GAIN_DB=<n>`（默认 15 dB）标定，用于覆盖
自适应级尚未收敛的冷启动窗口，保证连接后的首几句话电平足以转写。

## USB 枚举与诊断

运行态预期枚举为 Espressif `303A:1001`，即 ESP32-S3 硬件 USB
Serial/JTAG 端点。`2886:0056` 属于另一套 TinyUSB device 模式，本固件不使用。

硬复位或烧录后的复位会使 COM/tty 短暂消失，等待 Windows、macOS 或 Linux
重新枚举属于正常现象。排查时应区分两种状态：

- 完全没有 COM/tty：优先检查枚举、线材/供电、驱动、重启循环或无效镜像；
- COM/tty 已存在但服务仍显示未连接：这是 DBOT hello/session 问题，应先查看
  PC 服务日志，不要立即擦除或重复烧录。

## 烧录与日志

Linux 或 macOS：

```bash
cd hardware
./flash_rom.sh build
./flash_rom.sh upload /dev/ttyACM0
./flash_rom.sh log /dev/ttyACM0
```

Windows 使用对应的 PlatformIO upload 命令。设备若曾运行旧固件，第一次安装
纯 USB 版本前应整片擦除，避免旧 NVS 数据继续保留。

烧录完成后保持 USB 连接并启动 PC 服务。未连接 PC 服务时屏幕显示待机屏：
内建默认脸加「请连接PC服务」文案；DBOT hello 成功后待机屏清除，屏幕交还
PC 端表情系统，session 结束时恢复。待机屏只描画像素，不写入 PB 表情状态、
插值基线和显示 CRC。

## 分区

8 MiB 分区仅保留 NVS、单个 factory app、coredump 和 FFat，不保留备用
应用槽或更新元数据。

PB payload 约定见 `service/docs/esp32_pb_protocol.md`；PC 端串口实现见
`service/src/deskbot_server/infrastructure/serial/`。
