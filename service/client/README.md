# Open Desk Bot V2 Windows 客户端

`Build-Client.ps1` 将当前 Core、Web 前端、LiveKit Server、RTC Agent、便携
Python 和必要模型打包成一个 `OpenDeskBotV2.exe`。

客户端行为：

- 首次启动校验并释放版本化运行时到
  `%LOCALAPPDATA%\OpenDeskBotV2\runtime`；后续启动直接复用。
- 首次解包为多线程并行（每个工作线程独立打开同一 zip 只读实例），并行度
  `min(CPU, 16)`——上限取 16 而非 CPU 核数上限 8，因为目标机杀软（ESET，
  不可配排除）对每个落盘文件做同步实扫，工作线程大部分时间阻塞在扫描而非
  CPU，更多在途文件可以互相重叠扫描等待。并保留原有语义：载荷 SHA-256 校验、
  临时目录解包后原子 `Directory.Move`、Zip-Slip 路径防护。解包全程状态窗口
  实时显示「首次启动正在展开运行时（一次性，约 X 秒）：nn%」，剩余秒数按实测
  吞吐动态估算；完成后进入原有服务启动流程与文案。
- 载荷按“文件数优先”瘦身（同样因为杀软实扫开销 ≈ 文件数 × 单文件扫描）：
  - `site-packages` 无源码化：构建期用打包的便携 Python `compileall -b` 编译成
    同目录 legacy `.pyc` 后删除所有有编译产物的 `.py`（`.pyd`/`.dll`/数据文件
    不动），文件数约减半；无源 `.pyc` 导入时不做任何头校验，解包重写时间戳
    无影响。脚本内 `$sourceRetainedPackages` 名单可让个别确需运行期读自身源码
    的包保留 `.py`（当前：`cv2`——其 loader 导入时按字面文件名查找并执行
    `config*.py`）；`numpy\f2py`（Fortran 开发工具，运行期不用）整目录剔除。
  - 纯 Python 标准库压成 `python\python311.zip`（只含 `.pyc`，官方 embeddable
    风格）：`pythonXY.zip` 紧邻 `python.exe` 是 CPython 自动加入 `sys.path` 的
    landmark，因此**不写 `._pth`**——`._pth` 一旦存在解释器进入隔离路径模式，
    忽略 `PYTHONPATH`/`PYTHONHOME`，而启动器正是靠 `PYTHONPATH` 传入 `app\src`
    （稳定 bin 物化后其相对 `python.exe` 的位置不固定）。`site-packages` 与
    运行期按 `__file__` 找数据的标准库包（tkinter/idlelib/lib2to3/ensurepip/
    venv 等）保留散文件。构建内置三道硬门槛（zip 内 stdlib 启动导入、二进制
    依赖导入、launcher 式 `PYTHONPATH` 导入 app 包），任一失败自动回退为
    原样散文件 Lib——宁可慢解包也不出坏包。
  - `app\src`（业务码）保留 `.py` 源码（现场排障需要 traceback 源行），照旧
    预编译 PEP 552 unchecked-hash `__pycache__` pyc；配合运行期
    `PYTHONDONTWRITEBYTECODE=1`，用户机器不现场编译任何 Python 代码。
- 核心服务就绪后后台清理 `runtime` 下旧版本运行时和崩溃残留的临时解包目录，
  删除失败静默跳过、下次启动重试。
- 自动启动 Core（9000）、Web（5050）、LiveKit（7880）和 RTC Agent
  （18790），Web 就绪后在 EXE 自己的 WebView2 主窗口中打开页面。
- 就绪判定只看 Web（5050）与 Core（9000）；LiveKit / RTC Agent 属可选云语音
  服务，缺席只体现在托盘状态文案（未配置云凭证时提示「RTC 未就绪」），不会
  触发自动重启。只有 Web 或 Core 三分钟内未就绪才自动重启。
