using System.Collections.ObjectModel;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;
using iNKORE.UI.WPF.Modern.Controls;
using WinAppsManager.Dialogs;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class AppDetailsPage : Page, INotifyPropertyChanged
{
    /// <summary>
    /// 相册缩略图显示尺寸约 236×148（高 DPI 下最多约 472 物理像素）。
    /// 按 640 宽度解码足够清晰，同时把每张 1280×720 截图的内存从约 3.5 MB 降到约 0.9 MB；
    /// 大图预览仍由 <see cref="ScreenshotViewerWindow"/> 使用原始尺寸加载。
    /// </summary>
    private const int ScreenshotThumbnailDecodeWidth = 640;

    private CancellationTokenSource? _detailLoadCancellation;
    private Task? _detailLoadTask;
    private CancellationTokenSource? _copyLinkCancellation;
    private CancellationTokenSource? _linkLoadCancellation;
    private CancellationTokenSource _mediaCancellation = new();
    private IReadOnlyList<AppScreenshot> _viewerScreenshots = Array.Empty<AppScreenshot>();
    private IReadOnlyList<string> _tagChips = Array.Empty<string>();
    private bool _isLoading;
    private bool _hasFreshDetails;
    private bool _copyLinkInProgress;

    public AppItem AppItem { get; private set; }
    public ObservableCollection<ScreenshotViewItem> Screenshots { get; } = new();
    public IReadOnlyList<string> TagChips
    {
        get => _tagChips;
        private set
        {
            _tagChips = value;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(TagChips)));
        }
    }

    public event EventHandler? BackRequested;
    public event Action? DownloadCenterRequested;
    public event PropertyChangedEventHandler? PropertyChanged;

    public AppDetailsPage(AppItem appItem)
    {
        ArgumentNullException.ThrowIfNull(appItem);
        AppItem = appItem;
        InitializeComponent();
        DataContext = this;
        UpdateTagChips();
        UpdateStoreSections();
        RefreshIcon();
        RefreshDownloadAction();
        UpdateDownloadLinkDisplay();
    }

    private void BackButton_OnClick(object sender, RoutedEventArgs e) => BackRequested?.Invoke(this, EventArgs.Empty);

    private void WebsiteButton_OnClick(object sender, RoutedEventArgs e)
    {
        if (!ExternalLinkService.OpenWebsite(AppItem.WebsiteUrl))
        {
            AppLogService.Warn($"打开软件官网失败：{AppItem.Name}（{AppItem.Id}），地址无效或无法启动默认浏览器。");
        }
    }

    private async void Page_OnLoaded(object sender, RoutedEventArgs e)
    {
        AppCatalogService.CacheChanged += OnCacheChanged;
        DownloadCenterService.StatusChanged += OnDownloadCenterStatusChanged;
        DetailCacheStatusText.Text = AppCatalogService.CacheDescription;

        if (_detailLoadTask is { IsCompleted: false })
        {
            await _detailLoadTask;
            if (!IsLoaded)
            {
                return;
            }
        }

        await LoadDetailsAsync();
    }

    private void Page_OnUnloaded(object sender, RoutedEventArgs e)
    {
        _detailLoadCancellation?.Cancel();
        _copyLinkCancellation?.Cancel();
        _linkLoadCancellation?.Cancel();
        _mediaCancellation.Cancel();
        AppCatalogService.CacheChanged -= OnCacheChanged;
        DownloadCenterService.StatusChanged -= OnDownloadCenterStatusChanged;
    }

    private void OnDownloadCenterStatusChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnDownloadCenterStatusChanged(sender, args)));
            return;
        }
        RefreshDownloadAction();
    }

    private async void RetryDetailsButton_OnClick(object sender, RoutedEventArgs e) => await LoadDetailsAsync(forceRefresh: true);

    private Task LoadDetailsAsync(bool forceRefresh = false)
    {
        if (_detailLoadTask is { IsCompleted: false })
        {
            return _detailLoadTask;
        }

        _detailLoadTask = LoadDetailsCoreAsync(forceRefresh);
        return _detailLoadTask;
    }

    private async Task LoadDetailsCoreAsync(bool forceRefresh)
    {
        _isLoading = true;
        _hasFreshDetails = false;
        var cancellation = new CancellationTokenSource();
        _detailLoadCancellation = cancellation;
        DetailLoadingPanel.Visibility = Visibility.Visible;
        DetailErrorPanel.Visibility = Visibility.Collapsed;
        RefreshDownloadAction();
        try
        {
            var appItem = await AppCatalogService.GetAppAsync(AppItem.Id, cancellation.Token, forceRefresh);
            cancellation.Token.ThrowIfCancellationRequested();
            AppItem = appItem;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(AppItem)));
            UpdateTagChips();
            UpdateScreenshots();
            UpdateStoreSections();
            UpdateDownloadLinkDisplay();
            _hasFreshDetails = true;
            _ = RefreshDownloadLinkAsync();
            if (AppCatalogService.CacheState.NetworkError is not null)
            {
                DetailErrorText.Text = AppCatalogService.CacheState.NetworkError + " 正在显示离线缓存；下载时仍需连接软件服务。";
                DetailErrorPanel.Visibility = Visibility.Visible;
            }
            RefreshIcon();
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            // Navigation cancels a detail request without interrupting an active download.
        }
        catch (Exception exception) when (exception is HttpRequestException or JsonException or IOException or InvalidDataException or OperationCanceledException)
        {
            AppLogService.Warn(
                $"加载软件详情失败：{AppItem.Name}（{AppItem.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            DetailErrorText.Text = "无法加载软件详情，请检查软件服务和网络连接后重试。";
            DetailErrorPanel.Visibility = Visibility.Visible;
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            // async void 事件处理器不能把未知异常抛到 Dispatcher，否则会直接崩溃。
            AppLogService.Error(
                $"加载软件详情出现未预期错误：{AppItem.Name}（{AppItem.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            DetailErrorText.Text = "无法加载软件详情，请稍后重试。";
            DetailErrorPanel.Visibility = Visibility.Visible;
        }
        finally
        {
            _isLoading = false;
            _detailLoadCancellation = null;
            cancellation.Dispose();
            DetailLoadingPanel.Visibility = Visibility.Collapsed;
            RefreshDownloadAction();
            DetailCacheStatusText.Text = AppCatalogService.CacheDescription;
        }
    }

    private void RefreshIcon()
    {
        AppIcon.ClearValue(VisibilityProperty);
        IconFallback.ClearValue(VisibilityProperty);
    }

    private async void AppIcon_OnImageFailed(object sender, ExceptionRoutedEventArgs e)
    {
        await AppCatalogService.DiscardIconAsync(AppItem);
    }

    private void OnCacheChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnCacheChanged(sender, args)));
            return;
        }
        DetailCacheStatusText.Text = AppCatalogService.CacheDescription;
    }

    /// <summary>当前应用是否来自联想应用商店源：这类应用即使目录里没有 LatestRelease 也能直接开始下载。</summary>
    private bool IsStoreSource =>
        string.Equals(AppItem.SourceId, StoreCatalogMapper.LenovoSourceId, StringComparison.OrdinalIgnoreCase)
        || (string.IsNullOrEmpty(AppItem.SourceId) && AppCatalogService.IsLenovoSource);

    private void RefreshDownloadAction()
    {
        bool isStoreSource = IsStoreSource;
        var task = DownloadCenterService.Find(AppItem.Id);
        DownloadButton.Content = task is not null
            ? task.IsActive ? "查看下载" : "在下载中心查看"
            : (AppItem.LatestRelease is not null || isStoreSource) ? "开始下载" : "前往官网下载 ↗";
        // 联想源的下载地址由下载中心实时解析，详情拉取失败但目录摘要里有 soft_id 时仍允许开始下载。
        DownloadButton.IsEnabled = !_isLoading
            && (_hasFreshDetails || (isStoreSource && !string.IsNullOrWhiteSpace(AppItem.SourceAppId)));
        InstallFlowText.Text = isStoreSource
            ? "安装包由联想应用商店提供，开始下载后可在下载中心查看进度；完成后可从下载中心打开安装包。"
            : AppItem.LatestRelease is not null
                ? "开始下载后可在下载中心查看进度；完成后可从下载中心打开安装包。"
                : "前往软件官网下载 Windows 安装包，按软件安装向导完成安装。";
    }

    private void DownloadButton_OnClick(object sender, RoutedEventArgs e)
    {
        var existing = DownloadCenterService.Find(AppItem.Id);
        if (existing is not null)
        {
            DownloadCenterRequested?.Invoke();
            return;
        }

        if (AppItem.LatestRelease is null && !IsStoreSource)
        {
            if (!ExternalLinkService.OpenWebsite(AppItem.DownloadPageUrl))
            {
                AppLogService.Warn($"打开软件下载页失败：{AppItem.Name}（{AppItem.Id}），地址无效或无法启动默认浏览器。");
            }
            return;
        }

        _linkLoadCancellation?.Cancel();
        DownloadCenterService.Start(AppItem);
        DownloadCenterRequested?.Invoke();
    }

    private void UpdateDownloadLinkDisplay()
    {
        var release = AppItem.LatestRelease;
        bool hasDirectLink = !string.IsNullOrWhiteSpace(release?.DownloadUrl);
        bool isStoreSource = IsStoreSource;
        string url = hasDirectLink ? release!.DownloadUrl : AppItem.DownloadPageUrl;
        bool hasLink = !string.IsNullOrWhiteSpace(url);
        DownloadLinkPanel.Visibility = hasLink ? Visibility.Visible : Visibility.Collapsed;
        DownloadLinkTextBox.Text = hasLink ? url : string.Empty;
        DownloadLinkLabelText.Text = hasDirectLink ? "下载链接" : "官网下载页";
        DownloadLinkHintText.Text = hasDirectLink
            ? isStoreSource
                ? "下载链接由联想应用商店提供，临时生成；开始下载时会重新解析最新地址。"
                : "链接由软件服务临时生成；复制或下载时会重新获取最新地址。"
            : isStoreSource
                ? "安装包由联想应用商店提供；点击“开始下载”后由下载中心获取下载地址。"
                : "该软件暂无安装包直链，以上为官网下载页地址。";
        OpenDownloadLinkButton.IsEnabled = hasLink;
        WebsiteButton.Visibility = string.IsNullOrWhiteSpace(AppItem.WebsiteUrl)
            ? Visibility.Collapsed : Visibility.Visible;
    }

    private async Task RefreshDownloadLinkAsync()
    {
        bool isStoreSource = IsStoreSource;
        if (AppItem.LatestRelease is null && !isStoreSource)
        {
            return;
        }

        _linkLoadCancellation?.Cancel();
        var cancellation = new CancellationTokenSource();
        _linkLoadCancellation = cancellation;
        DownloadLinkHintText.Text = "正在获取最新下载链接…";
        try
        {
            var release = await AppCatalogService.GetLatestReleaseAsync(AppItem, cancellation.Token);
            cancellation.Token.ThrowIfCancellationRequested();
            AppItem.LatestRelease = release;
            AppItem.Version = release.Version;
            PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(AppItem)));
            UpdateDownloadLinkDisplay();
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            // Navigating away or starting a download cancels this background refresh.
        }
        catch (Exception exception)
        {
            AppLogService.Warn(
                $"加载下载链接失败：{AppItem.Name}（{AppItem.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
            DownloadLinkHintText.Text = isStoreSource
                ? "暂时无法获取下载链接；仍可点击“开始下载”，下载中心会在开始时重新解析地址。"
                : "暂时无法获取最新下载链接；可点击“复制下载链接”重试。";
        }
        finally
        {
            if (ReferenceEquals(_linkLoadCancellation, cancellation))
            {
                _linkLoadCancellation = null;
            }
            cancellation.Dispose();
        }
    }

    private void OpenDownloadLinkButton_OnClick(object sender, RoutedEventArgs e)
    {
        string url = DownloadLinkTextBox.Text.Trim();
        if (url.Length > 0 && !ExternalLinkService.OpenWebsite(url))
        {
            AppLogService.Warn($"打开下载链接失败：{AppItem.Name}（{AppItem.Id}），地址无效或无法启动默认浏览器。");
        }
    }

    private async void CopyLinkButton_OnClick(object sender, RoutedEventArgs e)
    {
        if (_copyLinkInProgress)
        {
            return;
        }

        _copyLinkInProgress = true;
        var copyButton = sender as System.Windows.Controls.Button;
        bool wasEnabled = copyButton?.IsEnabled ?? true;
        if (copyButton is not null)
        {
            copyButton.IsEnabled = false;
        }
        using var cancellation = new CancellationTokenSource();
        _copyLinkCancellation = cancellation;
        _linkLoadCancellation?.Cancel();
        LinkStatusText.Text = "正在获取下载链接…";
        LinkStatusText.Visibility = Visibility.Visible;

        try
        {
            string url = AppItem.DownloadPageUrl;
            if (AppItem.LatestRelease is not null || IsStoreSource)
            {
                var release = await AppCatalogService.GetLatestReleaseAsync(AppItem, cancellation.Token);
                url = release.DownloadUrl;
                AppItem.LatestRelease = release;
                AppItem.Version = release.Version;
                PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(nameof(AppItem)));
                UpdateDownloadLinkDisplay();
            }
            cancellation.Token.ThrowIfCancellationRequested();
            if (string.IsNullOrWhiteSpace(url))
            {
                LinkStatusText.Text = "当前没有可复制的下载链接。";
                return;
            }
            Clipboard.SetText(url);
            LinkStatusText.Text = "已复制下载链接。";
        }
        catch (OperationCanceledException)
        {
            LinkStatusText.Text = cancellation.IsCancellationRequested
                ? "已取消获取下载链接。" : "获取下载链接超时，请重试。";
            if (cancellation.IsCancellationRequested)
            {
                AppLogService.Info($"已取消获取下载链接：{AppItem.Name}（{AppItem.Id}）。");
            }
            else
            {
                AppLogService.Warn($"获取下载链接超时：{AppItem.Name}（{AppItem.Id}）。");
            }
        }
        catch (HttpRequestException exception)
        {
            LinkStatusText.Text = exception.StatusCode.HasValue
                ? $"获取下载链接失败（HTTP {(int)exception.StatusCode.Value}），请稍后重试。"
                : "无法连接软件服务，请检查连接后重试。";
            AppLogService.Warn(
                $"获取下载链接失败：{AppItem.Name}（{AppItem.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (JsonException exception)
        {
            LinkStatusText.Text = "下载链接信息无效，请刷新软件详情后重试。";
            AppLogService.Warn(
                $"下载链接信息无效：{AppItem.Name}（{AppItem.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (IOException exception)
        {
            LinkStatusText.Text = "读取下载链接时连接中断，请重试。";
            AppLogService.Warn(
                $"读取下载链接时连接中断：{AppItem.Name}（{AppItem.Id}），{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (ExternalException exception)
        {
            LinkStatusText.Text = "剪贴板暂时不可用，请稍后重试。";
            AppLogService.Warn($"剪贴板暂时不可用：{AppItem.Name}（{AppItem.Id}），{exception.Message}");
        }
        finally
        {
            _copyLinkCancellation = null;
            _copyLinkInProgress = false;
            if (copyButton is not null)
            {
                copyButton.IsEnabled = wasEnabled;
            }
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 标签、更新说明与截图相册（联想源补充展示）
    // ---------------------------------------------------------------------------------------------

    private void UpdateTagChips()
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var chips = new List<string>();
        foreach (string value in AppItem.Labels.Concat(AppItem.Tags))
        {
            string text = value?.Trim() ?? string.Empty;
            if (text.Length > 0 && seen.Add(text))
            {
                chips.Add(text);
            }
        }

        TagChips = chips.AsReadOnly();
    }

    private void UpdateStoreSections()
    {
        ReleaseNotesCard.Visibility = string.IsNullOrWhiteSpace(AppItem.ReleaseNotes)
            ? Visibility.Collapsed : Visibility.Visible;
        if (Screenshots.Count == 0)
        {
            ScreenshotCard.Visibility = Visibility.Collapsed;
        }
        RefreshDownloadAction();
    }

    private void UpdateScreenshots()
    {
        _mediaCancellation.Cancel();
        _mediaCancellation = new CancellationTokenSource();
        CancellationToken token = _mediaCancellation.Token;
        Screenshots.Clear();

        var items = new List<AppScreenshot>();
        var seenUrls = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        // 横版封面作为相册第一张；它和左上角的 76px 应用图标不同，不会重复。
        if (!string.IsNullOrWhiteSpace(AppItem.CoverUrl) && seenUrls.Add(AppItem.CoverUrl))
        {
            items.Add(new AppScreenshot { Index = -1, Kind = "image", Url = AppItem.CoverUrl });
        }
        foreach (var screenshot in AppItem.Screenshots)
        {
            if (string.IsNullOrWhiteSpace(screenshot.Url) || !seenUrls.Add(screenshot.Url))
            {
                continue;
            }
            items.Add(screenshot);
        }

        _viewerScreenshots = items.AsReadOnly();
        foreach (var screenshot in items)
        {
            var item = new ScreenshotViewItem(screenshot, isCover: screenshot.Index < 0);
            Screenshots.Add(item);
            _ = LoadScreenshotImageAsync(item, token);
        }

        ScreenshotCard.Visibility = items.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        ScreenshotHintText.Text = items.Count == 0
            ? string.Empty
            : (items.Any(item => item.IsVideo)
                ? "点击缩略图打开预览；视频条目可点击播放。"
                : "点击缩略图打开预览。");
    }

    private async Task LoadScreenshotImageAsync(ScreenshotViewItem item, CancellationToken ct)
    {
        try
        {
            // 相册只显示缩略图，按显示宽度解码，避免每张截图都保留整张 1280×720 位图。
            var image = await StoreMediaService.GetImageAsync(item.Url, ScreenshotThumbnailDecodeWidth, ct);
            ct.ThrowIfCancellationRequested();
            if (image is null)
            {
                item.MarkFailed();
            }
            else
            {
                item.SetImage(image);
            }
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // 离开详情页会取消尚未完成的截图下载。
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            item.MarkFailed();
            Debug.WriteLine(error);
        }
    }

    private void ScreenshotThumb_OnMouseLeftButtonUp(object sender, MouseButtonEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: ScreenshotViewItem item })
        {
            int index = Screenshots.IndexOf(item);
            if (index >= 0 && _viewerScreenshots.Count > 0)
            {
                // 视频播放走本机后端的联想视频代理，需要原始 soft_id；内置精选源没有视频，传 null。
                var viewer = new ScreenshotViewerWindow(_viewerScreenshots, index, ResolveLenovoSoftId())
                {
                    Owner = Window.GetWindow(this)
                };
                viewer.ShowDialog();
            }
            args.Handled = true;
        }
    }

    /// <summary>
    /// 当前详情对应的联想商店 <c>soft_id</c>（区分大小写）；内置精选或详情来自旧缓存（没有源标识）时返回
    /// <see langword="null"/>，预览窗口据此不显示视频播放器，只保留缩略图。
    /// </summary>
    private string? ResolveLenovoSoftId()
    {
        if (!string.Equals(AppItem.SourceId, StoreCatalogMapper.LenovoSourceId, StringComparison.OrdinalIgnoreCase))
        {
            return null;
        }

        string softId = AppItem.SourceAppId?.Trim() ?? string.Empty;
        return softId.Length == 0 ? null : softId;
    }

    /// <summary>相册只做横向滚动；鼠标位于相册上方且已滚到边界时，把纵向滚轮交还给整页。</summary>
    private void ScreenshotScrollViewer_OnPreviewMouseWheel(object sender, MouseWheelEventArgs args)
    {
        if (args.Handled || sender is not ScrollViewer viewer)
        {
            return;
        }

        bool canScrollHorizontally = viewer.ScrollableWidth > 0
            && ((args.Delta > 0 && viewer.HorizontalOffset > 0)
                || (args.Delta < 0 && viewer.HorizontalOffset < viewer.ScrollableWidth));
        if (canScrollHorizontally)
        {
            viewer.ScrollToHorizontalOffset(viewer.HorizontalOffset - Math.Sign(args.Delta) * 64);
            args.Handled = true;
            return;
        }

        args.Handled = true;
        PageScrollViewer.RaiseEvent(new MouseWheelEventArgs(args.MouseDevice, args.Timestamp, args.Delta)
        {
            RoutedEvent = UIElement.MouseWheelEvent,
            Source = PageScrollViewer
        });
    }
}

/// <summary>详情页截图相册的一张图片：异步加载状态与可绑定的 <see cref="ImageSource"/>。</summary>
public sealed class ScreenshotViewItem : INotifyPropertyChanged
{
    private ImageSource? _image;
    private bool _failed;

    public ScreenshotViewItem(AppScreenshot screenshot, bool isCover)
    {
        Screenshot = screenshot ?? throw new ArgumentNullException(nameof(screenshot));
        IsCover = isCover;
    }

    public AppScreenshot Screenshot { get; }

    public bool IsCover { get; }

    public string Url => Screenshot.Url;

    public bool IsVideo => Screenshot.IsVideo;

    public string AccessibilityName => IsVideo ? "视频缩略图，点击播放视频" : "应用截图，点击查看大图";

    public ImageSource? Image => _image;

    public bool HasImage => _image is not null;

    public bool IsLoading => _image is null && !_failed;

    public string PlaceholderText => _failed ? "图片无法加载" : "加载中…";

    public event PropertyChangedEventHandler? PropertyChanged;

    public void SetImage(ImageSource image)
    {
        _image = image;
        _failed = false;
        Raise(nameof(Image));
        Raise(nameof(HasImage));
        Raise(nameof(IsLoading));
        Raise(nameof(PlaceholderText));
    }

    public void MarkFailed()
    {
        _failed = true;
        Raise(nameof(IsLoading));
        Raise(nameof(PlaceholderText));
    }

    private void Raise(string name) => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
