# ESP32 与 PC 服务通信协议（DBOT v1 + PB v2.1）

USB CDC 是设备唯一业务链路。固件不建立 Wi-Fi、HTTP 或 WebSocket 连接，也不保存
服务器地址、云端凭据、配对码或设备 token。所有机器人使用相同镜像；
`device_id` 由 eFuse base MAC 派生：

```text
deskbot_<12 lowercase hex>
```

PC 负责联网、ASR、LLM、TTS、资源和时间。浏览器调试 WebSocket 与供应商 WSS
不进入本设备协议。

> 部署见 [SERVER.md](./SERVER.md)，进程边界见
> [ARCHITECTURE.md](./ARCHITECTURE.md)。

---

## 0. 运行时标识、握手与 live session

### 0.1 固件运行时标识

- 相同固件可连接任意兼容 PC 服务；
- eFuse MAC 产生稳定 `device_id`，仅用于 USB/RTC/ACK 路由、重连隔离和诊断；
- `device_id` 不参与业务数据路径；PC 的可写数据统一位于 `data/local/`；
- 固件不因 PC、服务配置或使用场景而重新编译。

### 0.2 DBOT hello

PC 打开 CDC 后创建新的 session epoch，并发送带非零 `client_nonce` 的 hello。
设备回复 hello ACK，至少包含：

- `device_id`；
- 固件/协议版本；
- capabilities；
- 当前 epoch；
- 与请求完全相同的 `ack_client_nonce`。

同一次握手重试复用相同 nonce。PC 只接受 epoch 与 nonce 都匹配的 ACK；重复 ACK
不得重复触发 ready。握手完成前设备不发送媒体或普通日志。

### 0.3 USB live session 激活

`SerialServiceBridge` 只在 hello epoch、nonce、设备码和能力校验全部通过后激活 live
session，然后启动业务 handler。该过程只建立内存中的通信路由，不创建硬件专属数据。

浏览器只能查看 Core 已验证的 live session 并切换当前控制目标。设备无需参与配对协议，
也不会接收 PC 或供应商凭据；仅知道 `device_id` 无法伪造一条 live USB 连接。

---

## 1. DBOT v1 framing

每个 frame 使用 24 字节定长 header，并同时校验 header CRC 与 payload CRC。header
包含协议标识、版本、channel/type、flags、payload 长度、sequence 与 session
epoch。实现必须：

- 对 payload 长度设置硬上限，在分配内存前拒绝异常长度；
- 对 partial frame 设置超时；
- 在噪声或坏 header 后按 magic 重新同步；
- 校验 epoch，拒绝前一 generation 的迟到 frame；
- 维护单调 sequence，并记录丢帧/重复帧；
- 只保留一个 reader 和一个有序 writer；
- 断线时清理未完成的 PB、音频、相机和 pending binary。

心跳超时、epoch 不匹配、CRC 错误、连续关键写失败或 USB link-down 都会终止当前
session。PC 管理器随后关闭旧 generation 并自动重新扫描。

具备 `session_end` 能力的固件在自身拆除 session 且链路仍可写时，会主动发送
控制帧 `{"type":"session_end","session_epoch":…,"reason":…}`；PC 收到后立即
判当前 session 失效并以新 generation 重连，不必等心跳超时。链路已死类原因不
发送该帧。旧固件从不发送；旧 host 会把它当未知控制消息记录并忽略。

### 1.1 逻辑通道

| 通道 | 方向 | 内容 |
|------|------|------|
| CONTROL JSON | 双向 | hello/ACK、heartbeat、flush、状态、取消与控制 |
| PB JSON | 双向 | `pb_*`、`pb_ack` 与 PB 元数据 |
| PB binary | PC → 设备 | PB 音频及 assets，长度由 JSON 声明 |
| microphone Opus | 设备 → PC | 批量 Opus 麦克风数据 |
| speaker audio | PC → 设备 | Opus 或 PB 声明的 s16le 音频 |
| camera JPEG | 设备 → PC | 一帧一个 JPEG payload |
| LOG | 设备 → PC | 握手后的帧化设备日志 |

不同通道的 binary 不得互相消费。CONTROL/PB JSON 必须是 UTF-8 JSON object。

