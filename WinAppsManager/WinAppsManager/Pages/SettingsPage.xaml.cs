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

    /// <summary>服务端返回的软件源列表；为空时下拉框使用本地静态两项兜底。</summary>
    private IReadOnlyList<StoreSourceInfo> _serverSources = Array.Empty<StoreSourceInfo>();
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
        var statistics = AppCatalogService.CacheStatistics;
        CacheFilesText.Text = $"目录文件 {statistics.CatalogFiles} 个 · 图标 {statistics.IconFiles} 个 · 占用 {statistics.Bytes / 1024.0:0.0} KB";
        var state = AppCatalogService.CacheState;
        CacheWarningText.Text = string.Join(" ", new[] { state.NetworkError, state.Warning }.Where(text => !string.IsNullOrWhiteSpace(text)));
        CacheWarningText.Visibility = CacheWarningText.Text.Length == 0 ? Visibility.Collapsed : Visibility.Visible;
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

    private static string SourceIdOf(AppSourcePreference source) => source switch
    {
        AppSourcePreference.BuiltIn => StoreCatalogMapper.BuiltinSourceId,
        AppSourcePreference.Soft360 => Soft360CatalogMapper.SourceId,
        _ => StoreCatalogMapper.LenovoSourceId
    };

    private static bool TryMapSourceId(string? id, out AppSourcePreference source)
    {
        source = default;
        if (string.Equals(id, StoreCatalogMapper.LenovoSourceId, StringComparison.OrdinalIgnoreCase))
        {
            source = AppSourcePreference.Lenovo;
            return true;
        }

        if (string.Equals(id, StoreCatalogMapper.BuiltinSourceId, StringComparison.OrdinalIgnoreCase))
        {
            source = AppSourcePreference.BuiltIn;
            return true;
        }

        if (string.Equals(id, Soft360CatalogMapper.SourceId, StringComparison.OrdinalIgnoreCase))
        {
            source = AppSourcePreference.Soft360;
            return true;
        }

        return false;
    }

    /// <summary>
    /// 按服务端返回的源重建下拉框；服务端不可用或返回空列表时回退到本地静态两项。
    /// 不可用的源与客户端不支持的源会标注并禁止选择。
    /// </summary>
    private void ApplySourceComboItems(IReadOnlyList<StoreSourceInfo>? sources)
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

                    bool supported = TryMapSourceId(id, out _);
                    string name = string.IsNullOrWhiteSpace(source.Name) ? id : source.Name!.Trim();
                    AppSourceCombo.Items.Add(new ComboBoxItem
                    {
                        Content = supported
                            ? (source.Enabled ? name : $"{name}（不可用）")
                            : $"{name}（客户端不支持）",
                        Tag = id,
                        IsEnabled = supported && source.Enabled
                    });
                }
            }

            if (AppSourceCombo.Items.Count == 0)
            {
                AppSourceCombo.Items.Add(new ComboBoxItem
                {
                    Content = StoreCatalogMapper.LenovoSourceName + "（默认）",
                    Tag = StoreCatalogMapper.LenovoSourceId
                });
                AppSourceCombo.Items.Add(new ComboBoxItem
                {
                    Content = StoreCatalogMapper.BuiltinSourceName,
                    Tag = StoreCatalogMapper.BuiltinSourceId
                });
            }

            SelectAppSource(AppCatalogService.CurrentSource);
        }
        finally
        {
            _isSynchronizing = false;
        }
    }

    private void SelectAppSource(AppSourcePreference source)
    {
        string id = SourceIdOf(source);
        foreach (var entry in AppSourceCombo.Items)
        {
            if (entry is ComboBoxItem item && string.Equals(item.Tag as string, id, StringComparison.OrdinalIgnoreCase))
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

        if (!TryMapSourceId(item.Tag as string, out var source))
        {
            // 服务端新增了客户端不认识的源：只提示，不改变当前软件源。
            SetStatusText(AppSourceStatusText, "该软件源暂不受当前客户端支持，已保留原有软件源。", isError: true);
            _isSynchronizing = true;
            try
            {
                SelectAppSource(AppCatalogService.CurrentSource);
            }
            finally
            {
                _isSynchronizing = false;
            }

            return;
        }

        if (source == AppCatalogService.CurrentSource)
        {
            RefreshAppSourceStatus();
            return;
        }

        await SwitchAppSourceAsync(source);
    }

    /// <summary>切换软件源：保存设置、清空并重建按源隔离的目录缓存；失败时回退到原软件源。</summary>
    private async Task SwitchAppSourceAsync(AppSourcePreference source)
    {
        var previous = AppCatalogService.CurrentSource;
        string name = AppCatalogService.GetSourceDisplayName(source);
        _isSwitchingSource = true;
        AppSourceCombo.IsEnabled = false;
        SetStatusText(AppSourceStatusText, $"正在切换到{name}并重新加载软件目录…");
        try
        {
            await AppCatalogService.SwitchSourceAsync(source);
            RefreshSettings();
            RefreshCacheStatus();
            RefreshCatalogCount();
            SetStatusText(AppSourceStatusText, BuildAppSourceStatusText());
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // 切换失败时不能改变当前软件源：恢复设置并让下拉框重新显示原源。
            try
            {
                AppSettingsService.SetAppSource(previous);
            }
            catch (Exception restoreError) when (restoreError is IOException or UnauthorizedAccessException)
            {
                Debug.WriteLine(restoreError);
            }

            string previousName = AppCatalogService.GetSourceDisplayName(AppCatalogService.CurrentSource);
            SetStatusText(AppSourceStatusText,
                $"切换到{name}失败：{error.Message} 已保留{previousName}。", isError: true);
            _isSynchronizing = true;
            try
            {
                SelectAppSource(AppCatalogService.CurrentSource);
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

    private void RefreshAppSourceStatus()
    {
        if (_isSwitchingSource)
        {
            return;
        }

        _isSynchronizing = true;
        try
        {
            SelectAppSource(AppCatalogService.CurrentSource);
        }
        finally
        {
            _isSynchronizing = false;
        }

        SetStatusText(AppSourceStatusText, BuildAppSourceStatusText());
    }

    private string BuildAppSourceStatusText()
    {
        var source = AppCatalogService.CurrentSource;
        string name = AppCatalogService.GetSourceDisplayName(source);
        string loaded = AppCatalogService.HasLoaded
            ? $"已加载 {AppCatalogService.Apps.Count} 款软件。"
            : "打开“软件列表”后会加载该源的软件目录。";
        string text = $"当前软件源：{name}。{loaded}";

        if (_serverSourcesLoaded && _serverSources.Count > 0)
        {
            var currentInfo = _serverSources.FirstOrDefault(item =>
                string.Equals(item.Id, SourceIdOf(source), StringComparison.OrdinalIgnoreCase));
            if (currentInfo is { AppCount: > 0 })
            {
                text += $"（服务端报告 {currentInfo.AppCount} 款）";
            }

            var unsupported = _serverSources
                .Where(item => !TryMapSourceId(item.Id, out _))
                .Select(item => string.IsNullOrWhiteSpace(item.Name) ? item.Id : item.Name)
                .Where(sourceName => !string.IsNullOrWhiteSpace(sourceName))
                .ToArray();
            if (unsupported.Length > 0)
            {
                text += $" 服务端还有客户端暂不支持的源：{string.Join("、", unsupported)}。";
            }
        }
        else if (!_serverSourcesLoaded)
        {
            text += " 暂时无法从服务端获取软件源列表，已显示本地默认源。";
        }

        return text;
    }

    /// <summary>从当前服务端加载软件源列表；失败时回退静态两项并提示，不影响已保存的软件源。</summary>
    private async Task LoadServerSourcesAsync()
    {
        try
        {
            var sources = await AppCatalogService.GetSourcesAsync();
            _serverSources = sources;
            _serverSourcesLoaded = true;
            ApplySourceComboItems(sources);
            await EnsureCurrentSourceAvailableAsync(sources);
            RefreshAppSourceStatus();
        }
        catch (Exception error) when (error is HttpRequestException or IOException or JsonException
            or InvalidDataException or OperationCanceledException)
        {
            _serverSources = Array.Empty<StoreSourceInfo>();
            _serverSourcesLoaded = false;
            ApplySourceComboItems(null);
            RefreshAppSourceStatus();
            AppLogService.Warn($"获取服务端软件源列表失败，使用本地默认源：{error.GetType().Name}：{error.Message}");
        }
    }

    /// <summary>
    /// 当前保存的软件源在服务端不可用（或不存在）时，自动回退到服务端 is_default 的可用源（默认联想应用商店）并提示。
    /// </summary>
    private async Task EnsureCurrentSourceAvailableAsync(IReadOnlyList<StoreSourceInfo> sources)
    {
        var current = AppCatalogService.CurrentSource;
        string currentId = SourceIdOf(current);
        var currentInfo = sources.FirstOrDefault(item =>
            string.Equals(item.Id, currentId, StringComparison.OrdinalIgnoreCase));
        if (currentInfo is { Enabled: true })
        {
            return;
        }

        var fallback = sources.FirstOrDefault(item => item.IsDefault && item.Enabled && TryMapSourceId(item.Id, out _))
                       ?? sources.FirstOrDefault(item => item.Enabled && TryMapSourceId(item.Id, out _));
        if (fallback is null || !TryMapSourceId(fallback.Id, out var fallbackSource) || fallbackSource == current)
        {
            SetStatusText(AppSourceStatusText, "服务端没有可用的软件源，已保留当前软件源。", isError: true);
            return;
        }

        string currentName = AppCatalogService.GetSourceDisplayName(current);
        string fallbackName = AppCatalogService.GetSourceDisplayName(fallbackSource);
        try
        {
            await SwitchAppSourceAsync(fallbackSource);
            SetStatusText(AppSourceStatusText,
                $"服务端上的{currentName}当前不可用，已自动切换到{fallbackName}。{BuildAppSourceStatusText()}");
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            SetStatusText(AppSourceStatusText,
                $"服务端上的{currentName}当前不可用，自动切换失败：{error.Message}", isError: true);
        }
    }

    private void OnSettingsChanged(object? sender, EventArgs args)
    {
        RefreshSettings();
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
            SelectAppSource(AppCatalogService.CurrentSource);
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
