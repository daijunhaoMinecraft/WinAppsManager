using System.Collections.Concurrent;
using System.Diagnostics.CodeAnalysis;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Media;
using System.Windows.Media.Imaging;

namespace WinAppsManager.Services;

/// <summary>
/// 把后端代理的联想商店图片（截图、视频缩略图、封面）加载为可冻结的 <see cref="ImageSource"/>。
/// <para>
/// 线程模型：解码在后台线程完成并 <see cref="Freezable.Freeze"/> 后返回，UI 线程只做绑定。
/// 缓存分两层：内存 LRU（按**解码后的像素字节**计，默认 64 张 / 32 MB）与磁盘缓存
/// <c>%LOCALAPPDATA%\WinAppsManager\Cache\store-media</c>（文件名 = sha256(URL)，默认上限 256 MB）。
/// 联想商店截图是 1280×720 / 1920×1080，一张解码后约 3.5–8 MB，因此内存预算必须按解码后大小记账，
/// 否则缓存几十张就会占用数百 MB 原生内存；相册缩略图建议用
/// <see cref="GetImageAsync(string?, int, CancellationToken)"/> 指定解码宽度。
/// 所有失败（网络、格式、权限）都静默返回 <see langword="null"/>，由 UI 显示占位图。
/// </para>
/// </summary>
public static class StoreMediaService
{
    /// <summary>内存缓存最多保留的图片数量。</summary>
    public const int MemoryImageLimit = 64;

    /// <summary>内存缓存的上限，按解码后的像素字节（宽 × 高 × 4）计算，默认 32 MB。</summary>
    public const long MemoryByteLimit = 32L * 1024 * 1024;

    /// <summary>磁盘缓存的字节上限（256 MB）。</summary>
    public const long DiskByteLimit = 256L * 1024 * 1024;

    private const int MaxImageBytes = 12 * 1024 * 1024;
    private const string TemporaryExtension = ".tmp";
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(12);
    private static readonly string[] ImageExtensions = [".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico"];

    // 后端地址可在设置中修改，因此这里每次取当前值，不再用静态字段固化。
    private static Uri Backend => AppCatalogService.BackendUri;
    private static string BackendRoot => AppCatalogService.BackendUri.AbsolutePath.TrimEnd('/');
    private static readonly HttpClient Client = CreateHttpClient();
    private static readonly Regex ManagedFile = new(
        "^[a-f0-9]{64}\\.(?:png|jpg|jpeg|gif|bmp|ico)$", RegexOptions.CultureInvariant);
    private static readonly Regex TemporaryFile = new(
        "^[a-f0-9]{64}\\.[a-f0-9]{32}\\.tmp$", RegexOptions.CultureInvariant);

    private static readonly object MemoryGate = new();
    private static readonly Dictionary<string, MemoryEntry> Memory = new(StringComparer.Ordinal);
    private static readonly LinkedList<string> MemoryOrder = new();
    private static readonly SemaphoreSlim DiskGate = new(1, 1);
    private static readonly ConcurrentDictionary<string, SemaphoreSlim> RequestLocks = new(StringComparer.Ordinal);
    private static readonly string CacheRoot = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinAppsManager", "Cache");
    private static readonly string DirectoryPathValue = Path.Combine(CacheRoot, "store-media");
    private static long _memoryBytes;

    public static string DirectoryPath => DirectoryPathValue;

    /// <summary>最近一次磁盘缓存失败的说明；成功时为 <see langword="null"/>。</summary>
    public static string? LastError { get; private set; }

    /// <summary>当前内存缓存中的图片数量。</summary>
    public static int MemoryImageCount
    {
        get { lock (MemoryGate) return Memory.Count; }
    }

    /// <summary>
    /// 加载一张后端图片为可跨线程使用的 <see cref="ImageSource"/>；任何失败都返回 <see langword="null"/>。
    /// 只接受当前后端 <c>/api/v1/store/</c> 下的图片地址，避免把任意 URL 当成图片去请求。
    /// </summary>
    public static Task<ImageSource?> GetImageAsync(string? url, CancellationToken ct = default) =>
        GetImageAsync(url, 0, ct);

