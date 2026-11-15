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

    /// <summary>当前软件来源是否声明提供安装包下载能力（能力来自服务端 <c>/sources</c>，不按来源 ID 判断）。</summary>
    private bool CanDownloadFromServer => AppCatalogService.SupportsDownload(AppItem.SourceId);

    private string SourceNameText => string.IsNullOrWhiteSpace(AppItem.SourceName)
        ? AppCatalogService.SourceDisplayName
        : AppItem.SourceName!;

    private void RefreshDownloadAction()
    {
        bool canDownload = CanDownloadFromServer;
        var task = DownloadCenterService.Find(AppItem);
        DownloadButton.Content = task is not null
            ? task.IsActive ? "查看下载" : "在下载中心查看"
            : canDownload ? "开始下载" : "前往官网下载 ↗";
        // 下载地址由下载中心在开始时按“软件 ID + 来源 ID”实时解析，详情拉取失败也仍可开始下载。
        DownloadButton.IsEnabled = !_isLoading && (canDownload || AppItem.LatestRelease is not null || _hasFreshDetails);
        InstallFlowText.Text = canDownload
            ? $"安装包由{SourceNameText}提供，开始下载后可在下载中心查看进度；完成后可从下载中心打开安装包。"
            : "前往软件官网下载 Windows 安装包，按软件安装向导完成安装。";
    }

    private void DownloadButton_OnClick(object sender, RoutedEventArgs e)
    {
        var existing = DownloadCenterService.Find(AppItem);
        if (existing is not null)
        {
            DownloadCenterRequested?.Invoke();
            return;
        }

        if (!CanDownloadFromServer && AppItem.LatestRelease is null)
        {
            // 当前来源不提供下载能力：打开服务端返回的官网/下载页。
            bool opened = ExternalLinkService.OpenWebsite(AppItem.DownloadPageUrl)
                          || ExternalLinkService.OpenWebsite(AppItem.WebsiteUrl);
            if (!opened)
            {
                AppLogService.Warn(
                    $"打开软件官网失败：{AppItem.Name}（{AppItem.Id}），当前来源没有提供下载地址，官网地址为空或无法启动浏览器。");
                LinkStatusText.Text = "当前来源没有提供安装包下载地址，也没有可打开的官网页面。";
                LinkStatusText.Visibility = Visibility.Visible;
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
        bool canDownload = CanDownloadFromServer;
        string url = hasDirectLink ? release!.DownloadUrl : AppItem.DownloadPageUrl;
        bool hasLink = !string.IsNullOrWhiteSpace(url);
        DownloadLinkPanel.Visibility = hasLink ? Visibility.Visible : Visibility.Collapsed;
        DownloadLinkTextBox.Text = hasLink ? url : string.Empty;
        DownloadLinkLabelText.Text = hasDirectLink ? "下载链接" : "官网下载页";
        DownloadLinkHintText.Text = hasDirectLink
            ? "下载链接由软件服务临时生成；开始下载时会重新解析最新地址。"
            : canDownload
                ? "点击“开始下载”后由下载中心获取下载地址。"
                : "该软件暂无安装包直链，以上为官网下载页地址。";
        OpenDownloadLinkButton.IsEnabled = hasLink;
        WebsiteButton.Visibility = string.IsNullOrWhiteSpace(AppItem.WebsiteUrl)
            ? Visibility.Collapsed : Visibility.Visible;
    }

    private async Task RefreshDownloadLinkAsync()
    {
        bool canDownload = CanDownloadFromServer;
        if (AppItem.LatestRelease is null && !canDownload)
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
            DownloadLinkHintText.Text = canDownload
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
            if (AppItem.LatestRelease is not null || CanDownloadFromServer)
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
    // 标签、更新说明与截图相册（来源提供时展示）
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
        var seenKeys = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        // 横版封面作为相册第一张；它和左上角的 76px 应用图标不同，不会重复。
        // 封面与服务端返回的截图都可能只有直连地址、只有代理兜底，或两者都有。
        if (!string.IsNullOrWhiteSpace(AppItem.CoverUrl) || !string.IsNullOrWhiteSpace(AppItem.CoverProxyUrl))
        {
            var cover = new AppScreenshot
            {
                Index = -1,
                Kind = "image",
                Url = AppItem.CoverUrl ?? string.Empty,
                ProxyUrl = AppItem.CoverProxyUrl
            };
            if (seenKeys.Add(ScreenshotKey(cover)))
            {
                items.Add(cover);
            }
        }

        foreach (var screenshot in AppItem.Screenshots)
        {
            if ((string.IsNullOrWhiteSpace(screenshot.Url) && string.IsNullOrWhiteSpace(screenshot.ProxyUrl))
                || !seenKeys.Add(ScreenshotKey(screenshot)))
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

    /// <summary>截图去重键：优先直连地址，只有代理地址时用代理地址（避免代理-only 条目被误判重复）。</summary>
    private static string ScreenshotKey(AppScreenshot screenshot) =>
        !string.IsNullOrWhiteSpace(screenshot.Url) ? screenshot.Url : "proxy:" + screenshot.ProxyUrl;

    private async Task LoadScreenshotImageAsync(ScreenshotViewItem item, CancellationToken ct)
    {
        try
        {
            // 相册只显示缩略图，按显示宽度解码，避免每张截图都保留整张 1280×720 位图。
            // 优先直连商店 CDN（Url），失败再回退后端代理（ProxyUrl），减少后端图片带宽。
            var image = await StoreMediaService.GetImageAsync(
                item.Url, item.ProxyUrl, ScreenshotThumbnailDecodeWidth, ct);
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
                // 视频条目直接使用服务端详情返回的 video_proxy_url，客户端不拼来源专属路径。
                var viewer = new ScreenshotViewerWindow(_viewerScreenshots, index)
                {
                    Owner = Window.GetWindow(this)
                };
                viewer.ShowDialog();
            }
            args.Handled = true;
        }
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

    /// <summary>后端代理兜底地址（直连 <see cref="Url"/> 失败时由 StoreMediaService 使用），可能为空。</summary>
    public string? ProxyUrl => Screenshot.ProxyUrl;

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
