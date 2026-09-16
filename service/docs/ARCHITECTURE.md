# deskbot-server 架构

## 总览

```text
┌────────────── Robot ──────────────┐
│ identical firmware               │
│ runtime device_id from eFuse MAC │
│ mic / camera / display / servos  │
└──────────── USB CDC ──────────────┘
                    │ DBOT v1
                    ▼
┌──────────── Core process :9000 ─────────────┐
│ SerialManager（唯一串口进程）               │
│ DeviceSession + SerialServiceBridge         │
│ RTC audio bridge + authenticated tool bridge│
│ vision / expressions / reminders / state    │
│ internal HTTP API                           │
│ browser subscriber WebSockets               │
└───────────────┬──────────────────┬───────────┘
                │ internal HTTP    │ provider HTTPS/WSS
                ▼                  ▼
┌──── Flask Web :5050 ────┐   ASR / LLM / TTS / resources
│ local console / device UI│
│ never opens serial      │
└─────────────────────────┘
```

机器人没有 Wi-Fi、SoftAP、NTP、设备 WSS、无线 OTA 或云凭证。PC 是唯一联网主体，
负责供应商访问、资源、时间与这台 PC 的唯一一份本地配置。

## 本地数据与运行时设备标识

- 一台 PC 只有一个可写业务数据空间：`data/local/`。
- `data/global/` 只保存系统内置模板；首次需要时复制到 `data/local/`，运行期不回写模板。
- 所有机器人使用相同固件，任意兼容机器人接入这台 PC 后都会读取同一份本地配置、表情、记忆和会话。
- 固件仍从 eFuse base MAC 派生 `device_id`（`deskbot_<12 hex>`），但它只标识当前 USB/RTC 会话，用于串口路由、ACK 对账、重连隔离和诊断。
- `device_id` 不参与持久化路径；更换机器人不会切换数据空间。
- `:9000` 仅将完成 DBOT hello 的 session 视为 live `usb_cdc`。多台设备同时在线时，页面选择的只是当前控制目标。
- 设备接入只依赖 USB hello，没有手工开通或配对流程；固件不保存服务器地址、配对码或长期 token。

## 进程边界

### 核心进程 `:9000`

- 独占串口扫描、打开、读写和自动重连；
- 运行对话、相机、人脸、PB、提醒和设备控制；
- 提供供 Flask 调用的轻量 HTTP API；
- 提供 `/camera_view` 和 `/device_pipeline?role=subscriber` 浏览器订阅；
- 拒绝所有设备网络生产者连接。

### Web 进程 `:5050`

- 直接提供本机页面，只维护必要的界面状态；
- 展示 USB 自动发现状态、当前运行时目标和本地数据管理界面；
- 通过本机进程间 API Key（`X-API-Key`，来源 `data/.free_api_key`，附带每日字节配额）访问 `:9000`；它只保护 Web→Core 通道，不代表用户身份；
- 不枚举、不打开、不关闭任何串口。

这条边界保证同一 COM/tty 不被两个进程争用，也避免 Web worker 各自建立设备连接。

## USB transport

`infrastructure/serial/` 实现 DBOT v1：

1. 扫描允许的 USB CDC 设备；
2. PC 建立 session epoch，并发送带稳定 `client_nonce` 的 hello；
3. 设备回显 nonce、设备码与 capabilities；
4. 双方进入 heartbeat 和 framed I/O；
5. 断线、epoch 不匹配、CRC/长度错误或超时使 session 失效；
6. 管理器关闭旧 generation，自动重新扫描。

每个 session 只有一个 reader 和一个有序 writer。协议按通道隔离 CONTROL JSON、
PB JSON/binary、麦克风 Opus、扬声器音频、相机 JPEG 与日志，避免二进制数据错配。

固件握手完成后的日志也走 LOG 帧，不与协议字节流混写。

## 对话链路

```text
USB mic Opus（ESP-SR AEC / NS / VAD，16 kHz）
  → DeviceSession ingress
  → rtc_runtime
  → per-device LiveKit room（本机 LiveKit Server :7880）
  → RTC Agent: Seed ASR → 通用大模型（DeepSeek/豆包/MiMo 预设或自定义）→ Seed TTS
  → LiveKit remote audio（16 kHz，端到端无二次重采样）
  → rtc_runtime ordered audio downlink
  → USB Opus
  → device speaker
```

