using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using Microsoft.Win32;
using WinAppsManager.Dialogs;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Condition = System.Windows.Condition;
using InfoBarSeverity = iNKORE.UI.WPF.Modern.Controls.InfoBarSeverity;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

/// <summary>
/// “自启动项”页：列出 Run/RunOnce 注册表项、启动文件夹快捷方式、登录时运行的计划任务，
/// 以及 Microsoft Store 应用的启动任务，支持启用、禁用、删除与新增。
/// <para>
/// 启用/禁用只改写 <c>StartupApproved</c>、任务的 Enabled 标志或 UWP 的 <c>State</c>，条目本身保留，
/// 随时可以还原；删除才会真正移除登记。复选框用 OneWay 绑定：写操作是异步的，失败时按真实状态同步回来。
/// </para>
/// </summary>
public partial class StartupPage : Page, INotifyPropertyChanged
{
    private readonly StringComparer _nameComparer =
        StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);

    private readonly HashSet<StartupItem> _pendingIcons = new();

    private IReadOnlyList<StartupItem> _allItems = Array.Empty<StartupItem>();
    private CancellationTokenSource? _scanCancellation;
    private Task? _scanTask;
    private ObservableCollection<StartupItem> _startupItems = new();
    private bool _isPageActive;
    private bool _isLoading;
    private bool _hasLoaded;
    private bool _isBusy;
    private bool _iconLoading;
    private bool _suppressToggleEvents;
    private string? _loadError;
    private string _sortField = "Name";
    private bool _sortDescending;
    private StartupItemKind? _kindFilter;

    public StartupPage()
    {
        InitializeComponent();
        DataContext = this;
        PopulateSourceFilter();
        SortCombo.SelectedIndex = 0;
        UpdateSortIndicators();
    }

    public event PropertyChangedEventHandler? PropertyChanged;

    /// <summary>“名称”“来源”“公司”三列的可拖动列宽，表头与每个数据行共用同一实例。</summary>
    public ColumnLayout Columns { get; } = new(260, 180, 150);

    /// <summary>
    /// 过滤/排序后的当前列表。整体替换而不是增量修改：一次 Reset 比几百条 Add 通知流畅得多。
    /// </summary>
    public ObservableCollection<StartupItem> StartupItems
    {
        get => _startupItems;
        private set
        {
            if (ReferenceEquals(_startupItems, value))
            {
                return;
            }

            _startupItems = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(StartupItems)));
        }
    }

    private void PopulateSourceFilter()
    {
        foreach (var (label, kind) in StartupSourceMetadata.FilterOptions)
        {
            SourceFilterCombo.Items.Add(new ComboBoxItem
            {
                Content = label,
                Tag = kind?.ToString() ?? string.Empty
            });
        }

        SourceFilterCombo.SelectedIndex = 0;
    }

    private async void OnPageLoaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = true;
        await LoadItemsAsync();
    }

    private void OnPageUnloaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = false;
        _scanCancellation?.Cancel();
        _scanTask = null;
        _pendingIcons.Clear();
    }

    private async void RefreshButton_OnClick(object sender, RoutedEventArgs args)
    {
        SetStatus(null);
        await LoadItemsAsync();
    }

    private Task LoadItemsAsync()
    {
        if (_scanTask is { IsCompleted: false })
        {
            return _scanTask;
        }

        _scanTask = LoadItemsCoreAsync();
        return _scanTask;
    }

    private async Task LoadItemsCoreAsync()
    {
        using var cancellation = new CancellationTokenSource();
        _scanCancellation = cancellation;
        var stopwatch = Stopwatch.StartNew();
        _isLoading = true;
        _loadError = null;
        UpdateChrome();
        AppLogService.Info("开始扫描自启动项：Run/RunOnce 注册表、启动文件夹、计划任务与应用启动任务。");

        try
        {
            IReadOnlyList<StartupItem> items = await StartupItemProvider.GetStartupItemsAsync(cancellation.Token);
            if (cancellation.IsCancellationRequested || !_isPageActive)
            {
                return;
            }

            _allItems = items;
            _hasLoaded = true;
            _pendingIcons.Clear();
            ApplyFilter();

            int enabledCount = items.Count(item => item.IsEnabled);
            int machineCount = items.Count(item => item.IsMachineScope);
            AppLogService.Info(
                $"自启动项扫描完成：共 {items.Count} 项（已启用 {enabledCount} 项，所有用户 {machineCount} 项），" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            AppLogService.Verbose("自启动项扫描已取消。");
        }
        catch (Exception exception)
        {
            Debug.WriteLine(exception);
            _loadError = _hasLoaded
                ? $"刷新自启动项失败：{exception.Message}。当前显示的是上一次的结果。"
                : $"读取自启动项失败：{exception.Message}。";
            AppLogService.Error(
                $"扫描自启动项失败：{exception.GetType().Name}：{exception.Message}，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms，当前{(_hasLoaded ? "保留上次结果" : "没有可显示的结果")}。",
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

    private void SourceFilterCombo_OnSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        _kindFilter = (SourceFilterCombo.SelectedItem as ComboBoxItem)?.Tag is string tag
                      && Enum.TryParse<StartupItemKind>(tag, out var kind)
            ? kind
            : null;
        ApplyFilter();
    }

    private void SortCombo_OnSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
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

    private void ReportHeader_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is not Button { Tag: string field })
        {
            return;
        }

        if (_sortField == field)
        {
            _sortDescending = !_sortDescending;
        }
        else
        {
            _sortField = field;
            _sortDescending = false;
            foreach (var item in SortCombo.Items.OfType<ComboBoxItem>())
            {
                if (string.Equals(item.Tag as string, field, StringComparison.Ordinal))
                {
                    SortCombo.SelectedItem = item;
                    break;
                }
            }
        }

        UpdateSortIndicators();
        ApplyFilter();
    }

    private void UpdateSortIndicators()
    {
        SortDirectionButton.Content = _sortDescending ? "↓" : "↑";
        SetHeaderText(HeaderName, "名称", "Name");
        SetHeaderText(HeaderSource, "来源", "Source");
        SetHeaderText(HeaderCompany, "公司", "Company");
    }

    private void SetHeaderText(Button? header, string label, string field)
    {
        if (header is null)
        {
            return;
        }

        header.Content = _sortField == field ? $"{label} {(_sortDescending ? "↓" : "↑")}" : label;
    }

    // ---- 列宽拖动 ----

    private void NameColumn_OnDragDelta(object sender, DragDeltaEventArgs args)
        => Columns.Drag(0, args.HorizontalChange);

    private void SourceColumn_OnDragDelta(object sender, DragDeltaEventArgs args)
        => Columns.Drag(1, args.HorizontalChange);

    private void CompanyColumn_OnDragDelta(object sender, DragDeltaEventArgs args)
        => Columns.Drag(2, args.HorizontalChange);

    private void ApplyFilter()
    {
        if (!_hasLoaded)
        {
            UpdateChrome();
            return;
        }

        string query = SearchBox.Text.Trim();
        string[] terms = query.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        IEnumerable<StartupItem> matches = _allItems.Where(item =>
            MatchesKind(item) && MatchesSearch(item, terms));
        var ordered = ApplySort(matches).ToArray();

        StartupItems = new ObservableCollection<StartupItem>(ordered);

        bool showEmpty = StartupItems.Count == 0;
        EmptyState.Visibility = showEmpty ? Visibility.Visible : Visibility.Collapsed;
        ItemsList.Visibility = showEmpty ? Visibility.Collapsed : Visibility.Visible;
        EmptyTitleText.Text = query.Length > 0 ? "没有匹配的自启动项" : "没有找到自启动项";
        EmptyDescriptionText.Text = query.Length > 0
            ? $"没有自启动项匹配“{query}”。"
            : "这里显示 Run/RunOnce 注册表项、启动文件夹快捷方式、登录时运行的计划任务和应用启动任务。";

        UpdateChrome();
    }

    private bool MatchesKind(StartupItem item)
        => _kindFilter is null || StartupSourceMetadata.GetKind(item.Source) == _kindFilter.Value;

    private static bool MatchesSearch(StartupItem item, string[] terms)
    {
        if (terms.Length == 0)
        {
            return true;
        }

        foreach (string term in terms)
        {
            bool matched =
                item.Name.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.RegistrationName.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.CompanyName.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.Description.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.SourceText.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.KindText.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.ScopeText.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.Command.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.Location.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.StatusText.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                item.RuntimeStatusText.Contains(term, StringComparison.OrdinalIgnoreCase) ||
                (item.IsInvalid && (term.Contains("失效") || term.Contains("无效")));
            if (!matched)
            {
                return false;
            }
        }

        return true;
    }

    private IEnumerable<StartupItem> ApplySort(IEnumerable<StartupItem> items) => _sortField switch
    {
        "Source" => _sortDescending
            ? items.OrderByDescending(item => (int)item.Source).ThenBy(item => item.Name, _nameComparer)
            : items.OrderBy(item => (int)item.Source).ThenBy(item => item.Name, _nameComparer),
        "Company" => _sortDescending
            ? items.OrderByDescending(item => item.CompanyName, _nameComparer).ThenBy(item => item.Name, _nameComparer)
            : items.OrderBy(item => item.CompanyName.Length == 0 ? 1 : 0)
                .ThenBy(item => item.CompanyName, _nameComparer)
                .ThenBy(item => item.Name, _nameComparer),
        "Status" => _sortDescending
            ? items.OrderByDescending(item => item.IsEnabled).ThenBy(item => item.Name, _nameComparer)
            : items.OrderBy(item => item.IsEnabled).ThenBy(item => item.Name, _nameComparer),
        _ => _sortDescending
            ? items.OrderByDescending(item => item.Name, _nameComparer)
            : items.OrderBy(item => item.Name, _nameComparer)
    };

    private void UpdateChrome()
    {
        LoadingPanel.Visibility = _isLoading ? Visibility.Visible : Visibility.Collapsed;
        RefreshButton.IsEnabled = !_isLoading && !_isBusy;
        AddButton.IsEnabled = !_isBusy;
        SortCombo.IsEnabled = !_isLoading;
        SortDirectionButton.IsEnabled = !_isLoading;
        SourceFilterCombo.IsEnabled = !_isLoading;

        ErrorPanel.Visibility = _loadError is null ? Visibility.Collapsed : Visibility.Visible;
        ErrorText.Text = _loadError ?? string.Empty;

        if (!_hasLoaded)
        {
            CountText.Text = _isLoading ? "正在读取本机的自启动项…" : "尚未读取自启动项";
            return;
        }

        int total = _allItems.Count;
        int enabled = _allItems.Count(item => item.IsEnabled);
        int shown = StartupItems.Count;
        string filterNote = shown == total ? string.Empty : $"，当前显示 {shown} 项";
        CountText.Text = $"共 {total} 项自启动项，已启用 {enabled} 项，已禁用 {total - enabled} 项{filterNote}";
    }

    /// <summary>
    /// 在页面顶部的 <see cref="InfoBar"/> 里显示一条操作结果。<paramref name="message"/> 为空时收起提示；
    /// 失败用 <c>Error</c>、只完成一部分用 <c>Warning</c>、成功用 <c>Success</c>，其余是普通信息。
    /// </summary>
    private void SetStatus(string? message, InfoBarSeverity severity = InfoBarSeverity.Informational)
    {
        if (string.IsNullOrWhiteSpace(message))
        {
            StatusInfoBar.IsOpen = false;
            return;
        }

        StatusInfoBar.Severity = severity;
        StatusInfoBar.Message = message;
        StatusInfoBar.IsOpen = true;
    }

    /// <summary>把操作结果按成功/失败映射成 InfoBar 级别。</summary>
    private void SetStatus(StartupOperationResult result, bool warningOnFailure = false)
        => SetStatus(
            result.Success
                ? result.Message
                : $"{result.Message}{(result.Error is null ? string.Empty : $"（{result.Error}）")}",
            result.Success
                ? InfoBarSeverity.Success
                : warningOnFailure
                    ? InfoBarSeverity.Warning
                    : InfoBarSeverity.Error);

    // ---- 图标：只为真正出现在视口里的行提取 ----

    private void StartupRow_OnLoaded(object sender, RoutedEventArgs args)
    {
        if (sender is not FrameworkElement { DataContext: StartupItem item })
        {
            return;
        }

        if (item.HasIcon || item.IconPath.Length == 0)
        {
            return;
        }

        if (!_pendingIcons.Add(item))
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
                var item = _pendingIcons.First();
                _pendingIcons.Remove(item);
                if (item.HasIcon || item.IconPath.Length == 0)
                {
                    continue;
                }

                ImageSource? icon;
                try
                {
                    icon = await Task.Run(() => InstalledApplicationIconService.GetIcon(item.IconPath));
                }
                catch (Exception exception)
                {
                    Debug.WriteLine(exception);
                    continue;
                }

                if (icon is not null)
                {
                    item.IconSource = icon;
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

    // ---- 启用 / 禁用 ----
    private async void EnabledCheckBox_OnClick(object sender, RoutedEventArgs args)
    {
        if (_suppressToggleEvents || _isBusy)
        {
            return;
        }

        if (sender is not CheckBox { DataContext: StartupItem item } checkBox)
        {
            return;
        }

        bool targetEnabled = checkBox.IsChecked == true;
        if (targetEnabled == item.IsEnabled)
        {
            return;
        }

        await ApplyEnabledAsync(item, targetEnabled, checkBox);
    }
    
    private async Task ApplyEnabledAsync(StartupItem item, bool enabled, CheckBox? checkBox = null)
    {
        if (_isBusy)
        {
            SyncCheckBox(checkBox, item.IsEnabled);
            return;
        }

        _isBusy = true;
        UpdateChrome();
        StartupOperationResult result;
        try
        {
            result = await StartupItemService.SetEnabledAsync(item, enabled);
        }
        finally
        {
            _isBusy = false;
        }

        // 只有写成功才把模型改成目标值；失败时模型保持原状，再把复选框拽回真实状态。
        _suppressToggleEvents = true;
        try
        {
            if (result.Success)
            {
                item.IsEnabled = enabled;
            }

            if (checkBox is not null)
            {
                checkBox.IsChecked = item.IsEnabled;
            }
        }
        finally
        {
            _suppressToggleEvents = false;
        }

        SetStatus(result);
        RefreshAfterToggle();
    }

    /// <summary>
    /// 单条启用/禁用之后刷新界面。默认只更新计数和按钮，**不重建行集合**：
    /// 这里可能正处在 CheckBox 的 Checked/Unchecked 事件里，替换 ItemsSource 会把事件来源行从
    /// 视觉树上摘掉；只有按“状态”排序时顺序才会变，那种情况推到下一个 dispatcher 回合再重建。
    /// </summary>
    private void RefreshAfterToggle()
    {
        UpdateChrome();
        if (_sortField != "Status")
        {
            return;
        }

        _ = Dispatcher.BeginInvoke(new Action(ApplyFilter), System.Windows.Threading.DispatcherPriority.Background);
    }

    private void SyncCheckBox(CheckBox? checkBox, bool value)
    {
        if (checkBox is null)
        {
            return;
        }

        _suppressToggleEvents = true;
        try
        {
            checkBox.IsChecked = value;
        }
        finally
        {
            _suppressToggleEvents = false;
        }
    }

    private async void ContextToggle_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextItem(sender, out var target) && target.Item.CanToggle)
        {
            await ApplyEnabledAsync(target.Item, !target.Item.IsEnabled);
        }
    }

    // ---- 删除 / 新增 ----

    private async void ContextDelete_OnClick(object sender, RoutedEventArgs args)
    {
        if (!TryGetContextItem(sender, out var target) || !target.Item.CanDelete)
        {
            return;
        }

        StartupItem item = target.Item;
        var confirm = MessageBox.Show(
            Window.GetWindow(this),
            $"确定要删除“{item.Name}”这条自启动登记吗？\n\n" +
            $"来源：{item.SourceText}\n位置：{item.Location}\n\n" +
            "删除会移除这条登记，之后只能手动重新添加；只想临时关掉的话，取消勾选即可。",
            "从列表中删除",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning,
            MessageBoxResult.No);
        if (confirm != MessageBoxResult.Yes)
        {
            return;
        }

        _isBusy = true;
        UpdateChrome();
        StartupOperationResult result;
        try
        {
            result = await StartupItemService.DeleteAsync(item);
        }
        finally
        {
            _isBusy = false;
        }

        if (!result.Success)
        {
            SetStatus(result);
            return;
        }

        SetStatus(result);
        await LoadItemsAsync();
    }

    private async void AddButton_OnClick(object sender, RoutedEventArgs args)
    {
        var dialog = new AddStartupItemWindow { Owner = Window.GetWindow(this) };
        if (dialog.ShowDialog() != true)
        {
            return;
        }

        SetStatus(
            dialog.ResultSuccess
                ? dialog.ResultMessage
                : $"{dialog.ResultMessage}{(dialog.ResultError is null ? string.Empty : $"（{dialog.ResultError}）")}",
            dialog.ResultSuccess ? InfoBarSeverity.Success : InfoBarSeverity.Error);

        if (dialog.ResultSuccess)
        {
            await LoadItemsAsync();
        }
    }

    // ---- 右键菜单与浏览 ----

    private void StartupRow_ContextMenuOpening(object sender, ContextMenuEventArgs args)
    {
        if (sender is not FrameworkElement { DataContext: StartupItem item, ContextMenu: { } menu })
        {
            return;
        }

        foreach (var menuItem in menu.Items.OfType<MenuItem>())
        {
            switch (menuItem.Tag as string)
            {
                case "toggle":
                    menuItem.Header = item.IsEnabled ? "启动时禁止运行" : "启动时允许运行";
                    menuItem.IsEnabled = item.CanToggle;
                    break;
                case "delete":
                    // 应用启动任务由应用自己声明，删不掉，只能禁用。
                    menuItem.IsEnabled = item.CanDelete;
                    menuItem.ToolTip = item.CanDelete
                        ? null
                        : "应用启动任务由应用自身声明，只能禁用，不能删除。";
                    break;
                case "location":
                    // 商店包安装在受保护的 \WindowsApps 下，定位一个 PNG 图标没有意义；
                    // 这类条目改由下面的“在 Microsoft Store 中查看”提供入口。
                    menuItem.Visibility = item.CanOpenStore ? Visibility.Collapsed : Visibility.Visible;
                    break;
                case "store":
                    menuItem.Visibility = item.CanOpenStore ? Visibility.Visible : Visibility.Collapsed;
                    break;
            }
        }
    }

    private void ContextOpenLocation_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextItem(sender, out var target))
        {
            OpenProgramLocation(target.Item);
        }
    }

    private void ContextOpenSource_OnClick(object sender, RoutedEventArgs args)
    {
        if (TryGetContextItem(sender, out var target))
        {
            OpenItemSource(target.Item);
        }
    }

    /// <summary>
    /// 打开 Microsoft Store 产品页（<c>ms-windows-store://pdp/?PFN=…</c>）。这个协议由 Store 应用
    /// 自己的清单注册，ShellExecute 会把它交给 Store；不是从商店安装的包，Store 会提示找不到产品。
    /// </summary>
    private void ContextOpenStore_OnClick(object sender, RoutedEventArgs args)
    {
        if (!TryGetContextItem(sender, out var target) || !target.Item.CanOpenStore)
        {
            return;
        }

        StartupItem item = target.Item;
        if (TryStart(item.StoreUri))
        {
            SetStatus($"正在 Microsoft Store 中打开“{item.Name}”的产品页。", InfoBarSeverity.Success);
            return;
        }

        SetStatus($"无法打开 Microsoft Store（{item.StoreUri}）。", InfoBarSeverity.Error);
    }

    /// <summary>定位到程序本身：在资源管理器里选中它。</summary>
    private void OpenProgramLocation(StartupItem item)
    {
        if (item.IconPath.Length > 0 && File.Exists(item.IconPath))
        {
            if (TryOpenExplorer($"/select,\"{item.IconPath}\""))
            {
                return;
            }
        }

        if (File.Exists(item.Location) && TryOpenExplorer($"/select,\"{item.Location}\""))
        {
            return;
        }

        SetStatus(
            item.IconPath.Length == 0
                ? $"“{item.Name}”没有可定位的程序文件（应用启动任务由应用的清单声明）。"
                : $"找不到“{item.Name}”对应的文件：{item.IconPath}",
            InfoBarSeverity.Warning);
    }

    /// <summary>定位到登记来源：注册表项打开 regedit，快捷方式打开启动文件夹，计划任务打开任务计划程序。</summary>
    private void OpenItemSource(StartupItem item)
    {
        switch (StartupSourceMetadata.GetKind(item.Source))
        {
            case StartupItemKind.StartupFolder:
                if (TryOpenExplorer($"/select,\"{item.Location}\""))
                {
                    SetStatus($"已在资源管理器中打开启动文件夹：{item.Location}", InfoBarSeverity.Success);
                    return;
                }

                break;

            case StartupItemKind.ScheduledTask:
                _ = OpenScheduledTaskAsync(item.Location);
                return;

            default:
                // 注册表与 UWP 启动任务都落在注册表里：先写 LastKey，再启动 regedit 直接跳过去。
                if (TryOpenRegistry(item.Location))
                {
                    SetStatus($"已在注册表编辑器中定位：{item.Location}", InfoBarSeverity.Success);
                    return;
                }

                break;
        }

        SetStatus($"无法打开来源位置：{item.Location}", InfoBarSeverity.Error);
    }

    private bool TryOpenRegistry(string keyPath)
    {
        if (!keyPath.StartsWith("HKEY_", StringComparison.OrdinalIgnoreCase))
        {
            return false;
        }

        // 去除末尾可能存在的反斜杠，避免与命令行双引号组合成 \" 导致转义异常
        string validKeyPath = ResolveValidRegistryKeyPath(keyPath).TrimEnd('\\');
        string rootPrefix = CultureInfo.InstalledUICulture.Name.StartsWith("zh", StringComparison.OrdinalIgnoreCase)
            ? "计算机"
            : "Computer";
        string targetLastKey = $@"{rootPrefix}\{validKeyPath}";

        string arguments =
            "/c reg add \"HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Applets\\Regedit\" " +
            $"/v LastKey /t REG_SZ /d \"{targetLastKey}\" /f >nul && start \"\" regedit.exe -m";

        try
        {
            var startInfo = new ProcessStartInfo("cmd.exe", arguments)
            {
                UseShellExecute = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };

            Process.Start(startInfo);
            return true;
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
        {
            AppLogService.Warn($"打开注册表并定位失败：{keyPath}：{exception.Message}", exception);
            return false;
        }
    }
    
    /// <summary>
    /// 如果传入的路径末尾包含了注册表值名称（Value Name），则剥离最后一段，返回其所属的项（Key）路径。
    /// </summary>
    private static string ResolveValidRegistryKeyPath(string fullPath)
    {
        int firstSlash = fullPath.IndexOf('\\');
        if (firstSlash < 0)
        {
            return fullPath;
        }

        string hiveName = fullPath[..firstSlash].ToUpperInvariant();
        string subKeyPath = fullPath[(firstSlash + 1)..];

        RegistryKey? baseKey = hiveName switch
        {
            "HKEY_CURRENT_USER" or "HKCU" => Registry.CurrentUser,
            "HKEY_LOCAL_MACHINE" or "HKLM" => Registry.LocalMachine,
            "HKEY_CLASSES_ROOT" or "HKCR" => Registry.ClassesRoot,
            "HKEY_USERS" or "HKU" => Registry.Users,
            "HKEY_CURRENT_CONFIG" or "HKCC" => Registry.CurrentConfig,
            _ => null
        };

        if (baseKey is null)
        {
            return fullPath;
        }

        // 尝试直接作为 Key 打开；如果打开失败，说明最后一段可能是 ValueName，截取到上一级
        using var testKey = baseKey.OpenSubKey(subKeyPath);
        if (testKey is not null)
        {
            return fullPath;
        }

        int lastSlash = fullPath.LastIndexOf('\\');
        return lastSlash > firstSlash ? fullPath[..lastSlash] : fullPath;
    }
    
    private static bool TryOpenExplorer(string arguments)
        => TryStart("explorer.exe", arguments);

    private static bool TryStart(string fileName, string? arguments = null)
    {
        try
        {
            var startInfo = new ProcessStartInfo(fileName) { UseShellExecute = true };
            if (!string.IsNullOrEmpty(arguments))
            {
                startInfo.Arguments = arguments;
            }

            Process.Start(startInfo);
            return true;
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
        {
            AppLogService.Warn($"启动外部程序失败：{fileName} {arguments}：{exception.Message}", exception);
            return false;
        }
    }

    private static bool TryGetContextItem(object sender, out ContextTarget target)
    {
        target = default;
        var menuItem = sender as MenuItem;
        var contextMenu = menuItem?.Parent as ContextMenu;
        if (contextMenu?.PlacementTarget is FrameworkElement { DataContext: StartupItem placementItem })
        {
            target = new ContextTarget(placementItem);
            return true;
        }

        if (menuItem?.DataContext is StartupItem menuItemOwner)
        {
            target = new ContextTarget(menuItemOwner);
            return true;
        }

        return false;
    }
    
    /// <summary>
    /// 打开任务计划程序并展开到任务所在文件夹。taskschd.msc 的左侧目录树是 MMC 的
    /// SysTreeView32，没有 UI Automation 提供程序，定位逻辑见 <see cref="TaskSchedulerNavigator"/>。
    /// </summary>
    private async Task OpenScheduledTaskAsync(string taskLocation)
    {
        int processId = 0;
        try
        {
            using Process? process = Process.Start(new ProcessStartInfo("taskschd.msc") { UseShellExecute = true });
            processId = process?.Id ?? 0;
        }
        catch (Exception exception) when (exception is Win32Exception or InvalidOperationException)
        {
            AppLogService.Warn($"打开任务计划程序失败：{taskLocation}：{exception.Message}", exception);
            SetStatus($"无法打开任务计划程序，任务路径：{taskLocation}", InfoBarSeverity.Error);
            return;
        }

        SetStatus($"正在任务计划程序中定位：{taskLocation}…");

        string detail = string.Empty;
        TaskSchedulerNavigator.NavigationResult result = await Task.Run(
            () => TaskSchedulerNavigator.TryNavigate(taskLocation, processId, out detail));

        switch (result)
        {
            case TaskSchedulerNavigator.NavigationResult.TaskSelected:
                SetStatus($"已在任务计划程序中选中任务：{taskLocation}", InfoBarSeverity.Success);
                break;

            case TaskSchedulerNavigator.NavigationResult.FolderSelected:
                SetStatus($"已在任务计划程序中展开到：{taskLocation}", InfoBarSeverity.Success);
                break;

            case TaskSchedulerNavigator.NavigationResult.FolderNotFound:
                AppLogService.Warn($"任务计划程序中找不到任务位置：{taskLocation}（{detail}）");
                SetStatus(
                    $"任务计划程序中没有该任务所在的位置：{detail}（任务可能已被删除）",
                    InfoBarSeverity.Warning);
                break;

            default:
                AppLogService.Warn($"任务计划程序自动定位失败：{taskLocation}（{result}：{detail}）");
                SetStatus(
                    $"已打开任务计划程序，但未能自动定位 {taskLocation}，请手动查看",
                    InfoBarSeverity.Warning);
                break;
        }
    }

    /// <summary>右键菜单命中的条目。</summary>
    private readonly record struct ContextTarget(StartupItem Item);
}
