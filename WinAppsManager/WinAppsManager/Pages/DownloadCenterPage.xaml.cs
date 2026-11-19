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
        int deletableInstallers = downloads.Count(task => task.CanSelectForDeletion);
        int selectedInstallers = downloads.Count(task => task.CanSelectForDeletion && task.IsSelected);

        EmptyState.Visibility = hasTasks ? Visibility.Collapsed : Visibility.Visible;
        DownloadsScrollViewer.Visibility = hasTasks ? Visibility.Visible : Visibility.Collapsed;
        CancelAllButton.Visibility = active > 0 ? Visibility.Visible : Visibility.Collapsed;
        ClearFinishedButton.Visibility = finished > 0 ? Visibility.Visible : Visibility.Collapsed;
        SelectAllInstallersButton.Visibility = deletableInstallers > 0
            ? Visibility.Visible
            : Visibility.Collapsed;
        DeleteInstallersButton.Visibility = deletableInstallers > 0
            ? Visibility.Visible
            : Visibility.Collapsed;
        DeleteInstallersButton.IsEnabled = selectedInstallers > 0;
        DeleteInstallersButton.Content = selectedInstallers > 0
            ? $"删除选中安装包 ({selectedInstallers})"
            : "删除选中安装包";
        SelectAllInstallersButton.Content = deletableInstallers > 0 &&
                                           selectedInstallers == deletableInstallers
            ? "取消全选"
            : "全选安装包";

        ActiveSummaryText.Text = !hasTasks
            ? "当前没有下载任务。"
            : active > 0
                ? $"{active} 个任务正在下载，{finished} 个已结束。" +
                  (deletableInstallers > 0 ? $" 可删除安装包 {deletableInstallers} 个。" : string.Empty)
                : $"{finished} 个已结束的下载任务。" +
                  (deletableInstallers > 0 ? $" 可删除安装包 {deletableInstallers} 个。" : string.Empty);
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
        bool select = Downloads.Any(task => task.CanSelectForDeletion && !task.IsSelected);
        foreach (var task in Downloads.Where(task => task.CanSelectForDeletion))
        {
            task.IsSelected = select;
        }

        AppLogService.Info(
            $"下载中心{(select ? "全选" : "取消全选")}安装包：可删除 {Downloads.Count(task => task.CanSelectForDeletion)} 个。");
        UpdateChrome();
    }

    private void DeleteInstallersButton_OnClick(object sender, RoutedEventArgs args)
    {
        var selected = Downloads
            .Where(task => task.CanSelectForDeletion && task.IsSelected)
            .ToArray();
        if (selected.Length == 0)
        {
            return;
        }

        string fileList = string.Join("\n", selected
            .Take(10)
            .Select(task => string.IsNullOrWhiteSpace(task.FileName) ? task.AppName : task.FileName));
        if (selected.Length > 10)
        {
            fileList += $"\n… 以及另外 {selected.Length - 10} 个";
        }

        Window? owner = Window.GetWindow(this);
        string message = $"将永久删除以下 {selected.Length} 个安装包文件，删除后无法恢复：\n\n{fileList}";
        MessageBoxResult answer = owner is null
            ? MessageBox.Show(message, "删除安装包", MessageBoxButton.YesNo,
                MessageBoxImage.Warning, MessageBoxResult.No)
            : MessageBox.Show(owner, message, "删除安装包", MessageBoxButton.YesNo,
                MessageBoxImage.Warning, MessageBoxResult.No);
        if (answer != MessageBoxResult.Yes)
        {
            return;
        }

        InstallerDeleteOutcome outcome = DownloadCenterService.DeleteInstallers(selected);
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
