# Open Desk Bot V2 macOS 客户端

`build-client-macos.sh` 将当前 Core、Web 前端、LiveKit Server、RTC Agent、
可重定位 Python 与必要模型组装成一个 `OpenDeskBotV2.app`（Apple Silicon），
并打出 `OpenDeskBotV2.dmg`。与 Windows 客户端
（[../client/README.md](../client/README.md)）保持同一份行为契约。

客户端行为：

- `.app` 的 `Contents/Resources/runtime` 就是运行时本体，**无自解压载荷**
  （macOS 没有 Windows 那套杀软实扫开销，Win 客户端的 pyc 无源码化、stdlib
  zip、多线程解包、stable-bin 物化在这里都不需要，也刻意不搬）。
- 自动启动 Core（9000）与 Web（5050）；LiveKit（7880）和 RTC Agent（18790）
  由 Core 自行拉起。就绪判定只看 Web 与 Core；LiveKit / RTC Agent 属可选云
  语音服务，缺席只体现在菜单栏状态文案。Web 或 Core 三分钟内未就绪自动重启
  （指数退避，连续 5 次失败停手并提示看日志）。
- Web 就绪后在 WKWebView 主窗口打开控制台；首次启动检测到 `.env` 缺失（或
  无 LLM Key）时直接打开大模型设置页（`/advanced?tab=llm`）。
- 关闭窗口只隐藏，服务与菜单栏项继续运行；点 Dock 图标或菜单栏「打开
  控制台」恢复。外部链接只放行 `http`/`https` 到系统默认浏览器，其它 URI
  scheme 记日志后忽略。
- 菜单栏（NSStatusItem）提供：状态 / 打开控制台 / 重启全部服务 / 打开日志
  目录 / 退出。
- 服务子进程经 `posix_spawn` + `POSIX_SPAWN_SETSID` 成为独立进程组组长。
  停止或退出时先向 Core 进程组发 SIGTERM 并等待最多 15 秒优雅退出（Core 的
  POSIX SIGTERM 处理与 Windows SIGBREAK 是同一条完整 shutdown 链，含 RTC
  Agent 与本地 LiveKit 各 5 秒子进程收尾），超时才对整组 SIGKILL。
- 启动时若 5050/9000 被占，且监听进程的可执行文件位于本 .app 的 runtime 内
  （上次崩溃遗留），自动清理后继续；属于其它程序则报错退出。
- 配置、数据库和日志保存在 `~/Library/Application Support/OpenDeskBotV2`，
  升级 .app 不覆盖。首启从包内 `seed/` 初始化 `config.yaml`、`.env.example`
  与 `data/global`（绝不覆盖已有文件）。
- 单实例（数据目录 flock）；二次启动激活已运行实例。
- 日志：`core-*.log` / `web-*.log` 按时间戳每种保留最近 10 份；
  `launcher.log` 超 10MB 滚动为 `.1`。
- 打包的 libopus 经 `DYLD_FALLBACK_LIBRARY_PATH` 暴露给 opuslib_next，
  不遮蔽系统或 Homebrew 已有安装。ffmpeg 不打包（与 Windows 客户端一致；
  缺席只影响个别 opus 转码路径）。

构建（需要 Apple Silicon + Xcode Command Line Tools + 首次联网）：

```bash
./client-macos/build-client-macos.sh
```

输出位于 `service/dist/OpenDeskBotV2.app` 与 `service/dist/OpenDeskBotV2.dmg`
（附 `.sha256` 旁挂文件）。

可选参数：

- `--include-face-stack`：打包人脸视觉栈与 MediaPipe 人脸模型（对应
  Windows 的 `-IncludeFaceStack`）。默认构建不含人脸栈，并有反向断言烟测
  防止依赖漂移后悄悄漏回。
- `--seed-env <path>`：内部分发时打入种子 `.env`（仅首启且用户没有 `.env`
  时落地，绝不覆盖既有配置）。
- `--skip-dmg` / `--skip-smoke`：只出 .app / 跳过烟测（调试脚本用）。

外部产物全部按固定 SHA-256 校验后才进包：

| 产物 | 版本 | 来源 |
|------|------|------|
| CPython（可重定位） | 3.11.13 | astral-sh/python-build-standalone |
| livekit-server | 1.13.6 | Homebrew bottle（ghcr 内容寻址）※ |
| libopus | 1.6.1 | Homebrew bottle（ghcr 内容寻址） |
| Silero VAD 模型 | 固定 commit | 与 `tools/fetch_test_assets.py` 同源同校验 |

※ LiveKit 上游 GitHub Releases 自 1.9.x 起不再发布 darwin 二进制；1.13.6
与 Windows 锁定的 1.13.5 仅差一个 patch 版本。开发模式（`./start.sh`）可用
`./tools/install_livekit_macos.sh` 安装同一版本。

## 签名与分发

当前无 Apple Developer ID，构建做 **ad-hoc 签名**：本机构建的 .app 直接
可用；通过 DMG 分发到其它 Mac 后首次打开会被 Gatekeeper 拦截，收件人需
**右键 → 打开**（或 `xattr -dr com.apple.quarantine
/Applications/OpenDeskBotV2.app`）。拿到 Developer ID 后在构建脚本中把
`codesign --sign -` 换成正式身份并加 notarization 即可。

构建不会把 `.env`、数据库、日志或 LiveKit 本机凭据写入 .app（除非显式
`--seed-env`）。
