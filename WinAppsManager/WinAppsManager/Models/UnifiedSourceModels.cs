using System.Globalization;
using System.Text.Json;

namespace WinAppsManager.Models;

/// <summary>
/// 服务端统一软件源 API（<c>docs/source-api.md</c>）的 DTO 与到客户端展示模型的映射。
/// <para>
/// 客户端不包含任何软件源 ID、名称、接口路径或字段分支：来源列表、默认来源、名称、搜索模式与能力
/// 全部来自 <c>GET /api/v1/sources</c>，其余数据来自 <c>/api/v1/sources/{source_id}/...</c>。
/// 新增来源只需服务端增加适配器，客户端自动显示、切换、搜索、详情与下载。
/// </para>
/// </summary>
public static class SourceCatalogMapper
{
    /// <summary>把统一目录/搜索条目映射为客户端 <see cref="AppItem"/>；URL 仍为服务端返回的原始形式。</summary>
    public static AppItem ToAppItem(this SourceAppSummary summary, string sourceId, string sourceName)
    {
        ArgumentNullException.ThrowIfNull(summary);
        string id = (summary.Id ?? string.Empty).Trim();
        string name = (summary.Name ?? string.Empty).Trim();
        if (id.Length == 0 || name.Length == 0)
        {
            throw new JsonException("软件目录条目缺少应用 ID 或名称。");
        }

        var app = new AppItem
        {
            Id = id,
            Name = name,
            Description = NormalizeText(summary.Description),
            Category = string.IsNullOrWhiteSpace(summary.Category) ? "未分类" : summary.Category.Trim(),
            CategoryId = NormalizeText(summary.CategoryId),
            Version = string.IsNullOrWhiteSpace(summary.Version) ? "以官网为准" : summary.Version.Trim(),
            SizeDisplay = ResolveSizeDisplay(summary.SizeDisplay, summary.SizeBytes),
            SizeBytes = summary.SizeBytes,
            Publisher = NormalizeText(summary.Publisher),
            Platforms = BuildPlatforms(summary.Platforms, summary.MinimumWindows),
            MinimumWindows = string.IsNullOrWhiteSpace(summary.MinimumWindows) ? null : summary.MinimumWindows.Trim(),
            Architectures = CleanList(summary.Architectures),
            License = string.IsNullOrWhiteSpace(summary.License) ? "免费" : summary.License.Trim(),
            IconUrl = NormalizeOptional(summary.IconUrl),
            IconFallbackUrl = NormalizeOptional(summary.IconProxyUrl),
            IconText = FirstNonEmpty(summary.IconText, FirstTextElement(name)),
            InstallationNotes = NormalizeText(summary.InstallationNotes),
            Tags = CleanList(summary.Tags),
            IsRecommended = summary.IsRecommended,
            IsBeta = summary.IsBeta,
            SortOrder = summary.SortOrder,
            DownloadCount = NormalizeOptional(summary.DownloadCount),
            Score = summary.Score,
            WebsiteUrl = NormalizeOptional(summary.WebsiteUrl) ?? string.Empty,
            DownloadPageUrl = NormalizeOptional(summary.DownloadPageUrl) ?? string.Empty,
            SourceId = sourceId,
            SourceName = sourceName,
            Features = Array.Empty<string>(),
            Screenshots = Array.Empty<AppScreenshot>(),
            Labels = Array.Empty<string>(),
            DetailTags = Array.Empty<AppDetailTag>()
        };

        if (!string.IsNullOrWhiteSpace(summary.IconBackground))
        {
            app.IconBackground = summary.IconBackground.Trim();
        }

        return app;
    }

