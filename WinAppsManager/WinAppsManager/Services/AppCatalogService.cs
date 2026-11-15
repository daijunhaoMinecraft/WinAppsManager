using System.Collections.Concurrent;
using System.Diagnostics;
using System.Diagnostics.CodeAnalysis;
using System.IO;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Text.Json;
using WinAppsManager.Models;

namespace WinAppsManager.Services;

/// <summary>
/// 软件目录客户端：同时支持“联想应用商店（lenovo，默认）”“内置精选（builtin）”与“360 软件管家（soft360）”三个软件源。
/// <para>
/// 内置源沿用原有 <c>/api/v1/apps</c> 接口、图标预热与离线缓存行为；联想源调用
/// <c>/api/v1/store/lenovo/*</c>、360 源调用 <c>/api/v1/store/360/*</c>，目录都是 5000~6000 款，
/// 因此不预热全部图标，由列表行按需调用 <see cref="EnsureIconAsync"/>。切换源用
/// <see cref="SwitchSourceAsync"/>，切换后目录缓存按源隔离（缓存 key 含源标识）。
/// </para>
/// </summary>
public static class AppCatalogService
{
    private const string BuiltinSourceId = StoreCatalogMapper.BuiltinSourceId;
    private const string LenovoSourceId = StoreCatalogMapper.LenovoSourceId;
    private const string Soft360SourceId = Soft360CatalogMapper.SourceId;
    private const int StoreSearchLimit = 30;

    /// <summary>搜索模式（服务端 <c>/sources</c> 的 <c>search_mode</c>）：目录完整，客户端本地搜索即可。</summary>
    public const string SearchModeCatalog = "catalog";

    /// <summary>搜索模式（服务端 <c>/sources</c> 的 <c>search_mode</c>）：目录只是子集，搜索必须调用服务端 API。</summary>
    public const string SearchModeApi = "api";

    /// <summary>当前后端服务地址（设置中可自定义，始终以 / 结尾）。</summary>
    public static string BackendBaseUrl
    {
        get
        {
            AppSettingsService.Initialize();
            return AppSettingsService.Current.BackendBaseUrl;
        }
    }

    /// <summary>当前后端根 Uri；请求路径都是相对它拼接，切换服务端后自动生效。</summary>
    public static Uri BackendUri => new(BackendBaseUrl);

    /// <summary>
    /// 构造联想商店视频代理地址（只做拼接与校验，不发请求）：
    /// <c>{BackendBaseUrl}api/v1/store/lenovo/apps/{softId}/screenshots/{index}/video</c>。
    /// <paramref name="softId"/> 是联想商店原始的 <c>soft_id</c>（<see cref="AppItem.SourceAppId"/>，区分大小写）；
    /// <paramref name="screenshotIndex"/> 是 <see cref="AppScreenshot.Index"/>，封面固定为 -1、不是视频。
    /// 参数无效或服务端地址异常时返回 <see langword="null"/>，由调用方降级为只显示缩略图。
    /// 客户端只访问本机后端，不直连联想 CDN。
    /// </summary>
    public static string? TryBuildLenovoVideoUrl(string? softId, int screenshotIndex)
    {
        string id = softId?.Trim() ?? string.Empty;
        if (id.Length == 0 || id is "." or ".." || screenshotIndex < 0)
        {
            return null;
        }

        try
        {
            return BuildApiUri(
                $"store/lenovo/apps/{Uri.EscapeDataString(id)}/screenshots/{screenshotIndex}/video").AbsoluteUri;
        }
        catch (Exception error) when (error is UriFormatException or ArgumentException)
        {
            return null;
        }
    }

    private static HttpClient CreateClient() => new()
    {
        // 逐请求的超时在 GetAsync 里用 CancellationTokenSource 控制：联想目录首次构建可能较慢。
        // 不使用 BaseAddress：服务端地址可在运行时修改，每次请求都用绝对地址。
        Timeout = Timeout.InfiniteTimeSpan
    };

