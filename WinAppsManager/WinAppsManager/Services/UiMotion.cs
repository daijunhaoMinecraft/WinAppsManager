using System.Diagnostics;
using System.Runtime.CompilerServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Media.Imaging;

namespace WinAppsManager.Services;

/// <summary>
/// 客户端界面动效的唯一入口：页面切换过渡、导航分类项淡入、列表行入场、弹窗打开、相册换图。
///
/// 约定：
/// <list type="bullet">
/// <item>时长、缓动和位移都集中在这个类型里，页面/弹窗里不要再各自写 Storyboard。</item>
/// <item>动画由设置页的 <see cref="Models.AppSettings.EnableAnimations"/> 控制（默认开启）；
/// 关闭时所有方法直接返回，元素保持基础值（不透明、无位移），绝不会停在半透明或偏移的中间状态。</item>
/// <item>页面切换只对“两张位图”做动画：旧页面一张、新页面一张，页面本身在过渡期间隐藏。
/// 直接给实时页面做位移动画时，页面里的列表实例化、图标解码和内部动画会占住 UI 线程，
/// 动画会卡在某一帧很久（本机实测最大帧间隔接近 300 ms）；改成位图后每帧只有两次贴图，
/// 帧间隔稳定，页面越复杂也不会更卡。</item>
/// </list>
/// </summary>
internal static class UiMotion
{
    /// <summary>
    /// 页面切换的时长。
    ///
    /// 页面切换一律用**纯交叉淡化**，不做任何位移：实测横向滑动要么让相邻的近似列表
    /// 并排出现（像页面被复制了），要么因为新页面内容还在加载而露出“先平移、再硬切内容”的断裂感。
    /// 淡化没有几何运动，不会暴露内容重复，也不会被内容加载时机拖累，观感最稳。
    /// </summary>
    private static readonly Duration PageFadeDuration = TimeSpan.FromMilliseconds(220);

    // ---- 弹窗（只做淡入，不缩放/位移） ----
    private static readonly Duration DialogDuration = TimeSpan.FromMilliseconds(200);

    // ---- 列表与导航项 ----
    private static readonly Duration NavigationItemDuration = TimeSpan.FromMilliseconds(200);
    private static readonly Duration ContentSwapDuration = TimeSpan.FromMilliseconds(220);

    /// <summary>内容替换淡入的起始透明度：不从 0 开始，避免画面整块闪黑。</summary>
    private const double ContentSwapFromOpacity = 0.35;


    private static readonly IEasingFunction FadeEasing = Freeze(new QuadraticEase { EasingMode = EasingMode.EaseOut });
    private static readonly IEasingFunction SlideEasing = Freeze(new CubicEase { EasingMode = EasingMode.EaseOut });
    private static readonly IEasingFunction DecelerateEasing = Freeze(new ExponentialEase
    {
        EasingMode = EasingMode.EaseOut,
        Exponent = 5
    });

    /// <summary>元素上的动效变换（缩放 + 平移），按元素缓存，避免每次动画都新建 TransformGroup。</summary>
    private sealed class MotionVisuals
    {
        public ScaleTransform Scale { get; } = new(1, 1);

        public TranslateTransform Translate { get; } = new();
    }

    private static readonly ConditionalWeakTable<FrameworkElement, MotionVisuals> VisualsCache = new();

    /// <summary>当前正在播放的过渡涉及的三个元素，用于“新过渡打断旧过渡”时安全收尾。</summary>
    private static FrameworkElement? _transitionPageHost;
    private static Image? _transitionOutgoing;

    /// <summary>
    /// 立刻结束当前过渡（如果有）。连续快速切换页面时，旧过渡的 storyboard 还没触发 Completed，
    /// 若直接开始新过渡，页面会残留半透明或位图缓存状态。
    /// 连续快速切换页面时，旧过渡的 storyboard 还没触发 Completed，若直接开始新过渡，
    /// </summary>
    public static void CompleteActiveTransition()
    {
        if (_transitionPageHost is null)
        {
            return;
        }

        FinishTransition(_transitionPageHost, _transitionOutgoing);
    }

    /// <summary>
    /// 是否允许播放动画：**始终为 true**。
    ///
    /// 历史上这里同时看过 <c>AppSettings.EnableAnimations</c> 和 Windows 的
    /// “在 Windows 中显示动画”（<see cref="SystemParameters.ClientAreaAnimation"/>）。
    /// 问题在于后者并不可靠：注册表 <c>MinAnimate=0</c> 会让它变 false，而 Windows 设置界面
    /// 可能仍显示动画已开启，用户完全查不出为什么“动画没了”。界面动效是本项目的产品特性，
    /// 不是可选项，因此两个开关都已移除，恒定播放。
    /// </summary>
    public static bool AnimationsEnabled => true;

