// Open Desk Bot V2 macOS 一体化客户端启动器。
//
// 与 Windows 版（client/OpenDeskBotV2Launcher.cs）保持同一份行为契约：
// 拉起 Core(:9000) / Web(:5050)，LiveKit(:7880) 与 RTC Agent(:18790) 由
// Core 自行管理；就绪判定只看 Web+Core；WKWebView 主窗口；菜单栏状态项；
// 优雅关闭先给 Core 进程组 SIGTERM 并等 15 秒，超时才 SIGKILL。
//
// 与 Windows 版的刻意差异（macOS 上不需要的机制一律不搬）：
//  - 无自解压载荷：.app 的 Contents/Resources/runtime 就是运行时本体。
//  - 无 stable-bin 物化与 DESKBOT_STABLE_BIN_DIR：那套是给 Windows 防火墙
//    按镜像路径放行用的；本机服务全部只听回环，rtc_agent_sdk 在未设该
//    环境变量时回退 sys.executable，行为正确。
//  - 无 Job Object：以 POSIX_SPAWN_SETSID 让 Core 成为进程组组长，退出时
//    对整组发信号兜底。
//
// 数据目录：~/Library/Application Support/OpenDeskBotV2（对应 %LOCALAPPDATA%）。

import AppKit
import WebKit
import Darwin

// MARK: - 常量

let kWebURL = "http://127.0.0.1:5050/"
let kLlmSetupURL = kWebURL + "advanced?tab=llm"
let kWebPort: UInt16 = 5050
let kCorePort: UInt16 = 9000
let kLiveKitPort: UInt16 = 7880
let kAgentPort: UInt16 = 18790
/// Core 优雅关闭预算：其退出路径对 RTC Agent 与本地 LiveKit 两个子进程各有
/// 最多 5 秒的串行等待，加上调度器/串口/watcher 收尾，15 秒足够（与 Win 一致）。
let kCoreGracefulExitSeconds: TimeInterval = 15
let kWebGracefulExitSeconds: TimeInterval = 3
let kStartupTimeoutSeconds: TimeInterval = 180
let kLogKeepCount = 10

// MARK: - 路径

struct LauncherPaths {
    let runtimeRoot: URL      // .app/Contents/Resources/runtime
    let appRoot: URL          // ~/Library/Application Support/OpenDeskBotV2
    let logRoot: URL

    static func resolve() -> LauncherPaths {
        let resources = Bundle.main.resourceURL
            ?? URL(fileURLWithPath: CommandLine.arguments[0])
                .deletingLastPathComponent().appendingPathComponent("../Resources")
        let runtime = resources.appendingPathComponent("runtime", isDirectory: true)
        let support = FileManager.default.urls(
            for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let appRoot = support.appendingPathComponent("OpenDeskBotV2", isDirectory: true)
        return LauncherPaths(
            runtimeRoot: runtime.standardizedFileURL,
            appRoot: appRoot,
            logRoot: appRoot.appendingPathComponent("logs", isDirectory: true))
    }

    var pythonRoot: URL { runtimeRoot.appendingPathComponent("python") }
    var pythonBinary: URL { pythonRoot.appendingPathComponent("bin/python3.11") }
    var sourceRoot: URL { runtimeRoot.appendingPathComponent("app/src") }
    var sitePackages: URL {
        pythonRoot.appendingPathComponent("lib/python3.11/site-packages")
    }
    var modelsDir: URL { runtimeRoot.appendingPathComponent("app/models") }
    var nativeLibDir: URL { runtimeRoot.appendingPathComponent("app/lib") }
    var liveKitBinary: URL {
        runtimeRoot.appendingPathComponent("app/bin/livekit-server")
    }
    var seedRoot: URL { runtimeRoot.appendingPathComponent("seed") }
}

// MARK: - 启动器日志

enum LauncherLog {
    private static var logURL: URL?
    private static let queue = DispatchQueue(label: "launcher-log")

    static func setup(_ logRoot: URL) {
        try? FileManager.default.createDirectory(
            at: logRoot, withIntermediateDirectories: true)
        let url = logRoot.appendingPathComponent("launcher.log")
        // 超过 10MB 滚动为 .1 备份（与 Win 客户端日志策略一致的简化版）。
        if let size = try? FileManager.default
            .attributesOfItem(atPath: url.path)[.size] as? Int, size > 10 * 1024 * 1024 {
            let backup = logRoot.appendingPathComponent("launcher.log.1")
            try? FileManager.default.removeItem(at: backup)
            try? FileManager.default.moveItem(at: url, to: backup)
        }
        logURL = url
    }

