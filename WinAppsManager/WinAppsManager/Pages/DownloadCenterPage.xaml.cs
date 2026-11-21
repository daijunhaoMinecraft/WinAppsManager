using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class DownloadCenterPage : Page
{
    private CancellationTokenSource? _iconLoadCancellation;

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

        _iconLoadCancellation?.Cancel();
        _iconLoadCancellation = new CancellationTokenSource();
        _ = LoadTaskIconsAsync(_iconLoadCancellation.Token);
    }

    private void Page_OnUnloaded(object sender, RoutedEventArgs args)
    {
        DownloadCenterService.StatusChanged -= OnDownloadStatusChanged;
        _iconLoadCancellation?.Cancel();
        _iconLoadCancellation?.Dispose();
        _iconLoadCancellation = null;
    }

    /// <summary>
    /// 历史记录只保存图标的远端地址；重启后需要把它解析成本地图标缓存路径，否则卡片会一直显示文字徽章。
    /// </summary>
    private async Task LoadTaskIconsAsync(CancellationToken cancellationToken)
    {
        try
        {
            foreach (var task in Downloads.ToArray())
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!string.IsNullOrWhiteSpace(task.App.DisplayIconSource) ||
                    (string.IsNullOrWhiteSpace(task.App.IconUrl) &&
                     string.IsNullOrWhiteSpace(task.App.IconFallbackUrl)))
                {
                    continue;
                }

                await AppCatalogService.EnsureIconAsync(task.App, cancellationToken);
            }
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
        }
        catch (Exception exception)
        {
            AppLogService.Warn($"加载下载中心图标失败：{exception.GetType().Name}：{exception.Message}");
        }
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
        int selectable = downloads.Count(task => task.CanSelect);
        int selected = downloads.Count(task => task.CanSelect && task.IsSelected);
        int resumable = downloads.Count(task => task.CanResume);

        EmptyState.Visibility = hasTasks ? Visibility.Collapsed : Visibility.Visible;
        DownloadsScrollViewer.Visibility = hasTasks ? Visibility.Visible : Visibility.Collapsed;
        CancelAllButton.Visibility = active > 0 ? Visibility.Visible : Visibility.Collapsed;
        ClearFinishedButton.Visibility = finished > 0 ? Visibility.Visible : Visibility.Collapsed;
        SelectAllInstallersButton.Visibility = selectable > 0
            ? Visibility.Visible
            : Visibility.Collapsed;
        DeleteInstallersButton.Visibility = selectable > 0
            ? Visibility.Visible
            : Visibility.Collapsed;
        DeleteInstallersButton.IsEnabled = selected > 0;
        DeleteInstallersButton.Content = selected > 0
            ? $"删除选中 ({selected})"
            : "删除选中";
        SelectAllInstallersButton.Content = selectable > 0 && selected == selectable
            ? "取消全选"
            : "全选";

        ActiveSummaryText.Text = !hasTasks
            ? "当前没有下载任务。"
            : $"{active} 个任务正在下载，{finished} 个已结束。" +
              (resumable > 0 ? $" 其中 {resumable} 个可以继续下载。" : string.Empty);
    }

    private void InstallerSelection_OnChanged(object sender, RoutedEventArgs args)
    {
        if (IsLoaded)
        {
            UpdateChrome();
        }
    }

    private void SelectAllInstallersButton_OnClick(object sender, RoutedEventArgs args)
    {
        bool select = Downloads.Any(task => task.CanSelect && !task.IsSelected);
        foreach (var task in Downloads.Where(task => task.CanSelect))
        {
            task.IsSelected = select;
        }

        AppLogService.Info(
            $"下载中心{(select ? "全选" : "取消全选")}下载任务：可勾选 {Downloads.Count(task => task.CanSelect)} 个。");
        UpdateChrome();
    }

    /// <summary>
    /// 删除选中的下载任务：正在下载的会先取消（取消会终止连接），已完成的删安装包文件，
    /// 未完成的连续传分片一起删掉，然后统一移除记录。
    /// </summary>
    private async void DeleteInstallersButton_OnClick(object sender, RoutedEventArgs args)
    {
        var selected = Downloads
            .Where(task => task.CanSelect && task.IsSelected)
            .ToArray();
        if (selected.Length == 0)
        {
            return;
        }

        var activeTasks = selected.Where(task => task.IsActive).ToArray();
        var fileTasks = selected.Where(task => task.CanOpenInstaller).ToArray();

        string itemList = string.Join("\n", selected.Take(10).Select(DescribeTask));
        if (selected.Length > 10)
        {
            itemList += $"\n… 以及另外 {selected.Length - 10} 个";
        }

        string message =
            $"将移除 {selected.Length} 条下载记录" +
            (fileTasks.Length > 0 ? $"，永久删除 {fileTasks.Length} 个已下载的安装包" : string.Empty) +
            (activeTasks.Length > 0 ? $"，并停止 {activeTasks.Length} 个正在进行的下载" : string.Empty) +
            "；未完成的续传分片也会一起删除，删除后无法恢复：\n\n" + itemList;

        Window? owner = Window.GetWindow(this);
        MessageBoxResult answer = owner is null
            ? MessageBox.Show(message, "删除下载任务", MessageBoxButton.YesNo,
                MessageBoxImage.Warning, MessageBoxResult.No)
            : MessageBox.Show(owner, message, "删除下载任务", MessageBoxButton.YesNo,
                MessageBoxImage.Warning, MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        if (activeTasks.Length > 0)
        {
            foreach (var task in activeTasks)
            {
                DownloadCenterService.Cancel(task);
            }

            await Task.WhenAll(activeTasks.Select(task => task.Completion));
        }

        InstallerDeleteOutcome outcome = DownloadCenterService.DeleteInstallers(selected);
        AppLogService.Info(
            $"删除选中下载任务：选中 {selected.Length} 个，删除文件 {outcome.DeletedFiles} 个，" +
            $"移除记录 {outcome.RemovedRecords} 条，失败 {outcome.Errors.Count} 个。");
        UpdateChrome();
        if (outcome.Errors.Count > 0)
        {
            string errorMessage =
                $"有 {outcome.Errors.Count} 个安装包无法删除，记录已保留，可关闭占用程序后重试：\n\n" +
                string.Join("\n", outcome.Errors.Take(10));
            if (owner is null)
            {
                MessageBox.Show(errorMessage, "部分安装包无法删除", MessageBoxButton.OK,
                    MessageBoxImage.Warning);
            }
            else
            {
                MessageBox.Show(owner, errorMessage, "部分安装包无法删除", MessageBoxButton.OK,
                    MessageBoxImage.Warning);
            }
        }
    }

    private static string DescribeTask(DownloadTaskViewModel task) => task.CanOpenInstaller
        ? $"{task.AppName}（安装包：{task.FileName}）"
        : $"{task.AppName}（{task.StatusText}）";

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
