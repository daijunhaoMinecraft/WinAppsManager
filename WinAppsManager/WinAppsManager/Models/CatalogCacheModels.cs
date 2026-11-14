namespace WinAppsManager.Models;

public enum CatalogCacheSource { None, Network, Memory, Disk, Offline }

/// <summary>
/// 目录缓存状态。<see cref="SourceName"/> 为当前软件源显示名（联想应用商店 / 内置精选），供状态行展示。
/// </summary>
public sealed record CatalogCacheState(CatalogCacheSource Source, DateTimeOffset? UpdatedAtUtc,
    string? Warning = null, string? NetworkError = null, string? SourceName = null);

/// <summary>本地缓存统计。<see cref="MediaFiles"/> 为联想商店截图/封面图片缓存文件数。</summary>
public sealed record ClientCacheStatistics(int CatalogFiles, int IconFiles, long Bytes, int MediaFiles = 0);

public sealed record CacheClearResult(bool LocalCleared, bool BackendCleared, IReadOnlyList<string> Errors);

public sealed class CatalogCacheDocument
{
    public int SchemaVersion { get; set; }
    public string BackendUrl { get; set; } = string.Empty;
    public string Architecture { get; set; } = string.Empty;

    /// <summary>目录所属软件源（<c>builtin</c> / <c>lenovo</c>）；旧缓存没有该字段时视为 <c>builtin</c>。</summary>
    public string? Source { get; set; }

    public DateTimeOffset UpdatedAtUtc { get; set; }
    public List<AppItem>? Items { get; set; }

    public bool IsFresh(DateTimeOffset now) => now >= UpdatedAtUtc && now - UpdatedAtUtc < TimeSpan.FromMinutes(5);
    public bool CanUseOffline(DateTimeOffset now) => now >= UpdatedAtUtc && now - UpdatedAtUtc <= TimeSpan.FromHours(24);
}
