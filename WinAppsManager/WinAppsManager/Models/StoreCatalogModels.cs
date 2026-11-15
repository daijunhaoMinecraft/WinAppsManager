using System.Text.Json;

namespace WinAppsManager.Models;

/// <summary>
/// 后端“联想应用商店”软件源的 API 模型（snake_case JSON，见 <c>docs/store-source-design.md</c> 3.4）
/// 以及到客户端展示模型 <see cref="AppItem"/> / <see cref="AppRelease"/> 的映射。
/// 客户端不直接访问联想接口，只解析本项目后端 <c>/api/v1/store/lenovo/*</c> 的响应。
/// </summary>
public static class StoreCatalogMapper
{
    /// <summary>联想应用商店源的固定 ID（与后端 <c>/sources</c> 一致）。</summary>
    public const string LenovoSourceId = "lenovo";

    /// <summary>内置精选源的固定 ID。</summary>
    public const string BuiltinSourceId = "builtin";

    /// <summary>联想应用商店显示名。</summary>
    public const string LenovoSourceName = "联想应用商店";

    /// <summary>内置精选显示名。</summary>
    public const string BuiltinSourceName = "内置精选";

    /// <summary>把联想目录条目（summary）映射为客户端 <see cref="AppItem"/>；URL 仍为后端返回的原始形式。</summary>
    public static AppItem ToAppItem(this StoreAppSummary summary, string? sourceName = null)
    {
        ArgumentNullException.ThrowIfNull(summary);
        string id = (summary.Id ?? string.Empty).Trim();
        string softId = (summary.SoftId ?? string.Empty).Trim();
        string name = (summary.Name ?? string.Empty).Trim();
        if (id.Length == 0 || name.Length == 0)
        {
            throw new JsonException("联想商店目录条目缺少应用 ID 或名称。");
        }

        if (softId.Length == 0)
        {
            throw new JsonException($"联想商店目录条目“{name}”缺少 soft_id。");
        }

        var app = new AppItem
        {
            Id = id,
            Name = name,
            Description = NormalizeText(summary.Description),
            Category = string.IsNullOrWhiteSpace(summary.Category) ? "未分类" : summary.Category.Trim(),
            Version = string.IsNullOrWhiteSpace(summary.Version) ? "以官网为准" : summary.Version.Trim(),
            SizeDisplay = string.IsNullOrWhiteSpace(summary.SizeDisplay) ? "以官网为准" : summary.SizeDisplay.Trim(),
            Publisher = NormalizeText(summary.Publisher),
            Platforms = string.IsNullOrWhiteSpace(summary.Platforms) ? "Windows 10 / 11" : summary.Platforms.Trim(),
            MinimumWindows = string.IsNullOrWhiteSpace(summary.MinimumWindows) ? null : summary.MinimumWindows.Trim(),
            Architectures = CleanList(summary.Architectures),
            License = string.IsNullOrWhiteSpace(summary.License) ? "免费" : summary.License.Trim(),
            IconUrl = summary.IconUrl,
            IconFallbackUrl = string.IsNullOrWhiteSpace(summary.IconProxyUrl) ? null : summary.IconProxyUrl.Trim(),
            IconText = NormalizeText(summary.IconText),
            InstallationNotes = NormalizeText(summary.InstallationNotes),
            Tags = CleanList(summary.Tags),
            IsRecommended = summary.IsRecommended,
            SortOrder = summary.SortOrder,
            SourceId = LenovoSourceId,
            SourceAppId = softId,
            SourceName = string.IsNullOrWhiteSpace(sourceName) ? LenovoSourceName : sourceName.Trim(),
            SizeBytes = summary.SizeBytes,
            DownloadCount = string.IsNullOrWhiteSpace(summary.DownloadCount) ? null : summary.DownloadCount.Trim(),
            Score = summary.Score,
            IsBeta = summary.IsBeta,
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

    /// <summary>把联想详情映射为客户端 <see cref="AppItem"/>（summary 字段 + 详情字段）；URL 仍为后端原始形式。</summary>
    public static AppItem ToAppItem(this StoreAppDetails details, string? sourceName = null)
    {
        ArgumentNullException.ThrowIfNull(details);
        var app = ((StoreAppSummary)details).ToAppItem(sourceName);
        app.LongDescription = NormalizeText(details.LongDescription);
        app.ReleaseNotes = NormalizeText(details.ReleaseNotes);
        app.RankingDescription = string.IsNullOrWhiteSpace(details.Ranking) ? null : details.Ranking.Trim();
        app.CoverUrl = string.IsNullOrWhiteSpace(details.CoverUrl) ? null : details.CoverUrl.Trim();
        app.CoverProxyUrl = string.IsNullOrWhiteSpace(details.CoverProxyUrl) ? null : details.CoverProxyUrl.Trim();
        app.Labels = CleanList(details.Labels);
        app.DetailTags = MapDetailTags(details.DetailTags);
        app.Screenshots = MapScreenshots(details.Screenshots);
        return app;
    }

    /// <summary>把联想下载地址响应映射为 <see cref="AppRelease"/>；字段缺失时返回 <see langword="null"/>。</summary>
    public static AppRelease? ToAppRelease(this StoreRelease release)
    {
        if (release is null || string.IsNullOrWhiteSpace(release.DownloadUrl))
        {
            return null;
        }

        return new AppRelease
        {
            Id = release.Id ?? string.Empty,
            Version = release.Version ?? string.Empty,
            Platform = release.Platform ?? "windows",
            Architecture = release.Architecture ?? string.Empty,
            DownloadUrl = release.DownloadUrl.Trim(),
            FileName = release.FileName ?? string.Empty,
            SizeBytes = release.SizeBytes,
            Sha256 = string.IsNullOrWhiteSpace(release.Sha256) ? null : release.Sha256.Trim()
        };
    }

    private static IReadOnlyList<AppScreenshot> MapScreenshots(List<StoreScreenshot>? screenshots)
    {
        if (screenshots is null || screenshots.Count == 0)
        {
            return Array.Empty<AppScreenshot>();
        }

        var result = new List<AppScreenshot>(screenshots.Count);
        foreach (var item in screenshots)
        {
            if (item is null || string.IsNullOrWhiteSpace(item.Url))
            {
                continue;
            }

            string kind = string.Equals(item.Kind, "video", StringComparison.OrdinalIgnoreCase) ? "video" : "image";
            result.Add(new AppScreenshot
            {
                Index = item.Index,
                Kind = kind,
                Url = item.Url.Trim(),
                // proxy_url 是后端截图代理路径（直连 CDN 失败时兜底）；旧服务端没有该字段时为 null。
                ProxyUrl = string.IsNullOrWhiteSpace(item.ProxyUrl) ? null : item.ProxyUrl.Trim()
            });
        }

        return result.AsReadOnly();
    }

    private static IReadOnlyList<AppDetailTag> MapDetailTags(List<StoreDetailTag>? tags)
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

        return result.AsReadOnly();
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

    private static string NormalizeText(string? value) => value?.Trim() ?? string.Empty;
}

/// <summary><c>GET /api/v1/store/lenovo/apps</c>（完整目录）的响应。</summary>
public sealed class StoreCatalogResponse
{
    public string? Source { get; set; }
    public string? SourceName { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }
    public bool Stale { get; set; }
    public int? Total { get; set; }
    public List<StoreAppSummary>? Items { get; set; }
}

/// <summary>联想商店应用摘要（目录/搜索结果条目）。</summary>
public class StoreAppSummary
{
    public string? Id { get; set; }
    public string? SoftId { get; set; }
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

    /// <summary>后端图标代理路径（相对 BaseUrl），联想 CDN 直连失败时的兜底。</summary>
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
}

/// <summary><c>GET /api/v1/store/lenovo/apps/{soft_id}</c> 的详情响应（summary 字段 + 详情字段）。</summary>
public sealed class StoreAppDetails : StoreAppSummary
{
    public string? LongDescription { get; set; }
    public string? ReleaseNotes { get; set; }
    public List<StoreScreenshot>? Screenshots { get; set; }

    /// <summary>横版封面地址：优先是联想 CDN 绝对 https 地址，无法直连时后端回退相对代理路径。</summary>
    public string? CoverUrl { get; set; }

    /// <summary>封面后端代理路径（相对 BaseUrl，<c>/api/v1/store/lenovo/apps/{soft_id}/cover</c>），直连 CDN 失败时兜底。</summary>
    public string? CoverProxyUrl { get; set; }

    public List<string>? Labels { get; set; }
    public string? Ranking { get; set; }
    public List<StoreDetailTag>? DetailTags { get; set; }
}

/// <summary>详情截图：<c>kind</c> 为 <c>image</c> 或 <c>video</c>（视频只提供缩略图地址）。</summary>
public sealed class StoreScreenshot
{
    public int Index { get; set; }
    public string? Kind { get; set; }

    /// <summary>图片地址：优先是联想 CDN 绝对 https 地址，无法直连时后端回退相对代理路径。</summary>
    public string? Url { get; set; }

    /// <summary>后端截图代理路径（相对 BaseUrl），直连 CDN 失败时的兜底；旧服务端没有该字段。</summary>
    public string? ProxyUrl { get; set; }
}

/// <summary>详情徽章：名称 + 提示文案。</summary>
public sealed class StoreDetailTag
{
    public string? Name { get; set; }
    public string? Tips { get; set; }
}

/// <summary><c>GET /api/v1/store/lenovo/search?q=...</c> 的响应。</summary>
public sealed class StoreSearchResponse
{
    public string? Source { get; set; }
    public string? Query { get; set; }
    public int? Total { get; set; }
    public List<StoreAppSummary>? Items { get; set; }
}

/// <summary>
/// <c>GET /api/v1/store/lenovo/apps/{soft_id}/download?architecture=...</c> 的响应（与内置源 <c>Release</c> 同构）。
/// </summary>
public sealed class StoreRelease
{
    public string? Id { get; set; }
    public string? Version { get; set; }
    public string? Platform { get; set; }
    public string? Architecture { get; set; }
    public string? DownloadUrl { get; set; }
    public string? FileName { get; set; }
    public long? SizeBytes { get; set; }
    public string? Sha256 { get; set; }
}

/// <summary>
/// <c>GET /api/v1/sources</c> 返回的单个软件源（<c>GET /api/v1/sources</c> 响应项）。
/// 服务端可能新增客户端不认识的源：客户端应按 <see cref="Id"/> 识别，未知 ID 不崩溃、只提示。
/// </summary>
public sealed class StoreSourceInfo
{
    public string? Id { get; set; }
    public string? Name { get; set; }
    public string? Description { get; set; }
    public bool Enabled { get; set; }
    public int? AppCount { get; set; }
    public bool IsDefault { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }

    /// <summary>
    /// 服务端是否提供“在线搜索”（JSON 字段 <c>online_search</c>）。
    /// 旧服务端没有该字段时为 <see langword="null"/>，客户端回退到按源判断（联想/360 = true、内置精选 = false）。
    /// 新服务端同时下发 <see cref="SearchMode"/> 时以搜索模式为准，该字段只作为旧服务端兼容回退。
    /// </summary>
    public bool? OnlineSearch { get; set; }

    /// <summary>
    /// 服务端搜索模式（JSON 字段 <c>search_mode</c>）：<c>catalog</c> 表示目录完整、客户端本地搜索即可；
    /// <c>api</c> 表示目录只是热门子集，搜索必须调用服务端搜索 API。
    /// 旧服务端没有该字段时为 <see langword="null"/>，客户端回退到按源判断（联想/内置精选 = catalog、360 = api）。
    /// </summary>
    public string? SearchMode { get; set; }
}

/// <summary><c>GET /api/v1/sources</c> 的响应。</summary>
public sealed class StoreSourcesResponse
{
    public List<StoreSourceInfo>? Items { get; set; }
    public int? Total { get; set; }
}
