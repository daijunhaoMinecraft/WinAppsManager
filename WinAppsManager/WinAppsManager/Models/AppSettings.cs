namespace WinAppsManager.Models;

public enum ThemePreference
{
    System,
    Light,
    Dark
}

public enum ProxyMode
{
    System,
    Direct,
    Custom
}

/// <summary>
/// 窗口系统背景（导航栏/标题栏的透明效果）的用户选择。实际可用性由系统版本与高对比度决定，
/// 判断逻辑见 <c>Services.BackdropSupport</c>：不支持的样式会自动回退到可用的样式。
/// </summary>
public enum BackdropStyle
{
    /// <summary>按系统能力自动选择：Windows 11 22H2 起用云母，其余支持的系统用亚克力，都不支持则无。</summary>
    Auto,

    /// <summary>不使用系统背景，窗口保持不透明底色。</summary>
    None,

    /// <summary>亚克力：Windows 10 1803+ 的旧式亚克力，Windows 11 22H2+ 使用 DWM 原生亚克力。</summary>
    Acrylic,

    /// <summary>云母：仅 Windows 11 22H2（内部版本 22523）及以上。</summary>
    Mica
}

public sealed class AppSettings
{
    /// <summary>默认后端地址；客户端所有目录/详情/图标/下载解析请求都以它为根。</summary>
    public const string DefaultBackendBaseUrl = "https://winapps.theconsole.top/";

    public ThemePreference Theme { get; set; } = ThemePreference.System;

    /// <summary>
    /// 窗口系统背景样式：默认不使用（无），可改为亚克力 / 云母 / 自动。
    /// 系统不支持所选样式时按 <c>Services.BackdropSupport.Resolve</c> 的规则回退。
    /// </summary>
    public BackdropStyle BackdropStyle { get; set; } = BackdropStyle.None;

    /// <summary>
    /// 主题强调色（<c>#RRGGBB</c>）。空字符串表示使用内置的默认蓝色；
    /// 只影响强调色，不改变浅色/深色模式。
    /// </summary>
    public string AccentColor { get; set; } = string.Empty;

    /// <summary>
    /// 页面背景图的绝对路径；空字符串表示纯色背景。图片按“铺满窗口”缩放，
    /// 叠在主题底色之上，因此卡片和文字仍然清晰。
    /// </summary>
    public string BackgroundImagePath { get; set; } = string.Empty;

    /// <summary>背景图不透明度（0.05–1）。只有设置了背景图时生效，默认值足够看清壁纸又不抢内容。</summary>
    public double BackgroundImageOpacity { get; set; } = DefaultBackgroundImageOpacity;

    /// <summary>背景图不透明度的默认值与取值范围。</summary>
    public const double DefaultBackgroundImageOpacity = 0.35;
    public const double MinimumBackgroundImageOpacity = 0.05;
    public const double MaximumBackgroundImageOpacity = 1.0;
    public int DownloadConnections { get; set; } = InstallerDownloadOptions.DefaultConnections;
    public int DownloadTimeoutSeconds { get; set; } = InstallerDownloadOptions.DefaultTimeoutSeconds;
    public string GitHubMirror { get; set; } = string.Empty;
    public ProxyMode ProxyMode { get; set; } = ProxyMode.System;
    public string CustomProxyUrl { get; set; } = string.Empty;
    public bool IgnoreCertificateErrors { get; set; }

    /// <summary>后端服务地址（http/https 绝对地址，始终以 / 结尾）；旧 settings.json 缺字段时使用默认地址。</summary>
    public string BackendBaseUrl { get; set; } = DefaultBackendBaseUrl;

    /// <summary>
    /// 安装包下载目录；空字符串表示默认目录 <c>%LOCALAPPDATA%\WinAppsManager\Downloads</c>。
    /// 旧 settings.json 没有该字段时按默认目录迁移。
    /// </summary>
    public string DownloadDirectory { get; set; } = string.Empty;

    /// <summary>
    /// 当前软件源的 <c>source_id</c>（来自服务端 <c>GET /api/v1/sources</c>，客户端不硬编码任何来源 ID）。
    /// 空字符串表示尚未选择：运行时采用服务端下发 <c>is_default=true</c> 的可用来源。
    /// 旧 settings.json 的 <c>Lenovo</c>/<c>BuiltIn</c>/<c>Soft360</c> 只在读取时做一次性迁移映射。
    /// </summary>
    public string SourceId { get; set; } = string.Empty;

    /// <summary>
    /// 是否把运行日志写入 <c>%LOCALAPPDATA%\WinAppsManager\Logs</c>；默认开启。
    /// 关闭后只保留调试器输出，不生成日志文件。旧 settings.json 缺字段时按默认开启迁移。
    /// </summary>
    public bool EnableFileLogging { get; set; } = true;

    /// <summary>
    /// 是否记录 Verbose 级别的详细日志；默认关闭，开启后日志文件增长更快。
    /// 只有 <see cref="EnableFileLogging"/> 开启时才写入文件。旧 settings.json 缺字段时按关闭迁移。
    /// </summary>
    public bool VerboseLogging { get; set; }
}
