using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using WinAppsManager.Dialogs;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class UninstallPage : Page, INotifyPropertyChanged
{
    private readonly StringComparer _nameComparer = StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);
    private IReadOnlyList<InstalledApplication> _allApplications = Array.Empty<InstalledApplication>();
    private CancellationTokenSource? _scanCancellation;
    private CancellationTokenSource? _iconLoadCancellation;
    private Task? _scanTask;
    private bool _isPageActive;
    private bool _isLoading;
    private bool _hasLoaded;
    private bool _suppressSelectionEvents;
    private string? _loadError;
    private string? _statusText;
    private string? _storeLoadError;
    private string _selectedCategory = UninstallCategories[0];
    private string _categorySignature = string.Empty;

    private ObservableCollection<InstalledApplication> _apps = new();


    /// <summary>
    /// Replaced as a whole on every filter/sort change: a single reset is far smoother than
    /// hundreds of incremental Add notifications.
    /// </summary>
    public ObservableCollection<InstalledApplication> Apps
    {
        get => _apps;
        private set
        {
            if (ReferenceEquals(_apps, value))
            {
                return;
            }

            _apps = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(Apps)));
        }
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    public UninstallPage()
    {
        InitializeComponent();
        DataContext = this;
        DisplayModeCombo.SelectedIndex = 0;
        SortCombo.SelectedIndex = 0;
        UpdateSortIndicators();
    }

    /// <summary>The four local uninstall list categories.</summary>
    public static readonly string[] UninstallCategories = ["应用程序", "系统组件", "隐藏组件", "商店应用"];

    public string SelectedCategory => _selectedCategory;

    public event Action<IReadOnlyList<CategoryOption>>? CategoriesChanged;

    public event Action<string>? CategoryChanged;

    /// <summary>Switches the list to one of the four uninstall categories.</summary>
    public void SelectCategory(string category)
    {
        string normalized = UninstallCategories.Contains(category) ? category : UninstallCategories[0];
        if (_selectedCategory == normalized)
        {
            return;
        }

        _selectedCategory = normalized;
        _restartMetadataPass = true;
        ApplyFilter();
        CategoryChanged?.Invoke(_selectedCategory);
    }

    private async void OnPageLoaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = true;
        await LoadApplicationsAsync();
    }

    private void OnPageUnloaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = false;
        _scanCancellation?.Cancel();
        _iconLoadCancellation?.Cancel();
        _scanTask = null;
    }

    private async void RefreshButton_OnClick(object sender, RoutedEventArgs args)
    {
        _statusText = null;
        await LoadApplicationsAsync();
    }

    private Task LoadApplicationsAsync()
    {
        if (_scanTask is { IsCompleted: false })
        {
            return _scanTask;
        }

        _scanTask = LoadApplicationsCoreAsync();
        return _scanTask;
    }

    private async Task LoadApplicationsCoreAsync()
    {
        using var cancellation = new CancellationTokenSource();
        _scanCancellation = cancellation;
        var scanStopwatch = Stopwatch.StartNew();
        _isLoading = true;
        _loadError = null;
        _storeLoadError = null;
        UpdateChrome();
        AppLogService.Info($"开始扫描已安装软件：读取 Windows 卸载注册表，当前分类={_selectedCategory}。");

        try
        {
            var registryStopwatch = Stopwatch.StartNew();
            IReadOnlyList<InstalledApplication> desktopApplications =
                await InstalledApplicationProvider.GetInstalledApplicationsAsync(cancellation.Token);
            AppLogService.Info(
                $"卸载注册表扫描完成：{desktopApplications.Count} 条注册表软件，耗时 {registryStopwatch.ElapsedMilliseconds} ms。");

            IReadOnlyList<InstalledApplication> storeApplications;
            if (!StoreApplicationProvider.IsSupported)
            {
                // Windows 7 / 8 没有 Microsoft Store / MSIX 应用，明确提示而不是静默失败。
                storeApplications = Array.Empty<InstalledApplication>();
                _storeLoadError = StoreApplicationProvider.UnsupportedReason;
                AppLogService.Info($"跳过商店应用扫描：{StoreApplicationProvider.UnsupportedReason}");
            }
            else
            {
                var storeStopwatch = Stopwatch.StartNew();
                AppLogService.Info("开始读取当前用户的 Microsoft Store/MSIX 应用。");
                try
                {
                    storeApplications = await StoreApplicationProvider
                        .GetInstalledStoreApplicationsAsync(cancellation.Token);
                    AppLogService.Info(
                        $"商店应用扫描完成：{storeApplications.Count} 个包，耗时 {storeStopwatch.ElapsedMilliseconds} ms。");
                }
                catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception exception)
                {
                    Debug.WriteLine(exception);
                    storeApplications = Array.Empty<InstalledApplication>();
                    _storeLoadError = "商店应用读取失败";
                    AppLogService.Warn(
                        $"商店应用扫描失败：{exception.GetType().Name}：{exception.Message}，列表将只显示桌面软件。",
                        exception);
                }
            }

            if (cancellation.IsCancellationRequested || !_isPageActive)
            {
                AppLogService.Verbose($"已安装软件扫描结果已丢弃：页面未激活或请求已取消，耗时 {scanStopwatch.ElapsedMilliseconds} ms。");
                return;
            }

            var applications = desktopApplications.Concat(storeApplications).ToArray();
            var selectedIds = _allApplications
                .Where(application => application.IsSelected)
                .Select(application => application.Id)
                .ToHashSet(StringComparer.Ordinal);
            _allApplications = applications;
            foreach (var application in _allApplications)
            {
                application.IsSelected = selectedIds.Contains(application.Id);
            }

            _hasLoaded = true;
            _restartMetadataPass = true;
            ApplyFilter();
            AppLogService.Info(
                $"已安装软件扫描完成：共 {_allApplications.Count} 项（应用程序 {_allApplications.Count(item => item.IsApplication)} · " +
                $"系统组件 {_allApplications.Count(item => item.IsSystemComponent)} · " +
                $"隐藏组件 {_allApplications.Count(item => item.IsHiddenComponent)} · " +
                $"商店应用 {_allApplications.Count(item => item.IsStoreApplication)}），耗时 {scanStopwatch.ElapsedMilliseconds} ms。");

            // The registry scan leaves a lot of short-lived strings behind. One compacting
            // collection here returns those segments to the OS instead of holding them for
            // the rest of the session (measured ~10 MB of private bytes).
            await Task.Run(BclCompat.CompactCollect);
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            // Navigating away cancels the scan; the next Loaded event starts a fresh one.
            AppLogService.Verbose($"已安装软件扫描已取消：耗时 {scanStopwatch.ElapsedMilliseconds} ms。");
        }
        catch (Exception exception)
        {
            _loadError = _hasLoaded
                ? "刷新 Windows 卸载信息失败，列表中仍显示上次读取的结果。"
                : "无法读取 Windows 卸载信息，请稍后重试。";
            AppLogService.Error(
                $"扫描已安装软件失败：{exception.GetType().Name}：{exception.Message}，" +
                $"耗时 {scanStopwatch.ElapsedMilliseconds} ms，当前{( _hasLoaded ? "保留上次结果" : "没有可显示的结果")}。",
                exception);
        }
        finally
        {
            if (ReferenceEquals(_scanCancellation, cancellation))
            {
                _scanCancellation = null;
                _isLoading = false;
                ApplyFilter();
            }
        }
    }

    private void SearchBox_OnTextChanged(object sender, TextChangedEventArgs args) => ApplyFilter();



    private string _sortField = "Name";
    private bool _sortDescending;
    private bool _syncingSort;
    private string _displayMode = "Report";
    private readonly HashSet<InstalledApplication> _pendingIcons = new();
    private bool _iconLoading;
    private bool _sortRefreshScheduled;
    private bool _restartMetadataPass = true;

    private void SortCombo_OnSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_syncingSort)
        {
            return;
        }

        _sortField = (SortCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "Name";
        UpdateSortIndicators();
        ApplyFilter();
    }

    private void SortDirection_OnClick(object sender, RoutedEventArgs args)
    {
        _sortDescending = !_sortDescending;
        UpdateSortIndicators();
        ApplyFilter();
    }

    /// <summary>Clicking a report header sorts by that column and toggles its direction.</summary>
    private void ReportHeader_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is not Button { Tag: string field } || field.Length == 0)
        {
            return;
        }

        if (string.Equals(field, _sortField, StringComparison.Ordinal))
        {
            _sortDescending = !_sortDescending;
        }
        else
        {
            _sortField = field;
            // Size/date read best largest/newest first; text columns read best A→Z.
            _sortDescending = field is "Size" or "Date";
        }

        _syncingSort = true;
        try
        {
            foreach (var item in SortCombo.Items.OfType<ComboBoxItem>())
            {
                if (string.Equals(item.Tag as string, _sortField, StringComparison.Ordinal))
                {
                    SortCombo.SelectedItem = item;
                    break;
                }
            }
        }
        finally
        {
            _syncingSort = false;
        }

        UpdateSortIndicators();
        ApplyFilter();
    }

    /// <summary>Draws the ▲/▼ marker on the active report column and on the direction button.</summary>
    private void UpdateSortIndicators()
    {
        if (SortDirectionButton is not null)
        {
            SortDirectionButton.Content = _sortDescending ? "↓" : "↑";
            SortDirectionButton.ToolTip = _sortDescending
                ? "当前：降序（点击切换为升序）"
                : "当前：升序（点击切换为降序）";
        }

        SetHeaderText(HeaderName, "名称", "Name");
        SetHeaderText(HeaderPublisher, "发布者", "Publisher");
        SetHeaderText(HeaderSize, "大小", "Size");
        SetHeaderText(HeaderDate, "安装日期", "Date");
    }

    private void SetHeaderText(Button? header, string label, string field)
    {
        if (header is null)
        {
            return;
        }

        header.Content = string.Equals(field, _sortField, StringComparison.Ordinal)
            ? $"{label} {(_sortDescending ? "▼" : "▲")}"
            : label;
    }

    private void DisplayMode_OnSelectionChanged(object sender, SelectionChangedEventArgs args) => ApplyDisplayMode();

    /// <summary>
    /// Swaps the row template only: the data, filters and selection all stay untouched.
    /// Report mode also shows the column headers, which is where its sorting comes from.
    /// </summary>
    private void ApplyDisplayMode()
    {
        if (AppsList is null)
        {
            return;
        }

        _displayMode = (DisplayModeCombo.SelectedItem as ComboBoxItem)?.Tag as string ?? "Report";
        AppsList.ItemTemplate = (DataTemplate)FindResource(_displayMode switch
        {
            "Card" => "CardItemTemplate",
            "List" => "ListItemTemplate",
            _ => "ReportItemTemplate"
        });
        ReportHeader.Visibility = _displayMode == "Report" ? Visibility.Visible : Visibility.Collapsed;
    }

    private IEnumerable<InstalledApplication> ApplySort(IEnumerable<InstalledApplication> applications)
    {
        // Rows with an unknown value always go to the bottom, in both directions: sorting by
        // date ascending otherwise put every "no date yet" row on top, which read as a bug.
        int direction = _sortDescending ? -1 : 1;
        switch (_sortField)
        {
            case "Size":
                return applications
                    .OrderBy(application => application.EstimatedSizeBytes is > 0 ? 0 : 1)
                    .ThenBy(application => application.EstimatedSizeBytes is > 0
                        ? direction * (application.EstimatedSizeBytes ?? 0)
                        : 0)
                    .ThenBy(application => application.Name, _nameComparer);
            case "Date":
                return applications
                    .OrderBy(application => application.InstallDate is not null ? 0 : 1)
                    .ThenBy(application => application.InstallDate is { } date
                        ? direction * date.DayNumber
                        : 0)
                    .ThenBy(application => application.Name, _nameComparer);
            case "Publisher":
                var byPublisher = applications
                    .OrderBy(application => application.Publisher.Length > 0 ? 0 : 1);
                return (_sortDescending
                        ? byPublisher.ThenByDescending(application => application.Publisher, _nameComparer)
                        : byPublisher.ThenBy(application => application.Publisher, _nameComparer))
                    .ThenBy(application => application.Name, _nameComparer);
            default:
                return _sortDescending
                    ? applications.OrderByDescending(application => application.Name, _nameComparer)
                    : applications.OrderBy(application => application.Name, _nameComparer);
        }
    }

    private void ApplyFilter()
    {
        if (!_hasLoaded)
        {
            UpdateChrome();
            return;
        }

        string query = SearchBox.Text.Trim();
        string category = SelectedCategory;
        string[] terms = query.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        IEnumerable<InstalledApplication> matches = _allApplications.Where(application =>
            MatchesCategory(application, category) && MatchesSearch(application, terms));
        var ordered = ApplySort(matches).ToArray();

        Apps = new ObservableCollection<InstalledApplication>(ordered);

        // Only a new set of rows restarts the metadata pass. Restarting it on every sort or
        // search result would cancel the running scan and start over from the first row forever.
        if (_isPageActive && _restartMetadataPass)
        {
            _restartMetadataPass = false;
            BeginLoadIcons(ordered);
        }

        bool showEmpty = Apps.Count == 0;
        EmptyState.Visibility = showEmpty ? Visibility.Visible : Visibility.Collapsed;
        AppsList.Visibility = showEmpty ? Visibility.Collapsed : Visibility.Visible;
        EmptyTitleText.Text = query.Length > 0 ? "没有匹配的已安装软件" : $"没有找到{_selectedCategory}";
        EmptyDescriptionText.Text = query.Length > 0
            ? "试试其他关键词，或在左侧切换到其他分类。"
            : _selectedCategory == UninstallCategories[3]
                ? "当前用户没有可显示的商店应用，或商店应用读取失败。"
                : "这个分类下没有条目，可以在左侧切换到其他分类。";
        UpdateChrome();
    }

    private static bool MatchesCategory(InstalledApplication application, string category) =>
        category.Length == 0 || string.Equals(application.CategoryText, category, StringComparison.Ordinal);

    private static bool MatchesSearch(InstalledApplication application, string[] terms)
    {
        if (terms.Length == 0)
        {
            return true;
        }

        string[] fields =
        [
            application.Name,
            application.Publisher,
            application.Version,
            application.InstallLocation,
            application.SourceText,
            application.CategoryText
        ];
        return terms.All(term => fields.Any(field =>
            field.Contains(term, StringComparison.OrdinalIgnoreCase)));
    }

    private void UpdateChrome()
    {
        LoadingPanel.Visibility = _isLoading ? Visibility.Visible : Visibility.Collapsed;
        ErrorPanel.Visibility = _loadError is null ? Visibility.Collapsed : Visibility.Visible;
        ErrorText.Text = _loadError ?? string.Empty;
        StatusPanel.Visibility = _statusText is null ? Visibility.Collapsed : Visibility.Visible;
        StatusText.Text = _statusText ?? string.Empty;
        if (!_hasLoaded)
        {
            AppsList.Visibility = Visibility.Collapsed;
        }

        RefreshButton.IsEnabled = !_isLoading;
        SelectAllCheckBox.IsEnabled = Apps.Count > 0 && !_isLoading;

        int selectedCount = _allApplications.Count(application => application.IsSelected);
        SelectedCountText.Text = $"已选择 {selectedCount} 个";
        ClearSelectionButton.Visibility = selectedCount > 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateUninstallAvailability(selectedCount);
        DeepCleanButton.IsEnabled = selectedCount > 0 && !_isLoading;

        bool allVisibleSelected = Apps.Count > 0 && Apps.All(application => application.IsSelected);
        _suppressSelectionEvents = true;
        try
        {
            SelectAllCheckBox.IsChecked = allVisibleSelected;
        }
        finally
        {
            _suppressSelectionEvents = false;
        }

        PageTitleText.Text = $"已安装软件 · {_selectedCategory}";
        PublishCategories();
        int applicationCount = _allApplications.Count(item => item.IsApplication);
        int systemCount = _allApplications.Count(item => item.IsSystemComponent);
        int hiddenCount = _allApplications.Count(item => item.IsHiddenComponent);
        int storeCount = _allApplications.Count(item => item.IsStoreApplication);
        CountText.Text = _isLoading && !_hasLoaded
            ? "正在读取本机的卸载信息…"
            : !_hasLoaded
                ? "读取失败，请重试。"
                : _allApplications.Count == 0
                    ? "没有发现可卸载的软件。"
                    : $"共 {_allApplications.Count} 款（应用程序 {applicationCount} · 系统组件 {systemCount} · 隐藏组件 {hiddenCount} · 商店应用 {storeCount}），当前显示 {Apps.Count} 款{StoreErrorSuffix}";
    }

    /// <summary>
    /// Hidden components never have a vendor uninstaller, so the normal uninstall entry point is
    /// hidden for them (in the hidden tab and for a mixed selection). Only "force uninstall"
    /// (leftover cleanup) stays available, which removes the registration key itself.
    /// </summary>
    private void UpdateUninstallAvailability(int selectedCount)
    {
        bool hiddenSelected = _allApplications.Any(item => item.IsSelected && item.IsHiddenComponent);
        bool hiddenCategory = string.Equals(
            _selectedCategory, UninstallCategories[2], StringComparison.Ordinal);
        UninstallButton.Content = selectedCount > 0 ? $"卸载所选 ({selectedCount})" : "卸载所选";
        UninstallButton.IsEnabled = selectedCount > 0 && !_isLoading && !hiddenSelected;
        UninstallButton.Visibility = hiddenCategory || hiddenSelected
            ? Visibility.Collapsed
            : Visibility.Visible;
    }

    private string StoreErrorSuffix => _storeLoadError is null ? string.Empty : $" · {_storeLoadError}";

    /// <summary>Publishes the four categories and their counts so the navigation can show them.</summary>
    private void PublishCategories()
    {
        var categories = new List<CategoryOption>
        {
            new(UninstallCategories[0], _allApplications.Count(item => item.IsApplication)),
            new(UninstallCategories[1], _allApplications.Count(item => item.IsSystemComponent)),
            new(UninstallCategories[2], _allApplications.Count(item => item.IsHiddenComponent)),
            new(UninstallCategories[3], _allApplications.Count(item => item.IsStoreApplication))
        };
        string signature = string.Join('|', categories.Select(category => $"{category.Name}:{category.Count}"));
        if (signature == _categorySignature)
        {
            return;
        }

        _categorySignature = signature;
        CategoriesChanged?.Invoke(categories);
    }


    /// <summary>
    /// Icons are extracted only for rows that actually appear. With virtualization that is a
    /// dozen instead of the whole category, which keeps the icon cache (and the visual tree) small.
    /// </summary>
    private void ApplicationRow_OnLoaded(object sender, RoutedEventArgs args)
    {
        if (sender is not FrameworkElement { DataContext: InstalledApplication application })
        {
            return;
        }

        if (application.HasIcon || application.DisplayIcon.Length == 0)
        {
            return;
        }

        if (!_pendingIcons.Add(application))
        {
            return;
        }

        _ = LoadQueuedIconsAsync();
    }


    private async Task LoadQueuedIconsAsync()
    {
        if (_iconLoading)
        {
            return;
        }

        _iconLoading = true;
        try
        {
            while (_pendingIcons.Count > 0)
            {
                var application = _pendingIcons.First();
                _pendingIcons.Remove(application);
                if (application.HasIcon || application.DisplayIcon.Length == 0)
                {
                    continue;
                }

                ImageSource? icon;
                try
                {
                    icon = await Task.Run(
                        () => InstalledApplicationIconService.GetIcon(application.DisplayIcon));
                }
                catch (Exception exception)
                {
                    Debug.WriteLine(exception);
                    continue;
                }

                if (icon is not null)
                {
                    application.IconSource = icon;
                }

                await System.Windows.Threading.Dispatcher.Yield(
                    System.Windows.Threading.DispatcherPriority.Background);
            }
        }
        finally
        {
            _iconLoading = false;
        }
    }

    private void SelectionCheckBox_OnChanged(object sender, RoutedEventArgs args)
    {
        if (_suppressSelectionEvents)
        {
            return;
        }

        if (sender is CheckBox { DataContext: InstalledApplication application } checkBox)
        {
            application.IsSelected = checkBox.IsChecked == true;
        }

        RefreshSelectionSummary();
    }


    /// <summary>
    /// Cheap update used while checkboxes are being realised: it never rebuilds the list or the
    /// header text, which kept the first render of a long list from stuttering.
    /// </summary>
    private void RefreshSelectionSummary()
    {
        int selectedCount = _allApplications.Count(application => application.IsSelected);
        SelectedCountText.Text = $"已选择 {selectedCount} 个";
        ClearSelectionButton.Visibility = selectedCount > 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateUninstallAvailability(selectedCount);
        DeepCleanButton.IsEnabled = selectedCount > 0 && !_isLoading;

        bool allVisibleSelected = Apps.Count > 0 && Apps.All(application => application.IsSelected);
        _suppressSelectionEvents = true;
        try
        {
            SelectAllCheckBox.IsChecked = allVisibleSelected;
        }
        finally
        {
            _suppressSelectionEvents = false;
        }
    }

    private void SelectAllCheckBox_OnChanged(object sender, RoutedEventArgs args)
    {
        if (_suppressSelectionEvents)
        {
            return;
        }

        bool select = SelectAllCheckBox.IsChecked == true;
        _suppressSelectionEvents = true;
        try
        {
            foreach (var application in Apps)
            {
                application.IsSelected = select;
            }
        }
        finally
        {
            _suppressSelectionEvents = false;
        }

        UpdateChrome();
    }

    private void ClearSelectionButton_OnClick(object sender, RoutedEventArgs args)
    {
        _suppressSelectionEvents = true;
        try
        {
            foreach (var application in _allApplications)
            {
                application.IsSelected = false;
            }
        }
        finally
        {
            _suppressSelectionEvents = false;
        }

        UpdateChrome();
    }

    private void ApplicationRow_OnMouseLeftButtonUp(object sender, MouseButtonEventArgs args)
    {
        if (IsInsideButton(args.OriginalSource as DependencyObject))
        {
            return;
        }

        if (sender is FrameworkElement { DataContext: InstalledApplication application })
        {
            application.IsSelected = !application.IsSelected;
            UpdateChrome();
            args.Handled = true;
        }
    }

    private async void UninstallButton_OnClick(object sender, RoutedEventArgs args)
    {
        var selected = _allApplications.Where(application => application.IsSelected).ToArray();
        await RunUninstallAsync(selected);
    }

    private async Task RunUninstallAsync(IReadOnlyList<InstalledApplication> applications)
    {
        if (applications.Count == 0)
        {
            return;
        }

        if (applications.Any(application => application.IsHiddenComponent))
        {
            SetStatus("隐藏组件没有厂商卸载程序，不提供常规卸载；请使用“强制卸载”清理它的注册表标识和残留。");
            return;
        }

        applications = applications
            .GroupBy(application => application.IsStoreApplication && application.PackageFullName.Length > 0
                    ? "store:" + application.PackageFullName
                    : application.Id, StringComparer.OrdinalIgnoreCase)
            .Select(group => group.First())
            .ToArray();

        var owner = Window.GetWindow(this);
        if (owner is null)
        {
            return;
        }

        var confirmation = new UninstallConfirmationWindow(applications) { Owner = owner };
        if (confirmation.ShowDialog() != true)
        {
            AppLogService.Verbose($"用户取消了卸载确认：{applications.Count} 个软件未执行卸载。");
            return;
        }

        _statusText = null;
        UpdateChrome();
        AppLogService.Info(
            $"开始卸载批次：{applications.Count} 个软件（{DescribeApplications(applications)}）。");
        var stopwatch = Stopwatch.StartNew();
        var progress = new UninstallProgressWindow(applications, RefreshInstalledApplicationsAsync)
        {
            Owner = owner
        };
        progress.ShowDialog();
        // 卸载结果以“刷新后的已安装软件列表里是否还存在该软件”为准；退出代码只作为明细，
        // 不再参与成功/失败判定，避免厂商卸载程序返回非零码但实际已卸载时被误判为失败。
        var confirmedRemoved = progress.Results
            .Where(result => result.IsSuccess && result.RegistryRemoved == true)
            .Select(result => result.App)
            .ToArray();
        AppLogService.Info(
            $"卸载批次结束：{progress.Summary} 刷新列表确认已移除 {confirmedRemoved.Length} 个，" +
            $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
        await RefreshAfterUninstallAsync(progress.Summary);
        if (confirmedRemoved.Length > 0)
        {
            await OpenDeepCleanAfterUninstallAsync(confirmedRemoved, progress.Summary);
        }
    }

    /// <summary>
    /// 卸载流程结束后由 <see cref="UninstallProgressWindow"/> 调用：重新读取一次“已安装软件”
    /// 列表（桌面卸载注册表 + 当前用户商店应用）作为合并快照，用来判断每个软件是否还存在。
    /// 读取失败时异常抛给进度窗口，由它记录日志并回退到按注册表键/包状态核对；这里不能吞掉
    /// 异常，否则“没读到列表”会被误判成“已卸载”。
    /// </summary>
    private static async Task<IReadOnlyList<InstalledApplication>> RefreshInstalledApplicationsAsync(
        CancellationToken cancellationToken)
    {
        var stopwatch = Stopwatch.StartNew();
        IReadOnlyList<InstalledApplication> desktopApplications =
            await InstalledApplicationProvider.GetInstalledApplicationsAsync(cancellationToken);
        cancellationToken.ThrowIfCancellationRequested();

        IReadOnlyList<InstalledApplication> storeApplications = Array.Empty<InstalledApplication>();
        if (StoreApplicationProvider.IsSupported)
        {
            storeApplications = await StoreApplicationProvider
                .GetInstalledStoreApplicationsAsync(cancellationToken);
        }

        var applications = new List<InstalledApplication>(desktopApplications.Count + storeApplications.Count);
        applications.AddRange(desktopApplications);
        applications.AddRange(storeApplications);
        AppLogService.Info(
            $"卸载后刷新已安装软件列表：注册表 {desktopApplications.Count} 项、" +
            $"商店应用 {storeApplications.Count} 个，共 {applications.Count} 项，" +
            $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
        return applications;
    }

    /// <summary>
    /// 刷新安装列表确认软件已不存在（<see cref="UninstallResult.IsSuccess"/> 且
    /// <see cref="UninstallResult.RegistryRemoved"/> 为 true，不再要求卸载程序退出码为 0）后，
    /// 自动执行安全残留清理，避免残留项把软件重新装回来；危险项仍留给“强制卸载”手动确认，
    /// 避免自动删除安装目录/用户数据。
    /// </summary>
    /// <summary>
    /// 卸载批次确认软件已从刷新后的安装列表移除后，自动打开“强制卸载”窗口，
    /// 让用户逐项选择要清理的残留。这里不再自动删除任何注册表项或文件；
    /// 安全项默认勾选、安装目录/AppData/快捷方式等危险项仍默认不勾选。
    /// </summary>
    private async Task OpenDeepCleanAfterUninstallAsync(
        IReadOnlyList<InstalledApplication> applications, string batchSummary)
    {
        if (applications.Count == 0)
        {
            return;
        }

        var owner = Window.GetWindow(this);
        if (owner is null)
        {
            return;
        }

        _statusText = $"{batchSummary} 已确认卸载，正在打开强制卸载以供选择残留…";
        UpdateChrome();
        AppLogService.Info(
            $"卸载后自动打开强制卸载：{applications.Count} 个软件" +
            $"（{DescribeApplications(applications)}），合并为一次扫描。");

        int deleted = 0;
        int scheduledForReboot = 0;
        int errors = 0;
        try
        {
            var dialog = new DeepCleanWindow(applications) { Owner = owner };
            dialog.ShowDialog();
            deleted = dialog.DeletedCount;
            scheduledForReboot = dialog.ScheduledForRebootCount;
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            errors++;
            AppLogService.Warn(
                $"卸载后打开强制卸载失败：{DescribeApplications(applications)}，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
        }

        await LoadApplicationsAsync();
        string text = $"{batchSummary} 已确认卸载，强制卸载清理：立即删除 {deleted} 项残留";
        if (scheduledForReboot > 0)
        {
            text += $"，已安排 {scheduledForReboot} 项在下次 Windows 重启时删除";
        }
        if (errors > 0)
        {
            text += "，强制卸载窗口打开失败";
        }

        _statusText = text + "。";
        AppLogService.Info(text + "。");
        UpdateChrome();
    }
    /// <summary>日志用的简短软件清单：最多列出 5 个名称，避免一次卸载很多时日志行过长。</summary>
    private static string DescribeApplications(IReadOnlyList<InstalledApplication> applications)
    {
        const int limit = 5;
        string names = string.Join("、", applications.Take(limit).Select(application => application.Name));
        return applications.Count > limit ? $"{names} 等 {applications.Count} 个" : names;
    }

    private async void DeepCleanButton_OnClick(object sender, RoutedEventArgs args)
    {
        var selected = _allApplications.Where(application => application.IsSelected).ToArray();
        if (selected.Length == 0)
        {
            return;
        }

        await OpenDeepCleanAsync(selected);
    }

    private async void ContextDeepClean_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextApplication(sender, out var application))
        {
            await OpenDeepCleanAsync([application]);
        }
    }

    private async Task OpenDeepCleanAsync(IReadOnlyList<InstalledApplication> applications)
    {
        var owner = Window.GetWindow(this);
        if (owner is null || applications.Count == 0)
        {
            return;
        }

        string applicationsText = DescribeApplications(applications);
        AppLogService.Info($"打开深度清理：{applicationsText}，合并为一次扫描。");
        var dialog = new DeepCleanWindow(applications) { Owner = owner };
        dialog.ShowDialog();
        if (dialog.DeletedCount == 0 && dialog.ScheduledForRebootCount == 0)
        {
            AppLogService.Verbose($"深度清理未删除任何残留：{applicationsText}。");
            return;
        }

        int deleted = dialog.DeletedCount;
        int scheduledForReboot = dialog.ScheduledForRebootCount;
        AppLogService.Info(
            $"深度清理完成：{applicationsText}，立即删除 {deleted} 项，" +
            $"重启后删除 {scheduledForReboot} 项。");
        await LoadApplicationsAsync();
        _statusText = $"深度清理：{applicationsText} 立即删除 {deleted} 项" +
            (scheduledForReboot > 0 ? $"，重启后删除 {scheduledForReboot} 项。" : "。");
        UpdateChrome();
    }

    /// <summary>
    /// Hidden components only offer "force uninstall": remove the normal uninstall entry from the
    /// row context menu. The toolbar button is handled by
    /// <see cref="UpdateUninstallAvailability"/>.
    /// </summary>
    private void ApplicationRow_ContextMenuOpening(object sender, ContextMenuEventArgs args)
    {
        if (sender is not FrameworkElement { DataContext: InstalledApplication application, ContextMenu: { } menu })
        {
            return;
        }

        foreach (var item in menu.Items.OfType<MenuItem>())
        {
            if (string.Equals(item.Tag as string, "uninstall", StringComparison.Ordinal))
            {
                item.Visibility = application.CanUninstall ? Visibility.Visible : Visibility.Collapsed;
            }
        }
    }

    private async void ContextUninstall_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextApplication(sender, out var application))
        {
            await RunUninstallAsync([application]);
        }
    }

    private void OpenWebsite_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: InstalledApplication application })
        {
            OpenWebsite(application);
        }
    }

    /// <summary>The per-row "官网" text: open the site without also toggling the row selection.</summary>
    private void WebsiteText_OnMouseLeftButtonUp(object sender, System.Windows.Input.MouseButtonEventArgs args)
    {
        args.Handled = true;
        if (sender is FrameworkElement { DataContext: InstalledApplication application })
        {
            OpenWebsite(application);
        }
    }

    private void ContextOpenWebsite_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextApplication(sender, out var application))
        {
            OpenWebsite(application);
        }
    }

    private void OpenWebsite(InstalledApplication application)
    {
        if (!application.HasUrl)
        {
            SetStatus("该软件没有在卸载注册表中登记官方网站。");
            return;
        }

        if (!ExternalLinkService.OpenWebsite(application.UrlInfoAbout))
        {
            AppLogService.Warn($"打开软件官网失败：{application.Name}（{application.Id}），地址无效或无法启动默认浏览器。");
        }
    }

    private void ContextOpenLocation_OnClick(object sender, RoutedEventArgs args)
    {
        if (!TryGetContextApplication(sender, out var application))
        {
            return;
        }

        string location = application.InstallLocation;
        if (location.Length == 0 || !Directory.Exists(location))
        {
            SetStatus(application.IsStoreApplication
                ? "商店应用安装在 WindowsApps 受保护目录中，无法直接打开安装位置。"
                : "该软件没有记录可访问的安装位置。");
            return;
        }

        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = "explorer.exe",
                Arguments = $"\"{location}\"",
                UseShellExecute = true
            });
            SetStatus(null);
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException or IOException)
        {
            AppLogService.Warn(
                $"打开软件安装位置失败：{application.Name}（{application.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            Debug.WriteLine(exception);
            SetStatus("无法打开该软件的安装位置。");
        }
    }

    private void ContextCopyName_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextApplication(sender, out var application))
        {
            TryCopyToClipboard(application.Name, "已复制软件名称。");
        }
    }

    private void ContextCopyCommand_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextApplication(sender, out var application))
        {
            TryCopyToClipboard(application.UninstallString, "已复制该软件的卸载命令。");
        }
    }

    private void ContextRefresh_OnClick(object sender, RoutedEventArgs args)
    {
        _statusText = null;
        _ = LoadApplicationsAsync();
    }

    private static bool TryGetContextApplication(object sender, out InstalledApplication application)
    {
        if (sender is MenuItem { DataContext: InstalledApplication item })
        {
            application = item;
            return true;
        }

        if (sender is MenuItem { Parent: ContextMenu { PlacementTarget: FrameworkElement target } } &&
            target.DataContext is InstalledApplication targetItem)
        {
            application = targetItem;
            return true;
        }

        application = null!;
        return false;
    }

    private void TryCopyToClipboard(string text, string successMessage)
    {
        if (string.IsNullOrWhiteSpace(text))
        {
            return;
        }

        try
        {
            Clipboard.SetText(text);
            SetStatus(successMessage);
        }
        catch (ExternalException exception)
        {
            // Another process can hold the clipboard open; the text usually still lands, so
            // fall back to an in-process copy before reporting a failure.
            Debug.WriteLine(exception);
            try
            {
                Clipboard.SetDataObject(text, copy: false);
                SetStatus(successMessage);
            }
            catch (ExternalException fallbackException)
            {
                Debug.WriteLine(fallbackException);
                SetStatus("无法访问剪贴板，请稍后重试。");
            }
        }
    }

    private void SetStatus(string? text)
    {
        _statusText = text;
        UpdateChrome();
    }

    private async Task RefreshAfterUninstallAsync(string summary)
    {
        await LoadApplicationsAsync();
        _statusText = summary;
        UpdateChrome();
    }

    private void BeginLoadIcons(IReadOnlyList<InstalledApplication> applications)
    {
        _iconLoadCancellation?.Cancel();
        if (!applications.Any(NeedsBackgroundLoad))
        {
            return;
        }

        var cancellation = new CancellationTokenSource();
        _iconLoadCancellation = cancellation;
        _ = LoadMetadataAsync(applications, cancellation);
    }

    /// <summary>
    /// The size or the install date still has to come from the file system or the uninstall key.
    /// An entry without <c>InstallLocation</c> still needs this pass: the date can come from the
    /// uninstall key's last write time, so skipping it would leave the column empty.
    /// </summary>
    private static bool NeedsBackgroundLoad(InstalledApplication application)
        => application.NeedsFilesystemProbe;

    /// <summary>How many results are applied per dispatcher turn.</summary>
    private const int EnrichmentBatchSize = 8;

    /// <summary>
    /// Fills in size/date for the visible category in batches. Icons are not touched here:
    /// they are extracted on demand by <see cref="ApplicationRow_OnLoaded"/>.
    /// </summary>
    private async Task LoadMetadataAsync(IReadOnlyList<InstalledApplication> applications,
        CancellationTokenSource cancellation)
    {
        try
        {
            var pending = new List<(InstalledApplication Application, long? SizeBytes, DateOnly? InstallDate)>(EnrichmentBatchSize);
            foreach (var application in applications)
            {
                cancellation.Token.ThrowIfCancellationRequested();
                if (!application.NeedsFilesystemProbe)
                {
                    continue;
                }

                bool needSize = application.EstimatedSizeBytes is not > 0;
                bool needDate = application.InstallDate is null;
                (long? SizeBytes, DateOnly? InstallDate) probe;
                try
                {
                    probe = await Task.Run(
                        () => InstalledApplicationEnricher.Probe(
                            application, needSize, needDate, cancellation.Token),
                        cancellation.Token);
                }
                catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception exception)
                {
                    Debug.WriteLine(exception);
                    continue;
                }

                cancellation.Token.ThrowIfCancellationRequested();
                pending.Add((application, probe.SizeBytes, probe.InstallDate));
                if (pending.Count >= EnrichmentBatchSize)
                {
                    ApplyEnrichmentBatch(pending);
                    pending.Clear();
                    await System.Windows.Threading.Dispatcher.Yield(
                        System.Windows.Threading.DispatcherPriority.Background);
                }
            }

            ApplyEnrichmentBatch(pending);
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            // Navigating away or refreshing cancels the metadata pass.
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
        }
        finally
        {
            if (ReferenceEquals(_iconLoadCancellation, cancellation))
            {
                _iconLoadCancellation = null;
            }

            cancellation.Dispose();
        }
    }

    /// <summary>
    /// Sorting by size/date while the metadata pass is still running would leave freshly discovered
    /// values in place forever, so schedule one coalesced re-sort per half second.
    /// </summary>
    private void ScheduleSortRefresh()
    {
        if (_sortField is not ("Size" or "Date") || _sortRefreshScheduled)
        {
            return;
        }

        _sortRefreshScheduled = true;
        _ = Task.Delay(500).ContinueWith(
            _ => Dispatcher.InvokeAsync(() =>
            {
                _sortRefreshScheduled = false;
                if (_sortField is "Size" or "Date")
                {
                    ApplyFilter();
                }
            }),
            TaskScheduler.Default);
    }

    /// <summary>Applies one batch inside a single dispatcher turn so the list lays out once per batch.</summary>
    private void ApplyEnrichmentBatch(
        IReadOnlyList<(InstalledApplication Application, long? SizeBytes, DateOnly? InstallDate)> batch)
    {
        foreach (var entry in batch)
        {
            entry.Application.ApplyFilesystemProbe(entry.SizeBytes, entry.InstallDate);
        }

        if (batch.Count > 0)
        {
            ScheduleSortRefresh();
        }
    }

    private static bool IsInsideButton(DependencyObject? source)
    {
        while (source is not null)
        {
            if (source is ButtonBase)
            {
                return true;
            }

            source = source is Visual or System.Windows.Media.Media3D.Visual3D
                ? VisualTreeHelper.GetParent(source)
                : LogicalTreeHelper.GetParent(source);
        }

        return false;
    }
}
