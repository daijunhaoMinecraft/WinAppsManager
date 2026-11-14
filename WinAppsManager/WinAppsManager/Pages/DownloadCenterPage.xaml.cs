using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class DownloadCenterPage : Page
{
    public ReadOnlyObservableCollection<DownloadTaskViewModel> Downloads => DownloadCenterService.Downloads;

    public event Action? SoftwareListRequested;

    public DownloadCenterPage()
    {
        InitializeComponent();
        DataContext = this;
    }

    private void Page_OnLoaded(object sender, RoutedEventArgs args)
    {
        DownloadCenterService.StatusChanged += OnDownloadStatusChanged;
        UpdateChrome();
    }

    private void Page_OnUnloaded(object sender, RoutedEventArgs args)
    {
        DownloadCenterService.StatusChanged -= OnDownloadStatusChanged;
    }

    private void OnDownloadStatusChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnDownloadStatusChanged(sender, args)));
            return;
        }
        UpdateChrome();
    }

    private void UpdateChrome()
    {
        var downloads = DownloadCenterService.Downloads;
        var active = downloads.Count(task => task.IsActive);
        var finished = downloads.Count(task => !task.IsActive);
        bool hasTasks = downloads.Count > 0;

        EmptyState.Visibility = hasTasks ? Visibility.Collapsed : Visibility.Visible;
        DownloadsScrollViewer.Visibility = hasTasks ? Visibility.Visible : Visibility.Collapsed;
        CancelAllButton.Visibility = active > 0 ? Visibility.Visible : Visibility.Collapsed;
        ClearFinishedButton.Visibility = finished > 0 ? Visibility.Visible : Visibility.Collapsed;
        ActiveSummaryText.Text = !hasTasks
            ? "当前没有下载任务。"
            : active > 0
                ? $"{active} 个任务正在下载，{finished} 个已结束。"
                : $"{finished} 个已结束的下载任务。";
    }

    private async void CancelAllButton_OnClick(object sender, RoutedEventArgs args)
    {
        int active = DownloadCenterService.ActiveCount;
        AppLogService.Info($"用户取消全部下载任务：活动 {active} 个。");
        CancelAllButton.IsEnabled = false;
        try
        {
            await DownloadCenterService.CancelAllAsync();
            AppLogService.Info($"全部下载任务取消请求已完成：取消前活动 {active} 个。");
        }
        catch (Exception exception)
        {
            AppLogService.Error("取消全部下载任务失败。", exception);
        }
        finally
        {
            CancelAllButton.IsEnabled = true;
            UpdateChrome();
        }
    }

    private void ClearFinishedButton_OnClick(object sender, RoutedEventArgs args)
    {
        int removed = DownloadCenterService.ClearFinished();
        AppLogService.Info($"用户清理已结束的下载任务：移除 {removed} 个。");
        UpdateChrome();
    }

    private void OpenDownloadFolderButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (DownloadCenterService.OpenDownloadFolder())
        {
            AppLogService.Info($"用户打开下载文件夹：{AppSettingsService.ResolvedDownloadDirectory}。");
        }
        else
        {
            AppLogService.Warn(
                $"打开下载文件夹失败：{AppSettingsService.ResolvedDownloadDirectory}，目录无法创建、不存在或没有访问权限。");
        }
    }

    private void CancelTaskButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: DownloadTaskViewModel task })
        {
            AppLogService.Info($"用户取消下载任务：{task.AppName}（{task.AppId}），状态={task.StatusText}。");
            DownloadCenterService.Cancel(task);
        }
    }

    private void OpenInstallerButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: DownloadTaskViewModel task })
        {
            if (DownloadCenterService.OpenInstaller(task))
            {
                AppLogService.Info($"用户打开安装包：{task.AppName}（{task.AppId}），文件={task.FilePath}。");
            }
            else
            {
                AppLogService.Warn(
                    $"打开安装包失败：{task.AppName}（{task.AppId}），文件={(string.IsNullOrWhiteSpace(task.FilePath) ? "未知" : task.FilePath)}，" +
                    "文件可能已被移动、删除或没有执行权限。");
            }
        }
    }

    private void RetryTaskButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: DownloadTaskViewModel task })
        {
            AppLogService.Info($"用户重试下载任务：{task.AppName}（{task.AppId}），原状态={task.StatusText}。");
            DownloadCenterService.Restart(task);
            UpdateChrome();
        }
    }

    private void RemoveTaskButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: DownloadTaskViewModel task })
        {
            bool removed = DownloadCenterService.Remove(task);
            AppLogService.Info(
                $"用户移除下载任务：{task.AppName}（{task.AppId}），状态={task.StatusText}，结果={(removed ? "已移除" : "任务仍在进行，未移除")}。");
            UpdateChrome();
        }
    }

    private void BrowseSoftwareButton_OnClick(object sender, RoutedEventArgs args) => SoftwareListRequested?.Invoke();

    private async void AppIcon_OnImageFailed(object sender, ExceptionRoutedEventArgs args)
    {
        if (sender is Image { DataContext: DownloadTaskViewModel task, Parent: Border imageContainer })
        {
            imageContainer.ClearValue(VisibilityProperty);
            await AppCatalogService.DiscardIconAsync(task.App);
        }
        args.Handled = true;
    }

    private void IconContainer_OnDataContextChanged(object sender, DependencyPropertyChangedEventArgs args)
    {
        if (sender is Border imageContainer)
        {
            imageContainer.ClearValue(VisibilityProperty);
        }
    }
}
