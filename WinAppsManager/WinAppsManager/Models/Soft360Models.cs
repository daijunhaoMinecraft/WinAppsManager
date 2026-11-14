using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace WinAppsManager.Models;

/// <summary>
/// 后端“360 软件管家”软件源的 API 模型（snake_case JSON，接口前缀 <c>/api/v1/store/360</c>）
/// 以及到客户端展示模型 <see cref="AppItem"/> 的映射。
/// <para>
/// 客户端不直接访问 360 接口，只解析本项目后端的响应。目录是各分类热门条目子集（约 6000 款），
/// 因此与联想源一样不预热全部图标：列表行按需调用 <c>AppCatalogService.EnsureIconAsync</c>，
/// 图标优先直连 360 图片 CDN（<c>icon_url</c>，兼容旧契约的 <c>logo_url</c>），
/// 失败回退后端代理路径（<c>icon_proxy_url</c>）。目录条目没有版本号（详情接口才有 version），
/// 下载地址由 <c>/download?architecture=</c> 实时解析且不缓存。
/// </para>
/// </summary>
public static class Soft360CatalogMapper
{
    /// <summary>360 软件管家源的固定 ID（与后端 <c>/api/v1/sources</c> 一致）。</summary>
    public const string SourceId = "soft360";

    /// <summary>360 软件管家显示名。</summary>
    public const string SourceName = "360软件管家";

    /// <summary>把 360 目录条目（summary）映射为客户端 <see cref="AppItem"/>；URL 仍为后端返回的原始形式。</summary>
    public static AppItem ToAppItem(this Soft360AppSummary summary)
    {
        ArgumentNullException.ThrowIfNull(summary);
        string softId = (summary.SoftId ?? string.Empty).Trim();
        // 360 的 name 是完整名称、bname 是简短名称：优先 name，缺失时才用 bname。
        string name = FirstNonEmpty(summary.Name, summary.BName);
        string id = (summary.Id ?? string.Empty).Trim();
        if (id.Length == 0 && softId.Length > 0)
        {
            id = SourceId + "-" + softId;
        }

        if (id.Length == 0 || name.Length == 0)
        {
            throw new JsonException("360软件管家目录条目缺少应用 ID 或名称。");
        }

        if (softId.Length == 0)
        {
            throw new JsonException($"360软件管家目录条目“{name}”缺少 soft_id。");
        }

        string? iconUrl = FirstNonEmpty(summary.IconUrl, summary.LogoUrl);
        return new AppItem
        {
            Id = id,
            Name = name,
            Description = NormalizeText(summary.Description),
            Category = string.IsNullOrWhiteSpace(summary.Category) ? "未分类" : summary.Category.Trim(),
            // 目录列表没有版本号：留空，打开详情后由详情响应的 version 填充。
            Version = NormalizeText(summary.Version),
            SizeBytes = summary.SizeBytes,
            SizeDisplay = FormatSize(summary.SizeBytes),
            Publisher = FirstNonEmpty(summary.Publisher, summary.Corp),
            Platforms = BuildPlatforms(summary.Platforms, summary.MinimumWindows),
            MinimumWindows = string.IsNullOrWhiteSpace(summary.MinimumWindows) ? null : summary.MinimumWindows.Trim(),
            Architectures = CleanList(summary.Architectures),
            License = string.IsNullOrWhiteSpace(summary.License) ? "免费" : summary.License.Trim(),
            IconUrl = iconUrl.Length == 0 ? null : iconUrl,
            IconFallbackUrl = string.IsNullOrWhiteSpace(summary.IconProxyUrl) ? null : summary.IconProxyUrl.Trim(),
            IconText = FirstTextElement(name),
            InstallationNotes = NormalizeText(summary.InstallationNotes),
            Tags = CleanList(summary.Tags),
            SortOrder = summary.SortOrder,
            SourceId = SourceId,
            SourceAppId = softId,
            SourceName = SourceName,
            Score = summary.Score,
            Features = Array.Empty<string>(),
            Screenshots = Array.Empty<AppScreenshot>(),
            Labels = Array.Empty<string>(),
            DetailTags = Array.Empty<AppDetailTag>()
        };
    }

