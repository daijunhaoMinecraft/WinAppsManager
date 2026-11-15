using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using System.Windows.Threading;
using WinAppsManager.Models;
using WinAppsManager.Services;

namespace WinAppsManager.Dialogs;

/// <summary>
/// 联想应用商店截图/视频预览窗口：深色对话框外观、上一张/下一张、Esc 关闭，
/// 键盘 Left/Right/Home/End 导航；图片由 <see cref="StoreMediaService"/> 异步加载并复用其内存/磁盘缓存。
/// 大图优先直连联想 CDN（<see cref="AppScreenshot.Url"/>），失败回退后端代理路径（<see cref="AppScreenshot.ProxyUrl"/>），
/// 与详情页缩略图使用同一套直连 + 回退链路。
/// <para>
/// 视频条目（<see cref="AppScreenshot.IsVideo"/>）额外提供 <see cref="MediaElement"/> 播放器：
/// 媒体源是后端联想视频代理地址（<see cref="AppCatalogService.TryBuildLenovoVideoUrl"/>，客户端不直连 CDN），
/// 缩略图在播放前充当海报；播放/暂停、拖动进度、静音、浏览器兜底与错误提示都在控件条内完成。
/// 切换到其它条目或关闭窗口时必须 <c>Stop()</c>、清空 <c>Source</c> 并停止计时器，避免媒体句柄泄漏。
/// 只负责展示，不触碰下载任务，也不会从后台线程访问 UI。
/// </para>
/// </summary>
public partial class ScreenshotViewerWindow : Window
{
    /// <summary>视频默认音量（0–1）；用户可用静音按钮关闭声音。</summary>
    private const double DefaultVideoVolume = 0.8;

    private const string IdleVideoStatus = "点击播放按钮观看视频。";

    private readonly IReadOnlyList<AppScreenshot> _screenshots;
    private readonly List<ImageSource?> _images;
    private readonly HashSet<int> _loading = new();
    private readonly CancellationTokenSource _cancellation = new();
    private readonly DispatcherTimer _progressTimer;
    private readonly string? _lenovoSoftId;
    private int _index;

    // ---- 视频播放状态：_videoUrl 是当前条目的后端代理地址，null 表示不是视频或参数不可用（只显示缩略图）。 ----
    private string? _videoUrl;
    private bool _videoReady;          // MediaOpened 已触发：可以播放、暂停与定位。
    private bool _videoFailed;         // MediaFailed 或加载异常：显示浏览器兜底，禁止再播放。
    private bool _durationKnown;       // 媒体是否给出了总时长（流式视频可能未知，此时进度条禁用）。
    private bool _isPlaying;           // 期望的播放状态（含正在加载）。
    private bool _playbackEnded;
    private bool _isDraggingProgress;  // 拖动进度条期间暂停计时刷新。
    private bool _suppressSliderChange;
    private bool _releasingMedia;      // 主动清理期间忽略 MediaElement 的异步事件。
    private string _idleVideoStatus = IdleVideoStatus;

    public ScreenshotViewerWindow(IReadOnlyList<AppScreenshot> screenshots, int startIndex, string? lenovoSoftId = null)
    {
        ArgumentNullException.ThrowIfNull(screenshots);
        InitializeComponent();

        _screenshots = screenshots;
        _lenovoSoftId = string.IsNullOrWhiteSpace(lenovoSoftId) ? null : lenovoSoftId.Trim();
        _images = new List<ImageSource?>(screenshots.Count);
        for (int i = 0; i < screenshots.Count; i++)
        {
            _images.Add(null);
        }

        _index = screenshots.Count == 0 ? 0 : Math.Clamp(startIndex, 0, screenshots.Count - 1);

        _progressTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(250) };
        _progressTimer.Tick += OnProgressTimerTick;
        ProgressSlider.ValueChanged += ProgressSlider_OnValueChanged;
        ProgressSlider.AddHandler(Thumb.DragStartedEvent, new DragStartedEventHandler(ProgressSlider_OnDragStarted));
        ProgressSlider.AddHandler(Thumb.DragCompletedEvent, new DragCompletedEventHandler(ProgressSlider_OnDragCompleted));

