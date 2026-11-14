using System.Windows;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows.Input;
using iNKORE.UI.WPF.Modern.Controls;
using WinAppsManager.Models;
using WinAppsManager.Pages;
using WinAppsManager.Services;

namespace WinAppsManager;

public partial class MainWindow : Window
{
    private const string CategoryTagPrefix = "category:";
    private const string UninstallCategoryTagPrefix = "uninstall-category:";

    /// <summary>
    /// 详情页会缓存最近打开的少量页面（返回/再次打开更快），但必须限制数量：
    /// 联想商店详情页含截图相册，每个页面会持有解码后的截图；不设上限时浏览几百款软件会持续占用数百 MB 内存。
    /// </summary>
    private const int MaxCachedDetailPages = 6;

    private readonly AppsInstallPage _appsInstallPage;
    private readonly DownloadCenterPage _downloadCenterPage;
    private UninstallPage? _uninstallPage;
    private SettingsPage? _settingsPage;
    private readonly Dictionary<string, NavigationViewItem> _categoryItems = new(StringComparer.Ordinal);
    private readonly Dictionary<string, NavigationViewItem> _uninstallCategoryItems = new(StringComparer.Ordinal);
    private readonly Dictionary<string, AppDetailsPage> _detailsPages = new(StringComparer.Ordinal);
    private readonly List<string> _detailPageOrder = new();
    private string _categorySignature = string.Empty;
    private bool _isSyncingNavigation;
    private bool _isWaitingForDownloads;
    private bool _downloadsStopped;

    public MainWindow()
    {
        InitializeComponent();
        _appsInstallPage = new AppsInstallPage();
        _appsInstallPage.AppDetailsRequested += ShowAppDetails;
        _appsInstallPage.CategoriesChanged += OnCategoriesChanged;
        _appsInstallPage.CategoryChanged += OnCategoryChanged;
        _downloadCenterPage = new DownloadCenterPage();
        _downloadCenterPage.SoftwareListRequested += () => ChangePage(PageType.Install);
        _uninstallPage = new UninstallPage();
        _uninstallPage.CategoriesChanged += OnUninstallCategoriesChanged;
        _uninstallPage.CategoryChanged += OnUninstallCategoryChanged;
        NavView.SelectedItem = SoftwareNavigationItem;
        Loaded += MainWindow_OnLoaded;
    }

    /// <summary>窗口与默认页面加载完成后记录一次启动摘要（版本/运行时由 AppLogService 启动行负责）。</summary>
    private void MainWindow_OnLoaded(object sender, RoutedEventArgs args)
    {
        try
        {
            AppLogService.Info(
                $"主窗口启动完成：默认页面={GetPageDisplayName(PageType.Install)}，软件源={AppCatalogService.SourceDisplayName}，" +
                $"服务端={AppCatalogService.BackendBaseUrl}，日志设置={DescribeLoggingSettings()}。");
        }
        catch (Exception exception)
        {
            AppLogService.Warn("记录主窗口启动摘要失败。", exception);
        }
    }

    private static string DescribeLoggingSettings() => !AppLogService.Enabled
        ? "不写文件"
        : AppLogService.MinimumLevel == AppLogLevel.Verbose ? "Verbose" : "Info";

