using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

public partial class UninstallProgressWindow : Window
{
    private readonly IReadOnlyList<InstalledApplication> _applications;
    private readonly CancellationTokenSource _cancellation = new();
    private bool _started;
    private bool _completed;
    private bool _stopRequested;
    private bool _closeWhenFinished;

    public ObservableCollection<UninstallResult> Results { get; } = new();
    public string Summary { get; private set; } = string.Empty;

    public UninstallProgressWindow(IReadOnlyList<InstalledApplication> applications)
    {
        ArgumentNullException.ThrowIfNull(applications);
        if (applications.Count == 0)
        {
            throw new ArgumentException("至少需要一个待卸载软件。", nameof(applications));
        }

        _applications = applications;
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
    /// Replaces each result verdict with the real registry state, so "卸载成功" means the
    /// software is actually gone instead of "the uninstaller exited with 0".
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

        CurrentText.Text = "正在核对注册表，确认软件是否已被移除…";
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
            Summary += $" 其中 {confirmed} 个已通过注册表确认移除。";
        }

        if (failed > 0)
        {
            Summary += " 请刷新列表确认结果；失败的软件可重试、使用深度清理移除残留，或使用 Windows 设置卸载。";
        }

        SummaryText.Text = Summary;
        CurrentText.Text = _stopRequested ? "卸载流程已停止。" : "全部卸载程序已结束。";
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

    private void StopButton_OnClick(object sender, RoutedEventArgs args) => RequestStop();

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
            $"用户请求停止卸载批次：已产生 {Results.Count} / {_applications.Count} 个结果，正在终止卸载程序及其子进程。");
        _cancellation.Cancel();
    }

    private void CloseButton_OnClick(object sender, RoutedEventArgs args) => Close();

    private void Window_OnClosing(object? sender, CancelEventArgs args)
    {
        if (_completed)
        {
            return;
        }

        // Closing the window stops the batch instead of being blocked forever.
        args.Cancel = true;
        _closeWhenFinished = true;
        RequestStop();
    }
}
