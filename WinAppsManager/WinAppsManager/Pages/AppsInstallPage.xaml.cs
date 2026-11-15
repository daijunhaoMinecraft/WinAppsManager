using System.Collections.ObjectModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Net.Http;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Controls.Primitives;
using System.Windows.Input;
using System.Windows.Media;
using WinAppsManager.Models;
using WinAppsManager.Services;
using Page = iNKORE.UI.WPF.Modern.Controls.Page;

namespace WinAppsManager.Pages;

public partial class AppsInstallPage : Page
{
    private const string AllCategories = "全部";
    private const int DefaultPageSize = 60;

    /// <summary>api 模式（服务端 <c>search_mode=api</c>）搜索框输入的防抖时长：输入停顿后才请求服务端，避免逐字请求。</summary>
    private static readonly TimeSpan ApiSearchDebounce = TimeSpan.FromMilliseconds(350);
    /// <summary>回到列表页时复用已加载目录的时间窗口，与 <c>AppCatalogService</c> 的 5 分钟内存目录缓存一致。</summary>
    private static readonly TimeSpan CatalogReuseWindow = TimeSpan.FromMinutes(5);
    private readonly StringComparer _nameComparer = StringComparer.Create(CultureInfo.GetCultureInfo("zh-CN"), true);
    private readonly HashSet<string> _iconLoads = new(StringComparer.Ordinal);
    private CancellationTokenSource? _loadCancellation;
    private CancellationTokenSource? _iconLoadCancellation;
    private CancellationTokenSource? _onlineSearchCancellation;
    /// <summary>api 模式下搜索框自动调用的服务端搜索（含防抖等待）的取消令牌：新输入会取消上一轮。</summary>
    private CancellationTokenSource? _apiSearchCancellation;
    private Task? _catalogLoadTask;
    private IReadOnlyList<AppItem> _catalogApps = Array.Empty<AppItem>();
    private IReadOnlyList<AppItem> _onlineSearchApps = Array.Empty<AppItem>();
    /// <summary>当前筛选/搜索/排序后的完整结果；分页只把当前页写入 <see cref="AppItems"/>。</summary>
    private IReadOnlyList<AppItem> _filteredApps = Array.Empty<AppItem>();
    private int _pageIndex;
    private int _pageSize = DefaultPageSize;
    private string _filterSignature = string.Empty;
    private int _catalogVersion;
    /// <summary>软件源/服务端切换计数：每次切换递增，用来丢弃切换前发出的旧目录请求结果。</summary>
    private int _sourceVersion;
    /// <summary>最近一次成功发布到页面的目录所属的软件源与当时的 <see cref="_sourceVersion"/>。</summary>
    private AppSourcePreference _loadedSource;
    private int _loadedSourceVersion = -1;
    /// <summary>最近一次成功发布目录的时间（UTC），用于回到列表页时复用 5 分钟内的结果。</summary>
    private DateTimeOffset _catalogLoadedAtUtc = DateTimeOffset.MinValue;
    /// <summary>最近一次成功发布的服务端目录快照引用：详情回写等操作会替换内存目录实例。</summary>
    private IReadOnlyList<AppItem>? _loadedCatalogSnapshot;
    private bool _isInitialized;
    private bool _isPageActive;
    private bool _isLoading;
    private bool _isOnlineSearching;
    /// <summary>是否正在执行 api 模式的服务端搜索（含防抖等待）；状态行据此显示“正在搜索…”。</summary>
    private bool _isApiSearching;
    /// <summary>最近一次 api 模式搜索的失败说明（成功、取消或复位时清空）。</summary>
    private string? _apiSearchError;
    private bool _hasSuccessfulLoad;
    /// <summary>
    /// 服务端软件源能力（<c>/api/v1/sources</c> 的 <c>search_mode</c> 与 <c>online_search</c>）是否已请求过；
    /// 请求失败时复位，允许下一次目录加载重试（失败期间回退按源判断）。
    /// </summary>
    private bool _sourceCapabilitiesRequested;
    private string _selectedCategory = AllCategories;
    private string? _onlineSearchQuery;
    private string? _onlineSearchError;
    private string? _loadError;

    public ObservableCollection<AppItem> AppItems { get; } = new();
    public string SelectedCategory => _selectedCategory;
    public event Action<AppItem>? AppDetailsRequested;
    public event Action<IReadOnlyList<CategoryOption>>? CategoriesChanged;
    public event Action<string>? CategoryChanged;

    public AppsInstallPage()
    {
        InitializeComponent();
        DataContext = this;
        SortCombo.SelectedIndex = 0;
        PageSizeCombo.SelectedIndex = 0;
        _isInitialized = true;
        // 页面在 MainWindow 中只创建一次且不会被丢弃；始终订阅 SourceChanged / BackendChanged，
        // 这样在设置页切换软件源或服务端（此时列表页未显示）也能清空旧状态，回到列表后重新加载。
        AppCatalogService.SourceChanged += OnSourceChanged;
        AppCatalogService.BackendChanged += OnBackendChanged;
        ApplyFilters();
    }

    private async void OnPageLoaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = true;
        AppCatalogService.CacheChanged += OnCacheChanged;
        // 服务端下发的软件源能力（search_mode / online_search）与目录并行预取：旧服务端缺字段时自动回退按源判断。
        _ = RefreshSourceCapabilitiesAsync();

        if (_catalogLoadTask is { IsCompleted: false })
        {
            await _catalogLoadTask;
            if (!_isPageActive)
            {
                return;
            }
        }

        // 切换软件源后的后台加载可能已经完成：5 分钟内的结果直接复用，不再重复请求目录。
        if (TryReuseLoadedCatalog())
        {
            AppLogService.Verbose(
                $"软件列表复用已加载目录：源={AppCatalogService.SourceDisplayName}，" +
                $"{_catalogApps.Count} 款软件，未重复请求。");
            ApplyFilters();
            EnsureApiSearchForCurrentQuery();
            return;
        }

