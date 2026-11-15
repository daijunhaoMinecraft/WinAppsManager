using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text.Json;
using System.Windows;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// Owns all install downloads so they keep running independently of the details
/// page and can be observed from the download center.
/// </summary>
public static class DownloadCenterService
{
    private static readonly ObservableCollection<DownloadTaskViewModel> Items = new();
    private static readonly ReadOnlyObservableCollection<DownloadTaskViewModel> ReadOnlyItems = new(Items);
    private static readonly object Gate = new();

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
        var removed = Items.Remove(task);
        if (removed)
        {
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
        foreach (var task in finished)
        {
            Items.Remove(task);
        }
        if (finished.Length > 0)
        {
            RaiseStatusChanged();
        }
        return finished.Length;
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
            RaiseStatusChanged();
        }
    }

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
