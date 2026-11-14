using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

namespace WinAppsManager.Services;

/// <summary>日志级别；写入文件时只按级别文案区分，默认记录 Info 及以上。</summary>
public enum AppLogLevel
{
    Verbose,
    Info,
    Warning,
    Error
}

/// <summary>
/// 客户端轻量文件日志。日志写到 <c>%LOCALAPPDATA%\WinAppsManager\Logs\</c>：
/// 按天分文件（<c>app-yyyy-MM-dd.log</c>），单文件超过 5 MB 自动追加序号，
/// 保留最近 14 天。日志失败绝不能影响主流程，所有 IO 异常都被吞掉并回退到 Debug 输出。
/// 不记录签名下载地址、代理密码、令牌或安装包内容。
/// </summary>
public static class AppLogService
{
    private const int RetentionDays = 14;
    private const long MaxFileBytes = 5 * 1024 * 1024;

    private static readonly object Sync = new();
    private static readonly List<(string Line, Exception? Exception)> PendingEntries = new();
    private const int MaxPendingEntries = 500;
    private static StreamWriter? _writer;
    private static string? _writerPath;
    private static DateOnly _writerDate;
    private static int _writerPart;
    private static string? _logDirectory;
    private static int _droppedPendingEntries;
    private static bool _initialized;
    private static bool _settingsApplied;
    private static bool _deferFileWrites;

    /// <summary>为 false 时只输出到 Debug/控制台，不写文件。由设置页“启用文件日志”控制。</summary>
    public static bool Enabled { get; set; } = true;

    private static AppLogLevel _minimumLevel = AppLogLevel.Info;

    /// <summary>
    /// 写入文件的最低级别，默认 <see cref="AppLogLevel.Info"/>；设置里开启“详细日志”后为
    /// <see cref="AppLogLevel.Verbose"/>。它只过滤写入，不影响 <see cref="Enabled"/> 的“是否写文件”语义。
    /// </summary>
    public static AppLogLevel MinimumLevel
    {
        get => _minimumLevel;
        set => _minimumLevel = value is AppLogLevel.Verbose or AppLogLevel.Info or AppLogLevel.Warning or AppLogLevel.Error
            ? value
            : AppLogLevel.Info;
    }

    /// <summary>
    /// 应用设置页的日志选项（设置加载或修改后调用）：<paramref name="fileLoggingEnabled"/> 控制是否写文件，
    /// <paramref name="verboseLogging"/> 控制是否记录 Verbose 级别。首次应用会把设置加载前缓存的启动日志
    /// 按原顺序写出；如果此时文件日志被关闭，则直接丢弃缓存，不生成日志文件。
    /// </summary>
    public static void ApplySettings(bool fileLoggingEnabled, bool verboseLogging)
    {
        if (!_settingsApplied)
        {
            _settingsApplied = true;
            _deferFileWrites = false;
            Enabled = fileLoggingEnabled;
            MinimumLevel = verboseLogging ? AppLogLevel.Verbose : AppLogLevel.Info;
            if (fileLoggingEnabled)
            {
                FlushPendingEntries();
                Info($"日志设置已应用：文件日志=启用，最低级别={LevelText(MinimumLevel)}。");
            }
            else
            {
                // 关闭文件日志：丢弃启动阶段缓存，不创建日志文件，只保留调试器输出。
                ClearPendingEntries();
                System.Diagnostics.Debug.WriteLine("文件日志已关闭：运行信息只输出到调试器。");
            }
            return;
        }

        // 运行中切换：关闭前先把动作写进已有日志文件，方便事后核对。
        if (Enabled && !fileLoggingEnabled)
        {
            Info("文件日志已关闭：之后的运行信息只输出到调试器，不再写入日志文件。");
        }

        Enabled = fileLoggingEnabled;
        MinimumLevel = verboseLogging ? AppLogLevel.Verbose : AppLogLevel.Info;
        if (fileLoggingEnabled)
        {
            Info($"日志设置已应用：文件日志=启用，最低级别={LevelText(MinimumLevel)}。");
        }
    }

    public static string LogDirectory => _logDirectory ??= Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinAppsManager", "Logs");

