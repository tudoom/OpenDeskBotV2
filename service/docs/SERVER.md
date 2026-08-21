# deskbot-server 运维与部署

## 启动

```bash
cd service
cp .env.example .env
# 填写独立的 ASR_API_KEY 与所选 LLM/TTS 供应商凭证
./start.sh
```

已有完整环境时：

```bash
SKIP_SETUP=1 ./start.sh
```

| 端口 | 进程 | 用途 |
|------|------|------|
| `9000` | Core | 串口唯一所有者、对话/相机/PB、内部 HTTP、浏览器订阅 WS |
| `5050` | Flask Web | 本机控制台、运行时设备选择、本地设置与调试 UI，不打开串口 |
| `18790` | RTC Agent SDK | 本机 LiveKit Agent 辅助服务，由 Core 管理，不对外开放 |
| `7880` | 本机 LiveKit Server | RTC 信令与媒体，由 Core 自动启动、监测和停止 |

Windows 有两种等价启动形态：开发用 `tools\run_local_windows.ps1`（先执行
`tools\install_livekit_windows.ps1` 安装本机 LiveKit Server），或使用
`client\Build-Client.ps1` 打包的一体化桌面客户端 `OpenDeskBotV2.exe`——它把
Core/Web/LiveKit/RTC Agent 放进同一个 Windows Job Object，就绪判定只看 Web 与
Core（LiveKit/RTC Agent 缺席只显示“RTC 未就绪”），停止时先发 CTRL_BREAK
（SIGBREAK）等待最多 15 秒优雅关闭，超时才硬杀。Core 收到 SIGBREAK/SIGTERM
会执行完整关闭序列（停调度器→串口→RTC binding→Agent SDK→本机 LiveKit）。

## USB 设备接入

1. 烧录统一的 USB-only 固件。
2. 使用可传数据的 USB 线连接机器人与运行服务的 PC。
3. Core 自动扫描兼容 CDC 端口并执行 DBOT hello。
4. hello 回传由 eFuse MAC 派生的 `device_id`；它仅用于当前 USB/RTC/ACK 路由。
5. Core 将完成 hello 的连接加入内存中的 live session 集合，不据此创建业务数据目录。
6. 打开 `:5050` 即可使用。单台设备自动成为当前目标，多台设备可切换控制目标。

`/api/usb-devices` 只接受本机进程间 API Key（`X-API-Key`，来源
`data/.free_api_key`），并只返回完成 hello 的 live USB session；浏览器提交字符串
不能创建连接。
所有兼容设备共享 `data/local/`，拔插或更换硬件不会切换表情、会话、记忆和设置。

`:9000` 必须独占串口。出现设备无法发现时，先关闭串口监视器、烧录器和重复的
Core 进程，再等待自动重扫；不要杀死无关串口进程。

## Web 控制台 `:5050`

主要页面：

| 页面 | 用途 |
|------|------|
| `/home` | 用户首页 |
| `/devices`、`/app/devices` | USB 自动发现、在线状态和当前运行时目标切换 |
| `/reminders`、`/app/scheduled-tasks` | 一次性与 cron 提醒 |
| `/sessions` | 会话中心 |
| `/preferences` | 互动偏好与离线提醒策略 |
| `/memories` | 长期记忆 |
| `/people` | 人脸档案 |
| `/advanced`、`/app/settings` | ASR、LLM、TTS 供应商凭据与本机服务配置 |
| `/debug/devices`、`/lab` | 相机、流水线、PB、舵机和控制调试 |

控制台直接提供本机页面，只维护必要的界面状态，并强制 `DESKBOT_WEB_HOST` 为
loopback；不存在切换身份或远程访问模式。

## Core HTTP `:9000`