    // ---------------------------------------------------------------------------------------------
    // 页面切换（全部基于位图，见类注释）
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 抓取一段内容当前的画面。返回 <c>null</c> 表示无法截图
    /// （动画关闭、元素还没布局、渲染失败），调用方应跳过过渡而不是让界面卡住。
    /// <see cref="RenderTargetBitmap.Render"/> 是同步软件渲染，1080×760 约 8–15 ms。
    /// </summary>
    public static ImageSource? CaptureSnapshot(FrameworkElement source)
    {
        if (source is null || !AnimationsEnabled || source.Visibility != Visibility.Visible)
        {
            return null;
        }

        Size size = source.RenderSize;
        if (size.Width < 1 || size.Height < 1)
        {
            return null;
        }

        // 上一轮过渡可能还没收尾：快照必须按最终位置拍，
        // 否则偏移量会被一起烘焙进位图，旧页面看起来会“跳”一下再滑出。
        Reset(source, GetVisuals(source));

        try
        {
            DpiScale dpi = VisualTreeHelper.GetDpi(source);
            int pixelWidth = Math.Max(1, (int)Math.Round(size.Width * dpi.DpiScaleX));
            int pixelHeight = Math.Max(1, (int)Math.Round(size.Height * dpi.DpiScaleY));
            var bitmap = new RenderTargetBitmap(
                pixelWidth, pixelHeight, 96 * dpi.DpiScaleX, 96 * dpi.DpiScaleY, PixelFormats.Pbgra32);
            bitmap.Render(source);
            bitmap.Freeze();
            return bitmap;
        }
        catch (Exception error) when (error is ArgumentException or InvalidOperationException
            or OutOfMemoryException or System.Runtime.InteropServices.COMException)
        {
            // 截图失败只影响过渡效果，不应影响页面切换本身。
            Debug.WriteLine(error);
            return null;
        }
    }

    /// <summary>
    /// 让元素在过渡期间启用位图缓存。真实页面参与动画时，每帧要重新合成整页
    /// （列表、图标、文字）：缓存成一张位图后，WPF 只栅格化一次，之后每帧只是一次整体
    /// 平移/透明度混合，开销与页面复杂度无关。
    /// 过渡结束必须调用 <see cref="ReleaseCompositionCache"/>，否则页面会一直显示旧位图
    /// （内容更新了也看不出来），窗口缩放时还会发虚。
    /// </summary>
    public static void EnableCompositionCache(FrameworkElement element)
    {
        if (element is null)
        {
            return;
        }

        // EnableClearType=false：过渡中元素带透明度，ClearType 无法正常工作，
        // 关掉可以避免文字边缘出现彩色毛刺。
        element.CacheMode = new BitmapCache { EnableClearType = false };
    }

    /// <summary>释放 <see cref="EnableCompositionCache"/> 建立的位图缓存，恢复正常绘制。</summary>
    public static void ReleaseCompositionCache(FrameworkElement? element)
    {
        if (element?.CacheMode is not null)
        {
            element.CacheMode = null;
        }
    }

    /// <summary>
    /// 在替换内容之前把旧页面快照盖到内容区上。接下来新页面要完成首次布局与渲染
    /// （最贵的一步，常含列表实例化与图标解码），这段时间用户看到的是静止的旧页面。
    /// </summary>
    public static void ShowTransitionPreview(Image? host, ImageSource? snapshot)
    {
        if (host is null)
        {
            return;
        }

        if (snapshot is null)
        {
            host.Source = null;
            host.Visibility = Visibility.Collapsed;
            return;
        }

        MotionVisuals visuals = GetVisuals(host);
        host.BeginAnimation(UIElement.OpacityProperty, null);
        visuals.Translate.BeginAnimation(TranslateTransform.XProperty, null);
        visuals.Translate.BeginAnimation(TranslateTransform.YProperty, null);
        host.Opacity = 1;
        visuals.Translate.X = 0;
        visuals.Translate.Y = 0;
        host.Source = snapshot;
        host.Visibility = Visibility.Visible;
    }