CONTROL 通道的本地命令面已收窄为 factory 只读查询与维护命令：`head_pos`
（只读位置查询）、`task`（任务栈/CPU 诊断转储）、`reboot`/`restart`（整机
重启）。旧的 factory/action 手势命令层（`head_*`、`adjust_*` 等）及其同步等待
机制已删除，舵机动作只经 PB `servo[]` 时间线执行。

### 1.2 JSON 与 binary 顺序

PB JSON 中的 `audio.next_bin_len` 和 `assets[].next_bin_len` 声明随后 PB binary
logical payload 的精确总长度。PC 将每份 logical payload 拆成一个或多个
**不超过 2048 字节**的 PB_WIRE 非 JSON frame；固件按声明顺序跨 frame 累计，
累计值精确达到 `next_bin_len` 后才解码或提交给 worker。长度不符、越界、缺片或
类型错位会取消当前 `req`，不能把 JPEG、Opus 或日志误当作 PB 音频。

单个不超过 2048 字节的旧式 PB binary frame 仍然兼容。CONTROL/heartbeat 可以在
fragments 之间处理，但另一条 PB JSON/BIN 流不得插入同一 logical binary。
`pb_cancel`、session/epoch 变化、CRC 错误和 fragment-progress 超时都必须释放未完成
的重组缓冲。

旧字段 `audio.next_bin: 1` 和无元数据裸 binary 不属于 DBOT v1。

---

## 2. 上行（设备 → PC）

### 2.1 麦克风

麦克风使用独立 Opus channel 批量上行。CONTROL JSON 表示录音/段状态；段结束
使用 `flush`，触发 PC 的语音流水线。固件只运行 ESP-SR AEC/NS/AGC/VAD，
不直接调用 ASR、LLM 或 TTS。

`audio_vad` 的 `speech_start` / `speech_end` 是原始声学边沿，不是识别成功回执：

- `speech_start` 只标记监听状态；它本身不截断旧播放，也不产生舵机动作。
- `speech_end` 只结束声学段，**不得**启动拍照或舵机搜索。
- RTC AgentSession 确认 barge-in 后才取消旧下行；final ASR transcript 只进入对话，
  不触发任何视觉驱动或自动舵机流程。

这样可避免噪声、空段、回声或重复 VAD end 造成机器人无故转头。

### 2.2 相机

每个 camera frame 使用 camera JPEG channel，payload 是完整 JPEG。拥塞时发送侧
应丢旧保新，不能阻塞心跳和麦克风。

相机命令面：

- `camera_once: true`（媒体自由 PB 中的布尔字段）请求恰好一帧新鲜 JPEG，不改变
  常态节奏；这是当前固件唯一的相机控制。
- **`cam_fps` 连续流通道的唯一生产者是 PC 端 CameraCadenceController**（预览租约
  与有界 capture boost 的合并值）；LLM 可写该字段的通道已在服务端源头删除，固件
  按 0..65535 校验后调用 `camera_set_fps`，fps=0 停止预览
  模块（`camera_set_fps` 已删除）。PC 侧的相机节奏单写者
  （`CameraCadenceController`：预览租约与有界拍照 boost 的 max 合成）仍会发送

### 2.3 状态与 ACK

| 消息 | 说明 |
|------|------|
| heartbeat | 维持 epoch/session 存活 |
| `flush` | 结束当前语音段 |
| `pb_ack` accepted | 设备已接受指定 `req`/`idx` |
| `pb_ack` played | 设备已完成播放/动画/舵机终态 |
| LOG frame | 帧化日志；不得在 CDC 字节流裸打印 |

PC 控制操作只有收到终态 `played` 后才可标记 `completed`。

---

## 3. 下行 pb 概述

| 项目 | 约定 |
|------|------|
| 版本 | `pb_ver: 2`（wire v2.1） |
| 音频 | mono **opus**（默认，`config.yaml` → `audio.output_codec`）或 **s16le**，**sr = 16000**（首包声明） |
| 画布 | **284 × 240**，原点左上 |
| 单包 | `chunk_ms ≤ 10000` |
| 口播默认 | `level = 1`，`action = "replace"` |

一条 pb JSON 可含 **`anim[]`**（表情）、**`servo[]`**（舵机）、**`audio`**（PCM 长度）。口播由服务端按音素组帧后合并为多片 `pb_start` → `pb_chunk*` → `pb_end`，或单片 **`pb_single`**。

**全局规则（固件必实现）**