设备语音只有这一条链路：旧的 WS 传统语音轮（flush → ASR → LLM）已删除，
`/asr_chat` handler 只处理控制面消息，媒体经 DeviceSession 专用队列直连
`rtc_runtime`。设备 VAD 同时用于上行门控和打断判定，但不会触发拍照、找脸或舵机
动作。RTC Agent 状态只驱动表情状态机；需要访问本机记忆、提醒、相机或设备控制时，
Agent 通过带随机 token 的 loopback bridge 调用 Core，工具仍由当前 USB session 执行。

### Agent 工具与 AI 生成（2026-09-14）

工具目录只有一处：`application/tool_executor.TOOL_CAPABILITIES`（渠道 × 工具矩阵），
RTC 侧 schema 在 `rtc_worker_tools`，文字侧说明在 `llm/utils.llm_tools_prompt_appendix`。
三条链路（Core 主循环 / 控制台文字对话 / RTC 语音）现在拿到同一套工具，包括原来只在
语音链路的 `play_expression` / `move_head`（文字对话转 Core HTTP `/api/device_face_play`、
`/api/device_servo`）。

控制台的五个"AI 生成"按钮全部接到了对话上，实现收在 `application/agent_generation`，
网页路由与 Agent 工具共用同一份提示词和白名单规则：

| 工具 | 做什么 | 落到哪 |
| --- | --- | --- |
| `generate_scene` | 编一段新表演（口播 + 表情 + 动作），默认立刻表演 | 表演列表 `scene_playbooks` |
| `generate_quest_scene` | 一句话生成陪伴场景（主线小目标 + 日常关心）并启用 | 主动陪伴 `quest_playbooks` |
| `generate_motion` | 设计一个新头部动作，存为模型可见预设，默认立刻做一遍 | `servo.json` presets |
| `generate_cartoon_faces` | 后台生成整套卡通表情（约 3～4 分钟），存库并换到设备 | 表情库 + 状态映射 |
| `generate_expression` | 文生 / 照着相机画面画一个矢量表情，可设为待机脸 | 表情库 |
| `generation_status` | 查后台生成的进度 / 结果 | `application/generation_jobs`（进程内账本） |

渠道差异只在 `agent_generation_tools.ChannelOps`（怎么表演、动头、让设备换表情、拍一帧、
做完了怎么开口）：Core / RTC 直接用运行时对象，Flask 文字对话经 Core HTTP
（新增 `/api/expression_apply_default` 让设备按新映射立刻换表情）。耗时的卡通套图在后台
线程里跑，完成后语音 Agent 在线时经 `rtc_instructions` 让它主动说一句。

两个连接边缘状态由固件/服务显式呈现，而不是装死：未连接 PC 服务时固件 display
worker 循环播放电脑最后下发的待机卡通脸（固件 ≥0.0.57，落在 FFat 里重启也保持；没有
卡通脸时画内建矢量脸），断开 10 s 后叠加「请先连接PC服务」，hello 后清除；RTC 冷启动
窗口内用户开口时，`speech_start` 触发节流的「语音启动中…」短表情反馈。

相机链路：

```text
USB JPEG → camera_jpeg_pipeline
         → validated latest-frame store
         → preview broker / latest-wins face analysis
         → browser camera_view subscriber
```

摄像头不会根据人脸位置控制舵机；常态人脸跟随、语音后找脸和 LLM 等待找人均不存在。
连续帧率的唯一真值由 PC 侧 `CameraCadenceController` 单写者计算（预览租约与有界
拍照 boost 取 max）；`cam_fps` 仅由该控制器下发（LLM 可写通道已删），`camera_once`
单帧请求，连续预览节奏信号仅对旧固件生效。

RTC 视觉问答也不会持续把视频送往云端。模型只有在问题确实依赖当前画面时才调用
`capture_and_describe`：

```text
用户视觉问题
  → 同一 RTC LLM 会话调用 capture_and_describe(question)
  → authenticated loopback bridge
  → Core 下发 camera_once=true，等待一张拍摄时间晚于请求的新 JPEG
  → 大小 / MIME / 完整解码 / 像素数校验
  → Worker 立即从工具响应移除图片字段
  → 复制当前 LiveKit history，临时加入 question + ImageContent
  → 同一 session.generate_reply(tool_choice="none")
  → LLM 流式文本 → Seed TTS → 设备扬声器
```

