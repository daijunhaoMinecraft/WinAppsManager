using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using iNKORE.UI.WPF.Modern;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class SettingsPage : Page
{
    private bool _isSynchronizing = true;
    private bool _isSwitchingSource;
    private bool _isPageActive;

    /// <summary>缓存统计的请求代号：只应用最后一次请求的结果，避免连续刷新时旧结果覆盖新结果。</summary>
    private int _cacheStatisticsGeneration;

    /// <summary>服务端返回的软件源列表（<c>GET /api/v1/sources</c>）；客户端不维护本地来源表。</summary>
    private IReadOnlyList<SourceInfo> _serverSources = Array.Empty<SourceInfo>();
    private bool _serverSourcesLoaded;

    public SettingsPage()
    {
        InitializeComponent();
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
        CachePathTextBox.Text = AppCatalogService.CacheDirectory;
        var version = Assembly.GetExecutingAssembly().GetName().Version;
        VersionText.Text = version is null ? "未知" : $"v{version.Major}.{version.Minor}.{version.Build}";
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
        CacheWarningText.Text = string.Join(" ", new[] { state.NetworkError, state.Warning }.Where(text => !string.IsNullOrWhiteSpace(text)));
        CacheWarningText.Visibility = CacheWarningText.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
        _ = RefreshCacheStatisticsAsync();
    }

    /// <summary>
    /// 缓存统计要逐个枚举并读取缓存目录里的文件属性（图标缓存可达数千个文件）。
    /// 这里放到线程池执行：在 UI 线程上同步统计会阻塞上百毫秒，页面切换动画因此卡在某一帧。
    /// </summary>
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

            CacheFilesText.Text = $"目录文件 {statistics.CatalogFiles} 个 · 图标 {statistics.IconFiles} 个 · 占用 {statistics.Bytes / 1024.0:0.0} KB";
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
                ? "目录、图标与服务版本缓存已清理。重新打开软件列表即可获取新资料。"
                : string.Join(" ", result.Errors);
            RefreshCacheStatus();
            RefreshCatalogCount();
        }
        catch (Exception error) when (error is IOException or UnauthorizedAccessException or OperationCanceledException)
        { CacheActionStatusText.Text = "缓存暂时无法完全清理，请稍后重试。"; }
        finally { ClearCacheButton.IsEnabled = true; }
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

        SetStatusText(BackendStatusText,
            (isDefault ? "当前使用默认服务端：" : "当前使用自定义服务端：") + current);
    }

    private async void OnApplyBackendBaseUrl(object sender, RoutedEventArgs args)
    {
        await ApplyBackendBaseUrlAsync(BackendBaseUrlTextBox.Text);
    }

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
                $"服务端地址已更新：{AppCatalogService.BackendBaseUrl} 软件目录已清空，重新打开“软件列表”即可从新服务端加载。");
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
            ? $"已加载 {AppCatalogService.Apps.Count} 款软件" : "目录尚未加载";
    }

    /// <summary>
    /// 按服务端返回的源重建下拉框：名称、默认源、可用状态全部来自 <c>/api/v1/sources</c>，
    /// 客户端不硬编码任何来源 ID/名称；服务端新增来源会自动出现在列表里。
    /// </summary>
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
                    if (id.Length == 0)
                    {
                        continue;
                    }

                    string name = string.IsNullOrWhiteSpace(source.Name) ? id : source.Name!.Trim();
                    var item = new ComboBoxItem
                    {
                        Content = source.Enabled
                            ? source.IsDefault ? $"{name}（默认）" : name
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
                // 服务端不可用：只显示当前已保存/已解析的来源 ID，不伪造本地来源列表。
                string currentId = AppCatalogService.CurrentSourceId;
                if (currentId.Length > 0)
                {
                    AppSourceCombo.Items.Add(new ComboBoxItem
                    {
                        Content = $"{AppCatalogService.SourceDisplayName}（服务端不可用）",
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
        if (sourceId.Length == 0)
        {
            return;
        }

        if (string.Equals(sourceId, AppCatalogService.CurrentSourceId, StringComparison.OrdinalIgnoreCase))
        {
            RefreshAppSourceStatus();
            return;
        }

        await SwitchAppSourceAsync(sourceId);
    }

    /// <summary>切换软件源：保存设置、清空并重建按来源隔离的目录缓存；失败时回退到原软件源。</summary>
    private async Task SwitchAppSourceAsync(string sourceId)
    {
        string previous = AppCatalogService.CurrentSourceId;
        string name = SourceNameOf(sourceId);
        _isSwitchingSource = true;
        AppSourceCombo.IsEnabled = false;
        AppLogService.Info($"用户请求切换软件源：{previous} → {sourceId}（{name}）。");
        SetStatusText(AppSourceStatusText, $"正在切换到{name}并重新加载软件目录…");
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
            // 切换失败时不能改变当前软件源：恢复设置并让下拉框重新显示原来源。
            try
            {
                AppSettingsService.SetSourceId(previous);
            }
            catch (Exception restoreError) when (restoreError is IOException or UnauthorizedAccessException or ArgumentException)
            {
                Debug.WriteLine(restoreError);
            }

            string previousName = AppCatalogService.SourceDisplayName;
            AppLogService.Error(
                $"切换软件源失败：{previous} → {sourceId}（{name}），已保留{previousName}。" +
                $"{error.GetType().Name}：{error.Message}",
                error);
            SetStatusText(AppSourceStatusText,
                $"切换到{name}失败：{error.Message} 已保留{previousName}。", isError: true);
            _isSynchronizing = true;
            try
            {
                SelectAppSource(AppCatalogService.CurrentSourceId);
            }
            finally
            {
                _isSynchronizing = false;
            }

            RefreshCatalogCount();
        }
        finally
        {
            _isSwitchingSource = false;
            AppSourceCombo.IsEnabled = true;
        }
    }

    /// <summary>来源显示名（来自服务端列表）；列表里没有时回退为来源 ID。</summary>
    private string SourceNameOf(string sourceId)
    {
        var info = _serverSources.FirstOrDefault(source =>
            string.Equals(source.Id, sourceId, StringComparison.OrdinalIgnoreCase));
        return string.IsNullOrWhiteSpace(info?.Name) ? sourceId : info!.Name!.Trim();
    }

    private void RefreshAppSourceStatus()
    {
        if (_isSwitchingSource)
        {
            return;
        }

        _isSynchronizing = true;
        try
        {
            SelectAppSource(AppCatalogService.CurrentSourceId);
        }
        finally
        {
            _isSynchronizing = false;
        }

        SetStatusText(AppSourceStatusText, BuildAppSourceStatusText());
    }

    private string BuildAppSourceStatusText()
    {
        string currentId = AppCatalogService.CurrentSourceId;
        if (currentId.Length == 0)
        {
            return _serverSourcesLoaded
                ? "服务端没有可用的软件源，请稍后点击“重新读取来源”。"
                : "暂时无法从服务端获取软件源列表；可用来源由服务端 /api/v1/sources 决定。";
        }

        string name = AppCatalogService.SourceDisplayName;
        string loaded = AppCatalogService.HasLoaded
            ? $"已加载 {AppCatalogService.Apps.Count} 款软件。"
            : "打开“软件列表”后会加载该源的软件目录。";
        string text = $"当前软件源：{name}（{currentId}）。{loaded}";

        var currentInfo = _serverSources.FirstOrDefault(item =>
            string.Equals(item.Id, currentId, StringComparison.OrdinalIgnoreCase));
        if (currentInfo is { AppCount: > 0 })
        {
            text += $"（服务端报告 {currentInfo.AppCount} 款）";
        }

        string capabilities = DescribeCapabilities(AppCatalogService.CurrentCapabilities);
        if (capabilities.Length > 0)
        {
            text += $" 支持：{capabilities}。";
        }

        if (!_serverSourcesLoaded)
        {
            text += " 暂时无法从服务端获取软件源列表，已保留当前来源。";
        }

        if (!string.IsNullOrWhiteSpace(AppCatalogService.SourceFallbackNote))
        {
            text += " " + AppCatalogService.SourceFallbackNote;
        }

        return text;
    }

    /// <summary>把来源能力翻译成简短中文，供设置页状态行展示（不按来源 ID 判断）。</summary>
    private static string DescribeCapabilities(SourceCapabilities capabilities)
    {
        var names = new List<string>();
        if (capabilities.Catalog) names.Add("目录");
        if (capabilities.Categories) names.Add("分类");
        if (capabilities.Search) names.Add("搜索");
        if (capabilities.Details) names.Add("详情");
        if (capabilities.Download) names.Add("下载");
        if (capabilities.Screenshots) names.Add("截图");
        if (capabilities.Cover) names.Add("封面");
        if (capabilities.Video) names.Add("视频");
        if (capabilities.Refresh) names.Add("刷新");
        return string.Join("、", names);
    }

    /// <summary>从当前服务端加载软件源列表；失败时保留当前来源并提示，不影响已保存的设置。</summary>
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
            AppLogService.Warn($"获取服务端软件源列表失败：{error.GetType().Name}：{error.Message}");
        }
        finally
        {
            RefreshSourcesButton.IsEnabled = true;
        }
    }

    private async void OnRefreshServerSources(object sender, RoutedEventArgs args) =>
        await LoadServerSourcesAsync(forceRefresh: true);

    private void OnSettingsChanged(object? sender, EventArgs args)
    {
        // AppSettingsService 可能在后台线程保存设置（例如软件源切换），WPF 控件只能在 UI 线程刷新。
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
            AppLogService.Error(
                $"刷新设置界面失败（线程 {Environment.CurrentManagedThreadId}）：" +
                $"{error.GetType().Name}：{error.Message}",
                error);
        }
    }

    private void RefreshSettings()
    {
        var current = AppSettingsService.Current;
        _isSynchronizing = true;
        try
        {
            SystemThemeOption.IsChecked = current.Theme == ThemePreference.System;
            LightThemeOption.IsChecked = current.Theme == ThemePreference.Light;
            DarkThemeOption.IsChecked = current.Theme == ThemePreference.Dark;

            DownloadConnectionsSlider.Value = current.DownloadConnections;
            DownloadConnectionsValueText.Text = $"{current.DownloadConnections} 个线程";
            DownloadTimeoutSlider.Value = current.DownloadTimeoutSeconds;
            DownloadTimeoutValueText.Text = $"{current.DownloadTimeoutSeconds} 秒";

            GitHubMirrorTextBox.Text = current.GitHubMirror;
            SelectMirrorPreset(current.GitHubMirror);

            SystemProxyOption.IsChecked = current.ProxyMode == ProxyMode.System;
            DirectProxyOption.IsChecked = current.ProxyMode == ProxyMode.Direct;
            CustomProxyOption.IsChecked = current.ProxyMode == ProxyMode.Custom;
            CustomProxyTextBox.Text = current.CustomProxyUrl;
            IgnoreCertificateErrorsCheckBox.IsChecked = current.IgnoreCertificateErrors;
            DownloadDirectoryTextBox.Text = AppSettingsService.ResolvedDownloadDirectory;
            EnableFileLoggingCheckBox.IsChecked = current.EnableFileLogging;
            VerboseLoggingCheckBox.IsChecked = current.VerboseLogging;
            // 文件日志关闭时详细级别没有写入目标，禁用开关避免误解；值仍保留在设置里。
            VerboseLoggingCheckBox.IsEnabled = current.EnableFileLogging;
            // 动效恒定播放，没有开关；这里只说明它会做什么。
            AnimationHintText.Text = "页面切换、列表加载和弹窗都带淡入过渡，界面动效始终开启。";
            SelectAppSource(AppCatalogService.CurrentSourceId);
        }
        finally
        {
            _isSynchronizing = false;
        }

        RefreshLogStatus();
        UpdateDownloadStatuses();
        RefreshBackendStatus();

        var actualTheme = ThemeManager.Current.ActualApplicationTheme == ApplicationTheme.Dark ? "深色" : "浅色";
        ThemeStatusText.Text = current.Theme == ThemePreference.System
            ? $"正在跟随 Windows 使用{actualTheme}模式，系统外观变化时会自动切换。"
            : $"当前使用{actualTheme}模式，下次启动会恢复你的主题偏好。";

        SettingsErrorText.Text = AppSettingsService.LastError ?? string.Empty;
        SettingsErrorPanel.Visibility = string.IsNullOrWhiteSpace(AppSettingsService.LastError)
            ? Visibility.Collapsed
            : Visibility.Visible;
    }

    private void SelectMirrorPreset(string mirror)
    {
        int selectedIndex = -1;
        for (var index = 0; index < GitHubMirrorPresetCombo.Items.Count; index++)
        {
            if (GitHubMirrorPresetCombo.Items[index] is ComboBoxItem item
                && string.Equals(item.Tag as string, mirror, StringComparison.Ordinal))
            {
                selectedIndex = index;
                break;
            }
        }
        GitHubMirrorPresetCombo.SelectedIndex = selectedIndex;
    }

    private void UpdateDownloadStatuses()
    {
        var current = AppSettingsService.Current;
        DownloadSummaryText.Text = $"当前设置：{current.DownloadConnections} 个线程，{current.DownloadTimeoutSeconds} 秒超时。更改会用于之后开始的下载。";
        UpdateDownloadDirectoryStatus();

        if (string.IsNullOrWhiteSpace(current.GitHubMirror))
        {
            SetStatusText(GitHubMirrorStatusText, "当前直连 GitHub，不使用第三方镜像。");
        }
        else
        {
            SetStatusText(GitHubMirrorStatusText,
                $"GitHub 链接将通过 {current.GitHubMirror} 下载；安装包仍会执行官方哈希和 PE 校验。");
        }

        CustomProxyTextBox.IsEnabled = current.ProxyMode == ProxyMode.Custom;
        SaveCustomProxyButton.IsEnabled = current.ProxyMode == ProxyMode.Custom;
        if (current.ProxyMode == ProxyMode.System)
        {
            SetStatusText(ProxyStatusText, "当前跟随 Windows 系统代理设置。");
        }
        else if (current.ProxyMode == ProxyMode.Direct)
        {
            SetStatusText(ProxyStatusText, "当前直连，不使用系统或自定义代理。");
        }
        else if (string.IsNullOrWhiteSpace(current.CustomProxyUrl))
        {
            SetStatusText(ProxyStatusText, "尚未填写自定义代理地址；下载会退回系统代理。", isError: true);
        }
        else if (!AppSettingsService.IsValidHttpUrl(current.CustomProxyUrl))
        {
            SetStatusText(ProxyStatusText, "自定义代理地址无效；下载会退回系统代理。", isError: true);
        }
        else
        {
            SetStatusText(ProxyStatusText, $"当前使用自定义代理：{current.CustomProxyUrl}");
        }
    }

    private void SetStatusText(TextBlock target, string text, bool isError = false)
    {
        target.Text = text;
        string brushKey = isError ? "Wam.DangerBrush" : "Wam.TextSecondaryBrush";
        if (TryFindResource(brushKey) is System.Windows.Media.Brush brush)
        {
            target.Foreground = brush;
        }
    }

    private void UpdateDownloadDirectoryStatus()
    {
        string directory = AppSettingsService.ResolvedDownloadDirectory;
        bool isDefault = string.IsNullOrWhiteSpace(AppSettingsService.Current.DownloadDirectory);
        SetStatusText(DownloadDirectoryStatusText,
            isDefault ? $"使用默认下载目录：{directory}" : $"当前下载目录：{directory}（更改只影响之后开始的任务）");
    }

    private void OnBrowseDownloadDirectory(object sender, RoutedEventArgs args)
    {
        try
        {
            string? selected = FolderPickerService.PickFolder(AppSettingsService.ResolvedDownloadDirectory, "选择安装包保存位置");
            if (selected is null)
            {
                return;
            }

            ApplyDownloadDirectory(selected);
        }
        catch (Exception error) when (error is InvalidOperationException or COMException or IOException or UnauthorizedAccessException)
        {
            SetStatusText(DownloadDirectoryStatusText, $"无法打开文件夹选择窗口：{error.Message}", isError: true);
        }
    }

    private void OnApplyDownloadDirectory(object sender, RoutedEventArgs args) =>
        ApplyDownloadDirectory(DownloadDirectoryTextBox.Text);

    private void OnResetDownloadDirectory(object sender, RoutedEventArgs args) =>
        ApplyDownloadDirectory(string.Empty);

    private void OnOpenDownloadDirectory(object sender, RoutedEventArgs args)
    {
        try
        {
            string directory = AppSettingsService.ResolvedDownloadDirectory;
            Directory.CreateDirectory(directory);
            Process.Start(new ProcessStartInfo(directory) { UseShellExecute = true });
            ShowActionStatus($"已打开下载目录：{directory}");
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
            ShowActionStatus(string.IsNullOrWhiteSpace(AppSettingsService.Current.DownloadDirectory)
                ? "已恢复默认下载目录。"
                : $"下载目录已更新：{AppSettingsService.ResolvedDownloadDirectory}");
        }
        catch (ArgumentException error)
        {
            SetStatusText(DownloadDirectoryStatusText, error.Message, isError: true);
        }
    }

    private void OnThemeChecked(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing || sender is not RadioButton option
            || !Enum.TryParse<ThemePreference>(option.Tag?.ToString(), out var theme))
        {
            return;
        }

        AppSettingsService.SetTheme(theme);
        RefreshSettings();
    }

    private void OnDownloadConnectionsValueChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        int value = (int)Math.Round(args.NewValue);
        DownloadConnectionsValueText.Text = $"{value} 个线程";
        if (_isSynchronizing || value == AppSettingsService.Current.DownloadConnections)
        {
            return;
        }

        AppSettingsService.SetDownloadConnections(value);
        UpdateDownloadStatuses();
    }

    private void OnDownloadTimeoutValueChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        int value = (int)Math.Round(args.NewValue);
        DownloadTimeoutValueText.Text = $"{value} 秒";
        if (_isSynchronizing || value == AppSettingsService.Current.DownloadTimeoutSeconds)
        {
            return;
        }

        AppSettingsService.SetDownloadTimeoutSeconds(value);
        UpdateDownloadStatuses();
    }

    private void OnApplyGitHubMirror(object sender, RoutedEventArgs args)
    {
        ApplyGitHubMirror(GitHubMirrorTextBox.Text);
    }

    private void OnGitHubMirrorPresetChanged(object sender, SelectionChangedEventArgs args)
    {
        if (_isSynchronizing || GitHubMirrorPresetCombo.SelectedItem is not ComboBoxItem item)
        {
            return;
        }

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

    private void OnIgnoreCertificateErrorsChanged(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing)
        {
            return;
        }

        bool ignore = IgnoreCertificateErrorsCheckBox.IsChecked == true;
        if (ignore == AppSettingsService.Current.IgnoreCertificateErrors)
        {
            return;
        }

        AppSettingsService.SetIgnoreCertificateErrors(ignore);
        RefreshSettings();
    }

    /// <summary>“启用文件日志”：保存后立即应用到 AppLogService，并刷新日志状态文字。</summary>
    private void OnEnableFileLoggingChanged(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing)
        {
            return;
        }

        bool enabled = EnableFileLoggingCheckBox.IsChecked == true;
        if (enabled == AppSettingsService.Current.EnableFileLogging)
        {
            return;
        }

        AppSettingsService.SetEnableFileLogging(enabled);
        RefreshSettings();
    }

    /// <summary>“记录详细日志”：保存后立即把 AppLogService.MinimumLevel 切到 Verbose 或 Info。</summary>
    private void OnVerboseLoggingChanged(object sender, RoutedEventArgs args)
    {
        if (_isSynchronizing)
        {
            return;
        }

        bool verbose = VerboseLoggingCheckBox.IsChecked == true;
        if (verbose == AppSettingsService.Current.VerboseLogging)
        {
            return;
        }

        AppSettingsService.SetVerboseLogging(verbose);
        RefreshSettings();
    }


    private void OnCopySettingsPath(object sender, RoutedEventArgs args)
    {
        try
        {
            Clipboard.SetText(AppSettingsService.SettingsFilePath);
            ShowActionStatus("设置文件路径已复制。");
        }
        catch (ExternalException exception)
        {
            ShowActionStatus($"暂时无法使用剪贴板，请直接选中路径复制。{exception.Message}");
        }
    }

    private void OnRetrySaveSettings(object sender, RoutedEventArgs args)
    {
        AppSettingsService.SetTheme(AppSettingsService.Current.Theme);
        RefreshSettings();
    }

    private void OnOpenSettingsDirectory(object sender, RoutedEventArgs args)
    {
        try
        {
            Directory.CreateDirectory(AppSettingsService.SettingsDirectory);
            Process.Start(new ProcessStartInfo(AppSettingsService.SettingsDirectory) { UseShellExecute = true });
            ShowActionStatus("已打开设置文件夹。");
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or Win32Exception or InvalidOperationException)
        {
            ShowActionStatus($"无法打开设置文件夹，请使用上方路径手动访问。{exception.Message}");
        }
    }

    /// <summary>刷新日志文件路径、数量/大小与当前日志设置；日志由 AppLogService 维护，与设置内容无关。</summary>
    private void RefreshLogStatus()
    {
        LogPathTextBox.Text = AppLogService.CurrentLogFilePath;
        var statistics = AppLogService.GetLogStatistics();
        string filesText = statistics.Files == 0
            ? "当前还没有日志文件。"
            : $"当前 {statistics.Files} 个日志文件，共 {FormatLogSize(statistics.TotalBytes)}。";
        string stateText = !AppLogService.Enabled
            ? "文件日志已关闭，只输出到调试器；不会生成新的日志文件。"
            : AppLogService.MinimumLevel == AppLogLevel.Verbose
                ? "文件日志已启用，记录详细日志（Verbose）。"
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
            SetStatusText(LogStatusText, "无法打开日志目录，请使用上方路径手动访问。", isError: true);
        }
    }

    private void OnCopyLogPath(object sender, RoutedEventArgs args)
    {
        try
        {
            Clipboard.SetText(AppLogService.LogDirectory);
            SetStatusText(LogStatusText, "日志目录路径已复制。");
        }
        catch (ExternalException exception)
        {
            SetStatusText(LogStatusText, $"暂时无法使用剪贴板，请直接选中路径复制。{exception.Message}", isError: true);
        }
    }

    private void ShowActionStatus(string message)
    {
        ActionStatusText.Text = message;
        ActionStatusText.Visibility = Visibility.Visible;
    }
}