- 首次启动检测到 `%LOCALAPPDATA%\OpenDeskBotV2\.env` 缺失（或无 LLM Key）时，
  主窗口直接打开 Web 控制台的大模型设置页（`/advanced?tab=llm`）。
- 不使用外部浏览器；外部网站链接仍交给系统默认浏览器处理，但只放行
  `http` / `https` 链接，其它 URI scheme（如自定义协议处理器）一律记录日志
  后忽略，防止页面内容注入拉起本地协议处理程序。
- 启动时后台清理 `logs` 目录：`core-*.log` / `web-*.log` 时间戳日志每种前缀
  只保留最近 10 份，更旧的（含 `.log.1` 备份）删除，删除失败静默；单个子进
  程日志超过 20MB 时滚动为 `.log.1` 备份后重新写入。
- 托盘菜单可重新显示客户端窗口、查看日志、重启全部服务或退出。
- 客户端图标来自 `client\app.ico`（多尺寸 16-256px）：构建时经
  `csc /win32icon` 写入 EXE 外壳图标，同时以 `/resource` 内嵌，运行时托盘与
  各窗口标题栏从内嵌资源按尺寸取帧，不依赖任何外部文件。
- 停止或重启服务时先向 Core 进程组发送 CTRL_BREAK 并等待最多 15 秒优雅退出
  （Core 收到 SIGBREAK 执行完整 shutdown 序列，其中含 RTC Agent 与本地
  LiveKit 两段各最多 5 秒的子进程收尾等待），超时或失败才由 Job Object
  硬杀。
- 所有子进程进入同一个 Windows Job Object；客户端退出或崩溃后不会遗留服务。
- 配置、数据库和日志保存在 `%LOCALAPPDATA%\OpenDeskBotV2`，升级 EXE 不覆盖。
- 同一时间只运行一个客户端；第二次双击只会重新打开 Web 页面。

构建：

```powershell
& .\client\Build-Client.ps1
```

输出位于 `service\dist\OpenDeskBotV2.exe`。

## 人脸视觉栈（默认不打包）

产品默认不带人脸功能。默认构建会在 staging 后从 `site-packages` 删除人脸
视觉栈——`mediapipe` / `cv2`（opencv）/ `insightface` / `onnx` 以及**仅**被
它们拖入的传递依赖（matplotlib 链、scipy / scikit-image 链、absl-py；名单
见脚本内 `$faceStackSitePackages` 数组，与 pyproject 的 optional extra
`face` 对应），`models\mediapipe` 人脸模型也不进包（`silero_vad` 语音 VAD
模型照常打包）。`onnxruntime` **始终保留**：语音 VAD
（livekit-plugins-silero）运行时依赖它。

- 需要带人脸功能的构建：`& .\client\Build-Client.ps1 -IncludeFaceStack`。
- 三道烟测门槛中的 `deps` 关按档位分开：默认档验证
  `flask/sqlalchemy/numpy/aiohttp/livekit` 可导入，并**反向断言**子进程
  `import mediapipe/cv2/insightface` 抛 `ImportError`（防止删包名单失效后
  人脸栈悄悄漏回载荷）；`-IncludeFaceStack` 档维持原有完整验证。
- 运行时行为差异（服务端已做明确降级）：默认包中相机预览、拍照问答完全
  不受影响；人脸检测/识别/跟踪不可用（首帧记一次 warning 后关闭检测，
  不逐帧重试）；人脸注册返回明确的「人脸功能未安装」错误；Web「认识的人」
  页在无档案时提示「人脸识别组件未随本安装包提供」。
- `runtime-manifest.json` 中的 `face_stack` 字段记录构建档位。客户端为 x64，目标 Windows 需要
Microsoft Edge WebView2 Runtime（Windows 10/11 和新版 Edge 通常已安装）。构建
会自动下载固定版本的 WebView2 SDK 供编译使用，但不会联网下载浏览器运行时。
下载的 SDK 包每次构建都按脚本内置的固定 SHA-256 校验（可用 `-WebView2Sha256`
覆盖期望值），校验失败即删除文件并终止构建，不会把未经校验的 SDK 编进 EXE。