    /// <summary>
    /// 播放页面过渡：**纯交叉淡化**——新页面（真实控件）淡入，旧页面快照淡出，全程没有任何位移。
    ///
    /// 为什么不做滑动：
    /// <list type="bullet">
    /// <item>滑动会让导航栏相邻的近似列表并排出现，看起来像页面被复制了；</item>
    /// <item>新页面的内容常常还在异步加载，滑动时容易被看成“先移动、再硬切内容”，有断裂感。</item>
    /// </list>
    /// 淡化没有几何运动，不受内容加载时机影响，是这里最稳的过渡。
    ///
    /// 动的是 <paramref name="pageHost"/> 这个真实页面而不是它的快照：快照会冻结在按下那一刻，
    /// 加载出来的内容永远补不進动画里。真实页面参与动画则没有这个断层。
    /// 为了不让每帧重新合成整页拖慢帧率，过渡期间给它打开位图缓存，结束后立即释放。
    /// 旧页面已经离开视觉树，只能靠快照淡出。
    /// </summary>
    public static void PlayPageTransition(FrameworkElement pageHost, Image? outgoingHost)
    {
        // 上一轮过渡可能还没播完（连续快速切换）：先收尾，避免状态残留。
        CompleteActiveTransition();

        if (outgoingHost is null || pageHost is null || !pageHost.IsLoaded || !AnimationsEnabled)
        {
            ShowPageImmediately(pageHost, outgoingHost);
            return;
        }

        _transitionPageHost = pageHost;
        _transitionOutgoing = outgoingHost;

        // 帧率统计必须在 CompleteActiveTransition 之后启动：那一步会结束上一轮的统计。
        BeginFrameProfile("Fade");

        MotionVisuals pageVisuals = GetVisuals(pageHost);
        MotionVisuals outgoingVisuals = GetVisuals(outgoingHost);
        Reset(pageHost, pageVisuals);
        Reset(outgoingHost, outgoingVisuals);

        // 起始值写进基础值：否则首帧会闪一下终态再从 0 开始淡入。
        pageHost.Opacity = 0;
        outgoingHost.Opacity = 1;
        outgoingHost.Visibility = Visibility.Visible;

        // 真实页面每帧都要合成：缓存成位图后只剩一次整体透明度混合，开销与页面复杂度无关。
        EnableCompositionCache(pageHost);

        var storyboard = new Storyboard();
        storyboard.Children.Add(CreateAnimation(
            pageHost, UIElement.OpacityProperty,
            new DoubleAnimation(0, 1, PageFadeDuration) { EasingFunction = FadeEasing }));
        storyboard.Children.Add(CreateAnimation(
            outgoingHost, UIElement.OpacityProperty,
            new DoubleAnimation(1, 0, PageFadeDuration) { EasingFunction = FadeEasing }));
        storyboard.Completed += (_, _) => FinishTransition(pageHost, outgoingHost);
        storyboard.Begin();
    }

    /// <summary>
    /// 收尾：真实页面复位到终态并释放位图缓存，旧页面快照隐藏清空。
    /// 与 <see cref="FinishTransition"/> 不同，这里不依赖“当前活动过渡”状态，可以随时调用。
    /// </summary>
    private static void ShowPageImmediately(FrameworkElement? pageHost, Image? outgoingHost)
    {
        _transitionPageHost = null;
        _transitionOutgoing = null;

        if (pageHost is not null)
        {
            // 过渡期间页面被加了透明度和位图缓存：两者都必须还原，否则页面会一直停在
            // 半透明或旧位图状态（内容更新了也看不出来）。
            pageHost.BeginAnimation(UIElement.OpacityProperty, null);
            MotionVisuals visuals = GetVisuals(pageHost);
            visuals.Translate.BeginAnimation(TranslateTransform.XProperty, null);
            visuals.Translate.BeginAnimation(TranslateTransform.YProperty, null);
            pageHost.Opacity = 1;
            visuals.Translate.X = 0;
            visuals.Translate.Y = 0;
            ReleaseCompositionCache(pageHost);
            pageHost.Visibility = Visibility.Visible;
        }

        ResetBitmapHost(outgoingHost);
        EndFrameProfile();
    }

