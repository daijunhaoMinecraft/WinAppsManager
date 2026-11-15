namespace WinAppsManager.Models;

public enum CatalogCacheSource { None, Network, Memory, Disk, Offline }

/// <summary>
/// 目录缓存状态。<see cref="SourceName"/> 为当前软件源显示名（由服务端 <c>/sources</c> 下发），供状态行展示。
/// </summary>
public sealed record CatalogCacheState(CatalogCacheSource Source, DateTimeOffset? UpdatedAtUtc,
    string? Warning = null, string? NetworkError = null, string? SourceName = null);

/// <summary>本地缓存统计。<see cref="MediaFiles"/> 为详情截图/封面图片缓存文件数。</summary>
public sealed record ClientCacheStatistics(int CatalogFiles, int IconFiles, long Bytes, int MediaFiles = 0);

public sealed record CacheClearResult(bool LocalCleared, bool BackendCleared, IReadOnlyList<string> Errors);

public sealed class CatalogCacheDocument
{
    /// <summary>缓存格式版本（2 = 统一软件源接口）；版本不匹配的缓存不会被读取。</summary>
    public int SchemaVersion { get; set; }
    public string BackendUrl { get; set; } = string.Empty;
    public string Architecture { get; set; } = string.Empty;

    /// <summary>目录所属软件源的 <c>source_id</c>（来自服务端 <c>/sources</c>）；与请求来源不一致的缓存不会被使用。</summary>
    public string? Source { get; set; }

    public DateTimeOffset UpdatedAtUtc { get; set; }
    public List<AppItem>? Items { get; set; }

    public bool IsFresh(DateTimeOffset now) => now >= UpdatedAtUtc && now - UpdatedAtUtc < TimeSpan.FromMinutes(5);
    public bool CanUseOffline(DateTimeOffset now) => now >= UpdatedAtUtc && now - UpdatedAtUtc <= TimeSpan.FromHours(24);
}