构建不会把 `.env`、数据库、日志、
LiveKit 本机凭据或 2 GB 的离线 SenseVoice 模型写入 EXE。默认也不再把构建机
的 `.env` 拷贝到本机 LocalAppData 配置目录；只有显式传 `-InstallLocalConfig`
且目标目录缺少 `.env` 时才复制（旧的 `-SkipLocalConfigInstall` 保留为无操作
的兼容别名）。该文件始终位于 EXE 外部。

## 安装器

上面的 `OpenDeskBotV2.exe` 是免安装的绿色版（开发常用）。在它之上还可产出一个
标准 Windows 安装器 `service\dist\OpenDeskBotV2-Setup.exe`，脚本为
`client\installer.nsi`（NSIS / MUI2）。

构建（先出 EXE，再打安装器）：

```powershell
& .\client\Build-Client.ps1 -BuildInstaller
```

- `-BuildInstaller` 默认关闭，不加时构建行为与之前完全一致，只出绿色版 EXE。
- `-MakeNsisPath` 指定 `makensis.exe`，默认取 Tauri 工具链自带的
  `%LOCALAPPDATA%\tauri\NSIS\makensis.exe`；找不到会报错并提示
  `winget install NSIS.NSIS`。
- 版本号取自启动器的 `AssemblyVersion`（当前 `2.0.0.0`）——即被打包的那个
  EXE 内嵌的版本，因此“程序和功能”里的显示版本始终与客户端一致；它也是
  NSIS `VIProductVersion` 需要的四段式版本。源 EXE / 图标 / LICENSE / 输出
  路径全部通过 makensis 的 `/D` 定义传入，`installer.nsi` 内用 `!define`
  兜底以便单独手动编译。
- 产物同样打印大小与 SHA-256，并写出 `.sha256` 旁挂文件，风格与 EXE 一致。

安装器行为：

- MUI2 向导，安装时选择简体中文 / English；产品名 “Open Desk Bot V2”，
  发布者 “OpenDeskBot Contributors”，安装器与已安装项图标均用 `app.ico`。
- 默认安装到 `%ProgramFiles%\OpenDeskBotV2`（x64，需管理员），把打包好的
  `OpenDeskBotV2.exe` 作为唯一释放文件写入。
- 开始菜单快捷方式必建；桌面快捷方式与“开机自启”（写 `HKCU\...\Run`）为组件
  页可选项，快捷方式图标取自 EXE 内嵌的 `app.ico`。
- 标准卸载项写入
  `HKLM\...\Uninstall\OpenDeskBotV2`（DisplayName / DisplayVersion /
  DisplayIcon / Publisher / UninstallString / EstimatedSize / NoModify /
  NoRepair）。卸载删除程序目录与快捷方式，并**弹窗询问是否一并删除用户数据**
  `%LOCALAPPDATA%\OpenDeskBotV2`（默认否，保留数据）。
- 因安装时已是提权上下文，顺手静默做掉两件原先要单独跑脚本的事，且失败均不
  阻断安装：(a) 把 `%LOCALAPPDATA%\OpenDeskBotV2` 加入 Microsoft Defender 排除
  （`Add-MpPreference`）；(b) 给安装目录的 EXE 加防火墙入站放行
  （`netsh advfirewall`）。二者在卸载时对应清理（`Remove-MpPreference`、删除
  防火墙规则）。注意：LiveKit ICE 已收敛到回环（commit 5001574），防火墙放行
  属双保险。
- 单实例安装（互斥锁）；升级覆盖前先 `taskkill` 掉在运行的客户端及其子进程，
  不改动数据目录。完成页可选“立即运行”。

关于压缩：被打包的 EXE 本身是 stub + 已压缩的 zip 运行时载荷，LZMA 再压收益
很小却在 ~363 MB 上耗时明显；脚本仍按需求用 `SetCompressor /SOLID lzma`，代价
是打包步骤较慢，可按需自行改为更快的压缩档。