    /// <summary>把 360 详情映射为客户端 <see cref="AppItem"/>（summary 字段 + 详情字段）；URL 仍为后端原始形式。</summary>
    public static AppItem ToAppItem(this Soft360AppDetails details)
    {
        ArgumentNullException.ThrowIfNull(details);
        var app = ((Soft360AppSummary)details).ToAppItem();
        string description = NormalizeText(details.Description);
        app.LongDescription = FirstNonEmpty(details.LongDescription, description, app.Description);
        app.Publisher = FirstNonEmpty(details.Publisher, details.Corp, app.Publisher);
        string version = NormalizeText(details.Version);
        if (version.Length > 0)
        {
            app.Version = version;
        }

        app.ReleaseNotes = NormalizeText(details.ReleaseNotes);
        app.CoverUrl = string.IsNullOrWhiteSpace(details.CoverUrl) ? null : details.CoverUrl.Trim();
        app.Labels = CleanList(details.Labels);
        app.DetailTags = MapDetailTags(details.DetailTags);
        app.Screenshots = MapScreenshots(details.Screenshots);
        return app;
    }

    private static IReadOnlyList<AppScreenshot> MapScreenshots(List<Soft360Screenshot>? screenshots)
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

            // 360 源目前只有截图、没有视频；仍按后端 kind 字段兼容将来的视频缩略图。
            string kind = string.Equals(item.Kind, "video", StringComparison.OrdinalIgnoreCase) ? "video" : "image";
            result.Add(new AppScreenshot
            {
                Index = item.Index,
                Kind = kind,
                Url = item.Url.Trim()
            });
        }

        return result.AsReadOnly();
    }

    private static IReadOnlyList<AppDetailTag> MapDetailTags(List<Soft360DetailTag>? tags)
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

    /// <summary>把字节数格式化成目录卡片显示的安装包大小；缺失时与内置源一致显示“以官网为准”。</summary>
    private static string FormatSize(long? bytes)
    {
        if (bytes is not > 0)
        {
            return "以官网为准";
        }

        double value = bytes.Value;
        return value switch
        {
            >= 1024d * 1024 * 1024 => $"{value / (1024d * 1024 * 1024):0.##} GB",
            >= 1024d * 1024 => $"{value / (1024d * 1024):0.#} MB",
            >= 1024 => $"{value / 1024d:0.#} KB",
            _ => $"{bytes.Value} B"
        };
    }

    /// <summary>后端未给出展示用 platforms 文案时，按 minimum_windows 生成一段人类可读的系统要求。</summary>
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
}

/// <summary><c>GET /api/v1/store/360/apps</c>（后端缓存目录）的响应。</summary>
public sealed class Soft360CatalogResponse
{
    public string? Source { get; set; }
    public string? SourceName { get; set; }
    public DateTimeOffset? UpdatedAt { get; set; }
    public bool Stale { get; set; }
    public int? Total { get; set; }
    public List<Soft360AppSummary>? Items { get; set; }
}

/// <summary>
/// <c>GET /api/v1/store/360/search?q=...&amp;limit=...</c> 的响应（360 在线搜索，
/// 覆盖服务端已逆向的全量目录，因此可能返回缓存目录之外的软件）。
/// 字段与后端 <c>Soft360SearchResponse</c> 一致：<c>source</c>/<c>query</c>/<c>total</c>/<c>items</c>，
/// 其中 <c>total</c> 等于 <c>items</c> 数量（服务端已按 limit 截断）。
/// </summary>
public sealed class Soft360SearchResponse
{
    public string? Source { get; set; }
    public string? Query { get; set; }
    public int? Total { get; set; }
    public List<Soft360AppSummary>? Items { get; set; }
}

