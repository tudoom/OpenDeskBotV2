using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Drawing;
using System.Globalization;
using System.IO;
using System.IO.Compression;
using System.IO.Pipes;
using System.Linq;
using System.Net.NetworkInformation;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;

using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: AssemblyTitle("Open Desk Bot V2")]
[assembly: AssemblyDescription("Open Desk Bot V2 local service client")]
[assembly: AssemblyCompany("Open Desk Bot")]
[assembly: AssemblyProduct("Open Desk Bot V2")]
[assembly: AssemblyVersion("2.0.0.0")]
[assembly: AssemblyFileVersion("2.0.0.0")]

namespace OpenDeskBotV2Client
{
    internal static class Program
    {
        private const string MutexName = @"Local\OpenDeskBotV2.Client";
        private const string ShowEventName = @"Local\OpenDeskBotV2.Client.Show";
        internal const string WebUrl = "http://127.0.0.1:5050/";

        [STAThread]
        private static void Main(string[] args)
        {
            // WebView2's managed assemblies are resources inside this one EXE.
            // Register the resolver before JIT-compiling any type that refers
            // to the SDK, then enter the no-inline application body.
            EmbeddedAssemblyLoader.Install();
            for (int i = 0; args != null && i < args.Length; i++)
            {
                if (string.Equals(
                        args[i], "--prepare-runtime",
                        StringComparison.OrdinalIgnoreCase))
                {
                    // Invoked by the installer (already elevated): perform the
                    // whole first-run pipeline — payload extraction, profile
                    // seeding, stable-bin materialization and (since we are
                    // elevated) the firewall rules — then exit without
                    // starting services. The user's first real launch is then
                    // instant, with no extraction wait and no UAC prompt.
                    Environment.ExitCode = RunPrepareRuntime() ? 0 : 1;
                    return;
                }
            }
            RunApplication();
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static bool RunPrepareRuntime()
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);

            string appRoot = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                "OpenDeskBotV2");
            Directory.CreateDirectory(appRoot);
            LauncherLog.Initialize(Path.Combine(appRoot, "logs", "launcher.log"));
            LauncherLog.Write("prepare-runtime started (installer context)");

