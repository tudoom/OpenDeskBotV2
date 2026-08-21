# Security

请勿在公开 Issue 报告未修复漏洞；请私下联系维护者并附影响、版本与最小复现。

## 信任边界

- 机器人只通过 USB CDC 与本机 `:9000` 核心进程通信，不保存 Wi-Fi、服务地址、
  API Key、配对码或设备 token。
- `device_id` 由 eFuse MAC 派生，只用于 USB/RTC/ACK 的运行时路由，不是数据作用域或凭证。
- 只有完成 DBOT hello 的 live `usb_cdc` session 才能接收真机操作；浏览器字符串不能
  伪造连接。
- `:9000` 是唯一串口进程；`:5050` 只能通过内部 HTTP API 操作设备。不要让
  两个进程或外部脚本同时打开同一串口。

## 密钥与敏感数据

- 勿提交 `.env`、供应商 Key、TLS 私钥、生产数据库或 `data/local/`。
- `data/opendesk.db` 含提醒、回执、操作记录和用量；`data/local/` 含会话、记忆、人脸、
  表情和偏好数据，均不得提交。
- ASR/LLM/TTS 供应商凭证只通过环境变量、`.env` 或受控管理界面注入，绝不能
  写入固件、`config.yaml` 示例、URL 查询串或日志。
- `ASR_API_KEY` 必须是语音转写服务的独立凭证，不能回退复用
  `LLM_API_KEY`（旧版可能为 `ARK_API_KEY`）或 TTS Key。ASR 管理 API 只返回 `api_key_set`；
  留空保留现有 Key，只有显式清除操作才删除。
- 日志不得记录认证 Header、Cookie、完整查询串或
  `Sec-WebSocket-Protocol` 中的短期调试 token。

## Web 与网络暴露

- 当前控制台只面向本机，因此 `:5050`、`:9000` 必须只监听 loopback；Web 会拒绝
  非回环 Host 和非回环监听配置，不能直接暴露到局域网或公网。
- 配置至少 32 字符随机 `DESKBOT_WEB_SECRET_KEY`，用于 Web→Core 与调试短期令牌签名。
- 当前产品没有远程控制认证边界，不得通过反向代理、端口映射或局域网地址暴露。
- WSS 仅用于 `/camera_view`、`/device_pipeline?role=subscriber` 等浏览器调试
  订阅，以及豆包/PaddleSpeech 等供应商协议。设备生产者 WebSocket 始终拒绝。
- 浏览器调试 token 默认 600 秒有效，通过 WebSocket 子协议传输；生产不得开启
  debug token 查询参数兼容。

## 本机操作边界

- 页面和本地数据 API 直接映射到唯一 `data/local/` 空间。
- Web→Core 短期令牌只保护进程间通道，不表示人类身份。
- 真机控制必须选择当前 live USB session；提醒、记忆、人脸、表情和会话不使用
  `device_id` 作为持久化作用域。
- 设备控制采用持久化 `operation_id` 和终态 ACK；客户端不能把 HTTP `202`
  误显示为设备已执行成功。

## 外部供应商与 SSRF

- 默认只允许公网 HTTPS/WSS 供应商地址，并在连接前校验解析结果，拒绝回环、
  内网、链路本地和云元数据地址。
- 云 ASR endpoint 同样执行上述 URL 与解析校验；错误响应只暴露稳定错误码、
  是否可重试和必要的状态码，不记录供应商响应原文。
- 确需自托管内网模型时，由运维使用
  `DESKBOT_PROVIDER_PRIVATE_ORIGINS` 精确允许 origin；不要允许宽泛网段。
- 定期轮换供应商 Key 与 Web 签名密钥。

## 备份与退役

- 如需保留本机数据，备份 `data/opendesk.db` 与 `data/local/`。
- 拔线只结束 live session，不删除或切换本地数据；换设备后继续使用同一备份。
- 设备从旧联网固件迁移到 USB-only 固件时，首次安装前整片擦除，保留 eFuse MAC。
