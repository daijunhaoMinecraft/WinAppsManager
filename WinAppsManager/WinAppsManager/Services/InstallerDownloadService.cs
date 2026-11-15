using System.Buffers;
using System.Buffers.Binary;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Runtime.ExceptionServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

public sealed class InstallerDownloadService
{
    private static readonly HttpClient SharedHttpClient = CreateHttpClient();
    private static readonly object FinalizeLock = new();
    private readonly HttpClient _httpClient;
    private readonly string _downloadDirectory;

    /// <summary>默认下载目录：<c>%LOCALAPPDATA%\WinAppsManager\Downloads</c>；用户可在设置里自定义。</summary>
    public static string DefaultDownloadDirectory { get; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinAppsManager", "Downloads");

    public static InstallerDownloadService Instance { get; } = new();

    public InstallerDownloadService(HttpClient? httpClient = null, string? downloadDirectory = null)
    {
        _httpClient = httpClient ?? SharedHttpClient;
        _downloadDirectory = Path.GetFullPath(downloadDirectory ?? DefaultDownloadDirectory);
    }

    public async Task<DownloadedInstaller> DownloadAsync(AppRelease release,
        IProgress<InstallerDownloadProgress>? progress = null, CancellationToken cancellationToken = default,
        InstallerDownloadOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(release);
        var metadata = CopyRelease(release);
        var uri = ValidateRelease(metadata);
        var settings = options ?? new InstallerDownloadOptions { ConnectionCount = 1 };
        ValidateOptions(settings);
        cancellationToken.ThrowIfCancellationRequested();
        Directory.CreateDirectory(_downloadDirectory);
        var partialPath = Path.Combine(_downloadDirectory, $"{metadata.FileName}.{Guid.NewGuid():N}.part");
        await using var tracker = new InstallerDownloadTracker(progress, settings.ConnectionCount, metadata.SizeBytes);
        try
        {
            var received = await TransferAsync(metadata, uri, partialPath, settings, tracker, cancellationToken).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            tracker.SetPhase(InstallerDownloadPhase.Verifying);
            cancellationToken.ThrowIfCancellationRequested();
            if (!string.IsNullOrWhiteSpace(metadata.Sha256))
            {
                await using var file = new FileStream(partialPath, FileMode.Open, FileAccess.Read, FileShare.Read,
                    81920, FileOptions.Asynchronous | FileOptions.SequentialScan);
                var hash = await BclCompat.Sha256Async(file, cancellationToken).ConfigureAwait(false);
                string actualHash = Convert.ToHexString(hash);
                if (!actualHash.Equals(metadata.Sha256, StringComparison.OrdinalIgnoreCase))
                {
                    AppLogService.Error(
                        $"安装包 SHA256 校验失败：{metadata.FileName}，期望 {metadata.Sha256}，实际 {actualHash}。");
                    throw new InvalidDataException("安装包校验失败，请重新下载。");
                }
            }
            await ValidateInstallerAsync(partialPath, metadata.FileName, cancellationToken).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            string filePath;
            lock (FinalizeLock)
            {
                filePath = GetAvailableFilePath(metadata.FileName);
                File.Move(partialPath, filePath);
            }
            await tracker.StopAsync().ConfigureAwait(false);
            tracker.SetPhase(InstallerDownloadPhase.Completed);
            return new DownloadedInstaller(filePath, received, metadata.Id);
        }
        catch (OperationCanceledException error) when (!cancellationToken.IsCancellationRequested)
        {
            tracker.FinishUncompleted(InstallerDownloadSegmentState.Failed);
            tracker.SetPhase(InstallerDownloadPhase.Failed, "下载连接超时，请检查网络后重试。");
            throw new TimeoutException("下载连接超时，请检查网络后重试。", error);
        }
        catch (OperationCanceledException)
        {
            tracker.FinishUncompleted(InstallerDownloadSegmentState.Cancelled);
            tracker.SetPhase(InstallerDownloadPhase.Cancelled, "下载已取消。");
            throw;
        }
        catch
        {
            tracker.FinishUncompleted(InstallerDownloadSegmentState.Failed);
            tracker.SetPhase(InstallerDownloadPhase.Failed, "下载未完成，请重试。");
            throw;
        }
        finally { DeletePartialFile(partialPath); }
    }

    private async Task<long> TransferAsync(AppRelease release, Uri uri, string path, InstallerDownloadOptions options,
        InstallerDownloadTracker tracker, CancellationToken ct)
    {
        if (options.ConnectionCount == 1 || (release.SizeBytes.HasValue && release.SizeBytes.Value / options.MinimumSegmentSize < 2))
        {
            string? fallbackMessage = options.ConnectionCount > 1 ? "安装包较小，正在使用单连接下载。" : null;
            if (fallbackMessage is null)
            {
                AppLogService.Info($"开始下载安装包：{release.FileName}（单连接）。");
            }
            else
            {
                AppLogService.Warn($"安装包单连接回退：{release.FileName}，安装包较小，无法拆分分段。");
            }

            using var response = await SendAsync(uri, null, null, ct, options.Timeout).ConfigureAwait(false);
            return await DownloadSingleAsync(release, response, path, tracker, ct, options.Timeout,
                fallbackMessage).ConfigureAwait(false);
        }
        using var probe = await SendAsync(uri, new RangeHeaderValue(0, 0), null, ct, options.Timeout).ConfigureAwait(false);
        if (probe.StatusCode == HttpStatusCode.OK)
        {
            AppLogService.Warn($"安装包单连接回退：{release.FileName}，服务器未提供分段响应。");
            return await DownloadSingleAsync(release, probe, path, tracker, ct, options.Timeout,
                "服务器未提供分段响应，正在使用单连接下载。").ConfigureAwait(false);
        }
        if (probe.StatusCode is HttpStatusCode.BadRequest or HttpStatusCode.Forbidden or HttpStatusCode.MethodNotAllowed
            or HttpStatusCode.RequestedRangeNotSatisfiable or HttpStatusCode.NotImplemented)
        {
            AppLogService.Warn(
                $"安装包单连接回退：{release.FileName}，服务器不支持分段请求（HTTP {(int)probe.StatusCode}）。");
            using var response = await SendAsync(uri, null, null, ct, options.Timeout).ConfigureAwait(false);
            return await DownloadSingleAsync(release, response, path, tracker, ct, options.Timeout, "服务器不支持分段请求，正在使用单连接下载。").ConfigureAwait(false);
        }
        probe.EnsureSuccessStatusCode();
        ValidateContentType(probe);
        if (probe.StatusCode != HttpStatusCode.PartialContent)
            throw new InvalidDataException("服务器返回了无效的分段探测响应。");
        var range = probe.Content.Headers.ContentRange;
        if (range is null || !range.Unit.Equals("bytes", StringComparison.OrdinalIgnoreCase)
            || range.From != 0 || range.To != 0 || range.Length is <= 0
            || (probe.Content.Headers.ContentLength.HasValue && probe.Content.Headers.ContentLength != 1))
            throw new InvalidDataException("服务器返回了无效的分段范围。");
        await ValidateProbeBodyAsync(probe, ct, options.Timeout).ConfigureAwait(false);
        if (!range.Length.HasValue)
        {
            AppLogService.Warn($"安装包单连接回退：{release.FileName}，服务器未提供安装包总大小。");
            probe.Dispose();
            using var response = await SendAsync(uri, null, null, ct, options.Timeout).ConfigureAwait(false);
            return await DownloadSingleAsync(release, response, path, tracker, ct, options.Timeout,
                "服务器未提供安装包总大小，正在使用单连接下载。").ConfigureAwait(false);
        }
        var total = range.Length.Value;
        var count = (int)Math.Min(options.ConnectionCount, Math.Max(1, total / options.MinimumSegmentSize));
        var validator = GetValidator(probe);
        if (count < 2 || HasContentEncoding(probe) || (validator is null && string.IsNullOrWhiteSpace(release.Sha256)))
        {
            string fallbackReason = count < 2
                ? "安装包较小，无法拆分分段"
                : HasContentEncoding(probe)
                    ? "服务器启用了内容压缩，无法安全分段"
                    : "服务器未提供 Etag/Last-Modified 校验信息且没有 SHA256";
            AppLogService.Warn($"安装包单连接回退：{release.FileName}，{fallbackReason}。");
            probe.Dispose();
            using var response = await SendAsync(uri, null, null, ct, options.Timeout).ConfigureAwait(false);
            return await DownloadSingleAsync(release, response, path, tracker, ct, options.Timeout,
                count < 2 ? "安装包较小，正在使用单连接下载。" : "服务器未提供可靠的分段校验信息，正在使用单连接下载。").ConfigureAwait(false);
        }
        probe.Dispose();
        AppLogService.Info($"开始分段下载安装包：{release.FileName}，{count} 个连接，共 {total} 字节。");
        try
        {
            return await DownloadSegmentsAsync(uri, release.FileName, path, total, count, validator, tracker, ct, options.Timeout).ConfigureAwait(false);
        }
        catch (RangeUnavailableException)
        {
            ct.ThrowIfCancellationRequested();
            AppLogService.Warn($"安装包单连接回退：{release.FileName}，服务器改变了分段响应（Range/ETag/Last-Modified 不一致）。");
            DeletePartialFile(path);
            tracker.SetPhase(InstallerDownloadPhase.Connecting, "服务器改变了分段响应，正在切换为单连接重新下载。");
            using var response = await SendAsync(uri, null, null, ct, options.Timeout).ConfigureAwait(false);
            return await DownloadSingleAsync(release, response, path, tracker, ct, options.Timeout,
                "服务器改变了分段响应，已切换为单连接重新下载。").ConfigureAwait(false);
        }
    }

    private async Task<HttpResponseMessage> SendAsync(Uri uri, RangeHeaderValue? range, RangeConditionHeaderValue? validator, CancellationToken ct, TimeSpan timeout)
    {
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutSource.CancelAfter(timeout);
        using var request = new HttpRequestMessage(HttpMethod.Get, uri);
        request.Headers.AcceptEncoding.Add(new StringWithQualityHeaderValue("identity"));
        request.Headers.Range = range;
        if (validator is not null) request.Headers.IfRange = validator;
        try { return await _httpClient.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, timeoutSource.Token).ConfigureAwait(false); }
        catch (OperationCanceledException error) when (!ct.IsCancellationRequested)
        { throw new TimeoutException("连接下载服务器超时，请重试。", error); }
    }