| 编号 | 规则 |
|------|------|
| R0 | `pb_start`/`pb_chunk`/`pb_end`/`pb_single` 中非空 `audio`/`servo`/`anim` 至少一项；纯动作/表情允许完全省略 `audio` |
| R1 | 队列决策仅 **`pb_start`**、**`pb_single`**；同 `req` 的 `pb_chunk`/`pb_end` 只续传 |
| R2 | `audio.next_bin_len > 0` → 后续一个或多个 PB binary frame 重组为等长 logical audio binary（opus batch 或 s16le PCM）；单 frame ≤ 2048 字节 |
| R3 | 协议错位 → 丢弃该 `req` 剩余片，清队列 |
| R4 | 同 `req` 内 `idx` 从 0 严格递增 |
| R5 | 有 `anim[]` 时 `sum(anim[i].ms) == chunk_ms` |
| R6 | 仅当存在 s16le `audio.next_bin_len > 0` 时，PCM 字节数 `== (chunk_ms * sr // 1000) * 2` |

16 kHz：`chunk_ms=113` → 3616 字节（2 个 transport fragments）；
`chunk_ms=1921` → 61472 字节（31 个 fragments）。固件的 logical PB binary
重组缓冲及声明上限须能容纳单个已声明 payload（服务端声明上限默认仍为
480000 字节，对 10s@16k s16le 的 320000 字节留有余量）；
超过实现上限时应在分配前拒绝并取消该 `req`。DBOT transport parser 每次只需接收
不超过 2048 字节的 PB fragment，而不是一次接收完整 logical payload。

---

## 4. 下行消息类型

| `type` | 用途 |
|--------|------|
| `pb_start` | 链首包（`idx=0`），触发队列；含音频时带 `sr`/`fmt`/`ch` |
| `pb_chunk` | 链中间包 |
| `pb_end` | 链末包 |
| `pb_single` | 整轮仅一条（idle、单段舵机、或口播仅一包） |
| `pb_cancel` | 中止 `req` |

多片：`pb_start`(0) → `pb_chunk`(1…N-2) → `pb_end`(N-1)。单片只发 **`pb_single`**，禁止无 `pb_start` 单发 `pb_end`。

### 4.1 公共字段

| 字段 | 说明 |
|------|------|
| `req` | 序列 ID（16 位 hex 常见） |
| `idx` | 分片序号，从 0 递增 |
| `chunk_ms` | 本片时长（ms） |
| `level` | 优先级 0–3：0 idle，1 口播，2 紧急，3 调试 |
| `action` | `replace`（默认）\| `append` \| `default` |
| `durable` | 可选布尔值；仅持久化控制操作/提醒使用 `true`。设备把终态写入有界 NVS 重放窗口，普通聊天省略并仅做本次启动内去重，避免闪存磨损和挤占提醒记录 |
| `volume` | 0–100，可选；同 `req` 后续 PCM 按此音量 |
| `camera_once` | 可选布尔；`true` 请求一帧新鲜 JPEG，不改变常态节奏 |
| `cam_fps` | 浏览器预览连续流帧率；仅由 PC 租约控制器下发（见 §2.2），0 为停止 |

忽略以 `_` 开头的键。

### 4.2 时序示例

```
→ PB JSON channel: pb_start  audio.next_bin_len=N  chunk_ms=T  anim[…]
→ PB binary channel: fragment 0（≤2048 字节）
→ PB binary channel: fragment 1…K（累计精确等于 N 字节 PCM）
→ PB JSON channel: pb_chunk …
→ PB binary fragments: …
→ PB JSON channel: pb_end …
→ PB binary fragments: …
```

### 4.3 无音频动作/表情

用户只要求动作或表情且没有口播文本时，PC 必须发送真正的 JSON-only PB，不得合成
句号 TTS，也不得按 `chunk_ms` 补零 PCM。此类消息：

- 保留完整的 `servo[]` / `anim[]` 时间轴和 `chunk_ms`；
- **省略** `audio`、`sr`、`fmt`、`ch`（不要写
  `audio: {"next_bin_len": 0}`）；
- JSON 后不跟 audio binary；
- 设备仍先回 `phase=accepted`，并在所有已提交的 display/motor worker 完成后回
  `phase=played`（失败或被替换则回 `failed` / `cancelled`）。