    static func write(_ message: String) {
        let formatter = ISO8601DateFormatter()
        let line = "[\(formatter.string(from: Date()))] \(message)\n"
        queue.async {
            guard let url = logURL,
                  let data = line.data(using: .utf8) else { return }
            if let handle = try? FileHandle(forWritingTo: url) {
                defer { try? handle.close() }
                _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? data.write(to: url)
            }
        }
        NSLog("OpenDeskBotV2: %@", message)
    }
}

// MARK: - 首启资料初始化（对应 ProfileInitializer）

enum ProfileInitializer {
    static func ensureInitialized(_ paths: LauncherPaths) throws {
        let fm = FileManager.default
        try fm.createDirectory(at: paths.appRoot, withIntermediateDirectories: true)
        let seed = paths.seedRoot
        try copyIfMissing(
            seed.appendingPathComponent("config.yaml"),
            paths.appRoot.appendingPathComponent("config.yaml"))
        try copyIfMissing(
            seed.appendingPathComponent(".env.example"),
            paths.appRoot.appendingPathComponent(".env.example"))
        // 内部分发包可携带预置凭证 seed/.env：仅在用户还没有 .env 时落地。
        let seedEnv = seed.appendingPathComponent(".env")
        if fm.fileExists(atPath: seedEnv.path) {
            try copyIfMissing(seedEnv, paths.appRoot.appendingPathComponent(".env"))
        }
        // 内部分发包可在 seed/ 顶层放任意附加文件（如企业 CA 证书包），首启一并落地，不覆盖既有文件。
        try copyTopLevelFilesIfMissing(
            seed, paths.appRoot, skipping: ["config.yaml", ".env.example", ".env"])
        try copyTreeIfMissing(
            seed.appendingPathComponent("data/global"),
            paths.appRoot.appendingPathComponent("data/global"))
        try fm.createDirectory(
            at: paths.appRoot.appendingPathComponent("data/local"),
            withIntermediateDirectories: true)
        try fm.createDirectory(at: paths.logRoot, withIntermediateDirectories: true)
    }

    private static func copyIfMissing(_ source: URL, _ destination: URL) throws {
        let fm = FileManager.default
        guard !fm.fileExists(atPath: destination.path) else { return }
        guard fm.fileExists(atPath: source.path) else {
            throw NSError(
                domain: "OpenDeskBotV2", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "缺少种子文件: \(source.path)"])
        }
        try fm.createDirectory(
            at: destination.deletingLastPathComponent(),
            withIntermediateDirectories: true)
        try fm.copyItem(at: source, to: destination)
    }

    private static func copyTopLevelFilesIfMissing(
        _ sourceRoot: URL, _ destRoot: URL, skipping: Set<String>
    ) throws {
        let fm = FileManager.default
        guard let items = try? fm.contentsOfDirectory(
            at: sourceRoot, includingPropertiesForKeys: [.isRegularFileKey], options: [])
        else { return }
        for item in items {
            let name = item.lastPathComponent
            if skipping.contains(name) || name == ".DS_Store" { continue }
            let values = try item.resourceValues(forKeys: [.isRegularFileKey])
            guard values.isRegularFile == true else { continue }
            try copyIfMissing(item, destRoot.appendingPathComponent(name))
        }
    }

    private static func copyTreeIfMissing(_ sourceRoot: URL, _ destRoot: URL) throws {
        let fm = FileManager.default
        guard let walker = fm.enumerator(
            at: sourceRoot, includingPropertiesForKeys: [.isRegularFileKey]) else {
            throw NSError(
                domain: "OpenDeskBotV2", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "缺少种子目录: \(sourceRoot.path)"])
        }
        let prefix = sourceRoot.standardizedFileURL.path
        for case let item as URL in walker {
            let values = try item.resourceValues(forKeys: [.isRegularFileKey])
            guard values.isRegularFile == true else { continue }
            let relative = String(item.standardizedFileURL.path.dropFirst(prefix.count))
                .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            try copyIfMissing(item, destRoot.appendingPathComponent(relative))
        }
    }
}

// MARK: - 日志轮转（对应 ClientLogRotation）

enum ClientLogRotation {
    static func cleanInBackground(_ logRoot: URL) {
        DispatchQueue.global(qos: .utility).async {
            for prefix in ["core-", "web-"] {
                clean(logRoot, prefix: prefix)
            }
        }
    }

    private static func clean(_ logRoot: URL, prefix: String) {
        let fm = FileManager.default
        guard let entries = try? fm.contentsOfDirectory(
            at: logRoot, includingPropertiesForKeys: nil) else { return }
        // 时间戳命名（yyyyMMdd-HHmmss）按文件名排序即按时间排序。
        let matching = entries
            .filter { $0.lastPathComponent.hasPrefix(prefix) }
            .sorted { $0.lastPathComponent > $1.lastPathComponent }
        for stale in matching.dropFirst(kLogKeepCount) {
            try? fm.removeItem(at: stale)
        }
    }
}

// MARK: - 端口探测