    /// <summary>
    /// 加载一张后端图片，并按 <paramref name="decodePixelWidth"/> 缩窄解码宽度（0 = 原始尺寸）。
    /// 相册缩略图（显示约 236×148）建议传 480，解码后内存从约 3.5 MB 降到约 0.5 MB；
    /// 大图预览传 0 或目标窗口宽度。不同解码宽度的结果在内存缓存中分开保存。
    /// </summary>
    public static async Task<ImageSource?> GetImageAsync(string? url, int decodePixelWidth, CancellationToken ct = default)
    {
        if (!TryMediaUri(url, out var uri, out var key)) return null;

        int width = decodePixelWidth < 0 ? 0 : Math.Min(decodePixelWidth, 4096);
        string memoryKey = MemoryKey(key, width);
        if (TryGetMemory(memoryKey, out var cached)) return cached;

        var diskPath = FindDiskFile(key);
        if (diskPath is not null)
        {
            var fromDisk = await LoadFromDiskAsync(memoryKey, diskPath, width, ct).ConfigureAwait(false);
            if (fromDisk is not null) return fromDisk;
        }

        // 同一张图（同一解码宽度）并发请求（例如列表滚回顶部）只下载一次。
        var requestLock = RequestLocks.GetOrAdd(memoryKey, static _ => new SemaphoreSlim(1, 1));
        await requestLock.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (TryGetMemory(memoryKey, out cached)) return cached;

            diskPath = FindDiskFile(key);
            if (diskPath is not null)
            {
                var fromDisk = await LoadFromDiskAsync(memoryKey, diskPath, width, ct).ConfigureAwait(false);
                if (fromDisk is not null) return fromDisk;
            }

            var bytes = await DownloadAsync(uri, ct).ConfigureAwait(false);
            if (bytes is null || bytes.Length == 0) return null;

            ImageSource image;
            try
            {
                image = await Task.Run(() => DecodeImage(bytes, width), ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                return null;
            }

            // 磁盘缓存始终保存原始字节，不同解码宽度共用同一份文件。
            string? extension = ImageExtension(bytes);
            if (extension is not null)
            {
                await TryPersistAsync(key + extension, bytes, ct).ConfigureAwait(false);
            }

            return Remember(memoryKey, image);
        }
        finally
        {
            requestLock.Release();
        }
    }

    private static string MemoryKey(string urlKey, int decodePixelWidth) =>
        decodePixelWidth == 0 ? urlKey : urlKey + "@" + decodePixelWidth.ToString(CultureInfo.InvariantCulture);

    /// <summary>清空内存图片缓存（磁盘缓存仍保留）。</summary>
    public static void ClearMemoryCache()
    {
        lock (MemoryGate)
        {
            Memory.Clear();
            MemoryOrder.Clear();
            _memoryBytes = 0;
        }
    }

    /// <summary>清空内存与磁盘图片缓存，返回无法清理的说明（成功时为空）。</summary>
    public static async Task<IReadOnlyList<string>> ClearCacheAsync(CancellationToken ct = default)
    {
        ClearMemoryCache();
        var errors = new List<string>();
        await DiskGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            GuardDirectory();
            if (!Directory.Exists(DirectoryPathValue)) return errors;
            foreach (var path in Directory.EnumerateFiles(DirectoryPathValue))
            {
                var name = Path.GetFileName(path);
                if (!ManagedFile.IsMatch(name) && !TemporaryFile.IsMatch(name)) continue;
                try
                {
                    GuardFile(path);
                    File.Delete(path);
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                {
                    errors.Add("部分截图缓存文件暂时无法清理，请稍后重试。");
                }
            }

            LastError = errors.Count > 0 ? errors[0] : null;
            return errors.Distinct().ToArray();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            errors.Add("截图缓存目录暂时无法清理，请检查目录权限。");
            LastError = errors[0];
            return errors;
        }
        finally
        {
            DiskGate.Release();
        }
    }

