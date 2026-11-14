using System.ComponentModel;
using System.Text.Json.Serialization;

namespace WinAppsManager.Models;

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

    /// <summary>图标兜底地址（联想源的后端代理路径）；直连 <see cref="IconUrl"/> 失败时使用。</summary>
    public string? IconFallbackUrl { get; set; }

    public string IconText { get; set; } = string.Empty;
    public string IconBackground { get; set; } = "#0067C0";
    public string InstallationNotes { get; set; } = string.Empty;
    public IReadOnlyList<string> Features { get; set; } = Array.Empty<string>();
    public IReadOnlyList<string> Tags { get; set; } = Array.Empty<string>();
    public bool IsRecommended { get; set; }
    public int SortOrder { get; set; }
    public AppRelease? LatestRelease { get; set; }

    // ---- 联想应用商店（lenovo）源补充字段：全部可选，内置精选源保持空值/默认值，不影响旧缓存与旧接口。 ----

    /// <summary>软件源标识：<c>lenovo</c> 或 <c>builtin</c>；内置精选旧缓存缺省为空。</summary>
    public string? SourceId { get; set; }

    /// <summary>源内原始应用 ID（联想的 <c>soft_id</c>，区分大小写），用于详情/下载/图标请求。</summary>
    public string? SourceAppId { get; set; }

    /// <summary>源显示名，例如“联想应用商店”。</summary>
    public string? SourceName { get; set; }

    /// <summary>安装包大小（字节）。联想的 <c>installFileSize</c> 解析值，仅用于展示。</summary>
    public long? SizeBytes { get; set; }

    /// <summary>下载量文案，例如“1.01亿次”。</summary>
    public string? DownloadCount { get; set; }

    /// <summary>评分（0–100，联想商店为整数分），为空表示源不提供。</summary>
    public int? Score { get; set; }

    /// <summary>是否为 Beta 版本（联想源 <c>stable=beta</c>），内置源恒为 false。</summary>
    public bool IsBeta { get; set; }

    /// <summary>榜单文案，例如“聊天社交类下载榜第2名”。</summary>
    public string? RankingDescription { get; set; }

    /// <summary>更新说明（联想的 <c>whatNew</c>），为空表示源不提供。</summary>
    public string? ReleaseNotes { get; set; }

    /// <summary>横版封面图地址（已按后端 BaseUrl 解析为绝对地址），可能为空。</summary>
    public string? CoverUrl { get; set; }

    /// <summary>截图/视频缩略图列表，内置精选源为空。</summary>
    public IReadOnlyList<AppScreenshot> Screenshots { get; set; } = Array.Empty<AppScreenshot>();

    /// <summary>详情标签（联想 <c>appLabelList</c>），内置精选源为空。</summary>
    public IReadOnlyList<string> Labels { get; set; } = Array.Empty<string>();

    /// <summary>详情徽章（联想 <c>detailsTag</c>，带提示文案），内置精选源为空。</summary>
    public IReadOnlyList<AppDetailTag> DetailTags { get; set; } = Array.Empty<AppDetailTag>();
}

/// <summary>联想应用商店的一张截图：<see cref="Kind"/> 为 <c>image</c> 或 <c>video</c>（视频只提供缩略图）。</summary>
public sealed class AppScreenshot
{
    public int Index { get; set; }

    /// <summary><c>image</c> 或 <c>video</c>。</summary>
    public string Kind { get; set; } = "image";

    /// <summary>图片地址（已按后端 BaseUrl 解析为绝对地址）。</summary>
    public string Url { get; set; } = string.Empty;

    /// <summary>派生属性：是否为视频缩略图（不参与磁盘缓存序列化，反序列化时自动重算）。</summary>
    public bool IsVideo => string.Equals(Kind, "video", StringComparison.OrdinalIgnoreCase);
}

/// <summary>联想应用商店详情的详情徽章，例如“安全认证”及其说明。</summary>
public sealed class AppDetailTag
{
    public string Name { get; set; } = string.Empty;
    public string Tips { get; set; } = string.Empty;
}