            StatusForm status = new StatusForm();
            status.Show();
            status.UpdateStatus("正在准备运行环境…", 2);
            try
            {
                string runtimeRoot = ExtractRuntimeResponsively(status, appRoot);
                ProfileInitializer.EnsureInitialized(runtimeRoot, appRoot);
                string stableBinDir =
                    MaterializeStableBinResponsively(status, runtimeRoot, appRoot);
                status.UpdateStatus("运行环境已就绪", 100);
                LauncherLog.Write("prepare-runtime finished runtime=" + runtimeRoot);
                return true;
            }
            catch (Exception exception)
            {
                // Best-effort: a failure here just means the first real launch
                // falls back to the normal in-launcher extraction path.
                LauncherLog.Write("prepare-runtime failed", exception);
                return false;
            }
            finally
            {
                try { status.Close(); }
                catch (Exception) { }
            }
        }

        [MethodImpl(MethodImplOptions.NoInlining)]
        private static void RunApplication()
        {
            bool ownsMutex;
            using (Mutex mutex = new Mutex(true, MutexName, out ownsMutex))
            {
                if (!ownsMutex)
                {
                    SignalExistingWindow();
                    return;
                }

                using (EventWaitHandle showWindowEvent = new EventWaitHandle(
                    false,
                    EventResetMode.AutoReset,
                    ShowEventName))
                {
                    Application.EnableVisualStyles();
                    Application.SetCompatibleTextRenderingDefault(false);

                    string appRoot = Path.Combine(
                        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                        "OpenDeskBotV2");
                    Directory.CreateDirectory(appRoot);
                    LauncherLog.Initialize(Path.Combine(appRoot, "logs", "launcher.log"));

                    StatusForm status = new StatusForm();
                    status.Show();
                    status.UpdateStatus("正在校验客户端运行环境…", 2);

                    ClientContext context = null;
                    try
                    {
                        string runtimeRoot = ExtractRuntimeResponsively(status, appRoot);
                        ProfileInitializer.EnsureInitialized(runtimeRoot, appRoot);
                        // Materialize the two socket-binding images (LiveKit
                        // and the Python interpreter) to a version-stable path
                        // and pre-authorize them in the firewall exactly once,
                        // so upgrades never re-trigger the inbound prompts.
                        string stableBinDir =
                            MaterializeStableBinResponsively(status, runtimeRoot, appRoot);
                        status.UpdateStatus("运行环境已就绪，正在启动全部服务…", 96);
                        context = new ClientContext(
                            runtimeRoot,
                            appRoot,
                            stableBinDir,
                            status,
                            showWindowEvent);
                        Application.Run(context);
                    }
                    catch (Exception exception)
                    {
                        LauncherLog.Write("fatal launcher error", exception);
                        if (status != null && !status.IsDisposed)
                        {
                            status.Hide();
                        }
                        MessageBox.Show(
                            "Open Desk Bot V2 启动失败。\r\n\r\n" +
                            exception.Message + "\r\n\r\n详细信息：" +
                            Path.Combine(appRoot, "logs", "launcher.log"),
                            "Open Desk Bot V2",
                            MessageBoxButtons.OK,
                            MessageBoxIcon.Error);
                    }
                    finally
                    {
                        if (context != null)
                        {
                            context.Dispose();
                        }
                        if (status != null && !status.IsDisposed)
                        {
                            status.Dispose();
                        }
                    }
                }
            }
        }

        // The payload copy + extraction can take tens of seconds on first
        // launch and its parallel phase would keep the UI thread hostage as a
        // Parallel.ForEach worker, so the whole extraction runs on a worker
        // thread while the UI thread pumps messages: progress callbacks
        // arrive via StatusForm.BeginInvoke and repaint immediately.
        private static string ExtractRuntimeResponsively(StatusForm status, string appRoot)
        {
            string executablePath = Application.ExecutablePath;
            string runtimeRoot = null;
            Exception failure = null;
            Thread extractor = new Thread(delegate()
            {
                try
                {
                    runtimeRoot = PayloadExtractor.EnsureExtracted(
                        executablePath,
                        appRoot,
                        delegate(string message, int progress)
                        {
                            status.UpdateStatus(message, progress);
                        });
                }
                catch (Exception exception)
                {
                    failure = exception;
                }
            });
            extractor.IsBackground = true;
            extractor.Name = "OpenDeskBotV2-extract";
            extractor.Start();
            while (extractor.IsAlive)
            {
                Application.DoEvents();
                Thread.Sleep(25);
            }
            extractor.Join();
            if (failure != null)
            {
                ExceptionDispatchInfo.Capture(failure).Throw();
            }
            return runtimeRoot;
        }

        // Copying the portable Python tree to the stable bin directory can take
        // a few seconds on an upgrade (it only runs when the runtime version
        // changed), so it runs on a worker thread while the UI thread keeps
        // pumping, mirroring ExtractRuntimeResponsively. A failure here is not
        // fatal: the caller simply falls back to the versioned runtime paths,
        // whose only downside is that per-version firewall prompts can return.
        private static string MaterializeStableBinResponsively(
            StatusForm status, string runtimeRoot, string appRoot)
        {
            string stableBinDir = null;
            Exception failure = null;
            Thread worker = new Thread(delegate()
            {
                try
                {
                    stableBinDir = StableBinMaterializer.EnsureMaterialized(
                        runtimeRoot,
                        appRoot,
                        delegate(string message, int progress)
                        {
                            status.UpdateStatus(message, progress);
                        });
                }
                catch (Exception exception)
                {
                    failure = exception;
                }
            });
            worker.IsBackground = true;
            worker.Name = "OpenDeskBotV2-materialize";
            worker.Start();
            while (worker.IsAlive)
            {
                Application.DoEvents();
                Thread.Sleep(25);
            }
            worker.Join();
            if (failure != null)
            {
                LauncherLog.Write(
                    "stable bin materialization failed; using versioned runtime",
                    failure);
                return null;
            }
            // One-time firewall provisioning runs on the UI thread because the
            // UAC prompt it may raise is modal; every later launch short-circuits
            // on the .firewall-done marker without any prompt.
            FirewallRuleInstaller.EnsureRulesOnce(stableBinDir);
            return stableBinDir;
        }

        private static void SignalExistingWindow()
        {
            try
            {
                using (EventWaitHandle showEvent = EventWaitHandle.OpenExisting(ShowEventName))
                {
                    showEvent.Set();
                }
            }
            catch (WaitHandleCannotBeOpenedException)
            {
                // The first process may still be between mutex acquisition and
                // event creation. A later double-click can request the window.
            }
        }

        // Every "open in the system browser" path funnels through here
        // (WebView2 NavigationStarting external split and NewWindowRequested).
        // ShellExecute on an arbitrary URI would let injected page content
        // launch any locally registered protocol handler, so only web URLs
        // are ever handed to the shell; everything else is logged and dropped.
        internal static void OpenExternalUrl(string url)
        {
            Uri uri;
            if (!Uri.TryCreate(url, UriKind.Absolute, out uri) ||
                (!String.Equals(uri.Scheme, Uri.UriSchemeHttp, StringComparison.OrdinalIgnoreCase) &&
                 !String.Equals(uri.Scheme, Uri.UriSchemeHttps, StringComparison.OrdinalIgnoreCase)))
            {
                LauncherLog.Write("blocked external open of non-http(s) url: " + url);
                return;
            }
            try
            {
                Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true });
            }
            catch (Exception exception)
            {
                LauncherLog.Write("could not open " + url, exception);
            }
        }
    }

    internal static class EmbeddedAssemblyLoader
    {
        private static bool installed;

        internal static void Install()
        {
            if (installed)
            {
                return;
            }
            installed = true;
            AppDomain.CurrentDomain.AssemblyResolve += Resolve;
        }

        private static Assembly Resolve(object sender, ResolveEventArgs eventArgs)
        {
            string simpleName = new AssemblyName(eventArgs.Name).Name;
            if (!simpleName.StartsWith("Microsoft.Web.WebView2", StringComparison.Ordinal))
            {
                return null;
            }
            string resourceName = simpleName + ".dll";
            Assembly owner = Assembly.GetExecutingAssembly();
            using (Stream stream = owner.GetManifestResourceStream(resourceName))
            {
                if (stream == null)
                {
                    return null;
                }
                byte[] payload = new byte[stream.Length];
                int offset = 0;
                while (offset < payload.Length)
                {
                    int read = stream.Read(payload, offset, payload.Length - offset);
                    if (read <= 0)
                    {
                        throw new EndOfStreamException(resourceName);
                    }
                    offset += read;
                }
                return Assembly.Load(payload);
            }
        }
    }

    internal static class AppIcon
    {
        // The build embeds client\app.ico twice: /win32icon paints the EXE's
        // shell icon and /resource embeds the identical multi-size .ico as a
        // managed resource. The tray icon and window title bars load the
        // managed copy (size-matched frame, no disk dependency); if that is
        // ever missing, the EXE's own /win32icon resource is the fallback,
        // and the generic system icon is the last resort.
        private static readonly Lazy<Icon> WindowIcon = new Lazy<Icon>(
            delegate() { return Load(0, 0); });
        private static readonly Lazy<Icon> TrayIconInstance = new Lazy<Icon>(
            delegate()
            {
                Size small = SystemInformation.SmallIconSize;
                return Load(small.Width, small.Height);
            });

        internal static Icon Window { get { return WindowIcon.Value; } }
        internal static Icon Tray { get { return TrayIconInstance.Value; } }

        private static Icon Load(int width, int height)
        {
            try
            {
                using (Stream stream = Assembly.GetExecutingAssembly()
                    .GetManifestResourceStream("app.ico"))
                {
                    if (stream != null)
                    {
                        return width > 0
                            ? new Icon(stream, width, height)
                            : new Icon(stream);
                    }
                }
            }
            catch (ArgumentException) { }
            catch (IOException) { }
            try
            {
                Icon associated = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
                if (associated != null)
                {
                    return associated;
                }
            }
            catch (ArgumentException) { }
            return SystemIcons.Application;
        }
    }

    internal sealed class StatusForm : Form
    {
        private readonly Label messageLabel;
        private readonly ProgressBar progressBar;

        internal StatusForm()
        {
            Text = "Open Desk Bot V2";
            ClientSize = new Size(470, 132);
            FormBorderStyle = FormBorderStyle.FixedDialog;
            StartPosition = FormStartPosition.CenterScreen;
            MaximizeBox = false;
            MinimizeBox = false;
            ControlBox = false;
            ShowInTaskbar = true;
            Icon = AppIcon.Window;

            Label title = new Label();
            title.AutoSize = true;
            title.Font = new Font(Font.FontFamily, 13.0f, FontStyle.Bold);
            title.Location = new Point(22, 18);
            title.Text = "Open Desk Bot V2";
            Controls.Add(title);

            messageLabel = new Label();
            messageLabel.AutoEllipsis = true;
            messageLabel.Location = new Point(23, 53);
            messageLabel.Size = new Size(424, 24);
            messageLabel.Text = "正在准备…";
            Controls.Add(messageLabel);

            progressBar = new ProgressBar();
            progressBar.Location = new Point(25, 87);
            progressBar.Size = new Size(420, 18);
            progressBar.Minimum = 0;
            progressBar.Maximum = 100;
            Controls.Add(progressBar);
        }

        internal void UpdateStatus(string message, int progress)
        {
            if (IsDisposed)
            {
                return;
            }
            if (InvokeRequired)
            {
                BeginInvoke(new Action<string, int>(UpdateStatus), message, progress);
                return;
            }
            messageLabel.Text = message;
            progressBar.Value = Math.Max(0, Math.Min(100, progress));
            Refresh();
            Application.DoEvents();
        }
    }

    internal static class PayloadExtractor
    {
        private static readonly byte[] FooterMagic = Encoding.ASCII.GetBytes("ODBV2PKG");
        private const int HashLength = 32;
        private const int FooterLength = 8 + HashLength + 8;

        internal static string EnsureExtracted(
            string executablePath,
            string appRoot,
            Action<string, int> progress)
        {
            long payloadOffset;
            long payloadLength;
            byte[] expectedHash;
            ReadFooter(
                executablePath,
                out payloadOffset,
                out payloadLength,
                out expectedHash);

            string hashHex = ToHex(expectedHash);
            string runtimeParent = Path.Combine(appRoot, "runtime");
            string runtimeRoot = Path.Combine(runtimeParent, hashHex.Substring(0, 16));
            string markerPath = Path.Combine(runtimeRoot, ".complete");
            if (File.Exists(markerPath))
            {
                string marker = File.ReadAllText(markerPath, Encoding.UTF8).Trim();
                if (String.Equals(marker, hashHex, StringComparison.OrdinalIgnoreCase) &&
                    RequiredRuntimeFilesExist(runtimeRoot))
                {
                    progress("已复用校验通过的本地运行环境", 92);
                    return runtimeRoot;
                }
            }

            Directory.CreateDirectory(runtimeParent);
            string temporaryRuntime = Path.Combine(
                runtimeParent,
                hashHex.Substring(0, 16) + ".extract-" +
                Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture));
            string temporaryZip = Path.Combine(
                runtimeParent,
                hashHex.Substring(0, 16) + ".payload-" +
                Process.GetCurrentProcess().Id.ToString(CultureInfo.InvariantCulture) + ".zip");

            DeleteGeneratedDirectory(temporaryRuntime, runtimeParent);
            if (File.Exists(temporaryZip))
            {
                File.Delete(temporaryZip);
            }

            try
            {
                CopyAndVerifyPayload(
                    executablePath,
                    payloadOffset,
                    payloadLength,
                    expectedHash,
                    temporaryZip,
                    progress);
                ExtractArchive(temporaryZip, temporaryRuntime, progress);
                if (!RequiredRuntimeFilesExist(temporaryRuntime))
                {
                    throw new InvalidDataException("客户端载荷缺少 Python、服务源码或 LiveKit。 ");
                }
                File.WriteAllText(
                    Path.Combine(temporaryRuntime, ".complete"),
                    hashHex + Environment.NewLine,
                    new UTF8Encoding(false));

                if (Directory.Exists(runtimeRoot))
                {
                    DeleteGeneratedDirectory(runtimeRoot, runtimeParent);
                }
                Directory.Move(temporaryRuntime, runtimeRoot);
                progress("客户端运行环境释放完成", 92);
                return runtimeRoot;
            }
            finally
            {
                if (File.Exists(temporaryZip))
                {
                    try { File.Delete(temporaryZip); }
                    catch (IOException) { }
                }
                if (Directory.Exists(temporaryRuntime))
                {
                    try { DeleteGeneratedDirectory(temporaryRuntime, runtimeParent); }
                    catch (IOException) { }
                }
            }
        }

        private static void ReadFooter(
            string executablePath,
            out long payloadOffset,
            out long payloadLength,
            out byte[] expectedHash)
        {
            using (FileStream stream = new FileStream(
                executablePath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read))
            {
                if (stream.Length <= FooterLength)
                {
                    throw new InvalidDataException("客户端 EXE 中没有运行时载荷。 ");
                }
                stream.Position = stream.Length - FooterLength;
                byte[] footer = new byte[FooterLength];
                ReadExactly(stream, footer, 0, footer.Length);
                byte[] magic = new byte[FooterMagic.Length];
                Buffer.BlockCopy(footer, 8 + HashLength, magic, 0, magic.Length);
                if (!magic.SequenceEqual(FooterMagic))
                {
                    throw new InvalidDataException("客户端 EXE 载荷标记无效。 ");
                }
                payloadLength = BitConverter.ToInt64(footer, 0);
                payloadOffset = stream.Length - FooterLength - payloadLength;
                if (payloadLength <= 0 || payloadOffset < 0)
                {
                    throw new InvalidDataException("客户端 EXE 载荷长度无效。 ");
                }
                expectedHash = new byte[HashLength];
                Buffer.BlockCopy(footer, 8, expectedHash, 0, HashLength);
            }
        }

        private static void CopyAndVerifyPayload(
            string executablePath,
            long payloadOffset,
            long payloadLength,
            byte[] expectedHash,
            string destination,
            Action<string, int> progress)
        {
            progress("首次启动：正在释放便携运行环境…", 4);
            byte[] buffer = new byte[1024 * 1024];
            long copied = 0;
            using (FileStream input = new FileStream(
                executablePath,
                FileMode.Open,
                FileAccess.Read,
                FileShare.Read))
            using (FileStream output = new FileStream(
                destination,
                FileMode.CreateNew,
                FileAccess.Write,
                FileShare.None))
            {
                input.Position = payloadOffset;
                while (copied < payloadLength)
                {
                    int wanted = (int)Math.Min(buffer.Length, payloadLength - copied);
                    int read = input.Read(buffer, 0, wanted);
                    if (read <= 0)
                    {
                        throw new EndOfStreamException("客户端运行时载荷不完整。 ");
                    }
                    output.Write(buffer, 0, read);
                    copied += read;
                    int percentage = 4 + (int)(copied * 30L / payloadLength);
                    progress("首次启动：正在复制运行环境…", percentage);
                }
            }

            progress("正在校验运行环境完整性…", 36);
            byte[] actualHash;
            using (SHA256 sha = SHA256.Create())
            using (FileStream payload = File.OpenRead(destination))
            {
                actualHash = sha.ComputeHash(payload);
            }
            if (!actualHash.SequenceEqual(expectedHash))
            {
                throw new InvalidDataException("客户端运行时载荷校验失败，请重新复制 EXE。 ");
            }
        }

        // Entries are decompressed in parallel: expanding a ~300 MB payload
        // is dominated by per-file close latency (Defender's real-time scan
        // runs synchronously on CloseHandle), so overlapping several files
        // cuts first-launch extraction to a fraction of the sequential time.
        // ZipArchive instances are not thread-safe, therefore every worker
        // opens its own reader over the same archive file (FileShare.Read)
        // and addresses entries by index, which is stable across instances.
        private static void ExtractArchive(
            string archivePath,
            string destinationRoot,
            Action<string, int> progress)
        {
            Directory.CreateDirectory(destinationRoot);
            string safePrefix = Path.GetFullPath(destinationRoot)
                .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;

            // Single-threaded pre-pass: reject unsafe paths (Zip-Slip) and
            // create the complete directory tree up front, so the parallel
            // phase never races on directory creation.
            List<int> fileEntries = new List<int>();
            long totalBytes = 0;
            using (ZipArchive archive = ZipFile.OpenRead(archivePath))
            {
                for (int index = 0; index < archive.Entries.Count; index++)
                {
                    ZipArchiveEntry entry = archive.Entries[index];
                    string target = ResolveSafeTargetPath(
                        destinationRoot, safePrefix, entry.FullName);
                    if (entry.Name.Length == 0)
                    {
                        Directory.CreateDirectory(target);
                        continue;
                    }
                    string parent = Path.GetDirectoryName(target);
                    if (!String.IsNullOrEmpty(parent))
                    {
                        Directory.CreateDirectory(parent);
                    }
                    fileEntries.Add(index);
                    totalBytes += entry.Length;
                }
            }

            long extractedBytes = 0;
            int lastReportedPercent = -1;
            long startedTicks = DateTime.UtcNow.Ticks;
            ReportExtractionProgress(progress, 0, totalBytes, startedTicks);

            ParallelOptions options = new ParallelOptions();
            // Cap 16 instead of the CPU-bound 8: target machines run an AV
            // (ESET) that scans every created file synchronously, so each
            // worker spends most of its time blocked in the scanner, not on
            // the CPU. More in-flight files overlap those per-file scan
            // stalls; disk and CPU are nowhere near saturated at 8.
            options.MaxDegreeOfParallelism =
                Math.Max(2, Math.Min(Environment.ProcessorCount, 16));
            try
            {
                Parallel.ForEach(
                    fileEntries,
                    options,
                    delegate() { return ZipFile.OpenRead(archivePath); },
                    delegate(int entryIndex, ParallelLoopState state, ZipArchive archive)
                    {
                        ZipArchiveEntry entry = archive.Entries[entryIndex];
                        string target = ResolveSafeTargetPath(
                            destinationRoot, safePrefix, entry.FullName);
                        using (Stream source = entry.Open())
                        using (FileStream output = new FileStream(
                            target,
                            FileMode.Create,
                            FileAccess.Write,
                            FileShare.None,
                            256 * 1024))
                        {
                            source.CopyTo(output, 256 * 1024);
                        }
                        try
                        {
                            File.SetLastWriteTimeUtc(target, entry.LastWriteTime.UtcDateTime);
                        }
                        catch (ArgumentOutOfRangeException) { }
                        catch (IOException) { }

                        long done = Interlocked.Add(ref extractedBytes, entry.Length);
                        int percent = totalBytes > 0
                            ? (int)(done * 100L / totalBytes)
                            : 100;
                        // Throttle UI updates to whole-percent steps; exactly
                        // one worker wins each step via CompareExchange.
                        int seen = Volatile.Read(ref lastReportedPercent);
                        if (percent > seen &&
                            Interlocked.CompareExchange(
                                ref lastReportedPercent, percent, seen) == seen)
                        {
                            ReportExtractionProgress(
                                progress, done, totalBytes, startedTicks);
                        }
                        return archive;
                    },
                    delegate(ZipArchive archive) { archive.Dispose(); });
            }
            catch (AggregateException aggregate)
            {
                // Surface the first real failure (for example the Zip-Slip
                // InvalidDataException) instead of an opaque aggregate.
                ExceptionDispatchInfo.Capture(
                    aggregate.Flatten().InnerExceptions[0]).Throw();
            }
        }

        // Until about one second of real throughput has been measured, the
        // remaining-time estimate assumes this rate (a 300 MB payload with
        // Defender real-time scanning lands around 25-40 MB/s in practice).
        private const double AssumedExtractBytesPerSecond = 30.0 * 1024 * 1024;

        private static void ReportExtractionProgress(
            Action<string, int> progress,
            long extractedBytes,
            long totalBytes,
            long startedTicks)
        {
            int percent = totalBytes > 0
                ? (int)(extractedBytes * 100L / totalBytes)
                : 100;
            double elapsedSeconds =
                (DateTime.UtcNow.Ticks - startedTicks) / (double)TimeSpan.TicksPerSecond;
            double bytesPerSecond = (extractedBytes > 0 && elapsedSeconds >= 1.0)
                ? extractedBytes / elapsedSeconds
                : AssumedExtractBytesPerSecond;
            long remainingBytes = Math.Max(0, totalBytes - extractedBytes);
            int remainingSeconds = Math.Max(1, (int)Math.Ceiling(
                remainingBytes / Math.Max(bytesPerSecond, 1.0)));
            progress(
                "首次启动正在展开运行时（一次性，约 " +
                remainingSeconds.ToString(CultureInfo.InvariantCulture) +
                " 秒）：" +
                percent.ToString(CultureInfo.InvariantCulture) + "%",
                40 + percent / 2);
        }

        private static string ResolveSafeTargetPath(
            string destinationRoot,
            string safePrefix,
            string entryFullName)
        {
            string relative = entryFullName.Replace('/', Path.DirectorySeparatorChar);
            string target = Path.GetFullPath(Path.Combine(destinationRoot, relative));
            if (!target.StartsWith(safePrefix, StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException("运行时压缩包包含不安全路径。 ");
            }
            return target;
        }

        private static bool RequiredRuntimeFilesExist(string root)
        {
            return File.Exists(Path.Combine(root, "python", "python.exe")) &&
                File.Exists(Path.Combine(root, "app", "src", "deskbot_server", "__main__.py")) &&
                File.Exists(Path.Combine(root, "app", "bin", "livekit-server.exe")) &&
                File.Exists(Path.Combine(root, "client", "x64", "WebView2Loader.dll"));
        }

        private static void DeleteGeneratedDirectory(string path, string expectedParent)
        {
            string fullPath = Path.GetFullPath(path).TrimEnd(Path.DirectorySeparatorChar);
            string fullParent = Path.GetFullPath(expectedParent)
                .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            if (!fullPath.StartsWith(fullParent, StringComparison.OrdinalIgnoreCase) ||
                fullPath.Length <= fullParent.Length)
            {
                throw new InvalidOperationException("拒绝清理非客户端运行时目录。 ");
            }
            if (Directory.Exists(fullPath))
            {
                Directory.Delete(fullPath, true);
            }
        }

        private static string ToHex(byte[] bytes)
        {
            StringBuilder output = new StringBuilder(bytes.Length * 2);
            foreach (byte value in bytes)
            {
                output.Append(value.ToString("x2", CultureInfo.InvariantCulture));
            }
            return output.ToString();
        }

        private static void ReadExactly(Stream stream, byte[] buffer, int offset, int count)
        {
            while (count > 0)
            {
                int read = stream.Read(buffer, offset, count);
                if (read <= 0)
                {
                    throw new EndOfStreamException();
                }
                offset += read;
                count -= read;
            }
        }
    }

    // Copies the two images that bind a non-loopback socket - the LiveKit SFU
    // and the portable Python interpreter - out of the per-version runtime and
    // into a path that is stable across upgrades (%LOCALAPPDATA%\OpenDeskBotV2
    // \bin). The firewall matches an allow-rule against a process image's final
    // file path, so that path must be a real physical file, not a link: a
    // junction would resolve back to the versioned target and a hardlink adds
    // same-volume / relink fragility for no gain here, so a plain copy is used.
    // python.exe cannot run apart from its own Lib/DLLs, therefore the whole
    // python\ tree is copied as one unit. The business sources (app\src) are
    // deliberately left in the versioned runtime; they never bind a socket.
    internal static class StableBinMaterializer
    {
        internal static string EnsureMaterialized(
            string runtimeRoot,
            string appRoot,
            Action<string, int> progress)
        {
            string stableBinDir = Path.Combine(appRoot, "bin");
            string version = Path.GetFileName(
                Path.GetFullPath(runtimeRoot).TrimEnd(Path.DirectorySeparatorChar));
            string markerPath = Path.Combine(stableBinDir, ".materialized-version");
            string stablePython = Path.Combine(stableBinDir, "python", "python.exe");
            string stableLiveKit = Path.Combine(stableBinDir, "livekit-server.exe");

            // Fast path: already materialized for this exact runtime version.
            if (File.Exists(markerPath) &&
                File.Exists(stablePython) &&
                File.Exists(stableLiveKit))
            {
                string current = File.ReadAllText(markerPath, Encoding.UTF8).Trim();
                if (String.Equals(current, version, StringComparison.OrdinalIgnoreCase))
                {
                    return stableBinDir;
                }
            }

            progress("正在物化稳定运行组件（首次或升级后一次性）…", 94);
            Directory.CreateDirectory(stableBinDir);
            // Clear the marker first so a crash mid-copy forces a clean redo
            // instead of trusting a half-written tree on the next launch.
            if (File.Exists(markerPath))
            {
                try { File.Delete(markerPath); }
                catch (IOException) { }
            }

            CopyFileOverwrite(
                Path.Combine(runtimeRoot, "app", "bin", "livekit-server.exe"),
                stableLiveKit);
            MirrorDirectory(
                Path.Combine(runtimeRoot, "python"),
                Path.Combine(stableBinDir, "python"));

            File.WriteAllText(
                markerPath, version + Environment.NewLine, new UTF8Encoding(false));
            progress("稳定运行组件已就绪", 95);
            return stableBinDir;
        }

        // The stable bin exists ONLY so the firewall-relevant image paths never
        // change across upgrades; the firewall matches the executable path, not
        // its libraries. site-packages (~450MB, >10k files) therefore stays in
        // the versioned runtime and is reached via PYTHONPATH — mirroring it
        // here turned every upgrade's materialization into a minutes-long
        // second copy for zero benefit.
        private static readonly string SkippedMirrorSubdir =
            "Lib" + Path.DirectorySeparatorChar + "site-packages";

        private static bool IsSkippedMirrorPath(string relative)
        {
            return relative.Equals(
                       SkippedMirrorSubdir, StringComparison.OrdinalIgnoreCase) ||
                   relative.StartsWith(
                       SkippedMirrorSubdir + Path.DirectorySeparatorChar,
                       StringComparison.OrdinalIgnoreCase);
        }

        private static void MirrorDirectory(string sourceRoot, string destRoot)
        {
            // Replace the previous version's tree wholesale (overwrite update;
            // no multi-version retention is needed for the stable bin).
            if (Directory.Exists(destRoot))
            {
                Directory.Delete(destRoot, true);
            }
            Directory.CreateDirectory(destRoot);
            string sourcePrefix = Path.GetFullPath(sourceRoot)
                .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            foreach (string directory in Directory.GetDirectories(
                sourceRoot, "*", SearchOption.AllDirectories))
            {
                string relative =
                    Path.GetFullPath(directory).Substring(sourcePrefix.Length);
                if (IsSkippedMirrorPath(relative))
                {
                    continue;
                }
                Directory.CreateDirectory(Path.Combine(destRoot, relative));
            }
            foreach (string file in Directory.GetFiles(
                sourceRoot, "*", SearchOption.AllDirectories))
            {
                string relative = Path.GetFullPath(file).Substring(sourcePrefix.Length);
                if (IsSkippedMirrorPath(relative))
                {
                    continue;
                }
                string target = Path.Combine(destRoot, relative);
                string parent = Path.GetDirectoryName(target);
                if (!String.IsNullOrEmpty(parent))
                {
                    Directory.CreateDirectory(parent);
                }
                File.Copy(file, target, true);
            }
        }

        private static void CopyFileOverwrite(string source, string destination)
        {
            string parent = Path.GetDirectoryName(destination);
            if (!String.IsNullOrEmpty(parent))
            {
                Directory.CreateDirectory(parent);
            }
            File.Copy(source, destination, true);
        }
    }

    // Pre-authorizes the two stable socket-binding images in Windows Defender
    // Firewall exactly once. Because their paths never change across upgrades,
    // a single inbound allow-rule per binary keeps matching forever, so this
    // elevation runs at most once for the lifetime of the install (and never at
    // all when an all-users installer already created the rules). Detection is
    // by program path, not by rule name, so installer-created rules of any name
    // are recognized and no redundant prompt is raised.
    internal static class FirewallRuleInstaller
    {
        private const string LiveKitRuleName = "Open Desk Bot V2 (LiveKit SFU)";
        private const string PythonRuleName = "Open Desk Bot V2 (Python runtime)";

        internal static void EnsureRulesOnce(string stableBinDir)
        {
            if (String.IsNullOrEmpty(stableBinDir))
            {
                return;
            }
            try
            {
                string doneMarker = Path.Combine(stableBinDir, ".firewall-done");
                if (File.Exists(doneMarker))
                {
                    return;
                }
                string liveKit = Path.Combine(stableBinDir, "livekit-server.exe");
                string python = Path.Combine(stableBinDir, "python", "python.exe");

                string existing = RunNetshCapture(
                    "advfirewall firewall show rule name=all verbose");
                if (existing != null &&
                    ProgramHasRule(existing, liveKit) &&
                    ProgramHasRule(existing, python))
                {
                    // Rules already present (e.g. added by the installer).
                    WriteDoneMarker(doneMarker);
                    return;
                }

                AddRulesElevated(liveKit, python);
                // Record the single attempt regardless of the outcome: on
                // success the rules now exist; if the user dismissed the one
                // UAC prompt, Windows still falls back to its own per-path
                // prompt the first time each binary binds and remembers that
                // decision for the (stable) path, so re-prompting our elevation
                // on every launch would only nag. The paths never move, so one
                // attempt is enough either way.
                WriteDoneMarker(doneMarker);
            }
            catch (Exception exception)
            {
                LauncherLog.Write("firewall rule provisioning failed", exception);
            }
        }

        private static void WriteDoneMarker(string doneMarker)
        {
            try
            {
                File.WriteAllText(
                    doneMarker,
                    DateTime.Now.ToString("o", CultureInfo.InvariantCulture) +
                    Environment.NewLine,
                    new UTF8Encoding(false));
            }
            catch (IOException) { }
        }

        // A rule references the binary when the exact image path appears in the
        // verbose dump. Field labels are localized but the path value is not, so
        // a case-insensitive substring test is language-independent.
        private static bool ProgramHasRule(string netshOutput, string programPath)
        {
            return netshOutput.IndexOf(programPath, StringComparison.OrdinalIgnoreCase) >= 0;
        }

        private static string RunNetshCapture(string arguments)
        {
            // Reading firewall rules does not require elevation.
            try
            {
                ProcessStartInfo start = new ProcessStartInfo();
                start.FileName = "netsh.exe";
                start.Arguments = arguments;
                start.UseShellExecute = false;
                start.CreateNoWindow = true;
                start.RedirectStandardOutput = true;
                start.RedirectStandardError = true;
                start.StandardOutputEncoding = Encoding.UTF8;
                using (Process process = Process.Start(start))
                {
                    if (process == null)
                    {
                        return null;
                    }
                    string output = process.StandardOutput.ReadToEnd();
                    process.WaitForExit();
                    return output;
                }
            }
            catch (Win32Exception exception)
            {
                LauncherLog.Write("netsh query failed", exception);
                return null;
            }
            catch (IOException exception)
            {
                LauncherLog.Write("netsh query failed", exception);
                return null;
            }
        }

        private static void AddRulesElevated(string liveKit, string python)
        {
            // The launcher is not elevated, so both rules are added by a single
            // elevated batch (one UAC prompt). Each rule is deleted first so a
            // rerun is idempotent, then re-added as an inbound allow bound to the
            // exact image path.
            string batchPath = Path.Combine(
                Path.GetTempPath(),
                "odbv2-firewall-" + Guid.NewGuid().ToString("N") + ".bat");
            StringBuilder script = new StringBuilder();
            script.AppendLine("@echo off");
            AppendRuleCommands(script, LiveKitRuleName, liveKit);
            AppendRuleCommands(script, PythonRuleName, python);
            // Same one-time elevation also adds the Defender scan exclusion for
            // the per-user data/runtime root, so green (portable) users get the
            // cold-start speedup without a separate admin script. Best-effort;
            // its failure never blocks the firewall rules or launch.
            string dataRoot = Path.GetDirectoryName(
                Path.GetDirectoryName(python.TrimEnd(Path.DirectorySeparatorChar)));
            if (!string.IsNullOrEmpty(dataRoot))
            {
                script.AppendLine(
                    "powershell -NoProfile -NonInteractive -Command " +
                    "\"try { Add-MpPreference -ExclusionPath '" + dataRoot +
                    "' -ErrorAction Stop } catch { }\"");
            }
            script.AppendLine("exit /b 0");
            try
            {
                File.WriteAllText(batchPath, script.ToString(), new UTF8Encoding(false));
                ProcessStartInfo start = new ProcessStartInfo();
                start.FileName = "cmd.exe";
                start.Arguments = "/c \"" + batchPath + "\"";
                start.UseShellExecute = true;
                start.Verb = "runas";
                start.CreateNoWindow = true;
                start.WindowStyle = ProcessWindowStyle.Hidden;
                using (Process process = Process.Start(start))
                {
                    if (process != null)
                    {
                        process.WaitForExit();
                        LauncherLog.Write(
                            "firewall rule provisioning elevated exit=" +
                            process.ExitCode.ToString(CultureInfo.InvariantCulture));
                    }
                }
            }
            catch (Win32Exception exception)
            {
                // 1223 == ERROR_CANCELLED: the user dismissed the UAC prompt.
                LauncherLog.Write("firewall elevation not completed", exception);
            }
            catch (IOException exception)
            {
                LauncherLog.Write("firewall elevation not completed", exception);
            }
            finally
            {
                try { File.Delete(batchPath); }
                catch (IOException) { }
            }
        }

        private static void AppendRuleCommands(
            StringBuilder script, string ruleName, string programPath)
        {
            script.AppendLine(
                "netsh advfirewall firewall delete rule name=\"" +
                ruleName + "\" >nul 2>&1");
            script.AppendLine(
                "netsh advfirewall firewall add rule name=\"" + ruleName +
                "\" dir=in action=allow program=\"" + programPath +
                "\" enable=yes profile=any");
        }
    }

    internal static class ProfileInitializer
    {
        internal static void EnsureInitialized(string runtimeRoot, string appRoot)
        {
            string seedRoot = Path.Combine(runtimeRoot, "seed");
            CopyFileIfMissing(
                Path.Combine(seedRoot, "config.yaml"),
                Path.Combine(appRoot, "config.yaml"));
            CopyFileIfMissing(
                Path.Combine(seedRoot, ".env.example"),
                Path.Combine(appRoot, ".env.example"));
            CopyTreeIfMissing(
                Path.Combine(seedRoot, "data", "global"),
                Path.Combine(appRoot, "data", "global"));
            Directory.CreateDirectory(Path.Combine(appRoot, "data", "local"));
            Directory.CreateDirectory(Path.Combine(appRoot, "logs"));
        }

        private static void CopyTreeIfMissing(string sourceRoot, string destinationRoot)
        {
            if (!Directory.Exists(sourceRoot))
            {
                throw new DirectoryNotFoundException(sourceRoot);
            }
            foreach (string source in Directory.GetFiles(sourceRoot, "*", SearchOption.AllDirectories))
            {
                string relative = source.Substring(sourceRoot.Length)
                    .TrimStart(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
                CopyFileIfMissing(source, Path.Combine(destinationRoot, relative));
            }
        }

        private static void CopyFileIfMissing(string source, string destination)
        {
            if (File.Exists(destination))
            {
                return;
            }
            string parent = Path.GetDirectoryName(destination);
            if (!String.IsNullOrEmpty(parent))
            {
                Directory.CreateDirectory(parent);
            }
            File.Copy(source, destination, false);
        }
    }

    internal static class RuntimeGarbageCollector
    {
        // Every upgrade leaves the previous versioned runtime (a multi-GB
        // Python tree) behind, and a crash during extraction can leave
        // ".extract-<pid>" / ".payload-<pid>.zip" temporaries. Sweep them
        // after the core services are ready so the cleanup never touches
        // the startup critical path. Failures are silent; the next launch
        // simply tries again.
        internal static void CollectInBackground(string appRoot, string activeRuntimeRoot)
        {
            Thread worker = new Thread(delegate() { Collect(appRoot, activeRuntimeRoot); });
            worker.IsBackground = true;
            worker.Priority = ThreadPriority.Lowest;
            worker.Name = "OpenDeskBotV2-runtime-gc";
            worker.Start();
        }

        private static void Collect(string appRoot, string activeRuntimeRoot)
        {
            try
            {
                string runtimeParent = Path.Combine(appRoot, "runtime");
                if (!Directory.Exists(runtimeParent))
                {
                    return;
                }
                string parentPrefix = Path.GetFullPath(runtimeParent)
                    .TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
                string activeName = Path.GetFileName(
                    Path.GetFullPath(activeRuntimeRoot).TrimEnd(Path.DirectorySeparatorChar));
                foreach (string directory in Directory.GetDirectories(runtimeParent))
                {
                    string name = Path.GetFileName(directory);
                    if (String.Equals(name, activeName, StringComparison.OrdinalIgnoreCase) ||
                        !IsGeneratedRuntimeName(name) ||
                        !Path.GetFullPath(directory).StartsWith(
                            parentPrefix, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }
                    try
                    {
                        Directory.Delete(directory, true);
                        LauncherLog.Write("runtime gc removed directory " + name);
                    }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                }
                foreach (string file in Directory.GetFiles(runtimeParent))
                {
                    string name = Path.GetFileName(file);
                    if (!IsGeneratedRuntimeName(name) ||
                        !Path.GetFullPath(file).StartsWith(
                            parentPrefix, StringComparison.OrdinalIgnoreCase))
                    {
                        continue;
                    }
                    try
                    {
                        File.Delete(file);
                        LauncherLog.Write("runtime gc removed file " + name);
                    }
                    catch (IOException) { }
                    catch (UnauthorizedAccessException) { }
                }
            }
            catch (Exception exception)
            {
                LauncherLog.Write("runtime gc failed", exception);
            }
        }

        // A generated entry is either a 16-hex-char versioned runtime
        // directory or a "<hash16>.extract-<pid>" / "<hash16>.payload-<pid>.zip"
        // temporary left behind by a crashed extraction. Nothing else inside
        // the runtime root is ever deleted.
        private static bool IsGeneratedRuntimeName(string name)
        {
            if (name == null || name.Length < 16)
            {
                return false;
            }
            for (int index = 0; index < 16; index++)
            {
                char digit = name[index];
                bool isHex = (digit >= '0' && digit <= '9') ||
                    (digit >= 'a' && digit <= 'f') ||
                    (digit >= 'A' && digit <= 'F');
                if (!isHex)
                {
                    return false;
                }
            }
            if (name.Length == 16)
            {
                return true;
            }
            string suffix = name.Substring(16);
            return suffix.StartsWith(".extract-", StringComparison.OrdinalIgnoreCase) ||
                suffix.StartsWith(".payload-", StringComparison.OrdinalIgnoreCase);
        }
    }

    internal static class ClientLogRotation
    {
        // Every service (re)start creates a fresh timestamped pair
        // (core-<stamp>.log / web-<stamp>.log), so a long failure loop can
        // litter the logs directory with hundreds of fragments. At startup a
        // background sweep keeps the newest stamps per prefix and deletes the
        // rest (including any ".log.1" size-rollover backups of that stamp).
        // Deletion failures are silent; the next launch simply tries again.
        private const int KeepNewestStamps = 10;
        private static readonly string[] RotatedPrefixes = { "core-", "web-" };

        internal static void CleanInBackground(string logRoot)
        {
            Thread worker = new Thread(delegate() { Clean(logRoot); });
            worker.IsBackground = true;
            worker.Priority = ThreadPriority.Lowest;
            worker.Name = "OpenDeskBotV2-log-rotation";
            worker.Start();
        }

        private static void Clean(string logRoot)
        {
            try
            {
                if (!Directory.Exists(logRoot))
                {
                    return;
                }
                foreach (string prefix in RotatedPrefixes)
                {
                    Dictionary<string, List<string>> filesByStamp =
                        new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
                    foreach (string file in Directory.GetFiles(logRoot, prefix + "*.log*"))
                    {
                        string stamp = ExtractStamp(Path.GetFileName(file), prefix);
                        if (stamp == null)
                        {
                            continue;
                        }
                        List<string> bucket;
                        if (!filesByStamp.TryGetValue(stamp, out bucket))
                        {
                            bucket = new List<string>();
                            filesByStamp[stamp] = bucket;
                        }
                        bucket.Add(file);
                    }
                    List<string> stamps = filesByStamp.Keys.ToList();
                    // "yyyyMMdd-HHmmss" sorts chronologically as text.
                    stamps.Sort(StringComparer.Ordinal);
                    stamps.Reverse();
                    for (int index = KeepNewestStamps; index < stamps.Count; index++)
                    {
                        foreach (string file in filesByStamp[stamps[index]])
                        {
                            try
                            {
                                File.Delete(file);
                            }
                            catch (IOException) { }
                            catch (UnauthorizedAccessException) { }
                        }
                    }
                }
            }
            catch (Exception exception)
            {
                LauncherLog.Write("log rotation cleanup failed", exception);
            }
        }

        // Only "<prefix>yyyyMMdd-HHmmss.log" and its ".log.1" rollover backup
        // are ever considered; anything else in the logs directory (core.log,
        // web.log, launcher.log, user files) is never touched.
        private static string ExtractStamp(string fileName, string prefix)
        {
            if (!fileName.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
            {
                return null;
            }
            string rest = fileName.Substring(prefix.Length);
            int dot = rest.IndexOf('.');
            if (dot <= 0)
            {
                return null;
            }
            string suffix = rest.Substring(dot);
            if (!String.Equals(suffix, ".log", StringComparison.OrdinalIgnoreCase) &&
                !String.Equals(suffix, ".log.1", StringComparison.OrdinalIgnoreCase))
            {
                return null;
            }
            string stamp = rest.Substring(0, dot);
            if (stamp.Length != 15 || stamp[8] != '-')
            {
                return null;
            }
            for (int index = 0; index < stamp.Length; index++)
            {
                if (index == 8)
                {
                    continue;
                }
                if (stamp[index] < '0' || stamp[index] > '9')
                {
                    return null;
                }
            }
            return stamp;
        }
    }

    internal sealed class ClientContext : ApplicationContext
    {
        private readonly string runtimeRoot;
        private readonly string appRoot;
        private readonly string stableBinDir;
        private readonly string logRoot;
        private readonly StatusForm statusForm;
        private readonly NotifyIcon trayIcon;
        private readonly ToolStripMenuItem statusItem;
        private readonly System.Windows.Forms.Timer monitorTimer;
        private readonly EventWaitHandle showWindowEvent;
        private readonly ManualResetEvent stopShowThread = new ManualResetEvent(false);
        private readonly Thread showWindowThread;
        private Process coreProcess;
        private Process webProcess;
        private ChildOutputLog coreOutput;
        private ChildOutputLog webOutput;
        private WindowsJob processJob;
        private RobotWindow mainWindow;
        private bool shuttingDown;
        private bool stoppingServices;
        private bool restartPending;
        private bool mainWindowOpened;
        private bool reportedReady;
        private bool reportedAllReady;
        private bool runtimeCleanupStarted;
        private bool cachedEnvProbeValid;
        private bool cachedEnvHasCredentials;
        private DateTime cachedEnvWriteUtc;
        private int consecutiveFailures;
        private DateTime restartAtUtc;
        private DateTime servicesStartedUtc;

        // First launch lands on the LLM settings tab when no cloud key exists.
        private const string LlmSetupUrl = Program.WebUrl + "advanced?tab=llm";

        internal ClientContext(
            string runtimeRoot,
            string appRoot,
            string stableBinDir,
            StatusForm statusForm,
            EventWaitHandle showWindowEvent)
        {
            this.runtimeRoot = runtimeRoot;
            this.appRoot = appRoot;
            this.stableBinDir = stableBinDir;
            this.statusForm = statusForm;
            this.showWindowEvent = showWindowEvent;
            logRoot = Path.Combine(appRoot, "logs");
            Directory.CreateDirectory(logRoot);
            ClientLogRotation.CleanInBackground(logRoot);

            statusItem = new ToolStripMenuItem("状态：正在启动") { Enabled = false };
            ToolStripMenuItem openItem = new ToolStripMenuItem("打开控制台");
            openItem.Click += delegate { ShowMainWindow(); };
            ToolStripMenuItem restartItem = new ToolStripMenuItem("重启全部服务");
            restartItem.Click += delegate { RestartServices(); };
            ToolStripMenuItem logsItem = new ToolStripMenuItem("打开日志目录");
            logsItem.Click += delegate { OpenLogs(); };
            ToolStripMenuItem exitItem = new ToolStripMenuItem("退出");
            exitItem.Click += delegate { ExitClient(); };

            ContextMenuStrip menu = new ContextMenuStrip();
            menu.Items.Add(statusItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(openItem);
            menu.Items.Add(restartItem);
            menu.Items.Add(logsItem);
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add(exitItem);

            trayIcon = new NotifyIcon();
            trayIcon.Text = "Open Desk Bot V2 - 正在启动";
            trayIcon.Icon = AppIcon.Tray;
            trayIcon.ContextMenuStrip = menu;
            trayIcon.Visible = true;
            trayIcon.DoubleClick += delegate { ShowMainWindow(); };

            monitorTimer = new System.Windows.Forms.Timer();
            monitorTimer.Interval = 750;
            monitorTimer.Tick += MonitorTick;

            showWindowThread = new Thread(WaitForShowWindowRequests);
            showWindowThread.IsBackground = true;
            showWindowThread.Name = "OpenDeskBotV2-show-window";
            showWindowThread.Start();

            try
            {
                StartServices();
                monitorTimer.Start();
            }
            catch (Exception exception)
            {
                LauncherLog.Write("initial service startup failed", exception);
                StopServices();
                throw;
            }
        }

        private void StartServices()
        {
            if (PortIsListening(5050) || PortIsListening(9000))
            {
                throw new InvalidOperationException(
                    "5050 或 9000 端口已被其它程序占用。请先关闭旧版服务后重试。 ");
            }

            stoppingServices = false;
            restartPending = false;
            reportedReady = false;
            reportedAllReady = false;
            mainWindowOpened = false;
            servicesStartedUtc = DateTime.UtcNow;
            statusItem.Text = "状态：正在启动";
            trayIcon.Text = "Open Desk Bot V2 - 正在启动";
            statusForm.UpdateStatus("正在启动 Web、Core、LiveKit 与 RTC Agent…", 98);
            if (!statusForm.Visible)
            {
                statusForm.Show();
            }

            processJob = new WindowsJob();
            string stamp = DateTime.Now.ToString("yyyyMMdd-HHmmss", CultureInfo.InvariantCulture);
            coreOutput = new ChildOutputLog(Path.Combine(logRoot, "core-" + stamp + ".log"));
            webOutput = new ChildOutputLog(Path.Combine(logRoot, "web-" + stamp + ".log"));

            try
            {
                // Core runs in its own process group so StopServices can send
                // CTRL_BREAK for a graceful shutdown before the job object
                // hard-kills whatever is left.
                coreProcess = ProcessGroupLauncher.Start(
                    BuildPythonStartInfo("deskbot_server", "core.log"),
                    coreOutput);
                processJob.Add(coreProcess);
                webProcess = StartPythonModule("deskbot_server.web", webOutput, "web.log");
                processJob.Add(webProcess);
                LauncherLog.Write(
                    "services launched core_pid=" + coreProcess.Id.ToString(CultureInfo.InvariantCulture) +
                    " web_pid=" + webProcess.Id.ToString(CultureInfo.InvariantCulture));
            }
            catch
            {
                StopServices();
                throw;
            }
        }

        private Process StartPythonModule(
            string module,
            ChildOutputLog output,
            string applicationLogName)
        {
            ProcessStartInfo start = BuildPythonStartInfo(module, applicationLogName);
            Process process = new Process();
            process.StartInfo = start;
            process.EnableRaisingEvents = true;
            process.OutputDataReceived += output.OnOutput;
            process.ErrorDataReceived += output.OnError;
            if (!process.Start())
            {
                throw new InvalidOperationException("无法启动 " + module);
            }
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            return process;
        }

        private ProcessStartInfo BuildPythonStartInfo(
            string module,
            string applicationLogName)
        {
            // The interpreter is taken from the version-stable bin directory so
            // that sys.executable (and therefore every python.exe the RTC agent
            // spawns from it) has a path that never changes across upgrades.
            // Business sources stay in the versioned runtime — they never bind a
            // socket, so their path is irrelevant to the firewall. If the stable
            // materialization is unavailable, fall back to the versioned Python.
            string stablePythonRoot = stableBinDir != null
                ? Path.Combine(stableBinDir, "python")
                : null;
            string pythonRoot =
                (stablePythonRoot != null &&
                 File.Exists(Path.Combine(stablePythonRoot, "python.exe")))
                    ? stablePythonRoot
                    : Path.Combine(runtimeRoot, "python");
            string python = Path.Combine(pythonRoot, "python.exe");
            string sourceRoot = Path.Combine(runtimeRoot, "app", "src");
            // site-packages always loads from the VERSIONED runtime: the
            // firewall only matches the executable's image path, so only the
            // interpreter core needs the stable location. Keeping the ~450MB
            // package tree out of the stable materialization turns the
            // per-upgrade copy from minutes into seconds.
            string sitePackages = Path.Combine(
                runtimeRoot, "python", "Lib", "site-packages");

            string versionedLiveKit =
                Path.Combine(runtimeRoot, "app", "bin", "livekit-server.exe");
            string stableLiveKit = stableBinDir != null
                ? Path.Combine(stableBinDir, "livekit-server.exe")
                : null;
            string liveKitBinary =
                (stableLiveKit != null && File.Exists(stableLiveKit))
                    ? stableLiveKit
                    : versionedLiveKit;

            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = python;
            start.Arguments = "-u -m " + module;
            start.WorkingDirectory = appRoot;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;

            SetEnvironment(start, "PYTHONHOME", pythonRoot);
            SetEnvironment(start, "PYTHONPATH", sourceRoot + Path.PathSeparator + sitePackages);
            SetEnvironment(start, "PYTHONNOUSERSITE", "1");
            SetEnvironment(start, "PYTHONDONTWRITEBYTECODE", "1");
            SetEnvironment(start, "PYTHONUNBUFFERED", "1");
            SetEnvironment(start, "PYTHONUTF8", "1");
            SetEnvironment(start, "PYTHONIOENCODING", "utf-8");
            SetEnvironment(start, "VIRTUAL_ENV", pythonRoot);
            string inheritedPath = start.EnvironmentVariables["Path"] ??
                start.EnvironmentVariables["PATH"] ?? "";
            SetEnvironment(
                start,
                "Path",
                pythonRoot + Path.PathSeparator +
                Path.Combine(pythonRoot, "DLLs") + Path.PathSeparator +
                inheritedPath);

            SetEnvironment(start, "DESKBOT_CLIENT_MODE", "1");
            SetEnvironment(start, "DESKBOT_PROJECT_ROOT", appRoot);
            SetEnvironment(start, "DESKBOT_DATA_DIR", Path.Combine(appRoot, "data"));
            SetEnvironment(start, "DESKBOT_MODELS_DIR", Path.Combine(runtimeRoot, "app", "models"));
            SetEnvironment(start, "DESKBOT_SERVER_CONFIG", Path.Combine(appRoot, "config.yaml"));
            SetEnvironment(start, "DESKBOT_ENV_FILE", Path.Combine(appRoot, ".env"));
            SetEnvironment(start, "DESKBOT_DB_PATH", Path.Combine(appRoot, "data", "opendesk.db"));
            SetEnvironment(start, "DESKBOT_LOCAL_LIVEKIT_BINARY", liveKitBinary);
            if (stableBinDir != null)
            {
                // local_livekit / rtc_agent_sdk read this to prefer the stable
                // socket-binding images over their versioned copies.
                SetEnvironment(start, "DESKBOT_STABLE_BIN_DIR", stableBinDir);
            }
            SetEnvironment(start, "DESKBOT_SERVER_HOST", "127.0.0.1");
            SetEnvironment(start, "DESKBOT_SERVER_PORT", "9000");
            SetEnvironment(start, "DESKBOT_WEB_HOST", "127.0.0.1");
            SetEnvironment(start, "DESKBOT_WEB_PORT", "5050");
            SetEnvironment(start, "DESKBOT_WEB_DEBUG", "0");
            SetEnvironment(start, "DESKBOT_USB_SERIAL_ENABLED", "1");
            SetEnvironment(start, "DESKBOT_SERVER_LOG_FILE", Path.Combine(logRoot, applicationLogName));
            SetEnvironment(start, "DESKBOT_WEB_SECRET_KEY", SharedSecret.Value);
            return start;
        }

        private static void SetEnvironment(ProcessStartInfo start, string name, string value)
        {
            string existingKey = start.EnvironmentVariables.Keys
                .Cast<string>()
                .FirstOrDefault(delegate(string key)
                {
                    return String.Equals(key, name, StringComparison.OrdinalIgnoreCase);
                });
            if (existingKey != null && !String.Equals(existingKey, name, StringComparison.Ordinal))
            {
                start.EnvironmentVariables.Remove(existingKey);
            }
            start.EnvironmentVariables[name] = value;
        }

        private void WaitForShowWindowRequests()
        {
            WaitHandle[] handles = { showWindowEvent, stopShowThread };
            while (!shuttingDown)
            {
                int signaled = WaitHandle.WaitAny(handles);
                if (signaled == 1 || shuttingDown)
                {
                    return;
                }
                try
                {
                    statusForm.BeginInvoke(new Action(ShowMainWindow));
                }
                catch (InvalidOperationException)
                {
                    return;
                }
            }
        }

        private void ShowMainWindow()
        {
            ShowMainWindowAt(null);
        }

        private void ShowMainWindowAt(string url)
        {
            if (shuttingDown)
            {
                return;
            }
            if (!PortIsListening(5050))
            {
                statusForm.UpdateStatus("Web 服务正在启动，完成后自动打开客户端…", 98);
                if (!statusForm.Visible)
                {
                    statusForm.Show();
                }
                statusForm.Activate();
                return;
            }
            if (mainWindow == null || mainWindow.IsDisposed)
            {
                NativeLibrarySearch.Configure(runtimeRoot);
                mainWindow = new RobotWindow(appRoot);
            }
            if (url != null)
            {
                mainWindow.NavigateTo(url);
            }
            else
            {
                mainWindow.NavigateHome();
            }
            if (!mainWindow.Visible)
            {
                mainWindow.Show();
            }
            if (mainWindow.WindowState == FormWindowState.Minimized)
            {
                mainWindow.WindowState = FormWindowState.Normal;
            }
            mainWindow.BringToFront();
            mainWindow.Activate();
        }

        private void CloseMainWindow()
        {
            if (mainWindow == null || mainWindow.IsDisposed)
            {
                return;
            }
            mainWindow.AllowClose();
            mainWindow.Close();
            mainWindow.Dispose();
            mainWindow = null;
        }

        private void StopShowWindowThread()
        {
            stopShowThread.Set();
            if (showWindowThread != null &&
                showWindowThread.IsAlive &&
                Thread.CurrentThread != showWindowThread)
            {
                showWindowThread.Join(2000);
            }
        }

        private void MonitorTick(object sender, EventArgs eventArgs)
        {
            if (shuttingDown || stoppingServices)
            {
                return;
            }
            if (restartPending)
            {
                if (DateTime.UtcNow >= restartAtUtc)
                {
                    TryScheduledRestart();
                }
                return;
            }

            if (!ProcessIsAlive(coreProcess) || !ProcessIsAlive(webProcess))
            {
                ScheduleRestart("服务进程意外退出");
                return;
            }

            bool webReady = PortIsListening(5050);
            bool coreReady = PortIsListening(9000);
            bool liveKitReady = PortIsListening(7880);
            bool agentReady = PortIsListening(18790);

            if (webReady && !mainWindowOpened)
            {
                mainWindowOpened = true;
                if (!CloudCredentialsConfigured())
                {
                    // Fresh installs have no .env yet, so land directly on
                    // the LLM settings tab instead of an unusable voice UI.
                    ShowMainWindowAt(LlmSetupUrl);
                }
                else
                {
                    ShowMainWindow();
                }
                statusForm.Hide();
            }

            // Readiness has two tiers. Web (5050) and Core (9000) are the
            // hard requirement: local and USB features work with just these
            // two, so they alone decide success. LiveKit (7880) and the RTC
            // Agent (18790) are optional cloud voice services - the agent
            // never listens until cloud credentials are configured, so their
            // absence only shows in the status text and never restarts.
            if (webReady && coreReady)
            {
                if (!reportedReady)
                {
                    reportedReady = true;
                    consecutiveFailures = 0;
                    statusForm.Hide();
                    trayIcon.ShowBalloonTip(
                        3000,
                        "Open Desk Bot V2",
                        "Web 与 Core 已就绪，控制台可以使用了。",
                        ToolTipIcon.Info);
                    LauncherLog.Write("core services are listening (web=5050 core=9000)");
                }
                if (!runtimeCleanupStarted)
                {
                    runtimeCleanupStarted = true;
                    RuntimeGarbageCollector.CollectInBackground(appRoot, runtimeRoot);
                }
                if (liveKitReady && agentReady)
                {
                    statusItem.Text = "状态：全部服务运行中";
                    trayIcon.Text = "Open Desk Bot V2 - 运行中";
                    if (!reportedAllReady)
                    {
                        reportedAllReady = true;
                        LauncherLog.Write("all services are listening");
                    }
                }
                else if (!CloudCredentialsConfigured())
                {
                    statusItem.Text = "状态：RTC 未就绪：未配置云服务凭证";
                    trayIcon.Text = "Open Desk Bot V2 - RTC 未配置云服务凭证";
                }
                else
                {
                    statusItem.Text = "状态：语音服务启动中…";
                    trayIcon.Text = "Open Desk Bot V2 - 语音服务启动中";
                }
            }
            else if (webReady)
            {
                statusItem.Text = "状态：控制台已启动，核心服务准备中";
                trayIcon.Text = "Open Desk Bot V2 - 核心服务准备中";
            }
            else
            {
                statusItem.Text = "状态：正在启动全部服务";
            }

            if (!reportedReady && DateTime.UtcNow - servicesStartedUtc > TimeSpan.FromMinutes(3))
            {
                ScheduleRestart("Web 或 Core 服务在三分钟内未就绪");
            }
        }

        private bool CloudCredentialsConfigured()
        {
            string envPath = Path.Combine(appRoot, ".env");
            try
            {
                if (!File.Exists(envPath))
                {
                    cachedEnvProbeValid = false;
                    return false;
                }
                DateTime lastWriteUtc = File.GetLastWriteTimeUtc(envPath);
                if (!cachedEnvProbeValid || lastWriteUtc != cachedEnvWriteUtc)
                {
                    cachedEnvHasCredentials = EnvFileHasCloudKey(envPath);
                    cachedEnvWriteUtc = lastWriteUtc;
                    cachedEnvProbeValid = true;
                }
                return cachedEnvHasCredentials;
            }
            catch (IOException)
            {
                return cachedEnvProbeValid && cachedEnvHasCredentials;
            }
            catch (UnauthorizedAccessException)
            {
                return cachedEnvProbeValid && cachedEnvHasCredentials;
            }
        }

        private static bool EnvFileHasCloudKey(string envPath)
        {
            // Mirrors rtc_agent_sdk._can_start: any non-empty LLM key name
            // counts as "cloud credentials configured".
            foreach (string rawLine in File.ReadAllLines(envPath))
            {
                string line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith("#", StringComparison.Ordinal))
                {
                    continue;
                }
                int separator = line.IndexOf('=');
                if (separator <= 0)
                {
                    continue;
                }
                string name = line.Substring(0, separator).Trim();
                string value = line.Substring(separator + 1).Trim().Trim('"', '\'');
                if (value.Length == 0)
                {
                    continue;
                }
                if (String.Equals(name, "LLM_API_KEY", StringComparison.OrdinalIgnoreCase) ||
                    String.Equals(name, "ARK_API_KEY", StringComparison.OrdinalIgnoreCase) ||
                    String.Equals(name, "OPENAI_API_KEY", StringComparison.OrdinalIgnoreCase))
                {
                    return true;
                }
            }
            return false;
        }

        private void ScheduleRestart(string reason)
        {
            LauncherLog.Write(reason);
            StopServices();
            consecutiveFailures++;
            if (consecutiveFailures > 5)
            {
                statusItem.Text = "状态：启动失败，请查看日志后手动重启";
                trayIcon.Text = "Open Desk Bot V2 - 启动失败";
                statusForm.Hide();
                trayIcon.ShowBalloonTip(
                    5000,
                    "Open Desk Bot V2 启动失败",
                    "服务连续重启失败，请从托盘打开日志目录。",
                    ToolTipIcon.Error);
                return;
            }
            int delaySeconds = Math.Min(30, 1 << Math.Min(consecutiveFailures, 5));
            restartAtUtc = DateTime.UtcNow.AddSeconds(delaySeconds);
            restartPending = true;
            statusItem.Text = "状态：将在 " + delaySeconds + " 秒后自动重启";
            statusForm.UpdateStatus("服务异常，正在自动恢复…", 98);
            if (!statusForm.Visible)
            {
                statusForm.Show();
            }
        }

        private void TryScheduledRestart()
        {
            restartPending = false;
            try
            {
                StartServices();
            }
            catch (Exception exception)
            {
                LauncherLog.Write("scheduled restart failed", exception);
                ScheduleRestart("自动重启失败：" + exception.Message);
            }
        }

        private void RestartServices()
        {
            if (shuttingDown)
            {
                return;
            }
            LauncherLog.Write("manual service restart requested");
            consecutiveFailures = 0;
            restartPending = false;
            StopServices();
            try
            {
                StartServices();
            }
            catch (Exception exception)
            {
                LauncherLog.Write("manual restart failed", exception);
                ScheduleRestart("手动重启失败：" + exception.Message);
            }
        }

        private void StopServices()
        {
            stoppingServices = true;
            StopCoreGracefully();
            if (processJob != null)
            {
                try { processJob.Terminate(); }
                catch (Win32Exception exception)
                {
                    LauncherLog.Write("could not terminate job", exception);
                }
            }
            KillIfAlive(webProcess);
            KillIfAlive(coreProcess);
            DisposeProcess(ref webProcess);
            DisposeProcess(ref coreProcess);
            if (processJob != null)
            {
                processJob.Dispose();
                processJob = null;
            }
            if (webOutput != null)
            {
                webOutput.Dispose();
                webOutput = null;
            }
            if (coreOutput != null)
            {
                coreOutput.Dispose();
                coreOutput = null;
            }
            stoppingServices = false;
        }

        // Budget for Core's graceful shutdown after CTRL_BREAK. Core's exit
        // path waits up to 5s each for the RTC agent and the local LiveKit
        // child processes to terminate (two sequential asyncio.wait_for(...,
        // timeout=5.0) waits) plus scheduler/serial/watcher teardown, so 15s
        // covers 2x5s of child waits with headroom for the remaining cleanup.
        private const int CoreGracefulExitTimeoutMs = 15000;

        private void StopCoreGracefully()
        {
            // Give Core a chance to run its full shutdown sequence (device
            // and USB session teardown) before the job object hard-kills it.
            // Core runs in its own process group, so the CTRL_BREAK reaches
            // Core and its LiveKit / agent children but not the launcher.
            if (!ProcessIsAlive(coreProcess))
            {
                return;
            }
            int corePid;
            try
            {
                corePid = coreProcess.Id;
            }
            catch (InvalidOperationException)
            {
                return;
            }
            if (!ConsoleSignals.TrySendCtrlBreak(corePid))
            {
                return;
            }
            try
            {
                if (coreProcess.WaitForExit(CoreGracefulExitTimeoutMs))
                {
                    LauncherLog.Write("core exited gracefully after CTRL_BREAK");
                }
                else
                {
                    LauncherLog.Write(
                        "core ignored CTRL_BREAK for " +
                        (CoreGracefulExitTimeoutMs / 1000).ToString(CultureInfo.InvariantCulture) +
                        "s; falling back to hard termination");
                }
            }
            catch (InvalidOperationException) { }
            catch (Win32Exception exception)
            {
                LauncherLog.Write("waiting for core graceful exit failed", exception);
            }
        }

        private static void KillIfAlive(Process process)
        {
            if (!ProcessIsAlive(process))
            {
                return;
            }
            try
            {
                process.Kill();
            }
            catch (InvalidOperationException) { }
            catch (Win32Exception) { }
            // Kill can fail while the process is already terminating; still
            // wait a bounded time so an immediate StartServices does not see
            // the old listener ports as "still occupied".
            try
            {
                process.WaitForExit(3000);
            }
            catch (InvalidOperationException) { }
            catch (Win32Exception) { }
        }

        private static void DisposeProcess(ref Process process)
        {
            if (process == null)
            {
                return;
            }
            try { process.Dispose(); }
            catch (InvalidOperationException) { }
            process = null;
        }

        private static bool ProcessIsAlive(Process process)
        {
            if (process == null)
            {
                return false;
            }
            try { return !process.HasExited; }
            catch (InvalidOperationException) { return false; }
        }

        private static bool PortIsListening(int port)
        {
            try
            {
                return IPGlobalProperties.GetIPGlobalProperties()
                    .GetActiveTcpListeners()
                    .Any(delegate(System.Net.IPEndPoint endpoint)
                    {
                        return endpoint.Port == port &&
                            System.Net.IPAddress.IsLoopback(endpoint.Address);
                    });
            }
            catch (NetworkInformationException)
            {
                return false;
            }
        }

        private void OpenLogs()
        {
            try
            {
                Process.Start("explorer.exe", "\"" + logRoot + "\"");
            }
            catch (Exception exception)
            {
                LauncherLog.Write("could not open logs directory", exception);
            }
        }

        private void ExitClient()
        {
            if (shuttingDown)
            {
                return;
            }
            shuttingDown = true;
            monitorTimer.Stop();
            StopShowWindowThread();
            CloseMainWindow();
            StopServices();
            trayIcon.Visible = false;
            ExitThread();
        }

        protected override void ExitThreadCore()
        {
            if (!shuttingDown)
            {
                shuttingDown = true;
                monitorTimer.Stop();
                StopShowWindowThread();
                CloseMainWindow();
                StopServices();
            }
            trayIcon.Visible = false;
            base.ExitThreadCore();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                if (!shuttingDown)
                {
                    shuttingDown = true;
                    StopShowWindowThread();
                    CloseMainWindow();
                    StopServices();
                }
                monitorTimer.Dispose();
                trayIcon.Dispose();
                stopShowThread.Dispose();
            }
            base.Dispose(disposing);
        }
    }

    internal sealed class RobotWindow : Form
    {
        private readonly string userDataFolder;
        private readonly WebView2 browser;
        private readonly Label loadingLabel;
        private string startUrl = Program.WebUrl;
        private bool initializing;
        private bool initialized;
        private bool closeAllowed;

        internal RobotWindow(string appRoot)
        {
            Text = "Open Desk Bot V2";
            Icon = AppIcon.Window;
            StartPosition = FormStartPosition.CenterScreen;
            MinimumSize = new Size(1024, 680);
            ClientSize = new Size(1440, 900);
            WindowState = FormWindowState.Maximized;
            BackColor = Color.FromArgb(246, 247, 249);
            userDataFolder = Path.Combine(appRoot, "webview2");

            loadingLabel = new Label();
            loadingLabel.Dock = DockStyle.Fill;
            loadingLabel.TextAlign = ContentAlignment.MiddleCenter;
            loadingLabel.Font = new Font(Font.FontFamily, 12.0f, FontStyle.Regular);
            loadingLabel.ForeColor = Color.FromArgb(72, 78, 90);
            loadingLabel.Text = "正在打开机器人控制台…";
            Controls.Add(loadingLabel);

            browser = new WebView2();
            browser.Dock = DockStyle.Fill;
            browser.Visible = false;
            browser.DefaultBackgroundColor = Color.FromArgb(246, 247, 249);
            Controls.Add(browser);

            Shown += delegate { InitializeBrowser(); };
            FormClosing += OnFormClosing;
        }

        internal void NavigateTo(string url)
        {
            if (String.IsNullOrEmpty(url))
            {
                url = Program.WebUrl;
            }
            startUrl = url;
            if (!initialized || browser.CoreWebView2 == null)
            {
                // InitializeBrowser navigates to startUrl once ready.
                return;
            }
            browser.CoreWebView2.Navigate(url);
        }

        internal void NavigateHome()
        {
            if (!initialized || browser.CoreWebView2 == null)
            {
                return;
            }
            string current = browser.Source == null ? "" : browser.Source.AbsoluteUri;
            if (!String.Equals(current, Program.WebUrl, StringComparison.OrdinalIgnoreCase))
            {
                browser.CoreWebView2.Navigate(Program.WebUrl);
            }
            else
            {
                browser.CoreWebView2.Reload();
            }
        }

        internal void AllowClose()
        {
            closeAllowed = true;
        }

        private async void InitializeBrowser()
        {
            if (initialized || initializing || IsDisposed)
            {
                return;
            }
            initializing = true;
            try
            {
                Directory.CreateDirectory(userDataFolder);
                CoreWebView2Environment environment =
                    await CoreWebView2Environment.CreateAsync(null, userDataFolder);
                await browser.EnsureCoreWebView2Async(environment);
                if (IsDisposed || browser.CoreWebView2 == null)
                {
                    return;
                }
                CoreWebView2Settings settings = browser.CoreWebView2.Settings;
                settings.AreDevToolsEnabled = false;
                settings.IsStatusBarEnabled = false;
                settings.AreDefaultContextMenusEnabled = true;
                settings.AreBrowserAcceleratorKeysEnabled = true;
                browser.CoreWebView2.NavigationStarting += OnNavigationStarting;
                browser.CoreWebView2.NewWindowRequested += OnNewWindowRequested;
                browser.CoreWebView2.ProcessFailed += OnBrowserProcessFailed;
                initialized = true;
                loadingLabel.Visible = false;
                browser.Visible = true;
                browser.BringToFront();
                browser.CoreWebView2.Navigate(startUrl);
                LauncherLog.Write(
                    "embedded WebView2 ready version=" + environment.BrowserVersionString);
            }
            catch (Exception exception)
            {
                LauncherLog.Write("embedded WebView2 initialization failed", exception);
                loadingLabel.Text =
                    "内嵌页面启动失败。\r\n\r\n请安装或修复 Microsoft Edge WebView2 Runtime，" +
                    "然后从托盘退出并重新启动客户端。\r\n\r\n" + exception.Message;
                loadingLabel.ForeColor = Color.Firebrick;
                loadingLabel.Visible = true;
                loadingLabel.BringToFront();
            }
            finally
            {
                initializing = false;
            }
        }

        private void OnNavigationStarting(
            object sender,
            CoreWebView2NavigationStartingEventArgs eventArgs)
        {
            if (IsInternalUri(eventArgs.Uri))
            {
                return;
            }
            eventArgs.Cancel = true;
            Program.OpenExternalUrl(eventArgs.Uri);
        }

        private void OnNewWindowRequested(
            object sender,
            CoreWebView2NewWindowRequestedEventArgs eventArgs)
        {
            eventArgs.Handled = true;
            if (IsInternalUri(eventArgs.Uri))
            {
                browser.CoreWebView2.Navigate(eventArgs.Uri);
                return;
            }
            Program.OpenExternalUrl(eventArgs.Uri);
        }

        private void OnBrowserProcessFailed(
            object sender,
            CoreWebView2ProcessFailedEventArgs eventArgs)
        {
            LauncherLog.Write("embedded WebView2 process failed kind=" + eventArgs.ProcessFailedKind);
            if (!IsDisposed && browser.CoreWebView2 != null)
            {
                browser.CoreWebView2.Reload();
            }
        }

        private static bool IsInternalUri(string value)
        {
            Uri uri;
            if (!Uri.TryCreate(value, UriKind.Absolute, out uri))
            {
                return false;
            }
            if (String.Equals(uri.Scheme, "about", StringComparison.OrdinalIgnoreCase))
            {
                return true;
            }
            return String.Equals(uri.Scheme, "http", StringComparison.OrdinalIgnoreCase) &&
                uri.Port == 5050 &&
                (String.Equals(uri.Host, "127.0.0.1", StringComparison.OrdinalIgnoreCase) ||
                 String.Equals(uri.Host, "localhost", StringComparison.OrdinalIgnoreCase) ||
                 String.Equals(uri.Host, "::1", StringComparison.OrdinalIgnoreCase));
        }

        private void OnFormClosing(object sender, FormClosingEventArgs eventArgs)
        {
            if (!closeAllowed && eventArgs.CloseReason == CloseReason.UserClosing)
            {
                eventArgs.Cancel = true;
                Hide();
            }
        }
    }

    internal static class NativeLibrarySearch
    {
        private static bool configured;

        internal static void Configure(string runtimeRoot)
        {
            if (configured)
            {
                return;
            }
            string nativeRoot = Path.Combine(runtimeRoot, "client", "x64");
            string loader = Path.Combine(nativeRoot, "WebView2Loader.dll");
            if (!File.Exists(loader))
            {
                throw new FileNotFoundException("WebView2Loader.dll is missing", loader);
            }
            if (!SetDllDirectory(nativeRoot))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
            configured = true;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool SetDllDirectory(string pathName);
    }

    internal sealed class ChildOutputLog : IDisposable
    {
        // A child that logs in a tight loop must not grow one file without
        // bound: past MaxLogBytes the current file rolls to "<name>.log.1"
        // (replacing any previous backup) and writing continues in a fresh
        // file, keeping each pair at ~40MB worst case.
        private const long MaxLogBytes = 20L * 1024 * 1024;

        private readonly object sync = new object();
        private readonly string path;
        private StreamWriter writer;
        private long approximateLength;
        private bool disposed;

        internal ChildOutputLog(string path)
        {
            this.path = path;
            Directory.CreateDirectory(Path.GetDirectoryName(path));
            writer = OpenWriter(true);
        }

        private StreamWriter OpenWriter(bool append)
        {
            StreamWriter opened = new StreamWriter(
                path,
                append,
                new UTF8Encoding(false)) { AutoFlush = true };
            approximateLength = opened.BaseStream.Length;
            return opened;
        }

        internal void OnOutput(object sender, DataReceivedEventArgs eventArgs)
        {
            Write("OUT", eventArgs.Data);
        }

        internal void OnError(object sender, DataReceivedEventArgs eventArgs)
        {
            Write("ERR", eventArgs.Data);
        }

        internal void WriteOutput(string line)
        {
            Write("OUT", line);
        }

        internal void WriteError(string line)
        {
            Write("ERR", line);
        }

        private void Write(string stream, string line)
        {
            if (line == null)
            {
                return;
            }
            lock (sync)
            {
                if (disposed)
                {
                    return;
                }
                try
                {
                    string entry =
                        DateTime.Now.ToString("o", CultureInfo.InvariantCulture) +
                        " [" + stream + "] " + line;
                    writer.WriteLine(entry);
                    approximateLength += Encoding.UTF8.GetByteCount(entry) + 2;
                    if (approximateLength > MaxLogBytes)
                    {
                        RollOver();
                    }
                }
                catch (IOException) { }
                catch (ObjectDisposedException) { }
            }
        }

        // Caller holds the lock. Failures never throw: if the backup move
        // does not work (file locked by a log viewer), the oversized file is
        // reopened truncated so the size stays bounded either way.
        private void RollOver()
        {
            writer.Dispose();
            bool append = true;
            try
            {
                string backup = path + ".1";
                if (File.Exists(backup))
                {
                    File.Delete(backup);
                }
                File.Move(path, backup);
            }
            catch (IOException)
            {
                append = false;
            }
            catch (UnauthorizedAccessException)
            {
                append = false;
            }
            writer = OpenWriter(append);
        }

        public void Dispose()
        {
            lock (sync)
            {
                if (disposed)
                {
                    return;
                }
                disposed = true;
                writer.Dispose();
            }
        }
    }

    internal static class SharedSecret
    {
        private static readonly Lazy<string> Secret = new Lazy<string>(Create);

        internal static string Value { get { return Secret.Value; } }

        private static string Create()
        {
            byte[] bytes = new byte[48];
            using (RandomNumberGenerator random = RandomNumberGenerator.Create())
            {
                random.GetBytes(bytes);
            }
            return Convert.ToBase64String(bytes);
        }
    }

    internal static class LauncherLog
    {
        private static readonly object Sync = new object();
        private static string path;

        internal static void Initialize(string logPath)
        {
            path = logPath;
            Directory.CreateDirectory(Path.GetDirectoryName(logPath));
            Write("launcher initialized version=" +
                Assembly.GetExecutingAssembly().GetName().Version);
        }

        internal static void Write(string message)
        {
            Write(message, null);
        }

        internal static void Write(string message, Exception exception)
        {
            if (String.IsNullOrEmpty(path))
            {
                return;
            }
            lock (Sync)
            {
                try
                {
                    string line = DateTime.Now.ToString("o", CultureInfo.InvariantCulture) +
                        " " + message;
                    if (exception != null)
                    {
                        line += Environment.NewLine + exception;
                    }
                    File.AppendAllText(path, line + Environment.NewLine, new UTF8Encoding(false));
                }
                catch (IOException) { }
            }
        }
    }

    internal static class ConsoleSignals
    {
        private const uint CtrlBreakEvent = 1;

        // The launcher is a GUI process without a console, so delivering a
        // console control event takes the standard dance: temporarily attach
        // to the target's hidden console, tell Windows to leave this process
        // alone, raise CTRL_BREAK for the target's process group, then detach
        // again. The event only reaches the target group because the target
        // was created with CREATE_NEW_PROCESS_GROUP.
        internal static bool TrySendCtrlBreak(int processId)
        {
            if (!AttachConsole((uint)processId))
            {
                LauncherLog.Write(
                    "AttachConsole failed pid=" +
                    processId.ToString(CultureInfo.InvariantCulture) +
                    " error=" +
                    Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture));
                return false;
            }
            bool ignoreInstalled = SetConsoleCtrlHandler(IntPtr.Zero, true);
            try
            {
                if (!GenerateConsoleCtrlEvent(CtrlBreakEvent, (uint)processId))
                {
                    LauncherLog.Write(
                        "GenerateConsoleCtrlEvent failed pid=" +
                        processId.ToString(CultureInfo.InvariantCulture) +
                        " error=" +
                        Marshal.GetLastWin32Error().ToString(CultureInfo.InvariantCulture));
                    return false;
                }
                LauncherLog.Write(
                    "CTRL_BREAK sent to core process group pid=" +
                    processId.ToString(CultureInfo.InvariantCulture));
                return true;
            }
            finally
            {
                FreeConsole();
                if (ignoreInstalled)
                {
                    SetConsoleCtrlHandler(IntPtr.Zero, false);
                }
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AttachConsole(uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool FreeConsole();

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool GenerateConsoleCtrlEvent(uint ctrlEvent, uint processGroupId);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetConsoleCtrlHandler(IntPtr handlerRoutine, bool add);
    }

    internal static class ProcessGroupLauncher
    {
        private const uint CreateNewProcessGroup = 0x00000200;
        private const uint CreateUnicodeEnvironment = 0x00000400;
        private const uint CreateNoWindow = 0x08000000;
        private const int StartFUseStdHandles = 0x00000100;

        // System.Diagnostics.Process cannot pass CREATE_NEW_PROCESS_GROUP,
        // which StopServices needs so CTRL_BREAK can target Core without
        // hitting the launcher. The process is therefore created through
        // CreateProcess directly, and stdout / stderr are pumped from
        // anonymous pipes into the same child log used by Process children.
        internal static Process Start(ProcessStartInfo start, ChildOutputLog output)
        {
            AnonymousPipeServerStream standardOutput = null;
            AnonymousPipeServerStream standardError = null;
            IntPtr processHandle = IntPtr.Zero;
            try
            {
                standardOutput = new AnonymousPipeServerStream(
                    PipeDirection.In,
                    HandleInheritability.Inheritable);
                standardError = new AnonymousPipeServerStream(
                    PipeDirection.In,
                    HandleInheritability.Inheritable);

                STARTUPINFO startup = new STARTUPINFO();
                startup.cb = Marshal.SizeOf(typeof(STARTUPINFO));
                startup.dwFlags = StartFUseStdHandles;
                startup.hStdInput = IntPtr.Zero;
                startup.hStdOutput = standardOutput.ClientSafePipeHandle.DangerousGetHandle();
                startup.hStdError = standardError.ClientSafePipeHandle.DangerousGetHandle();

                StringBuilder commandLine = new StringBuilder(
                    "\"" + start.FileName + "\" " + start.Arguments);
                PROCESS_INFORMATION information;
                if (!CreateProcess(
                    null,
                    commandLine,
                    IntPtr.Zero,
                    IntPtr.Zero,
                    true,
                    CreateNewProcessGroup | CreateUnicodeEnvironment | CreateNoWindow,
                    BuildEnvironmentBlock(start),
                    start.WorkingDirectory,
                    ref startup,
                    out information))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
                processHandle = information.hProcess;
                CloseHandle(information.hThread);

                // Close the parent copies of the write ends so the pumps see
                // end-of-stream as soon as the child exits.
                standardOutput.DisposeLocalCopyOfClientHandle();
                standardError.DisposeLocalCopyOfClientHandle();
                StartPump(standardOutput, output.WriteOutput, "OpenDeskBotV2-core-stdout");
                StartPump(standardError, output.WriteError, "OpenDeskBotV2-core-stderr");
                standardOutput = null;
                standardError = null;

                // The raw handle held in processHandle keeps the pid from
                // being recycled until the managed Process object exists.
                return Process.GetProcessById(information.dwProcessId);
            }
            finally
            {
                if (processHandle != IntPtr.Zero)
                {
                    CloseHandle(processHandle);
                }
                if (standardOutput != null)
                {
                    standardOutput.Dispose();
                }
                if (standardError != null)
                {
                    standardError.Dispose();
                }
            }
        }

        private static void StartPump(
            AnonymousPipeServerStream pipe,
            Action<string> sink,
            string threadName)
        {
            Thread pump = new Thread(delegate()
            {
                try
                {
                    using (StreamReader reader = new StreamReader(pipe, Encoding.UTF8))
                    {
                        string line;
                        while ((line = reader.ReadLine()) != null)
                        {
                            sink(line);
                        }
                    }
                }
                catch (IOException) { }
                catch (ObjectDisposedException) { }
            });
            pump.IsBackground = true;
            pump.Name = threadName;
            pump.Start();
        }

        private static string BuildEnvironmentBlock(ProcessStartInfo start)
        {
            List<string> names = start.EnvironmentVariables.Keys
                .Cast<string>()
                .ToList();
            names.Sort(StringComparer.OrdinalIgnoreCase);
            StringBuilder block = new StringBuilder();
            foreach (string name in names)
            {
                block.Append(name);
                block.Append('=');
                block.Append(start.EnvironmentVariables[name]);
                block.Append('\0');
            }
            // The marshalled LPWSTR adds the final terminator, producing the
            // double-null ending CreateProcess expects.
            block.Append('\0');
            return block.ToString();
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct STARTUPINFO
        {
            public int cb;
            public string lpReserved;
            public string lpDesktop;
            public string lpTitle;
            public int dwX;
            public int dwY;
            public int dwXSize;
            public int dwYSize;
            public int dwXCountChars;
            public int dwYCountChars;
            public int dwFillAttribute;
            public int dwFlags;
            public short wShowWindow;
            public short cbReserved2;
            public IntPtr lpReserved2;
            public IntPtr hStdInput;
            public IntPtr hStdOutput;
            public IntPtr hStdError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct PROCESS_INFORMATION
        {
            public IntPtr hProcess;
            public IntPtr hThread;
            public int dwProcessId;
            public int dwThreadId;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern bool CreateProcess(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            bool inheritHandles,
            uint creationFlags,
            string environment,
            string currentDirectory,
            ref STARTUPINFO startupInformation,
            out PROCESS_INFORMATION processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);
    }

    internal sealed class WindowsJob : IDisposable
    {
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private IntPtr handle;

        internal WindowsJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            if (handle == IntPtr.Zero)
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }

            JOBOBJECT_EXTENDED_LIMIT_INFORMATION information =
                new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
            information.BasicLimitInformation.LimitFlags = JobObjectLimitKillOnJobClose;
            int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
            IntPtr pointer = Marshal.AllocHGlobal(length);
            try
            {
                Marshal.StructureToPtr(information, pointer, false);
                if (!SetInformationJobObject(handle, 9, pointer, (uint)length))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error());
                }
            }
            finally
            {
                Marshal.FreeHGlobal(pointer);
            }
        }

        internal void Add(Process process)
        {
            if (!AssignProcessToJobObject(handle, process.Handle))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error());
            }
        }

        internal void Terminate()
        {
            if (handle != IntPtr.Zero && !TerminateJobObject(handle, 0))
            {
                int error = Marshal.GetLastWin32Error();
                if (error != 5 && error != 6)
                {
                    throw new Win32Exception(error);
                }
            }
        }

        public void Dispose()
        {
            if (handle != IntPtr.Zero)
            {
                CloseHandle(handle);
                handle = IntPtr.Zero;
            }
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IO_COUNTERS
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
            public IO_COUNTERS IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
        private static extern IntPtr CreateJobObject(IntPtr securityAttributes, string name);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            int informationClass,
            IntPtr information,
            uint informationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern bool CloseHandle(IntPtr handle);
    }
}