func portIsListening(_ port: UInt16) -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    guard fd >= 0 else { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let flags = fcntl(fd, F_GETFL, 0)
    _ = fcntl(fd, F_SETFL, flags | O_NONBLOCK)
    let result = withUnsafePointer(to: &addr) { pointer in
        pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    if result == 0 { return true }
    guard errno == EINPROGRESS else { return false }
    var pollTarget = pollfd(fd: fd, events: Int16(POLLOUT), revents: 0)
    guard poll(&pollTarget, 1, 250) == 1 else { return false }
    var socketError: Int32 = -1
    var length = socklen_t(MemoryLayout<Int32>.size)
    guard getsockopt(fd, SOL_SOCKET, SO_ERROR, &socketError, &length) == 0 else {
        return false
    }
    return socketError == 0
}

// MARK: - 子进程（posix_spawn + setsid + 日志重定向）

/// spawn.h 的 POSIX_SPAWN_SETSID 宏（Swift 不导入 #define 常量）。
let kPosixSpawnSetSid: Int16 = 0x0400

struct ChildProcess {
    let pid: pid_t
    var exited = false
    var exitStatus: Int32 = 0

    mutating func isAlive() -> Bool {
        if exited { return false }
        var status: Int32 = 0
        let reaped = waitpid(pid, &status, WNOHANG)
        if reaped == pid {
            exited = true
            exitStatus = status
            return false
        }
        return kill(pid, 0) == 0
    }

    /// 向整个进程组发信号（setsid 后 pid == pgid）。
    func signalGroup(_ signalNumber: Int32) {
        _ = killpg(pid, signalNumber)
    }
}

enum SpawnError: Error, CustomStringConvertible {
    case failed(String, Int32)
    var description: String {
        if case let .failed(what, code) = self {
            return "\(what) 失败 (errno=\(code) \(String(cString: strerror(code))))"
        }
        return "spawn failed"
    }
}

func spawnPythonModule(
    module: String,
    paths: LauncherPaths,
    environment: [String: String],
    logFile: URL
) throws -> ChildProcess {
    let python = paths.pythonBinary.path
    let argv = [python, "-u", "-m", module]

    let logFd = open(
        logFile.path, O_WRONLY | O_CREAT | O_APPEND, 0o644)
    guard logFd >= 0 else {
        throw SpawnError.failed("打开日志文件 \(logFile.path)", errno)
    }
    defer { close(logFd) }

    var fileActions: posix_spawn_file_actions_t?
    posix_spawn_file_actions_init(&fileActions)
    defer { posix_spawn_file_actions_destroy(&fileActions) }
    posix_spawn_file_actions_addopen(&fileActions, 0, "/dev/null", O_RDONLY, 0)
    posix_spawn_file_actions_adddup2(&fileActions, logFd, 1)
    posix_spawn_file_actions_adddup2(&fileActions, logFd, 2)
    // 工作目录 = appRoot（与 Win 版 start.WorkingDirectory 一致）。
    posix_spawn_file_actions_addchdir_np(&fileActions, paths.appRoot.path)

    var attributes: posix_spawnattr_t?
    posix_spawnattr_init(&attributes)
    defer { posix_spawnattr_destroy(&attributes) }
    // SETSID：子进程成为新会话/进程组组长，停止时可对整组发信号；
    // 同时恢复默认信号处置，避免继承 GUI 进程的屏蔽字。
    posix_spawnattr_setflags(
        &attributes, kPosixSpawnSetSid | Int16(POSIX_SPAWN_SETSIGDEF))
    var defaultSignals = sigset_t()
    sigfillset(&defaultSignals)
    posix_spawnattr_setsigdefault(&attributes, &defaultSignals)

    var argvPointers = argv.map { strdup($0) }
    argvPointers.append(nil)
    defer { argvPointers.forEach { free($0) } }
    var envPointers = environment.map { strdup("\($0.key)=\($0.value)") }
    envPointers.append(nil)
    defer { envPointers.forEach { free($0) } }

    var pid: pid_t = 0
    let rc = posix_spawn(&pid, python, &fileActions, &attributes,
                         &argvPointers, &envPointers)
    guard rc == 0 else {
        throw SpawnError.failed("启动 \(module)", rc)
    }
    LauncherLog.write("spawned \(module) pid=\(pid)")
    return ChildProcess(pid: pid)
}

// MARK: - 服务环境变量（对应 BuildPythonStartInfo 的契约）

func makeSecretKey() -> String {
    var bytes = [UInt8](repeating: 0, count: 48)
    _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
    return Data(bytes).base64EncodedString()
        .replacingOccurrences(of: "+", with: "-")
        .replacingOccurrences(of: "/", with: "_")
        .replacingOccurrences(of: "=", with: "")
}

func buildServiceEnvironment(
    paths: LauncherPaths,
    sharedSecret: String,
    serverLogName: String
) -> [String: String] {
    var env = ProcessInfo.processInfo.environment
    let pythonRoot = paths.pythonRoot.path

    env["PYTHONHOME"] = pythonRoot
    env["PYTHONPATH"] = "\(paths.sourceRoot.path):\(paths.sitePackages.path)"
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["VIRTUAL_ENV"] = pythonRoot
    env["PATH"] = "\(pythonRoot)/bin:" + (env["PATH"] ?? "/usr/bin:/bin")
    // opuslib_next 经 ctypes find_library/dlopen 找 libopus；随包 dylib 用
    // fallback 路径暴露，不遮蔽系统/Homebrew 已有安装。
    env["DYLD_FALLBACK_LIBRARY_PATH"] = paths.nativeLibDir.path

    env["DESKBOT_CLIENT_MODE"] = "1"
    env["DESKBOT_PROJECT_ROOT"] = paths.appRoot.path
    env["DESKBOT_DATA_DIR"] = paths.appRoot.appendingPathComponent("data").path
    env["DESKBOT_MODELS_DIR"] = paths.modelsDir.path
    env["DESKBOT_SERVER_CONFIG"] =
        paths.appRoot.appendingPathComponent("config.yaml").path
    env["DESKBOT_ENV_FILE"] = paths.appRoot.appendingPathComponent(".env").path
    env["DESKBOT_DB_PATH"] =
        paths.appRoot.appendingPathComponent("data/opendesk.db").path
    env["DESKBOT_LOCAL_LIVEKIT_BINARY"] = paths.liveKitBinary.path
    env["DESKBOT_SERVER_HOST"] = "127.0.0.1"
    env["DESKBOT_SERVER_PORT"] = "9000"
    env["DESKBOT_WEB_HOST"] = "127.0.0.1"
    env["DESKBOT_WEB_PORT"] = "5050"
    env["DESKBOT_WEB_DEBUG"] = "0"
    env["DESKBOT_USB_SERIAL_ENABLED"] = "1"
    env["DESKBOT_SERVER_LOG_FILE"] =
        paths.logRoot.appendingPathComponent(serverLogName).path
    env["DESKBOT_WEB_SECRET_KEY"] = sharedSecret
    return env
}

// MARK: - .env 云凭证探测（对应 CloudCredentialsConfigured）

final class EnvCredentialProbe {
    private let envURL: URL
    private var cachedValid = false
    private var cachedHasCredentials = false
    private var cachedModified: Date?

    init(appRoot: URL) {
        envURL = appRoot.appendingPathComponent(".env")
    }

    func cloudCredentialsConfigured() -> Bool {
        let fm = FileManager.default
        guard fm.fileExists(atPath: envURL.path) else {
            cachedValid = false
            return false
        }
        let modified = (try? fm.attributesOfItem(atPath: envURL.path)[.modificationDate])
            as? Date
        if !cachedValid || modified != cachedModified {
            cachedHasCredentials = Self.fileHasCloudKey(envURL)
            cachedModified = modified
            cachedValid = true
        }
        return cachedHasCredentials
    }

    /// 与 rtc_agent_sdk._can_start 对齐：任一非空 LLM Key 即视为已配置。
    private static func fileHasCloudKey(_ url: URL) -> Bool {
        guard let content = try? String(contentsOf: url, encoding: .utf8) else {
            return false
        }
        for rawLine in content.split(whereSeparator: \.isNewline) {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            guard let separator = line.firstIndex(of: "="),
                  separator != line.startIndex else { continue }
            let name = String(line[..<separator]).trimmingCharacters(in: .whitespaces)
            var value = String(line[line.index(after: separator)...])
                .trimmingCharacters(in: .whitespaces)
            value = value.trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
            if value.isEmpty { continue }
            for key in ["LLM_API_KEY", "ARK_API_KEY", "OPENAI_API_KEY"]
            where name.caseInsensitiveCompare(key) == .orderedSame {
                return true
            }
        }
        return false
    }
}

// MARK: - 残留服务清理

/// 端口被占时，若监听进程的可执行文件位于本 .app 的 runtime 内（上次崩溃
/// 遗留），直接清掉；属于其它程序则不动，交由上层报错。
enum StaleServiceCleaner {
    static func killStaleListeners(paths: LauncherPaths, ports: [UInt16]) {
        for port in ports {
            for pid in listenerPids(port) {
                var pathBuffer = [CChar](repeating: 0, count: 4 * 1024)
                let length = proc_pidpath(pid, &pathBuffer, UInt32(pathBuffer.count))
                guard length > 0 else { continue }
                let executable = String(cString: pathBuffer)
                if executable.hasPrefix(paths.runtimeRoot.path) {
                    LauncherLog.write(
                        "killing stale listener pid=\(pid) exe=\(executable)")
                    _ = killpg(pid, SIGKILL)
                    _ = kill(pid, SIGKILL)
                }
            }
        }
    }

    private static func listenerPids(_ port: UInt16) -> [pid_t] {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        process.arguments = ["-ti", "tcp:\(port)", "-sTCP:LISTEN"]
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return [] }
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let text = String(data: data, encoding: .utf8) else { return [] }
        return text.split(whereSeparator: \.isNewline).compactMap { pid_t($0) }
    }
}

