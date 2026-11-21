using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Threading;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Media = System.Windows.Media;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class SettingsPage : Page
{
    private bool _isSynchronizing = true;
    private bool _isSwitchingSource;
    private bool _isPageActive;
    private int _cacheStatisticsGeneration;

    private IReadOnlyList<SourceInfo> _serverSources = Array.Empty<SourceInfo>();
    private bool _serverSourcesLoaded;

    // 调色盘 Flyout 实时预览与取消回滚状态
    private string _accentBeforeFlyout = string.Empty;
    private string _pendingCustomAccentHex = string.Empty;
    private bool _isUpdatingColorPicker;
    
    // 主题色 ComboBox 动态自定义项引用
    private ComboBoxItem? _customAccentComboItem;
    private Border? _customAccentComboSwatch;
    private TextBlock? _customAccentComboHexText;

    private readonly DispatcherTimer _backgroundOpacityTimer = new()
    {
        Interval = TimeSpan.FromMilliseconds(300)
    };

    public SettingsPage()
    {
        InitializeComponent();
        PopulateAccentComboItems();

        _backgroundOpacityTimer.Tick += (_, _) =>
        {
            _backgroundOpacityTimer.Stop();
            ApplyBackgroundOpacity();
        };

        DownloadConnectionsSlider.Minimum = InstallerDownloadOptions.MinimumConnections;
        DownloadConnectionsSlider.Maximum = InstallerDownloadOptions.MaximumConnections;
        DownloadConnectionsSlider.TickFrequency = 1;
        DownloadConnectionsSlider.SmallChange = 1;

        DownloadTimeoutSlider.Minimum = InstallerDownloadOptions.MinimumTimeoutSeconds;
        DownloadTimeoutSlider.Maximum = InstallerDownloadOptions.MaximumTimeoutSeconds;
        DownloadTimeoutSlider.TickFrequency = InstallerDownloadOptions.TimeoutStepSeconds;
        DownloadTimeoutSlider.SmallChange = InstallerDownloadOptions.TimeoutStepSeconds;

        AppSettingsService.Initialize();
        SettingsPathTextBox.Text = AppSettingsService.SettingsFilePath;

        var version = Assembly.GetExecutingAssembly().GetName().Version;
        VersionText.Text = version is null ? "未知版本" : $"v{version.Major}.{version.Minor}.{version.Build}";

        ApplySourceComboItems(null);
        RefreshSettings();
        RefreshCacheStatus();
        RefreshAppSourceStatus();
        RefreshBackendStatus();
    }

    private async void OnPageLoaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = true;
        AppSettingsService.SettingsChanged += OnSettingsChanged;
        AppCatalogService.CatalogChanged += OnCatalogChanged;
        AppCatalogService.CacheChanged += OnCacheChanged;

        RefreshCatalogCount();
        RefreshSettings();
        RefreshCacheStatus();
        RefreshAppSourceStatus();
        RefreshBackendStatus();
        await LoadServerSourcesAsync();
    }

    private void OnPageUnloaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = false;
        AppSettingsService.SettingsChanged -= OnSettingsChanged;
        AppCatalogService.CatalogChanged -= OnCatalogChanged;
        AppCatalogService.CacheChanged -= OnCacheChanged;
    }

    // ---------------------------------------------------------------------------------------------
    // 缓存状态
    // ---------------------------------------------------------------------------------------------

    private void OnCacheChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(RefreshCacheStatus));
            return;
        }
        RefreshCacheStatus();
    }

    private void RefreshCacheStatus()
    {
        CacheStatusText.Text = AppCatalogService.CacheDescription;
        var state = AppCatalogService.CacheState;
        CacheWarningText.Text = string.Join(" ", new[] { state.NetworkError, state.Warning }.Where(t => !string.IsNullOrWhiteSpace(t)));
        CacheWarningText.Visibility = CacheWarningText.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        _ = RefreshCacheStatisticsAsync();
    }

    private async Task RefreshCacheStatisticsAsync()
    {
        int generation = ++_cacheStatisticsGeneration;
        try
        {
            var statistics = await Task.Run(() => AppCatalogService.CacheStatistics).ConfigureAwait(true);
            if (generation != _cacheStatisticsGeneration || !_isPageActive)
            {
                return;
            }

            CacheFilesText.Text = $"目录 {statistics.CatalogFiles} 项 · 图标 {statistics.IconFiles} 项 · 共 {statistics.Bytes / 1024.0:0.0} KB";
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException)
        {
            CacheFilesText.Text = "缓存统计暂时无法读取。";
            Debug.WriteLine(error);
        }
    }

    private async void OnClearCache(object sender, RoutedEventArgs args)
    {
        ClearCacheButton.IsEnabled = false;
        CacheActionStatusText.Visibility = Visibility.Visible;
        CacheActionStatusText.Text = "正在清理缓存…";
        try
        {
            var result = await AppCatalogService.ClearCacheAsync();
            CacheActionStatusText.Text = result.Errors.Count == 0
                ? "目录、图标与服务版本缓存已清理。"
                : string.Join(" ", result.Errors);
            RefreshCacheStatus();
            RefreshCatalogCount();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or OperationCanceledException)
        {
            CacheActionStatusText.Text = "缓存暂时无法完全清理，请稍后重试。";
        }
        finally
        {
            ClearCacheButton.IsEnabled = true;
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 服务端地址
    // ---------------------------------------------------------------------------------------------

    private void RefreshBackendStatus()
    {
        string current = AppCatalogService.BackendBaseUrl;
        bool isDefault = string.Equals(current, AppSettingsService.DefaultBackendBaseUrl, StringComparison.OrdinalIgnoreCase);
        if (!BackendBaseUrlTextBox.IsKeyboardFocusWithin)
        {
            BackendBaseUrlTextBox.Text = current;
        }

        BackendSummaryText.Text = isDefault ? "默认在线服务" : current;
        SetStatusText(BackendStatusText,
            (isDefault ? "当前使用默认服务端：" : "当前使用自定义服务端：") + current);
    }

    private async void OnApplyBackendBaseUrl(object sender, RoutedEventArgs args)
        => await ApplyBackendBaseUrlAsync(BackendBaseUrlTextBox.Text);

    private async Task ApplyBackendBaseUrlAsync(string url)
    {
        ApplyBackendButton.IsEnabled = false;
        TestBackendButton.IsEnabled = false;
        ResetBackendButton.IsEnabled = false;
        SetStatusText(BackendStatusText, "正在保存并应用新的服务端地址…");
        try
        {
            await AppCatalogService.SetBackendBaseUrlAsync(url);
            RefreshSettings();
            RefreshCacheStatus();
            RefreshCatalogCount();
            await LoadServerSourcesAsync();
            SetStatusText(BackendStatusText,
                $"服务端已切换至 {AppCatalogService.BackendBaseUrl}，打开“软件列表”即可重新加载。");
        }
        catch (ArgumentException error)
        {
            SetStatusText(BackendStatusText, error.Message, isError: true);
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            SetStatusText(BackendStatusText, $"应用服务端地址失败：{error.Message}", isError: true);
        }
        finally
        {
            ApplyBackendButton.IsEnabled = true;
            TestBackendButton.IsEnabled = true;
            ResetBackendButton.IsEnabled = true;
        }
    }

    private async void OnTestBackendBaseUrl(object sender, RoutedEventArgs args)
    {
        TestBackendButton.IsEnabled = false;
        SetStatusText(BackendStatusText, "正在测试服务端连接（最多 5 秒）…");
        try
        {
            var (ok, message) = await AppCatalogService.TestConnectionAsync(BackendBaseUrlTextBox.Text);
            SetStatusText(BackendStatusText, ok ? $"测试成功：{message}" : $"测试失败：{message}", isError: !ok);
        }
        catch (ArgumentException error)
        {
            SetStatusText(BackendStatusText, error.Message, isError: true);
        }
        finally
        {
            TestBackendButton.IsEnabled = true;
        }
    }

    private async void OnResetBackendBaseUrl(object sender, RoutedEventArgs args)
    {
        BackendBaseUrlTextBox.Text = AppSettingsService.DefaultBackendBaseUrl;
        await ApplyBackendBaseUrlAsync(AppSettingsService.DefaultBackendBaseUrl);
    }

    // ---------------------------------------------------------------------------------------------
    // 软件源
    // ---------------------------------------------------------------------------------------------

    private void OnCatalogChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            Dispatcher.BeginInvoke(new Action(RefreshCatalogCount));
            return;
        }
        RefreshCatalogCount();
    }

    private void RefreshCatalogCount()
    {
        CatalogCountText.Text = AppCatalogService.HasLoaded
            ? $"已加载 {AppCatalogService.Apps.Count} 款软件"
            : "目录未加载";
    }

    private void ApplySourceComboItems(IReadOnlyList<SourceInfo>? sources)
    {
        _isSynchronizing = true;
        try
        {
            AppSourceCombo.Items.Clear();
            if (sources is { Count: > 0 })
            {
                foreach (var source in sources)
                {
                    string id = source.Id?.Trim() ?? string.Empty;
                    if (id.Length == 0) continue;

                    string name = string.IsNullOrWhiteSpace(source.Name) ? id : source.Name!.Trim();
                    var item = new ComboBoxItem
                    {
                        Content = source.Enabled
                            ? (source.IsDefault ? $"{name}（默认）" : name)
                            : $"{name}（不可用）",
                        Tag = id,
                        IsEnabled = source.Enabled
                    };
                    if (!string.IsNullOrWhiteSpace(source.Description))
                    {
                        item.ToolTip = source.Description.Trim();
                    }
                    AppSourceCombo.Items.Add(item);
                }
            }

            if (AppSourceCombo.Items.Count == 0)
            {
                string currentId = AppCatalogService.CurrentSourceId;
                if (currentId.Length > 0)
                {
                    AppSourceCombo.Items.Add(new ComboBoxItem
                    {
                        Content = $"{AppCatalogService.SourceDisplayName}（离线）",
                        Tag = currentId,
                        IsEnabled = false
                    });
                }
            }

            SelectAppSource(AppCatalogService.CurrentSourceId);
        }
        finally
        {
            _isSynchronizing = false;
        }
    }

    private void SelectAppSource(string? sourceId)
    {
        if (string.IsNullOrWhiteSpace(sourceId))
        {
            AppSourceCombo.SelectedIndex = -1;
            return;
        }

        foreach (var entry in AppSourceCombo.Items)
        {
            if (entry is ComboBoxItem item
                && string.Equals(item.Tag as string, sourceId, StringComparison.OrdinalIgnoreCase))
            {
                AppSourceCombo.SelectedItem = item;
                return;
            }
        }
        AppSourceCombo.SelectedIndex = -1;
    }

    private async void OnAppSourceChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_isSynchronizing || _isSwitchingSource || AppSourceCombo.SelectedItem is not ComboBoxItem item)
        {
            return;
        }

        string sourceId = item.Tag as string ?? string.Empty;
        if (sourceId.Length == 0) return;

        if (string.Equals(sourceId, AppCatalogService.CurrentSourceId, StringComparison.OrdinalIgnoreCase))
        {
            RefreshAppSourceStatus();
            return;
        }

        await SwitchAppSourceAsync(sourceId);
    }

    private async Task SwitchAppSourceAsync(string sourceId)
    {
        string previous = AppCatalogService.CurrentSourceId;
        string name = SourceNameOf(sourceId);
        _isSwitchingSource = true;
        AppSourceCombo.IsEnabled = false;
        SetStatusText(AppSourceStatusText, $"正在切换到 {name} 并加载目录…");

        try
        {
            await AppCatalogService.SwitchSourceAsync(sourceId);
            RefreshSettings();
            RefreshCacheStatus();
            RefreshCatalogCount();
            SetStatusText(AppSourceStatusText, BuildAppSourceStatusText());
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            try { AppSettingsService.SetSourceId(previous); }
            catch (Exception restoreError) when (restoreError is IOException or UnauthorizedAccessException or ArgumentException)
            {
                Debug.WriteLine(restoreError);
            }

            SetStatusText(AppSourceStatusText,
                $"切换到 {name} 失败：{error.Message}（已保留 {AppCatalogService.SourceDisplayName}）",
                isError: true);

            _isSynchronizing = true;
            try { SelectAppSource(AppCatalogService.CurrentSourceId); }
            finally { _isSynchronizing = false; }

            RefreshCatalogCount();
        }
        finally
        {
            _isSwitchingSource = false;
            AppSourceCombo.IsEnabled = true;
        }
    }

    private string SourceNameOf(string sourceId)
    {
        var info = _serverSources.FirstOrDefault(s => string.Equals(s.Id, sourceId, StringComparison.OrdinalIgnoreCase));
        return string.IsNullOrWhiteSpace(info?.Name) ? sourceId : info!.Name!.Trim();
    }

    private void RefreshAppSourceStatus()
    {
        if (_isSwitchingSource) return;

        _isSynchronizing = true;
        try { SelectAppSource(AppCatalogService.CurrentSourceId); }
        finally { _isSynchronizing = false; }

        SetStatusText(AppSourceStatusText, BuildAppSourceStatusText());
    }

    private string BuildAppSourceStatusText()
    {
        string currentId = AppCatalogService.CurrentSourceId;
        if (currentId.Length == 0)
        {
            return _serverSourcesLoaded
                ? "服务端暂无可用软件源，请稍后刷新。"
                : "暂时无法连接服务端获取软件源列表。";
        }

        string capabilities = DescribeCapabilities(AppCatalogService.CurrentCapabilities);
        string capSuffix = capabilities.Length > 0 ? $" · 支持：{capabilities}" : string.Empty;

        var currentInfo = _serverSources.FirstOrDefault(item =>
            string.Equals(item.Id, currentId, StringComparison.OrdinalIgnoreCase));
        string countSuffix = currentInfo is { AppCount: > 0 } ? $"（共 {currentInfo.AppCount} 款）" : string.Empty;

        string text = $"当前源：{AppCatalogService.SourceDisplayName}{countSuffix}{capSuffix}";
        if (!_serverSourcesLoaded) text += "（离线模式）";
        if (!string.IsNullOrWhiteSpace(AppCatalogService.SourceFallbackNote))
        {
            text += $" · {AppCatalogService.SourceFallbackNote}";
        }
        return text;
    }

    private static string DescribeCapabilities(SourceCapabilities capabilities)
    {
        var names = new List<string>();
        if (capabilities.Catalog) names.Add("目录");
        if (capabilities.Categories) names.Add("分类");
        if (capabilities.Search) names.Add("搜索");
        if (capabilities.Details) names.Add("详情");
        if (capabilities.Download) names.Add("下载");
        if (capabilities.Screenshots) names.Add("截图");
        if (capabilities.Video) names.Add("视频");
        return string.Join(" / ", names);
    }

    private async Task LoadServerSourcesAsync(bool forceRefresh = false)
    {
        RefreshSourcesButton.IsEnabled = false;
        try
        {
            var sources = await AppCatalogService.GetSourcesAsync(forceRefresh);
            _serverSources = sources;
            _serverSourcesLoaded = true;
            ApplySourceComboItems(sources);
            RefreshAppSourceStatus();
        }
        catch (Exception error) when (error is HttpRequestException or IOException or JsonException
            or InvalidDataException or InvalidOperationException or OperationCanceledException)
        {
            _serverSources = Array.Empty<SourceInfo>();
            _serverSourcesLoaded = false;
            ApplySourceComboItems(null);
            RefreshAppSourceStatus();
        }
        finally
        {
            RefreshSourcesButton.IsEnabled = true;
        }
    }

    private async void OnRefreshServerSources(object sender, RoutedEventArgs args)
        => await LoadServerSourcesAsync(forceRefresh: true);

    // ---------------------------------------------------------------------------------------------
    // 全局设置状态同步
    // ---------------------------------------------------------------------------------------------

    private void OnSettingsChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnSettingsChanged(sender, args)));
            return;
        }

        try
        {
            RefreshSettings();
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            AppLogService.Error($"刷新设置界面失败：{error.Message}", error);
        }
    }

    private void RefreshSettings()
    {
        var current = AppSettingsService.Current;
        _isSynchronizing = true;
        try
        {
            // 主题模式
            string themeTag = current.Theme.ToString();
            foreach (var entry in ThemeModeCombo.Items)
            {
                if (entry is ComboBoxItem item && string.Equals(item.Tag as string, themeTag, StringComparison.Ordinal))
                {
                    ThemeModeCombo.SelectedItem = item;
                    break;
                }
            }
            // 主题色 ComboBox
            UpdateAccentSelection();

            // 窗口背景样式（无 / 亚克力 / 云母 / 自动）：系统不支持的样式不显示为选中，改为显示实际生效的样式。
            AcrylicBackdropItem.IsEnabled = BackdropSupport.IsAcrylicSupported;
            MicaBackdropItem.IsEnabled = BackdropSupport.IsMicaSupported;
            BackdropStyle backdropSelection = current.BackdropStyle switch
            {
                BackdropStyle.Acrylic when !BackdropSupport.IsAcrylicSupported =>
                    BackdropSupport.Resolve(BackdropStyle.Acrylic),
                BackdropStyle.Mica when !BackdropSupport.IsMicaSupported =>
                    BackdropSupport.Resolve(BackdropStyle.Mica),
                _ => current.BackdropStyle
            };
            string backdropTag = backdropSelection.ToString();
            foreach (var entry in BackdropStyleCombo.Items)
            {
                if (entry is ComboBoxItem item && string.Equals(item.Tag as string, backdropTag, StringComparison.Ordinal))
                {
                    BackdropStyleCombo.SelectedItem = item;
                    break;
                }
            }

            // 背景图
            bool hasBg = current.BackgroundImagePath.Length > 0;
            BackgroundImageBox.Text = hasBg ? Path.GetFileName(current.BackgroundImagePath) : "纯色默认背景";
            BackgroundImageBox.ToolTip = hasBg ? current.BackgroundImagePath : "点击“浏览…”选择本地图片";
            ClearBackgroundButton.IsEnabled = hasBg;
            BackgroundOpacitySlider.IsEnabled = hasBg;
            int opacityPercent = (int)Math.Round(current.BackgroundImageOpacity * 100);
            BackgroundOpacitySlider.Value = opacityPercent;
            BackgroundOpacityText.Text = $"{opacityPercent}%";
            BackgroundSummaryText.Text = hasBg
                ? $"{Path.GetFileName(current.BackgroundImagePath)}（{opacityPercent}% 不透明）"
                : "纯色背景";

            // 下载性能
            DownloadConnectionsSlider.Value = current.DownloadConnections;
            DownloadConnectionsValueText.Text = $"{current.DownloadConnections} 线程";
            DownloadTimeoutSlider.Value = current.DownloadTimeoutSeconds;
            DownloadTimeoutValueText.Text = $"{current.DownloadTimeoutSeconds} 秒";
            DownloadPerfSummaryText.Text = $"{current.DownloadConnections} 线程 · {current.DownloadTimeoutSeconds} 秒超时";

            // GitHub 镜像
            GitHubMirrorTextBox.Text = current.GitHubMirror;
            SelectMirrorPreset(current.GitHubMirror);
            GitHubMirrorSummaryText.Text = string.IsNullOrWhiteSpace(current.GitHubMirror)
                ? "直连 GitHub"
                : current.GitHubMirror.TrimEnd('/');

            // 代理与安全
            SystemProxyOption.IsChecked = current.ProxyMode == ProxyMode.System;
            DirectProxyOption.IsChecked = current.ProxyMode == ProxyMode.Direct;
            CustomProxyOption.IsChecked = current.ProxyMode == ProxyMode.Custom;
            CustomProxyTextBox.Text = current.CustomProxyUrl;
            IgnoreCertificateErrorsSwitch.IsOn = current.IgnoreCertificateErrors;
            CertificateWarningBar.IsOpen = current.IgnoreCertificateErrors;

            // 下载目录
            DownloadDirectoryTextBox.Text = AppSettingsService.ResolvedDownloadDirectory;

            // 日志
            EnableFileLoggingSwitch.IsOn = current.EnableFileLogging;
            VerboseLoggingSwitch.IsOn = current.VerboseLogging;
            VerboseLoggingSwitch.IsEnabled = current.EnableFileLogging;

            SelectAppSource(AppCatalogService.CurrentSourceId);
        }
        finally
        {
            _isSynchronizing = false;
        }

        RefreshLogStatus();
        UpdateDownloadStatuses();
        RefreshBackendStatus();

        bool hasError = !string.IsNullOrWhiteSpace(AppSettingsService.LastError);
        SettingsErrorBar.Message = AppSettingsService.LastError ?? string.Empty;
        SettingsErrorBar.IsOpen = hasError;
    }

    private void SelectMirrorPreset(string mirror)
    {
        int selectedIndex = -1;
        for (int i = 0; i < GitHubMirrorPresetCombo.Items.Count; i++)
        {
            if (GitHubMirrorPresetCombo.Items[i] is ComboBoxItem item
                && string.Equals(item.Tag as string, mirror, StringComparison.Ordinal))
            {
                selectedIndex = i;
                break;
            }
        }
        GitHubMirrorPresetCombo.SelectedIndex = selectedIndex;
    }

    private void UpdateDownloadStatuses()
    {
        var current = AppSettingsService.Current;
        UpdateDownloadDirectoryStatus();

        SetStatusText(GitHubMirrorStatusText,
            string.IsNullOrWhiteSpace(current.GitHubMirror)
                ? "当前直连 GitHub，不使用第三方镜像。"
                : $"GitHub 链接将通过 {current.GitHubMirror} 加速下载。");

        bool isCustomProxy = current.ProxyMode == ProxyMode.Custom;
        CustomProxyCard.IsEnabled = isCustomProxy;

        if (current.ProxyMode == ProxyMode.System)
        {
            ProxySummaryText.Text = "跟随系统代理";
            SetStatusText(ProxyStatusText, "当前跟随 Windows 系统代理设置。");
        }
        else if (current.ProxyMode == ProxyMode.Direct)
        {
            ProxySummaryText.Text = "直连（无代理）";
            SetStatusText(ProxyStatusText, "当前直连下载，不使用任何代理。");
        }
        else if (string.IsNullOrWhiteSpace(current.CustomProxyUrl) || !AppSettingsService.IsValidHttpUrl(current.CustomProxyUrl))
        {
            ProxySummaryText.Text = "自定义代理（未生效）";
            SetStatusText(ProxyStatusText, "自定义代理地址为空或无效，下载将临时回退到系统代理。", isError: true);
        }
        else
        {
            ProxySummaryText.Text = current.CustomProxyUrl;
            SetStatusText(ProxyStatusText, $"当前使用自定义代理：{current.CustomProxyUrl}");
        }
    }

    private void SetStatusText(TextBlock target, string text, bool isError = false)
    {
        target.Text = text;
        string brushKey = isError ? "Wam.DangerBrush" : "Wam.TextSecondaryBrush";
        if (TryFindResource(brushKey) is Media.Brush brush)
        {
            target.Foreground = brush;
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 主题与 ComboBox 调色盘（支持深浅色自适应）
    // ---------------------------------------------------------------------------------------------

    private void OnThemeComboSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_isSynchronizing || ThemeModeCombo.SelectedItem is not ComboBoxItem item
                             || !Enum.TryParse<ThemePreference>(item.Tag?.ToString(), out var theme)
                             || theme == AppSettingsService.Current.Theme)
        {
            return;
        }

        AppSettingsService.SetTheme(theme);
        RefreshSettings();
    }

    private void OnBackdropStyleComboSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_isSynchronizing || BackdropStyleCombo.SelectedItem is not ComboBoxItem item
                             || !Enum.TryParse<BackdropStyle>(item.Tag?.ToString(), out var style)
                             || style == AppSettingsService.Current.BackdropStyle)
        {
            return;
        }

        AppSettingsService.SetBackdropStyle(style);
        RefreshSettings();
    }
    
    /// <summary>填充主题色 ComboBox（色块 + 颜色名称 + HEX，动态资源绑定适配深浅色）。</summary>
    private void PopulateAccentComboItems()
    {
        AccentColorCombo.Items.Clear();

        for (int i = 0; i < AppSettingsService.AccentPresets.Length; i++)
        {
            var (hex, label) = AppSettingsService.AccentPresets[i];
            var color = (Media.Color)Media.ColorConverter.ConvertFromString(hex)!;
            string displayName = i == 0 ? $"{label}（默认）" : label;

            AccentColorCombo.Items.Add(new ComboBoxItem
            {
                Tag = hex,
                Content = CreateAccentComboItemContent(color, displayName, hex, out _, out _)
            });
        }

        // 末尾保留一个按需显示的“自定义颜色”条目
        _customAccentComboItem = new ComboBoxItem
        {
            Visibility = Visibility.Collapsed,
            Content = CreateAccentComboItemContent(
                Media.Colors.DodgerBlue,
                "自定义",
                "#2563EB",
                out _customAccentComboSwatch,
                out _customAccentComboHexText)
        };
        AccentColorCombo.Items.Add(_customAccentComboItem);
    }

    private static StackPanel CreateAccentComboItemContent(
        Media.Color color,
        string name,
        string hex,
        out Border swatchBorder,
        out TextBlock hexTextBlock)
    {
        swatchBorder = new Border
        {
            Width = 14,
            Height = 14,
            CornerRadius = new CornerRadius(4),
            Background = new Media.SolidColorBrush(color),
            BorderThickness = new Thickness(1),
            VerticalAlignment = VerticalAlignment.Center
        };
        swatchBorder.SetResourceReference(Border.BorderBrushProperty, "Wam.CardBorderBrush");

        var nameTextBlock = new TextBlock
        {
            Text = name,
            FontSize = 13,
            Margin = new Thickness(8, 0, 6, 0),
            VerticalAlignment = VerticalAlignment.Center
        };
        nameTextBlock.SetResourceReference(TextBlock.ForegroundProperty, "Wam.TextPrimaryBrush");

        hexTextBlock = new TextBlock
        {
            Text = hex.ToUpperInvariant(),
            FontFamily = new Media.FontFamily("Consolas"),
            FontSize = 11.5,
            VerticalAlignment = VerticalAlignment.Center
        };
        hexTextBlock.SetResourceReference(TextBlock.ForegroundProperty, "Wam.TextSecondaryBrush");

        var panel = new StackPanel { Orientation = Orientation.Horizontal };
        panel.Children.Add(swatchBorder);
        panel.Children.Add(nameTextBlock);
        panel.Children.Add(hexTextBlock);
        return panel;
    }

    private void OnAccentComboSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_isSynchronizing || AccentColorCombo.SelectedItem is not ComboBoxItem item)
        {
            return;
        }

        string hex = item.Tag as string ?? string.Empty;
        if (string.IsNullOrEmpty(hex)) return;

        AppSettingsService.SetAccentColor(hex);
        RefreshSettings();
    }

    private void OnResetAccentClick(object sender, RoutedEventArgs args)
    {
        AppSettingsService.SetAccentColor(string.Empty);
        RefreshSettings();
    }

    /// <summary>同步 ComboBox 选中项及“恢复默认”按钮可见性。</summary>
    private void UpdateAccentSelection()
    {
        string defaultHex = AppSettingsService.AccentPresets[0].Hex;
        string activeHex = AppSettingsService.Current.AccentColor.Length > 0
            ? AppSettingsService.Current.AccentColor
            : defaultHex;

        bool isDefault = string.Equals(activeHex, defaultHex, StringComparison.OrdinalIgnoreCase);
        ResetAccentButton.Visibility = isDefault ? Visibility.Collapsed : Visibility.Visible;

        bool prevSync = _isSynchronizing;
        _isSynchronizing = true;
        try
        {
            ComboBoxItem? matchedPreset = null;
            foreach (var entry in AccentColorCombo.Items)
            {
                if (entry is ComboBoxItem item
                    && item != _customAccentComboItem
                    && item.Tag is string hex
                    && string.Equals(hex, activeHex, StringComparison.OrdinalIgnoreCase))
                {
                    matchedPreset = item;
                    break;
                }
            }

            if (matchedPreset is not null)
            {
                if (_customAccentComboItem is not null)
                {
                    _customAccentComboItem.Visibility = Visibility.Collapsed;
                }
                AccentColorCombo.SelectedItem = matchedPreset;
            }
            else if (_customAccentComboItem is not null)
            {
                _customAccentComboItem.Tag = activeHex.ToUpperInvariant();
                _customAccentComboItem.Visibility = Visibility.Visible;
                if (_customAccentComboSwatch is not null)
                {
                    _customAccentComboSwatch.Background = new Media.SolidColorBrush(
                        (Media.Color)Media.ColorConverter.ConvertFromString(activeHex)!);
                }
                if (_customAccentComboHexText is not null)
                {
                    _customAccentComboHexText.Text = activeHex.ToUpperInvariant();
                }
                AccentColorCombo.SelectedItem = _customAccentComboItem;
            }
        }
        finally
        {
            _isSynchronizing = prevSync;
        }
    }

    // ---- 内联调色盘浮层交互（支持实时预览与取消回滚） ----

    private void OnCustomAccentFlyoutOpened(object sender, object args)
    {
        string activeHex = AppSettingsService.Current.AccentColor.Length > 0
            ? AppSettingsService.Current.AccentColor
            : AppSettingsService.AccentPresets[0].Hex;

        _pendingCustomAccentHex = activeHex;
        var color = (Media.Color)Media.ColorConverter.ConvertFromString(activeHex)!;

        _isUpdatingColorPicker = true;
        try
        {
            InlineAccentColorPicker.SelectedColor = color;
            UpdateFlyoutApplyButtonPreview(color);
        }
        finally
        {
            _isUpdatingColorPicker = false;
        }
    }

    private void OnInlineColorPickerChanged(object sender, RoutedEventArgs args)
    {
        if (_isUpdatingColorPicker) return;

        var color = InlineAccentColorPicker.SelectedColor;
        _pendingCustomAccentHex = $"#{color.R:X2}{color.G:X2}{color.B:X2}";
        UpdateFlyoutApplyButtonPreview(color);
    }

    private void UpdateFlyoutApplyButtonPreview(Media.Color color)
    {
        bool isLight = (((0.299 * color.R) + (0.587 * color.G) + (0.114 * color.B)) / 255.0) > 0.62;
        ApplyCustomAccentButton.Background = new Media.SolidColorBrush(color);
        ApplyCustomAccentButton.Foreground = isLight ? Media.Brushes.Black : Media.Brushes.White;
    }

    private void OnApplyCustomAccentClick(object sender, RoutedEventArgs args)
    {
        if (!string.IsNullOrEmpty(_pendingCustomAccentHex))
        {
            AppSettingsService.SetAccentColor(_pendingCustomAccentHex);
        }
        CustomAccentFlyout.Hide();
        RefreshSettings();
    }

    private void OnCancelCustomAccentClick(object sender, RoutedEventArgs args)
    {
        CustomAccentFlyout.Hide();
    }

    private void OnCustomAccentFlyoutClosed(object sender, object args)
    {
        ApplyCustomAccentButton.ClearValue(BackgroundProperty);
        ApplyCustomAccentButton.ClearValue(ForegroundProperty);
    }
    
    // ---------------------------------------------------------------------------------------------
    // 背景图
    // ---------------------------------------------------------------------------------------------

    private void OnBrowseBackgroundImage(object sender, RoutedEventArgs args)
    {
        var dialog = new Microsoft.Win32.OpenFileDialog
        {
            Title = "选择背景图",
            Filter = "图片 (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff)|*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.tif;*.tiff|所有文件 (*.*)|*.*",
            CheckFileExists = true
        };

        if (dialog.ShowDialog(Window.GetWindow(this)) == true)
        {
            ApplyBackgroundImage(dialog.FileName);
        }
    }

    private void OnClearBackgroundImage(object sender, RoutedEventArgs args) => ApplyBackgroundImage(null);

    private void ApplyBackgroundImage(string? path)
    {
        try
        {
            AppSettingsService.SetBackgroundImage(path);
        }
        catch (ArgumentException error)
        {
            ShowActionStatus(error.Message);
        }
        RefreshSettings();
    }

    private void OnBackgroundOpacityChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        if (BackgroundOpacityText is null) return;

        BackgroundOpacityText.Text = $"{Math.Round(args.NewValue)}%";
        if (_isSynchronizing || AppSettingsService.Current.BackgroundImagePath.Length == 0) return;

        _backgroundOpacityTimer.Stop();
        _backgroundOpacityTimer.Start();
    }

    private void ApplyBackgroundOpacity()
    {
        try
        {
            AppSettingsService.SetBackgroundImageOpacity(BackgroundOpacitySlider.Value / 100.0);
            RefreshSettings();
        }
        catch (ArgumentOutOfRangeException error)
        {
            AppLogService.Warn($"保存背景图不透明度失败：{error.Message}");
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 下载与网络
    // ---------------------------------------------------------------------------------------------

    private void UpdateDownloadDirectoryStatus()
    {
        string directory = AppSettingsService.ResolvedDownloadDirectory;
        bool isDefault = string.IsNullOrWhiteSpace(AppSettingsService.Current.DownloadDirectory);
        SetStatusText(DownloadDirectoryStatusText,
            isDefault ? $"默认目录：{directory}" : $"自定义目录：{directory}");
    }

    private void OnBrowseDownloadDirectory(object sender, RoutedEventArgs args)
    {
        try
        {
            string? selected = FolderPickerService.PickFolder(AppSettingsService.ResolvedDownloadDirectory, "选择安装包保存位置");
            if (selected is not null) ApplyDownloadDirectory(selected);
        }
        catch (Exception error) when (error is InvalidOperationException or COMException or IOException or UnauthorizedAccessException)
        {
            SetStatusText(DownloadDirectoryStatusText, $"无法打开文件夹选择窗口：{error.Message}", isError: true);
        }
    }

    private void OnApplyDownloadDirectory(object sender, RoutedEventArgs args)
        => ApplyDownloadDirectory(DownloadDirectoryTextBox.Text);

    private void OnResetDownloadDirectory(object sender, RoutedEventArgs args)
        => ApplyDownloadDirectory(string.Empty);

    private void OnOpenDownloadDirectory(object sender, RoutedEventArgs args)
    {
        try
        {
            string directory = AppSettingsService.ResolvedDownloadDirectory;
            Directory.CreateDirectory(directory);
            Process.Start(new ProcessStartInfo(directory) { UseShellExecute = true });
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or InvalidOperationException or Win32Exception)
        {
            SetStatusText(DownloadDirectoryStatusText, $"无法打开下载目录：{error.Message}", isError: true);
        }
    }

    private void ApplyDownloadDirectory(string directory)
    {
        try
        {
            AppSettingsService.SetDownloadDirectory(directory);
            RefreshSettings();
        }
        catch (ArgumentException error)
        {
            SetStatusText(DownloadDirectoryStatusText, error.Message, isError: true);
        }
    }

    private void OnDownloadConnectionsValueChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        if (DownloadConnectionsValueText is null) return;

        int value = (int)Math.Round(args.NewValue);
        DownloadConnectionsValueText.Text = $"{value} 线程";
        if (_isSynchronizing || value == AppSettingsService.Current.DownloadConnections) return;

        AppSettingsService.SetDownloadConnections(value);
        DownloadPerfSummaryText.Text = $"{value} 线程 · {AppSettingsService.Current.DownloadTimeoutSeconds} 秒超时";
    }

    private void OnDownloadTimeoutValueChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        if (DownloadTimeoutValueText is null) return;

        int value = (int)Math.Round(args.NewValue);
        DownloadTimeoutValueText.Text = $"{value} 秒";
        if (_isSynchronizing || value == AppSettingsService.Current.DownloadTimeoutSeconds) return;

        AppSettingsService.SetDownloadTimeoutSeconds(value);
        DownloadPerfSummaryText.Text = $"{AppSettingsService.Current.DownloadConnections} 线程 · {value} 秒超时";
    }

    private void OnApplyGitHubMirror(object sender, RoutedEventArgs args)
        => ApplyGitHubMirror(GitHubMirrorTextBox.Text);

    private void OnGitHubMirrorPresetChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_isSynchronizing || GitHubMirrorPresetCombo.SelectedItem is not ComboBoxItem item) return;

        string mirror = item.Tag as string ?? string.Empty;
        GitHubMirrorTextBox.Text = mirror;
        ApplyGitHubMirror(mirror);
    }

    private void ApplyGitHubMirror(string mirror)
    {
        try
        {
            AppSettingsService.SetGitHubMirror(mirror);
            RefreshSettings();
        }
        catch (ArgumentException error)
        {
            SetStatusText(GitHubMirrorStatusText, error.Message, isError: true);
        }
    }

    private void OnProxyModeChecked(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing || sender is not RadioButton { Tag: string tag }
            || !Enum.TryParse<ProxyMode>(tag, ignoreCase: true, out var mode)
            || mode == AppSettingsService.Current.ProxyMode)
        {
            return;
        }

        AppSettingsService.SetProxyMode(mode);
        RefreshSettings();
    }

    private void OnSaveCustomProxy(object sender, RoutedEventArgs args)
    {
        try
        {
            AppSettingsService.SetCustomProxyUrl(CustomProxyTextBox.Text);
            RefreshSettings();
        }
        catch (ArgumentException error)
        {
            SetStatusText(ProxyStatusText, error.Message, isError: true);
        }
    }

    private void OnIgnoreCertificateErrorsToggled(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing) return;

        bool ignore = IgnoreCertificateErrorsSwitch.IsOn;
        if (ignore == AppSettingsService.Current.IgnoreCertificateErrors) return;

        AppSettingsService.SetIgnoreCertificateErrors(ignore);
        RefreshSettings();
    }

    // ---------------------------------------------------------------------------------------------
    // 日志与配置存储
    // ---------------------------------------------------------------------------------------------

    private void OnEnableFileLoggingToggled(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing) return;

        bool enabled = EnableFileLoggingSwitch.IsOn;
        if (enabled == AppSettingsService.Current.EnableFileLogging) return;

        AppSettingsService.SetEnableFileLogging(enabled);
        RefreshSettings();
    }

    private void OnVerboseLoggingToggled(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing) return;

        bool verbose = VerboseLoggingSwitch.IsOn;
        if (verbose == AppSettingsService.Current.VerboseLogging) return;

        AppSettingsService.SetVerboseLogging(verbose);
        RefreshSettings();
    }

    private void RefreshLogStatus()
    {
        LogPathTextBox.Text = AppLogService.CurrentLogFilePath;
        var statistics = AppLogService.GetLogStatistics();

        LogSummaryText.Text = !AppLogService.Enabled
            ? "文件日志已关闭"
            : AppLogService.MinimumLevel == AppLogLevel.Verbose
                ? $"详细日志（{statistics.Files} 个文件）"
                : $"标准日志（{statistics.Files} 个文件）";

        string filesText = statistics.Files == 0
            ? "暂无日志文件。"
            : $"共 {statistics.Files} 个日志文件（{FormatLogSize(statistics.TotalBytes)}）。";
        string stateText = !AppLogService.Enabled
            ? "文件日志已关闭，仅向调试器输出。"
            : AppLogService.MinimumLevel == AppLogLevel.Verbose
                ? "已启用详细日志（Verbose）。"
                : "文件日志已启用。";

        SetStatusText(LogStatusText, $"{filesText}{stateText}");
    }

    private static string FormatLogSize(long bytes) => bytes switch
    {
        >= 1024 * 1024 => $"{bytes / 1024.0 / 1024.0:0.#} MB",
        >= 1024 => $"{bytes / 1024.0:0.#} KB",
        _ => $"{bytes} 字节"
    };

    private void OnOpenLogFolder(object sender, RoutedEventArgs args)
    {
        if (AppLogService.OpenLogFolder())
        {
            RefreshLogStatus();
        }
        else
        {
            SetStatusText(LogStatusText, "无法打开日志目录，请复制路径手动访问。", isError: true);
        }
    }

    private void OnCopyLogPath(object sender, RoutedEventArgs args)
    {
        try
        {
            Clipboard.SetText(AppLogService.LogDirectory);
            ShowActionStatus("日志目录路径已复制。");
        }
        catch (ExternalException exception)
        {
            ShowActionStatus($"复制失败：{exception.Message}");
        }
    }

    private void OnCopySettingsPath(object sender, RoutedEventArgs args)
    {
        try
        {
            Clipboard.SetText(AppSettingsService.SettingsFilePath);
            ShowActionStatus("配置文件路径已复制。");
        }
        catch (ExternalException exception)
        {
            ShowActionStatus($"复制失败：{exception.Message}");
        }
    }

    private void OnOpenSettingsDirectory(object sender, RoutedEventArgs args)
    {
        try
        {
            Directory.CreateDirectory(AppSettingsService.SettingsDirectory);
            Process.Start(new ProcessStartInfo(AppSettingsService.SettingsDirectory) { UseShellExecute = true });
            ShowActionStatus("已打开配置文件夹。");
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or Win32Exception or InvalidOperationException)
        {
            ShowActionStatus($"无法打开文件夹：{exception.Message}");
        }
    }

    private void OnRetrySaveSettings(object sender, RoutedEventArgs args)
    {
        AppSettingsService.SetTheme(AppSettingsService.Current.Theme);
        RefreshSettings();
    }

    private void ShowActionStatus(string message)
    {
        ActionStatusText.Text = message;
        ActionStatusText.Visibility = Visibility.Visible;
    }
}