    /// <summary>把位图宿主停动画、复位、清空图像并隐藏。</summary>
    private static void ResetBitmapHost(Image? host)
    {
        if (host is null)
        {
            return;
        }

        host.BeginAnimation(UIElement.OpacityProperty, null);
        MotionVisuals visuals = GetVisuals(host);
        visuals.Translate.BeginAnimation(TranslateTransform.XProperty, null);
        visuals.Translate.BeginAnimation(TranslateTransform.YProperty, null);
        host.Opacity = 1;
        visuals.Translate.X = 0;
        visuals.Translate.Y = 0;
        host.Source = null;
        host.Visibility = Visibility.Collapsed;
    }

    /// <summary>
    /// 收尾：停动画、复位、释放位图缓存并隐藏旧页面快照。
    /// 只会处理“当前正在进行的那次过渡”，被新过渡取代的旧 storyboard 调用它时会直接返回。
    /// </summary>
    private static void FinishTransition(FrameworkElement? pageHost, Image? outgoingHost)
    {
        if (!ReferenceEquals(pageHost, _transitionPageHost))
        {
            return;
        }

        ShowPageImmediately(pageHost, outgoingHost);
    }

    // ---------------------------------------------------------------------------------------------
    // 弹窗与列表
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 弹窗打开动画：**只做透明度淡入**，不缩放、不位移。
    /// 在窗口的 <c>Loaded</c> 事件里调用，此时窗口还没渲染第一帧，不会先闪一下完整界面。
    /// </summary>
    public static void PlayDialogOpen(Window window)
    {
        if (window?.Content is not FrameworkElement root || !AnimationsEnabled)
        {
            return;
        }

        // 只做淡入，**不做缩放和位移**。缩放会让弹窗里的文字和图标在动画期间被重新栅格化，
        // 看着像在轻微抖动；位移又容易和窗口自身的居中定位打架。纯透明度最干净。
        MotionVisuals visuals = GetVisuals(root);
        Reset(root, visuals);
        root.Opacity = 0;

        var storyboard = new Storyboard();
        storyboard.Children.Add(CreateAnimation(
            root, UIElement.OpacityProperty,
            new DoubleAnimation(0, 1, DialogDuration) { EasingFunction = FadeEasing }));
        storyboard.Completed += (_, _) =>
        {
            root.BeginAnimation(UIElement.OpacityProperty, null);
            root.Opacity = 1;
        };
        storyboard.Begin();
    }

    /// <summary>
    /// 导航分类项依次淡入：分类列表重建后按顺序错开 26ms，最多累计 260ms。
    /// 只动 <see cref="UIElement.OpacityProperty"/>，不改 RenderTransform，避免与
    /// NavigationViewItem 模板内的选中指示器动画互相覆盖。
    /// </summary>
    public static void PlayNavigationItemsIn(IEnumerable<FrameworkElement> items)
    {
        if (!AnimationsEnabled)
        {
            return;
        }

        int index = 0;
        foreach (FrameworkElement item in items)
        {
            if (item is null)
            {
                continue;
            }

            TimeSpan delay = TimeSpan.FromMilliseconds(Math.Min(index * 26, 260));
            index++;
            if (item.IsLoaded)
            {
                FadeInWithDelay(item, delay);
                continue;
            }

            void OnLoaded(object? sender, RoutedEventArgs args)
            {
                item.Loaded -= OnLoaded;
                FadeInWithDelay(item, delay);
            }

            item.Loaded += OnLoaded;
        }
    }

    /// <summary>
    /// 图片/内容替换时的柔和淡入（相册左右切换、详情截图加载完成）。从 0.35 而不是 0 开始，
    /// 这样既不会让画面整块消失，又能看出内容确实换了。
    /// </summary>
    public static void PlayContentSwap(FrameworkElement element)
    {
        if (element is null || !AnimationsEnabled)
        {
            return;
        }

        element.BeginAnimation(UIElement.OpacityProperty, null);
        element.BeginAnimation(
            UIElement.OpacityProperty,
            new DoubleAnimation(ContentSwapFromOpacity, 1, ContentSwapDuration) { EasingFunction = FadeEasing });
    }

    // ---------------------------------------------------------------------------------------------
    // 帧率诊断（仅 Verbose 日志开启时生效）
    // ---------------------------------------------------------------------------------------------

    private static EventHandler? _frameProfileHandler;
    private static string _frameProfileTag = string.Empty;
    private static int _frameProfileFrames;
    private static long _frameProfileLastTicks;
    private static long _frameProfileMaxGapMs;

