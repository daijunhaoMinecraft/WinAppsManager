using System.ComponentModel;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows;
using System.Windows.Media;
using iNKORE.UI.WPF.Modern;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

public static class AppSettingsService
{
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        WriteIndented = true,
        Converters = { new JsonStringEnumConverter(allowIntegerValues: false) }
    };

    private static bool _isInitialized;
    private static string _settingsDirectory = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinAppsManager");

    public static AppSettings Current { get; private set; } = new();

    /// <summary>是否已调用过 <see cref="Initialize"/>（用于避免在设置加载前读取软件源等字段）。</summary>
    public static bool IsInitialized => _isInitialized;

    public static string SettingsDirectory => _settingsDirectory;

    public static string SettingsFilePath => Path.Combine(SettingsDirectory, "settings.json");

    public static string? LastError { get; private set; }

    public static event EventHandler? SettingsChanged;

    /// <summary>默认下载目录（用户没有自定义时使用）。</summary>
    public static string DefaultDownloadDirectory => InstallerDownloadService.DefaultDownloadDirectory;

    /// <summary>默认后端服务地址。</summary>
    public static string DefaultBackendBaseUrl => AppSettings.DefaultBackendBaseUrl;

    /// <summary>当前生效的下载目录：自定义值为空时回退到 <see cref="DefaultDownloadDirectory"/>。</summary>
    public static string ResolvedDownloadDirectory => string.IsNullOrWhiteSpace(Current.DownloadDirectory)
        ? DefaultDownloadDirectory
        : Current.DownloadDirectory;

    public static void Initialize(string? settingsDirectory = null)
    {
        if (_isInitialized)
        {
            if (settingsDirectory is not null && Path.GetFullPath(settingsDirectory) != SettingsDirectory)
            {
                throw new InvalidOperationException("设置已初始化，无法在运行中切换存储目录。");
            }
            return;
        }

        if (settingsDirectory is not null)
        {
            BclCompat.ThrowIfNullOrWhiteSpace(settingsDirectory);
            _settingsDirectory = Path.GetFullPath(settingsDirectory);
        }
        _isInitialized = true;
        LoadSettings();
        // 日志选项在设置加载后立即应用：旧配置缺字段时 AppSettings 默认值就是“写文件、不记录 Verbose”。
        ApplyLoggingSettings();
        ThemeManager.Current.ActualApplicationThemeChanged += OnActualApplicationThemeChanged;
        SystemParameters.StaticPropertyChanged += OnSystemParametersChanged;
        ApplyTheme();
    }

    public static void Shutdown()
    {
        if (!_isInitialized)
        {
            return;
        }

        ThemeManager.Current.ActualApplicationThemeChanged -= OnActualApplicationThemeChanged;
        SystemParameters.StaticPropertyChanged -= OnSystemParametersChanged;
        _isInitialized = false;
    }

    public static bool SetTheme(ThemePreference theme)
    {
        if (!Enum.IsDefined(theme))
        {
            throw new ArgumentOutOfRangeException(nameof(theme));
        }

        Initialize();
        Current.Theme = theme;
        ApplyTheme();
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    public static bool SetDownloadConnections(int connections)
    {
        if (connections is < InstallerDownloadOptions.MinimumConnections or > InstallerDownloadOptions.MaximumConnections)
        {
            throw new ArgumentOutOfRangeException(nameof(connections));
        }

        Initialize();
        Current.DownloadConnections = connections;
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    public static bool SetDownloadTimeoutSeconds(int seconds)
    {
        if (seconds is < InstallerDownloadOptions.MinimumTimeoutSeconds or > InstallerDownloadOptions.MaximumTimeoutSeconds)
        {
            throw new ArgumentOutOfRangeException(nameof(seconds));
        }

        Initialize();
        Current.DownloadTimeoutSeconds = seconds;
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 保存自定义下载目录；传空字符串或空白表示恢复默认目录。
    /// 路径会展开环境变量并规整为绝对路径，无效路径抛出 <see cref="ArgumentException"/>；
    /// 目录不存在时会尝试创建。设置只影响之后开始的下载任务。
    /// </summary>
    public static bool SetDownloadDirectory(string? directory)
    {
        Initialize();
        Current.DownloadDirectory = NormalizeDownloadDirectory(directory, createDirectory: true);
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 保存自定义后端服务地址；传空字符串表示恢复默认地址。
    /// 地址必须是无用户名/密码、无查询参数/片段的 http(s) 绝对地址，末尾统一补 <c>/</c>；
    /// 无效地址抛出 <see cref="ArgumentException"/>。切换后是否会重新加载目录由 <c>AppCatalogService</c> 负责。
    /// </summary>
    public static bool SetBackendBaseUrl(string? url)
    {
        Initialize();
        Current.BackendBaseUrl = NormalizeBackendBaseUrl(url);
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>规整后端服务地址：去空白、补协议、校验并统一末尾斜杠；错误时抛出 <see cref="ArgumentException"/>。</summary>
    public static string NormalizeBackendBaseUrl(string? url)
    {
        string text = (url ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return DefaultBackendBaseUrl;
        }

        if (text.Length > 300)
        {
            throw new ArgumentException("服务端地址过长，请检查后重试。", nameof(url));
        }

        // 允许用户只填 host:port（例如 192.168.1.10:8080）。
        if (!text.Contains("://", StringComparison.Ordinal))
        {
            text = "http://" + text;
        }

        if (!Uri.TryCreate(text, UriKind.Absolute, out var uri)
            || (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            throw new ArgumentException("服务端地址必须是 http:// 或 https:// 开头的绝对地址，例如 http://127.0.0.1:8080/。", nameof(url));
        }

        if (!string.IsNullOrEmpty(uri.UserInfo))
        {
            throw new ArgumentException("服务端地址不能包含用户名或密码。", nameof(url));
        }

        if (!string.IsNullOrEmpty(uri.Query) || !string.IsNullOrEmpty(uri.Fragment))
        {
            throw new ArgumentException("服务端地址不能包含查询参数（?）或片段（#）。", nameof(url));
        }

        if (string.IsNullOrWhiteSpace(uri.Host))
        {
            throw new ArgumentException("服务端地址缺少主机名。", nameof(url));
        }

        string authority = uri.GetLeftPart(UriPartial.Authority);
        string path = uri.AbsolutePath.TrimEnd('/');
        string normalized = authority + path + "/";
        if (!Uri.TryCreate(normalized, UriKind.Absolute, out _))
        {
            throw new ArgumentException("服务端地址格式无效，请检查后重试。", nameof(url));
        }

        return normalized;
    }

    private static bool TryNormalizeBackendBaseUrl(string? url, out string normalized)
    {
        try
        {
            normalized = NormalizeBackendBaseUrl(url);
            return true;
        }
        catch (ArgumentException)
        {
            normalized = DefaultBackendBaseUrl;
            return false;
        }
    }

    public static bool SetGitHubMirror(string? mirror)
    {
        Initialize();
        Current.GitHubMirror = NormalizeMirror(mirror);
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    public static bool SetProxyMode(ProxyMode mode)
    {
        if (!Enum.IsDefined(mode))
        {
            throw new ArgumentOutOfRangeException(nameof(mode));
        }

        Initialize();
        Current.ProxyMode = mode;
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    public static bool SetCustomProxyUrl(string? url)
    {
        Initialize();
        Current.CustomProxyUrl = NormalizeProxyUrl(url);
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    public static bool SetIgnoreCertificateErrors(bool ignore)
    {
        Initialize();
        Current.IgnoreCertificateErrors = ignore;
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 保存软件源选择（服务端 <c>/sources</c> 下发的 <c>source_id</c>）；空字符串表示跟随服务端默认来源。
    /// 非法 ID 会抛出 <see cref="ArgumentException"/>，不会改动当前设置。
    /// </summary>
    public static bool SetSourceId(string? sourceId)
    {
        Initialize();
        string text = (sourceId ?? string.Empty).Trim();
        if (text.Length > 0 && !TryNormalizeSourceId(text, out _))
        {
            throw new ArgumentException("软件源 ID 无效。", nameof(sourceId));
        }

        Current.SourceId = text;
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 旧 settings.json 的软件源写法（<c>AppSource</c>/<c>app_source</c> 枚举名）到服务端 <c>source_id</c> 的
    /// 一次性迁移映射。只在读取设置时使用，运行路径不按来源 ID 分支。
    /// </summary>
    private static readonly Dictionary<string, string> LegacySourceIdMap = new(StringComparer.OrdinalIgnoreCase)
    {
        ["Lenovo"] = "lenovo",
        ["BuiltIn"] = "builtin",
        ["Builtin"] = "builtin",
        ["Soft360"] = "soft360"
    };

    /// <summary>规整 <c>source_id</c>：字母/数字与 <c>. _ -</c>，最长 64 字符；非法值返回 false。</summary>
    private static bool TryNormalizeSourceId(string? value, out string normalized)
    {
        string text = (value ?? string.Empty).Trim();
        normalized = string.Empty;
        if (text.Length == 0)
        {
            return true;
        }

        if (text.Length > 64)
        {
            return false;
        }

        foreach (char character in text)
        {
            if (!char.IsLetterOrDigit(character) && character is not ('.' or '_' or '-'))
            {
                return false;
            }
        }

        normalized = text;
        return true;
    }

    /// <summary>
    /// 保存“是否写文件日志”，并立即应用到 <see cref="AppLogService"/>；旧 settings.json 缺字段时按默认开启迁移。
    /// 返回 false 表示本次运行已生效但写盘失败（原因见 <see cref="LastError"/>）。
    /// </summary>
    public static bool SetEnableFileLogging(bool enabled)
    {
        Initialize();
        Current.EnableFileLogging = enabled;
        ApplyLoggingSettings();
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 保存“是否记录 Verbose 详细日志”，并立即应用到 <see cref="AppLogService"/>；
    /// 旧 settings.json 缺字段时按默认关闭迁移。文件日志关闭时该选项不产生文件输出。
    /// </summary>
    public static bool SetVerboseLogging(bool enabled)
    {
        Initialize();
        Current.VerboseLogging = enabled;
        ApplyLoggingSettings();
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 把设置里的日志选项应用到 <see cref="AppLogService"/>：<see cref="AppSettings.EnableFileLogging"/>
    /// 决定是否写文件，<see cref="AppSettings.VerboseLogging"/> 决定最低记录级别。设置加载与修改后调用。
    /// </summary>
    private static void ApplyLoggingSettings()
    {
        AppLogService.ApplySettings(Current.EnableFileLogging, Current.VerboseLogging);
    }

    public static bool IsValidHttpUrl(string? value)
    {
        return Uri.TryCreate((value ?? string.Empty).Trim(), UriKind.Absolute, out var uri)
            && (uri.Scheme == Uri.UriSchemeHttps || uri.Scheme == Uri.UriSchemeHttp);
    }

    private static string NormalizeMirror(string? mirror)
    {
        string text = (mirror ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return string.Empty;
        }
        if (!IsValidHttpUrl(text))
        {
            throw new ArgumentException("GitHub 镜像地址必须是 http:// 或 https:// 开头的绝对地址。", nameof(mirror));
        }
        return text.EndsWith('/') ? text : text + "/";
    }

    private static string NormalizeProxyUrl(string? url)
    {
        string text = (url ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return string.Empty;
        }
        if (!IsValidHttpUrl(text))
        {
            throw new ArgumentException("自定义代理必须是 http:// 或 https:// 开头的绝对地址，例如 http://127.0.0.1:7890。", nameof(url));
        }
        return text;
    }

    /// <summary>
    /// 规整下载目录：空值表示使用默认目录；否则展开环境变量、校验并转成绝对路径（可选创建目录）。
    /// 无效时抛出 <see cref="ArgumentException"/>，调用方负责显示提示。
    /// </summary>
    private static string NormalizeDownloadDirectory(string? directory, bool createDirectory)
    {
        string text = (directory ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return string.Empty;
        }

        text = Environment.ExpandEnvironmentVariables(text);
        if (text.IndexOfAny(Path.GetInvalidPathChars()) >= 0)
        {
            throw new ArgumentException("下载目录包含无效字符，请重新选择。", nameof(directory));
        }

        if (!Path.IsPathRooted(text))
        {
            throw new ArgumentException("下载目录必须是绝对路径，例如 D:\\Downloads。", nameof(directory));
        }

        string full;
        try
        {
            full = Path.GetFullPath(text);
        }
        catch (Exception error) when (error is ArgumentException or NotSupportedException or PathTooLongException)
        {
            throw new ArgumentException("下载目录路径无效，请重新选择。", nameof(directory));
        }

        if (File.Exists(full))
        {
            throw new ArgumentException("下载目录指向的是一个文件，请选择文件夹。", nameof(directory));
        }

        // 去掉末尾分隔符（盘根例如 C:\ 保留），便于设置文件里显示统一。
        string root = Path.GetPathRoot(full) ?? string.Empty;
        if (full.Length > root.Length)
        {
            full = full.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        }

        if (createDirectory)
        {
            try
            {
                Directory.CreateDirectory(full);
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException or NotSupportedException)
            {
                throw new ArgumentException($"无法创建或访问下载目录：{error.Message}", nameof(directory));
            }
        }

        return full;
    }

    private static bool TryNormalizeDownloadDirectory(string? directory, bool createDirectory, out string normalized)
    {
        try
        {
            normalized = NormalizeDownloadDirectory(directory, createDirectory);
            return true;
        }
        catch (ArgumentException)
        {
            normalized = string.Empty;
            return false;
        }
    }

    private static void LoadSettings()
    {
        LastError = null;
        try
        {
            var settings = JsonSerializer.Deserialize<StoredSettings>(
                File.ReadAllText(SettingsFilePath), JsonOptions);
            if (settings is null || !Enum.IsDefined(settings.Theme))
            {
                throw new JsonException("设置中的主题选项无效。");
            }

            var warnings = new List<string>();
            Current = new AppSettings { Theme = settings.Theme };
            if (settings.DownloadConnections.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadInt(settings.DownloadConnections,
                        InstallerDownloadOptions.MinimumConnections, InstallerDownloadOptions.MaximumConnections,
                        out var connections))
                {
                    Current.DownloadConnections = connections;
                }
                else
                {
                    warnings.Add($"保存的下载线程数无效，已恢复为 {InstallerDownloadOptions.DefaultConnections} 个线程。重新选择线程数可保存设置。");
                }
            }

            if (settings.DownloadTimeoutSeconds.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadInt(settings.DownloadTimeoutSeconds,
                        InstallerDownloadOptions.MinimumTimeoutSeconds, InstallerDownloadOptions.MaximumTimeoutSeconds,
                        out var timeout))
                {
                    Current.DownloadTimeoutSeconds = timeout;
                }
                else
                {
                    warnings.Add($"保存的下载超时秒数无效，已恢复为 {InstallerDownloadOptions.DefaultTimeoutSeconds} 秒。");
                }
            }

            if (settings.GitHubMirror.ValueKind != JsonValueKind.Undefined)
            {                if (TryReadString(settings.GitHubMirror, out var mirror))
                {
                    try
                    {
                        Current.GitHubMirror = NormalizeMirror(mirror);
                    }
                    catch (ArgumentException)
                    {
                        warnings.Add("保存的 GitHub 镜像地址无效，已恢复为直连。");
                    }
                }
                else
                {
                    warnings.Add("保存的 GitHub 镜像地址无效，已恢复为直连。");
                }
            }

            if (settings.ProxyMode.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadString(settings.ProxyMode, out var proxyModeText)
                    && Enum.TryParse<ProxyMode>(proxyModeText, ignoreCase: true, out var proxyMode)
                    && Enum.IsDefined(proxyMode))
                {
                    Current.ProxyMode = proxyMode;
                }
                else
                {
                    warnings.Add("保存的代理模式无效，已恢复为跟随系统代理。");
                }
            }

            if (settings.CustomProxyUrl.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadString(settings.CustomProxyUrl, out var proxyUrl))
                {
                    try
                    {
                        Current.CustomProxyUrl = NormalizeProxyUrl(proxyUrl);
                    }
                    catch (ArgumentException)
                    {
                        warnings.Add("保存的自定义代理地址无效，已清除。");
                    }
                }
                else
                {
                    warnings.Add("保存的自定义代理地址无效，已清除。");
                }
            }

            if (settings.IgnoreCertificateErrors.ValueKind != JsonValueKind.Undefined)
            {
                if (settings.IgnoreCertificateErrors.ValueKind is JsonValueKind.True or JsonValueKind.False)
                {
                    Current.IgnoreCertificateErrors = settings.IgnoreCertificateErrors.GetBoolean();
                }
                else
                {
                    warnings.Add("保存的证书校验设置无效，已恢复为校验证书。");
                }
            }

            // 下载目录是后加字段：旧 settings.json 缺字段或值为空都用默认目录；
            // 非法路径只回退该字段（不创建目录，避免启动时因权限问题失败）。
            if (settings.DownloadDirectory.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadString(settings.DownloadDirectory, out var downloadDirectory)
                    && TryNormalizeDownloadDirectory(downloadDirectory, createDirectory: false, out var normalizedDirectory))
                {
                    Current.DownloadDirectory = normalizedDirectory;
                }
                else
                {
                    warnings.Add("保存的下载目录无效，已恢复为默认下载目录。");
                }
            }

            // 服务端地址是后加字段：旧 settings.json 缺字段或值无效时回退默认地址（缺字段不提示，
            // 避免升级用户每次启动都看到警告）。
            var backendElement = settings.BackendBaseUrl.ValueKind != JsonValueKind.Undefined
                ? settings.BackendBaseUrl
                : settings.BackendBaseUrlSnakeCase;
            if (backendElement.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadString(backendElement, out var backendText)
                    && TryNormalizeBackendBaseUrl(backendText, out var normalizedBackend))
                {
                    Current.BackendBaseUrl = normalizedBackend;
                }
                else
                {
                    warnings.Add("保存的服务端地址无效，已恢复为默认地址 http://127.0.0.1:8080/。");
                }
            }

            // 软件源改为服务端下发的 source_id（客户端不再有来源枚举）。新字段优先；旧 settings.json 的
            // AppSource/app_source（Lenovo / BuiltIn / Soft360）只在读取时做一次性迁移映射，运行路径不再按来源分支。
            var sourceIdElement = settings.SourceId.ValueKind != JsonValueKind.Undefined
                ? settings.SourceId
                : settings.SourceIdSnakeCase;
            if (sourceIdElement.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadString(sourceIdElement, out var sourceIdText)
                    && TryNormalizeSourceId(sourceIdText, out var normalizedSourceId))
                {
                    Current.SourceId = normalizedSourceId;
                }
                else
                {
                    warnings.Add("保存的软件源 ID 无效，已改为跟随服务端默认软件源。");
                }
            }
            else
            {
                var legacySourceElement = settings.AppSource.ValueKind != JsonValueKind.Undefined
                    ? settings.AppSource
                    : settings.AppSourceSnakeCase;
                if (legacySourceElement.ValueKind != JsonValueKind.Undefined)
                {
                    if (TryReadString(legacySourceElement, out var legacySourceText)
                        && !string.IsNullOrWhiteSpace(legacySourceText))
                    {
                        if (LegacySourceIdMap.TryGetValue(legacySourceText.Trim(), out var migratedSourceId))
                        {
                            Current.SourceId = migratedSourceId;
                            AppLogService.Info($"已把旧设置中的软件源“{legacySourceText.Trim()}”迁移为来源 ID“{migratedSourceId}”。");
                        }
                        else if (TryNormalizeSourceId(legacySourceText, out var directSourceId))
                        {
                            Current.SourceId = directSourceId;
                        }
                        else
                        {
                            warnings.Add("保存的软件源选项无效，已改为跟随服务端默认软件源。");
                        }
                    }
                }
            }

            // 日志选项是后加字段：旧 settings.json 缺字段时按默认（写文件、不记录 Verbose）迁移；
            // 非法值只回退该字段并记入 LastError，不影响其他设置。
            if (settings.EnableFileLogging.ValueKind != JsonValueKind.Undefined)
            {
                if (settings.EnableFileLogging.ValueKind is JsonValueKind.True or JsonValueKind.False)
                {
                    Current.EnableFileLogging = settings.EnableFileLogging.GetBoolean();
                }
                else
                {
                    warnings.Add("保存的文件日志开关无效，已恢复为启用文件日志。");
                }
            }

            if (settings.VerboseLogging.ValueKind != JsonValueKind.Undefined)
            {
                if (settings.VerboseLogging.ValueKind is JsonValueKind.True or JsonValueKind.False)
                {
                    Current.VerboseLogging = settings.VerboseLogging.GetBoolean();
                }
                else
                {
                    warnings.Add("保存的详细日志开关无效，已恢复为关闭详细日志。");
                }
            }

            LastError = warnings.Count > 0 ? string.Join(" ", warnings) : null;
            if (LastError is not null)
            {
                AppLogService.Warn($"设置加载时发现 {warnings.Count} 条警告，已使用修正后的值：{LastError}");
            }
        }
        catch (Exception exception) when (exception is FileNotFoundException or DirectoryNotFoundException)
        {
            Current = new AppSettings();
            AppLogService.Info("未找到已保存的设置文件，使用默认设置。");
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException)
        {
            Current = new AppSettings();
            LastError = $"无法读取已保存的设置，已暂时使用默认设置。重新保存可恢复设置文件。{exception.Message}";
            AppLogService.Warn(LastError, exception);
        }
    }

    private static bool TryReadInt(JsonElement element, int minimum, int maximum, out int value)
    {
        value = default;
        return element.ValueKind == JsonValueKind.Number
            && element.TryGetInt32(out value)
            && value >= minimum
            && value <= maximum;
    }

    private static bool TryReadString(JsonElement element, out string value)
    {
        value = string.Empty;
        if (element.ValueKind == JsonValueKind.Null)
        {
            return true;
        }
        if (element.ValueKind != JsonValueKind.String)
        {
            return false;
        }
        value = element.GetString() ?? string.Empty;
        return true;
    }

    private static bool SaveSettings()
    {
        var temporaryPath = SettingsFilePath + "." + Guid.NewGuid().ToString("N") + ".tmp";
        try
        {
            Directory.CreateDirectory(SettingsDirectory);
            File.WriteAllText(temporaryPath, JsonSerializer.Serialize(Current, JsonOptions), new UTF8Encoding(false));
            File.Move(temporaryPath, SettingsFilePath, overwrite: true);
            LastError = null;
            return true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
        {
            LastError = $"设置已在本次运行中生效，但无法保存。下次启动可能恢复原来的设置。{exception.Message}";
            AppLogService.Error(LastError, exception);
            return false;
        }
        finally
        {
            try
            {
                File.Delete(temporaryPath);
            }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException)
            {
                // A failed temporary-file cleanup must not prevent using the selected theme.
                System.Diagnostics.Debug.WriteLine(exception);
            }
        }
    }

    private static void ApplyTheme()
    {
        ThemeManager.Current.ApplicationTheme = Current.Theme switch
        {
            ThemePreference.Light => ApplicationTheme.Light,
            ThemePreference.Dark => ApplicationTheme.Dark,
            _ => null
        };
        UpdateThemeResources();
    }

    private static void OnActualApplicationThemeChanged(ThemeManager sender, object args)
    {
        RunOnApplicationDispatcher(() =>
        {
            UpdateThemeResources();
            SettingsChanged?.Invoke(null, EventArgs.Empty);
        });
    }

    private static void OnSystemParametersChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (args.PropertyName == nameof(SystemParameters.HighContrast))
        {
            RunOnApplicationDispatcher(UpdateThemeResources);
        }
    }

    private static void RunOnApplicationDispatcher(Action action)
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is null || dispatcher.HasShutdownStarted)
        {
            return;
        }

        if (dispatcher.CheckAccess())
        {
            action();
        }
        else
        {
            dispatcher.BeginInvoke(action);
        }
    }

    private static void UpdateThemeResources()
    {
        var resources = Application.Current?.Resources;
        if (resources is null)
        {
            return;
        }

        if (SystemParameters.HighContrast)
        {
            resources["Wam.PageBackgroundBrush"] = SystemColors.WindowBrush;
            resources["Wam.CardBackgroundBrush"] = SystemColors.WindowBrush;
            resources["Wam.CardBorderBrush"] = SystemColors.WindowTextBrush;
            resources["Wam.TextPrimaryBrush"] = SystemColors.WindowTextBrush;
            resources["Wam.TextSecondaryBrush"] = SystemColors.WindowTextBrush;
            resources["Wam.AccentBrush"] = SystemColors.HotTrackBrush;
            resources["Wam.AccentSoftBrush"] = SystemColors.WindowBrush;
            resources["Wam.SubtleBackgroundBrush"] = SystemColors.ControlBrush;
            resources["Wam.SuccessBrush"] = SystemColors.HotTrackBrush;
            resources["Wam.DangerBrush"] = SystemColors.WindowTextBrush;
            resources["Wam.ProgressTrackBrush"] = SystemColors.ControlDarkBrush;
            return;
        }

        var dark = ThemeManager.Current.ActualApplicationTheme == ApplicationTheme.Dark;
        resources["Wam.PageBackgroundBrush"] = CreateBrush(dark ? "#181B20" : "#F5F7FA");
        resources["Wam.CardBackgroundBrush"] = CreateBrush(dark ? "#242830" : "#FFFFFF");
        resources["Wam.CardBorderBrush"] = CreateBrush(dark ? "#343A45" : "#E0E5ED");
        resources["Wam.TextPrimaryBrush"] = CreateBrush(dark ? "#F5F7FB" : "#172238");
        resources["Wam.TextSecondaryBrush"] = CreateBrush(dark ? "#AAB5C4" : "#637188");
        resources["Wam.AccentBrush"] = CreateBrush(dark ? "#81B4FF" : "#2563EB");
        resources["Wam.AccentSoftBrush"] = CreateBrush(dark ? "#1A3454" : "#EAF1FF");
        resources["Wam.SubtleBackgroundBrush"] = CreateBrush(dark ? "#2D333D" : "#F1F4F9");
        resources["Wam.SuccessBrush"] = CreateBrush(dark ? "#4CC38A" : "#1A7F37");
        resources["Wam.DangerBrush"] = CreateBrush(dark ? "#FF99A4" : "#C42B1C");
        resources["Wam.ProgressTrackBrush"] = CreateBrush(dark ? "#39404C" : "#C9D3E0");
    }

    private static SolidColorBrush CreateBrush(string color)
    {
        var brush = new SolidColorBrush((Color)ColorConverter.ConvertFromString(color));
        brush.Freeze();
        return brush;
    }

    private sealed class StoredSettings
    {
        public ThemePreference Theme { get; set; } = ThemePreference.System;
        public JsonElement DownloadConnections { get; set; }
        public JsonElement DownloadTimeoutSeconds { get; set; }
        public JsonElement GitHubMirror { get; set; }
        public JsonElement ProxyMode { get; set; }
        public JsonElement CustomProxyUrl { get; set; }
        public JsonElement IgnoreCertificateErrors { get; set; }
        public JsonElement DownloadDirectory { get; set; }

        /// <summary>settings.json 由 <see cref="JsonSerializer"/> 直接序列化 <see cref="AppSettings"/>，键名为 PascalCase。</summary>
        public JsonElement BackendBaseUrl { get; set; }

        /// <summary>兼容手工写成 snake_case 的 <c>backend_base_url</c>。</summary>
        [JsonPropertyName("backend_base_url")]
        public JsonElement BackendBaseUrlSnakeCase { get; set; }

        /// <summary>当前软件源的 <c>source_id</c>（settings.json 由 <see cref="JsonSerializer"/> 序列化，键名为 PascalCase）。</summary>
        public JsonElement SourceId { get; set; }

        /// <summary>兼容手工写成 snake_case 的 <c>source_id</c>。</summary>
        [JsonPropertyName("source_id")]
        public JsonElement SourceIdSnakeCase { get; set; }

        /// <summary>旧版本软件源字段（Lenovo / BuiltIn / Soft360 枚举名），读取时一次性迁移为 <see cref="SourceId"/>。</summary>
        public JsonElement AppSource { get; set; }

        /// <summary>兼容手工写成 snake_case 的旧字段 <c>app_source</c>。</summary>
        [JsonPropertyName("app_source")]
        public JsonElement AppSourceSnakeCase { get; set; }

        /// <summary>是否写文件日志；缺字段按默认 true 迁移。</summary>
        public JsonElement EnableFileLogging { get; set; }

        /// <summary>是否记录 Verbose 日志；缺字段按默认 false 迁移。</summary>
        public JsonElement VerboseLogging { get; set; }
    }
}
