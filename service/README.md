# opendesk-service

Open Desk Bot V2 机器人的 PC 服务。机器人通过 USB CDC 上传麦克风 Opus 与相机
JPEG；设备端 ESP-SR 完成 **AEC / NS / VAD**，PC 通过 LiveKit RTC 完成
**Seed ASR → 通用大模型/tools → Seed TTS**（大模型经 OpenAI 兼容接口接入，
控制台提供 DeepSeek / 豆包 / Xiaomi MiMo 预设，也可自定义），再通过同一 USB
链路下发音频、表情和显式设备动作。

机器人本身不连接 Wi-Fi 或云服务器。联网、供应商 API、资源下载和时间同步都由
PC 负责。

**License:** [GPL-3.0](LICENSE)

## 系统边界

```text
统一固件机器人
  runtime device_id = "deskbot_" + eFuse base MAC
          │
          │ USB CDC / DBOT v1
          ▼
核心进程 :9000（唯一串口进程）
  ├─ USB 自动发现、握手、心跳、重连
  ├─ LiveKit RTC 音频桥、Core 工具桥、vision / PB
  ├─ 供 :5050 调用的内部 HTTP API
  └─ 浏览器调试订阅：camera_view、device_pipeline subscriber
          ▲
          │ X-API-Key（本机进程间凭证）
Web 控制台 :5050（不打开串口）
```

设备生产者 WebSocket 已关闭。`/asr_chat`、`/camera`、`/camera_uplink` 和
`/device_pipeline` 生产者连接会返回 `usb_cdc_required`。保留的 WebSocket 仅用于
浏览器调试订阅或第三方供应商协议。

## 安装与启动

推荐 Ubuntu 22.04/24.04 或 macOS；Windows 可使用等价 Python 环境。需要
Python 3.11、Opus、ffmpeg 和首次安装时的网络访问。

```bash
cd service
cp .env.example .env
# 至少填写独立的 ASR_API_KEY 与所选 LLM/TTS 供应商凭证。
chmod +x start.sh
./start.sh
```

首次启动会创建虚拟环境、安装锁定的默认依赖并下载人脸关键点与 Silero VAD 等
模型（curl 自动重试并可做 SHA-256 校验）。注意默认依赖已不含人脸视觉栈
（mediapipe/opencv/insightface 移入 optional extra `face`，需要时
`pip install -e '.[face]'`；缺栈时人脸功能明确降级，相机预览/拍照不受影响，
下载下来的人脸模型只在装了 `face` extra 后才会被用到）；不会安装 FunASR/torch，也不会
下载或导出 SenseVoice。网络受限时模型下载失败不会卡死安装：脚本给出警告并在
下次启动自动重下，可设置镜像环境变量 `DESKBOT_FACE_MODEL_URL` /
`DESKBOT_SILERO_VAD_URL`（以及对应的 `*_SHA256` 期望值做强校验）指向国内镜像。
后续可使用：

```bash
SKIP_SETUP=1 ./start.sh
# 或
FAST_START=1 ./start.sh
```

| 进程 | 默认地址 | 职责 |
|------|----------|------|
| Web 控制台 | `http://127.0.0.1:5050/` | 本机控制台、USB 连接状态、本地任务、设置和调试 |
| 核心服务 | `http://127.0.0.1:9000/` | 串口、对话流水线、内部 HTTP、浏览器订阅 WS |
| LiveKit Server | `http://127.0.0.1:7880/` | 本机 RTC 信令和媒体；由 Core 自动启动、监测和停止 |

Windows 首次运行前执行 `powershell -ExecutionPolicy Bypass -File tools/install_livekit_windows.ps1`。
安装器锁定并校验官方 LiveKit Server；运行时密钥和配置保存在忽略提交的
`data/local/livekit/`，不需要 nginx 或 cloud-token-service。信令和 ICE 媒体均只走
本机进程；Windows 下 ICE 使用本机网卡候选完成进程间配对。

核心进程必须独占机器人串口。不要同时运行串口监视器、烧录器或第二个核心服务。

### Windows 桌面客户端（推荐给普通用户）

不想手动跑脚本的 Windows 用户可以使用一体化桌面客户端：
`client\Build-Client.ps1` 会把 Core、Web 前端、LiveKit Server、RTC Agent、便携
Python 和必需模型打包成单个 `OpenDeskBotV2.exe`（需要 WebView2 Runtime）。
**默认构建不含人脸视觉栈**（mediapipe/opencv/insightface 及其专属传递依赖和
mediapipe 人脸模型都不进包；`onnxruntime` 因语音 VAD 依赖保留）：相机预览与
拍照问答照常可用，人脸检测/识别/注册明确降级为「功能未安装」。需要带人脸
功能的安装包用 `-IncludeFaceStack` 构建；pip 安装侧对应
`pip install -e '.[face]'`。双击
即自动启动全部服务并在自带窗口打开控制台；首次启动缺少 LLM Key 时直接打开
大模型设置页。所有子进程放在同一个 Windows Job Object 中，客户端退出不会遗留
服务；停止/重启先给 Core 15 秒优雅关闭窗口（CTRL_BREAK），超时才硬杀。配置、
数据库与日志保存在 `%LOCALAPPDATA%\OpenDeskBotV2`，升级 EXE 不覆盖。详见
[client/README.md](client/README.md)。