    /// <summary>当前（或下一条将要写入的）日志文件完整路径，供设置页显示。</summary>
    public static string CurrentLogFilePath
    {
        get
        {
            lock (Sync)
            {
                return _writerPath ?? BuildLogPath(DateOnly.FromDateTime(DateTime.Now), 0);
            }
        }
    }

    public static void Initialize()
    {
        if (_initialized)
        {
            return;
        }

        _initialized = true;
        // 设置还没加载前先缓存日志：由 ApplySettings 决定是否落盘，避免关闭文件日志时仍生成日志文件。
        _deferFileWrites = !_settingsApplied;
        string version = GetApplicationVersion();
        try
        {
            Directory.CreateDirectory(LogDirectory);
            DeleteExpiredLogs();
        }
        catch (Exception exception)
        {
            System.Diagnostics.Debug.WriteLine("初始化日志目录失败：" + exception.Message);
        }

        Info(
            $"==== 应用启动 v{version} | {RuntimeInformation.FrameworkDescription} | " +
            $"OS {Environment.OSVersion.VersionString} | {RuntimeInformation.OSArchitecture}/{RuntimeInformation.ProcessArchitecture} ====");
        Info($"日志目录：{LogDirectory}");
        Info($"程序目录：{AppContext.BaseDirectory}");
    }

    /// <summary>把设置加载前缓存的日志按原顺序写入文件；失败时保持静默。</summary>
    private static void FlushPendingEntries()
    {
        (string Line, Exception? Exception)[] entries;
        int dropped;
        lock (Sync)
        {
            entries = PendingEntries.ToArray();
            PendingEntries.Clear();
            dropped = _droppedPendingEntries;
            _droppedPendingEntries = 0;
        }

        foreach (var entry in entries)
        {
            WritePreformatted(entry.Line, entry.Exception);
        }

        if (dropped > 0)
        {
            Info($"设置加载前的日志条目过多，已丢弃最早的 {dropped} 条。");
        }
    }

    private static void ClearPendingEntries()
    {
        lock (Sync)
        {
            PendingEntries.Clear();
            _droppedPendingEntries = 0;
        }
    }

    public static void Shutdown()
    {
        if (_deferFileWrites)
        {
            // 设置服务没有完成初始化（异常退出路径）：按当前默认设置写出缓存，避免启动信息全部丢失。
            _deferFileWrites = false;
            FlushPendingEntries();
        }

        Info("==== 应用退出 ====");
        lock (Sync)
        {
            CloseWriter();
        }
    }

    public static void Verbose(string message, Exception? exception = null) => Write(AppLogLevel.Verbose, message, exception);

    public static void Info(string message) => Write(AppLogLevel.Info, message);

    public static void Warn(string message, Exception? exception = null) => Write(AppLogLevel.Warning, message, exception);

    public static void Error(string message, Exception? exception = null) => Write(AppLogLevel.Error, message, exception);

    public static void Write(AppLogLevel level, string message, Exception? exception = null)
    {
        // 低于最低级别的调用在进入 IO 前返回：关闭详细日志时 Verbose 几乎零开销，也不会写文件。
        if (level < MinimumLevel)
        {
            return;
        }

        string line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss.fff} [{LevelText(level)}] [T{Environment.CurrentManagedThreadId}] {message}";
        System.Diagnostics.Debug.WriteLine(line);
#if !DEBUG
        // Release 构建下 Trace 仍然可用，便于用调试器/日志工具查看。
        System.Diagnostics.Trace.WriteLine(line);
#endif

        if (_deferFileWrites)
        {
            // 设置加载前的日志先缓存（含异常文本），ApplySettings 再按是否写文件决定落盘或丢弃。
            lock (Sync)
            {
                if (PendingEntries.Count >= MaxPendingEntries)
                {
                    _droppedPendingEntries++;
                    return;
                }

                PendingEntries.Add((line, exception));
            }
            return;
        }

        if (!Enabled)
        {
            return;
        }

