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

/// <summary>软件目录来源。默认使用联想应用商店，另一个选项是项目内置的精选目录。</summary>
public enum AppSourcePreference
{
    /// <summary>联想应用商店（后端代理，目录约 5000 款，默认）。</summary>
    Lenovo,

    /// <summary>项目内置的精选软件目录（原有的 45 款软件）。</summary>
    BuiltIn
}

public sealed class AppSettings
{
    /// <summary>默认后端地址；客户端所有目录/详情/图标/下载解析请求都以它为根。</summary>
    public const string DefaultBackendBaseUrl = "http://127.0.0.1:8080/";

    public ThemePreference Theme { get; set; } = ThemePreference.System;
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

    /// <summary>当前软件源；旧的 settings.json 没有该字段时按默认值 <see cref="AppSourcePreference.Lenovo"/> 迁移。</summary>
    public AppSourcePreference AppSource { get; set; } = AppSourcePreference.Lenovo;

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
