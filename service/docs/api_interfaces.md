# 接口清单

更新时间：2026-08-19

本文描述当前单机产品接口。ESP32 与 PC 的生产数据只走 USB CDC；浏览器访问
`:5050`，Web 再通过本机进程间 API Key（`X-API-Key`）访问 `:9000`。ESP32
PB/DBOT 字段见 [esp32_pb_protocol.md](./esp32_pb_protocol.md)。

## 数据作用域约定

- 一台 PC 只有一个可写数据空间 `data/local/`。
- `data/global/` 是只读系统模板，首次使用时可作为 `data/local/` 的初始化种子。
- 本地会话只有 `local` scope。人脸识别结果可以进入本轮上下文，但不产生另一套会话、记忆或配置。
- 表情、口型、情绪映射、场景、记忆、人脸档案、提醒、偏好、音色和模型配置均不按硬件隔离。
- `device_id` 只标识已完成 hello 的 live USB/RTC 会话，用于真机下发、ACK 对账、调试订阅、重连和日志。
- 换一台机器人接入同一 PC 时继续使用相同本地数据；浏览器字符串不能创建 live 连接。

## 服务与端口

| 服务 | 默认地址 | 实现位置 | 说明 |
|------|----------|----------|------|
| Web 控制台 | `http://127.0.0.1:5050` | `deskbot_server.web` | 本机页面、本地数据管理、USB 状态、调试页和 Core 代理 |
| Deskbot Core | `http://127.0.0.1:9000` / `ws://127.0.0.1:9000` | `deskbot_server.ws`、`deskbot_server.infrastructure.serial` | USB CDC 唯一串口进程、RTC/业务流水线、内部 HTTP、调试订阅 |
| RTC Agent | `http://127.0.0.1:18790` | `deskbot_server.rtc_agent_sdk` | 本地 token endpoint、LiveKit worker 与 AgentSession |
| 本机 LiveKit Server | `http://127.0.0.1:7880` | `deskbot_server.local_livekit` | RTC 信令与媒体，由 Core 启停 |

四个本地端口都必须保持 loopback。当前产品没有远程控制认证边界，不支持通过反向
代理、端口映射或局域网地址暴露。机器人不连接这些 TCP 端口，也不建立设备 WebSocket。

## 本机访问边界

### Web 控制台 `:5050`

- `/` 直接进入 `/home`，页面不要求身份流程。
- Host 只接受 `localhost`、`127.0.0.0/8` 或 `::1`，其它 Host 返回 `421`。
- 页面与本地 JSON API 直接映射到唯一 `data/local/` 空间。
- 写请求仍校验 Origin 与 `Sec-Fetch-Site`，防止其它网页驱动本机控制台。
- `/proxy/deskbot/*` 向 Core 附带本机进程间 API Key（`X-API-Key`）；它不代表人类身份。

### Deskbot Core `:9000`

| 范围 | 访问条件 |
|------|----------|
| `GET /health` | loopback 存活探针 |
| `/api/*` | 本机进程间 API Key（`X-API-Key` 请求头，凭证在 `data/.free_api_key`）；不提供通用外部服务 Key |
| `/camera_view`、`/device_pipeline?role=subscriber` | 控制台签发的短期 debug token |
| 设备生产者 WebSocket | 已关闭，返回 `usb_cdc_required` |

进程间 API Key 与 debug token 都只是本机进程/通道防护，不建立用户、角色或数据
隔离；API Key 附带每日 1 GiB 字节配额，仅作本机资源保护。供应商 ASR/LLM/TTS
Key 是第三方服务凭据，只保存在 PC 端，绝不下发到固件。

浏览器 WebSocket 通过子协议传递短期 debug token：

```javascript
new WebSocket(url, [
  "deskbot.debug.v1",
  "deskbot.debug.auth." + token,
]);
```

token 默认不放 URL query，也不写入访问日志。

## Web 页面 `:5050`

