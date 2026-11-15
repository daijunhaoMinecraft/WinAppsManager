using System.Collections.Concurrent;
using System.Diagnostics.CodeAnalysis;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

public sealed class ClientCacheStore
{
    /// <summary>Marker used as <see cref="AppItem.DisplayIconSource"/> for icons that only live in memory.</summary>
    public const string MemoryIconPrefix = "memory://icon/";
    private const string BuiltinSourceId = "builtin";
    private const int MaxIconBytes = 5 * 1024 * 1024;
    private static readonly HttpClient DefaultIconClient = CreateDefaultIconClient();

    private static HttpClient CreateDefaultIconClient()
    {
        var client = new HttpClient { Timeout = TimeSpan.FromSeconds(6) };
        // 联想 CDN 对缺少/异常 User-Agent 的请求返回 403；固定客户端标识（实测 WinAppsManager/1.0 可用）。
        // 360 图片 CDN（qhimg.com / mediav.com）对 UA 不敏感，同一个客户端也用于它。
        client.DefaultRequestHeaders.TryAddWithoutValidation("User-Agent", "WinAppsManager/1.0");
        return client;
    }
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = BclCompat.SnakeCaseLower, PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    /// <summary>WPF 原生可解码的图标格式；联想商店的 logo 实际出现 png/jpg/gif/ico/bmp。</summary>
    private static readonly string[] ImageExtensions = [".png", ".ico", ".jpg", ".jpeg", ".gif", ".bmp"];


    private static readonly Regex ManagedFile = new(
        "^[a-f0-9]{64}(?:\\.(?:json|png|ico|jpg|jpeg|gif|bmp)|\\.meta\\.json|\\.[a-f0-9]{32}\\.tmp)$", RegexOptions.CultureInvariant);
    private static readonly Regex CatalogFile = new("^[a-f0-9]{64}\\.json$", RegexOptions.CultureInvariant);
    private static readonly Regex IconFile = new("^[a-f0-9]{64}\\.(?:png|ico|jpg|jpeg|gif|bmp)$", RegexOptions.CultureInvariant);
    private readonly Uri _backend;
    private readonly string _architecture;
    private readonly string _sourceId;
    private readonly string? _legacyCatalogPath;
    private readonly HttpClient _iconClient;
    private readonly SemaphoreSlim _ioGate = new(1, 1);
    private readonly SemaphoreSlim _iconSlots = new(4, 4);
    private readonly ConcurrentDictionary<string, SemaphoreSlim> _iconLocks = new();
    private readonly object _memoryGate = new();
    private readonly Dictionary<string, MemoryIcon> _memoryIcons = new(StringComparer.Ordinal);
    private readonly Queue<string> _memoryEviction = new();
    private long _memoryIconBytes;

    public string DirectoryPath { get; }
    public string CatalogPath { get; }
    public string CatalogDirectory => Path.Combine(DirectoryPath, "catalog");
    public string IconDirectory => Path.Combine(DirectoryPath, "icons");
    public string? LastError { get; private set; }

    /// <summary>Number of icons kept in memory while the disk cache cannot be written.</summary>
    public int MemoryIconLimit { get; set; } = 100;

    /// <summary>Total size, in bytes, allowed for the in-memory icon cache.</summary>
    public long MemoryIconByteLimit { get; set; } = 16L * 1024 * 1024;

    /// <summary>How long a cached icon is served before the backend is asked to confirm it with ETag/Last-Modified.</summary>
    public TimeSpan IconRevalidateInterval { get; set; } = TimeSpan.FromHours(24);

    public int MemoryIconCount
    {
        get { lock (_memoryGate) return _memoryIcons.Count; }
    }

    public ClientCacheStore(string directoryPath, string backendUrl, string architecture, string sourceId = "builtin",
        HttpClient? iconClient = null)
    {
        DirectoryPath = Path.GetFullPath(directoryPath).TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
        _backend = new Uri(backendUrl, UriKind.Absolute);
        _architecture = architecture;
        _sourceId = string.IsNullOrWhiteSpace(sourceId) ? "builtin" : sourceId.Trim();
        _iconClient = iconClient ?? DefaultIconClient;
        // 目录缓存 key 含软件源：同一个后端在“内置精选/联想应用商店”之间切换时不会串用对方的目录缓存。
        CatalogPath = Path.Combine(CatalogDirectory, Key(_backend.AbsoluteUri + "\n" + architecture + "\n" + _sourceId) + ".json");
        // 旧版本（软件源功能之前）的目录缓存文件 key 不含源标识；旧文件没有 Source 字段，按内置精选读取。
        _legacyCatalogPath = string.Equals(_sourceId, BuiltinSourceId, StringComparison.Ordinal)
            ? Path.Combine(CatalogDirectory, Key(_backend.AbsoluteUri + "\n" + architecture) + ".json")
            : null;
    }