- `GET /health`：存活探针；
- `GET /api/usb-devices`：只供本机 Web 用进程间 API Key 发现当前 live USB 设备；
- `GET /api/devices`：当前 live USB 连接与在线状态；
- `/api/pipeline_recent`：设备流水线窗口；
- `/api/device_*`、`/api/scene_*`：设备控制和 PB；
- `/api/control_operation`：查询持久化控制操作终态；
- `/api/servo_config` 等：读取本机配置；真机下发时另选当前 live 连接。

除 `/health` 外使用本机进程间 API Key（`X-API-Key` 请求头；凭证保存在
`data/.free_api_key`，附带每日 1 GiB 字节配额作为本机资源保护）；它用于阻止
绕过 Web 的意外调用，不表示人类身份。Core 只向真实 live session 下发真机操作，
不提供通用外部控制凭据。

实体动作通常先返回 `202`。客户端必须保存 `operation_id` 并查询
`/api/control_operation`；只有 `completed` 且设备已回传 `played` ACK 才算成功。

## WebSocket `:9000`

### 浏览器调试订阅

- `/camera_view?device_id=<id>`
- `/device_pipeline?role=subscriber&device_id=<id>`

订阅使用控制台自动签发的短期 debug token，并持续复核目标仍是 live USB session。
浏览器通过以下子协议携带 token：

```javascript
new WebSocket(url, [
  "deskbot.debug.v1",
  "deskbot.debug.auth." + token,
]);
```

### 已关闭的设备入口

以下路径不再接收设备生产数据：

- `/asr_chat`
- `/camera`
- `/camera_uplink`
- `/device_pipeline` 非 subscriber 角色

连接会收到 `usb_cdc_required` 并以 policy violation 关闭。不要为机器人配置
`ws://`、`wss://` 或任何网络凭据。

## PC 供应商连接

PC 负责全部外部访问：

- ASR 模型或服务；
- LLM HTTPS API（通用大模型，控制台预设 DeepSeek/豆包/MiMo，可自定义）；
- 豆包 TTS 双向 WSS；
- 资源下载与安全联网工具；
- PC 系统时间。

供应商 WSS/HTTPS 与浏览器订阅 WSS 仍受支持，但都不属于设备 transport。凭证放在
`.env` 或受控管理界面，并在日志中脱敏。

普通用户提供的供应商 URL 默认禁止回环、内网、链路本地与云元数据地址。自托管
内网 provider 只能由运维通过 `DESKBOT_PROVIDER_PRIVATE_ORIGINS` 精确放行。

### ASR provider

默认安装使用轻量的 `doubao_streaming` adapter，不包含 torch、FunASR 或
SenseVoice 模型。Core 把设备上传的 s16le 音频通过火山豆包大模型 ASR 的 WSS
流式协议发送；也保留 `openai_compatible` 与本地 `funasr` 作为显式选项。