        await LoadCatalogAsync();
    }

    private void OnPageUnloaded(object sender, RoutedEventArgs args)
    {
        _isPageActive = false;
        AppCatalogService.CacheChanged -= OnCacheChanged;
        _loadCancellation?.Cancel();
        _iconLoadCancellation?.Cancel();
        _onlineSearchCancellation?.Cancel();
        CancelApiSearch(clearError: false);
    }

    /// <summary>商店源（联想应用商店 / 360 软件管家）按需图标加载的取消令牌：页面重新显示时重建，避免取消过的令牌复用。</summary>
    private CancellationToken IconLoadToken
    {
        get
        {
            if (_iconLoadCancellation is null || _iconLoadCancellation.IsCancellationRequested)
            {
                _iconLoadCancellation?.Dispose();
                _iconLoadCancellation = new CancellationTokenSource();
            }

            return _iconLoadCancellation.Token;
        }
    }

    private void OnSourceChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnSourceChanged(sender, args)));
            return;
        }

        // 软件源已切换：退出在线搜索、分类复位“全部”，并取消/丢弃旧源的目录请求。
        // 页面当前是否显示不影响后台加载：设置页切换后也立即加载新源目录，成功发布分类后
        // MainWindow.OnCategoriesChanged 会立即重建左侧导航。
        _sourceVersion++;
        AppLogService.Info(
            $"软件源已变化，清空软件列表状态：源={AppCatalogService.SourceDisplayName}，" +
            $"服务端={AppCatalogService.BackendBaseUrl}，页面{(_isPageActive ? "正在显示" : "未显示")}，" +
            "将在后台立即加载新源目录并刷新左侧分类导航。");
        ExitOnlineSearchState();
        _iconLoadCancellation?.Cancel();
        _loadCancellation?.Cancel();
        _catalogApps = Array.Empty<AppItem>();
        _loadedCatalogSnapshot = null;
        _loadedSourceVersion = -1;
        _catalogLoadedAtUtc = DateTimeOffset.MinValue;
        _catalogVersion++;
        _hasSuccessfulLoad = false;
        _loadError = null;
        if (_selectedCategory != AllCategories)
        {
            _selectedCategory = AllCategories;
            CategoryChanged?.Invoke(_selectedCategory);
        }

        ApplyFilters();
        _ = ReloadCatalogAfterSourceChangeAsync();
    }

    /// <summary>
    /// 软件源/服务端切换后的目录重载：先取消并等待上一个源的请求退出（旧结果会因
    /// <see cref="_sourceVersion"/> 不匹配被 <see cref="LoadCatalogCoreAsync"/> 丢弃），再按当前源加载。
    /// 页面未显示时同样执行，加载成功会发布 <see cref="CategoriesChanged"/> 让左侧导航立即变化；
    /// 异常都在目录加载内部处理，不会影响设置页切换流程。
    /// </summary>
    private async Task ReloadCatalogAfterSourceChangeAsync()
    {
        int sourceVersion = _sourceVersion;
        _loadCancellation?.Cancel();
        var pending = _catalogLoadTask;
        if (pending is { IsCompleted: false })
        {
            try
            {
                await pending;
            }
            catch (Exception error) when (error is not OutOfMemoryException)
            {
                // LoadCatalogCoreAsync 内部已处理异常；这里只为等待旧请求退出，避免意外异常中断后台刷新。
                AppLogService.Verbose(
                    $"等待旧目录请求结束时忽略异常：{error.GetType().Name}：{error.Message}");
            }
        }

        if (sourceVersion != _sourceVersion)
        {
            // 等待期间软件源又变化了：后一次变化已经安排了自己的后台加载，这里直接退出。
            AppLogService.Verbose("软件源在等待旧目录请求期间再次变化，跳过本次后台目录加载。");
            return;
        }

        await LoadCatalogAsync();
    }

    /// <summary>服务端地址切换后目录缓存与内存目录都已清空：按切换软件源的同样方式清空页面状态并重新加载。</summary>
    private void OnBackendChanged(object? sender, EventArgs args)
    {
        // 换了服务端：软件源能力（search_mode / online_search）要按新服务端重新获取。
        _sourceCapabilitiesRequested = false;
        OnSourceChanged(sender, args);
    }

    /// <summary>目录加载失败后的“重试”按钮：重新走普通加载（客户端已无手动刷新目录入口）。</summary>
    private async void RetryCatalog_OnClick(object sender, RoutedEventArgs args) => await LoadCatalogAsync();

    private Task LoadCatalogAsync()
    {
        if (_catalogLoadTask is { IsCompleted: false })
        {
            return _catalogLoadTask;
        }

        _catalogLoadTask = LoadCatalogCoreAsync();
        return _catalogLoadTask;
    }

    /// <summary>
    /// 服务端软件源能力由 <c>GET /api/v1/sources</c> 下发：每源的 <c>search_mode</c> 决定搜索方式是
    /// 本地目录搜索（catalog）还是服务端搜索 API（api），<c>online_search</c> 只作为旧服务端的兼容回退。
    /// 目录加载时静默预取一次（与目录请求并行，不阻塞列表显示），拿到结果后刷新界面；
    /// 旧服务端缺字段或请求失败时保持按源判断的回退，并允许下一次目录加载重试
    /// （见 <see cref="AppCatalogService.CurrentSearchMode"/> / <see cref="AppCatalogService.OnlineSearchAvailable"/>）。
    /// </summary>
    private async Task RefreshSourceCapabilitiesAsync()
    {
        if (_sourceCapabilitiesRequested)
        {
            return;
        }

        _sourceCapabilitiesRequested = true;
        try
        {
            // 预取前的状态（未加载时是按源回退值）；只有服务端能力真正改变结果时才刷新界面，
            // 避免列表已显示后无意义地重建当前页。
            string? beforeMode = AppCatalogService.ServerSearchMode;
            bool beforeOnlineSearch = AppCatalogService.OnlineSearchAvailable;
            await AppCatalogService.GetSourcesAsync();
            if (!_isInitialized)
            {
                return;
            }

            string? afterMode = AppCatalogService.ServerSearchMode;
            bool afterOnlineSearch = AppCatalogService.OnlineSearchAvailable;
            if (string.Equals(beforeMode, afterMode, StringComparison.Ordinal)
                && beforeOnlineSearch == afterOnlineSearch)
            {
                return;
            }

            AppLogService.Info(
                $"服务端软件源能力已更新：搜索模式 {beforeMode ?? "（按源回退）"} → {afterMode ?? "（按源回退）"}，" +
                $"在线搜索 {beforeOnlineSearch} → {afterOnlineSearch}。");
            if (Dispatcher.CheckAccess())
            {
                OnSourceCapabilitiesChanged();
            }
            else
            {
                _ = Dispatcher.BeginInvoke(new Action(OnSourceCapabilitiesChanged));
            }
        }
        catch (HttpRequestException error) when (error.StatusCode == System.Net.HttpStatusCode.NotFound)
        {
            // 旧服务端没有 /sources 路由：保持按源判断的回退，不需要在每次目录加载时重复请求。
            AppLogService.Verbose("服务端没有 /sources 接口，搜索模式与在线搜索能力回退按源判断。");
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            // 旧服务端（响应里没有 search_mode / online_search 字段）或网络失败都不影响软件列表，继续用回退判断。
            _sourceCapabilitiesRequested = false;
            AppLogService.Verbose(
                $"获取服务端软件源能力失败，回退按源判断：{error.GetType().Name}：{error.Message}");
        }
    }

    /// <summary>
    /// 服务端下发的搜索模式 / 在线搜索能力变化后刷新界面：api 模式按当前关键词补一次自动搜索，
    /// catalog 模式回到本地搜索，并重绘空状态与状态行。
    /// </summary>
    private void OnSourceCapabilitiesChanged()
    {
        if (!_isPageActive)
        {
            // 页面未显示时只更新能力状态；回到列表页（OnPageLoaded）会按当前关键词重新触发搜索。
            return;
        }

        UpdateServerSearchState(SearchBox.Text.Trim());
        ApplyFilters();
    }

    /// <summary>
    /// api 模式（服务端说目录只是子集）下，目录加载完成 / 切换源 / 回到列表页后按搜索框里的关键词
    /// 补一次服务端搜索，避免新目录只用客户端本地过滤而漏掉目录外软件。
    /// 已有同一关键词的结果时不重复请求。
    /// </summary>
    private void EnsureApiSearchForCurrentQuery()
    {
        if (!_isPageActive || _isApiSearching)
        {
            return;
        }

        if (!AppCatalogService.UsesApiSearch)
        {
            return;
        }

        string query = SearchBox.Text.Trim();
        if (query.Length == 0)
        {
            return;
        }

        if (string.Equals(_onlineSearchQuery, query, StringComparison.Ordinal))
        {
            return;
        }

        ScheduleApiSearch(query, TimeSpan.Zero);
    }

    /// <summary>
    /// 回到列表页时能否复用已加载的目录：源与源版本一致、最近一次成功加载在 5 分钟内，
    /// 且服务端内存目录没有被详情回写等操作替换（引用不同说明目录已变化，走一次 5 分钟内存缓存读取）。
    /// 命中时不发起任何目录请求。
    /// </summary>
    private bool TryReuseLoadedCatalog()
    {
        if (!_hasSuccessfulLoad || !AppCatalogService.HasLoaded) return false;
        if (_loadedSource != AppCatalogService.CurrentSource || _loadedSourceVersion != _sourceVersion) return false;
        if (_loadedCatalogSnapshot is null || !ReferenceEquals(AppCatalogService.Apps, _loadedCatalogSnapshot)) return false;
        return DateTimeOffset.UtcNow - _catalogLoadedAtUtc < CatalogReuseWindow;
    }

    private async Task LoadCatalogCoreAsync()
    {
        using var cancellation = new CancellationTokenSource();
        _loadCancellation = cancellation;
        int sourceVersion = _sourceVersion;
        AppSourcePreference source = AppCatalogService.CurrentSource;
        var stopwatch = Stopwatch.StartNew();
        _isLoading = true;
        _loadError = null;
        UpdateLoadStatus();
        AppLogService.Info($"软件列表开始加载目录：源={AppCatalogService.SourceDisplayName}。");
        // 与目录加载并行预取服务端软件源能力（search_mode / online_search）；不阻塞目录请求。
        _ = RefreshSourceCapabilitiesAsync();

        try
        {
            // 普通加载：联想应用商店 / 360 软件管家的目录在后端只读本地快照并由定时线程自动更新，
            // 客户端已无手动刷新入口。
            IReadOnlyList<AppItem> apps = await AppCatalogService.GetAppsAsync(cancellation.Token);
            if (cancellation.IsCancellationRequested)
            {
                AppLogService.Verbose($"软件列表目录加载结果已丢弃：请求已取消，耗时 {stopwatch.ElapsedMilliseconds} ms。");
                return;
            }

            if (sourceVersion != _sourceVersion || AppCatalogService.CurrentSource != source)
            {
                // 旧源的结果：请求期间软件源又切换过，不能覆盖新源状态，也不发布分类。
                AppLogService.Verbose(
                    $"软件列表目录加载结果已丢弃：软件源已从{AppCatalogService.GetSourceDisplayName(source)}切换为" +
                    $"{AppCatalogService.SourceDisplayName}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
                return;
            }

            // 页面未显示（例如在设置页切换软件源）也接受并发布结果：分类事件会让 MainWindow
            // 立即重建左侧导航，回到列表页时直接复用这次加载，不再重复请求目录。
            var categories = BuildCategories(apps);
            bool categoryChanged = categories.All(option => option.Name != _selectedCategory);
            if (categoryChanged)
            {
                _selectedCategory = AllCategories;
            }
            _catalogApps = apps;
            _catalogLoadedAtUtc = DateTimeOffset.UtcNow;
            _loadedCatalogSnapshot = apps;
            _loadedSource = source;
            _loadedSourceVersion = sourceVersion;
            _catalogVersion++;
            _hasSuccessfulLoad = true;
            _loadError = AppCatalogService.CacheState.NetworkError;
            if (categoryChanged)
            {
                CategoryChanged?.Invoke(_selectedCategory);
            }
            CategoriesChanged?.Invoke(categories);
            AppLogService.Info(
                $"软件列表目录加载成功：源={AppCatalogService.SourceDisplayName}，{apps.Count} 款软件，" +
                $"缓存={AppCatalogService.CacheState.Source}，按当前系统隐藏 {AppCatalogService.HiddenIncompatibleCount} 款，" +
                $"页面{(_isPageActive ? "正在显示" : "未显示")}，已发布 {categories.Count} 个分类供左侧导航更新，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            // A cached page can be detached during navigation. Its next Loaded event can retry if necessary.
            AppLogService.Verbose($"软件列表目录加载已取消：耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException)
        {
            _loadError = "获取软件目录超时，请稍后重试。";
            AppLogService.Warn(
                $"软件列表目录加载超时：源={AppCatalogService.SourceDisplayName}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (HttpRequestException exception)
        {
            _loadError = exception.StatusCode.HasValue
                ? $"软件目录服务返回异常（HTTP {(int)exception.StatusCode.Value}），请稍后重试。"
                : "无法连接软件目录服务，请确认服务已启动后重试。";
            AppLogService.Warn(
                $"软件列表目录加载失败：源={AppCatalogService.SourceDisplayName}，" +
                $"HTTP {(exception.StatusCode.HasValue ? ((int)exception.StatusCode.Value).ToString(CultureInfo.InvariantCulture) : "无响应")}，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (JsonException exception)
        {
            _loadError = "软件目录服务返回的数据格式不正确，请检查服务后重试。";
            AppLogService.Warn(
                $"软件列表目录解析失败：源={AppCatalogService.SourceDisplayName}，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (System.IO.IOException exception)
        {
            _loadError = "读取软件目录时连接中断，请重试。";
            AppLogService.Warn(
                $"软件列表目录加载连接中断：源={AppCatalogService.SourceDisplayName}，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            _loadError = "加载软件目录时发生未知错误，请稍后重试。";
            AppLogService.Error(
                $"软件列表目录加载出现未预期错误：源={AppCatalogService.SourceDisplayName}，" +
                $"{exception.GetType().Name}：{exception.Message}，耗时 {stopwatch.ElapsedMilliseconds} ms。",
                exception);
            Debug.WriteLine(exception);
        }
        finally
        {
            if (ReferenceEquals(_loadCancellation, cancellation))
            {
                _loadCancellation = null;
                _isLoading = false;
                if (!AppCatalogService.HasLoaded)
                {
                    _hasSuccessfulLoad = false;
                    _catalogApps = Array.Empty<AppItem>();
                }
                // api 模式的源在目录就绪后按当前关键词补一次服务端搜索（目录只是子集）。
                EnsureApiSearchForCurrentQuery();
                ApplyFilters();
            }
        }
    }

    private void SearchBox_OnTextChanged(object sender, TextChangedEventArgs args)
    {
        if (!_isInitialized)
        {
            return;
        }

        if (!_isOnlineSearching && !_isApiSearching)
        {
            _onlineSearchError = null;
        }

        // 搜索方式由服务端 /api/v1/sources 的 search_mode 决定：api 模式输入即防抖调用服务端搜索 API，
        // catalog 模式保持原有本地目录搜索（目录完整）。
        UpdateServerSearchState(SearchBox.Text.Trim());
        ApplyFilters();
    }

    /// <summary>
    /// 按服务端搜索模式处理搜索框输入：<c>catalog</c> 模式（目录完整）保持原有本地搜索；
    /// <c>api</c> 模式（目录只是子集）输入即防抖调用服务端搜索 API，清空输入恢复完整目录。
    /// 旧服务端没有 <c>search_mode</c> 时按源回退（见 <see cref="AppCatalogService.UsesApiSearch"/>）。
    /// </summary>
    private void UpdateServerSearchState(string query)
    {
        if (!AppCatalogService.UsesApiSearch)
        {
            // catalog 模式不需要自动 API 搜索；清掉可能残留的在途请求与错误。
            CancelApiSearch(clearError: true);
            return;
        }

        if (query.Length == 0)
        {
            // 清空搜索框：取消在途搜索并把列表恢复成完整目录。
            CancelApiSearch(clearError: true);
            _onlineSearchApps = Array.Empty<AppItem>();
            _onlineSearchQuery = null;
            _onlineSearchError = null;
            return;
        }

        ScheduleApiSearch(query, ApiSearchDebounce);
    }

    /// <summary>安排一次防抖后的服务端搜索；会先取消上一轮（含其防抖等待），只保留最后一次输入。</summary>
    private void ScheduleApiSearch(string query, TimeSpan delay)
    {
        CancelApiSearch(clearError: true);
        var cancellation = new CancellationTokenSource();
        _apiSearchCancellation = cancellation;
        _isApiSearching = true;
        _onlineSearchError = null;
        string sourceName = AppCatalogService.SourceDisplayName;
        OnlineSearchStatusText.Text = $"正在{sourceName}搜索“{query}”…";
        _ = RunApiSearchAsync(query, delay, cancellation);
    }

    /// <summary>
    /// api 模式的服务端搜索：防抖等待后调用 <see cref="AppCatalogService.SearchStoreAsync"/>，
    /// 结果替换列表（可能包含目录外软件）。取消/超时/HTTP/格式错误都只更新状态，不抛出到 UI。
    /// </summary>
    private async Task RunApiSearchAsync(string query, TimeSpan delay, CancellationTokenSource cancellation)
    {
        CancellationToken ct = cancellation.Token;
        string sourceName = AppCatalogService.SourceDisplayName;
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Verbose($"{sourceName}服务端搜索开始：关键词“{query}”。");
        try
        {
            if (delay > TimeSpan.Zero)
            {
                await Task.Delay(delay, ct);
            }

            var results = await AppCatalogService.SearchStoreAsync(query, ct);
            ct.ThrowIfCancellationRequested();
            if (!ReferenceEquals(_apiSearchCancellation, cancellation))
            {
                // 已被更新的一轮输入取代：丢弃这次结果。
                return;
            }

            _onlineSearchApps = results;
            _onlineSearchQuery = query;
            _apiSearchError = null;
            if (_selectedCategory != AllCategories)
            {
                // 服务端搜索结果已经按关键词匹配，不再叠加分类过滤（与旧版在线搜索一致）。
                _selectedCategory = AllCategories;
                CategoryChanged?.Invoke(_selectedCategory);
            }

            AppLogService.Info(
                $"{sourceName}服务端搜索完成：关键词“{query}”，{results.Count} 条结果，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            // 新的输入、清空搜索框、切换源或离开页面都会取消搜索，此时不覆盖当前界面状态。
            AppLogService.Verbose(
                $"{sourceName}服务端搜索已取消：关键词“{query}”，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (OperationCanceledException)
        {
            SetApiSearchFailure(cancellation, query, $"{sourceName}搜索超时，请稍后重试。");
            AppLogService.Warn(
                $"{sourceName}服务端搜索超时：关键词“{query}”，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (HttpRequestException exception)
        {
            SetApiSearchFailure(cancellation, query, exception.StatusCode.HasValue
                ? $"{sourceName}搜索失败（HTTP {(int)exception.StatusCode.Value}），请稍后重试。"
                : "无法连接软件服务，请确认服务已启动后重试。");
            AppLogService.Warn(
                $"{sourceName}服务端搜索失败：关键词“{query}”，" +
                $"HTTP {(exception.StatusCode.HasValue ? ((int)exception.StatusCode.Value).ToString(CultureInfo.InvariantCulture) : "无响应")}，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (JsonException exception)
        {
            SetApiSearchFailure(cancellation, query, $"{sourceName}返回的搜索结果格式不正确，请稍后重试。");
            AppLogService.Warn(
                $"{sourceName}服务端搜索结果解析失败：关键词“{query}”，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (System.IO.IOException exception)
        {
            SetApiSearchFailure(cancellation, query, "搜索时连接中断，请重试。");
            AppLogService.Warn(
                $"{sourceName}服务端搜索连接中断：关键词“{query}”，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (InvalidDataException exception)
        {
            SetApiSearchFailure(cancellation, query, $"{sourceName}返回的搜索结果无效，请稍后重试。");
            AppLogService.Warn(
                $"{sourceName}服务端搜索结果无效：关键词“{query}”，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            SetApiSearchFailure(cancellation, query, $"{sourceName}搜索失败，请稍后重试。");
            AppLogService.Error(
                $"{sourceName}服务端搜索出现未预期错误：关键词“{query}”，{error.GetType().Name}：{error.Message}，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。",
                error);
            Debug.WriteLine(error);
        }
        finally
        {
            // 取消的旧请求只做资源释放，不改新请求的状态。
            cancellation.Dispose();
            if (ReferenceEquals(_apiSearchCancellation, cancellation))
            {
                _apiSearchCancellation = null;
                _isApiSearching = false;
                if (_isPageActive && _isInitialized)
                {
                    ApplyFilters();
                }
            }
        }
    }

    /// <summary>记录一次服务端搜索失败：列表进入“搜索结果为空 + 错误说明”状态，而不是退回本地子集。</summary>
    private void SetApiSearchFailure(CancellationTokenSource cancellation, string query, string message)
    {
        if (!ReferenceEquals(_apiSearchCancellation, cancellation))
        {
            return;
        }

        _onlineSearchApps = Array.Empty<AppItem>();
        _onlineSearchQuery = query;
        _apiSearchError = message;
        _onlineSearchError = null;
    }

    /// <summary>取消在途的服务端搜索（含防抖等待）；令牌的实际释放交给 <see cref="RunApiSearchAsync"/> 的 finally。</summary>
    private void CancelApiSearch(bool clearError)
    {
        var previous = _apiSearchCancellation;
        _apiSearchCancellation = null;
        previous?.Cancel();
        _isApiSearching = false;
        if (clearError)
        {
            _apiSearchError = null;
        }
    }

    private void Filter_OnSelectionChanged(object sender, SelectionChangedEventArgs args) => ApplyFilters();

    private List<CategoryOption> BuildCategories(IReadOnlyList<AppItem> apps)
    {
        var categories = new List<CategoryOption> { new(AllCategories, apps.Count) };
        foreach (var group in apps.GroupBy(app => app.Category, StringComparer.Ordinal)
                     .OrderBy(group => group.Key, _nameComparer))
        {
            categories.Add(new CategoryOption(group.Key, group.Count()));
        }
        return categories;
    }

    public void SelectCategory(string category)
    {
        string normalized = string.IsNullOrWhiteSpace(category) ? AllCategories : category;
        if (_selectedCategory == normalized)
        {
            ApplyFilters();
            return;
        }

        _selectedCategory = normalized;
        ApplyFilters();
        CategoryChanged?.Invoke(_selectedCategory);
    }

    private void ApplyFilters()
    {
        if (!_isInitialized)
        {
            return;
        }

        string query = SearchBox.Text.Trim();
        string category = _selectedCategory;
        bool apiMode = AppCatalogService.UsesApiSearch;
        // 服务端搜索结果：api 模式搜索框自动搜索，或旧服务端兼容的“在线搜索”按钮；显示哪个关键词的
        // 结果由 _onlineSearchQuery 决定（为 null 时显示完整目录）。
        bool serverSearchResults = _onlineSearchQuery is not null;
        bool searching = _isOnlineSearching || _isApiSearching;
        string? searchError = _apiSearchError ?? _onlineSearchError;
        string[] terms = query.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries);
        IReadOnlyList<AppItem> sourceApps = serverSearchResults ? _onlineSearchApps : _catalogApps;
        bool hasData = serverSearchResults || _hasSuccessfulLoad;
        // 服务端搜索结果已经是软件源按关键词匹配的结果，不再叠加本地搜索与分类过滤。
        IEnumerable<AppItem> matches = hasData
            ? sourceApps.Where(app => serverSearchResults
                || ((category == AllCategories || app.Category == category) && MatchesSearch(app, terms)))
            : Array.Empty<AppItem>();

        string? sort = (SortCombo.SelectedItem as ComboBoxItem)?.Tag as string;
        IOrderedEnumerable<AppItem> ordered = sort == "name"
            ? matches.OrderBy(app => app.Name, _nameComparer)
            : matches.OrderByDescending(app => app.IsRecommended).ThenBy(app => app.SortOrder)
                .ThenBy(app => app.Name, _nameComparer);
        _filteredApps = ordered.ToList();

        // 筛选条件、排序、服务端搜索或目录版本变化时回到第一页；仅翻页/改每页数量时保持当前页。
        string signature = string.Join("\u0001", new[]
        {
            query, category, sort ?? string.Empty, _onlineSearchQuery ?? string.Empty,
            _catalogVersion.ToString(CultureInfo.InvariantCulture), AppCatalogService.CurrentSource.ToString()
        });
        if (!string.Equals(signature, _filterSignature, StringComparison.Ordinal))
        {
            _filterSignature = signature;
            _pageIndex = 0;
        }

        ShowCurrentPage();

        bool hasFilters = query.Length > 0 || category != AllCategories || sort == "name" || serverSearchResults;
        string sourceName = AppCatalogService.SourceDisplayName;
        ResultsTitle.Text = serverSearchResults
            ? apiMode ? $"“{_onlineSearchQuery}” 的搜索结果" : $"{sourceName}在线搜索结果"
            : query.Length > 0 ? $"“{query}” 的搜索结果"
            : category == AllCategories ? "全部软件" : category;
        int hiddenIncompatible = AppCatalogService.HiddenIncompatibleCount;
        string hiddenSuffix = !serverSearchResults && hiddenIncompatible > 0
            ? $"（已按当前系统隐藏 {hiddenIncompatible} 款不兼容的软件）" : string.Empty;
        if (searching)
        {
            // api 模式输入即显示“正在搜索…”，结果返回后换成条数。
            ResultsCount.Text = apiMode && query.Length > 0 ? $"正在搜索“{query}”…" : "正在在线搜索…";
        }
        else if (!hasData)
        {
            ResultsCount.Text = "目录尚未加载";
        }
        else if (apiMode && serverSearchResults && _apiSearchError is not null)
        {
            ResultsCount.Text = "搜索失败，请稍后重试。";
        }
        else if (serverSearchResults)
        {
            ResultsCount.Text = apiMode
                ? $"搜索结果共 {_filteredApps.Count} 款"
                : $"找到 {_filteredApps.Count} 款在线结果";
        }
        else if (query.Length > 0 || category != AllCategories)
        {
            ResultsCount.Text = $"找到 {_filteredApps.Count} 款，共 {_catalogApps.Count} 款软件{hiddenSuffix}";
        }
        else
        {
            ResultsCount.Text = $"共 {_filteredApps.Count} 款软件{hiddenSuffix}";
        }

        ApiSearchProgress.Visibility = _isApiSearching ? Visibility.Visible : Visibility.Collapsed;
        ResetFiltersButton.Visibility = hasFilters ? Visibility.Visible : Visibility.Collapsed;
        // 只有旧服务端兼容的“在线搜索”结果才需要“返回本地目录”；api 模式靠清空搜索框恢复目录。
        ReturnToCatalogButton.Visibility = serverSearchResults && !apiMode ? Visibility.Visible : Visibility.Collapsed;

        // api 模式第一次搜索时目录可能还没加载完：用空状态承载“正在搜索…”，避免误显示“目录尚未加载”。
        bool showEmpty = _filteredApps.Count == 0 && (hasData || (apiMode && _isApiSearching));
        EmptyState.Visibility = showEmpty ? Visibility.Visible : Visibility.Collapsed;
        // 旧服务端兼容按钮：服务端没下发 search_mode 且 online_search 允许时才显示；catalog 模式目录完整
        // 本地搜索即可，api 模式搜索框自动请求服务端搜索 API，两者都不显示按钮。
        bool canOnlineSearch = !serverSearchResults && !apiMode && !searching && query.Length > 0
            && _hasSuccessfulLoad && AppCatalogService.OnlineSearchAvailable;
        string onlineSearchText = $"在{sourceName}在线搜索";
        OnlineSearchButton.Visibility = showEmpty && canOnlineSearch
            ? Visibility.Visible : Visibility.Collapsed;
        OnlineSearchButton.IsEnabled = !_isOnlineSearching;
        OnlineSearchButton.Content = onlineSearchText;
        AutomationProperties.SetName(OnlineSearchButton, onlineSearchText);
        bool showOnlineStatus = showEmpty && (searching || (!apiMode && searchError is not null));
        OnlineSearchStatusPanel.Visibility = showOnlineStatus ? Visibility.Visible : Visibility.Collapsed;
        OnlineSearchProgress.Visibility = searching ? Visibility.Visible : Visibility.Collapsed;
        if (showOnlineStatus && !searching && !apiMode && searchError is not null)
        {
            OnlineSearchStatusText.Text = searchError;
        }

        if (showEmpty && apiMode && _isApiSearching)
        {
            EmptyTitleText.Text = $"正在{sourceName}搜索…";
            EmptyDescriptionText.Text = query.Length > 0 ? $"正在搜索“{query}”，请稍候。" : "请稍候。";
        }
        else if (serverSearchResults && apiMode)
        {
            EmptyTitleText.Text = _apiSearchError is not null ? "搜索失败" : "没有找到相关软件";
            EmptyDescriptionText.Text = _apiSearchError is not null
                ? $"{_apiSearchError} 可以稍后重试，或清空搜索查看完整目录。"
                : "可以换一个关键词后重试，或清空搜索查看完整目录。";
        }
        else if (serverSearchResults)
        {
            EmptyTitleText.Text = $"{sourceName}没有找到相关软件";
            EmptyDescriptionText.Text = string.IsNullOrWhiteSpace(_onlineSearchError)
                ? "可以返回本地目录，或换一个关键词后再试。" : _onlineSearchError;
        }
        else if (searching)
        {
            EmptyTitleText.Text = "正在搜索…";
            EmptyDescriptionText.Text = "请稍候。";
        }
        else
        {
            EmptyTitleText.Text = _catalogApps.Count == 0 ? "目录中还没有软件" : "没有找到匹配的软件";
            EmptyDescriptionText.Text = _catalogApps.Count == 0
                ? "稍后重新打开软件列表，查看新加入的软件。"
                : canOnlineSearch ? $"本地目录没有匹配结果，可以在{sourceName}在线搜索。" : "试试其他关键词，或清空分类和搜索条件。";
        }
        EmptyResetButton.Content = apiMode && (serverSearchResults || _isApiSearching) ? "清空搜索"
            : serverSearchResults ? "返回本地目录" : "显示全部软件";
        AppsList.Visibility = hasData && _filteredApps.Count > 0 ? Visibility.Visible : Visibility.Collapsed;
        UpdateLoadStatus();
    }

    private int PageCount => Math.Max(1, (_filteredApps.Count + _pageSize - 1) / _pageSize);

    /// <summary>把当前页写入 <see cref="AppItems"/>（列表只承载一页，其余结果留在内存里用于分页）。</summary>
    private void ShowCurrentPage()
    {
        int pages = PageCount;
        _pageIndex = Math.Clamp(_pageIndex, 0, pages - 1);
        string? selectedId = (AppsList.SelectedItem as AppItem)?.Id;
        AppItems.Clear();
        int start = _pageIndex * _pageSize;
        int end = Math.Min(start + _pageSize, _filteredApps.Count);
        for (int index = start; index < end; index++)
        {
            AppItems.Add(_filteredApps[index]);
        }

        AppsList.SelectedItem = AppItems.FirstOrDefault(app => app.Id == selectedId);
        UpdatePagination();
    }

    private void UpdatePagination()
    {
        bool show = _filteredApps.Count > 0;
        PaginationBar.Visibility = show ? Visibility.Visible : Visibility.Collapsed;
        if (!show)
        {
            return;
        }

        int pages = PageCount;
        PaginationInfoText.Text = $"第 {_pageIndex + 1} / {pages} 页 · 共 {_filteredApps.Count} 款";
        PageNumberText.Text = $"{_pageIndex + 1} / {pages}";
        PreviousPageButton.IsEnabled = _pageIndex > 0;
        NextPageButton.IsEnabled = _pageIndex < pages - 1;
    }

    private void PreviousPage_OnClick(object sender, RoutedEventArgs args)
    {
        if (_pageIndex <= 0)
        {
            return;
        }

        _pageIndex--;
        ShowCurrentPage();
        ScrollListToTop();
    }

    private void NextPage_OnClick(object sender, RoutedEventArgs args)
    {
        if (_pageIndex >= PageCount - 1)
        {
            return;
        }

        _pageIndex++;
        ShowCurrentPage();
        ScrollListToTop();
    }

    private void PageSizeCombo_OnSelectionChanged(object sender, SelectionChangedEventArgs args)
    {
        if (!_isInitialized || PageSizeCombo.SelectedItem is not ComboBoxItem item
            || !int.TryParse(item.Tag?.ToString(), NumberStyles.Integer, CultureInfo.InvariantCulture, out int size)
            || size <= 0 || size == _pageSize)
        {
            return;
        }

        _pageSize = size;
        _pageIndex = 0;
        ShowCurrentPage();
        ScrollListToTop();
    }

    private void ScrollListToTop()
    {
        if (AppItems.Count > 0)
        {
            AppsList.ScrollIntoView(AppItems[0]);
        }
    }

    private void UpdateLoadStatus()
    {
        LoadingPanel.Visibility = _isLoading ? Visibility.Visible : Visibility.Collapsed;
        LoadingStatusText.Text = "正在加载软件目录…";
        CatalogErrorPanel.Visibility = _loadError is not null ? Visibility.Visible : Visibility.Collapsed;
        CatalogErrorText.Text = _loadError is null ? string.Empty
            : _hasSuccessfulLoad ? $"{_loadError} 当前仍显示上次成功加载的目录。" : _loadError;
        CatalogCacheStatusText.Text = AppCatalogService.CacheDescription;
        SourceStatusText.Text = BuildSourceStatusText();
    }

    private string BuildSourceStatusText()
    {
        string sourceName = AppCatalogService.SourceDisplayName;
        if (_isApiSearching)
        {
            return $"{sourceName} · 正在搜索";
        }

        if (_apiSearchError is not null && _onlineSearchQuery is not null)
        {
            return $"{sourceName} · 搜索失败";
        }

        if (_onlineSearchQuery is not null)
        {
            return AppCatalogService.UsesApiSearch
                ? $"{sourceName} · 服务端搜索结果"
                : $"{sourceName} · 在线搜索结果";
        }

        if (!_hasSuccessfulLoad)
        {
            return sourceName;
        }

        string status = $"{sourceName} · 共 {_catalogApps.Count} 款应用";
        if (!AppCatalogService.IsStoreCatalogSource)
        {
            return status;
        }

        // 联想应用商店与 360 软件管家的目录请求只读服务端快照：显示目录自身更新时间，便于确认自动更新进度。
        // 该函数会在加载/缓存变化/筛选等多条路径重复调用，因此每次都从基础文案重建，不做字符串追加。
        DateTimeOffset? updatedAt = AppCatalogService.CatalogUpdatedAtUtc
                                    ?? AppCatalogService.CacheState.UpdatedAtUtc;
        return updatedAt.HasValue
            ? $"{status} · 目录更新于 {updatedAt.Value.ToLocalTime():MM-dd HH:mm}"
            : status;
    }

    private void OnCacheChanged(object? sender, EventArgs args)
    {
        if (!Dispatcher.CheckAccess())
        {
            _ = Dispatcher.BeginInvoke(new Action(() => OnCacheChanged(sender, args)));
            return;
        }
        CatalogCacheStatusText.Text = AppCatalogService.CacheDescription;
        SourceStatusText.Text = BuildSourceStatusText();
    }

    private static bool MatchesSearch(AppItem app, string[] terms)
    {
        if (terms.Length == 0) return true;
        string?[] searchableFields = new[]
            {
                app.Name, app.Description, app.LongDescription, app.Category, app.Publisher,
                app.SourceName, app.DownloadCount, app.SizeDisplay
            }
            .Concat(app.Tags)
            .Concat(app.Labels)
            .ToArray();
        return terms.All(term => searchableFields.Any(field =>
            field?.Contains(term, StringComparison.OrdinalIgnoreCase) == true));
    }

    private void ResetFilters_OnClick(object sender, RoutedEventArgs args)
    {
        ExitOnlineSearchState();
        _isInitialized = false;
        SearchBox.Clear();
        SortCombo.SelectedIndex = 0;
        _isInitialized = true;
        SelectCategory(AllCategories);
        SearchBox.Focus();
    }

    private void ReturnToCatalog_OnClick(object sender, RoutedEventArgs args)
    {
        ExitOnlineSearchState();
        ApplyFilters();
        SearchBox.Focus();
    }

    private async void OnlineSearch_OnClick(object sender, RoutedEventArgs args)
    {
        string query = SearchBox.Text.Trim();
        // 旧服务端兼容入口：服务端下发 search_mode 后由搜索模式决定搜索方式（catalog 本地搜索、
        // api 自动调用服务端搜索 API），不再显示/触发这个手动按钮。
        if (query.Length == 0 || _isOnlineSearching || _isApiSearching
            || AppCatalogService.UsesApiSearch || !AppCatalogService.OnlineSearchAvailable)
        {
            return;
        }

        string sourceName = AppCatalogService.SourceDisplayName;
        _isOnlineSearching = true;
        _onlineSearchError = null;
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Info($"{sourceName}在线搜索开始：关键词“{query}”。");
        OnlineSearchStatusText.Text = $"正在{sourceName}在线搜索“{query}”…";
        OnlineSearchStatusPanel.Visibility = Visibility.Visible;
        OnlineSearchProgress.Visibility = Visibility.Visible;
        ApplyFilters();

        _onlineSearchCancellation?.Cancel();
        var cancellation = new CancellationTokenSource();
        _onlineSearchCancellation = cancellation;
        try
        {
            var results = await AppCatalogService.SearchStoreAsync(query, cancellation.Token);
            cancellation.Token.ThrowIfCancellationRequested();
            if (results.Count == 0)
            {
                _onlineSearchApps = Array.Empty<AppItem>();
                _onlineSearchQuery = null;
                _onlineSearchError = $"{sourceName}没有找到匹配的软件，可以换一个关键词再试。";
                AppLogService.Info(
                    $"{sourceName}在线搜索完成：关键词“{query}”，没有匹配结果，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            }
            else
            {
                _onlineSearchApps = results;
                _onlineSearchQuery = query;
                _onlineSearchError = null;
                if (_selectedCategory != AllCategories)
                {
                    _selectedCategory = AllCategories;
                    CategoryChanged?.Invoke(_selectedCategory);
                }
                AppLogService.Info(
                    $"{sourceName}在线搜索完成：关键词“{query}”，{results.Count} 条结果，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            }
        }
        catch (OperationCanceledException) when (cancellation.IsCancellationRequested)
        {
            // 离开页面或用户主动返回目录会取消搜索，此时不覆盖当前界面状态。
            AppLogService.Verbose($"{sourceName}在线搜索已取消：关键词“{query}”，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            if (_isPageActive && ReferenceEquals(_onlineSearchCancellation, cancellation))
            {
                _onlineSearchError = "在线搜索已取消。";
            }
        }
        catch (OperationCanceledException)
        {
            _onlineSearchError = $"{sourceName}在线搜索超时，请稍后重试。";
            AppLogService.Warn(
                $"{sourceName}在线搜索超时：关键词“{query}”，耗时 {stopwatch.ElapsedMilliseconds} ms。");
        }
        catch (HttpRequestException exception)
        {
            _onlineSearchError = exception.StatusCode.HasValue
                ? $"{sourceName}在线搜索失败（HTTP {(int)exception.StatusCode.Value}），请稍后重试。"
                : "无法连接软件服务，请确认服务已启动后重试。";
            AppLogService.Warn(
                $"{sourceName}在线搜索失败：关键词“{query}”，" +
                $"HTTP {(exception.StatusCode.HasValue ? ((int)exception.StatusCode.Value).ToString(CultureInfo.InvariantCulture) : "无响应")}，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (JsonException exception)
        {
            _onlineSearchError = $"{sourceName}返回的搜索结果格式不正确，请稍后重试。";
            AppLogService.Warn(
                $"{sourceName}在线搜索结果解析失败：关键词“{query}”，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (System.IO.IOException exception)
        {
            _onlineSearchError = "在线搜索时连接中断，请重试。";
            AppLogService.Warn(
                $"{sourceName}在线搜索连接中断：关键词“{query}”，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (InvalidDataException exception)
        {
            _onlineSearchError = $"{sourceName}返回的搜索结果无效，请稍后重试。";
            AppLogService.Warn(
                $"{sourceName}在线搜索结果无效：关键词“{query}”，{exception.GetType().Name}：{exception.Message}",
                exception);
        }
        catch (Exception error) when (error is not OutOfMemoryException)
        {
            _onlineSearchError = $"{sourceName}在线搜索失败，请稍后重试。";
            AppLogService.Error(
                $"{sourceName}在线搜索出现未预期错误：关键词“{query}”，{error.GetType().Name}：{error.Message}，" +
                $"耗时 {stopwatch.ElapsedMilliseconds} ms。",
                error);
            Debug.WriteLine(error);
        }
        finally
        {
            if (ReferenceEquals(_onlineSearchCancellation, cancellation))
            {
                _onlineSearchCancellation = null;
            }
            cancellation.Dispose();
            _isOnlineSearching = false;
            OnlineSearchProgress.Visibility = Visibility.Collapsed;
            if (_isPageActive)
            {
                ApplyFilters();
            }
        }
    }

    private void ExitOnlineSearchState()
    {
        _onlineSearchCancellation?.Cancel();
        _onlineSearchCancellation = null;
        // 同时取消 api 模式自动搜索并清空其结果/错误：切换源、切换服务端、清空筛选都会走到这里。
        CancelApiSearch(clearError: true);
        _onlineSearchApps = Array.Empty<AppItem>();
        _onlineSearchQuery = null;
        _onlineSearchError = null;
        _isOnlineSearching = false;
        OnlineSearchProgress.Visibility = Visibility.Collapsed;
        OnlineSearchStatusPanel.Visibility = Visibility.Collapsed;
    }

    private void AppCard_OnLoaded(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: AppItem app })
        {
            QueueIconLoad(app);
        }
    }

    /// <summary>
    /// 联想应用商店 / 360 软件管家约有 5000~6000 款软件，图标只在行容器显示时按需加载；
    /// 内置源继续使用原有预热逻辑。
    /// </summary>
    private void QueueIconLoad(AppItem app)
    {
        if (!_isPageActive || !AppCatalogService.IsStoreCatalogSource) return;
        if (!string.IsNullOrWhiteSpace(app.DisplayIconSource)) return;
        if (!_iconLoads.Add(app.Id)) return;
        _ = EnsureRowIconAsync(app);
    }

    private async Task EnsureRowIconAsync(AppItem app)
    {
        try
        {
            await AppCatalogService.EnsureIconAsync(app, IconLoadToken);
        }
        catch (OperationCanceledException)
        {
            // 离开页面或切换软件源会取消未完成的按需图标加载。
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException or UnauthorizedAccessException)
        {
            // 图标缺失不应影响列表显示，行内继续使用文字徽章。
            Debug.WriteLine(error);
        }
        finally
        {
            _iconLoads.Remove(app.Id);
        }
    }

    private void DetailsButton_OnClick(object sender, RoutedEventArgs args)
    {
        if (sender is FrameworkElement { DataContext: AppItem app }) AppDetailsRequested?.Invoke(app);
        args.Handled = true;
    }

    private void AppCard_OnMouseLeftButtonUp(object sender, MouseButtonEventArgs args)
    {
        if (IsInsideButton(args.OriginalSource as DependencyObject)) return;
        if (sender is FrameworkElement { DataContext: AppItem app })
        {
            AppDetailsRequested?.Invoke(app);
            args.Handled = true;
        }
    }

    private void AppsList_OnPreviewKeyDown(object sender, KeyEventArgs args)
    {
        if ((args.Key == Key.Enter || args.Key == Key.Space) && AppsList.SelectedItem is AppItem app &&
            !IsInsideButton(args.OriginalSource as DependencyObject))
        {
            AppDetailsRequested?.Invoke(app);
            args.Handled = true;
        }
    }

    private async void SoftwareIcon_OnImageFailed(object sender, ExceptionRoutedEventArgs args)
    {
        if (sender is Image { DataContext: AppItem app, Parent: Border imageContainer })
        {
            imageContainer.ClearValue(VisibilityProperty);
            await AppCatalogService.DiscardIconAsync(app);
        }
        args.Handled = true;
    }

    private void SoftwareIconContainer_OnDataContextChanged(object sender, DependencyPropertyChangedEventArgs args)
    {
        // Recycling a failed image container for a different app must allow the new app's image to load.
        if (sender is Border imageContainer) imageContainer.ClearValue(VisibilityProperty);
        // 回收容器换到新行时也要触发商店源的按需图标加载。
        if (args.NewValue is AppItem app) QueueIconLoad(app);
    }

    private static bool IsInsideButton(DependencyObject? source)
    {
        while (source is not null)
        {
            if (source is ButtonBase) return true;
            source = source is Visual or System.Windows.Media.Media3D.Visual3D
                ? VisualTreeHelper.GetParent(source) : LogicalTreeHelper.GetParent(source);
        }
        return false;
    }
}