    private static async Task ValidateProbeBodyAsync(HttpResponseMessage response, CancellationToken ct, TimeSpan timeout)
    {
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutSource.CancelAfter(timeout);
        await using var source = await response.Content.ReadAsStreamAsync(timeoutSource.Token).ConfigureAwait(false);
        var bytes = new byte[2];
        var received = 0;
        while (received < bytes.Length)
        {
            timeoutSource.CancelAfter(timeout);
            var count = await source.ReadAsync(bytes.AsMemory(received, bytes.Length - received), timeoutSource.Token).ConfigureAwait(false);
            if (count == 0) break;
            received += count;
        }
        if (received != 1) throw new InvalidDataException("服务器返回的分段探测内容不完整或超出范围。");
    }

    private static async Task<long> DownloadSingleAsync(AppRelease release, HttpResponseMessage response, string path,
        InstallerDownloadTracker tracker, CancellationToken ct, TimeSpan timeout, string? message)
    {
        response.EnsureSuccessStatusCode();
        ValidateContentType(response);
        if (response.StatusCode == HttpStatusCode.PartialContent)
            throw new InvalidDataException("服务器没有返回完整的安装包响应。");
        var contentLength = response.Content.Headers.ContentLength;
        if (contentLength == 0)
        {
            AppLogService.Error($"安装包长度校验失败：{release.FileName}，服务器返回的内容长度为 0。");
            throw new InvalidDataException("下载的安装包为空，请稍后重试。");
        }
        var total = contentLength ?? release.SizeBytes;
        tracker.Configure(total, [new InstallerDownloadSegmentProgress(1, 0, total.HasValue ? total - 1 : null,
            0, InstallerDownloadSegmentState.Downloading)], message);
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutSource.CancelAfter(timeout);
        var buffer = ArrayPool<byte>.Shared.Rent(81920);
        long received = 0;
        try
        {
            await using var source = await response.Content.ReadAsStreamAsync(timeoutSource.Token).ConfigureAwait(false);
            await using var destination = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None,
                buffer.Length, FileOptions.Asynchronous | FileOptions.SequentialScan);
            while (true)
            {
                timeoutSource.CancelAfter(timeout);
                var count = await source.ReadAsync(buffer.AsMemory(), timeoutSource.Token).ConfigureAwait(false);
                if (count == 0) break;

                await destination.WriteAsync(buffer.AsMemory(0, count), timeoutSource.Token).ConfigureAwait(false);
                received = checked(received + count);
                tracker.AddBytes(0, count);
            }
            await destination.FlushAsync(timeoutSource.Token).ConfigureAwait(false);

            tracker.SetSegmentState(0, InstallerDownloadSegmentState.Completed);
            return received;
        }
        finally { ArrayPool<byte>.Shared.Return(buffer); }
    }

    private async Task<long> DownloadSegmentsAsync(Uri uri, string fileName, string path, long total, int count,
        RangeConditionHeaderValue? validator, InstallerDownloadTracker tracker, CancellationToken ct, TimeSpan timeout)
    {
        var segments = Partition(total, count);
        tracker.Configure(total, segments, $"正在使用 {count} 个连接分段下载。");
        await using var destination = new FileStream(path, FileMode.CreateNew, FileAccess.ReadWrite, FileShare.None,
            81920, FileOptions.Asynchronous | FileOptions.RandomAccess);
        destination.SetLength(total);
        using var siblings = CancellationTokenSource.CreateLinkedTokenSource(ct);
        Exception? firstFailure = null;
        var tasks = segments.Select((segment, index) => RunSegmentAsync(segment, index)).ToArray();
        try { await Task.WhenAll(tasks).ConfigureAwait(false); }
        catch
        {
            if (ct.IsCancellationRequested) throw new OperationCanceledException(ct);
            if (firstFailure is not null) ExceptionDispatchInfo.Capture(firstFailure).Throw();
            throw;
        }
        await destination.FlushAsync(ct).ConfigureAwait(false);
        return total;

        async Task RunSegmentAsync(InstallerDownloadSegmentProgress segment, int index)
        {
            try
            {
                tracker.SetSegmentState(index, InstallerDownloadSegmentState.Connecting);
                using var response = await SendAsync(uri, new RangeHeaderValue(segment.StartByte, segment.EndByte), validator, siblings.Token, timeout).ConfigureAwait(false);
                if (response.StatusCode is HttpStatusCode.OK or HttpStatusCode.RequestedRangeNotSatisfiable) throw new RangeUnavailableException();
                response.EnsureSuccessStatusCode();
                ValidateContentType(response);
                if (HasContentEncoding(response)) throw new RangeUnavailableException();
                ValidateSegmentResponse(response, fileName, segment, total, validator);
                tracker.SetSegmentState(index, InstallerDownloadSegmentState.Downloading);
                await CopySegmentAsync(response, destination.SafeFileHandle, fileName, segment, index, tracker, siblings.Token, timeout).ConfigureAwait(false);
                tracker.SetSegmentState(index, InstallerDownloadSegmentState.Completed);
            }
            catch (Exception error)
            {
                var cancelled = error is OperationCanceledException && siblings.IsCancellationRequested;
                tracker.SetSegmentState(index, cancelled ? InstallerDownloadSegmentState.Cancelled : InstallerDownloadSegmentState.Failed);
                if (!cancelled) Interlocked.CompareExchange(ref firstFailure, error, null);
                siblings.Cancel();
                throw;
            }
        }
    }

    private static async Task CopySegmentAsync(HttpResponseMessage response, SafeFileHandle handle, string fileName,
        InstallerDownloadSegmentProgress segment, int index, InstallerDownloadTracker tracker, CancellationToken ct, TimeSpan timeout)
    {
        using var timeoutSource = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeoutSource.CancelAfter(timeout);
        var buffer = ArrayPool<byte>.Shared.Rent(81920);
        long received = 0;
        var expected = segment.TotalBytes!.Value;
        try
        {
            await using var source = await response.Content.ReadAsStreamAsync(timeoutSource.Token).ConfigureAwait(false);
            while (received < expected)
            {
                timeoutSource.CancelAfter(timeout);
                var maximum = (int)Math.Min(buffer.Length, expected - received);
                var count = await source.ReadAsync(buffer.AsMemory(0, maximum), timeoutSource.Token).ConfigureAwait(false);
                if (count == 0)
                {
                    AppLogService.Error(
                        $"下载分段长度校验失败：{fileName}，分段 {segment.Index} 期望 {expected} 字节，实际 {received} 字节。");
                    throw new InvalidDataException("一个下载分段未完整传输，请重试。");
                }
                await RandomAccess.WriteAsync(handle, buffer.AsMemory(0, count), segment.StartByte + received, timeoutSource.Token).ConfigureAwait(false);
                received += count;
                tracker.AddBytes(index, count);
            }
            timeoutSource.CancelAfter(timeout);
            if (await source.ReadAsync(buffer.AsMemory(0, 1), timeoutSource.Token).ConfigureAwait(false) != 0)
            {
                AppLogService.Error(
                    $"下载分段长度校验失败：{fileName}，分段 {segment.Index} 期望 {expected} 字节，服务器返回了超出范围的内容。");
                throw new InvalidDataException("一个下载分段返回了超出范围的内容。");
            }
        }
        catch (OperationCanceledException error) when (!ct.IsCancellationRequested)
        { throw new TimeoutException("一个下载分段长时间没有响应，请重试。", error); }
        finally { ArrayPool<byte>.Shared.Return(buffer); }
    }

    private static InstallerDownloadSegmentProgress[] Partition(long total, int count)
    {
        var parts = new InstallerDownloadSegmentProgress[count];
        long baseSize = total / count, remainder = total % count, start = 0;
        for (var index = 0; index < count; index++)
        {
            var length = baseSize + (index < remainder ? 1 : 0);
            parts[index] = new InstallerDownloadSegmentProgress(index + 1, start, start + length - 1, 0, InstallerDownloadSegmentState.Waiting);
            start += length;
        }
        return parts;
    }

    private static RangeConditionHeaderValue? GetValidator(HttpResponseMessage response)
    {
        if (response.Headers.ETag is { IsWeak: false } tag) return new RangeConditionHeaderValue(tag);
        return response.Content.Headers.LastModified is { } modified ? new RangeConditionHeaderValue(modified) : null;
    }

    private static void ValidateSegmentResponse(HttpResponseMessage response, string fileName,
        InstallerDownloadSegmentProgress segment, long total, RangeConditionHeaderValue? validator)
    {
        var range = response.Content.Headers.ContentRange;
        if (response.StatusCode != HttpStatusCode.PartialContent || range is null || !range.Unit.Equals("bytes", StringComparison.OrdinalIgnoreCase)
            || range.From != segment.StartByte || range.To != segment.EndByte || range.Length != total
            || (response.Content.Headers.ContentLength.HasValue && response.Content.Headers.ContentLength != segment.TotalBytes))
        {
            AppLogService.Error(
                $"下载分段校验失败：{fileName}，分段 {segment.Index} 期望 {segment.StartByte}-{segment.EndByte}" +
                $"（总长 {total}），服务器返回状态 {response.StatusCode}、范围 {range?.From}-{range?.To}（总长 {range?.Length}）。");
            throw new InvalidDataException("服务器返回的下载分段范围或大小不正确。");
        }
        if (validator?.EntityTag is { } tag && response.Headers.ETag is { } actualTag && (actualTag.IsWeak || actualTag.Tag != tag.Tag))
            throw new RangeUnavailableException();
        if (validator?.Date is { } date && response.Content.Headers.LastModified is { } actualDate && actualDate != date)
            throw new RangeUnavailableException();
    }

    private static bool HasContentEncoding(HttpResponseMessage response) => response.Content.Headers.ContentEncoding
        .Any(value => !value.Equals("identity", StringComparison.OrdinalIgnoreCase));

    private static void ValidateContentType(HttpResponseMessage response)
    {
        var type = response.Content.Headers.ContentType?.MediaType;
        if (type is not null && (type.StartsWith("text/", StringComparison.OrdinalIgnoreCase) || type.Contains("json", StringComparison.OrdinalIgnoreCase)))
            throw new InvalidDataException("下载地址返回了网页或错误信息，请刷新软件详情后重试。");
    }


    private static void ValidateOptions(InstallerDownloadOptions options)
    {
        if (options.ConnectionCount < InstallerDownloadOptions.MinimumConnections || options.ConnectionCount > InstallerDownloadOptions.MaximumConnections)
            throw new ArgumentOutOfRangeException(nameof(options), "下载连接数必须在 1 到 32 之间。");
        if (options.TimeoutSeconds < InstallerDownloadOptions.MinimumTimeoutSeconds || options.TimeoutSeconds > InstallerDownloadOptions.MaximumTimeoutSeconds)
            throw new ArgumentOutOfRangeException(nameof(options), "下载超时秒数必须在 10 到 300 之间。");
        if (options.MinimumSegmentSize <= 0) throw new ArgumentOutOfRangeException(nameof(options), "最小分段大小必须大于 0。");
    }

    private static AppRelease CopyRelease(AppRelease source) => new()
    {
        Id = source.Id, Version = source.Version, Platform = source.Platform, Architecture = source.Architecture,
        DownloadUrl = source.DownloadUrl, FileName = source.FileName, SizeBytes = source.SizeBytes, Sha256 = source.Sha256
    };

    public static void OpenInstaller(string filePath)
    {
        var absolutePath = Path.GetFullPath(filePath);
        if (!File.Exists(absolutePath)) throw new FileNotFoundException("安装包已被移动或删除，请重新下载。", absolutePath);
        Process.Start(new ProcessStartInfo(absolutePath) { UseShellExecute = true });
    }

    private string GetAvailableFilePath(string fileName)
    {
        var candidate = Path.Combine(_downloadDirectory, fileName);
        var stem = Path.GetFileNameWithoutExtension(fileName);
        var extension = Path.GetExtension(fileName);
        for (var index = 1; File.Exists(candidate) || Directory.Exists(candidate); index++) candidate = Path.Combine(_downloadDirectory, $"{stem} ({index}){extension}");
        return candidate;
    }

    /// <summary>安装包扩展名白名单：EXE 用 PE 头校验，MSI 是 OLE 容器，MSIX/APPX 是 ZIP 容器，MSU 是 CAB 容器。</summary>
    private static readonly string[] InstallerExtensions = [".exe", ".msi", ".msix", ".msixbundle", ".appx", ".msu"];

    private static bool IsSupportedInstallerFile(string fileName) =>
        InstallerExtensions.Any(extension => fileName.EndsWith(extension, StringComparison.OrdinalIgnoreCase));

    private static Uri ValidateRelease(AppRelease release)
    {
        if (!Uri.TryCreate(release.DownloadUrl, UriKind.Absolute, out var uri) || (uri.Scheme != Uri.UriSchemeHttps && uri.Scheme != Uri.UriSchemeHttp))
            throw new InvalidDataException("后端提供的下载地址无效，请刷新软件详情。");
        if (string.IsNullOrWhiteSpace(release.FileName) || release.FileName != Path.GetFileName(release.FileName)
            || release.FileName.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0 || !IsSupportedInstallerFile(release.FileName)
            || (release.Sha256 is not null && (release.Sha256.Length != 64 || !release.Sha256.All(Uri.IsHexDigit))))
            throw new InvalidDataException("后端提供的安装包信息无效，请刷新软件详情。");
        return uri;
    }

    private static async Task ValidateInstallerAsync(string path, string fileName, CancellationToken ct)
    {
        var extension = Path.GetExtension(fileName).ToLowerInvariant();
        await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.Asynchronous);
        if (extension == ".exe")
        {
            if (stream.Length < 68) throw new InvalidDataException("下载内容不是有效的 Windows 安装包。");
            var header = new byte[64];
            await BclCompat.ReadExactlyAsync(stream, header, ct).ConfigureAwait(false);
            var peOffset = BinaryPrimitives.ReadInt32LittleEndian(header.AsSpan(60, 4));
            if (header[0] != 'M' || header[1] != 'Z' || peOffset < 64 || peOffset > stream.Length - 4) throw new InvalidDataException("下载内容不是有效的 Windows 安装包。");
            stream.Position = peOffset;
            var signature = new byte[4];
            await BclCompat.ReadExactlyAsync(stream, signature, ct).ConfigureAwait(false);
            if (signature[0] != 'P' || signature[1] != 'E' || signature[2] != 0 || signature[3] != 0) throw new InvalidDataException("下载内容不是有效的 Windows 安装包。");
            return;
        }

        var prefix = new byte[8];
        if (stream.Length < 8) throw new InvalidDataException("下载内容不是有效的 Windows 安装包。");
        await BclCompat.ReadExactlyAsync(stream, prefix, ct).ConfigureAwait(false);
        var valid = extension switch
        {
            // OLE 复合文档（MSI 数据库）签名
            ".msi" => prefix[0] == 0xD0 && prefix[1] == 0xCF && prefix[2] == 0x11 && prefix[3] == 0xE0
                      && prefix[4] == 0xA1 && prefix[5] == 0xB1 && prefix[6] == 0x1A && prefix[7] == 0xE1,
            // MSU 是 CAB 容器（Windows 更新独立安装程序包）
            ".msu" => prefix[0] == 0x4D && prefix[1] == 0x53 && prefix[2] == 0x43 && prefix[3] == 0x46,
            // MSIX / APPX 是 ZIP 容器
            _ => prefix[0] == 0x50 && prefix[1] == 0x4B && prefix[2] == 0x03 && prefix[3] == 0x04
        };
        if (!valid) throw new InvalidDataException("下载内容不是有效的 Windows 安装包。");
    }

    private static void DeletePartialFile(string path)
    {
        try { if (File.Exists(path)) File.Delete(path); }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException) { Debug.WriteLine(error); }
    }

    private static HttpClient CreateHttpClient()
    {
        var client = new HttpClient(new SocketsHttpHandler
        {
            ConnectTimeout = TimeSpan.FromSeconds(15), MaxConnectionsPerServer = InstallerDownloadOptions.MaximumConnections
        }) { Timeout = Timeout.InfiniteTimeSpan };
        client.DefaultRequestHeaders.UserAgent.ParseAdd("WinAppsManager/1.0");
        return client;
    }

    private sealed class RangeUnavailableException : Exception { }
}
