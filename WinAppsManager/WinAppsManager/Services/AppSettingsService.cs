using System.ComponentModel;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Imaging;
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

    /// <summary>旧版本内置的本机默认服务端；读取设置时自动迁移到当前默认在线服务端。</summary>
    private const string LegacyDefaultBackendBaseUrl = "http://127.0.0.1:8080/";
    private const string LegacyLocalhostBackendBaseUrl = "http://localhost:8080/";

    /// <summary>当前生效的下载目录：自定义值为空时回退到 <see cref="DefaultDownloadDirectory"/>。</summary>
    public static string ResolvedDownloadDirectory => string.IsNullOrWhiteSpace(Current.DownloadDirectory)
        ? DefaultDownloadDirectory
        : Current.DownloadDirectory;
    
    private static (string Path, double Opacity, bool Dark, Brush Brush)? _cachedBackgroundBrush;
    private static (Color Accent, bool Dark)? _lastAppliedAccent;

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

    /// <summary>
    /// 保存窗口系统背景样式（自动 / 无 / 亚克力 / 云母）。样式是否真正生效由
    /// <see cref="BackdropSupport"/> 按系统能力判断；不支持的样式会回退到可用样式。
    /// </summary>
    public static bool SetBackdropStyle(BackdropStyle style)
    {
        if (!Enum.IsDefined(style))
        {
            throw new ArgumentOutOfRangeException(nameof(style));
        }

        Initialize();
        Current.BackdropStyle = style;
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 保存主题强调色；传空字符串表示恢复内置的默认蓝色。无效颜色抛出 <see cref="ArgumentException"/>。
    /// 只重算主题资源，不切换浅色 / 深色模式。
    /// </summary>
    public static bool SetAccentColor(string? color)
    {
        Initialize();
        Current.AccentColor = NormalizeAccentColor(color);
        ApplyTheme();
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>
    /// 保存页面背景图；传空字符串表示恢复纯色背景。
    /// 文件不存在或格式不支持时抛出 <see cref="ArgumentException"/>，设置不会被改动。
    /// </summary>
    public static bool SetBackgroundImage(string? path)
    {
        Initialize();
        Current.BackgroundImagePath = NormalizeBackgroundImage(path);
        ApplyTheme();
        var saved = SaveSettings();
        SettingsChanged?.Invoke(null, EventArgs.Empty);
        return saved;
    }

    /// <summary>保存背景图不透明度（0.05–1）；超出范围抛出 <see cref="ArgumentOutOfRangeException"/>。</summary>
    public static bool SetBackgroundImageOpacity(double opacity)
    {
        if (opacity is < AppSettings.MinimumBackgroundImageOpacity or > AppSettings.MaximumBackgroundImageOpacity)
        {
            throw new ArgumentOutOfRangeException(nameof(opacity));
        }

        Initialize();
        Current.BackgroundImageOpacity = Math.Round(opacity, 2);
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
            throw new ArgumentException($"服务端地址必须是 http:// 或 https:// 开头的绝对地址，例如 {DefaultBackendBaseUrl}。", nameof(url));
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

    /// <summary>“外观”页可选的主题色（第一个是默认蓝色）。</summary>
    public static readonly (string Hex, string Label)[] AccentPresets =
    [
        ("#2563EB", "蓝"),
        ("#0F7B6C", "青绿"),
        ("#107C10", "绿"),
        ("#7A3EB1", "紫"),
        ("#C239B3", "洋红"),
        ("#C42B1C", "红"),
        ("#CA5010", "橙"),
        ("#986F0B", "金"),
        ("#4A5568", "灰")
    ];

    /// <summary>背景图允许的扩展名；WPF 的位图解码器覆盖这几种，其余格式直接拒绝。</summary>
    private static readonly string[] SupportedBackgroundImageExtensions =
        [".png", ".jpg", ".jpeg", ".bmp", ".gif", ".tif", ".tiff"];

    /// <summary>规整主题强调色：空 = 默认色；接受 <c>#RRGGBB</c> 或 <c>RRGGBB</c>，其他值抛出 <see cref="ArgumentException"/>。</summary>
    public static string NormalizeAccentColor(string? color)
    {
        string text = (color ?? string.Empty).Trim();
        if (text.Length == 0)
        {
            return string.Empty;
        }

        if (!TryParseColor(text, out Color parsed))
        {
            throw new ArgumentException($"主题色“{color}”不是有效的颜色值，请使用 #RRGGBB。", nameof(color));
        }

        return $"#{parsed.R:X2}{parsed.G:X2}{parsed.B:X2}";
    }

    private static bool TryNormalizeAccentColor(string? color, out string normalized)
    {
        try
        {
            normalized = NormalizeAccentColor(color);
            return true;
        }
        catch (ArgumentException)
        {
            normalized = string.Empty;
            return false;
        }
    }

    private static bool TryParseColor(string text, out Color color)
    {
        color = Colors.Transparent;
        string value = text.Trim().Trim('"');
        if (value.StartsWith('#'))
        {
            value = value[1..];
        }

        if (value.Length != 6 || !value.All(Uri.IsHexDigit))
        {
            return false;
        }

        color = Color.FromRgb(
            Convert.ToByte(value[..2], 16),
            Convert.ToByte(value.Substring(2, 2), 16),
            Convert.ToByte(value.Substring(4, 2), 16));
        return true;
    }

    /// <summary>规整背景图路径：空 = 无背景图；文件不存在或格式不支持时抛出 <see cref="ArgumentException"/>。</summary>
    public static string NormalizeBackgroundImage(string? path)
    {
        string text = (path ?? string.Empty).Trim().Trim('"');
        if (text.Length == 0)
        {
            return string.Empty;
        }

        string expanded;
        try
        {
            expanded = Path.GetFullPath(Environment.ExpandEnvironmentVariables(text));
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException
                                              or PathTooLongException or System.Security.SecurityException)
        {
            throw new ArgumentException($"背景图路径无效：{text}", nameof(path));
        }

        if (!File.Exists(expanded))
        {
            throw new ArgumentException($"找不到背景图文件：{expanded}", nameof(path));
        }

        string extension = Path.GetExtension(expanded).ToLowerInvariant();
        if (!SupportedBackgroundImageExtensions.Contains(extension))
        {
            throw new ArgumentException(
                $"背景图只支持 {string.Join("、", SupportedBackgroundImageExtensions)} 格式。",
                nameof(path));
        }

        return expanded;
    }

    private static bool TryNormalizeBackgroundImage(string? path, out string normalized)
    {
        try
        {
            normalized = NormalizeBackgroundImage(path);
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
            var migratedBackend = false;
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
                    if (string.Equals(normalizedBackend, LegacyDefaultBackendBaseUrl, StringComparison.OrdinalIgnoreCase)
                        || string.Equals(normalizedBackend, LegacyLocalhostBackendBaseUrl, StringComparison.OrdinalIgnoreCase))
                    {
                        // 旧版本默认是本机后端；部署在线服务端后，把仍在使用旧默认值的用户迁移过去。
                        normalizedBackend = DefaultBackendBaseUrl;
                        migratedBackend = true;
                    }

                    Current.BackendBaseUrl = normalizedBackend;
                }
                else
                {
                    warnings.Add($"保存的服务端地址无效，已恢复为默认地址 {DefaultBackendBaseUrl}。");
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

            // 外观（强调色 / 背景图 / 不透明度）是后加字段：缺字段按默认值走，非法值只回退该字段。
            if (settings.AccentColor.ValueKind != JsonValueKind.Undefined
                && TryReadString(settings.AccentColor, out var accentText))
            {
                if (TryNormalizeAccentColor(accentText, out var normalizedAccent))
                {
                    Current.AccentColor = normalizedAccent;
                }
                else if (accentText.Trim().Length > 0)
                {
                    warnings.Add("保存的主题色无效，已恢复为默认蓝色。");
                }
            }

            if (settings.BackgroundImagePath.ValueKind != JsonValueKind.Undefined
                && TryReadString(settings.BackgroundImagePath, out var backgroundText))
            {
                if (TryNormalizeBackgroundImage(backgroundText, out var normalizedBackground))
                {
                    Current.BackgroundImagePath = normalizedBackground;
                }
                else if (backgroundText.Trim().Length > 0)
                {
                    warnings.Add("保存的背景图不可用（文件不存在或格式不支持），已恢复为纯色背景。");
                }
            }

            if (settings.BackgroundImageOpacity.ValueKind != JsonValueKind.Undefined)
            {
                if (TryReadDouble(settings.BackgroundImageOpacity, out var backgroundOpacity))
                {
                    Current.BackgroundImageOpacity = Math.Clamp(
                        backgroundOpacity,
                        AppSettings.MinimumBackgroundImageOpacity,
                        AppSettings.MaximumBackgroundImageOpacity);
                }
                else
                {
                    warnings.Add("保存的背景图不透明度无效，已恢复为默认值。");
                }
            }

            // 窗口系统背景样式：缺字段走自动；非法值只回退该字段。
            if (settings.BackdropStyle.ValueKind != JsonValueKind.Undefined)
            {
                if (settings.BackdropStyle.ValueKind == JsonValueKind.String
                    && Enum.TryParse<BackdropStyle>(settings.BackdropStyle.GetString(), ignoreCase: true, out var backdrop)
                    && Enum.IsDefined(backdrop))
                {
                    Current.BackdropStyle = backdrop;
                }
                else
                {
                    warnings.Add("保存的窗口背景样式无效，已改为“无”。");
                }
            }

            if (migratedBackend)
            {
                if (SaveSettings())
                {
                    AppLogService.Info($"旧版默认服务端已迁移到 {DefaultBackendBaseUrl}，并已写回设置文件。");
                }
                else
                {
                    AppLogService.Warn($"旧版默认服务端已迁移到 {DefaultBackendBaseUrl}，但设置文件保存失败；本次运行仍使用新地址。");
                }
            }

            LastError = warnings.Count > 0 ? string.Join(" ", warnings) : LastError;
            if (warnings.Count > 0)
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

    private static bool TryReadDouble(JsonElement element, out double value)
    {
        value = default;
        return element.ValueKind == JsonValueKind.Number && element.TryGetDouble(out value);
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

        // 同步所有已打开窗口的 ElementTheme，防止窗口级设置拦截全局主题切换
        var elementTheme = Current.Theme switch
        {
            ThemePreference.Light => ElementTheme.Light,
            ThemePreference.Dark => ElementTheme.Dark,
            _ => ElementTheme.Default
        };
        if (Application.Current is not null)
        {
            foreach (Window window in Application.Current.Windows)
            {
                ThemeManager.SetRequestedTheme(window, elementTheme);
            }
        }

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
            resources["Wam.WindowBackgroundBrush"] = SystemColors.WindowBrush;
            resources["Wam.WindowWallpaperBrush"] = Brushes.Transparent;
            resources["Wam.PaneAcrylicVisibility"] = Visibility.Collapsed;
            resources["Wam.PaneTintColor"] = SystemColors.WindowColor;
            resources["Wam.PaneBackgroundBrush"] = SystemColors.ControlBrush;
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
            resources["Wam.IconPlateBrush"] = SystemColors.ControlBrush;
            resources["Wam.Acrylic10TintColor"] = SystemColors.WindowColor;
            resources["Wam.MoveBackgroundBrush"] = SystemColors.WindowBrush;
            return;
        }

        var dark = Current.Theme switch
        {
            ThemePreference.Dark => true,
            ThemePreference.Light => false,
            _ => ThemeManager.Current.ActualApplicationTheme == ApplicationTheme.Dark
        };
        bool hasCustomWallpaper = !string.IsNullOrWhiteSpace(Current.BackgroundImagePath)
                                  && File.Exists(Current.BackgroundImagePath);

        // if (hasCustomWallpaper)
        // {
        //     // 启用自定义壁纸时：壁纸画在窗口底层，侧边栏优先复用 iNKORE 内置亚克力画刷，否则使用半透明遮罩
        //     resources["Wam.WindowWallpaperBrush"] = CreatePageBackgroundBrush(dark);
        //     resources["Wam.PaneBackgroundBrush"] =
        //         (Application.Current?.TryFindResource("AcrylicInAppFillColorDefaultBrush") as Brush)
        //         ?? (Application.Current?.TryFindResource("SystemControlAcrylicElementBrush") as Brush)
        //         ?? CreateBrush(dark ? "#CC181B20" : "#D9F5F7FA");
        // }
        // else
        // {
        //     // 默认纯色模式：侧边栏保持透明，直接透出 MainWindow 的系统级 Acrylic/Mica 背景
        //     resources["Wam.WindowWallpaperBrush"] = Brushes.Transparent;
        //     resources["Wam.PaneBackgroundBrush"] = Brushes.Transparent;
        // }
        
        resources["Wam.WindowWallpaperBrush"] = hasCustomWallpaper
            ? CreatePageBackgroundBrush(dark)
            : Brushes.Transparent;
        resources["Wam.PaneAcrylicVisibility"] = hasCustomWallpaper
            ? Visibility.Visible
            : Visibility.Collapsed;
        resources["Wam.PaneTintColor"] = dark
            ? Color.FromRgb(0x18, 0x1B, 0x20)
            : Color.FromRgb(0xF5, 0xF7, 0xFA);
        resources["Wam.PageBackgroundBrush"] = CreatePageBackgroundBrush(dark);
        // Windows 10 系统级亚克力（Acrylic10）的着色。iNKORE 会把这里的 alpha 再乘 0.8，
        // 所以取接近不透明的主题底色：既能透出一点桌面/窗口后面的内容，又保证导航栏文字对比度稳定。
        resources["Wam.Acrylic10TintColor"] = dark
            ? Color.FromArgb(0xF2, 0x1B, 0x1F, 0x26)
            : Color.FromArgb(0xF2, 0xF5, 0xF7, 0xFA);
        // 拖动/缩放期间替换系统亚克力的窗口底色：取亚克力在深色/浅色内容上的中间值，
        // 让“停用亚克力 → 恢复亚克力”的色彩跳变尽量小。
        resources["Wam.MoveBackgroundBrush"] = CreateBrush(dark
            ? Color.FromRgb(0x31, 0x34, 0x39)
            : Color.FromRgb(0xDC, 0xE0, 0xE1));
        resources["Wam.CardBackgroundBrush"] = CreateBrush(dark ? "#242830" : "#FFFFFF");
        resources["Wam.CardBorderBrush"] = CreateBrush(dark ? "#343A45" : "#E0E5ED");
        resources["Wam.TextPrimaryBrush"] = CreateBrush(dark ? "#F5F7FB" : "#172238");
        resources["Wam.TextSecondaryBrush"] = CreateBrush(dark ? "#AAB5C4" : "#637188");
        resources["Wam.SubtleBackgroundBrush"] = CreateBrush(dark ? "#2D333D" : "#F1F4F9");
        resources["Wam.SuccessBrush"] = CreateBrush(dark ? "#4CC38A" : "#1A7F37");
        resources["Wam.DangerBrush"] = CreateBrush(dark ? "#FF99A4" : "#C42B1C");
        resources["Wam.ProgressTrackBrush"] = CreateBrush(dark ? "#39404C" : "#C9D3E0");
        // 图标底板：浅色模式下纯白 / 透明的应用图标（如 App Installer）在浅背景上几乎看不见，
        // 统一垫一层淡淡的灰底当作“图标瓦片”。
        resources["Wam.IconPlateBrush"] = CreateBrush(dark ? "#2E3540" : "#E3E8F0");
        ApplyAccentResources(resources, dark);
    }

    /// <summary>
    /// 页面背景：默认是纯色；设置了背景图时返回“主题底色 + 半透明图片”的不透明画刷。
    /// 图片单独一层是为了让它叠在底色之上（而不是透出窗口底色），这样浅色/深色下都稳定。
    /// </summary>
    private static Brush CreatePageBackgroundBrush(bool dark)
    {
        string path = Current.BackgroundImagePath;
        double opacity = Math.Clamp(
            Current.BackgroundImageOpacity,
            AppSettings.MinimumBackgroundImageOpacity,
            AppSettings.MaximumBackgroundImageOpacity);

        if (_cachedBackgroundBrush is { } cached
            && string.Equals(cached.Path, path, StringComparison.OrdinalIgnoreCase)
            && Math.Abs(cached.Opacity - opacity) < 0.001
            && cached.Dark == dark)
        {
            return cached.Brush;
        }

        var baseBrush = CreateBrush(dark ? "#181B20" : "#F5F7FA");
        if (string.IsNullOrWhiteSpace(path) || !File.Exists(path))
        {
            _cachedBackgroundBrush = (path, opacity, dark, baseBrush);
            return baseBrush;
        }

        try
        {
            var bitmap = new BitmapImage();
            bitmap.BeginInit();
            bitmap.CacheOption = BitmapCacheOption.OnLoad;
            bitmap.UriSource = new Uri(path, UriKind.Absolute);
            // 壁纸通常远大于窗口，按 1920 宽解码：省内存，缩放到窗口后依然清晰。
            bitmap.DecodePixelWidth = 1920;
            bitmap.EndInit();
            bitmap.Freeze();

            var bounds = new Rect(0, 0, Math.Max(bitmap.PixelWidth, 16), Math.Max(bitmap.PixelHeight, 9));
            var drawing = new DrawingGroup();
            drawing.Children.Add(new GeometryDrawing(baseBrush, null, new RectangleGeometry(bounds)));
            var imageLayer = new DrawingGroup
            {
                Opacity = Math.Clamp(
                    Current.BackgroundImageOpacity,
                    AppSettings.MinimumBackgroundImageOpacity,
                    AppSettings.MaximumBackgroundImageOpacity)
            };
            imageLayer.Children.Add(new ImageDrawing(bitmap, bounds));
            drawing.Children.Add(imageLayer);
            drawing.Freeze();

            var brush = new DrawingBrush(drawing)
            {
                Stretch = Stretch.UniformToFill,
                AlignmentX = AlignmentX.Center,
                AlignmentY = AlignmentY.Center,
                TileMode = TileMode.None,
                ViewboxUnits = BrushMappingMode.Absolute,
                Viewbox = bounds
            };
            brush.Freeze();
            return brush;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException
                                              or ArgumentException or NotSupportedException
                                              or System.Security.SecurityException)
        {
            AppLogService.Warn(
                $"加载背景图失败，已回退为纯色背景：{path}，{exception.GetType().Name}：{exception.Message}");
            return baseBrush;
        }
    }

    /// <summary>
    /// 写入强调色。iNKORE 的 <c>Accent*</c> 画刷是**具体实例**（不跟随 <c>SystemAccentColor</c> 动态变化），
    /// 只改颜色键按钮不会变色，所以按钮背景、正文强调色、软背景这些键必须一起覆盖。
    /// </summary>
    private static void ApplyAccentResources(ResourceDictionary resources, bool dark)
    {
        Color accent = ResolveAccentColor();
        if (_lastAppliedAccent is { } last && last.Accent == accent && last.Dark == dark)
        {
            return;
        }
        _lastAppliedAccent = (accent, dark);
        Color textAccent = dark ? Blend(accent, Colors.White, 0.45) : accent;
        Color buttonBackground = dark ? Blend(accent, Colors.White, 0.30) : accent;
        Color buttonHover = dark ? Blend(accent, Colors.White, 0.42) : Blend(accent, Colors.White, 0.12);
        Color buttonPressed = dark ? Blend(accent, Colors.White, 0.20) : Blend(accent, Colors.Black, 0.12);
        Color soft = dark
            ? Blend(accent, (Color)ColorConverter.ConvertFromString("#181B20")!, 0.80)
            : Blend(accent, Colors.White, 0.90);
        Color light1 = Blend(accent, Colors.White, 0.20);
        Color light2 = Blend(accent, Colors.White, 0.35);
        Color light3 = Blend(accent, Colors.White, 0.50);
        Color dark1 = Blend(accent, Colors.Black, 0.12);
        Color dark2 = Blend(accent, Colors.Black, 0.24);
        Color dark3 = Blend(accent, Colors.Black, 0.36);

        var accentBrush = CreateBrush(textAccent);
        var buttonBrush = CreateBrush(buttonBackground);
        var onAccentBrush = CreateBrush(IsLightColor(buttonBackground) ? Colors.Black : Colors.White);

        resources["Wam.AccentBrush"] = accentBrush;
        resources["Wam.AccentSoftBrush"] = CreateBrush(soft);

        resources["SystemAccentColor"] = accent;
        resources["SystemAccentColorLight1"] = light1;
        resources["SystemAccentColorLight2"] = light2;
        resources["SystemAccentColorLight3"] = light3;
        resources["SystemAccentColorDark1"] = dark1;
        resources["SystemAccentColorDark2"] = dark2;
        resources["SystemAccentColorDark3"] = dark3;
        resources["SystemAccentColorLight1Brush"] = CreateBrush(light1);
        resources["SystemAccentColorLight2Brush"] = CreateBrush(light2);
        resources["SystemAccentColorLight3Brush"] = CreateBrush(light3);
        resources["SystemAccentColorDark1Brush"] = CreateBrush(dark1);
        resources["SystemAccentColorDark2Brush"] = CreateBrush(dark2);
        resources["SystemAccentColorDark3Brush"] = CreateBrush(dark3);

        resources["AccentFillColorDefaultBrush"] = buttonBrush;
        resources["AccentFillColorSecondaryBrush"] = buttonBrush;
        resources["AccentFillColorTertiaryBrush"] = buttonBrush;
        resources["AccentFillColorSelectedTextBackgroundBrush"] = buttonBrush;
        resources["AccentAAFillColorDefaultBrush"] = buttonBrush;
        resources["AccentButtonBackground"] = buttonBrush;
        resources["AccentButtonBackgroundPointerOver"] = CreateBrush(buttonHover);
        resources["AccentButtonBackgroundPressed"] = CreateBrush(buttonPressed);
        resources["AccentButtonForeground"] = onAccentBrush;
        resources["AccentButtonForegroundPointerOver"] = onAccentBrush;
        resources["AccentButtonForegroundPressed"] = onAccentBrush;
        resources["AccentTextFillColorPrimaryBrush"] = accentBrush;
        resources["AccentTextFillColorSecondaryBrush"] = accentBrush;
        resources["AccentTextFillColorTertiaryBrush"] = accentBrush;
        resources["SystemControlBackgroundAccentBrush"] = buttonBrush;
        resources["SystemControlForegroundAccentBrush"] = accentBrush;
        resources["SystemControlHighlightAccentBrush"] = accentBrush;
        resources["SystemControlDisabledAccentBrush"] = buttonBrush;
    }

    /// <summary>当前生效的强调色：用户在“外观”里选的优先，否则是内置默认蓝色。</summary>
    private static Color ResolveAccentColor()
        => Current.AccentColor.Length > 0 && TryParseColor(Current.AccentColor, out Color configured)
            ? configured
            : (Color)ColorConverter.ConvertFromString(DefaultAccentColor)!;

    private const string DefaultAccentColor = "#2563EB";

    /// <summary>按比例把 <paramref name="from"/> 混向 <paramref name="to"/>（0 = 原色，1 = 目标色）。</summary>
    private static Color Blend(Color from, Color to, double amount)
    {
        double ratio = Math.Clamp(amount, 0, 1);
        return Color.FromRgb(
            (byte)Math.Round(from.R + ((to.R - from.R) * ratio)),
            (byte)Math.Round(from.G + ((to.G - from.G) * ratio)),
            (byte)Math.Round(from.B + ((to.B - from.B) * ratio)));
    }

    /// <summary>亮色背景上用深色文字，深色背景上用白字——用户挑浅色强调色时文字才不会糊在一起。</summary>
    private static bool IsLightColor(Color color)
        => (((0.299 * color.R) + (0.587 * color.G) + (0.114 * color.B)) / 255.0) > 0.62;

    private static SolidColorBrush CreateBrush(string color)
        => CreateBrush((Color)ColorConverter.ConvertFromString(color));

    private static SolidColorBrush CreateBrush(Color color)
    {
        var brush = new SolidColorBrush(color);
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

        /// <summary>主题强调色（<c>#RRGGBB</c>），后加字段。</summary>
        public JsonElement AccentColor { get; set; }

        /// <summary>页面背景图绝对路径，后加字段。</summary>
        public JsonElement BackgroundImagePath { get; set; }

        /// <summary>背景图不透明度，后加字段。</summary>
        public JsonElement BackgroundImageOpacity { get; set; }

        /// <summary>窗口系统背景样式（Auto/None/Acrylic/Mica），后加字段。</summary>
        public JsonElement BackdropStyle { get; set; }

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