        WritePreformatted(line, exception);
    }

    /// <summary>把已经格式化好的行写入文件；任何 IO 或异常格式化失败都被吞掉，不影响主流程。</summary>
    private static void WritePreformatted(string line, Exception? exception)
    {
        if (!Enabled)
        {
            return;
        }

        lock (Sync)
        {
            try
            {
                EnsureWriter();
                _writer!.WriteLine(line);
                if (exception is not null)
                {
                    _writer.WriteLine(exception);
                }

                _writer.Flush();
            }
            catch (Exception error)
            {
                // 日志写入失败不能影响应用，关闭 writer 等下一条重试。
                System.Diagnostics.Debug.WriteLine("写入日志失败：" + error.Message);
                CloseWriter();
            }
        }
    }

    /// <summary>在资源管理器里打开日志目录。</summary>
    public static bool OpenLogFolder()
    {
        try
        {
            Directory.CreateDirectory(LogDirectory);
            Process.Start(new ProcessStartInfo(LogDirectory) { UseShellExecute = true });
            return true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
            or InvalidOperationException or System.ComponentModel.Win32Exception)
        {
            System.Diagnostics.Debug.WriteLine("打开日志目录失败：" + exception.Message);
            return false;
        }
    }

    /// <summary>日志目录下当前日志文件数量与总大小，用于设置页状态文字。</summary>
    public static (int Files, long TotalBytes) GetLogStatistics()
    {
        try
        {
            if (!Directory.Exists(LogDirectory))
            {
                return (0, 0);
            }

            int files = 0;
            long total = 0;
            foreach (var path in Directory.EnumerateFiles(LogDirectory, "app-*.log"))
            {
                files++;
                try
                {
                    total += new FileInfo(path).Length;
                }
                catch (IOException)
                {
                    // 文件正被占用/刚好被清理，忽略。
                }
            }

            return (files, total);
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            return (0, 0);
        }
    }

    private static void EnsureWriter()
    {
        var today = DateOnly.FromDateTime(DateTime.Now);
        if (_writer is not null && _writerPath is not null && _writerDate == today)
        {
            long length = 0;
            try
            {
                length = new FileInfo(_writerPath).Length;
            }
            catch (IOException)
            {
                // 忽略，按未超限处理。
            }

            if (length < MaxFileBytes)
            {
                return;
            }

            CloseWriter();
            _writerPart++;
        }
        else if (_writerDate != today)
        {
            // 跨天：先关闭前一天的 writer，再为今天新建文件。
            CloseWriter();
            _writerPart = 0;
        }

        Directory.CreateDirectory(LogDirectory);
        string path = BuildLogPath(today, _writerPart);
        var stream = new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite);
        _writer = new StreamWriter(stream, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        _writerPath = path;
        _writerDate = today;
    }

    private static void CloseWriter()
    {
        try
        {
            _writer?.Dispose();
        }
        catch (IOException)
        {
            // 忽略
        }
        finally
        {
            _writer = null;
            _writerPath = null;
        }
    }

    private static string BuildLogPath(DateOnly date, int part)
    {
        string name = part == 0
            ? $"app-{date:yyyy-MM-dd}.log"
            : $"app-{date:yyyy-MM-dd}.{part}.log";
        return Path.Combine(LogDirectory, name);
    }

    private static void DeleteExpiredLogs()
    {
        try
        {
            DateTime threshold = DateTime.Now.AddDays(-RetentionDays);
            foreach (var path in Directory.EnumerateFiles(LogDirectory, "app-*.log"))
            {
                try
                {
                    if (File.GetLastWriteTime(path) < threshold)
                    {
                        File.Delete(path);
                    }
                }
                catch (IOException)
                {
                    // 正在被占用，留到下次清理。
                }
            }
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            // 清理失败不影响启动。
        }
    }

    private static string LevelText(AppLogLevel level) => level switch
    {
        AppLogLevel.Verbose => "VERBOSE",
        AppLogLevel.Info => "INFO",
        AppLogLevel.Warning => "WARN",
        AppLogLevel.Error => "ERROR",
        _ => "INFO"
    };

    private static string GetApplicationVersion()
    {
        try
        {
            Assembly assembly = typeof(AppLogService).Assembly;
            string? informational = assembly.GetCustomAttribute<AssemblyInformationalVersionAttribute>()?.InformationalVersion;
            return informational ?? assembly.GetName().Version?.ToString() ?? "unknown";
        }
        catch (Exception)
        {
            return "unknown";
        }
    }
}