// MARK: - 主窗口（WKWebView）

final class MainWindowController: NSObject, NSWindowDelegate,
    WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow?
    private var webView: WKWebView?

    func show(at urlString: String) {
        if window == nil {
            createWindow()
        }
        if let url = URL(string: urlString) {
            webView?.load(URLRequest(url: url))
        }
        window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func showExisting() {
        guard let window else {
            show(at: kWebURL)
            return
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func close() {
        window?.orderOut(nil)
    }

    private func createWindow() {
        let configuration = WKWebViewConfiguration()
        configuration.mediaTypesRequiringUserActionForPlayback = []
        if configuration.preferences.responds(
            to: Selector(("setDeveloperExtrasEnabled:"))) {
            configuration.preferences.setValue(
                true, forKey: "developerExtrasEnabled")
        }
        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1280, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false)
        window.title = "Open Desk Bot V2"
        window.minSize = NSSize(width: 980, height: 640)
        window.center()
        window.contentView = webView
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.setFrameAutosaveName("OpenDeskBotV2Main")

        self.window = window
        self.webView = webView
    }

    // 关闭窗口只隐藏，服务与菜单栏项继续运行（对应 Win 最小化到托盘）。
    func windowShouldClose(_ sender: NSWindow) -> Bool {
        sender.orderOut(nil)
        return false
    }

    // WKWebView 默认不弹 JS 对话框：不实现这三个回调时 confirm() 直接返回
    // false、alert() 静默吞掉——控制台里「固件更新」「恢复出厂口型」「删除」
    // 等都以 confirm 开头，表现就是"点了没反应"。用 NSAlert 落地为原生弹窗。
    func webView(
        _ webView: WKWebView,
        runJavaScriptAlertPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping () -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "好")
        alert.runModal()
        completionHandler()
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptConfirmPanelWithMessage message: String,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (Bool) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = message
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        completionHandler(alert.runModal() == .alertFirstButtonReturn)
    }

    func webView(
        _ webView: WKWebView,
        runJavaScriptTextInputPanelWithPrompt prompt: String,
        defaultText: String?,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let alert = NSAlert()
        alert.messageText = prompt
        alert.addButton(withTitle: "确定")
        alert.addButton(withTitle: "取消")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = defaultText ?? ""
        alert.accessoryView = field
        alert.window.initialFirstResponder = field
        completionHandler(alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil)
    }

    // 没有这个委托时，WKWebView 里点 <input type="file"> 不会弹任何窗口，
    // 表现为按钮"点不动"（2026-09-03：声音复刻的「选择训练音频」）。
    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = parameters.allowsDirectories
        panel.allowsMultipleSelection = parameters.allowsMultipleSelection
        panel.resolvesAliases = true
        // 页面自己用 accept 限定类型；这里不再二次过滤，避免 .pcm 这类
        // 系统不认识的扩展名被面板挡掉。
        panel.begin { response in
            completionHandler(response == .OK ? panel.urls : nil)
        }
    }

    // 只放行本机控制台导航；外部 http/https 交系统浏览器；其余 scheme
    // 记日志后忽略（防页面内容注入拉起本地协议处理程序，与 Win 一致）。
    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url else {
            decisionHandler(.cancel)
            return
        }
        if isConsoleURL(url) {
            decisionHandler(.allow)
            return
        }
        decisionHandler(.cancel)
        openExternal(url)
    }

    func webView(
        _ webView: WKWebView,
        createWebViewWith configuration: WKWebViewConfiguration,
        for navigationAction: WKNavigationAction,
        windowFeatures: WKWindowFeatures
    ) -> WKWebView? {
        if let url = navigationAction.request.url {
            if isConsoleURL(url) {
                webView.load(URLRequest(url: url))
            } else {
                openExternal(url)
            }
        }
        return nil
    }

    private func isConsoleURL(_ url: URL) -> Bool {
        guard let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else { return false }
        let host = url.host?.lowercased()
        return host == "127.0.0.1" || host == "localhost"
    }

    private func openExternal(_ url: URL) {
        guard let scheme = url.scheme?.lowercased(),
              scheme == "http" || scheme == "https" else {
            LauncherLog.write("blocked non-http(s) url: \(url.absoluteString)")
            return
        }
        NSWorkspace.shared.open(url)
    }
}