/// <summary>360 软件管家应用摘要（目录条目 / 在线搜索结果条目）。</summary>
public class Soft360AppSummary
{
    /// <summary>客户端使用的应用 ID，约定为 <c>soft360-{soft_id}</c>；缺失时客户端按 soft_id 拼出。</summary>
    public string? Id { get; set; }

    /// <summary>360 原始软件 ID，详情/下载/图标请求都使用它。</summary>
    public string? SoftId { get; set; }

    public string? Name { get; set; }

    /// <summary>简短名称；name 缺失时作为显示名回退。后端 JSON 字段名是 <c>bname</c>（单个词）。</summary>
    [JsonPropertyName("bname")]
    public string? BName { get; set; }

    public string? Description { get; set; }
    public string? Category { get; set; }
    public string? CategoryId { get; set; }

    /// <summary>安装包字节数（360 的 soft_size）。</summary>
    public long? SizeBytes { get; set; }

    /// <summary>评分（0–100）。</summary>
    public int? Score { get; set; }

    /// <summary>360 图片 CDN 的图标绝对 https 地址（后端字段名 <c>icon_url</c>），客户端直连并本地缓存。</summary>
    public string? IconUrl { get; set; }

    /// <summary>兼容旧契约的 <c>logo_url</c> 字段；<see cref="IconUrl"/> 缺失时作为图标地址回退。</summary>
    [JsonPropertyName("logo_url")]
    public string? LogoUrl { get; set; }

    /// <summary>后端图标代理路径（相对 BaseUrl），CDN 直连失败时的兜底。</summary>
    public string? IconProxyUrl { get; set; }

    /// <summary>可提供的安装包架构（x64/x86，可能同时包含两者）。</summary>
    public List<string>? Architectures { get; set; }

    /// <summary>最低 Windows 版本（7/8/8.1/10/11），客户端据此隐藏不兼容条目。</summary>
    public string? MinimumWindows { get; set; }

    public string? Version { get; set; }
    public string? Publisher { get; set; }

    /// <summary>厂商名称；部分响应使用 corp 字段。</summary>
    public string? Corp { get; set; }

    /// <summary>展示用系统要求文案；缺失时客户端按 minimum_windows 生成。</summary>
    public string? Platforms { get; set; }

    /// <summary>详情标签（目录条目里通常是分类名）。</summary>
    public List<string>? Tags { get; set; }

    /// <summary>许可文案，360 源默认“免费”。</summary>
    public string? License { get; set; }

    /// <summary>是否免费；当前客户端只用许可文案展示。</summary>
    public bool IsFree { get; set; } = true;

    /// <summary>源内排序值（目录中的热门顺序）。</summary>
    public int SortOrder { get; set; }

    /// <summary>安装说明文案。</summary>
    public string? InstallationNotes { get; set; }
}

/// <summary><c>GET /api/v1/store/360/apps/{soft_id}</c> 的详情响应（summary 字段 + 详情字段）。</summary>
public sealed class Soft360AppDetails : Soft360AppSummary
{
    /// <summary>完整介绍；后端没有单独字段时由 description 填充。</summary>
    public string? LongDescription { get; set; }

    /// <summary>更新说明；360 源通常为空。</summary>
    public string? ReleaseNotes { get; set; }

    /// <summary>截图列表，<c>url</c> 是后端图片代理的相对路径，由 <c>AppCatalogService</c> 解析成绝对地址。</summary>
    public List<Soft360Screenshot>? Screenshots { get; set; }

    /// <summary>横版封面；360 源当前为 null。</summary>
    public string? CoverUrl { get; set; }

    public List<string>? Labels { get; set; }
    public List<Soft360DetailTag>? DetailTags { get; set; }
}

/// <summary>360 详情截图（<c>kind</c> 为 <c>image</c>，兼容将来的 <c>video</c>）。</summary>
public sealed class Soft360Screenshot
{
    public int Index { get; set; }
    public string? Url { get; set; }
    public string? Kind { get; set; }
}

/// <summary>360 详情徽章：名称 + 提示文案。</summary>
public sealed class Soft360DetailTag
{
    public string? Name { get; set; }
    public string? Tips { get; set; }
}