### 消费页面

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/` | 跳转 `/home` |
| GET | `/health` | Web 存活探针，返回纯文本 `ok` |
| GET | `/home` | 首页、当前 USB 状态与常用入口 |
| GET | `/voice` | 本机音色与声音复刻 |
| GET | `/expr` | 本机表情库、捏脸与设备预览 |
| GET | `/lab` | 摄像头、舵机、场景、PB 与流水线实验功能 |
| GET | `/memories` | 本机长期记忆 |
| GET | `/reminders` | 本机提醒 |
| GET | `/sessions` | 唯一 local 会话 |
| GET | `/preferences` | 本机互动偏好 |
| GET | `/people` | 本机人脸档案 |
| GET | `/devices` | live USB 连接与当前运行时目标 |
| GET | `/miot` | 本机米家配置与同步 |
| GET | `/advanced` | 供应商、模型、用量与诊断设置 |

### 运维与调试页面

| 方法 | 路径 | 用途 |
|------|------|------|
| GET | `/app/` | 本机工作台 |
| GET | `/app/devices` | live USB 连接状态 |
| GET | `/app/scheduled-tasks` | 提醒列表 |
| GET | `/app/face-profiles` | 人脸档案 |
| GET | `/app/memories` | 长期记忆 |
| GET | `/app/llm-models` | LLM 模型配置 |
| GET | `/app/usage` | 本机汇总用量 |
| GET | `/app/settings` | 本机供应商与运行参数 |
| GET | `/debug/devices` | 在线连接、流水线、表情和舵机调试 |
| GET | `/debug/tts` | TTS 调试 |
| GET | `/debug/llm` | LLM 试聊与 system prompt |
| GET | `/debug/simulation` | 模拟对话和显示调试 |

## Web JSON API `:5050`

### 本机高级设置与供应商配置

| 方法 | 路径 | 用途 | 主要输入 |
|------|------|------|----------|
| GET | `/api/advanced` | 汇总本机连接、用量和 LLM/ASR/TTS 状态 | - |
| GET | `/api/setup/asr` | 读取脱敏的 ASR 状态 | - |
| PATCH/POST | `/api/setup/asr` | 保存本机 ASR 配置 | JSON: `provider`, `endpoint`, `model`, `language`, `timeout_seconds`, `max_audio_bytes`, `model_dir`, `api_key` 可选；`clear_api_key=true` 显式清除 |
| POST | `/api/setup/asr/test` | 执行受限连通性测试 | JSON 可选：`pcm_base64`, `sample_rate` |
| GET | `/api/setup/llm` | 读取脱敏的通用大模型配置 | - |
| PATCH/POST | `/api/setup/llm` | 保存 LLM 配置（控制台预设 DeepSeek/豆包/MiMo 或自定义） | JSON: `api_key`, `model_name`, `protocol`, `base_url` 可选；`ark_api_key` 为图片表情独立凭证，留空表示保留现值 |
| POST | `/api/setup/llm/test` | LLM 连通性测试 | JSON 可选：待测配置字段 |
| POST | `/api/setup/llm/models` | 列出所选供应商可用模型 | JSON: 供应商连接字段 |

配置读取只返回 `api_key_set`，不返回第三方 Key。更新时省略或传空 `api_key` 会保留
现有值，只有 `clear_api_key=true` 才删除。保存使用文件锁和原子替换更新 `.env`，Core
在下一轮语音前热加载。

### 本地数据 API

以下接口统一读写 `data/local/` 或本机数据库，不接受 `device_id` 作为数据作用域。

| 方法 | 路径 | 用途 | 主要输入 |
|------|------|------|----------|
| GET | `/app/api/scheduled-tasks` | 分页查询提醒 | query: `page`, `per_page` |
| POST | `/app/api/scheduled-tasks` | 创建一次性或 cron 提醒 | JSON: `description`, `run_at`/`delay_seconds`/`delay_minutes`/`cron`, `task_kind` |
| PATCH | `/app/api/scheduled-tasks/{task_id}` | 编辑提醒 | JSON: 待修改字段 |
| POST | `/app/api/scheduled-tasks/{task_id}/{pause\|resume\|retry}` | 暂停、恢复或重试 | - |
| DELETE | `/app/api/scheduled-tasks/{task_id}` | 删除提醒 | - |
| GET | `/app/api/preferences` | 读取本机互动偏好与 revision | - |
| PATCH | `/app/api/preferences` | CAS 更新互动偏好 | JSON: `expected_revision`, `preferences` |
| GET | `/app/api/sessions` | 查询唯一 local 会话 | query: `page`, `per_page` |
| POST | `/app/api/sessions` | 新建/轮换 local 会话 | JSON: `title` 可选 |
| GET | `/app/api/sessions/{session_id}` | 读取一条本机会话 | - |
| POST | `/app/api/sessions/{session_id}/activate` | 激活本机会话 | - |
| POST | `/app/api/sessions/clear-current` | 清除当前会话指针 | - |
| DELETE | `/app/api/sessions/{session_id}` | 删除会话 | - |
| GET | `/app/api/sessions/{session_id}/export` | 导出会话 JSON | - |
| GET | `/app/api/face-profiles` | 查询人脸档案摘要 | - |
| PUT/PATCH | `/app/api/face-profiles/{person_id}` | 更新人脸名称 | JSON: `name` |
| DELETE | `/app/api/face-profiles/{person_id}` | 删除人脸档案 | - |
| GET | `/app/api/memories` | 查询长期记忆 | - |
| POST | `/app/api/memories` | 新增长期记忆 | JSON/form: `text` |
| GET | `/app/api/memories/{entry_id}` | 查询单条记忆 | - |
| PUT/PATCH | `/app/api/memories/{entry_id}` | 更新记忆 | JSON: `text` |
| DELETE | `/app/api/memories/{entry_id}` | 删除记忆 | - |
| GET | `/app/api/llm-models` | 查询本机 LLM 模型和当前选择 | - |
| POST | `/app/api/llm-models` | 新增本机 LLM 模型 | JSON: `name`, `model_name`, `protocol`, `base_url`, `api_key` |
| PUT | `/app/api/llm-models/{model_id}` | 更新模型 | JSON: 模型字段，`api_key` 可选 |
| DELETE | `/app/api/llm-models/{model_id}` | 删除模型 | - |
| POST | `/app/api/llm-models/select` | 选择/清空当前模型 | JSON: `model_id`，可为 `null` |
| GET | `/api/face_expression_transaction` | 一致读取表情场景、映射和 revision | - |
| POST | `/api/face_expression_transaction` | 原子创建/更新/删除用户表情并修改映射或音素 | JSON: `expected_revision`, `scenes.create/update/delete`, `map_patch`, `phonemes.upsert` |
| GET/POST | `/api/face_mouth_by_phoneme` | 读取/保存本机音素口型组 | POST JSON: `mouth_by_phoneme_groups` |
| GET/POST | `/api/face_expr_scenes` | 旧客户端兼容的整库场景接口；新页面不再使用 | POST JSON: `scenes` 或 `config` |
| GET/POST | `/api/scene_playbooks` | 读取/保存本机场景 playbook | POST JSON: playbook 数组 |
| GET/POST | `/api/camera_face_config` | 读取/保存本机人脸检测配置 | POST JSON: camera face 文档 |

舵机配置没有独立的 Flask 路由：页面经 `/proxy/deskbot/api/servo_config` 与
`/proxy/deskbot/api/servo_contract` 访问 Core 的对应端点（旧的 5050 直连
`servo_config` 端点已删除）。
表情首次初始化时从 `data/global/deskbot-face.json` 复制到
`data/local/deskbot-face.json`；之后所有编辑只写本地文件。换设备不会重新初始化。
`deskbot-face.json` schema v2 在同一原子文档内保存 `revision`、`mappings`、
`phonemes` 和 `emotions`。每个表情使用显式 `origin=system|user`，不能根据名称前缀
推断所有权。系统表情只能复制为新的用户表情；更新和删除只接受用户表情。客户端
带旧 revision 写入时返回 HTTP 409，避免两个页面相互覆盖。

### live USB 连接 API

| 方法 | 路径 | 用途 | 主要输入 |
|------|------|------|----------|
| GET | `/app/api/devices` | 列出完成 hello 的 live 连接与当前运行时目标 | - |
| POST | `/app/api/devices/select` | 切换当前运行时目标 | JSON: `device_id` |
| GET | `/api/debug/ws_token` | 签发短期调试订阅 token | - |
| POST | `/api/face_profiles/register` | 将当前检测到的人脸写入本机档案 | JSON: `device_id`, `face_id`, `name` |

`/app/api/devices` 只展示 Core 已验证的 live session。选择目标只影响后续真机路由，
不改变任何持久化数据。

### TTS、LLM 与调试 API

| 方法 | 路径 | 用途 | 主要输入 |
|------|------|------|----------|
| GET | `/api/doubao_tts/speakers` | 读取本机豆包说话人预设 | - |
| GET/POST | `/api/doubao_tts/config` | 读取脱敏状态/保存 TTS 配置 | POST JSON: `api_key`, `speaker`, `resource_id`, `model`, `ws_url`, `sample_rate`, `audio_format` |
| POST | `/api/doubao_tts/synthesize` | 调试合成并返回 WAV base64 | JSON: `text`，可带临时 TTS 配置 |
| GET/POST | `/api/llm/system_prompt` | 读取/保存本机 system prompt | POST JSON: `system_prompt` 或 `content` |
| POST | `/api/llm/chat` | 调试 LLM，不直接走设备 ASR | JSON: `text`, `history`, `system_prompt`, `temperature`, `device_context` |
| GET | `/api/health` | 汇总 Web/Core 与供应商相关健康状态 | - |

### Web→Core 代理

`/proxy/deskbot/{subpath}` 只代理显式 allowlist 中的方法和路径。它向 Core 附带
本机进程间 API Key（`X-API-Key`），并将本地配置调用与 live 设备控制分开：前者
不补 `device_id`，后者只允许当前真实连接。未列路径返回 `proxy_path_not_allowed`。

## Core WebSocket `:9000`

Core WebSocket 只提供浏览器/CLI 调试订阅，不承载设备生产数据。

### 已关闭的设备生产者路径

| 路径 | 当前行为 |
|------|----------|
| `/asr_chat` | 返回 `usb_cdc_required`，close code `1008` |
| `/camera`、`/camera_uplink` | 返回 `usb_cdc_required`，close code `1008` |
| `/device_pipeline` 非 subscriber | 返回 `usb_cdc_required`，close code `1008` |

### `/camera_view`

```text
ws://127.0.0.1:9000/camera_view?device_id=<runtime-id>
```

服务端先发送 `ready`，之后每帧发送 `camera_frame` 元数据和紧随其后的 JPEG binary。
人脸推理完成后追加一条 `face_meta` 纯文本消息（含 landmarks/yaw/pitch 等，不带
JPEG，也没有紧随的 binary）；同一帧 JPEG 只发送一次。
`device_id` 可省略并使用当前 live 目标；指定时必须对应当前真实连接。

### `/device_pipeline?role=subscriber`

```text
ws://127.0.0.1:9000/device_pipeline?role=subscriber&device_id=<runtime-id>
```

连接后发送 `pipeline_snapshot`，随后推送 `pipeline_event` / `pipeline_stage`。这里的
`device_id` 只过滤实时事件，不过滤或选择持久化数据。

## Core HTTP API `:9000`

下表中的 `device_id` 都是 live 路由参数。没有 `device_id` 的配置端点统一访问
`data/local/`。

| 方法 | 路径 | 用途 | 主要输入 |
|------|------|------|----------|
| GET | `/health` | Core 存活探针 | - |
| GET | `/api/rtc_health` | 本机 LiveKit Server、Agent、Room、音轨与恢复状态 | - |
| GET | `/api/usb-devices` | 枚举完成 hello 的 live USB session | `X-API-Key` |
| GET | `/api/servo_contract` | 舵机硬件包络、批次限制与 preset catalog（三端契约单一来源） | - |
| GET/POST | `/api/asr_auto_reply` | 读取/持久化「自动应答」总开关（写入 `data/local/debug_prefs.json`） | POST query: `enabled=1/0` |
| GET | `/api/devices` | live 连接与在线状态 | - |
| GET | `/api/pipeline_recent` | 获取一条连接的流水线窗口 | query: `device_id`, `limit` |
| GET | `/api/control_operation` | 查询异步控制状态 | query: `device_id`, `operation_id` |
| POST | `/api/device_servo` | 向 live 连接下发舵机 PB | query: `device_id`, `operation_id`, `dyaw`, `dpitch`, `ms` 等 |
| GET | `/api/pb_scenes` | 列出本机场景名 | - |
| GET | `/api/face_catalog` | 列出本机音素与情绪表情目录 | - |
| POST | `/api/device_face_play` | 向 live 连接下发表情链 | query: `device_id`, `operation_id`, `kind`, `name` |
| POST | `/api/device_tts` | 直接 TTS 并下发 | JSON: `device_id`, `operation_id`, `text`, `scene` 可选 |
| POST | `/api/scene_playbook/run` | 在 live 连接执行本地 playbook | JSON: `device_id`, `operation_id`, `playbook` 或 `name` |
| POST | `/api/device_pb_scene` | 按本地场景名下发 PB 链 | query: `device_id`, `operation_id`, `scene` |
| POST | `/api/device_pb_anim` | 下发单片动画 PB | JSON: `device_id`, `operation_id`, `anim`, `chunk_ms` 等 |
| POST | `/api/device_pb_expr_scene` | 下发本地设计表情 | query: `device_id`, `operation_id`, `scene` 或 `name` |
| GET/POST | `/api/face_expr_scenes` | 读取/保存本地表情库 | POST JSON: 场景数组；无数据作用域参数 |
| GET/POST | `/api/face_mouth_by_phoneme` | 读取/保存本地音素口型 | POST JSON: 组表；无数据作用域参数 |
| GET/POST | `/api/scene_playbooks` | 读取/保存本地 playbook | POST JSON: 数组；无数据作用域参数 |
| GET/POST | `/api/servo_config` | 读取/保存本地舵机配置 | POST JSON: servo 文档 |

异步真机控制首次接受时返回 `202`，只表示 Core 已接纳请求。客户端必须为一次动作生成
稳定 `operation_id` 并查询 `/api/control_operation`；只有状态为 `completed` 且设备
回传终态 `played` ACK 才算成功。相同 ID 只能重试相同 live 目标、控制类型和载荷。

## 源码索引

| 范围 | 文件 |
|------|------|
| Flask app 与本机请求防护 | `service/src/deskbot_server/web/app.py` |
| 本机页面/API | `service/src/deskbot_server/web/blueprints/app2c_bp.py`、`app_bp.py` |
| Web 调试路由 | `service/src/deskbot_server/web/blueprints/debug_bp.py` |
| Web→Core 代理 | `service/src/deskbot_server/web/blueprints/proxy_bp.py` |
| Core HTTP/WS 路由 | `service/src/deskbot_server/ws/http_api.py`、`ws/router.py` |
| 本机进程间 API Key / debug token | `service/src/deskbot_server/auth/api_key_service.py`、`auth/debug_ws_token.py`、`ws/api_key_gate.py` |
| USB 串口会话 | `service/src/deskbot_server/infrastructure/serial/` |
| RTC gateway/runtime/Agent | `service/src/deskbot_server/rtc_gateway.py`、`rtc_runtime.py`、`rtc_agent_sdk.py` |
| 本地数据路径 | `service/src/deskbot_server/device_data.py`、各 `*_store.py` |
| PB/表情/口型 | `service/src/deskbot_server/pb/` |