    public static bool IsMemoryIconSource(string? source) =>
        source is not null && source.StartsWith(MemoryIconPrefix, StringComparison.Ordinal);

    /// <summary>Resolves an in-memory icon marker (see <see cref="MemoryIconPrefix"/>) back to its bytes.</summary>
    public bool TryGetMemoryIcon(string? source, [NotNullWhen(true)] out byte[]? bytes)
    {
        bytes = null;
        if (!IsMemoryIconSource(source)) return false;
        lock (_memoryGate)
        {
            if (!_memoryIcons.TryGetValue(source![MemoryIconPrefix.Length..], out var icon)) return false;
            bytes = icon.Bytes;
            return true;
        }
    }

    public async Task<CatalogCacheDocument?> ReadCatalogAsync(DateTimeOffset now, CancellationToken ct = default)
    {
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var document = await ReadCatalogFileAsync(CatalogPath, ct).ConfigureAwait(false);
            // 内置精选源兼容旧版本缓存文件（key 不含源标识，文档也没有 Source 字段）。
            if (document is null && _legacyCatalogPath is not null)
            {
                document = await ReadCatalogFileAsync(_legacyCatalogPath, ct).ConfigureAwait(false);
            }
            if (document is null || document.SchemaVersion != 1 || document.BackendUrl != _backend.AbsoluteUri
                || document.Architecture != _architecture || document.Items is null || !document.CanUseOffline(now)) return null;
            // 旧缓存没有 Source 字段，视为内置精选；源不匹配当作没有缓存，避免两个源串用目录。
            string storedSource = string.IsNullOrWhiteSpace(document.Source) ? BuiltinSourceId : document.Source.Trim();
            if (!string.Equals(storedSource, _sourceId, StringComparison.Ordinal)) return null;
            // Even a file created by an older client must never republish its signed download URLs.
            foreach (var app in document.Items)
            {
                if (app?.LatestRelease is not null) app.LatestRelease.DownloadUrl = string.Empty;
            }
            LastError = null;
            return document;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            LastError = "本地目录缓存暂时无法读取，将尝试连接软件服务。";
            return null;
        }
        finally { _ioGate.Release(); }
    }

    private async Task<CatalogCacheDocument?> ReadCatalogFileAsync(string path, CancellationToken ct)
    {
        try
        {
            if (!File.Exists(path)) return null;
            GuardFile(path);
            await using var stream = File.OpenRead(path);
            return await JsonSerializer.DeserializeAsync<CatalogCacheDocument>(stream, JsonOptions, ct).ConfigureAwait(false);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException)
        {
            LastError = "本地目录缓存暂时无法读取，将尝试连接软件服务。";
            return null;
        }
    }

    public async Task<bool> WriteCatalogAsync(IReadOnlyList<AppItem> items, DateTimeOffset updatedAtUtc, CancellationToken ct = default)
    {
        var document = new CatalogCacheDocument
        {
            SchemaVersion = 1, BackendUrl = _backend.AbsoluteUri, Architecture = _architecture,
            Source = _sourceId, UpdatedAtUtc = updatedAtUtc,
            Items = items.Select(CloneForDisk).ToList()
        };
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureDirectory(CatalogDirectory);
            var bytes = JsonSerializer.SerializeToUtf8Bytes(document, JsonOptions);
            await AtomicWriteAsync(CatalogPath, bytes, ct).ConfigureAwait(false);
            // 内置精选源写过新格式缓存后，删除旧格式文件，避免统计里出现两份目录缓存。
            if (_legacyCatalogPath is not null && !string.Equals(_legacyCatalogPath, CatalogPath, StringComparison.Ordinal))
            {
                try
                {
                    if (File.Exists(_legacyCatalogPath))
                    {
                        GuardFile(_legacyCatalogPath);
                        File.Delete(_legacyCatalogPath);
                    }
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                {
                    // 旧缓存删不掉不影响新缓存使用。
                }
            }
            LastError = null;
            return true;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            LastError = "目录已加载，但本地缓存暂时无法保存；本次运行仍可正常使用。";
            return false;
        }
        finally { _ioGate.Release(); }
    }

    public async Task AssignCachedIconsAsync(IEnumerable<AppItem> items, CancellationToken ct = default)
    {
        foreach (var app in items)
        {
            ct.ThrowIfCancellationRequested();
            app.DisplayIconSource = await FindIconAsync(app.IconUrl, ct).ConfigureAwait(false);
        }
    }

    public async Task CacheMissingIconsAsync(IEnumerable<AppItem> items, CancellationToken ct = default)
    {
        await Task.WhenAll(items.Select(async app =>
        {
            if (string.IsNullOrWhiteSpace(app.IconUrl)) return;
            try
            {
                var path = await GetIconAsync(app.IconUrl, ct).ConfigureAwait(false);
                ct.ThrowIfCancellationRequested();
                app.DisplayIconSource = path;
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { }
            catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException or UnauthorizedAccessException or OperationCanceledException)
            {
                app.DisplayIconSource = null;
                LastError = "部分软件图标暂时无法缓存，已使用文字图标。";
            }
        })).ConfigureAwait(false);
    }

    public async Task<string?> FindIconAsync(string? url, CancellationToken ct = default)
    {
        if (!TryIconUri(url, out var uri)) return null;
        var key = Key(_backend.AbsoluteUri + "\n" + uri.AbsoluteUri);
        try
        {
            await _ioGate.WaitAsync(ct).ConfigureAwait(false);
            try
            {
                var cached = FindIconFile(key);
                if (cached is { } found)
                {
                    ForgetMemoryIcon(key);
                    return found.Path;
                }
            }
            finally { _ioGate.Release(); }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // A broken disk cache must not hide an icon that is still available in memory.
            if (!HasMemoryIcon(key)) LastError = "部分本地图标缓存暂时无法读取，已使用文字图标。";
        }
        return HasMemoryIcon(key) ? MemoryIconPrefix + key : null;
    }

    public async Task<string?> GetIconAsync(string url, CancellationToken ct = default)
    {
        if (!TryIconUri(url, out var uri)) return null;
        var key = Key(_backend.AbsoluteUri + "\n" + uri.AbsoluteUri);
        var iconLock = _iconLocks.GetOrAdd(key, _ => new SemaphoreSlim(1, 1));
        await iconLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            // The icon was already downloaded in this process but the disk cache could not be written yet.
            if (TryGetMemoryIconEntry(key, out var remembered))
            {
                var extension = ImageExtension(remembered.Bytes) ?? ".png";
                var persisted = await PersistIconAsync(key, extension, remembered.Bytes, remembered.ETag, remembered.LastModified, ct)
                    .ConfigureAwait(false);
                if (persisted is not null)
                {
                    ForgetMemoryIcon(key);
                    return persisted;
                }
                return MemoryIconPrefix + key;
            }

            IconResource? existing = null;
            try
            {
                await _ioGate.WaitAsync(ct).ConfigureAwait(false);
                try { existing = FindIconFile(key); }
                finally { _ioGate.Release(); }
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                LastError = "部分本地图标缓存暂时无法读取，将重新下载图标。";
            }

            if (existing is { } cachedIcon)
            {
                var metadata = await ReadIconMetadataAsync(key, ct).ConfigureAwait(false);
                if (IsIconMetadataFresh(metadata)) return cachedIcon.Path;
                var revalidated = await RevalidateIconAsync(uri, key, cachedIcon, metadata, ct).ConfigureAwait(false);
                if (revalidated is not null) return revalidated;
                // The backend is unreachable: an existing icon file stays usable, so never drop it.
                LastError = "部分图标暂时无法重新验证，已继续使用本地图标缓存。";
                return cachedIcon.Path;
            }

            var response = await RequestIconAsync(uri, null, ct).ConfigureAwait(false);
            if (response.NotModified || response.Bytes is null) return null;
            var bytes = response.Bytes;
            var detected = ImageExtension(bytes) ?? throw new InvalidDataException("Backend icon is not a supported image format.");
            var path = await PersistIconAsync(key, detected, bytes, response.ETag, response.LastModified, ct).ConfigureAwait(false);
            if (path is not null)
            {
                ForgetMemoryIcon(key);
                return path;
            }
            return RememberIcon(key, bytes, response.ETag, response.LastModified);
        }
        finally { iconLock.Release(); }
    }

    public async Task DiscardIconAsync(string? path, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(path)) return;
        if (IsMemoryIconSource(path))
        {
            ForgetMemoryIcon(path[MemoryIconPrefix.Length..]);
            return;
        }
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            GuardFile(path);
            if (Path.GetDirectoryName(Path.GetFullPath(path)) != IconDirectory) return;
            var name = Path.GetFileName(path);
            if (!ManagedFile.IsMatch(name)) return;
            File.Delete(path);
            var key = name[..64];
            ForgetMemoryIcon(key);
            var metadata = MetadataPath(key);
            if (File.Exists(metadata))
            {
                GuardFile(metadata);
                File.Delete(metadata);
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { LastError = "无法清除损坏的图标缓存。"; }
        finally { _ioGate.Release(); }
    }

    public async Task<IReadOnlyList<string>> ClearAsync(CancellationToken ct = default)
    {
        var errors = new List<string>();
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            ClearMemoryIcons();
            foreach (var directory in new[] { CatalogDirectory, IconDirectory })
            {
                if (!Directory.Exists(directory)) continue;
                try
                {
                    GuardDirectory(directory);
                    foreach (var file in Directory.EnumerateFiles(directory))
                    {
                        if (!ManagedFile.IsMatch(Path.GetFileName(file))) continue;
                        try { GuardFile(file); File.Delete(file); }
                        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                        { errors.Add("部分本地缓存文件暂时无法清理，请稍后重试。"); }
                    }
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                { errors.Add("本地缓存目录暂时无法清理，请检查目录权限。"); }
            }
            LastError = errors.FirstOrDefault();
            return errors.Distinct().ToArray();
        }
        finally { _ioGate.Release(); }
    }

    public ClientCacheStatistics Statistics()
    {
        int catalogs = 0, icons = 0;
        long bytes = 0;
        try
        {
            foreach (var directory in new[] { CatalogDirectory, IconDirectory })
            {
                if (!Directory.Exists(directory)) continue;
                GuardDirectory(directory);
                foreach (var path in Directory.EnumerateFiles(directory))
                {
                    var name = Path.GetFileName(path);
                    if (!ManagedFile.IsMatch(name)) continue;
                    GuardFile(path);
                    if (CatalogFile.IsMatch(name)) catalogs++;
                    if (IconFile.IsMatch(name)) icons++;
                    bytes += new FileInfo(path).Length;
                }
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { LastError = "本地缓存统计暂时无法读取。"; }
        return new ClientCacheStatistics(catalogs, icons, bytes);
    }

    private async Task<string?> RevalidateIconAsync(Uri uri, string key, IconResource existing, IconMetadata? metadata, CancellationToken ct)
    {
        IconResponse response;
        try
        {
            var conditional = HasIconValidator(metadata) ? metadata : null;
            response = await RequestIconAsync(uri, conditional, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested) { throw; }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException or OperationCanceledException)
        {
            return null;
        }

        if (response.NotModified)
        {
            var refreshed = new IconMetadata
            {
                ETag = response.ETag ?? metadata?.ETag,
                LastModified = response.LastModified ?? metadata?.LastModified,
                CheckedAtUtc = DateTimeOffset.UtcNow
            };
            await TryWriteIconMetadataAsync(key, refreshed, ct).ConfigureAwait(false);
            return existing.Path;
        }

        if (response.Bytes is null) return existing.Path;
        var extension = ImageExtension(response.Bytes);
        if (extension is null) return existing.Path;
        var path = await PersistIconAsync(key, extension, response.Bytes, response.ETag, response.LastModified, ct).ConfigureAwait(false);
        if (path is not null)
        {
            ForgetMemoryIcon(key);
            return path;
        }
        return RememberIcon(key, response.Bytes, response.ETag, response.LastModified) ?? existing.Path;
    }

    private async Task<IconResponse> RequestIconAsync(Uri uri, IconMetadata? conditional, CancellationToken ct)
    {
        await _iconSlots.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
            timeout.CancelAfter(TimeSpan.FromSeconds(6));
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            if (!string.IsNullOrWhiteSpace(conditional?.ETag))
            {
                request.Headers.TryAddWithoutValidation("If-None-Match", conditional.ETag);
            }
            else if (conditional?.LastModified is { } modified)
            {
                request.Headers.IfModifiedSince = modified;
            }
            using var response = await _iconClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token)
                .ConfigureAwait(false);
            var etag = response.Headers.ETag?.Tag;
            var lastModified = response.Content.Headers.LastModified;
            if (response.StatusCode == HttpStatusCode.NotModified) return new IconResponse(true, null, etag, lastModified);
            response.EnsureSuccessStatusCode();
            if (response.Content.Headers.ContentLength is > MaxIconBytes) throw new InvalidDataException("Icon too large.");
            await using var source = await response.Content.ReadAsStreamAsync(timeout.Token).ConfigureAwait(false);
            using var destination = new MemoryStream();
            var buffer = new byte[8192];
            while (true)
            {
                var count = await source.ReadAsync(buffer, timeout.Token).ConfigureAwait(false);
                if (count == 0) break;
                if (destination.Length + count > MaxIconBytes) throw new InvalidDataException("Icon too large.");
                destination.Write(buffer, 0, count);
            }
            return new IconResponse(false, destination.ToArray(), etag, lastModified);
        }
        finally { _iconSlots.Release(); }
    }

    private async Task<string?> PersistIconAsync(string key, string extension, byte[] bytes, string? etag,
        DateTimeOffset? lastModified, CancellationToken ct)
    {
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            try
            {
                EnsureDirectory(IconDirectory);
                var path = Path.Combine(IconDirectory, key + extension);
                await AtomicWriteAsync(path, bytes, ct).ConfigureAwait(false);
                // A backend icon that switched format must not leave an older file of the other kind behind.
                foreach (var otherExtension in ImageExtensions)
                {
                    if (string.Equals(otherExtension, extension, StringComparison.OrdinalIgnoreCase)) continue;
                    var other = Path.Combine(IconDirectory, key + otherExtension);
                    if (!File.Exists(other)) continue;
                    try { GuardFile(other); File.Delete(other); }
                    catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
                }
                await TryWriteIconMetadataUnsafeAsync(key, new IconMetadata
                {
                    ETag = etag, LastModified = lastModified, CheckedAtUtc = DateTimeOffset.UtcNow
                }, ct).ConfigureAwait(false);
                return path;
            }
            catch (Exception error) when (error is IOException or UnauthorizedAccessException)
            {
                LastError = "图标已下载，但本地缓存暂时无法保存；本次运行改用内存图标。";
                return null;
            }
        }
        finally { _ioGate.Release(); }
    }

    private async Task TryWriteIconMetadataAsync(string key, IconMetadata metadata, CancellationToken ct)
    {
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try { await TryWriteIconMetadataUnsafeAsync(key, metadata, ct).ConfigureAwait(false); }
        finally { _ioGate.Release(); }
    }

    private async Task TryWriteIconMetadataUnsafeAsync(string key, IconMetadata metadata, CancellationToken ct)
    {
        try
        {
            EnsureDirectory(IconDirectory);
            await AtomicWriteAsync(MetadataPath(key), JsonSerializer.SerializeToUtf8Bytes(metadata, JsonOptions), ct)
                .ConfigureAwait(false);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // Losing the revalidation record only costs an extra conditional request later.
        }
    }

    private async Task<IconMetadata?> ReadIconMetadataAsync(string key, CancellationToken ct)
    {
        await _ioGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            var path = MetadataPath(key);
            if (!File.Exists(path)) return null;
            GuardFile(path);
            await using var stream = File.OpenRead(path);
            var metadata = await JsonSerializer.DeserializeAsync<IconMetadata>(stream, JsonOptions, ct).ConfigureAwait(false);
            return metadata is { Version: 1 } ? metadata : null;
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or JsonException or ArgumentException)
        {
            return null;
        }
        finally { _ioGate.Release(); }
    }

    private bool IsIconMetadataFresh(IconMetadata? metadata)
    {
        if (metadata is null) return false;
        var interval = IconRevalidateInterval < TimeSpan.Zero ? TimeSpan.Zero : IconRevalidateInterval;
        var age = DateTimeOffset.UtcNow - metadata.CheckedAtUtc;
        if (age < TimeSpan.Zero) age = TimeSpan.Zero;
        return age < interval;
    }

    private static bool HasIconValidator(IconMetadata? metadata) =>
        metadata is not null && (!string.IsNullOrWhiteSpace(metadata.ETag) || metadata.LastModified.HasValue);

    private IconResource? FindIconFile(string key)
    {
        foreach (var extension in ImageExtensions)
        {
            var path = Path.Combine(IconDirectory, key + extension);
            if (!File.Exists(path)) continue;
            GuardFile(path);
            var prefix = new byte[24];
            using var stream = File.OpenRead(path);
            var count = stream.Read(prefix);
            if (ImageExtension(prefix.AsSpan(0, count)) == extension) return new IconResource(path, extension);
        }
        return null;
    }

    private string MetadataPath(string key) => Path.Combine(IconDirectory, key + ".meta.json");

    private bool HasMemoryIcon(string key)
    {
        lock (_memoryGate) return _memoryIcons.ContainsKey(key);
    }

    private bool TryGetMemoryIconEntry(string key, [NotNullWhen(true)] out MemoryIcon? icon)
    {
        lock (_memoryGate) return _memoryIcons.TryGetValue(key, out icon);
    }

    private string? RememberIcon(string key, byte[] bytes, string? etag, DateTimeOffset? lastModified)
    {
        lock (_memoryGate)
        {
            if (_memoryIcons.Remove(key, out var previous)) _memoryIconBytes -= previous.Bytes.Length;
            _memoryIcons[key] = new MemoryIcon(bytes, etag, lastModified);
            _memoryIconBytes += bytes.Length;
            _memoryEviction.Enqueue(key);
            TrimMemoryIcons();
            return _memoryIcons.ContainsKey(key) ? MemoryIconPrefix + key : null;
        }
    }

    private void ForgetMemoryIcon(string key)
    {
        lock (_memoryGate)
        {
            if (_memoryIcons.Remove(key, out var previous)) _memoryIconBytes -= previous.Bytes.Length;
        }
    }

    private void TrimMemoryIcons()
    {
        int limit = Math.Max(0, MemoryIconLimit);
        long byteLimit = Math.Max(0, MemoryIconByteLimit);
        while (_memoryIcons.Count > limit || _memoryIconBytes > byteLimit)
        {
            if (!_memoryEviction.TryDequeue(out var oldest)) break;
            if (_memoryIcons.Remove(oldest, out var removed)) _memoryIconBytes -= removed.Bytes.Length;
        }
    }

    private void ClearMemoryIcons()
    {
        lock (_memoryGate)
        {
            _memoryIcons.Clear();
            _memoryEviction.Clear();
            _memoryIconBytes = 0;
        }
    }

    private static AppItem CloneForDisk(AppItem source)
    {
        // 内置源的 LatestRelease 带临时签名下载地址，写盘前必须去掉；联想源目录条目没有 release 时无需复制。
        if (source.LatestRelease is null || string.IsNullOrEmpty(source.LatestRelease.DownloadUrl)) return source;
        var clone = JsonSerializer.Deserialize<AppItem>(JsonSerializer.Serialize(source, JsonOptions), JsonOptions)!;
        if (clone.LatestRelease is not null) clone.LatestRelease.DownloadUrl = string.Empty;
        return clone;
    }

    private bool TryIconUri(string? value, out Uri uri)
    {
        uri = null!;
        if (!Uri.TryCreate(value, UriKind.Absolute, out var parsed)
            || (parsed.Scheme != Uri.UriSchemeHttp && parsed.Scheme != Uri.UriSchemeHttps)
            || parsed.UserInfo.Length != 0) return false;

        // 后端自身提供的图标路径（内置源图标、商店图标兜底）始终允许。
        // 必须先于公网校验，因为后端可能部署在局域网/回环地址上。
        if (parsed.Scheme == _backend.Scheme
            && parsed.Host == _backend.Host
            && parsed.Port == _backend.Port)
        {
            var root = _backend.AbsolutePath.TrimEnd('/');
            bool allowed = parsed.AbsolutePath.StartsWith(root + "/api/v1/apps/", StringComparison.Ordinal)
                           && parsed.AbsolutePath.EndsWith("/icon", StringComparison.Ordinal);
            if (!allowed && parsed.AbsolutePath.StartsWith(root + "/icon/", StringComparison.Ordinal))
            {
                allowed = true;
            }

            // 商店源的 icon_proxy_url / cover / screenshots 也是后端兜底路径。
            if (!allowed && parsed.AbsolutePath.StartsWith(root + "/api/v1/store/", StringComparison.Ordinal))
            {
                allowed = parsed.AbsolutePath.EndsWith("/icon", StringComparison.Ordinal)
                          || parsed.AbsolutePath.EndsWith("/cover", StringComparison.Ordinal)
                          || parsed.AbsolutePath.Contains("/screenshots/", StringComparison.Ordinal);
            }

            if (allowed)
            {
                uri = parsed;
                return true;
            }
        }

        // 图标/CDN 地址不再维护固定域名白名单：后端只会下发可信来源的绝对 URL，
        // 客户端按“安全公网地址”规则校验后直连；未知厂商 CDN（如 qhmsg.com）也可用。
        if (!IsSafePublicHost(parsed.Host)) return false;
        uri = parsed;
        return true;
    }

    /// <summary>
    /// 安全公网主机校验：拒绝 localhost、内网后缀、回环/私网/链路本地/组播/保留 IP。
    /// 不做 DNS 解析（客户端只负责直连后端下发的 CDN 图标），解析后的 IP 仍由系统网络栈决定。
    /// </summary>
    private static bool IsSafePublicHost(string host)
    {
        if (string.IsNullOrWhiteSpace(host)) return false;
        if (host.Equals("localhost", StringComparison.OrdinalIgnoreCase)) return false;

        string[] blockedSuffixes = [".local", ".internal", ".lan", ".home.arpa", ".localdomain", ".corp"];
        foreach (string suffix in blockedSuffixes)
        {
            if (host.EndsWith(suffix, StringComparison.OrdinalIgnoreCase)) return false;
        }

        if (IPAddress.TryParse(host, out var address))
        {
            if (IPAddress.IsLoopback(address)) return false;
            if (address.AddressFamily == AddressFamily.InterNetwork)
            {
                byte[] bytes = address.GetAddressBytes();
                int first = bytes[0];
                int second = bytes[1];
                if (first is 0 or 10 or 127
                    || (first == 169 && second == 254)
                    || (first == 172 && second is >= 16 and <= 31)
                    || (first == 192 && second == 168)
                    || (first == 100 && second is >= 64 and <= 127)
                    || first >= 224)
                {
                    return false;
                }
            }
            else if (address.AddressFamily == AddressFamily.InterNetworkV6)
            {
                if (address.IsIPv6LinkLocal || address.IsIPv6SiteLocal || address.IsIPv6Multicast) return false;
                byte[] bytes = address.GetAddressBytes();
                if ((bytes[0] & 0xfe) == 0xfc) return false; // fc00::/7 ULA
            }

            return true;
        }

        // 单标签主机名（如 printer、intranet）一律拒绝；公网域名至少包含一个点。
        return host.Contains('.');
    }
    private static string? ImageExtension(ReadOnlySpan<byte> bytes)
    {
        if (bytes.Length >= 24 && bytes[..8].SequenceEqual(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 })) return ".png";
        if (bytes.Length >= 6 && bytes[0] == 0 && bytes[1] == 0 && bytes[2] == 1 && bytes[3] == 0 && (bytes[4] != 0 || bytes[5] != 0)) return ".ico";
        if (bytes.Length >= 3 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[2] == 0xFF) return ".jpg";
        if (bytes.Length >= 6 && bytes[0] == 'G' && bytes[1] == 'I' && bytes[2] == 'F' && bytes[3] == '8') return ".gif";
        if (bytes.Length >= 2 && bytes[0] == 'B' && bytes[1] == 'M') return ".bmp";
        return null;
    }

    private async Task AtomicWriteAsync(string path, byte[] bytes, CancellationToken ct)
    {
        GuardFile(path);
        var name = Path.GetFileName(path);
        var temporary = Path.Combine(Path.GetDirectoryName(path)!, name[..64] + "." + Guid.NewGuid().ToString("N") + ".tmp");
        try
        {
            await File.WriteAllBytesAsync(temporary, bytes, ct).ConfigureAwait(false);
            ct.ThrowIfCancellationRequested();
            File.Move(temporary, path, true);
        }
        finally { try { File.Delete(temporary); } catch (IOException) { } catch (UnauthorizedAccessException) { } }
    }

    private void EnsureDirectory(string directory)
    {
        GuardDirectory(directory);
        Directory.CreateDirectory(directory);
        GuardDirectory(directory);
    }

    private void GuardDirectory(string directory)
    {
        var full = Path.GetFullPath(directory);
        if (full != DirectoryPath && !full.StartsWith(DirectoryPath + Path.DirectorySeparatorChar, StringComparison.Ordinal))
            throw new IOException("Cache directory is outside its root.");
        foreach (var path in new[] { DirectoryPath, full })
            if (Directory.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
                throw new IOException("Linked cache directories are not supported.");
    }

    private void GuardFile(string path)
    {
        var full = Path.GetFullPath(path);
        GuardDirectory(Path.GetDirectoryName(full)!);
        if (!ManagedFile.IsMatch(Path.GetFileName(full)) || (File.Exists(full) && (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0))
            throw new IOException("File is not a managed cache file.");
    }

    private static string Key(string value) => Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();

    private readonly record struct IconResource(string Path, string Extension);
    private readonly record struct IconResponse(bool NotModified, byte[]? Bytes, string? ETag, DateTimeOffset? LastModified);
    private sealed record MemoryIcon(byte[] Bytes, string? ETag, DateTimeOffset? LastModified);

    private sealed class IconMetadata
    {
        [JsonPropertyName("version")] public int Version { get; set; } = 1;
        [JsonPropertyName("etag")] public string? ETag { get; set; }
        [JsonPropertyName("last_modified")] public DateTimeOffset? LastModified { get; set; }
        [JsonPropertyName("checked_at_utc")] public DateTimeOffset CheckedAtUtc { get; set; }
    }
}

/// <summary>One frame of an ICO container, as described by the file's directory entries.</summary>
public sealed record IcoIconFrame(int Width, int Height, int BitCount, int ColorCount, int Offset, int Length, bool IsPng);

/// <summary>
/// Framework independent ICO helpers. The WPF decoder cannot always choose a frame reliably, so the converter
/// inspects the directory itself and hands a single decodable payload (PNG frame or wrapped DIB frame) to WPF.
/// </summary>
public static class IconImageFormat
{
    private const int IcoHeaderSize = 6;
    private const int IcoEntrySize = 16;

    public static bool IsPng(ReadOnlySpan<byte> data) =>
        data.Length >= 8 && data[0] == 137 && data[1] == 80 && data[2] == 78 && data[3] == 71
        && data[4] == 13 && data[5] == 10 && data[6] == 26 && data[7] == 10;

    public static bool IsIco(ReadOnlySpan<byte> data) =>
        data.Length >= IcoHeaderSize && data[0] == 0 && data[1] == 0 && data[2] == 1 && data[3] == 0
        && (data[4] != 0 || data[5] != 0);

    /// <summary>Returns all frames of an ICO container, or <see langword="null"/> when the payload is not a usable ICO.</summary>
    public static IReadOnlyList<IcoIconFrame>? ReadIcoFrames(ReadOnlySpan<byte> data)
    {
        if (!IsIco(data)) return null;
        int count = data[4] | (data[5] << 8);
        if (count == 0 || data.Length < IcoHeaderSize + count * IcoEntrySize) return null;
        var frames = new List<IcoIconFrame>(count);
        for (int index = 0; index < count; index++)
        {
            int entry = IcoHeaderSize + index * IcoEntrySize;
            int width = data[entry] == 0 ? 256 : data[entry];
            int height = data[entry + 1] == 0 ? 256 : data[entry + 1];
            int colors = data[entry + 2];
            int bitCount = data[entry + 6] | (data[entry + 7] << 8);
            long length = ReadUInt32(data, entry + 8);
            long offset = ReadUInt32(data, entry + 12);
            if (length <= 0 || offset < IcoHeaderSize + count * IcoEntrySize || offset + length > data.Length) return null;
            var payload = data.Slice((int)offset, (int)length);
            frames.Add(new IcoIconFrame(width, height, bitCount, colors, (int)offset, (int)length, IsPng(payload)));
        }
        return frames;
    }

    /// <summary>Picks the highest quality frame; the UI scales it down and WPF prefers a large source.</summary>
    public static IcoIconFrame? SelectBestIcoFrame(IReadOnlyList<IcoIconFrame>? frames) => frames?
        .OrderByDescending(frame => (long)frame.Width * frame.Height)
        .ThenByDescending(frame => frame.BitCount)
        .ThenByDescending(frame => frame.Width)
        .FirstOrDefault();

    public static byte[]? ExtractIcoFrame(ReadOnlySpan<byte> data, IcoIconFrame frame)
    {
        if (frame.Offset < 0 || frame.Length <= 0 || frame.Offset + frame.Length > data.Length) return null;
        return data.Slice(frame.Offset, frame.Length).ToArray();
    }

    /// <summary>Rebuilds a one-frame ICO around a raw DIB frame so WPF's icon decoder accepts it.</summary>
    public static byte[]? WrapIcoFrame(ReadOnlySpan<byte> data, IcoIconFrame frame)
    {
        var image = ExtractIcoFrame(data, frame);
        if (image is null) return null;
        // Some ICO directories leave the bit count at zero; the bitmap header still carries the real value.
        int bitCount = frame.BitCount != 0 ? frame.BitCount : DibBitCount(data, frame);
        var result = new byte[IcoHeaderSize + IcoEntrySize + image.Length];
        result[2] = 1;
        result[4] = 1;
        result[IcoHeaderSize] = frame.Width >= 256 ? (byte)0 : (byte)frame.Width;
        result[IcoHeaderSize + 1] = frame.Height >= 256 ? (byte)0 : (byte)frame.Height;
        result[IcoHeaderSize + 2] = (byte)Math.Clamp(frame.ColorCount, 0, 255);
        result[IcoHeaderSize + 4] = 1;
        result[IcoHeaderSize + 6] = (byte)(bitCount & 0xFF);
        result[IcoHeaderSize + 7] = (byte)((bitCount >> 8) & 0xFF);
        WriteUInt32(result, IcoHeaderSize + 8, image.Length);
        WriteUInt32(result, IcoHeaderSize + 12, IcoHeaderSize + IcoEntrySize);
        image.CopyTo(result, IcoHeaderSize + IcoEntrySize);
        return result;
    }

    private static int DibBitCount(ReadOnlySpan<byte> data, IcoIconFrame frame)
    {
        // BITMAPINFOHEADER: biSize (4), biWidth (4), biHeight (4), biPlanes (2), biBitCount (2 at +14).
        if (frame.Offset < 0 || frame.Length < 16 || frame.Offset + 16 > data.Length) return 0;
        var header = data.Slice(frame.Offset, 16);
        if (ReadUInt32(header, 0) != 40) return 0;
        return header[14] | (header[15] << 8);
    }

    private static long ReadUInt32(ReadOnlySpan<byte> data, int offset) =>
        data[offset] | ((long)data[offset + 1] << 8) | ((long)data[offset + 2] << 16) | ((long)data[offset + 3] << 24);

    private static void WriteUInt32(byte[] data, int offset, int value)
    {
        data[offset] = (byte)value;
        data[offset + 1] = (byte)(value >> 8);
        data[offset + 2] = (byte)(value >> 16);
        data[offset + 3] = (byte)(value >> 24);
    }
}
