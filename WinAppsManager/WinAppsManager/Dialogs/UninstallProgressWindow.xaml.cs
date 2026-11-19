using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

public partial class UninstallProgressWindow : Window
{
    /// <summary>
    /// Some uninstallers commit their last registry writes slightly after the process tree exits;
    /// a short pause keeps the list refresh from reading the state in between.
    /// </summary>
    private static readonly TimeSpan VerificationSettleDelay = TimeSpan.FromMilliseconds(400);

    private readonly IReadOnlyList<InstalledApplication> _applications;
    private readonly Func<CancellationToken, Task<IReadOnlyList<InstalledApplication>>>? _refreshInstalledApplications;
    private readonly CancellationTokenSource _cancellation = new();
    private bool _started;
    private bool _completed;
    private bool _stopRequested;
    private bool _closeWhenFinished;
    private bool _verifiedByRefresh;

    public ObservableCollection<UninstallResult> Results { get; } = new();
    public string Summary { get; private set; } = string.Empty;

    /// <param name="applications">待卸载软件，按列表顺序逐个执行。</param>
    /// <param name="refreshInstalledApplications">
    /// 卸载流程结束后重新读取一次“已安装软件”列表（桌面注册表 + 商店应用）的委托。传入时
    /// 卸载结果按刷新后的列表判定（软件是否还存在），确认移除后由调用方清理残留；为
    /// <c>null</c> 或刷新失败时回退到 <see cref="UninstallVerifier"/> 的注册表/包核对。
    /// </param>
    public UninstallProgressWindow(
        IReadOnlyList<InstalledApplication> applications,
        Func<CancellationToken, Task<IReadOnlyList<InstalledApplication>>>? refreshInstalledApplications = null)
    {
        ArgumentNullException.ThrowIfNull(applications);
        if (applications.Count == 0)
        {
            throw new ArgumentException("至少需要一个待卸载软件。", nameof(applications));
        }

        _applications = applications;
        _refreshInstalledApplications = refreshInstalledApplications;
        InitializeComponent();
        DataContext = this;
        OverallProgress.Maximum = applications.Count;
        ProgressCountText.Text = $"0 / {applications.Count}";
    }

    private async void Window_OnLoaded(object sender, RoutedEventArgs args)
    {
        if (_started)
        {
            return;
        }

        _started = true;
        UiMotion.PlayDialogOpen(this);
        AppLogService.Info(
            $"卸载进度窗口开始执行：{_applications.Count} 个软件" +
            $"（桌面软件 {_applications.Count(application => !application.IsStoreApplication)}，" +
            $"商店应用 {_applications.Count(application => application.IsStoreApplication)}）。");
        try
        {
            for (int index = 0; index < _applications.Count; index++)
            {
                var application = _applications[index];
                if (_cancellation.IsCancellationRequested)
                {
                    Results.Add(UninstallResult.CreateCanceled(application, "已跳过：用户停止了本批卸载。"));
                    OverallProgress.Value = index + 1;
                    ProgressCountText.Text = $"{index + 1} / {_applications.Count}";
                    continue;
                }

                CurrentText.Text = $"正在卸载（{index + 1}/{_applications.Count}）：{application.Name}";
                ProgressCountText.Text = $"{index} / {_applications.Count}";
                UninstallResult result;
                try
                {
                    result = await UninstallRunner.UninstallAsync(application, _cancellation.Token);
                }
                catch (OperationCanceledException)
                {
                    result = UninstallResult.CreateCanceled(application, "已停止卸载：卸载程序已终止。");
                }
                catch (Exception exception)
                {
                    Debug.WriteLine(exception);
                    AppLogService.Error(
                        $"卸载 {application.Name}（{application.Id}）时出现未预期错误，已标记为失败。",
                        exception);
                    result = UninstallResult.CreateFailed(
                        application,
                        "启动卸载程序时发生意外错误，请使用 Windows 设置中的卸载入口。");
                }

                Results.Add(result);
                if (result.Status == UninstallStatus.Failed)
                {
                    AppLogService.Warn(
                        $"卸载失败：{application.Name}（{application.Id}），{result.DetailText}。");
                }
                else if (result.Status == UninstallStatus.Canceled)
                {
                    // 停止批次时剩余条目会逐个记为“已跳过”，只写详细日志避免一次刷出很多行。
                    AppLogService.Verbose(
                        $"卸载已取消：{application.Name}（{application.Id}），{result.Message}。");
                }

                OverallProgress.Value = index + 1;
                ProgressCountText.Text = $"{index + 1} / {_applications.Count}";
                if (index + 1 < _applications.Count && !_cancellation.IsCancellationRequested)
                {
                    // Give installers that return early a moment to commit their registry changes.
                    try
                    {
                        await Task.Delay(250, _cancellation.Token);
                    }
                    catch (OperationCanceledException)
                    {
                        // Stopping is handled at the top of the next iteration.
                    }
                }
            }

            if (!_cancellation.IsCancellationRequested)
            {
                await VerifyResultsAsync();
            }
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            AppLogService.Error("执行卸载批次时出现未预期错误，批次已提前结束。", exception);
        }
        finally
        {
            CompleteBatchUi();
        }

        if (_closeWhenFinished)
        {
            Close();
        }
    }