## 首次使用

1. 给所有机器人烧录同一份 USB-only 固件。
2. 启动 PC 服务并打开 `http://127.0.0.1:5050/`；页面直接进入本机控制台。
3. 用 USB 数据线连接机器人；`:9000` 自动扫描端口并完成 DBOT 握手。
4. 握手成功的连接自动出现在“USB 设备”页。单台设备自动选中，多台设备可切换当前控制目标。
5. 现在即可使用对话、提醒、记忆、人脸、偏好和调试功能。

Core 只接受真实完成 DBOT hello 的 live `usb_cdc` 会话，浏览器提交字符串不能伪造
连接。`device_id` 只用于 USB/RTC/ACK 路由和诊断，不参与业务数据路径。设备不需要
配对码、制造密钥或长期 token。

一台 PC 只有一份 `data/local/`。所有兼容机器人接入后共享这份表情、会话、记忆和
设置；拔插或换一台机器人不会创建另一套数据，真正实现即插即用。

## PC 与外部服务

PC 可按配置访问：

- 火山 Seed ASR 流式识别；
- 通用大模型（OpenAI 兼容接口；DeepSeek / 豆包 / Xiaomi MiMo 预设或自定义）；
- 火山 Seed TTS 流式合成；
- 受安全策略保护的联网搜索和资源下载；
- PC 系统时间。

供应商 API Key、HTTPS/WSS endpoint 和 CA 信任只存在 PC 侧。Web→Core 使用本机
进程间 API Key（`data/.free_api_key`，`X-API-Key` 请求头，附带每日字节配额），
凭证不下发到固件。它不表示用户身份，也不是外部控制凭据。

### LLM 配置

对话走通用大模型的 OpenAI 兼容接口，全部调用路径固定关闭深度思考。控制台
「通用大模型」卡片提供 DeepSeek / 豆包（火山方舟）/ Xiaomi MiMo 三个预设——
选择后自动填入推荐模型与 base URL（并给出各家 Key 控制台链接），也可自定义。
出厂默认为 MiMo `mimo-v2.5`。在 Web 控制台填写，或在 `.env` 设置：

```dotenv
LLM_API_KEY=<供应商 API Key>
# LLM_PROTOCOL=openai
# LLM_MODEL=mimo-v2.5
# LLM_BASE_URL=https://api.xiaomimimo.com/v1
```

旧版火山方舟安装仍兼容 `ARK_API_KEY` / `ARK_MODEL` / `ARK_BASE_URL`（新安装不要
填写）；独立的 `ark_api_key` 输入只用于图片生成表情凭证。LLM Key 只用于 LLM，
不与 ASR 或 TTS 共用。涉及当前画面的提问会触发一次新鲜 `camera_once`：JPEG 仅以
内存图片块进入同一个 RTC 会话，不进入日志或持久会话，也不会触发人脸跟随。Web
管理界面留空 Key 表示保留现有值，不会覆盖已经保存的运行时配置。

### ASR 配置

默认 `ASR_PROVIDER=doubao_streaming`。PC 将 USB 上行的 s16le 音频通过火山
豆包大模型 ASR 的 WSS 流式协议发送，设备不持有云端凭证。注意 ASR 是双轨的：
**设备实时语音固定使用豆包流式（RTC 内硬编码），不随 `ASR_PROVIDER` 切换**；
该选项只影响连通性测试与文本兼容路径。最小配置：

```dotenv
ASR_PROVIDER=doubao_streaming
ASR_API_KEY=<新版火山语音 API Key>
# VOLCENGINE_ASR_RESOURCE_ID=volc.seedasr.sauc.duration
# VOLCENGINE_ASR_ENDPOINT=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async
ASR_LANGUAGE=zh-CN
```

`ASR_API_KEY` 与 `LLM_API_KEY`、豆包 TTS Key 完全独立，不会互相复用。显式选择
`openai_compatible` 后仍可配置 `ASR_ENDPOINT` 与 `ASR_MODEL`，已有配置不会被迁移或覆盖。可在
“高级设置 → 语音识别 ASR”保存并测试；接口不回显 Key，Key 输入留空表示保留，
只有“清除 ASR Key”会删除。`:5050` 以文件锁和原子替换写入 `.env`，`:9000`
在下一轮语音前检查文件变化并加载，不需要重启。

未配置 Key 不会阻止服务启动；用户首次说话时会收到可操作的 `asr_not_configured`
错误，设备随后恢复监听，不会把配置错误当成空识别。endpoint 默认必须为公网
HTTPS，回环、内网、链路本地和云元数据地址会被拒绝。

只有明确需要完全离线识别时才启用本地回退：

```bash
pip install -e '.[local-asr]'
# 自行准备 SenseVoice 模型后，在 .env 设置：
# ASR_PROVIDER=funasr
# ASR_MODEL_DIR=./models/SenseVoiceSmall
```