    /// <summary>
    /// 统计一次过渡期间的实际渲染帧数与最大帧间隔。WPF 的 <see cref="CompositionTarget.Rendering"/>
    /// 每个合成帧触发一次，帧间隔突然变大即代表掉帧；只在 Verbose 日志下启用，正常使用零开销。
    /// </summary>
    public static void BeginFrameProfile(string tag)
    {
        if (AppLogService.MinimumLevel != AppLogLevel.Verbose)
        {
            return;
        }

        EndFrameProfile();
        _frameProfileTag = tag;
        _frameProfileFrames = 0;
        _frameProfileLastTicks = 0;
        _frameProfileMaxGapMs = 0;
        _frameProfileHandler = (_, _) =>
        {
            long now = Stopwatch.GetTimestamp();
            if (_frameProfileLastTicks != 0)
            {
                long gapMs = (now - _frameProfileLastTicks) * 1000 / Stopwatch.Frequency;
                if (gapMs > _frameProfileMaxGapMs)
                {
                    _frameProfileMaxGapMs = gapMs;
                }
            }

            _frameProfileLastTicks = now;
            _frameProfileFrames++;
        };
        CompositionTarget.Rendering += _frameProfileHandler;
    }

    /// <summary>结束帧率诊断并写一条 Verbose 日志。</summary>
    public static void EndFrameProfile()
    {
        if (_frameProfileHandler is null)
        {
            return;
        }

        CompositionTarget.Rendering -= _frameProfileHandler;
        _frameProfileHandler = null;
        AppLogService.Verbose(
            $"过渡帧率：{_frameProfileTag}，共 {_frameProfileFrames} 帧，最大帧间隔 {_frameProfileMaxGapMs} ms。");
    }

    // ---------------------------------------------------------------------------------------------
    // 内部辅助
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 带延迟的淡入。延迟期间用 <see cref="FillBehavior.HoldEnd"/> 把透明度固定在 0（否则元素会先显示再消失），
    /// 结束后移除动画时钟并把透明度写回 1，元素不会长期持有动画。
    /// </summary>
    private static void FadeInWithDelay(FrameworkElement element, TimeSpan delay)
    {
        element.BeginAnimation(UIElement.OpacityProperty, null);
        element.Opacity = 0;

        var fade = new DoubleAnimation(0, 1, NavigationItemDuration)
        {
            BeginTime = delay,
            EasingFunction = FadeEasing,
            FillBehavior = FillBehavior.HoldEnd
        };
        fade.Completed += (_, _) =>
        {
            element.BeginAnimation(UIElement.OpacityProperty, null);
            element.Opacity = 1;
        };
        element.BeginAnimation(UIElement.OpacityProperty, fade);
    }

    /// <summary>清除元素上可能残留的动画，并把透明度/平移/缩放写回最终状态。</summary>
    private static void Reset(FrameworkElement element, MotionVisuals visuals)
    {
        element.BeginAnimation(UIElement.OpacityProperty, null);
        visuals.Translate.BeginAnimation(TranslateTransform.XProperty, null);
        visuals.Translate.BeginAnimation(TranslateTransform.YProperty, null);
        visuals.Scale.BeginAnimation(ScaleTransform.ScaleXProperty, null);
        visuals.Scale.BeginAnimation(ScaleTransform.ScaleYProperty, null);

        element.Opacity = 1;
        visuals.Translate.X = 0;
        visuals.Translate.Y = 0;
        visuals.Scale.ScaleX = 1;
        visuals.Scale.ScaleY = 1;
    }

    private static MotionVisuals GetVisuals(FrameworkElement element)
    {
        return VisualsCache.GetValue(element, static target =>
        {
            var visuals = new MotionVisuals();
            var group = new TransformGroup();
            group.Children.Add(visuals.Scale);
            group.Children.Add(visuals.Translate);
            // 缩放围绕元素中心，弹窗“弹出”时不会往右下角偏。
            target.RenderTransformOrigin = new Point(0.5, 0.5);
            target.RenderTransform = group;
            return visuals;
        });
    }

    private static DoubleAnimation CreateAnimation(
        DependencyObject target,
        DependencyProperty property,
        DoubleAnimation animation)
    {
        // FillBehavior 保持默认的 Stop：调用方已把基础值设为终态，动画结束后自然停住，
        // 不会长期持有动画时钟（位图宿主是长生命周期元素，这点很重要）。
        Storyboard.SetTarget(animation, target);
        Storyboard.SetTargetProperty(animation, new PropertyPath(property));
        return animation;
    }

    private static T Freeze<T>(T freezable) where T : Freezable
    {
        freezable.Freeze();
        return freezable;
    }
}