// MARK: - 启动状态小窗（对应 StatusForm）

final class StatusWindowController {
    private var window: NSWindow?
    private var label: NSTextField?
    private var spinner: NSProgressIndicator?

    func show(_ message: String) {
        if window == nil {
            createWindow()
        }
        label?.stringValue = message
        spinner?.startAnimation(nil)
        window?.makeKeyAndOrderFront(nil)
    }

    func update(_ message: String) {
        label?.stringValue = message
    }

    func hide() {
        spinner?.stopAnimation(nil)
        window?.orderOut(nil)
    }

    private func createWindow() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 420, height: 110),
            styleMask: [.titled],
            backing: .buffered,
            defer: false)
        window.title = "Open Desk Bot V2"
        window.center()
        window.isReleasedWhenClosed = false

        let content = NSView(frame: window.contentLayoutRect)
        let spinner = NSProgressIndicator(
            frame: NSRect(x: 24, y: 44, width: 24, height: 24))
        spinner.style = .spinning
        spinner.controlSize = .regular
        let label = NSTextField(labelWithString: "正在启动…")
        label.frame = NSRect(x: 64, y: 40, width: 332, height: 32)
        label.lineBreakMode = .byWordWrapping
        label.maximumNumberOfLines = 2
        content.addSubview(spinner)
        content.addSubview(label)
        window.contentView = content

        self.window = window
        self.label = label
        self.spinner = spinner
    }
}

// MARK: - 服务管理（对应 ClientContext 的核心逻辑）

final class ServiceManager {
    private let paths: LauncherPaths
    private let sharedSecret = makeSecretKey()
    private let envProbe: EnvCredentialProbe

    private var coreProcess: ChildProcess?
    private var webProcess: ChildProcess?
    private var monitorTimer: Timer?

    private var shuttingDown = false
    private var stoppingServices = false
    private var restartPending = false
    private var mainWindowOpened = false
    private var reportedReady = false
    private var reportedAllReady = false
    private var consecutiveFailures = 0
    private var restartAt = Date.distantFuture
    private var servicesStarted = Date()