工具返回 `None`，因此 LiveKit 不会再生成第二次文本工具 follow-up。图片 base64 不进入
文本工具结果、日志、pipeline、工具幂等账本或正式会话 history；SpeechHandle 结束后
立即清空临时上下文。最终文字回答仍正常留在当前 RTC 会话。拍照固定为
`display=false`，所以“画面里有什么”不会把照片盖到机器人屏幕。捕获、校验或模型
视觉能力失败时，错误留在当前 RTC 会话明确说明，不回退到另一条 LLM，也不会根据
人脸元数据猜测整幅画面。

LLM 返回纯 `moves` / `anims` 且 `tts` 为空时，PC 直接生成不含
`audio`/`sr`/`fmt`/`ch` 的 JSON-only PB；动作、表情和设备 `played` 终态确认仍
完整保留。只有同时存在真实 TTS 时才生成音频并把口型、舵机和表情合到同一时间轴。

部分内部模块仍保留 `asr_chat`、`AsrChatHub` 或 WebSocket-compatible 方法名，用于
复用成熟的对话状态机和 downlink 接口；它们不表示设备网络入口仍然开放。

## 控制与幂等

Flask 将用户动作提交给 `:9000`。实体动作使用稳定 `operation_id`：

```text
accepted → running → completed | failed | cancelled | timeout
```

`202 Accepted` 只表示操作已持久化。服务等待设备终态 `played` ACK 后才写
`completed`。相同 `operation_id` 只能重试完全相同的设备、类型和 payload。

## 定时提醒

2026-09-14 起没有独立的定时任务调度器：定时提醒就是主动陪伴「定时提醒」场景里带钟点的
care 小目标（`quest_playbooks_store`：`schedule_time` HH:MM、`schedule_days` 每周几、
`schedule_date` 一次性、`done_at` 提过），由 `QuestProactiveLoop` 的 5 秒轮询按本地时区
触发（`quest_proactive.pick_scheduled_due`）：

- 到点后 2 小时内补提（离线 / 通道忙时下个 tick 再试，超过就算今天错过）；
- 到点时正处在勿扰窗口的，勿扰结束后 2 小时内补提；
- 定时提醒不占「每天最多开口」名额，也不等冷场；
- 一次性提醒提过写 `done_at`，页面标「已提醒」，7 天后自动清掉。

Agent 的 `schedule_task` 工具（`application/timed_reminders`）直接往这个场景里写，不需要主人
在控制台批准；控制台「主动陪伴」页可以增删改。

## 浏览器与供应商 WebSocket

设备生产者路径 `/asr_chat`、`/camera`、`/camera_uplink` 和
`/device_pipeline` producer 均 fail closed，返回 `usb_cdc_required`。

保留的浏览器订阅：

- `/camera_view?device_id=<id>`
- `/device_pipeline?role=subscriber&device_id=<id>`

订阅只允许选择当前已完成 USB hello 的运行时连接，并使用短期 debug token。该 token
通过 WebSocket 子协议传递，只限制本机调试通道。豆包等供应商 WSS
是 PC 出站连接，与设备 transport 无关。

## 模块

| 模块 | 职责 |
|------|------|
| `infrastructure/serial/` | DBOT framing、session、扫描、重连和上下行适配 |
| `application/` | 对话、相机、控制、turn arbiter、主动陪伴与定时提醒用例 |
| `core/` | 类型、设置、端口 Protocol 与并发限制 |
| `ws/` | `:9000` HTTP、浏览器订阅路由及兼容状态机 |
| `web/` | `:5050` Flask 本机页面、本地数据 API 和受限代理 |
| `db/` | SQLAlchemy 播放回执、工具/控制操作、陪伴进度和本机用量 |
| `pb/` | PB v2.1、表情、口型、舵机和 wire |
| `pipeline/` | Opus 编解码运行时、上行批解码与麦克风健康监测 |
| `vision/` | JPEG、人脸几何/识别和 generation fencing；不控制舵机 |

## 部署边界

```text
local browser ── HTTP ──> Web :5050 ── X-API-Key ──> Core :9000
local robot   ── USB CDC ───────────────────────────────> Core :9000
Core / Agent  ── outbound HTTPS/WSS ───────────────────> ASR / LLM / TTS
```

`:5050`、`:9000` 和 Agent 辅助端口只绑定 loopback。当前控制台不支持直接暴露到局域网
或公网；如未来增加远程入口，需要另行设计独立认证边界。机器人始终
通过本机 USB 接入，不经过反向代理。第三方供应商凭据只保存在 PC 端。