    private static readonly HttpClient Client = CreateClient();
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        PropertyNamingPolicy = BclCompat.SnakeCaseLower,
        PropertyNameCaseInsensitive = true
    };
    private static readonly TimeSpan DefaultRequestTimeout = TimeSpan.FromSeconds(12);
    private static readonly TimeSpan CatalogRequestTimeout = TimeSpan.FromSeconds(40);
    private static readonly TimeSpan StoreRequestTimeout = TimeSpan.FromSeconds(20);

    /// <summary><see cref="_catalogServerUpdatedAtTicks"/> 的“未知”哨兵值。</summary>
    private const long UnknownCatalogTimestamp = long.MinValue;
    private static readonly string CacheRootPath = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "WinAppsManager", "Cache");

    private static readonly object InitializationGate = new();
    private static readonly SemaphoreSlim CatalogGate = new(1, 1);
    private static readonly SemaphoreSlim IconDownloadGate = new(6, 6);
    private static readonly Dictionary<string, (AppItem App, DateTimeOffset Updated)> Details = new(StringComparer.Ordinal);
    private static readonly ConcurrentDictionary<string, AppItem> KnownApps = new(StringComparer.Ordinal);
    /// <summary>
    /// 服务端 <c>GET /api/v1/sources</c> 下发的“是否支持在线搜索”（按源 ID）。
    /// 写入来自 <see cref="GetSourcesAsync"/>，读取来自 <see cref="OnlineSearchAvailable"/>；
    /// 切换服务端地址时清空（不同服务端能力可能不同）。用并发字典保证多线程读取安全。
    /// </summary>
    private static readonly ConcurrentDictionary<string, bool> OnlineSearchCapabilities = new(StringComparer.OrdinalIgnoreCase);

    /// <summary>
    /// 服务端 <c>GET /api/v1/sources</c> 下发的搜索模式（按源 ID，值只会是
    /// <see cref="SearchModeCatalog"/> 或 <see cref="SearchModeApi"/>）。写入来自 <see cref="GetSourcesAsync"/>，
    /// 读取来自 <see cref="CurrentSearchMode"/>；切换服务端地址时清空（不同服务端可能使用不同搜索方式）。
    /// 旧服务端没有 <c>search_mode</c> 时不记录，读取时回退到按源判断。
    /// </summary>
    private static readonly ConcurrentDictionary<string, string> SearchModes = new(StringComparer.OrdinalIgnoreCase);
    private static readonly List<Task> IconTasks = new();

    private static bool _initialized;
    private static AppSourcePreference _currentSource = AppSourcePreference.Lenovo;
    private static ClientCacheStore? _store;
    private static CancellationTokenSource IconCancellation = new();
    private static IReadOnlyList<AppItem> _apps = Array.Empty<AppItem>();
    private static int _hiddenIncompatible;
    private static bool _hasLoaded;
    private static DateTimeOffset? _catalogUpdatedAt;
    // 服务端 catalog.json 的 updated_at（UTC ticks）。普通请求可能返回几分钟/几小时前的缓存，
    // 因此它和本地加载时间 _catalogUpdatedAt 分开保存；未知时为 long.MinValue（避免 Nullable<T> 撕裂读）。
    private static long _catalogServerUpdatedAtTicks = UnknownCatalogTimestamp;
    private static string? _lastNetworkError;
    private static CatalogCacheState _cacheState = new(CatalogCacheSource.None, null);

    public static IReadOnlyList<AppItem> Apps => Volatile.Read(ref _apps);
    public static bool HasLoaded => Volatile.Read(ref _hasLoaded);
    public static CatalogCacheState CacheState => Volatile.Read(ref _cacheState);

    /// <summary>
    /// 最近一次联想目录响应里的 <c>updated_at</c>（服务端 catalog.json 的生成时间，UTC）。
    /// 普通请求只读服务端缓存，该时间可能早于本地加载时间；尚未成功拉取过联想目录时为 <see langword="null"/>。
    /// 供状态行显示“目录更新于”，未成功加载时调用方应省略该信息。
    /// </summary>
    public static DateTimeOffset? CatalogUpdatedAtUtc
    {
        get
        {
            long ticks = Volatile.Read(ref _catalogServerUpdatedAtTicks);
            return ticks == UnknownCatalogTimestamp ? null : new DateTimeOffset(ticks, TimeSpan.Zero);
        }
    }

    /// <summary>当前软件源偏好（联想应用商店 / 内置精选 / 360 软件管家），已持久化到设置。</summary>
    public static AppSourcePreference CurrentSource
    {
        get
        {
            EnsureInitialized();
            return _currentSource;
        }
    }

    /// <summary>当前软件源是否为联想应用商店。</summary>
    public static bool IsLenovoSource => CurrentSource == AppSourcePreference.Lenovo;

    /// <summary>当前软件源是否为 360 软件管家。</summary>
    public static bool IsSoft360Source => CurrentSource == AppSourcePreference.Soft360;

    /// <summary>
    /// 当前软件源是否为“后端目录型”商店源（联想应用商店 / 360 软件管家）：
    /// 目录全量拉取、不预热全部图标（列表行按需加载），且状态行可显示目录更新时间。
    /// </summary>
    public static bool IsStoreCatalogSource => IsLenovoSource || IsSoft360Source;

    /// <summary>
    /// 当前软件源由服务端下发的搜索模式（<c>catalog</c> / <c>api</c>）；旧服务端没有 <c>search_mode</c>
    /// （或尚未从服务端加载过）时为 <see langword="null"/>。页面据此区分“服务端权威值”与“按源回退”。
    /// </summary>
    public static string? ServerSearchMode
    {
        get
        {
            if (SearchModes.TryGetValue(SourceIdOf(CurrentSource), out var mode))
            {
                return mode;
            }

            return null;
        }
    }

    /// <summary>
    /// 当前软件源生效的搜索模式：<see cref="SearchModeCatalog"/>（目录完整，客户端本地搜索）或
    /// <see cref="SearchModeApi"/>（目录只是子集，搜索走服务端 API）。
    /// <para>
    /// 服务端下发值优先；旧服务端缺失 <c>search_mode</c> 时回退到按源判断（联想应用商店 / 内置精选 = catalog，
    /// 360 软件管家 = api），因此未加载、加载中或切换软件源期间返回值都稳定。
    /// </para>
    /// </summary>
    public static string CurrentSearchMode => ServerSearchMode ?? FallbackSearchMode(CurrentSource);

    /// <summary>当前软件源是否必须调用服务端搜索 API（目录只是各分类热门子集，本地过滤会漏掉目录外软件）。</summary>
    public static bool UsesApiSearch => CurrentSearchMode == SearchModeApi;

    /// <summary>当前软件源是否可以在完整目录上本地搜索（联想应用商店 / 内置精选）。</summary>
    public static bool UsesCatalogSearch => !UsesApiSearch;

    /// <summary>
    /// 旧服务端没有 <c>search_mode</c> 时按源回退：联想应用商店 / 内置精选的目录完整（catalog），
    /// 360 软件管家的目录只是热门子集（api）。
    /// </summary>
    private static string FallbackSearchMode(AppSourcePreference source) =>
        source == AppSourcePreference.Soft360 ? SearchModeApi : SearchModeCatalog;

    /// <summary>
    /// 当前软件源是否需要“在线搜索”按钮（软件列表页空结果时的手动入口）。
    /// <para>
    /// 服务端下发 <c>search_mode</c> 后搜索方式完全由它决定：<c>catalog</c> 模式目录完整（本地搜索即可），
    /// <c>api</c> 模式由搜索框自动调用服务端搜索 API，两者都不再需要旧式“在线搜索”按钮，因此返回
    /// <see langword="false"/>。<c>online_search</c> 只在旧服务端（没有 <c>search_mode</c>）时作为兼容回退：
    /// 服务端给出值就以服务端为准，否则按源判断（联想 / 360 = 支持，内置精选 = 不支持）。
    /// </para>
    /// </summary>
    public static bool OnlineSearchAvailable
    {
        get
        {
            // 新服务端：搜索模式是权威值，catalog / api 两种模式都不需要旧式按钮。
            if (ServerSearchMode is not null)
            {
                return false;
            }

            AppSourcePreference source = CurrentSource;
            if (OnlineSearchCapabilities.TryGetValue(SourceIdOf(source), out bool value))
            {
                return value;
            }

            // 旧服务端没有 online_search 字段时的回退：联想应用商店 / 360 软件管家支持在线搜索，
            // 内置精选源不支持（与旧版客户端硬编码的按源判断完全一致）。
            return source switch
            {
                AppSourcePreference.Lenovo => true,
                AppSourcePreference.Soft360 => true,
                _ => false
            };
        }
    }

    /// <summary>当前软件源显示名，例如“联想应用商店”。</summary>
    public static string SourceDisplayName => GetSourceDisplayName(CurrentSource);

    public static string GetSourceDisplayName(AppSourcePreference source) => source switch
    {
        AppSourcePreference.Lenovo => StoreCatalogMapper.LenovoSourceName,
        AppSourcePreference.Soft360 => Soft360CatalogMapper.SourceName,
        _ => StoreCatalogMapper.BuiltinSourceName
    };

    /// <summary>本地缓存根目录（图标/目录/商店图片都在其下）。</summary>
    public static string CacheDirectory
    {
        get
        {
            EnsureInitialized();
            return _store!.DirectoryPath;
        }
    }

    /// <summary>本地缓存统计（目录文件、图标文件、商店截图/封面图片文件）。</summary>
    public static ClientCacheStatistics CacheStatistics
    {
        get
        {
            var local = Store.Statistics();
            var media = StoreMediaService.CacheStatistics();
            return new ClientCacheStatistics(local.CatalogFiles, local.IconFiles, local.Bytes + media.Bytes, media.Files);
        }
    }

    /// <summary>上次发布目录时因为与当前系统架构/版本不兼容而被隐藏的软件数量。</summary>
    public static int HiddenIncompatibleCount => Volatile.Read(ref _hiddenIncompatible);

    /// <summary>Resolves an icon that the cache could download but not persist to disk.</summary>
    public static bool TryGetMemoryIcon(string? source, [NotNullWhen(true)] out byte[]? bytes) =>
        Store.TryGetMemoryIcon(source, out bytes);

    public static string CacheDescription
    {
        get
        {
            var state = CacheState;
            string sourceName = SourceDisplayName;
            string stateText = state.Source switch
            {
                CatalogCacheSource.Network => "已从软件服务更新",
                CatalogCacheSource.Memory => "正在使用内存缓存",
                CatalogCacheSource.Disk => "正在使用本地缓存",
                CatalogCacheSource.Offline => "正在使用离线缓存",
                _ => "目录缓存尚未加载"
            };
            return state.UpdatedAtUtc.HasValue
                ? $"{sourceName} · {stateText} · 更新于 {state.UpdatedAtUtc.Value.ToLocalTime():MM-dd HH:mm}"
                : $"{sourceName} · {stateText}";
        }
    }

    public static string RuntimeArchitecture => RuntimeInformation.OSArchitecture switch
    {
        Architecture.X64 => "x64",
        Architecture.X86 => "x86",
        Architecture.Arm64 => "arm64",
        _ => throw new PlatformNotSupportedException("当前 Windows 系统架构暂不支持下载安装包。")
    };

    /// <summary>目录内容变化（重新发布目录、详情回写、清空缓存）。可能在后台线程触发，UI 需切回 Dispatcher。</summary>
    public static event EventHandler? CatalogChanged;

    /// <summary>缓存状态/统计变化。可能在后台线程触发，UI 需切回 Dispatcher。</summary>
    public static event EventHandler? CacheChanged;

    /// <summary>软件源切换完成。可能在后台线程触发，UI 需切回 Dispatcher 后重新加载目录。</summary>
    public static event EventHandler? SourceChanged;

    /// <summary>后端服务地址切换完成（内存目录/详情/图标已清空）。可能在后台线程触发，UI 需切回 Dispatcher 后重新加载。</summary>
    public static event EventHandler? BackendChanged;

    // ---------------------------------------------------------------------------------------------
    // 软件源切换
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 切换软件源：保存设置、清空内存目录/详情/已知应用表、取消进行中的图标任务、重建按源隔离的目录缓存，
    /// 然后触发 <see cref="CatalogChanged"/> 与 <see cref="SourceChanged"/>（调用方随后应重新加载目录）。
    /// </summary>
    public static async Task SwitchSourceAsync(AppSourcePreference source, CancellationToken ct = default)
    {
        if (!Enum.IsDefined(source))
        {
            throw new ArgumentOutOfRangeException(nameof(source));
        }

        EnsureInitialized();
        // 这里保留调用方上下文：UI 线程调用时，设置保存与 SourceChanged 事件都在 UI 线程上，
        // 避免 SettingsPage 收到跨线程的 SettingsChanged。
        await CatalogGate.WaitAsync(ct);
        try
        {
            AppSettingsService.SetAppSource(source);
            if (source == _currentSource)
            {
                return;
            }

            string previousName = GetSourceDisplayName(_currentSource);
            string targetName = GetSourceDisplayName(source);
            AppLogService.Info($"正在切换软件源：{previousName} → {targetName}。");
            await CancelIconWarmupAsync();
            _currentSource = source;
            _store = CreateStore(source);
            Details.Clear();
            KnownApps.Clear();
            Volatile.Write(ref _apps, Array.Empty<AppItem>());
            Volatile.Write(ref _hasLoaded, false);
            Volatile.Write(ref _hiddenIncompatible, 0);
            _catalogUpdatedAt = null;
            SetCatalogUpdatedAt(null);
            _lastNetworkError = null;
            SetCacheState(CatalogCacheSource.None, null);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            SourceChanged?.Invoke(null, EventArgs.Empty);
            AppLogService.Info($"已切换到软件源：{targetName}。");
        }
        catch (OperationCanceledException)
        {
            AppLogService.Info($"切换软件源已取消：{GetSourceDisplayName(source)}。");
            throw;
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            AppLogService.Warn(
                $"切换软件源失败：{GetSourceDisplayName(source)}，{exception.GetType().Name}：{exception.Message}",
                exception);
            throw;
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 服务端地址 / 软件源发现
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 切换后端服务地址：保存设置 → 取消图标任务 → 清空内存目录/详情/已知应用表 → 按新地址重建目录缓存，
    /// 然后触发 <see cref="BackendChanged"/> 与 <see cref="CatalogChanged"/>（调用方随后应重新加载目录）。
    /// 地址无效时抛出 <see cref="ArgumentException"/>，不会改动当前地址。
    /// </summary>
    public static async Task SetBackendBaseUrlAsync(string? url)
    {
        EnsureInitialized();
        string normalized = AppSettingsService.NormalizeBackendBaseUrl(url);
        // 与切换软件源一致：保留调用方上下文（UI 线程），让设置保存与事件都在 UI 线程上触发。
        await CatalogGate.WaitAsync().ConfigureAwait(true);
        try
        {
            if (string.Equals(normalized, BackendBaseUrl, StringComparison.OrdinalIgnoreCase))
            {
                return;
            }

            string previous = BackendBaseUrl;
            AppLogService.Info($"正在切换后端服务地址：{previous} → {normalized}。");
            AppSettingsService.SetBackendBaseUrl(normalized);
            await CancelIconWarmupAsync().ConfigureAwait(true);
            _store = CreateStore(_currentSource);
            Details.Clear();
            KnownApps.Clear();
            Volatile.Write(ref _apps, Array.Empty<AppItem>());
            Volatile.Write(ref _hasLoaded, false);
            Volatile.Write(ref _hiddenIncompatible, 0);
            _catalogUpdatedAt = null;
            SetCatalogUpdatedAt(null);
            _lastNetworkError = null;
            SetCacheState(CatalogCacheSource.None, null);
            // 不同服务端的软件源能力可能不同：清空在线搜索能力表与搜索模式表，等下一次 /sources 加载重新填充；
            // 清空后 OnlineSearchAvailable / CurrentSearchMode 回退到按源判断，不会误用旧服务端的标志。
            OnlineSearchCapabilities.Clear();
            SearchModes.Clear();
            BackendChanged?.Invoke(null, EventArgs.Empty);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            AppLogService.Info($"已切换后端服务地址：{normalized}。");
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    /// <summary>
    /// 从后端 <c>GET /api/v1/sources</c> 动态发现软件源。返回服务端给出的全部源（可能包含客户端不认识的 ID）。
    /// <para>
    /// 同时记录每个源的 <c>search_mode</c>（供 <see cref="CurrentSearchMode"/> / <see cref="UsesApiSearch"/>）
    /// 与 <c>online_search</c>（旧服务端兼容回退，供 <see cref="OnlineSearchAvailable"/>）。
    /// 旧服务端缺这些字段时不记录，读取时回退到按源判断。
    /// </para>
    /// 失败时抛出异常，调用方应回退到静态软件源列表。
    /// </summary>
    public static async Task<IReadOnlyList<StoreSourceInfo>> GetSourcesAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        var response = await GetAsync<StoreSourcesResponse>("sources", ct, TimeSpan.FromSeconds(10)).ConfigureAwait(false);
        var items = new List<StoreSourceInfo>();
        bool anyOnlineSearch = false;
        bool anySearchMode = false;
        foreach (var item in response.Items ?? new List<StoreSourceInfo>())
        {
            if (item is null || string.IsNullOrWhiteSpace(item.Id))
            {
                continue;
            }

            items.Add(item);
            string sourceId = item.Id.Trim();
            // 线程安全地记住服务端下发的在线搜索能力；旧服务端没有 online_search（null）时不记录。
            if (item.OnlineSearch.HasValue)
            {
                OnlineSearchCapabilities[sourceId] = item.OnlineSearch.Value;
                anyOnlineSearch = true;
            }

            // search_mode 是新契约的权威值：catalog = 目录完整、客户端本地搜索；api = 目录是子集、调用服务端搜索 API。
            string? searchMode = NormalizeSearchMode(item.SearchMode);
            if (searchMode is not null)
            {
                SearchModes[sourceId] = searchMode;
                anySearchMode = true;
            }
        }

        if (!anyOnlineSearch)
        {
            // 服务端完全没有下发该字段（旧版本）：清空能力表，让 OnlineSearchAvailable 回退到按源判断，
            // 避免继续沿用更新过的服务端留下的旧标志。
            OnlineSearchCapabilities.Clear();
        }

        if (!anySearchMode)
        {
            // 同上：旧服务端没有 search_mode，让 CurrentSearchMode 回退到按源判断（联想/内置 catalog、360 api）。
            SearchModes.Clear();
        }

        if (anyOnlineSearch)
        {
            AppLogService.Verbose(
                "服务端在线搜索能力：" + string.Join("，", OnlineSearchCapabilities
                    .OrderBy(pair => pair.Key, StringComparer.Ordinal)
                    .Select(pair => $"{pair.Key}={pair.Value}")));
        }

        if (anySearchMode)
        {
            AppLogService.Verbose(
                "服务端搜索模式：" + string.Join("，", SearchModes
                    .OrderBy(pair => pair.Key, StringComparer.Ordinal)
                    .Select(pair => $"{pair.Key}={pair.Value}")) +
                $"；当前源生效值 {CurrentSearchMode}" +
                (ServerSearchMode is null ? "（旧服务端未下发，按源回退）" : string.Empty));
        }

        return items.AsReadOnly();
    }

    /// <summary>
    /// 把服务端下发的 <c>search_mode</c> 规范成 <see cref="SearchModeCatalog"/> / <see cref="SearchModeApi"/>；
    /// 未知值返回 <see langword="null"/>（不记录，读取时按源回退）。
    /// </summary>
    private static string? NormalizeSearchMode(string? value)
    {
        string text = value?.Trim() ?? string.Empty;
        if (text.Equals(SearchModeCatalog, StringComparison.OrdinalIgnoreCase)) return SearchModeCatalog;
        if (text.Equals(SearchModeApi, StringComparison.OrdinalIgnoreCase)) return SearchModeApi;
        return null;
    }

    /// <summary>
    /// 测试服务端连接：对候选地址请求 <c>GET /api/v1/health</c>（5 秒超时），不修改当前设置。
    /// 地址格式无效时抛出 <see cref="ArgumentException"/>。
    /// </summary>
    public static async Task<(bool Ok, string Message)> TestConnectionAsync(string? url, CancellationToken ct = default)
    {
        string normalized = AppSettingsService.NormalizeBackendBaseUrl(url);
        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
        timeout.CancelAfter(TimeSpan.FromSeconds(5));
        try
        {
            var healthUri = new Uri(new Uri(normalized), "api/v1/health");
            using var response = await Client
                .GetAsync(healthUri, HttpCompletionOption.ResponseHeadersRead, timeout.Token)
                .ConfigureAwait(false);
            return response.IsSuccessStatusCode
                ? (true, "连接正常。")
                : (false, $"服务返回 HTTP {(int)response.StatusCode}。");
        }
        catch (OperationCanceledException) when (!ct.IsCancellationRequested)
        {
            return (false, "连接超时（5 秒）。");
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidOperationException)
        {
            return (false, $"无法连接到服务端：{error.Message}");
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 目录
    // ---------------------------------------------------------------------------------------------

    public static async Task<IReadOnlyList<AppItem>> GetAppsAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        string sourceName = SourceDisplayName;
        var stopwatch = Stopwatch.StartNew();
        AppLogService.Info($"开始加载软件目录：源={sourceName}。");
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            IReadOnlyList<AppItem> apps;
            if (_currentSource == AppSourcePreference.Lenovo)
            {
                apps = await GetLenovoAppsAsync(ct).ConfigureAwait(false);
            }
            else if (_currentSource == AppSourcePreference.Soft360)
            {
                apps = await GetSoft360AppsAsync(ct).ConfigureAwait(false);
            }
            else
            {
                apps = await GetBuiltinAppsAsync(ct).ConfigureAwait(false);
            }
            AppLogService.Info(
                $"软件目录加载完成：源={sourceName}，{apps.Count} 款软件，缓存={CacheState.Source}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            return apps;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            AppLogService.Info($"软件目录加载已取消：源={sourceName}，耗时 {stopwatch.ElapsedMilliseconds} ms。");
            throw;
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            AppLogService.Warn(
                $"软件目录加载失败：源={sourceName}，耗时 {stopwatch.ElapsedMilliseconds} ms，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
            throw;
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    private static async Task<IReadOnlyList<AppItem>> GetBuiltinAppsAsync(CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        if (HasLoaded && IsFresh(_catalogUpdatedAt, now))
        {
            await Store.AssignCachedIconsAsync(Apps, ct).ConfigureAwait(false);
            SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline, _catalogUpdatedAt,
                Store.LastError, _lastNetworkError);
            QueueIcons(Apps, _lastNetworkError is null);
            return Apps;
        }
        var disk = await ReadUsableDiskAsync(now, ct).ConfigureAwait(false);
        if (!HasLoaded && disk?.IsFresh(now) == true)
        {
            await PublishCatalogAsync(disk.Items!, disk.UpdatedAtUtc, CatalogCacheSource.Disk, ct).ConfigureAwait(false);
            return Apps;
        }
        try
        {
            // 普通加载固定走非强制 GET；不再拼任何 refresh 查询参数（客户端已无手动刷新入口）。
            var response = await GetAsync<CatalogResponse>(WithRuntimeArchitecture("apps"), ct).ConfigureAwait(false);
            if (response.Items is null || response.Total is null || response.Total < response.Items.Count)
                throw new JsonException("软件目录响应缺少有效的 items 或 total 字段。");
            var normalized = response.Items.Select(app => NormalizeApp(app)).ToArray();
            ValidateUniqueIds(normalized);
            ct.ThrowIfCancellationRequested();
            _lastNetworkError = null;
            Details.Clear();
            await PublishCatalogAsync(normalized, DateTimeOffset.UtcNow, CatalogCacheSource.Network, ct).ConfigureAwait(false);
            // 内置源维持既有行为：网络目录只更新内存（详情回写时会写磁盘缓存）。
            SetCacheState(CatalogCacheSource.Network, _catalogUpdatedAt, Store.LastError);
            return Apps;
        }
        catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested)
        {
            _lastNetworkError = FriendlyMetadataError(error);
            if (HasLoaded && CanUseOffline(_catalogUpdatedAt, now))
            {
                AppLogService.Warn(
                    $"内置精选目录更新失败，正在使用内存缓存：{_lastNetworkError}（{error.GetType().Name}：{error.Message}）");
                await Store.AssignCachedIconsAsync(Apps, ct).ConfigureAwait(false);
                SetCacheState(CatalogCacheSource.Offline, _catalogUpdatedAt, Store.LastError, _lastNetworkError);
                return Apps;
            }
            if (disk is not null)
            {
                AppLogService.Warn(
                    $"内置精选目录更新失败，已回退本地目录缓存：{_lastNetworkError}（{error.GetType().Name}：{error.Message}）");
                await PublishCatalogAsync(disk.Items!, disk.UpdatedAtUtc, CatalogCacheSource.Offline, ct).ConfigureAwait(false);
                return Apps;
            }
            AppLogService.Warn(
                $"内置精选目录更新失败且没有可用缓存：{_lastNetworkError}（{error.GetType().Name}：{error.Message}）");
            Volatile.Write(ref _apps, Array.Empty<AppItem>());
            Volatile.Write(ref _hasLoaded, false);
            _catalogUpdatedAt = null;
            SetCacheState(CatalogCacheSource.None, null, Store.LastError, _lastNetworkError);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            throw;
        }
    }

    /// <summary>
    /// 后端目录型商店源（联想应用商店 / 360 软件管家）的通用目录加载流程：
    /// 5 分钟内存缓存 → 磁盘缓存 → 普通 GET 拉取快照并落盘；失败时按 24 小时窗口回退内存/磁盘缓存。
    /// 两种源都不预热全部图标（目录约 5000~6000 款），由列表行按需调用 <see cref="EnsureIconAsync"/>。
    /// <paramref name="request"/> 负责发请求并把响应映射为 <see cref="AppItem"/>（内部校验 items/total/来源）。
    /// </summary>
    private static async Task<IReadOnlyList<AppItem>> GetStoreSourceAppsAsync(string sourceName, string staleWarning,
        Func<CancellationToken, Task<StoreCatalogSnapshot>> request, CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        if (HasLoaded && IsFresh(_catalogUpdatedAt, now))
        {
            SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline,
                CatalogUpdatedAtUtc ?? _catalogUpdatedAt, Store.LastError, _lastNetworkError);
            return Apps;
        }

        var disk = await ReadUsableDiskAsync(now, ct).ConfigureAwait(false);
        if (!HasLoaded && disk?.IsFresh(now) == true)
        {
            await PublishCatalogAsync(disk.Items!, disk.UpdatedAtUtc, CatalogCacheSource.Disk, ct).ConfigureAwait(false);
            return Apps;
        }

        try
        {
            // 目录请求永远走普通 GET：后端只读本地快照、由后端定时线程自动更新；客户端已移除手动刷新入口，
            // 不再拼任何 refresh 查询参数。
            var snapshot = await request(ct).ConfigureAwait(false);
            ct.ThrowIfCancellationRequested();
            _lastNetworkError = null;
            Details.Clear();
            DateTimeOffset fetchedAt = DateTimeOffset.UtcNow;
            await PublishCatalogAsync(snapshot.Items, fetchedAt, CatalogCacheSource.Network, ct).ConfigureAwait(false);
            // 目录同样落盘（key 含源标识），离线时仍可浏览；目录很大，写入耗时但只在目录更新时发生。
            await Store.WriteCatalogAsync(snapshot.Items, fetchedAt, ct).ConfigureAwait(false);
            // 记录服务端快照的 updated_at：普通请求可能直接拿到几分钟/几小时前的缓存，
            // 不能把本地加载时间当成“目录更新于”。stale 仍然写入 CacheState.Warning（沿用现有提示）。
            SetCatalogUpdatedAt(snapshot.ServerUpdatedAt);
            SetCacheState(CatalogCacheSource.Network, CatalogUpdatedAtUtc ?? fetchedAt,
                snapshot.Stale ? staleWarning : Store.LastError);
            return Apps;
        }
        catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested)
        {
            _lastNetworkError = FriendlyMetadataError(error);
            if (HasLoaded && CanUseOffline(_catalogUpdatedAt, now))
            {
                AppLogService.Warn(
                    $"{sourceName}目录更新失败，正在使用内存缓存：{_lastNetworkError}（{error.GetType().Name}：{error.Message}）");
                SetCacheState(CatalogCacheSource.Offline, CatalogUpdatedAtUtc ?? _catalogUpdatedAt, Store.LastError, _lastNetworkError);
                return Apps;
            }
            if (disk is not null)
            {
                AppLogService.Warn(
                    $"{sourceName}目录更新失败，已回退本地目录缓存：{_lastNetworkError}（{error.GetType().Name}：{error.Message}）");
                await PublishCatalogAsync(disk.Items!, disk.UpdatedAtUtc, CatalogCacheSource.Offline, ct).ConfigureAwait(false);
                return Apps;
            }
            AppLogService.Warn(
                $"{sourceName}目录更新失败且没有可用缓存：{_lastNetworkError}（{error.GetType().Name}：{error.Message}）");
            Volatile.Write(ref _apps, Array.Empty<AppItem>());
            Volatile.Write(ref _hasLoaded, false);
            _catalogUpdatedAt = null;
            SetCatalogUpdatedAt(null);
            SetCacheState(CatalogCacheSource.None, null, Store.LastError, _lastNetworkError);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            throw;
        }
    }

    private static Task<IReadOnlyList<AppItem>> GetLenovoAppsAsync(CancellationToken ct) =>
        GetStoreSourceAppsAsync(StoreCatalogMapper.LenovoSourceName,
            "联想应用商店目录当前为服务端缓存数据，可能不是最新。", LoadLenovoCatalogSnapshotAsync, ct);

    private static async Task<StoreCatalogSnapshot> LoadLenovoCatalogSnapshotAsync(CancellationToken ct)
    {
        var response = await GetAsync<StoreCatalogResponse>("store/lenovo/apps", ct, CatalogRequestTimeout).ConfigureAwait(false);
        if (response.Items is null || response.Total is null || response.Total < response.Items.Count)
        {
            throw new JsonException("联想商店目录响应缺少有效的 items 或 total 字段。");
        }

        if (!string.IsNullOrWhiteSpace(response.Source)
            && !string.Equals(response.Source, LenovoSourceId, StringComparison.Ordinal))
        {
            throw new JsonException("联想商店目录响应的来源与请求不一致。");
        }

        var normalized = MapStoreSummaries(response.Items, response.SourceName);
        if (normalized.Length == 0 && response.Items.Count > 0)
        {
            throw new JsonException("联想商店目录响应中没有可用的软件条目。");
        }

        return new StoreCatalogSnapshot(normalized, response.UpdatedAt, response.Stale);
    }

    private static Task<IReadOnlyList<AppItem>> GetSoft360AppsAsync(CancellationToken ct) =>
        GetStoreSourceAppsAsync(Soft360CatalogMapper.SourceName,
            "360软件管家目录当前为服务端缓存数据，可能不是最新。", LoadSoft360CatalogSnapshotAsync, ct);

    private static async Task<StoreCatalogSnapshot> LoadSoft360CatalogSnapshotAsync(CancellationToken ct)
    {
        var response = await GetAsync<Soft360CatalogResponse>("store/360/apps", ct, CatalogRequestTimeout).ConfigureAwait(false);
        if (response.Items is null || response.Total is null || response.Total < response.Items.Count)
        {
            throw new JsonException("360软件管家目录响应缺少有效的 items 或 total 字段。");
        }

        if (!string.IsNullOrWhiteSpace(response.Source)
            && !string.Equals(response.Source, Soft360SourceId, StringComparison.Ordinal))
        {
            throw new JsonException("360软件管家目录响应的来源与请求不一致。");
        }

        var normalized = MapSoft360Summaries(response.Items, response.SourceName);
        if (normalized.Length == 0 && response.Items.Count > 0)
        {
            throw new JsonException("360软件管家目录响应中没有可用的软件条目。");
        }

        return new StoreCatalogSnapshot(normalized, response.UpdatedAt, response.Stale);
    }

    // ---------------------------------------------------------------------------------------------
    // 详情
    // ---------------------------------------------------------------------------------------------

    public static async Task<AppItem> GetAppAsync(string id, CancellationToken ct = default, bool forceRefresh = false)
    {
        EscapeResourceId(id);
        EnsureInitialized();
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (_currentSource == AppSourcePreference.Lenovo)
            {
                return await GetLenovoAppAsync(id, forceRefresh, ct).ConfigureAwait(false);
            }

            if (_currentSource == AppSourcePreference.Soft360)
            {
                return await GetSoft360AppAsync(id, forceRefresh, ct).ConfigureAwait(false);
            }

            return await GetBuiltinAppAsync(id, forceRefresh, ct).ConfigureAwait(false);
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    private static async Task<AppItem> GetBuiltinAppAsync(string id, bool forceRefresh, CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        (AppItem App, DateTimeOffset Updated)? cached = Details.TryGetValue(id, out var detail) ? detail : null;
        var catalogApp = Apps.FirstOrDefault(app => app.Id == id);
        if (cached is null && catalogApp is not null && _catalogUpdatedAt.HasValue) cached = (catalogApp, _catalogUpdatedAt.Value);
        if (!forceRefresh && cached.HasValue && IsFresh(cached.Value.Updated, now))
        {
            await Store.AssignCachedIconsAsync(new[] { cached.Value.App }, ct).ConfigureAwait(false);
            SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline,
                cached.Value.Updated, Store.LastError, _lastNetworkError);
            QueueIcons(new[] { cached.Value.App }, _lastNetworkError is null);
            return cached.Value.App;
        }
        var disk = await ReadUsableDiskAsync(now, ct).ConfigureAwait(false);
        var diskApp = disk?.Items?.FirstOrDefault(app => app.Id == id);
        if (!forceRefresh && cached is null && diskApp is not null && disk!.IsFresh(now))
        {
            await Store.AssignCachedIconsAsync(new[] { diskApp }, ct).ConfigureAwait(false);
            Details[id] = (diskApp, disk.UpdatedAtUtc);
            SetCacheState(CatalogCacheSource.Disk, disk.UpdatedAtUtc, Store.LastError);
            QueueIcons(new[] { diskApp }, true);
            return diskApp;
        }
        try
        {
            var app = NormalizeApp(await GetAsync<AppItem>(WithRuntimeArchitecture($"apps/{EscapeResourceId(id)}"), ct).ConfigureAwait(false));
            if (app.Id != id) throw new JsonException("软件详情响应与请求的软件 ID 不一致。");
            ct.ThrowIfCancellationRequested();
            _lastNetworkError = null;
            Details[id] = (app, DateTimeOffset.UtcNow);
            await Store.AssignCachedIconsAsync(new[] { app }, ct).ConfigureAwait(false);
            if (catalogApp is not null)
            {
                Volatile.Write(ref _apps, Array.AsReadOnly(Apps.Select(item => item.Id == id ? app : item).ToArray()));
                if (_catalogUpdatedAt.HasValue) await Store.WriteCatalogAsync(Apps, _catalogUpdatedAt.Value, ct).ConfigureAwait(false);
                CatalogChanged?.Invoke(null, EventArgs.Empty);
            }
            SetCacheState(CatalogCacheSource.Network, Details[id].Updated, Store.LastError);
            QueueIcons(new[] { app }, true);
            return app;
        }
        catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested
            && (error is not HttpRequestException request || request.StatusCode is null || (int)request.StatusCode >= 500))
        {
            _lastNetworkError = FriendlyMetadataError(error);
            if (cached.HasValue && CanUseOffline(cached.Value.Updated, now))
            {
                SetCacheState(CatalogCacheSource.Offline, cached.Value.Updated, Store.LastError, _lastNetworkError);
                return cached.Value.App;
            }
            if (diskApp is not null)
            {
                await Store.AssignCachedIconsAsync(new[] { diskApp }, ct).ConfigureAwait(false);
                Details[id] = (diskApp, disk!.UpdatedAtUtc);
                SetCacheState(CatalogCacheSource.Offline, disk.UpdatedAtUtc, Store.LastError, _lastNetworkError);
                return diskApp;
            }
            SetCacheState(CacheState.Source, CacheState.UpdatedAtUtc, Store.LastError, _lastNetworkError);
            throw;
        }
    }

    private static async Task<AppItem> GetLenovoAppAsync(string id, bool forceRefresh, CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        // forceRefresh 只跳过客户端详情缓存，不会改变联想 GET URL。
        // 只有真正拉取过的详情才算新鲜缓存；目录里的 summary 不能当成详情（否则会缺截图/更新说明）。
        (AppItem App, DateTimeOffset Updated)? cached = Details.TryGetValue(id, out var detail) ? detail : null;
        var known = FindKnownApp(id);
        if (!forceRefresh && cached is { } hit && IsFresh(hit.Updated, now))
        {
            SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline,
                hit.Updated, Store.LastError, _lastNetworkError);
            return hit.App;
        }

        string? softId = ResolveSoftId(id, cached?.App ?? known);
        if (string.IsNullOrWhiteSpace(softId))
        {
            throw new InvalidDataException("未找到该应用对应的联想商店条目，请稍后重新加载软件列表后重试。");
        }

        try
        {
            var details = await GetAsync<StoreAppDetails>(
                $"store/lenovo/apps/{Uri.EscapeDataString(softId)}", ct, StoreRequestTimeout).ConfigureAwait(false);
            if (!string.IsNullOrWhiteSpace(details.Id) && !string.Equals(details.Id, id, StringComparison.Ordinal))
            {
                throw new JsonException("联想商店详情响应与请求的应用 ID 不一致。");
            }
            if (!string.IsNullOrWhiteSpace(details.SoftId)
                && !string.Equals(details.SoftId, softId, StringComparison.OrdinalIgnoreCase))
            {
                throw new JsonException("联想商店详情响应与请求的应用 soft_id 不一致。");
            }
            if (string.IsNullOrWhiteSpace(details.Id)) details.Id = id;
            if (string.IsNullOrWhiteSpace(details.SoftId)) details.SoftId = softId;

            var app = NormalizeApp(details.ToAppItem(cached?.App.SourceName));
            app.DisplayIconSource = (cached?.App ?? known)?.DisplayIconSource;
            ct.ThrowIfCancellationRequested();
            _lastNetworkError = null;
            Details[id] = (app, DateTimeOffset.UtcNow);
            RegisterKnownApp(app);
            // 单款详情只加载这一张图标，避免联想目录整体预热。
            await EnsureIconAsync(app, ct).ConfigureAwait(false);
            if (Apps.FirstOrDefault(item => item.Id == id) is not null)
            {
                Volatile.Write(ref _apps, Array.AsReadOnly(Apps.Select(item => item.Id == id ? app : item).ToArray()));
                if (_catalogUpdatedAt.HasValue) await Store.WriteCatalogAsync(Apps, _catalogUpdatedAt.Value, ct).ConfigureAwait(false);
                CatalogChanged?.Invoke(null, EventArgs.Empty);
            }
            SetCacheState(CatalogCacheSource.Network, Details[id].Updated, Store.LastError);
            return app;
        }
        catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested
            && (error is not HttpRequestException request || request.StatusCode is null || (int)request.StatusCode >= 500))
        {
            _lastNetworkError = FriendlyMetadataError(error);
            // 详情拉取失败时退回已缓存的详情或目录 summary，界面至少还能显示基本信息。
            var fallback = cached?.App ?? known;
            var fallbackUpdated = cached?.Updated ?? _catalogUpdatedAt;
            if (fallback is not null && CanUseOffline(fallbackUpdated, now))
            {
                SetCacheState(CatalogCacheSource.Offline, fallbackUpdated, Store.LastError, _lastNetworkError);
                return fallback;
            }
            SetCacheState(CacheState.Source, CacheState.UpdatedAtUtc, Store.LastError, _lastNetworkError);
            throw;
        }
    }

    private static async Task<AppItem> GetSoft360AppAsync(string id, bool forceRefresh, CancellationToken ct)
    {
        var now = DateTimeOffset.UtcNow;
        // 只有真正拉取过的详情才算新鲜缓存；目录摘要没有版本/截图/介绍，不能当成详情。
        (AppItem App, DateTimeOffset Updated)? cached = Details.TryGetValue(id, out var detail) ? detail : null;
        var known = FindKnownApp(id);
        if (!forceRefresh && cached is { } hit && IsFresh(hit.Updated, now))
        {
            SetCacheState(_lastNetworkError is null ? CatalogCacheSource.Memory : CatalogCacheSource.Offline,
                hit.Updated, Store.LastError, _lastNetworkError);
            // 详情页的通用下载路径依赖 LatestRelease 非空，详情缓存里不保存临时下载地址，这里现解析一次。
            return await AttachSoft360PresentationReleaseAsync(hit.App, ct).ConfigureAwait(false);
        }

        string? softId = ResolveSoftId(id, cached?.App ?? known);
        if (string.IsNullOrWhiteSpace(softId))
        {
            throw new InvalidDataException("未找到该应用对应的360软件管家条目，请稍后重新加载软件列表后重试。");
        }

        try
        {
            var details = await GetAsync<Soft360AppDetails>(
                $"store/360/apps/{Uri.EscapeDataString(softId)}", ct, StoreRequestTimeout).ConfigureAwait(false);
            if (!string.IsNullOrWhiteSpace(details.Id) && !string.Equals(details.Id, id, StringComparison.Ordinal))
            {
                throw new JsonException("360软件管家详情响应与请求的应用 ID 不一致。");
            }
            if (!string.IsNullOrWhiteSpace(details.SoftId)
                && !string.Equals(details.SoftId, softId, StringComparison.OrdinalIgnoreCase))
            {
                throw new JsonException("360软件管家详情响应与请求的应用 soft_id 不一致。");
            }
            if (string.IsNullOrWhiteSpace(details.Id)) details.Id = id;
            if (string.IsNullOrWhiteSpace(details.SoftId)) details.SoftId = softId;

            var app = NormalizeApp(details.ToAppItem());
            app.DisplayIconSource = (cached?.App ?? known)?.DisplayIconSource;
            ct.ThrowIfCancellationRequested();
            _lastNetworkError = null;
            Details[id] = (app, DateTimeOffset.UtcNow);
            RegisterKnownApp(app);
            // 单款详情只加载这一张图标，避免 360 目录整体预热。
            await EnsureIconAsync(app, ct).ConfigureAwait(false);
            if (Apps.FirstOrDefault(item => item.Id == id) is not null)
            {
                Volatile.Write(ref _apps, Array.AsReadOnly(Apps.Select(item => item.Id == id ? app : item).ToArray()));
                if (_catalogUpdatedAt.HasValue) await Store.WriteCatalogAsync(Apps, _catalogUpdatedAt.Value, ct).ConfigureAwait(false);
                CatalogChanged?.Invoke(null, EventArgs.Empty);
            }

            // 详情页没有 360 源分支：它的“开始下载”按钮在 LatestRelease 非空时走通用下载路径。
            // 返回给页面的副本附带一次实时解析的下载地址；详情缓存与目录保持无地址状态，
            // 临时下载地址绝不写入内存目录或磁盘缓存。解析失败只影响详情页直链展示，
            // 下载中心开始下载时会重新解析。
            SetCacheState(CatalogCacheSource.Network, Details[id].Updated, Store.LastError);
            return await AttachSoft360PresentationReleaseAsync(app, ct).ConfigureAwait(false);
        }
        catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested
            && (error is not HttpRequestException request || request.StatusCode is null || (int)request.StatusCode >= 500))
        {
            _lastNetworkError = FriendlyMetadataError(error);
            // 详情拉取失败时退回已缓存的详情或目录 summary，界面至少还能显示基本信息。
            var fallback = cached?.App ?? known;
            var fallbackUpdated = cached?.Updated ?? _catalogUpdatedAt;
            if (fallback is not null && CanUseOffline(fallbackUpdated, now))
            {
                SetCacheState(CatalogCacheSource.Offline, fallbackUpdated, Store.LastError, _lastNetworkError);
                return fallback;
            }
            SetCacheState(CacheState.Source, CacheState.UpdatedAtUtc, Store.LastError, _lastNetworkError);
            throw;
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 缓存清理
    // ---------------------------------------------------------------------------------------------

    public static async Task<CacheClearResult> ClearCacheAsync(CancellationToken ct = default)
    {
        EnsureInitialized();
        await CatalogGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            await CancelIconWarmupAsync().ConfigureAwait(false);
            foreach (var app in Apps.Concat(Details.Values.Select(value => value.App)).Concat(KnownApps.Values))
            {
                app.DisplayIconSource = null;
            }

            var errors = (await Store.ClearAsync(ct).ConfigureAwait(false)).ToList();
            foreach (var error in await StoreMediaService.ClearCacheAsync(ct).ConfigureAwait(false))
            {
                errors.Add(error);
            }
            bool localCleared = errors.Count == 0;

            Volatile.Write(ref _apps, Array.Empty<AppItem>());
            Volatile.Write(ref _hasLoaded, false);
            Volatile.Write(ref _hiddenIncompatible, 0);
            _catalogUpdatedAt = null;
            SetCatalogUpdatedAt(null);
            _lastNetworkError = null;
            Details.Clear();
            KnownApps.Clear();

            bool backendCleared = false;
            try
            {
                using var timeout = CancellationTokenSource.CreateLinkedTokenSource(ct);
                timeout.CancelAfter(TimeSpan.FromSeconds(12));
                // 后端 DELETE /cache 会同时清理内置源版本缓存与联想/360 源目录、详情、图片缓存（不会清 token）。
                using var response = await Client.DeleteAsync(BuildApiUri("cache"), timeout.Token).ConfigureAwait(false);
                response.EnsureSuccessStatusCode();
                backendCleared = true;
            }
            catch (Exception error) when (IsMetadataFailure(error) && !ct.IsCancellationRequested)
            {
                errors.Add("本地缓存已处理，但软件服务的版本缓存暂时无法清理。请连接服务后重试。");
            }

            SetCacheState(CatalogCacheSource.None, null, errors.Count > 0 ? string.Join(" ", errors) : null);
            CatalogChanged?.Invoke(null, EventArgs.Empty);
            return new CacheClearResult(localCleared, backendCleared, errors);
        }
        finally
        {
            CatalogGate.Release();
        }
    }

    public static async Task DiscardIconAsync(AppItem app)
    {
        var path = app.DisplayIconSource;
        app.DisplayIconSource = null;
        await Store.DiscardIconAsync(path).ConfigureAwait(false);
        CacheChanged?.Invoke(null, EventArgs.Empty);
    }

    /// <summary>
    /// 按需加载一款软件的图标（联想应用商店 / 360 软件管家的列表行使用）：先查磁盘缓存，未命中再直连 CDN，
    /// 失败回退后端代理；内部并发上限 6，失败静默返回 <see langword="false"/>，UI 继续显示文字徽章。
    /// </summary>
    public static async Task<bool> EnsureIconAsync(AppItem app, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(app);
        EnsureInitialized();
        if (!string.IsNullOrWhiteSpace(app.DisplayIconSource)) return true;
        if (string.IsNullOrWhiteSpace(app.IconUrl) && string.IsNullOrWhiteSpace(app.IconFallbackUrl)) return false;

        await IconDownloadGate.WaitAsync(ct).ConfigureAwait(false);
        try
        {
            if (!string.IsNullOrWhiteSpace(app.DisplayIconSource)) return true;
            // 联想 / 360 商店源的 icon_url/logo_url 是 CDN 绝对地址（直连），后端代理路径（icon_proxy_url）作为兜底；
            // 内置源只有后端 /icon 地址，EnumerateIconUrls 只会返回一项。
            foreach (string candidate in EnumerateIconUrls(app))
            {
                // CDN 单条失败（404/超时/坏图）不能阻止后端代理兜底，因此逐个候选单独捕获。
                try
                {
                    var cached = await Store.FindIconAsync(candidate, ct).ConfigureAwait(false);
                    if (!string.IsNullOrWhiteSpace(cached))
                    {
                        app.DisplayIconSource = cached;
                        return true;
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
                    or UnauthorizedAccessException or OperationCanceledException)
                {
                    // 继续尝试下一个候选地址。
                }
            }

            foreach (string candidate in EnumerateIconUrls(app))
            {
                try
                {
                    var path = await Store.GetIconAsync(candidate, ct).ConfigureAwait(false);
                    if (!string.IsNullOrWhiteSpace(path))
                    {
                        app.DisplayIconSource = path;
                        return true;
                    }
                }
                catch (OperationCanceledException) when (ct.IsCancellationRequested)
                {
                    throw;
                }
                catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
                    or UnauthorizedAccessException or OperationCanceledException)
                {
                    // 主地址失败时继续尝试 icon_proxy_url 兜底。
                }
            }

            return false;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception error) when (error is HttpRequestException or IOException or InvalidDataException
            or UnauthorizedAccessException or OperationCanceledException)
        {
            // 图标缺失不应影响列表加载。
            return false;
        }
        finally
        {
            IconDownloadGate.Release();
        }
    }

    /// <summary>图标候选地址：主地址（联想 CDN 直连 / 内置源后端图标）在前，兜底代理地址在后。</summary>
    private static IEnumerable<string> EnumerateIconUrls(AppItem app)
    {
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        if (!string.IsNullOrWhiteSpace(app.IconUrl) && seen.Add(app.IconUrl!))
        {
            yield return app.IconUrl!;
        }

        if (!string.IsNullOrWhiteSpace(app.IconFallbackUrl) && seen.Add(app.IconFallbackUrl!))
        {
            yield return app.IconFallbackUrl!;
        }
    }

    // ---------------------------------------------------------------------------------------------
    // 下载地址 / 在线搜索
    // ---------------------------------------------------------------------------------------------

    /// <summary>
    /// 按软件 ID 获取最新版下载地址。联想应用商店与 360 软件管家需要能根据 ID 找到源内 soft_id
    /// （目录/详情/在线搜索结果），内置精选源直接请求版本接口。
    /// </summary>
    public static async Task<AppRelease> GetLatestReleaseAsync(string appId, CancellationToken ct = default)
    {
        EnsureInitialized();
        if (_currentSource == AppSourcePreference.Lenovo)
        {
            BclCompat.ThrowIfNullOrWhiteSpace(appId);
            var known = FindKnownApp(appId)
                        ?? throw new InvalidDataException("未找到该应用对应的联想商店条目，请稍后重新加载软件列表后重试。");
            return await GetLenovoLatestReleaseAsync(known, ct).ConfigureAwait(false);
        }

        if (_currentSource == AppSourcePreference.Soft360)
        {
            BclCompat.ThrowIfNullOrWhiteSpace(appId);
            var known = FindKnownApp(appId)
                        ?? throw new InvalidDataException("未找到该应用对应的360软件管家条目，请稍后重新加载软件列表后重试。");
            return await GetSoft360LatestReleaseAsync(known, ct).ConfigureAwait(false);
        }

        var release = await GetAsync<AppRelease>(
            WithRuntimeArchitecture($"apps/{EscapeResourceId(appId)}/releases/latest"), ct).ConfigureAwait(false);
        release = ValidateRuntimeRelease(NormalizeRelease(release));
        if (string.Equals(release.Id, "latest", StringComparison.OrdinalIgnoreCase))
        {
            throw new JsonException("最新版本响应必须返回实际版本 ID。");
        }
        return release;
    }

    /// <summary>
    /// 按软件对象获取最新版下载地址。联想应用商店 / 360 软件管家直接用已知应用表里的 <c>soft_id</c> 请求后端实时解析，
    /// 因此列表/详情里的 <see cref="AppItem.LatestRelease"/> 为空也能开始下载。
    /// </summary>
    public static async Task<AppRelease> GetLatestReleaseAsync(AppItem app, CancellationToken ct = default)
    {
        ArgumentNullException.ThrowIfNull(app);
        EnsureInitialized();
        bool lenovoSource = _currentSource == AppSourcePreference.Lenovo;
        bool soft360Source = _currentSource == AppSourcePreference.Soft360;
        string sourceName = GetSourceDisplayName(_currentSource);
        string appName = string.IsNullOrWhiteSpace(app.Name) ? app.Id : app.Name;
        try
        {
            AppRelease release;
            if (lenovoSource)
            {
                release = await GetLenovoLatestReleaseAsync(app, ct).ConfigureAwait(false);
            }
            else if (soft360Source)
            {
                release = await GetSoft360LatestReleaseAsync(app, ct).ConfigureAwait(false);
            }
            else
            {
                release = await GetLatestReleaseAsync(app.Id, ct).ConfigureAwait(false);
            }
            AppLogService.Info(
                $"获取最新版本完成：{appName}（{app.Id}），版本={release.Version}，源={sourceName}。");
            return release;
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            AppLogService.Info($"获取最新版本已取消：{appName}（{app.Id}）。");
            throw;
        }
        catch (Exception exception) when (exception is not OutOfMemoryException)
        {
            AppLogService.Warn(
                $"获取最新版本失败：{appName}（{app.Id}），源={sourceName}，" +
                $"{exception.GetType().Name}：{exception.Message}",
                exception);
            throw;
        }
    }

    private static async Task<AppRelease> GetLenovoLatestReleaseAsync(AppItem app, CancellationToken ct)
    {
        BclCompat.ThrowIfNullOrWhiteSpace(app.Id);
        string? softId = ResolveSoftId(app.Id, app);
        if (string.IsNullOrWhiteSpace(softId))
        {
            throw new InvalidDataException("未找到该应用对应的联想商店条目，无法获取下载地址；请稍后重新加载软件列表后重试。");
        }

        // 联想 GET 路由不接受 refresh 查询参数：下载地址只带 architecture（客户端已无手动刷新入口）。
        var path = $"store/lenovo/apps/{Uri.EscapeDataString(softId)}/download" +
                   $"?architecture={Uri.EscapeDataString(RuntimeArchitecture)}";
        var response = await GetAsync<StoreRelease>(path, ct, StoreRequestTimeout).ConfigureAwait(false);
        var release = response.ToAppRelease()
                      ?? throw new InvalidDataException("联想应用商店没有返回可用的下载地址，该软件可能暂无安装包。");
        release = NormalizeRelease(release);
        if (!string.Equals(release.Platform, "windows", StringComparison.OrdinalIgnoreCase))
        {
            throw new JsonException("联想应用商店返回的安装包平台不是 Windows。");
        }
        if (!IsReleaseArchitectureCompatible(release.Architecture))
        {
            throw new JsonException($"安装包架构（{release.Architecture}）与当前 Windows 系统（{RuntimeArchitecture}）不一致。");
        }
        // 联想的 getPcSoftDownloadUrlList 不返回精确字节数：Release.size_bytes 取自目录 installFileSize
        // （安装体积，不是安装包字节数，官方说明只能用于展示）。把它当作精确大小交给下载器会误报
        // “安装包大小与官方资料不一致”，所以这里必须清空；真实大小由下载响应头实时确定，进度与校验不受影响。
        release.SizeBytes = null;
        return release;
    }

    private static async Task<AppRelease> GetSoft360LatestReleaseAsync(AppItem app, CancellationToken ct)
    {
        BclCompat.ThrowIfNullOrWhiteSpace(app.Id);
        string? softId = ResolveSoftId(app.Id, app);
        if (string.IsNullOrWhiteSpace(softId))
        {
            throw new InvalidDataException("未找到该应用对应的360软件管家条目，无法获取下载地址；请稍后重新加载软件列表后重试。");
        }

        // 360 下载地址是后端实时解析的（不缓存、no-store），这里只带 architecture 查询参数。
        var path = $"store/360/apps/{Uri.EscapeDataString(softId)}/download" +
                   $"?architecture={Uri.EscapeDataString(RuntimeArchitecture)}";
        var release = await GetAsync<AppRelease>(path, ct, StoreRequestTimeout).ConfigureAwait(false);
        release = NormalizeRelease(release);
        if (!string.Equals(release.Platform, "windows", StringComparison.OrdinalIgnoreCase))
        {
            throw new JsonException("360软件管家返回的安装包平台不是 Windows。");
        }

        // 360 的部分软件只提供 32 位安装包：x64 Windows 允许下载 x86 包（WOW64），其余按不兼容处理。
        if (!IsReleaseArchitectureCompatible(release.Architecture))
        {
            throw new JsonException($"安装包架构（{release.Architecture}）与当前 Windows 系统（{RuntimeArchitecture}）不一致。");
        }

        // 360 的 size 来自安装包大小（down.size / soft_size），可以作为下载器期望大小保留。
        return release;
    }

    /// <summary>详情加载时顺带解析下载地址：失败只记日志并返回 <see langword="null"/>，不影响详情展示。</summary>
    private static async Task<AppRelease?> TryGetSoft360LatestReleaseAsync(AppItem app, CancellationToken ct)
    {
        try
        {
            return await GetSoft360LatestReleaseAsync(app, ct).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (ct.IsCancellationRequested)
        {
            throw;
        }
        catch (Exception error) when (IsMetadataFailure(error))
        {
            AppLogService.Verbose(
                $"360软件管家下载地址暂时无法解析：{app.Name}（{app.Id}），{error.GetType().Name}：{error.Message}");
            return null;
        }
    }

    /// <summary>
    /// 返回带 <see cref="AppItem.LatestRelease"/> 的详情副本，供详情页复用通用下载流程。
    /// 只在副本上挂临时下载地址，避免它进入目录集合、详情缓存或磁盘缓存；解析失败时返回原对象。
    /// </summary>
    private static async Task<AppItem> AttachSoft360PresentationReleaseAsync(AppItem app, CancellationToken ct)
    {
        AppRelease? release = await TryGetSoft360LatestReleaseAsync(app, ct).ConfigureAwait(false);
        if (release is null)
        {
            return app;
        }

        AppItem? presented;
        try
        {
            presented = JsonSerializer.Deserialize<AppItem>(
                JsonSerializer.Serialize(app, JsonOptions), JsonOptions);
        }
        catch (JsonException)
        {
            return app;
        }

        if (presented is null)
        {
            return app;
        }

        // DisplayIconSource 带 [JsonIgnore]（它不是可持久化字段），复制时手动带过去。
        presented.DisplayIconSource = app.DisplayIconSource;
        presented.LatestRelease = release;
        return presented;
    }

    /// <summary>按版本 ID 获取下载地址。联想应用商店 / 360 软件管家只提供最新版本，因此不支持该操作。</summary>
    public static async Task<AppRelease> GetReleaseAsync(string appId, string releaseId, CancellationToken ct = default)
    {
        EnsureInitialized();
        if (_currentSource == AppSourcePreference.Lenovo)
        {
            throw new NotSupportedException("联想应用商店源只提供最新版本下载地址，请使用最新版本下载。");
        }

        if (_currentSource == AppSourcePreference.Soft360)
        {
            throw new NotSupportedException("360软件管家源只提供最新版本下载地址，请使用最新版本下载。");
        }

        var release = await GetAsync<AppRelease>(
            $"apps/{EscapeResourceId(appId)}/releases/{EscapeResourceId(releaseId)}", ct).ConfigureAwait(false);
        release = NormalizeRelease(release);
        if (!string.Equals(release.Id, releaseId, StringComparison.Ordinal))
        {
            throw new JsonException("软件版本响应与请求的版本 ID 不一致。");
        }
        return release;
    }

    /// <summary>
    /// 服务端搜索 API：联想应用商店调用 <c>/store/lenovo/search</c>，360 软件管家调用
    /// <c>/store/360/search</c>（服务端已覆盖全量目录，可能返回缓存目录之外的软件）。
    /// <para>
    /// 服务端 <c>/sources</c> 下发 <c>search_mode=api</c> 的源（360 软件管家）由列表页搜索框自动调用，
    /// 作为该源的主要搜索方式；<c>catalog</c> 模式的源只把它用于旧服务端兼容的“在线搜索”按钮。
    /// 返回应用摘要并登记“已知应用表”，后续详情/下载仍能按 <see cref="AppItem.Id"/> 找到 <c>soft_id</c>。
    /// 内置精选源没有搜索 API，返回空列表。
    /// </para>
    /// </summary>
    public static async Task<IReadOnlyList<AppItem>> SearchStoreAsync(string query, CancellationToken ct = default)
    {
        if (string.IsNullOrWhiteSpace(query)) return Array.Empty<AppItem>();
        EnsureInitialized();
        string text = query.Trim();
        return _currentSource switch
        {
            AppSourcePreference.Lenovo => await SearchLenovoStoreAsync(text, ct).ConfigureAwait(false),
            AppSourcePreference.Soft360 => await SearchSoft360StoreAsync(text, ct).ConfigureAwait(false),
            _ => Array.Empty<AppItem>()
        };
    }

    private static async Task<IReadOnlyList<AppItem>> SearchLenovoStoreAsync(string text, CancellationToken ct)
    {
        var path = $"store/lenovo/search?q={Uri.EscapeDataString(text)}&limit={StoreSearchLimit}";
        var response = await GetAsync<StoreSearchResponse>(path, ct, StoreRequestTimeout).ConfigureAwait(false);
        if (response.Items is null || response.Items.Count == 0) return Array.Empty<AppItem>();

        string sourceName = SourceDisplayName;
        var results = new List<AppItem>(response.Items.Count);
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var summary in response.Items)
        {
            AppItem app;
            try
            {
                app = NormalizeApp(summary.ToAppItem(sourceName));
            }
            catch (JsonException)
            {
                continue;
            }
            if (!ids.Add(app.Id)) continue;
            RegisterKnownApp(app);
            results.Add(app);
        }

        return results.AsReadOnly();
    }

    /// <summary>
    /// 360 软件管家在线搜索：响应字段与后端 <c>Soft360SearchResponse</c> 一致（source/query/total/items），
    /// 条目是 <see cref="Soft360AppSummary"/>，映射复用 <see cref="Soft360CatalogMapper.ToAppItem"/>。
    /// 结果可能不在缓存目录里，但登记“已知应用表”后详情/下载仍能按 <c>soft_id</c> 打开。
    /// </summary>
    private static async Task<IReadOnlyList<AppItem>> SearchSoft360StoreAsync(string text, CancellationToken ct)
    {
        var path = $"store/360/search?q={Uri.EscapeDataString(text)}&limit={StoreSearchLimit}";
        var response = await GetAsync<Soft360SearchResponse>(path, ct, StoreRequestTimeout).ConfigureAwait(false);
        if (!string.IsNullOrWhiteSpace(response.Source)
            && !string.Equals(response.Source, Soft360SourceId, StringComparison.Ordinal))
        {
            throw new JsonException("360软件管家搜索响应的来源与请求不一致。");
        }

        if (response.Items is null || response.Items.Count == 0) return Array.Empty<AppItem>();

        var results = new List<AppItem>(response.Items.Count);
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var summary in response.Items)
        {
            AppItem app;
            try
            {
                app = NormalizeApp(summary.ToAppItem());
            }
            catch (JsonException)
            {
                continue;
            }
            if (!ids.Add(app.Id)) continue;
            RegisterKnownApp(app);
            results.Add(app);
        }

        return results.AsReadOnly();
    }

    // ---------------------------------------------------------------------------------------------
    // 内部实现
    // ---------------------------------------------------------------------------------------------

    private static void EnsureInitialized()
    {
        if (Volatile.Read(ref _initialized)) return;
        lock (InitializationGate)
        {
            if (_initialized) return;
            // 保证先加载 settings.json，再读取软件源偏好（默认联想应用商店）。
            AppSettingsService.Initialize();
            _currentSource = NormalizeSource(AppSettingsService.Current.AppSource);
            _store = CreateStore(_currentSource);
            Volatile.Write(ref _initialized, true);
        }
    }

    private static AppSourcePreference NormalizeSource(AppSourcePreference source) =>
        Enum.IsDefined(source) ? source : AppSourcePreference.Lenovo;

    /// <summary>把 <c>api/v1/</c> 相对路径拼成当前服务端的绝对地址。</summary>
    private static Uri BuildApiUri(string path) => new(BackendUri, "api/v1/" + path.TrimStart('/'));

    /// <summary>
    /// 按当前软件源重建本地缓存：目录缓存 key 含源标识（lenovo / soft360 / builtin），
    /// 同一个后端在三个源之间切换时不会串用对方的目录缓存；图标按 URL 哈希共用同一目录。
    /// </summary>
    private static ClientCacheStore CreateStore(AppSourcePreference source) =>
        new(CacheRootPath, BackendBaseUrl, RuntimeArchitecture, SourceIdOf(source));

    /// <summary>软件源标识（与后端 <c>/api/v1/sources</c> 的 id 一致），用于隔离目录缓存。</summary>
    private static string SourceIdOf(AppSourcePreference source) => source switch
    {
        AppSourcePreference.Lenovo => LenovoSourceId,
        AppSourcePreference.Soft360 => Soft360SourceId,
        _ => BuiltinSourceId
    };

    private static ClientCacheStore Store
    {
        get
        {
            EnsureInitialized();
            return _store!;
        }
    }

    private static void RegisterKnownApp(AppItem app)
    {
        if (app is null || string.IsNullOrWhiteSpace(app.Id)) return;
        KnownApps[app.Id] = app;
    }

    private static AppItem? FindKnownApp(string id)
    {
        if (KnownApps.TryGetValue(id, out var registered)) return registered;
        if (Details.TryGetValue(id, out var detail)) return detail.App;
        return Apps.FirstOrDefault(item => item.Id == id);
    }

    private static string? ResolveSoftId(string id, AppItem? app)
    {
        if (!string.IsNullOrWhiteSpace(app?.SourceAppId)) return app!.SourceAppId;
        return FindKnownApp(id)?.SourceAppId;
    }

    private static AppItem[] MapStoreSummaries(IEnumerable<StoreAppSummary> items, string? sourceName)
    {
        string resolvedSourceName = string.IsNullOrWhiteSpace(sourceName) ? StoreCatalogMapper.LenovoSourceName : sourceName;
        var result = new List<AppItem>();
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var summary in items)
        {
            AppItem app;
            try
            {
                app = NormalizeApp(summary.ToAppItem(resolvedSourceName));
            }
            catch (JsonException)
            {
                // 单条脏数据不能拖垮整份 5000 条目录。
                continue;
            }
            if (!ids.Add(app.Id)) continue;
            RegisterKnownApp(app);
            result.Add(app);
        }

        return result.ToArray();
    }

    private static AppItem[] MapSoft360Summaries(IEnumerable<Soft360AppSummary> items, string? sourceName)
    {
        string resolvedSourceName = string.IsNullOrWhiteSpace(sourceName)
            ? Soft360CatalogMapper.SourceName : sourceName.Trim();
        var result = new List<AppItem>();
        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var summary in items)
        {
            AppItem app;
            try
            {
                app = NormalizeApp(summary.ToAppItem());
            }
            catch (JsonException)
            {
                // 单条脏数据不能拖垮整份约 6000 条的目录。
                continue;
            }

            app.SourceName = resolvedSourceName;
            if (!ids.Add(app.Id)) continue;
            RegisterKnownApp(app);
            result.Add(app);
        }

        return result.ToArray();
    }

    private static async Task PublishCatalogAsync(IEnumerable<AppItem> items, DateTimeOffset updated,
        CatalogCacheSource source, CancellationToken ct)
    {
        // 按当前系统过滤：32 位系统不显示只有 x64 安装包的软件，Windows 7 不显示要求 Win10/11 的软件。
        var compatible = new List<AppItem>();
        var hidden = 0;
        foreach (var item in items)
        {
            if (PlatformCompatibility.IsCompatible(item, RuntimeArchitecture, Environment.OSVersion.Version))
            {
                compatible.Add(item);
            }
            else
            {
                hidden++;
            }
        }

        // 联想应用商店 / 360 软件管家都是几千款的大目录：跳过批量磁盘图标分配与预热，由列表行按需加载图标。
        bool storeCatalog = IsStoreCatalogSource;
        Volatile.Write(ref _hiddenIncompatible, hidden);
        var snapshot = Array.AsReadOnly(compatible.ToArray());
        if (storeCatalog)
        {
            foreach (var app in snapshot) RegisterKnownApp(app);
        }
        else
        {
            await Store.AssignCachedIconsAsync(snapshot, ct).ConfigureAwait(false);
        }

        _catalogUpdatedAt = updated;
        Volatile.Write(ref _apps, snapshot);
        Volatile.Write(ref _hasLoaded, true);
        SetCacheState(source, updated, Store.LastError, source == CatalogCacheSource.Offline ? _lastNetworkError : null);
        CatalogChanged?.Invoke(null, EventArgs.Empty);
        if (!storeCatalog) QueueIcons(snapshot, source != CatalogCacheSource.Offline);
    }

    private static async Task<CatalogCacheDocument?> ReadUsableDiskAsync(DateTimeOffset now, CancellationToken ct)
    {
        var disk = await Store.ReadCatalogAsync(now, ct).ConfigureAwait(false);
        if (disk is null) return null;
        try
        {
            disk.Items = disk.Items!.Select(app => NormalizeApp(app, displayOnly: true)).ToList();
            ValidateUniqueIds(disk.Items);
            return disk;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static void ValidateUniqueIds(IEnumerable<AppItem> items)
    {
        var ids = items.Select(app => app.Id).ToArray();
        if (ids.Distinct(StringComparer.Ordinal).Count() != ids.Length)
            throw new JsonException("软件目录中包含重复的软件 ID。");
    }

    private static bool IsFresh(DateTimeOffset? updated, DateTimeOffset now) => updated.HasValue
        && now >= updated.Value && now - updated.Value < TimeSpan.FromMinutes(5);
    private static bool CanUseOffline(DateTimeOffset? updated, DateTimeOffset now) => updated.HasValue
        && now >= updated.Value && now - updated.Value <= TimeSpan.FromHours(24);
    private static bool IsMetadataFailure(Exception error) => error is HttpRequestException or IOException or JsonException or InvalidDataException or OperationCanceledException;
    private static string FriendlyMetadataError(Exception error) => error is OperationCanceledException
        ? "更新软件资料超时，请稍后重试。" : error is JsonException ? "软件服务返回的资料格式无效，请稍后重试。"
        : "更新软件资料失败，请检查软件服务和网络连接。";

    private static void SetCacheState(CatalogCacheSource source, DateTimeOffset? updated, string? warning = null, string? networkError = null)
    {
        Volatile.Write(ref _cacheState, new CatalogCacheState(source, updated, warning, networkError, SourceDisplayName));
        CacheChanged?.Invoke(null, EventArgs.Empty);
    }

    /// <summary>记录服务端 catalog.json 的 <c>updated_at</c>（UTC ticks，<see langword="null"/> 表示未知）。</summary>
    private static void SetCatalogUpdatedAt(DateTimeOffset? updatedAt) =>
        Volatile.Write(ref _catalogServerUpdatedAtTicks, updatedAt?.UtcDateTime.Ticks ?? UnknownCatalogTimestamp);

    private static void QueueIcons(IEnumerable<AppItem> items, bool allowNetwork)
    {
        if (!allowNetwork) return;
        IconTasks.RemoveAll(task => task.IsCompleted);
        IconTasks.Add(WarmIconsAsync(items.ToArray(), IconCancellation.Token));
    }

    private static async Task WarmIconsAsync(AppItem[] items, CancellationToken ct)
    {
        await Store.CacheMissingIconsAsync(items, ct).ConfigureAwait(false);
        if (!ct.IsCancellationRequested)
        {
            var state = CacheState;
            SetCacheState(state.Source, state.UpdatedAtUtc, Store.LastError ?? state.Warning, state.NetworkError);
        }
    }

    private static async Task CancelIconWarmupAsync()
    {
        IconCancellation.Cancel();
        var tasks = IconTasks.ToArray();
        IconTasks.Clear();
        try
        {
            await Task.WhenAll(tasks).ConfigureAwait(false);
        }
        catch (Exception error) when (error is OperationCanceledException or HttpRequestException or IOException)
        {
            // 图标预热失败/被取消都不影响切换源或清理缓存。
        }
        IconCancellation.Dispose();
        IconCancellation = new CancellationTokenSource();
    }

    private static bool IsReleaseArchitectureCompatible(string releaseArchitecture)
    {
        if (string.Equals(releaseArchitecture, RuntimeArchitecture, StringComparison.OrdinalIgnoreCase)) return true;
        // x64 Windows 通过 WOW64 运行 32 位安装包；arm64 只接受 arm64 包（其余由 x64 模拟处理）。
        return string.Equals(RuntimeArchitecture, "x64", StringComparison.OrdinalIgnoreCase)
               && string.Equals(releaseArchitecture, "x86", StringComparison.OrdinalIgnoreCase);
    }

    private static async Task<T> GetAsync<T>(string path, CancellationToken ct, TimeSpan? timeout = null) where T : class
    {
        using var requestCancellation = CancellationTokenSource.CreateLinkedTokenSource(ct);
        requestCancellation.CancelAfter(timeout ?? DefaultRequestTimeout);
        CancellationToken requestToken = requestCancellation.Token;
        using var response = await Client.GetAsync(BuildApiUri(path), HttpCompletionOption.ResponseHeadersRead, requestToken).ConfigureAwait(false);
        response.EnsureSuccessStatusCode();
        await using var stream = await response.Content.ReadAsStreamAsync(requestToken).ConfigureAwait(false);
        return await JsonSerializer.DeserializeAsync<T>(stream, JsonOptions, requestToken).ConfigureAwait(false)
            ?? throw new JsonException("软件目录服务返回了空响应。");
    }

    private static AppItem NormalizeApp(AppItem app, bool displayOnly = false)
    {
        if (app is null || string.IsNullOrWhiteSpace(app.Id) || string.IsNullOrWhiteSpace(app.Name) || app.Id is "." or "..")
        {
            throw new JsonException("软件目录中的软件缺少 ID 或名称。");
        }

        app.Category = string.IsNullOrWhiteSpace(app.Category) ? "未分类" : app.Category;
        app.Tags ??= Array.Empty<string>();
        app.Features ??= Array.Empty<string>();
        app.Labels ??= Array.Empty<string>();
        app.DetailTags ??= Array.Empty<AppDetailTag>();
        app.Screenshots ??= Array.Empty<AppScreenshot>();
        app.IconUrl = ResolveHttpUrl(app.IconUrl, allowEmpty: true);
        // 联想源 icon_proxy_url 是相对后端路径，统一解析成绝对地址（后端地址可在设置中修改）。
        app.IconFallbackUrl = ResolveOptionalHttpUrl(app.IconFallbackUrl);
        app.WebsiteUrl = ResolveHttpUrl(app.WebsiteUrl, allowEmpty: true);
        app.DownloadPageUrl = ResolveHttpUrl(app.DownloadPageUrl, allowEmpty: true);
        app.CoverUrl = ResolveOptionalHttpUrl(app.CoverUrl);
        // 封面兜底是后端代理路径（相对 BaseUrl），与截图/图标的 proxy_url 一样统一解析成绝对地址。
        app.CoverProxyUrl = ResolveOptionalHttpUrl(app.CoverProxyUrl);
        if (app.Screenshots.Count > 0)
        {
            var screenshots = new List<AppScreenshot>(app.Screenshots.Count);
            foreach (var screenshot in app.Screenshots)
            {
                if (screenshot is null || string.IsNullOrWhiteSpace(screenshot.Url)) continue;
                string? url = ResolveOptionalHttpUrl(screenshot.Url);
                if (url is null) continue;
                screenshots.Add(new AppScreenshot
                {
                    Index = screenshot.Index,
                    Kind = string.Equals(screenshot.Kind, "video", StringComparison.OrdinalIgnoreCase) ? "video" : "image",
                    Url = url,
                    // 直连 CDN 失败时的后端代理兜底；旧服务端没有该字段（null）时保持 null。
                    ProxyUrl = ResolveOptionalHttpUrl(screenshot.ProxyUrl)
                });
            }
            app.Screenshots = screenshots.Count == 0 ? Array.Empty<AppScreenshot>() : screenshots.AsReadOnly();
        }

        if (app.LatestRelease is not null)
        {
            app.LatestRelease = ValidateRuntimeRelease(NormalizeRelease(app.LatestRelease, displayOnly));
        }

        return app;
    }

    private static AppRelease NormalizeRelease(AppRelease release, bool displayOnly = false)
    {
        if (string.IsNullOrWhiteSpace(release.Id) || string.IsNullOrWhiteSpace(release.Version) ||
            string.IsNullOrWhiteSpace(release.FileName) || release.Id is "." or "..")
        {
            throw new JsonException("软件版本信息缺少 ID、版本号或安装包文件名。");
        }

        release.DownloadUrl = ResolveHttpUrl(release.DownloadUrl, allowEmpty: displayOnly);
        return release;
    }

    private static AppRelease ValidateRuntimeRelease(AppRelease release)
    {
        if (!string.Equals(release.Platform, "windows", StringComparison.OrdinalIgnoreCase) ||
            !string.Equals(release.Architecture, RuntimeArchitecture, StringComparison.OrdinalIgnoreCase))
        {
            throw new JsonException("安装包平台或架构与当前 Windows 系统不一致。");
        }
        return release;
    }

    /// <summary>
    /// 内置精选源 GET 的查询串：只追加 <c>architecture</c>。
    /// <para>
    /// 客户端已移除所有手动刷新入口，因此这里不再拼接 <c>refresh</c> 查询参数；联想源 GET 路由同样
    /// 不接受任何刷新参数（目录由后端定时线程自动更新）。禁止把刷新参数带回任何 GET 请求。
    /// </para>
    /// </summary>
    private static string WithRuntimeArchitecture(string path) =>
        $"{path}?architecture={Uri.EscapeDataString(RuntimeArchitecture)}";

    private static string? ResolveOptionalHttpUrl(string? value)
    {
        if (string.IsNullOrWhiteSpace(value)) return null;
        try
        {
            return ResolveHttpUrl(value, allowEmpty: false);
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string ResolveHttpUrl(string? value, bool allowEmpty)
    {
        if (string.IsNullOrWhiteSpace(value))
        {
            return allowEmpty ? string.Empty : throw new JsonException("软件版本信息缺少下载地址。");
        }

        if (!Uri.TryCreate(BackendUri, value, out var uri) ||
            (uri.Scheme != Uri.UriSchemeHttp && uri.Scheme != Uri.UriSchemeHttps))
        {
            throw new JsonException("软件目录中的网址格式无效。");
        }

        return uri.AbsoluteUri;
    }

    private static string EscapeResourceId(string id)
    {
        BclCompat.ThrowIfNullOrWhiteSpace(id);
        if (id is "." or "..")
        {
            throw new ArgumentException("资源 ID 不能是路径片段。", nameof(id));
        }

        return Uri.EscapeDataString(id);
    }

    /// <summary>一次目录拉取结果：映射后的条目、服务端快照的 updated_at、以及服务端 stale 标记。</summary>
    private readonly record struct StoreCatalogSnapshot(AppItem[] Items, DateTimeOffset? ServerUpdatedAt, bool Stale);

    private sealed class CatalogResponse
    {
        public List<AppItem>? Items { get; set; }
        public int? Total { get; set; }
    }
}