    var onStatusChanged: ((String) -> Void)?
    var onOpenMainWindow: ((String) -> Void)?
    var onStartupUpdate: ((String, Bool) -> Void)?   // (文案, 显示状态窗?)

    init(paths: LauncherPaths) {
        self.paths = paths
        self.envProbe = EnvCredentialProbe(appRoot: paths.appRoot)
    }

    func startAndMonitor() throws {
        try startServices()
        let timer = Timer(timeInterval: 0.75, repeats: true) { [weak self] _ in
            self?.monitorTick()
        }
        RunLoop.main.add(timer, forMode: .common)
        monitorTimer = timer
    }

    private func startServices() throws {
        if portIsListening(kWebPort) || portIsListening(kCorePort) {
            StaleServiceCleaner.killStaleListeners(
                paths: paths, ports: [kWebPort, kCorePort])
            Thread.sleep(forTimeInterval: 1.5)
            if portIsListening(kWebPort) || portIsListening(kCorePort) {
                throw NSError(
                    domain: "OpenDeskBotV2", code: 2,
                    userInfo: [NSLocalizedDescriptionKey:
                        "5050 或 9000 端口已被其它程序占用。请先关闭旧版服务后重试。"])
            }
        }

        stoppingServices = false
        restartPending = false
        reportedReady = false
        reportedAllReady = false
        mainWindowOpened = false
        servicesStarted = Date()
        onStatusChanged?("状态：正在启动")
        onStartupUpdate?("正在启动 Web、Core、LiveKit 与 RTC Agent…", true)

        let formatter = DateFormatter()
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        formatter.locale = Locale(identifier: "en_US_POSIX")
        let stamp = formatter.string(from: Date())

        do {
            coreProcess = try spawnPythonModule(
                module: "deskbot_server",
                paths: paths,
                environment: buildServiceEnvironment(
                    paths: paths, sharedSecret: sharedSecret,
                    serverLogName: "core.log"),
                logFile: paths.logRoot.appendingPathComponent("core-\(stamp).log"))
            webProcess = try spawnPythonModule(
                module: "deskbot_server.web",
                paths: paths,
                environment: buildServiceEnvironment(
                    paths: paths, sharedSecret: sharedSecret,
                    serverLogName: "web.log"),
                logFile: paths.logRoot.appendingPathComponent("web-\(stamp).log"))
            LauncherLog.write(
                "services launched core_pid=\(coreProcess!.pid) " +
                "web_pid=\(webProcess!.pid)")
        } catch {
            stopServices()
            throw error
        }
    }

    private func monitorTick() {
        if shuttingDown || stoppingServices { return }
        if restartPending {
            if Date() >= restartAt {
                tryScheduledRestart()
            }
            return
        }

        let coreAlive = coreProcess?.isAlive() ?? false
        let webAlive = webProcess?.isAlive() ?? false
        if !coreAlive || !webAlive {
            scheduleRestart("服务进程意外退出")
            return
        }

        let webReady = portIsListening(kWebPort)
        let coreReady = portIsListening(kCorePort)
        let liveKitReady = portIsListening(kLiveKitPort)
        let agentReady = portIsListening(kAgentPort)

        if webReady && !mainWindowOpened {
            mainWindowOpened = true
            // 首启没有 .env（或无 LLM Key）时直接落到大模型设置页。
            let url = envProbe.cloudCredentialsConfigured() ? kWebURL : kLlmSetupURL
            onOpenMainWindow?(url)
            onStartupUpdate?("", false)
        }

        // 就绪分两档：Web+Core 是硬性要求；LiveKit 与 RTC Agent 属可选云
        // 语音服务（未配置凭证时 Agent 不监听），缺席只影响状态文案。
        if webReady && coreReady {
            if !reportedReady {
                reportedReady = true
                consecutiveFailures = 0
                onStartupUpdate?("", false)
                LauncherLog.write(
                    "core services are listening (web=5050 core=9000)")
            }
            if liveKitReady && agentReady {
                onStatusChanged?("状态：全部服务运行中")
                if !reportedAllReady {
                    reportedAllReady = true
                    LauncherLog.write("all services are listening")
                }
            } else if !envProbe.cloudCredentialsConfigured() {
                onStatusChanged?("状态：RTC 未就绪：未配置云服务凭证")
            } else {
                onStatusChanged?("状态：语音服务启动中…")
            }
        } else if webReady {
            onStatusChanged?("状态：控制台已启动，核心服务准备中")
        } else {
            onStatusChanged?("状态：正在启动全部服务")
        }

        if !reportedReady,
           Date().timeIntervalSince(servicesStarted) > kStartupTimeoutSeconds {
            scheduleRestart("Web 或 Core 服务在三分钟内未就绪")
        }
    }

    private func scheduleRestart(_ reason: String) {
        LauncherLog.write(reason)
        stopServices()
        consecutiveFailures += 1
        if consecutiveFailures > 5 {
            onStatusChanged?("状态：启动失败，请查看日志后手动重启")
            onStartupUpdate?("服务连续重启失败，请从菜单栏打开日志目录。", true)
            return
        }
        let delaySeconds = min(30, 1 << min(consecutiveFailures, 5))
        restartAt = Date().addingTimeInterval(TimeInterval(delaySeconds))
        restartPending = true
        onStatusChanged?("状态：将在 \(delaySeconds) 秒后自动重启")
        onStartupUpdate?("服务异常，正在自动恢复…", true)
    }