```json
{
  "type": "pb_single",
  "req": "gesture-01",
  "idx": 0,
  "chunk_ms": 800,
  "pb_ver": 2,
  "action": "replace",
  "level": 1,
  "anim": [{"elements": {}, "ms": 800}],
  "servo": [
    {"xm": 0, "ym": 0, "x": 90, "y": 70, "ms": 400},
    {"xm": 0, "ym": 0, "x": 90, "y": 90, "ms": 400}
  ]
}
```

固件 R0 的无音频分支会直接把 display/motor 绑定到同一绝对时间轴；终态屏障把
`audio_expected=false` 视为音频已完成，但仍等待每个实际提交的表情和舵机任务。

---

## 5. 动画 `anim[]`

`anim` **必须是数组**。每项：

| 字段 | 说明 |
|------|------|
| `elements` | 图层容器（见下表） |
| `ms` | 该段子动画时长（≥1） |
| `phoneme` | 可选，音素符号（调试） |

### 5.1 `elements` 图层

| 键 | 说明 |
|----|------|
| `bg` | 背景（最先绘制） |
| `nose` | 鼻 |
| `mouth` | 口型 |
| `eye_l` / `eye_r` | 左/右眼 |
| `extra` | 装饰（腮红、文字等） |

### 5.2 片内时间轴

```
t = 0
for k in 0 .. anim.length-1:
  在 [t, t + anim[k].ms) 绘制 anim[k].elements
  t += anim[k].ms
```

有 PCM 时子动画切换与采样边界对齐。

### 5.3 绘制顺序

**`bg` → `nose` → `mouth` → `eye_l` → `eye_r` → `extra`**

### 5.4 图元与颜色

每个图元须有 **`shape`**（比较前转小写、做别名归一化）。wire 上颜色为 **`c`（RGB565 整数）**；配置 JSON 可写 `#RGB` / 命名色，服务端转换；缺省 **65535（白）**。

```
R5 = (R8 >> 3) & 0x1F;  G6 = (G8 >> 2) & 0x3F;  B5 = (B8 >> 3) & 0x1F
c  = (R5 << 11) | (G6 << 5) | B5
```

未知 `shape`：**跳过**该图元，不判整包失败。

### 5.5 `shape` 对照表

| 主 `shape` | 别名（等价） | 必填字段 |
|------------|--------------|----------|
| `rect` | `fill_rect`, `fillRect` | `x`,`y`,`w`,`h` |
| `rect_outline` | `draw_rect`, `drawRect` | 同上 |
| `circle` | `fill_circle`, `fillCircle` | `x`,`y`,`r` |
| `circle_outline` | `draw_circle`, `drawCircle` | 同上 |
| `line` | `drawLine`, `draw_line` | `x1`,`y1`,`x2`,`y2` |
| `pixel` | `point`, `drawPixel` | `x`,`y` |
| `hline` | `h_line`, `drawFastHLine` | `x`,`y`,`w` |
| `vline` | `v_line`, `drawFastVLine` | `x`,`y`,`h` |
| `ellipse` / `ellipse_fill` | `drawEllipse` / `fillEllipse` | `x`,`y` + (`rw`,`rh` 或 `w`,`h` 作半轴) |
| `triangle` / `triangle_fill` | `drawTriangle` / `fillTriangle` | `x0,y0,x1,y1,x2,y2` 或第一点 `x,y` |
| `round_rect` / `round_rect_outline` | `fillRoundRect` / `drawRoundRect` | `x`,`y`,`w`,`h`, `radius` 或 `r` |
| `rotated_rect_outline` / `rotated_rect_fill` | — | `x`,`y`,`w`,`h`,`angle`（**中心**坐标） |
| `text` | — | `x`,`y`,`text`,`size`,`c` |
| `image` | — | `asset`（0-based 下标）+ 见 §6.2 |

三角形勿与 `rect` 的 `w`,`h` 混淆。

### 5.6 可选 `assets[]`（JPEG 等）

若存在 `assets[]`，读完 PCM 后按 `assets[i].next_bin_len` 依次读 binary；`shape: image` 的 `asset` 指向下标。

---

## 6. 音频与舵机

### 6.1 PCM

```json
"audio": { "next_bin_len": 61472 }
```

| `sr`/`fmt`/`ch` 以本 `req` **首条含音频**的包为准（当前 16000 / opus 或 s16le / 1）。

