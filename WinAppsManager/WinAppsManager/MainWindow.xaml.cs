using System.Windows;
using System.ComponentModel;
using System.Diagnostics;
using System.Windows.Input;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;
using iNKORE.UI.WPF.Helpers;
using iNKORE.UI.WPF.Modern.Controls;
using iNKORE.UI.WPF.Modern.Controls.Helpers;
using iNKORE.UI.WPF.Modern.Controls.Primitives;
using iNKORE.UI.WPF.Modern.Helpers.Styles;
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
    /// 详情页可能含截图相册，每个页面会持有解码后的截图；不设上限时浏览几百款软件会持续占用数百 MB 内存。
    /// </summary>
    private const int MaxCachedDetailPages = 6;

    private readonly AppsInstallPage _appsInstallPage;
    private readonly DownloadCenterPage _downloadCenterPage;
    private UninstallPage? _uninstallPage;
    private StartupPage? _startupPage;
    private SettingsPage? _settingsPage;
    private readonly Dictionary<string, NavigationViewItem> _categoryItems = new(StringComparer.Ordinal);
    private readonly Dictionary<string, NavigationViewItem> _uninstallCategoryItems = new(StringComparer.Ordinal);
    private readonly Dictionary<string, AppDetailsPage> _detailsPages = new(StringComparer.Ordinal);
    private readonly List<string> _detailPageOrder = new();
    private string _categorySignature = string.Empty;
    private bool _isSyncingNavigation;
    private bool _isWaitingForDownloads;
    private bool _downloadsStopped;

    /// <summary>当前停留的顶层页面（详情页不算）：用来判断导航切换的滑动方向。</summary>
    private PageType _currentTopLevelPage = PageType.Install;

    /// <summary>最近一次已经播放过过渡的页面实例，用于忽略重复的导航回调（SelectionChanged + ItemInvoked）。</summary>
    private object? _pageTransitionPlayedFor;

    private const int WmEnterSizeMove = 0x0231;
    private const int WmExitSizeMove = 0x0232;

    /// <summary>当前实际启用的系统背景；null = 没有启用（Windows 7/8、高对比度或初始化失败）。</summary>
    private BackdropType? _activeBackdrop;

    /// <summary>拖动/缩放期间是否临时停用了 Windows 10 旧式亚克力（见 <see cref="SuspendAcrylicForMove"/>）。</summary>
    private bool _acrylicSuspended;

    private HwndSource? _windowSource;

    /// <summary>NavigationView.IsPaneOpen 的变更通知；用于在导航栏收起/展开时处理分类区。</summary>
    private DependencyPropertyDescriptor? _paneOpenDescriptor;
    private DispatcherTimer? _paneOpenRestoreTimer;
    private bool _softwareSectionExpanded = true;
    private bool _uninstallSectionExpanded = true;

    /// <summary>
    /// NavigationView 在紧凑模式（导航栏收起）下，点击带子项的父项会弹出一个浮层来显示子项。
    /// 子项是直接加入 <c>MenuItems</c> 的 <see cref="NavigationViewItem"/> 控件，同一实例被浮层
    /// 的 ItemsRepeater 复用时无法挂载到第二棵视觉树，浮层里只剩空白占位。
    /// 因此收起导航栏时清空子项、展开时再重建（见 <see cref="OnNavigationPaneOpenChanged"/>），
    /// 紧凑模式下的父项只负责跳转，不再弹浮层。
    /// </summary>
    public MainWindow()
    {
        InitializeComponent();
        // 内容铺满整个窗口（见 MainWindow.xaml 的 TitleBar.ExtendViewIntoTitleBar）：
        // 这里把导航视图下移一个标题栏高度，标题栏按钮仍由窗口模板绘制在最上层。
        NavView.Margin = new Thickness(0, TitleBar.GetHeight(this), 0, 0);
        ApplyPreferredBackdrop();
        SystemParameters.StaticPropertyChanged += OnSystemParametersChanged;
        AppSettingsService.SettingsChanged += OnAppSettingsChanged;
        _paneOpenDescriptor = DependencyPropertyDescriptor.FromProperty(
            NavigationView.IsPaneOpenProperty, typeof(NavigationView));
        _paneOpenDescriptor?.AddValueChanged(NavView, OnNavigationPaneOpenChanged);
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

    /// <summary>
    /// 按用户在“设置 → 外观”选择的窗口背景样式（自动 / 无 / 亚克力 / 云母）应用系统背景。
    /// 实际用哪一种由 <see cref="BackdropSupport.Resolve"/> 按系统能力决定：不支持的样式回退到可用样式；
    /// Windows 7/8 与高对比度下不做任何原生调用，窗口保持不透明底色。
    /// 导航栏本身已经是透明的（见 MainWindow.xaml 覆盖的 NavigationViewDefault/ExpandedPaneBackground），
    /// 所以只要窗口底色透明，系统背景就会出现在标题栏与导航栏上。
    /// </summary>
    private void ApplyPreferredBackdrop()
    {
        BackdropStyle resolved = BackdropSupport.Resolve(AppSettingsService.Current.BackdropStyle);
        BackdropType? backdrop = BackdropSupport.ToBackdropType(resolved);
        BackdropType current = WindowHelper.GetSystemBackdropType(this);

        if (backdrop is null)
        {
            _activeBackdrop = null;
            // 枚举里没有 0（None = 1），从未设置过时读到的就是 0。Windows 7/8 从未应用系统背景，
            // 这里不能调用 SetSystemBackdropType，否则会走 Acrylic10Helper 的 P/Invoke。
            if (Enum.IsDefined(typeof(BackdropType), current))
            {
                WindowHelper.SetSystemBackdropType(this, BackdropType.None);
            }
            return;
        }

        _activeBackdrop = backdrop;
        if (current != backdrop.Value)
        {
            WindowHelper.SetSystemBackdropType(this, backdrop.Value);
        }
    }

    /// <summary>设置为其他项（主题、背景图、窗口背景样式等）后重新评估系统背景；与背景无关的改动会被幂等忽略。</summary>
    private void OnAppSettingsChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(ApplyPreferredBackdrop));
            return;
        }

        ApplyPreferredBackdrop();
    }

    /// <summary>
    /// 导航栏“收起（只剩图标）→ 再次展开”后，iNKORE 的 NavigationView 会把分类区留在折叠状态，
    /// 展开后的导航栏只剩 5 个顶级项、下方一大片空白。这里在收起时记住分类区的展开状态，展开时恢复。
    /// </summary>
    private void OnNavigationPaneOpenChanged(object? sender, EventArgs args)
    {
        if (NavView.IsPaneOpen)
        {
            // 等展开动画结束再恢复分类区：在动画中途把 IsExpanded 置为 true，库仍认为处于
            // 紧凑模式，会把子项 repeater 挂到浮层上，展开后子项就“消失”了（只剩空白的分类区）。
            _paneOpenRestoreTimer ??= new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(420) };
            _paneOpenRestoreTimer.Stop();
            _paneOpenRestoreTimer.Tick -= OnPaneOpenRestoreTick;
            _paneOpenRestoreTimer.Tick += OnPaneOpenRestoreTick;
            _paneOpenRestoreTimer.Start();
            return;
        }

        _paneOpenRestoreTimer?.Stop();
        // 收起时先记住展开状态，再把分类区折起来：紧凑模式下库会为仍展开的父项弹子项浮层，
        // 而浮层无法复用已挂载的 NavigationViewItem 实例，只会渲染出空白。
        // 收起状态点击父项由 NavigationView_OnItemInvoked 直接展开导航栏、走内联列表。
        _softwareSectionExpanded = SoftwareNavigationItem.IsExpanded;
        _uninstallSectionExpanded = UninstallNavigationItem.IsExpanded;
        SoftwareNavigationItem.IsExpanded = false;
        UninstallNavigationItem.IsExpanded = false;
    }

    private void OnPaneOpenRestoreTick(object? sender, EventArgs args)
    {
        _paneOpenRestoreTimer?.Stop();
        if (!NavView.IsPaneOpen)
        {
            return;
        }

        SoftwareNavigationItem.IsExpanded = _softwareSectionExpanded;
        UninstallNavigationItem.IsExpanded = _uninstallSectionExpanded;
    }

    protected override void OnClosed(EventArgs e)
    {
        _paneOpenRestoreTimer?.Stop();
        _paneOpenDescriptor?.RemoveValueChanged(NavView, OnNavigationPaneOpenChanged);
        AppSettingsService.SettingsChanged -= OnAppSettingsChanged;
        SystemParameters.StaticPropertyChanged -= OnSystemParametersChanged;
        base.OnClosed(e);
    }

    protected override void OnSourceInitialized(EventArgs e)
    {
        base.OnSourceInitialized(e);
        _windowSource = HwndSource.FromHwnd(new WindowInteropHelper(this).Handle);
        _windowSource?.AddHook(WindowProc);
    }

    /// <summary>只关心系统移动/缩放循环的开始与结束，用来在拖动期间停用 Windows 10 旧式亚克力。</summary>
    private IntPtr WindowProc(IntPtr hwnd, int message, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        switch (message)
        {
            case WmEnterSizeMove:
                SuspendAcrylicForMove();
                break;
            case WmExitSizeMove:
                ResumeAcrylicAfterMove();
                break;
        }

        return IntPtr.Zero;
    }

    /// <summary>
    /// Windows 10 的旧式亚克力（<c>ACCENT_ENABLE_ACRYLICBLURBEHIND</c>）在交互拖动/缩放时由 DWM
    /// 每帧重算整个窗口后面的模糊，WPF 窗口会明显发滞（实测拖动期间 DWM CPU 约翻倍）。
    /// 这里在系统进入移动/缩放循环时暂时移除它、并把窗口切成不透明底色（透明窗口失去亚克力后
    /// 会露出黑色/未模糊的桌面），松手后立即恢复；Windows 11 的 Mica / Acrylic11 是 DWM 原生优化路径，
    /// 不做处理。底色取亚克力的中间色，减小切换时的明暗跳变。
    /// </summary>
    private void SuspendAcrylicForMove()
    {
        if (_acrylicSuspended || _activeBackdrop?.GetActualBackdropType() != BackdropType.Acrylic10)
        {
            return;
        }

        _acrylicSuspended = true;
        Acrylic10Helper.Remove(new WindowInteropHelper(this).Handle);
        SetResourceReference(BackgroundProperty, "Wam.MoveBackgroundBrush");
        WindowHelper.SetApplyBackground(this, true);
    }

    private void ResumeAcrylicAfterMove()
    {
        if (!_acrylicSuspended)
        {
            return;
        }

        _acrylicSuspended = false;
        SetResourceReference(BackgroundProperty, "Wam.PageBackgroundBrush");
        // 清除本地值，交回 TheWindowStyle 的触发器（有系统背景时为透明）。
        ClearValue(WindowHelper.ApplyBackgroundProperty);
        if (_activeBackdrop?.GetActualBackdropType() == BackdropType.Acrylic10)
        {
            BackdropHelper.Apply(this, _activeBackdrop.Value);
        }
    }

    /// <summary>高对比度在运行中切换时重新决定是否启用系统背景（其余系统参数变化忽略）。</summary>
    private void OnSystemParametersChanged(object? sender, PropertyChangedEventArgs args)
    {
        if (args.PropertyName == nameof(SystemParameters.HighContrast))
        {
            ApplyPreferredBackdrop();
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
            if (NavView.SelectedItem is NavigationViewItem selectedItem && TryGetCategoryName(selectedItem, out _))
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
            UiMotion.PlayNavigationItemsIn(_categoryItems.Values);
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
            if (NavView.SelectedItem is NavigationViewItem selectedItem && TryGetUninstallCategoryName(selectedItem, out _))
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
            UiMotion.PlayNavigationItemsIn(_uninstallCategoryItems.Values);
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
                // 收起状态下点击带分类的父项：展开导航栏，让分类（全部/聊天工具/…）可见。
                // 紧凑模式下库原本会弹一个浮层，但浮层无法复用已挂载的子项控件，只会留下空白；
                // 这里直接展开导航栏，并且只展开当前分区，避免另一个分区的分类把当前分类挤出可视区。
                if (!NavView.IsPaneOpen
                    && (ReferenceEquals(item, SoftwareNavigationItem) || ReferenceEquals(item, UninstallNavigationItem)))
                {
                    _softwareSectionExpanded = ReferenceEquals(item, SoftwareNavigationItem);
                    _uninstallSectionExpanded = ReferenceEquals(item, UninstallNavigationItem);
                    NavView.IsPaneOpen = true;
                }

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
        // iNKORE NavigationView 对同一次点击可能先后触发 SelectionChanged 与 ItemInvoked，
        // 导致 ChangePage 被调用两次。第二次直接忽略，否则页面会连着播两遍过渡，
        // 视觉上像是“卡了一下再重播”。
        object? current = GetPageContent(pageType);
        if (current is not null
            && ReferenceEquals(FrameView.Content, current)
            && ReferenceEquals(_pageTransitionPlayedFor, current))
        {
            _currentTopLevelPage = pageType;
            return;
        }

        ShowPageForPageType(pageType);
    }

    /// <summary>把顶层页面放进内容区。页面切换一律交叉淡化，见 <see cref="UiMotion.PlayPageTransition"/>。</summary>
    private void ShowPageForPageType(PageType pageType)
    {
        AppLogService.Info($"切换页面：{GetPageDisplayName(pageType)}（{pageType}）。");
        switch (pageType)
        {
            case PageType.Install:
                ShowPage(_appsInstallPage);
                SetNavigationSelection(SoftwareNavigationItem);
                UpdateCategoryIndicators(_appsInstallPage.SelectedCategory);
                break;
            case PageType.Downloads:
                ShowPage(_downloadCenterPage);
                SetNavigationSelection(DownloadsNavigationItem);
                break;
            case PageType.Uninstall:
                ShowPage(_uninstallPage ??= new UninstallPage());
                SetNavigationSelection(UninstallNavigationItem);
                UpdateUninstallCategoryIndicators(_uninstallPage.SelectedCategory);
                break;
            case PageType.Settings:
                ShowPage(_settingsPage ??= new SettingsPage());
                SetNavigationSelection(SettingsNavigationItem);
                break;
            case PageType.Startup:
                ShowPage(_startupPage ??= new StartupPage());
                SetNavigationSelection(StartupNavigationItem);
                break;
        }

        _currentTopLevelPage = pageType;
    }

    private object? GetPageContent(PageType pageType) => pageType switch
    {
        PageType.Install => _appsInstallPage,
        PageType.Downloads => _downloadCenterPage,
        PageType.Uninstall => _uninstallPage,
        PageType.Startup => _startupPage,
        PageType.Settings => _settingsPage,
        _ => null
    };

    /// <summary>
    /// 把页面放进 <see cref="FrameView"/> 并播放过渡。重复设置同一个页面不重播动画，
    /// 否则切换分类（<see cref="ActivateCategory"/> 会重新调用 <see cref="ChangePage"/>）时列表会莫名闪一下。
    ///
    /// 过渡只做两件事：抓一张**旧页面**快照盖在最上层（它已经离开视觉树，只能靠快照），
    /// 然后让**新页面本身**淡入/滑入。新页面必须是真实控件——早先给新页面也拍快照再滑，
    /// 快照按下时详情页还没加载完，动画播的是一张半空图，播完硬切成真内容，
    /// 用户看到的就是“先平移、然后画面突然变成详情页”。
    /// 帧率由 <see cref="UiMotion.PlayPageTransition"/> 内部的位图缓存保证。
    /// </summary>
    private void ShowPage(object page)
    {
        // FrameView.Content 已是目标页面时仍然继续：连续快速点击导航项会先后触发
        // SelectionChanged 与 ItemInvoked，第二次调用时内容可能已经换好了；
        // 此时只播放过渡、不重复设置内容。
        bool contentAlreadySet = ReferenceEquals(FrameView.Content, page);
        if (contentAlreadySet && _pageTransitionPlayedFor == page)
        {
            return;
        }

        var stopwatch = Stopwatch.StartNew();
        // 上一轮过渡可能还在进行：先收尾，清掉残留的透明度/位图缓存与“切换中”标记。
        UiMotion.CompleteActiveTransition();
        ImageSource? outgoing = UiMotion.CaptureSnapshot(PageHost);
        long captureOutgoingMs = stopwatch.ElapsedMilliseconds;

        // 旧页面快照盖上：新页面在它后面完成首次布局，用户看不到“空白”。
        UiMotion.ShowTransitionPreview(OutgoingSnapshot, outgoing);

        if (!contentAlreadySet)
        {
            FrameView.Content = page;
            PageHost.UpdateLayout();
        }

        long layoutMs = stopwatch.ElapsedMilliseconds - captureOutgoingMs;

        _pageTransitionPlayedFor = page;
        // 帧率统计由 UiMotion.PlayPageTransition 内部启动：它会在打断上一轮过渡后再开始，
        // 提前调用会让统计被下一轮立刻重置（日志里表现为“共 0 帧”）。
        UiMotion.PlayPageTransition(PageHost, OutgoingSnapshot);


        if (AppLogService.MinimumLevel == AppLogLevel.Verbose)
        {
            AppLogService.Verbose(
                $"页面过渡：Fade，旧页面快照={(outgoing is null ? "无" : $"{captureOutgoingMs} ms")}，" +
                $"新页面布局={layoutMs} ms。");
        }
    }

    /// <summary>从详情页返回列表：用“从左侧滑入”的返回方向，和进入详情页的“从右侧滑入”成对。</summary>
    private void ReturnToSoftwareList() => ChangePage(PageType.Install);

    private void ShowAppDetails(AppItem appItem)
    {
        // 详情页按“来源 ID + 应用 ID”缓存：不同软件源可能有相同的应用 ID，不能互相复用页面。
        string detailKey = (appItem.SourceId ?? string.Empty) + ":" + appItem.Id;
        if (_detailsPages.TryGetValue(detailKey, out var detailsPage))
        {
            _detailPageOrder.Remove(detailKey);
            _detailPageOrder.Add(detailKey);
            AppLogService.Verbose($"打开软件详情：{appItem.Name}（{appItem.Id}），复用已缓存的详情页。");
        }
        else
        {
            detailsPage = new AppDetailsPage(appItem);
            detailsPage.BackRequested += (_, _) => ReturnToSoftwareList();
            detailsPage.DownloadCenterRequested += () => ChangePage(PageType.Downloads);
            _detailsPages.Add(detailKey, detailsPage);
            _detailPageOrder.Add(detailKey);
            // 超出上限后按最久未打开的顺序丢弃详情页，避免截图相册在内存里越积越多。
            while (_detailPageOrder.Count > MaxCachedDetailPages)
            {
                string evictedId = _detailPageOrder[0];
                _detailPageOrder.RemoveAt(0);
                _detailsPages.Remove(evictedId);
            }
            AppLogService.Verbose($"打开软件详情：{appItem.Name}（{appItem.Id}），新建详情页。");
        }

        ShowPage(detailsPage);
    }

    private static string GetPageDisplayName(PageType pageType) => pageType switch
    {
        PageType.Install => "软件列表",
        PageType.Downloads => "下载中心",
        PageType.Uninstall => "卸载软件",
        PageType.Startup => "自启动项",
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
            ReturnToSoftwareList();
            e.Handled = true;
        }
    }

    public enum PageType
    {
        Install = 1,
        Settings = 2,
        Downloads = 3,
        Uninstall = 4,
        Startup = 5
    }
}