    /// <summary>把统一详情映射为客户端 <see cref="AppItem"/>（目录字段 + 详情字段）；URL 仍为服务端原始形式。</summary>
    public static AppItem ToAppItem(this SourceAppDetails details, string sourceId, string sourceName)
    {
        ArgumentNullException.ThrowIfNull(details);
        var app = ((SourceAppSummary)details).ToAppItem(sourceId, sourceName);
        app.LongDescription = FirstNonEmpty(details.LongDescription, details.Description, app.Description);
        app.ReleaseNotes = NormalizeText(details.ReleaseNotes);
        app.RankingDescription = NormalizeOptional(details.Ranking);
        app.CoverUrl = NormalizeOptional(details.CoverUrl);
        app.CoverProxyUrl = NormalizeOptional(details.CoverProxyUrl);
        app.Labels = CleanList(details.Labels);
        app.DetailTags = MapDetailTags(details.DetailTags);
        app.Screenshots = MapScreenshots(details.Screenshots);
        return app;
    }

    /// <summary>把统一详情里的截图列表映射为客户端 <see cref="AppScreenshot"/>（图片 URL 与代理兜底都保留）。</summary>
    private static IReadOnlyList<AppScreenshot> MapScreenshots(List<SourceScreenshot>? screenshots)
    {
        if (screenshots is null || screenshots.Count == 0)
        {
            return Array.Empty<AppScreenshot>();
        }

        var result = new List<AppScreenshot>(screenshots.Count);
        foreach (var item in screenshots)
        {
            if (item is null)
            {
                continue;
            }

            string url = NormalizeText(item.Url);
            string? proxyUrl = NormalizeOptional(item.ProxyUrl);
            if (url.Length == 0 && proxyUrl is null)
            {
                // 既没有直连地址也没有代理兜底的条目无法显示。
                continue;
            }

            string kind = string.Equals(item.Kind, "video", StringComparison.OrdinalIgnoreCase) ? "video" : "image";
            result.Add(new AppScreenshot
            {
                Index = item.Index,
                Kind = kind,
                Url = url,
                ProxyUrl = proxyUrl,
                // 只有视频条目才播放视频；服务端返回的播放代理地址由客户端原样使用，不构造来源专属路径。
                VideoProxyUrl = kind == "video" ? NormalizeOptional(item.VideoProxyUrl) : null
            });
        }

        return result.Count == 0 ? Array.Empty<AppScreenshot>() : result.AsReadOnly();
    }

    private static IReadOnlyList<AppDetailTag> MapDetailTags(List<SourceDetailTag>? tags)
    {
        if (tags is null || tags.Count == 0)
        {
            return Array.Empty<AppDetailTag>();
        }

        var result = new List<AppDetailTag>(tags.Count);
        foreach (var item in tags)
        {
            if (item is null || string.IsNullOrWhiteSpace(item.Name))
            {
                continue;
            }

            result.Add(new AppDetailTag
            {
                Name = item.Name.Trim(),
                Tips = NormalizeText(item.Tips)
            });
        }

        return result.Count == 0 ? Array.Empty<AppDetailTag>() : result.AsReadOnly();
    }

    private static IReadOnlyList<string> CleanList(List<string>? values)
    {
        if (values is null || values.Count == 0)
        {
            return Array.Empty<string>();
        }

        var result = new List<string>(values.Count);
        foreach (var value in values)
        {
            if (!string.IsNullOrWhiteSpace(value))
            {
                result.Add(value.Trim());
            }
        }

        return result.Count == 0 ? Array.Empty<string>() : result.AsReadOnly();
    }

    /// <summary>服务端没有提供展示用大小时，按 <c>size_bytes</c> 生成；两者都没有时显示“以官网为准”。</summary>
    private static string ResolveSizeDisplay(string? sizeDisplay, long? sizeBytes)
    {
        if (!string.IsNullOrWhiteSpace(sizeDisplay))
        {
            return sizeDisplay.Trim();
        }

        if (sizeBytes is not > 0)
        {
            return "以官网为准";
        }

        double value = sizeBytes.Value;
        return value switch
        {
            >= 1024d * 1024 * 1024 => $"{value / (1024d * 1024 * 1024):0.##} GB",
            >= 1024d * 1024 => $"{value / (1024d * 1024):0.#} MB",
            >= 1024 => $"{value / 1024d:0.#} KB",
            _ => $"{sizeBytes.Value} B"
        };
    }