    /// <summary>
    /// Replaces each result verdict with the real state of the machine, so "卸载成功" means the
    /// software is actually gone instead of "the uninstaller exited with 0". The primary source is
    /// a fresh scan of the installed-software list the page itself shows: an application that is
    /// absent from that snapshot no longer exists, no matter which exit code the vendor uninstaller
    /// returned. The per-application registry/package check stays as the fallback when the refresh
    /// cannot be performed.
    /// </summary>
    private async Task VerifyResultsAsync()
    {
        var pending = Results
            .Where(result => result.Status != UninstallStatus.Canceled && result.RegistryRemoved is null)
            .ToArray();
        if (pending.Length == 0)
        {
            return;
        }

        if (_refreshInstalledApplications is not null &&
            await TryApplyRefreshVerificationAsync(pending))
        {
            return;
        }

        await VerifyResultsAgainstSystemStateAsync(pending);
    }

    /// <summary>
    /// Refreshes the installed-software list once for the whole batch and marks every item whose
    /// identity no longer appears in it as removed.
    /// </summary>
    /// <returns>
    /// <c>true</c> when the refresh produced a verdict for every pending item (including "stopped
    /// by the user"), <c>false</c> when the refresh failed and the caller has to fall back.
    /// </returns>
    private async Task<bool> TryApplyRefreshVerificationAsync(IReadOnlyList<UninstallResult> pending)
    {
        CurrentText.Text = "正在刷新已安装软件列表，确认软件是否已被移除…";
        IReadOnlyList<InstalledApplication> snapshot;
        try
        {
            await Task.Delay(VerificationSettleDelay, _cancellation.Token);
            snapshot = await _refreshInstalledApplications!(_cancellation.Token);
        }
        catch (OperationCanceledException) when (_cancellation.IsCancellationRequested)
        {
            // The user stopped the batch while it was being verified: keep what is already shown.
            return true;
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            AppLogService.Warn(
                "卸载流程结束后刷新已安装软件列表失败，改为核对注册表/包状态判定结果。" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
            return false;
        }

        if (!CoversBatchKinds(snapshot))
        {
            // A scan that lost a whole kind of entries is a failed read, not a machine where
            // everything is gone. Never turn "could not read the list" into "已卸载".
            AppLogService.Warn(
                "卸载后刷新的安装列表不完整（刷新后 " +
                $"{snapshot.Count} 项，桌面软件 {snapshot.Count(application => !application.IsStoreApplication)} 项、" +
                $"商店应用 {snapshot.Count(application => application.IsStoreApplication)} 个），" +
                "改为逐个核对注册表/包状态判定结果。");
            return false;
        }

        var installedKeys = BuildInstalledIdentityKeys(snapshot);
        int removed = 0;
        int stillInstalledCount = 0;
        foreach (var result in pending)
        {
            bool stillInstalled = IsStillPresent(result.App, installedKeys);
            result.ApplyVerification(stillInstalled);
            if (stillInstalled)
            {
                stillInstalledCount++;
            }
            else
            {
                removed++;
            }
        }

        _verifiedByRefresh = true;
        AppLogService.Info(
            $"已按刷新后的软件列表判定卸载结果：刷新后共 {snapshot.Count} 项，本次核对 {pending.Count} 个，" +
            $"确认移除 {removed} 个，仍在列表中 {stillInstalledCount} 个。");
        return true;
    }

    /// <summary>
    /// The refreshed list has to contain at least one entry of every kind the batch contained
    /// (desktop / Store). Both providers swallow per-entry errors and return what they read, so an
    /// empty section means the scan failed rather than that all those applications were removed.
    /// </summary>
    private bool CoversBatchKinds(IReadOnlyList<InstalledApplication> snapshot)
    {
        bool snapshotHasDesktop = snapshot.Any(application => !application.IsStoreApplication);
        bool snapshotHasStore = snapshot.Any(application => application.IsStoreApplication);
        bool batchHasDesktop = _applications.Any(application => !application.IsStoreApplication);
        bool batchHasStore = _applications.Any(application => application.IsStoreApplication);
        return (!batchHasDesktop || snapshotHasDesktop) && (!batchHasStore || snapshotHasStore);
    }

    /// <summary>
    /// Builds the identity keys of one refreshed list so a result can be looked up by the same
    /// identifiers the provider assigned: <see cref="InstalledApplication.Id"/> first, then the
    /// package full name (Store apps) or the uninstall registry path + name (desktop apps), which
    /// still match when an entry moved between registry hives/views or the provider id changed.
    /// </summary>
    private static HashSet<string> BuildInstalledIdentityKeys(IReadOnlyList<InstalledApplication> applications)
    {
        var keys = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach (var application in applications)
        {
            AddIdentityKey(keys, "id", application.Id);
            if (application.IsStoreApplication)
            {
                AddIdentityKey(keys, "package", application.PackageFullName);
                AddIdentityKey(keys, "package-name", application.PackageName);
            }
            else
            {
                AddIdentityKey(keys, "registry", application.RegistryPath);
                if (application.RegistryPath.Length > 0)
                {
                    AddIdentityKey(keys, "registry-name", $"{application.RegistryPath}\u001f{application.Name}");
                }
            }
        }

        return keys;
    }

    private static void AddIdentityKey(HashSet<string> keys, string kind, string value)
    {
        if (value.Length > 0)
        {
            keys.Add($"{kind}\u001f{value}");
        }
    }

    /// <summary>
    /// <c>true</c> when the refreshed list still contains this application under any of its
    /// identities; a false result means the software is no longer installed.
    /// </summary>
    private static bool IsStillPresent(InstalledApplication application, HashSet<string> installedKeys)
    {
        if (installedKeys.Contains($"id\u001f{application.Id}"))
        {
            return true;
        }

        if (application.IsStoreApplication)
        {
            return (application.PackageFullName.Length > 0 &&
                    installedKeys.Contains($"package\u001f{application.PackageFullName}")) ||
                   (application.PackageName.Length > 0 &&
                    installedKeys.Contains($"package-name\u001f{application.PackageName}"));
        }

        if (application.RegistryPath.Length == 0)
        {
            return false;
        }

        return installedKeys.Contains($"registry\u001f{application.RegistryPath}") ||
               installedKeys.Contains(
                   $"registry-name\u001f{application.RegistryPath}\u001f{application.Name}");
    }

    /// <summary>
    /// Fallback used when no refresh delegate is available or the refreshed list could not be read:
    /// check each application against its registry key / package directly.
    /// </summary>
    private async Task VerifyResultsAgainstSystemStateAsync(IReadOnlyList<UninstallResult> pending)
    {
        CurrentText.Text = "正在核对注册表与包状态，确认软件是否已被移除…";
        foreach (var result in pending)
        {
            bool? stillInstalled;
            try
            {
                stillInstalled = await Task.Run(
                    () => UninstallVerifier.IsStillInstalled(result.App),
                    _cancellation.Token);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            catch (Exception exception)
            {
                Debug.WriteLine(exception);
                AppLogService.Warn(
                    $"核对 {result.App.Name}（{result.App.Id}）是否真正卸载失败，结果保持未核对。",
                    exception);
                continue;
            }

            result.ApplyVerification(stillInstalled);
        }
    }

    private void CompleteBatchUi()
    {
        _completed = true;
        int completed = Results.Count(result => result.Status == UninstallStatus.Completed);
        int failed = Results.Count(result => result.Status == UninstallStatus.Failed);
        int canceled = Results.Count(result => result.Status == UninstallStatus.Canceled);
        int confirmed = Results.Count(result => result.RegistryRemoved == true);
        Summary = _stopRequested
            ? $"卸载已停止。已完成 {completed} 个，失败 {failed} 个，已取消 {canceled} 个。"
            : $"已完成 {completed} 个，失败 {failed} 个，已取消 {canceled} 个。";
        if (confirmed > 0)
        {
            Summary += _verifiedByRefresh
                ? $" 其中 {confirmed} 个已通过刷新列表确认移除。"
                : $" 其中 {confirmed} 个已通过注册表确认移除。";
        }

        if (failed > 0)
        {
            Summary += " 失败的软件可重试、使用强制卸载清理残留，或使用 Windows 设置卸载。";
        }

        SummaryText.Text = Summary;
        CurrentText.Text = _stopRequested ? "卸载流程已停止。" : "全部卸载程序已结束，并已按刷新后的软件列表核对结果。";
        TitleText.Text = _stopRequested ? "卸载已停止" : "卸载流程结束";
        BusyRing.IsActive = false;
        BusyRing.Visibility = Visibility.Collapsed;
        CompleteIcon.Visibility = Visibility.Visible;
        StopButton.Visibility = Visibility.Collapsed;
        CloseButton.Visibility = Visibility.Visible;
        AppLogService.Info($"卸载批次汇总：{Summary}");

        if (!_closeWhenFinished)
        {
            CloseButton.Focus();
        }
    }

    private void StopButton_OnClick(object sender, RoutedEventArgs args) => ConfirmStop();

    /// <summary>
    /// Cancelling a batch is destructive: the current vendor uninstaller may already have removed
    /// files or registry entries. Ask once before terminating the process tree, and reuse the same
    /// prompt when the user closes the progress window.
    /// </summary>
    private bool ConfirmStop()
    {
        if (_completed || _stopRequested)
        {
            return false;
        }

        MessageBoxResult answer = MessageBox.Show(
            this,
            "停止卸载会立即终止当前厂商卸载程序及其子进程。\n\n" +
            "软件可能停留在部分卸载的状态，之后需要重新安装或手动清理残留。\n\n" +
            "确定要停止本批卸载吗？",
            "确认停止卸载",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning,
            MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes)
        {
            AppLogService.Info("用户取消了“停止卸载”操作，批次继续执行。");
            return false;
        }

        RequestStop();
        return true;
    }

    private void RequestStop()
    {
        if (_completed || _stopRequested)
        {
            return;
        }

        _stopRequested = true;
        StopButton.IsEnabled = false;
        StopButton.Content = "正在停止…";
        SummaryText.Text = "正在终止卸载程序及其子进程，请稍候…";
        CurrentText.Text = "正在停止卸载…";
        AppLogService.Info(
            $"用户确认停止卸载批次：已产生 {Results.Count} / {_applications.Count} 个结果，正在终止卸载程序及其子进程。");
        _cancellation.Cancel();
    }

    private void CloseButton_OnClick(object sender, RoutedEventArgs args) => Close();

    private void Window_OnClosing(object? sender, CancelEventArgs args)
    {
        if (_completed)
        {
            return;
        }

        // Closing the window stops the batch instead of being blocked forever, but only after the
        // user explicitly confirms the destructive cancellation.
        args.Cancel = true;
        if (!ConfirmStop())
        {
            return;
        }

        _closeWhenFinished = true;
    }
}