    private func tryScheduledRestart() {
        restartPending = false
        do {
            try startServices()
        } catch {
            LauncherLog.write("scheduled restart failed: \(error)")
            scheduleRestart("自动重启失败：\(error.localizedDescription)")
        }
    }

    func restartServices() {
        if shuttingDown { return }
        LauncherLog.write("manual service restart requested")
        consecutiveFailures = 0
        restartPending = false
        stopServices()
        do {
            try startServices()
        } catch {
            LauncherLog.write("manual restart failed: \(error)")
            scheduleRestart("手动重启失败：\(error.localizedDescription)")
        }
    }

    func stopServices() {
        stoppingServices = true
        stopCoreGracefully()
        // Web 是无状态 Flask，SIGTERM 短等后硬杀即可（Win 版直接 Job 杀）。
        if var web = webProcess, web.isAlive() {
            web.signalGroup(SIGTERM)
            waitForExit(&web, timeout: kWebGracefulExitSeconds)
            if web.isAlive() {
                web.signalGroup(SIGKILL)
                waitForExit(&web, timeout: 2)
            }
            webProcess = web
        }
        if var core = coreProcess, core.isAlive() {
            core.signalGroup(SIGKILL)
            waitForExit(&core, timeout: 2)
            coreProcess = core
        }
        coreProcess = nil
        webProcess = nil
        // Job Object 等价兜底：Core 用 start_new_session 拉起的 livekit-server
        // （及可能的 agent worker）在独立会话里，上面的组信号打不到。凡是
        // 命令行里带本 .app runtime 路径的进程一律清掉；启动器自身位于
        // Contents/MacOS，不受影响。
        sweepRuntimeProcesses()
        stoppingServices = false
    }

    private func sweepRuntimeProcesses() {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        process.arguments = ["-9", "-f", paths.runtimeRoot.path]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        guard (try? process.run()) != nil else { return }
        process.waitUntilExit()
        if process.terminationStatus == 0 {
            LauncherLog.write("swept leftover runtime processes")
        }
    }

    /// 先给 Core 进程组 SIGTERM 触发完整 shutdown 序列（设备/USB 会话收尾、
    /// RTC Agent 与本地 LiveKit 子进程各 5 秒等待），15 秒未退才由上面硬杀。
    private func stopCoreGracefully() {
        guard var core = coreProcess, core.isAlive() else { return }
        LauncherLog.write("sending SIGTERM to core process group \(core.pid)")
        core.signalGroup(SIGTERM)
        waitForExit(&core, timeout: kCoreGracefulExitSeconds)
        if core.isAlive() {
            LauncherLog.write("core graceful shutdown timed out; killing group")
        } else {
            LauncherLog.write("core exited gracefully")
        }
        coreProcess = core
    }

    private func waitForExit(_ process: inout ChildProcess, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if !process.isAlive() { return }
            Thread.sleep(forTimeInterval: 0.1)
        }
    }

    func shutdown(completion: @escaping () -> Void) {
        if shuttingDown {
            completion()
            return
        }
        shuttingDown = true
        monitorTimer?.invalidate()
        monitorTimer = nil
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.stopServices()
            DispatchQueue.main.async(execute: completion)
        }
    }
}

// MARK: - 应用委托

final class AppDelegate: NSObject, NSApplicationDelegate {
    private let paths = LauncherPaths.resolve()
    private var serviceManager: ServiceManager?
    private var statusItem: NSStatusItem?
    private var statusMenuItem: NSMenuItem?
    private let mainWindow = MainWindowController()
    private let statusWindow = StatusWindowController()
    private var lockFileDescriptor: Int32 = -1

    func applicationDidFinishLaunching(_ notification: Notification) {
        try? FileManager.default.createDirectory(
            at: paths.appRoot, withIntermediateDirectories: true)
        LauncherLog.setup(paths.logRoot)
        LauncherLog.write("launcher starting; runtime=\(paths.runtimeRoot.path)")

        guard acquireSingleInstanceLock() else {
            LauncherLog.write("another instance is running; activating it")
            activateExistingInstance()
            NSApp.terminate(nil)
            return
        }

        guard FileManager.default.fileExists(atPath: paths.pythonBinary.path) else {
            fatalStartupError(
                "运行时不完整：缺少 \(paths.pythonBinary.path)\n" +
                "请重新安装 Open Desk Bot V2。")
            return
        }

        do {
            try ProfileInitializer.ensureInitialized(paths)
        } catch {
            fatalStartupError("初始化配置目录失败：\(error.localizedDescription)")
            return
        }
        ClientLogRotation.cleanInBackground(paths.logRoot)

        buildMainMenu()
        buildStatusItem()
        statusWindow.show("正在启动 Web、Core、LiveKit 与 RTC Agent…")

        let manager = ServiceManager(paths: paths)
        manager.onStatusChanged = { [weak self] text in
            self?.statusMenuItem?.title = text
        }
        manager.onOpenMainWindow = { [weak self] url in
            self?.statusWindow.hide()
            self?.mainWindow.show(at: url)
        }
        manager.onStartupUpdate = { [weak self] text, visible in
            if visible {
                self?.statusWindow.show(text)
            } else {
                self?.statusWindow.hide()
            }
        }
        serviceManager = manager
        do {
            try manager.startAndMonitor()
        } catch {
            LauncherLog.write("initial service startup failed: \(error)")
            fatalStartupError(
                "服务启动失败：\(error.localizedDescription)\n" +
                "详情见日志目录 \(paths.logRoot.path)")
        }
    }