    private void OnCategoriesChanged(IReadOnlyList<CategoryOption> categories)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnCategoriesChanged(categories)));
            return;
        }

        _isSyncingNavigation = true;
        try
        {
            var signature = string.Join('\u001f', categories.Select(category => category.Name));
            if (signature == _categorySignature)
            {
                return;
            }

            // The navigation view's selection model cannot safely observe a child
            // collection reset while that child is selected.
            if (_categoryItems.Values.Contains(NavView.SelectedItem))
            {
                NavView.SelectedItem = SoftwareNavigationItem;
            }

            SoftwareNavigationItem.MenuItems.Clear();
            _categoryItems.Clear();
            foreach (var category in categories)
            {
                var item = new NavigationViewItem
                {
                    Content = category.Name,
                    Tag = CategoryTagPrefix + category.Name,
                    SelectsOnInvoked = false
                };
                _categoryItems[category.Name] = item;
                SoftwareNavigationItem.MenuItems.Add(item);
            }
            _categorySignature = signature;
        }
        catch (Exception exception)
        {
            // 分类重建失败只影响导航子项，不阻塞页面；记录后继续走 finally 恢复同步标记。
            AppLogService.Error($"重建软件分类导航失败（{categories.Count} 个分类）。", exception);
        }
        finally
        {
            _isSyncingNavigation = false;
        }

        if (ReferenceEquals(FrameView.Content, _appsInstallPage))
        {
            UpdateCategoryIndicators(_appsInstallPage.SelectedCategory);
        }
    }

    private void OnCategoryChanged(string category)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnCategoryChanged(category)));
            return;
        }
        UpdateCategoryIndicators(category);
    }

    private void UpdateCategoryIndicators(string category)
        => ApplyCategoryIndicators(_categoryItems, category);

    private void UpdateUninstallCategoryIndicators(string category)
        => ApplyCategoryIndicators(_uninstallCategoryItems, category);

    private static void ApplyCategoryIndicators(
        IReadOnlyDictionary<string, NavigationViewItem> items,
        string category)
    {
        foreach (var (name, item) in items)
        {
            bool active = name == category;
            item.FontWeight = active ? FontWeights.SemiBold : FontWeights.Normal;
            // Keep the category text on a dynamic resource reference. Assigning a Brush here
            // froze the light-theme colour, which made the labels unreadable after switching
            // to dark mode or high contrast.
            item.SetResourceReference(
                ForegroundProperty,
                active ? "Wam.AccentBrush" : "Wam.TextPrimaryBrush");
        }
    }

    private void OnUninstallCategoriesChanged(IReadOnlyList<CategoryOption> categories)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnUninstallCategoriesChanged(categories)));
            return;
        }

        _isSyncingNavigation = true;
        try
        {
            if (_uninstallCategoryItems.Values.Contains(NavView.SelectedItem))
            {
                NavView.SelectedItem = UninstallNavigationItem;
            }

            UninstallNavigationItem.MenuItems.Clear();
            _uninstallCategoryItems.Clear();
            foreach (var category in categories)
            {
                var item = new NavigationViewItem
                {
                    Content = category.Name,
                    Tag = UninstallCategoryTagPrefix + category.Name,
                    SelectsOnInvoked = false
                };
                _uninstallCategoryItems[category.Name] = item;
                UninstallNavigationItem.MenuItems.Add(item);
            }
        }
        catch (Exception exception)
        {
            // 卸载分类子项重建失败时保留现有导航，只记录异常。
            AppLogService.Error($"重建卸载分类导航失败（{categories.Count} 个分类）。", exception);
        }
        finally
        {
            _isSyncingNavigation = false;
        }

        if (ReferenceEquals(FrameView.Content, _uninstallPage) && _uninstallPage is not null)
        {
            UpdateUninstallCategoryIndicators(_uninstallPage.SelectedCategory);
        }
    }

    private void OnUninstallCategoryChanged(string category)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnUninstallCategoryChanged(category)));
            return;
        }

        UpdateUninstallCategoryIndicators(category);
    }

    private void ActivateUninstallCategory(string category)
    {
        _uninstallPage?.SelectCategory(category);
        ChangePage(PageType.Uninstall);
    }

    private void SetNavigationSelection(NavigationViewItem item)
    {
        if (ReferenceEquals(NavView.SelectedItem, item))
        {
            return;
        }

        _isSyncingNavigation = true;
        try
        {
            NavView.SelectedItem = item;
        }
        finally
        {
            _isSyncingNavigation = false;
        }
    }

    private static bool TryGetCategoryName(NavigationViewItem item, out string category)
    {
        if (item.Tag is string tag && tag.StartsWith(CategoryTagPrefix, StringComparison.Ordinal))
        {
            category = tag[CategoryTagPrefix.Length..];
            return true;
        }
        category = string.Empty;
        return false;
    }

    private static bool TryGetUninstallCategoryName(NavigationViewItem item, out string category)
    {
        if (item.Tag is string tag && tag.StartsWith(UninstallCategoryTagPrefix, StringComparison.Ordinal))
        {
            category = tag[UninstallCategoryTagPrefix.Length..];
            return true;
        }
        category = string.Empty;
        return false;
    }

    private void NavigationView_OnSelectionChanged(NavigationView sender, NavigationViewSelectionChangedEventArgs args)
    {
        if (_isSyncingNavigation)
        {
            return;
        }

        if (args.SelectedItem is NavigationViewItem selectedItem)
        {
            if (TryGetCategoryName(selectedItem, out var category))
            {
                ActivateCategory(category);
                return;
            }
            if (TryGetUninstallCategoryName(selectedItem, out var uninstallCategory))
            {
                ActivateUninstallCategory(uninstallCategory);
                return;
            }
            if (Enum.TryParse<PageType>(selectedItem.Tag?.ToString(), out var pageType))
            {
                ChangePage(pageType);
            }
        }
    }

    private void NavigationView_OnItemInvoked(NavigationView sender, NavigationViewItemInvokedEventArgs args)
    {
        if (_isSyncingNavigation)
        {
            return;
        }

        if (args.InvokedItemContainer is NavigationViewItem item)
        {
            if (TryGetCategoryName(item, out var category))
            {
                ActivateCategory(category);
                return;
            }
            if (TryGetUninstallCategoryName(item, out var uninstallCategory))
            {
                ActivateUninstallCategory(uninstallCategory);
                return;
            }
            if (Enum.TryParse<PageType>(item.Tag?.ToString(), out var pageType))
            {
                ChangePage(pageType);
            }
        }
    }

    private void ActivateCategory(string category)
    {
        _appsInstallPage.SelectCategory(category);
        ChangePage(PageType.Install);
    }

    public void ChangePage(PageType pageType)
    {
        AppLogService.Info($"切换页面：{GetPageDisplayName(pageType)}（{pageType}）。");
        switch (pageType)
        {
            case PageType.Install:
                FrameView.Content = _appsInstallPage;
                SetNavigationSelection(SoftwareNavigationItem);
                UpdateCategoryIndicators(_appsInstallPage.SelectedCategory);
                break;
            case PageType.Downloads:
                FrameView.Content = _downloadCenterPage;
                SetNavigationSelection(DownloadsNavigationItem);
                break;
            case PageType.Uninstall:
                FrameView.Content = _uninstallPage ??= new UninstallPage();
                SetNavigationSelection(UninstallNavigationItem);
                UpdateUninstallCategoryIndicators(_uninstallPage.SelectedCategory);
                break;
            case PageType.Settings:
                FrameView.Content = _settingsPage ??= new SettingsPage();
                SetNavigationSelection(SettingsNavigationItem);
                break;
        }
    }

    private void ShowAppDetails(AppItem appItem)
    {
        if (_detailsPages.TryGetValue(appItem.Id, out var detailsPage))
        {
            _detailPageOrder.Remove(appItem.Id);
            _detailPageOrder.Add(appItem.Id);
            AppLogService.Verbose($"打开软件详情：{appItem.Name}（{appItem.Id}），复用已缓存的详情页。");
        }
        else
        {
            detailsPage = new AppDetailsPage(appItem);
            detailsPage.BackRequested += (_, _) => ChangePage(PageType.Install);
            detailsPage.DownloadCenterRequested += () => ChangePage(PageType.Downloads);
            _detailsPages.Add(appItem.Id, detailsPage);
            _detailPageOrder.Add(appItem.Id);
            // 超出上限后按最久未打开的顺序丢弃详情页，避免截图相册在内存里越积越多。
            while (_detailPageOrder.Count > MaxCachedDetailPages)
            {
                string evictedId = _detailPageOrder[0];
                _detailPageOrder.RemoveAt(0);
                _detailsPages.Remove(evictedId);
            }
            AppLogService.Verbose($"打开软件详情：{appItem.Name}（{appItem.Id}），新建详情页。");
        }

        FrameView.Content = detailsPage;
    }

    private static string GetPageDisplayName(PageType pageType) => pageType switch
    {
        PageType.Install => "软件列表",
        PageType.Downloads => "下载中心",
        PageType.Uninstall => "卸载软件",
        PageType.Settings => "设置",
        _ => pageType.ToString()
    };

    protected override async void OnClosing(CancelEventArgs e)
    {
        AppLogService.Info($"关闭主窗口：活动下载 {DownloadCenterService.ActiveCount} 个。");
        base.OnClosing(e);
        if (e.Cancel || _downloadsStopped)
        {
            return;
        }

        if (DownloadCenterService.ActiveCount == 0)
        {
            return;
        }

        e.Cancel = true;
        if (_isWaitingForDownloads)
        {
            return;
        }

        _isWaitingForDownloads = true;
        int activeBeforeCancel = DownloadCenterService.ActiveCount;
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Info($"正在取消 {activeBeforeCancel} 个活动下载，完成后关闭窗口。");
        IsEnabled = false;
        try
        {
            await DownloadCenterService.CancelAllAsync();
            AppLogService.Info(
                $"关闭窗口前已取消并等待 {activeBeforeCancel} 个活动下载结束，剩余活动任务 {DownloadCenterService.ActiveCount} 个，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception)
        {
            AppLogService.Error(
                $"关闭窗口时取消 {activeBeforeCancel} 个活动下载失败，已等待 {stopwatch.ElapsedMilliseconds} ms。",
                exception);
        }
        finally
        {
            _downloadsStopped = true;
            _ = Dispatcher.BeginInvoke(new Action(Close));
        }
    }

    private void MainWindow_OnPreviewKeyDown(object sender, KeyEventArgs e)
    {
        if (FrameView.Content is not AppDetailsPage)
        {
            return;
        }

        if (e.Key == Key.Escape || (e.SystemKey == Key.Left && Keyboard.Modifiers == ModifierKeys.Alt))
        {
            ChangePage(PageType.Install);
            e.Handled = true;
        }
    }

    public enum PageType
    {
        Install = 1,
        Settings = 2,
        Downloads = 3,
        Uninstall = 4
    }
}