    /// <summary>服务端未给出展示用 platforms 文案时，按 minimum_windows 生成一段人类可读的系统要求。</summary>
    private static string BuildPlatforms(string? platforms, string? minimumWindows)
    {
        if (!string.IsNullOrWhiteSpace(platforms))
        {
            return platforms.Trim();
        }

        return (minimumWindows ?? string.Empty).Trim() switch
        {
            "7" => "Windows 7 / 8 / 8.1 / 10 / 11",
            "8" => "Windows 8 / 8.1 / 10 / 11",
            "8.1" => "Windows 8.1 / 10 / 11",
            "10" => "Windows 10 / 11",
            "11" => "Windows 11",
            _ => "Windows 10 / 11"
        };
    }

    /// <summary>图标缺失时的文字徽章：取名称的第一个文本元素（避免把代理对/组合字符截断成乱码）。</summary>
    private static string FirstTextElement(string value) =>
        value.Length == 0 ? string.Empty : StringInfo.GetNextTextElement(value);

    private static string FirstNonEmpty(params string?[] values)
    {
        foreach (var value in values)
        {
            if (!string.IsNullOrWhiteSpace(value))
            {
                return value.Trim();
            }
        }

        return string.Empty;
    }

    private static string NormalizeText(string? value) => value?.Trim() ?? string.Empty;

    private static string? NormalizeOptional(string? value) =>
        string.IsNullOrWhiteSpace(value) ? null : value.Trim();
}

/// <summary>
/// 软件源能力（<c>capabilities</c>）：缺省能力按契约假定 <c>catalog</c>/<c>details</c>/<c>download</c> 可用，
/// 其余为 <see langword="false"/>。客户端据此决定是否请求可选接口，不再按来源 ID 判断。
/// </summary>
public sealed class SourceCapabilities
{
    public bool Categories { get; set; }
    public bool Catalog { get; set; }
    public bool Search { get; set; }
    public bool Details { get; set; }
    public bool Download { get; set; }
    public bool Screenshots { get; set; }
    public bool Cover { get; set; }
    public bool Video { get; set; }
    public bool Refresh { get; set; }

    /// <summary>服务端没有下发 <c>capabilities</c> 时使用的能力集合（契约规定 catalog/details/download 可假定可用）。</summary>
    public static SourceCapabilities Default { get; } = new()
    {
        Catalog = true,
        Details = true,
        Download = true
    };
}

/// <summary><c>GET /api/v1/sources</c> 返回的单个软件源（来源 ID / 名称 / 默认源 / 搜索模式 / 能力）。</summary>
public sealed class SourceInfo
{
    public string? Id { get; set; }
    public string? Name { get; set; }
    public string? Description { get; set; }
    public bool Enabled { get; set; }
    public int? AppCount { get; set; }
    public bool IsDefault { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }

    /// <summary>服务端搜索模式：<c>catalog</c>（目录完整，客户端本地搜索）或 <c>api</c>（调用服务端搜索）。</summary>
    public string? SearchMode { get; set; }

    /// <summary>旧服务端兼容字段（新客户端以 <see cref="SearchMode"/> 为准，不按来源判断）。</summary>
    public bool? OnlineSearch { get; set; }

    /// <summary>来源能力；缺失时为 <see langword="null"/>，读取方应使用 <see cref="CapabilitiesOrDefault"/>。</summary>
    public SourceCapabilities? Capabilities { get; set; }

    public SourceCapabilities CapabilitiesOrDefault => Capabilities ?? SourceCapabilities.Default;
}

/// <summary><c>GET /api/v1/sources</c> 的响应。</summary>
public sealed class SourcesResponse
{
    public List<SourceInfo>? Items { get; set; }
    public int? Total { get; set; }
}