    func applicationShouldTerminate(
        _ sender: NSApplication
    ) -> NSApplication.TerminateReply {
        guard let manager = serviceManager else { return .terminateNow }
        statusWindow.show("正在停止全部服务…")
        mainWindow.close()
        manager.shutdown {
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication, hasVisibleWindows flag: Bool
    ) -> Bool {
        if !flag {
            mainWindow.showExisting()
        }
        return true
    }

    // MARK: 单实例

    private func acquireSingleInstanceLock() -> Bool {
        let lockURL = paths.appRoot.appendingPathComponent(".launcher.lock")
        let fd = open(lockURL.path, O_WRONLY | O_CREAT, 0o644)
        guard fd >= 0 else { return true }
        if flock(fd, LOCK_EX | LOCK_NB) != 0 {
            close(fd)
            return false
        }
        lockFileDescriptor = fd
        return true
    }

    private func activateExistingInstance() {
        if let bundleId = Bundle.main.bundleIdentifier {
            let running = NSRunningApplication
                .runningApplications(withBundleIdentifier: bundleId)
                .filter { $0.processIdentifier != getpid() }
            if let other = running.first {
                other.activate(options: [.activateAllWindows])
                return
            }
        }
        // 找不到同 bundle 实例（如从命令行裸跑二进制）：直接打开控制台页。
        if let url = URL(string: kWebURL) {
            NSWorkspace.shared.open(url)
        }
    }

    // MARK: 菜单

    private func buildMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(NSMenuItem(
            title: "关于 Open Desk Bot V2",
            action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
            keyEquivalent: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(
            title: "隐藏", action: #selector(NSApplication.hide(_:)),
            keyEquivalent: "h"))
        appMenu.addItem(.separator())
        appMenu.addItem(NSMenuItem(
            title: "退出 Open Desk Bot V2",
            action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        // 编辑菜单：让 WKWebView 里的输入框支持 Cmd+C/V/X/A。
        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "编辑")
        editMenu.addItem(NSMenuItem(
            title: "撤销", action: Selector(("undo:")), keyEquivalent: "z"))
        editMenu.addItem(NSMenuItem(
            title: "重做", action: Selector(("redo:")), keyEquivalent: "Z"))
        editMenu.addItem(.separator())
        editMenu.addItem(NSMenuItem(
            title: "剪切", action: #selector(NSText.cut(_:)), keyEquivalent: "x"))
        editMenu.addItem(NSMenuItem(
            title: "拷贝", action: #selector(NSText.copy(_:)), keyEquivalent: "c"))
        editMenu.addItem(NSMenuItem(
            title: "粘贴", action: #selector(NSText.paste(_:)), keyEquivalent: "v"))
        editMenu.addItem(NSMenuItem(
            title: "全选", action: #selector(NSText.selectAll(_:)),
            keyEquivalent: "a"))
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        NSApp.mainMenu = mainMenu
    }

    private func buildStatusItem() {
        let item = NSStatusBar.system.statusItem(
            withLength: NSStatusItem.squareLength)
        if let button = item.button {
            let icon = NSApp.applicationIconImage.copy() as! NSImage
            icon.size = NSSize(width: 18, height: 18)
            button.image = icon
            button.toolTip = "Open Desk Bot V2"
        }

        let menu = NSMenu()
        let status = NSMenuItem(title: "状态：正在启动", action: nil,
                                keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(
            title: "打开控制台", action: #selector(openConsole),
            keyEquivalent: "o"))
        menu.addItem(NSMenuItem(
            title: "重启全部服务", action: #selector(restartAll),
            keyEquivalent: ""))
        menu.addItem(NSMenuItem(
            title: "打开日志目录", action: #selector(openLogs), keyEquivalent: ""))
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(
            title: "退出", action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: ""))
        for entry in menu.items where entry.action != nil {
            entry.target = (entry.action ==
                #selector(NSApplication.terminate(_:))) ? NSApp : self
        }
        item.menu = menu

        statusItem = item
        statusMenuItem = status
    }

    @objc private func openConsole() {
        mainWindow.showExisting()
    }

    @objc private func restartAll() {
        serviceManager?.restartServices()
    }

    @objc private func openLogs() {
        NSWorkspace.shared.open(paths.logRoot)
    }

    private func fatalStartupError(_ message: String) {
        statusWindow.hide()
        let alert = NSAlert()
        alert.alertStyle = .critical
        alert.messageText = "Open Desk Bot V2 无法启动"
        alert.informativeText = message
        alert.addButton(withTitle: "退出")
        alert.runModal()
        serviceManager?.shutdown {
            NSApp.terminate(nil)
        }
        if serviceManager == nil {
            NSApp.terminate(nil)
        }
    }
}

// MARK: - 入口

let app = NSApplication.shared
app.setActivationPolicy(.regular)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