ASR 是双轨的：**设备实时语音固定使用豆包流式 ASR（RTC 内硬编码），不随
`ASR_PROVIDER` 切换**；provider 选择只影响连通性测试与文本兼容路径，控制台
的 ASR 卡片也如此披露。

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `ASR_PROVIDER` | `doubao_streaming` | `doubao_streaming`、`openai_compatible` 或 `funasr` |
| `ASR_API_KEY` | 空 | 新版火山语音 API Key；绝不复用 LLM/TTS Key |
| `VOLCENGINE_ASR_RESOURCE_ID` | `volc.seedasr.sauc.duration` | 火山流式 ASR 2.0 资源 ID |
| `VOLCENGINE_ASR_ENDPOINT` | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` | 默认要求公网 WSS |
| `ASR_ENDPOINT` | OpenAI transcription URL | 仅 `openai_compatible` 使用 |
| `ASR_MODEL` | `whisper-1` | 仅 `openai_compatible` 使用 |
| `ASR_LANGUAGE` | `zh-CN` | 可选语言提示，最长 32 字符 |
| `ASR_TIMEOUT_SECONDS` | `30` | 单次请求超时，1–300 秒 |
| `ASR_MAX_AUDIO_BYTES` | `10485760` | 单轮音频上限，1 KiB–100 MiB |

服务允许在 `ASR_API_KEY` 尚未配置时启动。第一轮语音会产生
`asr_not_configured` 流水线错误，Core 向设备重新发送 `mic=open` 并恢复监听，
不会把配置失败误判成“用户没说话”。

本机用户可在 `/advanced?tab=llm` 的“语音识别 ASR”区域查看脱敏状态、保存并
测试。对应接口为 `GET/PATCH /api/setup/asr` 与
`POST /api/setup/asr/test`。Key 永不回显；更新时空 Key 表示保留，
`clear_api_key=true` 才会清除。`:5050` 使用文件锁和原子替换更新 `.env`，
`:9000` 在下一轮语音前按文件签名热加载。

本地回退必须显式安装和准备模型：

```bash
pip install -e '.[local-asr]'
# .env:
# ASR_PROVIDER=funasr
# ASR_MODEL_DIR=./models/SenseVoiceSmall
```

启动脚本和运行时都不会自动下载或导出 SenseVoice。模型/依赖缺失时会返回可操作
的配置错误，不影响 Web 与 Core 进程启动。

## 配置与数据

| 位置 | 内容 |
|------|------|
| `.env` | 供应商 Key、生产安全开关、监听地址 |
| `config.yaml` | 音频、VAD、模型、PB 和运行参数 |
| `data/opendesk.db` | 本机提醒、播放回执、工具/控制操作、复刻任务与汇总用量 |
| `data/local/` | 这台 PC 唯一的会话、记忆、人脸、表情、偏好和服务配置 |
| `data/global/` | 只读系统模板与默认资源；首次初始化时作为种子 |

两个进程对持久数据使用数据库事务、文件锁和原子替换；不要手工同时覆盖运行时
JSON。备份时保存数据库与 `data/local/`；`data/global/` 可由安装包恢复。

## 本机部署边界

最小基线：

```bash
DESKBOT_ENV=production
DESKBOT_WEB_SECRET_KEY=<至少32字符随机值>
DESKBOT_SERVER_HOST=127.0.0.1
DESKBOT_WEB_HOST=127.0.0.1
```

控制台只面向本机，因此 `5050`、`9000` 与 RTC Agent 辅助端口必须保持 loopback，
不得通过反向代理、端口映射或局域网地址直接暴露。若未来需要远程控制，应先新增独立
认证产品边界，不能复用当前本机接口。`DESKBOT_WEB_SECRET_KEY` 只签发进程间和调试
短期令牌，不建立人类身份。机器人继续通过本机 USB 连接。

## 定时任务恢复

调度器使用带 fencing token 的租约：

- 启动时恢复过期的 `running`；
- 执行期间续租；
- 进程异常后由 lease expiry 回队；
- 正常停止时取消并等待派生 worker，安全释放当前租约；
- 当前设备离线时按本机策略过期、宽限重试或上线补发；
- 勿扰时段（用户时区）内到期的提醒 defer 到勿扰窗口结束后播报，不丢弃也不硬试；
- durable played receipt 防止服务在设备已播报后崩溃导致重复播放。

## 诊断

```bash
source .venv/bin/activate
python tools/network_connectivity_test.py \
  --device-id deskbot_e83dc1faf074
```

这里的 `device_id` 只选择当前 live USB 连接，不选择数据空间。默认诊断只读，且工具
本身不打开串口。`--control-rounds 1` 会提交小幅舵机动作并
等待终态 ACK，应只在确认周边安全后显式使用。

测试：

```bash
ruff check src tools tests
PYTHONPATH=src pytest tests -q
```

## 固件安装

固件编译、整片擦除与烧录见
[`../../hardware/README_zh.md`](../../hardware/README_zh.md)。当前部署文档不承诺
Web 控制台固件升级 UI；不要把普通设备控制接口当作升级能力。