    /// <summary>统计磁盘缓存文件数与占用字节数（损坏文件不计入）。</summary>
    public static StoreMediaCacheStatistics CacheStatistics()
    {
        int files = 0;
        long bytes = 0;
        try
        {
            GuardDirectory();
            if (Directory.Exists(DirectoryPathValue))
            {
                foreach (var path in Directory.EnumerateFiles(DirectoryPathValue))
                {
                    if (!ManagedFile.IsMatch(Path.GetFileName(path))) continue;
                    GuardFile(path);
                    files++;
                    bytes += new FileInfo(path).Length;
                }
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            LastError = "截图缓存统计暂时无法读取。";
        }

        return new StoreMediaCacheStatistics(files, bytes);
    }

    private static async Task<ImageSource?> LoadFromDiskAsync(string memoryKey, string path, int decodePixelWidth,
        CancellationToken ct)
    {
        try
        {
            var bytes = await File.ReadAllBytesAsync(path, ct).ConfigureAwait(false);
            if (bytes.Length == 0 || bytes.Length > MaxImageBytes)
            {
                TryDelete(path);
                return null;
            }

            var image = await Task.Run(() => DecodeImage(bytes, decodePixelWidth), ct).ConfigureAwait(false);
            return Remember(memoryKey, image);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // 损坏的缓存文件直接删掉，下次重新下载。
            TryDelete(path);
            return null;
        }
    }

    private static async Task<byte[]?> DownloadAsync(Uri uri, CancellationToken ct)
    {
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(RequestTimeout);
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            using var response = await Client
                .SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeout.Token).ConfigureAwait(false);
            if (!response.IsSuccessStatusCode) return null;
            if (response.Content.Headers.ContentLength is > MaxImageBytes) return null;
            await using var stream = await response.Content.ReadAsStreamAsync(timeout.Token).ConfigureAwait(false);
            using var destination = new MemoryStream();
            var buffer = new byte[8192];
            while (true)
            {
                int read = await stream.ReadAsync(buffer, timeout.Token).ConfigureAwait(false);
                if (read == 0) break;
                if (destination.Length + read > MaxImageBytes) return null;
                destination.Write(buffer, 0, read);
            }

            return destination.ToArray();
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException or OperationCanceledException)
        {
            return null;
        }
    }

    private static async Task TryPersistAsync(string fileName, byte[] bytes, CancellationToken ct)
    {
        await DiskGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            EnsureDirectory();
            var path = Path.Combine(DirectoryPathValue, fileName);
            GuardFile(path);
            string key = fileName[..64];
            var temporary = Path.Combine(DirectoryPathValue,
                key + "." + Guid.NewGuid().ToString("N") + TemporaryExtension);
            try
            {
                await File.WriteAllBytesAsync(temporary, bytes, ct).ConfigureAwait(false);
                ct.ThrowIfCancellationRequested();
                File.Move(temporary, path, overwrite: true);
                LastError = null;
                PruneDiskCache();
            }
            finally
            {
                try { File.Delete(temporary); }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException) { }
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or OperationCanceledException)
        {
            if (error is OperationCanceledException) throw;
            LastError = "截图已加载，但本地图片缓存暂时无法保存。";
        }
        finally
        {
            DiskGate.Release();
        }
    }

    /// <summary>超过磁盘上限时按最近写入时间从旧到新淘汰（调用方需持有 <see cref="DiskGate"/>）。</summary>
    private static void PruneDiskCache()
    {
        try
        {
            if (!Directory.Exists(DirectoryPathValue)) return;
            var files = new List<FileInfo>();
            long total = 0;
            foreach (var path in Directory.EnumerateFiles(DirectoryPathValue))
            {
                if (!ManagedFile.IsMatch(Path.GetFileName(path))) continue;
                var info = new FileInfo(path);
                files.Add(info);
                total += info.Length;
            }

            if (total <= DiskByteLimit) return;
            foreach (var file in files.OrderBy(info => info.LastWriteTimeUtc))
            {
                if (total <= DiskByteLimit) break;
                try
                {
                    GuardFile(file.FullName);
                    total -= file.Length;
                    file.Delete();
                }
                catch (Exception error) when (error is IOException or UnauthorizedAccessException)
                {
                    // 删不掉的文件下次继续尝试，不能因此让图片加载失败。
                }
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            LastError = "截图缓存目录暂时无法整理。";
        }
    }

    private static string? FindDiskFile(string key)
    {
        try
        {
            GuardDirectory();
            if (!Directory.Exists(DirectoryPathValue)) return null;
            foreach (var extension in ImageExtensions)
            {
                var path = Path.Combine(DirectoryPathValue, key + extension);
                if (File.Exists(path)) return path;
            }
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            LastError = "本地截图缓存暂时无法读取，将重新下载。";
        }

        return null;
    }

    private static void TryDelete(string path)
    {
        try
        {
            GuardFile(path);
            File.Delete(path);
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            // 删不掉就留着，下次读取再判定损坏。
        }
    }

    private static ImageSource DecodeImage(byte[] bytes, int decodePixelWidth)
    {
        using var stream = new MemoryStream(bytes, writable: false);
        var image = new BitmapImage();
        image.BeginInit();
        // StreamSource 下不能同时使用 BitmapCreateOptions.IgnoreImageCache（EndInit 会因缺少缓存 key 抛异常）；
        // BitmapCacheOption.OnLoad 已经保证图片完全读入内存并且可以释放流。
        image.CacheOption = BitmapCacheOption.OnLoad;
        if (decodePixelWidth > 0)
        {
            // 相册缩略图按显示宽度解码，避免把 1280×720 / 1920×1080 的整张位图留在内存里。
            image.DecodePixelWidth = decodePixelWidth;
        }
        image.StreamSource = stream;
        image.EndInit();
        image.Freeze();
        return image;
    }

    private static bool TryGetMemory(string key, [NotNullWhen(true)] out ImageSource? image)
    {
        lock (MemoryGate)
        {
            if (Memory.TryGetValue(key, out var entry))
            {
                MemoryOrder.Remove(entry.Node);
                MemoryOrder.AddLast(entry.Node);
                image = entry.Image;
                return true;
            }
        }

        image = null;
        return false;
    }

    private static ImageSource Remember(string key, ImageSource image)
    {
        // 关键：按解码后的像素字节记账。联想商店截图 1280×720 约 3.5 MB、1920×1080 约 8 MB，
        // 若按压缩字节（几十~几百 KB）记账，64 张全尺寸截图会保留数百 MB 原生内存。
        long bytes = DecodedBytes(image);
        lock (MemoryGate)
        {
            if (Memory.Remove(key, out var previous))
            {
                _memoryBytes -= previous.Bytes;
                MemoryOrder.Remove(previous.Node);
            }

            var node = MemoryOrder.AddLast(key);
            Memory[key] = new MemoryEntry(image, bytes, node);
            _memoryBytes += bytes;
            while (Memory.Count > MemoryImageLimit || _memoryBytes > MemoryByteLimit)
            {
                var oldest = MemoryOrder.First;
                if (oldest is null) break;
                MemoryOrder.RemoveFirst();
                if (Memory.Remove(oldest.Value, out var removed)) _memoryBytes -= removed.Bytes;
            }
        }

        return image;
    }

    private static long DecodedBytes(ImageSource image) =>
        image is BitmapSource bitmap ? (long)bitmap.PixelWidth * bitmap.PixelHeight * 4 : 0;

    private static bool TryMediaUri(string? value, out Uri uri, out string key)
    {
        uri = null!;
        key = string.Empty;
        if (!Uri.TryCreate(value, UriKind.Absolute, out var parsed)) return false;
        if (parsed.Scheme != Backend.Scheme || parsed.Host != Backend.Host || parsed.Port != Backend.Port) return false;
        if (parsed.UserInfo.Length != 0) return false;
        // 只接受后端代理的商店图片路径：/api/v1/store/.../icon、/cover、/screenshots/{index}。
        if (!parsed.AbsolutePath.StartsWith(BackendRoot + "/api/v1/store/", StringComparison.Ordinal)) return false;
        uri = parsed;
        key = Key(parsed.AbsoluteUri);
        return true;
    }

    private static HttpClient CreateHttpClient()
    {
        // 后端图片接口对 UA 不敏感，但统一带一个非 curl UA 以兼容将来可能直连的 CDN。
        var client = new HttpClient { Timeout = Timeout.InfiniteTimeSpan };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("WinAppsManager/1.0");
        return client;
    }

    private static void EnsureDirectory()
    {
        GuardDirectory();
        Directory.CreateDirectory(DirectoryPathValue);
        GuardDirectory();
    }

    private static void GuardDirectory()
    {
        var full = Path.GetFullPath(DirectoryPathValue);
        if (full != CacheRoot && !full.StartsWith(CacheRoot + Path.DirectorySeparatorChar, StringComparison.Ordinal))
        {
            throw new IOException("Store media cache directory is outside its root.");
        }

        foreach (var path in new[] { CacheRoot, full })
        {
            if (Directory.Exists(path) && (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0)
            {
                throw new IOException("Linked cache directories are not supported.");
            }
        }
    }

    private static void GuardFile(string path)
    {
        var full = Path.GetFullPath(path);
        GuardDirectory();
        if (Path.GetFullPath(Path.GetDirectoryName(full)!) != Path.GetFullPath(DirectoryPathValue)
            || (!ManagedFile.IsMatch(Path.GetFileName(full)) && !TemporaryFile.IsMatch(Path.GetFileName(full))))
        {
            throw new IOException("File is not a managed store media cache file.");
        }

        if (File.Exists(full) && (File.GetAttributes(full) & FileAttributes.ReparsePoint) != 0)
        {
            throw new IOException("Linked cache files are not supported.");
        }
    }

    private static string? ImageExtension(ReadOnlySpan<byte> bytes)
    {
        if (bytes.Length >= 8 && bytes[..8].SequenceEqual(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 })) return ".png";
        if (bytes.Length >= 3 && bytes[0] == 0xFF && bytes[1] == 0xD8 && bytes[2] == 0xFF) return ".jpg";
        if (bytes.Length >= 6 && bytes[0] == 'G' && bytes[1] == 'I' && bytes[2] == 'F' && bytes[3] == '8') return ".gif";
        if (bytes.Length >= 2 && bytes[0] == 'B' && bytes[1] == 'M') return ".bmp";
        if (bytes.Length >= 6 && bytes[0] == 0 && bytes[1] == 0 && bytes[2] == 1 && bytes[3] == 0) return ".ico";
        return null;
    }

    private static string Key(string value) =>
        Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(value))).ToLowerInvariant();

    /// <summary>内存 LRU 条目；<see cref="Bytes"/> 是解码后的像素字节数（宽 × 高 × 4），不是压缩后大小。</summary>
    private sealed record MemoryEntry(ImageSource Image, long Bytes, LinkedListNode<string> Node);
}

/// <summary>联想商店截图/封面磁盘缓存的统计结果。</summary>
public sealed record StoreMediaCacheStatistics(int Files, long Bytes);