本地依赖与模型不属于默认安装，运行时也不会自动下载或导出模型。

## USB 数据链路

DBOT v1 使用 CRC 保护的 24 字节帧头、payload CRC、sequence、session epoch、
`client_nonce` 幂等握手、心跳和流重同步。主要通道：

- CONTROL JSON：握手、状态、flush、取消和控制；
- PB JSON / PB binary：PB v2.1、音频和 assets；
- 麦克风 Opus 上行；
- 扬声器 Opus/PB 音频下行；
- 相机 JPEG 上行；
- 帧化设备日志。

握手完成前不接受媒体。断线、epoch 变化或协议错误会清理当前未完成状态；服务会
自动扫描并重连。完整约定见
[docs/esp32_pb_protocol.md](docs/esp32_pb_protocol.md)。

麦克风上行的权限矩阵（何时“听得见”）：

- 固件允许采集需同时满足：PC 的 `mic=open/mute` 提示为 open、USB transport
  就绪，且——在**未协商成 `esp32_aec` 全双工**时——扬声器不在发声、也不在
  播放后的尾音抑制窗口内。协商成 `esp32_aec`（AEC/NS/ESP-SR VAD/全双工能力
  齐备）后，RTC 播放期间麦克风保持打开，回声由设备 AEC 消除，支持插话打断。
- 传统 PB 播放（TTS、提醒等携带音频二进制的下发）与 RTC 模式无关：PC 在首个
  媒体声明前显式下发 `mic=mute` 半双工屏障并等待固件确认，播放链发送完成后再
  `mic=open` 恢复（恢复不被确认会重试并记录）。纯 JSON PB（仅表情/舵机）不
  触碰麦克风。

## Web 控制台功能

| 功能 | 说明 |
|------|------|
| USB 设备 | 握手后自动发现；单设备自动选中，多设备切换当前运行时目标 |
| 我的提醒 | 一次性与 cron 提醒，含租约、崩溃恢复和离线策略 |
| 会话中心 | 本机唯一 `local` 会话的查看、清空和导出 |
| 互动偏好 | 主动行为、勿扰、离线提醒与音色 |
| 记忆与人物 | `data/local/` 中共享的长期记忆和人脸档案 |
| 用量 | 这台 PC 的供应商调用汇总 |
| 调试台 | 相机预览、流水线事件、TTS/LLM、PB/舵机控制 |

设备控制 API 返回 `202` 只表示请求已持久化；只有设备回传终态 `played` ACK，
操作状态变为 `completed` 后，UI 才应显示成功。

## 浏览器调试 WebSocket

保留两个只读/观察用途的订阅：

- `/camera_view?device_id=<id>`
- `/device_pipeline?role=subscriber&device_id=<id>`

订阅要求目标是已完成本机 USB 握手的 live session，并使用控制台自动签发的短期 debug token。
浏览器通过 `deskbot.debug.v1` 与 `deskbot.debug.auth.<token>` 子协议发送 token，
不把 token 放进 URL。

## 本机部署边界

- `:5050`、`:9000` 与 RTC Agent 辅助端口必须只监听 loopback；非回环
  `DESKBOT_WEB_HOST` 会直接拒绝启动。
- 设置至少 32 字符随机 `DESKBOT_WEB_SECRET_KEY`，仅用于 Web→Core 与调试短期令牌签名。
- 当前控制台不具备远程访问认证边界，不得通过反向代理、端口映射或局域网地址暴露。
- 不记录供应商凭据、完整查询串或 WebSocket 子协议中的 token。

设备通过本机 USB 连接，不经公网反向代理；供应商 HTTPS/WSS 只是 PC 的出站连接。

## 数据目录

| 路径 | 内容 |
|------|------|
| `data/opendesk.db` | 本机提醒、播放回执、工具/控制操作、复刻任务与汇总用量 |
| `data/local/` | 这台 PC 唯一的会话、记忆、人脸、表情、偏好和服务配置 |
| `data/global/` | 只读系统模板与默认资源；用于初始化 `data/local/` |

## 测试与诊断

```bash
source .venv/bin/activate
ruff check src tools tests
PYTHONPATH=src pytest tests -q
```

只读 USB 服务诊断：

```bash
python tools/network_connectivity_test.py --device-id deskbot_e83dc1faf074
```

这里的 `device_id` 只选择 live USB 连接。该工具不打开串口，只查询 Core 已建立的
真实会话。需要真机动作验收时可显式
增加 `--control-rounds 1`。

固件编译和烧录见 [`../hardware/README_zh.md`](../hardware/README_zh.md)。
当前文档不声明 Web 控制台具备固件升级能力。

## 文档

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/SERVER.md](docs/SERVER.md)
- [docs/api_interfaces.md](docs/api_interfaces.md)
- [docs/esp32_pb_protocol.md](docs/esp32_pb_protocol.md)
- [SECURITY.md](SECURITY.md)
- [CONTRIBUTING.md](CONTRIBUTING.md)
