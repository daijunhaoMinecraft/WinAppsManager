using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Security;
using System.Text;
using System.Text.Json;
using System.Windows;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>Result of deleting the installer files selected in the download center.</summary>
public sealed record InstallerDeleteOutcome(
    int DeletedFiles,
    int RemovedRecords,
    IReadOnlyList<string> Errors);

/// <summary>
/// Owns all install downloads so they keep running independently of the details
/// page and can be observed from the download center. Finished downloads are persisted
/// to <c>%LOCALAPPDATA%\WinAppsManager\download-history.json</c> and restored on startup.
/// </summary>
public static class DownloadCenterService
{
    private static readonly ObservableCollection<DownloadTaskViewModel> Items = new();
    private static readonly ReadOnlyObservableCollection<DownloadTaskViewModel> ReadOnlyItems = new(Items);
    private static readonly object Gate = new();
    private static readonly object HistoryGate = new();
    private static readonly string HistoryPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "WinAppsManager",
        "download-history.json");
    private static readonly JsonSerializerOptions HistoryJsonOptions = new()
    {
        PropertyNameCaseInsensitive = true,
        WriteIndented = true
    };

    static DownloadCenterService()
    {
        LoadHistory();
    }

    public static ReadOnlyObservableCollection<DownloadTaskViewModel> Downloads => ReadOnlyItems;
    public static event EventHandler? StatusChanged;

    public static int ActiveCount => Items.Count(item => item.IsActive);

    public static DownloadTaskViewModel? Find(string appId)
    {
        lock (Gate)
        {
            return Items.FirstOrDefault(item => item.AppId == appId);
        }
    }

    /// <summary>
    /// 查找某个软件条目对应的下载任务：除应用 ID 外还校验来源 ID，避免不同软件源的同名 ID 串用任务。
    /// </summary>
    public static DownloadTaskViewModel? Find(AppItem app)
    {
        ArgumentNullException.ThrowIfNull(app);
        lock (Gate)
        {
            return Items.FirstOrDefault(item =>
                item.AppId == app.Id
                && (string.IsNullOrWhiteSpace(app.SourceId)
                    || string.IsNullOrWhiteSpace(item.App.SourceId)
                    || string.Equals(item.App.SourceId, app.SourceId, StringComparison.OrdinalIgnoreCase)));
        }
    }

    public static DownloadTaskViewModel Start(AppItem app)
    {
        ArgumentNullException.ThrowIfNull(app);
        AppLogService.Info($"开始下载：{app.Name}（{app.Id}，源={app.SourceId ?? "当前来源"}）。");
        DownloadTaskViewModel task;
        lock (Gate)
        {
            var existing = Find(app);
            if (existing is { IsActive: true })
            {
                return existing;
            }
            if (existing is not null)
            {
                Items.Remove(existing);
            }

            task = new DownloadTaskViewModel(app);
            Items.Insert(0, task);
        }

        SaveHistory();
        task.Completion = RunAsync(task);
        RaiseStatusChanged();
        return task;
    }

    public static bool Remove(DownloadTaskViewModel task)
    {
        ArgumentNullException.ThrowIfNull(task);
        if (task.IsActive)
        {
            return false;
        }

        bool removed;
        lock (Gate)
        {
            removed = Items.Remove(task);
        }
        if (removed)
        {
            // 记录没了，未完成分片也没有意义，一并删掉免得占空间。
            InstallerDownloadService.DeleteResumeFiles(task.FileName, AppSettingsService.ResolvedDownloadDirectory);
            SaveHistory();
            RaiseStatusChanged();
        }
        return removed;
    }

    public static DownloadTaskViewModel Restart(DownloadTaskViewModel task)
    {
        ArgumentNullException.ThrowIfNull(task);
        Remove(task);
        return Start(task.App);
    }

    public static int ClearFinished()
    {
        var finished = Items.Where(item => !item.IsActive).ToArray();
        lock (Gate)
        {
            foreach (var task in finished)
            {
                Items.Remove(task);
            }
        }
        if (finished.Length > 0)
        {
            foreach (var task in finished)
            {
                InstallerDownloadService.DeleteResumeFiles(task.FileName, AppSettingsService.ResolvedDownloadDirectory);
            }

            SaveHistory();
            RaiseStatusChanged();
        }
        return finished.Length;
    }

    /// <summary>
    /// Deletes the selected installer files and removes their history records. A task whose file is
    /// already gone is still removed from the history; a file that cannot be deleted stays in the
    /// list so the user can retry after closing the locking program.
    /// </summary>
    public static InstallerDeleteOutcome DeleteInstallers(IEnumerable<DownloadTaskViewModel> tasks)
    {
        ArgumentNullException.ThrowIfNull(tasks);
        var selected = tasks.Where(task => task.CanRemove).Distinct().ToArray();
        int deletedFiles = 0;
        int removedRecords = 0;
        var errors = new List<string>();

        foreach (var task in selected)
        {
            if (task.IsActive)
            {
                continue;
            }

            string? filePath = task.FilePath;
            if (!string.IsNullOrWhiteSpace(filePath) && File.Exists(filePath))
            {
                try
                {
                    File.Delete(filePath);
                    deletedFiles++;
                }
                catch (Exception exception) when (exception is IOException or
                                                      UnauthorizedAccessException or
                                                      SecurityException)
                {
                    errors.Add($"{task.AppName}：{exception.Message}");
                    continue;
                }
            }

            // 未完成的续传分片同样删掉：记录都移除了，留着只会占磁盘。
            InstallerDownloadService.DeleteResumeFiles(task.FileName, AppSettingsService.ResolvedDownloadDirectory);

            lock (Gate)
            {
                if (Items.Remove(task))
                {
                    removedRecords++;
                }
            }
        }

        if (removedRecords > 0)
        {
            SaveHistory();
            RaiseStatusChanged();
        }

        AppLogService.Info(
            $"下载中心删除安装包：删除文件 {deletedFiles} 个，移除记录 {removedRecords} 个，失败 {errors.Count} 个。");
        return new InstallerDeleteOutcome(deletedFiles, removedRecords, errors);
    }

    public static void Cancel(DownloadTaskViewModel task)
    {
        ArgumentNullException.ThrowIfNull(task);
        task.RequestCancel();
        RaiseStatusChanged();
    }

    public static async Task CancelAllAsync()
    {
        var active = Items.Where(item => item.IsActive).ToArray();
        foreach (var task in active)
        {
            task.RequestCancel();
        }
        var completions = active.Select(task => task.Completion).ToArray();
        if (completions.Length > 0)
        {
            await Task.WhenAll(completions).ConfigureAwait(false);
        }
        RaiseStatusChanged();
    }

    public static bool OpenInstaller(DownloadTaskViewModel task)
    {
        ArgumentNullException.ThrowIfNull(task);
        if (string.IsNullOrWhiteSpace(task.FilePath) || !File.Exists(task.FilePath))
        {
            task.MarkFileMissing("安装包已被移动或删除，请重新下载。");
            SaveHistory();
            RaiseStatusChanged();
            return false;
        }

        try
        {
            InstallerDownloadService.OpenInstaller(task.FilePath);
            return true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or InvalidOperationException or Win32Exception)
        {
            task.MarkFileMissing("安装包无法打开，请重新下载。");
            RaiseStatusChanged();
            Debug.WriteLine(exception);
            return false;
        }
    }

    public static bool OpenDownloadFolder()
    {
        try
        {
            string directory = AppSettingsService.ResolvedDownloadDirectory;
            Directory.CreateDirectory(directory);
            Process.Start(new ProcessStartInfo(directory) { UseShellExecute = true });
            return true;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or InvalidOperationException or Win32Exception)
        {
            Debug.WriteLine(exception);
            return false;
        }
    }

    private static async Task RunAsync(DownloadTaskViewModel task)
    {
        using var cancellation = new CancellationTokenSource();
        task.AttachCancellation(cancellation);
        var stopwatch = Stopwatch.StartNew();
        try
        {
            // 传入 AppItem：按其 SourceId 请求对应来源的下载地址，目录里的 LatestRelease 为空也能开始下载。
            var release = await AppCatalogService.GetLatestReleaseAsync(task.App, cancellation.Token).ConfigureAwait(false);
            cancellation.Token.ThrowIfCancellationRequested();
            AppLogService.Info(
                $"获取安装包信息完成：{task.AppName}（{task.AppId}），版本={release.Version}，" +
                $"大小={(release.SizeBytes is > 0 ? $"{release.SizeBytes.Value} 字节" : "未知")}。");
            await OnUiAsync(() => task.SetRelease(release)).ConfigureAwait(false);

            var progress = new UiProgress<InstallerDownloadProgress>(task.ApplyProgress);
            var settings = AppSettingsService.Current;
            var options = new InstallerDownloadOptions
            {
                ConnectionCount = settings.DownloadConnections,
                TimeoutSeconds = settings.DownloadTimeoutSeconds
            };
            var downloadRelease = DownloadNetworkService.ApplyMirror(release);
            using var httpClient = DownloadNetworkService.CreateHttpClient();
            // 下载目录来自设置；修改后只影响之后开始的任务，已有任务沿用启动时的路径。
            var downloadService = new InstallerDownloadService(httpClient, AppSettingsService.ResolvedDownloadDirectory);
            var result = await downloadService
                .DownloadAsync(downloadRelease, progress, cancellation.Token, options)
                .ConfigureAwait(false);
            await OnUiAsync(() => task.Complete(result)).ConfigureAwait(false);
            AppLogService.Info(
                $"下载完成：{task.AppName}（{task.AppId}），文件={result.FilePath}，" +
                $"{result.SizeBytes} 字节，耗时 {stopwatch.ElapsedMilliseconds} ms。");

            // 数字签名仅作为提示展示，校验失败/未签名绝不影响安装包保存和打开。
            AuthenticodeSignatureInfo signature;
            try
            {
                signature = await Task.Run(() => AuthenticodeSignatureService.Inspect(result.FilePath))
                    .ConfigureAwait(false);
            }
            catch (Exception exception) when (exception is not OutOfMemoryException)
            {
                signature = new AuthenticodeSignatureInfo(
                    false, false, string.Empty, string.Empty, string.Empty, null, null,
                    "数字签名：无法检查",
                    "读取安装包数字签名失败，仍可打开安装包。",
                    exception.GetType().Name);
                AppLogService.Warn(
                    $"读取安装包数字签名失败：{task.AppName}（{task.AppId}），" +
                    $"{exception.GetType().Name}：{exception.Message}",
                    exception);
            }

            await OnUiAsync(() => task.ApplySignature(signature)).ConfigureAwait(false);
            AppLogService.Info(
                $"安装包数字签名：{task.AppName}（{task.AppId}），{signature.StatusText}；" +
                $"{signature.DetailText}");
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            await OnUiAsync(task.Cancel).ConfigureAwait(false);
            AppLogService.Info(
                $"下载已取消：{task.AppName}（{task.AppId}），耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException)
        {
            await OnUiAsync(() => task.Fail("获取安装包信息超时，请重试。")).ConfigureAwait(false);
            AppLogService.Warn(
                $"下载超时：{task.AppName}（{task.AppId}），耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception) when (exception is HttpRequestException or IOException or InvalidDataException
            or UnauthorizedAccessException or TimeoutException or JsonException or NotSupportedException)
        {
            await OnUiAsync(() => task.Fail(DescribeError(exception))).ConfigureAwait(false);
            AppLogService.Warn(
                $"下载失败：{task.AppName}（{task.AppId}），{exception.GetType().Name}：{exception.Message}",
                exception);
            Debug.WriteLine(exception);
        }
        catch (Exception exception)
        {
            await OnUiAsync(() => task.Fail("下载失败，请稍后重试。")).ConfigureAwait(false);
            AppLogService.Error(
                $"下载出现未预期错误：{task.AppName}（{task.AppId}），{exception.GetType().Name}：{exception.Message}",
                exception);
            Debug.WriteLine(exception);
        }
        finally
        {
            task.DetachCancellation();
            SaveHistory();
            RaiseStatusChanged();
        }
    }

    private static void LoadHistory()
    {
        try
        {
            if (!File.Exists(HistoryPath))
            {
                return;
            }

            string json = File.ReadAllText(HistoryPath);
            var records = JsonSerializer.Deserialize<List<DownloadHistoryRecord>>(json, HistoryJsonOptions);
            if (records is null || records.Count == 0)
            {
                return;
            }

            lock (Gate)
            {
                foreach (var record in records.OrderByDescending(entry => entry.StartedAt))
                {
                    var task = DownloadTaskViewModel.Restore(record);
                    if (task is not null)
                    {
                        Items.Add(task);
                    }
                }
            }

            AppLogService.Info($"下载历史已加载：{records.Count} 条。");
        }
        catch (Exception exception) when (exception is IOException or
                                              UnauthorizedAccessException or
                                              JsonException or
                                              NotSupportedException)
        {
            AppLogService.Warn(
                $"加载下载历史失败，已忽略历史文件：{exception.GetType().Name}：{exception.Message}",
                exception);
        }
    }

    private static void SaveHistory()
    {
        List<DownloadHistoryRecord> records;
        lock (Gate)
        {
            records = Items
                .Where(task => task.CanRemove)
                .Select(ToRecord)
                .Take(200)
                .ToList();
        }

        try
        {
            lock (HistoryGate)
            {
                string? directory = Path.GetDirectoryName(HistoryPath);
                if (!string.IsNullOrWhiteSpace(directory))
                {
                    Directory.CreateDirectory(directory);
                }

                string temporaryPath = HistoryPath + ".tmp";
                File.WriteAllText(
                    temporaryPath,
                    JsonSerializer.Serialize(records, HistoryJsonOptions),
                    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
                File.Move(temporaryPath, HistoryPath, overwrite: true);
            }
        }
        catch (Exception exception) when (exception is IOException or
                                              UnauthorizedAccessException or
                                              JsonException or
                                              NotSupportedException)
        {
            AppLogService.Warn(
                $"保存下载历史失败：{exception.GetType().Name}：{exception.Message}",
                exception);
        }
    }

    private static DownloadHistoryRecord ToRecord(DownloadTaskViewModel task) => new()
    {
        AppId = task.AppId,
        AppName = task.AppName,
        AppCategory = task.Category,
        SourceId = task.App.SourceId ?? string.Empty,
        SourceName = task.App.SourceName ?? string.Empty,
        AppVersion = task.App.Version,
        IconUrl = task.App.IconUrl,
        IconFallbackUrl = task.App.IconFallbackUrl,
        IconText = task.App.IconText,
        IconBackground = task.App.IconBackground,
        AppSizeBytes = task.App.SizeBytes,
        Version = task.Version,
        FileName = task.FileName,
        FilePath = task.FilePath ?? string.Empty,
        DownloadUrl = task.DownloadUrl,
        ReleaseId = task.App.LatestRelease?.Id ?? string.Empty,
        BytesReceived = task.BytesReceived,
        TotalBytes = task.TotalBytes,
        Status = task.Phase.ToString(),
        Message = task.StatusMessage,
        StartedAt = task.StartedAt,
        HasSignatureInfo = task.HasSignatureInfo,
        SignatureIsValid = task.SignatureIsValid,
        SignatureStatusText = task.SignatureStatusText,
        SignatureDetailText = task.SignatureDetailText
    };

    private static string DescribeError(Exception exception) => exception switch
    {
        HttpRequestException requestException when requestException.StatusCode.HasValue
            => $"获取下载地址失败（HTTP {(int)requestException.StatusCode.Value}），请稍后重试或访问软件官网。",
        HttpRequestException => "获取下载地址失败，请检查网络连接后重试。",
        UnauthorizedAccessException => "无法写入下载目录，请检查目录权限后重试。",
        InvalidDataException => $"获取下载地址失败：{exception.Message}",
        NotSupportedException => $"获取下载地址失败：{exception.Message}",
        TimeoutException => exception.Message,
        JsonException => "获取下载地址失败：安装包信息格式无效，请稍后重试。",
        _ => "下载失败，请检查可用磁盘空间后重试。"
    };

    private static Task OnUiAsync(Action action)
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is null || dispatcher.HasShutdownStarted || dispatcher.CheckAccess())
        {
            action();
            return Task.CompletedTask;
        }
        return dispatcher.InvokeAsync(action).Task;
    }

    private static void RaiseStatusChanged()
    {
        var dispatcher = Application.Current?.Dispatcher;
        if (dispatcher is not null && !dispatcher.HasShutdownStarted && !dispatcher.CheckAccess())
        {
            _ = dispatcher.BeginInvoke(new Action(() => StatusChanged?.Invoke(null, EventArgs.Empty)));
            return;
        }
        StatusChanged?.Invoke(null, EventArgs.Empty);
    }

    private sealed class UiProgress<T>(Action<T> report) : IProgress<T>
    {
        public void Report(T value) => _ = OnUiAsync(() => report(value));
    }
}