        // WPF 的 MediaElement 在 AllowsTransparency=True 的分层窗口里通常只能出声、视频画面为黑屏，
        // 因此相册含可播放视频时改用普通不透明窗口（用主题底色铺满，圆角卡片布局保持不变）；
        // 纯图片相册仍使用原来的透明分层窗口外观。
        if (HasPlayableVideoCandidate())
        {
            AllowsTransparency = false;
            SetResourceReference(Control.BackgroundProperty, "Wam.PageBackgroundBrush");
        }

        Loaded += OnWindowLoaded;
        Closing += OnWindowClosing;
        Closed += OnWindowClosed;
        UpdateNavigation();
    }

    private void OnWindowLoaded(object sender, RoutedEventArgs args)
    {
        UpdateCurrent();
        _ = EnsureImageAsync(_index);
        _ = EnsureImageAsync(_index + 1);
        _ = EnsureImageAsync(_index - 1);
        CloseButton.Focus();
    }

    private void OnWindowClosing(object? sender, CancelEventArgs args)
    {
        // 关闭前必须停止并释放媒体，否则 MediaElement 会一直占用文件/网络句柄。
        ReleaseVideoPlayback();
    }

    private void OnWindowClosed(object? sender, EventArgs args)
    {
        _cancellation.Cancel();
        ReleaseVideoPlayback();
    }

    private void CloseButton_OnClick(object sender, RoutedEventArgs args) => Close();

    private void PreviousButton_OnClick(object sender, RoutedEventArgs args) => ShowIndex(_index - 1);

    private void NextButton_OnClick(object sender, RoutedEventArgs args) => ShowIndex(_index + 1);

    private void Header_OnMouseLeftButtonDown(object sender, MouseButtonEventArgs args)
    {
        if (args.ButtonState == MouseButtonState.Pressed)
        {
            try
            {
                DragMove();
            }
            catch (InvalidOperationException)
            {
                // 鼠标已松开时 DragMove 会抛异常；无边框窗口拖动失败不影响预览。
            }
        }
    }

    private void Window_OnPreviewKeyDown(object sender, KeyEventArgs args)
    {
        switch (args.Key)
        {
            case Key.Left:
                ShowIndex(_index - 1);
                args.Handled = true;
                break;
            case Key.Right:
                ShowIndex(_index + 1);
                args.Handled = true;
                break;
            case Key.Home:
                ShowIndex(0);
                args.Handled = true;
                break;
            case Key.End:
                ShowIndex(_screenshots.Count - 1);
                args.Handled = true;
                break;
            case Key.Space:
                // 只有视频条目且地址可用时才抢占 Space；图片预览保持原有按键行为。
                if (_videoUrl is not null && !_videoFailed)
                {
                    TogglePlayback();
                    args.Handled = true;
                }
                break;
            case Key.Escape:
                Close();
                args.Handled = true;
                break;
        }
    }

    private void ShowIndex(int index)
    {
        if (_screenshots.Count == 0)
        {
            return;
        }

        int target = Math.Clamp(index, 0, _screenshots.Count - 1);
        if (target == _index)
        {
            return;
        }

        _index = target;
        UpdateCurrent();
        _ = EnsureImageAsync(_index);
        _ = EnsureImageAsync(_index + 1);
        _ = EnsureImageAsync(_index - 1);
    }

    private void UpdateNavigation()
    {
        if (_screenshots.Count == 0)
        {
            CounterText.Text = string.Empty;
            PreviousButton.IsEnabled = false;
            NextButton.IsEnabled = false;
            return;
        }

        CounterText.Text = $"{_index + 1} / {_screenshots.Count}";
        PreviousButton.IsEnabled = _index > 0;
        NextButton.IsEnabled = _index < _screenshots.Count - 1;
    }

    private void UpdateCurrent()
    {
        // 任何切换都先释放上一个条目的媒体（视频切视频时也同样），避免旧 MediaElement 继续解码。
        ReleaseVideoPlayback();

        if (_screenshots.Count == 0)
        {
            PreviewImage.Source = null;
            StatusPanel.Visibility = Visibility.Visible;
            LoadingRing.IsActive = false;
            StatusText.Text = "没有可预览的图片。";
            VideoBadge.Visibility = Visibility.Collapsed;
            HeaderTitleText.Text = "截图预览";
            Title = "截图预览";
            VideoControls.Visibility = Visibility.Collapsed;
            HintText.Text = "← / → 切换，Home / End 跳到首尾，Esc 关闭";
            return;
        }

        AppScreenshot screenshot = _screenshots[_index];
        bool isVideo = screenshot.IsVideo;
        // 封面（Index = -1）是图片；视频需要联想 soft_id + 原始截图索引才能拼出后端代理地址。
        _videoUrl = isVideo && screenshot.Index >= 0
            ? AppCatalogService.TryBuildLenovoVideoUrl(_lenovoSoftId, screenshot.Index)
            : null;

        HeaderTitleText.Text = isVideo ? "视频预览" : "截图预览";
        Title = isVideo ? "视频预览" : "截图预览";
        VideoBadge.Visibility = isVideo ? Visibility.Visible : Visibility.Collapsed;
        HintText.Text = isVideo
            ? _videoUrl is not null
                ? "视频由本机软件服务代理播放 · Space 播放 / 暂停，← / → 切换，Home / End 首尾，Esc 关闭"
                : "视频条目只提供联想应用商店的视频缩略图 · ← / → 切换，Home / End 首尾，Esc 关闭"
            : "← / → 切换，Home / End 跳到首尾，Esc 关闭";

        if (_videoUrl is not null)
        {
            PrepareVideoControls();
        }
        else
        {
            VideoControls.Visibility = Visibility.Collapsed;
        }

        UpdateImageDisplay();
        UpdateNavigation();
    }

    private void UpdateImageDisplay()
    {
        ImageSource? image = _index >= 0 && _index < _images.Count ? _images[_index] : null;
        if (image is not null)
        {
            PreviewImage.Source = image;
            StatusPanel.Visibility = Visibility.Collapsed;
            _idleVideoStatus = IdleVideoStatus;
            ApplyIdleVideoStatus();
            return;
        }

        PreviewImage.Source = null;
        if (IsCurrentVideo())
        {
            // 视频条目的缩略图状态写进控件条，避免与中央播放按钮/错误提示重叠。
            StatusPanel.Visibility = Visibility.Collapsed;
            _idleVideoStatus = _loading.Contains(_index)
                ? "正在加载视频封面…"
                : "视频封面暂时无法加载，仍可点击播放。";
            ApplyIdleVideoStatus();
            return;
        }

        bool loading = _loading.Contains(_index);
        StatusPanel.Visibility = Visibility.Visible;
        LoadingRing.IsActive = loading;
        StatusText.Text = loading ? "正在加载图片…" : "这张图片暂时无法加载。";
    }

    /// <summary>仅在视频还没开始加载/播放且没有失败时刷新控件条的待机提示，避免覆盖播放状态。</summary>
    private void ApplyIdleVideoStatus()
    {
        if (_videoUrl is null || _videoReady || _videoFailed || _isPlaying)
        {
            return;
        }

        SetVideoStatus(_idleVideoStatus, isError: false);
    }

    private async Task EnsureImageAsync(int index)
    {
        if (index < 0 || index >= _screenshots.Count)
        {
            return;
        }

        if (_images[index] is not null || !_loading.Add(index))
        {
            return;
        }

        if (index == _index)
        {
            // 立即把“正在加载图片/视频封面”的占位状态显示出来，而不是等加载结束才刷新。
            UpdateImageDisplay();
        }

        try
        {
            // 大图按原始尺寸加载：优先直连商店 CDN（AppScreenshot.Url），失败再回退后端代理（ProxyUrl）。
            AppScreenshot screenshot = _screenshots[index];
            var image = await StoreMediaService.GetImageAsync(
                screenshot.Url, screenshot.ProxyUrl, 0, _cancellation.Token);
            _cancellation.Token.ThrowIfCancellationRequested();
            if (image is not null)
            {
                _images[index] = image;
            }
        }
        catch (OperationCanceledException) when (_cancellation.IsCancellationRequested)
        {
            // 关闭预览窗口会取消尚未完成的图片加载。
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // 加载失败由占位文字降级显示；日志只在详细模式下记录，避免多张坏图刷屏。
            AppLogService.Verbose(
                $"预览图片加载失败：第 {index + 1} / {_screenshots.Count} 张，{error.GetType().Name}：{error.Message}",
                error);
            Debug.WriteLine(error);
        }
        finally
        {
            _loading.Remove(index);
            if (index == _index)
            {
                UpdateImageDisplay();
            }
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 视频播放器
    // ---------------------------------------------------------------------------------------------

    private bool IsCurrentVideo() =>
        _index >= 0 && _index < _screenshots.Count && _screenshots[_index].IsVideo;

    /// <summary>相册里是否至少有一个可通过后端代理播放的视频条目（决定是否需要关闭窗口透明层）。</summary>
    private bool HasPlayableVideoCandidate()
    {
        if (_lenovoSoftId is null)
        {
            return false;
        }

        foreach (AppScreenshot screenshot in _screenshots)
        {
            if (screenshot.IsVideo && screenshot.Index >= 0)
            {
                return true;
            }
        }

        return false;
    }

    private void PrepareVideoControls()
    {
        _videoReady = false;
        _videoFailed = false;
        _durationKnown = false;
        _isPlaying = false;
        _playbackEnded = false;
        _isDraggingProgress = false;
        _idleVideoStatus = IdleVideoStatus;

        VideoControls.Visibility = Visibility.Visible;
        VideoPlayer.Visibility = Visibility.Collapsed;
        CenterPlayButton.Visibility = Visibility.Visible;
        CenterPlayButton.IsEnabled = true;
        VideoErrorPanel.Visibility = Visibility.Collapsed;
        PlayPauseButton.IsEnabled = true;

        VideoLoadingRing.IsActive = false;
        VideoLoadingRing.Visibility = Visibility.Collapsed;
        VideoPlayer.Volume = DefaultVideoVolume;
        VideoPlayer.IsMuted = false;
        MuteButton.Content = "静音";

        // 打开媒体前不知道总时长，进度条先禁用，MediaOpened 后再按 NaturalDuration 启用。
        ProgressSlider.IsEnabled = false;
        _suppressSliderChange = true;
        try
        {
            ProgressSlider.Maximum = 1;
            ProgressSlider.Value = 0;
        }
        finally
        {
            _suppressSliderChange = false;
        }

        TimeText.Text = "0:00 / 0:00";
        UpdatePlayPauseIcon();
        SetVideoStatus(IdleVideoStatus, isError: false);
    }

    private void CenterPlayButton_OnClick(object sender, RoutedEventArgs args) => StartPlayback();

    private void PlayPauseButton_OnClick(object sender, RoutedEventArgs args) => TogglePlayback();

    private void TogglePlayback()
    {
        if (_videoUrl is null || _videoFailed)
        {
            return;
        }

        if (_isPlaying)
        {
            PausePlayback();
        }
        else
        {
            StartPlayback();
        }
    }

    private void StartPlayback()
    {
        if (_videoUrl is null || _videoFailed)
        {
            return;
        }

        if (_videoReady)
        {
            if (_playbackEnded)
            {
                _playbackEnded = false;
                TrySetPosition(TimeSpan.Zero);
            }

            try
            {
                VideoPlayer.Volume = DefaultVideoVolume;
                VideoPlayer.Play();
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                AppLogService.Warn(
                    $"视频预览播放失败：{VideoLogContext()}，{error.GetType().Name}：{error.Message}", error);
                MarkVideoFailed();
                return;
            }

            _isPlaying = true;
            UpdatePlayPauseIcon();
            CenterPlayButton.Visibility = Visibility.Collapsed;
            StartProgressTimer();
            SetVideoStatus("正在播放", isError: false);
            return;
        }

        if (_isPlaying)
        {
            // 已经在请求加载，忽略重复的播放指令。
            return;
        }

        _isPlaying = true;
        _playbackEnded = false;
        CenterPlayButton.Visibility = Visibility.Collapsed;
        PlayPauseButton.IsEnabled = false;
        UpdatePlayPauseIcon();
        VideoLoadingRing.Visibility = Visibility.Visible;
        VideoLoadingRing.IsActive = true;
        SetVideoStatus("正在加载视频…", isError: false);

        try
        {
            // 媒体源始终是后端代理地址（含 Range 支持），不直接访问联想 CDN。
            VideoPlayer.Volume = DefaultVideoVolume;
            VideoPlayer.Source = new Uri(_videoUrl, UriKind.Absolute);
            VideoPlayer.Play();
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            AppLogService.Warn(
                $"视频预览加载失败：{VideoLogContext()}，{error.GetType().Name}：{error.Message}", error);
            MarkVideoFailed();
        }
    }

    private void PausePlayback()
    {
        if (!_videoReady || _videoFailed)
        {
            return;
        }

        try
        {
            VideoPlayer.Pause();
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            Debug.WriteLine(error);
            return;
        }

        _isPlaying = false;
        StopProgressTimer();
        UpdatePlayPauseIcon();
        CenterPlayButton.Visibility = Visibility.Visible;
        UpdateProgressFromPosition();
        SetVideoStatus("已暂停", isError: false);
    }

    private void MuteButton_OnClick(object sender, RoutedEventArgs args)
    {
        VideoPlayer.IsMuted = !VideoPlayer.IsMuted;
        MuteButton.Content = VideoPlayer.IsMuted ? "取消静音" : "静音";
    }

    private void BrowserButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (_videoUrl is null)
        {
            return;
        }

        // 兜底：用系统浏览器打开同一个后端代理地址（后端支持 Range，浏览器可以正常拖动进度）。
        if (!ExternalLinkService.OpenWebsite(_videoUrl))
        {
            AppLogService.Warn($"无法在浏览器中打开视频代理地址：{VideoLogContext()}。");
        }
    }

    private void VideoPlayer_OnMediaOpened(object sender, RoutedEventArgs args)
    {
        if (_releasingMedia || _videoUrl is null || VideoPlayer.Source is null)
        {
            return;
        }

        _videoReady = true;
        _videoFailed = false;
        _playbackEnded = false;
        _isPlaying = true;

        VideoPlayer.Visibility = Visibility.Visible;
        StatusPanel.Visibility = Visibility.Collapsed;
        VideoLoadingRing.IsActive = false;
        VideoLoadingRing.Visibility = Visibility.Collapsed;
        CenterPlayButton.Visibility = Visibility.Collapsed;
        PlayPauseButton.IsEnabled = true;

        Duration natural = VideoPlayer.NaturalDuration;
        _durationKnown = natural.HasTimeSpan;
        // 流式视频可能没有总时长：此时禁用进度条、只显示已播放时间，浏览器兜底仍可拖动。
        ProgressSlider.IsEnabled = _durationKnown;
        double seconds = _durationKnown ? Math.Max(1d, natural.TimeSpan.TotalSeconds) : 1d;
        _suppressSliderChange = true;
        try
        {
            ProgressSlider.Maximum = seconds;
        }
        finally
        {
            _suppressSliderChange = false;
        }

        UpdatePlayPauseIcon();
        UpdateProgressFromPosition();
        StartProgressTimer();
        SetVideoStatus("正在播放", isError: false);
    }

    private void VideoPlayer_OnMediaEnded(object sender, RoutedEventArgs args)
    {
        if (_releasingMedia || _videoUrl is null || VideoPlayer.Source is null)
        {
            return;
        }

        _isPlaying = false;
        _playbackEnded = true;
        StopProgressTimer();
        UpdateProgressFromPosition();
        UpdatePlayPauseIcon();
        CenterPlayButton.Visibility = Visibility.Visible;
        SetVideoStatus("播放结束，点击播放可重新观看。", isError: false);
    }

    private void VideoPlayer_OnMediaFailed(object sender, ExceptionRoutedEventArgs args)
    {
        if (_releasingMedia || _videoUrl is null || VideoPlayer.Source is null)
        {
            return;
        }

        Exception? error = args.ErrorException;
        AppLogService.Warn(
            $"视频预览播放失败：{VideoLogContext()}，{error?.GetType().Name ?? "MediaFailed"}：{error?.Message ?? "未知错误"}",
            error);
        MarkVideoFailed();
    }

    /// <summary>播放失败：隐藏播放按钮，显示“视频暂时无法播放”，保留浏览器兜底按钮。</summary>
    private void MarkVideoFailed()
    {
        _videoFailed = true;
        _videoReady = false;
        _isPlaying = false;
        _playbackEnded = false;
        StopProgressTimer();
        ReleaseMediaSource();

        VideoPlayer.Visibility = Visibility.Collapsed;
        VideoLoadingRing.IsActive = false;
        VideoLoadingRing.Visibility = Visibility.Collapsed;
        CenterPlayButton.Visibility = Visibility.Collapsed;
        PlayPauseButton.IsEnabled = false;
        ProgressSlider.IsEnabled = false;
        VideoErrorPanel.Visibility = Visibility.Visible;
        UpdatePlayPauseIcon();
        SetVideoStatus("视频暂时无法播放，可点击“在浏览器打开”观看。", isError: true);
    }

    private void VideoPlayer_OnBufferingStarted(object sender, RoutedEventArgs args)
    {
        if (_releasingMedia || _videoUrl is null || VideoPlayer.Source is null || _videoFailed)
        {
            return;
        }

        VideoLoadingRing.Visibility = Visibility.Visible;
        VideoLoadingRing.IsActive = true;
        SetVideoStatus("正在缓冲…", isError: false);
    }

    private void VideoPlayer_OnBufferingEnded(object sender, RoutedEventArgs args)
    {
        if (_releasingMedia || _videoUrl is null || VideoPlayer.Source is null || _videoFailed)
        {
            return;
        }

        VideoLoadingRing.IsActive = false;
        VideoLoadingRing.Visibility = Visibility.Collapsed;
        SetVideoStatus(_isPlaying ? "正在播放" : "已暂停", isError: false);
    }

    private void UpdatePlayPauseIcon()
    {
        bool showPause = _isPlaying && _videoReady && !_videoFailed;
        PlayIcon.Visibility = showPause ? Visibility.Collapsed : Visibility.Visible;
        PauseIcon.Visibility = showPause ? Visibility.Visible : Visibility.Collapsed;
    }

    private void SetVideoStatus(string text, bool isError)
    {
        VideoStatusText.Text = text;
        string brushKey = isError ? "Wam.DangerBrush" : "Wam.TextSecondaryBrush";
        if (TryFindResource(brushKey) is Brush brush)
        {
            VideoStatusText.Foreground = brush;
        }
    }

    // ---- 进度条与计时器 ----

    private void ProgressSlider_OnValueChanged(object sender, RoutedPropertyChangedEventArgs<double> args)
    {
        if (_suppressSliderChange)
        {
            return;
        }

        if (_isDraggingProgress)
        {
            UpdateTimeText(TimeSpan.FromSeconds(ProgressSlider.Value), CurrentDuration());
            return;
        }

        if (!_videoReady || _videoFailed)
        {
            return;
        }

        // 不立即从 MediaElement 读回位置：seek 是异步的，读回旧值会让滑块“弹回”；播放时由计时器刷新。
        double seconds = ProgressSlider.Value;
        TrySetPosition(TimeSpan.FromSeconds(seconds));
        UpdateTimeText(TimeSpan.FromSeconds(seconds), CurrentDuration());
    }

    private void ProgressSlider_OnDragStarted(object sender, DragStartedEventArgs args)
    {
        // 拖动期间暂停计时刷新，只在拖动结束时 seek 一次，避免频繁定位和解码抖动。
        _isDraggingProgress = true;
        StopProgressTimer();
        UpdateTimeText(TimeSpan.FromSeconds(ProgressSlider.Value), CurrentDuration());
    }

    private void ProgressSlider_OnDragCompleted(object sender, DragCompletedEventArgs args)
    {
        _isDraggingProgress = false;
        if (_videoReady && !_videoFailed)
        {
            double seconds = ProgressSlider.Value;
            TrySetPosition(TimeSpan.FromSeconds(seconds));
            UpdateTimeText(TimeSpan.FromSeconds(seconds), CurrentDuration());
        }

        if (_isPlaying && _videoReady && !_videoFailed)
        {
            StartProgressTimer();
        }
    }

    private void OnProgressTimerTick(object? sender, EventArgs args)
    {
        if (_isDraggingProgress || !_videoReady || _videoFailed)
        {
            return;
        }

        UpdateProgressFromPosition();
    }

    private void StartProgressTimer()
    {
        if (!_progressTimer.IsEnabled)
        {
            _progressTimer.Start();
        }
    }

    private void StopProgressTimer() => _progressTimer.Stop();

    private void UpdateProgressFromPosition()
    {
        if (!_videoReady)
        {
            return;
        }

        TimeSpan position = VideoPlayer.Position;
        if (!_durationKnown)
        {
            TimeText.Text = $"{FormatTime(position)} / --:--";
            return;
        }

        TimeSpan duration = CurrentDuration();
        SetSliderValue(position.TotalSeconds);
        UpdateTimeText(position, duration);
    }

    private void UpdateTimeText(TimeSpan position, TimeSpan duration) =>
        TimeText.Text = $"{FormatTime(position)} / {FormatTime(duration)}";

    private void SetSliderValue(double seconds)
    {
        double maximum = Math.Max(0, ProgressSlider.Maximum);
        double value = double.IsNaN(seconds) ? 0 : Math.Clamp(seconds, 0, maximum);
        _suppressSliderChange = true;
        try
        {
            ProgressSlider.Value = value;
        }
        finally
        {
            _suppressSliderChange = false;
        }
    }

    private TimeSpan CurrentDuration()
    {
        Duration natural = VideoPlayer.NaturalDuration;
        return natural.HasTimeSpan ? natural.TimeSpan : TimeSpan.Zero;
    }

    private void TrySetPosition(TimeSpan position)
    {
        try
        {
            VideoPlayer.Position = position < TimeSpan.Zero ? TimeSpan.Zero : position;
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // 媒体尚未打开或已结束时定位会失败，忽略即可，下一次 MediaOpened 会重置进度。
            Debug.WriteLine(error);
        }
    }

    private static string FormatTime(TimeSpan value)
    {
        if (value < TimeSpan.Zero)
        {
            value = TimeSpan.Zero;
        }

        return value.TotalHours >= 1
            ? value.ToString(@"h\:mm\:ss", CultureInfo.InvariantCulture)
            : value.ToString(@"m\:ss", CultureInfo.InvariantCulture);
    }

    // ---- 媒体清理 ----

    /// <summary>切换条目/关闭窗口时清理播放状态：停止计时器、Stop + Close + 清空 Source。</summary>
    private void ReleaseVideoPlayback()
    {
        StopProgressTimer();
        _videoReady = false;
        _videoFailed = false;
        _durationKnown = false;
        _isPlaying = false;
        _playbackEnded = false;
        _isDraggingProgress = false;
        _videoUrl = null;
        _idleVideoStatus = IdleVideoStatus;

        ReleaseMediaSource();

        VideoPlayer.Visibility = Visibility.Collapsed;
        VideoLoadingRing.IsActive = false;
        VideoLoadingRing.Visibility = Visibility.Collapsed;
        CenterPlayButton.Visibility = Visibility.Collapsed;
        VideoErrorPanel.Visibility = Visibility.Collapsed;
        PlayPauseButton.IsEnabled = true;
        ProgressSlider.IsEnabled = true;
        UpdatePlayPauseIcon();

        _suppressSliderChange = true;
        try
        {
            ProgressSlider.Value = 0;
            ProgressSlider.Maximum = 1;
        }
        finally
        {
            _suppressSliderChange = false;
        }

        TimeText.Text = "0:00 / 0:00";
    }

    /// <summary>释放 MediaElement 的媒体源；调用期间置位 <see cref="_releasingMedia"/> 忽略异步事件。</summary>
    private void ReleaseMediaSource()
    {
        _releasingMedia = true;
        try
        {
            try
            {
                VideoPlayer.Stop();
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                Debug.WriteLine(error);
            }

            try
            {
                VideoPlayer.Close();
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                Debug.WriteLine(error);
            }

            try
            {
                VideoPlayer.Source = null;
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                Debug.WriteLine(error);
            }
        }
        finally
        {
            _releasingMedia = false;
        }
    }

    private string VideoLogContext()
    {
        int screenshotIndex = _index >= 0 && _index < _screenshots.Count ? _screenshots[_index].Index : -1;
        return $"soft_id={_lenovoSoftId ?? "(无)"}，截图索引={screenshotIndex}（相册第 {_index + 1}/{_screenshots.Count} 张）";
    }
}