/// <summary>来源分类（<c>GET /api/v1/sources/{id}/categories</c>）。</summary>
public sealed class SourceCategory
{
    public string? Id { get; set; }
    public string? Name { get; set; }
    public int? Count { get; set; }
    public string? Group { get; set; }
}

/// <summary><c>GET /api/v1/sources/{id}/categories</c> 的响应。</summary>
public sealed class SourceCategoriesResponse
{
    public string? Source { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }
    public List<SourceCategory>? Categories { get; set; }
}

/// <summary>统一应用字段（目录/搜索条目；详情在此基础上增加更多字段）。</summary>
public class SourceAppSummary
{
    public string? Id { get; set; }
    public string? Name { get; set; }
    public string? Description { get; set; }
    public string? Category { get; set; }
    public string? CategoryId { get; set; }
    public string? Group { get; set; }
    public string? Version { get; set; }
    public string? SizeDisplay { get; set; }
    public long? SizeBytes { get; set; }
    public string? Publisher { get; set; }
    public string? Platforms { get; set; }
    public List<string>? Architectures { get; set; }
    public string? MinimumWindows { get; set; }
    public string? IconUrl { get; set; }
    public string? IconProxyUrl { get; set; }
    public string? IconText { get; set; }
    public string? IconBackground { get; set; }
    public List<string>? Tags { get; set; }
    public int? Score { get; set; }
    public string? DownloadCount { get; set; }
    public string? License { get; set; }
    public bool? IsFree { get; set; }
    public bool IsRecommended { get; set; }
    public bool IsBeta { get; set; }
    public int SortOrder { get; set; }
    public string? InstallationNotes { get; set; }

    // 契约核心字段之外的可选展示字段：服务端不返回时留空，列表/详情界面自动隐藏对应入口。
    public string? WebsiteUrl { get; set; }
    public string? DownloadPageUrl { get; set; }
}

/// <summary><c>GET /api/v1/sources/{id}/apps/{app_id}</c> 的详情响应（目录字段 + 详情字段）。</summary>
public sealed class SourceAppDetails : SourceAppSummary
{
    public string? LongDescription { get; set; }
    public string? ReleaseNotes { get; set; }
    public List<SourceScreenshot>? Screenshots { get; set; }
    public string? CoverUrl { get; set; }
    public string? CoverProxyUrl { get; set; }
    public List<string>? Labels { get; set; }
    public string? Ranking { get; set; }
    public List<SourceDetailTag>? DetailTags { get; set; }
}

/// <summary>统一详情截图：<c>kind</c> 为 <c>image</c> 或 <c>video</c>；视频条目带 <c>video_proxy_url</c>。</summary>
public sealed class SourceScreenshot
{
    public int Index { get; set; }
    public string? Kind { get; set; }
    public string? Url { get; set; }
    public string? ProxyUrl { get; set; }

    /// <summary>视频播放代理地址（<c>video_proxy_url</c>）：客户端直接交给 MediaElement 播放，不构造来源专属路径。</summary>
    public string? VideoProxyUrl { get; set; }
}

/// <summary>统一详情徽章：名称 + 提示文案。</summary>
public sealed class SourceDetailTag
{
    public string? Name { get; set; }
    public string? Tips { get; set; }
}

/// <summary><c>GET /api/v1/sources/{id}/apps</c>（完整目录）的响应。</summary>
public class SourceCatalogResponse
{
    public string? Source { get; set; }
    public string? SourceName { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }
    public bool Stale { get; set; }
    public int? Total { get; set; }
    public List<SourceAppSummary>? Items { get; set; }
}

/// <summary><c>GET /api/v1/sources/{id}/search?q=&amp;limit=</c> 的响应（目录字段 + query）。</summary>
public sealed class SourceSearchResponse : SourceCatalogResponse
{
    public string? Query { get; set; }
}