### 6.2 舵机 `servo[]`

```json
"servo": [
  { "xm": 1, "ym": 1, "x": 0, "y": 30, "ms": 380 }
]
```

| `xm`/`ym` | 0 绝对；1 相对增量；2 本轴保持 |
|-----------|------------------------------|

与 `anim[]` **并行**调度；无舵机时省略 `servo` 键。

---

## 7. 优先级队列

收到 **`pb_start` / `pb_single`** 时按 `level`（0–3）与 `action` 决策：

| 条件 | 行为 |
|------|------|
| `level` > `queue_level` | 清空队列，立即执行 |
| `level` == `queue_level` 且 `replace` | 清空后执行 |
| `append` | 追加队尾 |
| `default` | 队列中更高优先级序列 >1 条则丢弃，否则同 append |

---

## 8. 回压与取消

**上行 `pb_ack`**

```json
{
  "type": "pb_ack",
  "req": "a1b2c3d4e5f67890",
  "idx": 2,
  "audio_buf_ms": 360
}
```

可选上行 **`servo` object**（单对象，非数组）反馈当前位置与软限位。

**下行 `pb_cancel`**

```json
{ "type": "pb_cancel", "req": "a1b2c3d4e5f67890" }
```

---

## 9. 表情配置（服务端生成 `anim[]`）

口播时服务端从 `data/local/deskbot-face.json` 的 `phonemes`/`emotions` 生成默认
**`face_bundle`**，保存后按 **mtime 热重载**。所有接入这台 PC 的设备共用该文件；
`device_id` 只决定 PB 下发到哪个 live USB session。`data/global/deskbot-face.json` 仅是
只读的首次初始化模板。显式配置 `tts.pb_face_bundle_json` 或
`DESKBOT_PB_FACE_BUNDLE_JSON` 时仍可覆盖默认整包。

| 顶层键 | 说明 |
|--------|------|
| `mouth_by_phoneme` | 音素 → 口型 `{ elements[], offset? }` |
| `mouth_by_phoneme_groups` | 共享条：`states[]` + `elements` + `offset` |
| `eye_l` / `eye_r` | `default` / `open` / `close` 图元数组 |
| `nose` | `default` 图元数组 |
| `extra` | 任意态名 → 图元数组；`metadata.extra_state` 选态 |
| `metadata.blink` | `open_ms` / `close_ms` 控制眨眼相位 |

**offset**：口型 `offset (dx,dy)` 仅平移 **鼻、眼、extra** 坐标；**嘴不动**。未知音素用 `"_"` 或内置默认。

固件只需解析 wire 上的 `anim[].elements`；编辑表情见仓库 `data/`。

---

## 10. 与旧版差异

| 项目 | 旧版 | 当前 |
|------|------|------|
| `anim` | 单对象 `{elements}` | **数组** `[{elements, ms, phoneme?}, …]` |
| `servo` | 单对象 | **数组** |
| 根级 `phoneme` | 有 | **无**（在 `anim[i].phoneme`） |
| 音频长度 | `audio.next_bin: 1` | **`audio.next_bin_len`** 字节数 |
| 颜色 | `color` 字符串 | wire **`c` RGB565** |
| 设备传输 | WebSocket `/asr_chat` + JSON/binary 帧 | USB CDC 上的 **DBOT v1** 多通道 frame |

---

## 11. 固件实现清单

1. 只在 USB CDC 上运行 DBOT v1；完成 hello/ACK、nonce、epoch 与 capabilities 握手。
2. 校验 24 字节 header、长度和双 CRC；按 channel 分流，并在坏帧后按 magic 重同步。
3. 麦克风 Opus、camera JPEG 和 LOG 使用各自上行通道；CONTROL/PB JSON 不得与
   binary 互相消费。
4. 下行处理 `pb_*`，按 `audio.next_bin_len` 从 PB binary channel 接收音频；
   断线、超时或错位时取消当前 `req` 并清理 pending binary。
5. `anim[]` 按 `ms` 切换；绘制顺序见 §5.3；按 R6 校验 PCM 长度。
6. `pb_start`/`pb_single` 入队（见 §7）；周期性上报 `pb_ack`，完成后发送
   `played` 终态。
7. 忽略未知键和 `_` 前缀键；跳过未知 `shape`。
