using System.ComponentModel;
using System.Text.Json.Serialization;

namespace WinAppsManager.Models;

/// <summary>
/// 客户端展示用的软件条目。所有字段都来自服务端统一软件源 API
/// （<c>/api/v1/sources/{source_id}/...</c>，见 <c>docs/source-api.md</c>），
/// 客户端不包含任何来源 ID、名称或接口路径判断。
/// </summary>
public class AppItem : INotifyPropertyChanged
{
    private string? _displayIconSource;
    public event PropertyChangedEventHandler? PropertyChanged;

    [JsonIgnore]
    public string? DisplayIconSource
    {
        get => _displayIconSource;
        set
        {
            if (_displayIconSource == value) return;
            _displayIconSource = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(DisplayIconSource)));
        }
    }

    public string Id { get; set; } = string.Empty;
    public string Name { get; set; } = string.Empty;
    public string Description { get; set; } = string.Empty;
    public string LongDescription { get; set; } = string.Empty;
    public string Category { get; set; } = string.Empty;

    /// <summary>服务端分类 ID（与 <c>/categories</c> 的 <c>id</c> 对应）；用于按分类精确筛选，可能为空。</summary>
    public string CategoryId { get; set; } = string.Empty;

    public string Publisher { get; set; } = string.Empty;
    public string Version { get; set; } = "以官网为准";
    public string SizeDisplay { get; set; } = "以官网为准";
    public string License { get; set; } = "免费";
    public string Platforms { get; set; } = "Windows 10 / 11";
    /// <summary>后端核实的最低 Windows 版本（7/8/8.1/10/11），为空表示未声明。</summary>
    public string? MinimumWindows { get; set; }
    /// <summary>后端声明可提供的安装包架构（x64/x86/arm64），为空表示未声明。</summary>
    public IReadOnlyList<string> Architectures { get; set; } = Array.Empty<string>();
    public string WebsiteUrl { get; set; } = string.Empty;
    public string DownloadPageUrl { get; set; } = string.Empty;
    public string? IconUrl { get; set; }

    /// <summary>图标兜底地址（服务端返回的 <c>icon_proxy_url</c>，通常为后端代理路径）；直连 <see cref="IconUrl"/> 失败时使用。</summary>
    public string? IconFallbackUrl { get; set; }

    public string IconText { get; set; } = string.Empty;
    public string IconBackground { get; set; } = "#0067C0";
    public string InstallationNotes { get; set; } = string.Empty;
    public IReadOnlyList<string> Features { get; set; } = Array.Empty<string>();
    public IReadOnlyList<string> Tags { get; set; } = Array.Empty<string>();
    public bool IsRecommended { get; set; }
    public int SortOrder { get; set; }
    public AppRelease? LatestRelease { get; set; }

    // ---- 可选展示字段：服务端不返回时保持空值/默认值，界面自动隐藏对应区域。 ----

    /// <summary>软件源 ID（服务端 <c>/sources</c> 的 <c>id</c>）；客户端只用它区分来源，不按具体值分支。</summary>
    public string? SourceId { get; set; }

    /// <summary>软件源显示名（服务端 <c>/sources</c> 的 <c>name</c>），用于界面上标注来源。</summary>
    public string? SourceName { get; set; }

    /// <summary>安装包大小（字节），仅用于展示与下载进度预期。</summary>
    public long? SizeBytes { get; set; }

    /// <summary>下载量文案，例如“1.01亿次”。</summary>
    public string? DownloadCount { get; set; }

    /// <summary>评分（0–100），为空表示源不提供。</summary>
    public int? Score { get; set; }

    /// <summary>是否为 Beta 版本。</summary>
    public bool IsBeta { get; set; }

    /// <summary>榜单文案，例如“聊天社交类下载榜第2名”。</summary>
    public string? RankingDescription { get; set; }

    /// <summary>更新说明，为空表示源不提供。</summary>
    public string? ReleaseNotes { get; set; }

    /// <summary>横版封面图地址，可能为空。</summary>
    public string? CoverUrl { get; set; }

    /// <summary>封面兜底地址（服务端返回的 <c>cover_proxy_url</c>）；直连 <see cref="CoverUrl"/> 失败时回退。</summary>
    public string? CoverProxyUrl { get; set; }

    /// <summary>截图/视频缩略图列表，为空表示源不提供详情图片。</summary>
    public IReadOnlyList<AppScreenshot> Screenshots { get; set; } = Array.Empty<AppScreenshot>();

    /// <summary>详情标签，为空表示源不提供。</summary>
    public IReadOnlyList<string> Labels { get; set; } = Array.Empty<string>();

    /// <summary>详情徽章（带提示文案），为空表示源不提供。</summary>
    public IReadOnlyList<AppDetailTag> DetailTags { get; set; } = Array.Empty<AppDetailTag>();
}

/// <summary>详情页的一张截图：<see cref="Kind"/> 为 <c>image</c> 或 <c>video</c>（视频只提供缩略图）。</summary>
public sealed class AppScreenshot
{
    public int Index { get; set; }

    /// <summary><c>image</c> 或 <c>video</c>。</summary>
    public string Kind { get; set; } = "image";

    /// <summary>图片地址（服务端返回的直连地址，可能为空而只有代理兜底）。</summary>
    public string Url { get; set; } = string.Empty;

    /// <summary>截图兜底地址（服务端返回的 <c>proxy_url</c>）；直连 <see cref="Url"/> 失败时由 <c>StoreMediaService</c> 回退。</summary>
    public string? ProxyUrl { get; set; }

    /// <summary>视频播放代理地址（服务端详情返回的 <c>video_proxy_url</c>，仅视频条目有值）；客户端直接播放，不构造来源专属路径。</summary>
    public string? VideoProxyUrl { get; set; }

    /// <summary>派生属性：是否为视频缩略图（不参与磁盘缓存序列化，反序列化时自动重算）。</summary>
    public bool IsVideo => string.Equals(Kind, "video", StringComparison.OrdinalIgnoreCase);
}

/// <summary>详情徽章，例如“安全认证”及其说明。</summary>
public sealed class AppDetailTag
{
    public string Name { get; set; } = string.Empty;
    public string Tips { get; set; } = string.Empty;
}
